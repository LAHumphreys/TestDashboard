"""SQLite storage layer for testboard.

All SQL for the project lives in this module. Responsibilities:

- Thread-local ``sqlite3`` connections (the HTTP server is threaded): a
  :class:`Storage` instance keeps a ``threading.local()`` and each thread
  lazily opens its own connection with ``PRAGMA journal_mode=WAL``,
  ``PRAGMA busy_timeout=10000`` and ``PRAGMA foreign_keys=ON``. Because
  connections are per-thread, a ``:memory:`` database only works
  single-threaded (fine for unit tests; end-to-end tests use a temp file).
- Versioned migrations via the ``schema_version`` table (see
  :data:`MIGRATIONS`).
- Idempotent run import: a run is uniquely keyed by
  ``(environment, script, test_name, start_time)``; re-import updates in
  place using explicit upsert logic (SELECT then UPDATE-or-INSERT), never
  ``INSERT OR REPLACE`` (which churns rowids) and never ``ON CONFLICT DO
  UPDATE`` (not available in Python 3.6's bundled sqlite).
- The (potentially large) ``output`` column is NEVER selected by list
  queries — only :meth:`Storage.get_run` fetches it.
- Four derived tables keep estate-wide reads proportional to the number
  of TESTS (and the page actually returned) rather than to the number of
  runs ever recorded: ``latest_runs`` (one row per test: its newest run,
  that run's result and the previous run's result),
  ``current_assignments`` (one row per test: who owns it now, with
  ``assignments`` kept as the audit log), ``activity_hours`` (run
  counts per environment, UTC hour and result — what the staleness
  cutoff and the trend chart read instead of scanning a window of
  ``runs``) and ``script_hours`` (the same counts per script, carrying
  the exact first start and last end in each bucket — what the Timeline
  page reads to show a night's script running order). All are
  maintained inside the same transaction as the write that changes them
  — see :meth:`Storage._maintain_latest`, :meth:`Storage.set_assignee`,
  :meth:`Storage._apply_activity_deltas` and
  :meth:`Storage._apply_script_hour_changes` — so they cannot drift
  from ``runs``.

Only ``str``/``int``/``float``/``None`` cross the sqlite boundary
(``detect_types=0``); datetimes are converted with
:func:`testboard.model.format_iso` / :func:`testboard.model.parse_iso`
inside this module, so lexical string comparison in SQL equals time
comparison.

Python 3.6 compatible; standard library only; parameterized SQL only.
"""

import datetime
import hashlib
import os
import sqlite3
import threading
import time
import zlib
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from testboard import dbconfig
from testboard import model
from testboard.model import Result, RunRecord, StoredRun

__all__ = [
    "MIGRATIONS",
    "describe_open_error",
    "DASHBOARD_SORTS",
    "QUEUE_KINDS",
    "TestSummaryRow",
    "TestStatusRow",
    "DailyResultCount",
    "RollupCount",
    "ScriptFailures",
    "FailureStreak",
    "Comment",
    "User",
    "EnvironmentExpectation",
    "EnvironmentProduct",
    "UpsertCounts",
    "ScriptHourBucket",
    "Storage",
]


#: How long a memoized nightly trend may be reused. Bounds how stale the
#: chart can be after a write made by another PROCESS (an offline prune
#: while the server runs); writes made by this process clear it at once.
_TREND_CACHE_TTL_SECONDS = 60.0

#: Cap on memoized trend windows, so arbitrary ``days`` values cannot
#: grow the cache without limit.
_TREND_CACHE_MAX_ENTRIES = 32

#: Connections are thread-local and the page cache is per connection, so
#: a cache budget has to be divided by the number of threads that might
#: hold one — not handed to each of them.
#:
#: This is not an estimate. The server serves requests from a fixed pool
#: of exactly this many worker threads, each holding one connection for
#: its lifetime, so the divisor is the true count. Eight is generous for
#: a dashboard whose reads are milliseconds once warm, and small enough
#: that eight page caches fit any sensible budget.
DEFAULT_MAX_CONNECTIONS = 8

#: Never shrink a connection below SQLite's own default; a budget so
#: small that it makes things worse is a configuration mistake, not an
#: instruction to obey.
_MIN_CACHE_KIB = 2000

#: zlib level for stored test output. Measured on a year of realistic
#: harness logs: level 3 and level 6 both deflate ~16.5x, level 9 adds
#: nothing for twice the CPU. 6 is zlib's default and the safer choice
#: across output that looks nothing like the sample.
_OUTPUT_COMPRESS_LEVEL = 6


def _compress_output(output: str) -> bytes:
    """Encode run output for storage (UTF-8, deflated)."""
    return zlib.compress(
        output.encode("utf-8"), _OUTPUT_COMPRESS_LEVEL
    )


def _output_fingerprint(output: str) -> str:
    """SHA-1 hex of the output text, as stored in ``runs.output_fingerprint``.

    Hashing the TEXT rather than the compressed bytes keeps the value
    meaningful if the compression level ever changes, and lets the
    unchanged-record check in :meth:`Storage.upsert_runs` skip the
    compression entirely — the hash is cheaper to compute than the
    deflate it replaces.
    """
    return hashlib.sha1(output.encode("utf-8")).hexdigest()


def _decompress_output(stored: Any) -> str:
    """Decode stored run output back to text.

    Tolerates plain ``str`` so a database written before the output
    column became a compressed BLOB still reads.
    """
    if stored is None:
        return ""
    if isinstance(stored, str):
        return stored
    return zlib.decompress(stored).decode("utf-8")


