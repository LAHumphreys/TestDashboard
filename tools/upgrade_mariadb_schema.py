#!/usr/bin/env python3
"""Upgrade a LIVE MariaDB schema, in place, from v7 to v10.

**The gap this closes.** Production runs MariaDB at schema v7. Migrations
8, 9 and 10 (``environment_products``; ``streams`` plus ``stream_id``
columns on ``runs``/``latest_runs``/``assignments``/
``current_assignments``/``comments``; the ``latest_runs`` rebuild;
``activity_hours``/``script_hours`` PK widening) exist only as
SQLite/Python steps in ``testboard/storage.py``. The app never runs DDL
on MariaDB (``testboard/mariadb.py`` refuses a version mismatch in BOTH
directions), and ``tools/migrate_to_mariadb.py`` only ever does a full
load from SQLite — there was no way to bring an EXISTING MariaDB
database with live data forward. This is that way.

**What this is not.** It is not a general-purpose migration runner.
Storage's SQLite migrations are the source of truth for what changes;
this tool is a one-time, hand-verified translation of exactly the three
migrations production is missing (7->8, 8->9, 9->10), expressed as
MariaDB DDL. Extending it past migration 10 is future work, not a
generic mechanism — ``tests/test_upgrade_mariadb_schema.py`` pins that
``TARGET_VERSION`` matches ``storage.MIGRATIONS[-1][0]`` today and will
fail loudly, on purpose, the day that stops being true.

**DDL is AUTOCOMMIT on MariaDB 10.3.** Unlike the SQLite migrations
(one transaction, rolled back whole on failure), every ``CREATE TABLE``/
``ALTER TABLE`` statement below commits itself the instant it runs.
There is no wrapping transaction that undoes a partial upgrade. THE
PRE-UPGRADE ``mysqldump`` IS THE ROLLBACK — not this tool, not MariaDB's
transaction log. Read that paragraph again before running this live.

**The one number that decides whether this is fast or slow: `runs`'s row
count.** ``runs`` is production's big table (~4.4M rows); every other
table this tool touches is thousands of rows at most. The plan's
"bounded by tests, not by run history" claim rests entirely on
``ALTER TABLE runs ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1``
(step 8->9) qualifying for MariaDB's INSTANT ADD COLUMN — a real InnoDB
feature since 10.3.2, not a hope: it applies here because the column is
appended LAST, carries a constant DEFAULT, and the table is
``ROW_FORMAT=DYNAMIC``, all of which this tool's own generated DDL
guarantees. Verified empirically on THIS box's local server (12.3.2 —
see the module's own test-time measurements) at 500,000 synthetic rows:
the statement completed in well under a tenth of a second, and forcing
``ALGORITHM=INSTANT`` explicitly on an equivalent ADD COLUMN succeeded
rather than being refused — direct evidence the instant path applies,
not just a fast clock. **This has NOT been confirmed on production's
10.3 stream** — CI's ``python36-mariadb`` job (``mariadb:10.3``) is the
first real evidence at that version. If instant does not apply for some
reason specific to 10.3, MariaDB falls back to the next InnoDB algorithm
that fits (normally an online, LOCK=NONE rebuild — concurrent reads and
writes keep working — not the old blocking COPY algorithm, though only
INSTANT is fast). ``cmd_upgrade`` prints ``runs``'s row count before
running anything and times every statement live; a `runs` step that
takes materially longer than a few seconds against production-scale data
is the signal that this fell back, and the honest thing to do is let it
finish rather than interrupt a running DDL statement mid-flight.

**Privileges.** Connects with a ``testboard_migrate``-style credential
(``docs/MARIADB_MIGRATION.md`` §A.4/§A.9) — the same option-file
mechanism as everything else, via ``testboard.dbconfig``. That account's
grants are scoped to ``ON testboard.*`` — no CREATE DATABASE privilege —
which is why ``verify`` builds its comparison schema as TEMPORARY TABLES
inside the SAME database rather than a second one.

Usage::

    python3 tools/upgrade_mariadb_schema.py upgrade --config CNF --dry-run
    python3 tools/upgrade_mariadb_schema.py upgrade --config CNF
    python3 tools/upgrade_mariadb_schema.py verify  --config CNF

Python 3.6 compatible; standard library only (via
``tools.migrate_to_mariadb``, which owns the vendored-driver import;
this module never mentions the driver itself, so it needs no entry on
``tests/test_vendored_driver.py``'s allowlist).
"""

