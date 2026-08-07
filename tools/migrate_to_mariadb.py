#!/usr/bin/env python3
"""Run the SQLite -> MariaDB migration as a script instead of by hand.

``docs/MARIADB_MIGRATION.md`` describes the migration as a sequence of
SQL statements a person types. That was honest but wrong-shaped: the
steps that matter most are the *gates* (is the collation case-sensitive?
is strict mode on? are there orphan rows?), and a gate a tired person
can skip at 2am is not a gate. This runs the whole unprivileged half —
audit, preflight, export, load, verify — and **stops** at the first
failed gate with a non-zero exit code.

What it deliberately does NOT do:

* Anything needing the MariaDB root password. Creating the database and
  the two accounts stays in the runbook's §A, done once by whoever holds
  root. This script only ever uses ``testboard_migrate``.
* Freeze the feeder (§E.2) or restart it (§E.5). Those are decisions
  about when users lose writes, and they belong to a person.

It talks to MariaDB through the **vendored** driver in
``third_party/pymysql``, never by shelling out to the ``mysql`` client.
That is not a style choice. This project's deployment property is that
a checkout runs on a RHEL 8 box with nothing installed, no build step
and no virtualenv; a migration that needed a client package on the web
server would break exactly the property vendoring exists to provide,
and would do it at the moment the estate is least able to absorb it.
The driver is *present*, not *installed*.

It also means the preflight connects with the same driver the dashboard
itself will use (§F), so an auth plugin the driver cannot do fails here
rather than the first time the service starts.

**What is verified and what is not.** Everything that touches SQLite —
the audit, the sizing arithmetic, the comparison logic — is covered by
``tests/test_migrate_mariadb.py``. Nothing that talks to MariaDB is:
there is no MariaDB in this development environment or in CI, so those
paths are driven in tests by a fake client returning scripted answers.
That proves the decisions, not the SQL. It is exactly why §E.1's dry run
against a copy of production is not optional.

Usage::

    python3 tools/migrate_to_mariadb.py audit     --db PATH
    python3 tools/migrate_to_mariadb.py preflight --config CNF
    python3 tools/migrate_to_mariadb.py export    --db PATH --out DIR
    python3 tools/migrate_to_mariadb.py load      --config CNF --out DIR
    python3 tools/migrate_to_mariadb.py verify    --config CNF --out DIR
    python3 tools/migrate_to_mariadb.py all       --db PATH --out DIR \\
                                                  --config CNF

Python 3.6 compatible; standard library only.
"""

import argparse
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
from typing import (
    Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple,
)

if __name__ == "__main__" and __package__ is None:  # pragma: no cover
    # Run as a path (``python3 tools/migrate_to_mariadb.py``) the repo
    # root is not on sys.path, so ``from tools import ...`` fails. The
    # runbook tells people to run it exactly that way.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import export_for_mariadb as exporter  # noqa: E402


#: Exit code for a failed gate — a real finding, not a crash. Separate
#: from 2 (usage/unexpected error) so a wrapper can tell them apart.
EXIT_GATE_FAILED = 3

#: Export bytes per byte of database, for the free-space check. The
#: export is text and the blobs are hex-encoded, so it is bigger than
#: the database it came from; 2.5x matches the runbook's §0 estimate.
EXPORT_SIZE_FACTOR = 2.5

#: Identity columns are sized from the audit, not guessed: a measured
#: maximum times this, rounded up, so a name that grows a little after
#: the audit does not mean re-running the whole migration. Doubling is
#: as generous as the 3072-byte index key allows — utf8mb4 is charged
#: at 4 bytes a character, so the three columns together may total only
#: 761 characters and the runbook's defaults already spend 574 of them.
SIZE_HEADROOM = 2

#: ...but never smaller than the runbook's §B.1 defaults.
MIN_SIZES = {"environment": 64, "script": 255, "test_name": 255}

#: The blob has to fit in one packet with room for protocol overhead
#: and for the hex form the load sends.
PACKET_SAFETY_FACTOR = 2.5


class Settings(NamedTuple):
    """Connection details, read from a mysql option file."""

    host: str
    port: int
    user: str
    password: str
    database: str
    unix_socket: Optional[str]

    def describe(self) -> str:
        """One line for the log. Never contains the password."""
        where = self.unix_socket or "{0}:{1}".format(self.host, self.port)
        return "{0}@{1}/{2}".format(self.user, where, self.database)


class Check(NamedTuple):
    """One gate's outcome.

    ``blocking`` is decided when the check is defined, not when it
    fails: an orphan row stops the migration, a long test name only
    changes a VARCHAR. Mixing the two is how a "warning" ends up being
    the thing that lost the data.
    """

    name: str
    ok: bool
    detail: str
    blocking: bool
    advice: str


class Audit(NamedTuple):
    """What the SQLite database says about itself."""

    checks: List[Check]
    sizes: Dict[str, int]
    max_blob_bytes: int
    total_blob_bytes: int
    volumes: Dict[str, int]
    schema_version: Optional[int]

    def failed(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.blocking]


# --------------------------------------------------------------------
# Option file
# --------------------------------------------------------------------

