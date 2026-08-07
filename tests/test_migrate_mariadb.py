"""The migration script's gates must actually stop the migration.

``tools/migrate_to_mariadb.py`` exists because a procedure made of SQL
a person types is a procedure whose checks get skipped at 2am. That is
only an improvement if the automated checks are *harder* to skip than
the manual ones, so what is tested here is mostly refusal: an audit that
finds orphan rows, a preflight that finds a case-insensitive collation,
a verification that finds a row-count difference — each must return a
non-zero exit code rather than a paragraph of output the operator can
read past.

**What this file cannot cover.** There is no MariaDB here or in CI, so
every server interaction is driven through a fake connection
returning scripted answers. That proves the *decisions* are right — this
answer means stop, that answer means proceed — and proves nothing about
whether the SQL is accepted by a real server. The runbook's §E.1 dry run
against a copy of production is what covers the other half, and no
result in this file is a substitute for running it.

Python 3.6 compatible; standard library only.
"""

import datetime
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from testboard.model import Result, RunRecord
from testboard.storage import Storage
from tools import export_for_mariadb as exporter
from tools import migrate_to_mariadb as migrate

START = datetime.datetime(2026, 7, 25, 2, 0, 0)


def seed(db_path: str) -> Storage:
    """A small but complete database: two runs, a user, a comment."""
    store = Storage(db_path)
    store.upsert_runs([
        RunRecord(
            environment="linux sim", script="suite/a b.py",
            test_name="test_weird [1]\twith tab", result=Result.FAIL,
            start_time=START, end_time=START + datetime.timedelta(seconds=3),
            output="line one\nline two\ttabbed\ncafé 🙂", source_link="",
            known_failure_reason=None),
        RunRecord(
            environment="linux sim", script="suite/a b.py",
            test_name="plain", result=Result.PASS,
            start_time=START, end_time=START + datetime.timedelta(seconds=1),
            output="", source_link="http://x/y",
            known_failure_reason="known"),
    ])
    store.ensure_user("alice", START)
    store.add_comment("linux sim", "suite/a b.py", "plain", "alice",
                      "a comment", START)
    return store


class FakeServer(object):
    """A stand-in for the MariaDB connection. Every answer is scripted.

    Substring matching on the SQL rather than exact text: what is being
    tested is the decision taken on a given *answer*, and pinning the
    exact wording of every statement would make this file a
    change-detector instead of a test.

    Values pass through untouched, because the driver hands back native
    Python types — ``int``, ``Decimal``, ``bytes``, ``None`` — and it is
    that variety the comparison in ``render_rows`` has to absorb. A fake
    that stringified everything first would hide the bug it exists to
    catch.
    """

    def __init__(self, script: Sequence[Tuple[str, Any]]) -> None:
        self.script = list(script)
        self.executed = []  # type: List[str]

    def _answer(self, sql: str) -> Any:
        self.executed.append(sql)
        for pattern, outcome in self.script:
            if pattern in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return []

    def rows(self, sql: str) -> List[Tuple[Any, ...]]:
        return [tuple(row) for row in self._answer(sql)]

    def run(self, sql: str) -> None:
        self._answer(sql)

    def close(self) -> None:
        pass


#: A server that passes every gate, as the baseline each test bends.
HEALTHY = [
    ("SELECT VERSION()", [("10.6.16-MariaDB", "STRICT_TRANS_TABLES",
                           134217728, "ON", "utf8mb4",
                           "utf8mb4_nopad_bin")]),
    ("COUNT(*) FROM _tb_collation_probe", [(3,)]),
    ("INSERT INTO _tb_strict_probe", ValueError("Data too long")),
    ("SHOW TABLES", []),
]


class OptionFileTest(unittest.TestCase):
    """Credentials come from a file, never from a command line."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text: str) -> str:
        path = os.path.join(self.tmp, "my.cnf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_it_reads_the_mysql_client_format(self) -> None:
        path = self.write(
            "[client]\n"
            "host = dbhost.example\n"
            "port = 3307\n"
            "user = testboard_migrate\n"
            "password = s3cret\n"
            "database = testboard\n")
        settings = migrate.read_option_file(path)
        self.assertEqual(settings.host, "dbhost.example")
        self.assertEqual(settings.port, 3307)
        self.assertEqual(settings.user, "testboard_migrate")
        self.assertEqual(settings.password, "s3cret")
        self.assertEqual(settings.database, "testboard")

    def test_a_bare_key_does_not_break_the_parse(self) -> None:
        """my.cnf allows valueless keys; configparser rejects them.

        That is why this is hand-parsed — an option file copied from
        the server's own /etc/my.cnf must not fail to load.
        """
        path = self.write(
            "!includedir /etc/my.cnf.d\n"
            "[client]\n"
            "local-infile\n"
            "user=u\npassword=p\ndatabase=d\n")
        settings = migrate.read_option_file(path)
        self.assertEqual(settings.user, "u")

    def test_quotes_are_stripped_as_the_client_strips_them(self) -> None:
        path = self.write("[client]\nuser=u\npassword=\"p a s s\"\n"
                          "database=d\n")
        self.assertEqual(migrate.read_option_file(path).password, "p a s s")

    def test_a_password_with_a_hash_survives(self) -> None:
        """A '#' mid-line is part of the value, not a comment."""
        path = self.write("[client]\nuser=u\npassword=aa#bb\ndatabase=d\n")
        self.assertEqual(migrate.read_option_file(path).password, "aa#bb")

    def test_other_sections_are_ignored(self) -> None:
        """[mysqld] belongs to the server; reading it would pick up the

        server's own datadir/user settings as if they were ours."""
        path = self.write(
            "[mysqld]\nuser=mysql\n[client]\nuser=u\npassword=p\n"
            "database=d\n")
        self.assertEqual(migrate.read_option_file(path).user, "u")

    def test_a_missing_password_is_refused_with_the_fix(self) -> None:
        path = self.write("[client]\nuser=u\ndatabase=d\n")
        with self.assertRaises(SystemExit) as caught:
            migrate.read_option_file(path)
        self.assertIn("password", str(caught.exception))

    def test_a_missing_file_names_the_runbook_section(self) -> None:
        """The fix is a file to write, so say which section writes it."""
        with self.assertRaises(SystemExit) as caught:
            migrate.read_option_file(os.path.join(self.tmp, "nope.cnf"))
        self.assertIn("A.9", str(caught.exception))

    def test_the_description_never_contains_the_password(self) -> None:
        """It is printed at every connect, into logs somebody keeps."""
        path = self.write("[client]\nuser=u\npassword=hunter2\n"
                          "database=d\n")
        described = migrate.read_option_file(path).describe()
        self.assertNotIn("hunter2", described)
        self.assertIn("u@", described)