import argparse
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

if __name__ == "__main__" and __package__ is None:  # pragma: no cover
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import export_for_mariadb as exporter  # noqa: E402
from tools import migrate_to_mariadb as migrate  # noqa: E402
from tools.migrate_to_mariadb import Check, DatabaseError  # noqa: E402,F401
from testboard import dbconfig, model  # noqa: E402
from testboard.dbconfig import Settings  # noqa: E402

EXIT_GATE_FAILED = migrate.EXIT_GATE_FAILED

#: Versions this tool will resume from. DDL autocommits per statement and
#: schema_version is bumped LAST in each step, so a run interrupted
#: between steps leaves the database sitting at 7, 8 or 9 with that
#: step's own DDL already applied up to the point of failure — see
#: :func:`consistency_check`, which is what tells THAT state apart from
#: a genuinely clean 7/8/9 and refuses it by name.
EXPECTED_FROM_VERSIONS = (7, 8, 9)

#: What this tool upgrades TO. Mirrors ``storage.MIGRATIONS[-1][0]``
#: today; pinned equal to it by a test that fails on purpose the day
#: migration 11 (WP-15, parked) ships, so this tool cannot silently fall
#: one migration behind the code it is meant to bring MariaDB level with.
TARGET_VERSION = 10

#: Table/column existence probes, each true iff the recorded
#: schema_version is at least this value. Checked in BOTH directions
#: (present when it should be, absent when it should not be) so a
#: database sitting at "version says 7 but streams already exists" -
#: DDL applied, schema_version not yet bumped, the exact shape an
#: interrupted run leaves - is caught rather than silently re-run into a
#: raw duplicate-object error from the driver.
_MARKERS = (
    ("environment_products table", 8,
     lambda conn: _table_exists(conn, "environment_products")),
    ("streams table", 9, lambda conn: _table_exists(conn, "streams")),
    ("runs.stream_id column", 9,
     lambda conn: _column_exists(conn, "runs", "stream_id")),
    ("latest_runs.stream_id column", 9,
     lambda conn: _column_exists(conn, "latest_runs", "stream_id")),
    ("activity_hours.stream_id column", 10,
     lambda conn: _column_exists(conn, "activity_hours", "stream_id")),
    ("script_hours.stream_id column", 10,
     lambda conn: _column_exists(conn, "script_hours", "stream_id")),
)  # type: Tuple[Tuple[str, int, Callable[[Any], bool]], ...]

#: Tables whose row count is worth printing under --dry-run: the ones an
#: ALTER TABLE step below actually touches. environment_products/streams
#: are new and empty, so they are not listed.
_ROW_COUNT_TABLES = (
    "runs", "comments", "assignments", "current_assignments",
    "latest_runs", "activity_hours", "script_hours",
)

#: A live-run tripwire, not a hard limit. The runs ALTER is expected to
#: take well under a second (MariaDB's instant ADD COLUMN - see the
#: module docstring); a run that clears this threshold is the signal
#: that it fell back to a table rebuild instead, worth telling the
#: operator about in the moment rather than only after the fact.
_INSTANT_ADD_WARNING_SECONDS = 5.0


def _touches_runs_stream_id(statement: str) -> bool:
    """True for the one statement whose cost is not bounded by tests."""
    stripped = statement.strip()
    return (stripped.startswith("ALTER TABLE runs ")
            and "stream_id" in stripped)


# --------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------

def _table_exists(conn: Any, name: str) -> bool:
    rows = migrate.query(
        conn,
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{0}'".format(
            name))
    return bool(rows[0][0])


def _column_exists(conn: Any, table: str, column: str) -> bool:
    rows = migrate.query(
        conn,
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{0}' "
        "AND COLUMN_NAME = '{1}'".format(table, column))
    return bool(rows[0][0])


