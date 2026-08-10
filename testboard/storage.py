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
    "COMPARE_CATEGORIES",
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
    "Stream",
    "StreamResult",
    "CompareCounts",
    "CompareRow",
    "UpsertCounts",
    "UpsertRejection",
    "ScriptHourBucket",
    "MAINLINE_STREAM_ID",
    "Storage",
]

#: ``streams.id`` of the mainline stream, seeded by migration 9. Runs
#: never carrying ``branch``/``build`` resolve here without a lookup —
#: see :func:`Storage._stream_key_for`. "Estate views pin stream_id = 1"
#: (docs/STREAMS_PLAN.md §1) reads this constant, not a magic number.
MAINLINE_STREAM_ID = 1


#: How long a memoized nightly trend may be reused. Bounds how stale the
#: chart can be after a write made by another PROCESS (an offline prune
#: while the server runs); writes made by this process clear it at once.
_TREND_CACHE_TTL_SECONDS = 60.0

#: Cap on memoized trend windows, so arbitrary ``days`` values cannot
#: grow the cache without limit.
_TREND_CACHE_MAX_ENTRIES = 32

#: Cap on the summary/watch memo (below) -- deliberately larger than
#: :data:`_TREND_CACHE_MAX_ENTRIES`: that cache holds one method's keys,
#: this one is shared by several (summary_rollup, queue_counts,
#: status_queue x6 kinds, test_counts_by_environment,
#: latest_run_time_by_environment, environments, scripts), so the same
#: "one or two working sets" sizing works out to more raw entries. Same
#: full-clear-rather-than-evict policy as the trend cache, same reason:
#: the scope-key space (stream x environment/product-allowlist x cutoff)
#: is small, so unbounded growth -- not eviction quality -- is the risk.
#:
#: Deliberately does NOT also size :meth:`Storage.failure_streak_bounds_
#: many`'s per-entry memo (see :data:`_STREAK_CACHE_MAX_ENTRIES`) --
#: measured live on this project's own ~12k-mainline-test / 5-env
#: estate copy: one unscoped ``/api/summary`` request alone touched 139
#: distinct keys once every queue kind's status_queue entry and every
#: FAIL row's failure-streak entry were counted, comfortably over this
#: cap on its own. Sharing one dict meant the per-request cap-then-clear
#: fired MID-request, wiping summary_rollup/queue_counts and the
#: earlier-processed queue kinds before the request even finished --
#: found by profiling a "warm" repeat call that was still costing 28 SQL
#: statements. Splitting the two removes the coupling: this cap only
#: has to hold one scope's worth of ~20 request-composition keys, never
#: however many tests happen to be failing across whatever pages were
#: viewed.
_SUMMARY_CACHE_MAX_ENTRIES = 128

