#!/usr/bin/env python3
"""Upgrade a LIVE MariaDB schema, in place, one migration at a time.

**What this is.** The MariaDB half of every schema migration since the
cutover. ``testboard/storage.py``'s ``MIGRATIONS`` is the source of
truth for WHAT changes; the app never runs DDL on MariaDB
(``testboard/mariadb.py`` refuses a version mismatch in BOTH
directions), and ``tools/migrate_to_mariadb.py`` only ever does a full
load from SQLite. So a database that is already serving production and
needs to move to a newer ``schema_version`` moves by THIS tool, and
nothing else.

**The ledger.** ``LEDGER`` below holds one :class:`Step` per SQLite
migration above :data:`CUTOVER_VERSION` (7 — the version production
cut over to MariaDB at; no MariaDB database has ever existed below it).
Each step is a hand-written, hand-verified MariaDB translation of its
SQLite migration — NOT a mechanical one: the dialects differ in ways
that matter (a PRIMARY KEY is widened here in one ``ALTER TABLE`` where
SQLite rebuilds the table; sizes come from the estate's own audit;
collations are per column — ``docs/MARIADB_MIGRATION.md`` §B has the
list). Every step declares what it creates (``probes``) and which
tables its ``ALTER TABLE`` statements rewrite (``alters``); everything
the tool needs to know about versions is DERIVED from the ledger, never
restated: the versions it resumes from, the version it reaches, the
consistency check, and the row counts a dry run prints.

**What keeps the ledger honest.** :func:`ledger_gaps` is a pure check
that every SQLite migration above the cutover has a step, that the
steps are contiguous, that every step declares at least one probe, and
that every probe names something the step's own DDL mentions.
``tests/test_upgrade_mariadb_schema.py::LedgerTest`` runs it on every
suite run, with no server needed — so a migration written for SQLite
without its MariaDB step fails the build on the developer's machine, on
the same commit, rather than at deployment when the server refuses to
start. The tool runs the same check at start-up and refuses to proceed
if it is behind the code it was shipped with.

**Adding a step** (the whole procedure; ``docs/MARIADB_MIGRATION.md``
§G.5 says the same in the operator's words):

1. Claim the version in ``docs/UPGRADE_PLAN.md`` §1 and write the
   SQLite migration in ``storage.MIGRATIONS``, as ever.
2. Add a :class:`Step` to ``LEDGER`` for it: the DDL as a function of
   the live sizes, its probes, the tables it rewrites. Do NOT bump
   ``schema_version`` inside the step — :func:`plan` appends that as
   the LAST statement of every step, which is the invariant the
   consistency check relies on.
3. Add the new table(s) to ``tools/export_for_mariadb.py``'s ``ddl()``
   (the fresh-install schema and the oracle ``verify`` diffs against)
   and ``TABLE_ORDER``. The dual-backend suite runs on that DDL.
4. Run the suite. ``LedgerTest`` is green when steps 1–3 agree; the
   MariaDB-gated tests then upgrade a v7 fixture through EVERY step
   and diff the result against the oracle, and CI's
   ``python36-mariadb-upgraded`` leg runs the whole suite on a
   database built that way.

**DDL is AUTOCOMMIT on MariaDB 10.3.** Unlike the SQLite migrations
(one transaction, rolled back whole on failure), every ``CREATE TABLE``/
``ALTER TABLE`` statement here commits itself the instant it runs.
There is no wrapping transaction that undoes a partial upgrade. THE
PRE-UPGRADE ``mysqldump`` IS THE ROLLBACK — not this tool, not MariaDB's
transaction log. Read that paragraph again before running this live.
That is also why each step ends with the version bump and why the tool
resumes from ANY version the ledger starts a step at: a run interrupted
mid-step leaves the schema past what ``schema_version`` records, and
:func:`consistency_check` tells that state apart from a clean one by
probing every declared marker in both directions.

**The one number that decides whether a step is fast or slow: `runs`'s
row count.** ``runs`` is production's big table (~4.4M rows); every
other table any step touches is thousands of rows at most. Step 8→9's
``ALTER TABLE runs ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 1``
qualifies for MariaDB's INSTANT ADD COLUMN — a real InnoDB feature
since 10.3.2: the column is appended LAST, carries a constant DEFAULT,
and the table is ``ROW_FORMAT=DYNAMIC``. Verified empirically on THIS
box's local server (12.3.2) at 500,000 synthetic rows: well under a
tenth of a second, and forcing ``ALGORITHM=INSTANT`` explicitly
succeeded rather than being refused. **This has NOT been confirmed on
production's 10.3 stream** — CI's ``python36-mariadb`` job
(``mariadb:10.3``) is the first real evidence at that version. If
instant does not apply, MariaDB falls back to the next InnoDB algorithm
that fits (normally an online, LOCK=NONE rebuild — concurrent reads and
writes keep working — not the old blocking COPY algorithm, though only
INSTANT is fast). ``cmd_upgrade`` prints ``runs``'s row count before
running anything and times every statement live; ANY statement that
alters ``runs`` and takes materially longer than a few seconds is the
signal that this fell back, and the honest thing to do is let it
finish rather than interrupt a running DDL statement mid-flight. A
future step that must touch ``runs`` should keep to the same shape.

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
from typing import (
    Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple,
)

if __name__ == "__main__" and __package__ is None:  # pragma: no cover
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import export_for_mariadb as exporter  # noqa: E402
from tools import migrate_to_mariadb as migrate  # noqa: E402
from tools.migrate_to_mariadb import Check, DatabaseError  # noqa: E402,F401
from testboard import dbconfig, model  # noqa: E402
from testboard.dbconfig import Settings  # noqa: E402
from testboard.storage import MIGRATIONS  # noqa: E402

EXIT_GATE_FAILED = migrate.EXIT_GATE_FAILED

#: The schema version production cut over to MariaDB at (2026-08-11).
#: No MariaDB database has ever existed below it, so the ledger starts
#: here: migrations 1..7 are the fresh-load schema's history, not steps.
CUTOVER_VERSION = 7


# --------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------

class Probe(NamedTuple):
    """One thing a step creates, checkable in both directions on a live
    server: a table, or a column of one (``column`` None = the table)."""
    label: str
    table: str
    column: Optional[str]


class Step(NamedTuple):
    """One SQLite migration's MariaDB translation.

    ``statements(sizes, now_iso)`` returns the DDL/DML, WITHOUT the
    ``schema_version`` bump — :func:`plan` appends that. ``probes`` are
    what :func:`consistency_check` tests for; ``alters`` are the
    existing tables the step's ``ALTER TABLE`` rewrites, whose row
    counts a dry run prints as a proxy for its cost.
    """
    from_version: int
    package: str
    summary: str
    statements: Callable[[exporter.Sizes, str], List[str]]
    probes: Tuple[Probe, ...]
    alters: Tuple[str, ...]


def _quote_iso(dt_text: str) -> str:
    return "'{0}'".format(dt_text)


def step_7_to_8(sizes: exporter.Sizes) -> List[str]:
    """environment_products - migration 8. Mirrors storage.py's SQLite
    entry: a declared environment -> product table, same shape as
    environment_expectations, no backfill, no data touched at all.

    Column-for-column identical to ``exporter.ddl()``'s
    ``environment_products`` CREATE TABLE (the oracle ``verify`` diffs
    against later) - kept as a literal string here rather than sliced
    out of that function so this file reads top-to-bottom as the
    migration it is; the two staying in sync is exactly what
    ``verify``'s schema diff exists to prove, every run.
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
    oracle, not merely assumed), so that is what this uses rather than
    reproducing SQLite's own workaround for a limitation MariaDB does
    not have.
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
    ]


