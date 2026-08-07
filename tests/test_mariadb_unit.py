"""The MariaDB backend's decisions, pinned against a fake driver.

No MariaDB is needed here and none is used: what these tests prove is
that the backend SENDS the right things — connect kwargs, statement
rewrites, the ping policy, the version refusals. Whether a real server
ACCEPTS them is the job of tests/test_mariadb_backend.py (which only
exists when TESTBOARD_TEST_DB_CNF points at a server) and of the CI
mariadb:10.3 job. The split matters: these tests fail fast on a broken
decision; those fail honestly on a broken assumption.

Python 3.6 compatible; standard library only.
"""

import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from testboard import mariadb
from testboard import storage as storage_module
from testboard.dbconfig import Settings
from testboard.storage import MIGRATIONS, Storage

SETTINGS = Settings(host="db.example", port=3306, user="testboard_app",
                    password="hunter2", database="testboard",
                    unix_socket=None)

LATEST = MIGRATIONS[-1][0]


class FakeCursor(object):
    """Records executes; answers from a scripted {substring: rows} map."""

    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._rows = []  # type: List[Tuple[Any, ...]]
        self.lastrowid = 1
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        for pattern, outcome in self._conn.script:
            if pattern in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                self._rows = list(outcome)
                return
        self._rows = []

    def executemany(self, sql: str, seq: Any) -> None:
        self._conn.executed.append((sql, list(seq)))

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeConnection(object):
    def __init__(self, script: Sequence[Tuple[str, Any]]) -> None:
        self.script = list(script)
        self.executed = []  # type: List[Tuple[str, Any]]
        self.pings = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        self.pings += 1

    def close(self) -> None:
        self.closed = True


#: A server whose answers pass every construction-time check.
HEALTHY = [
    ("@@sql_mode", [("STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",)]),
    ("SELECT version FROM schema_version", [(LATEST,)]),
]


class Recorder(object):
    """Stands in for the vendored driver; records connect kwargs."""

    class IntegrityError(Exception):
        pass

    class ProgrammingError(Exception):
        pass

    class OperationalError(Exception):
        pass

    def __init__(self, script: Sequence[Tuple[str, Any]]) -> None:
        self.script = list(script)
        self.connect_kwargs = []  # type: List[Dict[str, Any]]
        self.connections = []  # type: List[FakeConnection]

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(dict(kwargs))
        conn = FakeConnection(self.script)
        self.connections.append(conn)
        return conn


class BackendCase(unittest.TestCase):
    """Base: a MariaDBBackend over a scripted fake driver."""

    def backend(self, script: Optional[Sequence[Tuple[str, Any]]] = None,
                settings: Settings = SETTINGS
                ) -> Tuple[mariadb.MariaDBBackend, Recorder]:
        recorder = Recorder(HEALTHY if script is None else script)
        real = mariadb._driver
        mariadb._driver = lambda: recorder
        self.addCleanup(setattr, mariadb, "_driver", real)
        return mariadb.MariaDBBackend(settings), recorder


class ConnectTest(BackendCase):
    """Every connect kwarg is load-bearing; pin each one."""

    def test_the_load_bearing_kwargs(self) -> None:
        from third_party.pymysql.constants import CLIENT
        backend, recorder = self.backend()
        backend.raw_connect()
        kwargs = recorder.connect_kwargs[0]
        self.assertEqual(kwargs["charset"], "utf8mb4")
        self.assertTrue(kwargs["autocommit"])
        self.assertTrue(kwargs["client_flag"] & CLIENT.FOUND_ROWS,
                        "without FOUND_ROWS, UPDATE reports rows CHANGED "
                        "and the UPDATE-then-INSERT sites duplicate-key")
        self.assertTrue(kwargs["binary_prefix"],
                        "zlib blobs are not valid utf8mb4")
        self.assertIn("innodb_lock_wait_timeout",
                      kwargs["init_command"])
        self.assertEqual(kwargs["host"], "db.example")
        self.assertEqual(kwargs["port"], 3306)

    def test_a_socket_replaces_host_and_port(self) -> None:
        backend, recorder = self.backend(
            settings=SETTINGS._replace(unix_socket="/run/mysql.sock"))
        backend.raw_connect()
        kwargs = recorder.connect_kwargs[0]
        self.assertEqual(kwargs["unix_socket"], "/run/mysql.sock")
        self.assertNotIn("host", kwargs)

    def test_a_lax_server_is_refused_at_connect(self) -> None:
        """The data was loaded under strict mode; serving without it
        silently truncates over-long values into collisions."""
        backend, _ = self.backend(
            [("@@sql_mode", [("NO_ENGINE_SUBSTITUTION",)])])
        with self.assertRaises(RuntimeError) as caught:
            backend.raw_connect()
        self.assertIn("strict", str(caught.exception).lower())
        self.assertIn("A.5", str(caught.exception))

    def test_the_refusal_closes_the_connection(self) -> None:
        backend, recorder = self.backend(
            [("@@sql_mode", [("NO_ENGINE_SUBSTITUTION",)])])
        with self.assertRaises(RuntimeError):
            backend.raw_connect()
        self.assertTrue(recorder.connections[0].closed)