def read_option_file(path: str) -> Settings:
    """Parse a mysql ``[client]`` option file.

    Deliberately the same file format the ``mysql`` client reads
    (runbook §A.9), so there is one credentials format in this project
    and not two. ``configparser`` is not used: my.cnf allows bare keys
    with no ``=`` (``local-infile``) and ``!includedir`` directives,
    both of which it rejects outright.

    The password never comes from a command line. Anything on a command
    line is visible to every user on the box through ``ps``.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise SystemExit(
            "no option file at {0}. Create one as shown in "
            "docs/MARIADB_MIGRATION.md §A.9 and chmod it 600.".format(
                expanded))
    _warn_if_world_readable(expanded)

    values = {}  # type: Dict[str, str]
    section = ""
    with io.open(expanded, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line[0] in "#;!":
                continue
            if line.startswith("["):
                section = line.strip("[]").strip().lower()
                continue
            if section not in ("client", "mysql", "testboard"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip().lower().replace("_", "-")] = _unquote(
                    value.strip())
            else:
                values[line.lower().replace("_", "-")] = "1"

    missing = [k for k in ("user", "password", "database")
               if not values.get(k)]
    if missing:
        raise SystemExit(
            "option file {0} is missing: {1}. It needs host, user, "
            "password and database under a [client] section.".format(
                expanded, ", ".join(missing)))

    return Settings(
        host=values.get("host", "127.0.0.1"),
        port=int(values.get("port", "3306")),
        user=values["user"],
        password=values["password"],
        database=values["database"],
        unix_socket=values.get("socket") or None,
    )


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, as the mysql client does."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _warn_if_world_readable(path: str) -> None:
    """A credentials file readable by everyone is a finding, not style.

    Not fatal — refusing to run would block a dry run for no safety
    gain — and POSIX-only, because on Windows every file looks
    group-readable and a warning that always fires is one nobody reads.
    """
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:  # pragma: no cover - unreadable file already failed
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print("WARNING: {0} is readable beyond its owner. It holds a "
              "database password: chmod 600 it.".format(path))


# --------------------------------------------------------------------
# The SQLite audit (runbook §C.1)
# --------------------------------------------------------------------

def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def audit(db_path: str) -> Audit:
    """Read-only audit of the SQLite source. Never writes.

    Everything here is the runbook's §C.1, with the answers turned into
    decisions rather than into numbers a person has to interpret: the
    VARCHAR sizes come out of the measured maxima, and the orphan
    counts become a gate because InnoDB will refuse the load.
    """
    conn = sqlite3.connect("file:{0}?mode=ro".format(db_path), uri=True)
    try:
        checks = []  # type: List[Check]

        present = set(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))
        missing = [t for t in exporter.TABLE_ORDER if t not in present]
        checks.append(Check(
            "source_tables", not missing,
            "missing: {0}".format(", ".join(missing)) if missing
            else "all {0} tables present".format(len(exporter.TABLE_ORDER)),
            blocking=True,
            advice="The export tool expects tables this database does "
                   "not have, so the export would fail partway. The "
                   "usual cause is a database older than the code: the "
                   "schema migrations have not been applied to this "
                   "file. Open it once with the current server (which "
                   "runs them), then audit again — and make sure the "
                   "code you migrate with is the code that is "
                   "deployed."))
        if missing:
            # Nothing below can be trusted about a half-known schema,
            # and a stack trace here would look like a tool bug rather
            # than the finding it is.
            return Audit(checks=checks, sizes=dict(MIN_SIZES),
                         max_blob_bytes=0, total_blob_bytes=0,
                         volumes={}, schema_version=None)

        row = conn.execute(
            "SELECT MAX(LENGTH(environment)), MAX(LENGTH(script)), "
            "MAX(LENGTH(test_name)), MAX(LENGTH(source_link)) "
            "FROM runs").fetchone()
        observed = {
            "environment": row[0] or 0,
            "script": row[1] or 0,
            "test_name": row[2] or 0,
        }
        sizes = choose_sizes(observed)
        checks.append(Check(
            "identity_lengths", True,
            "longest: environment {0}, script {1}, test_name {2}; "
            "chose VARCHAR {3}/{4}/{5}".format(
                observed["environment"], observed["script"],
                observed["test_name"], sizes["environment"],
                sizes["script"], sizes["test_name"]),
            blocking=False, advice=""))

        link = row[3] or 0
        checks.append(Check(
            "source_link_length", link <= 1024,
            "longest source_link is {0} of 1024".format(link),
            blocking=True,
            advice="A source_link longer than the VARCHAR(1024) in the "
                   "generated schema would be truncated (or rejected "
                   "under strict mode). Widen it before loading."))

        stamps = conn.execute(
            "SELECT MIN(LENGTH(start_time)), MAX(LENGTH(start_time)), "
            "MIN(LENGTH(end_time)), MAX(LENGTH(end_time)) "
            "FROM runs").fetchone()
        stamp_ok = all(v in (None, 26) for v in stamps)
        checks.append(Check(
            "timestamp_widths", stamp_ok,
            "start_time {0}-{1}, end_time {2}-{3} characters".format(*stamps),
            blocking=True,
            advice="Every timestamp must be exactly 26 characters or "
                   "lexical ordering is already broken — and the "
                   "migration is not the cause. Fix the source first; "
                   "VARCHAR(26) would truncate anything longer."))

        ragged = _scalar(conn,
                         "SELECT COUNT(*) FROM runs "
                         "WHERE environment <> TRIM(environment) "
                         "OR script <> TRIM(script) "
                         "OR test_name <> TRIM(test_name)")
        checks.append(Check(
            "identity_whitespace", ragged == 0,
            "{0} run(s) have leading or trailing space in an identity "
            "column".format(ragged),
            blocking=False,
            advice="Harmless under utf8mb4_nopad_bin, which is what the "
                   "runbook's §A.3 asks for. On a PAD SPACE collation "
                   "these merge with their trimmed twins — so if §A.3 "
                   "was not followed exactly, this count is the damage."))

        collide = _scalar(conn,
                          "SELECT COUNT(*) - COUNT(DISTINCT "
                          "LOWER(environment) || '/' || LOWER(script) || "
                          "'/' || LOWER(test_name) || '/' || start_time) "
                          "FROM runs")
        users_collide = _scalar(
            conn, "SELECT COUNT(*) - COUNT(DISTINCT LOWER(username)) "
                  "FROM users")
        checks.append(Check(
            "case_collisions", True,
            "{0} run(s) and {1} user(s) differ only by case".format(
                collide, users_collide),
            blocking=False,
            advice="Not a blocker — it is the measurement of what a "
                   "case-insensitive collation would silently merge. If "
                   "either number is above zero, the C.2 collation probe "
                   "is the only thing standing between you and data "
                   "loss, and it must pass."))

        orphans = [
            ("run_outputs -> runs",
             "SELECT COUNT(*) FROM run_outputs o LEFT JOIN runs r "
             "ON r.id = o.run_id WHERE r.id IS NULL"),
            ("latest_runs -> runs",
             "SELECT COUNT(*) FROM latest_runs l LEFT JOIN runs r "
             "ON r.id = l.run_id WHERE r.id IS NULL"),
            ("comments -> users",
             "SELECT COUNT(*) FROM comments c LEFT JOIN users u "
             "ON u.username = c.author WHERE u.username IS NULL"),
            ("assignments -> users",
             "SELECT COUNT(*) FROM assignments a LEFT JOIN users u "
             "ON u.username = a.assigned_by WHERE u.username IS NULL"),
            ("current_assignments -> users",
             "SELECT COUNT(*) FROM current_assignments ca LEFT JOIN users u "
             "ON u.username = ca.assignee "
             "WHERE ca.assignee IS NOT NULL AND u.username IS NULL"),
        ]
        found = []  # type: List[str]
        for label, sql in orphans:
            count = _scalar(conn, sql)
            if count:
                found.append("{0}: {1}".format(label, count))
        checks.append(Check(
            "orphan_rows", not found,
            "; ".join(found) if found else "none",
            blocking=True,
            advice="SQLite never enforced these references, so these "
                   "rows point at parents that do not exist. The "
                   "generated schema does not enforce them either "
                   "(runbook §B.6) — they would load without complaint, "
                   "and that is the problem: a migration is the one "
                   "moment dangling rows are cheap to find. Delete them "
                   "in the source, or fix whatever produced them, "
                   "before exporting."))

        blob = conn.execute(
            "SELECT MAX(LENGTH(output)), SUM(LENGTH(output)) "
            "FROM run_outputs").fetchone()
        max_blob = blob[0] or 0
        total_blob = blob[1] or 0

        volumes = {}  # type: Dict[str, int]
        for table in exporter.TABLE_ORDER:
            volumes[table] = _scalar(
                conn, "SELECT COUNT(*) FROM {0}".format(table)) or 0

        version = _scalar(conn, "SELECT version FROM schema_version")

        return Audit(checks=checks, sizes=sizes, max_blob_bytes=max_blob,
                     total_blob_bytes=total_blob, volumes=volumes,
                     schema_version=version)
    finally:
        conn.close()


def choose_sizes(observed: Dict[str, int]) -> Dict[str, int]:
    """Pick VARCHAR lengths from measured maxima.

    Generous where it can be — headroom is free and re-running a 950 MB
    migration because a test name grew by eight characters is not — but
    the 3072-byte index key is a hard InnoDB limit, and utf8mb4 is
    charged at four bytes a character, so the three identity columns
    may total only 761 characters between them (runbook §B.1).

    So: ask for the generous sizing, and if it does not fit, tighten
    rather than refuse. Refusing to migrate data that *does* fit, only
    because the padding around it does not, would be a tool being
    fussy at the operator's expense. It refuses only when the values
    themselves do not fit, which is a real finding about the source.
    """
    attempts = (
        # (headroom, apply the runbook's default floors)
        (SIZE_HEADROOM, True),
        (1.25, False),
        (1.0, False),
    )
    tightest = {}  # type: Dict[str, int]
    for headroom, use_floor in attempts:
        sizes = {}  # type: Dict[str, int]
        for column, minimum in sorted(MIN_SIZES.items()):
            want = _round_up(int(observed.get(column, 0) * headroom) + 1, 32)
            sizes[column] = max(minimum, want) if use_floor else want
        tightest = sizes
        budget = exporter.Sizes(sizes["environment"], sizes["script"],
                                sizes["test_name"])
        if budget.index_bytes() <= 3072:
            return sizes

    budget = exporter.Sizes(tightest["environment"], tightest["script"],
                            tightest["test_name"])
    raise SystemExit(
        "the values in this database do not fit an indexable schema: "
        "environment {0}, script {1}, test_name {2} characters need {3} "
        "bytes of index key and InnoDB allows 3072 "
        "(docs/MARIADB_MIGRATION.md §B.1). This is a finding about the "
        "source, not a setting to relax — something has an identity "
        "value far longer than the estate's norm. Find it before "
        "migrating: SELECT environment, script, test_name FROM runs "
        "ORDER BY LENGTH(script) + LENGTH(test_name) DESC LIMIT "
        "10;".format(observed.get("environment", 0),
                     observed.get("script", 0),
                     observed.get("test_name", 0), budget.index_bytes()))


def _round_up(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


# --------------------------------------------------------------------
# MariaDB side
# --------------------------------------------------------------------

class DatabaseError(Exception):
    """A statement the server refused. Carries which one."""

    def __init__(self, message: str, statement: str = "") -> None:
        Exception.__init__(self, message)
        self.statement = statement

    def __str__(self) -> str:
        base = Exception.__str__(self)
        if not self.statement:
            return base
        return "{0}\n  in: {1}".format(base, first_line(self.statement))


class Database(object):
    """The MariaDB side, over the vendored PyMySQL.

    Through the driver rather than the ``mysql`` command-line client,
    and the reason is the same one that put the driver in the tree at
    all: **the migration must not depend on anything being installed on
    the web server.** Shelling out would have made a client RPM a
    prerequisite of the migration, which is precisely the deployment
    problem vendoring removes. `third_party/pymysql` is *present*, not
    *installed* — copy the checkout and run it.

    A second thing falls out of it for free: this connects with the same
    driver the dashboard itself will use once §F lands, so an auth
    plugin the driver cannot do is found here, in the preflight, and not
    the first time the service starts.

    One connection for the whole run, deliberately: ``load.sql`` opens
    with ``SET FOREIGN_KEY_CHECKS = 0``, and session variables die with
    the session that set them.
    """

    def __init__(self, settings: Settings, local_infile: bool = False) -> None:
        self.settings = settings
        self.local_infile = local_infile
        self.conn = self._connect()

    def _connect(self) -> Any:
        pymysql = _driver()
        kwargs = {
            "user": self.settings.user,
            "password": self.settings.password,
            "database": self.settings.database,
            "charset": "utf8mb4",
            "local_infile": self.local_infile,
            "autocommit": True,
            # PyMySQL's own max_allowed_packet (16 MB by default) bounds
            # what this side SENDS. It is left alone deliberately: the
            # statements here are tiny, and LOAD DATA LOCAL INFILE
            # streams the .tsv in chunks of min(max_allowed_packet, 16
            # KB) — see LoadLocalFile in the vendored connections.py. So
            # a large captured output cannot overflow a packet on the
            # way in, whatever its size. The server-side check in
            # preflight() still matters for §F, where reading a blob
            # back means the SERVER sends one large packet.
        }  # type: Dict[str, Any]
        if self.settings.unix_socket:
            kwargs["unix_socket"] = self.settings.unix_socket
        else:
            kwargs["host"] = self.settings.host
            kwargs["port"] = self.settings.port
        try:
            return pymysql.connect(**kwargs)
        except RuntimeError as exc:
            if "cryptography" in str(exc):
                raise SystemExit(_SHA256_ADVICE.format(self.settings.user))
            raise SystemExit(
                "could not connect as {0}: {1}".format(
                    self.settings.describe(), exc))
        except Exception as exc:
            raise SystemExit(
                "could not connect as {0}: {1}\nIf that says access "
                "denied: MariaDB matches an account against the host it "
                "sees the connection coming from, so 'localhost' (the "
                "socket) is a different account from the machine's own "
                "IP (TCP). Check which host the grant in §A.4 was "
                "written for.".format(self.settings.describe(), exc))

    def rows(self, sql: str) -> List[Tuple[Any, ...]]:
        """Run one statement and return every row."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            fetched = cursor.fetchall()
            return [tuple(row) for row in fetched] if fetched else []
        except Exception as exc:
            raise DatabaseError(str(exc), sql)
        finally:
            cursor.close()

    def run(self, sql: str) -> None:
        """Run one statement for its effect."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as exc:
            raise DatabaseError(str(exc), sql)
        finally:
            cursor.close()

    def run_file(self, path: str, cwd: str,
                 log: Optional[Callable[[str], None]] = None) -> int:
        """Execute a generated .sql file, one statement at a time.

        Returns the number of statements run. *cwd* is the export
        directory, used to resolve the relative data-file names in
        ``load.sql`` — see :func:`absolutise_infile` for why they are
        rewritten rather than resolved by changing directory.

        Reading the file whole is fine: ``schema.sql`` and ``load.sql``
        are a few kilobytes each — the 2.4 GB is in the ``.tsv`` files,
        which the *server* pulls from the driver and which never pass
        through here.

        Every statement is logged as it finishes. A single ``LOAD DATA``
        of the ``runs`` table can run for tens of minutes on a 950 MB
        database, and on cutover night silence and a hang look the same.
        """
        say = log or (lambda message: None)
        with io.open(path, encoding="utf-8") as handle:
            statements = split_statements(handle.read())
        for number, statement in enumerate(statements, 1):
            started = time.time()
            self.run(absolutise_infile(statement, cwd))
            say("  [{0}/{1}] {2}  {3:.0f}s".format(
                number, len(statements), first_line(statement),
                time.time() - started))
        return len(statements)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - closing a dead socket
            pass


_SHA256_ADVICE = (
    "the account {0} uses a sha256-based auth plugin, which the "
    "vendored driver cannot do without the compiled 'cryptography' "
    "package.\nDo NOT install cryptography on the server — that gives "
    "up the 'nothing to build on the server' property this whole "
    "design protects. Have the account recreated with "
    "mysql_native_password instead (runbook §A.4).")


def _driver() -> Any:
    """Import the vendored driver, or say where it should have been."""
    try:
        from third_party import pymysql
    except ImportError as exc:  # pragma: no cover - vendored, always there
        raise SystemExit(
            "the vendored MySQL driver is missing ({0}). It should be in "
            "third_party/pymysql, and it ships with the repository — see "
            "third_party/README.md. Nothing needs installing; if the "
            "directory is absent, the checkout is "
            "incomplete.".format(exc))
    return pymysql


def connect(settings: Settings, local_infile: bool = False) -> Database:
    """Open the connection and prove it works before relying on it."""
    return Database(settings, local_infile=local_infile)


def query(conn: Any, sql: str) -> List[Tuple[Any, ...]]:
    """Run one statement and return every row."""
    return conn.rows(sql)


def execute(conn: Any, sql: str) -> None:
    """Run one statement for its effect."""
    conn.run(sql)


def server_settings(conn: Any) -> Dict[str, str]:
    """The server variables the runbook's §A.5 cares about."""
    row = query(conn,
                "SELECT VERSION(), @@sql_mode, @@max_allowed_packet, "
                "@@local_infile, @@character_set_database, "
                "@@collation_database")[0]
    names = ("version", "sql_mode", "max_allowed_packet", "local_infile",
             "character_set_database", "collation_database")
    return dict(zip(names, [_text(v) for v in row]))


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def preflight(conn: Any, max_blob_bytes: int = 0,
              need_local_infile: bool = True,
              allow_non_empty: bool = False) -> List[Check]:
    """Prove the server is fit to load into, before loading into it.

    These are the runbook's §C.2, §C.3 and §C.4 — the five seconds that
    are worth more than the rest of the procedure. Each one is a gate:
    the collation probe failing means loading now would merge distinct
    tests permanently, so there is nothing useful to do afterwards
    except reload from scratch.
    """
    checks = []  # type: List[Check]
    settings = server_settings(conn)

    version = settings["version"]
    checks.append(Check(
        "server_version", _version_tuple(version) >= (10, 2),
        "MariaDB {0}".format(version), blocking=True,
        advice="Below 10.2 there is no utf8mb4_nopad_bin, so trailing "
               "spaces compare equal and the collation guidance in the "
               "runbook's §B.3 changes. Upgrade, or read §B.3 and "
               "accept the difference knowingly."))
    if _version_tuple(version) < (10, 6):
        print("NOTE: {0} is below the 10.6 LTS line the runbook targets. "
              "Supported, but check your vendor's support window."
              .format(version))

    strict = "STRICT_TRANS_TABLES" in settings["sql_mode"] or \
             "STRICT_ALL_TABLES" in settings["sql_mode"]
    checks.append(Check(
        "sql_mode_strict", strict,
        "sql_mode = {0}".format(settings["sql_mode"] or "(empty)"),
        blocking=True,
        advice="Without strict mode an over-long test name is silently "
               "truncated, and two different tests become one "
               "permanently with no error. This is the most damaging "
               "thing that can go wrong in the whole migration "
               "(runbook §A.5)."))

    collation = settings["collation_database"]
    checks.append(Check(
        "database_collation", collation.endswith("_bin"),
        "collation_database = {0}".format(collation), blocking=True,
        advice="testboard identifies a test by an exact triple and a "
               "user by an exact name. A case-insensitive collation "
               "merges 'Login' with 'login'. Have the database "
               "recreated per the runbook's §A.3."))

    packet = int(settings["max_allowed_packet"] or 0)  # client: a string
    needed = int(max_blob_bytes * PACKET_SAFETY_FACTOR)
    # `load` run on its own — which is exactly what the retry advice
    # tells you to do — has no audit behind it and so no measurement.
    # The 64 MB floor below still gates; what must not happen is the
    # line claiming the largest output is zero bytes, which reads as a
    # measurement rather than as its absence.
    measured = (
        "{0:,} bytes".format(max_blob_bytes) if max_blob_bytes
        else "not measured (run 'audit', or pass --max-blob-bytes)")
    checks.append(Check(
        "max_allowed_packet", packet >= max(needed, 64 * 1024 * 1024),
        "max_allowed_packet = {0:,} bytes; largest captured output is "
        "{1}".format(packet, measured), blocking=True,
        advice="The load itself streams in 16 KB chunks, so this will "
               "not break the migration — but the dashboard reads a "
               "whole captured output in one row (GET /api/runs/{id}), "
               "and the server has to be able to send that in one "
               "packet. Raise it in the server config (runbook §A.5) "
               "before §F, not after."))

    if need_local_infile:
        checks.append(Check(
            "local_infile", settings["local_infile"].upper()
            in ("1", "ON", "TRUE"),
            "local_infile = {0}".format(settings["local_infile"]),
            blocking=True,
            advice="The bulk load path needs it on the server. It can "
                   "be turned off again afterwards. Without it, fall "
                   "back to a client-side load (runbook §D.3)."))

    checks.append(_grant_probe(conn))
    checks.append(_collation_probe(conn))
    checks.append(_strict_probe(conn))

    tables = [_text(r[0]) for r in query(conn, "SHOW TABLES")]
    real = [t for t in tables if not t.startswith("_")]
    checks.append(Check(
        "target_is_empty", not real or allow_non_empty,
        "{0} table(s) already present: {1}".format(
            len(real), ", ".join(sorted(real)) or "none"),
        blocking=True,
        advice="Loading on top of an existing schema produces a mixture "
               "of two loads that no verification can untangle. If this "
               "is the wreckage of a failed load, drop the tables and "
               "run the load again — testboard_migrate holds DROP on "
               "this database, so it can do that itself (it cannot drop "
               "the database, which needs whoever ran §A). Or pass "
               "--force if you know it is a scratch database."))
    return checks