def step_10_to_11(sizes: exporter.Sizes) -> List[str]:
    """test_mutes + mute_history - migration 11.

    Mirrors storage.py's entry 11: two new tables, no backfill, nothing
    existing touched. Column-for-column identical to ``exporter.ddl()``
    (the oracle ``verify`` diffs against) - kept literal here for the
    same reason as step_7_to_8. ``reason`` is TEXT (not indexed, not
    bounded by the audit); ``action`` shares ``result``'s ascii_bin.
    """
    env = "VARCHAR({0})".format(sizes.environment)
    script = "VARCHAR({0})".format(sizes.script)
    name = "VARCHAR({0})".format(sizes.test_name)
    stamp = "VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin"
    return [
        "CREATE TABLE test_mutes (\n"
        "  stream_id       BIGINT NOT NULL,\n"
        "  environment     {env} NOT NULL,\n"
        "  script          {script} NOT NULL,\n"
        "  test_name       {name} NOT NULL,\n"
        "  reason          TEXT NOT NULL,\n"
        "  muted_at {stamp} NOT NULL,\n"
        "  expires_at      {stamp} NOT NULL,\n"
        "  muted_by VARCHAR(100) NOT NULL,\n"
        "  extensions      INT NOT NULL DEFAULT 0,\n"
        "  PRIMARY KEY (stream_id, environment, script, test_name)\n"
        ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC".format(
            env=env, script=script, name=name, stamp=stamp),

        "CREATE TABLE mute_history (\n"
        "  id          BIGINT NOT NULL AUTO_INCREMENT,\n"
        "  stream_id   BIGINT NOT NULL,\n"
        "  environment {env} NOT NULL,\n"
        "  script      {script} NOT NULL,\n"
        "  test_name   {name} NOT NULL,\n"
        "  action      VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin "
        "NOT NULL,\n"
        "  reason      TEXT NULL,\n"
        "  expires_at  {stamp} NULL,\n"
        "  actor       VARCHAR(100) NOT NULL,\n"
        "  acted_at    {stamp} NOT NULL,\n"
        "  PRIMARY KEY (id)\n"
        ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC".format(
            env=env, script=script, name=name, stamp=stamp),

        "CREATE INDEX idx_test_mutes_expiry "
        "ON test_mutes (expires_at)",
        "CREATE INDEX idx_mute_history_triple "
        "ON mute_history (environment, script, test_name, id)",
    ]