#: Versioned schema migrations: (version, [DDL statements]). On startup the
#: ``schema_version`` table is created if absent (current version 0), every
#: migration with a version greater than the current one is applied inside a
#: single transaction, and the version is updated.
#:
#: There is one entry, and it creates the schema outright. Migrations
#: exist for changes made to databases that are already in service — so
#: the first change after this ships adds entry 2, and never edits
#: entry 1.
MIGRATIONS = [
    (
        1,
        [
            # One run of one test. `output` deliberately lives in its own
            # table (see run_outputs): it is unbounded, it is read by
            # exactly one endpoint, and keeping it out of here is what
            # keeps this table dense enough for index-then-row-lookup
            # queries to stay cheap at millions of rows.
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                result TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                source_link TEXT NOT NULL,
                known_failure_reason TEXT,
                UNIQUE (environment, script, test_name, start_time)
            )
            """,
            # Serves the nightly trend: covering, so counting a window of
            # runs by result never touches the table itself.
            """
            CREATE INDEX idx_runs_start_time_result
                ON runs (start_time, result)
            """,
            # The captured output of a run, zlib-deflated (~16x on log
            # text, which at nightly scale is most of the database).
            """
            CREATE TABLE run_outputs (
                run_id INTEGER PRIMARY KEY REFERENCES runs(id),
                output BLOB NOT NULL
            )
            """,
            # One row per TEST: its newest run, that run's result, and
            # the result of the run before it. Maintained inside every
            # import transaction (see Storage._maintain_latest). Every
            # estate-wide read goes through this table, which is why the
            # home screen costs the same at 100 tests and at 100,000.
            """
            CREATE TABLE latest_runs (
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                run_id INTEGER NOT NULL REFERENCES runs(id),
                start_time TEXT NOT NULL,
                result TEXT NOT NULL,
                prev_result TEXT,
                PRIMARY KEY (environment, script, test_name)
            )
            """,
            # "Only failures, in the default order" without a sort step;
            # the PRIMARY KEY already serves the default ordering.
            """
            CREATE INDEX idx_latest_runs_result
                ON latest_runs (result, environment, script, test_name)
            """,
            # The "not run recently" filter and the start_time sort.
            """
            CREATE INDEX idx_latest_runs_start_time
                ON latest_runs (start_time)
            """,
            """
            CREATE TABLE users (
                username TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                author TEXT NOT NULL REFERENCES users(username),
                created_at TEXT NOT NULL,
                text TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX idx_comments_triple
                ON comments (environment, script, test_name, id)
            """,
            # Append-only assignment history: who assigned what to whom,
            # and when. Never updated in place.
            """
            CREATE TABLE assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                assignee TEXT REFERENCES users(username),
                assigned_by TEXT NOT NULL REFERENCES users(username),
                assigned_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX idx_assignments_triple
                ON assignments (environment, script, test_name, id)
            """,
            # Who owns a test NOW, so no list query has to re-derive it
            # from the history above. Written in the same transaction as
            # the history row (see Storage.set_assignee).
            """
            CREATE TABLE current_assignments (
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                assignee TEXT REFERENCES users(username),
                PRIMARY KEY (environment, script, test_name)
            )
            """,
            """
            CREATE INDEX idx_current_assignments_assignee
                ON current_assignments (assignee)
            """,
            # Tests a human has approved as "no longer in the suite".
            # Presence of the row IS the retirement; clearing it puts the
            # test back. Deliberately NOT a column on latest_runs: that
            # table is derived from `runs` and could be rebuilt, which
            # would silently discard people's approvals.
            #
            # Retirement hides a test from the ESTATE views (counts,
            # queues, the default test list). It never touches history —
            # the test's runs, comments and detail page are unchanged,
            # because "not in the suite any more" is not "never
            # happened".
            """
            CREATE TABLE test_retirements (
                environment TEXT NOT NULL,
                script TEXT NOT NULL,
                test_name TEXT NOT NULL,
                retired_at TEXT NOT NULL,
                retired_by TEXT NOT NULL REFERENCES users(username),
                PRIMARY KEY (environment, script, test_name)
            )
            """,
        ],
    ),
    (
        2,
        [
            # Deactivating a user hides them from the assignee pickers
            # without touching anything they did. The estate already has
            # one person holding two usernames, and nothing could retire
            # the spare.
            #
            # Presence of `deactivated_at` IS the state, the same shape
            # as test_retirements: reversible by clearing it, and no
            # boolean that can disagree with its own timestamp.
            #
            # History is deliberately untouched. Comments and assignment
            # records keep the name they were made under, because "this
            # account is no longer used" is not "this person never said
            # anything" — the same reasoning that keeps a retired test's
            # runs intact.
            #
            # Two ADD COLUMNs, no backfill: NULL already means "active",
            # which is what every existing row should mean. SQLite adds a
            # column without rewriting the table, so this is O(1) against
            # the production database rather than O(users).
            "ALTER TABLE users ADD COLUMN deactivated_at TEXT",
            "ALTER TABLE users ADD COLUMN deactivated_by TEXT",
        ],
    ),
    (
        3,
        [
            # How long a test's newest run took, denormalised onto the
            # row every estate-wide read already goes through.
            #
            # One column, three unrelated wins:
            #
            #  1. "Where is the time going" becomes a GROUP BY over
            #     ~12k rows instead of an aggregate over millions.
            #  2. The duration sort stops evaluating an expression over
            #     the whole filtered set on every sorted page.
            #  3. It removes the only call to a SQLite date function in
            #     the codebase, which was a blocker for the MariaDB
            #     port (see docs/MARIADB_MIGRATION.md).
            #
            # DEFAULT 0 rather than NULL: SQLite needs a non-null
            # default to add a NOT NULL column without rewriting the
            # table, and every row is filled in by the backfill below
            # before this migration commits.
            "ALTER TABLE latest_runs "
            "ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 0",
            # The backfill is a CALLABLE, not SQL, on purpose: the
            # duration has to be computed by model.duration_seconds, the
            # same function the API uses, or the stored value and the
            # displayed one can disagree. Doing it in SQL would mean
            # julianday() — reintroducing exactly what this migration
            # exists to remove.
            #
            # It touches latest_runs (one row per TEST, ~12k) and never
            # `runs` (~4.4M). That is what makes it affordable in a
            # startup migration; keep it that way.
            "python: backfill_latest_durations",
        ],
    ),
    (
        4,
        [
            # Sort indexes for the paged test list.
            #
            # Without a matching index SQLite orders all 12,008 rows to
            # return 250 of them, on every page: the plan says
            # "SCAN lr ... USE TEMP B-TREE FOR ORDER BY". With one it is
            # "SCAN lr USING INDEX" and it stops after 250.
            #
            # MEASURED against the query dashboard() actually runs, on a
            # copy of production (12,008 tests, 540,192 runs), 250 rows:
            #
            #                        before    after
            #   start_time DESC      177.3ms    2.6ms
            #   start_time ASC       177.1ms    2.9ms
            #   duration   DESC      159.9ms    7.4ms
            #   duration   ASC       158.1ms    8.1ms
            #
            # TWO indexes cover all four cases because every
            # DASHBOARD_SORTS entry appends the full primary key and the
            # whole ORDER BY takes ONE direction — so an all-ascending
            # index serves the ascending pages forwards and the
            # descending pages read backwards. An index with a DESC
            # first column and ascending tiebreaks matches NEITHER, and
            # is what a first attempt at this produced.
            #
            # THE TRADE, measured rather than assumed. Every index here
            # is maintained on each upserted test, and a nightly import
            # touches all 12,008:
            #
            #   no extra indexes      4.34s
            #   these two            10.16s   (+130%)
            #   four (both dirs)     13.66s   (+215%)
            #
            # The second pair is redundant, as above. +5.7s on an
            # unattended nightly import buys pages that answer in
            # single-digit milliseconds instead of ~170ms — and far
            # worse than 170ms on the production network mount, where
            # the temp B-tree's page reads are round trips.
            #
            # `result` is already served by idx_latest_runs_result and
            # the default order is the primary key, so neither needs one.
            """
            CREATE INDEX idx_latest_runs_start_sort
                ON latest_runs (start_time, environment, script, test_name)
            """,
            """
            CREATE INDEX idx_latest_runs_duration_sort
                ON latest_runs (duration_seconds, environment, script,
                                test_name)
            """,
        ],
    ),
    (
        5,
        [
            # How many tests an environment is SUPPOSED to run, stated by
            # a person instead of guessed from history.
            #
            # It is the denominator of the coverage test in
            # analytics.find_passes: a block of activity counts as a pass
            # of the suite only if it ran at least half of the
            # environment's tests, and "one whole pass of grace" is what
            # decides whether a quiet test is missing or merely waiting
            # its turn.
            #
            # Inferring that denominator from `latest_runs` gives a
            # HIGH-WATER MARK — every test ever seen in the environment,
            # including ones that quietly left the suite. Too large a
            # denominator means no block reaches coverage, no pass counts,
            # and the cutoff falls back to the 36-hour wall clock: the
            # exact Monday-morning bug the derived cutoff exists to fix,
            # with no symptom anywhere to look at. That silence is why
            # this is declarable.
            #
            # Presence of the row IS the declaration, the same shape as
            # test_retirements and users.deactivated_at: clearing it
            # returns to inference, and there is no flag that can
            # disagree with its own value.
            #
            # `environment` is a TEXT primary key and therefore
            # case-SENSITIVE here, and would not be under a default
            # MariaDB collation — see docs/MARIADB_MIGRATION.md B.3.
            # Deliberately not normalised: `latest_runs.environment` is
            # not normalised either, and one of the two being folded
            # would be worse than neither.
            #
            # MEASURED on the DEV database (218 MB, 540,192 runs,
            # 12,008 tests of generated data), brought to version 4
            # first so this is entry 5 alone: 8 ms, including opening
            # the connection.
            #
            # Production is roughly four times that size and has NOT
            # been measured from here. It does not need to be: CREATE
            # TABLE writes one page, rewrites no existing row and reads
            # none, so the number cannot grow with the database. A
            # migration that touched existing rows would need the real
            # thing — see entry 3, whose backfill does.
            """
            CREATE TABLE environment_expectations (
                environment TEXT PRIMARY KEY,
                expected_tests INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL REFERENCES users(username)
            )
            """,
        ],
    ),
    (
        6,
        [
            # Run counts per (environment, UTC hour, result), maintained
            # inside the import transaction like latest_runs. The third
            # derived table, and it exists because the two questions that
            # read a window of `runs` were the only reads in the codebase
            # whose cost grew with the SIZE OF HISTORY:
            #
            # - activity_buckets (the staleness cutoff, computed by
            #   /api/summary, /api/time, /api/environments and the stale
            #   filter): the planner answered its GROUP BY over
            #   environment with a FULL SCAN of the runs UNIQUE index —
            #   every row ever recorded, not the fortnight asked about.
            #   MEASURED on the dev copy (540,192 runs): 607ms mean in
            #   production traffic, 70% of /api/summary; the production
            #   perf log shows the same shape at 3.5s mean over 4.4M
            #   rows on the network mount, growing every night.
            # - daily_result_counts (the trend chart): a covering range
            #   scan, ~170k index entries per fortnight window.
            #
            # This table answers both from ~30 tiny rows per environment
            # per day, so their cost stops depending on how much history
            # exists. The invariant is exact equality with
            # `SELECT environment, SUBSTR(start_time, 1, 13), result,
            # COUNT(*) FROM runs GROUP BY 1, 2, 3` — enforced by
            # tests/test_storage.py::ActivityHoursTest against live
            # maintenance, environment deletion and pruning.
            #
            # Hours are SUBSTR(start_time, 1, 13) ("YYYY-MM-DDTHH"), the
            # same expression the queries it replaces used, so it means
            # the same thing on MariaDB (docs/MARIADB_MIGRATION.md E.4).
            # No extra index: a year is under ~100k rows and every read
            # is a range over `hour` within a table this small.
            """
            CREATE TABLE activity_hours (
                environment TEXT NOT NULL,
                hour TEXT NOT NULL,
                result TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (environment, hour, result)
            )
            """,
            # SHA-1 of the run's output TEXT, so a re-imported record can
            # be recognised as byte-identical without reading the stored
            # blob back. The site feeder re-pushes its whole recent
            # window every 10 minutes; before this column each of those
            # records rewrote its runs row AND re-REPLACEd its compressed
            # output — MEASURED at ~2.3 KB of WAL per unchanged record,
            # ~23 MB per 10-minute push of 10,000, all through the
            # production network mount for zero information.
            #
            # NULL means "not stamped yet" (every pre-migration row):
            # the first re-import of such a row takes the full write
            # path once and stamps it. No backfill — reading every
            # output blob (~most of the database) in a startup migration
            # is exactly what §1.2 of docs/UPGRADE_PLAN.md forbids; the
            # active window self-heals in one push cycle.
            #
            # O(1): ADD COLUMN with no default rewrites nothing.
            """
            ALTER TABLE runs ADD COLUMN output_fingerprint TEXT
            """,
            # Build activity_hours from the full existing history — one
            # aggregate read pass over `runs`; the rows written are the
            # aggregate itself (1,077 rows on the dev copy).
            #
            # MEASURED on the dev copy (218 MB, 540,192 runs): 2.4s as
            # part of a cold server startup, 0.8s on a warm connection.
            # Production is ~4.4M rows on a network mount and MUST be
            # measured on a copy before the drop ships — the operator
            # note (docs/drops/2026-07-31.md) records that number and
            # what a hung-looking startup means.
            "python: rebuild_activity_hours",
        ],
    ),
    (
        7,
        [
            # Run counts per (environment, UTC hour, script, result),
            # plus the EXACT first start and last end inside each
            # bucket. The fourth derived table, maintained inside the
            # import transaction like the other three. It exists for
            # the Timeline page (WP-18): "what order did the scripts
            # run in last night" is a script x time question, and no
            # existing table has that shape — activity_hours drops the
            # script, latest_runs drops the history, and scanning a
            # window of `runs` at request time is exactly the
            # O(history) mistake migration 6 was bought to end.
            #
            # The two timestamp columns are what give SUB-HOUR ordering
            # (scripts start minutes apart) while the hour bucketing is
            # what keeps the table small: a script touches a handful of
            # hours a night, so this grows like scripts x active hours,
            # two orders of magnitude slower than `runs`.
            #
            # The PRIMARY KEY leads (environment, hour) — the Timeline
            # reads one environment over one block of hours, and that
            # column order makes the read a pure index range instead of
            # a scan of the environment's whole history.
            #
            # The invariant is exact equality with `SELECT environment,
            # SUBSTR(start_time, 1, 13), script, result, COUNT(*),
            # MIN(start_time), MAX(end_time) FROM runs GROUP BY 1, 2,
            # 3, 4` — enforced by tests/test_storage.py::ScriptHoursTest
            # against live maintenance, environment deletion and
            # pruning. SUBSTR keeps the expression portable to MariaDB
            # for the same reason as activity_hours
            # (docs/MARIADB_MIGRATION.md E.4).
            """
            CREATE TABLE script_hours (
                environment TEXT NOT NULL,
                hour TEXT NOT NULL,
                script TEXT NOT NULL,
                result TEXT NOT NULL,
                count INTEGER NOT NULL,
                first_start TEXT NOT NULL,
                last_end TEXT NOT NULL,
                PRIMARY KEY (environment, hour, script, result)
            )
            """,
            # Build script_hours from the full existing history — one
            # aggregate read pass over `runs`, the same shape as
            # migration 6's backfill and subject to the same rule:
            # MEASURE it on a production copy before the drop ships and
            # put the number in the operator note (§1.2).
            "python: rebuild_script_hours",
        ],
    ),
    (
        8,
        [
            # Environment -> product, declared. The same shape and
            # lifecycle as `environment_expectations` (migration 5):
            # presence of the row IS the declaration, `environment` is a
            # case-sensitive TEXT PRIMARY KEY for the same reason as
            # migration 5, and there is no backfill — an environment
            # absent from this table belongs to the implicit product ""
            # (see docs/STREAMS_PLAN.md §2.1).
            #
            # WP-20 (products) is drop 1 of the products & streams work
            # (docs/STREAMS_PLAN.md). Products are a read-time grouping
            # of environments, not a new component of test identity —
            # there is deliberately no product column anywhere else, and
            # `runs` is untouched.
            #
            # O(1): CREATE TABLE writes one page, rewrites no existing
            # row and reads none, exactly like migration 5. MEASURED on
            # a copy of the dev database (220 MB, at version 7): 26.8 ms
            # including opening the connection. Production has not been
            # measured from here and does not need to be, for the same
            # reason migration 5 did not: nothing existing is read or
            # rewritten, so the number cannot grow with the database.
            """
            CREATE TABLE environment_products (
                environment TEXT PRIMARY KEY,
                product     TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT NOT NULL REFERENCES users(username)
            )
            """,
        ],
    ),
]  # type: List[Tuple[int, List[str]]]

#: Prefix marking a migration step that runs Python instead of SQL.
#:
#: Migrations stay a list of STRINGS rather than a mix of strings and
#: callables: the list is hashed by tests/test_migrations.py to freeze
#: the deployed schema, it is read by people, and a function object in
#: the middle of it is neither hashable in a stable way nor legible.
#: A marked string keeps the structure uniform and the dispatch
#: greppable.
_PYTHON_STEP_PREFIX = "python:"


def _backfill_latest_durations(conn: sqlite3.Connection) -> None:
    """Fill ``latest_runs.duration_seconds`` for migration 3.

    Reads the timestamps of the runs ``latest_runs`` already points at
    and computes durations with :func:`model.duration_seconds` — the
    same function the API uses, so the stored value can never disagree
    with a displayed one.

    Bounded by the number of TESTS, not the number of runs: the join is
    on ``latest_runs.run_id``, which is one row per test.
    """
    rows = conn.execute(
        "SELECT lr.environment, lr.script, lr.test_name, "
        "       r.start_time, r.end_time "
        "FROM latest_runs lr JOIN runs r ON r.id = lr.run_id"
    ).fetchall()
    updates = [
        (
            model.duration_seconds(
                model.parse_iso(row[3]), model.parse_iso(row[4])
            ),
            row[0], row[1], row[2],
        )
        for row in rows
    ]
    conn.executemany(
        "UPDATE latest_runs SET duration_seconds = ? "
        "WHERE environment = ? AND script = ? AND test_name = ?",
        updates,
    )


def _rebuild_activity_hours(conn: sqlite3.Connection) -> None:
    """Rebuild ``activity_hours`` from ``runs``, exactly (migration 6).

    DELETE-then-INSERT rather than incremental, because this is the
    definition the incremental maintenance in :meth:`Storage.upsert_runs`
    is held equal to — by ``tests/test_storage.py::ActivityHoursTest``
    and by :meth:`Storage.prune_runs_before`, which calls this after
    deleting history so the invariant survives retention.

    One aggregate read pass over ``runs``; prints its elapsed time when
    there was anything to do, per docs/UPGRADE_PLAN.md §1.2 — a silent
    multi-second startup on the production mount should say what it is.
    """
    started = time.time()
    conn.execute("DELETE FROM activity_hours")
    cursor = conn.execute(
        "INSERT INTO activity_hours (environment, hour, result, count) "
        "SELECT environment, SUBSTR(start_time, 1, 13), result, COUNT(*) "
        "FROM runs GROUP BY environment, SUBSTR(start_time, 1, 13), result"
    )
    if cursor.rowcount:
        print(
            "activity_hours: rebuilt {0} rows in {1:.1f}s".format(
                cursor.rowcount, time.time() - started
            ),
            flush=True,
        )


def _rebuild_script_hours(conn: sqlite3.Connection) -> None:
    """Rebuild ``script_hours`` from ``runs``, exactly (migration 7).

    DELETE-then-INSERT rather than incremental, for the same reason as
    :func:`_rebuild_activity_hours`: this is the definition the
    incremental maintenance in :meth:`Storage.upsert_runs` is held
    equal to — by ``tests/test_storage.py::ScriptHoursTest`` and by
    :meth:`Storage.prune_runs_before`, which calls this after deleting
    history so the invariant survives retention.

    One aggregate read pass over ``runs``; prints its elapsed time when
    there was anything to do, per docs/UPGRADE_PLAN.md §1.2 — a silent
    multi-second startup on the production mount should say what it is.
    """
    started = time.time()
    conn.execute("DELETE FROM script_hours")
    cursor = conn.execute(
        "INSERT INTO script_hours "
        "(environment, hour, script, result, count, first_start, last_end) "
        "SELECT environment, SUBSTR(start_time, 1, 13), script, result, "
        "COUNT(*), MIN(start_time), MAX(end_time) "
        "FROM runs GROUP BY environment, SUBSTR(start_time, 1, 13), "
        "script, result"
    )
    if cursor.rowcount:
        print(
            "script_hours: rebuilt {0} rows in {1:.1f}s".format(
                cursor.rowcount, time.time() - started
            ),
            flush=True,
        )


#: Python migration steps, by the name used after the prefix.
_MIGRATION_STEPS = {
    "backfill_latest_durations": _backfill_latest_durations,
    "rebuild_activity_hours": _rebuild_activity_hours,
    "rebuild_script_hours": _rebuild_script_hours,
}  # type: Dict[str, Any]


def apply_migration_statement(
    conn: sqlite3.Connection, statement: str
) -> None:
    """Apply one migration step — SQL, or a marked Python step.

    Shared by :meth:`Storage._migrate` and the tests that build a
    database at a given version, so the two can never disagree about
    what applying a migration means.

    A Python step runs inside the caller's transaction and must not
    commit or roll back.
    """
    stripped = statement.strip()
    if not stripped.startswith(_PYTHON_STEP_PREFIX):
        conn.execute(statement)
        return
    name = stripped[len(_PYTHON_STEP_PREFIX):].strip()
    step = _MIGRATION_STEPS.get(name)
    if step is None:
        raise RuntimeError(
            "migration refers to an unknown Python step {0!r}; known "
            "steps are {1}".format(name, sorted(_MIGRATION_STEPS))
        )
    step(conn)


class TestSummaryRow(NamedTuple):
    """One dashboard row: the latest run of a test, without ``output``."""

    environment: str
    script: str
    test_name: str
    run_id: int
    result: Result
    start_time: datetime.datetime
    end_time: datetime.datetime
    source_link: str
    known_failure_reason: Optional[str]
    assignee: Optional[str]
    retired_at: Optional[datetime.datetime]
    retired_by: Optional[str]
    #: Newest comment on this test — only populated when the caller asked
    #: for it (``with_latest_comment``), otherwise None.
    latest_comment: Optional["LatestComment"]


class TestStatusRow(NamedTuple):
    """A dashboard row plus the result of the run before the latest one.

    ``prev_result`` is ``None`` for a test with only one recorded run.
    The latest-vs-previous pair is what separates a NEW failure (previous
    run was not FAIL) from a still-failing test, and a fixed test
    (previous FAIL, latest not) — the summary endpoint's core distinction.
    """

    environment: str
    script: str
    test_name: str
    run_id: int
    result: Result
    start_time: datetime.datetime
    end_time: datetime.datetime
    source_link: str
    known_failure_reason: Optional[str]
    assignee: Optional[str]
    retired_at: Optional[datetime.datetime]
    retired_by: Optional[str]
    latest_comment: Optional["LatestComment"]
    prev_result: Optional[Result]


class DailyResultCount(NamedTuple):
    """Count of runs on one UTC calendar day with one result."""

    day: datetime.date
    result: Result
    count: int


class LatestComment(NamedTuple):
    """The newest comment on a test, for list views that show one."""

    author: str
    created_at: datetime.datetime
    text: str


class RollupCount(NamedTuple):
    """One cell of the estate rollup: a GROUP BY count over ``latest_runs``.

    The whole ``/api/summary`` headline — totals, per-environment
    breakdown, new-vs-still-failing, "ran last night" — is derived from a
    few dozen of these instead of from every test row. ``recent`` is True
    when the test's latest run started at or after the recency cutoff.
    """

    environment: str
    result: Result
    prev_result: Optional[Result]
    recent: bool
    retired: bool
    count: int


class DurationSlice(NamedTuple):
    """One row of the "where is the time going" drill-down.

    ``key`` is an environment, a script or a test name depending on the
    level asked for; ``total_seconds`` is the sum of the newest run of
    each test underneath it, and ``test_count`` is how many tests that
    sum covers.
    """

    key: str
    total_seconds: float
    test_count: int


class DurationRollup(NamedTuple):
    """A level of the drill-down, plus what it does NOT include.

    ``excluded_tests`` counts tests left out because they have not
    reported inside the recency window. They still have a duration on
    file, and counting it would claim time was spent that was not — so
    they are dropped, and reported, rather than silently included or
    silently ignored.
    """

    slices: List[DurationSlice]
    total_seconds: float
    test_count: int
    excluded_tests: int


class ScriptFailures(NamedTuple):
    """A script ranked by how many of its tests currently fail."""

    environment: str
    script: str
    failing: int


class FailureStreak(NamedTuple):
    """When a test's current failure run started, and its last pass before.

    ``failing_since`` is the start time of the oldest run in the
    consecutive FAIL streak ending at the test's latest run;
    ``last_pass_before`` is the newest PASS strictly older than that
    streak. Both are None when the latest run is not a FAIL.
    """

    failing_since: Optional[datetime.datetime]
    last_pass_before: Optional[datetime.datetime]


class Comment(NamedTuple):
    """A comment attached to a test (the triple, not a single run)."""

    comment_id: int
    environment: str
    script: str
    test_name: str
    author: str
    created_at: datetime.datetime
    text: str


class User(NamedTuple):
    """A dashboard user: unique username plus creation timestamp.

    ``deactivated_at`` is None for an active user. A deactivated user is
    hidden from the assignee pickers and keeps every comment and
    assignment they ever made; see migration 2.

    No field defaults: ``typing.NamedTuple`` only learned them in 3.6.1,
    and the target is stated as 3.6 without a micro version.
    """

    username: str
    created_at: datetime.datetime
    deactivated_at: Optional[datetime.datetime]
    deactivated_by: Optional[str]

    @property
    def active(self) -> bool:
        """True while this account is offered in assignee pickers."""
        return self.deactivated_at is None


class EnvironmentExpectation(NamedTuple):
    """How many tests an environment is declared to run, and who said so.

    The declared alternative to a denominator inferred from history —
    see migration 5 for why inference is not good enough, and
    :func:`analytics.effective_test_counts` for how the two combine.

    There is deliberately no cadence field. Every boundary in
    :func:`analytics.find_passes` comes from observed gaps, so a declared
    schedule would either sit unread or displace the observation, which
    is the design it would be displacing. Add one when something needs
    it.
    """

    environment: str
    expected_tests: int
    updated_at: datetime.datetime
    updated_by: str


class EnvironmentProduct(NamedTuple):
    """Which product an environment belongs to, and who said so.

    Same shape and lifecycle as :class:`EnvironmentExpectation` (migration
    5): presence of the row IS the declaration. An environment absent
    from this table belongs to the implicit product ``""`` — see
    migration 8 and docs/STREAMS_PLAN.md §2.1.
    """

    environment: str
    product: str
    updated_at: datetime.datetime
    updated_by: str


class ScriptHourBucket(NamedTuple):
    """One ``script_hours`` row: a script's activity inside one UTC hour.

    ``first_start`` and ``last_end`` are exact run timestamps, not hour
    edges — they are what lets the Timeline order scripts that started
    minutes apart without ever reading ``runs``. Feeds
    :func:`testboard.analytics.group_script_executions`.
    """

    script: str
    hour: str
    result: Result
    count: int
    first_start: datetime.datetime
    last_end: datetime.datetime


class UpsertCounts(NamedTuple):
    """Result of a batch upsert.

    ``unchanged`` counts records that were byte-identical to what is
    already stored (metadata and output fingerprint both matching) and
    therefore wrote nothing at all. The site feeder re-pushes its whole
    recent window every 10 minutes, so in steady state this is MOST
    records; the split exists so that a no-op push is visible as one in
    logs and in the import response, instead of masquerading as 10,000
    updates.
    """

    inserted: int
    updated: int
    unchanged: int


#: Every column of ``users``, in :class:`User` field order. One place, so
#: adding a field cannot leave one query returning a short row.
_USER_SELECT = (
    "SELECT username, created_at, deactivated_at, deactivated_by FROM users"
)


def _user_from_row(row: Sequence[Any]) -> "User":
    """Build a :class:`User` from a :data:`_USER_SELECT` row."""
    return User(
        username=row[0],
        created_at=model.parse_iso(row[1]),
        deactivated_at=None if row[2] is None else model.parse_iso(row[2]),
        deactivated_by=row[3],
    )


# Columns fetched for a StoredRun in list contexts (output lives in its
# own table and is only ever read by get_run).
_RUN_COLUMN_NAMES = (
    "id", "environment", "script", "test_name", "result",
    "start_time", "end_time", "source_link", "known_failure_reason",
)
_RUN_COLUMNS = ", ".join(_RUN_COLUMN_NAMES)

#: Allowed ``ORDER BY`` clauses for :meth:`Storage.dashboard`, keyed by the
#: sort name the API accepts. A sort key can never be interpolated into
#: SQL (``ORDER BY`` takes no parameters), so callers may only choose from
#: this table — anything else is rejected before it reaches the database.
#: Every entry ends with the full primary key so that ties order
#: deterministically and paging can never repeat or skip a row.
_PK_ORDER = ("lr.environment", "lr.script", "lr.test_name")
DASHBOARD_SORTS = {
    "environment": _PK_ORDER,
    "script": ("lr.script", "lr.environment", "lr.test_name"),
    "test_name": ("lr.test_name", "lr.environment", "lr.script"),
    "result": ("lr.result",) + _PK_ORDER,
    "start_time": ("lr.start_time",) + _PK_ORDER,
    "assignee": ("ca.assignee",) + _PK_ORDER,
    # Reads the denormalised column on latest_runs (migration 3) rather
    # than `julianday(r.end_time) - julianday(r.start_time)`.
    #
    # The reason is PORTABILITY, not speed. julianday() was the only
    # SQLite-specific date function in the codebase and has no MariaDB
    # equivalent with the same semantics, so it blocked the port.
    # Measured on 12,008 tests / 540,192 runs, replacing it made this
    # sort 1.1x faster — real but marginal, and nothing like the win the
    # first version of this comment claimed.
    #
    # What the sort actually costs is the sort: the same query without
    # ORDER BY runs in 3.9ms and with it in 155ms. An index does not
    # help, because every DASHBOARD_SORTS entry orders its first column
    # in the requested direction and the primary-key tiebreak ASC, and a
    # mixed-direction ORDER BY cannot be served by an all-ASC index.
    # Fixing that needs per-direction indexes for every sort key, which
    # is a separate piece of work with its own measurements.
    "duration": ("lr.duration_seconds",) + _PK_ORDER,
}  # type: Dict[str, Tuple[str, ...]]

#: How many test identities go into one :meth:`Storage.recent_results`
#: query. Each costs three bound parameters and SQLite caps a statement
#: at 999, so 100 leaves room and keeps the query count proportional to
#: page-size-over-100 rather than to page size.
_RECENT_CHUNK = 100

#: Levels the duration drill-down can group by, mapped to their column.
#: A GROUP BY column cannot be a bound parameter, so — exactly like
#: :data:`DASHBOARD_SORTS` — callers choose from this table and anything
#: else is refused before it reaches the database.
_DURATION_GROUPS = {
    "environment": "lr.environment",
    "script": "lr.script",
    "test_name": "lr.test_name",
}  # type: Dict[str, str]

#: Every table keyed by ``environment``, in an order safe to delete in:
#: derived rows before the rows they were derived from, so no statement
#: leaves a reference dangling even inside the transaction.
#:
#: Table names are interpolated into SQL, so this tuple is also the
#: whitelist that makes that safe — nothing reaches the format string
#: from a caller. ``run_outputs`` is absent deliberately: it has no
#: ``environment`` column and is reached through ``runs.id``.
#:
#: ``tests/test_storage.py::EnvironmentDeleteTest`` asserts this covers
#: every such table in the live schema, so a migration that adds one
#: fails the suite instead of quietly leaving its rows behind.
_ENVIRONMENT_TABLES = (
    "latest_runs",
    "current_assignments",
    "assignments",
    "comments",
    "test_retirements",
    "environment_expectations",
    "environment_products",
    "activity_hours",
    "script_hours",
    "runs",
)  # type: Tuple[str, ...]

#: Triage queue membership, as SQL predicates over ``lr`` (latest_runs)
#: and ``ca`` (current_assignments). These are the SQL half of the same
#: definitions :func:`testboard.analytics.summarize_rollup` applies to the
#: rollup counts — ``tests/test_storage.py`` asserts the two agree.
QUEUE_KINDS = (
    "new_failures",
    "still_failing",
    "fixed",
    "unexpected_passes",
    "not_run",
    "assigned",
)

#: Queues whose predicate needs the recency cutoff bound to it.
_STALE_QUEUES = ("not_run",)

_QUEUE_PREDICATES = {
    "new_failures": (
        "lr.result = '{fail}' AND "
        "(lr.prev_result IS NULL OR lr.prev_result <> '{fail}')"
    ),
    "still_failing": "lr.result = '{fail}' AND lr.prev_result = '{fail}'",
    "fixed": "lr.prev_result = '{fail}' AND lr.result <> '{fail}'",
    "unexpected_passes": "lr.result = '{up}'",
    # Stopped reporting. The cutoff is bound as a parameter, not
    # formatted in, so this is the one predicate that takes an argument.
    "not_run": "lr.start_time < ?",
    "assigned": (
        "ca.assignee IS NOT NULL AND lr.result IN ('{fail}', '{up}')"
    ),
}  # type: Dict[str, str]

# Bound to the enum rather than to hand-written literals, so renaming a
# Result member cannot silently leave a queue matching nothing.
_QUEUE_PREDICATES = {
    kind: sql.format(
        fail=Result.FAIL.value, up=Result.UNEXPECTED_PASS.value
    )
    for kind, sql in _QUEUE_PREDICATES.items()
}


def _escape_like(text: str) -> str:
    r"""Escape ``%``, ``_`` and ``\`` in *text* for a LIKE ... ESCAPE '\' pattern."""
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _run_from_row(row: Sequence[Any], output: Optional[str]) -> StoredRun:
    """Convert a runs-table row (per ``_RUN_COLUMNS`` order) to a StoredRun.

    *row* is a raw sqlite tuple (the sole typed boundary with the driver);
    *output* is the output text when it was fetched, else ``None``.
    """
    return StoredRun(
        run_id=int(row[0]),
        environment=row[1],
        script=row[2],
        test_name=row[3],
        result=Result(row[4]),
        start_time=model.parse_iso(row[5]),
        end_time=model.parse_iso(row[6]),
        source_link=row[7],
        known_failure_reason=row[8],
        output=output,
    )


def describe_open_error(path: str, exc: BaseException) -> str:
    """Explain why a database could not be opened, and what to do about it.

    Every entry point (server, feeder tooling, prune) funnels sqlite's
    terse one-liners through here. The generic advice — "check the
    directory exists and is writable" — is wrong for most causes, and an
    operator reading it at 3am should not have to guess which one they
    hit, so the filesystem is probed to name the actual problem.
    """
    detail = str(exc)
    lowered = detail.lower()
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute) or "."

    if "not a database" in lowered:
        return (
            "{0}\n"
            "  This file is not a SQLite database. Either the path is\n"
            "  wrong, or the file was truncated/overwritten.\n"
            "  - check the path is the one you meant\n"
            "  - if it is, restore the database from your last backup\n"
            "  - to start over, move the file aside and let testboard\n"
            "    create a new empty database at that path"
        ).format(absolute)
    if "malformed" in lowered or "disk image" in lowered:
        return (
            "{0}\n"
            "  The database file is corrupt. Restore it from your last\n"
            "  backup. If you have no backup, salvage what you can with:\n"
            "    sqlite3 {0} \".recover\" | sqlite3 recovered.db"
        ).format(absolute)
    if "disk is full" in lowered or "no space" in lowered:
        return (
            "{0}\n"
            "  The disk holding the database is full. Free space, then\n"
            "  reclaim what old runs are using:\n"
            "    python3 tools/prune_runs.py --db {0} --keep-days 365 --vacuum"
        ).format(absolute)
    if "locked" in lowered or "busy" in lowered:
        return (
            "{0}\n"
            "  The database is locked by another process. Something else\n"
            "  is holding a write transaction — usually a second server,\n"
            "  an import, or a prune/VACUUM still running. Wait for it to\n"
            "  finish, or stop it, then retry."
        ).format(absolute)
    if "readonly" in lowered or "permission" in lowered:
        return (
            "{0}\n"
            "  The database (or its directory) is not writable by this\n"
            "  user. Fix the ownership/permissions, e.g.:\n"
            "    chown testboard:testboard {0}"
        ).format(absolute)

    # "unable to open database file" covers several distinct causes;
    # look at the filesystem to say which one this is.
    if os.path.isdir(absolute):
        return (
            "{0}\n"
            "  That path is a directory, not a database file. Point --db\n"
            "  at a file inside it, e.g. {1}"
        ).format(absolute, os.path.join(absolute, "testboard.db"))
    if not os.path.isdir(parent):
        return (
            "{0}\n"
            "  The directory {1} does not exist. Create it first:\n"
            "    mkdir -p {1}"
        ).format(absolute, parent)
    if not os.access(parent, os.W_OK):
        return (
            "{0}\n"
            "  The directory {1} is not writable by this user. Fix the\n"
            "  permissions, or choose a different --db location."
        ).format(absolute, parent)
    return (
        "{0}\n"
        "  SQLite reported: {1}\n"
        "  Check the path, the free space on that filesystem, and that\n"
        "  this user may write there."
    ).format(absolute, detail)


#: Correlated lookup of a test's newest comment. One seek to the high end
#: of ``idx_comments_triple`` per returned row, so it is only ever added
#: to a query that asked for it.
_LATEST_COMMENT_COLUMNS = (
    "(SELECT c.author FROM comments AS c "
    " WHERE c.environment = lr.environment AND c.script = lr.script "
    " AND c.test_name = lr.test_name ORDER BY c.id DESC LIMIT 1), "
    "(SELECT c.created_at FROM comments AS c "
    " WHERE c.environment = lr.environment AND c.script = lr.script "
    " AND c.test_name = lr.test_name ORDER BY c.id DESC LIMIT 1), "
    "(SELECT c.text FROM comments AS c "
    " WHERE c.environment = lr.environment AND c.script = lr.script "
    " AND c.test_name = lr.test_name ORDER BY c.id DESC LIMIT 1)"
)

#: The columns of a status row, in TestSummaryRow field order. Anything
#: selected AFTER them is indexed from len(_STATUS_COLUMN_NAMES), so
#: adding a column here cannot silently shift a later one.
_STATUS_COLUMN_NAMES = (
    "lr.environment", "lr.script", "lr.test_name", "lr.run_id",
    "lr.result", "lr.start_time", "r.end_time", "r.source_link",
    "r.known_failure_reason", "ca.assignee", "tr.retired_at",
    "tr.retired_by",
)

#: Index of the first latest-comment column in a row that includes them,
#: and how many columns it occupies (author, created_at, text).
_LATEST_COMMENT_OFFSET = len(_STATUS_COLUMN_NAMES)
_LATEST_COMMENT_COUNT = 3


def _latest_comment_from(row: Sequence[Any]) -> Optional[LatestComment]:
    """Build a LatestComment from the trailing columns, if there is one."""
    author = row[_LATEST_COMMENT_OFFSET]
    if author is None:
        return None
    return LatestComment(
        author=author,
        created_at=model.parse_iso(row[_LATEST_COMMENT_OFFSET + 1]),
        text=row[_LATEST_COMMENT_OFFSET + 2],
    )


def _summary_row_from(
    row: Sequence[Any], latest_comment: Optional[LatestComment] = None
) -> TestSummaryRow:
    """Convert a row in ``Storage._STATUS_COLUMNS`` order to a TestSummaryRow."""
    return TestSummaryRow(
        environment=row[0],
        script=row[1],
        test_name=row[2],
        run_id=int(row[3]),
        result=Result(row[4]),
        start_time=model.parse_iso(row[5]),
        end_time=model.parse_iso(row[6]),
        source_link=row[7],
        known_failure_reason=row[8],
        assignee=row[9],
        retired_at=None if row[10] is None else model.parse_iso(row[10]),
        retired_by=row[11],
        latest_comment=latest_comment,
    )


class _SqliteBackend(object):
    """The SQLite half of the storage seam.

    A backend owns HOW a connection is made and the few SQL spellings
    that differ by engine; :class:`Storage` owns WHAT is executed — the
    method bodies, the transactions, the SELECT-first upsert shape. The
    MariaDB counterpart lives in ``testboard.mariadb`` and is imported
    only by :meth:`Storage.mariadb`, so a SQLite deployment never loads
    the vendored driver.

    The engine-specific fragments are class attributes ON PURPOSE:
    ``tests/test_sql_portability.py`` inventories THIS file's string
    literals, and moving the SQLite spellings elsewhere would blind
    that scan.

    ``conn`` parameters across Storage stay annotated
    ``sqlite3.Connection``: that is the shape both backends present —
    ``execute``/``executemany`` returning a cursor with
    ``fetchone``/``fetchall``/``rowcount``/``lastrowid`` — and the
    MariaDB wrapper duck-types it.
    """

    #: Raised on a unique-key race; ensure_user/create_user catch it.
    integrity_error = sqlite3.IntegrityError

    #: SQLite owns its schema: MIGRATIONS run at startup. The MariaDB
    #: backend never runs DDL — its schema is created by the migration
    #: tooling (docs/MARIADB_MIGRATION.md §D) and only VERIFIED here.
    runs_migrations = True

    #: Substring search. SQLite's LIKE is ASCII-case-insensitive and
    #: its string literals do not process backslashes, so the wire
    #: carries ESCAPE '\'. MariaDB needs different spelling AND an
    #: explicit collation to keep search case-insensitive over the
    #: migrated _bin columns.
    like_test_name = "lr.test_name LIKE ? ESCAPE '\\'"

    #: "No limit, but an offset" — SQLite spells no-limit as -1, which
    #: MariaDB rejects outright.
    limit_all_offset = " LIMIT -1 OFFSET ?"

    def __init__(self, path: str, cache_mb: Optional[int],
                 mmap_mb: Optional[int], max_connections: int) -> None:
        self._path = path
        self._cache_mb = cache_mb
        self._mmap_mb = mmap_mb
        self._max_connections = max_connections

    def connect(self) -> sqlite3.Connection:
        """One new connection, with the pragmas every connection gets."""
        conn = sqlite3.connect(
            self._path, detect_types=0, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._apply_cache_pragmas(conn)
        return conn

    def _apply_cache_pragmas(self, conn: sqlite3.Connection) -> None:
        """Set the page cache and mmap size for one connection.

        SQLite's default cache is 2 MB. Against a database of a few
        hundred megabytes that means nearly every read misses and goes to
        the filesystem — invisible on local disk, where the OS page cache
        absorbs it, and very visible on a network mount, where each miss
        is a round trip.

        The share is per connection because the cache is: the caller's
        budget is for the process, and there is one connection per
        thread.
        """
        if self._cache_mb is not None:
            share_kib = max(
                _MIN_CACHE_KIB,
                int(self._cache_mb) * 1024 // self._max_connections,
            )
            # Negative means KiB rather than pages, so the budget does not
            # silently change meaning with the database's page size.
            conn.execute("PRAGMA cache_size=-{0}".format(share_kib))
        if self._mmap_mb is not None:
            conn.execute("PRAGMA mmap_size={0}".format(
                max(0, int(self._mmap_mb)) * 1024 * 1024))

    def cache_bytes_per_connection(self) -> Optional[int]:
        """The per-connection page cache this backend asks for, or None.

        SQLite-specific arithmetic by nature: InnoDB has one shared
        buffer pool for the whole server, not a cache per connection,
        so the MariaDB backend answers None (runbook §B.4).
        """
        if self._cache_mb is None:
            return None
        return max(
            _MIN_CACHE_KIB,
            int(self._cache_mb) * 1024 // self._max_connections,
        ) * 1024

    def vacuum(self, conn: sqlite3.Connection) -> None:
        """Rebuild the file. SQLite-shaped maintenance; see Storage.vacuum."""
        conn.execute("VACUUM")


class Storage:
    """Backend-agnostic storage with per-thread connections.

    ``Storage(path)`` is SQLite, exactly as it always was: constructing
    one opens a connection for the calling thread and immediately runs
    any pending schema migrations; each other thread lazily gets its own
    connection with the same pragmas (WAL journal, 10s busy timeout,
    foreign keys on). SQLite is a permanent first-class backend — the
    zero-setup path a second instance starts on — not a legacy mode.

    :meth:`Storage.mariadb` is the same object over MariaDB via the
    vendored driver: same method bodies, same transactions, different
    connection factory and a handful of dialect spellings
    (see :class:`_SqliteBackend`).
    """

    def __init__(self, path: str, cache_mb: Optional[int] = None,
                 mmap_mb: Optional[int] = None,
                 max_connections: int = DEFAULT_MAX_CONNECTIONS) -> None:
        """Open the SQLite database at *path* and migrate immediately.

        ``cache_mb`` is a budget for the WHOLE process, not per
        connection. That distinction is the entire reason this parameter
        exists: connections are thread-local, so a value passed straight
        to ``PRAGMA cache_size`` is multiplied by however many threads
        the server happens to have open. Asking for 512 MB and getting
        10 GB is a memory exhaustion bug, not a tuning win. The budget is
        therefore divided by ``max_connections`` before it is used.

        ``mmap_mb`` maps the database instead of read()ing it, letting
        the OS page cache serve pages with no copy. It is a large win on
        local disk and worth nothing - occasionally worse - on a network
        mount, where the pages are not really local to cache. Off by
        default for that reason.
        """
        connections = max(1, int(max_connections))
        self._init_with_backend(
            _SqliteBackend(path, cache_mb, mmap_mb, connections),
            connections)

    def _init_with_backend(self, backend: Any, max_connections: int) -> None:
        """The shared half of construction; *backend* decides the engine.

        ``backend`` is duck-typed (see :class:`_SqliteBackend` for the
        surface) because naming the MariaDB class here would import the
        driver into every SQLite deployment.
        """
        self._backend = backend
        self._max_connections = max(1, int(max_connections))
        self._local = threading.local()
        self._trend_cache = {}  # type: Dict[Tuple[str, Optional[str], Optional[Tuple[str, ...]]], Tuple[float, List[DailyResultCount]]]
        self._trend_lock = threading.Lock()
        self._migrate()

    @classmethod
    def mariadb(cls, settings: dbconfig.Settings,
                max_connections: int = DEFAULT_MAX_CONNECTIONS) -> "Storage":
        """The same Storage over MariaDB, via the vendored driver.

        *settings* comes from :func:`testboard.dbconfig.read_option_file`
        — the §A.10 credentials file. The import is inside the method so
        that a SQLite deployment (``Storage(path)``) never loads the
        driver: SQLite is not a fallback here, it is the other equal
        backend.

        The database must already hold the schema the migration tooling
        creates, at exactly this build's version — this constructor
        verifies and refuses, it never migrates (runbook §D, §F).
        """
        from testboard import mariadb as mariadb_backend
        store = cls.__new__(cls)
        store._init_with_backend(
            mariadb_backend.MariaDBBackend(settings), max_connections)
        return store

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, opening it on first use.

        The annotation says ``sqlite3.Connection`` because that is the
        shape callers rely on; the MariaDB backend returns a wrapper
        presenting the same one (execute/executemany/close).
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._backend.connect()
            self._local.conn = conn
        return conn

    @property
    def max_connections(self) -> int:
        """Connections this Storage was sized for.

        The server uses this as its worker-pool size, because the pool
        size *is* the connection count: one long-lived connection per
        worker thread. Deriving one from the other keeps the cache
        arithmetic true instead of leaving two numbers to drift apart.
        """
        return self._max_connections

    def cache_bytes_per_connection(self) -> Optional[int]:
        """The per-connection cache this Storage asks for, or None."""
        return self._backend.cache_bytes_per_connection()

    def close(self) -> None:
        """Close the calling thread's connection, if it has one open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Create ``schema_version`` if absent and apply pending migrations.

        All pending migrations plus the version bump happen inside one
        transaction, so a failed migration leaves the schema untouched.

        A database written by a NEWER testboard than this one is refused
        rather than used: its schema may have columns or tables this code
        does not know about, and writing to it with older code is how a
        database gets quietly corrupted.

        Only the SQLite backend ever runs the DDL below. On MariaDB the
        schema is created by the migration tooling (runbook §D), so the
        backend VERIFIES the stored version matches this build exactly
        and refuses in both directions — an app that silently ran SQLite
        DDL against InnoDB would be worse than one that stopped.
        """
        conn = self._conn()
        if not self._backend.runs_migrations:
            self._backend.check_schema(conn, MIGRATIONS[-1][0])
            return
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")
            current = 0
        else:
            current = int(row[0])

        latest = MIGRATIONS[-1][0]
        if current > latest:
            raise RuntimeError(
                "this database was created by a NEWER version of "
                "testboard (its schema version is {0}; this build "
                "understands up to {1}). Using it with older code could "
                "corrupt it. Update this checkout to the version that "
                "wrote the database, or point --db at a different "
                "file.".format(current, latest)
            )

        pending = [
            (version, statements)
            for version, statements in MIGRATIONS
            if version > current
        ]
        if not pending:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for _version, statements in pending:
                for statement in statements:
                    apply_migration_statement(conn, statement)
            conn.execute(
                "UPDATE schema_version SET version = ?", (pending[-1][0],)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def upsert_runs(self, records: Sequence[RunRecord]) -> UpsertCounts:
        """Insert or update a batch of runs in ONE transaction.

        A run is keyed by ``(environment, script, test_name, start_time)``.
        For each record the existing row is looked up (an index hit on
        the UNIQUE constraint); if found the row is UPDATEd in place
        (preserving its rowid), otherwise a new row is INSERTed.

        A record IDENTICAL to what is stored — every metadata field
        equal and the output fingerprint matching — writes NOTHING: no
        runs update, no output re-REPLACE, no ``latest_runs`` touch, no
        un-retirement, no memo invalidation. The site feeder re-pushes
        its whole recent window every 10 minutes whether anything ran or
        not, and before this check each of those pushes rewrote ~10,000
        rows plus their compressed outputs (~23 MB of WAL) through the
        production network mount, evicting the page caches every reader
        depends on. It also silently un-retired tests: a re-pushed OLD
        run is not the test "reporting a run again", and treating it as
        one made retirement impossible to keep for more than 10 minutes.

        A NULL fingerprint (any row imported before migration 6) never
        matches, so such a record takes the full write path once and is
        stamped; the active window self-heals in one push cycle.

        ``activity_hours`` is maintained here, in the same transaction:
        +1 for each inserted run, and a paired -1/+1 when an update
        changes a stored result. Start times are immutable (they are
        part of the run's key), so a run can never move between hours.

        ``script_hours`` is maintained the same way, with one extra
        wrinkle: its buckets carry MIN(start_time) and MAX(end_time),
        and a MIN/MAX cannot be decremented. Inserts only ever GROW a
        bucket, so they are applied as merged deltas like the counts;
        an update that changes a stored result or end time could
        SHRINK one, so those (rare — the fingerprint skip means a
        re-push of unchanged data never gets here) mark their buckets
        for exact recomputation from ``runs`` instead. See
        :meth:`_apply_script_hour_changes`.
        """
        conn = self._conn()
        inserted = 0
        updated = 0
        unchanged = 0
        # Net activity_hours changes for this batch, applied in one pass
        # at the end: a batch of 500 typically spans a handful of
        # (environment, hour, result) cells, not 500.
        deltas = {}  # type: Dict[Tuple[str, str, str], int]
        # script_hours changes: pure growth (from inserts), merged per
        # bucket, plus the buckets an update may have shrunk.
        grown = {}  # type: Dict[Tuple[str, str, str, str], List[Any]]
        recompute = set()  # type: Set[Tuple[str, str, str, str]]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rec in records:
                start = model.format_iso(rec.start_time)
                end = model.format_iso(rec.end_time)
                fingerprint = _output_fingerprint(rec.output)
                row = conn.execute(
                    "SELECT id, result, end_time, source_link, "
                    "known_failure_reason, output_fingerprint "
                    "FROM runs WHERE environment = ? AND "
                    "script = ? AND test_name = ? AND start_time = ?",
                    (rec.environment, rec.script, rec.test_name, start),
                ).fetchone()
                if (
                    row is not None
                    and row[1] == rec.result.value
                    and row[2] == end
                    and row[3] == rec.source_link
                    and row[4] == rec.known_failure_reason
                    and row[5] == fingerprint
                ):
                    unchanged += 1
                    continue
                hour = start[:13]
                if row is None:
                    cursor = conn.execute(
                        "INSERT INTO runs (environment, script, test_name, "
                        "result, start_time, end_time, source_link, "
                        "known_failure_reason, output_fingerprint) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            rec.environment,
                            rec.script,
                            rec.test_name,
                            rec.result.value,
                            start,
                            end,
                            rec.source_link,
                            rec.known_failure_reason,
                            fingerprint,
                        ),
                    )
                    run_id = int(cursor.lastrowid)
                    inserted += 1
                    key = (rec.environment, hour, rec.result.value)
                    deltas[key] = deltas.get(key, 0) + 1
                    script_key = (
                        rec.environment, hour, rec.script, rec.result.value
                    )
                    growth = grown.get(script_key)
                    if growth is None:
                        grown[script_key] = [1, start, end]
                    else:
                        growth[0] += 1
                        if start < growth[1]:
                            growth[1] = start
                        if end > growth[2]:
                            growth[2] = end
                else:
                    run_id = int(row[0])
                    conn.execute(
                        "UPDATE runs SET result = ?, end_time = ?, "
                        "source_link = ?, known_failure_reason = ?, "
                        "output_fingerprint = ? WHERE id = ?",
                        (
                            rec.result.value,
                            end,
                            rec.source_link,
                            rec.known_failure_reason,
                            fingerprint,
                            run_id,
                        ),
                    )
                    updated += 1
                    if row[1] != rec.result.value:
                        old_key = (rec.environment, hour, row[1])
                        new_key = (rec.environment, hour, rec.result.value)
                        deltas[old_key] = deltas.get(old_key, 0) - 1
                        deltas[new_key] = deltas.get(new_key, 0) + 1
                    if row[1] != rec.result.value or row[2] != end:
                        # Either bucket may have shrunk; both are
                        # recomputed from `runs` once the batch's row
                        # writes are all in. When only the end time
                        # changed the two keys are the same key.
                        recompute.add(
                            (rec.environment, hour, rec.script, row[1])
                        )
                        recompute.add((
                            rec.environment, hour, rec.script,
                            rec.result.value,
                        ))
                # The payload lives in its own table, deflated;
                # re-importing a run replaces it — unless the
                # fingerprint says the stored bytes are already right,
                # which spares the largest table in the database a
                # delete-and-reinsert of a blob it already holds.
                if row is None or row[5] != fingerprint:
                    conn.execute(
                        "INSERT OR REPLACE INTO run_outputs "
                        "(run_id, output) VALUES (?, ?)",
                        (run_id, _compress_output(rec.output)),
                    )
                self._maintain_latest(conn, rec, run_id, start)
                self._unretire_on_new_run(conn, rec, start)
            self._apply_activity_deltas(conn, deltas)
            self._apply_script_hour_changes(conn, grown, recompute)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if inserted or updated:
            # An all-unchanged push proved the memoized trend is still
            # true; clearing it would make the feeder's 10-minute no-op
            # re-push defeat the memo forever.
            self._invalidate_trend_cache()
        return UpsertCounts(
            inserted=inserted, updated=updated, unchanged=unchanged
        )

    @staticmethod
    def _apply_activity_deltas(
        conn: sqlite3.Connection,
        deltas: Dict[Tuple[str, str, str], int],
    ) -> None:
        """Apply a batch's net ``activity_hours`` changes, exactly.

        SELECT-then-UPDATE-or-INSERT, per this module's portability rule
        (no ``ON CONFLICT DO UPDATE`` on 3.6's sqlite). Rows that reach
        zero are DELETEd, not kept: the invariant is byte equality with
        the ``GROUP BY`` over ``runs`` (see :func:`_rebuild_activity_hours`),
        and a GROUP BY yields no zero-count groups.
        """
        for (environment, hour, result), delta in sorted(deltas.items()):
            if delta == 0:
                continue
            row = conn.execute(
                "SELECT count FROM activity_hours WHERE environment = ? "
                "AND hour = ? AND result = ?",
                (environment, hour, result),
            ).fetchone()
            count = (0 if row is None else int(row[0])) + delta
            if count <= 0:
                conn.execute(
                    "DELETE FROM activity_hours WHERE environment = ? "
                    "AND hour = ? AND result = ?",
                    (environment, hour, result),
                )
            elif row is None:
                conn.execute(
                    "INSERT INTO activity_hours "
                    "(environment, hour, result, count) "
                    "VALUES (?, ?, ?, ?)",
                    (environment, hour, result, count),
                )
            else:
                conn.execute(
                    "UPDATE activity_hours SET count = ? "
                    "WHERE environment = ? AND hour = ? AND result = ?",
                    (count, environment, hour, result),
                )

    @staticmethod
    def _apply_script_hour_changes(
        conn: sqlite3.Connection,
        grown: Dict[Tuple[str, str, str, str], List[Any]],
        recompute: Set[Tuple[str, str, str, str]],
    ) -> None:
        """Apply a batch's net ``script_hours`` changes, exactly.

        Two kinds of change, because MIN/MAX cannot be decremented:

        - *grown* buckets only ever got bigger (inserted runs), so the
          stored row is merged with the batch's count/min/max — one
          SELECT-then-UPDATE-or-INSERT per touched bucket, same
          portability rule as :meth:`_apply_activity_deltas`.
        - *recompute* buckets may have SHRUNK (an update changed a
          stored result or end time), so they are re-derived from
          ``runs`` outright. The query is bounded by one script's index
          range and runs only when a re-import actually changed a
          stored row, which the fingerprint skip makes rare. A bucket
          both grown and recomputed is recomputed only — by the time
          this runs, the batch's inserts are already in ``runs``, so
          the recomputation already counts them.

        Rows whose count reaches zero are DELETEd: the invariant is
        equality with a GROUP BY over ``runs``, and a GROUP BY yields
        no empty groups.
        """
        for key in sorted(recompute):
            environment, hour, script, result = key
            grown.pop(key, None)
            row = conn.execute(
                "SELECT COUNT(*), MIN(start_time), MAX(end_time) "
                "FROM runs WHERE environment = ? AND script = ? "
                "AND result = ? AND SUBSTR(start_time, 1, 13) = ?",
                (environment, script, result, hour),
            ).fetchone()
            count = int(row[0])
            conn.execute(
                "DELETE FROM script_hours WHERE environment = ? "
                "AND hour = ? AND script = ? AND result = ?",
                (environment, hour, script, result),
            )
            if count > 0:
                conn.execute(
                    "INSERT INTO script_hours (environment, hour, "
                    "script, result, count, first_start, last_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (environment, hour, script, result, count,
                     row[1], row[2]),
                )
        for key, growth in sorted(grown.items()):
            environment, hour, script, result = key
            row = conn.execute(
                "SELECT count, first_start, last_end FROM script_hours "
                "WHERE environment = ? AND hour = ? AND script = ? "
                "AND result = ?",
                (environment, hour, script, result),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO script_hours (environment, hour, "
                    "script, result, count, first_start, last_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (environment, hour, script, result,
                     growth[0], growth[1], growth[2]),
                )
            else:
                conn.execute(
                    "UPDATE script_hours SET count = ?, first_start = ?, "
                    "last_end = ? WHERE environment = ? AND hour = ? "
                    "AND script = ? AND result = ?",
                    (
                        int(row[0]) + growth[0],
                        min(row[1], growth[1]),
                        max(row[2], growth[2]),
                        environment, hour, script, result,
                    ),
                )

    @staticmethod
    def _previous_result(
        conn: sqlite3.Connection,
        environment: str,
        script: str,
        test_name: str,
        before: str,
    ) -> Optional[str]:
        """Result of the newest run of the triple older than *before*.

        One seek on the UNIQUE ``(environment, script, test_name,
        start_time)`` index; None when the run is the test's first.
        """
        row = conn.execute(
            "SELECT result FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND start_time < ? "
            "ORDER BY start_time DESC LIMIT 1",
            (environment, script, test_name, before),
        ).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _maintain_latest(
        conn: sqlite3.Connection,
        rec: RunRecord,
        run_id: int,
        start: str,
    ) -> None:
        """Keep ``latest_runs`` describing the test's newest run.

        Called inside the upsert transaction for every record. The row
        carries the newest run's ``result`` and the result of the run
        before it, both of which every estate-wide read depends on, so
        all four orderings a record can arrive in are handled explicitly:

        - first sighting of the triple — insert, deriving ``prev_result``
          from ``runs`` (older runs may already exist);
        - newer than the current latest (the nightly case) — the run the
          row pointed at becomes the previous one, no query needed;
        - the same run re-imported — its result may have changed, its
          predecessor cannot have;
        - older than the current latest (a backfill) — the pointer stays
          put, but this run may now sit immediately before it, so
          ``prev_result`` is re-derived.

        Lexical string comparison equals time comparison for these
        timestamps.
        """
        row = conn.execute(
            "SELECT start_time, result FROM latest_runs "
            "WHERE environment = ? AND script = ? AND test_name = ?",
            (rec.environment, rec.script, rec.test_name),
        ).fetchone()
        # Computed with the same function the API serialises with, so a
        # stored duration and a displayed one cannot disagree.
        duration = model.duration_seconds(rec.start_time, rec.end_time)
        if row is None:
            conn.execute(
                "INSERT INTO latest_runs (environment, script, test_name, "
                "run_id, start_time, result, prev_result, duration_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.environment,
                    rec.script,
                    rec.test_name,
                    run_id,
                    start,
                    rec.result.value,
                    Storage._previous_result(
                        conn, rec.environment, rec.script, rec.test_name,
                        start,
                    ),
                    duration,
                ),
            )
            return

        latest_start, latest_result = row[0], row[1]
        key = (rec.environment, rec.script, rec.test_name)
        if start > latest_start:
            conn.execute(
                "UPDATE latest_runs SET run_id = ?, start_time = ?, "
                "result = ?, prev_result = ?, duration_seconds = ? "
                "WHERE environment = ? AND script = ? AND test_name = ?",
                (run_id, start, rec.result.value, latest_result, duration)
                + key,
            )
        elif start == latest_start:
            # The same run re-imported. Its end_time may have been
            # corrected, so the duration is rewritten too — a re-import
            # exists to repair a record, and a stale duration would
            # survive the repair.
            conn.execute(
                "UPDATE latest_runs SET run_id = ?, result = ?, "
                "duration_seconds = ? "
                "WHERE environment = ? AND script = ? AND test_name = ?",
                (run_id, rec.result.value, duration) + key,
            )
        else:
            conn.execute(
                "UPDATE latest_runs SET prev_result = ? "
                "WHERE environment = ? AND script = ? AND test_name = ?",
                (
                    Storage._previous_result(
                        conn, rec.environment, rec.script, rec.test_name,
                        latest_start,
                    ),
                ) + key,
            )

    # ------------------------------------------------------------------
    # Estate reads (the dashboard list and the summary rollups)
    #
    # Every query below filters and counts against `latest_runs` alone —
    # one small row per test — and only joins `runs` for the handful of
    # rows it actually returns. That is what keeps a 12k-test estate
    # sitting on half a million runs answering in single-digit
    # milliseconds.
    # ------------------------------------------------------------------

    #: FROM/JOIN shared by every query that returns whole status rows.
    _LATEST_JOIN = (
        "FROM latest_runs AS lr "
        "JOIN runs AS r ON r.id = lr.run_id "
        "LEFT JOIN current_assignments AS ca "
        "  ON ca.environment = lr.environment "
        " AND ca.script = lr.script "
        " AND ca.test_name = lr.test_name "
        "LEFT JOIN test_retirements AS tr "
        "  ON tr.environment = lr.environment "
        " AND tr.script = lr.script "
        " AND tr.test_name = lr.test_name"
    )

    #: The same retirement join for queries that count rather than return
    #: rows (no need to touch `runs`).
    _LATEST_COUNT_JOIN = (
        "FROM latest_runs AS lr "
        "LEFT JOIN current_assignments AS ca "
        "  ON ca.environment = lr.environment "
        " AND ca.script = lr.script "
        " AND ca.test_name = lr.test_name "
        "LEFT JOIN test_retirements AS tr "
        "  ON tr.environment = lr.environment "
        " AND tr.script = lr.script "
        " AND tr.test_name = lr.test_name"
    )

    #: Excludes tests approved as no longer in the suite.
    _NOT_RETIRED = "tr.retired_at IS NULL"

    #: Columns of a TestSummaryRow / TestStatusRow, in NamedTuple order.
    _STATUS_COLUMNS = ", ".join(_STATUS_COLUMN_NAMES)

    @staticmethod
    def _environments_clause(
        environments: Optional[Sequence[str]], column: str = "lr.environment"
    ) -> Tuple[Optional[str], List[Any]]:
        """One AND-able clause for an optional environment allow-list.

        Shared by every reader that WP-20's ``product=`` filter reaches
        (the product is resolved to its environments once, at the API
        boundary — storage never hears the word "product").

        ``None`` means "no filter": the caller omits the clause
        entirely, exactly as if this helper did not exist. An
        explicitly EMPTY sequence means "match nothing" — a product
        that resolved to zero environments (unknown, or declared with
        none) — and must filter out every row, the opposite of no
        filter. ``"1=0"`` says that without a parameter, which is what
        keeps an empty ``IN ()`` (invalid SQL) off the wire.
        """
        if environments is None:
            return None, []
        if not environments:
            return "1=0", []
        placeholders = ", ".join("?" for _ in environments)
        return "{} IN ({})".format(column, placeholders), list(environments)

    def _dashboard_filters(
        self,
        environment: Optional[str],
        script: Optional[str],
        result_values: Optional[List[str]],
        q: Optional[str],
        stale_before: Optional[datetime.datetime],
        include_retired: bool = False,
        assignees: Optional[Sequence[str]] = None,
        include_unassigned: bool = False,
        environments: Optional[Sequence[str]] = None,
    ) -> Tuple[List[str], List[Any]]:
        """Build the shared WHERE clauses for the dashboard list and count.

        *include_retired* keeps tests approved as no longer in the suite;
        by default they are hidden, which is the whole point of retiring
        one. *assignees* and *include_unassigned* combine as OR — "show
        me Alice's and Bob's open items, plus anything nobody owns".
        *environments* is the WP-20 product filter, resolved by the
        caller to an allow-list — see :meth:`_environments_clause`. It
        combines with *environment* by AND, which is never contradictory
        in practice: a caller passes one or the other, never both with
        different values.
        """
        clauses = []  # type: List[str]
        params = []  # type: List[Any]
        if environment is not None:
            clauses.append("lr.environment = ?")
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            clauses.append(envs_clause)
            params.extend(envs_params)
        if script is not None:
            clauses.append("lr.script = ?")
            params.append(script)
        if result_values is not None:
            placeholders = ", ".join("?" for _ in result_values)
            clauses.append("lr.result IN ({})".format(placeholders))
            params.extend(result_values)
        if q is not None:
            # The clause text is the backend's: LIKE semantics and the
            # ESCAPE spelling both differ by engine (_SqliteBackend
            # documents how). The pattern built here is shared.
            clauses.append(self._backend.like_test_name)
            params.append("%{}%".format(_escape_like(q)))
        if stale_before is not None:
            clauses.append("lr.start_time < ?")
            params.append(model.format_iso(stale_before))
        if not include_retired:
            clauses.append(Storage._NOT_RETIRED)

        owner_clauses = []  # type: List[str]
        if assignees:
            placeholders = ", ".join("?" for _ in assignees)
            owner_clauses.append(
                "ca.assignee IN ({})".format(placeholders)
            )
            params.extend(assignees)
        if include_unassigned:
            owner_clauses.append("ca.assignee IS NULL")
        if owner_clauses:
            clauses.append("({})".format(" OR ".join(owner_clauses)))
        return clauses, params

    @staticmethod
    def _order_by(sort: str, descending: bool) -> str:
        """Render an ORDER BY clause from the :data:`DASHBOARD_SORTS` table.

        ``sort`` must be a key of that table — ORDER BY cannot be
        parameterized, so an unknown key is a programming error here and
        a 400 at the API boundary, never a string pasted into SQL.
        """
        try:
            columns = DASHBOARD_SORTS[sort]
        except KeyError:
            raise ValueError("unknown sort key: {!r}".format(sort))
        direction = " DESC" if descending else " ASC"
        return " ORDER BY " + ", ".join(
            column + direction for column in columns
        )

    def dashboard(
        self,
        environment: Optional[str] = None,
        script: Optional[str] = None,
        results: Optional[Sequence[Result]] = None,
        q: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        include_retired: bool = False,
        assignees: Optional[Sequence[str]] = None,
        include_unassigned: bool = False,
        with_latest_comment: bool = False,
        sort: str = "environment",
        descending: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
        environments: Optional[Sequence[str]] = None,
    ) -> List[TestSummaryRow]:
        """Return ONE PAGE of the latest run per test, never with ``output``.

        Filters: exact *environment*, exact *script*, ``result IN``
        *results* (an explicitly empty sequence matches nothing), *q* as a
        substring match on ``test_name`` (``LIKE`` with ``%``/``_``/``\\``
        escaped and ``ESCAPE '\\'``; case-insensitive for ASCII letters,
        per SQLite's default LIKE), and *stale_before* keeping only tests
        whose latest run started before it ("not run recently"). Tests
        retired as no longer in the suite are hidden unless
        *include_retired*; *assignees*/*include_unassigned* narrow by
        owner. *environments* is the WP-20 ``product=`` filter, already
        resolved to an allow-list by the caller — see
        :meth:`_environments_clause`. With *with_latest_comment* each row
        carries the newest comment on that test — an index seek per
        returned row, so it is opt-in and never paid for by the home
        screen.

        *sort* is a key of :data:`DASHBOARD_SORTS`; every ordering ends
        with the full test identity, so *limit*/*offset* paging is stable
        and can neither repeat nor skip a row. Pass ``limit=None`` for
        every matching row — reserved for callers that know the result set
        is small, since the whole point of this signature is not to
        materialize an estate. :meth:`dashboard_count` gives the exact
        total for the same filters.
        """
        result_values = (
            None if results is None else [r.value for r in results]
        )  # type: Optional[List[str]]
        if result_values is not None and not result_values:
            return []

        clauses, params = self._dashboard_filters(
            environment, script, result_values, q, stale_before,
            include_retired, assignees, include_unassigned, environments,
        )
        columns = self._STATUS_COLUMNS
        if with_latest_comment:
            columns += ", " + _LATEST_COMMENT_COLUMNS
        sql = "SELECT {} {}".format(columns, self._LATEST_JOIN)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += self._order_by(sort, descending)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += self._backend.limit_all_offset
            params.append(offset)

        rows = self._conn().execute(sql, params).fetchall()
        if not with_latest_comment:
            return [_summary_row_from(row) for row in rows]
        return [_summary_row_from(row, _latest_comment_from(row))
                for row in rows]

    def dashboard_count(
        self,
        environment: Optional[str] = None,
        script: Optional[str] = None,
        results: Optional[Sequence[Result]] = None,
        q: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        include_retired: bool = False,
        assignees: Optional[Sequence[str]] = None,
        include_unassigned: bool = False,
        environments: Optional[Sequence[str]] = None,
    ) -> int:
        """Exact number of tests matching the same filters as :meth:`dashboard`."""
        result_values = (
            None if results is None else [r.value for r in results]
        )  # type: Optional[List[str]]
        if result_values is not None and not result_values:
            return 0
        clauses, params = self._dashboard_filters(
            environment, script, result_values, q, stale_before,
            include_retired, assignees, include_unassigned, environments,
        )
        sql = "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._conn().execute(sql, params).fetchone()
        return int(row[0])

    def environments(self) -> List[str]:
        """Return every distinct environment with a recorded test, sorted."""
        rows = self._conn().execute(
            "SELECT DISTINCT environment FROM latest_runs "
            "ORDER BY environment"
        ).fetchall()
        return [row[0] for row in rows]

    def scripts(self, environment: Optional[str] = None) -> List[str]:
        """Return every distinct script name (optionally in one env), sorted."""
        sql = "SELECT DISTINCT script FROM latest_runs"
        params = []  # type: List[Any]
        if environment is not None:
            sql += " WHERE environment = ?"
            params.append(environment)
        sql += " ORDER BY script"
        return [row[0] for row in self._conn().execute(sql, params)]

    def assignees(self) -> List[str]:
        """Return every user who currently owns at least one test, sorted."""
        rows = self._conn().execute(
            "SELECT DISTINCT assignee FROM current_assignments "
            "WHERE assignee IS NOT NULL ORDER BY assignee"
        ).fetchall()
        return [row[0] for row in rows]

    def summary_rollup(
        self,
        recent_cutoff: datetime.datetime,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> List[RollupCount]:
        """Group the whole estate by environment, result, previous result.

        Returns a few dozen :class:`RollupCount` cells — one GROUP BY over
        ``latest_runs`` — from which
        :func:`testboard.analytics.summarize_rollup` derives every
        headline number. A test counts as having run recently when its
        latest run started at or after *recent_cutoff*. *environments* is
        the WP-20 ``product=`` filter — see :meth:`_environments_clause`.
        """
        sql = (
            "SELECT lr.environment, lr.result, lr.prev_result, "
            "CASE WHEN lr.start_time >= ? THEN 1 ELSE 0 END AS recent, "
            "CASE WHEN tr.retired_at IS NULL THEN 0 ELSE 1 END AS retired, "
            "COUNT(*) "
            "FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name"
        )
        params = [model.format_iso(recent_cutoff)]  # type: List[Any]
        where = []  # type: List[str]
        if environment is not None:
            where.append("lr.environment = ?")
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            where.append(envs_clause)
            params.extend(envs_params)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " GROUP BY lr.environment, lr.result, lr.prev_result, recent, "
            "retired ORDER BY lr.environment, lr.result"
        )
        return [
            RollupCount(
                environment=row[0],
                result=Result(row[1]),
                prev_result=None if row[2] is None else Result(row[2]),
                recent=bool(row[3]),
                retired=bool(row[4]),
                count=int(row[5]),
            )
            for row in self._conn().execute(sql, params).fetchall()
        ]

    def assigned_open_count(
        self,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> int:
        """Count tests that have an assignee and are FAIL or UNEXPECTED_PASS."""
        sql = (
            "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN + " WHERE "
            + _QUEUE_PREDICATES["assigned"] + " AND " + self._NOT_RETIRED
        )
        params = []  # type: List[Any]
        if environment is not None:
            sql += " AND lr.environment = ?"
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            sql += " AND " + envs_clause
            params.extend(envs_params)
        return int(self._conn().execute(sql, params).fetchone()[0])

    def activity_buckets(
        self, since: datetime.datetime
    ) -> List[Tuple[str, datetime.datetime, int]]:
        """(environment, hour, runs) for every active hour since *since*.

        Feeds :func:`analytics.find_passes`. Bucketed to the hour and
        grouped by environment on purpose:

        - hour resolution is all this question needs, so a fortnight is
          a few hundred rows rather than a hundred thousand runs;
        - per environment because environments run SEQUENTIALLY. The
          first reports in the small hours and the last hours later, so
          on one shared timeline whichever ran first looks stale for the
          rest of the morning.

        The run count is what separates a real pass of the suite from an
        ad-hoc re-run triggered after a fix. Both are blocks of
        activity; only one means "everything has reported".

        Read from ``activity_hours`` (migration 6), not from ``runs``:
        the GROUP BY this used to run was answered with a full scan of
        the runs UNIQUE index — the whole history, every request, on
        every endpoint that needs the staleness cutoff. See the
        migration 6 comment for the measurements. *since* is quantised
        DOWN to its hour, which can only widen the window by part of one
        hour at the 14-day edge — and :func:`analytics.recent_cutoff`
        clamps to the floor anyway, so an extra old bucket cannot move
        the answer.
        """
        rows = self._conn().execute(
            "SELECT environment, hour, SUM(count) FROM activity_hours "
            "WHERE hour >= ? GROUP BY environment, hour "
            "ORDER BY environment, hour",
            (model.format_iso(since)[:13],),
        ).fetchall()
        return [
            (row[0], model.parse_iso(row[1] + ":00:00.000000"), int(row[2]))
            for row in rows
        ]

    def script_activity(
        self,
        environment: str,
        since: datetime.datetime,
        until: datetime.datetime,
    ) -> List[ScriptHourBucket]:
        """Every ``script_hours`` bucket for one environment's window.

        Feeds :func:`analytics.group_script_executions`, which turns
        these into the Timeline's per-script execution rows. Ordered by
        (script, hour) — the order the grouping walks in.

        Read from ``script_hours`` (migration 7), never from ``runs``:
        the window is one block of hours for one environment, and the
        PRIMARY KEY leads (environment, hour), so this is a pure index
        range over a table that grows like scripts x active hours. Both
        edges are quantised to their hour, which can only WIDEN the
        window — the caller trims executions to the exact block, and an
        extra boundary bucket cannot invent one (block edges are, by
        construction, more than the execution gap apart).
        """
        rows = self._conn().execute(
            "SELECT script, hour, result, count, first_start, last_end "
            "FROM script_hours WHERE environment = ? "
            "AND hour >= ? AND hour <= ? ORDER BY script, hour",
            (
                environment,
                model.format_iso(since)[:13],
                model.format_iso(until)[:13],
            ),
        ).fetchall()
        return [
            ScriptHourBucket(
                script=row[0],
                hour=row[1],
                result=Result(row[2]),
                count=int(row[3]),
                first_start=model.parse_iso(row[4]),
                last_end=model.parse_iso(row[5]),
            )
            for row in rows
        ]

    def script_test_counts(self, environment: str) -> Dict[str, int]:
        """How many tests each of an environment's scripts has, INFERRED.

        The "of 45" in the Timeline's "ran 41 of 45 known tests" — what
        makes a partial run of a script visible as one. One row per
        test via ``latest_runs`` (an index range on the PRIMARY KEY,
        which leads with ``environment``), excluding retired tests for
        the usual reason: they are not in the suite, so a run that
        skips them has not missed anything.

        A high-water mark, like every count inferred from
        ``latest_runs`` — a test that quietly stopped running still
        counts until someone retires it. The Timeline words it "known
        tests" rather than "expected" for exactly that reason.
        """
        rows = self._conn().execute(
            "SELECT lr.script, COUNT(*) FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script "
            " AND tr.test_name = lr.test_name "
            "WHERE lr.environment = ? AND " + self._NOT_RETIRED +
            " GROUP BY lr.script",
            (environment,),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def test_counts_by_environment(self) -> Dict[str, int]:
        """How many tests each environment currently has, INFERRED.

        The denominator for "did that block of activity actually cover
        this environment, or was it a handful of re-runs after a fix".

        Retired tests are excluded, for the reason they are excluded from
        every other estate view: they are not in the suite, so a pass
        that does not run them has not missed anything. Counting them
        inflates the denominator, which makes real passes fail the
        coverage test — and a failed coverage test is SILENT, dropping
        the cutoff back to the wall clock with nothing to see.

        Even so this is a high-water mark: a test that quietly stopped
        being run and was never retired stays here forever. That is what
        :meth:`declared_test_counts` exists to override.
        """
        rows = self._conn().execute(
            "SELECT lr.environment, COUNT(*) FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script "
            " AND tr.test_name = lr.test_name "
            "WHERE " + self._NOT_RETIRED + " GROUP BY lr.environment"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # ------------------------------------------------------------------
    # Declared environment expectations (migration 5)
    # ------------------------------------------------------------------

    def declared_test_counts(self) -> Dict[str, int]:
        """Declared expected test count per environment.

        Only environments somebody has declared appear. Combined with
        the inferred counts by :func:`analytics.effective_test_counts`.
        """
        rows = self._conn().execute(
            "SELECT environment, expected_tests FROM "
            "environment_expectations"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def list_environment_expectations(
        self,
    ) -> List[EnvironmentExpectation]:
        """Every declaration, ordered by environment."""
        rows = self._conn().execute(
            "SELECT environment, expected_tests, updated_at, updated_by "
            "FROM environment_expectations ORDER BY environment"
        ).fetchall()
        return [
            EnvironmentExpectation(
                environment=row[0],
                expected_tests=int(row[1]),
                updated_at=model.parse_iso(row[2]),
                updated_by=row[3],
            )
            for row in rows
        ]

    def set_environment_expectation(
        self,
        environment: str,
        expected_tests: int,
        updated_by: str,
        updated_at: datetime.datetime,
    ) -> EnvironmentExpectation:
        """Declare (or redeclare) an environment's expected test count.

        UPDATE-then-INSERT rather than ``INSERT OR REPLACE``: the two are
        indistinguishable here, but OR REPLACE deletes and re-inserts,
        which is not what MariaDB's ``ON DUPLICATE KEY UPDATE`` does, and
        ``tests/test_sql_portability.py`` counts every use of it against
        a committed expectation for exactly that reason.
        """
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(updated_by, updated_at)
            stamp = model.format_iso(updated_at)
            cursor = conn.execute(
                "UPDATE environment_expectations SET expected_tests = ?, "
                "updated_at = ?, updated_by = ? WHERE environment = ?",
                (expected_tests, stamp, updated_by, environment),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO environment_expectations "
                    "(environment, expected_tests, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?)",
                    (environment, expected_tests, stamp, updated_by),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return EnvironmentExpectation(
            environment=environment,
            expected_tests=expected_tests,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def clear_environment_expectation(self, environment: str) -> bool:
        """Drop a declaration, returning to inference. True if one went."""
        cursor = self._conn().execute(
            "DELETE FROM environment_expectations WHERE environment = ?",
            (environment,),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Declared environment -> product mapping (migration 8, WP-20)
    # ------------------------------------------------------------------

    def environment_products_map(self) -> Dict[str, str]:
        """Every declared environment -> product mapping.

        Environments absent here belong to the implicit product ``""``
        — callers combine this with :meth:`known_environments` when they
        need every environment accounted for, the same shape as
        :meth:`declared_test_counts`.
        """
        rows = self._conn().execute(
            "SELECT environment, product FROM environment_products"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def list_environment_products(self) -> List[EnvironmentProduct]:
        """Every declaration, ordered by environment."""
        rows = self._conn().execute(
            "SELECT environment, product, updated_at, updated_by "
            "FROM environment_products ORDER BY environment"
        ).fetchall()
        return [
            EnvironmentProduct(
                environment=row[0],
                product=row[1],
                updated_at=model.parse_iso(row[2]),
                updated_by=row[3],
            )
            for row in rows
        ]

    def distinct_products(self) -> List[str]:
        """Every distinct DECLARED product, sorted.

        The implicit product ``""`` (environments nobody has mapped) is
        never a member — it is "no product declared", not a product of
        its own, and the frontend switcher's ``>= 2`` test reads this
        list directly (docs/STREAMS_PLAN.md §2.3).
        """
        rows = self._conn().execute(
            "SELECT DISTINCT product FROM environment_products "
            "ORDER BY product"
        ).fetchall()
        return [row[0] for row in rows]

    def environments_for_product(self, product: str) -> List[str]:
        """Environments declared as belonging to *product*, sorted.

        ``product == ""`` is the implicit grouping: every KNOWN
        environment (see :meth:`known_environments`) that nobody has
        mapped to a product. A named product with no environments
        mapped to it (a typo, or one that has since been remapped) comes
        back as an empty list — the API layer turns that into "empty
        result", never a 404, because a product exists by having
        environments (docs/STREAMS_PLAN.md §2.6).
        """
        if product == "":
            mapped = set(self.environment_products_map())
            return [
                environment for environment in self.known_environments()
                if environment not in mapped
            ]
        rows = self._conn().execute(
            "SELECT environment FROM environment_products "
            "WHERE product = ? ORDER BY environment",
            (product,),
        ).fetchall()
        return [row[0] for row in rows]

    def set_environment_product(
        self,
        environment: str,
        product: str,
        updated_by: str,
        updated_at: datetime.datetime,
    ) -> EnvironmentProduct:
        """Declare (or redeclare) which product an environment belongs to.

        UPDATE-then-INSERT, the same shape as
        :meth:`set_environment_expectation` and for the same reason:
        ``INSERT OR REPLACE`` deletes and re-inserts, which
        ``tests/test_sql_portability.py`` counts against a committed
        expectation of exactly two acceptable sites, neither of them
        this one.
        """
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(updated_by, updated_at)
            stamp = model.format_iso(updated_at)
            cursor = conn.execute(
                "UPDATE environment_products SET product = ?, "
                "updated_at = ?, updated_by = ? WHERE environment = ?",
                (product, stamp, updated_by, environment),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO environment_products "
                    "(environment, product, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?)",
                    (environment, product, stamp, updated_by),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return EnvironmentProduct(
            environment=environment,
            product=product,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def clear_environment_product(self, environment: str) -> bool:
        """Drop a mapping, returning to the implicit product "". True if
        one went."""
        cursor = self._conn().execute(
            "DELETE FROM environment_products WHERE environment = ?",
            (environment,),
        )
        return cursor.rowcount > 0

    def known_environments(self) -> List[str]:
        """Every environment that has run a test or carries a declaration.

        Both halves matter: an environment declared before its first
        import must still be listed (otherwise it cannot be corrected),
        and an environment nobody has declared must appear so that it
        can be.

        Cost: a scan of the ``latest_runs`` PRIMARY KEY, which begins
        with ``environment``, so it reads index pages and never the
        table — proportional to the number of TESTS (~12k, 2 ms
        measured), never to the number of runs. The same shape as
        :meth:`environments`, which ``/api/summary`` already calls on
        every load. Pinned by a query-plan test, per the plan's §0.4.

        There is no cheaper form available: SQLite here cannot skip-scan
        a composite index to its distinct leading values, and an index
        on ``environment`` alone would be maintained on every one of the
        12,008 rows a nightly import touches — see migration 4 for what
        that trade costs. Use :meth:`environment_exists` when the
        question is about one name.
        """
        rows = self._conn().execute(
            "SELECT environment FROM latest_runs "
            "UNION SELECT environment FROM environment_expectations "
            "UNION SELECT environment FROM environment_products "
            "ORDER BY 1"
        ).fetchall()
        return [row[0] for row in rows]

    def environment_exists(self, environment: str) -> bool:
        """True if *environment* has run a test or carries a declaration.

        Three index seeks, so validating one name costs nothing that
        grows with the estate — unlike asking for the whole list and
        searching it.
        """
        row = self._conn().execute(
            "SELECT 1 FROM latest_runs WHERE environment = ? LIMIT 1",
            (environment,),
        ).fetchone()
        if row is not None:
            return True
        row = self._conn().execute(
            "SELECT 1 FROM environment_expectations WHERE environment = ?",
            (environment,),
        ).fetchone()
        if row is not None:
            return True
        row = self._conn().execute(
            "SELECT 1 FROM environment_products WHERE environment = ?",
            (environment,),
        ).fetchone()
        return row is not None

    def latest_run_time_by_environment(
        self,
    ) -> Dict[str, datetime.datetime]:
        """When each environment last reported anything.

        Read from ``latest_runs`` — one row per TEST — rather than from
        ``runs``, so it costs a grouped read of ~12k rows and never
        touches the millions of historical runs. ``latest_runs.start_time``
        is by definition each test's newest run, so the maximum within
        an environment is that environment's newest run.

        Retired tests are included deliberately: this answers "when did
        we last hear from this environment", which is a question about
        the feeder, not about the suite's contents.
        """
        rows = self._conn().execute(
            "SELECT environment, MAX(start_time) FROM latest_runs "
            "GROUP BY environment"
        ).fetchall()
        return {
            row[0]: model.parse_iso(row[1])
            for row in rows if row[1] is not None
        }

    def latest_run_time(self) -> Optional[datetime.datetime]:
        """Start time of the newest run on record, or None if empty.

        The estate's own clock. Reported alongside the summary so a
        stalled feeder is visible as a stalled feeder, rather than as
        every test in the estate quietly going stale.
        """
        row = self._conn().execute(
            "SELECT MAX(start_time) FROM runs").fetchone()
        if row is None or row[0] is None:
            return None
        return model.parse_iso(row[0])

    def duration_rollup(
        self,
        group_by: str,
        recent_cutoff: Optional[datetime.datetime],
        environment: Optional[str] = None,
        script: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> DurationRollup:
        """Where the suite's time went, grouped one level at a time.

        *group_by* is ``"environment"``, ``"script"`` or ``"test_name"``
        — validated against :data:`_DURATION_GROUPS` rather than
        interpolated, because a GROUP BY column cannot be a parameter.

        Aggregates ``latest_runs.duration_seconds``: **the newest run of
        each test**, not a historical window. So it answers "where did
        the last run of the suite spend its time", which is cheap (one
        row per test, ~12k) and is what the page has to say it means. A
        windowed version needs a real aggregate table and is a different
        piece of work.

        Two exclusions, both deliberate:

        - **Retired tests**, consistent with every other estate view:
          they are not in the suite.
        - **Tests that have not reported since** *recent_cutoff*. Their
          duration is still on file, and including it would claim time
          was spent last night that was not. They are counted into
          ``excluded_tests`` so the page can say how many rather than
          quietly dropping them.

        Pass ``recent_cutoff=None`` to include them anyway. That is not
        the default and should not become it — but an all-or-nothing
        cutoff turns the whole page into "0.0s across 0 tests" whenever
        the suite has not run for a day and a half, which is a long
        weekend or one bad night. Refusing to show anything is not more
        honest than showing it clearly labelled.

        *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`.
        """
        column = _DURATION_GROUPS.get(group_by)
        if column is None:
            raise ValueError(
                "group_by must be one of {0}, got {1!r}".format(
                    sorted(_DURATION_GROUPS), group_by
                )
            )
        conn = self._conn()
        where = [
            "tr.environment IS NULL",
        ]
        params = []  # type: List[Any]
        if environment is not None:
            where.append("lr.environment = ?")
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            where.append(envs_clause)
            params.extend(envs_params)
        if script is not None:
            where.append("lr.script = ?")
            params.append(script)
        joined = (
            "FROM latest_runs lr "
            "LEFT JOIN test_retirements tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script "
            " AND tr.test_name = lr.test_name "
            "WHERE " + " AND ".join(where)
        )
        recency = "" if recent_cutoff is None else "AND lr.start_time >= ? "
        recency_params = (
            () if recent_cutoff is None
            else (model.format_iso(recent_cutoff),)
        )

        rows = conn.execute(
            "SELECT {0}, SUM(lr.duration_seconds), COUNT(*) {1} "
            "{2}GROUP BY {0} "
            "ORDER BY SUM(lr.duration_seconds) DESC, {0}".format(
                column, joined, recency),
            tuple(params) + recency_params,
        ).fetchall()
        slices = [
            DurationSlice(
                key=row[0],
                total_seconds=float(row[1] or 0.0),
                test_count=int(row[2]),
            )
            for row in rows
        ]
        if recent_cutoff is None:
            excluded = 0
        else:
            excluded = conn.execute(
                "SELECT COUNT(*) {0} AND lr.start_time < ?".format(joined),
                tuple(params) + recency_params,
            ).fetchone()[0]
        return DurationRollup(
            slices=slices,
            total_seconds=sum(s.total_seconds for s in slices),
            test_count=sum(s.test_count for s in slices),
            excluded_tests=int(excluded),
        )

    def top_failing_scripts(
        self,
        environment: Optional[str] = None,
        limit: int = 10,
        environments: Optional[Sequence[str]] = None,
    ) -> List[ScriptFailures]:
        """Scripts with the most currently-failing tests, worst first.

        *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`.
        """
        sql = (
            "SELECT lr.environment, lr.script, COUNT(*) AS failing "
            "FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE lr.result = ? AND " + self._NOT_RETIRED
        )
        params = [Result.FAIL.value]  # type: List[Any]
        if environment is not None:
            sql += " AND lr.environment = ?"
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            sql += " AND " + envs_clause
            params.extend(envs_params)
        sql += (
            " GROUP BY lr.environment, lr.script "
            "ORDER BY failing DESC, lr.environment, lr.script LIMIT ?"
        )
        params.append(limit)
        return [
            ScriptFailures(
                environment=row[0], script=row[1], failing=int(row[2])
            )
            for row in self._conn().execute(sql, params).fetchall()
        ]

    @staticmethod
    def _queue_clause(
        kind: str,
        environment: Optional[str],
        assignee: Optional[str],
        stale_before: Optional[datetime.datetime] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> Tuple[str, List[Any]]:
        """Build the WHERE clause for one triage queue.

        Retired tests are always excluded: approving a test as no longer
        in the suite is precisely a statement that it should stop
        appearing in the work queues — and the ``not_run`` queue, where
        that approval is given, is exactly where they would otherwise
        pile up. *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`.
        """
        try:
            predicate = _QUEUE_PREDICATES[kind]
        except KeyError:
            raise ValueError("unknown queue kind: {!r}".format(kind))
        sql = " WHERE {} AND {}".format(predicate, Storage._NOT_RETIRED)
        params = []  # type: List[Any]
        if kind in _STALE_QUEUES:
            if stale_before is None:
                raise ValueError(
                    "queue {!r} needs stale_before".format(kind)
                )
            params.append(model.format_iso(stale_before))
        if environment is not None:
            sql += " AND lr.environment = ?"
            params.append(environment)
        envs_clause, envs_params = Storage._environments_clause(
            environments
        )
        if envs_clause is not None:
            sql += " AND " + envs_clause
            params.extend(envs_params)
        if assignee is not None:
            sql += " AND ca.assignee = ?"
            params.append(assignee)
        return sql, params

    def status_queue(
        self,
        kind: str,
        environment: Optional[str] = None,
        limit: Optional[int] = None,
        assignee: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        with_latest_comment: bool = False,
        environments: Optional[Sequence[str]] = None,
    ) -> List[TestStatusRow]:
        """Return one triage queue (see :data:`QUEUE_KINDS`), newest info first.

        *kind* selects the membership predicate; *assignee* narrows the
        queue to one person's tests (the "my actions" view, which must be
        filtered in SQL — filtering a capped queue client-side would hide
        a user's own tests behind other people's). Ordered by test
        identity; at most *limit* rows. :meth:`status_queue_count` gives
        the exact total. *environments* is the WP-20 ``product=`` filter.

        *with_latest_comment* adds each test's newest comment — what
        somebody already worked out about this failure, which is the
        first thing a person triaging it needs to know. One index seek
        per returned row.
        """
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before, environments,
        )
        columns = self._STATUS_COLUMNS
        if with_latest_comment:
            columns += ", " + _LATEST_COMMENT_COLUMNS
        sql = "SELECT {}, lr.prev_result {}{}".format(
            columns, self._LATEST_JOIN, where
        )
        sql += self._order_by("environment", False)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        # A TestStatusRow is a TestSummaryRow plus prev_result, in that
        # field order, so the shared converter builds the common part.
        # prev_result is selected last, after any comment columns.
        prev_index = len(_STATUS_COLUMN_NAMES) + (
            _LATEST_COMMENT_COUNT if with_latest_comment else 0
        )
        return [
            TestStatusRow(
                *_summary_row_from(
                    row,
                    _latest_comment_from(row) if with_latest_comment
                    else None,
                ),
                prev_result=(
                    None if row[prev_index] is None
                    else Result(row[prev_index])
                )
            )
            for row in self._conn().execute(sql, params).fetchall()
        ]

    def status_queue_count(
        self,
        kind: str,
        environment: Optional[str] = None,
        assignee: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> int:
        """Exact size of a triage queue, ignoring any display cap."""
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before, environments,
        )
        sql = "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN + where
        return int(self._conn().execute(sql, params).fetchone()[0])

    def recent_results(
        self,
        triples: Sequence[Tuple[str, str, str]],
        since: datetime.datetime,
        per_test_limit: int = 20,
    ) -> Dict[Tuple[str, str, str], List[Result]]:
        """The last few results of each of a PAGE of tests.

        Returns ``{triple: [oldest, ..., newest]}``, at most
        *per_test_limit* entries each, for runs at or after *since*.

        The point of this method is what it refuses to be: a query per
        row. A page of a hundred tests asking "how has this one been
        behaving" one at a time is a hundred round trips, which is the
        shape of bug ``tests/test_frontend_calls.py`` exists to catch on
        the frontend and which is no better here.

        Triples are batched into groups of :data:`_RECENT_CHUNK`, so the
        query count is proportional to the PAGE SIZE divided by the
        chunk — never to the page size itself, and never to the size of
        the estate. Batching rather than one giant query is not a
        style choice: SQLite caps a statement at 999 bound parameters
        and each triple costs three.
        """
        found = {}  # type: Dict[Tuple[str, str, str], List[Result]]
        if not triples:
            return found
        cutoff = model.format_iso(since)
        conn = self._conn()
        unique = list(dict.fromkeys(tuple(t) for t in triples))
        for start in range(0, len(unique), _RECENT_CHUNK):
            chunk = unique[start:start + _RECENT_CHUNK]
            clause = " OR ".join(
                "(environment = ? AND script = ? AND test_name = ?)"
                for _ in chunk
            )
            params = []  # type: List[Any]
            for triple in chunk:
                params.extend(triple)
            params.append(cutoff)
            rows = conn.execute(
                "SELECT environment, script, test_name, result, start_time "
                "FROM runs WHERE ({0}) AND start_time >= ? "
                "ORDER BY environment, script, test_name, start_time".format(
                    clause),
                tuple(params),
            ).fetchall()
            for row in rows:
                key = (row[0], row[1], row[2])
                series = found.setdefault(key, [])
                series.append(Result(row[3]))
        # Keep the newest `per_test_limit`, still oldest-first.
        limit = max(1, int(per_test_limit))
        return {
            key: series[-limit:] for key, series in found.items()
        }

    def failure_streak_bounds(
        self,
        environment: str,
        script: str,
        test_name: str,
        latest_start: datetime.datetime,
    ) -> FailureStreak:
        """When the test's current FAIL streak began, and its last pass before.

        *latest_start* is the start time of the test's newest run, which
        the caller has already established is a FAIL. Three index seeks,
        no history walk: find the newest non-FAIL run below it (the run
        that bounds the streak), take the oldest run above that bound
        (every run in between is a FAIL by construction), then find the
        newest PASS older than the streak. ``FAILED_AS_EXPECTED`` and
        ``UNEXPECTED_PASS`` bound a streak but are not passes, so the
        last-pass seek is separate.
        """
        conn = self._conn()
        triple = (environment, script, test_name)
        start = model.format_iso(latest_start)

        bound_row = conn.execute(
            "SELECT start_time FROM runs WHERE environment = ? "
            "AND script = ? AND test_name = ? AND start_time < ? "
            "AND result <> ? ORDER BY start_time DESC LIMIT 1",
            triple + (start, Result.FAIL.value),
        ).fetchone()

        if bound_row is None:
            since_row = conn.execute(
                "SELECT MIN(start_time) FROM runs WHERE environment = ? "
                "AND script = ? AND test_name = ? AND start_time <= ?",
                triple + (start,),
            ).fetchone()
        else:
            since_row = conn.execute(
                "SELECT MIN(start_time) FROM runs WHERE environment = ? "
                "AND script = ? AND test_name = ? AND start_time > ? "
                "AND start_time <= ?",
                triple + (bound_row[0], start),
            ).fetchone()
        if since_row is None or since_row[0] is None:
            return FailureStreak(failing_since=None, last_pass_before=None)
        failing_since = since_row[0]

        pass_row = conn.execute(
            "SELECT start_time FROM runs WHERE environment = ? "
            "AND script = ? AND test_name = ? AND start_time < ? "
            "AND result = ? ORDER BY start_time DESC LIMIT 1",
            triple + (failing_since, Result.PASS.value),
        ).fetchone()
        return FailureStreak(
            failing_since=model.parse_iso(failing_since),
            last_pass_before=(
                None if pass_row is None else model.parse_iso(pass_row[0])
            ),
        )

    def daily_result_counts(
        self,
        since: datetime.datetime,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> List[DailyResultCount]:
        """Run counts grouped by UTC calendar day and result.

        Read from ``activity_hours`` (migration 6): summing a window of
        hour-resolution cells gives exactly the per-day counts the old
        ~170,000-entry index scan produced, from a few hundred rows.
        The memo layer predates that table (it was worth ~345ms a
        request then); it stays because it still spares a query and a
        parse per request and its invalidation is already correct.

        *since* is quantised DOWN to its hour. Every caller passes a
        midnight, for which the quantisation changes nothing; a caller
        passing mid-hour would see the boundary hour counted whole.
        *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`; it is part of the cache key (as a
        sorted tuple, so the same set in a different order is still one
        cache entry) precisely so a scoped and an unscoped request for
        the same window cannot serve each other's answer.

        The cache is cleared by every write this process makes (see
        :meth:`upsert_runs` and :meth:`prune_runs_before`) — but NOT by
        a re-import that changed nothing, which is what the site feeder
        sends every 10 minutes — and entries expire after
        :data:`_TREND_CACHE_TTL_SECONDS` so a write made by a DIFFERENT
        process (an offline prune while the server is up) cannot pin a
        stale trend for long.

        Days with no runs simply do not appear — the caller zero-fills
        its display range. Ordered by day, then result value.
        """
        sql = (
            "SELECT SUBSTR(hour, 1, 10) AS day, result, SUM(count) "
            "FROM activity_hours WHERE hour >= ?"
        )
        params = [model.format_iso(since)[:13]]  # type: List[Any]
        if environment is not None:
            sql += " AND environment = ?"
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(
            environments, column="environment"
        )
        if envs_clause is not None:
            sql += " AND " + envs_clause
            params.extend(envs_params)
        sql += " GROUP BY day, result ORDER BY day, result"

        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = (params[0], environment, envs_key)
        cached = self._cached_trend(key)
        if cached is not None:
            return cached

        rows = self._conn().execute(sql, params).fetchall()
        counts = [
            DailyResultCount(
                day=datetime.datetime.strptime(row[0], "%Y-%m-%d").date(),
                result=Result(row[1]),
                count=int(row[2]),
            )
            for row in rows
        ]
        self._store_trend(key, counts)
        return counts

    def _cached_trend(
        self, key: Tuple[str, Optional[str], Optional[Tuple[str, ...]]]
    ) -> Optional[List[DailyResultCount]]:
        """Return a memoized trend for *key*, or None if absent/expired."""
        now = time.time()
        with self._trend_lock:
            entry = self._trend_cache.get(key)
            if entry is None:
                return None
            stored_at, counts = entry
            if now - stored_at > _TREND_CACHE_TTL_SECONDS:
                del self._trend_cache[key]
                return None
            return counts

    def _store_trend(
        self,
        key: Tuple[str, Optional[str], Optional[Tuple[str, ...]]],
        counts: List[DailyResultCount],
    ) -> None:
        """Memoize a computed trend, bounding the cache size."""
        with self._trend_lock:
            if len(self._trend_cache) >= _TREND_CACHE_MAX_ENTRIES:
                # Arbitrary `days` values would otherwise grow this
                # without limit; the working set is one or two windows.
                self._trend_cache.clear()
            self._trend_cache[key] = (time.time(), counts)

    def _invalidate_trend_cache(self) -> None:
        """Drop memoized trends: the runs they were computed from changed."""
        with self._trend_lock:
            self._trend_cache.clear()

    def test_exists(
        self, environment: str, script: str, test_name: str
    ) -> bool:
        """Return True if at least one run exists for the test triple."""
        row = self._conn().execute(
            "SELECT 1 FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? LIMIT 1",
            (environment, script, test_name),
        ).fetchone()
        return row is not None

    def latest_run(
        self, environment: str, script: str, test_name: str
    ) -> Optional[StoredRun]:
        """Return the newest run for the triple (``output=None``), or None."""
        row = self._conn().execute(
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? ORDER BY start_time DESC LIMIT 1".format(
                _RUN_COLUMNS
            ),
            (environment, script, test_name),
        ).fetchone()
        if row is None:
            return None
        return _run_from_row(row, None)

    def run_history(
        self,
        environment: str,
        script: str,
        test_name: str,
        limit: int = 50,
        before: Optional[datetime.datetime] = None,
    ) -> List[StoredRun]:
        """Return runs for the triple, newest first, ``output=None``.

        When *before* is given only runs with ``start_time`` strictly
        earlier than it are returned (pagination cursor). At most *limit*
        runs are returned.
        """
        sql = (
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ?".format(_RUN_COLUMNS)
        )
        params = [environment, script, test_name]  # type: List[Any]
        if before is not None:
            sql += " AND start_time < ?"
            params.append(model.format_iso(before))
        sql += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [_run_from_row(row, None) for row in rows]

    def runs_since(
        self,
        environment: str,
        script: str,
        test_name: str,
        since: datetime.datetime,
        limit: int,
    ) -> List[StoredRun]:
        """Return runs with ``start_time >= since``, newest first.

        Capped at *limit* rows; ``output=None``. This is the analytics
        window query.
        """
        rows = self._conn().execute(
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND start_time >= ? "
            "ORDER BY start_time DESC LIMIT ?".format(_RUN_COLUMNS),
            (
                environment,
                script,
                test_name,
                model.format_iso(since),
                limit,
            ),
        ).fetchall()
        return [_run_from_row(row, None) for row in rows]

    def script_runs(
        self,
        environment: str,
        script: str,
        since: datetime.datetime,
        limit: int,
        until: Optional[datetime.datetime] = None,
    ) -> List[StoredRun]:
        """Every run of one script since *since*, OLDEST FIRST, no ``output``.

        Feeds :func:`testboard.analytics.group_executions`, which needs
        the runs in time order to work out where one execution of the
        suite ends and the next begins. The UNIQUE index is keyed
        ``(environment, script, test_name, start_time)``, so filtering on
        the first two columns is an index range scan.

        Bounded by *limit* — a busy script over a long window is a lot of
        runs, and the caller only ever charts the recent ones. The limit
        takes the OLDEST rows in the window, so the executions returned
        are contiguous rather than a truncated middle.

        *until* (inclusive, on ``start_time``) closes the window at the
        top: the Timeline's row expansion asks for exactly one script
        execution, and without a ceiling it would drag in every run
        from the block to now.
        """
        sql = (
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND start_time >= ?".format(_RUN_COLUMNS)
        )
        params = [
            environment, script, model.format_iso(since),
        ]  # type: List[Any]
        if until is not None:
            sql += " AND start_time <= ?"
            params.append(model.format_iso(until))
        sql += " ORDER BY start_time LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [_run_from_row(row, None) for row in rows]

    def script_exists(self, environment: str, script: str) -> bool:
        """Return True if any run exists for the (environment, script) pair."""
        row = self._conn().execute(
            "SELECT 1 FROM runs WHERE environment = ? AND script = ? LIMIT 1",
            (environment, script),
        ).fetchone()
        return row is not None

    def get_run(self, run_id: int) -> Optional[StoredRun]:
        """Return a single run by id INCLUDING ``output``, or None.

        The only query in the project that reads ``run_outputs`` — one
        primary-key join, for one run, and the only place output is
        decompressed.
        """
        row = self._conn().execute(
            "SELECT {}, o.output FROM runs AS r "
            "LEFT JOIN run_outputs AS o ON o.run_id = r.id "
            "WHERE r.id = ?".format(
                ", ".join("r." + col for col in _RUN_COLUMN_NAMES)
            ),
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return _run_from_row(row, _decompress_output(row[9]))

    # ------------------------------------------------------------------
    # Retirement ("this test is no longer in the suite")
    # ------------------------------------------------------------------

    def set_retired(
        self,
        environment: str,
        script: str,
        test_name: str,
        retired: bool,
        username: str,
        comment: str,
        now: datetime.datetime,
    ) -> Comment:
        """Retire a test, or put it back, recording who said so and why.

        A test that stops being reported shows up forever under "not
        run". Retiring it is a human approving that absence, so it takes
        a *username* and a *comment* — and both land in the test's normal
        comment thread, where the next person will look. Returns the
        comment that was recorded.

        Retirement hides the test from the estate views (counts, queues,
        the default test list). It does not touch its history.
        """
        conn = self._conn()
        triple = (environment, script, test_name)
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(username, now)
            if retired:
                conn.execute(
                    "INSERT OR REPLACE INTO test_retirements "
                    "(environment, script, test_name, retired_at, "
                    "retired_by) VALUES (?, ?, ?, ?, ?)",
                    triple + (model.format_iso(now), username),
                )
            else:
                conn.execute(
                    "DELETE FROM test_retirements WHERE environment = ? "
                    "AND script = ? AND test_name = ?",
                    triple,
                )
            comment_id = self._insert_comment(
                conn, triple, username, comment, now
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return Comment(
            comment_id=comment_id,
            environment=environment,
            script=script,
            test_name=test_name,
            author=username,
            created_at=now,
            text=comment,
        )

    def is_retired(
        self, environment: str, script: str, test_name: str
    ) -> bool:
        """True when the test has been approved as no longer in the suite."""
        row = self._conn().execute(
            "SELECT 1 FROM test_retirements WHERE environment = ? "
            "AND script = ? AND test_name = ?",
            (environment, script, test_name),
        ).fetchone()
        return row is not None

    @staticmethod
    def _unretire_on_new_run(
        conn: sqlite3.Connection, rec: RunRecord, start: str
    ) -> None:
        """Clear a retirement because the test just reported a run.

        A test approved as gone that starts running again is back, and
        leaving it hidden would be the worse failure — silently MISSING
        data is worse than an unexpected row. The comment thread records
        why the approval lapsed, so the story reads in order.
        """
        triple = (rec.environment, rec.script, rec.test_name)
        row = conn.execute(
            "SELECT retired_by FROM test_retirements WHERE environment = ? "
            "AND script = ? AND test_name = ?",
            triple,
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "DELETE FROM test_retirements WHERE environment = ? "
            "AND script = ? AND test_name = ?",
            triple,
        )
        Storage._insert_comment(
            conn, triple, row[0],
            "Automatically un-retired: this test reported a run at {0}, "
            "so it is in the suite again.".format(start),
            model.parse_iso(start),
        )

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def count_runs_before(self, cutoff: datetime.datetime) -> int:
        """How many runs :meth:`prune_runs_before` would delete (dry run)."""
        row = self._conn().execute(
            "SELECT COUNT(*) FROM runs WHERE start_time < ? "
            "AND id NOT IN (SELECT run_id FROM latest_runs)",
            (model.format_iso(cutoff),),
        ).fetchone()
        return int(row[0])

    def prune_runs_before(self, cutoff: datetime.datetime) -> int:
        """Delete runs older than *cutoff*; return how many were removed.

        A test's newest run is NEVER deleted, however old it is: it is
        the row the dashboard shows, and dropping it would make a test
        that stopped running silently disappear rather than show up as
        "not run". Everything else older than the cutoff goes, outputs
        first.

        ``prev_result`` is then re-derived for every test, because a
        pruned run may have been the one a ``latest_runs`` row was
        pointing back at. One statement, one seek per test.

        Space is not returned to the file system until someone runs
        ``VACUUM`` (see ``tools/prune_runs.py --vacuum``); until then
        SQLite reuses the freed pages for new runs, which for a
        steady-state nightly import is usually what you want.
        """
        conn = self._conn()
        limit = model.format_iso(cutoff)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM run_outputs WHERE run_id IN ("
                "  SELECT id FROM runs WHERE start_time < ? "
                "  AND id NOT IN (SELECT run_id FROM latest_runs))",
                (limit,),
            )
            cursor = conn.execute(
                "DELETE FROM runs WHERE start_time < ? "
                "AND id NOT IN (SELECT run_id FROM latest_runs)",
                (limit,),
            )
            deleted = int(cursor.rowcount)
            conn.execute(
                "UPDATE latest_runs SET prev_result = ("
                "  SELECT p.result FROM runs AS p "
                "  WHERE p.environment = latest_runs.environment "
                "    AND p.script = latest_runs.script "
                "    AND p.test_name = latest_runs.test_name "
                "    AND p.start_time < latest_runs.start_time "
                "  ORDER BY p.start_time DESC LIMIT 1)"
            )
            # A prune breaks the "activity_hours == GROUP BY over runs"
            # invariant for every touched hour, and the surviving
            # latest-run rows make the arithmetic of a partial decrement
            # fiddly. Rebuilding is one aggregate pass over what is LEFT
            # — this is an offline maintenance path that just deleted
            # most of the table, not a request handler.
            _rebuild_activity_hours(conn)
            _rebuild_script_hours(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self._invalidate_trend_cache()
        return deleted

    def count_environment_rows(self, environment: str) -> Dict[str, int]:
        """Rows :meth:`delete_environment` would delete, per table.

        The dry run for a delete that cannot be undone. Counted with the
        same predicates the delete uses, so the report and the action
        cannot describe different things.
        """
        counts = {}  # type: Dict[str, int]
        conn = self._conn()
        for table in _ENVIRONMENT_TABLES:
            counts[table] = int(conn.execute(
                "SELECT COUNT(*) FROM {0} WHERE environment = ?".format(
                    table),
                (environment,),
            ).fetchone()[0])
        counts["run_outputs"] = int(conn.execute(
            "SELECT COUNT(*) FROM run_outputs WHERE run_id IN "
            "(SELECT id FROM runs WHERE environment = ?)",
            (environment,),
        ).fetchone()[0])
        return counts

    def delete_environment(self, environment: str) -> Dict[str, int]:
        """Delete an environment and everything belonging to it.

        For an environment that should never have existed — a reader
        mis-configuration that imported runs under a name like
        ``UNKNOWN``. This is **not** retirement, which marks a test as no
        longer in the suite while keeping its history. It removes the
        rows, and nothing puts them back.

        Every table carrying an ``environment`` column is covered (see
        :data:`_ENVIRONMENT_TABLES`), plus ``run_outputs``, which is
        reached through ``runs.id``. Ordered so that no statement leaves
        a dangling reference even momentarily: outputs before their runs,
        derived rows before the runs they were derived from.

        One transaction. A half-deleted environment is worse than either
        outcome — ``latest_runs`` is what every estate-wide read goes
        through, so a row there pointing at a deleted run would be a
        broken dashboard rather than a stale one.

        Returns the per-table row counts deleted. An environment that
        does not exist is not an error: every count is zero.
        """
        conn = self._conn()
        deleted = {}  # type: Dict[str, int]
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "DELETE FROM run_outputs WHERE run_id IN "
                "(SELECT id FROM runs WHERE environment = ?)",
                (environment,),
            )
            deleted["run_outputs"] = int(cursor.rowcount)
            for table in _ENVIRONMENT_TABLES:
                cursor = conn.execute(
                    "DELETE FROM {0} WHERE environment = ?".format(table),
                    (environment,),
                )
                deleted[table] = int(cursor.rowcount)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # The memoized trend is estate-wide and has just stopped being
        # true. (activity_hours needs no special step: it is in
        # _ENVIRONMENT_TABLES, so the environment's rows are gone.)
        self._invalidate_trend_cache()
        return deleted

    def vacuum(self) -> None:
        """Compact the database, backend permitting.

        On SQLite: VACUUM — exclusive and expensive (it rewrites the
        whole file), a maintenance-window operation, never part of
        serving a request. On MariaDB: a logged no-op — InnoDB manages
        its own space and the nearest equivalent (OPTIMIZE TABLE) is a
        different decision an operator takes on the server, not one the
        dashboard should spring on them.
        """
        self._backend.vacuum(self._conn())

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def ensure_user(
        self, username: str, created_at: datetime.datetime
    ) -> None:
        """Create *username* with *created_at* if it does not exist yet."""
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            try:
                conn.execute(
                    "INSERT INTO users (username, created_at) "
                    "VALUES (?, ?)",
                    (username, model.format_iso(created_at)),
                )
            except self._backend.integrity_error:
                # Another thread created the user between our SELECT and
                # INSERT; the user exists, which is all we need.
                pass

    def create_user(
        self, username: str, created_at: datetime.datetime
    ) -> Tuple[User, bool]:
        """Create a user explicitly; return ``(user, created)``.

        An existing user is returned unchanged (its original
        ``created_at`` preserved) with ``created=False``.
        """
        conn = self._conn()
        row = conn.execute(
            _USER_SELECT + " WHERE username = ?", (username,)
        ).fetchone()
        if row is not None:
            return _user_from_row(row), False
        try:
            conn.execute(
                "INSERT INTO users (username, created_at) VALUES (?, ?)",
                (username, model.format_iso(created_at)),
            )
        except self._backend.integrity_error:
            # Lost a race with another thread: fetch what it created.
            row = conn.execute(
                _USER_SELECT + " WHERE username = ?", (username,)
            ).fetchone()
            return _user_from_row(row), False
        return User(username, created_at, None, None), True

    def list_users(self, include_inactive: bool = False) -> List[User]:
        """Users ordered by username; active only unless asked otherwise.

        Active-only is the default because the overwhelmingly common
        caller is an assignee picker, and the whole point of
        deactivation is that those get shorter. A caller that wants the
        full roster — the management view — has to say so.
        """
        sql = _USER_SELECT
        if not include_inactive:
            sql += " WHERE deactivated_at IS NULL"
        rows = self._conn().execute(sql + " ORDER BY username").fetchall()
        return [_user_from_row(row) for row in rows]

    def get_user(self, username: str) -> Optional[User]:
        """One user by name, active or not; None if unknown."""
        row = self._conn().execute(
            _USER_SELECT + " WHERE username = ?", (username,)
        ).fetchone()
        return None if row is None else _user_from_row(row)

    def open_assignments_held_by(
        self, username: str, limit: int = 20
    ) -> Tuple[int, List[Tuple[str, str, str]]]:
        """How many live tests *username* owns, and a sample of them.

        Retired tests are excluded. They are not in the suite any more,
        so counting them would block deactivation over work that no
        longer exists — and retirement deliberately does not clear an
        assignment, so those rows stick around forever.

        Returns ``(total, sample)``; the sample is capped so a user
        holding a thousand tests does not produce a thousand-line error
        message.
        """
        conn = self._conn()
        where = (
            "FROM current_assignments ca "
            "LEFT JOIN test_retirements tr "
            "  ON tr.environment = ca.environment "
            " AND tr.script = ca.script "
            " AND tr.test_name = ca.test_name "
            "WHERE ca.assignee = ? AND tr.environment IS NULL"
        )
        total = conn.execute(
            "SELECT COUNT(*) " + where, (username,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT ca.environment, ca.script, ca.test_name " + where
            + " ORDER BY ca.environment, ca.script, ca.test_name LIMIT ?",
            (username, max(0, int(limit))),
        ).fetchall()
        return int(total), [(row[0], row[1], row[2]) for row in rows]

    def set_user_active(
        self,
        username: str,
        active: bool,
        changed_by: str,
        changed_at: datetime.datetime,
    ) -> User:
        """Deactivate or reactivate *username*; return its new state.

        Raises :class:`KeyError` if the user does not exist. Callers are
        expected to have checked :meth:`open_assignments_held_by` first —
        this method does not enforce it, because "can this be
        deactivated" is a policy question the API answers, and storage
        that silently refused would be impossible to use for a repair.

        *changed_by* is recorded only for a deactivation; reactivating
        clears both columns, so the account returns to being
        indistinguishable from one that was never deactivated.
        """
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                raise KeyError(username)
            if active:
                conn.execute(
                    "UPDATE users SET deactivated_at = NULL, "
                    "deactivated_by = NULL WHERE username = ?",
                    (username,),
                )
            else:
                self.ensure_user(changed_by, changed_at)
                conn.execute(
                    "UPDATE users SET deactivated_at = ?, "
                    "deactivated_by = ? WHERE username = ?",
                    (model.format_iso(changed_at), changed_by, username),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        user = self.get_user(username)
        assert user is not None  # it existed inside the transaction
        return user

    def is_active_user(self, username: str) -> bool:
        """True if *username* is unknown or active; False if deactivated.

        Unknown reads as active on purpose: usernames are created
        implicitly by the act of commenting or assigning, so "not in the
        table yet" is a new person, not a retired one.
        """
        row = self._conn().execute(
            "SELECT deactivated_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return row is None or row[0] is None

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(
        self,
        environment: str,
        script: str,
        test_name: str,
        author: str,
        text: str,
        created_at: datetime.datetime,
    ) -> Comment:
        """Add a comment to a test triple, implicitly creating *author*."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(author, created_at)
            comment_id = self._insert_comment(
                conn, (environment, script, test_name), author, text,
                created_at,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return Comment(
            comment_id=comment_id,
            environment=environment,
            script=script,
            test_name=test_name,
            author=author,
            created_at=created_at,
            text=text,
        )

    @staticmethod
    def _insert_comment(
        conn: sqlite3.Connection,
        triple: Tuple[str, str, str],
        author: str,
        text: str,
        created_at: datetime.datetime,
    ) -> int:
        """Append one comment inside the caller's transaction; return its id.

        Shared by the comment endpoint, retirement (which records the
        human's reason) and un-retirement (which records the machine's),
        so every one of them lands in the same thread.
        """
        cursor = conn.execute(
            "INSERT INTO comments (environment, script, test_name, "
            "author, created_at, text) VALUES (?, ?, ?, ?, ?, ?)",
            triple + (author, model.format_iso(created_at), text),
        )
        return int(cursor.lastrowid)

    def comments(
        self, environment: str, script: str, test_name: str
    ) -> List[Comment]:
        """Return all comments for the triple, oldest first."""
        rows = self._conn().execute(
            "SELECT id, environment, script, test_name, author, "
            "created_at, text FROM comments WHERE environment = ? "
            "AND script = ? AND test_name = ? ORDER BY id",
            (environment, script, test_name),
        ).fetchall()
        return [
            Comment(
                comment_id=int(row[0]),
                environment=row[1],
                script=row[2],
                test_name=row[3],
                author=row[4],
                created_at=model.parse_iso(row[5]),
                text=row[6],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Assignments
    # ------------------------------------------------------------------

    def set_assignee(
        self,
        environment: str,
        script: str,
        test_name: str,
        assignee: Optional[str],
        assigned_by: str,
        assigned_at: datetime.datetime,
    ) -> None:
        """Append an assignment-history row and update the current state.

        ``assignee=None`` clears the assignment (auditable: a row with a
        NULL assignee is appended to ``assignments``, and
        ``current_assignments`` is set to NULL rather than deleted). Both
        *assigned_by* and *assignee* (when not None) are implicitly
        created as users. Both writes share one transaction, so the log
        and the current state can never disagree.
        """
        conn = self._conn()
        triple = (environment, script, test_name)
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(assigned_by, assigned_at)
            if assignee is not None:
                self.ensure_user(assignee, assigned_at)
            conn.execute(
                "INSERT INTO assignments (environment, script, test_name, "
                "assignee, assigned_by, assigned_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                triple + (
                    assignee,
                    assigned_by,
                    model.format_iso(assigned_at),
                ),
            )
            existing = conn.execute(
                "SELECT 1 FROM current_assignments WHERE environment = ? "
                "AND script = ? AND test_name = ?",
                triple,
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO current_assignments (environment, script, "
                    "test_name, assignee) VALUES (?, ?, ?, ?)",
                    triple + (assignee,),
                )
            else:
                conn.execute(
                    "UPDATE current_assignments SET assignee = ? "
                    "WHERE environment = ? AND script = ? "
                    "AND test_name = ?",
                    (assignee,) + triple,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def current_assignee(
        self, environment: str, script: str, test_name: str
    ) -> Optional[str]:
        """Return the triple's current assignee (a ``current_assignments`` hit).

        None when the test was never assigned OR when the latest
        assignment cleared it.
        """
        row = self._conn().execute(
            "SELECT assignee FROM current_assignments WHERE environment = ? "
            "AND script = ? AND test_name = ?",
            (environment, script, test_name),
        ).fetchone()
        if row is None:
            return None
        return row[0]