def _version_tuple(version: str) -> Tuple[int, ...]:
    """Parse '10.6.16-MariaDB-log' into (10, 6, 16)."""
    parts = []  # type: List[int]
    for chunk in version.split("-")[0].split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)


def _grant_probe(conn: Any) -> Check:
    """Prove the account can do what the load needs (runbook §C.3)."""
    try:
        execute(conn, "DROP TABLE IF EXISTS _tb_grant_probe")
        execute(conn, "CREATE TABLE _tb_grant_probe "
                      "(id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                      "v VARCHAR(10))")
        execute(conn, "INSERT INTO _tb_grant_probe (v) VALUES ('ok')")
        execute(conn, "DROP TABLE _tb_grant_probe")
        return Check("grants", True,
                     "create, insert and drop all succeeded",
                     blocking=True, advice="")
    except Exception as exc:
        return Check(
            "grants", False, str(exc), blocking=True,
            advice="Cheaper to find out now than four hours into a "
                   "load. This should be the testboard_migrate account, "
                   "not testboard_app — the app account deliberately "
                   "cannot create tables (runbook §A.4).")


def _collation_probe(conn: Any) -> Check:
    """The single most valuable five seconds (runbook §C.2).

    'a', 'A' and 'a ' must survive as three distinct primary keys. If
    they do not, the collation would merge distinct tests during the
    load and the damage is not repairable without a full reload.
    """
    try:
        execute(conn, "DROP TABLE IF EXISTS _tb_collation_probe")
        execute(conn, "CREATE TABLE _tb_collation_probe "
                      "(k VARCHAR(64) NOT NULL PRIMARY KEY)")
        try:
            execute(conn, "INSERT INTO _tb_collation_probe (k) VALUES "
                          "('a'), ('A'), ('a ')")
            count = int(query(
                conn, "SELECT COUNT(*) FROM _tb_collation_probe")[0][0])
        finally:
            execute(conn, "DROP TABLE _tb_collation_probe")
    except Exception as exc:
        return Check("collation_probe", False,
                     "probe failed: {0}".format(exc), blocking=True,
                     advice=_COLLATION_ADVICE)
    return Check(
        "collation_probe", count == 3,
        "'a', 'A' and 'a ' stored as {0} row(s), want 3".format(count),
        blocking=True, advice=_COLLATION_ADVICE)