class SizingTest(unittest.TestCase):
    """VARCHAR sizes come from the data, and stay inside InnoDB's limit."""

    def test_small_values_get_the_documented_defaults(self) -> None:
        sizes = migrate.choose_sizes(
            {"environment": 13, "script": 28, "test_name": 26})
        self.assertEqual(sizes, dict(migrate.MIN_SIZES))

    def test_a_long_value_widens_the_column(self) -> None:
        """A script path longer than the default is a data fact, not a

        reason to truncate: the column grows to fit with room over."""
        sizes = migrate.choose_sizes(
            {"environment": 13, "script": 300, "test_name": 26})
        self.assertGreaterEqual(sizes["script"], 320)
        self.assertLessEqual(
            exporter.Sizes(sizes["environment"], sizes["script"],
                           sizes["test_name"]).index_bytes(), 3072)

    def test_padding_is_given_up_before_the_migration_is(self) -> None:
        """The three columns share 761 characters of index key, so a

        long script path costs the others their headroom. Tightening is
        right; refusing to migrate data that fits would not be."""
        sizes = migrate.choose_sizes(
            {"environment": 13, "script": 500, "test_name": 26})
        self.assertGreaterEqual(sizes["script"], 500)
        self.assertLessEqual(
            exporter.Sizes(sizes["environment"], sizes["script"],
                           sizes["test_name"]).index_bytes(), 3072)

    def test_values_that_cannot_be_indexed_at_all_are_a_hard_stop(
            self) -> None:
        """Silently shrinking to fit would truncate the very values that

        triggered it — under strict mode that is a failed load, and
        without it, two tests merged into one."""
        with self.assertRaises(SystemExit) as caught:
            migrate.choose_sizes(
                {"environment": 13, "script": 400, "test_name": 400})
        self.assertIn("3072", str(caught.exception))

    def test_the_chosen_sizes_are_ones_the_exporter_accepts(self) -> None:
        sizes = migrate.choose_sizes(
            {"environment": 60, "script": 120, "test_name": 40})
        budget = exporter.Sizes(sizes["environment"], sizes["script"],
                                sizes["test_name"])
        self.assertLessEqual(budget.index_bytes(), 3072)