#: Every MariaDB migration since the cutover, in order. One entry per
#: ``storage.MIGRATIONS`` entry above CUTOVER_VERSION — a gap fails
#: ``LedgerTest`` and stops the tool at start-up (see ledger_gaps).
LEDGER = (
    Step(
        from_version=7, package="WP-20",
        summary="environment_products table",
        statements=lambda sizes, now_iso: step_7_to_8(sizes),
        probes=(Probe("environment_products table",
                      "environment_products", None),),
        alters=(),
    ),
    Step(
        from_version=8, package="WP-21",
        summary="streams table; stream_id on runs/comments/assignments/"
                "current_assignments; latest_runs keyed by stream",
        statements=lambda sizes, now_iso: step_8_to_9(now_iso),
        probes=(Probe("streams table", "streams", None),
                Probe("runs.stream_id column", "runs", "stream_id"),
                Probe("latest_runs.stream_id column", "latest_runs",
                      "stream_id")),
        alters=("runs", "comments", "assignments", "current_assignments",
                "latest_runs"),
    ),
    Step(
        from_version=9, package="WP-23",
        summary="activity_hours/script_hours keyed by stream",
        statements=lambda sizes, now_iso: step_9_to_10(),
        probes=(Probe("activity_hours.stream_id column", "activity_hours",
                      "stream_id"),
                Probe("script_hours.stream_id column", "script_hours",
                      "stream_id")),
        alters=("activity_hours", "script_hours"),
    ),
    Step(
        from_version=10, package="WP-40",
        summary="test_mutes and mute_history tables",
        statements=lambda sizes, now_iso: step_10_to_11(sizes),
        probes=(Probe("test_mutes table",
                      "test_mutes", None),
                Probe("mute_history table",
                      "mute_history", None)),
        alters=(),
    ),
)  # type: Tuple[Step, ...]