def current_version(conn: Any) -> int:
    """The recorded ``schema_version``, or raise if there is none."""
    try:
        rows = migrate.query(conn, "SELECT version FROM schema_version")
    except DatabaseError as exc:
        raise SystemExit(
            "could not read schema_version: {0}\nThis does not look "
            "like a testboard database at all - the schema is created "
            "by tools/migrate_to_mariadb.py, never by hand, per "
            "docs/MARIADB_MIGRATION.md section D.".format(exc))
    if not rows:
        raise SystemExit(
            "schema_version exists but is empty. This is not a state "
            "the migration tooling ever produces on its own - stop and "
            "restore from the pre-upgrade mysqldump.")
    return int(rows[0][0])


def discover_sizes(conn: Any) -> exporter.Sizes:
    """Read the VARCHAR sizes THIS database was actually loaded with.

    ``environment_products.environment`` (migration 8's new table) and
    the rebuilt ``latest_runs``/``activity_hours``/``script_hours`` must
    all size their identity columns to match ``runs.environment`` etc.
    EXACTLY, whatever the original load chose (docs/MARIADB_MIGRATION.md
    §B.1 - it is a measured-per-estate number, not the exporter's
    default of 64/255/255). Guessing wrong here does not fail loudly: it
    produces a schema that diverges from ``runs`` and verify's schema
    diff is what catches it, but reading the truth is cheaper than
    relying on that safety net.
    """
    def _length(column: str) -> int:
        rows = migrate.query(
            conn,
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'runs' "
            "AND COLUMN_NAME = '{0}'".format(column))
        if not rows or rows[0][0] is None:
            raise SystemExit(
                "could not measure runs.{0}'s VARCHAR length - is this "
                "really a testboard schema?".format(column))
        return int(rows[0][0])

    return exporter.Sizes(
        _length("environment"), _length("script"), _length("test_name"))


def row_counts(conn: Any) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    for table in _ROW_COUNT_TABLES:
        rows = migrate.query(
            conn, "SELECT COUNT(*) FROM {0}".format(table))
        counts[table] = int(rows[0][0])
    return counts


def consistency_check(conn: Any, recorded: int) -> List[str]:
    """Every marker must agree with what *recorded* implies. Empty = ok."""
    problems = []  # type: List[str]
    for name, min_version, probe in _MARKERS:
        expected = recorded >= min_version
        actual = probe(conn)
        if actual != expected:
            problems.append(
                "{0}: expected {1}, found {2}".format(
                    name, "present" if expected else "absent",
                    "present" if actual else "absent"))
    return problems


def mysqldump_hint(settings: Settings) -> str:
    """The exact rollback command, printed before anything runs.

    DDL autocommits (see the module docstring) - there is no "undo" this
    tool can offer. A dump taken NOW, before the first statement, is the
    only rollback that exists. Printed with real values filled in
    (except the password, which never appears) so nobody has to
    reconstruct the command from memory during an incident.
    """
    where = settings.unix_socket or "{0} --port={1}".format(
        settings.host, settings.port)
    socket_or_host = (
        "--socket={0}".format(settings.unix_socket) if settings.unix_socket
        else "--host={0} --port={1}".format(settings.host, settings.port))
    return (
        "ROLLBACK PLAN - take this dump BEFORE running anything live:\n"
        "  mysqldump --defaults-file=<your admin .cnf> {0} \\\n"
        "      --single-transaction --routines --triggers {1} \\\n"
        "      > testboard-preupgrade-$(date +%Y%m%dT%H%M%S).sql\n"
        "DDL is AUTOCOMMIT on MariaDB 10.3 - this dump is THE rollback, "
        "not a transaction this tool can roll back for you "
        "(connecting to {2}).".format(
            socket_or_host, settings.database, where))


# --------------------------------------------------------------------
# The DDL, mirroring storage.MIGRATIONS entries 8/9/10 exactly
# --------------------------------------------------------------------

def _quote_iso(dt_text: str) -> str:
    return "'{0}'".format(dt_text)