class AuditTest(unittest.TestCase):
    """The read-only source audit, against a real SQLite file."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = seed(self.db)
        store.close()

    def named(self, result: migrate.Audit, name: str) -> migrate.Check:
        for check in result.checks:
            if check.name == name:
                return check
        raise AssertionError("no check named " + name)

    def raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_a_healthy_database_passes_every_gate(self) -> None:
        result = migrate.audit(self.db)
        self.assertEqual(
            [c.name for c in result.failed()], [],
            "a freshly written database must not fail its own audit")

    def test_it_does_not_write_to_the_source(self) -> None:
        """Opened read-only on purpose: this runs against production."""
        before = os.path.getmtime(self.db)
        migrate.audit(self.db)
        self.assertEqual(os.path.getmtime(self.db), before)

    def test_the_volumes_match_the_tables(self) -> None:
        result = migrate.audit(self.db)
        self.assertEqual(result.volumes["runs"], 2)
        self.assertEqual(result.volumes["users"], 1)
        self.assertEqual(result.volumes["comments"], 1)

    def test_it_records_the_schema_version_to_compare_later(self) -> None:
        result = migrate.audit(self.db)
        conn = self.raw()
        expected = conn.execute("SELECT version FROM schema_version") \
            .fetchone()[0]
        self.assertEqual(result.schema_version, expected)

    def test_an_orphan_row_stops_the_migration(self) -> None:
        """Dangling rows are latent corruption, and the migration is the
        one cheap moment to find them (runbook §B.6)."""
        conn = self.raw()
        conn.execute(
            "INSERT INTO run_outputs (run_id, output) VALUES (99999, ?)",
            (sqlite3.Binary(b"x"),))
        conn.commit()
        check = self.named(migrate.audit(self.db), "orphan_rows")
        self.assertFalse(check.ok)
        self.assertTrue(check.blocking)
        self.assertIn("run_outputs", check.detail)

    def test_a_missing_table_stops_before_anything_else_runs(self) -> None:
        """A database older than the code fails the export halfway.

        Discovered for real: the export tool grew a table that the
        development database predates, and the first symptom was a
        stack trace from deep inside the audit.
        """
        conn = self.raw()
        conn.execute("DROP TABLE test_retirements")
        conn.commit()
        result = migrate.audit(self.db)
        check = self.named(result, "source_tables")
        self.assertFalse(check.ok)
        self.assertIn("test_retirements", check.detail)
        self.assertEqual(
            [c.name for c in result.checks], ["source_tables"],
            "nothing after a half-known schema should have been reported")

    def test_a_mangled_timestamp_stops_the_migration(self) -> None:
        """VARCHAR(26) would truncate it, and lexical ordering is the

        project's whole time model."""
        conn = self.raw()
        conn.execute("UPDATE runs SET start_time = '2026-07-25T02:00:00' "
                     "WHERE id = 1")
        conn.commit()
        check = self.named(migrate.audit(self.db), "timestamp_widths")
        self.assertFalse(check.ok)
        self.assertTrue(check.blocking)

    def test_case_collisions_are_measured_but_do_not_block(self) -> None:
        """They are the size of what a wrong collation would destroy —

        with the right collation (§A.1) they are entirely harmless, so
        blocking on them would be refusing to migrate correct data."""
        conn = self.raw()
        conn.execute(
            "INSERT INTO runs (environment, script, test_name, result, "
            "start_time, end_time, source_link) "
            "SELECT environment, script, UPPER(test_name), result, "
            "start_time, end_time, source_link FROM runs WHERE id = 2")
        conn.commit()
        check = self.named(migrate.audit(self.db), "case_collisions")
        self.assertTrue(check.ok)
        self.assertFalse(check.blocking)
        self.assertIn("collation", check.advice)

    def test_trailing_whitespace_is_a_warning_with_the_reason(self) -> None:
        conn = self.raw()
        conn.execute("UPDATE runs SET test_name = 'plain ' WHERE id = 2")
        conn.commit()
        check = self.named(migrate.audit(self.db), "identity_whitespace")
        self.assertFalse(check.ok)
        self.assertFalse(check.blocking)

    def test_it_measures_the_largest_blob_for_the_packet_check(self) -> None:
        result = migrate.audit(self.db)
        conn = self.raw()
        biggest = conn.execute(
            "SELECT MAX(LENGTH(output)) FROM run_outputs").fetchone()[0]
        self.assertEqual(result.max_blob_bytes, biggest)


class PreflightTest(unittest.TestCase):
    """Every server gate, driven by a fake connection.

    Scripted answers only. This says nothing about whether MariaDB
    accepts the SQL — see this module's docstring.
    """

    def run_preflight(self, overrides: Sequence[Tuple[str, Any]] = (),
                      **kwargs: Any) -> Dict[str, migrate.Check]:
        script = list(overrides) + list(HEALTHY)
        server = FakeServer(script)
        checks = migrate.preflight(server, max_blob_bytes=1024, **kwargs)
        return dict((c.name, c) for c in checks)

    def blocking_failures(
        self, checks: Dict[str, migrate.Check]
    ) -> List[str]:
        return sorted(n for n, c in checks.items()
                      if not c.ok and c.blocking)

    def test_a_healthy_server_passes(self) -> None:
        self.assertEqual(self.blocking_failures(self.run_preflight()), [])

    def test_a_case_insensitive_collation_stops_everything(self) -> None:
        """The probe stores 'a', 'A' and 'a ' and counts them.

        Two rows means the collation merged values that testboard
        treats as different tests. Loading 4.4M rows on top of that is
        not recoverable by any later step.
        """
        checks = self.run_preflight(
            [("COUNT(*) FROM _tb_collation_probe", [(2,)])])
        self.assertIn("collation_probe", self.blocking_failures(checks))
        self.assertIn("§A.3", checks["collation_probe"].advice)

    def test_a_duplicate_key_from_the_probe_is_the_same_finding(self) -> None:
        checks = self.run_preflight(
            [("INSERT INTO _tb_collation_probe",
              ValueError("Duplicate entry 'A' for key 'PRIMARY'"))])
        self.assertIn("collation_probe", self.blocking_failures(checks))

    def test_strict_mode_off_is_detected_by_a_probe_that_must_fail(
            self) -> None:
        """The insert succeeding is the failure. A probe that passes by

        succeeding cannot tell strict mode from a statement that never
        ran."""
        checks = self.run_preflight(
            [("INSERT INTO _tb_strict_probe", [])])
        self.assertIn("strict_probe", self.blocking_failures(checks))
        self.assertIn("TRUNCATED", checks["strict_probe"].detail)

    def test_the_probe_tables_are_always_dropped(self) -> None:
        """They are created in the database about to receive the load;

        leaving one behind would trip the 'target is empty' gate on the
        next run and look like a half-finished migration."""
        server = FakeServer(list(HEALTHY))
        migrate.preflight(server)
        for name in ("_tb_collation_probe", "_tb_strict_probe",
                     "_tb_grant_probe"):
            self.assertTrue(
                any(sql.startswith("DROP TABLE") and name in sql
                    for sql in server.executed),
                "never dropped " + name)

    def test_a_non_strict_sql_mode_is_caught_before_the_probe(self) -> None:
        checks = self.run_preflight([
            ("SELECT VERSION()", [("10.6.16-MariaDB", "NO_ENGINE_SUBSTITUTION",
                                   134217728, "ON", "utf8mb4",
                                   "utf8mb4_nopad_bin")])])
        self.assertIn("sql_mode_strict", self.blocking_failures(checks))

    def test_a_case_insensitive_database_collation_is_caught(self) -> None:
        checks = self.run_preflight([
            ("SELECT VERSION()", [("10.6.16-MariaDB", "STRICT_TRANS_TABLES",
                                   134217728, "ON", "utf8mb4",
                                   "utf8mb4_general_ci")])])
        self.assertIn("database_collation", self.blocking_failures(checks))

    def test_a_packet_smaller_than_the_biggest_blob_is_caught(self) -> None:
        """Found at 90% of a four-hour load otherwise, as a connection

        that 'has gone away'."""
        server = FakeServer(list(HEALTHY))
        checks = dict(
            (c.name, c) for c in
            migrate.preflight(server, max_blob_bytes=200 * 1024 * 1024))
        self.assertFalse(checks["max_allowed_packet"].ok)

    def test_an_old_server_is_blocked(self) -> None:
        checks = self.run_preflight([
            ("SELECT VERSION()", [("10.1.48-MariaDB", "STRICT_TRANS_TABLES",
                                   134217728, "ON", "utf8mb4",
                                   "utf8mb4_bin")])])
        self.assertIn("server_version", self.blocking_failures(checks))

    def test_version_parsing_survives_vendor_suffixes(self) -> None:
        for text, expected in (
                ("10.6.16-MariaDB-log", (10, 6, 16)),
                ("10.11.6-MariaDB", (10, 11, 6)),
                ("5.5.68-MariaDB", (5, 5, 68))):
            self.assertEqual(migrate._version_tuple(text), expected)

    def test_a_populated_target_blocks_unless_forced(self) -> None:
        """Loading on top of an existing schema makes a mixture that no

        verification can untangle."""
        checks = self.run_preflight([("SHOW TABLES", [("runs",), ("users",)])])
        self.assertIn("target_is_empty", self.blocking_failures(checks))
        forced = self.run_preflight(
            [("SHOW TABLES", [("runs",), ("users",)])], allow_non_empty=True)
        self.assertNotIn("target_is_empty", self.blocking_failures(forced))

    def test_missing_grants_are_found_before_the_load_not_during(
            self) -> None:
        checks = self.run_preflight(
            [("CREATE TABLE _tb_grant_probe",
              ValueError("CREATE command denied to user 'testboard'"))])
        self.assertIn("grants", self.blocking_failures(checks))
        self.assertIn("§A.4", checks["grants"].advice)