#: Versions this tool will resume from: wherever a step starts.
EXPECTED_FROM_VERSIONS = tuple(
    step.from_version for step in LEDGER)  # type: Tuple[int, ...]

#: What this tool upgrades TO: where the last step ends. Equal to
#: ``storage.MIGRATIONS[-1][0]`` whenever ledger_gaps() is empty.
TARGET_VERSION = LEDGER[-1].from_version + 1

#: Tables whose row count a dry run prints: the ones some step's ALTER
#: TABLE rewrites, in ledger order, ``runs`` always first because it is
#: the one table whose size is not bounded by the number of tests.
_ROW_COUNT_TABLES = tuple(
    ["runs"] + [table for step in LEDGER for table in step.alters
                if table != "runs"])  # type: Tuple[str, ...]

#: A live-run tripwire, not a hard limit. Any ALTER on ``runs`` is
#: expected to take well under a second (MariaDB's instant ADD COLUMN -
#: see the module docstring); a run that clears this threshold is the
#: signal that it fell back to a table rebuild instead, worth telling
#: the operator about in the moment rather than only after the fact.
_INSTANT_ADD_WARNING_SECONDS = 5.0


def _touches_runs(statement: str) -> bool:
    """True for a statement whose cost is not bounded by tests."""
    return statement.strip().startswith("ALTER TABLE runs ")


def _bump(version: int) -> str:
    return "UPDATE schema_version SET version = {0}".format(version)


def plan(sizes: exporter.Sizes,
         now_iso: str) -> "List[Tuple[int, List[str]]]":
    """Every step, in order, keyed by the version it starts FROM, each
    ending with its ``schema_version`` bump. This is the exact statement
    list a live run executes; ``tests/backends.py``'s VIA_UPGRADE path
    runs it too, so the dual-backend suite can serve from its result."""
    return [
        (step.from_version,
         list(step.statements(sizes, now_iso))
         + [_bump(step.from_version + 1)])
        for step in LEDGER
    ]


def rewritten_tables(steps: Sequence[Step]) -> List[Tuple[int, str]]:
    """``(target version, table)`` for every existing table one of
    *steps* rewrites — the list that decides whether the SERVER must
    stop before the upgrade (runbook §G.3). Empty means every pending
    step only creates things: the old code keeps serving through it,
    and the only gap is the restart into the new code. Non-empty means
    an ALTER holds that table's lock for as long as it takes while the
    app's connections wait ten seconds at most.

    The feeders never need stopping either way: an unreachable server
    is "deferred, not lost" under every feeder's contract, and there
    are too many of them to coordinate.
    """
    return [(step.from_version + 1, table)
            for step in steps for table in step.alters]