_COLLATION_ADVICE = (
    "The database is not case-sensitive and NO PAD. Loading now would "
    "merge distinct tests into one, irreversibly. Stop: have the "
    "database recreated with utf8mb4_nopad_bin (runbook §A.3). A "
    "duplicate-key error here is the same finding.")


def _strict_probe(conn: Any) -> Check:
    """Prove truncation is loud (runbook §C.4).

    Inverted on purpose: the INSERT *must* fail. A probe that passes by
    succeeding cannot tell "strict mode is on" from "the statement did
    not run".
    """
    try:
        execute(conn, "DROP TABLE IF EXISTS _tb_strict_probe")
        execute(conn, "CREATE TABLE _tb_strict_probe (v VARCHAR(4))")
    except Exception as exc:
        return Check("strict_probe", False,
                     "could not create the probe table: {0}".format(exc),
                     blocking=True, advice="")
    truncated = False
    try:
        execute(conn, "INSERT INTO _tb_strict_probe VALUES ('toolong')")
        truncated = True
    except Exception:
        truncated = False
    finally:
        execute(conn, "DROP TABLE _tb_strict_probe")
    return Check(
        "strict_probe", not truncated,
        "an over-long value was {0}".format(
            "SILENTLY TRUNCATED" if truncated else "rejected, as it must be"),
        blocking=True,
        advice="Strict mode is off: over-long values are truncated with "
               "no error, which merges distinct tests. Fix sql_mode "
               "(runbook §A.5) and start again — a load done without it "
               "cannot be trusted even if the counts match.")