class DriverTest(unittest.TestCase):
    """The connection is made with the vendored driver, and nothing else.

    This is the property the whole project is built around: a checkout
    runs on the deployment host with nothing installed. A migration that
    shelled out to the ``mysql`` client would have needed a client
    package on the web server — the exact dependency vendoring exists to
    remove — so it is pinned here rather than left to judgement.
    """

    def test_the_module_shells_out_to_nothing(self) -> None:
        """No subprocess, anywhere. If a future change reaches for one,

        this fails and points at the reason rather than at a style
        rule."""
        source = io.open(migrate.__file__.replace(".pyc", ".py"),
                         encoding="utf-8").read()
        for banned in ("import subprocess", "os.system", "os.popen",
                       "shutil.which"):
            self.assertNotIn(
                banned, source,
                "migrate_to_mariadb.py must not shell out: the migration "
                "has to run on a host with nothing installed")

    def test_the_shell_out_detector_can_actually_fail(self) -> None:
        """A guard that never fires is decoration. This is the exact

        mistake the detector exists to catch, so prove it catches it."""
        planted = "import os\nimport subprocess\nsubprocess.Popen(['mysql'])"
        found = [banned for banned in ("import subprocess", "os.system",
                                       "os.popen", "shutil.which")
                 if banned in planted]
        self.assertEqual(found, ["import subprocess"])

    def test_it_uses_the_vendored_driver_not_an_installed_one(self) -> None:
        source = io.open(migrate.__file__.replace(".pyc", ".py"),
                         encoding="utf-8").read()
        self.assertIn("from third_party import pymysql", source)
        self.assertNotIn("\nimport pymysql", source)

    def test_the_driver_it_would_use_is_actually_importable(self) -> None:
        """Present, not installed — no pip step stands behind this."""
        driver = migrate._driver()
        self.assertTrue(callable(driver.connect))

    def test_the_driver_can_do_load_data_local_infile(self) -> None:
        """The load depends on it. PyMySQL implements the LOCAL INFILE

        side of the protocol itself; if a future vendored version drops
        it, the load would fail at the last possible moment."""
        from third_party.pymysql import connections
        self.assertTrue(hasattr(connections, "LoadLocalFile"))


