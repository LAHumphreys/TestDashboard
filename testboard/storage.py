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
- Two derived tables keep estate-wide reads proportional to the number of
  TESTS (and the page actually returned) rather than to the number of
  runs ever recorded: ``latest_runs`` (one row per test: its newest run,
  that run's result and the previous run's result) and
  ``current_assignments`` (one row per test: who owns it now, with
  ``assignments`` kept as the audit log). Both are maintained inside the
  same transaction as the write that changes them — see
  :meth:`Storage._maintain_latest` and :meth:`Storage.set_assignee` — so
  they cannot drift from ``runs``.

Only ``str``/``int``/``float``/``None`` cross the sqlite boundary
(``detect_types=0``); datetimes are converted with
:func:`testboard.model.format_iso` / :func:`testboard.model.parse_iso`
inside this module, so lexical string comparison in SQL equals time
comparison.

Python 3.6 compatible; standard library only; parameterized SQL only.
"""

import datetime
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
    Tuple,
)

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
    "UpsertCounts",
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


#: Python migration steps, by the name used after the prefix.
_MIGRATION_STEPS = {
    "backfill_latest_durations": _backfill_latest_durations,
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


class UpsertCounts(NamedTuple):
    """Result of a batch upsert: how many rows were inserted vs updated."""

    inserted: int
    updated: int


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


class Storage:
    """SQLite-backed storage with per-thread connections.

    Constructing a :class:`Storage` opens a connection for the calling
    thread and immediately runs any pending schema migrations. Each other
    thread that touches the instance lazily gets its own connection with
    the same pragmas (WAL journal, 10s busy timeout, foreign keys on).
    """

    def __init__(self, path: str, cache_mb: Optional[int] = None,
                 mmap_mb: Optional[int] = None,
                 max_connections: int = DEFAULT_MAX_CONNECTIONS) -> None:
        """Open the database at *path* and run migrations immediately.

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
        self._path = path
        self._cache_mb = cache_mb
        self._mmap_mb = mmap_mb
        self._max_connections = max(1, int(max_connections))
        self._local = threading.local()
        self._trend_cache = {}  # type: Dict[Tuple[str, Optional[str]], Tuple[float, List[DailyResultCount]]]
        self._trend_lock = threading.Lock()
        self._migrate()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, opening it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._path, detect_types=0, isolation_level=None
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._apply_cache_pragmas(conn)
            self._local.conn = conn
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
        if self._cache_mb is None:
            return None
        return max(
            _MIN_CACHE_KIB,
            int(self._cache_mb) * 1024 // self._max_connections,
        ) * 1024

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
        """
        conn = self._conn()
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
        For each record the existing row id is looked up (an index hit on
        the UNIQUE constraint); if found the row is UPDATEd in place
        (preserving its rowid), otherwise a new row is INSERTed. Per-record
        work is index hits only, so a 5000-record batch imports fast.
        """
        conn = self._conn()
        inserted = 0
        updated = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rec in records:
                start = model.format_iso(rec.start_time)
                end = model.format_iso(rec.end_time)
                row = conn.execute(
                    "SELECT id FROM runs WHERE environment = ? AND "
                    "script = ? AND test_name = ? AND start_time = ?",
                    (rec.environment, rec.script, rec.test_name, start),
                ).fetchone()
                if row is None:
                    cursor = conn.execute(
                        "INSERT INTO runs (environment, script, test_name, "
                        "result, start_time, end_time, source_link, "
                        "known_failure_reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            rec.environment,
                            rec.script,
                            rec.test_name,
                            rec.result.value,
                            start,
                            end,
                            rec.source_link,
                            rec.known_failure_reason,
                        ),
                    )
                    run_id = int(cursor.lastrowid)
                    inserted += 1
                else:
                    run_id = int(row[0])
                    conn.execute(
                        "UPDATE runs SET result = ?, end_time = ?, "
                        "source_link = ?, "
                        "known_failure_reason = ? WHERE id = ?",
                        (
                            rec.result.value,
                            end,
                            rec.source_link,
                            rec.known_failure_reason,
                            run_id,
                        ),
                    )
                    updated += 1
                # The payload lives in its own table, deflated;
                # re-importing a run replaces it.
                conn.execute(
                    "INSERT OR REPLACE INTO run_outputs (run_id, output) "
                    "VALUES (?, ?)",
                    (run_id, _compress_output(rec.output)),
                )
                self._maintain_latest(conn, rec, run_id, start)
                self._unretire_on_new_run(conn, rec, start)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self._invalidate_trend_cache()
        return UpsertCounts(inserted=inserted, updated=updated)

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
    def _dashboard_filters(
        environment: Optional[str],
        script: Optional[str],
        result_values: Optional[List[str]],
        q: Optional[str],
        stale_before: Optional[datetime.datetime],
        include_retired: bool = False,
        assignees: Optional[Sequence[str]] = None,
        include_unassigned: bool = False,
    ) -> Tuple[List[str], List[Any]]:
        """Build the shared WHERE clauses for the dashboard list and count.

        *include_retired* keeps tests approved as no longer in the suite;
        by default they are hidden, which is the whole point of retiring
        one. *assignees* and *include_unassigned* combine as OR — "show
        me Alice's and Bob's open items, plus anything nobody owns".
        """
        clauses = []  # type: List[str]
        params = []  # type: List[Any]
        if environment is not None:
            clauses.append("lr.environment = ?")
            params.append(environment)
        if script is not None:
            clauses.append("lr.script = ?")
            params.append(script)
        if result_values is not None:
            placeholders = ", ".join("?" for _ in result_values)
            clauses.append("lr.result IN ({})".format(placeholders))
            params.extend(result_values)
        if q is not None:
            clauses.append("lr.test_name LIKE ? ESCAPE '\\'")
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
        owner. With *with_latest_comment* each row carries the newest
        comment on that test — an index seek per returned row, so it is
        opt-in and never paid for by the home screen.

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
            include_retired, assignees, include_unassigned,
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
            sql += " LIMIT -1 OFFSET ?"
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
    ) -> int:
        """Exact number of tests matching the same filters as :meth:`dashboard`."""
        result_values = (
            None if results is None else [r.value for r in results]
        )  # type: Optional[List[str]]
        if result_values is not None and not result_values:
            return 0
        clauses, params = self._dashboard_filters(
            environment, script, result_values, q, stale_before,
            include_retired, assignees, include_unassigned,
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
    ) -> List[RollupCount]:
        """Group the whole estate by environment, result, previous result.

        Returns a few dozen :class:`RollupCount` cells — one GROUP BY over
        ``latest_runs`` — from which
        :func:`testboard.analytics.summarize_rollup` derives every
        headline number. A test counts as having run recently when its
        latest run started at or after *recent_cutoff*.
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
        if environment is not None:
            sql += " WHERE lr.environment = ?"
            params.append(environment)
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
        self, environment: Optional[str] = None
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

        SUBSTR rather than a date function, so this means the same thing
        on MariaDB (see docs/MARIADB_MIGRATION.md E.4).
        """
        rows = self._conn().execute(
            "SELECT environment, SUBSTR(start_time, 1, 13), COUNT(*) "
            "FROM runs WHERE start_time >= ? "
            "GROUP BY environment, SUBSTR(start_time, 1, 13) "
            "ORDER BY environment, 2",
            (model.format_iso(since),),
        ).fetchall()
        return [
            (row[0], model.parse_iso(row[1] + ":00:00.000000"), int(row[2]))
            for row in rows
        ]

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
            "ORDER BY 1"
        ).fetchall()
        return [row[0] for row in rows]

    def environment_exists(self, environment: str) -> bool:
        """True if *environment* has run a test or carries a declaration.

        Two index seeks, so validating one name costs nothing that grows
        with the estate — unlike asking for the whole list and searching
        it.
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
        self, environment: Optional[str] = None, limit: int = 10
    ) -> List[ScriptFailures]:
        """Scripts with the most currently-failing tests, worst first."""
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
    ) -> Tuple[str, List[Any]]:
        """Build the WHERE clause for one triage queue.

        Retired tests are always excluded: approving a test as no longer
        in the suite is precisely a statement that it should stop
        appearing in the work queues — and the ``not_run`` queue, where
        that approval is given, is exactly where they would otherwise
        pile up.
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
    ) -> List[TestStatusRow]:
        """Return one triage queue (see :data:`QUEUE_KINDS`), newest info first.

        *kind* selects the membership predicate; *assignee* narrows the
        queue to one person's tests (the "my actions" view, which must be
        filtered in SQL — filtering a capped queue client-side would hide
        a user's own tests behind other people's). Ordered by test
        identity; at most *limit* rows. :meth:`status_queue_count` gives
        the exact total.

        *with_latest_comment* adds each test's newest comment — what
        somebody already worked out about this failure, which is the
        first thing a person triaging it needs to know. One index seek
        per returned row.
        """
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before
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
    ) -> int:
        """Exact size of a triage queue, ignoring any display cap."""
        where, params = self._queue_clause(
            kind, environment, assignee, stale_before
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
    ) -> List[DailyResultCount]:
        """Run counts grouped by UTC calendar day and result.

        This is the one read whose cost is set by the number of RUNS
        rather than the number of tests: a 14-night window over a
        12,000-test estate is ~170,000 index entries, and measured on a
        year of history (4.4M runs) that scan is ~345ms — the rest of the
        home screen put together is under 50ms. So the result is memoized
        per (window, environment).

        The cache is cleared by every write this process makes (see
        :meth:`upsert_runs` and :meth:`prune_runs_before`), and entries
        expire after :data:`_TREND_CACHE_TTL_SECONDS` so a write made by
        a DIFFERENT process — an offline prune while the server is up —
        cannot pin a stale trend for long. Nightly data changes once a
        day; the chart does not need to be fresher than that.

        Only runs with ``start_time >= since`` are counted — a range scan
        over the covering ``idx_runs_start_time_result`` index, so its
        cost tracks the width of the window, not the size of the history
        behind it. Days with no runs simply
        do not appear — the caller zero-fills its display range. Ordered
        by day, then result value.
        """
        sql = (
            "SELECT substr(start_time, 1, 10) AS day, result, COUNT(*) "
            "FROM runs WHERE start_time >= ?"
        )
        params = [model.format_iso(since)]  # type: List[Any]
        if environment is not None:
            sql += " AND environment = ?"
            params.append(environment)
        sql += " GROUP BY day, result ORDER BY day, result"

        key = (params[0], environment)
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
        self, key: Tuple[str, Optional[str]]
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
        self, key: Tuple[str, Optional[str]],
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
        """
        rows = self._conn().execute(
            "SELECT {} FROM runs WHERE environment = ? AND script = ? "
            "AND start_time >= ? ORDER BY start_time LIMIT ?".format(
                _RUN_COLUMNS
            ),
            (environment, script, model.format_iso(since), limit),
        ).fetchall()
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
        # The trend and the bucket memo are estate-wide and have just
        # stopped being true.
        self._invalidate_trend_cache()
        return deleted

    def vacuum(self) -> None:
        """Rebuild the database file, returning freed pages to the disk.

        Exclusive and expensive (it rewrites the whole file) — a
        maintenance-window operation, never part of serving a request.
        """
        self._conn().execute("VACUUM")

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
            except sqlite3.IntegrityError:
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
        except sqlite3.IntegrityError:
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