def step_7_to_8(sizes: exporter.Sizes) -> List[str]:
    """environment_products - migration 8. Mirrors storage.py's SQLite
    entry: a declared environment -> product table, same shape as
    environment_expectations, no backfill, no data touched at all.

    Column-for-column identical to ``exporter.ddl()``'s
    ``environment_products`` CREATE TABLE (the v10 oracle
    ``verify`` diffs against later) - kept as a literal string here
    rather than sliced out of that function so this file reads
    top-to-bottom as the migration it is; the two staying in sync is
    exactly what ``verify``'s schema diff exists to prove, every run.
    """
    env = "VARCHAR({0})".format(sizes.environment)
    return [
        "CREATE TABLE environment_products (\n"
        "  environment {0} NOT NULL,\n"
        "  product     VARCHAR(255) NOT NULL,\n"
        "  updated_at  VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin "
        "NOT NULL,\n"
        "  updated_by  VARCHAR(100) NOT NULL,\n"
        "  PRIMARY KEY (environment)\n"
        ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC".format(env),
        "UPDATE schema_version SET version = 8",
    ]


def step_8_to_9(now_iso: str) -> List[str]:
    """streams + stream_id columns + the latest_runs rebuild - migration 9.

    Mirrors storage.py's three-part entry 9: the ``streams`` table
    (seeded with mainline, row id 1, product='' kind='mainline' name=''
    - docs/STREAMS_PLAN.md §1), ``stream_id`` appended to
    runs/comments/assignments/current_assignments (NOT NULL DEFAULT 1 on
    runs, nullable elsewhere - the same split storage.py's migration
    documents: runs/latest_runs are identity-scoped, comments/
    assignments are annotations on the triple and stream_id there is
    provenance, not partition), and latest_runs widened to lead its
    PRIMARY KEY with stream_id.

    SQLite cannot widen a PRIMARY KEY with ALTER TABLE, so storage.py's
    version of this step is CREATE new / INSERT..SELECT / DROP / RENAME.
    MariaDB's ALTER TABLE can add a column, drop a key and add a new one
    in ONE statement - a single multi-clause ALTER here reaches the
    IDENTICAL resulting schema (proven by ``verify``'s diff against the
    v10 oracle, not merely assumed), so that is what this uses rather
    than reproducing SQLite's own workaround for a limitation MariaDB
    does not have.
    """
    return [
        "CREATE TABLE streams (\n"
        "  id         BIGINT NOT NULL AUTO_INCREMENT,\n"
        "  product    VARCHAR(255) NOT NULL,\n"
        "  kind       VARCHAR(20) NOT NULL,\n"
        "  name       VARCHAR(255) NOT NULL,\n"
        "  first_seen VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin "
        "NOT NULL,\n"
        "  last_seen  VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin "
        "NOT NULL,\n"
        "  PRIMARY KEY (id),\n"
        "  UNIQUE KEY uq_streams_identity (product, kind, name)\n"
        ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC",

        "INSERT INTO streams (id, product, kind, name, first_seen, "
        "last_seen) VALUES (1, '', 'mainline', '', {0}, {0})".format(
            _quote_iso(now_iso)),

        "ALTER TABLE runs ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1",
        "ALTER TABLE comments ADD COLUMN stream_id BIGINT NULL",
        "ALTER TABLE assignments ADD COLUMN stream_id BIGINT NULL",
        "ALTER TABLE current_assignments ADD COLUMN stream_id BIGINT NULL",

        "ALTER TABLE latest_runs "
        "ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1 FIRST, "
        "DROP PRIMARY KEY, "
        "ADD PRIMARY KEY (stream_id, environment, script, test_name)",

        # The four pre-9 indexes (storage.py's Migration9IndexesTest
        # pins these exact names on the SQLite side) rebuilt to lead
        # with stream_id, plus the one genuinely new index.
        "DROP INDEX idx_latest_runs_result ON latest_runs",
        "DROP INDEX idx_latest_runs_start_time ON latest_runs",
        "DROP INDEX idx_latest_runs_start_sort ON latest_runs",
        "DROP INDEX idx_latest_runs_duration_sort ON latest_runs",
        "CREATE INDEX idx_latest_runs_result ON latest_runs "
        "(stream_id, result, environment, script, test_name)",
        "CREATE INDEX idx_latest_runs_start_time "
        "ON latest_runs (stream_id, start_time)",
        "CREATE INDEX idx_latest_runs_start_sort ON latest_runs "
        "(stream_id, start_time, environment, script, test_name)",
        "CREATE INDEX idx_latest_runs_duration_sort ON latest_runs "
        "(stream_id, duration_seconds, environment, script, test_name)",
        "CREATE INDEX idx_latest_runs_triple "
        "ON latest_runs (environment, script, test_name)",

        "UPDATE schema_version SET version = 9",
    ]