def ledger_gaps(migration_versions: Sequence[int],
                ledger: Optional[Sequence[Step]] = None) -> List[str]:
    """Everything wrong with *ledger* (default: ``LEDGER``) against the
    SQLite migrations. Empty means the two halves agree.

    Pure and server-free, so ``LedgerTest`` runs it on every suite run
    and ``cmd_upgrade`` runs it before touching a connection. *ledger*
    is a parameter only so the test can plant a broken one.
    """
    if ledger is None:
        ledger = LEDGER
    problems = []  # type: List[str]
    sqlite_versions = sorted(v for v in migration_versions
                             if v > CUTOVER_VERSION)
    step_targets = [step.from_version + 1 for step in ledger]
    for version in sqlite_versions:
        if version not in step_targets:
            problems.append(
                "storage.MIGRATIONS has migration {0} but the ledger has "
                "no step from {1} to {0}: add a Step to "
                "tools/upgrade_mariadb_schema.LEDGER".format(
                    version, version - 1))
    for version in step_targets:
        if version not in sqlite_versions:
            problems.append(
                "the ledger has a step to {0} but storage.MIGRATIONS has "
                "no migration {0}".format(version))
    expected = CUTOVER_VERSION
    for step in ledger:
        if step.from_version != expected:
            problems.append(
                "the ledger is not contiguous: expected a step from {0}, "
                "found one from {1}".format(expected, step.from_version))
        expected = step.from_version + 1
    sizes = exporter.Sizes(64, 255, 255)
    for step in ledger:
        target = step.from_version + 1
        if not step.probes:
            problems.append(
                "step to {0} declares no probe, so an interrupted run "
                "could not be told apart from a clean one".format(target))
        try:
            statements = list(step.statements(
                sizes, "2026-01-01T00:00:00.000000"))
        except Exception as exc:  # a step that cannot even be rendered
            problems.append(
                "step to {0} cannot render its statements: {1!r}".format(
                    target, exc))
            continue
        text = "\n".join(statements)
        for probe in step.probes:
            if not re.search(r"\b{0}\b".format(re.escape(probe.table)),
                             text):
                problems.append(
                    "step to {0} probes '{1}' but its statements never "
                    "mention table {2}".format(target, probe.label,
                                               probe.table))
            elif probe.column and not re.search(
                    r"\b{0}\b".format(re.escape(probe.column)), text):
                problems.append(
                    "step to {0} probes '{1}' but its statements never "
                    "mention column {2}".format(target, probe.label,
                                                probe.column))
        for statement in statements:
            if statement.strip().lower().startswith(
                    "update schema_version"):
                problems.append(
                    "step to {0} bumps schema_version itself; plan() "
                    "appends the bump, a step must not".format(target))
    return problems


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