class ConnectionTest(unittest.TestCase):
    """What gets passed to the driver, and what the errors say.

    The driver is stubbed: there is no MariaDB here. What is pinned is
    the connect arguments and the error translation, which is where an
    operator either gets an actionable message or a stack trace.
    """

    SETTINGS = migrate.Settings(
        host="db.example", port=3306, user="testboard_migrate",
        password="hunter2", database="testboard", unix_socket=None)

    def stub_driver(self, connect: Any) -> List[Dict[str, Any]]:
        """Replace the driver with a recorder. Returns the kwargs seen."""
        seen = []  # type: List[Dict[str, Any]]

        class FakeDriver(object):
            @staticmethod
            def connect(**kwargs: Any) -> Any:
                seen.append(kwargs)
                return connect(**kwargs)

        real = migrate._driver
        migrate._driver = lambda: FakeDriver
        self.addCleanup(setattr, migrate, "_driver", real)
        return seen

    def test_local_infile_is_only_enabled_for_the_load(self) -> None:
        """It lets the server ask the client for a local file. Off unless

        a load actually needs it."""
        seen = self.stub_driver(lambda **kw: object())
        migrate.connect(self.SETTINGS)
        self.assertFalse(seen[0]["local_infile"])
        migrate.connect(self.SETTINGS, local_infile=True)
        self.assertTrue(seen[1]["local_infile"])

    def test_it_connects_utf8mb4_and_autocommit(self) -> None:
        """utf8mb4 because the identity columns are; autocommit because

        the load is a sequence of independent statements and a dropped
        connection must not silently roll back a finished one."""
        seen = self.stub_driver(lambda **kw: object())
        migrate.connect(self.SETTINGS)
        self.assertEqual(seen[0]["charset"], "utf8mb4")
        self.assertTrue(seen[0]["autocommit"])

    def test_a_socket_replaces_host_and_port(self) -> None:
        """'localhost' over a socket is a different MariaDB account from

        the same machine over TCP — sending both would pick one at
        random from the operator's point of view."""
        seen = self.stub_driver(lambda **kw: object())
        migrate.connect(self.SETTINGS._replace(unix_socket="/var/lib/x.sock"))
        self.assertIn("unix_socket", seen[0])
        self.assertNotIn("host", seen[0])

    def test_a_sha256_account_names_the_fix_and_forbids_the_install(
            self) -> None:
        """PyMySQL raises a bare RuntimeError naming a package. Installing

        it is the obvious move and the wrong one: it needs a compiler,
        which gives up the property this design protects."""
        def boom(**kwargs: Any) -> Any:
            raise RuntimeError(
                "'cryptography' package is required for sha256_password "
                "or caching_sha2_password auth methods")

        self.stub_driver(boom)
        with self.assertRaises(SystemExit) as caught:
            migrate.connect(self.SETTINGS)
        message = str(caught.exception)
        self.assertIn("mysql_native_password", message)
        self.assertIn("§A.4", message)
        self.assertIn("Do NOT install", message)

    def test_access_denied_explains_the_host_matching(self) -> None:
        def boom(**kwargs: Any) -> Any:
            raise ValueError(
                "(1045, \"Access denied for user 'testboard_migrate'\")")

        self.stub_driver(boom)
        with self.assertRaises(SystemExit) as caught:
            migrate.connect(self.SETTINGS)
        self.assertIn("localhost", str(caught.exception))

    def test_a_connection_failure_never_prints_the_password(self) -> None:
        """The message goes into a terminal, a ticket, or a paste."""
        def boom(**kwargs: Any) -> Any:
            raise ValueError("nope")

        self.stub_driver(boom)
        with self.assertRaises(SystemExit) as caught:
            migrate.connect(self.SETTINGS)
        self.assertNotIn("hunter2", str(caught.exception))