# --------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------

def split_statements(sql: str) -> List[str]:
    r"""Split a generated script into statements.

    The driver sends one statement per ``execute``, so the generated
    files have to be cut up. Naive splitting on ``;`` corrupts the load:
    ``load.sql``'s ``FIELDS TERMINATED BY '\t' ESCAPED BY '\\'`` clauses
    are full of quoted punctuation, and a semicolon inside a quoted
    string ends nothing. Quote state and backslash escapes are tracked;
    ``--`` comments run to end of line.

    This is deliberately narrow: it parses the two files *this
    repository generates*, which contain no stored routines and no
    ``DELIMITER`` directives. ``tests/test_migrate_mariadb.py`` runs it
    over the real generated output rather than over sample SQL.
    """
    statements = []  # type: List[str]
    current = []  # type: List[str]
    quote = ""
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote:
            current.append(char)
            if char == "\\" and index + 1 < length:
                current.append(sql[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
            index += 1
            continue
        if sql[index:index + 2] == "--":
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue
        if char == ";":
            statements.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def absolutise_infile(statement: str, out_dir: str) -> str:
    """Point a ``LOAD DATA LOCAL INFILE`` at the export directory.

    The generated ``load.sql`` names its data files relatively, because
    it is also usable by hand from inside that directory. The driver
    opens the path relative to *this process's* working directory, which
    would either fail or — much worse on a re-run — read a stale file of
    the same name from somewhere else. Rewriting the path is safer than
    ``os.chdir``, which is process-global and would outlive the load.
    """
    marker = "INFILE '"
    start = statement.find(marker)
    if start == -1:
        return statement
    start += len(marker)
    end = statement.find("'", start)
    if end == -1:
        return statement
    name = statement[start:end]
    # startswith("/") as well as isabs(): the target is a RHEL box, and
    # on a Windows dev machine isabs("/data/x") is False.
    if os.path.isabs(name) or name.startswith("/"):
        return statement
    full = os.path.abspath(os.path.join(out_dir, name))
    return statement[:start] + full.replace("\\", "/") + statement[end:]


def first_line(statement: str) -> str:
    """The head of a statement, for a log line or an error."""
    line = statement.strip().split("\n")[0].strip()
    return line if len(line) <= 70 else line[:67] + "..."


def run_sql_file(conn: Any, path: str, out_dir: str,
                 log: Callable[[str], None]) -> None:
    """Execute one generated .sql file, and time it."""
    name = os.path.basename(path)
    started = time.time()
    try:
        count = conn.run_file(path, cwd=out_dir, log=log)
    except DatabaseError as exc:
        raise SystemExit(
            "{0} failed after {1:.0f}s.\n{2}\nThe appendix in "
            "docs/MARIADB_MIGRATION.md lists each common cause of this "
            "with its fix. Nothing is half-loaded that a reload cannot "
            "replace — to retry: drop the tables that were created "
            "(testboard_migrate can), fix the cause, and run the load "
            "step again.".format(name, time.time() - started, exc))
    log("  {0}: {1} statement(s) in {2:.0f}s".format(
        name, count, time.time() - started))


# --------------------------------------------------------------------
# Verification (runbook §E.4)
# --------------------------------------------------------------------

def render_rows(rows: Sequence[Sequence[Any]]) -> List[str]:
    """Format MariaDB rows exactly as the exporter formats SQLite's.

    The two engines return different Python types for the same answer —
    ``SUM()`` is an int on SQLite and a ``Decimal`` on MariaDB, and text
    can arrive as bytes — so comparing repr()s would report a mismatch
    on every single check and teach the operator to ignore the output.
    """
    out = []  # type: List[str]
    for row in rows:
        out.append("\t".join(_render(value) for value in row))
    return out


def _render(value: Any) -> str:
    if value is None:
        return exporter.NULL
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    text = str(value)
    # Decimal('4400000') and int 4400000 must render the same; so must
    # Decimal('4400000.0'), which SUM can produce.
    if "." in text:
        stripped = text.rstrip("0").rstrip(".")
        if stripped and all(c.isdigit() or c == "-" for c in stripped):
            return stripped or "0"
    return text


def parse_verify_source(text: str) -> Dict[str, List[str]]:
    """Read ``verify_source.txt`` back into {check: [lines]}."""
    sections = {}  # type: Dict[str, List[str]]
    name = None  # type: Optional[str]
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("#"):
            continue
        if line.startswith("== "):
            name = line[3:].strip()
            sections[name] = []
            continue
        if name is not None and line.strip():
            sections[name].append(line)
    return sections


def verify(conn: Any, out_dir: str,
           log: Callable[[str], None]) -> List[Check]:
    """Compare MariaDB against the answers recorded from SQLite.

    The queries come from the exporter's single list, so both sides ask
    the identical question. Two hand-written variants would drift, and
    a verification that drifts is worse than none: it reports agreement
    between two different questions (runbook §E.4).
    """
    path = os.path.join(out_dir, "verify_source.txt")
    if not os.path.isfile(path):
        raise SystemExit(
            "no verify_source.txt in {0}. It is written by the export "
            "step, and without it there is nothing to compare "
            "against.".format(out_dir))
    with io.open(path, encoding="utf-8") as handle:
        expected = parse_verify_source(handle.read())

    checks = []  # type: List[Check]
    for name, sql in exporter.VERIFY_QUERIES:
        want = expected.get(name)
        if want is None:
            checks.append(Check(
                name, False,
                "not present in verify_source.txt — the export was made "
                "by a different version of the tool", blocking=True,
                advice=""))
            continue
        got = render_rows(query(conn, sql))
        if got == want:
            checks.append(Check(name, True,
                                "{0} row(s) agree".format(len(got)),
                                blocking=True, advice=""))
        else:
            checks.append(Check(
                name, False, _describe_difference(want, got),
                blocking=True, advice=_VERIFY_ADVICE.get(name, "")))
        log("  {0:<20} {1}".format(name, "ok" if checks[-1].ok else "DIFFERS"))
    return checks


_VERIFY_ADVICE = {
    "output_bytes":
        "The most likely thing to go wrong in the whole load. A "
        "difference here means the hex round-trip mangled a blob — "
        "usually a missing UNHEX() or a character-set conversion "
        "applied to the hex text. Blobs must never be converted.",
    "distinct_tests":
        "If MariaDB has FEWER distinct tests than SQLite, the collation "
        "merged them. Stop, drop the database, fix §A.3, reload. Do not "
        "try to repair the rows.",
    "by_day_result":
        "A per-day disagreement usually means a partial load or mangled "
        "timestamps. The day is SUBSTR-ed, not parsed, so it cannot be "
        "a date-format difference.",
    "schema_version":
        "The loaded schema_version must equal what the source recorded. "
        "A mismatch means the export and the database are not the pair "
        "you think they are.",
}


def _describe_difference(want: List[str], got: List[str]) -> str:
    """Show the first disagreement, not a wall of rows."""
    if len(want) != len(got):
        head = "{0} row(s) in SQLite, {1} in MariaDB".format(
            len(want), len(got))
    else:
        head = "{0} row(s) each".format(len(want))
    for index in range(max(len(want), len(got))):
        left = want[index] if index < len(want) else "(missing)"
        right = got[index] if index < len(got) else "(missing)"
        if left != right:
            return "{0}; first difference at row {1}: sqlite [{2}] " \
                   "mariadb [{3}]".format(head, index + 1, left, right)
    return head


# --------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------

def report(title: str, checks: Sequence[Check],
           log: Callable[[str], None]) -> bool:
    """Print the outcomes. Returns True if no blocking check failed."""
    log("")
    log(title)
    log("-" * len(title))
    for check in checks:
        if check.ok:
            mark = "ok  "
        else:
            mark = "STOP" if check.blocking else "warn"
        log("{0}  {1:<22} {2}".format(mark, check.name, check.detail))
    failures = [c for c in checks if not c.ok and c.blocking]
    for check in failures:
        if check.advice:
            log("")
            log("STOP: {0}".format(check.name))
            for line in _wrap(check.advice, 72):
                log("      " + line)
    warnings = [c for c in checks if not c.ok and not c.blocking]
    for check in warnings:
        if check.advice:
            log("")
            log("note: {0}".format(check.name))
            for line in _wrap(check.advice, 72):
                log("      " + line)
    return not failures


def _wrap(text: str, width: int) -> List[str]:
    lines = []  # type: List[str]
    current = ""  # type: str
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = word if not current else current + " " + word
    if current:
        lines.append(current)
    return lines


def check_disk_space(db_path: str, out_dir: str,
                     log: Callable[[str], None]) -> Check:
    """The export is bigger than the database. Find out before, not at 90%.

    Production is ~950 MB, so this is ~2.4 GB of TSV — and an export
    that dies on a full filesystem leaves a directory this tool will
    then refuse to reuse.
    """
    size = os.path.getsize(db_path)
    needed = int(size * EXPORT_SIZE_FACTOR)
    target = out_dir
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    free = shutil.disk_usage(target or ".").free
    return Check(
        "disk_space", free >= needed,
        "{0:.1f} GB free at {1}; the export needs about {2:.1f} GB "
        "({3:.1f} GB database x {4})".format(
            free / 1e9, target, needed / 1e9, size / 1e9,
            EXPORT_SIZE_FACTOR),
        blocking=True,
        advice="The export is text and the blobs are hex-encoded, so it "
               "is bigger than the database. Point --out at a "
               "filesystem with room, or pass --skip-space-check if you "
               "know this estimate is wrong for your data.")


# --------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------

def cmd_audit(args: argparse.Namespace) -> int:
    log = print
    log("auditing {0} (read-only)".format(args.db))
    result = audit(args.db)
    ok = report("Source audit (runbook §C.1)", result.checks, log)
    if not ok:
        # Deliberately no sizes or volumes after a failed gate: numbers
        # printed under a STOP get copied into the next command.
        return EXIT_GATE_FAILED
    log("")
    log("volumes: " + ", ".join(
        "{0} {1:,}".format(name, count)
        for name, count in sorted(result.volumes.items()) if count))
    log("captured output: {0:,} bytes total, largest {1:,}".format(
        result.total_blob_bytes, result.max_blob_bytes))
    log("schema_version: {0}".format(result.schema_version))
    log("chosen sizes: --env-len {0} --script-len {1} --test-len {2}".format(
        result.sizes["environment"], result.sizes["script"],
        result.sizes["test_name"]))
    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_to_json(result))
        log("wrote {0}".format(args.json))
    return 0 if ok else EXIT_GATE_FAILED


def _to_json(result: Audit) -> str:
    payload = {
        "sizes": result.sizes,
        "max_blob_bytes": result.max_blob_bytes,
        "total_blob_bytes": result.total_blob_bytes,
        "volumes": result.volumes,
        "schema_version": result.schema_version,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                    "blocking": c.blocking} for c in result.checks],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def cmd_preflight(args: argparse.Namespace) -> int:
    log = print
    settings = read_option_file(args.config)
    max_blob = args.max_blob_bytes
    log("connecting as {0}".format(settings.describe()))
    conn = connect(settings)
    try:
        checks = preflight(conn, max_blob_bytes=max_blob,
                           allow_non_empty=args.force)
    finally:
        conn.close()
    ok = report("Server preflight (runbook §C.2-§C.4)", checks, log)
    return 0 if ok else EXIT_GATE_FAILED