def _probe_present(conn: Any, probe: Probe) -> bool:
    if probe.column is None:
        return _table_exists(conn, probe.table)
    return _column_exists(conn, probe.table, probe.column)


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

    Every table a step creates or rebuilds must size its identity
    columns to match ``runs.environment`` etc. EXACTLY, whatever the
    original load chose (docs/MARIADB_MIGRATION.md §B.1 - it is a
    measured-per-estate number, not the exporter's default of
    64/255/255). Guessing wrong here does not fail loudly: it produces
    a schema that diverges from ``runs`` and verify's schema diff is
    what catches it, but reading the truth is cheaper than relying on
    that safety net.
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
    """Every step's probes must agree with what *recorded* implies:
    present iff the recorded version has reached that step's target.
    Empty = ok."""
    problems = []  # type: List[str]
    for step in LEDGER:
        expected = recorded >= step.from_version + 1
        for probe in step.probes:
            actual = _probe_present(conn, probe)
            if actual != expected:
                problems.append(
                    "{0}: expected {1}, found {2}".format(
                        probe.label, "present" if expected else "absent",
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
# verify: schema diff against the exporter's own DDL (the oracle)
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
    """Compare the live schema against a freshly-built one at the
    target version.

    The oracle is ``exporter.ddl()``/``exporter.INDEXES`` - the SAME
    generator ``tools/migrate_to_mariadb.py`` uses for a from-scratch
    load and ``tests/backends.py`` uses for the dual-backend suite -
    built as TEMPORARY TABLES inside this same database and dropped
    again before returning, win or lose.
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
                "matches the v{0} oracle".format(TARGET_VERSION) if ok
                else "DIFFERS from the v{0} oracle - see the diff printed "
                     "above".format(TARGET_VERSION),
                blocking=True,
                advice="the loaded schema for {0} does not match what a "
                       "fresh v{1} export/load would create. Do not serve "
                       "from this database - restore from the pre-upgrade "
                       "mysqldump and re-run.".format(table, TARGET_VERSION)))
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

def _self_check(log: Callable[[str], None]) -> bool:
    """Refuse to run a ledger that is behind (or ahead of) the SQLite
    migrations in this checkout. LedgerTest makes this unreachable on
    a green suite; it is here for a checkout nobody ran the suite on."""
    problems = ledger_gaps([version for version, _ in MIGRATIONS])
    if not problems:
        return True
    log("STOP: this tool's ledger does not match storage.MIGRATIONS in "
        "this checkout:")
    for problem in problems:
        log("  - " + problem)
    log("Nothing was run. Fix the ledger (tests/test_upgrade_mariadb_"
        "schema.py::LedgerTest says the same) before upgrading anything.")
    return False


def cmd_upgrade(args: argparse.Namespace) -> int:
    log = print
    if not _self_check(log):
        return EXIT_GATE_FAILED
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
                "from {1} (it upgrades to {2}). A version below {3} "
                "predates MariaDB here entirely (no such database "
                "should exist); a version above {2} was written by NEWER "
                "code than this checkout - deploy that code instead of "
                "running this tool.".format(
                    recorded, EXPECTED_FROM_VERSIONS, TARGET_VERSION,
                    CUTOVER_VERSION))
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
        log("*** runs has {0:,} rows. Everything else any step touches "
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
        pending = [step for step in LEDGER if step.from_version >= recorded]
        steps = [(v, s) for v, s in plan(sizes, now_iso) if v >= recorded]
        log("")
        log("steps to run: {0} -> {1}".format(recorded, TARGET_VERSION))
        for step in pending:
            log("  {0} -> {1}  {2}: {3}".format(
                step.from_version, step.from_version + 1, step.package,
                step.summary))
        log("")
        rewritten = rewritten_tables(pending)
        if rewritten:
            log("SERVER: STOP IT FIRST. These steps rewrite existing "
                "tables, and an ALTER holds the table's lock for as long "
                "as it takes while the app waits ten seconds at most: "
                + ", ".join("{0} (step to {1})".format(table, version)
                            for version, table in rewritten)
                + ". Feeders need no action - a push that meets a stopped "
                "server is deferred by every feeder's contract, not lost "
                "(runbook section G.3).")
        else:
            log("SERVER: may keep running. No pending step rewrites an "
                "existing table - they only create - so the old code "
                "serves through the upgrade and refuses only at its next "
                "start. Stop it for the restart into the new code, as "
                "for any drop. Feeders need no action.")

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
                if (_touches_runs(statement)
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
        log("Verifying against a fresh v{0} schema...".format(
            TARGET_VERSION))
        checks = schema_diff(conn, sizes, log)
        ok = migrate.report("Schema verification", checks, log)
        if not ok:
            log("")
            log("STOP: the upgraded schema does NOT match a fresh v{0} "
                "load. Do not restart the server against this "
                "database. Restore from the pre-upgrade mysqldump."
                .format(TARGET_VERSION))
            return EXIT_GATE_FAILED
        log("")
        log("Schema verified. Still yours to do: restart the server, "
            "then the first-hour checks in this drop's operator note "
            "(docs/drops/).")
        return 0
    finally:
        conn.close()


def cmd_verify(args: argparse.Namespace) -> int:
    log = print
    if not _self_check(log):
        return EXIT_GATE_FAILED
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
               "docs/MARIADB_MIGRATION.md section G.")
    subs = parser.add_subparsers(dest="command")

    def add_config(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--config", required=True, metavar="CNF",
            help="mysql option file with the testboard_migrate "
                 "credentials (runbook §A.9)")

    p_up = subs.add_parser(
        "upgrade",
        help="bring a live database (at v{0} or later) to v{1}, "
             "stepwise".format(CUTOVER_VERSION, TARGET_VERSION))
    add_config(p_up)
    p_up.add_argument(
        "--dry-run", action="store_true",
        help="print every statement and row-count estimate; run nothing")

    p_ver = subs.add_parser(
        "verify",
        help="diff an already-upgraded (v{0}) schema against a fresh "
             "export's DDL".format(TARGET_VERSION))
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