class StatementSplitTest(unittest.TestCase):
    """The generated .sql files, cut up for a driver that takes one at a time.

    Splitting on ';' is the obvious approach and it corrupts the load:
    the LOAD DATA statements are full of quoted punctuation.
    """

    def test_a_semicolon_inside_a_string_does_not_split(self) -> None:
        parts = migrate.split_statements(
            "INSERT INTO t VALUES ('a;b'); SELECT 1;")
        self.assertEqual(len(parts), 2)
        self.assertIn("'a;b'", parts[0])

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        parts = migrate.split_statements(
            r"INSERT INTO t VALUES ('it\'s; fine'); SELECT 1;")
        self.assertEqual(len(parts), 2)

    def test_comments_are_dropped_not_executed(self) -> None:
        parts = migrate.split_statements(
            "-- a comment with a ; in it\nSELECT 1;")
        self.assertEqual(parts, ["SELECT 1"])

    def test_a_trailing_statement_without_a_semicolon_survives(self) -> None:
        self.assertEqual(migrate.split_statements("SELECT 1"), ["SELECT 1"])

    def test_no_empty_statements_are_produced(self) -> None:
        self.assertEqual(migrate.split_statements(";;\n;\n-- x\n"), [])

    def test_the_real_generated_files_split_cleanly(self) -> None:
        """The files it actually has to parse, not a sample of SQL."""
        tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = seed(db)
        store.close()
        out = os.path.join(tmp, "out")
        exporter.export(db, out, exporter.Sizes(64, 255, 255))

        with io.open(os.path.join(out, "schema.sql"), encoding="utf-8") as fh:
            schema = migrate.split_statements(fh.read())
        creates = [s for s in schema if s.startswith("CREATE TABLE")]
        self.assertEqual(
            len(creates), len(exporter.TABLE_ORDER),
            "one CREATE TABLE per table, or a table is silently missing")
        for statement in creates:
            self.assertTrue(statement.rstrip().endswith(")")
                            or "ROW_FORMAT" in statement,
                            "a CREATE TABLE was cut short: " + statement[-60:])

        with io.open(os.path.join(out, "load.sql"), encoding="utf-8") as fh:
            load = migrate.split_statements(fh.read())
        loads = [s for s in load if s.startswith("LOAD DATA")]
        self.assertEqual(
            len(loads), len(exporter.TABLE_ORDER),
            "one LOAD DATA per table, or a table goes unloaded")
        for statement in loads:
            self.assertIn("FIELDS TERMINATED BY", statement,
                          "a LOAD DATA lost its escaping clause in the split")
            self.assertIn("LINES TERMINATED BY", statement)

    def test_the_session_settings_survive_and_come_first(self) -> None:
        """load.sql opens by turning off FK and uniqueness checks. Those

        are session variables: they only hold because every statement
        goes through one connection. If the split dropped them the load
        would be far slower and would fail on load order."""
        tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = seed(db)
        store.close()
        out = os.path.join(tmp, "out")
        exporter.export(db, out, exporter.Sizes(64, 255, 255))
        with io.open(os.path.join(out, "load.sql"), encoding="utf-8") as fh:
            parts = migrate.split_statements(fh.read())
        self.assertIn("SET FOREIGN_KEY_CHECKS = 0", parts[0])
        self.assertTrue(
            any("FOREIGN_KEY_CHECKS = 1" in p for p in parts),
            "the checks are turned off and never back on")

    def test_indexes_are_built_after_the_data_is_in(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = seed(db)
        store.close()
        out = os.path.join(tmp, "out")
        exporter.export(db, out, exporter.Sizes(64, 255, 255))
        with io.open(os.path.join(out, "load.sql"), encoding="utf-8") as fh:
            parts = migrate.split_statements(fh.read())
        last_load = max(i for i, p in enumerate(parts)
                        if p.startswith("LOAD DATA"))
        first_index = min(i for i, p in enumerate(parts)
                          if p.startswith("CREATE INDEX"))
        self.assertLess(last_load, first_index)

    def test_parents_load_before_children(self) -> None:
        """FK-order loading is kept even though the generated schema
        declares no constraints (runbook §B.6)."""
        tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = seed(db)
        store.close()
        out = os.path.join(tmp, "out")
        exporter.export(db, out, exporter.Sizes(64, 255, 255))
        with io.open(os.path.join(out, "load.sql"), encoding="utf-8") as fh:
            parts = migrate.split_statements(fh.read())
        order = [p.split("INTO TABLE ")[1].split("\n")[0].strip()
                 for p in parts if "INTO TABLE" in p]
        self.assertLess(order.index("runs"), order.index("run_outputs"))
        self.assertLess(order.index("users"), order.index("comments"))


class InfilePathTest(unittest.TestCase):
    """LOAD DATA LOCAL INFILE resolves against the client's own directory."""

    def test_a_relative_path_becomes_absolute(self) -> None:
        """The driver opens it relative to this process's cwd. Left

        alone, a re-run from elsewhere either fails or — worse — loads a
        stale file of the same name."""
        out = os.path.join(tempfile.gettempdir(), "export")
        result = migrate.absolutise_infile(
            "LOAD DATA LOCAL INFILE 'runs.tsv'\nINTO TABLE runs", out)
        self.assertIn(os.path.abspath(out).replace("\\", "/"), result)
        self.assertIn("runs.tsv", result)

    def test_a_statement_without_an_infile_is_untouched(self) -> None:
        statement = "CREATE TABLE runs (id BIGINT)"
        self.assertEqual(
            migrate.absolutise_infile(statement, "/tmp/x"), statement)

    def test_an_absolute_path_is_left_alone(self) -> None:
        statement = "LOAD DATA LOCAL INFILE '/data/runs.tsv' INTO TABLE runs"
        self.assertEqual(
            migrate.absolutise_infile(statement, "/tmp/x"), statement)

    def test_the_generated_file_still_uses_relative_paths(self) -> None:
        """The other half of the coupling: if the exporter ever writes

        absolute paths, the rewrite becomes dead code and this says so."""
        tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = seed(db)
        store.close()
        out = os.path.join(tmp, "out")
        exporter.export(db, out, exporter.Sizes(64, 255, 255))
        with io.open(os.path.join(out, "load.sql"), encoding="utf-8") as fh:
            self.assertIn("INFILE 'runs.tsv'", fh.read())


class RunFileTest(unittest.TestCase):
    """Executing a generated file: one connection, and a usable failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = seed(self.db)
        store.close()
        self.out = os.path.join(self.tmp, "out")
        exporter.export(self.db, self.out, exporter.Sizes(64, 255, 255))

    def database(self) -> Any:
        """A Database whose connection is a recorder."""
        database = migrate.Database.__new__(migrate.Database)
        database.settings = ConnectionTest.SETTINGS
        database.local_infile = True
        database.conn = None
        database.executed = []

        def run(sql: str) -> None:
            database.executed.append(sql)

        database.run = run
        return database

    def test_every_statement_in_the_file_is_executed_in_order(self) -> None:
        database = self.database()
        count = database.run_file(
            os.path.join(self.out, "load.sql"), cwd=self.out)
        self.assertEqual(count, len(database.executed))
        loads = [s for s in database.executed if s.startswith("LOAD DATA")]
        self.assertEqual(len(loads), len(exporter.TABLE_ORDER))

    def test_each_statement_reports_as_it_finishes(self) -> None:
        """A single LOAD DATA of `runs` runs for tens of minutes on 950 MB.

        With no output, "working" and "hung" look identical, and the
        operator is watching a freeze window.
        """
        database = self.database()
        lines = []  # type: List[str]
        count = database.run_file(
            os.path.join(self.out, "load.sql"), cwd=self.out,
            log=lines.append)
        self.assertEqual(len(lines), count)
        self.assertTrue(any("LOAD DATA" in line for line in lines))
        self.assertTrue(all(line.strip().startswith("[") for line in lines),
                        "each line should carry its position in the file")

    def test_the_data_files_are_addressed_absolutely(self) -> None:
        database = self.database()
        database.run_file(os.path.join(self.out, "load.sql"), cwd=self.out)
        for statement in database.executed:
            if "INFILE" not in statement:
                continue
            path = statement.split("INFILE '")[1].split("'")[0]
            self.assertTrue(os.path.isfile(path),
                            "load would look for a file that is not there: "
                            + path)

    def test_a_failure_names_the_file_the_statement_and_the_runbook(
            self) -> None:
        """A load that dies four hours in must say where to look."""
        database = self.database()

        def boom(path: str, cwd: str, log: Any = None) -> int:
            raise migrate.DatabaseError(
                "(1062, \"Duplicate entry 'x' for key 'PRIMARY'\")",
                "LOAD DATA LOCAL INFILE 'runs.tsv'\nINTO TABLE runs")

        database.run_file = boom
        with self.assertRaises(SystemExit) as caught:
            migrate.run_sql_file(database, os.path.join(self.out, "load.sql"),
                                 self.out, lambda m: None)
        message = str(caught.exception)
        self.assertIn("load.sql", message)
        self.assertIn("1062", message)
        self.assertIn("LOAD DATA LOCAL INFILE 'runs.tsv'", message,
                      "the failing statement itself must be in the message")
        self.assertIn("MARIADB_MIGRATION.md", message)


class VerifyTest(unittest.TestCase):
    """The agreement checks, and what a disagreement does."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = seed(self.db)
        store.close()
        self.out = os.path.join(self.tmp, "out")
        exporter.export(self.db, self.out, exporter.Sizes(64, 255, 255))
        self.expected = self._expected()

    def _expected(self) -> Dict[str, List[Tuple[Any, ...]]]:
        """The true answers, read from the SQLite source itself.

        A MariaDB that agreed with SQLite would return exactly these,
        so this is the fake server's script for the happy path.
        """
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        answers = {}  # type: Dict[str, List[Tuple[Any, ...]]]
        for name, sql in exporter.VERIFY_QUERIES:
            answers[sql] = [tuple(r) for r in conn.execute(sql)]
        return answers

    def server(self, tweak: Optional[Dict[str, List[Any]]] = None) -> Any:
        script = []  # type: List[Tuple[str, Any]]
        for sql, rows in self.expected.items():
            script.append((sql, (tweak or {}).get(sql, rows)))
        # Longest first: several verify queries are prefixes of others
        # under substring matching.
        script.sort(key=lambda pair: len(pair[0]), reverse=True)
        return FakeServer(script)

    def names(self, checks: Sequence[migrate.Check]) -> List[str]:
        return [c.name for c in checks if not c.ok]

    def test_an_identical_database_agrees_on_every_check(self) -> None:
        checks = migrate.verify(self.server(), self.out, lambda m: None)
        self.assertEqual(self.names(checks), [])
        self.assertEqual(len(checks), len(exporter.VERIFY_QUERIES))

    def test_a_missing_row_is_caught(self) -> None:
        sql = dict(exporter.VERIFY_QUERIES)["runs_total"]
        checks = migrate.verify(
            self.server({sql: [(1,)]}), self.out, lambda m: None)
        self.assertIn("runs_total", self.names(checks))

    def test_a_lost_blob_is_caught(self) -> None:
        """The hex round-trip is the most likely thing to go wrong."""
        sql = dict(exporter.VERIFY_QUERIES)["output_bytes"]
        original = self.expected[sql][0]
        checks = migrate.verify(
            self.server({sql: [(original[0], (original[1] or 0) - 1)]}),
            self.out, lambda m: None)
        self.assertIn("output_bytes", self.names(checks))
        advice = [c.advice for c in checks if c.name == "output_bytes"][0]
        self.assertIn("UNHEX", advice)

    def test_merged_tests_are_caught_and_named_as_the_collation(
            self) -> None:
        sql = dict(exporter.VERIFY_QUERIES)["distinct_tests"]
        checks = migrate.verify(
            self.server({sql: [(1,)]}), self.out, lambda m: None)
        failed = [c for c in checks if c.name == "distinct_tests"][0]
        self.assertFalse(failed.ok)
        self.assertIn("collation", failed.advice)

    def test_a_decimal_sum_still_matches_an_integer_one(self) -> None:
        """MariaDB returns SUM() as a Decimal and SQLite as an int.

        Comparing repr()s would fail every run and teach the operator
        that a red verification means nothing.
        """
        import decimal
        sql = dict(exporter.VERIFY_QUERIES)["output_bytes"]
        count, total = self.expected[sql][0]
        checks = migrate.verify(
            self.server({sql: [(count, decimal.Decimal(str(total)))]}),
            self.out, lambda m: None)
        self.assertNotIn("output_bytes", self.names(checks))

    def test_bytes_from_the_wire_still_match_text(self) -> None:
        """Some column types come back as bytes depending on charset."""
        sql = dict(exporter.VERIFY_QUERIES)["run_span"]
        low, high = self.expected[sql][0]
        checks = migrate.verify(
            self.server({sql: [(low.encode("utf-8"),
                                high.encode("utf-8"))]}),
            self.out, lambda m: None)
        self.assertNotIn("run_span", self.names(checks))

    def test_the_difference_report_points_at_the_first_bad_row(self) -> None:
        sql = dict(exporter.VERIFY_QUERIES)["by_day_result"]
        rows = list(self.expected[sql])
        rows[0] = (rows[0][0], rows[0][1], 999999)
        checks = migrate.verify(
            self.server({sql: rows}), self.out, lambda m: None)
        detail = [c for c in checks if c.name == "by_day_result"][0].detail
        self.assertIn("first difference at row 1", detail)
        self.assertIn("999999", detail)

    def test_a_missing_source_file_is_refused_not_skipped(self) -> None:
        """No verify_source.txt means nothing to compare against —

        which must not read as 'verified'."""
        os.remove(os.path.join(self.out, "verify_source.txt"))
        with self.assertRaises(SystemExit):
            migrate.verify(self.server(), self.out, lambda m: None)

    def test_an_export_from_an_older_tool_is_refused(self) -> None:
        """A check the source file does not carry cannot be verified,

        and quietly passing it would report agreement that was never
        tested."""
        path = os.path.join(self.out, "verify_source.txt")
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("== distinct_tests", "== something_else")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        checks = migrate.verify(self.server(), self.out, lambda m: None)
        self.assertIn("distinct_tests", self.names(checks))


class ReportingTest(unittest.TestCase):
    """A failed gate has to be visible and has to be fatal."""

    def lines(self, checks: Sequence[migrate.Check]) -> Tuple[bool, str]:
        collected = []  # type: List[str]
        ok = migrate.report("T", checks, collected.append)
        return ok, "\n".join(collected)

    def test_a_blocking_failure_returns_false_and_prints_the_advice(
            self) -> None:
        ok, text = self.lines([
            migrate.Check("x", False, "detail", True, "do the thing")])
        self.assertFalse(ok)
        self.assertIn("STOP", text)
        self.assertIn("do the thing", text)

    def test_a_warning_does_not_block_but_is_still_shown(self) -> None:
        ok, text = self.lines([
            migrate.Check("x", False, "detail", False, "context")])
        self.assertTrue(ok)
        self.assertIn("warn", text)
        self.assertIn("context", text)

    def test_a_passing_run_says_so_without_advice_noise(self) -> None:
        ok, text = self.lines([
            migrate.Check("x", True, "fine", True, "never mind")])
        self.assertTrue(ok)
        self.assertNotIn("never mind", text)


class CliTest(unittest.TestCase):
    """Exit codes, because a wrapper script reads those and not prose."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = seed(self.db)
        store.close()

    def test_a_clean_audit_exits_zero(self) -> None:
        code = migrate.main(["audit", "--db", self.db])
        self.assertEqual(code, 0)

    def test_a_failed_gate_exits_nonzero(self) -> None:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO run_outputs (run_id, output) VALUES (99999, ?)",
            (sqlite3.Binary(b"x"),))
        conn.commit()
        self.assertEqual(
            migrate.main(["audit", "--db", self.db]),
            migrate.EXIT_GATE_FAILED)

    def test_the_gate_exit_code_is_distinct_from_a_crash(self) -> None:
        """3 means 'the database said no'; 2 means 'the tool broke'."""
        self.assertNotIn(migrate.EXIT_GATE_FAILED, (0, 1, 2))

    def test_the_audit_json_carries_the_sizes_the_export_needs(self) -> None:
        import json
        path = os.path.join(self.tmp, "audit.json")
        migrate.main(["audit", "--db", self.db, "--json", path])
        with io.open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("environment", payload["sizes"])
        self.assertIn("max_blob_bytes", payload)

    def test_export_sizes_default_to_the_audit(self) -> None:
        """Typing the sizes by hand is how they end up wrong."""
        out = os.path.join(self.tmp, "out")
        code = migrate.main(["export", "--db", self.db, "--out", out])
        self.assertEqual(code, 0)
        with io.open(os.path.join(out, "schema.sql"), encoding="utf-8") as fh:
            self.assertIn("VARCHAR(64)", fh.read())

    def _all_args(self) -> Any:
        parser = migrate.build_parser()
        return parser.parse_args([
            "all", "--db", self.db, "--out", os.path.join(self.tmp, "out"),
            "--config", os.path.join(self.tmp, "my.cnf")])

    def _trace_phases(self) -> List[str]:
        """Run `all` with every phase stubbed, recording the order."""
        order = []  # type: List[str]

        def stub(name: str) -> Any:
            def run(args: Any) -> int:
                order.append(name)
                return 0
            return run

        for name in ("preflight", "export", "load", "verify"):
            setattr(migrate, "cmd_" + name, stub(name))
            self.addCleanup(setattr, migrate, "cmd_" + name,
                            getattr(migrate, "cmd_" + name))
        migrate.cmd_all(self._all_args())
        return order

    def test_the_server_is_proved_fit_before_the_export_is_written(
            self) -> None:
        """The collation probe costs a second; the export costs twenty

        minutes and 2.4 GB of disk, inside the freeze window. A gate
        that fires after the expensive step is not a gate.
        """
        order = self._trace_phases()
        self.assertLess(order.index("preflight"), order.index("export"))
        self.assertEqual(order, ["preflight", "export", "load", "verify"])

    def test_the_source_is_audited_once_not_once_per_phase(self) -> None:
        """The audit scans 4.4M rows: MAX(LENGTH(...)), a LOWER() distinct

        count and five orphan LEFT JOINs. Free on a two-row fixture,
        minutes on production, and those minutes are downtime.
        """
        calls = []  # type: List[str]
        real = migrate.audit

        def counting(db_path: str) -> Any:
            calls.append(db_path)
            return real(db_path)

        migrate.audit = counting
        self.addCleanup(setattr, migrate, "audit", real)
        self._trace_phases()
        self.assertEqual(len(calls), 1, "audited the source {0} times"
                         .format(len(calls)))

    def test_no_subcommand_prints_help_rather_than_a_traceback(self) -> None:
        self.assertEqual(migrate.main([]), 2)

    def test_the_disk_space_check_measures_the_export_not_the_db(
            self) -> None:
        """A 950 MB database needs ~2.4 GB of export, and finding that

        out at 90% leaves a directory the tool then refuses to reuse."""
        check = migrate.check_disk_space(self.db, self.tmp, lambda m: None)
        self.assertTrue(check.blocking)
        self.assertIn("free", check.detail)


if __name__ == "__main__":
    unittest.main()
