"""The MariaDB half of the storage seam, over the vendored driver.

:class:`testboard.storage.Storage` runs the same method bodies — the
same transactions, the same SELECT-first upsert — on either engine;
what differs is HOW a connection is made and a handful of SQL
spellings. The SQLite half lives in ``storage.py`` as
``_SqliteBackend``; this module is its MariaDB twin and is imported
ONLY by ``Storage.mariadb()``, so a SQLite deployment never loads the
driver. SQLite remains a permanent first-class backend — this module
adds an engine, it does not replace one.

The connection wrapper duck-types the small ``sqlite3.Connection``
surface Storage relies on: ``execute``/``executemany`` returning a
cursor with ``fetchone``/``fetchall``/``rowcount``/``lastrowid``, and
``close``. Three statement rewrites happen at execute time, exact or
prefix match only — substring surgery on SQL is where silent
corruption lives:

* ``BEGIN IMMEDIATE`` → ``START TRANSACTION`` (InnoDB has no immediate
  mode; row locks make it unnecessary, and lock waits are bounded by
  the ``innodb_lock_wait_timeout`` this module sets at connect).
* ``INSERT OR REPLACE`` → ``REPLACE`` — same delete-then-insert
  semantics, and the two sites that use it are pinned by
  ``tests/test_sql_portability.py`` as tables where that is
  unobservable (runbook §B.5).
* qmark → pyformat: ``%`` doubled first, then ``?`` → ``%s``. The SQL
  text in storage.py stays qmark-canonical forever; the translation is
  cached per statement text.

Schema: **this backend never runs DDL.** The schema is created by the
migration tooling (runbook §D); at startup the stored
``schema_version`` must equal this build's exactly, and both
directions of mismatch refuse loudly.

Python 3.6 compatible; standard library plus the vendored driver only.
"""

import time
from typing import Any, Callable, Dict, Optional, Tuple

from testboard.dbconfig import Settings

__all__ = ["MariaDBBackend", "describe_connect_error"]

#: Seconds a connection may sit idle before the next use pings it.
#: MariaDB's wait_timeout defaults to 28800 s (8 h) and a worker
#: thread's connection can easily idle overnight; a ping on the first
#: borrow after a quiet spell reconnects transparently. Never inside a
#: transaction — a transaction that has been idle long enough to need a
#: ping has already lost its locks, and silently starting a new
#: connection mid-transaction would commit half of it.
PING_IDLE_SECONDS = 60.0

#: The InnoDB analogue of SQLite's ``PRAGMA busy_timeout=10000``.
#: Rides in init_command so a driver-level reconnect re-applies it.
_INIT_COMMAND = "SET SESSION innodb_lock_wait_timeout=10"

#: MariaDB spells "no limit" as the largest possible row count; -1 is
#: a syntax error there (the SQLite spelling lives in _SqliteBackend).
_LIMIT_ALL_OFFSET = " LIMIT 18446744073709551615 OFFSET ?"

#: Substring search, matching the SQLite behaviour testers know:
#: case-insensitive, with %/_/\ escaped by storage's _escape_like. The
#: explicit COLLATE keeps it case-insensitive over the migrated _bin
#: columns (slightly broader than SQLite: Unicode case and accents fold
#: too — documented in the drop note). The wire must carry ESCAPE '\\'
#: because MariaDB string literals process backslashes; SQLite's
#: spelling of the same escape is ESCAPE '\'.
_LIKE_TEST_NAME = (
    "lr.test_name COLLATE utf8mb4_general_ci LIKE ? ESCAPE '\\\\'"
)


def _driver() -> Any:
    """Import the vendored driver, or say where it should have been."""
    try:
        from third_party import pymysql
    except ImportError as exc:  # pragma: no cover - vendored, always there
        raise RuntimeError(
            "the vendored MySQL driver is missing ({0}). It should be in "
            "third_party/pymysql and it ships with the repository — see "
            "third_party/README.md. Nothing needs installing; if the "
            "directory is absent, the checkout is incomplete.".format(exc))
    return pymysql