def cmd_export(args: argparse.Namespace) -> int:
    log = print
    sizes = _sizes_for(args)
    if not args.skip_space_check:
        space = check_disk_space(args.db, args.out, log)
        if not report("Space", [space], log):
            return EXIT_GATE_FAILED
    log("")
    log("exporting {0} -> {1}".format(args.db, args.out))
    log("index key budget: {0} of 3072 bytes".format(sizes.index_bytes()))
    started = time.time()
    counts = exporter.export(args.db, args.out, sizes, log=log)
    log("export finished in {0:.0f}s: {1:,} runs, {2:,} outputs".format(
        time.time() - started, counts.get("runs", 0),
        counts.get("run_outputs", 0)))
    return 0


def _sizes_for(args: argparse.Namespace) -> exporter.Sizes:
    """Sizes from the audit unless the operator overrode them."""
    if args.env_len and args.script_len and args.test_len:
        return exporter.Sizes(args.env_len, args.script_len, args.test_len)
    measured = audit(args.db).sizes
    return exporter.Sizes(
        args.env_len or measured["environment"],
        args.script_len or measured["script"],
        args.test_len or measured["test_name"])


def cmd_load(args: argparse.Namespace) -> int:
    log = print
    settings = read_option_file(args.config)
    schema = os.path.join(args.out, "schema.sql")
    load = os.path.join(args.out, "load.sql")
    for path in (schema, load):
        if not os.path.isfile(path):
            raise SystemExit(
                "{0} is missing. Run the export step first.".format(path))

    log("connecting as {0}".format(settings.describe()))
    conn = connect(settings, local_infile=True)
    try:
        checks = preflight(conn, max_blob_bytes=args.max_blob_bytes,
                           allow_non_empty=args.force)
        if not report("Server preflight (runbook §C.2-§C.4)", checks, log):
            log("")
            log("Not loading. Every check above is a gate for a reason; "
                "clearing them is cheaper than reloading 4 million rows.")
            return EXIT_GATE_FAILED

        log("")
        log("creating the schema")
        run_sql_file(conn, schema, args.out, log)
        log("")
        log("loading data — the long one. On a ~950 MB database this is "
            "tens of minutes, and no output means it is working.")
        run_sql_file(conn, load, args.out, log)
    finally:
        conn.close()
    log("")
    log("Now verify, BEFORE letting anyone in (runbook §E.4/§E.6): "
        "rollback is clean only until the first human write.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    log = print
    settings = read_option_file(args.config)
    conn = connect(settings)
    try:
        checks = verify(conn, args.out, log)
    finally:
        conn.close()
    ok = report("Verification (runbook §E.4)", checks, log)
    if ok:
        log("")
        log("Every check agrees. These are agreement checks, not proof: "
            "now open the dashboard, read a run's output (the one "
            "endpoint that reads a blob), post a comment, make an "
            "assignment.")
    return 0 if ok else EXIT_GATE_FAILED


def cmd_all(args: argparse.Namespace) -> int:
    """Audit, preflight, export, load, verify — stopping at the first no.

    **The server preflight runs before the export, not after it.** The
    collation probe costs a second and the export costs twenty minutes
    and 2.4 GB; a gate that fires after the expensive step is not a
    gate, and on cutover night those twenty minutes are inside the
    freeze window.

    Time every phase: the dry run against a copy of production is the
    only honest estimate of the downtime window (runbook §E.1), and it
    is only an estimate if somebody wrote the numbers down.
    """
    log = print
    phases = []  # type: List[Tuple[str, float]]
    overall = time.time()

    started = time.time()
    result = audit(args.db)
    if not report("Source audit (runbook §C.1)", result.checks, log):
        return EXIT_GATE_FAILED
    args.max_blob_bytes = result.max_blob_bytes
    # The sizes are measured once, here, and handed on: re-deriving them
    # inside the export would mean a second pass of MAX(LENGTH(...)) and
    # five orphan LEFT JOINs over 4.4M rows, inside the window §E.1
    # exists to measure.
    args.env_len = args.env_len or result.sizes["environment"]
    args.script_len = args.script_len or result.sizes["script"]
    args.test_len = args.test_len or result.sizes["test_name"]
    phases.append(("audit", time.time() - started))

    started = time.time()
    code = cmd_preflight(args)
    if code:
        return code
    phases.append(("preflight", time.time() - started))

    started = time.time()
    code = cmd_export(args)
    if code:
        return code
    phases.append(("export", time.time() - started))

    started = time.time()
    code = cmd_load(args)
    if code:
        return code
    phases.append(("load", time.time() - started))

    started = time.time()
    code = cmd_verify(args)
    if code:
        return code
    phases.append(("verify", time.time() - started))

    log("")
    log("Timings — write these down; they are your downtime estimate")
    for name, seconds in phases:
        log("  {0:<10} {1:>8.0f}s".format(name, seconds))
    log("  {0:<10} {1:>8.0f}s".format("TOTAL", time.time() - overall))
    log("")
    log("Still yours to do: restart the feeder and run a catch-up "
        "(runbook §E.5). Keep the SQLite file and this export "
        "directory for at least a month (§E.6).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_to_mariadb.py",
        description=__doc__.split("\n")[0],
        epilog="Credentials come from a mysql option file (chmod 600), "
               "never from a command line, because a command line is "
               "visible to every user on the box via ps. See "
               "docs/MARIADB_MIGRATION.md.")
    subs = parser.add_subparsers(dest="command")

    def add_db(target: argparse.ArgumentParser) -> None:
        target.add_argument("--db", required=True,
                            help="the SQLite file, opened read-only")

    def add_config(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--config", required=True, metavar="CNF",
            help="mysql option file with the testboard_migrate "
                 "credentials")
        target.add_argument(
            "--max-blob-bytes", type=int, default=0,
            help="largest captured output, from the audit; used to "
                 "check max_allowed_packet")
        target.add_argument(
            "--force", action="store_true",
            help="proceed even if the target database already has "
                 "tables (scratch databases only)")

    def add_sizes(target: argparse.ArgumentParser) -> None:
        target.add_argument("--env-len", type=int, default=0)
        target.add_argument("--script-len", type=int, default=0)
        target.add_argument("--test-len", type=int, default=0)
        target.add_argument("--skip-space-check", action="store_true")

    p_audit = subs.add_parser(
        "audit", help="read-only audit of the SQLite source (§C.1)")
    add_db(p_audit)
    p_audit.add_argument("--json", default="",
                         help="also write the findings here")

    p_pre = subs.add_parser(
        "preflight", help="prove the server is fit to load into (§C.2-C.4)")
    add_config(p_pre)

    p_exp = subs.add_parser("export", help="write the load files (§D)")
    add_db(p_exp)
    p_exp.add_argument("--out", required=True, help="empty output directory")
    add_sizes(p_exp)

    p_load = subs.add_parser("load", help="create the schema and load (§D.3)")
    add_config(p_load)
    p_load.add_argument("--out", required=True, help="the export directory")

    p_ver = subs.add_parser("verify", help="compare the two databases (§E.4)")
    add_config(p_ver)
    p_ver.add_argument("--out", required=True, help="the export directory")

    p_all = subs.add_parser(
        "all", help="audit, export, load, verify — stopping at the first no")
    add_db(p_all)
    add_config(p_all)
    p_all.add_argument("--out", required=True, help="empty output directory")
    add_sizes(p_all)
    return parser


COMMANDS = {
    "audit": cmd_audit,
    "preflight": cmd_preflight,
    "export": cmd_export,
    "load": cmd_load,
    "verify": cmd_verify,
    "all": cmd_all,
}  # type: Dict[str, Callable[[argparse.Namespace], int]]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if not hasattr(args, "max_blob_bytes"):
        args.max_blob_bytes = 0
    return COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