def step_9_to_10() -> List[str]:
    """activity_hours/script_hours PK widening - migration 10.

    Every existing row gets stream_id = 1 - a LITERAL, not a
    re-aggregation from runs: both tables have been mainline-only since
    migrations 6/7, so every row on file already IS mainline's (same
    reasoning as storage.py's ``_rebuild_activity_hours_with_stream``).
    ADD COLUMN ... DEFAULT 1 does exactly that for every existing row as
    part of the ALTER, with no separate UPDATE needed.
    """
    return [
        "ALTER TABLE activity_hours "
        "ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1 FIRST, "
        "DROP PRIMARY KEY, "
        "ADD PRIMARY KEY (stream_id, environment, hour, result)",
        "ALTER TABLE script_hours "
        "ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1 FIRST, "
        "DROP PRIMARY KEY, "
        "ADD PRIMARY KEY (stream_id, environment, hour, script, result)",
        "UPDATE schema_version SET version = 10",
    ]


def plan(sizes: exporter.Sizes, now_iso: str) -> "List[Tuple[int, List[str]]]":
    """The three steps, in order, each keyed by the version it starts FROM."""
    return [
        (7, step_7_to_8(sizes)),
        (8, step_8_to_9(now_iso)),
        (9, step_9_to_10()),
    ]


# --------------------------------------------------------------------
# verify: schema diff against the exporter's own v10 DDL (the oracle)
# --------------------------------------------------------------------

#: Every table name that can appear in exporter.ddl()/INDEXES, longest
#: first so a substring of one name is never renamed inside another
#: (none of these actually collide, but the ordering costs nothing and
#: removes the need to prove it).
_ORACLE_PREFIX = "_tb_oracle_"


def _for_oracle(sql_text: str) -> str:
    """Rewrite CREATE TABLE/INDEX text onto ``_tb_oracle_``-prefixed
    TEMPORARY tables, so the comparison schema lives inside the SAME
    database as the real one under upgrade rather than needing a second
    database - which the testboard_migrate credential's grants
    (``ON testboard.*`` only, docs/MARIADB_MIGRATION.md §A.4) do not
    allow it to create.
    """
    text = sql_text
    for table in sorted(exporter.TABLE_ORDER, key=len, reverse=True):
        text = re.sub(r"\b{0}\b".format(re.escape(table)),
                      _ORACLE_PREFIX + table, text)
    text = re.sub(r"^CREATE TABLE ", "CREATE TEMPORARY TABLE ", text,
                  flags=re.MULTILINE)
    return text