class _Connection(object):
    """One thread's connection: rewrite, translate, ping, delegate.

    Presents the ``sqlite3.Connection`` surface Storage uses. The
    ``clock`` parameter exists for the unit tests; production uses
    ``time.monotonic``.
    """

    def __init__(self, backend: "MariaDBBackend",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._backend = backend
        self._clock = clock
        self._raw = backend.raw_connect()
        self._last_used = clock()
        self.in_transaction = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        """Run one statement, returning the live cursor."""
        stripped = sql.strip()
        now = self._clock()
        if (not self.in_transaction
                and now - self._last_used >= PING_IDLE_SECONDS):
            self._raw.ping(reconnect=True)
        self._last_used = now

        if stripped == "BEGIN IMMEDIATE":
            self.in_transaction = True
        elif stripped in ("COMMIT", "ROLLBACK"):
            self.in_transaction = False

        cursor = self._raw.cursor()
        cursor.execute(self._backend.translate(sql), tuple(params))
        return cursor

    def executemany(self, sql: str, seq_of_params: Any) -> Any:
        """Run one statement per parameter tuple, returning the cursor."""
        self._last_used = self._clock()
        cursor = self._raw.cursor()
        cursor.executemany(
            self._backend.translate(sql),
            [tuple(params) for params in seq_of_params])
        return cursor

    def close(self) -> None:
        self._raw.close()


class MariaDBBackend(object):
    """Connection factory and dialect spellings for MariaDB.

    The counterpart of ``storage._SqliteBackend``; see that class for
    the seam's division of labour.
    """

    #: The schema is created by the migration tooling, never here.
    runs_migrations = False

    like_test_name = _LIKE_TEST_NAME
    limit_all_offset = _LIMIT_ALL_OFFSET

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        driver = _driver()
        #: Storage catches this around its user-creation races.
        self.integrity_error = driver.IntegrityError
        self._driver_module = driver
        self._sql_cache = {}  # type: Dict[str, str]

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def connect(self) -> _Connection:
        """One wrapped connection for the calling thread."""
        return _Connection(self)

    def raw_connect(self) -> Any:
        """A configured driver connection, strictness verified.

        Every kwarg here is load-bearing:

        * ``client_flag=FOUND_ROWS``: UPDATE reports rows MATCHED, as
          SQLite does. Without it a re-declared value reports 0 and the
          UPDATE-then-INSERT sites would raise duplicate keys.
        * ``binary_prefix=True``: captured outputs are zlib bytes, not
          valid utf8mb4; the prefix keeps them out of charset
          conversion.
        * ``autocommit=True``: matches SQLite's isolation_level=None —
          transactions are the explicit BEGIN/COMMIT in storage.py and
          nothing else.
        * ``init_command``: the lock-wait bound, re-applied by the
          driver on any reconnect (which is what makes ping-reconnect
          safe to allow).
        """
        driver = self._driver_module
        from third_party.pymysql.constants import CLIENT
        kwargs = {
            "user": self.settings.user,
            "password": self.settings.password,
            "database": self.settings.database,
            "charset": "utf8mb4",
            "autocommit": True,
            "client_flag": CLIENT.FOUND_ROWS,
            "binary_prefix": True,
            "init_command": _INIT_COMMAND,
            "connect_timeout": 10,
        }  # type: Dict[str, Any]
        if self.settings.unix_socket:
            kwargs["unix_socket"] = self.settings.unix_socket
        else:
            kwargs["host"] = self.settings.host
            kwargs["port"] = self.settings.port
        conn = driver.connect(**kwargs)
        self._assert_strict(conn)
        return conn

    def _assert_strict(self, conn: Any) -> None:
        """Refuse to serve under a laxer regime than the data was loaded.

        Without strict mode an over-long value is silently truncated —
        the exact corruption the migration's preflight gates against
        (runbook §A.5). The data was loaded under strict mode; serving
        under anything less would let the running dashboard do what the
        load was forbidden to.
        """
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT @@sql_mode")
            row = cursor.fetchone()
        finally:
            cursor.close()
        mode = row[0] if row else ""
        if isinstance(mode, bytes):
            mode = mode.decode("utf-8", "replace")
        if ("STRICT_TRANS_TABLES" not in mode
                and "STRICT_ALL_TABLES" not in mode):
            conn.close()
            # ASCII only in operator-facing strings: under a non-UTF-8
            # locale, Python 3.6 cannot print a section sign, and an
            # error path that crashes while reporting masks the finding.
            raise RuntimeError(
                "this MariaDB server is not running in strict mode "
                "(sql_mode = {0!r}). The data was loaded under strict "
                "mode and serving without it silently truncates "
                "over-long values into collisions. Fix the server per "
                "docs/MARIADB_MIGRATION.md section A.5 and restart "
                "it.".format(mode))

    # ------------------------------------------------------------------
    # Dialect
    # ------------------------------------------------------------------

    def translate(self, sql: str) -> str:
        """Rewrite one storage.py statement for MariaDB, with a cache.

        Order matters twice: the ``INSERT OR REPLACE`` prefix swap runs
        before placeholder translation (both operate on the original
        text), and ``%`` doubling runs before ``?`` → ``%s`` or the
        introduced placeholders would double themselves.
        """
        cached = self._sql_cache.get(sql)
        if cached is not None:
            return cached
        stripped = sql.strip()
        text = sql
        if stripped == "BEGIN IMMEDIATE":
            text = "START TRANSACTION"
        elif stripped.startswith("INSERT OR REPLACE "):
            lead = sql[:len(sql) - len(sql.lstrip())]
            text = lead + "REPLACE " + stripped[len("INSERT OR REPLACE "):]
        translated = text.replace("%", "%%").replace("?", "%s")
        self._sql_cache[sql] = translated
        return translated

    # ------------------------------------------------------------------
    # Backend capabilities
    # ------------------------------------------------------------------

    def check_schema(self, conn: Any, latest: int) -> None:
        """Verify the loaded schema is exactly this build's version.

        Called by ``Storage._migrate`` instead of running migrations.
        Both directions refuse: an older schema means the data was
        loaded by an older checkout (reload it, or run this build's
        tooling); a newer one is the same corruption risk the SQLite
        path refuses, in the same pinned words.
        """
        try:
            row = conn.execute("SELECT version FROM schema_version") \
                .fetchone()
        except self._driver_module.ProgrammingError:
            raise RuntimeError(
                "this MariaDB database has no testboard schema (no "
                "schema_version table). The schema is created by the "
                "migration tooling, never by the dashboard - run "
                "tools/migrate_to_mariadb.py per "
                "docs/MARIADB_MIGRATION.md section D before pointing "
                "the server at it.")
        current = 0 if row is None else int(row[0])
        if current > latest:
            raise RuntimeError(
                "this database was created by a NEWER version of "
                "testboard (its schema version is {0}; this build "
                "understands up to {1}). Using it with older code "
                "could corrupt it. Deploy the checkout that loaded "
                "it.".format(current, latest))
        if current < latest:
            raise RuntimeError(
                "this MariaDB database is at schema version {0} but "
                "this build expects {1}. The dashboard never migrates "
                "MariaDB - re-run the data migration with this "
                "checkout's tooling (docs/MARIADB_MIGRATION.md section "
                "D) so the loaded schema matches the code.".format(
                    current, latest))

    def cache_bytes_per_connection(self) -> Optional[int]:
        """None: InnoDB has one shared buffer pool, not a cache per
        connection (runbook §B.4)."""
        return None

    def vacuum(self, conn: Any) -> None:
        """No-op: InnoDB manages its own space. OPTIMIZE TABLE is an
        operator's decision on the server, not the dashboard's."""
        print("vacuum: no-op on MariaDB (InnoDB manages its own space; "
              "see OPTIMIZE TABLE if reclaiming disk is the goal)")


def describe_connect_error(settings: Settings, exc: BaseException) -> str:
    """A startup failure message an operator can act on.

    The MariaDB twin of ``storage.describe_open_error`` — the same two
    findings the migration tool explains, because they are the same two
    things that go wrong: an auth plugin the vendored driver cannot do,
    and MariaDB's host-based account matching.
    """
    if "cryptography" in str(exc):
        return (
            "could not connect as {0}: the account uses a sha256-based "
            "auth plugin, which the vendored driver cannot do without "
            "the compiled 'cryptography' package.\nDo NOT install "
            "cryptography on the server - that gives up the 'nothing "
            "to build on the server' property. Have the account "
            "recreated with mysql_native_password instead "
            "(docs/MARIADB_MIGRATION.md section A.4).".format(
                settings.describe()))
    return (
        "could not connect as {0}: {1}\nIf that says access denied: "
        "MariaDB matches an account against the host it sees the "
        "connection coming from, so 'localhost' (the socket) is a "
        "different account from the machine's own IP (TCP). The "
        "vendored driver does NOT treat host=localhost as the socket - "
        "add a socket= line to the option file, or make the grant "
        "match (docs/MARIADB_MIGRATION.md sections A.1 and "
        "A.9).".format(settings.describe(), exc))