#: Cap on the per-entry failure-streak memo (see
#: :meth:`Storage.failure_streak_bounds_many`), separate from
#: :data:`_SUMMARY_CACHE_MAX_ENTRIES` for the reason documented there.
#: Sized to the estate, not to one request: the working set is "every
#: currently-FAIL triple anyone has looked at recently", which is
#: bounded by how many tests are failing estate-wide (typically a small
#: fraction of the total), not by one page's row cap -- 4096 covers
#: several thousand simultaneously-failing tests before the same
#: full-clear-and-refill policy kicks in.
_STREAK_CACHE_MAX_ENTRIES = 4096

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
    (
        9,
        [
            # Streams: build runs beside the mainline nightlies
            # (docs/STREAMS_PLAN.md, WP-21, drop 2 of the products &
            # streams work; kind narrowed to {mainline, build} by WP-25 —
            # see the AMENDED note below). OBSERVED, not registered: a
            # stream row is created lazily inside the import transaction
            # the first time an import record names a build
            # (Storage._find_or_create_stream) — there is no admin UI
            # that creates one. Row 1 is THE mainline stream and is
            # seeded by the python step below; every run recorded
            # before this migration is, and remains, on it.
            #
            # FOLDED IN, not appended as migration 10: the
            # assignments/current_assignments ALTERs a few entries below
            # were added after this entry first shipped internally, but
            # before this branch was accepted or deployed anywhere —
            # docs/STREAMS_PLAN.md §3.6's triage-from-a-branch behaviour
            # (found in first human use of the branch dashboard) needs
            # them, and migration 9 is still unshipped. No database
            # anywhere has the narrower shape this entry had before the
            # fold (this repo's own scratch copies do not count, and are
            # rebuilt from v8 rather than trusted); a database that DOES
            # exist at v9 already only if someone ran an earlier build of
            # this exact branch, which is a dev machine, not a
            # deployment. `CLAUDE.md`'s "never edit `MIGRATIONS[0]`" rule
            # protects a migration that is IN PRODUCTION; this one is not
            # yet, so folding here is the right side of that line, not a
            # violation of it — a migration 10 for one more nullable
            # column on a schema element nobody has deployed would just
            # be two round trips of ALTER TABLE where one already does.
            #
            # AMENDED AGAIN IN PLACE (WP-25, docs/ONE_KIND_PLAN.md,
            # 2026-08-09), same precedent as the fold above and for the
            # same reason -- still unshipped anywhere, so this is deletion
            # before first contact, not a migration: the 'branch' kind is
            # gone. ``kind`` was NEVER constrained by a CHECK -- validation
            # lived entirely in Python (Storage._stream_key_for) -- so
            # this amendment is comment-only; the DDL below is BYTE
            # IDENTICAL to what shipped internally. From this commit on,
            # every stream this migration's own writer or any later
            # import creates has ``kind`` = ``'mainline'`` (row 1, seeded
            # below) or ``'build'`` -- never ``'branch'``.
            """
            CREATE TABLE streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE (product, kind, name)
            )
            """,
            "python: seed_mainline_stream",
            # ALTER TABLE ... ADD COLUMN with a NOT NULL DEFAULT cannot
            # ALSO carry an inline REFERENCES while
            # `PRAGMA foreign_keys=ON` (every connection this module
            # opens): SQLite refuses with "Cannot add a REFERENCES
            # column with non-NULL default value" — measured directly
            # against a copy of this migration before it shipped (it is
            # not documented anywhere in SQLite's own ALTER TABLE page
            # in a way that would have been found without trying it).
            # `latest_runs`, rebuilt below via CREATE TABLE rather than
            # ALTER, keeps the real FK on its stream_id; `runs` already
            # carries no FK to anything (not even to itself for the
            # UNIQUE it relies on), so dropping this one is a narrowing
            # of an already-absent guarantee, not a new gap.
            "ALTER TABLE runs ADD COLUMN stream_id INTEGER NOT NULL "
            "DEFAULT 1",
            # Nullable, so the restriction above does not apply (also
            # verified directly): a comment is an annotation on the
            # (environment, script, test_name) triple, stream-agnostic
            # by design (docs/STREAMS_PLAN.md §0.4), so NULL means
            # "posted before this migration — there was only one stream,
            # nothing to record" and every comment from here on carries
            # the stream it was posted from. ON DELETE SET NULL: deleting
            # a stream (tools/drop_stream.py) must not take its comments
            # with it — the annotation still belongs to the triple.
            "ALTER TABLE comments ADD COLUMN stream_id INTEGER "
            "REFERENCES streams(id) ON DELETE SET NULL",
            # Same shape as comments.stream_id, same reason, for the
            # other annotation-not-identity table: an assignment is a
            # decision about the (environment, script, test_name)
            # triple, not about one stream's copy of it — triage from a
            # branch assigns the SAME test everyone else sees
            # (docs/STREAMS_PLAN.md §0.4/§3.6), so this is metadata
            # about WHERE the assignment was made, never a partition key.
            # NULL means "made before this column existed, or with no
            # declared stream context" — the same reading as a null
            # comment.stream_id, and what every assignment ever made
            # before this migration has. ON DELETE SET NULL for the same
            # reason: deleting a stream (tools/drop_stream.py) must not
            # take an assignment's history with it.
            "ALTER TABLE assignments ADD COLUMN stream_id INTEGER "
            "REFERENCES streams(id) ON DELETE SET NULL",
            # The CURRENT-state twin of the ALTER above — see its
            # comment. Both are written together, in the same
            # transaction, by Storage.set_assignee.
            "ALTER TABLE current_assignments ADD COLUMN stream_id "
            "INTEGER REFERENCES streams(id) ON DELETE SET NULL",
            # latest_runs is DERIVED (~12k rows: one per test, on
            # mainline alone — see migration 5's comment for the
            # precedent). It is rebuilt with the stream in the key
            # rather than migrated in place because SQLite cannot widen
            # a PRIMARY KEY with ALTER TABLE: CREATE new / INSERT..
            # SELECT (stream_id 1 for every existing row — every run on
            # file predates streams) / DROP / RENAME, then recreate its
            # five indexes (four carried over from migrations 1 and 4,
            # now leading with stream_id so a stream-scoped read is
            # still a pure index range and not a table-wide filter; one
            # new index on the bare triple for "this test on every
            # stream", which WP-22's every-build view reads).
            #
            # MEASURED on a COPY of the dev database (220 MB, 12,008
            # tests / 540,192 runs, brought to version 8 first so this
            # is entry 9 alone): see the print the python step emits,
            # captured in the commit message and docs/drops/2026-08-14.md
            # — this is DEV data, not production, which is roughly four
            # times its size (per CLAUDE.md) and has not been measured
            # from here.
            "python: rebuild_latest_runs_with_stream",
        ],
    ),
    (
        10,
        [
            # activity_hours/script_hours gain stream_id in their PRIMARY
            # KEY (docs/STREAMS_PLAN.md §5.1, WP-23: long-running branch
            # streams get their own trend/staleness, not just a delta
            # view). Both are DERIVED tables, same shape as latest_runs at
            # migration 9: SQLite cannot widen a PRIMARY KEY with ALTER,
            # so this is CREATE new / INSERT..SELECT / DROP / RENAME.
            #
            # Every existing row gets stream_id = 1 (a LITERAL, not a
            # re-aggregation from `runs`): both tables have been
            # mainline-only since migration 6/7 (the WP-21 writer
            # explicitly skipped maintaining them for stream_id != 1), so
            # every row on file already IS mainline's — copying is
            # correct and far cheaper than re-deriving from `runs`, which
            # is why this is a straight row copy rather than the
            # GROUP-BY-over-runs shape _rebuild_activity_hours uses.
            #
            # Takes 10 from WP-15's parked reservation, which moves to
            # 11 — the fifth time this exact swap has happened (see
            # UPGRADE_PLAN.md §1's running note); the WIP branch must
            # renumber to 11 before it merges.
            #
            # MEASURED on a COPY of the dev database (220 MB, 540,192
            # runs, 12,008 tests, brought to version 9 first so this is
            # entry 10 alone): 0.038-0.041s across two runs, rebuilding
            # 1,077 activity_hours rows and 21,988 script_hours rows
            # (both a straight copy with stream_id=1, not a re-aggregate
            # over `runs` — see above). Captured in the commit message
            # and docs/drops/2026-08-14.md. This is DEV data, not
            # production (roughly four times its size per CLAUDE.md) and
            # has not been measured from here.
            #
            # Each step below does its own CREATE TABLE ... _new / INSERT
            # ../SELECT / DROP / RENAME internally (see
            # :func:`_rebuild_activity_hours_with_stream` and
            # :func:`_rebuild_script_hours_with_stream`) — the same
            # single-python-step shape migration 9 used for latest_runs;
            # there is no separate literal DDL step here.
            "python: rebuild_activity_hours_with_stream",
            "python: rebuild_script_hours_with_stream",
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


def _seed_mainline_stream(conn: sqlite3.Connection) -> None:
    """Insert THE mainline stream row (id 1) for migration 9.

    Every run recorded before this migration is a mainline run, and
    ``runs.stream_id DEFAULT 1`` (the next step) is what makes that true
    without touching a single existing row. ``product`` and ``name`` are
    both ``""`` — mainline is not scoped to a product and has no name to
    show (docs/STREAMS_PLAN.md §1).
    """
    now = model.format_iso(model.utcnow())
    conn.execute(
        "INSERT INTO streams (id, product, kind, name, first_seen, "
        "last_seen) VALUES (1, '', 'mainline', '', ?, ?)",
        (now, now),
    )


def _rebuild_latest_runs_with_stream(conn: sqlite3.Connection) -> None:
    """Rebuild ``latest_runs`` with ``stream_id`` in its key (migration 9).

    SQLite cannot widen a PRIMARY KEY with ALTER TABLE, so this is
    CREATE new / INSERT..SELECT / DROP / RENAME — the same shape as
    every other derived-table rebuild in this module, and the migration
    comment on entry 9 has the measured timing. Every existing row gets
    ``stream_id = 1``: it predates streams, so it IS a mainline row.

    Recreates all five indexes: the four inherited from migrations 1
    and 4 (now leading with ``stream_id``, so a stream-scoped read stays
    a pure index range) plus one new index on the bare triple — "this
    test on every stream" (WP-22's every-build view; cheap to add now
    while the table is still ~12k rows, per docs/STREAMS_PLAN.md §3.1).
    """
    started = time.time()
    conn.execute(
        "CREATE TABLE latest_runs_new ("
        "stream_id INTEGER NOT NULL DEFAULT 1 REFERENCES streams(id), "
        "environment TEXT NOT NULL, "
        "script TEXT NOT NULL, "
        "test_name TEXT NOT NULL, "
        "run_id INTEGER NOT NULL REFERENCES runs(id), "
        "start_time TEXT NOT NULL, "
        "result TEXT NOT NULL, "
        "prev_result TEXT, "
        "duration_seconds REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (stream_id, environment, script, test_name)"
        ")"
    )
    cursor = conn.execute(
        "INSERT INTO latest_runs_new (stream_id, environment, script, "
        "test_name, run_id, start_time, result, prev_result, "
        "duration_seconds) "
        "SELECT 1, environment, script, test_name, run_id, start_time, "
        "result, prev_result, duration_seconds FROM latest_runs"
    )
    conn.execute("DROP TABLE latest_runs")
    conn.execute("ALTER TABLE latest_runs_new RENAME TO latest_runs")
    conn.execute(
        "CREATE INDEX idx_latest_runs_result ON latest_runs "
        "(stream_id, result, environment, script, test_name)"
    )
    conn.execute(
        "CREATE INDEX idx_latest_runs_start_time "
        "ON latest_runs (stream_id, start_time)"
    )
    conn.execute(
        "CREATE INDEX idx_latest_runs_start_sort ON latest_runs "
        "(stream_id, start_time, environment, script, test_name)"
    )
    conn.execute(
        "CREATE INDEX idx_latest_runs_duration_sort ON latest_runs "
        "(stream_id, duration_seconds, environment, script, test_name)"
    )
    conn.execute(
        "CREATE INDEX idx_latest_runs_triple "
        "ON latest_runs (environment, script, test_name)"
    )
    if cursor.rowcount:
        print(
            "latest_runs: rebuilt {0} rows with stream_id in {1:.1f}s"
            .format(cursor.rowcount, time.time() - started),
            flush=True,
        )


def _rebuild_activity_hours_with_stream(conn: sqlite3.Connection) -> None:
    """Rebuild ``activity_hours`` with ``stream_id`` in its key (migration 10).

    Same shape as :func:`_rebuild_latest_runs_with_stream`: CREATE new /
    INSERT..SELECT / DROP / RENAME, because SQLite cannot widen a
    PRIMARY KEY with ALTER. Every existing row copies across with
    ``stream_id = 1`` (a literal, not a re-aggregation from ``runs``) —
    this table has been mainline-only since migration 6, so every row on
    file already IS mainline's (docs/STREAMS_PLAN.md §5.1).

    This function is distinct from :func:`_rebuild_activity_hours`
    (kept unchanged: migration 6's step still runs it against the
    pre-stream table shape on a fresh install) and from the runtime
    rebuild :meth:`Storage.prune_runs_before` uses post-migration-10
    (:func:`_rebuild_activity_hours_all_streams`, which re-aggregates
    from ``runs`` because a prune can touch every stream's rows).
    """
    started = time.time()
    conn.execute(
        "CREATE TABLE activity_hours_new ("
        "stream_id INTEGER NOT NULL DEFAULT 1 REFERENCES streams(id), "
        "environment TEXT NOT NULL, "
        "hour TEXT NOT NULL, "
        "result TEXT NOT NULL, "
        "count INTEGER NOT NULL, "
        "PRIMARY KEY (stream_id, environment, hour, result)"
        ")"
    )
    cursor = conn.execute(
        "INSERT INTO activity_hours_new "
        "(stream_id, environment, hour, result, count) "
        "SELECT 1, environment, hour, result, count FROM activity_hours"
    )
    conn.execute("DROP TABLE activity_hours")
    conn.execute("ALTER TABLE activity_hours_new RENAME TO activity_hours")
    if cursor.rowcount:
        print(
            "activity_hours: rebuilt {0} rows with stream_id in {1:.1f}s"
            .format(cursor.rowcount, time.time() - started),
            flush=True,
        )


def _rebuild_script_hours_with_stream(conn: sqlite3.Connection) -> None:
    """Rebuild ``script_hours`` with ``stream_id`` in its key (migration 10).

    See :func:`_rebuild_activity_hours_with_stream` — same shape, same
    reasoning, the ``script_hours`` twin.
    """
    started = time.time()
    conn.execute(
        "CREATE TABLE script_hours_new ("
        "stream_id INTEGER NOT NULL DEFAULT 1 REFERENCES streams(id), "
        "environment TEXT NOT NULL, "
        "hour TEXT NOT NULL, "
        "script TEXT NOT NULL, "
        "result TEXT NOT NULL, "
        "count INTEGER NOT NULL, "
        "first_start TEXT NOT NULL, "
        "last_end TEXT NOT NULL, "
        "PRIMARY KEY (stream_id, environment, hour, script, result)"
        ")"
    )
    cursor = conn.execute(
        "INSERT INTO script_hours_new (stream_id, environment, hour, "
        "script, result, count, first_start, last_end) "
        "SELECT 1, environment, hour, script, result, count, first_start, "
        "last_end FROM script_hours"
    )
    conn.execute("DROP TABLE script_hours")
    conn.execute("ALTER TABLE script_hours_new RENAME TO script_hours")
    if cursor.rowcount:
        print(
            "script_hours: rebuilt {0} rows with stream_id in {1:.1f}s"
            .format(cursor.rowcount, time.time() - started),
            flush=True,
        )


def _rebuild_activity_hours_all_streams(conn: sqlite3.Connection) -> None:
    """Rebuild ``activity_hours`` from ``runs``, including ``stream_id``.

    Used at RUNTIME by :meth:`Storage.prune_runs_before` on an
    already-migrated (post-migration-10) database — unlike
    :func:`_rebuild_activity_hours`, which is what migration 6's own
    step calls against a table that does not have ``stream_id`` yet and
    must stay untouched for that reason. A prune can delete rows
    belonging to ANY stream, so this re-derives every stream's
    partition in one pass rather than assuming stream 1 (WP-23,
    docs/STREAMS_PLAN.md §5.1/§5.2). DELETE-then-INSERT, the same
    invariant as the un-scoped rebuild: exact equality with ``SELECT
    stream_id, environment, SUBSTR(start_time, 1, 13), result, COUNT(*)
    FROM runs GROUP BY 1, 2, 3, 4``.
    """
    started = time.time()
    conn.execute("DELETE FROM activity_hours")
    cursor = conn.execute(
        "INSERT INTO activity_hours "
        "(stream_id, environment, hour, result, count) "
        "SELECT stream_id, environment, SUBSTR(start_time, 1, 13), result, "
        "COUNT(*) FROM runs "
        "GROUP BY stream_id, environment, SUBSTR(start_time, 1, 13), result"
    )
    if cursor.rowcount:
        print(
            "activity_hours: rebuilt {0} rows (all streams) in {1:.1f}s"
            .format(cursor.rowcount, time.time() - started),
            flush=True,
        )


def _rebuild_script_hours_all_streams(conn: sqlite3.Connection) -> None:
    """Rebuild ``script_hours`` from ``runs``, including ``stream_id``.

    See :func:`_rebuild_activity_hours_all_streams` — same reasoning,
    the ``script_hours`` twin, used by the same runtime caller.
    """
    started = time.time()
    conn.execute("DELETE FROM script_hours")
    cursor = conn.execute(
        "INSERT INTO script_hours (stream_id, environment, hour, script, "
        "result, count, first_start, last_end) "
        "SELECT stream_id, environment, SUBSTR(start_time, 1, 13), script, "
        "result, COUNT(*), MIN(start_time), MAX(end_time) "
        "FROM runs GROUP BY stream_id, environment, "
        "SUBSTR(start_time, 1, 13), script, result"
    )
    if cursor.rowcount:
        print(
            "script_hours: rebuilt {0} rows (all streams) in {1:.1f}s"
            .format(cursor.rowcount, time.time() - started),
            flush=True,
        )


#: Python migration steps, by the name used after the prefix.
_MIGRATION_STEPS = {
    "backfill_latest_durations": _backfill_latest_durations,
    "rebuild_activity_hours": _rebuild_activity_hours,
    "rebuild_script_hours": _rebuild_script_hours,
    "seed_mainline_stream": _seed_mainline_stream,
    "rebuild_latest_runs_with_stream": _rebuild_latest_runs_with_stream,
    "rebuild_activity_hours_with_stream": _rebuild_activity_hours_with_stream,
    "rebuild_script_hours_with_stream": _rebuild_script_hours_with_stream,
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
    #: WHERE the current assignment was made from (WP-21) — an
    #: annotation, never a partition of the assignment itself (the
    #: assignee above is the same regardless of which stream someone is
    #: viewing). None for a mainline-made assignment, or one made before
    #: this column existed. See Storage.set_assignee.
    assignment_stream_id: Optional[int]
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
    assignment_stream_id: Optional[int]
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
    """A comment attached to a test (the triple, not a single run).

    ``stream_id`` (WP-21, migration 9) is "posted from" — an annotation
    only, never part of the comment's identity: the comment lives on the
    triple, stream-agnostic, so it is visible from every stream's test
    detail page (docs/STREAMS_PLAN.md §1/§3.4). ``None`` means posted
    before this migration, when there was only one stream to be from.
    """

    comment_id: int
    environment: str
    script: str
    test_name: str
    author: str
    created_at: datetime.datetime
    text: str
    stream_id: Optional[int]


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


class Stream(NamedTuple):
    """One observed build/mainline stream (migration 9, WP-21; kind
    narrowed from {mainline, branch, build} to {mainline, build} by
    WP-25, docs/ONE_KIND_PLAN.md — the ``branch`` kind died before it
    ever shipped, so ``kind`` is either ``'mainline'`` or ``'build'``).

    OBSERVED, not registered: created lazily inside the import
    transaction the first time a record names a build (see
    :meth:`Storage._find_or_create_stream`); there is no admin UI that
    creates one. Row 1 is THE mainline stream (``product=''``,
    ``kind='mainline'``, ``name=''``), seeded by migration 9.
    ``failing`` is the stream's own current failing count, from its
    partition of ``latest_runs`` — populated by :meth:`Storage.list_streams`
    only (no hidden staleness constant: the caller judges freshness from
    ``last_seen`` itself, per docs/STREAMS_PLAN.md §3.5).
    """

    stream_id: int
    product: str
    kind: str
    name: str
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    failing: int


class CompareCounts(NamedTuple):
    """The six headline counts of :meth:`Storage.compare_streams`.

    Each test of the *stream*'s product is classified by its latest
    result on *stream* against its latest result on *baseline* — see
    docs/STREAMS_PLAN.md §3.5. A test with no result on either side
    (removed, or never run there — the dashboard cannot tell which) is
    ``no_result``, never rendered as a pass, a fail, or an implied
    anything (§0.6).

    ``agree`` is the implicit sixth bucket (both sides have a result and
    it is not FAIL on either side) — carried as a real field, not left
    for the caller to infer by subtracting the other five from a total
    it does not have, because neither ``compare_counts`` nor
    ``compare_counts_many`` otherwise exposes the comparison universe
    size the frontend's "N tests agree and are not listed" line and the
    coverage line (§3.6) both need.
    """

    new_failures: int
    new_passes: int
    both_failing: int
    new_tests: int
    no_result: int
    agree: int


class CompareRow(NamedTuple):
    """One test in a :meth:`Storage.compare_category` page.

    ``stream_result``/``baseline_result`` are ``None`` when that side has
    no result for the triple (a category-consistent fact — a row is
    only ever in the ``new_tests``/``no_result`` categories because one
    side is None). ``stream_run_id``/``stream_start_time`` (WP-21,
    review-from-a-branch) are likewise ``None`` exactly when
    ``stream_result`` is — a triple with no result on the stream side
    has no run to review, which is a fact, not a gap: the frontend shows
    no Review expander for those rows rather than a broken one. Both are
    always the STREAM's own run, never the baseline's — reviewing a
    branch's failure means that branch's output, and the shared review
    panel's "View in timeline" link needs the right run's start time to
    deep-link correctly. ``assignee`` is the triple's CURRENT assignee,
    unpartitioned by stream (docs/STREAMS_PLAN.md §3.4: assigning from a
    branch row assigns the same test everyone else sees).
    """

    environment: str
    script: str
    test_name: str
    stream_result: Optional[Result]
    baseline_result: Optional[Result]
    stream_run_id: Optional[int]
    stream_start_time: Optional[datetime.datetime]
    assignee: Optional[str]


class StreamResult(NamedTuple):
    """One stream's latest result for a single (environment, script,
    test_name) triple (WP-22, docs/STREAMS_PLAN.md §4.1: the test page's
    "Every build" disclosure and its stream dropdown).

    Produced by :meth:`Storage.stream_results_for_triple`, which reads
    ONLY ``latest_runs`` rows that exist for the triple — a stream that
    never ran this test is simply absent from the list (§0.6: "a stream
    with no result for a test says nothing about it"). The caller
    decides how to render that absence; this type never fakes a result
    to paper over it. ``stream.failing`` is always 0 here (meaningless
    for this read), the same convention :meth:`Storage.stream_identities`
    uses.
    """

    stream: Stream
    result: Result
    run_id: int
    start_time: datetime.datetime


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


class UpsertRejection(NamedTuple):
    """One record :meth:`Storage.upsert_runs` refused to store.

    WP-21's only storage-level rejection: the legacy-UNIQUE collision
    of docs/STREAMS_PLAN.md §3.2 (a different stream already holds this
    exact ``(environment, script, test_name, start_time)``). Everything
    else that can go wrong with a record is caught by
    :func:`model.parse_run_record` before storage ever sees it.
    ``index`` is the record's position in the sequence passed to
    :meth:`~Storage.upsert_runs` — the caller (``api._handle_import``)
    maps it back to the original request index.
    """

    index: int
    message: str
    environment: str
    script: str
    test_name: str
    start_time: datetime.datetime


class UpsertCounts(NamedTuple):
    """Result of a batch upsert.

    ``unchanged`` counts records that were byte-identical to what is
    already stored (metadata and output fingerprint both matching) and
    therefore wrote nothing at all. The site feeder re-pushes its whole
    recent window every 10 minutes, so in steady state this is MOST
    records; the split exists so that a no-op push is visible as one in
    logs and in the import response, instead of masquerading as 10,000
    updates.

    ``rejections`` (WP-21) lists every record refused at the storage
    layer — see :class:`UpsertRejection`. Empty for every batch that
    carries no branch/build collision, which in practice is every batch.
    """

    inserted: int
    updated: int
    unchanged: int
    rejections: List[UpsertRejection]


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

#: The five categories a stream-vs-baseline comparison classifies every
#: test into (docs/STREAMS_PLAN.md §3.5), plus the implicit "agree"
#: bucket (both sides share the same non-FAIL verdict) that the UI's
#: "N tests agree and are not listed" line derives by subtraction
#: rather than an endpoint returning it directly. Public — the API
#: layer validates its ``category=`` query param against this, the
#: same shape as :data:`QUEUE_KINDS`.
COMPARE_CATEGORIES = (
    "new_failures",
    "new_passes",
    "both_failing",
    "new_tests",
    "no_result",
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
    "r.known_failure_reason", "ca.assignee", "ca.stream_id",
    "tr.retired_at", "tr.retired_by",
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
        assignment_stream_id=(
            None if row[10] is None else int(row[10])
        ),
        retired_at=None if row[11] is None else model.parse_iso(row[11]),
        retired_by=row[12],
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
        # WP-23: the key gained a leading stream_id (int) so a branch's
        # trend request can never serve, or be served by, mainline's.
        self._trend_cache = {}  # type: Dict[Tuple[int, str, Optional[str], Optional[Tuple[str, ...]]], Tuple[float, List[DailyResultCount]]]
        self._trend_lock = threading.Lock()
        # WP-23 "ONE MORE PERF SLICE": one shared TTL-bounded memo for the
        # handful of expensive /api/summary and /api/watch storage reads
        # (see _cached_summary/_store_summary below) -- same discipline as
        # _trend_cache, generalised to hold several methods' keys in one
        # dict rather than one dict per method, since there are too many
        # of them to justify the trend cache's per-method duplication.
        # Every key's first element is the method's own name, so no two
        # methods can ever collide on a key.
        self._summary_cache = {}  # type: Dict[Tuple[Any, ...], Tuple[float, Any]]
        self._summary_lock = threading.Lock()
        # A SEPARATE dict for failure_streak_bounds_many's per-entry memo
        # (see _cached_streak/_store_streak below and
        # _STREAK_CACHE_MAX_ENTRIES) -- sharing _summary_cache measured as
        # a real bug, not a hypothetical one: one entries-set worth of
        # per-triple keys blew past _SUMMARY_CACHE_MAX_ENTRIES mid-request
        # and cleared summary_rollup/queue_counts before the SAME request
        # finished using them.
        self._streak_cache = {}  # type: Dict[Tuple[Any, ...], Tuple[float, Any]]
        self._streak_lock = threading.Lock()
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

    @staticmethod
    def _stream_key_for(rec: RunRecord) -> Optional[Tuple[str, str]]:
        """``(kind, name)`` for a non-mainline record, or None for mainline.

        ``kind`` is always ``"build"`` — the only non-mainline kind left
        after WP-25 (docs/ONE_KIND_PLAN.md) collapsed the streams.kind
        enum from {mainline, branch, build} to {mainline, build}; the
        ``branch`` kind died before it ever shipped, so this is deletion,
        not migration. :func:`model.parse_run_record` rejects a record
        carrying ``branch`` outright, before it is ever batched.
        """
        if rec.build:
            return ("build", rec.build)
        return None

    @staticmethod
    def _find_or_create_stream(
        conn: sqlite3.Connection, product: str, kind: str, name: str,
        seen_at: str,
    ) -> int:
        """Find or create the ``(product, kind, name)`` stream row.

        Product is resolved by the caller from the record's environment
        at the moment a NEW stream is created, and is then fixed — an
        existing stream's product is never touched here, matching
        docs/STREAMS_PLAN.md §3.3 ("resolved ... at creation time, then
        fixed"). ``first_seen``/``last_seen`` both start at *seen_at*;
        :meth:`upsert_runs` widens them for the rest of the batch in one
        pass at the end (see ``stream_bounds``), the same batching shape
        as :meth:`_apply_activity_deltas`.
        """
        row = conn.execute(
            "SELECT id FROM streams WHERE product = ? AND kind = ? "
            "AND name = ?",
            (product, kind, name),
        ).fetchone()
        if row is not None:
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO streams (product, kind, name, first_seen, "
            "last_seen) VALUES (?, ?, ?, ?, ?)",
            (product, kind, name, seen_at, seen_at),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _stream_label(conn: sqlite3.Connection, stream_id: int) -> str:
        """``"mainline"`` or ``"{kind}:{name}"`` for an error message."""
        if stream_id == MAINLINE_STREAM_ID:
            return "mainline"
        row = conn.execute(
            "SELECT kind, name FROM streams WHERE id = ?", (stream_id,)
        ).fetchone()
        if row is None:
            return "stream {0}".format(stream_id)
        return "{0}:{1}".format(row[0], row[1])

    def upsert_runs(self, records: Sequence[RunRecord]) -> UpsertCounts:
        """Insert or update a batch of runs in ONE transaction.

        A run is keyed by ``(stream_id, environment, script, test_name,
        start_time)`` — see docs/STREAMS_PLAN.md §3.2 for how that
        coexists with the table-level UNIQUE on ``(environment, script,
        test_name, start_time)`` alone (frozen since migration 1, and
        never widened: rebuilding ``runs`` — 4.4M rows, a network mount —
        is not a startup-migration operation). For each record the
        existing row is looked up FIRST BY THE FULL KEY (an index hit on
        the UNIQUE constraint, filtered to this record's stream in
        Python — cheap, the constraint has no stream column to seek on);
        if found the row is UPDATEd in place (preserving its rowid),
        otherwise a SECOND lookup checks whether the legacy key
        (``environment``, ``script``, ``test_name``, ``start_time``
        alone) is already claimed by a DIFFERENT stream. If so the
        record is REJECTED — naming both streams — rather than either
        silently overwriting the other stream's row or letting the
        INSERT hit the UNIQUE constraint and roll back the whole batch
        (violating the one-bad-record rule). This is believed to be
        near-impossible in practice (a branch run microsecond-identical
        to a mainline run of the same triple) and the second SELECT
        costs nothing that grows with the estate — one index seek, only
        on the insert path.

        Non-mainline records resolve their stream INSIDE this
        transaction via :meth:`_find_or_create_stream`: product from the
        record's environment (:meth:`environment_products_map`,
        cached once per batch since environments repeat), fixed at
        creation. ``streams.first_seen``/``last_seen`` are widened in
        one pass at the end of the batch, not per record.

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
        **Un-retirement is mainline-only** (WP-21, docs/STREAMS_PLAN.md
        §3.4): a branch run may predate a retirement decided on
        mainline, and must not silently reverse it.

        A NULL fingerprint (any row imported before migration 6) never
        matches, so such a record takes the full write path once and is
        stamped; the active window self-heals in one push cycle.

        ``activity_hours``/``script_hours`` are maintained here, in the
        same transaction, for EVERY stream (WP-23, docs/STREAMS_PLAN.md
        §5.1/§5.2 — before this, WP-21 skipped non-mainline records
        entirely so a branch's activity could never pollute mainline's
        trend/staleness; the skip is gone, and stream_id now leads both
        tables' PRIMARY KEY, so the SAME isolation holds by construction:
        each stream's rows are a disjoint partition, and a write to one
        can never touch another's cells): +1 for each inserted run, and
        a paired -1/+1 when an update changes a stored result. Start
        times are immutable (they are part of the run's key), so a run
        can never move between hours.

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
        rejections = []  # type: List[UpsertRejection]
        # Net activity_hours changes for this batch, applied in one pass
        # at the end: a batch of 500 typically spans a handful of
        # (stream_id, environment, hour, result) cells, not 500.
        deltas = {}  # type: Dict[Tuple[int, str, str, str], int]
        # script_hours changes: pure growth (from inserts), merged per
        # bucket, plus the buckets an update may have shrunk.
        grown = {}  # type: Dict[Tuple[int, str, str, str, str], List[Any]]
        recompute = set()  # type: Set[Tuple[int, str, str, str, str]]
        # WP-21: per-batch caches so a whole batch of one branch costs
        # one product lookup and one stream find-or-create, not one per
        # record; and the observed first/last seen bounds, widened onto
        # `streams` in a single pass per touched stream at the end.
        # Keyed by stream_id directly (not by (product, kind, name)):
        # mainline's own bound is tracked here too — every record widens
        # ITS OWN stream's first_seen/last_seen, mainline included, so
        # streams.last_seen for row 1 is a true "mainline's own clock"
        # rather than frozen at whatever migration 9 seeded it to. This
        # is what a mainline card's "baseline last_seen" (WP-21 §3.5/
        # §3.6, the compare endpoint and the Watchlist's s: cards) reads.
        stream_cache = {}  # type: Dict[Tuple[str, str, str], int]
        env_product_cache = {}  # type: Dict[str, str]
        stream_bounds = {}  # type: Dict[int, List[str]]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for index, rec in enumerate(records):
                start = model.format_iso(rec.start_time)
                end = model.format_iso(rec.end_time)
                fingerprint = _output_fingerprint(rec.output)

                key = self._stream_key_for(rec)
                if key is None:
                    stream_id = MAINLINE_STREAM_ID
                else:
                    kind, name = key
                    product = env_product_cache.get(rec.environment)
                    if product is None:
                        prow = conn.execute(
                            "SELECT product FROM environment_products "
                            "WHERE environment = ?",
                            (rec.environment,),
                        ).fetchone()
                        product = prow[0] if prow is not None else ""
                        env_product_cache[rec.environment] = product
                    cache_key = (product, kind, name)
                    stream_id = stream_cache.get(cache_key)
                    if stream_id is None:
                        stream_id = self._find_or_create_stream(
                            conn, product, kind, name, start
                        )
                        stream_cache[cache_key] = stream_id
                bounds = stream_bounds.setdefault(stream_id, [start, start])
                if start < bounds[0]:
                    bounds[0] = start
                if start > bounds[1]:
                    bounds[1] = start

                row = conn.execute(
                    "SELECT id, result, end_time, source_link, "
                    "known_failure_reason, output_fingerprint "
                    "FROM runs WHERE environment = ? AND "
                    "script = ? AND test_name = ? AND start_time = ? "
                    "AND stream_id = ?",
                    (
                        rec.environment, rec.script, rec.test_name, start,
                        stream_id,
                    ),
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
                    collision = conn.execute(
                        "SELECT stream_id FROM runs WHERE environment = ? "
                        "AND script = ? AND test_name = ? "
                        "AND start_time = ?",
                        (rec.environment, rec.script, rec.test_name, start),
                    ).fetchone()
                    if collision is not None:
                        other = self._stream_label(
                            conn, int(collision[0])
                        )
                        mine = self._stream_label(conn, stream_id)
                        rejections.append(UpsertRejection(
                            index=index,
                            message=(
                                "environment/script/test_name/start_time "
                                "already recorded on stream {0!r}; this "
                                "record targets stream {1!r}, and the "
                                "runs table's UNIQUE (environment, "
                                "script, test_name, start_time) is frozen "
                                "since migration 1 and cannot hold both "
                                "(docs/STREAMS_PLAN.md §3.2)".format(
                                    other, mine
                                )
                            ),
                            environment=rec.environment,
                            script=rec.script,
                            test_name=rec.test_name,
                            start_time=rec.start_time,
                        ))
                        continue
                    cursor = conn.execute(
                        "INSERT INTO runs (environment, script, test_name, "
                        "result, start_time, end_time, source_link, "
                        "known_failure_reason, output_fingerprint, "
                        "stream_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                            stream_id,
                        ),
                    )
                    run_id = int(cursor.lastrowid)
                    inserted += 1
                    key2 = (stream_id, rec.environment, hour, rec.result.value)
                    deltas[key2] = deltas.get(key2, 0) + 1
                    script_key = (
                        stream_id, rec.environment, hour, rec.script,
                        rec.result.value,
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
                        old_key = (
                            stream_id, rec.environment, hour, row[1]
                        )
                        new_key = (
                            stream_id, rec.environment, hour,
                            rec.result.value,
                        )
                        deltas[old_key] = deltas.get(old_key, 0) - 1
                        deltas[new_key] = deltas.get(new_key, 0) + 1
                    if row[1] != rec.result.value or row[2] != end:
                        # Either bucket may have shrunk; both are
                        # recomputed from `runs` once the batch's row
                        # writes are all in. When only the end time
                        # changed the two keys are the same key.
                        recompute.add((
                            stream_id, rec.environment, hour, rec.script,
                            row[1],
                        ))
                        recompute.add((
                            stream_id, rec.environment, hour, rec.script,
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
                self._maintain_latest(conn, rec, run_id, start, stream_id)
                if stream_id == MAINLINE_STREAM_ID:
                    self._unretire_on_new_run(conn, rec, start)
            self._apply_activity_deltas(conn, deltas)
            self._apply_script_hour_changes(conn, grown, recompute)
            # Widen streams.first_seen/last_seen once per touched stream,
            # not once per record. SELECT-then-UPDATE, not SQL MIN()/MAX()
            # with two arguments: that spelling is SQLite's own scalar
            # MIN/MAX (2+ args picks the smaller/larger of its arguments)
            # — MariaDB's MIN()/MAX() are aggregates-only and reject it
            # with a syntax error (LEAST()/GREATEST() is the MariaDB
            # spelling, and would be the wrong direction to unify on:
            # this module's portability rule is SELECT-then-UPDATE, the
            # same shape as :meth:`_apply_activity_deltas`, not a third
            # SQL dialect fork). Comparison is lexical on ISO-8601
            # strings, which sorts chronologically.
            for stream_id, (first, last) in sorted(stream_bounds.items()):
                row = conn.execute(
                    "SELECT first_seen, last_seen FROM streams "
                    "WHERE id = ?", (stream_id,),
                ).fetchone()
                new_first = first if first < row[0] else row[0]
                new_last = last if last > row[1] else row[1]
                if new_first != row[0] or new_last != row[1]:
                    conn.execute(
                        "UPDATE streams SET first_seen = ?, "
                        "last_seen = ? WHERE id = ?",
                        (new_first, new_last, stream_id),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if inserted or updated:
            # An all-unchanged push proved the memoized trend is still
            # true; clearing it would make the feeder's 10-minute no-op
            # re-push defeat the memo forever. Same reasoning for the
            # summary/watch memo (WP-23 "ONE MORE PERF SLICE").
            self._invalidate_trend_cache()
            self._invalidate_summary_cache()
        return UpsertCounts(
            inserted=inserted, updated=updated, unchanged=unchanged,
            rejections=rejections,
        )

    @staticmethod
    def _apply_activity_deltas(
        conn: sqlite3.Connection,
        deltas: Dict[Tuple[int, str, str, str], int],
    ) -> None:
        """Apply a batch's net ``activity_hours`` changes, exactly.

        Keyed by ``(stream_id, environment, hour, result)`` since
        migration 10 (WP-23): every stream is maintained, not only
        mainline's (the WP-21 skip is gone — see :meth:`upsert_runs`).
        SELECT-then-UPDATE-or-INSERT, per this module's portability rule
        (no ``ON CONFLICT DO UPDATE`` on 3.6's sqlite). Rows that reach
        zero are DELETEd, not kept: the invariant is byte equality with
        the ``GROUP BY`` over ``runs`` (see
        :func:`_rebuild_activity_hours_all_streams`), and a GROUP BY
        yields no zero-count groups.
        """
        for (stream_id, environment, hour, result), delta in sorted(
                deltas.items()):
            if delta == 0:
                continue
            row = conn.execute(
                "SELECT count FROM activity_hours WHERE stream_id = ? "
                "AND environment = ? AND hour = ? AND result = ?",
                (stream_id, environment, hour, result),
            ).fetchone()
            count = (0 if row is None else int(row[0])) + delta
            if count <= 0:
                conn.execute(
                    "DELETE FROM activity_hours WHERE stream_id = ? "
                    "AND environment = ? AND hour = ? AND result = ?",
                    (stream_id, environment, hour, result),
                )
            elif row is None:
                conn.execute(
                    "INSERT INTO activity_hours "
                    "(stream_id, environment, hour, result, count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (stream_id, environment, hour, result, count),
                )
            else:
                conn.execute(
                    "UPDATE activity_hours SET count = ? "
                    "WHERE stream_id = ? AND environment = ? "
                    "AND hour = ? AND result = ?",
                    (count, stream_id, environment, hour, result),
                )

    @staticmethod
    def _apply_script_hour_changes(
        conn: sqlite3.Connection,
        grown: Dict[Tuple[int, str, str, str, str], List[Any]],
        recompute: Set[Tuple[int, str, str, str, str]],
    ) -> None:
        """Apply a batch's net ``script_hours`` changes, exactly.

        Keyed by ``(stream_id, environment, hour, script, result)``
        since migration 10 (WP-23) — same widening as
        :meth:`_apply_activity_deltas`. Two kinds of change, because
        MIN/MAX cannot be decremented:

        - *grown* buckets only ever got bigger (inserted runs), so the
          stored row is merged with the batch's count/min/max — one
          SELECT-then-UPDATE-or-INSERT per touched bucket, same
          portability rule as :meth:`_apply_activity_deltas`.
        - *recompute* buckets may have SHRUNK (an update changed a
          stored result or end time), so they are re-derived from
          ``runs`` outright, filtered to THIS bucket's stream so a
          shrink on one stream can never borrow another stream's runs.
          The query is bounded by one script's index range and runs
          only when a re-import actually changed a stored row, which
          the fingerprint skip makes rare. A bucket both grown and
          recomputed is recomputed only — by the time this runs, the
          batch's inserts are already in ``runs``, so the recomputation
          already counts them.

        Rows whose count reaches zero are DELETEd: the invariant is
        equality with a GROUP BY over ``runs``, and a GROUP BY yields
        no empty groups.
        """
        for key in sorted(recompute):
            stream_id, environment, hour, script, result = key
            grown.pop(key, None)
            row = conn.execute(
                "SELECT COUNT(*), MIN(start_time), MAX(end_time) "
                "FROM runs WHERE environment = ? AND script = ? "
                "AND result = ? AND SUBSTR(start_time, 1, 13) = ? "
                "AND stream_id = ?",
                (environment, script, result, hour, stream_id),
            ).fetchone()
            count = int(row[0])
            conn.execute(
                "DELETE FROM script_hours WHERE stream_id = ? "
                "AND environment = ? AND hour = ? AND script = ? "
                "AND result = ?",
                (stream_id, environment, hour, script, result),
            )
            if count > 0:
                conn.execute(
                    "INSERT INTO script_hours (stream_id, environment, "
                    "hour, script, result, count, first_start, last_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (stream_id, environment, hour, script, result, count,
                     row[1], row[2]),
                )
        for key, growth in sorted(grown.items()):
            stream_id, environment, hour, script, result = key
            row = conn.execute(
                "SELECT count, first_start, last_end FROM script_hours "
                "WHERE stream_id = ? AND environment = ? AND hour = ? "
                "AND script = ? AND result = ?",
                (stream_id, environment, hour, script, result),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO script_hours (stream_id, environment, "
                    "hour, script, result, count, first_start, last_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (stream_id, environment, hour, script, result,
                     growth[0], growth[1], growth[2]),
                )
            else:
                conn.execute(
                    "UPDATE script_hours SET count = ?, first_start = ?, "
                    "last_end = ? WHERE stream_id = ? AND environment = ? "
                    "AND hour = ? AND script = ? AND result = ?",
                    (
                        int(row[0]) + growth[0],
                        min(row[1], growth[1]),
                        max(row[2], growth[2]),
                        stream_id, environment, hour, script, result,
                    ),
                )

    @staticmethod
    def _previous_result(
        conn: sqlite3.Connection,
        environment: str,
        script: str,
        test_name: str,
        before: str,
        stream_id: int,
    ) -> Optional[str]:
        """Result of the newest run of the triple older than *before*.

        Scoped to *stream_id* (WP-21): a branch's ``prev_result`` must
        derive from that branch's own history of the triple, never
        mainline's, or the delta view would inherit drift that never
        happened on the branch. One seek — the runs table's UNIQUE index
        has no stream column to lead with, but ``(environment, script,
        test_name, start_time)`` already narrows to a handful of rows at
        most, so filtering ``stream_id`` in the WHERE clause costs
        nothing that grows with the estate.
        """
        row = conn.execute(
            "SELECT result FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND start_time < ? AND stream_id = ? "
            "ORDER BY start_time DESC LIMIT 1",
            (environment, script, test_name, before, stream_id),
        ).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _maintain_latest(
        conn: sqlite3.Connection,
        rec: RunRecord,
        run_id: int,
        start: str,
        stream_id: int,
    ) -> None:
        """Keep ``latest_runs`` describing the test's newest run.

        Called inside the upsert transaction for every record, against
        *stream_id*'s partition of ``latest_runs`` (WP-21, migration 9:
        the PRIMARY KEY is ``(stream_id, environment, script,
        test_name)``). The row carries the newest run's ``result`` and
        the result of the run before it, both of which every estate-wide
        read depends on, so all four orderings a record can arrive in
        are handled explicitly:

        - first sighting of the triple ON THIS STREAM — insert, deriving
          ``prev_result`` from that stream's own runs (older runs on the
          SAME stream may already exist);
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
            "WHERE stream_id = ? AND environment = ? AND script = ? "
            "AND test_name = ?",
            (stream_id, rec.environment, rec.script, rec.test_name),
        ).fetchone()
        # Computed with the same function the API serialises with, so a
        # stored duration and a displayed one cannot disagree.
        duration = model.duration_seconds(rec.start_time, rec.end_time)
        if row is None:
            conn.execute(
                "INSERT INTO latest_runs (stream_id, environment, script, "
                "test_name, run_id, start_time, result, prev_result, "
                "duration_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stream_id,
                    rec.environment,
                    rec.script,
                    rec.test_name,
                    run_id,
                    start,
                    rec.result.value,
                    Storage._previous_result(
                        conn, rec.environment, rec.script, rec.test_name,
                        start, stream_id,
                    ),
                    duration,
                ),
            )
            return

        latest_start, latest_result = row[0], row[1]
        key = (stream_id, rec.environment, rec.script, rec.test_name)
        where = (
            "WHERE stream_id = ? AND environment = ? AND script = ? "
            "AND test_name = ?"
        )
        if start > latest_start:
            conn.execute(
                "UPDATE latest_runs SET run_id = ?, start_time = ?, "
                "result = ?, prev_result = ?, duration_seconds = ? "
                + where,
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
                "duration_seconds = ? " + where,
                (run_id, rec.result.value, duration) + key,
            )
        else:
            conn.execute(
                "UPDATE latest_runs SET prev_result = ? " + where,
                (
                    Storage._previous_result(
                        conn, rec.environment, rec.script, rec.test_name,
                        latest_start, stream_id,
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
        stream_id: int = MAINLINE_STREAM_ID,
        assignment_origin: Optional[str] = None,
        assigned_only: bool = False,
        open_items: bool = False,
    ) -> Tuple[List[str], List[Any]]:
        """Build the shared WHERE clauses for the dashboard list and count.

        *include_retired* keeps tests approved as no longer in the suite;
        by default they are hidden, which is the whole point of retiring
        one. *assignees* and *include_unassigned* combine as OR — "show
        me Alice's and Bob's open items, plus anything nobody owns".
        *assigned_only* (2026-08-10, found in the first morning of
        build-verify testing) is an AND-level "ca.assignee IS NOT NULL"
        — the filter behind Open Actions' "All assigned" view. It exists
        because an assignment on a mainline-PASSING test was previously
        visible NOWHERE: every queue predicate and all three of the
        page's result options gate on FAIL/UNEXPECTED_PASS, an
        assumption from mainline triage ("assigned" implied "because it
        is failing") that the build-verify flow broke — a test assigned
        to investigate why it did not run on an RC passes happily on
        mainline. AND-level deliberately, not part of the OR group:
        combined with *assignees* it narrows ("Alice's assignments, any
        result"); combined with *include_unassigned* it is contradictory
        and correctly returns nothing (the frontend does not offer that
        combination).
        *open_items* (2026-08-10, same morning, the user's refinement
        of the first cut) is Open Actions' DEFAULT view, "needs
        action": failing, stale annotation, OR currently assigned to
        someone — an assignment IS an open action whatever its result,
        which is what the page's name always claimed. Server-side
        because this OR spans the result axis and the owner axis,
        which no combination of the AND-composed params here can
        express. The first cut was a separate fourth Result option
        ("All assigned"); the user's verdict — one more mode adds
        confusion, the default should simply not lie — replaced it the
        same morning, before anything shipped.
        *environments* is the WP-20 product filter, resolved by the
        caller to an allow-list — see :meth:`_environments_clause`. It
        combines with *environment* by AND, which is never contradictory
        in practice: a caller passes one or the other, never both with
        different values. *stream_id* (WP-21, default mainline) is
        ALWAYS bound: every dashboard read is scoped to exactly one
        stream's partition of ``latest_runs`` — the leading column of
        every WP-21 index (see migration 9), so this equality predicate
        is what keeps the sort indexes usable rather than forcing a
        TEMP B-TREE. *assignment_origin* (WP-21, ``"build"`` or
        ``"mainline"``, default no filter; the non-mainline value was
        spelled ``"branch"`` until WP-25 collapsed the stream kinds —
        renamed before anything shipped) is Open Actions' origin
        filter — WHERE the CURRENT assignment was made from, an
        entirely different axis from *stream_id* above (which scopes
        the test's OWN result, not who assigned it or from where) —
        server-side by the same rule the *assignees* filter already
        follows (a client-side filter over one paged fetch would turn
        "every build-originated item" into "the build-originated ones
        that happen to be on this page").
        """
        clauses = ["lr.stream_id = ?"]  # type: List[str]
        params = [stream_id]  # type: List[Any]
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

        if assigned_only:
            clauses.append("ca.assignee IS NOT NULL")

        if open_items:
            clauses.append(
                "(lr.result IN (?, ?) OR ca.assignee IS NOT NULL)"
            )
            params.extend(
                [Result.FAIL.value, Result.UNEXPECTED_PASS.value])

        if assignment_origin == "build":
            clauses.append("(ca.stream_id IS NOT NULL AND ca.stream_id != ?)")
            params.append(MAINLINE_STREAM_ID)
        elif assignment_origin == "mainline":
            clauses.append("(ca.stream_id IS NULL OR ca.stream_id = ?)")
            params.append(MAINLINE_STREAM_ID)
        elif assignment_origin is not None:
            raise ValueError(
                "assignment_origin must be 'build', 'mainline' or None, "
                "got {!r}".format(assignment_origin)
            )
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
        stream_id: int = MAINLINE_STREAM_ID,
        assignment_origin: Optional[str] = None,
        assigned_only: bool = False,
        open_items: bool = False,
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
        screen. *stream_id* (WP-21, default mainline) selects one
        partition of ``latest_runs`` — the ``/api/dashboard`` ``stream=``
        param, resolved by the caller. *assignment_origin* (WP-21,
        Open Actions only) narrows by WHERE the current assignment was
        made from — see :meth:`_dashboard_filters`. *assigned_only*
        (2026-08-10) keeps only rows with a current assignee, whatever
        their result; *open_items* (same day) is the "needs action"
        composite — failing, stale annotation, or assigned — see
        :meth:`_dashboard_filters` for why both exist.

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
            stream_id, assignment_origin, assigned_only, open_items,
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
        stream_id: int = MAINLINE_STREAM_ID,
        assignment_origin: Optional[str] = None,
        assigned_only: bool = False,
        open_items: bool = False,
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
            stream_id, assignment_origin, assigned_only, open_items,
        )
        sql = "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._conn().execute(sql, params).fetchone()
        return int(row[0])

    def environments(
        self, environments: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Return every distinct environment with a recorded test, sorted.

        Mainline only (WP-21): this feeds the estate-wide environment
        picker, and a branch importing a brand-new environment name must
        not make it appear estate-wide before mainline has ever reported
        it — the same reasoning as :meth:`summary_rollup` and every
        other estate view (docs/STREAMS_PLAN.md §1, "estate views read
        stream_id = 1").

        *environments* (WP-23 fix) is the WP-20 ``product=`` allow-list,
        applied here the same way :meth:`dashboard` already applies it —
        ``None`` means no filter (every deployed caller before this fix
        saw exactly that, so this is additive). Its absence was the bug:
        ``/api/summary?product=Atlas`` and ``?product=Beacon`` returned
        the IDENTICAL environments list, because this method never
        looked at product scope at all — found live, "both products
        seem to have the same envs".

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`) -- cheap per call (single-digit ms) but
        called on every ``/api/summary`` load regardless of ``parts=``,
        so it is part of the fixed per-request floor the memo targets.
        """
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = ("environments", envs_key)
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        clause, clause_params = self._environments_clause(
            environments, column="environment"
        )
        sql = (
            "SELECT DISTINCT environment FROM latest_runs "
            "WHERE stream_id = ?"
        )
        params = [MAINLINE_STREAM_ID] + clause_params  # type: List[Any]
        if clause is not None:
            sql += " AND " + clause
        sql += " ORDER BY environment"
        rows = self._conn().execute(sql, params).fetchall()
        result = [row[0] for row in rows]
        self._store_summary(key, result)
        return result

    def scripts(
        self,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Return every distinct script name (optionally in one env), sorted.

        Mainline only (WP-21) — see :meth:`environments`. *environments*
        (WP-23 fix) is the same product allow-list :meth:`environments`
        takes, combined with *environment* by AND — never contradictory
        in practice, since a caller passes an exact environment or a
        product scope, not conflicting values for both.

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`) -- same reasoning as :meth:`environments`.
        """
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = ("scripts", environment, envs_key)
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        clause, clause_params = self._environments_clause(
            environments, column="environment"
        )
        sql = "SELECT DISTINCT script FROM latest_runs WHERE stream_id = ?"
        params = [MAINLINE_STREAM_ID]  # type: List[Any]
        if environment is not None:
            sql += " AND environment = ?"
            params.append(environment)
        if clause is not None:
            sql += " AND " + clause
            params.extend(clause_params)
        sql += " ORDER BY script"
        result = [row[0] for row in self._conn().execute(sql, params)]
        self._store_summary(key, result)
        return result

    def assignees(self) -> List[str]:
        """Return every user who currently owns at least one test, sorted."""
        rows = self._conn().execute(
            "SELECT DISTINCT assignee FROM current_assignments "
            "WHERE assignee IS NOT NULL ORDER BY assignee"
        ).fetchall()
        return [row[0] for row in rows]

    def assignment_stream_ids(self) -> List[int]:
        """Every DISTINCT non-mainline stream currently annotating an
        assignment (WP-21, docs/STREAMS_PLAN.md §3.6), sorted.

        Open Actions' build/mainline origin filter needs a real,
        estate-wide existence check to honour "zero visible change when
        no assignment carries a stream" — a check built only from
        whichever page of rows happened to be fetched would flicker the
        filter in and out as someone pages or re-filters. Mirrors
        :meth:`assignees`' shape exactly, which the same page's owner
        filter already reads the same way.
        """
        rows = self._conn().execute(
            "SELECT DISTINCT stream_id FROM current_assignments "
            "WHERE stream_id IS NOT NULL AND stream_id != ? "
            "ORDER BY stream_id",
            (MAINLINE_STREAM_ID,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def summary_rollup(
        self,
        recent_cutoff: datetime.datetime,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[RollupCount]:
        """Group one stream's estate by environment, result, previous result.

        Returns a few dozen :class:`RollupCount` cells — one GROUP BY over
        ``latest_runs`` — from which
        :func:`testboard.analytics.summarize_rollup` derives every
        headline number. A test counts as having run recently when its
        latest run started at or after *recent_cutoff*. *environments* is
        the WP-20 ``product=`` filter — see :meth:`_environments_clause`.
        *stream_id* (WP-23, default mainline): ``/api/summary`` was
        mainline-only through WP-21/22 (docs/STREAMS_PLAN.md §3.5); WP-23
        gives a long-running branch its OWN headline tiles by scoping
        this the same way :meth:`dashboard` already scopes its rows —
        ``/api/summary`` itself stays mainline by default (unchanged
        behaviour for every existing caller), but the "own results" tab
        reads this with the branch's stream id.

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`), keyed on the EXACT *recent_cutoff*
        value -- never rounded or truncated to an hour the way the trend
        cache's ``since`` is, because *recent_cutoff* is reported
        verbatim as ``stale_before`` and gates every ``recent``-column
        count; two different real cutoffs sharing one cache slot would
        serve counts computed for the wrong window. This is safe rather
        than merely correct-but-useless: on a healthy estate
        *recent_cutoff* is data-derived (the start of a covered pass)
        and stable across nearby requests, so repeat loads still hit; on
        a sparse estate the 36h wall-clock fallback moves every request
        and the memo quietly stops helping -- it helps exactly when
        there is enough data for the number to matter. ``/api/watch``
        computes this SAME (mainline, unscoped) cutoff for its own
        ``summary_rollup`` call (see ``_handle_watch``), so the two
        endpoints share one cache entry on the common unscoped load.
        """
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = (
            "summary_rollup", recent_cutoff, environment, envs_key,
            stream_id,
        )
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
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
        where = ["lr.stream_id = ?"]  # type: List[str]
        params.append(stream_id)
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
        result = [
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
        self._store_summary(key, result)
        return result

    def assigned_open_count(
        self,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> int:
        """Count tests that have an assignee and are FAIL or UNEXPECTED_PASS.

        *stream_id* (WP-23, default mainline) — see :meth:`summary_rollup`.
        Assignment itself is never partitioned by stream (it is an
        annotation on the triple, docs/STREAMS_PLAN.md §0.4), so this
        still reads the ONE current assignee; what changes is which
        stream's ``latest_runs`` partition supplies the FAIL/
        UNEXPECTED_PASS result the count is gated on.
        """
        sql = (
            "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN + " WHERE "
            + _QUEUE_PREDICATES["assigned"] + " AND " + self._NOT_RETIRED
            + " AND lr.stream_id = ?"
        )
        params = [stream_id]  # type: List[Any]
        if environment is not None:
            sql += " AND lr.environment = ?"
            params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            sql += " AND " + envs_clause
            params.extend(envs_params)
        return int(self._conn().execute(sql, params).fetchone()[0])

    def activity_buckets(
        self,
        since: datetime.datetime,
        stream_id: int = MAINLINE_STREAM_ID,
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

        *stream_id* (WP-23, migration 10, default mainline): every
        estate-wide caller before this drop implicitly read mainline
        because the table held nothing else; now that every stream is
        maintained, an unfiltered read would silently MIX a branch's
        activity into mainline's own pass detection the moment a branch
        reports against the same environment. This is the fix for that
        latent cross-stream leak (docs/STREAMS_PLAN.md §5.1/§5.2) as
        much as it is the "own results" tab's own scoping mechanism —
        the parameter is the same one either way.
        """
        rows = self._conn().execute(
            "SELECT environment, hour, SUM(count) FROM activity_hours "
            "WHERE stream_id = ? AND hour >= ? "
            "GROUP BY environment, hour ORDER BY environment, hour",
            (stream_id, model.format_iso(since)[:13]),
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[ScriptHourBucket]:
        """Every ``script_hours`` bucket for one environment's window.

        Feeds :func:`analytics.group_script_executions`, which turns
        these into the Timeline's per-script execution rows. Ordered by
        (script, hour) — the order the grouping walks in.

        Read from ``script_hours`` (migration 7), never from ``runs``:
        the window is one block of hours for one environment, and the
        PRIMARY KEY leads (stream_id, environment, hour), so filtering
        *stream_id* (WP-23, default mainline — the Timeline's own
        ``stream=`` param) keeps this a pure index range over a table
        that grows like streams x scripts x active hours, rather than
        mixing a branch's script executions into mainline's Timeline the
        moment it reports against the same environment. Both edges are
        quantised to their hour, which can only WIDEN the window — the
        caller trims executions to the exact block, and an extra
        boundary bucket cannot invent one (block edges are, by
        construction, more than the execution gap apart).
        """
        rows = self._conn().execute(
            "SELECT script, hour, result, count, first_start, last_end "
            "FROM script_hours WHERE stream_id = ? AND environment = ? "
            "AND hour >= ? AND hour <= ? ORDER BY script, hour",
            (
                stream_id,
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

    def script_test_counts(
        self,
        environment: str,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Dict[str, int]:
        """How many tests each of an environment's scripts has, INFERRED.

        The "of 45" in the Timeline's "ran 41 of 45 known tests" — what
        makes a partial run of a script visible as one. One row per
        test via ``latest_runs`` (an index range on the PRIMARY KEY,
        which leads with ``stream_id, environment``), excluding retired
        tests for the usual reason: they are not in the suite, so a run
        that skips them has not missed anything.

        *stream_id* (WP-23, migration 10, default mainline): before this
        parameter existed, this always read EVERY stream's rows for the
        triple (``latest_runs`` holds one row per test PER STREAM since
        migration 9), so a branch reporting against the same environment
        silently inflated mainline's own "known tests" denominator —
        found by the WP-23 sweep for cross-stream leaks
        (docs/STREAMS_PLAN.md §5.1/§5.2), not by a plan line naming this
        method specifically.

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
            "WHERE lr.stream_id = ? AND lr.environment = ? AND "
            + self._NOT_RETIRED + " GROUP BY lr.script",
            (stream_id, environment),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def test_counts_by_environment(
        self, stream_id: int = MAINLINE_STREAM_ID
    ) -> Dict[str, int]:
        """How many tests each environment currently has, INFERRED.

        The denominator for "did that block of activity actually cover
        this environment, or was it a handful of re-runs after a fix".

        Retired tests are excluded, for the reason they are excluded from
        every other estate view: they are not in the suite, so a pass
        that does not run them has not missed anything. Counting them
        inflates the denominator, which makes real passes fail the
        coverage test — and a failed coverage test is SILENT, dropping
        the cutoff back to the wall clock with nothing to see.

        *stream_id* (WP-23, migration 10, default mainline) — see
        :meth:`script_test_counts` for why this parameter exists: an
        unfiltered read here counted every stream's copy of a test
        towards the SAME environment's denominator, which is the same
        latent cross-stream leak, one level up.

        Even so this is a high-water mark: a test that quietly stopped
        being run and was never retired stays here forever. That is what
        :meth:`declared_test_counts` exists to override.

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`) -- this is the ``_pass_view`` input
        both ``/api/summary`` and ``/api/watch`` fetch on every request,
        for the SAME (mainline, unscoped) key on the common unscoped
        load, so one computation serves both endpoints.
        """
        key = ("test_counts_by_environment", stream_id)
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        rows = self._conn().execute(
            "SELECT lr.environment, COUNT(*) FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script "
            " AND tr.test_name = lr.test_name "
            "WHERE lr.stream_id = ? AND " + self._NOT_RETIRED
            + " GROUP BY lr.environment",
            (stream_id,),
        ).fetchall()
        result = {row[0]: int(row[1]) for row in rows}
        self._store_summary(key, result)
        return result

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

    def product_for_environment(self, environment: str) -> str:
        """One environment's declared product, or ``""`` if unmapped
        (the implicit grouping — same reading as everywhere else in
        this feature). A single-row lookup for callers that need
        exactly one environment's product (WP-22's per-triple streams
        endpoint) rather than the whole map :meth:`environment_products_map`
        returns for callers that need every environment's.
        """
        row = self._conn().execute(
            "SELECT product FROM environment_products WHERE environment = ?",
            (environment,),
        ).fetchone()
        return "" if row is None else row[0]

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

    # ------------------------------------------------------------------
    # Streams (migration 9, WP-21) — observed build/mainline runs (the
    # 'branch' kind died before it ever shipped; see migration 9's own
    # comment and docs/ONE_KIND_PLAN.md, WP-25)
    # ------------------------------------------------------------------

    def _stream_from_row(self, row: Sequence[Any]) -> "Stream":
        """Build a :class:`Stream` from a ``streams`` row plus its own
        failing count (one small indexed query)."""
        stream_id = int(row[0])
        failing_row = self._conn().execute(
            "SELECT COUNT(*) FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE lr.stream_id = ? AND lr.result = ? AND " + self._NOT_RETIRED,
            (stream_id, Result.FAIL.value),
        ).fetchone()
        return Stream(
            stream_id=stream_id,
            product=row[1],
            kind=row[2],
            name=row[3],
            first_seen=model.parse_iso(row[4]),
            last_seen=model.parse_iso(row[5]),
            failing=int(failing_row[0]),
        )

    def get_stream(self, stream_id: int) -> Optional["Stream"]:
        """One stream by id, or None. See :class:`Stream`."""
        row = self._conn().execute(
            "SELECT id, product, kind, name, first_seen, last_seen "
            "FROM streams WHERE id = ?",
            (stream_id,),
        ).fetchone()
        if row is None:
            return None
        return self._stream_from_row(row)

    def environments_for_stream(self, stream_id: int) -> List[str]:
        """Every environment *stream_id* has at least one run on,
        sorted (docs/ONE_KIND_PLAN.md §2b.1, WP-25).

        A build that ran on one environment shows a bare empty page on
        every OTHER environment's Time/Timeline — the data is honest
        (the build never ran there), the page was not (it said nothing
        about where the data actually is). This is that "where" query:
        ONE grouped read over *this stream's own* ``latest_runs``
        partition — O(the partition), never O(the estate), the same
        discipline every other stream read here follows. Called only
        when the caller already knows it is scoped away from mainline
        and the current view came back empty; a page with data never
        pays for it.
        """
        rows = self._conn().execute(
            "SELECT DISTINCT environment FROM latest_runs "
            "WHERE stream_id = ? ORDER BY environment",
            (stream_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def list_streams(self, product: str) -> List["Stream"]:
        """Every non-mainline stream of *product*, id ascending.

        The Build picker's data (``GET /api/streams?product=``,
        docs/STREAMS_PLAN.md §3.5): id, kind, name, first_seen, last_seen
        and a cheap latest verdict (failing count) — no hidden staleness
        constant, the picker folds by age FROM the reported ``last_seen``.
        Mainline (id 1) is never listed here; it is not a "build" a
        tester picks, it is the default.
        """
        rows = self._conn().execute(
            "SELECT id, product, kind, name, first_seen, last_seen "
            "FROM streams WHERE product = ? AND id != ? ORDER BY id",
            (product, MAINLINE_STREAM_ID),
        ).fetchall()
        return [self._stream_from_row(row) for row in rows]

    def count_stream_rows(self, stream_id: int) -> Dict[str, int]:
        """Rows :meth:`delete_stream` would delete, per table (dry run)."""
        conn = self._conn()
        counts = {}  # type: Dict[str, int]
        counts["runs"] = int(conn.execute(
            "SELECT COUNT(*) FROM runs WHERE stream_id = ?", (stream_id,)
        ).fetchone()[0])
        counts["run_outputs"] = int(conn.execute(
            "SELECT COUNT(*) FROM run_outputs WHERE run_id IN "
            "(SELECT id FROM runs WHERE stream_id = ?)", (stream_id,)
        ).fetchone()[0])
        counts["latest_runs"] = int(conn.execute(
            "SELECT COUNT(*) FROM latest_runs WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()[0])
        # WP-23, migration 10: these two are now maintained per stream
        # too, so a stream drop must clear its own partition of them or
        # the "derived == GROUP BY over runs" invariant rots silently
        # the moment its runs are gone but its hour buckets are not.
        counts["activity_hours"] = int(conn.execute(
            "SELECT COUNT(*) FROM activity_hours WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()[0])
        counts["script_hours"] = int(conn.execute(
            "SELECT COUNT(*) FROM script_hours WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()[0])
        return counts

    def delete_stream(self, stream_id: int) -> Dict[str, int]:
        """Delete a stream and everything belonging to it. Cannot be undone.

        Refuses stream 1 (mainline) — the caller (``tools/drop_stream.py``)
        must never be able to remove it. Comments posted from this stream
        are NOT deleted: they annotate the (environment, script,
        test_name) triple, not the stream. ``comments.stream_id`` is
        cleared with an explicit UPDATE, in the same transaction, rather
        than relied on SQLite's ``ON DELETE SET NULL``: the MariaDB
        schema declares no foreign keys at all (runbook §B.6), so an FK
        action would be silently a no-op there and the two backends
        would disagree about a comment's tag after a delete — the
        explicit UPDATE is identical on both (docs/STREAMS_PLAN.md
        §3.8).

        One transaction: outputs before their runs, ``latest_runs``
        before ``runs`` (derived rows before the rows they were derived
        from — the same ordering :meth:`delete_environment` uses).
        """
        if stream_id == MAINLINE_STREAM_ID:
            raise ValueError("refusing to delete the mainline stream")
        conn = self._conn()
        deleted = {}  # type: Dict[str, int]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE comments SET stream_id = NULL WHERE stream_id = ?",
                (stream_id,),
            )
            cursor = conn.execute(
                "DELETE FROM run_outputs WHERE run_id IN "
                "(SELECT id FROM runs WHERE stream_id = ?)", (stream_id,)
            )
            deleted["run_outputs"] = int(cursor.rowcount)
            cursor = conn.execute(
                "DELETE FROM latest_runs WHERE stream_id = ?", (stream_id,)
            )
            deleted["latest_runs"] = int(cursor.rowcount)
            # WP-23, migration 10: this stream's own hour-table
            # partitions, deleted before `runs` for the same "derived
            # rows before the rows they were derived from" ordering.
            cursor = conn.execute(
                "DELETE FROM activity_hours WHERE stream_id = ?",
                (stream_id,),
            )
            deleted["activity_hours"] = int(cursor.rowcount)
            cursor = conn.execute(
                "DELETE FROM script_hours WHERE stream_id = ?",
                (stream_id,),
            )
            deleted["script_hours"] = int(cursor.rowcount)
            cursor = conn.execute(
                "DELETE FROM runs WHERE stream_id = ?", (stream_id,)
            )
            deleted["runs"] = int(cursor.rowcount)
            cursor = conn.execute(
                "DELETE FROM streams WHERE id = ?", (stream_id,)
            )
            deleted["streams"] = int(cursor.rowcount)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self._invalidate_trend_cache()
        self._invalidate_summary_cache()
        return deleted

    #: Categories a comparison classifies every test into — the "five
    #: See module-level :data:`COMPARE_CATEGORIES`.
    _COMPARE_CATEGORIES = COMPARE_CATEGORIES

    @classmethod
    def _compare_pairs_sql(
        cls, stream_id: int, baseline_id: int, environments: Sequence[str],
    ) -> Tuple[str, List[Any]]:
        """Full outer join of two ``latest_runs`` partitions on the triple.

        SQLite has no ``FULL OUTER JOIN``: this is the standard
        emulation — a LEFT JOIN stream->baseline (every triple on the
        stream, with the baseline's result or NULL), UNION ALL a LEFT
        JOIN baseline->stream restricted to the ANTI-JOIN complement
        (triples on the baseline the stream has nothing for, which the
        first half already excluded from double-counting). Columns are
        named consistently across both halves — ``stream_result``,
        ``baseline_result``, ``stream_run_id``, ``stream_start_time`` —
        never swapped, so the caller's CASE expression means the same
        thing for every row. ``stream_run_id``/``stream_start_time`` are
        ALWAYS the stream side's own run (``s.*``/``s2.*`` respectively,
        never the baseline's), by the same invariant.

        WP-23 perf pass: each side joins ``latest_runs`` to ITSELF
        directly on ``(stream_id, environment, script, test_name)`` —
        its PRIMARY KEY — rather than through a materialized
        ``(SELECT ... FROM latest_runs WHERE stream_id = ?)`` derived
        table as this used to (via the now-removed
        ``_compare_partition_sql``). A derived table has no index of
        its own, so SQLite could only nested-loop the two ~2k-row
        partitions against each other; joining the real table lets the
        planner SEARCH it by its PK for every outer row instead — the
        same shape :meth:`compare_counts_many` (the Watchlist's batched
        path) already used, which is why that path was never slow.
        MEASURED on the dev-scale seeded copy (220 MB; build 2026.9.1 =
        2036 latest rows, build 2026.9.0 = 2036), `?stream=5&baseline=4`:
        498ms -> single digits (see the commit message for the exact
        before/after and `tests/test_sql_portability.py` for the pinned
        query plan).

        Retirement now has to be applied to EACH side independently
        inside the join rather than once per pre-filtered partition:
        the anchor side (``s``/``m2``) is filtered in the WHERE exactly
        as before (a retired anchor row is excluded outright — it was
        never a partition member); the JOINED side (``m``/``s2``) is
        instead NULLED via a ``CASE`` when its own retirement row
        exists, which is what makes a retired triple on that side read
        as "not there" — identical to how the old derived-table
        partition simply omitted it. This is why the anti-join
        complement in the second half tests
        ``s2.result IS NULL OR tr_s2.retired_at IS NOT NULL`` rather
        than the single ``s2.result IS NULL`` the derived-table version
        used: a retired ``s2`` match must count as absent, and without
        the retirement half of that OR it would not.
        """
        envs_clause, envs_params = Storage._environments_clause(
            environments, column="s.environment"
        )
        envs_clause_m2, envs_params_m2 = Storage._environments_clause(
            environments, column="m2.environment"
        )
        pairs_sql = (
            "SELECT s.environment AS environment, s.script AS script, "
            "s.test_name AS test_name, s.result AS stream_result, "
            "CASE WHEN tr_m.retired_at IS NOT NULL THEN NULL "
            " ELSE m.result END AS baseline_result, "
            "s.run_id AS stream_run_id, s.start_time AS stream_start_time "
            "FROM latest_runs s "
            "LEFT JOIN test_retirements tr_s "
            "  ON tr_s.environment = s.environment "
            " AND tr_s.script = s.script AND tr_s.test_name = s.test_name "
            "LEFT JOIN latest_runs m "
            "  ON m.stream_id = ? AND m.environment = s.environment "
            " AND m.script = s.script AND m.test_name = s.test_name "
            "LEFT JOIN test_retirements tr_m "
            "  ON tr_m.environment = m.environment "
            " AND tr_m.script = m.script AND tr_m.test_name = m.test_name "
            "WHERE s.stream_id = ? AND tr_s.retired_at IS NULL"
        )
        if envs_clause is not None:
            pairs_sql += " AND " + envs_clause
        pairs_sql += (
            " UNION ALL "
            "SELECT m2.environment AS environment, m2.script AS script, "
            "m2.test_name AS test_name, "
            "CASE WHEN tr_s2.retired_at IS NOT NULL THEN NULL "
            " ELSE s2.result END AS stream_result, "
            "m2.result AS baseline_result, "
            "CASE WHEN tr_s2.retired_at IS NOT NULL THEN NULL "
            " ELSE s2.run_id END AS stream_run_id, "
            "CASE WHEN tr_s2.retired_at IS NOT NULL THEN NULL "
            " ELSE s2.start_time END AS stream_start_time "
            "FROM latest_runs m2 "
            "LEFT JOIN test_retirements tr_m2 "
            "  ON tr_m2.environment = m2.environment "
            " AND tr_m2.script = m2.script AND tr_m2.test_name = m2.test_name "
            "LEFT JOIN latest_runs s2 "
            "  ON s2.stream_id = ? AND s2.environment = m2.environment "
            " AND s2.script = m2.script AND s2.test_name = m2.test_name "
            "LEFT JOIN test_retirements tr_s2 "
            "  ON tr_s2.environment = s2.environment "
            " AND tr_s2.script = s2.script AND tr_s2.test_name = s2.test_name "
            "WHERE m2.stream_id = ? AND tr_m2.retired_at IS NULL "
            "AND (s2.result IS NULL OR tr_s2.retired_at IS NOT NULL)"
        )
        if envs_clause_m2 is not None:
            pairs_sql += " AND " + envs_clause_m2
        params = (
            [baseline_id, stream_id] + envs_params
            + [stream_id, baseline_id] + envs_params_m2
        )
        return pairs_sql, params

    #: The classification every comparison row falls into. ``{fail}`` is
    #: substituted once at import time, the same pattern as
    #: :data:`_QUEUE_PREDICATES` — bound to the enum, not a hand-written
    #: literal, so a renamed Result member cannot silently match nothing.
    _COMPARE_CASE = (
        "CASE "
        "WHEN stream_result IS NULL THEN 'no_result' "
        "WHEN baseline_result IS NULL THEN 'new_tests' "
        "WHEN stream_result = '{fail}' AND baseline_result = '{fail}' "
        "  THEN 'both_failing' "
        "WHEN stream_result = '{fail}' THEN 'new_failures' "
        "WHEN baseline_result = '{fail}' THEN 'new_passes' "
        "ELSE 'agree' END"
    ).format(fail=Result.FAIL.value)

    def compare_counts(
        self, stream_id: int, baseline_id: int = MAINLINE_STREAM_ID,
    ) -> "CompareCounts":
        """The five headline counts of a stream-vs-baseline comparison.

        Raises :class:`KeyError` if *stream_id* does not exist. Two
        joins of two ``latest_runs`` partitions (see
        :meth:`_compare_pairs_sql`) — never a scan of ``runs``, and
        bounded by the stream's own product's test count (a few
        thousand to ~12k), not by history.
        """
        stream = self.get_stream(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        environments = self.environments_for_product(stream.product)
        pairs_sql, params = self._compare_pairs_sql(
            stream_id, baseline_id, environments
        )
        # The derived-table alias ("pairs") is required by MariaDB
        # ("Every derived table must have its own alias") though SQLite
        # does not need one; harmless there.
        sql = (
            "SELECT {0} AS category, COUNT(*) FROM ({1}) pairs "
            "GROUP BY category"
        ).format(self._COMPARE_CASE, pairs_sql)
        rows = self._conn().execute(sql, params).fetchall()
        counts = {row[0]: int(row[1]) for row in rows}
        return CompareCounts(
            new_failures=counts.get("new_failures", 0),
            new_passes=counts.get("new_passes", 0),
            both_failing=counts.get("both_failing", 0),
            new_tests=counts.get("new_tests", 0),
            no_result=counts.get("no_result", 0),
            agree=counts.get("agree", 0),
        )

    def compare_category(
        self,
        stream_id: int,
        category: str,
        baseline_id: int = MAINLINE_STREAM_ID,
        limit: int = 250,
        offset: int = 0,
    ) -> List["CompareRow"]:
        """ONE PAGE of one comparison category, paginated in SQL.

        *category* must be one of :data:`_COMPARE_CATEGORIES` — like
        :data:`DASHBOARD_SORTS`, callers choose from a whitelist and
        anything else is refused before it reaches the database (a
        GROUP BY/CASE label cannot be a bound parameter safely combined
        with pagination logic here, and there is no reason to allow
        one that means nothing).
        """
        if category not in self._COMPARE_CATEGORIES:
            raise ValueError(
                "category must be one of {0}, got {1!r}".format(
                    self._COMPARE_CATEGORIES, category
                )
            )
        stream = self.get_stream(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        environments = self.environments_for_product(stream.product)
        pairs_sql, params = self._compare_pairs_sql(
            stream_id, baseline_id, environments
        )
        # current_assignments is joined on the FINAL PAGE only (after
        # categorization/pagination) — bounded by `limit`, the same
        # shape as every other page-only join in this module (e.g. the
        # dashboard's `ca` join), not a cost that grows with the
        # comparison's size.
        sql = (
            "SELECT categorized.environment, categorized.script, "
            "categorized.test_name, categorized.stream_result, "
            "categorized.baseline_result, categorized.stream_run_id, "
            "categorized.stream_start_time, ca.assignee "
            "FROM (SELECT environment, script, test_name, "
            "stream_result, baseline_result, stream_run_id, "
            "stream_start_time, {0} AS category "
            "FROM ({1}) pairs) categorized "
            "LEFT JOIN current_assignments ca "
            "  ON ca.environment = categorized.environment "
            " AND ca.script = categorized.script "
            " AND ca.test_name = categorized.test_name "
            "WHERE categorized.category = ? "
            "ORDER BY categorized.environment, categorized.script, "
            "categorized.test_name "
            "LIMIT ? OFFSET ?"
        ).format(self._COMPARE_CASE, pairs_sql)
        rows = self._conn().execute(
            sql, params + [category, limit, offset]
        ).fetchall()
        return [
            CompareRow(
                environment=row[0],
                script=row[1],
                test_name=row[2],
                stream_result=None if row[3] is None else Result(row[3]),
                baseline_result=None if row[4] is None else Result(row[4]),
                stream_run_id=None if row[5] is None else int(row[5]),
                stream_start_time=(
                    None if row[6] is None else model.parse_iso(row[6])
                ),
                assignee=row[7],
            )
            for row in rows
        ]

    def compare_category_count(
        self,
        stream_id: int,
        category: str,
        baseline_id: int = MAINLINE_STREAM_ID,
    ) -> int:
        """Exact size of one comparison category, ignoring any display cap."""
        if category not in self._COMPARE_CATEGORIES:
            raise ValueError(
                "category must be one of {0}, got {1!r}".format(
                    self._COMPARE_CATEGORIES, category
                )
            )
        stream = self.get_stream(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        environments = self.environments_for_product(stream.product)
        pairs_sql, params = self._compare_pairs_sql(
            stream_id, baseline_id, environments
        )
        sql = (
            "SELECT COUNT(*) FROM (SELECT {0} AS category FROM ({1}) "
            "pairs) categorized WHERE category = ?"
        ).format(self._COMPARE_CASE, pairs_sql)
        row = self._conn().execute(
            sql, params + [category]
        ).fetchone()
        return int(row[0])

    def compare_counts_many(
        self, stream_environments: Dict[int, Sequence[str]],
        baselines: Optional[Dict[int, int]] = None,
    ) -> Dict[int, "CompareCounts"]:
        """Compare N streams against their baselines, in ONE query total.

        For the Watchlist's ``s:`` cards (docs/STREAMS_PLAN.md §3.6):
        ``/api/watch`` already fetches every OTHER card's data in O(1)
        queries regardless of card count (``tests/test_api.py::TestWatch
        ::test_query_count_does_not_grow_with_card_count``), and this is
        what keeps stream cards honouring the same property instead of
        costing one :meth:`compare_counts` call per card.

        *stream_environments* is ``{stream_id: [environments of that
        stream's own product]}``, precomputed by the CALLER from data it
        already holds (``environment_products_map()``, one query
        ``/api/watch`` already makes) — this method makes no further
        query to resolve it, which is what keeps the total flat.
        Streams that resolved to no environments (an undeclared product)
        get an all-zero :class:`CompareCounts`.

        *baselines* is ``{stream_id: baseline_stream_id}`` (WP-22,
        docs/STREAMS_PLAN.md §4.1: a build's default baseline is its
        predecessor build, not mainline, when one exists — see
        :meth:`previous_builds`). A stream absent from *baselines*, or
        the argument omitted entirely, keeps the original behaviour
        (compared against mainline) — every caller from before this
        drop is unaffected. Every DISTINCT baseline id named (mainline
        included) is folded into the SAME query's ``IN`` clause, so
        this stays ONE query regardless of how many cards ask for a
        non-mainline baseline, not one more per card.

        Unlike :meth:`compare_counts`, classification happens in PYTHON
        rather than as a SQL CASE: each requested stream can scope to a
        DIFFERENT set of environments (a different product), so a single
        SQL FULL OUTER JOIN emulation would need one subquery pair per
        distinct product anyway. At Watchlist-card counts (dozens, not
        thousands of tests) fetching the raw rows once and grouping in
        memory is simpler and no more expensive.
        ``tests/test_storage.py::CompareCountsManyTest`` cross-checks
        this against :meth:`compare_counts` for the same stream
        (mainline and, for the WP-22 predecessor path, a non-mainline
        baseline too), so the two classifications cannot silently drift
        apart.
        """
        if not stream_environments:
            return {}
        baselines = baselines or {}
        conn = self._conn()
        stream_ids = sorted(stream_environments)
        all_envs = sorted({
            env for envs in stream_environments.values() for env in envs
        })
        baseline_ids = sorted({
            baselines.get(stream_id, MAINLINE_STREAM_ID)
            for stream_id in stream_ids
        })
        id_placeholders = ", ".join("?" for _ in stream_ids)
        clauses = ["lr.stream_id IN ({0})".format(id_placeholders)]
        params = list(stream_ids)  # type: List[Any]
        if all_envs:
            baseline_placeholders = ", ".join("?" for _ in baseline_ids)
            env_placeholders = ", ".join("?" for _ in all_envs)
            clauses.append(
                "(lr.stream_id IN ({0}) AND lr.environment IN "
                "({1}))".format(baseline_placeholders, env_placeholders)
            )
            params.extend(baseline_ids)
            params.extend(all_envs)
        rows = conn.execute(
            "SELECT lr.stream_id, lr.environment, lr.script, "
            "lr.test_name, lr.result FROM latest_runs lr "
            "LEFT JOIN test_retirements tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE (" + " OR ".join(clauses) + ") AND tr.retired_at IS NULL",
            params,
        ).fetchall()

        # A row can land in either bucket, or both — a stream can be one
        # card's OWN stream and another card's baseline (e.g. an
        # explicitly-requested predecessor build) in the same request.
        baseline_partitions = {}  # type: Dict[int, Dict[Tuple[str, str, str], str]]
        partitions = {}  # type: Dict[int, Dict[Tuple[str, str, str], str]]
        baseline_id_set = set(baseline_ids)
        for row in rows:
            sid, environment, script, test_name = (
                int(row[0]), row[1], row[2], row[3]
            )
            triple = (environment, script, test_name)
            if sid in baseline_id_set:
                baseline_partitions.setdefault(sid, {})[triple] = row[4]
            if sid in stream_environments:
                partitions.setdefault(sid, {})[triple] = row[4]

        fail = Result.FAIL.value
        results = {}  # type: Dict[int, CompareCounts]
        for stream_id, envs in stream_environments.items():
            env_set = set(envs)
            baseline_id = baselines.get(stream_id, MAINLINE_STREAM_ID)
            baseline = {
                triple: result
                for triple, result
                in baseline_partitions.get(baseline_id, {}).items()
                if triple[0] in env_set
            }
            # Match compare_counts' SQL path (_compare_pairs_sql applies
            # the SAME environment scope to both sides): a stream's own
            # runs are, by construction, always from its own product's
            # environments already, so this filter is a no-op in the
            # ordinary case and only bites the edge case of an
            # undeclared/misconfigured product (empty scope).
            mine = {
                triple: result
                for triple, result in partitions.get(stream_id, {}).items()
                if triple[0] in env_set
            }
            counts = {
                "new_failures": 0, "new_passes": 0, "both_failing": 0,
                "new_tests": 0, "no_result": 0, "agree": 0,
            }  # type: Dict[str, int]
            for triple in set(baseline) | set(mine):
                a = mine.get(triple)
                b = baseline.get(triple)
                if a is None:
                    counts["no_result"] += 1
                elif b is None:
                    counts["new_tests"] += 1
                elif a == fail and b == fail:
                    counts["both_failing"] += 1
                elif a == fail:
                    counts["new_failures"] += 1
                elif b == fail:
                    counts["new_passes"] += 1
                else:
                    # Both sides have a result and it is not FAIL on
                    # either side -- the same "agree" bucket
                    # compare_counts' SQL CASE ends on.
                    counts["agree"] += 1
            results[stream_id] = CompareCounts(**counts)
        return results

    def stream_identities(self, ids: Sequence[int]) -> Dict[int, "Stream"]:
        """Batch stream metadata (id, product, kind, name, first_seen,
        last_seen) for exactly *ids*, in ONE query.

        For the Watchlist's ``s:`` cards (docs/STREAMS_PLAN.md §3.6),
        which need every requested stream's identity and freshness but
        NOT a failing count — the verdict comes from
        :meth:`compare_counts_many` instead. ``failing`` on the returned
        :class:`Stream` objects is always 0: meaningless here, kept only
        so the same NamedTuple shape can be reused rather than adding a
        second one. Ids that do not exist are simply absent from the
        result — the caller renders those as error cards.
        """
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self._conn().execute(
            "SELECT id, product, kind, name, first_seen, last_seen "
            "FROM streams WHERE id IN ({0})".format(placeholders),
            list(ids),
        ).fetchall()
        return {
            int(row[0]): Stream(
                stream_id=int(row[0]), product=row[1], kind=row[2],
                name=row[3], first_seen=model.parse_iso(row[4]),
                last_seen=model.parse_iso(row[5]), failing=0,
            )
            for row in rows
        }

    def stream_results_for_triple(
        self, environment: str, script: str, test_name: str,
    ) -> List["StreamResult"]:
        """Every stream's latest result for one (environment, script,
        test_name) triple, newest first by that stream's own run
        (WP-22, docs/STREAMS_PLAN.md §4.1).

        Reads ``idx_latest_runs_triple (environment, script, test_name)``
        — the index WP-21 added for exactly this query — joined to
        ``streams`` for identity. Row count is the number of streams
        that HAVE run this triple (bounded by the product's stream
        count, small), never the number of streams that exist: a
        stream with no result for this test is simply absent, per §0.6
        — the caller (the test page's "Every build" table) is the one
        that renders that absence as NO RESULT, by unioning this list
        against ``GET /api/streams`` on the frontend; the dropdown next
        to it wants exactly this list unmodified, since a dropdown
        entry with nothing to show is not useful.

        No product filter here even though every real-world caller is
        scoped to one product: this triple's ``environment`` is already
        the discriminator (a stream can only carry a run of this exact
        environment if it was created by an import naming it, which
        fixes that stream's product at creation time — see
        :meth:`_find_or_create_stream`), so filtering again by a
        product resolved from the CURRENT ``environment_products``
        mapping would silently drop a legitimate row if that mapping
        was ever changed after the fact. Reading by environment alone
        is what stays correct across a remap.
        """
        rows = self._conn().execute(
            "SELECT s.id, s.product, s.kind, s.name, s.first_seen, "
            "s.last_seen, lr.result, lr.run_id, lr.start_time "
            "FROM latest_runs lr JOIN streams s ON s.id = lr.stream_id "
            "WHERE lr.environment = ? AND lr.script = ? "
            "AND lr.test_name = ? ORDER BY lr.start_time DESC",
            (environment, script, test_name),
        ).fetchall()
        return [
            StreamResult(
                stream=Stream(
                    stream_id=int(row[0]), product=row[1], kind=row[2],
                    name=row[3], first_seen=model.parse_iso(row[4]),
                    last_seen=model.parse_iso(row[5]), failing=0,
                ),
                result=Result(row[6]),
                run_id=int(row[7]),
                start_time=model.parse_iso(row[8]),
            )
            for row in rows
        ]

    def previous_builds(
        self, streams: Sequence["Stream"],
    ) -> Dict[int, "Stream"]:
        """For every ``kind='build'`` stream in *streams*, its nearest
        earlier same-product build by ``last_seen`` (id as tiebreak) —
        the WP-22 default comparison baseline (docs/STREAMS_PLAN.md
        §4.1: "the previous build by last_seen where one exists, else
        mainline"). A stream that is not a build, or has no earlier
        build of the same product, is simply absent from the result;
        the caller's own fallback is mainline, the same default every
        comparison already has.

        ONE query regardless of how many build streams are asked
        about, bounded to their DISTINCT products (an ``IN`` clause,
        one round trip) — the same flat-cost discipline
        :meth:`compare_counts_many` and :meth:`stream_identities` hold
        for ``/api/watch``'s ``s:`` cards, the only caller under that
        constraint today.
        """
        builds = [stream for stream in streams if stream.kind == "build"]
        if not builds:
            return {}
        products = sorted({stream.product for stream in builds})
        placeholders = ", ".join("?" for _ in products)
        rows = self._conn().execute(
            "SELECT id, product, kind, name, first_seen, last_seen "
            "FROM streams WHERE kind = 'build' AND product IN ({0}) "
            "ORDER BY product, last_seen, id".format(placeholders),
            products,
        ).fetchall()
        by_product = {}  # type: Dict[str, List[Sequence[Any]]]
        for row in rows:
            by_product.setdefault(row[1], []).append(row)
        result = {}  # type: Dict[int, Stream]
        for stream in builds:
            predecessor = None  # type: Optional[Sequence[Any]]
            for row in by_product.get(stream.product, []):
                if int(row[0]) == stream.stream_id:
                    break
                predecessor = row
            if predecessor is not None:
                result[stream.stream_id] = Stream(
                    stream_id=int(predecessor[0]), product=predecessor[1],
                    kind=predecessor[2], name=predecessor[3],
                    first_seen=model.parse_iso(predecessor[4]),
                    last_seen=model.parse_iso(predecessor[5]), failing=0,
                )
        return result

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
        stream_id: int = MAINLINE_STREAM_ID,
        environments: Optional[Sequence[str]] = None,
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

        *stream_id* (WP-23, default mainline): fed to the environment
        pill and Watchlist environment cards, both mainline concepts in
        every existing caller — a branch import must not make an
        environment's MAINLINE "last reported" line look fresher than
        mainline actually is. The default preserves that; the "own
        results" tab passes its own stream's id to get the branch's own
        clock instead. *environments* (WP-23 fix) is the WP-20
        ``product=`` allow-list — without it a product-scoped page's
        environment pills included every OTHER product's environments
        too (found live alongside the same bug in :meth:`environments`).

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`) -- same shared-key reasoning as
        :meth:`test_counts_by_environment`.
        """
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = ("latest_run_time_by_environment", stream_id, envs_key)
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        clause, clause_params = self._environments_clause(
            environments, column="environment"
        )
        sql = (
            "SELECT environment, MAX(start_time) FROM latest_runs "
            "WHERE stream_id = ?"
        )
        params = [stream_id] + clause_params  # type: List[Any]
        if clause is not None:
            sql += " AND " + clause
        sql += " GROUP BY environment"
        rows = self._conn().execute(sql, params).fetchall()
        result = {
            row[0]: model.parse_iso(row[1])
            for row in rows if row[1] is not None
        }
        self._store_summary(key, result)
        return result

    def unassigned_failing_by_environment(
        self, stream_id: int = MAINLINE_STREAM_ID,
    ) -> Dict[str, int]:
        """Count of currently-FAILING, currently-UNASSIGNED tests, per
        environment, on one stream (default mainline).

        The Watchlist's unassigned-failure highlight (docs/STREAMS_PLAN.md
        §2.4): ONE grouped aggregate over ``latest_runs`` LEFT JOIN
        ``current_assignments`` — the same "fetch once, slice per card
        in Python" shape every other Watchlist number already uses
        (:func:`testboard.api._handle_watch`'s own docstring), so `e:`
        cards read this dict directly and `p:` cards sum it over their
        own environments, with NO extra query per card either way.
        Assignments are TRIPLE-scoped, never stream-scoped (the same
        assignee is read from a branch's own row of the same test —
        see :class:`CompareRow`), so the join is on the plain triple,
        not on stream_id — only ``latest_runs`` itself is scoped to
        *stream_id*, deciding WHICH result counts as "currently
        failing".

        Retired tests are excluded, matching every other verdict number
        on this page (:func:`analytics.summarize_by_product`) — a
        retired test is not "in the suite" any more, so it cannot be a
        failure needing an owner.
        """
        rows = self._conn().execute(
            "SELECT lr.environment, COUNT(*) FROM latest_runs lr "
            "LEFT JOIN current_assignments ca "
            "  ON ca.environment = lr.environment "
            " AND ca.script = lr.script AND ca.test_name = lr.test_name "
            "LEFT JOIN test_retirements tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE lr.stream_id = ? AND lr.result = ? "
            "AND ca.assignee IS NULL AND tr.retired_at IS NULL "
            "GROUP BY lr.environment",
            (stream_id, Result.FAIL.value),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def unassigned_failing_by_stream(
        self, stream_ids: Sequence[int],
    ) -> Dict[int, int]:
        """The same count as :meth:`unassigned_failing_by_environment`,
        grouped by stream instead — the `s:` card's own question is
        "failing on THIS stream and unassigned", never mainline's.

        ONE query covering every requested id (the same
        "batched, not per-card" shape :meth:`stream_identities` already
        established) — ``{}`` with no query at all when *stream_ids* is
        empty, so a request with no ``s:`` cards costs nothing extra
        here either.
        """
        if not stream_ids:
            return {}
        placeholders = ", ".join("?" for _ in stream_ids)
        rows = self._conn().execute(
            "SELECT lr.stream_id, COUNT(*) FROM latest_runs lr "
            "LEFT JOIN current_assignments ca "
            "  ON ca.environment = lr.environment "
            " AND ca.script = lr.script AND ca.test_name = lr.test_name "
            "LEFT JOIN test_retirements tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE lr.stream_id IN ({0}) AND lr.result = ? "
            "AND ca.assignee IS NULL AND tr.retired_at IS NULL "
            "GROUP BY lr.stream_id".format(placeholders),
            list(stream_ids) + [Result.FAIL.value],
        ).fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    def latest_run_time(
        self, stream_id: int = MAINLINE_STREAM_ID
    ) -> Optional[datetime.datetime]:
        """Start time of the newest run on record, or None if empty.

        The estate's own clock. Reported alongside the summary so a
        stalled feeder is visible as a stalled feeder, rather than as
        every test in the estate quietly going stale. *stream_id*
        (WP-23, default mainline) — see
        :meth:`latest_run_time_by_environment`.
        """
        row = self._conn().execute(
            "SELECT MAX(start_time) FROM runs WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
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
        stream_id: int = MAINLINE_STREAM_ID,
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
        :meth:`_environments_clause`. *stream_id* (WP-23, default
        mainline): the Time page stayed mainline-only through WP-21/22
        (docs/STREAMS_PLAN.md §3.5/§3.10); WP-23 lets a long-running
        branch's "own results" tab read WHERE ITS OWN suite spent its
        time, the same way the mainline page always has — the default
        keeps every existing caller unscoped exactly as before.
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
            "lr.stream_id = ?",
        ]
        params = [stream_id]  # type: List[Any]
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[ScriptFailures]:
        """Scripts with the most currently-failing tests, worst first.

        *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`. *stream_id* (WP-23, default
        mainline) — see :meth:`duration_rollup`.
        """
        sql = (
            "SELECT lr.environment, lr.script, COUNT(*) AS failing "
            "FROM latest_runs AS lr "
            "LEFT JOIN test_retirements AS tr "
            "  ON tr.environment = lr.environment "
            " AND tr.script = lr.script AND tr.test_name = lr.test_name "
            "WHERE lr.result = ? AND " + self._NOT_RETIRED
            + " AND lr.stream_id = ?"
        )
        params = [Result.FAIL.value, stream_id]  # type: List[Any]
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Tuple[str, List[Any]]:
        """Build the WHERE clause for one triage queue.

        Retired tests are always excluded: approving a test as no longer
        in the suite is precisely a statement that it should stop
        appearing in the work queues — and the ``not_run`` queue, where
        that approval is given, is exactly where they would otherwise
        pile up. *environments* is the WP-20 ``product=`` filter — see
        :meth:`_environments_clause`. *stream_id* (WP-23, default
        mainline): ``/api/summary``'s queues were mainline-only through
        WP-21/22 (docs/STREAMS_PLAN.md §3.5) — a branch's failures must
        never appear in the MAINLINE triage queues (§0.4/§3.4), which
        still holds: the default is unchanged, and every existing caller
        that never passes *stream_id* keeps reading stream 1. What is
        new is the "own results" tab (§5.2), which reads a long-running
        branch's OWN triage numbers by passing that branch's id here —
        a second, independent set of queues, never merged with
        mainline's.
        """
        try:
            predicate = _QUEUE_PREDICATES[kind]
        except KeyError:
            raise ValueError("unknown queue kind: {!r}".format(kind))
        sql = " WHERE lr.stream_id = ? AND {} AND {}".format(
            predicate, Storage._NOT_RETIRED
        )
        params = [stream_id]  # type: List[Any]
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[TestStatusRow]:
        """Return one triage queue (see :data:`QUEUE_KINDS`), newest info first.

        *kind* selects the membership predicate; *assignee* narrows the
        queue to one person's tests (the "my actions" view, which must be
        filtered in SQL — filtering a capped queue client-side would hide
        a user's own tests behind other people's). Ordered by test
        identity; at most *limit* rows. :meth:`status_queue_count` gives
        the exact total. *environments* is the WP-20 ``product=`` filter.
        *stream_id* (WP-23, default mainline) — see :meth:`_queue_clause`.

        *with_latest_comment* adds each test's newest comment — what
        somebody already worked out about this failure, which is the
        first thing a person triaging it needs to know. One index seek
        per returned row.

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`) -- the single most expensive part of
        ``/api/summary``'s full (unpaginated) payload, measured, because
        every kind's row FETCH (unlike the counts above) was never
        batched. *with_latest_comment* is part of the cache key, so a
        comment-bearing entry can never satisfy a comment-free request or
        vice versa; comment TEXT is part of the cached VALUE, so
        :meth:`add_comment` and :meth:`set_retired` (which also posts
        one) both invalidate this alongside the runs/assignment/
        retirement writes every other cached method already needs.
        """
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = (
            "status_queue", kind, environment, limit, assignee,
            stale_before, with_latest_comment, envs_key, stream_id,
        )
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before, environments,
            stream_id,
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
        result = [
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
        self._store_summary(key, result)
        return result

    def status_queue_count(
        self,
        kind: str,
        environment: Optional[str] = None,
        assignee: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        environments: Optional[Sequence[str]] = None,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> int:
        """Exact size of a triage queue, ignoring any display cap.

        *stream_id* (WP-23, default mainline) — see :meth:`_queue_clause`.
        """
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before, environments,
            stream_id,
        )
        sql = "SELECT COUNT(*) " + self._LATEST_COUNT_JOIN + where
        return int(self._conn().execute(sql, params).fetchone()[0])

    def queue_counts(
        self,
        environment: Optional[str] = None,
        assignee: Optional[str] = None,
        stale_before: Optional[datetime.datetime] = None,
        environments: Optional[Sequence[str]] = None,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Dict[str, int]:
        """Exact size of EVERY triage queue, in one grouped pass.

        WP-23 perf pass: ``/api/summary``'s full payload used to call
        :meth:`status_queue_count` once per :data:`QUEUE_KINDS` entry for
        the tab-badge totals, and AGAIN once per kind for each queue's
        own ``"total"`` field — 2x(len(QUEUE_KINDS)+1) separate queries
        (12 on this project's own 6-kind QUEUE_KINDS) every request, each
        one scanning the SAME ``latest_runs``/``current_assignments``/
        ``test_retirements`` join with only the CASE predicate differing.
        This is the same join, once, with every kind's predicate as its
        own ``SUM(CASE WHEN ... THEN 1 ELSE 0 END)`` column — semantically
        identical to calling :meth:`status_queue_count` once per kind
        (``tests/test_storage.py`` pins the two agreeing), but one round
        trip instead of up to seven.

        Returns a dict keyed by every :data:`QUEUE_KINDS` entry plus
        ``"mine"`` (the ``assigned`` predicate further filtered to
        *assignee* — 0, not a KeyError, when *assignee* is falsy, the
        same contract :func:`testboard.api._summary_queue_totals` already
        had). *environment*/*environments*/*stream_id* — see
        :meth:`_queue_clause`; applied ONCE, in the shared WHERE, since
        every kind reads the same scoped partition. *stale_before* is
        required (as it is for :meth:`status_queue_count`) because
        ``not_run`` is always one of :data:`QUEUE_KINDS`.

        WP-23 "ONE MORE PERF SLICE": memoized (see
        :meth:`_cached_summary`), keyed on the exact *stale_before* value
        -- see :meth:`summary_rollup` for why cutoffs are never rounded
        for caching purposes. Invalidated by assignment writes too (not
        only runs/retirement writes), since the ``mine`` column and the
        ``assigned``/``unassigned`` kinds read ``current_assignments``.
        """
        if stale_before is None:
            raise ValueError("queue_counts needs stale_before")
        envs_key = (
            None if environments is None else tuple(sorted(environments))
        )
        key = (
            "queue_counts", environment, assignee, stale_before, envs_key,
            stream_id,
        )
        cached = self._cached_summary(key)
        if cached is not None:
            return cached
        # not_run's predicate carries the one parameterised placeholder
        # among QUEUE_KINDS (`lr.start_time < ?`) -- its bind value is
        # appended in the same left-to-right order the columns
        # themselves are built in, so select_params ends up matching
        # the SELECT-list's ?s positionally.
        select_params = []  # type: List[Any]
        ordered_columns = []  # type: List[str]
        for kind in QUEUE_KINDS:
            ordered_columns.append(
                "SUM(CASE WHEN {} THEN 1 ELSE 0 END)".format(
                    _QUEUE_PREDICATES[kind]))
            if kind in _STALE_QUEUES:
                select_params.append(model.format_iso(stale_before))
        include_mine = bool(assignee)
        if include_mine:
            ordered_columns.append(
                "SUM(CASE WHEN {} AND ca.assignee = ? "
                "THEN 1 ELSE 0 END)".format(_QUEUE_PREDICATES["assigned"])
            )
            select_params.append(assignee)
        sql = (
            "SELECT " + ", ".join(ordered_columns) + " "
            + self._LATEST_COUNT_JOIN
        )
        where = ["lr.stream_id = ?", self._NOT_RETIRED]
        where_params = [stream_id]  # type: List[Any]
        if environment is not None:
            where.append("lr.environment = ?")
            where_params.append(environment)
        envs_clause, envs_params = self._environments_clause(environments)
        if envs_clause is not None:
            where.append(envs_clause)
            where_params.extend(envs_params)
        sql += " WHERE " + " AND ".join(where)
        row = self._conn().execute(
            sql, select_params + where_params
        ).fetchone()
        counts = {
            kind: int(row[i] or 0) for i, kind in enumerate(QUEUE_KINDS)
        }
        counts["mine"] = (
            int(row[len(QUEUE_KINDS)] or 0) if include_mine else 0
        )
        self._store_summary(key, counts)
        return counts

    def recent_results(
        self,
        triples: Sequence[Tuple[str, str, str]],
        since: datetime.datetime,
        per_test_limit: int = 20,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Dict[Tuple[str, str, str], List[Result]]:
        """The last few results of each of a PAGE of tests.

        Returns ``{triple: [oldest, ..., newest]}``, at most
        *per_test_limit* entries each, for runs at or after *since*, on
        *stream_id* (WP-21, default mainline) — the caller's own stream,
        so a branch's stability history is never the mainline history of
        the same triple.

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
            params.append(stream_id)
            params.append(cutoff)
            rows = conn.execute(
                "SELECT environment, script, test_name, result, start_time "
                "FROM runs WHERE ({0}) AND stream_id = ? "
                "AND start_time >= ? "
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

    def latest_results_for_streams(
        self,
        keys: Sequence[Tuple[int, str, str, str]],
    ) -> Dict[Tuple[int, str, str, str], Result]:
        """Batched ``latest_runs.result`` lookup, keyed by ``(stream_id,
        environment, script, test_name)`` — ``latest_runs``'s own PRIMARY
        KEY, so every key is an index seek, never a scan.

        WP-23 perf-round ADDENDUM 3 (Open Actions' truthful display,
        docs/STREAMS_PLAN.md §5.4): a row's own ``result`` from
        :meth:`dashboard` is always MAINLINE's (Open Actions never
        passes ``stream_id`` to that call — assignments are estate-level,
        §0.4) — but when the row's CURRENT assignment was made from a
        non-mainline stream, that stream's OWN latest result for the
        same triple can disagree with mainline's, and showing only
        mainline's was a contradiction on its face ("assigned from the
        RC" reading PASS while the RC failure it represents is live). A
        key ABSENT from the returned dict means no result at all on
        that stream for that triple — never fabricated, never defaulted
        to a colour.

        Chunked at :data:`_RECENT_CHUNK` (100, the same batch size
        :meth:`recent_results` uses) to stay under SQLite's
        999-bound-parameter ceiling — each key costs 4 params here, the
        same as a triple costs 3 there.
        """
        found = {}  # type: Dict[Tuple[int, str, str, str], Result]
        unique = list(dict.fromkeys(keys))
        if not unique:
            return found
        conn = self._conn()
        for start in range(0, len(unique), _RECENT_CHUNK):
            chunk = unique[start:start + _RECENT_CHUNK]
            clause = " OR ".join(
                "(stream_id = ? AND environment = ? AND script = ? "
                "AND test_name = ?)"
                for _ in chunk
            )
            params = []  # type: List[Any]
            for stream_id, environment, script, test_name in chunk:
                params.extend([stream_id, environment, script, test_name])
            rows = conn.execute(
                "SELECT stream_id, environment, script, test_name, "
                "result FROM latest_runs WHERE " + clause,
                tuple(params),
            ).fetchall()
            for row in rows:
                key = (int(row[0]), row[1], row[2], row[3])
                found[key] = Result(row[4])
        return found

    def failure_streak_bounds(
        self,
        environment: str,
        script: str,
        test_name: str,
        latest_start: datetime.datetime,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> FailureStreak:
        """When the test's current FAIL streak began, and its last pass before.

        *latest_start* is the start time of the test's newest run, which
        the caller has already established is a FAIL. Three index seeks,
        no history walk: find the newest non-FAIL run below it (the run
        that bounds the streak), take the oldest run above that bound
        (every run in between is a FAIL by construction), then find the
        newest PASS older than the streak. ``FAILED_AS_EXPECTED`` and
        ``UNEXPECTED_PASS`` bound a streak but are not passes, so the
        last-pass seek is separate. *stream_id* (WP-21, default
        mainline) scopes every seek to the caller's own stream.
        """
        conn = self._conn()
        triple = (environment, script, test_name)
        start = model.format_iso(latest_start)

        bound_row = conn.execute(
            "SELECT start_time FROM runs WHERE environment = ? "
            "AND script = ? AND test_name = ? AND start_time < ? "
            "AND result <> ? AND stream_id = ? "
            "ORDER BY start_time DESC LIMIT 1",
            triple + (start, Result.FAIL.value, stream_id),
        ).fetchone()

        if bound_row is None:
            since_row = conn.execute(
                "SELECT MIN(start_time) FROM runs WHERE environment = ? "
                "AND script = ? AND test_name = ? AND start_time <= ? "
                "AND stream_id = ?",
                triple + (start, stream_id),
            ).fetchone()
        else:
            since_row = conn.execute(
                "SELECT MIN(start_time) FROM runs WHERE environment = ? "
                "AND script = ? AND test_name = ? AND start_time > ? "
                "AND start_time <= ? AND stream_id = ?",
                triple + (bound_row[0], start, stream_id),
            ).fetchone()
        if since_row is None or since_row[0] is None:
            return FailureStreak(failing_since=None, last_pass_before=None)
        failing_since = since_row[0]

        pass_row = conn.execute(
            "SELECT start_time FROM runs WHERE environment = ? "
            "AND script = ? AND test_name = ? AND start_time < ? "
            "AND result = ? AND stream_id = ? "
            "ORDER BY start_time DESC LIMIT 1",
            triple + (failing_since, Result.PASS.value, stream_id),
        ).fetchone()
        return FailureStreak(
            failing_since=model.parse_iso(failing_since),
            last_pass_before=(
                None if pass_row is None else model.parse_iso(pass_row[0])
            ),
        )

    def failure_streak_bounds_many(
        self,
        entries: Sequence[Tuple[str, str, str, datetime.datetime]],
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Dict[Tuple[str, str, str], FailureStreak]:
        """Batched form of :meth:`failure_streak_bounds` for a PAGE of rows.

        WP-23 perf pass: a triage queue's ``still_failing``/``mine`` rows
        called :meth:`failure_streak_bounds` once per FAIL row — three
        queries each, 127 calls measured on one request at dev scale.
        This does the SAME three-step computation (newest non-FAIL
        before the row's own latest run; the oldest run after that bound
        i.e. where the streak began; the newest PASS before that) for a
        whole PAGE in three round trips total (per chunk — see below),
        never one per row.

        *entries* is ``(environment, script, test_name, latest_start)``
        — the caller has already established each row is a FAIL, exactly
        as :meth:`failure_streak_bounds` requires of its own caller.
        Duplicate triples (the same test showing up in two queues, e.g.
        ``still_failing`` and ``mine``) are computed once. *stream_id*
        (WP-21, default mainline) scopes every seek to one stream, same
        as the single-row method.

        Each of the three steps is one query per chunk: a "driving"
        table of the chunk's own triples (built as a UNION ALL of
        literal SELECTs — no VALUES-table-constructor syntax, which
        this project's dual-backend translation does not need to know
        about) joined to a correlated subquery that reproduces exactly
        one step of :meth:`failure_streak_bounds`'s SQL, with the
        SHARED params (*stream_id*, the FAIL/PASS literals) bound once
        per query rather than once per row. Chunked at
        :data:`_RECENT_CHUNK` (the same batch size
        :meth:`recent_results` uses) to stay well under SQLite's
        999-bound-parameter ceiling — each row costs 4-5 params here,
        against :meth:`recent_results`'s 3, so the same chunk size still
        leaves headroom. Steps 2 and 3 are skipped entirely for rows a
        prior step already resolved to "no streak" (no non-FAIL run at
        all before the FAIL streak, in the whole history) or dropped
        (nothing to look up), so an all-still-failing-since-the-first-run
        page never runs step 3 at all.

        Returns a dict keyed by triple — never by triple *and*
        start_time — matching :meth:`failure_streak_bounds`'s own
        single-triple contract (a stream's ``latest_runs`` has at most
        one current start_time per triple, so this is not a loss of
        information).

        WP-23 "ONE MORE PERF SLICE": memoized PER ENTRY, in the SEPARATE
        :attr:`_streak_cache` dict (:meth:`_cached_streak`/
        :meth:`_store_streak`), unlike every other cached method here
        which memoizes one whole call's result into :attr:`_summary_cache`
        -- see :data:`_STREAK_CACHE_MAX_ENTRIES` for why sharing the one
        dict was wrong (measured: it thrashed :attr:`_summary_cache` mid-
        request). A queue's row set changes page to page and request to
        request even when the underlying failures do not (different
        LIMIT, different sort tie-breaks upstream), so caching the batch
        call as one unit would almost never hit; caching each
        ``(stream_id, environment, script, test_name, start_time)``
        streak individually means a test that is STILL failing on the
        next request is a hit even though the page around it changed.
        Only genuine misses reach the chunked batch machinery below,
        which is otherwise unchanged.
        """
        unique = list(dict.fromkeys(
            (env, script, test, model.format_iso(latest))
            for env, script, test, latest in entries
        ))  # type: List[Tuple[str, str, str, str]]
        result = {}  # type: Dict[Tuple[str, str, str], FailureStreak]
        if not unique:
            return result
        to_compute = []  # type: List[Tuple[str, str, str, str]]
        for env, script, test, start_iso in unique:
            triple = (env, script, test)
            cache_key = ("failure_streak", stream_id, env, script, test,
                         start_iso)
            cached = self._cached_streak(cache_key)
            if cached is None:
                to_compute.append((env, script, test, start_iso))
            else:
                result[triple] = cached
        if not to_compute:
            return result
        conn = self._conn()
        for start in range(0, len(to_compute), _RECENT_CHUNK):
            chunk = to_compute[start:start + _RECENT_CHUNK]
            bounds = self._streak_bound_many(conn, chunk, stream_id)
            since_inputs = [
                (env, script, test, start_iso, bounds[(env, script, test)])
                for (env, script, test, start_iso) in chunk
            ]
            since_map = self._streak_since_many(
                conn, since_inputs, stream_id)
            pass_inputs = [
                (env, script, test, since_map[(env, script, test)])
                for (env, script, test, _start_iso) in chunk
                if since_map[(env, script, test)] is not None
            ]
            pass_map = (
                self._streak_pass_many(conn, pass_inputs, stream_id)
                if pass_inputs else {}
            )  # type: Dict[Tuple[str, str, str], Optional[str]]
            for (env, script, test, start_iso) in chunk:
                key = (env, script, test)
                failing_since_iso = since_map[key]
                if failing_since_iso is None:
                    streak = FailureStreak(
                        failing_since=None, last_pass_before=None)
                else:
                    pass_iso = pass_map.get(key)
                    streak = FailureStreak(
                        failing_since=model.parse_iso(failing_since_iso),
                        last_pass_before=(
                            None if pass_iso is None
                            else model.parse_iso(pass_iso)
                        ),
                    )
                result[key] = streak
                self._store_streak(
                    ("failure_streak", stream_id, env, script, test,
                     start_iso),
                    streak,
                )
        return result

    @staticmethod
    def _driving_table(column_names: Sequence[str], rows: int) -> str:
        """A UNION ALL of *rows* literal SELECTs naming *column_names*.

        The portable stand-in for a VALUES-as-table constructor: every
        branch is a plain ``SELECT ? AS col, ? AS col2, ...`` with no
        FROM, each naming its own columns via ``AS`` — the same style
        :meth:`_compare_pairs_sql` already uses for its UNION ALL, and
        deliberately NOT the ``FROM (...) AS v(col1, col2, ...)``
        derived-table column-list form, which is not something this
        project's dual-backend translation (a plain ``?`` -> ``%s``
        text substitution, nothing SQL-shape-aware) has ever had to
        vouch for. Both SQLite and MariaDB accept the per-branch ``AS``
        form identically.
        """
        row_sql = "SELECT " + ", ".join(
            "? AS {}".format(name) for name in column_names
        )
        return " UNION ALL ".join([row_sql] * rows)

    def _streak_bound_many(
        self,
        conn: Any,
        chunk: Sequence[Tuple[str, str, str, str]],
        stream_id: int,
    ) -> Dict[Tuple[str, str, str], Optional[str]]:
        """Step 1 for a chunk: newest non-FAIL run before each row's own
        latest start, batched. See :meth:`failure_streak_bounds_many`."""
        driving = self._driving_table(
            ("environment", "script", "test_name", "start_time"),
            len(chunk),
        )
        sql = (
            "SELECT v.environment, v.script, v.test_name, "
            "(SELECT r.start_time FROM runs r "
            " WHERE r.environment = v.environment "
            " AND r.script = v.script AND r.test_name = v.test_name "
            " AND r.stream_id = ? AND r.start_time < v.start_time "
            " AND r.result <> ? "
            " ORDER BY r.start_time DESC LIMIT 1) AS bound "
            "FROM (" + driving + ") AS v"
        )
        params = [stream_id, Result.FAIL.value]  # type: List[Any]
        for env, script, test, start_iso in chunk:
            params.extend([env, script, test, start_iso])
        rows = conn.execute(sql, params).fetchall()
        return {(row[0], row[1], row[2]): row[3] for row in rows}

    def _streak_since_many(
        self,
        conn: Any,
        chunk: Sequence[Tuple[str, str, str, str, Optional[str]]],
        stream_id: int,
    ) -> Dict[Tuple[str, str, str], Optional[str]]:
        """Step 2 for a chunk: where each row's current streak began,
        given step 1's bound (nullable). See
        :meth:`failure_streak_bounds_many`.

        Unifies :meth:`failure_streak_bounds`'s two branches (a bound
        found, or not) into one condition, since both are now driven by
        the SAME batched query: ``bound IS NULL`` reproduces the
        no-bound branch's ``start_time <= latest`` exactly (no lower
        bound at all); a real bound adds ``start_time > bound``.
        """
        driving = self._driving_table(
            ("environment", "script", "test_name", "start_time", "bound"),
            len(chunk),
        )
        sql = (
            "SELECT v.environment, v.script, v.test_name, "
            "(SELECT MIN(r.start_time) FROM runs r "
            " WHERE r.environment = v.environment "
            " AND r.script = v.script AND r.test_name = v.test_name "
            " AND r.stream_id = ? AND r.start_time <= v.start_time "
            " AND (v.bound IS NULL OR r.start_time > v.bound)"
            ") AS failing_since "
            "FROM (" + driving + ") AS v"
        )
        params = [stream_id]  # type: List[Any]
        for env, script, test, start_iso, bound_iso in chunk:
            params.extend([env, script, test, start_iso, bound_iso])
        rows = conn.execute(sql, params).fetchall()
        return {(row[0], row[1], row[2]): row[3] for row in rows}

    def _streak_pass_many(
        self,
        conn: Any,
        chunk: Sequence[Tuple[str, str, str, str]],
        stream_id: int,
    ) -> Dict[Tuple[str, str, str], Optional[str]]:
        """Step 3 for a chunk: the newest PASS before each row's own
        failing_since (step 2's result). See
        :meth:`failure_streak_bounds_many`. Only called with rows that
        HAVE a failing_since — a row with none needs no lookup."""
        driving = self._driving_table(
            ("environment", "script", "test_name", "failing_since"),
            len(chunk),
        )
        sql = (
            "SELECT v.environment, v.script, v.test_name, "
            "(SELECT r.start_time FROM runs r "
            " WHERE r.environment = v.environment "
            " AND r.script = v.script AND r.test_name = v.test_name "
            " AND r.stream_id = ? AND r.start_time < v.failing_since "
            " AND r.result = ? "
            " ORDER BY r.start_time DESC LIMIT 1) AS last_pass "
            "FROM (" + driving + ") AS v"
        )
        params = [stream_id, Result.PASS.value]  # type: List[Any]
        for env, script, test, failing_since_iso in chunk:
            params.extend([env, script, test, failing_since_iso])
        rows = conn.execute(sql, params).fetchall()
        return {(row[0], row[1], row[2]): row[3] for row in rows}

    def daily_result_counts(
        self,
        since: datetime.datetime,
        environment: Optional[str] = None,
        environments: Optional[Sequence[str]] = None,
        stream_id: int = MAINLINE_STREAM_ID,
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
        :meth:`_environments_clause`. *stream_id* (WP-23, migration 10,
        default mainline): before this parameter existed the table held
        only mainline's rows, so an unfiltered SUM was correct by
        construction; now every stream is maintained, so this is BOTH
        the fix for that latent cross-stream leak and the mechanism the
        "own results" trend chart reads. Both are part of the cache key
        (*environments* as a sorted tuple, so the same set in a
        different order is still one cache entry) precisely so a scoped
        and an unscoped — or a mainline and a branch — request for the
        same window cannot serve each other's answer.

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
            "FROM activity_hours WHERE stream_id = ? AND hour >= ?"
        )
        params = [
            stream_id, model.format_iso(since)[:13]
        ]  # type: List[Any]
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
        key = (stream_id, params[1], environment, envs_key)
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
        self,
        key: Tuple[int, str, Optional[str], Optional[Tuple[str, ...]]],
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
        key: Tuple[int, str, Optional[str], Optional[Tuple[str, ...]]],
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

    def _cached_summary(self, key: Tuple[Any, ...]) -> Optional[Any]:
        """Return a memoized summary/watch component for *key*, or None.

        Same TTL semantics as :meth:`_cached_trend`, reusing
        :data:`_TREND_CACHE_TTL_SECONDS` rather than a second constant:
        it bounds how stale an answer can be after a write made by a
        DIFFERENT process (an offline tool while the server is up); a
        write made by THIS process invalidates at once, via
        :meth:`_invalidate_summary_cache`.
        """
        now = time.time()
        with self._summary_lock:
            entry = self._summary_cache.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if now - stored_at > _TREND_CACHE_TTL_SECONDS:
                del self._summary_cache[key]
                return None
            return value

    def _store_summary(self, key: Tuple[Any, ...], value: Any) -> None:
        """Memoize a computed summary/watch component, bounding cache size."""
        with self._summary_lock:
            if len(self._summary_cache) >= _SUMMARY_CACHE_MAX_ENTRIES:
                self._summary_cache.clear()
            self._summary_cache[key] = (time.time(), value)

    def _cached_streak(self, key: Tuple[Any, ...]) -> Optional[Any]:
        """Return a memoized failure streak for *key*, or None.

        Same TTL/lock discipline as :meth:`_cached_summary`, against the
        SEPARATE dict :meth:`failure_streak_bounds_many` uses -- see
        :data:`_STREAK_CACHE_MAX_ENTRIES` for why it is not the same
        dict.
        """
        now = time.time()
        with self._streak_lock:
            entry = self._streak_cache.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if now - stored_at > _TREND_CACHE_TTL_SECONDS:
                del self._streak_cache[key]
                return None
            return value

    def _store_streak(self, key: Tuple[Any, ...], value: Any) -> None:
        """Memoize one computed failure streak, bounding cache size."""
        with self._streak_lock:
            if len(self._streak_cache) >= _STREAK_CACHE_MAX_ENTRIES:
                self._streak_cache.clear()
            self._streak_cache[key] = (time.time(), value)

    def _invalidate_summary_cache(self) -> None:
        """Drop every memoized summary/watch component, BOTH dicts.

        Called from every mutator that can change what any of the cached
        methods below would compute -- ``runs``/``latest_runs`` writes
        (:meth:`upsert_runs`, :meth:`delete_stream`,
        :meth:`prune_runs_before`, :meth:`delete_environment` -- the same
        four sites that already call :meth:`_invalidate_trend_cache`),
        ``current_assignments`` writes (:meth:`set_assignee`, which
        ``queue_counts``/``status_queue``'s assigned/mine predicates
        read), ``test_retirements`` writes (:meth:`set_retired`, which
        ``summary_rollup``/``queue_counts``/``status_queue``/
        ``test_counts_by_environment`` all read), and comment writes
        (:meth:`add_comment`, plus :meth:`set_retired` again -- it also
        posts one -- since ``status_queue(with_latest_comment=True)``
        caches the comment text itself). Unconditional full clear, same
        as the trend cache: the scope-key space is small, so there is no
        value in selective invalidation, only risk in getting it wrong.
        Clears :attr:`_streak_cache` too -- every one of those writes can
        also change a failure's streak bounds (a new run, a pruned or
        deleted stream/environment) or make an entry irrelevant (a
        retirement), and the two dicts share one external invalidation
        surface even though they are sized and populated differently.

        ``set_environment_product``/``clear_environment_product`` are
        deliberately NOT here: every cached method below takes its
        product/environment scope as an explicit ``environments``
        allow-list argument (never joins ``environment_products``
        itself), and that allow-list is part of every cache key, so a
        remap changes which KEY a request computes rather than making an
        existing entry wrong -- the old entry is simply never looked up
        again, not served stale.
        """
        with self._summary_lock:
            self._summary_cache.clear()
        with self._streak_lock:
            self._streak_cache.clear()

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
        self, environment: str, script: str, test_name: str,
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> Optional[StoredRun]:
        """Return the newest run for the triple (``output=None``), or None.

        *stream_id* (WP-21, default mainline) — the test detail page's
        ``stream=`` param.
        """
        row = self._conn().execute(
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND stream_id = ? "
            "ORDER BY start_time DESC LIMIT 1".format(
                _RUN_COLUMNS
            ),
            (environment, script, test_name, stream_id),
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[StoredRun]:
        """Return runs for the triple, newest first, ``output=None``.

        When *before* is given only runs with ``start_time`` strictly
        earlier than it are returned (pagination cursor). At most *limit*
        runs are returned. *stream_id* (WP-21, default mainline) — the
        test detail page's ``stream=`` param; the history table is one
        stream's runs of the triple, never a mix.
        """
        sql = (
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND stream_id = ?".format(_RUN_COLUMNS)
        )
        params = [
            environment, script, test_name, stream_id,
        ]  # type: List[Any]
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
        stream_id: int = MAINLINE_STREAM_ID,
    ) -> List[StoredRun]:
        """Return runs with ``start_time >= since``, newest first.

        Capped at *limit* rows; ``output=None``. This is the analytics
        window query, scoped to *stream_id* (WP-21, default mainline) —
        analytics computed for a branch-scoped test detail page must be
        that branch's own runs, never mainline's.
        """
        rows = self._conn().execute(
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND test_name = ? AND start_time >= ? AND stream_id = ? "
            "ORDER BY start_time DESC LIMIT ?".format(_RUN_COLUMNS),
            (
                environment,
                script,
                test_name,
                model.format_iso(since),
                stream_id,
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
        stream_id: int = MAINLINE_STREAM_ID,
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

        *stream_id* (F7, default mainline): the Timeline's TOP-level
        blocks/rows have been stream-aware since migration 10
        (``script_hours`` carries ``stream_id``), but this row-EXPANSION
        read of the raw ``runs`` table stayed hardcoded to mainline
        until now — a real gap: a branch-scoped Timeline page expanding
        a row would otherwise have silently shown MAINLINE's runs from
        the same time window instead of the branch's own (or nothing,
        if mainline happened to be quiet then), which is exactly the
        "wrong and looks right" shape this project's house rules warn
        against.
        """
        sql = (
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND start_time >= ? AND stream_id = ?".format(_RUN_COLUMNS)
        )
        params = [
            environment, script, model.format_iso(since),
            stream_id,
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
        # WP-23 "ONE MORE PERF SLICE": summary_rollup/queue_counts/
        # status_queue all read test_retirements' "retired" flag, and
        # status_queue(with_latest_comment=True) would otherwise cache
        # the comment this just posted as missing.
        self._invalidate_summary_cache()
        return Comment(
            comment_id=comment_id,
            environment=environment,
            script=script,
            test_name=test_name,
            author=username,
            created_at=now,
            text=comment,
            stream_id=None,
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

        **Mainline only** (WP-21, docs/STREAMS_PLAN.md §3.4): the caller
        (:meth:`upsert_runs`) only reaches this for
        ``stream_id == MAINLINE_STREAM_ID`` — a branch run may predate a
        retirement decided on mainline and must not silently reverse it.
        The recorded comment is stamped ``stream_id=MAINLINE_STREAM_ID``
        for the same reason it is only ever called for one.
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
            MAINLINE_STREAM_ID,
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
                "    AND p.stream_id = latest_runs.stream_id "
                "  ORDER BY p.start_time DESC LIMIT 1)"
            )
            # A prune breaks the "activity_hours == GROUP BY over runs"
            # invariant for every touched hour, and the surviving
            # latest-run rows make the arithmetic of a partial decrement
            # fiddly. Rebuilding is one aggregate pass over what is LEFT
            # — this is an offline maintenance path that just deleted
            # most of the table, not a request handler. The all-streams
            # variants (WP-23, migration 10): a prune can touch any
            # stream's rows, not only mainline's, and unlike migration
            # 6/7's own steps this runs against the LIVE (already
            # stream_id-in-PK) schema, so it must re-derive every
            # stream's partition, not assume stream 1
            # (_rebuild_activity_hours/_rebuild_script_hours stay
            # untouched — they are what a fresh install's migration 6/7
            # steps run against the pre-stream table shape).
            _rebuild_activity_hours_all_streams(conn)
            _rebuild_script_hours_all_streams(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self._invalidate_trend_cache()
        self._invalidate_summary_cache()
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
        self._invalidate_summary_cache()
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
        stream_id: Optional[int] = None,
    ) -> Comment:
        """Add a comment to a test triple, implicitly creating *author*.

        *stream_id* (WP-21) records which stream the comment was posted
        FROM — an annotation, not part of the comment's identity; the
        comment is still visible from every stream's test detail page.
        ``None`` (the default) is a plain "no stream context" comment,
        same as every comment posted before this migration.
        """
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_user(author, created_at)
            comment_id = self._insert_comment(
                conn, (environment, script, test_name), author, text,
                created_at, stream_id,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # WP-23 "ONE MORE PERF SLICE": status_queue(with_latest_comment=
        # True) caches each row's comment text; a new comment must not
        # keep serving the previous one (or "no comment") until the TTL
        # expires.
        self._invalidate_summary_cache()
        return Comment(
            comment_id=comment_id,
            environment=environment,
            script=script,
            test_name=test_name,
            author=author,
            created_at=created_at,
            text=text,
            stream_id=stream_id,
        )

    @staticmethod
    def _insert_comment(
        conn: sqlite3.Connection,
        triple: Tuple[str, str, str],
        author: str,
        text: str,
        created_at: datetime.datetime,
        stream_id: Optional[int] = None,
    ) -> int:
        """Append one comment inside the caller's transaction; return its id.

        Shared by the comment endpoint, retirement (which records the
        human's reason, ``stream_id=None`` — retiring is a triage
        decision, not something posted from a stream) and un-retirement
        (which records the machine's, ``stream_id=MAINLINE_STREAM_ID``
        since un-retirement only ever fires on a mainline import), so
        every one of them lands in the same thread.
        """
        cursor = conn.execute(
            "INSERT INTO comments (environment, script, test_name, "
            "author, created_at, text, stream_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            triple + (author, model.format_iso(created_at), text, stream_id),
        )
        return int(cursor.lastrowid)

    def comments(
        self, environment: str, script: str, test_name: str
    ) -> List[Comment]:
        """Return all comments for the triple, oldest first."""
        rows = self._conn().execute(
            "SELECT id, environment, script, test_name, author, "
            "created_at, text, stream_id FROM comments WHERE "
            "environment = ? AND script = ? AND test_name = ? "
            "ORDER BY id",
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
                stream_id=(None if row[7] is None else int(row[7])),
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
        stream_id: Optional[int] = None,
    ) -> None:
        """Append an assignment-history row and update the current state.

        ``assignee=None`` clears the assignment (auditable: a row with a
        NULL assignee is appended to ``assignments``, and
        ``current_assignments`` is set to NULL rather than deleted). Both
        *assigned_by* and *assignee* (when not None) are implicitly
        created as users. Both writes share one transaction, so the log
        and the current state can never disagree.

        *stream_id* (WP-21, default ``None``) is WHERE the assignment
        was made from — an annotation on the assignment, never a
        partition key: the test being assigned is the same test
        regardless of which stream's dashboard someone was looking at
        (docs/STREAMS_PLAN.md §3.4/§3.6). ``None`` means "made from
        mainline, or before this column existed" — the API layer only
        ever passes a non-None value when the caller was actually
        scoped to a non-mainline stream.
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
                "assignee, assigned_by, assigned_at, stream_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                triple + (
                    assignee,
                    assigned_by,
                    model.format_iso(assigned_at),
                    stream_id,
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
                    "test_name, assignee, stream_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    triple + (assignee, stream_id),
                )
            else:
                conn.execute(
                    "UPDATE current_assignments SET assignee = ?, "
                    "stream_id = ? "
                    "WHERE environment = ? AND script = ? "
                    "AND test_name = ?",
                    (assignee, stream_id) + triple,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # WP-23 "ONE MORE PERF SLICE": queue_counts' "mine" column and
        # status_queue's assigned/unassigned kinds both read
        # current_assignments.
        self._invalidate_summary_cache()

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