class TranslateTest(BackendCase):
    """The rewrite table, row by row."""

    def test_qmark_becomes_pyformat(self) -> None:
        backend, _ = self.backend()
        self.assertEqual(
            backend.translate("SELECT a FROM t WHERE b = ? AND c = ?"),
            "SELECT a FROM t WHERE b = %s AND c = %s")

    def test_literal_percents_are_doubled_before_placeholders(self) -> None:
        """Order matters: doubling after ?→%s would double the
        placeholders themselves."""
        backend, _ = self.backend()
        self.assertEqual(
            backend.translate("SELECT '100%' FROM t WHERE a = ?"),
            "SELECT '100%%' FROM t WHERE a = %s")

    def test_begin_immediate_becomes_start_transaction(self) -> None:
        backend, _ = self.backend()
        self.assertEqual(backend.translate("BEGIN IMMEDIATE"),
                         "START TRANSACTION")

    def test_commit_and_rollback_pass_through(self) -> None:
        backend, _ = self.backend()
        self.assertEqual(backend.translate("COMMIT"), "COMMIT")
        self.assertEqual(backend.translate("ROLLBACK"), "ROLLBACK")

    def test_insert_or_replace_becomes_replace(self) -> None:
        backend, _ = self.backend()
        self.assertEqual(
            backend.translate(
                "INSERT OR REPLACE INTO run_outputs (run_id, output) "
                "VALUES (?, ?)"),
            "REPLACE INTO run_outputs (run_id, output) VALUES (%s, %s)")

    def test_a_plain_insert_is_untouched(self) -> None:
        """The prefix match must not fire inside ordinary INSERTs."""
        backend, _ = self.backend()
        self.assertEqual(
            backend.translate("INSERT INTO users (username) VALUES (?)"),
            "INSERT INTO users (username) VALUES (%s)")

    def test_the_translation_is_cached(self) -> None:
        backend, _ = self.backend()
        sql = "SELECT 1 FROM t WHERE a = ?"
        first = backend.translate(sql)
        self.assertIs(first, backend.translate(sql))

    def test_the_two_real_rewrite_sites_translate_cleanly(self) -> None:
        """Run the actual INSERT OR REPLACE statements from storage.py
        through the translator, not lookalikes."""
        import ast
        import io as io_module
        with io_module.open(storage_module.__file__.replace(".pyc", ".py"),
                            encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        found = 0
        backend, _ = self.backend()
        for node in ast.walk(tree):
            value = None
            if hasattr(ast, "Constant") and isinstance(
                    node, ast.Constant) and isinstance(node.value, str):
                value = node.value
            elif hasattr(ast, "Str") and isinstance(node, ast.Str):
                value = node.s
            # startswith, not contains: docstrings MENTION the phrase in
            # prose; only the two real SQL literals BEGIN with it.
            if value and value.lstrip().startswith("INSERT OR REPLACE"):
                found += 1
                translated = backend.translate(value)
                self.assertNotIn("INSERT OR REPLACE", translated)
                self.assertIn("REPLACE INTO", translated)
                self.assertNotIn("?", translated)
        self.assertEqual(found, 2, "expected exactly the two pinned sites")


class FragmentTest(BackendCase):
    """The dialect fragments composed into SQL at composition time."""

    def test_the_limit_fragment_spells_no_limit_the_mariadb_way(
            self) -> None:
        backend, _ = self.backend()
        self.assertIn("18446744073709551615", backend.limit_all_offset)
        self.assertNotIn("-1", backend.limit_all_offset)

    def test_the_like_fragment_is_case_insensitive_with_wire_escape(
            self) -> None:
        """COLLATE keeps search case-insensitive over _bin columns; the
        wire must carry ESCAPE '\\\\' because MariaDB string literals
        process backslashes (SQLite's spelling is ESCAPE '\\')."""
        backend, _ = self.backend()
        self.assertIn("COLLATE utf8mb4_general_ci", backend.like_test_name)
        self.assertIn("ESCAPE '\\\\'", backend.like_test_name)

    def test_the_integrity_error_is_the_drivers(self) -> None:
        backend, recorder = self.backend()
        self.assertIs(backend.integrity_error, Recorder.IntegrityError)


class PingPolicyTest(BackendCase):
    """Reconnect protection without ever retrying a statement."""

    def connection(self, clock_values: List[float]
                   ) -> Tuple[mariadb._Connection, FakeConnection]:
        backend, recorder = self.backend()
        clock = iter(clock_values)
        conn = mariadb._Connection(backend, clock=lambda: next(clock))
        return conn, recorder.connections[0]

    def test_a_fresh_connection_is_not_pinged(self) -> None:
        conn, raw = self.connection([0.0, 1.0])
        conn.execute("SELECT 1")
        self.assertEqual(raw.pings, 0)

    def test_an_idle_connection_is_pinged_before_use(self) -> None:
        conn, raw = self.connection(
            [0.0, mariadb.PING_IDLE_SECONDS + 1.0])
        conn.execute("SELECT 1")
        self.assertEqual(raw.pings, 1)

    def test_never_inside_a_transaction(self) -> None:
        """A mid-transaction reconnect would silently commit half of
        it. The transaction fails instead, exactly as SQLite's does."""
        conn, raw = self.connection(
            [0.0, 1.0, mariadb.PING_IDLE_SECONDS * 3,
             mariadb.PING_IDLE_SECONDS * 4])
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT 1")   # long after; still no ping
        self.assertEqual(raw.pings, 0)

    def test_commit_ends_the_transaction_for_the_policy(self) -> None:
        conn, raw = self.connection(
            [0.0, 1.0, 2.0, mariadb.PING_IDLE_SECONDS * 3])
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("COMMIT")
        conn.execute("SELECT 1")
        self.assertEqual(raw.pings, 1)


class SchemaCheckTest(BackendCase):
    """The backend verifies the schema; it never creates it."""

    def storage_with(self, script: Sequence[Tuple[str, Any]]) -> Storage:
        backend, _ = self.backend(script)
        real = mariadb.MariaDBBackend
        mariadb.MariaDBBackend = lambda settings: backend  # type: ignore
        self.addCleanup(setattr, mariadb, "MariaDBBackend", real)
        return Storage.mariadb(SETTINGS)

    def test_a_matching_version_constructs(self) -> None:
        store = self.storage_with(HEALTHY)
        self.assertIsNone(store.cache_bytes_per_connection())

    def test_an_older_schema_points_at_the_migration_tooling(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.storage_with(
                [("@@sql_mode", [("STRICT_TRANS_TABLES",)]),
                 ("SELECT version FROM schema_version", [(LATEST - 1,)])])
        self.assertIn("MARIADB_MIGRATION.md", str(caught.exception))
        self.assertIn("never migrates", str(caught.exception))

    def test_a_newer_schema_reuses_the_pinned_wording(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.storage_with(
                [("@@sql_mode", [("STRICT_TRANS_TABLES",)]),
                 ("SELECT version FROM schema_version", [(LATEST + 1,)])])
        self.assertIn("NEWER version", str(caught.exception))

    def test_a_database_with_no_schema_names_the_runbook_step(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.storage_with(
                [("@@sql_mode", [("STRICT_TRANS_TABLES",)]),
                 ("SELECT version FROM schema_version",
                  Recorder.ProgrammingError("1146: no such table"))])
        self.assertIn("migration tooling", str(caught.exception))

    def test_no_ddl_is_ever_sent(self) -> None:
        """The whole point of runs_migrations=False."""
        store = self.storage_with(HEALTHY)
        conn = store._conn()
        for sql, _params in conn._raw.executed:
            self.assertNotIn("CREATE TABLE", sql)
            self.assertNotIn("ALTER TABLE", sql)


class CapabilityTest(BackendCase):
    """The branch points Storage consults."""

    def test_vacuum_is_a_noop(self) -> None:
        backend, recorder = self.backend()
        conn = backend.connect()
        backend.vacuum(conn)
        executed = [sql for sql, _ in recorder.connections[0].executed]
        self.assertNotIn("VACUUM", executed)

    def test_cache_arithmetic_is_declared_not_applicable(self) -> None:
        backend, _ = self.backend()
        self.assertIsNone(backend.cache_bytes_per_connection())

    def test_executemany_is_translated_too(self) -> None:
        backend, recorder = self.backend()
        conn = backend.connect()
        conn.executemany("UPDATE t SET a = ? WHERE b = ?",
                         [(1, 2), (3, 4)])
        sql, params = recorder.connections[0].executed[-1]
        self.assertEqual(sql, "UPDATE t SET a = %s WHERE b = %s")
        self.assertEqual(params, [(1, 2), (3, 4)])


class ConnectErrorTest(unittest.TestCase):
    """Startup failures must be actionable and never leak the password."""

    def test_the_sha256_case_forbids_the_install(self) -> None:
        message = mariadb.describe_connect_error(
            SETTINGS, RuntimeError(
                "'cryptography' package is required for sha256_password"))
        self.assertIn("mysql_native_password", message)
        self.assertIn("Do NOT install", message)
        self.assertNotIn("hunter2", message)

    def test_access_denied_explains_host_matching_and_the_socket(
            self) -> None:
        message = mariadb.describe_connect_error(
            SETTINGS, OSError("(1045, \"Access denied\")"))
        self.assertIn("localhost", message)
        self.assertIn("socket", message)
        self.assertNotIn("hunter2", message)


if __name__ == "__main__":
    unittest.main()