def _normalize_show_create(text: str) -> str:
    """Strip what legitimately differs (the table name, the current
    AUTO_INCREMENT counter) so the comparison is about structure."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = re.sub(r"CREATE TEMPORARY TABLE `[^`]+`",
                  "CREATE TABLE `_`", text)
    text = re.sub(r"CREATE TABLE `[^`]+`", "CREATE TABLE `_`", text)
    text = re.sub(r"AUTO_INCREMENT=\d+\s*", "", text)
    return " ".join(text.split())


def schema_diff(conn: Any, sizes: exporter.Sizes,
                log: Callable[[str], None]) -> List[Check]:
    """Compare the live schema against a freshly-built v10 schema.

    The oracle is ``exporter.ddl()``/``exporter.INDEXES`` - the SAME
    generator ``tools/migrate_to_mariadb.py`` uses for a from-scratch
    load - built as TEMPORARY TABLES inside this same database and
    dropped again before returning, win or lose.
    """
    oracle_ddl = _for_oracle(exporter.ddl(sizes))
    oracle_idx = _for_oracle(exporter.INDEXES)
    created = []  # type: List[str]
    try:
        for statement in migrate.split_statements(oracle_ddl):
            migrate.execute(conn, statement)
        for statement in migrate.split_statements(oracle_idx):
            migrate.execute(conn, statement)
        created = list(exporter.TABLE_ORDER)

        checks = []  # type: List[Check]
        for table in exporter.TABLE_ORDER:
            real_rows = migrate.query(
                conn, "SHOW CREATE TABLE {0}".format(table))
            oracle_rows = migrate.query(
                conn, "SHOW CREATE TABLE {0}{1}".format(
                    _ORACLE_PREFIX, table))
            real = _normalize_show_create(real_rows[0][1])
            oracle = _normalize_show_create(oracle_rows[0][1])
            ok = real == oracle
            checks.append(Check(
                "schema:" + table, ok,
                "matches the v10 oracle" if ok
                else "DIFFERS from the v10 oracle - see the diff printed "
                     "above",
                blocking=True,
                advice="the loaded schema for {0} does not match what a "
                       "fresh v10 export/load would create. Do not serve "
                       "from this database - restore from the pre-upgrade "
                       "mysqldump and re-run.".format(table)))
            if not ok and log:
                log("  --- live: {0}".format(table))
                log("  " + real)
                log("  +++ oracle: {0}".format(table))
                log("  " + oracle)
        return checks
    finally:
        for table in reversed(created):
            try:
                migrate.execute(
                    conn, "DROP TEMPORARY TABLE IF EXISTS {0}{1}".format(
                        _ORACLE_PREFIX, table))
            except DatabaseError:  # pragma: no cover - best-effort cleanup
                pass


# --------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------

def cmd_upgrade(args: argparse.Namespace) -> int:
    log = print
    settings = migrate.read_option_file(args.config)
    log("connecting as {0}".format(settings.describe()))
    conn = migrate.connect(settings)
    try:
        log("")
        log(mysqldump_hint(settings))
        log("")

        recorded = current_version(conn)
        log("schema_version = {0}".format(recorded))

        if recorded == TARGET_VERSION:
            log("")
            log("STOP: already at schema version {0}. Nothing to "
                "upgrade.".format(TARGET_VERSION))
            return EXIT_GATE_FAILED
        if recorded not in EXPECTED_FROM_VERSIONS:
            log("")
            log("STOP: schema_version is {0}. This tool only resumes "
                "from {1} (it upgrades to {2}). A version below 7 needs "
                "the earlier migrations first (this database predates "
                "what this tool understands); a version above 9 that "
                "is not 10 is newer than this tool knows how to "
                "reason about at all.".format(
                    recorded, EXPECTED_FROM_VERSIONS, TARGET_VERSION))
            return EXIT_GATE_FAILED

        problems = consistency_check(conn, recorded)
        if problems:
            log("")
            log("STOP: schema_version says {0} but the actual schema "
                "disagrees:".format(recorded))
            for problem in problems:
                log("  - " + problem)
            log("")
            log("This is the shape an INTERRUPTED upgrade leaves - DDL "
                "already applied past what schema_version records "
                "(DDL autocommits; the version bump is the LAST "
                "statement of each step). Do not re-run this tool "
                "against it: restore from the pre-upgrade mysqldump "
                "(see the command printed above) and start again.")
            return EXIT_GATE_FAILED

        sizes = discover_sizes(conn)
        log("identity column sizes (from the live schema): environment "
            "{0}, script {1}, test_name {2}".format(
                sizes.environment, sizes.script, sizes.test_name))
        counts = row_counts(conn)
        log("row counts: " + ", ".join(
            "{0} {1:,}".format(name, counts[name])
            for name in _ROW_COUNT_TABLES if name in counts))
        log("")
        log("*** runs has {0:,} rows. Everything else this tool touches "
            "is a few thousand rows at most - runs is the ONE table "
            "where 'bounded by tests, not by run history' depends on "
            "MariaDB's instant ADD COLUMN actually applying (see this "
            "tool's own module docstring). Expected: well under a "
            "second, on any row count, verified locally with an "
            "explicit ALGORITHM=INSTANT force-success - but NOT yet "
            "confirmed on production's 10.3 stream (only this box's "
            "local server has been tried). Watch the per-statement "
            "timer below if you are running this live; a runs step "
            "taking materially longer than a few seconds means it did "
            "NOT take the instant path.".format(counts.get("runs", 0)))

        now_iso = model.format_iso(model.utcnow())
        steps = [(v, s) for v, s in plan(sizes, now_iso) if v >= recorded]

        if args.dry_run:
            log("")
            log("DRY RUN - nothing below will be executed.")
            for from_version, statements in steps:
                log("")
                log("-- step {0} -> {1} --".format(
                    from_version, from_version + 1))
                for statement in statements:
                    log(migrate.first_line(statement))
            log("")
            log("Row counts above are the ones each ALTER TABLE step "
                "touches - MariaDB rewrites the whole table for a "
                "column add or a PRIMARY KEY change, so they are a "
                "reasonable proxy for how long each step takes; they "
                "are NOT a timing estimate on their own. See the runs "
                "note above for the one number that actually matters.")
            return 0

        log("")
        log("Running for real. DDL is AUTOCOMMIT - each statement "
            "below is permanent the moment it succeeds.")
        overall = time.time()
        for from_version, statements in steps:
            log("")
            log("-- step {0} -> {1} --".format(
                from_version, from_version + 1))
            for statement in statements:
                started = time.time()
                migrate.execute(conn, statement)
                elapsed = time.time() - started
                log("  [{0:.1f}s] {1}".format(
                    elapsed, migrate.first_line(statement)))
                if (_touches_runs_stream_id(statement)
                        and elapsed > _INSTANT_ADD_WARNING_SECONDS):
                    log("")
                    log("  *** That took {0:.1f}s against {1:,} rows - "
                        "MUCH longer than the sub-second instant add "
                        "this tool expects (see the module docstring "
                        "and the note printed before this run started). "
                        "It almost certainly means MariaDB fell back to "
                        "a table rebuild rather than the instant path. "
                        "The statement already committed successfully "
                        "(DDL autocommits) - there is nothing to "
                        "interrupt or undo, this is informational, not "
                        "a failure.".format(elapsed, counts.get("runs", 0)))
        log("")
        log("All steps applied in {0:.1f}s (DEV timing on this box; "
            "not a production number - see the report).".format(
                time.time() - overall))

        log("")
        log("Verifying against a fresh v10 schema...")
        checks = schema_diff(conn, sizes, log)
        ok = migrate.report("Schema verification", checks, log)
        if not ok:
            log("")
            log("STOP: the upgraded schema does NOT match a fresh v10 "
                "load. Do not restart the server against this "
                "database. Restore from the pre-upgrade mysqldump.")
            return EXIT_GATE_FAILED
        log("")
        log("Schema verified. Still yours to do: restart the server, "
            "then the first-hour checks in docs/drops/2026-08-11.md.")
        return 0
    finally:
        conn.close()


def cmd_verify(args: argparse.Namespace) -> int:
    log = print
    settings = migrate.read_option_file(args.config)
    log("connecting as {0}".format(settings.describe()))
    conn = migrate.connect(settings)
    try:
        recorded = current_version(conn)
        if recorded != TARGET_VERSION:
            log("STOP: schema_version is {0}, not {1} - verify only "
                "makes sense once the upgrade is believed complete."
                .format(recorded, TARGET_VERSION))
            return EXIT_GATE_FAILED
        sizes = discover_sizes(conn)
        checks = schema_diff(conn, sizes, log)
        ok = migrate.report("Schema verification", checks, log)
        return 0 if ok else EXIT_GATE_FAILED
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upgrade_mariadb_schema.py",
        description=__doc__.split("\n")[0],
        epilog="Credentials come from a mysql option file (chmod 600), "
               "never from a command line - see "
               "docs/MARIADB_MIGRATION.md.")
    subs = parser.add_subparsers(dest="command")

    def add_config(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--config", required=True, metavar="CNF",
            help="mysql option file with the testboard_migrate "
                 "credentials (runbook §A.9)")

    p_up = subs.add_parser(
        "upgrade", help="apply migrations 8/9/10 to a live v7-v9 "
                        "database, stepwise")
    add_config(p_up)
    p_up.add_argument(
        "--dry-run", action="store_true",
        help="print every statement and row-count estimate; run nothing")

    p_ver = subs.add_parser(
        "verify", help="diff an already-upgraded (v10) schema against "
                       "a fresh v10 export's DDL")
    add_config(p_ver)

    return parser


COMMANDS = {
    "upgrade": cmd_upgrade,
    "verify": cmd_verify,
}  # type: Dict[str, Callable[[argparse.Namespace], int]]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
