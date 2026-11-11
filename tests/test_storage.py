"""Unit tests for :mod:`testboard.storage`.

Covers: migration idempotency, explicit upsert insert/update counts and
idempotent re-import, dashboard latest-run-per-test with every filter and
LIKE escaping, history pagination, runs_since bounds, implicit user
creation via comments/assignments, assignment history and clearing,
output-column hygiene (only get_run fetches it), thread-local connections,
and a 5000-record single-transaction batch import under 10 seconds.

Windows-safe cleanup: sqlite connections are closed before the temp
directory is removed, and rmtree uses ignore_errors=True.
"""

import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from typing import (
    Any, Callable, Dict, List, Optional, Sequence, Set, Tuple,
)

from testboard import analytics, model, storage
from testboard.model import Result, RunRecord
from testboard.storage import Storage

BASE = datetime.datetime(2026, 7, 1, 2, 0, 0)
CREATED = datetime.datetime(2026, 7, 1, 9, 0, 0)


def trace_sql_into(conn: sqlite3.Connection, into: List[str]) -> None:
    """Register a trace callback that appends each statement to *into*.

    Not ``conn.set_trace_callback(into.append)``: 3.6's sqlite3 keeps
    registered callbacks in an internal dict, so the callable must be
    hashable, and a bound ``list.append`` hashes via the list — a
    TypeError on the deployment interpreter. 3.8+ stores the callback
    as a plain attribute, so the difference is invisible on any dev
    machine. The lambda is the fix, not decoration.
    """
    conn.set_trace_callback(lambda statement: into.append(statement))


def make_record(
    environment: str = "linux-sim",
    script: str = "suite.py",
    test_name: str = "test_a",
    result: Result = Result.PASS,
    start: Optional[datetime.datetime] = None,
    end: Optional[datetime.datetime] = None,
    output: str = "all good\n",
    source_link: str = "https://example.com/suite.py#L1",
    known_failure_reason: Optional[str] = None,
    build: Optional[str] = None,
) -> RunRecord:
    """Build a RunRecord with sensible defaults for tests."""
    if start is None:
        start = BASE
    if end is None:
        end = start + datetime.timedelta(seconds=3)
    return RunRecord(
        environment=environment,
        script=script,
        test_name=test_name,
        result=result,
        start_time=start,
        end_time=end,
        output=output,
        source_link=source_link,
        known_failure_reason=known_failure_reason,
        build=build,
    )


class StorageTestBase(unittest.TestCase):
    """Creates a Storage on a temp-file database with safe cleanup."""

    def _make_storage(self) -> Storage:
        """The backend under test. tests/test_mariadb_backend.py
        overrides this to run the same tests against MariaDB."""
        return Storage(self.db_path)

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="testboard_storage_")
        # LIFO cleanup: connections are closed before the dir is removed.
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = self._make_storage()
        self.addCleanup(self.store.close)


class TestMigrations(StorageTestBase):
    """Schema creation, versioning and idempotency."""

    def _fetch_schema(self) -> List[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'index')"
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

    def test_creates_all_tables_and_indexes(self) -> None:
        names = self._fetch_schema()
        for expected in (
            "schema_version",
            "runs",
            "users",
            "comments",
            "assignments",
            "idx_comments_triple",
            "idx_assignments_triple",
        ):
            self.assertIn(expected, names)

    def test_schema_version_is_current(self) -> None:
        latest = storage.MIGRATIONS[-1][0]
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(latest,)])

    def test_wal_mode_enabled(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(mode).lower(), "wal")

    def test_migrations_idempotent_reopen_same_file(self) -> None:
        self.store.upsert_runs([make_record()])
        second = Storage(self.db_path)
        self.addCleanup(second.close)
        # Version unchanged, single row, data intact.
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(storage.MIGRATIONS[-1][0],)])
        self.assertTrue(second.test_exists("linux-sim", "suite.py", "test_a"))

    def test_memory_database_works_single_threaded(self) -> None:
        mem = Storage(":memory:")
        self.addCleanup(mem.close)
        counts = mem.upsert_runs([make_record()])
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=1, updated=0, unchanged=0, rejections=[]),
        )
        self.assertTrue(mem.test_exists("linux-sim", "suite.py", "test_a"))


class TestUpsertRuns(StorageTestBase):
    """Explicit upsert semantics and counts."""

    def test_insert_counts(self) -> None:
        counts = self.store.upsert_runs(
            [
                make_record(test_name="test_a"),
                make_record(test_name="test_b"),
            ]
        )
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=2, updated=0, unchanged=0, rejections=[]),
        )

    def test_mixed_insert_and_update_counts(self) -> None:
        self.store.upsert_runs([make_record(test_name="test_a")])
        counts = self.store.upsert_runs(
            [
                make_record(test_name="test_a", result=Result.FAIL),
                make_record(test_name="test_b"),
            ]
        )
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=1, updated=1, unchanged=0, rejections=[]),
        )

    def test_reimport_identical_batch_is_unchanged_no_duplicates(self) -> None:
        """A byte-identical re-import writes nothing and reports it.

        This used to expect ``updated=3``: the second import rewrote
        every row and its output blob to store what was already there.
        The site feeder re-pushes its whole recent window every 10
        minutes, so that churn was ~23 MB of WAL per push through the
        production network mount — the skip is the point, and this pin
        is what keeps it.
        """
        batch = [
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
            make_record(test_name="test_c"),
        ]
        first = self.store.upsert_runs(batch)
        second = self.store.upsert_runs(batch)
        self.assertEqual(
            first,
            storage.UpsertCounts(inserted=3, updated=0, unchanged=0, rejections=[]),
        )
        self.assertEqual(
            second,
            storage.UpsertCounts(inserted=0, updated=0, unchanged=3, rejections=[]),
        )
        self.assertEqual(len(self.store.dashboard()), 3)
        history = self.store.run_history("linux-sim", "suite.py", "test_a")
        self.assertEqual(len(history), 1)

    def test_update_replaces_mutable_fields_and_keeps_run_id(self) -> None:
        self.store.upsert_runs([make_record()])
        original = self.store.latest_run("linux-sim", "suite.py", "test_a")
        assert original is not None
        changed = make_record(
            result=Result.FAIL,
            end=BASE + datetime.timedelta(seconds=99),
            output="Traceback: boom\n",
            source_link="https://example.com/new#L5",
            known_failure_reason="JIRA-123",
        )
        counts = self.store.upsert_runs([changed])
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=0, updated=1, unchanged=0, rejections=[]),
        )
        run = self.store.get_run(original.run_id)
        assert run is not None
        self.assertEqual(run.run_id, original.run_id)  # rowid preserved
        self.assertEqual(run.result, Result.FAIL)
        self.assertEqual(
            run.end_time, BASE + datetime.timedelta(seconds=99)
        )
        self.assertEqual(run.output, "Traceback: boom\n")
        self.assertEqual(run.source_link, "https://example.com/new#L5")
        self.assertEqual(run.known_failure_reason, "JIRA-123")

    def test_same_test_name_different_start_times_are_distinct(self) -> None:
        counts = self.store.upsert_runs(
            [
                make_record(start=BASE),
                make_record(start=BASE + datetime.timedelta(days=1)),
            ]
        )
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=2, updated=0, unchanged=0, rejections=[]),
        )
        history = self.store.run_history("linux-sim", "suite.py", "test_a")
        self.assertEqual(len(history), 2)

    def test_empty_batch(self) -> None:
        counts = self.store.upsert_runs([])
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=0, updated=0, unchanged=0, rejections=[]),
        )


class TestDashboard(StorageTestBase):
    """Latest-run-per-test query with all filters."""

    def setUp(self) -> None:
        super(TestDashboard, self).setUp()
        day = datetime.timedelta(days=1)
        self.store.upsert_runs(
            [
                # test_a: old PASS then newer FAIL (dashboard must show FAIL)
                make_record(test_name="test_a", start=BASE, result=Result.PASS),
                make_record(
                    test_name="test_a", start=BASE + day, result=Result.FAIL
                ),
                make_record(
                    test_name="test_b",
                    start=BASE,
                    result=Result.FAILED_AS_EXPECTED,
                    known_failure_reason="known",
                ),
                make_record(
                    environment="win-uat",
                    script="other.py",
                    test_name="test_c",
                    start=BASE,
                    result=Result.UNEXPECTED_PASS,
                ),
            ]
        )

    def test_latest_run_per_test(self) -> None:
        rows = self.store.dashboard()
        self.assertEqual(len(rows), 3)
        by_name = {row.test_name: row for row in rows}
        self.assertEqual(by_name["test_a"].result, Result.FAIL)
        self.assertEqual(
            by_name["test_a"].start_time,
            BASE + datetime.timedelta(days=1),
        )

    def test_ordering(self) -> None:
        rows = self.store.dashboard()
        triples = [(r.environment, r.script, r.test_name) for r in rows]
        self.assertEqual(triples, sorted(triples))

    def test_row_shape_has_no_output_field(self) -> None:
        self.assertNotIn("output", storage.TestSummaryRow._fields)

    def test_filter_environment(self) -> None:
        rows = self.store.dashboard(environment="win-uat")
        self.assertEqual([r.test_name for r in rows], ["test_c"])
        self.assertEqual(self.store.dashboard(environment="nope"), [])

    def test_filter_environments_list_is_an_or(self) -> None:
        """The WP-20 product filter: an allow-list of environments,
        resolved by the caller from ``environment_products``."""
        rows = self.store.dashboard(environments=["linux-sim", "win-uat"])
        self.assertEqual(
            sorted(r.test_name for r in rows),
            ["test_a", "test_b", "test_c"])
        rows = self.store.dashboard(environments=["win-uat"])
        self.assertEqual([r.test_name for r in rows], ["test_c"])

    def test_filter_environments_empty_list_matches_nothing(self) -> None:
        """A product that resolved to zero environments (unknown, or
        declared with none) must filter out everything, not act as if
        no filter were given."""
        self.assertEqual(self.store.dashboard(environments=[]), [])
        self.assertEqual(
            self.store.dashboard_count(environments=[]), 0)

    def test_filter_environments_none_means_unfiltered(self) -> None:
        self.assertEqual(len(self.store.dashboard(environments=None)), 3)

    def test_filter_script(self) -> None:
        rows = self.store.dashboard(script="other.py")
        self.assertEqual([r.test_name for r in rows], ["test_c"])

    def test_filter_results(self) -> None:
        rows = self.store.dashboard(results=[Result.FAIL])
        self.assertEqual([r.test_name for r in rows], ["test_a"])
        rows = self.store.dashboard(
            results=[Result.FAIL, Result.UNEXPECTED_PASS]
        )
        self.assertEqual(
            sorted(r.test_name for r in rows), ["test_a", "test_c"]
        )

    def test_filter_results_matches_latest_only(self) -> None:
        # test_a has an older PASS run, but its latest run is FAIL, so a
        # PASS filter must not resurface it.
        rows = self.store.dashboard(results=[Result.PASS])
        self.assertEqual(rows, [])

    def test_filter_results_empty_sequence_matches_nothing(self) -> None:
        self.assertEqual(self.store.dashboard(results=[]), [])

    def test_filter_q_substring_case_insensitive(self) -> None:
        rows = self.store.dashboard(q="EST_A")
        self.assertEqual([r.test_name for r in rows], ["test_a"])

    def test_filter_combination(self) -> None:
        rows = self.store.dashboard(
            environment="linux-sim",
            script="suite.py",
            results=[Result.FAIL, Result.FAILED_AS_EXPECTED],
            q="test_a",
        )
        self.assertEqual([r.test_name for r in rows], ["test_a"])
        rows = self.store.dashboard(environment="win-uat", q="test_a")
        self.assertEqual(rows, [])

    def test_assignee_joined_and_none_when_unassigned(self) -> None:
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED
        )
        by_name = {row.test_name: row for row in self.store.dashboard()}
        self.assertEqual(by_name["test_a"].assignee, "alice")
        self.assertIsNone(by_name["test_b"].assignee)

    def test_assignee_reflects_latest_assignment(self) -> None:
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED
        )
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", None, "bob",
            CREATED + datetime.timedelta(minutes=1),
        )
        by_name = {row.test_name: row for row in self.store.dashboard()}
        self.assertIsNone(by_name["test_a"].assignee)


class TestDashboardLikeEscaping(StorageTestBase):
    """q filter must treat %, _ and backslash literally."""

    def setUp(self) -> None:
        super(TestDashboardLikeEscaping, self).setUp()
        minute = datetime.timedelta(minutes=1)
        names = ["load_100%", "load_1000", "a_b", "aXb", "back\\slash"]
        self.store.upsert_runs(
            [
                make_record(test_name=name, start=BASE + i * minute)
                for i, name in enumerate(names)
            ]
        )

    def test_percent_is_literal(self) -> None:
        rows = self.store.dashboard(q="100%")
        self.assertEqual([r.test_name for r in rows], ["load_100%"])

    def test_underscore_is_literal(self) -> None:
        rows = self.store.dashboard(q="a_b")
        self.assertEqual([r.test_name for r in rows], ["a_b"])

    def test_backslash_is_literal(self) -> None:
        rows = self.store.dashboard(q="back\\slash")
        self.assertEqual([r.test_name for r in rows], ["back\\slash"])

    def test_plain_substring_still_matches(self) -> None:
        rows = self.store.dashboard(q="load_")
        self.assertEqual(
            sorted(r.test_name for r in rows), ["load_100%", "load_1000"]
        )


class TestRunQueries(StorageTestBase):
    """test_exists / latest_run / run_history / runs_since / get_run."""

    def setUp(self) -> None:
        super(TestRunQueries, self).setUp()
        self.day = datetime.timedelta(days=1)
        self.starts = [BASE + i * self.day for i in range(5)]
        self.store.upsert_runs(
            [
                make_record(
                    start=start,
                    result=Result.PASS if i % 2 == 0 else Result.FAIL,
                    output="run {}\n".format(i),
                )
                for i, start in enumerate(self.starts)
            ]
        )

    def test_test_exists(self) -> None:
        self.assertTrue(
            self.store.test_exists("linux-sim", "suite.py", "test_a")
        )
        self.assertFalse(
            self.store.test_exists("linux-sim", "suite.py", "missing")
        )
        self.assertFalse(
            self.store.test_exists("other-env", "suite.py", "test_a")
        )

    def test_latest_run(self) -> None:
        run = self.store.latest_run("linux-sim", "suite.py", "test_a")
        assert run is not None
        self.assertEqual(run.start_time, self.starts[-1])
        self.assertIsNone(run.output)

    def test_latest_run_unknown_triple(self) -> None:
        self.assertIsNone(
            self.store.latest_run("linux-sim", "suite.py", "missing")
        )

    def test_history_newest_first_without_output(self) -> None:
        runs = self.store.run_history("linux-sim", "suite.py", "test_a")
        self.assertEqual(
            [r.start_time for r in runs], list(reversed(self.starts))
        )
        for run in runs:
            self.assertIsNone(run.output)

    def test_history_limit(self) -> None:
        runs = self.store.run_history(
            "linux-sim", "suite.py", "test_a", limit=2
        )
        self.assertEqual(
            [r.start_time for r in runs], [self.starts[4], self.starts[3]]
        )

    def test_history_before_is_strict(self) -> None:
        runs = self.store.run_history(
            "linux-sim", "suite.py", "test_a", before=self.starts[2]
        )
        self.assertEqual(
            [r.start_time for r in runs], [self.starts[1], self.starts[0]]
        )

    def test_history_pagination_chain(self) -> None:
        seen = []  # type: List[datetime.datetime]
        before = None  # type: Optional[datetime.datetime]
        while True:
            page = self.store.run_history(
                "linux-sim", "suite.py", "test_a", limit=2, before=before
            )
            if not page:
                break
            seen.extend(run.start_time for run in page)
            before = page[-1].start_time
        self.assertEqual(seen, list(reversed(self.starts)))

    def test_runs_since_inclusive_bound(self) -> None:
        runs = self.store.runs_since(
            "linux-sim", "suite.py", "test_a", self.starts[2], 50
        )
        self.assertEqual(
            [r.start_time for r in runs],
            [self.starts[4], self.starts[3], self.starts[2]],
        )
        for run in runs:
            self.assertIsNone(run.output)

    def test_runs_since_limit_keeps_newest(self) -> None:
        runs = self.store.runs_since(
            "linux-sim", "suite.py", "test_a", self.starts[0], 2
        )
        self.assertEqual(
            [r.start_time for r in runs], [self.starts[4], self.starts[3]]
        )

    def test_runs_since_nothing_in_window(self) -> None:
        runs = self.store.runs_since(
            "linux-sim", "suite.py", "test_a",
            self.starts[-1] + self.day, 50,
        )
        self.assertEqual(runs, [])

    def test_get_run_includes_output(self) -> None:
        latest = self.store.latest_run("linux-sim", "suite.py", "test_a")
        assert latest is not None
        run = self.store.get_run(latest.run_id)
        assert run is not None
        self.assertEqual(run.output, "run 4\n")
        self.assertEqual(run.run_id, latest.run_id)
        self.assertEqual(run.test_name, "test_a")

    def test_get_run_unknown_id(self) -> None:
        self.assertIsNone(self.store.get_run(999999))


class TestUsers(StorageTestBase):
    """ensure_user / create_user / list_users."""

    def test_create_user_new_then_existing(self) -> None:
        user, created = self.store.create_user("alice", CREATED)
        self.assertTrue(created)
        self.assertEqual(user, storage.User("alice", CREATED, None, None))
        later = CREATED + datetime.timedelta(days=1)
        again, created_again = self.store.create_user("alice", later)
        self.assertFalse(created_again)
        # Existing user returned unchanged: original created_at kept.
        self.assertEqual(again, storage.User("alice", CREATED, None, None))

    def test_ensure_user_is_idempotent(self) -> None:
        self.store.ensure_user("bob", CREATED)
        self.store.ensure_user(
            "bob", CREATED + datetime.timedelta(days=1)
        )
        users = self.store.list_users()
        self.assertEqual(users, [storage.User("bob", CREATED, None, None)])

    def test_list_users_ordered_by_username(self) -> None:
        for name in ("carol", "alice", "bob"):
            self.store.ensure_user(name, CREATED)
        self.assertEqual(
            [u.username for u in self.store.list_users()],
            ["alice", "bob", "carol"],
        )

    def test_list_users_empty(self) -> None:
        self.assertEqual(self.store.list_users(), [])


class TestUserDeactivation(StorageTestBase):
    """Migration 2: hiding a user without erasing what they did.

    The motivating case is real: one person in the estate holds two
    usernames, and until now nothing could take the spare out of the
    assignee pickers.
    """

    def setUp(self) -> None:
        StorageTestBase.setUp(self)
        for name in ("alice", "bob"):
            self.store.ensure_user(name, CREATED)

    def test_a_new_user_is_active(self) -> None:
        user, _created = self.store.create_user("carol", CREATED)
        self.assertTrue(user.active)
        self.assertIsNone(user.deactivated_at)

    def test_deactivating_hides_the_user_from_the_default_listing(
        self
    ) -> None:
        """The whole feature, in one assertion.

        The assignee pickers read the default listing, so this is what
        makes a duplicate account stop being offered.
        """
        self.store.set_user_active("bob", False, "alice", CREATED)
        self.assertEqual(
            [u.username for u in self.store.list_users()], ["alice"])

    def test_the_full_roster_is_still_available_on_request(self) -> None:
        self.store.set_user_active("bob", False, "alice", CREATED)
        everyone = self.store.list_users(include_inactive=True)
        self.assertEqual([u.username for u in everyone], ["alice", "bob"])
        self.assertEqual([u.active for u in everyone], [True, False])

    def test_deactivation_records_who_and_when(self) -> None:
        when = CREATED + datetime.timedelta(days=3)
        user = self.store.set_user_active("bob", False, "alice", when)
        self.assertEqual(user.deactivated_at, when)
        self.assertEqual(user.deactivated_by, "alice")
        self.assertFalse(user.active)

    def test_reactivating_leaves_no_trace(self) -> None:
        """Reversible means indistinguishable, not "flagged as back"."""
        self.store.set_user_active("bob", False, "alice", CREATED)
        user = self.store.set_user_active("bob", True, "alice", CREATED)
        self.assertTrue(user.active)
        self.assertIsNone(user.deactivated_at)
        self.assertIsNone(user.deactivated_by)

    def test_history_is_untouched_by_deactivation(self) -> None:
        """A dead account is not a person who never said anything.

        Same reasoning as retiring a test: it leaves the runs alone.
        """
        self.store.add_comment(
            "linux", "a.py", "t", "bob", "looked at this", CREATED)
        self.store.set_user_active("bob", False, "alice", CREATED)
        comments = self.store.comments("linux", "a.py", "t")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].author, "bob")
        self.assertEqual(comments[0].text, "looked at this")

    def test_deactivating_an_unknown_user_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.store.set_user_active("nobody", False, "alice", CREATED)

    def test_an_unknown_username_counts_as_active(self) -> None:
        """Usernames are created implicitly by commenting or assigning,
        so "not in the table" is a new person, not a retired one."""
        self.assertTrue(self.store.is_active_user("brand_new"))
        self.assertTrue(self.store.is_active_user("alice"))
        self.store.set_user_active("alice", False, "bob", CREATED)
        self.assertFalse(self.store.is_active_user("alice"))

    def test_open_assignments_are_counted(self) -> None:
        self.store.upsert_runs([
            make_record("linux", "a.py", "one"),
            make_record("linux", "a.py", "two"),
        ])
        for name in ("one", "two"):
            self.store.set_assignee(
                "linux", "a.py", name, "bob", "alice", CREATED)
        total, sample = self.store.open_assignments_held_by("bob")
        self.assertEqual(total, 2)
        self.assertEqual(
            sample,
            [("linux", "a.py", "one"), ("linux", "a.py", "two")])

    def test_retired_tests_do_not_count_as_open_work(self) -> None:
        """Retirement deliberately leaves the assignment in place.

        Counting those would block deactivation over work that no
        longer exists, and it would never clear on its own.
        """
        self.store.upsert_runs([make_record("linux", "a.py", "gone")])
        self.store.set_assignee(
            "linux", "a.py", "gone", "bob", "alice", CREATED)
        self.assertEqual(
            self.store.open_assignments_held_by("bob")[0], 1)
        self.store.set_retired(
            "linux", "a.py", "gone", True, "alice",
            "no longer in the suite", CREATED)
        self.assertEqual(
            self.store.open_assignments_held_by("bob")[0], 0)

    def test_a_cleared_assignment_is_not_open_work(self) -> None:
        self.store.upsert_runs([make_record("linux", "a.py", "one")])
        self.store.set_assignee(
            "linux", "a.py", "one", "bob", "alice", CREATED)
        self.store.set_assignee(
            "linux", "a.py", "one", None, "alice", CREATED)
        self.assertEqual(
            self.store.open_assignments_held_by("bob")[0], 0)

    def test_the_sample_is_capped_but_the_total_is_not(self) -> None:
        """A user holding a thousand tests must not produce a
        thousand-line error message."""
        records = [make_record("linux", "a.py", "t%03d" % i) for i in range(30)]
        self.store.upsert_runs(records)
        for record in records:
            self.store.set_assignee(
                "linux", "a.py", record.test_name, "bob", "alice", CREATED)
        total, sample = self.store.open_assignments_held_by("bob", limit=5)
        self.assertEqual(total, 30)
        self.assertEqual(len(sample), 5)


class TestMigrationTwoAgainstExistingData(unittest.TestCase):
    """Migration 2 runs against a database that already has users.

    An empty-database test proves the DDL parses. Production is not
    empty, and that is the only place this will ever really run.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="testboard_migrate2_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "v1.db")

    def _build_at_version_one(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for statement in storage.MIGRATIONS[0][1]:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.execute(
                "INSERT INTO users (username, created_at) VALUES (?, ?)",
                ("existing", model.format_iso(CREATED)),
            )
            conn.commit()
        finally:
            conn.close()

    def test_existing_users_survive_and_read_as_active(self) -> None:
        self._build_at_version_one()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        users = store.list_users()
        self.assertEqual([u.username for u in users], ["existing"])
        self.assertTrue(users[0].active)
        self.assertEqual(users[0].created_at, CREATED)

    def test_the_upgraded_database_can_then_deactivate(self) -> None:
        self._build_at_version_one()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        store.set_user_active("existing", False, "existing", CREATED)
        self.assertEqual(store.list_users(), [])
        self.assertEqual(
            len(store.list_users(include_inactive=True)), 1)


class TestLatestRunDuration(StorageTestBase):
    """Migration 3: how long the newest run of each test took.

    Denormalised onto ``latest_runs`` so that "where is the time going"
    is a GROUP BY over one row per test, and so the duration sort stops
    evaluating a date expression over the whole filtered set.
    """

    def _duration(self, test_name: str = "test_a") -> float:
        row = self.store._conn().execute(
            "SELECT duration_seconds FROM latest_runs WHERE test_name = ?",
            (test_name,),
        ).fetchone()
        return row[0]

    def test_it_is_set_on_first_import(self) -> None:
        self.store.upsert_runs([
            make_record(start=BASE, end=BASE + datetime.timedelta(seconds=7))
        ])
        self.assertAlmostEqual(self._duration(), 7.0)

    def test_a_newer_run_replaces_the_duration(self) -> None:
        self.store.upsert_runs([
            make_record(start=BASE, end=BASE + datetime.timedelta(seconds=7))
        ])
        later = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(start=later,
                        end=later + datetime.timedelta(seconds=2))
        ])
        self.assertAlmostEqual(self._duration(), 2.0)

    def test_reimporting_a_run_corrects_its_duration(self) -> None:
        """A re-import exists to repair a record.

        If the duration did not follow, the repair would leave a stale
        number behind — which is exactly the case the feeder's
        force-reload flag is for.
        """
        self.store.upsert_runs([
            make_record(start=BASE, end=BASE + datetime.timedelta(seconds=7))
        ])
        self.store.upsert_runs([
            make_record(start=BASE, end=BASE + datetime.timedelta(seconds=99))
        ])
        self.assertAlmostEqual(self._duration(), 99.0)

    def test_an_older_backfilled_run_does_not_touch_the_duration(
        self
    ) -> None:
        """latest_runs describes the NEWEST run. Importing an older one
        must not repoint the duration at it."""
        self.store.upsert_runs([
            make_record(start=BASE, end=BASE + datetime.timedelta(seconds=7))
        ])
        earlier = BASE - datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(start=earlier,
                        end=earlier + datetime.timedelta(seconds=500))
        ])
        self.assertAlmostEqual(self._duration(), 7.0)

    def test_sub_second_durations_survive(self) -> None:
        """REAL, not INTEGER: most tests take well under a second, and
        rounding them all to zero would make the time view useless."""
        self.store.upsert_runs([
            make_record(
                start=BASE,
                end=BASE + datetime.timedelta(milliseconds=250))
        ])
        self.assertAlmostEqual(self._duration(), 0.25)

    def test_every_row_agrees_with_the_run_it_points_at(self) -> None:
        """The invariant, checked against the source of truth."""
        records = []
        for index in range(12):
            start = BASE + datetime.timedelta(hours=index)
            records.append(make_record(
                test_name="t%02d" % index,
                start=start,
                end=start + datetime.timedelta(seconds=index * 1.5),
            ))
        self.store.upsert_runs(records)
        rows = self.store._conn().execute(
            "SELECT lr.duration_seconds, r.start_time, r.end_time "
            "FROM latest_runs lr JOIN runs r ON r.id = lr.run_id"
        ).fetchall()
        self.assertEqual(len(rows), 12)
        for stored, start_iso, end_iso in rows:
            expected = model.duration_seconds(
                model.parse_iso(start_iso), model.parse_iso(end_iso))
            self.assertAlmostEqual(stored, expected)

    def test_the_duration_sort_still_orders_by_duration(self) -> None:
        lengths = [5, 1, 9, 3]
        for index, seconds in enumerate(lengths):
            self.store.upsert_runs([make_record(
                test_name="t%d" % index,
                start=BASE,
                end=BASE + datetime.timedelta(seconds=seconds),
            )])
        ascending = [
            row.test_name
            for row in self.store.dashboard(sort="duration", limit=10)
        ]
        self.assertEqual(ascending, ["t1", "t3", "t0", "t2"])
        descending = [
            row.test_name
            for row in self.store.dashboard(
                sort="duration", descending=True, limit=10)
        ]
        self.assertEqual(descending, ["t2", "t0", "t3", "t1"])


class TestDurationRollup(StorageTestBase):
    """Where the suite's time went, one level at a time (WP-6)."""

    def setUp(self) -> None:
        StorageTestBase.setUp(self)
        self.cutoff = BASE - datetime.timedelta(hours=1)
        records = []
        for env, script, name, secs in (
            ("linux", "a.py", "one", 10),
            ("linux", "a.py", "two", 5),
            ("linux", "b.py", "three", 20),
            ("win", "a.py", "four", 2),
        ):
            records.append(make_record(
                environment=env, script=script, test_name=name,
                start=BASE, end=BASE + datetime.timedelta(seconds=secs)))
        self.store.upsert_runs(records)

    def test_environments_are_ranked_by_total_time(self) -> None:
        rollup = self.store.duration_rollup("environment", self.cutoff)
        self.assertEqual(
            [(s.key, s.total_seconds, s.test_count) for s in rollup.slices],
            [("linux", 35.0, 3), ("win", 2.0, 1)])
        self.assertEqual(rollup.total_seconds, 37.0)
        self.assertEqual(rollup.test_count, 4)

    def test_drilling_into_an_environment_groups_by_script(self) -> None:
        rollup = self.store.duration_rollup(
            "script", self.cutoff, environment="linux")
        self.assertEqual(
            [(s.key, s.total_seconds) for s in rollup.slices],
            [("b.py", 20.0), ("a.py", 15.0)])

    def test_drilling_into_a_script_groups_by_test(self) -> None:
        rollup = self.store.duration_rollup(
            "test_name", self.cutoff, environment="linux", script="a.py")
        self.assertEqual(
            [(s.key, s.total_seconds) for s in rollup.slices],
            [("one", 10.0), ("two", 5.0)])

    def test_retired_tests_are_excluded(self) -> None:
        """Consistent with every other estate view: not in the suite."""
        self.store.set_retired(
            "linux", "b.py", "three", True, "alice", "gone", CREATED)
        rollup = self.store.duration_rollup("environment", self.cutoff)
        self.assertEqual(rollup.total_seconds, 17.0)
        self.assertEqual(rollup.test_count, 3)

    def test_stale_tests_are_excluded_and_counted(self) -> None:
        """A test that last ran three weeks ago still has a duration.

        Counting it would claim time was spent last night that was not.
        Dropping it silently would present a smaller number as the
        whole, so the count comes back with the answer.
        """
        rollup = self.store.duration_rollup(
            "environment", BASE + datetime.timedelta(days=1))
        self.assertEqual(rollup.slices, [])
        self.assertEqual(rollup.excluded_tests, 4)

    def test_stale_tests_can_be_included_deliberately(self) -> None:
        """An all-or-nothing cutoff blanks the page after a long
        weekend. Including them is opt-in, and reports nothing excluded
        because nothing was."""
        rollup = self.store.duration_rollup("environment", None)
        self.assertEqual(rollup.total_seconds, 37.0)
        self.assertEqual(rollup.excluded_tests, 0)

    def test_environments_list_filter(self) -> None:
        """The WP-20 product filter: an allow-list rather than one exact
        match, so a multi-environment product scopes the same way."""
        rollup = self.store.duration_rollup(
            "environment", self.cutoff, environments=["linux", "win"])
        self.assertEqual(rollup.total_seconds, 37.0)
        rollup = self.store.duration_rollup(
            "environment", self.cutoff, environments=["win"])
        self.assertEqual(
            [(s.key, s.total_seconds) for s in rollup.slices],
            [("win", 2.0)])
        rollup = self.store.duration_rollup(
            "environment", self.cutoff, environments=[])
        self.assertEqual(rollup.slices, [])
        self.assertEqual(rollup.test_count, 0)

    def test_an_unknown_group_by_is_refused(self) -> None:
        """GROUP BY cannot be parameterised, so the whitelist IS the
        security boundary — same rule as DASHBOARD_SORTS."""
        for bad in ("lr.environment; DROP TABLE runs", "output", ""):
            with self.assertRaises(ValueError):
                self.store.duration_rollup(bad, self.cutoff)

    def test_the_rollup_reads_one_row_per_test(self) -> None:
        """It must not become proportional to the run count."""
        for day in range(1, 30):
            self.store.upsert_runs([make_record(
                environment="linux", script="a.py", test_name="one",
                start=BASE - datetime.timedelta(days=day),
                end=BASE - datetime.timedelta(days=day) +
                datetime.timedelta(seconds=99))])
        rollup = self.store.duration_rollup("environment", self.cutoff)
        # Still the LATEST run's 10s, not any of the 99s history.
        self.assertEqual(rollup.total_seconds, 37.0)
        plan = self.store._conn().execute(
            "EXPLAIN QUERY PLAN SELECT lr.environment, "
            "SUM(lr.duration_seconds), COUNT(*) FROM latest_runs lr "
            "LEFT JOIN test_retirements tr "
            "ON tr.environment = lr.environment AND tr.script = lr.script "
            "AND tr.test_name = lr.test_name "
            "WHERE tr.environment IS NULL GROUP BY lr.environment"
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertNotIn(
            "runs", detail.replace("latest_runs", ""),
            "the rollup must not touch the runs table: " + detail)


class TestMigrationThreeBackfill(unittest.TestCase):
    """Migration 3 against a database that already holds runs.

    The backfill is the only part of this round that touches existing
    rows, so it is the only part that can be wrong in a way an empty
    database would never show.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="testboard_migrate3_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "v2.db")

    def _build_at_version_two(self, tests: int = 5) -> List[float]:
        """Build a v2 database with *tests* tests; return their durations."""
        conn = sqlite3.connect(self.db_path)
        expected = []  # type: List[float]
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for version, statements in storage.MIGRATIONS:
                if version > 2:
                    break
                for statement in statements:
                    storage.apply_migration_statement(conn, statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            for index in range(tests):
                start = BASE + datetime.timedelta(hours=index)
                end = start + datetime.timedelta(seconds=index * 2 + 0.5)
                expected.append((end - start).total_seconds())
                conn.execute(
                    "INSERT INTO runs (environment, script, test_name, "
                    "result, start_time, end_time, source_link) "
                    "VALUES ('linux', 's.py', ?, 'PASS', ?, ?, '')",
                    ("t%d" % index, model.format_iso(start),
                     model.format_iso(end)),
                )
                run_id = conn.execute(
                    "SELECT id FROM runs WHERE test_name = ?",
                    ("t%d" % index,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO latest_runs (environment, script, "
                    "test_name, run_id, start_time, result) "
                    "VALUES ('linux', 's.py', ?, ?, ?, 'PASS')",
                    ("t%d" % index, run_id, model.format_iso(start)),
                )
            conn.commit()
        finally:
            conn.close()
        return expected

    def test_every_existing_row_is_backfilled(self) -> None:
        expected = self._build_at_version_two()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        rows = store._conn().execute(
            "SELECT test_name, duration_seconds FROM latest_runs "
            "ORDER BY test_name"
        ).fetchall()
        self.assertEqual(len(rows), len(expected))
        for (name, stored), want in zip(rows, expected):
            self.assertAlmostEqual(stored, want, msg=name)

    def test_no_row_is_left_at_the_placeholder_default(self) -> None:
        """DEFAULT 0 exists only so SQLite can add a NOT NULL column
        without rewriting the table. A row still holding it after the
        migration means the backfill missed it."""
        self._build_at_version_two()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        zeros = store._conn().execute(
            "SELECT COUNT(*) FROM latest_runs WHERE duration_seconds = 0"
        ).fetchone()[0]
        self.assertEqual(zeros, 0)

    def test_the_backfill_reads_one_row_per_test_not_per_run(self) -> None:
        """The reason it is affordable in a startup migration.

        latest_runs holds one row per test; `runs` holds every run ever.
        A backfill that walked `runs` would be minutes of held
        transaction against production instead of milliseconds.
        """
        self._build_at_version_two(tests=3)
        conn = sqlite3.connect(self.db_path)
        try:
            # Add history: many runs, still only three tests.
            for index in range(3):
                for extra in range(1, 40):
                    start = BASE + datetime.timedelta(hours=index,
                                                      minutes=extra)
                    conn.execute(
                        "INSERT INTO runs (environment, script, test_name, "
                        "result, start_time, end_time, source_link) "
                        "VALUES ('linux', 's.py', ?, 'PASS', ?, ?, '')",
                        ("t%d" % index, model.format_iso(start),
                         model.format_iso(
                             start + datetime.timedelta(seconds=1))),
                    )
            conn.commit()
            total_runs = conn.execute(
                "SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(total_runs, 100)

        seen = []  # type: List[str]
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        conn = store._conn()
        trace_sql_into(conn, seen)
        try:
            storage._backfill_latest_durations(conn)
        finally:
            conn.set_trace_callback(None)
        # One SELECT plus the executemany, regardless of run count.
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1, seen)
        self.assertIn("latest_runs", selects[0])


class TestNoSqliteDateFunctions(unittest.TestCase):
    """julianday() is gone, and must stay gone.

    It was the codebase's only SQLite-specific date function. Removing
    it (migration 3) is the one piece of MariaDB portability work that
    could be finished and verified without a MariaDB anywhere near the
    machine, so it is worth a guard that says so.
    """

    #: SQLite date/time functions. MariaDB either lacks these or gives
    #: them different semantics, and all of them would silently return
    #: something rather than failing loudly.
    BANNED = ("julianday", "strftime", "unixepoch")

    def _sql_literals(self) -> List[str]:
        """Every non-docstring string literal in storage.py.

        Scanning the raw file text does not work: ``date(`` is a
        substring of ``update(`` and ``validate(``, and ``datetime(`` is
        ordinary Python. Only string literals can reach the database, so
        those are what get scanned — and docstrings are excluded,
        because prose explaining why julianday was removed must not
        register as a use of it.

        The scan itself is test_sql_portability's — same file, same
        docstring exclusion, and that copy knows the 3.6 parser emits
        ``ast.Str`` where 3.8+ emits ``ast.Constant``. A second copy
        here had only the Constant arm and found nothing on the ubi8
        CI leg, which is exactly the drift sharing prevents.
        """
        from tests.test_sql_portability import sql_literals
        return sql_literals()

    def test_the_literal_scan_finds_the_sql(self) -> None:
        """A scan that matched nothing would pass forever."""
        literals = self._sql_literals()
        self.assertGreater(len(literals), 100)
        self.assertTrue(
            any("SELECT" in text and "latest_runs" in text
                for text in literals),
            "the scan did not find storage.py's SQL")

    def test_no_sqlite_date_function_reaches_the_database(self) -> None:
        pattern = re.compile(
            r"\b(" + "|".join(self.BANNED) + r")\s*\(", re.IGNORECASE)
        offenders = [
            text for text in self._sql_literals() if pattern.search(text)
        ]
        self.assertEqual(
            offenders, [],
            "SQLite-specific date functions have no MariaDB equivalent "
            "with the same semantics. Compute the value in Python and "
            "store it, the way migration 3 did for durations")

    def test_the_detector_would_catch_a_reintroduction(self) -> None:
        pattern = re.compile(
            r"\b(" + "|".join(self.BANNED) + r")\s*\(", re.IGNORECASE)
        self.assertTrue(pattern.search(
            "ORDER BY julianday(r.end_time) - julianday(r.start_time)"))
        self.assertTrue(pattern.search("SELECT strftime('%s', start_time)"))
        # ...and would not fire on ordinary identifiers that contain it.
        self.assertIsNone(pattern.search("UPDATE latest_runs SET x = ?"))
        self.assertIsNone(pattern.search("SELECT validate(x)"))


class TestComments(StorageTestBase):
    """Comment storage and implicit user creation."""

    def test_add_comment_implicitly_creates_author(self) -> None:
        comment = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "alice", "looks flaky",
            CREATED,
        )
        self.assertEqual(comment.author, "alice")
        self.assertEqual(comment.text, "looks flaky")
        self.assertEqual(comment.created_at, CREATED)
        self.assertEqual(comment.environment, "linux-sim")
        self.assertEqual(comment.script, "suite.py")
        self.assertEqual(comment.test_name, "test_a")
        self.assertEqual(
            [u.username for u in self.store.list_users()], ["alice"]
        )

    def test_comments_oldest_first(self) -> None:
        first = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "alice", "first", CREATED
        )
        second = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "bob", "second",
            CREATED + datetime.timedelta(minutes=1),
        )
        listed = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual(
            [c.comment_id for c in listed],
            [first.comment_id, second.comment_id],
        )
        self.assertEqual([c.text for c in listed], ["first", "second"])
        self.assertLess(first.comment_id, second.comment_id)

    def test_comments_scoped_to_triple(self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "alice", "here", CREATED
        )
        self.assertEqual(
            self.store.comments("linux-sim", "suite.py", "test_b"), []
        )

    def test_comment_roundtrip_from_storage(self) -> None:
        added = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "alice", "hi", CREATED
        )
        listed = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual(listed, [added])


class TestAssignments(StorageTestBase):
    """Assignment history, current assignee, clearing, implicit users."""

    TRIPLE = ("linux-sim", "suite.py", "test_a")

    def test_set_assignee_implicitly_creates_both_users(self) -> None:
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            "alice", "bob", CREATED,
        )
        self.assertEqual(
            [u.username for u in self.store.list_users()],
            ["alice", "bob"],
        )
        self.assertEqual(
            self.store.current_assignee(*self.TRIPLE), "alice"
        )

    def test_reassignment_latest_wins(self) -> None:
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            "alice", "bob", CREATED,
        )
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            "carol", "bob", CREATED + datetime.timedelta(minutes=1),
        )
        self.assertEqual(
            self.store.current_assignee(*self.TRIPLE), "carol"
        )

    def test_clearing_with_none_only_creates_assigned_by(self) -> None:
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            "alice", "bob", CREATED,
        )
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            None, "dave", CREATED + datetime.timedelta(minutes=1),
        )
        self.assertIsNone(self.store.current_assignee(*self.TRIPLE))
        self.assertEqual(
            [u.username for u in self.store.list_users()],
            ["alice", "bob", "dave"],
        )

    def test_never_assigned_is_none(self) -> None:
        self.assertIsNone(self.store.current_assignee(*self.TRIPLE))

    def test_assignment_scoped_to_triple(self) -> None:
        self.store.set_assignee(
            self.TRIPLE[0], self.TRIPLE[1], self.TRIPLE[2],
            "alice", "bob", CREATED,
        )
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_b")
        )


class TestAssignmentStreamId(StorageTestBase):
    """``stream_id`` on set_assignee (WP-21, folded into migration 9):
    WHERE an assignment was made from, an annotation on
    assignments/current_assignments, never a partition of who owns the
    test — the assignee itself is read the same way regardless
    (docs/STREAMS_PLAN.md §3.4/§3.6)."""

    TRIPLE = ("linux-sim", "suite.py", "test_a")

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([make_record(build="feat/x")])
        self.stream_id = self.store.list_streams("")[0].stream_id

    def _current_stream_id(self) -> Optional[int]:
        """Read ``current_assignments.stream_id`` directly — there is no
        higher-level accessor for it alone (the dashboard/queue reads
        return the whole row; a bare assignment needs no test to
        exist)."""
        row = self.store._conn().execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = ? AND script = ? AND test_name = ?",
            self.TRIPLE,
        ).fetchone()
        return None if row is None else row[0]

    def test_defaults_to_none(self) -> None:
        self.store.set_assignee(
            *self.TRIPLE, assignee="alice", assigned_by="bob",
            assigned_at=CREATED,
        )
        self.assertIsNone(self._current_stream_id())

    def test_round_trips_when_given(self) -> None:
        self.store.set_assignee(
            *self.TRIPLE, assignee="alice", assigned_by="bob",
            assigned_at=CREATED, stream_id=self.stream_id,
        )
        self.assertEqual(self._current_stream_id(), self.stream_id)
        # The assignee itself is unaffected -- it is not partitioned by
        # stream_id, only annotated with it.
        self.assertEqual(self.store.current_assignee(*self.TRIPLE), "alice")

    def test_reassignment_updates_the_stream_id_too(self) -> None:
        self.store.set_assignee(
            *self.TRIPLE, assignee="alice", assigned_by="bob",
            assigned_at=CREATED, stream_id=self.stream_id,
        )
        self.store.set_assignee(
            *self.TRIPLE, assignee="carol", assigned_by="bob",
            assigned_at=CREATED + datetime.timedelta(minutes=1),
        )
        self.assertIsNone(self._current_stream_id())
        self.assertEqual(self.store.current_assignee(*self.TRIPLE), "carol")

    def test_clearing_can_still_carry_a_stream_id(self) -> None:
        """Clearing FROM a branch's view is itself a decision made
        there -- the annotation is not only for the positive case."""
        self.store.set_assignee(
            *self.TRIPLE, assignee=None, assigned_by="bob",
            assigned_at=CREATED, stream_id=self.stream_id,
        )
        self.assertIsNone(self.store.current_assignee(*self.TRIPLE))
        self.assertEqual(self._current_stream_id(), self.stream_id)

    def test_the_history_row_also_carries_it(self) -> None:
        self.store.set_assignee(
            *self.TRIPLE, assignee="alice", assigned_by="bob",
            assigned_at=CREATED, stream_id=self.stream_id,
        )
        row = self.store._conn().execute(
            "SELECT stream_id FROM assignments WHERE "
            "environment = ? AND script = ? AND test_name = ? "
            "ORDER BY id DESC LIMIT 1",
            self.TRIPLE,
        ).fetchone()
        self.assertEqual(row[0], self.stream_id)


class TestBulkSetAssignee(StorageTestBase):
    """Storage.bulk_set_assignee — Open Actions' bulk assign/unassign
    (2026-08-10, found while cleaning up assignments a dead build left
    behind): the SAME filters :meth:`Storage.dashboard`/
    :meth:`Storage.dashboard_count` read, acting on the whole matched
    set in one transaction instead of one row at a time.
    """

    def setUp(self) -> None:
        super(TestBulkSetAssignee, self).setUp()
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE, result=Result.FAIL),
            make_record(test_name="test_b", start=BASE, result=Result.PASS),
            make_record(
                environment="win-uat", script="other.py",
                test_name="test_c", start=BASE,
                result=Result.UNEXPECTED_PASS,
            ),
        ])
        # A build stream, and test_b already carrying an assignment made
        # FROM it -- proves a bulk op clears that origin the same way a
        # row-level re-assign from Open Actions already does (it, too,
        # sends no stream_id; assignments are estate-level,
        # docs/STREAMS_PLAN.md §0.4).
        self.store.upsert_runs([make_record(
            test_name="test_b", start=BASE, result=Result.PASS,
            build="feat/x")])
        self.build_stream_id = self.store.list_streams("")[0].stream_id
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_b", "carol", "dave", CREATED,
            stream_id=self.build_stream_id,
        )

    def test_bulk_assign_only_touches_matched_rows(self) -> None:
        updated = self.store.bulk_set_assignee(
            "alice", "bob", CREATED, results=[Result.FAIL])
        self.assertEqual(updated, 1)
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"),
            "alice")
        # test_b was untouched by a FAIL-only filter -- still carol's.
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"),
            "carol")

    def test_bulk_assign_returns_the_matched_count(self) -> None:
        updated = self.store.bulk_set_assignee("alice", "bob", CREATED)
        self.assertEqual(updated, 3)
        self.assertEqual(updated, self.store.dashboard_count())

    def test_bulk_unassign_clears_only_matched_rows(self) -> None:
        self.store.bulk_set_assignee("alice", "bob", CREATED)
        updated = self.store.bulk_set_assignee(
            None, "bob", CREATED + datetime.timedelta(minutes=1),
            environment="linux-sim")
        self.assertEqual(updated, 2)
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"))
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"))
        # win-uat's test_c was outside the environment filter.
        self.assertEqual(
            self.store.current_assignee("win-uat", "other.py", "test_c"),
            "alice")

    def test_bulk_assign_over_a_never_assigned_row_inserts(self) -> None:
        updated = self.store.bulk_set_assignee(
            "alice", "bob", CREATED, environment="win-uat")
        self.assertEqual(updated, 1)
        self.assertEqual(
            self.store.current_assignee("win-uat", "other.py", "test_c"),
            "alice")

    def test_bulk_assign_over_an_already_assigned_row_updates(self) -> None:
        updated = self.store.bulk_set_assignee(
            "alice", "bob", CREATED, environment="linux-sim",
            script="suite.py", q="test_b")
        self.assertEqual(updated, 1)
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"),
            "alice")

    def test_bulk_assign_clears_the_stream_origin(self) -> None:
        """test_b's assignment started build-originated; a bulk assign
        writes NULL, matching a row-level re-assign from Open Actions
        (which also sends no stream_id) — never a partition of who owns
        the test, only an annotation of where it was made from."""
        self.store.bulk_set_assignee(
            "alice", "bob", CREATED, environment="linux-sim",
            script="suite.py", q="test_b")
        conn = self.store._conn()
        current = conn.execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_b'"
        ).fetchone()
        self.assertIsNone(current[0])
        history = conn.execute(
            "SELECT stream_id FROM assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_b' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNone(history[0])

    def test_bulk_unassign_over_a_build_originated_row_also_clears_origin(
        self
    ) -> None:
        self.store.bulk_set_assignee(
            None, "bob", CREATED, environment="linux-sim",
            script="suite.py", q="test_b")
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"))
        current = self.store._conn().execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_b'"
        ).fetchone()
        self.assertIsNone(current[0])

    def test_comment_posted_on_every_matched_test(self) -> None:
        self.store.bulk_set_assignee(
            "alice", "bob", CREATED, comment_text="build looked dead")
        for triple in (
            ("linux-sim", "suite.py", "test_a"),
            ("linux-sim", "suite.py", "test_b"),
            ("win-uat", "other.py", "test_c"),
        ):
            comments = self.store.comments(*triple)
            self.assertEqual(len(comments), 1, triple)
            self.assertEqual(comments[0].author, "bob")
            self.assertEqual(comments[0].text, "build looked dead")
            self.assertIsNone(comments[0].stream_id)

    def test_no_comment_when_comment_text_is_none(self) -> None:
        self.store.bulk_set_assignee("alice", "bob", CREATED)
        self.assertEqual(
            self.store.comments("linux-sim", "suite.py", "test_a"), [])

    def test_empty_comment_text_posts_nothing(self) -> None:
        """Storage's own contract is plain truthiness — an empty string
        is skipped exactly like None. Deciding that WHITESPACE-only
        counts as empty too is the API layer's job
        (testboard.api._handle_bulk_assignments), not storage's."""
        self.store.bulk_set_assignee(
            "alice", "bob", CREATED, comment_text="")
        self.assertEqual(
            self.store.comments("linux-sim", "suite.py", "test_a"), [])

    def test_zero_matches_returns_zero_and_writes_nothing(self) -> None:
        # setUp's build-originated assignment already created carol/dave;
        # the assertion is that a zero-match call creates no ONE else,
        # not that the estate has no users at all.
        before = self.store.list_users()
        updated = self.store.bulk_set_assignee(
            "alice", "bob", CREATED, environment="does-not-exist")
        self.assertEqual(updated, 0)
        self.assertEqual(self.store.list_users(), before)

    def test_implicitly_creates_both_users(self) -> None:
        self.store.bulk_set_assignee("alice", "bob", CREATED)
        self.assertEqual(
            sorted(u.username for u in self.store.list_users()),
            ["alice", "bob", "carol", "dave"])

    def test_history_row_is_appended_for_every_matched_triple(self) -> None:
        self.store.bulk_set_assignee("alice", "bob", CREATED)
        count = self.store._conn().execute(
            "SELECT COUNT(*) FROM assignments WHERE assignee = 'alice'"
        ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_invalidates_the_summary_cache(self) -> None:
        """queue_counts is memoized (_summary_cache) and its "mine"
        column reads current_assignments — the same surface
        set_assignee's own invalidation test
        (test_set_assignee_invalidates_queue_counts) pins for a single
        row. "mine" counts the ``assigned`` queue predicate (FAIL or
        UNEXPECTED_PASS with a current assignee — see QUEUE_KINDS),
        which is test_a (FAIL) and test_c (UNEXPECTED_PASS) here, not
        test_b (PASS): the count below is 2, not "every matched row",
        deliberately."""
        stale_before = BASE + datetime.timedelta(hours=1)
        before = self.store.queue_counts(
            stale_before=stale_before, assignee="alice")
        self.assertEqual(before["mine"], 0)
        self.store.bulk_set_assignee("alice", "bob", CREATED)
        after = self.store.queue_counts(
            stale_before=stale_before, assignee="alice")
        self.assertEqual(
            after["mine"], 2,
            "the memo kept serving zero assigned tests")

    def test_atomicity_a_failure_mid_transaction_writes_nothing(
        self
    ) -> None:
        """A synthetic failure on the current_assignments write — AFTER
        the assignments-history executemany has already run inside the
        SAME transaction — must leave NEITHER applied: proof the whole
        operation commits or rolls back as one unit, not table by
        table. sqlite3.Connection does not allow monkeypatching its own
        bound methods (a read-only attribute on the C type), so this
        substitutes Storage._conn() with a thin proxy for the duration
        of one call, restored in `finally`.
        """
        real_conn = self.store._conn()

        class _FlakyConn(object):
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *a: object, **kw: object) -> object:
                return self._real.execute(sql, *a, **kw)

            def executemany(
                self, sql: str, *a: object, **kw: object
            ) -> object:
                if "current_assignments" in sql:
                    raise sqlite3.OperationalError("synthetic failure")
                return self._real.executemany(sql, *a, **kw)

        proxy = _FlakyConn(real_conn)
        self.store._conn = lambda: proxy  # type: ignore[method-assign]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.bulk_set_assignee("alice", "bob", CREATED)
        finally:
            del self.store._conn  # restores the class's bound method
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"))
        count = self.store._conn().execute(
            "SELECT COUNT(*) FROM assignments WHERE assignee = 'alice'"
        ).fetchone()[0]
        self.assertEqual(
            count, 0, "the history row survived a rolled-back transaction")

    def test_the_query_count_is_bounded_not_per_row(self) -> None:
        """500 matched tests must not mean 500 SELECTs: one SELECT for
        the matched set (already LEFT JOINed to current_assignments, so
        it doubles as the existence check for the upsert below it) plus
        one each for ensure_user(bob)/ensure_user(alice) — bounded
        regardless of how many rows match, the same "one SELECT plus
        the executemany" shape ``_backfill_latest_durations`` and
        :meth:`Storage.failure_streak_bounds_many` already use."""
        records = [
            make_record(
                script="bulk.py", test_name="test_{:03d}".format(i),
                start=BASE, result=Result.FAIL,
            )
            for i in range(500)
        ]
        self.store.upsert_runs(records)
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            updated = self.store.bulk_set_assignee(
                "alice", "bob", CREATED, script="bulk.py")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(updated, 500)
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        self.assertEqual(
            len(selects), 3,
            "expected a constant SELECT count regardless of matched "
            "rows, got {0}: {1}".format(len(selects), selects))

    def test_empty_input_issues_no_writes(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            updated = self.store.bulk_set_assignee(
                "alice", "bob", CREATED, environment="does-not-exist")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(updated, 0)
        writes = [
            s for s in seen
            if s.strip().upper().startswith(("INSERT", "UPDATE"))
        ]
        self.assertEqual(writes, [])


class TestBulkSetAssigneeForTriples(StorageTestBase):
    """Storage.bulk_set_assignee_for_triples — the multi-select action
    bar's "assign selected"/"unassign selected" (an EXPLICIT list of
    triples, as opposed to :meth:`TestBulkSetAssignee`'s "everything
    the current filters match"). Shares :meth:`Storage.bulk_set_assignee`'s
    WRITE phase VERBATIM (:meth:`Storage._write_bulk_assignments`) — the
    tests here that overlap with ``TestBulkSetAssignee`` above exist to
    PROVE that sharing, not merely to re-cover the write mechanics.
    """

    def setUp(self) -> None:
        super(TestBulkSetAssigneeForTriples, self).setUp()
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE, result=Result.FAIL),
            make_record(test_name="test_b", start=BASE, result=Result.PASS),
            make_record(
                environment="win-uat", script="other.py",
                test_name="test_c", start=BASE,
                result=Result.UNEXPECTED_PASS,
            ),
        ])
        self.store.upsert_runs([make_record(
            test_name="test_b", start=BASE, result=Result.PASS,
            build="feat/x")])
        self.build_stream_id = self.store.list_streams("")[0].stream_id
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_b", "carol", "dave", CREATED,
            stream_id=self.build_stream_id,
        )

    def test_only_the_named_triples_are_touched(self) -> None:
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None)],
        )
        self.assertEqual((updated, unknown), (1, 0))
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"),
            "alice")
        # test_b was not named -- still carol's.
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"),
            "carol")

    def test_unassign_over_named_triples(self) -> None:
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            None, "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("linux-sim", "suite.py", "test_b", None)],
        )
        self.assertEqual((updated, unknown), (2, 0))
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"))
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"))

    def test_insert_and_update_both_happen_in_one_call(self) -> None:
        """test_a has never been assigned (insert path); test_b already
        has a row (update path) -- both in the SAME call, same as a
        mixed filter-mode match."""
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("linux-sim", "suite.py", "test_b", None)],
        )
        self.assertEqual((updated, unknown), (2, 0))
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"),
            "alice")
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"),
            "alice")

    def test_a_stream_id_per_triple_is_written_as_its_origin(self) -> None:
        """The BUILD delta table's own selection: this triple's row
        carries the SAME stream_id a row-level assign from that page
        would send (docs/STREAMS_PLAN.md §3.4/§3.6) — unlike filter
        mode, which always writes NULL."""
        self.store.upsert_runs([make_record(
            environment="win-uat", script="other.py", test_name="test_c",
            start=BASE, result=Result.UNEXPECTED_PASS, build="feat/y")])
        other_stream_id = [
            s.stream_id for s in self.store.list_streams("")
            if s.name == "feat/y"
        ][0]
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", self.build_stream_id),
             ("win-uat", "other.py", "test_c", other_stream_id)],
        )
        self.assertEqual((updated, unknown), (2, 0))
        conn = self.store._conn()
        row = conn.execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_a'"
        ).fetchone()
        self.assertEqual(row[0], self.build_stream_id)
        row = conn.execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'win-uat' AND script = 'other.py' "
            "AND test_name = 'test_c'"
        ).fetchone()
        self.assertEqual(row[0], other_stream_id)
        history = conn.execute(
            "SELECT stream_id FROM assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_a' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(history[0], self.build_stream_id)

    def test_a_triple_with_no_origin_writes_null(self) -> None:
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None)],
        )
        self.assertEqual((updated, unknown), (1, 0))
        current = self.store._conn().execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_a'"
        ).fetchone()
        self.assertIsNone(current[0])

    def test_unknown_triples_are_skipped_not_a_hard_failure(self) -> None:
        """A page can be stale (a retirement, or a triple that never
        reported) -- unknown triples are counted, never fail the call."""
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("linux-sim", "suite.py", "no_such_test", None),
             ("nowhere", "nothing.py", "ghost", None)],
        )
        self.assertEqual((updated, unknown), (1, 2))
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"),
            "alice")

    def test_every_triple_unknown_writes_nothing_and_creates_no_user(
        self
    ) -> None:
        before = self.store.list_users()
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("nowhere", "nothing.py", "ghost", None)],
        )
        self.assertEqual((updated, unknown), (0, 1))
        self.assertEqual(self.store.list_users(), before)

    def test_a_duplicated_triple_counts_once(self) -> None:
        """updated + unknown is the DISTINCT triple count, not
        len(triples) -- a duplicate keeps its LAST origin."""
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("linux-sim", "suite.py", "test_a", self.build_stream_id)],
        )
        self.assertEqual((updated, unknown), (1, 0))
        current = self.store._conn().execute(
            "SELECT stream_id FROM current_assignments WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_a'"
        ).fetchone()
        self.assertEqual(current[0], self.build_stream_id)

    def test_comment_posted_on_every_named_triple(self) -> None:
        self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("win-uat", "other.py", "test_c", None)],
            comment_text="build looked dead",
        )
        for triple in (
            ("linux-sim", "suite.py", "test_a"),
            ("win-uat", "other.py", "test_c"),
        ):
            comments = self.store.comments(*triple)
            self.assertEqual(len(comments), 1, triple)
            self.assertEqual(comments[0].text, "build looked dead")
        # Untouched (not in the list) -- no comment.
        self.assertEqual(
            self.store.comments("linux-sim", "suite.py", "test_b"), [])

    def test_zero_triples_returns_zero_and_zero(self) -> None:
        updated, unknown = self.store.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED, [])
        self.assertEqual((updated, unknown), (0, 0))

    def test_filter_mode_and_triples_mode_leave_identical_table_states(
        self
    ) -> None:
        """The SAME matched set, reached the two different ways, must
        leave storage in the SAME state -- the proof the write phase is
        genuinely shared, not two implementations that happen to agree
        today."""
        # A second, identically-seeded store for the filter-mode side --
        # a fresh temp file rather than self.store, so the two writes
        # below cannot see or interfere with each other.
        second_dir = tempfile.mkdtemp(prefix="testboard_storage_cmp_")
        self.addCleanup(shutil.rmtree, second_dir, True)
        store_b = Storage(os.path.join(second_dir, "test.db"))
        self.addCleanup(store_b.close)
        store_b.upsert_runs([
            make_record(test_name="test_a", start=BASE, result=Result.FAIL),
            make_record(test_name="test_b", start=BASE, result=Result.PASS),
        ])
        store_a_env_only = self.store
        store_a_env_only.bulk_set_assignee_for_triples(
            "alice", "bob", CREATED,
            [("linux-sim", "suite.py", "test_a", None),
             ("linux-sim", "suite.py", "test_b", None)],
            comment_text="note",
        )
        store_b.bulk_set_assignee(
            "alice", "bob", CREATED, environment="linux-sim",
            comment_text="note",
        )
        for triple in (
            ("linux-sim", "suite.py", "test_a"),
            ("linux-sim", "suite.py", "test_b"),
        ):
            self.assertEqual(
                store_a_env_only.current_assignee(*triple),
                store_b.current_assignee(*triple), triple)
            comments_a = [c.text for c in store_a_env_only.comments(*triple)]
            comments_b = [c.text for c in store_b.comments(*triple)]
            self.assertEqual(comments_a, comments_b, triple)

    def test_atomicity_a_failure_mid_transaction_writes_nothing(
        self
    ) -> None:
        real_conn = self.store._conn()

        class _FlakyConn(object):
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *a: object, **kw: object) -> object:
                return self._real.execute(sql, *a, **kw)

            def executemany(
                self, sql: str, *a: object, **kw: object
            ) -> object:
                if "current_assignments" in sql:
                    raise sqlite3.OperationalError("synthetic failure")
                return self._real.executemany(sql, *a, **kw)

        proxy = _FlakyConn(real_conn)
        self.store._conn = lambda: proxy  # type: ignore[method-assign]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.bulk_set_assignee_for_triples(
                    "alice", "bob", CREATED,
                    [("linux-sim", "suite.py", "test_a", None)],
                )
        finally:
            del self.store._conn
        self.assertIsNone(
            self.store.current_assignee("linux-sim", "suite.py", "test_a"))
        count = self.store._conn().execute(
            "SELECT COUNT(*) FROM assignments WHERE assignee = 'alice'"
        ).fetchone()[0]
        self.assertEqual(
            count, 0, "the history row survived a rolled-back transaction")

    def test_the_query_count_is_flat_in_selected_row_count(self) -> None:
        """250 selected triples (more than one _RECENT_CHUNK, WP-24's
        list-mode-scale check) must not mean 250 SELECTs -- one SELECT
        per chunk of 100 (ceil(250/100) = 3) plus two for
        ensure_user(bob)/ensure_user(alice), each one SELECT
        (:meth:`Storage.ensure_user`'s own existence check)."""
        records = [
            make_record(
                script="bulk.py", test_name="test_{:03d}".format(i),
                start=BASE, result=Result.FAIL,
            )
            for i in range(250)
        ]
        self.store.upsert_runs(records)
        triples = [
            ("linux-sim", "bulk.py", "test_{:03d}".format(i), None)
            for i in range(250)
        ]
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            updated, unknown = self.store.bulk_set_assignee_for_triples(
                "alice", "bob", CREATED, triples)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((updated, unknown), (250, 0))
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        self.assertEqual(
            len(selects), 5,
            "expected a chunk-bounded SELECT count (ceil(250/100)=3 "
            "chunks + 2 ensure_user checks), got {0}: {1}".format(
                len(selects), selects))

    def test_500_triples_costs_five_chunks_not_five_hundred_selects(
        self
    ) -> None:
        """A second point on the same line as the 250-row test above --
        proves the count SCALES with chunks, not with a fixed constant
        that happened to match one input size."""
        records = [
            make_record(
                script="bulk.py", test_name="test_{:03d}".format(i),
                start=BASE, result=Result.FAIL,
            )
            for i in range(500)
        ]
        self.store.upsert_runs(records)
        triples = [
            ("linux-sim", "bulk.py", "test_{:03d}".format(i), None)
            for i in range(500)
        ]
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            updated, unknown = self.store.bulk_set_assignee_for_triples(
                "alice", "bob", CREATED, triples)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((updated, unknown), (500, 0))
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        # ceil(500/100)=5 chunks + 2 ensure_user checks.
        self.assertEqual(len(selects), 7, selects)

    def test_empty_input_issues_no_writes(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            updated, unknown = self.store.bulk_set_assignee_for_triples(
                "alice", "bob", CREATED, [])
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((updated, unknown), (0, 0))
        writes = [
            s for s in seen
            if s.strip().upper().startswith(("INSERT", "UPDATE"))
        ]
        self.assertEqual(writes, [])


class TestThreading(StorageTestBase):
    """Per-thread connections: writes in one thread visible in another."""

    def test_write_in_worker_thread_visible_in_main_thread(self) -> None:
        errors = []  # type: List[BaseException]

        def worker() -> None:
            try:
                self.store.upsert_runs(
                    [make_record(test_name="from_thread")]
                )
            except BaseException as exc:  # pragma: no cover - diagnostics
                errors.append(exc)
            finally:
                # Each thread must close its own connection.
                self.store.close()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(
            self.store.test_exists("linux-sim", "suite.py", "from_thread")
        )

    def test_close_is_per_thread_and_reopens_lazily(self) -> None:
        self.store.upsert_runs([make_record()])
        self.store.close()
        self.store.close()  # double close is harmless
        # A subsequent call lazily reopens the calling thread's connection.
        self.assertTrue(
            self.store.test_exists("linux-sim", "suite.py", "test_a")
        )


class TestLargeBatch(StorageTestBase):
    """5000-record import in one call must be fast (single transaction)."""

    def test_5000_runs_import_under_10_seconds(self) -> None:
        minute = datetime.timedelta(minutes=1)
        records = []  # type: List[RunRecord]
        for i in range(5000):
            records.append(
                make_record(
                    script="suite_{}.py".format(i % 10),
                    test_name="test_{}".format(i % 50),
                    start=BASE + i * minute,
                    result=Result.PASS if i % 3 else Result.FAIL,
                    output="line one\nline two for run {}\n".format(i),
                )
            )
        started = time.monotonic()
        counts = self.store.upsert_runs(records)
        elapsed = time.monotonic() - started
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=5000, updated=0, unchanged=0, rejections=[])
        )
        self.assertLess(
            elapsed,
            10.0,
            "5000-record batch took {:.2f}s (must be < 10s)".format(elapsed),
        )
        # i % 10 is determined by i % 50, so there are exactly 50 distinct
        # (script, test_name) pairs -> 50 dashboard rows.
        self.assertEqual(len(self.store.dashboard()), 50)


class TestPreviousResult(StorageTestBase):
    """latest_runs.prev_result, the pair every triage queue is built on."""

    def prev_result(self) -> Optional[str]:
        """Read prev_result straight out of the table, bypassing the API.

        Through the store's own connection so the same read works on
        either backend; "bypassing the API" means raw SQL, not a raw
        file handle.
        """
        return self.store._conn().execute(
            "SELECT prev_result FROM latest_runs"
        ).fetchone()[0]

    def test_start_time_index_created(self) -> None:
        """Migrations add the covering (start_time, result) trend index."""
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            conn.close()
        self.assertIn("idx_runs_start_time_result", names)
        self.assertNotIn("idx_runs_start_time", names)
        self.assertIn("idx_latest_runs_result", names)

    def test_single_run_has_no_prev(self) -> None:
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.assertIsNone(self.prev_result())

    def test_prev_is_run_immediately_before_latest(self) -> None:
        """prev_result is the second-newest run, not any older one."""
        self.store.upsert_runs([
            make_record(result=Result.PASS, start=BASE),
            make_record(
                result=Result.FAILED_AS_EXPECTED,
                start=BASE + datetime.timedelta(days=1),
            ),
            make_record(
                result=Result.FAIL,
                start=BASE + datetime.timedelta(days=2),
            ),
        ])
        self.assertEqual(self.prev_result(), "FAILED_AS_EXPECTED")

    def test_prev_tracks_separate_nightly_imports(self) -> None:
        """The common path: one batch per night, each its own transaction."""
        for offset, result in enumerate(
            [Result.PASS, Result.PASS, Result.FAIL]
        ):
            self.store.upsert_runs([make_record(
                result=result, start=BASE + datetime.timedelta(days=offset)
            )])
        self.assertEqual(self.prev_result(), "PASS")

    def test_reimporting_the_latest_run_keeps_its_predecessor(self) -> None:
        """A corrected re-import changes result, never prev_result."""
        newer = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(result=Result.PASS, start=BASE),
            make_record(result=Result.FAIL, start=newer),
        ])
        self.store.upsert_runs([
            make_record(result=Result.FAILED_AS_EXPECTED, start=newer)
        ])
        self.assertEqual(self.prev_result(), "PASS")
        self.assertIs(
            self.store.dashboard()[0].result, Result.FAILED_AS_EXPECTED
        )

    def test_backfilled_run_becomes_the_new_predecessor(self) -> None:
        """A late arrival that lands between two runs re-derives the pair."""
        self.store.upsert_runs([
            make_record(result=Result.PASS, start=BASE),
            make_record(
                result=Result.FAIL,
                start=BASE + datetime.timedelta(days=2),
            ),
        ])
        self.assertEqual(self.prev_result(), "PASS")
        self.store.upsert_runs([make_record(
            result=Result.UNEXPECTED_PASS,
            start=BASE + datetime.timedelta(days=1),
        )])
        self.assertEqual(self.prev_result(), "UNEXPECTED_PASS")

    def test_first_sighting_derives_prev_from_existing_runs(self) -> None:
        """Out-of-order arrival inside one batch still pairs correctly."""
        self.store.upsert_runs([
            make_record(
                result=Result.FAIL,
                start=BASE + datetime.timedelta(days=1),
            ),
            make_record(result=Result.PASS, start=BASE),
        ])
        self.assertEqual(self.prev_result(), "PASS")

    def test_environments_listing(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-b"),
            make_record(environment="env-a", test_name="test_b"),
        ])
        self.assertEqual(self.store.environments(), ["env-a", "env-b"])

    def test_scripts_listing(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-b", script="z.py"),
            make_record(environment="env-a", script="a.py"),
            make_record(environment="env-a", script="m.py"),
        ])
        self.assertEqual(self.store.scripts(), ["a.py", "m.py", "z.py"])
        self.assertEqual(
            self.store.scripts(environment="env-a"), ["a.py", "m.py"]
        )

    def test_environments_narrowed_by_an_allow_list(self) -> None:
        """WP-23 fix: environments() never took a product allow-list at
        all, which is what let /api/summary?product=X return every
        OTHER product's environments too — found live."""
        self.store.upsert_runs([
            make_record(environment="env-a"),
            make_record(environment="env-b"),
            make_record(environment="env-c"),
        ])
        self.assertEqual(
            self.store.environments(environments=["env-a", "env-c"]),
            ["env-a", "env-c"])

    def test_environments_none_means_unfiltered(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-a"),
            make_record(environment="env-b"),
        ])
        self.assertEqual(
            self.store.environments(environments=None),
            self.store.environments())

    def test_environments_empty_allow_list_means_nothing(self) -> None:
        """The established convention (_environments_clause): an
        explicitly EMPTY sequence is "match nothing" (an unknown/
        undeclared product), not "no filter"."""
        self.store.upsert_runs([make_record(environment="env-a")])
        self.assertEqual(self.store.environments(environments=[]), [])

    def test_scripts_narrowed_by_an_allow_list(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-a", script="a.py"),
            make_record(environment="env-b", script="b.py"),
        ])
        self.assertEqual(
            self.store.scripts(environments=["env-a"]), ["a.py"])

    def test_scripts_environment_and_allow_list_combine(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-a", script="a.py"),
            make_record(environment="env-a", script="b.py",
                        test_name="test_b"),
        ])
        self.assertEqual(
            self.store.scripts(
                environment="env-a", environments=["env-a", "env-c"]),
            ["a.py", "b.py"])
        self.assertEqual(
            self.store.scripts(
                environment="env-a", environments=["env-c"]),
            [])


class TestLatestRunsMaintenance(StorageTestBase):
    """latest_runs stays in lockstep with upserts, including backfills."""

    def latest_pointer(self) -> Optional[tuple]:
        return self.store._conn().execute(
            "SELECT run_id, start_time FROM latest_runs WHERE "
            "environment = 'linux-sim' AND script = 'suite.py' "
            "AND test_name = 'test_a'"
        ).fetchone()

    def test_insert_creates_pointer(self) -> None:
        self.store.upsert_runs([make_record()])
        pointer = self.latest_pointer()
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer[1], model.format_iso(BASE))

    def test_newer_run_moves_pointer(self) -> None:
        self.store.upsert_runs([make_record(start=BASE)])
        newer = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(start=newer, result=Result.FAIL)
        ])
        self.assertEqual(self.latest_pointer()[1], model.format_iso(newer))
        rows = self.store.dashboard()
        self.assertIs(rows[0].result, Result.FAIL)

    def test_backfilled_older_run_leaves_pointer(self) -> None:
        self.store.upsert_runs([make_record(start=BASE)])
        older = BASE - datetime.timedelta(days=7)
        self.store.upsert_runs([
            make_record(start=older, result=Result.FAIL)
        ])
        self.assertEqual(self.latest_pointer()[1], model.format_iso(BASE))
        rows = self.store.dashboard()
        self.assertIs(rows[0].result, Result.PASS)

    def test_reimport_of_latest_updates_in_place(self) -> None:
        self.store.upsert_runs([make_record(result=Result.PASS)])
        before = self.latest_pointer()
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        after = self.latest_pointer()
        self.assertEqual(before, after)  # same row, same time
        rows = self.store.dashboard()
        self.assertIs(rows[0].result, Result.FAIL)


class TestDailyResultCounts(StorageTestBase):
    """Per-day, per-result run counts for the trend chart."""

    def test_groups_by_day_and_result(self) -> None:
        day2 = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(test_name="t1", start=BASE),
            make_record(test_name="t2", start=BASE, result=Result.FAIL),
            make_record(test_name="t3", start=BASE),
            make_record(test_name="t1", start=day2, result=Result.FAIL),
        ])
        counts = self.store.daily_result_counts(
            BASE - datetime.timedelta(days=1)
        )
        as_tuples = [(c.day, c.result, c.count) for c in counts]
        self.assertEqual(as_tuples, [
            (BASE.date(), Result.FAIL, 1),
            (BASE.date(), Result.PASS, 2),
            (day2.date(), Result.FAIL, 1),
        ])

    def test_since_is_inclusive_lower_bound(self) -> None:
        day2 = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(test_name="t1", start=BASE),
            make_record(test_name="t1", start=day2),
        ])
        counts = self.store.daily_result_counts(day2)
        self.assertEqual(
            [(c.day, c.count) for c in counts], [(day2.date(), 1)]
        )

    def test_environment_filter(self) -> None:
        self.store.upsert_runs([
            make_record(environment="env-a", start=BASE),
            make_record(environment="env-b", start=BASE),
        ])
        counts = self.store.daily_result_counts(
            BASE - datetime.timedelta(days=1), environment="env-a"
        )
        self.assertEqual([(c.day, c.count) for c in counts],
                         [(BASE.date(), 1)])

    def test_environments_list_filter(self) -> None:
        """The WP-20 product filter: an allow-list of environments."""
        self.store.upsert_runs([
            make_record(environment="env-a", start=BASE),
            make_record(environment="env-b", start=BASE),
            make_record(environment="env-c", start=BASE),
        ])
        counts = self.store.daily_result_counts(
            BASE - datetime.timedelta(days=1),
            environments=["env-a", "env-b"],
        )
        self.assertEqual([(c.day, c.count) for c in counts],
                         [(BASE.date(), 2)])

    def test_environments_list_is_a_distinct_cache_key(self) -> None:
        """A scoped and an unscoped request for the same window must not
        serve each other's memoized answer — the bug this guards is
        silent, not an exception."""
        self.store.upsert_runs([
            make_record(environment="env-a", start=BASE),
            make_record(environment="env-b", start=BASE),
        ])
        since = BASE - datetime.timedelta(days=1)
        unscoped = self.store.daily_result_counts(since)
        scoped = self.store.daily_result_counts(
            since, environments=["env-a"])
        self.assertEqual([(c.day, c.count) for c in unscoped],
                         [(BASE.date(), 2)])
        self.assertEqual([(c.day, c.count) for c in scoped],
                         [(BASE.date(), 1)])
        # Ask again, in reverse order, to prove neither call is serving
        # the other's cached entry now that both are warm.
        self.assertEqual(
            [(c.day, c.count)
             for c in self.store.daily_result_counts(
                 since, environments=["env-a"])],
            [(BASE.date(), 1)])
        self.assertEqual(
            [(c.day, c.count) for c in self.store.daily_result_counts(since)],
            [(BASE.date(), 2)])


class TestDescribeOpenError(unittest.TestCase):
    """A failure to open the database must name the cause AND the fix.

    "Check that the directory exists and is writable" is wrong advice
    for a corrupt file, a locked database or a full disk — and an
    operator reading it should not have to work out which one they hit.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard-openerr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def describe(self, path: str, message: str) -> str:
        return storage.describe_open_error(
            path, sqlite3.OperationalError(message)
        )

    def test_not_a_database_suggests_the_path_or_a_restore(self) -> None:
        path = os.path.join(self.tmp, "junk.db")
        with open(path, "wb") as handle:
            handle.write(b"not a database at all")
        text = self.describe(path, "file is not a database")
        self.assertIn("not a SQLite database", text)
        self.assertIn("backup", text)
        self.assertNotIn("writable", text)

    def test_corruption_suggests_recovery(self) -> None:
        text = self.describe(
            os.path.join(self.tmp, "x.db"), "database disk image is malformed"
        )
        self.assertIn("corrupt", text)
        self.assertIn(".recover", text)

    def test_full_disk_points_at_the_prune_tool(self) -> None:
        text = self.describe(
            os.path.join(self.tmp, "x.db"), "database or disk is full"
        )
        self.assertIn("full", text)
        self.assertIn("prune_runs.py", text)

    def test_locked_explains_the_contention(self) -> None:
        text = self.describe(
            os.path.join(self.tmp, "x.db"), "database is locked"
        )
        self.assertIn("locked by another process", text)

    def test_missing_directory_is_named_with_the_mkdir(self) -> None:
        missing = os.path.join(self.tmp, "no", "such", "dir")
        text = self.describe(
            os.path.join(missing, "x.db"), "unable to open database file"
        )
        self.assertIn("does not exist", text)
        self.assertIn("mkdir", text)
        self.assertIn(missing, text)

    def test_directory_given_instead_of_a_file(self) -> None:
        path = os.path.join(self.tmp, "adir.db")
        os.mkdir(path)
        text = self.describe(path, "unable to open database file")
        self.assertIn("is a directory", text)

    def test_unrecognised_error_still_quotes_sqlite(self) -> None:
        text = self.describe(
            os.path.join(self.tmp, "x.db"), "something entirely new"
        )
        self.assertIn("something entirely new", text)


class TestSchemaVersionGuard(StorageTestBase):
    """A database from a newer testboard is refused, not silently used."""

    def test_newer_schema_is_refused_with_both_versions(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE schema_version SET version = ?",
                         (storage.MIGRATIONS[-1][0] + 5,))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(RuntimeError) as caught:
            Storage(self.db_path).close()
        message = str(caught.exception)
        self.assertIn("NEWER version", message)
        self.assertIn(str(storage.MIGRATIONS[-1][0] + 5), message)

    def test_migration_one_creates_the_schema_and_the_rest_extend_it(
        self
    ) -> None:
        """This test used to assert there was exactly ONE migration.

        That was right while nothing was deployed: with no database in
        service there is no history to preserve, so the schema was kept
        as a single entry that could be edited freely.

        testboard went live on 2026-07-26 and the premise expired. Entry
        1 now describes a database that exists, so the rule inverted:
        never edit it, always append. The count is no longer meaningful
        — it only tells you how many changes have shipped.

        What is still worth asserting is the SHAPE that replaced it:
        entry 1 creates everything outright, and every later entry only
        extends. A later entry containing CREATE TABLE for something
        entry 1 already created would mean the two disagree about the
        same object, and which one a given database got would depend on
        when it was first opened.

        The freeze itself lives in tests/test_migrations.py.
        """
        first = [s.strip().upper() for s in storage.MIGRATIONS[0][1]]
        self.assertTrue(
            all(s.startswith("CREATE") for s in first),
            "migration 1 should create the schema outright")

        created = set()
        for statement in storage.MIGRATIONS[0][1]:
            match = re.search(
                r"CREATE\s+TABLE\s+(\w+)", statement, re.IGNORECASE)
            if match:
                created.add(match.group(1).lower())
        self.assertIn("runs", created)

        for version, statements in storage.MIGRATIONS[1:]:
            for statement in statements:
                match = re.search(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                    statement, re.IGNORECASE)
                if match:
                    self.assertNotIn(
                        match.group(1).lower(), created,
                        "migration {0} re-creates table '{1}', which "
                        "migration 1 already creates".format(
                            version, match.group(1)))


class EstateTestBase(StorageTestBase):
    """A seeded estate covering every triage classification.

    Each test gets two nights so the latest/previous pair is meaningful:
    ``NIGHT_1`` then ``NIGHT_2``, except ``test_first_fail`` which only
    ever ran once (so its previous result is NULL).
    """

    NIGHT_1 = BASE
    NIGHT_2 = BASE + datetime.timedelta(days=1)

    #: name -> (previous result, latest result)
    ESTATE = {
        "test_steady": (Result.PASS, Result.PASS),
        "test_new_fail": (Result.PASS, Result.FAIL),
        "test_still_fail": (Result.FAIL, Result.FAIL),
        "test_fixed": (Result.FAIL, Result.PASS),
        "test_stale_note": (Result.FAILED_AS_EXPECTED,
                            Result.UNEXPECTED_PASS),
    }

    def setUp(self) -> None:
        super().setUp()
        records = []  # type: List[RunRecord]
        for name, (previous, latest) in sorted(self.ESTATE.items()):
            records.append(make_record(
                test_name=name, result=previous, start=self.NIGHT_1
            ))
            records.append(make_record(
                test_name=name, result=latest, start=self.NIGHT_2
            ))
        # Ran for the first time last night: no previous result at all.
        records.append(make_record(
            test_name="test_first_fail", result=Result.FAIL,
            start=self.NIGHT_2,
        ))
        # Stopped being reported, and a human has approved that. It is
        # failing AND stale, so it would show up in several places if
        # retirement were not applied everywhere.
        records.append(make_record(
            test_name="test_deleted", result=Result.FAIL,
            start=self.NIGHT_1,
        ))
        self.store.upsert_runs(records)
        self.store.set_retired(
            "linux-sim", "suite.py", "test_deleted", True, "alice",
            "Removed from the suite in release 4.2.", CREATED,
        )


class TestStatusQueues(EstateTestBase):
    """The SQL triage queues, and that they agree with the pure classifier."""

    def queue_names(self, kind: str, **kwargs: object) -> List[str]:
        return [
            row.test_name
            for row in self.store.status_queue(kind, **kwargs)
        ]

    def test_new_failures_includes_first_ever_run(self) -> None:
        self.assertEqual(
            self.queue_names("new_failures"),
            ["test_first_fail", "test_new_fail"],
        )

    def test_still_failing(self) -> None:
        self.assertEqual(
            self.queue_names("still_failing"), ["test_still_fail"]
        )

    def test_fixed(self) -> None:
        self.assertEqual(self.queue_names("fixed"), ["test_fixed"])

    def test_unexpected_passes(self) -> None:
        self.assertEqual(
            self.queue_names("unexpected_passes"), ["test_stale_note"]
        )

    def test_assigned_spans_fail_and_unexpected_pass(self) -> None:
        for name in ("test_steady", "test_still_fail", "test_stale_note"):
            self.store.set_assignee(
                "linux-sim", "suite.py", name, "alice", "bob", CREATED
            )
        # test_steady is assigned but passing, so it is not an open action.
        self.assertEqual(
            self.queue_names("assigned"),
            ["test_stale_note", "test_still_fail"],
        )

    def test_assignee_filter_is_applied_in_sql(self) -> None:
        """"My actions" must not be a client-side filter of a capped list."""
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_still_fail", "alice", "bob",
            CREATED,
        )
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_new_fail", "carol", "bob",
            CREATED,
        )
        self.assertEqual(
            self.queue_names("assigned", assignee="alice"),
            ["test_still_fail"],
        )
        self.assertEqual(
            self.store.status_queue_count("assigned", assignee="carol"), 1
        )

    def test_not_run_queue_is_where_retirement_happens(self) -> None:
        """Stale tests get their own queue — the one with the approve action."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        names = self.queue_names("not_run", stale_before=cutoff)
        # Every test except the ones that ran on NIGHT_2, and never the
        # retired one (which is the point: approving it clears it here).
        self.assertNotIn("test_deleted", names)
        self.assertNotIn("test_first_fail", names)
        self.assertEqual(
            self.store.status_queue_count(
                "not_run", stale_before=cutoff),
            len(names),
        )

    def test_counts_match_the_pure_classifier(self) -> None:
        """The SQL predicates and summarize_rollup must define the same thing.

        These are two independent implementations of "new failure",
        "still failing" and "fixed" — one in SQL over latest_runs, one in
        Python over the rollup counts. This test is what stops them
        drifting apart, on an estate small enough that no queue cap
        applies.
        """
        status = analytics.summarize_rollup(
            self.store.summary_rollup(self.NIGHT_1),
            self.store.assigned_open_count(),
        ).status
        for kind, expected in (
            ("new_failures", status.new_failures),
            ("still_failing", status.still_failing),
            ("fixed", status.fixed),
            ("unexpected_passes", status.results[Result.UNEXPECTED_PASS]),
            ("assigned", status.assigned_open),
        ):
            self.assertEqual(
                self.store.status_queue_count(kind), expected,
                "queue {!r} disagrees with the rollup".format(kind),
            )
            self.assertEqual(
                len(self.store.status_queue(kind)), expected,
                "queue {!r} rows disagree with its count".format(kind),
            )

    def test_environment_scoping(self) -> None:
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_win_fail",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        self.assertEqual(
            self.store.status_queue_count("new_failures"), 3
        )
        self.assertEqual(
            self.queue_names("new_failures", environment="win-sim"),
            ["test_win_fail"],
        )

    def test_environments_list_scoping(self) -> None:
        """The WP-20 product filter: an allow-list, not an exact match."""
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_win_fail",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        self.assertEqual(
            self.queue_names(
                "new_failures", environments=["linux-sim", "win-sim"]),
            sorted(["test_first_fail", "test_new_fail", "test_win_fail"]),
        )
        self.assertEqual(
            self.store.status_queue_count(
                "new_failures", environments=["win-sim"]),
            1,
        )
        self.assertEqual(
            self.store.status_queue_count(
                "new_failures", environments=[]),
            0,
            "an empty allow-list (an unknown product) must match nothing",
        )

    def test_limit_caps_rows_but_not_the_count(self) -> None:
        self.assertEqual(
            len(self.store.status_queue("new_failures", limit=1)), 1
        )
        self.assertEqual(
            self.store.status_queue_count("new_failures"), 2
        )

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.status_queue("everything")

    def test_retired_tests_are_absent_from_every_queue(self) -> None:
        """A retired test is failing and stale, and appears nowhere."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        for kind in storage.QUEUE_KINDS:
            self.assertNotIn(
                "test_deleted",
                self.queue_names(kind, stale_before=cutoff),
                "retired test leaked into the {!r} queue".format(kind),
            )
            self.assertNotIn("test_deleted", [
                row.test_name
                for row in self.store.dashboard(limit=100)
            ])

    def test_retired_test_is_still_reachable_when_asked_for(self) -> None:
        """Retirement hides a test from the estate; it does not erase it."""
        names = [
            row.test_name
            for row in self.store.dashboard(include_retired=True, limit=100)
        ]
        self.assertIn("test_deleted", names)
        self.assertTrue(
            self.store.is_retired("linux-sim", "suite.py", "test_deleted")
        )
        # Its history and its comment thread are untouched.
        self.assertEqual(
            len(self.store.run_history(
                "linux-sim", "suite.py", "test_deleted", limit=10)),
            1,
        )
        comments = self.store.comments(
            "linux-sim", "suite.py", "test_deleted")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].author, "alice")
        self.assertIn("release 4.2", comments[0].text)

    def test_retired_test_returns_when_it_runs_again(self) -> None:
        """A test that reports a run is back in the suite, automatically."""
        self.store.upsert_runs([make_record(
            test_name="test_deleted", result=Result.PASS,
            start=self.NIGHT_2 + datetime.timedelta(days=1),
        )])
        self.assertFalse(
            self.store.is_retired("linux-sim", "suite.py", "test_deleted")
        )
        self.assertIn("test_deleted", [
            row.test_name for row in self.store.dashboard(limit=100)
        ])
        # ...and the thread says why the approval lapsed.
        texts = [
            c.text for c in
            self.store.comments("linux-sim", "suite.py", "test_deleted")
        ]
        self.assertTrue(
            any("un-retired" in text for text in texts), texts
        )

    def test_retiring_is_reversible_by_hand(self) -> None:
        self.store.set_retired(
            "linux-sim", "suite.py", "test_deleted", False, "bob",
            "Actually it was only skipped; putting it back.", CREATED,
        )
        self.assertFalse(
            self.store.is_retired("linux-sim", "suite.py", "test_deleted")
        )
        # It has a single FAIL run, so it comes back as a first failure.
        self.assertIn("test_deleted", self.queue_names("new_failures"))

    def test_summary_rollup_cells(self) -> None:
        cells = {
            (c.result, c.prev_result): c.count
            for c in self.store.summary_rollup(self.NIGHT_1)
        }
        self.assertEqual(cells[(Result.FAIL, Result.PASS)], 1)
        self.assertEqual(cells[(Result.FAIL, Result.FAIL)], 1)
        self.assertEqual(cells[(Result.FAIL, None)], 1)
        self.assertEqual(cells[(Result.PASS, Result.FAIL)], 1)

    def test_top_failing_scripts(self) -> None:
        self.store.upsert_runs([make_record(
            script="other.py", test_name="test_o", result=Result.FAIL,
            start=self.NIGHT_2,
        )])
        top = self.store.top_failing_scripts()
        self.assertEqual(
            [(s.script, s.failing) for s in top],
            [("suite.py", 3), ("other.py", 1)],
        )
        self.assertEqual(len(self.store.top_failing_scripts(limit=1)), 1)

    def test_summary_rollup_environments_filter(self) -> None:
        """The WP-20 product filter, on the query the estate headline
        and the products[] breakdown are both built from."""
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_win_fail",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        scoped = {
            c.environment
            for c in self.store.summary_rollup(
                self.NIGHT_1, environments=["linux-sim"])
        }
        self.assertEqual(scoped, {"linux-sim"})
        self.assertEqual(
            self.store.summary_rollup(self.NIGHT_1, environments=[]), [])

    def test_assigned_open_count_environments_filter(self) -> None:
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_win_fail",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        self.store.set_assignee(
            "win-sim", "suite.py", "test_win_fail", "alice", "bob", CREATED,
        )
        self.assertEqual(
            self.store.assigned_open_count(environments=["win-sim"]), 1)
        self.assertEqual(
            self.store.assigned_open_count(environments=["linux-sim"]), 0)

    def test_top_failing_scripts_environments_filter(self) -> None:
        self.store.upsert_runs([make_record(
            environment="win-sim", script="other.py", test_name="test_o",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        top = self.store.top_failing_scripts(environments=["win-sim"])
        self.assertEqual([(s.script, s.failing) for s in top],
                          [("other.py", 1)])


class TestQueueCounts(EstateTestBase):
    """Storage.queue_counts: the batched, one-query form of
    status_queue_count (WP-23 perf pass — see its own docstring for the
    measured before/after). Every assertion here mirrors an existing
    TestStatusQueues one, proving the two AGREE — the same discipline
    test_counts_match_the_pure_classifier already applies between the
    SQL predicates and the pure classifier.
    """

    def test_every_kind_agrees_with_status_queue_count(self) -> None:
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(stale_before=cutoff)
        for kind in storage.QUEUE_KINDS:
            self.assertEqual(
                counts[kind],
                self.store.status_queue_count(kind, stale_before=cutoff),
                "queue_counts disagrees with status_queue_count for "
                "{!r}".format(kind),
            )

    def test_mine_agrees_with_the_assignee_filtered_query(self) -> None:
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_still_fail", "alice", "bob",
            CREATED,
        )
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_new_fail", "carol", "bob",
            CREATED,
        )
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(
            assignee="alice", stale_before=cutoff)
        self.assertEqual(counts["mine"], 1)
        self.assertEqual(
            counts["mine"],
            self.store.status_queue_count(
                "assigned", assignee="alice", stale_before=cutoff),
        )

    def test_mine_is_zero_without_an_assignee(self) -> None:
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(stale_before=cutoff)
        self.assertEqual(counts["mine"], 0)

    def test_environment_scoping_agrees(self) -> None:
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_win_fail",
            result=Result.FAIL, start=self.NIGHT_2,
        )])
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(
            environment="win-sim", stale_before=cutoff)
        self.assertEqual(counts["new_failures"], 1)
        self.assertEqual(
            counts["new_failures"],
            self.store.status_queue_count(
                "new_failures", environment="win-sim",
                stale_before=cutoff),
        )

    def test_retired_tests_are_excluded_from_every_kind(self) -> None:
        """test_deleted is failing AND stale; must not inflate any count,
        the same NOT_RETIRED guarantee status_queue_count already has."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(stale_before=cutoff)
        self.assertEqual(
            counts["not_run"],
            self.store.status_queue_count("not_run", stale_before=cutoff),
        )
        self.assertNotIn(
            "test_deleted",
            [r.test_name for r in
             self.store.status_queue("not_run", stale_before=cutoff)],
        )

    def test_an_unmatched_scope_returns_zeros_not_none(self) -> None:
        """SUM(CASE...) over zero WHERE-matched rows is SQL NULL in both
        backends, not 0 -- the ``or 0`` guard is what keeps this from
        crashing int(None)."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        counts = self.store.queue_counts(
            environment="does-not-exist", stale_before=cutoff)
        for kind in storage.QUEUE_KINDS:
            self.assertEqual(counts[kind], 0, kind)

    def test_stale_before_is_required(self) -> None:
        """not_run is always one of QUEUE_KINDS, so unlike
        status_queue_count (only needed when THAT kind is requested)
        this needs it unconditionally."""
        with self.assertRaises(ValueError):
            self.store.queue_counts()

    def _selects(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return [s for s in seen if s.strip().upper().startswith("SELECT")]

    def test_one_query_regardless_of_assignee(self) -> None:
        """WIDENED for WP-36, not weakened. What this pins is that the
        queue totals cost a fixed number of statements -- it replaced
        one per queue kind, twice over -- and never one that grows with
        the kinds or with whether an assignee was named.

        The number it pins has changed meaning. It was "one statement":
        a pass over the whole partition with every kind as a SUM(CASE).
        It is now one statement the summary does not already run -- the
        ownership count, driven from current_assignments -- plus the
        rollup pass the same summary has always run and memoizes. So:
        one SELECT once that rollup is in hand, which is how the
        handler calls it; two at most from nothing; the same number
        with or without an assignee; and the one that is this method's
        own must not be a pass over latest_runs.

        WIDENED for WP-40, not weakened: the FIRST read after a write
        also asks the mute epoch (one indexed
        ``SELECT MIN(expires_at)`` on the stream's mutes,
        memoized with everything else), so a cold call is one statement
        longer. The repeat is still ZERO statements, asserted last.
        """
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        cold = self._selects(lambda: self.store.queue_counts(
            assignee="alice", stale_before=cutoff))
        # The ownership count, plus the rollup pass: the environment
        # names and one query per environment this fixture holds on
        # mainline (WP-38 split the pass per environment).
        names = self.store._stream_environment_names(
            storage.MAINLINE_STREAM_ID)
        self.assertEqual(len(cold), 1 + (1 + len(names)) + 1, cold)
        self.assertIn("MIN(EXPIRES_AT)", " ".join(cold[-1].split()).upper())
        self.store._invalidate_summary_cache()
        anonymous = self._selects(lambda: self.store.queue_counts(
            stale_before=cutoff))
        self.assertEqual(len(anonymous), len(cold))

        self.store._invalidate_summary_cache()
        self.store.summary_rollup(cutoff)
        within = self._selects(lambda: self.store.queue_counts(
            assignee="alice", stale_before=cutoff))
        self.assertEqual(len(within), 2, within)
        own = " ".join(within[0].split()).upper()
        self.assertIn("FROM CURRENT_ASSIGNMENTS AS CA JOIN LATEST_RUNS", own)
        self.assertIn(
            "MIN(EXPIRES_AT)", " ".join(within[1].split()).upper())
        self.assertEqual(
            self._selects(lambda: self.store.queue_counts(
                assignee="alice", stale_before=cutoff)), [])

    def test_the_ownership_count_reaches_latest_runs_by_its_key(
            self) -> None:
        """The reason that statement is not a pass: it starts from the
        handful of assigned tests. SQLite's planner, SQLite's words --
        excluded from the MariaDB variants by name."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        statement = self._selects(lambda: self.store.queue_counts(
            assignee="alice", stale_before=cutoff))[0]
        plan = [
            str(row[-1]).upper() for row in self.store._conn().execute(
                "EXPLAIN QUERY PLAN " + statement).fetchall()]
        latest = [line for line in plan if "LATEST_RUNS" in line
                  or " LR " in line + " " or " AS LR" in line]
        self.assertTrue(latest, plan)
        for line in latest:
            self.assertTrue(line.startswith("SEARCH"), plan)

    def test_every_kind_still_agrees_with_its_own_query(self) -> None:
        """The five result-shaped kinds are now sums over rollup cells,
        each predicate restated in Python. status_queue_count still
        runs _QUEUE_PREDICATES as SQL, so it is the oracle: every kind,
        every scope this class seeds, with and without an assignee."""
        cutoff = self.NIGHT_2 - datetime.timedelta(hours=1)
        scopes = [
            {}, {"environment": "win-sim"}, {"environment": "linux-sim"},
            {"environments": ["win-sim"]},
            {"environments": ["linux-sim", "win-sim"]},
            {"environments": []}, {"environment": "does-not-exist"},
        ]  # type: List[Dict[str, Any]]
        for scope in scopes:
            for assignee in (None, "alice", "nobody"):
                self.store._invalidate_summary_cache()
                counts = self.store.queue_counts(
                    assignee=assignee, stale_before=cutoff, **scope)
                for kind in storage.QUEUE_KINDS:
                    self.assertEqual(
                        counts[kind],
                        self.store.status_queue_count(
                            kind, stale_before=cutoff, **scope),
                        (kind, scope, assignee))
                mine = 0
                if assignee:
                    mine = self.store.status_queue_count(
                        "assigned", stale_before=cutoff,
                        assignee=assignee, **scope)
                self.assertEqual(
                    counts["mine"], mine, (scope, assignee))


class PartitionRollupTest(StorageTestBase):
    """WP-36: one grouped read of a stream's partition of latest_runs,
    and every scope of a summary a filter of it.

    The oracle throughout is SQL that does not go through the shared
    pass: written out here, scoped in its own WHERE clause, so a filter
    that dropped or leaked an environment disagrees with it.
    """

    CUTOFF = BASE + datetime.timedelta(hours=12)

    def setUp(self) -> None:
        super().setUp()
        day = datetime.timedelta(days=1)
        records = []
        for environment, count in (("linux-sim", 5), ("win-sim", 3),
                                   ("mac-sim", 2)):
            for index in range(count):
                name = "test_{}".format(index)
                records.append(make_record(
                    environment=environment, test_name=name,
                    result=Result.FAIL if index % 2 else Result.PASS,
                    start=BASE))
                if index % 3 == 0:
                    records.append(make_record(
                        environment=environment, test_name=name,
                        result=Result.FAIL if index % 2 == 0
                        else Result.PASS,
                        start=BASE + day + datetime.timedelta(
                            seconds=index)))
        records.append(make_record(
            environment="linux-sim", test_name="test_0",
            result=Result.FAIL, build="feat/x",
            start=BASE + 2 * day))
        self.store.upsert_runs(records)
        self.store.set_retired(
            "win-sim", "suite.py", "test_1", True, "amy", "gone", CREATED)
        self.stream_id = self.store.list_streams("")[0].stream_id

    def _oracle(self, stream_id: int,
                environments: Optional[List[str]]) -> List[Tuple[Any, ...]]:
        sql = (
            "SELECT lr.environment, lr.result, lr.prev_result, "
            "CASE WHEN lr.start_time >= ? THEN 1 ELSE 0 END, "
            "CASE WHEN tr.retired_at IS NULL THEN 0 ELSE 1 END, COUNT(*) "
            "FROM latest_runs lr LEFT JOIN test_retirements tr "
            "ON tr.environment = lr.environment AND tr.script = lr.script "
            "AND tr.test_name = lr.test_name WHERE lr.stream_id = ?")
        params = [model.format_iso(self.CUTOFF), stream_id]  # type: List[Any]
        if environments is not None:
            if not environments:
                return []
            sql += " AND lr.environment IN ({})".format(
                ", ".join("?" for _ in environments))
            params.extend(environments)
        sql += " GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2"
        return [
            (row[0], row[1], row[2], bool(row[3]), bool(row[4]),
             int(row[5]))
            for row in self.store._conn().execute(sql, params).fetchall()]

    @staticmethod
    def _ordered(rows: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        return sorted(rows, key=repr)

    @staticmethod
    def _cells(rows: List[storage.RollupCount]) -> List[Tuple[Any, ...]]:
        return sorted((
            (row.environment, row.result.value,
             None if row.prev_result is None else row.prev_result.value,
             row.recent, row.retired, row.count)
            for row in rows), key=repr)

    def _selects(self, call: Callable[[], Any]) -> int:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return len([
            s for s in seen if s.strip().upper().startswith("SELECT")])

    def test_every_scope_is_what_its_own_query_would_return(self) -> None:
        for stream_id in (storage.MAINLINE_STREAM_ID, self.stream_id):
            for environments in (None, ["win-sim"], ["linux-sim"],
                                 ["linux-sim", "mac-sim"], [],
                                 ["no-such-environment"]):
                self.assertEqual(
                    self._cells(self.store.summary_rollup(
                        self.CUTOFF, environments=environments,
                        stream_id=stream_id)),
                    self._ordered(self._oracle(stream_id, environments)),
                    (stream_id, environments))
            for environment in ("win-sim", "no-such-environment"):
                self.assertEqual(
                    self._cells(self.store.summary_rollup(
                        self.CUTOFF, environment=environment,
                        stream_id=stream_id)),
                    self._ordered(self._oracle(stream_id, [environment])),
                    (stream_id, environment))

    def test_the_two_filters_combine_by_and(self) -> None:
        self.assertEqual(
            self.store.summary_rollup(
                self.CUTOFF, environment="win-sim",
                environments=["linux-sim"]), [])
        self.assertEqual(
            self._cells(self.store.summary_rollup(
                self.CUTOFF, environment="win-sim",
                environments=["linux-sim", "win-sim"])),
            self._ordered(self._oracle(
                storage.MAINLINE_STREAM_ID, ["win-sim"])))

    def test_the_order_every_caller_was_given_is_kept(self) -> None:
        rows = self.store.summary_rollup(self.CUTOFF)
        keys = [(row.environment, row.result.value) for row in rows]
        self.assertEqual(keys, sorted(keys))

    #: A cold pass over this fixture's partition: the environment
    #: names, then one query per environment (WP-38 split the pass so
    #: a push into one environment leaves the others served).
    COLD_PASS = 1 + 3

    def test_a_second_scope_costs_no_query(self) -> None:
        self.assertEqual(
            self._selects(lambda: self.store.summary_rollup(self.CUTOFF)),
            self.COLD_PASS)
        for call in (
            lambda: self.store.summary_rollup(
                self.CUTOFF, environment="win-sim"),
            lambda: self.store.summary_rollup(
                self.CUTOFF, environments=["linux-sim", "mac-sim"]),
            lambda: self.store.latest_run_time_by_environment(),
            lambda: self.store.latest_run_time_by_environment(
                environments=["win-sim"]),
        ):
            self.assertEqual(self._selects(call), 0)

    def test_another_stream_or_cutoff_is_its_own_pass(self) -> None:
        """Shared within a partition and a window; never across."""
        self.store.summary_rollup(self.CUTOFF)
        # The build has one environment: its names, then that one.
        self.assertEqual(self._selects(
            lambda: self.store.summary_rollup(
                self.CUTOFF, stream_id=self.stream_id)), 1 + 1)
        # Another cutoff re-reads every environment; the names are
        # the stream's, not the window's, and are served.
        self.assertEqual(self._selects(
            lambda: self.store.summary_rollup(
                self.CUTOFF + datetime.timedelta(seconds=1))), 3)

    def test_a_push_into_one_environment_re_reads_that_one(self) -> None:
        """WP-38: the point of the split."""
        self.store.summary_rollup(self.CUTOFF)
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_0",
            start=BASE + datetime.timedelta(days=5))])
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            after = self.store.summary_rollup(self.CUTOFF)
        finally:
            conn.set_trace_callback(None)
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")]
        # win-sim's cells, and nothing else: the environment-name list
        # survives a push into an environment it already names.
        self.assertEqual(len(selects), 1, selects)
        self.assertTrue(any("'win-sim'" in s for s in selects), selects)
        self.assertFalse(any("DISTINCT environment" in s for s in selects))
        self.assertFalse(any("'linux-sim'" in s for s in selects), selects)
        self.assertEqual(
            self._cells(after),
            self._ordered(self._oracle(storage.MAINLINE_STREAM_ID, None)))

    def test_a_write_discards_it(self) -> None:
        before = self._cells(self.store.summary_rollup(self.CUTOFF))
        self.store.upsert_runs([make_record(
            environment="mac-sim", test_name="test_new",
            result=Result.FAIL,
            start=BASE + datetime.timedelta(days=3))])
        after = self._cells(self.store.summary_rollup(self.CUTOFF))
        self.assertNotEqual(after, before)
        self.assertEqual(
            after, self._ordered(
                self._oracle(storage.MAINLINE_STREAM_ID, None)))
        self.assertEqual(
            self.store.latest_run_time_by_environment()["mac-sim"],
            BASE + datetime.timedelta(days=3))

    def test_last_reported_is_the_same_derived_or_queried(self) -> None:
        for stream_id in (storage.MAINLINE_STREAM_ID, self.stream_id):
            for environments in (None, ["win-sim"],
                                 ["linux-sim", "mac-sim"], []):
                self.store._invalidate_summary_cache()
                queried = self.store.latest_run_time_by_environment(
                    stream_id, environments=environments)
                self.store._invalidate_summary_cache()
                self.store.summary_rollup(
                    self.CUTOFF, stream_id=stream_id)
                derived = self.store.latest_run_time_by_environment(
                    stream_id, environments=environments)
                self.assertEqual(
                    derived, queried, (stream_id, environments))

    def test_last_reported_still_counts_a_retired_tests_run(self) -> None:
        """It answers "when did we last hear from this environment" --
        a question about the feeder, not the suite."""
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_1",
            start=BASE + datetime.timedelta(hours=1))])
        self.store.set_retired(
            "win-sim", "suite.py", "test_1", True, "amy", "gone", CREATED)
        newest = self.store._conn().execute(
            "SELECT MAX(start_time) FROM latest_runs WHERE stream_id = 1 "
            "AND environment = 'win-sim'").fetchone()[0]
        self.store.summary_rollup(self.CUTOFF)
        self.assertEqual(
            self.store.latest_run_time_by_environment()["win-sim"],
            model.parse_iso(newest))

    def test_the_environment_list_is_the_same_derived_or_queried(
            self) -> None:
        """Mainline only, as it always was: the build's one run on
        linux-sim must not change it, and an environment only a build
        has reported must not appear in it."""
        self.store.upsert_runs([make_record(
            environment="build-only-rig", build="feat/x",
            start=BASE + datetime.timedelta(days=4))])
        for environments in (None, ["win-sim"], ["mac-sim", "linux-sim"],
                             [], ["build-only-rig"]):
            self.store._invalidate_summary_cache()
            queried = self.store.environments(environments=environments)
            self.store._invalidate_summary_cache()
            self.store.summary_rollup(self.CUTOFF)
            self.store.summary_rollup(
                self.CUTOFF, stream_id=self.stream_id)
            derived = self.store.environments(environments=environments)
            self.assertEqual(derived, queried, environments)
            self.assertNotIn("build-only-rig", derived)

    def test_the_streams_clock_is_its_newest_run(self) -> None:
        for stream_id in (storage.MAINLINE_STREAM_ID, self.stream_id):
            newest = self.store._conn().execute(
                "SELECT MAX(start_time) FROM runs WHERE stream_id = ?",
                (stream_id,)).fetchone()[0]
            self.assertEqual(
                self.store.latest_run_time(stream_id),
                model.parse_iso(newest))
        self.assertIsNone(self.store.latest_run_time(999999))


class StreamClockQueryPlanTest(StorageTestBase):
    """WP-36: a stream's newest run is one index seek.

    `runs` has no index that leads with stream_id, so asked there the
    planner walks idx_runs_start_time_result back from the newest run
    on record until it meets one of the stream's -- for a build that
    last ran months ago, through months of everyone else's runs.
    """

    def _plan(self, sql: str) -> str:
        return " | ".join(
            str(row[-1]) for row in self.store._conn().execute(
                "EXPLAIN QUERY PLAN " + sql, (2,)).fetchall()).upper()

    def test_it_is_read_from_the_streams_own_index(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            self.store.latest_run_time(2)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(seen), 1, seen)
        self.assertNotIn(" RUNS ", " " + seen[0].upper().replace(
            "LATEST_RUNS", "") + " ")
        plan = self._plan(
            "SELECT MAX(start_time) FROM latest_runs WHERE stream_id = ?")
        self.assertIn("SEARCH", plan)
        self.assertIn("IDX_LATEST_RUNS_START", plan)

    def test_the_old_statement_would_be_caught(self) -> None:
        plan = self._plan(
            "SELECT MAX(start_time) FROM runs WHERE stream_id = ?")
        self.assertNotIn("STREAM_ID=?", plan)


class TestFailureStreakBounds(StorageTestBase):
    """failing_since / last_pass_before, via index seeks over the history."""

    def seed(self, results: List[Result]) -> datetime.datetime:
        """Import one run per day, oldest first; return the latest start."""
        records = []  # type: List[RunRecord]
        for offset, result in enumerate(results):
            records.append(make_record(
                result=result, start=BASE + datetime.timedelta(days=offset)
            ))
        self.store.upsert_runs(records)
        return BASE + datetime.timedelta(days=len(results) - 1)

    def bounds(self, results: List[Result]) -> storage.FailureStreak:
        latest = self.seed(results)
        return self.store.failure_streak_bounds(
            "linux-sim", "suite.py", "test_a", latest
        )

    def test_streak_starts_after_the_last_non_fail(self) -> None:
        streak = self.bounds([
            Result.PASS, Result.PASS, Result.FAIL, Result.FAIL
        ])
        self.assertEqual(
            streak.failing_since, BASE + datetime.timedelta(days=2)
        )
        self.assertEqual(
            streak.last_pass_before, BASE + datetime.timedelta(days=1)
        )

    def test_single_failing_run(self) -> None:
        streak = self.bounds([Result.PASS, Result.FAIL])
        self.assertEqual(
            streak.failing_since, BASE + datetime.timedelta(days=1)
        )
        self.assertEqual(streak.last_pass_before, BASE)

    def test_never_passed(self) -> None:
        streak = self.bounds([Result.FAIL, Result.FAIL])
        self.assertEqual(streak.failing_since, BASE)
        self.assertIsNone(streak.last_pass_before)

    def test_non_fail_results_bound_the_streak_without_being_a_pass(
        self
    ) -> None:
        """FAILED_AS_EXPECTED ends a streak but is not the "last pass"."""
        streak = self.bounds([
            Result.PASS, Result.FAILED_AS_EXPECTED, Result.FAIL
        ])
        self.assertEqual(
            streak.failing_since, BASE + datetime.timedelta(days=2)
        )
        self.assertEqual(streak.last_pass_before, BASE)

    def test_streak_is_not_truncated_by_history_length(self) -> None:
        """A long-broken test reports its true start, however far back."""
        streak = self.bounds([Result.PASS] + [Result.FAIL] * 300)
        self.assertEqual(
            streak.failing_since, BASE + datetime.timedelta(days=1)
        )
        self.assertEqual(streak.last_pass_before, BASE)


class TestFailureStreakBoundsMany(StorageTestBase):
    """The batched form of failure_streak_bounds (WP-23 perf pass — see
    its own docstring). Every scenario mirrors one of
    TestFailureStreakBounds's above, proving the batch AGREES with the
    single-row method row for row rather than merely "looking
    plausible" — the same discipline TestQueueCounts applies.
    """

    def seed_triple(
        self, test_name: str, results: List[Result]
    ) -> datetime.datetime:
        """One run per day, oldest first, for ONE named test; returns the
        latest start."""
        records = []  # type: List[RunRecord]
        for offset, result in enumerate(results):
            records.append(make_record(
                test_name=test_name, result=result,
                start=BASE + datetime.timedelta(days=offset),
            ))
        self.store.upsert_runs(records)
        return BASE + datetime.timedelta(days=len(results) - 1)

    def test_agrees_with_the_single_row_method_for_a_batch(self) -> None:
        latest_a = self.seed_triple(
            "test_a", [Result.PASS, Result.PASS, Result.FAIL, Result.FAIL])
        latest_b = self.seed_triple("test_b", [Result.PASS, Result.FAIL])
        latest_c = self.seed_triple("test_c", [Result.FAIL, Result.FAIL])
        latest_d = self.seed_triple(
            "test_d",
            [Result.PASS, Result.FAILED_AS_EXPECTED, Result.FAIL])
        entries = [
            ("linux-sim", "suite.py", "test_a", latest_a),
            ("linux-sim", "suite.py", "test_b", latest_b),
            ("linux-sim", "suite.py", "test_c", latest_c),
            ("linux-sim", "suite.py", "test_d", latest_d),
        ]
        batched = self.store.failure_streak_bounds_many(entries)
        for environment, script, test_name, latest in entries:
            expected = self.store.failure_streak_bounds(
                environment, script, test_name, latest)
            self.assertEqual(
                batched[(environment, script, test_name)], expected,
                "batched result disagrees for {!r}".format(test_name),
            )
        # And the actual VALUES are right, not just self-consistent with
        # the single-row method (which could share the same bug):
        self.assertEqual(
            batched[("linux-sim", "suite.py", "test_a")].failing_since,
            BASE + datetime.timedelta(days=2),
        )
        self.assertIsNone(
            batched[("linux-sim", "suite.py", "test_c")].last_pass_before
        )

    def test_streak_is_not_truncated_by_history_length(self) -> None:
        """Mirrors TestFailureStreakBounds's test of the same name — a
        long-broken test's true start must survive batching too."""
        latest = self.seed_triple(
            "test_a", [Result.PASS] + [Result.FAIL] * 300)
        batched = self.store.failure_streak_bounds_many(
            [("linux-sim", "suite.py", "test_a", latest)])
        streak = batched[("linux-sim", "suite.py", "test_a")]
        self.assertEqual(
            streak.failing_since, BASE + datetime.timedelta(days=1))
        self.assertEqual(streak.last_pass_before, BASE)

    def test_an_unknown_triple_resolves_to_no_streak_and_skips_step_three(
        self
    ) -> None:
        """A triple with no runs on this stream at all -- defensive:
        production always calls this with a row's OWN latest_runs
        start_time, which by construction matches a real run, so
        failing_since is never actually None in practice (the row's own
        run always satisfies step 2's own start_time <= latest). Still
        resolves cleanly rather than crashing, and step 3 (the pass
        lookup) is skipped for it since there is no failing_since to
        look a pass up against -- 2 queries, not 3."""
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            batched = self.store.failure_streak_bounds_many([
                ("linux-sim", "suite.py", "test_ghost", BASE),
            ])
        finally:
            conn.set_trace_callback(None)
        streak = batched[("linux-sim", "suite.py", "test_ghost")]
        self.assertIsNone(streak.failing_since)
        self.assertIsNone(streak.last_pass_before)
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        self.assertEqual(len(selects), 2, selects)

    def test_duplicate_triples_are_resolved_once(self) -> None:
        latest = self.seed_triple("test_a", [Result.PASS, Result.FAIL])
        entry = ("linux-sim", "suite.py", "test_a", latest)
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            batched = self.store.failure_streak_bounds_many(
                [entry, entry, entry])
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(batched), 1)
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        self.assertEqual(len(selects), 3, selects)

    def test_query_count_is_bounded_by_chunks_not_rows(self) -> None:
        """250 FAIL triples must not mean 250*3 queries. Chunked at
        _RECENT_CHUNK (100, the same batch size recent_results uses):
        ceil(250/100)*3 = 9, and every one of the 250 has a real
        failing_since (each has a PASS then a FAIL), so step 3 runs for
        every chunk too -- this is the worst case, not a lucky one."""
        entries = []  # type: List[Tuple[str, str, str, datetime.datetime]]
        for i in range(250):
            name = "test_{:03d}".format(i)
            latest = self.seed_triple(name, [Result.PASS, Result.FAIL])
            entries.append(("linux-sim", "suite.py", name, latest))
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            batched = self.store.failure_streak_bounds_many(entries)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(batched), 250)
        selects = [
            s for s in seen if s.strip().upper().startswith("SELECT")
        ]
        self.assertEqual(
            len(selects), 9,
            "expected ceil(250/100)*3 queries, got {0}".format(
                len(selects)))

    def test_empty_input_issues_no_query(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            result = self.store.failure_streak_bounds_many([])
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(result, {})
        self.assertEqual(seen, [])


class TestDashboardPaging(StorageTestBase):
    """Server-side paging, sorting and counting for the test list."""

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(test_name="test_{:02d}".format(i),
                        result=Result.FAIL if i % 5 == 0 else Result.PASS)
            for i in range(30)
        ])

    def test_page_and_total(self) -> None:
        rows = self.store.dashboard(limit=10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0].test_name, "test_00")
        self.assertEqual(self.store.dashboard_count(), 30)

    def test_offset_pages_without_gaps_or_repeats(self) -> None:
        seen = []  # type: List[str]
        for offset in range(0, 30, 7):
            seen.extend(
                row.test_name
                for row in self.store.dashboard(limit=7, offset=offset)
            )
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)
        self.assertEqual(seen, sorted(seen))

    def test_descending_sort(self) -> None:
        rows = self.store.dashboard(
            sort="test_name", descending=True, limit=3
        )
        self.assertEqual(
            [r.test_name for r in rows],
            ["test_29", "test_28", "test_27"],
        )

    def test_every_advertised_sort_key_works(self) -> None:
        for key in storage.DASHBOARD_SORTS:
            for descending in (False, True):
                rows = self.store.dashboard(
                    sort=key, descending=descending, limit=5
                )
                self.assertEqual(
                    len(rows), 5, "sort {!r} returned no page".format(key)
                )

    def test_unknown_sort_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.dashboard(sort="output")

    def test_count_matches_filters(self) -> None:
        self.assertEqual(
            self.store.dashboard_count(results=[Result.FAIL]), 6
        )
        self.assertEqual(self.store.dashboard_count(q="test_1"), 10)
        self.assertEqual(self.store.dashboard_count(results=[]), 0)

    def test_stale_filter(self) -> None:
        """"Not run recently" keeps only tests whose latest run is old."""
        self.store.upsert_runs([make_record(
            test_name="test_fresh",
            start=BASE + datetime.timedelta(days=10),
        )])
        cutoff = BASE + datetime.timedelta(days=5)
        rows = self.store.dashboard(stale_before=cutoff, limit=100)
        self.assertEqual(len(rows), 30)
        self.assertNotIn("test_fresh", [r.test_name for r in rows])
        self.assertEqual(self.store.dashboard_count(stale_before=cutoff), 30)


class TestRunOutputs(StorageTestBase):
    """Output lives in its own table so metadata reads never touch it."""

    def test_output_column_removed_from_runs(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(runs)")
            }
            self.assertNotIn("output", columns)
            self.assertIn(
                "output",
                {row[1] for row in
                 conn.execute("PRAGMA table_info(run_outputs)")},
            )
        finally:
            conn.close()

    def test_output_round_trips(self) -> None:
        self.store.upsert_runs([make_record(output="line one\nline two\n")])
        run_id = self.store.dashboard()[0].run_id
        self.assertEqual(
            self.store.get_run(run_id).output, "line one\nline two\n"
        )

    def test_reimport_replaces_output(self) -> None:
        self.store.upsert_runs([make_record(output="first\n")])
        self.store.upsert_runs([make_record(output="corrected\n")])
        run_id = self.store.dashboard()[0].run_id
        self.assertEqual(self.store.get_run(run_id).output, "corrected\n")
        self.assertEqual(
            self.store._conn().execute(
                "SELECT COUNT(*) FROM run_outputs"
            ).fetchone()[0],
            1,
        )

    def test_output_is_stored_compressed(self) -> None:
        """Log text is ~75% of the database at scale, so it is deflated."""
        text = "2026-07-26 02:14:33 INFO harness: step done\n" * 400
        self.store.upsert_runs([make_record(output=text)])
        run_id = self.store.dashboard()[0].run_id
        stored = self.store._conn().execute(
            "SELECT output FROM run_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        self.assertIsInstance(stored, bytes)
        self.assertLess(len(stored), len(text.encode("utf-8")) // 4)
        # ...and it comes back byte-for-byte.
        self.assertEqual(self.store.get_run(run_id).output, text)

    def test_unicode_output_round_trips(self) -> None:
        text = "Ünïcödé ✓ — 日本語\ttab\r\nCRLF\n"
        self.store.upsert_runs([make_record(output=text)])
        run_id = self.store.dashboard()[0].run_id
        self.assertEqual(self.store.get_run(run_id).output, text)

    def test_empty_output_round_trips(self) -> None:
        self.store.upsert_runs([make_record(output="")])
        run_id = self.store.dashboard()[0].run_id
        self.assertEqual(self.store.get_run(run_id).output, "")

    def test_plain_text_output_still_reads(self) -> None:
        """A row written before output was compressed must still read."""
        self.store.upsert_runs([make_record()])
        run_id = self.store.dashboard()[0].run_id
        # The store's connection is in autocommit outside transactions
        # on both backends, so no commit is needed.
        self.store._conn().execute(
            "UPDATE run_outputs SET output = ? WHERE run_id = ?",
            ("legacy uncompressed text\n", run_id),
        )
        self.assertEqual(
            self.store.get_run(run_id).output,
            "legacy uncompressed text\n",
        )


class TestPruneRuns(StorageTestBase):
    """Retention: old runs go, the estate's current state does not."""

    def seed_history(self, days: int = 10) -> None:
        self.store.upsert_runs([
            make_record(
                test_name="test_a", start=BASE + datetime.timedelta(days=i),
                output="output for day {}\n".format(i),
            )
            for i in range(days)
        ] + [
            # A test that stopped running long ago.
            make_record(test_name="test_gone", start=BASE)
        ])

    def test_deletes_old_runs_and_their_outputs(self) -> None:
        self.seed_history()
        cutoff = BASE + datetime.timedelta(days=5)
        deleted = self.store.prune_runs_before(cutoff)
        self.assertEqual(deleted, 5)
        remaining = self.store.run_history(
            "linux-sim", "suite.py", "test_a", limit=100
        )
        self.assertEqual(len(remaining), 5)
        self.assertTrue(all(r.start_time >= cutoff for r in remaining))
        conn = self.store._conn()
        outputs = conn.execute(
            "SELECT COUNT(*) FROM run_outputs"
        ).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(outputs, runs)

    def test_never_deletes_a_tests_latest_run(self) -> None:
        """A test that stopped running must still show as "not run"."""
        self.seed_history()
        self.store.prune_runs_before(BASE + datetime.timedelta(days=365))
        names = [row.test_name for row in self.store.dashboard()]
        self.assertIn("test_gone", names)
        self.assertEqual(len(names), 2)

    def test_prev_result_is_rederived(self) -> None:
        """Pruning the run a status row pointed back at cannot leave a lie."""
        self.store.upsert_runs([
            make_record(result=Result.PASS, start=BASE),
            make_record(
                result=Result.FAIL,
                start=BASE + datetime.timedelta(days=1),
            ),
        ])
        self.store.prune_runs_before(BASE + datetime.timedelta(days=1))
        self.assertIsNone(
            self.store._conn().execute(
                "SELECT prev_result FROM latest_runs"
            ).fetchone()[0]
        )
        # The test is now a first-ever failure rather than a regression.
        self.assertEqual(
            self.store.status_queue_count("still_failing"), 0
        )
        self.assertEqual(self.store.status_queue_count("new_failures"), 1)

    def test_nothing_to_prune(self) -> None:
        self.seed_history(days=3)
        self.assertEqual(self.store.prune_runs_before(BASE), 0)


if __name__ == "__main__":
    unittest.main()


class TestRecentResults(StorageTestBase):
    """The list-view result history must not be a query per row (WP-8).

    A page of a hundred tests asking "how has this one been behaving"
    one at a time is a hundred round trips. That is the same shape of
    bug tests/test_frontend_calls.py exists to catch on the frontend,
    and it is no better on the server.
    """

    def _seed(self, tests: int, runs_each: int) -> List[Tuple[str, str, str]]:
        records = []
        for index in range(tests):
            for run in range(runs_each):
                start = BASE + datetime.timedelta(days=run)
                records.append(make_record(
                    environment="linux", script="s.py",
                    test_name="t%03d" % index,
                    result=Result.FAIL if run % 2 else Result.PASS,
                    start=start,
                    end=start + datetime.timedelta(seconds=1)))
        self.store.upsert_runs(records)
        return [("linux", "s.py", "t%03d" % i) for i in range(tests)]

    def test_it_returns_results_oldest_first(self) -> None:
        triples = self._seed(1, 4)
        history = self.store.recent_results(
            triples, BASE - datetime.timedelta(days=1))
        self.assertEqual(
            history[triples[0]],
            [Result.PASS, Result.FAIL, Result.PASS, Result.FAIL])

    def test_it_keeps_only_the_newest_n(self) -> None:
        triples = self._seed(1, 10)
        history = self.store.recent_results(
            triples, BASE - datetime.timedelta(days=1), per_test_limit=3)
        # Runs 7, 8, 9 -> FAIL, PASS, FAIL
        self.assertEqual(len(history[triples[0]]), 3)
        self.assertEqual(history[triples[0]][-1], Result.FAIL)

    def test_it_honours_the_since_cutoff(self) -> None:
        triples = self._seed(1, 6)
        history = self.store.recent_results(
            triples, BASE + datetime.timedelta(days=4))
        self.assertEqual(len(history[triples[0]]), 2)

    def test_an_unknown_test_is_simply_absent(self) -> None:
        history = self.store.recent_results(
            [("nope", "nope.py", "nope")], BASE)
        self.assertEqual(history, {})

    def test_no_triples_issues_no_query_at_all(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            self.assertEqual(self.store.recent_results([], BASE), {})
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(seen, [])

    def test_the_query_count_is_bounded_by_chunks_not_rows(self) -> None:
        """The assertion this class exists for.

        250 tests must not mean 250 queries. With a chunk size of 100 it
        is 3, and it stays 3 however much history each test has.
        """
        triples = self._seed(250, 4)
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            history = self.store.recent_results(
                triples, BASE - datetime.timedelta(days=1))
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(history), 250)
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertEqual(
            len(selects), 3,
            "expected ceil(250/100) queries, got {0}".format(len(selects)))

    def test_duplicate_triples_are_not_fetched_twice(self) -> None:
        triples = self._seed(2, 2)
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            self.store.recent_results(triples + triples + triples, BASE)
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)


class TestLatestResultsForStreams(StorageTestBase):
    """Storage.latest_results_for_streams -- ADDENDUM to the perf round
    (Open Actions' truthful display, docs/STREAMS_PLAN.md §5.4). Same
    batching discipline as TestRecentResults above: a page of rows
    asking "what did THIS stream see for this test" one at a time is a
    query per row, the exact shape this project's own guard tests exist
    to catch.
    """

    def _seed_mainline_and_branch(
        self,
        test_name: str = "test_a",
        mainline_result: Result = Result.PASS,
        branch_result: Result = Result.FAIL,
    ) -> int:
        """Mainline's own result, then the SAME triple on a branch with
        a DIFFERENT result -- the exact contradiction case this feature
        exists for. Returns the branch's stream_id."""
        self.store.upsert_runs([make_record(
            test_name=test_name, result=mainline_result, start=BASE,
        )])
        self.store.upsert_runs([make_record(
            test_name=test_name, result=branch_result,
            start=BASE + datetime.timedelta(days=1), build="feat/x",
        )])
        streams = self.store.list_streams("")
        return streams[0].stream_id

    def test_the_origin_streams_own_result_is_returned(self) -> None:
        branch_id = self._seed_mainline_and_branch()
        found = self.store.latest_results_for_streams(
            [(branch_id, "linux-sim", "suite.py", "test_a")])
        self.assertEqual(
            found[(branch_id, "linux-sim", "suite.py", "test_a")],
            Result.FAIL,
        )

    def test_a_triple_the_stream_never_ran_is_simply_absent(self) -> None:
        branch_id = self._seed_mainline_and_branch()
        found = self.store.latest_results_for_streams(
            [(branch_id, "linux-sim", "suite.py", "test_never_ran")])
        self.assertEqual(found, {})

    def test_mainline_and_branch_are_kept_separate_by_stream_id(
        self
    ) -> None:
        """The same triple, two different stream_id keys, must not
        collide -- proving the batched query keys on stream_id too, not
        only the triple."""
        branch_id = self._seed_mainline_and_branch()
        found = self.store.latest_results_for_streams([
            (1, "linux-sim", "suite.py", "test_a"),
            (branch_id, "linux-sim", "suite.py", "test_a"),
        ])
        self.assertEqual(found[(1, "linux-sim", "suite.py", "test_a")],
                          Result.PASS)
        self.assertEqual(
            found[(branch_id, "linux-sim", "suite.py", "test_a")],
            Result.FAIL,
        )

    def test_empty_input_issues_no_query(self) -> None:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            found = self.store.latest_results_for_streams([])
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(found, {})
        self.assertEqual(seen, [])

    def test_the_query_count_is_bounded_by_chunks_not_keys(self) -> None:
        """250 keys must not mean 250 queries. Chunked at _RECENT_CHUNK
        (100): ceil(250/100) = 3."""
        records = []
        for index in range(250):
            records.append(make_record(
                test_name="t%03d" % index, result=Result.PASS, start=BASE,
            ))
        self.store.upsert_runs(records)
        keys = [
            (1, "linux-sim", "suite.py", "t%03d" % i) for i in range(250)
        ]
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            found = self.store.latest_results_for_streams(keys)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(found), 250)
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertEqual(
            len(selects), 3,
            "expected ceil(250/100) queries, got {0}".format(len(selects)))

    def test_duplicate_keys_are_not_fetched_twice(self) -> None:
        branch_id = self._seed_mainline_and_branch()
        key = (branch_id, "linux-sim", "suite.py", "test_a")
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            self.store.latest_results_for_streams([key, key, key])
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)


class TestSortIndexesAreUsed(StorageTestBase):
    """Migration 4: the descending sorts must not sort the whole table.

    Without a matching index SQLite orders all 12,008 rows to return
    250 of them, on every page — "USE TEMP B-TREE FOR ORDER BY" in the
    plan. Measured on a copy of production that is 149ms cold and 0.8ms
    indexed, and much worse than 149ms on a network mount where the
    sort's page reads are round trips.

    Asserting on the PLAN rather than on a duration: a timing test on a
    fast dev machine proves nothing about a slow production one, but the
    plan is the same on both.
    """

    def setUp(self) -> None:
        StorageTestBase.setUp(self)
        records = []
        for index in range(40):
            start = BASE + datetime.timedelta(minutes=index)
            records.append(make_record(
                test_name="t%03d" % index, start=start,
                end=start + datetime.timedelta(seconds=index % 7)))
        self.store.upsert_runs(records)

    def _plan(self, sort: str, descending: bool) -> str:
        """Plan the query dashboard() ACTUALLY runs.

        Captured with a trace callback rather than rebuilt by hand: a
        lookalike query would keep passing after the real one changed,
        which is the failure mode this test exists to prevent.
        """
        captured = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, captured)
        try:
            self.store.dashboard(
                sort=sort, descending=descending, limit=250, offset=0)
        finally:
            conn.set_trace_callback(None)
        selects = [
            sql for sql in captured
            if sql.strip().upper().startswith("SELECT")
            and "latest_runs" in sql
        ]
        self.assertTrue(selects, "no dashboard query was captured")
        rows = conn.execute(
            "EXPLAIN QUERY PLAN " + selects[-1]).fetchall()
        return " | ".join(str(row[-1]) for row in rows)

    def test_descending_start_time_uses_an_index(self) -> None:
        plan = self._plan("start_time", True)
        self.assertNotIn(
            "TEMP B-TREE", plan.upper(),
            "start_time DESC is sorting the whole table: " + plan)

    def test_descending_duration_uses_an_index(self) -> None:
        plan = self._plan("duration", True)
        self.assertNotIn(
            "TEMP B-TREE", plan.upper(),
            "duration DESC is sorting the whole table: " + plan)

    def test_the_indexes_exist_and_are_plain_ascending(self) -> None:
        rows = self.store._conn().execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_latest_runs_start_sort', "
            "             'idx_latest_runs_duration_sort')"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for _name, sql in rows:
            # ASCENDING on purpose. Every DASHBOARD_SORTS entry appends
            # the full primary key and the whole ORDER BY takes one
            # direction, so an all-ascending index serves ascending
            # pages forwards and descending pages backwards. An index
            # with a DESC first column and ascending tiebreaks matches
            # neither — which is what the first attempt at this created.
            self.assertNotIn("DESC", sql.upper())

    def test_ascending_uses_the_same_index_read_forwards(self) -> None:
        """One index per sort key, not one per direction.

        A separate descending index was built first and helped nothing:
        it had a DESC first column with ascending tiebreaks, which
        matches neither an ascending page nor a descending one.
        """
        for sort in ("start_time", "duration"):
            plan = self._plan(sort, False)
            self.assertNotIn("TEMP B-TREE", plan.upper(), sort)

    def test_sorted_results_are_still_correct(self) -> None:
        """An index that changes the answer is worse than a slow scan."""
        rows = self.store.dashboard(
            sort="start_time", descending=True, limit=40)
        times = [row.start_time for row in rows]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(times), 40)


class TestEnvironmentExpectations(StorageTestBase):
    """Declared expected test counts (migration 5).

    The declaration exists because the inferred denominator — every test
    ever seen in an environment — is a high-water mark, and too high a
    denominator makes real passes fail the coverage test SILENTLY.
    """

    def _declare(self, environment: str = "linux-sim",
                 expected: int = 400) -> None:
        self.store.set_environment_expectation(
            environment, expected, "alice", CREATED)

    def test_nothing_is_declared_to_begin_with(self) -> None:
        self.assertEqual(self.store.declared_test_counts(), {})
        self.assertEqual(self.store.list_environment_expectations(), [])

    def test_declare_then_read_back(self) -> None:
        self._declare()
        self.assertEqual(
            self.store.declared_test_counts(), {"linux-sim": 400})
        (row,) = self.store.list_environment_expectations()
        self.assertEqual(row.environment, "linux-sim")
        self.assertEqual(row.expected_tests, 400)
        self.assertEqual(row.updated_by, "alice")
        self.assertEqual(row.updated_at, CREATED)

    def test_redeclaring_replaces_rather_than_duplicates(self) -> None:
        self._declare(expected=400)
        later = CREATED + datetime.timedelta(days=1)
        self.store.set_environment_expectation(
            "linux-sim", 900, "bob", later)
        rows = self.store.list_environment_expectations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].expected_tests, 900)
        self.assertEqual(rows[0].updated_by, "bob")
        self.assertEqual(rows[0].updated_at, later)

    def test_declaring_creates_the_user_that_did_it(self) -> None:
        """Same rule as comments and assignments: a name that acted is a
        user, so the audit trail never points at nobody."""
        self._declare()
        self.assertIsNotNone(self.store.get_user("alice"))

    def test_clearing_returns_to_inference(self) -> None:
        self._declare()
        self.assertTrue(
            self.store.clear_environment_expectation("linux-sim"))
        self.assertEqual(self.store.declared_test_counts(), {})

    def test_clearing_what_was_never_declared_is_false_not_an_error(
        self
    ) -> None:
        self.assertFalse(
            self.store.clear_environment_expectation("never-existed"))

    def test_environments_are_case_sensitive(self) -> None:
        """SQLite compares TEXT keys byte for byte; a default MariaDB
        collation would not (runbook B.3). Pinned rather than
        normalised, because latest_runs.environment is not normalised
        either and folding one of the two would be worse than neither."""
        self._declare("linux", 100)
        self._declare("Linux", 200)
        self.assertEqual(
            self.store.declared_test_counts(),
            {"linux": 100, "Linux": 200})

    def test_known_environments_covers_run_and_declared_alike(
        self
    ) -> None:
        """A declaration whose environment has been renamed away must
        stay listed, or it can never be cleared."""
        self.store.upsert_runs([make_record(environment="linux-sim")])
        self._declare("retired-env", 10)
        self.assertEqual(
            self.store.known_environments(), ["linux-sim", "retired-env"])

    def test_the_upsert_is_not_insert_or_replace(self) -> None:
        """tests/test_sql_portability.py counts every OR REPLACE against
        a committed expectation, because it deletes and re-inserts and
        MariaDB's ON DUPLICATE KEY UPDATE does not."""
        with open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "testboard", "storage.py"),
            "rb",
        ) as handle:
            source = handle.read().decode("utf-8")
        self.assertNotIn(
            "INSERT OR REPLACE INTO environment_expectations", source)


class TestEnvironmentProducts(StorageTestBase):
    """Declared environment -> product mapping (migration 8, WP-20).

    An environment absent from this table belongs to the implicit
    product ``""`` — this is the read-time grouping the products
    feature is built on, and it exists so a second product's
    environments can be kept out of every estate view without touching
    test identity.
    """

    def _declare(self, environment: str = "linux-sim",
                 product: str = "Atlas") -> None:
        self.store.set_environment_product(
            environment, product, "alice", CREATED)

    def test_nothing_is_declared_to_begin_with(self) -> None:
        self.assertEqual(self.store.environment_products_map(), {})
        self.assertEqual(self.store.list_environment_products(), [])
        self.assertEqual(self.store.distinct_products(), [])

    def test_declare_then_read_back(self) -> None:
        self._declare()
        self.assertEqual(
            self.store.environment_products_map(), {"linux-sim": "Atlas"})
        (row,) = self.store.list_environment_products()
        self.assertEqual(row.environment, "linux-sim")
        self.assertEqual(row.product, "Atlas")
        self.assertEqual(row.updated_by, "alice")
        self.assertEqual(row.updated_at, CREATED)

    def test_redeclaring_replaces_rather_than_duplicates(self) -> None:
        self._declare(product="Atlas")
        later = CREATED + datetime.timedelta(days=1)
        self.store.set_environment_product(
            "linux-sim", "Borealis", "bob", later)
        rows = self.store.list_environment_products()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].product, "Borealis")
        self.assertEqual(rows[0].updated_by, "bob")
        self.assertEqual(rows[0].updated_at, later)

    def test_declaring_creates_the_user_that_did_it(self) -> None:
        self._declare()
        self.assertIsNotNone(self.store.get_user("alice"))

    def test_clearing_returns_to_the_implicit_product(self) -> None:
        self._declare()
        self.assertTrue(self.store.clear_environment_product("linux-sim"))
        self.assertEqual(self.store.environment_products_map(), {})

    def test_clearing_what_was_never_declared_is_false_not_an_error(
        self
    ) -> None:
        self.assertFalse(
            self.store.clear_environment_product("never-existed"))

    def test_product_for_environment_single_row_lookup(self) -> None:
        """WP-22's single-environment lookup (used by the per-triple
        streams endpoint) must agree with the whole-map read."""
        self._declare("linux-sim", "Atlas")
        self.assertEqual(
            self.store.product_for_environment("linux-sim"), "Atlas")
        self.assertEqual(
            self.store.product_for_environment("never-declared"), "")

    def test_environments_are_case_sensitive(self) -> None:
        """Same reasoning, and the same test, as
        TestEnvironmentExpectations.test_environments_are_case_sensitive:
        a default MariaDB collation would fold these two together
        (runbook B.3); this project's does not."""
        self._declare("linux", "Atlas")
        self._declare("Linux", "Borealis")
        self.assertEqual(
            self.store.environment_products_map(),
            {"linux": "Atlas", "Linux": "Borealis"})

    def test_known_environments_covers_run_and_declared_alike(
        self
    ) -> None:
        """A product declared before the environment's first import, or
        after it stopped reporting, must still be listed — the same
        rule migration 5 established, so it can always be corrected."""
        self.store.upsert_runs([make_record(environment="linux-sim")])
        self._declare("retired-env", "Atlas")
        self.assertEqual(
            self.store.known_environments(), ["linux-sim", "retired-env"])

    def test_environment_exists_is_true_once_a_product_is_declared(
        self
    ) -> None:
        self.assertFalse(self.store.environment_exists("brand-new"))
        self._declare("brand-new", "Atlas")
        self.assertTrue(self.store.environment_exists("brand-new"))

    def test_the_upsert_is_not_insert_or_replace(self) -> None:
        """tests/test_sql_portability.py counts every OR REPLACE against
        a committed expectation, because it deletes and re-inserts and
        MariaDB's ON DUPLICATE KEY UPDATE does not."""
        with open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "testboard", "storage.py"),
            "rb",
        ) as handle:
            source = handle.read().decode("utf-8")
        self.assertNotIn(
            "INSERT OR REPLACE INTO environment_products", source)


class TestEnvironmentsForProduct(StorageTestBase):
    """Resolving a product name to its member environments.

    The read the WP-20 ``product=`` API filter is built on: it turns a
    declared name into an allow-list of environments, which every
    scoped endpoint then filters ``environment IN (...)`` on.
    """

    def test_the_implicit_product_is_every_unmapped_known_environment(
        self
    ) -> None:
        self.store.upsert_runs([make_record(environment="linux-sim")])
        self.store.upsert_runs([make_record(environment="win")])
        self.store.set_environment_product(
            "win", "Atlas", "alice", CREATED)
        self.assertEqual(
            self.store.environments_for_product(""), ["linux-sim"])

    def test_a_named_product_returns_its_mapped_environments_sorted(
        self
    ) -> None:
        self.store.set_environment_product(
            "win", "Atlas", "alice", CREATED)
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.set_environment_product(
            "mac", "Borealis", "alice", CREATED)
        self.assertEqual(
            self.store.environments_for_product("Atlas"),
            ["linux-sim", "win"])

    def test_an_unknown_product_is_an_empty_list_not_an_error(self) -> None:
        self.assertEqual(self.store.environments_for_product("Nope"), [])

    def test_distinct_products_excludes_the_implicit_one(self) -> None:
        self.store.set_environment_product(
            "win", "Atlas", "alice", CREATED)
        self.store.upsert_runs([make_record(environment="linux-sim")])
        self.assertEqual(self.store.distinct_products(), ["Atlas"])


class TestInferredTestCounts(StorageTestBase):
    """The denominator a declaration overrides."""

    def _seed(self) -> None:
        for index in range(4):
            self.store.upsert_runs([make_record(
                environment="linux-sim", test_name="t%d" % index)])
        self.store.upsert_runs([make_record(environment="win", test_name="w")])

    def test_counts_are_per_environment(self) -> None:
        self._seed()
        self.assertEqual(
            self.store.test_counts_by_environment(),
            {"linux-sim": 4, "win": 1})

    def test_retired_tests_do_not_inflate_the_denominator(self) -> None:
        """A pass that does not run a retired test has missed nothing.

        Counting them makes real passes fail the coverage test, and a
        failed coverage test is invisible: the cutoff quietly drops back
        to the wall clock.
        """
        self._seed()
        self.store.set_retired(
            "linux-sim", "suite.py", "t0", True, "alice", "gone", CREATED)
        self.assertEqual(
            self.store.test_counts_by_environment(),
            {"linux-sim": 3, "win": 1})

    def test_un_retiring_puts_it_back(self) -> None:
        self._seed()
        self.store.set_retired(
            "linux-sim", "suite.py", "t0", True, "alice", "gone", CREATED)
        self.store.set_retired(
            "linux-sim", "suite.py", "t0", False, "alice", "back", CREATED)
        self.assertEqual(
            self.store.test_counts_by_environment()["linux-sim"], 4)


class TestMigrationFiveOnAnExistingDatabase(unittest.TestCase):
    """Migration 5 applied to a database built at version 4."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="testboard_migrate5_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "v4.db")

    def _build_at_version_four(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for version, statements in storage.MIGRATIONS:
                if version > 4:
                    break
                for statement in statements:
                    storage.apply_migration_statement(conn, statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (4)")
            conn.execute(
                "INSERT INTO users (username, created_at) VALUES ('amy', ?)",
                (model.format_iso(CREATED),))
            conn.commit()
        finally:
            conn.close()

    def test_the_table_arrives_and_existing_rows_survive(self) -> None:
        self._build_at_version_four()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        self.assertEqual(store.declared_test_counts(), {})
        self.assertIsNotNone(store.get_user("amy"))
        version = store._conn().execute(
            "SELECT version FROM schema_version").fetchone()[0]
        # Opening migrates to the NEWEST version, so this pin tracks the
        # end of MIGRATIONS rather than naming 5: the test is about
        # migration 5's table arriving on a v4 database, not about 5
        # being the last migration there is.
        self.assertEqual(version, storage.MIGRATIONS[-1][0])

    def test_a_declaration_can_be_made_immediately_after(self) -> None:
        self._build_at_version_four()
        store = Storage(self.db_path)
        self.addCleanup(store.close)
        store.set_environment_expectation("linux", 12, "amy", CREATED)
        self.assertEqual(store.declared_test_counts(), {"linux": 12})


class TestEnvironmentListingCost(StorageTestBase):
    """No new list query may grow with the size of the estate.

    The plan's rule (0.4) arising from this round: assert the cost, not
    just the answer. `runs` holds every run ever recorded and is three
    orders of magnitude larger than `latest_runs`; a listing that
    touches it is a defect even when it is fast on a small database.
    """

    def setUp(self) -> None:
        StorageTestBase.setUp(self)
        for env in ("linux-sim", "win-sim"):
            for index in range(5):
                # Several runs each, so a query that walked history
                # rather than the derived table would show it.
                for day in range(4):
                    self.store.upsert_runs([make_record(
                        environment=env, test_name="t%d" % index,
                        start=BASE + datetime.timedelta(days=day))])

    def _plan(self, sql: str, params: tuple = ()) -> str:
        rows = self.store._conn().execute(
            "EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " ".join(str(row[-1]) for row in rows)

    def test_the_listing_never_touches_runs(self) -> None:
        plan = self._plan(
            "SELECT environment FROM latest_runs "
            "UNION SELECT environment FROM environment_expectations "
            "ORDER BY 1")
        self.assertNotIn(
            "runs", plan.replace("latest_runs", ""),
            "the environment listing must read the derived table, not "
            "history: " + plan)

    def test_the_listing_reads_an_index_not_the_table(self) -> None:
        """It is proportional to the number of TESTS, and only to that.

        The latest_runs primary key begins with `environment`, so this
        is a covering-index scan. There is no cheaper form: SQLite here
        cannot skip-scan to the distinct leading values, and an index on
        `environment` alone would be maintained on every row a nightly
        import touches.
        """
        plan = self._plan(
            "SELECT environment FROM latest_runs "
            "UNION SELECT environment FROM environment_expectations "
            "ORDER BY 1")
        self.assertIn("COVERING INDEX", plan.upper(), plan)

    def test_checking_one_environment_is_a_seek_not_a_scan(self) -> None:
        """Validating a single name must not cost a listing."""
        plan = self._plan(
            "SELECT 1 FROM latest_runs WHERE environment = ? LIMIT 1",
            ("linux-sim",))
        self.assertNotIn("SCAN", plan.upper(), plan)

    def test_environment_exists_answers_correctly(self) -> None:
        self.assertTrue(self.store.environment_exists("linux-sim"))
        self.assertFalse(self.store.environment_exists("typo"))

    def test_a_declaration_alone_makes_an_environment_known(self) -> None:
        """So a renamed environment's stale declaration can be cleared."""
        self.store.set_environment_expectation(
            "gone-away", 5, "alice", CREATED)
        self.store._conn().execute("DELETE FROM latest_runs")
        self.assertTrue(self.store.environment_exists("gone-away"))
        self.assertIn("gone-away", self.store.known_environments())


class TestLatestRunTimeByEnvironment(StorageTestBase):
    """When each environment last reported.

    Per environment, because they run SEQUENTIALLY and hours apart: one
    estate-wide figure is only the newest of them, and looks healthy
    while the environment somebody is waiting on has not started.
    """

    def _seed(self) -> None:
        self.store.upsert_runs([
            make_record(environment="linux-sim", test_name="a",
                        start=BASE),
            make_record(environment="linux-sim", test_name="b",
                        start=BASE + datetime.timedelta(hours=2)),
            make_record(environment="win-sim", test_name="c",
                        start=BASE + datetime.timedelta(hours=5)),
        ])

    def test_each_environment_reports_its_newest_run(self) -> None:
        self._seed()
        self.assertEqual(
            self.store.latest_run_time_by_environment(),
            {"linux-sim": BASE + datetime.timedelta(hours=2),
             "win-sim": BASE + datetime.timedelta(hours=5)})

    def test_an_empty_estate_is_an_empty_map(self) -> None:
        self.assertEqual(self.store.latest_run_time_by_environment(), {})

    def test_narrowed_by_an_allow_list(self) -> None:
        """WP-23 fix: a product-scoped page's environment_updated pills
        must not include another product's environments — found live
        alongside the same gap in environments()/scripts()."""
        self._seed()
        self.assertEqual(
            sorted(self.store.latest_run_time_by_environment(
                environments=["linux-sim"])),
            ["linux-sim"])
        self.assertEqual(
            self.store.latest_run_time_by_environment(environments=[]),
            {})

    def test_a_re_import_of_an_older_run_does_not_move_it_back(
        self
    ) -> None:
        """latest_runs holds each test's NEWEST run, so a back-filled
        older run must not make the environment look staler than it is."""
        self._seed()
        self.store.upsert_runs([make_record(
            environment="linux-sim", test_name="a",
            start=BASE - datetime.timedelta(days=3))])
        self.assertEqual(
            self.store.latest_run_time_by_environment()["linux-sim"],
            BASE + datetime.timedelta(hours=2))

    def test_retired_tests_still_count(self) -> None:
        """This is a question about the FEEDER — when did we last hear
        from this environment — not about what is in the suite. A
        retired test reporting still means the environment ran."""
        self._seed()
        self.store.set_retired(
            "win-sim", "suite.py", "c", True, "alice", "gone", CREATED)
        self.assertIn(
            "win-sim", self.store.latest_run_time_by_environment())

    def test_it_never_touches_the_runs_table(self) -> None:
        """The plan's 0.4 rule. `latest_runs` is one row per TEST
        (~12k); `runs` is every run ever recorded, and grouping that
        would make the home screen proportional to history."""
        self._seed()
        plan = " ".join(str(row[-1]) for row in self.store._conn().execute(
            "EXPLAIN QUERY PLAN SELECT environment, MAX(start_time) "
            "FROM latest_runs GROUP BY environment"))
        self.assertNotIn(
            "runs", plan.replace("latest_runs", ""),
            "must read the derived table, not history: " + plan)
        self.assertIn("INDEX", plan.upper(), plan)


class UnassignedFailingTest(StorageTestBase):
    """The Watchlist's unassigned-failure highlight (docs/STREAMS_PLAN.md
    §2.4): currently-FAILING tests with no current assignee, batched
    per environment and per stream so /api/watch pays no per-card
    query for it."""

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(environment="linux-sim", test_name="a",
                        result=Result.FAIL),
            make_record(environment="linux-sim", test_name="b",
                        result=Result.FAIL),
            make_record(environment="linux-sim", test_name="c",
                        result=Result.PASS),
            make_record(environment="win-sim", test_name="d",
                        result=Result.FAIL),
        ])
        self.store.set_assignee(
            "linux-sim", "suite.py", "a", "alice", "bob", CREATED)

    def test_counts_only_failing_and_unassigned(self) -> None:
        counts = self.store.unassigned_failing_by_environment()
        # test_a is FAIL but assigned; test_b is FAIL and unassigned;
        # test_c passes. linux-sim's count is exactly test_b.
        self.assertEqual(counts, {"linux-sim": 1, "win-sim": 1})

    def test_assignment_is_by_triple_not_by_stream(self) -> None:
        """Assignments are stream-agnostic: assigning "a" from mainline
        must also clear IT off a branch's own unassigned-failing count
        for the same triple, because there is only ever one owner."""
        self.store.upsert_runs([make_record(
            environment="linux-sim", test_name="a", result=Result.FAIL,
            build="feat/x", start=BASE + datetime.timedelta(hours=1))])
        stream_id = self.store.list_streams("")[0].stream_id
        counts = self.store.unassigned_failing_by_stream([stream_id])
        # test_a fails on the branch too, but it is still assigned (the
        # SAME assignment, made from mainline) -- zero, not one.
        self.assertEqual(counts.get(stream_id, 0), 0)

    def test_retired_tests_are_excluded(self) -> None:
        self.store.set_retired(
            "linux-sim", "suite.py", "b", True, "alice", "gone", CREATED)
        counts = self.store.unassigned_failing_by_environment()
        self.assertEqual(counts.get("linux-sim", 0), 0)

    def test_a_clean_estate_has_no_entries(self) -> None:
        empty_dir = tempfile.mkdtemp(prefix="testboard_storage_empty_")
        self.addCleanup(shutil.rmtree, empty_dir, True)
        store2 = Storage(os.path.join(empty_dir, "test.db"))
        try:
            self.assertEqual(store2.unassigned_failing_by_environment(), {})
        finally:
            store2.close()

    def test_by_stream_batches_every_requested_id_in_one_query(
        self
    ) -> None:
        self.store.upsert_runs([
            make_record(environment="linux-sim", test_name="e",
                        result=Result.FAIL, build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
            make_record(environment="linux-sim", test_name="f",
                        result=Result.FAIL, build="feat/y",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        ids = [s.stream_id for s in self.store.list_streams("")]
        conn = self.store._conn()
        statements = []  # type: List[str]
        trace_sql_into(conn, statements)
        try:
            counts = self.store.unassigned_failing_by_stream(ids)
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in statements if s.strip().upper().startswith(
            "SELECT")]
        self.assertEqual(len(selects), 1)
        self.assertEqual(counts, {ids[0]: 1, ids[1]: 1})

    def test_empty_stream_id_list_costs_no_query(self) -> None:
        conn = self.store._conn()
        statements = []  # type: List[str]
        trace_sql_into(conn, statements)
        try:
            result = self.store.unassigned_failing_by_stream([])
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(result, {})
        self.assertEqual(statements, [])


class EnvironmentDeleteTest(StorageTestBase):
    """Deleting an environment must leave the database consistent.

    For an environment that should never have been imported — a reader
    mis-configuration filing runs under a name like ``UNKNOWN``. Unlike
    retirement, this removes rows, so the risk is not "the dashboard
    shows the wrong thing" but "the dashboard cannot read itself":
    `latest_runs` is what every estate-wide query goes through, and a
    row there pointing at a deleted run is a broken page, not a stale
    one.
    """

    def _seed(self) -> None:
        for environment in ("linux-sim", "UNKNOWN"):
            self.store.upsert_runs([
                make_record(environment=environment, test_name="a"),
                make_record(environment=environment, test_name="b",
                            result=Result.FAIL,
                            start=BASE + datetime.timedelta(hours=1)),
            ])
        self.store.ensure_user("alice", CREATED)
        for environment in ("linux-sim", "UNKNOWN"):
            self.store.add_comment(
                environment, "suite.py", "a", "alice", "looking", CREATED)
            self.store.set_assignee(
                environment, "suite.py", "b", "alice", "alice", CREATED)
            self.store.set_environment_expectation(
                environment, 2, "alice", CREATED)
        self.store.set_retired(
            "UNKNOWN", "suite.py", "a", True, "alice", "bogus env", CREATED)

    def _tables_with_environment(self) -> List[str]:
        """Every table in the LIVE schema carrying an environment column."""
        conn = self.store._conn()
        names = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        found = []
        for name in names:
            columns = [
                row[1] for row in conn.execute(
                    "PRAGMA table_info({0})".format(name))
            ]
            if "environment" in columns:
                found.append(name)
        return sorted(found)

    def test_the_table_list_covers_the_whole_schema(self) -> None:
        """A migration that adds an environment table must fail here.

        The delete interpolates table names from a hand-written tuple.
        If a later migration adds a table keyed by environment and does
        not add it there, the rows are silently left behind and nothing
        else in the suite notices — so this asks the schema rather than
        trusting the tuple.
        """
        self.assertEqual(
            sorted(storage._ENVIRONMENT_TABLES),
            self._tables_with_environment(),
            "_ENVIRONMENT_TABLES no longer matches the schema; a table "
            "keyed by environment would keep its rows after a delete")

    def test_it_removes_every_trace_of_the_environment(self) -> None:
        self._seed()
        self.store.delete_environment("UNKNOWN")
        conn = self.store._conn()
        for table in storage._ENVIRONMENT_TABLES:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM {0} WHERE environment = ?".format(
                    table), ("UNKNOWN",)).fetchone()[0]
            self.assertEqual(remaining, 0, table)

    def test_it_leaves_the_other_environments_alone(self) -> None:
        self._seed()
        before = self.store.dashboard_count(environment="linux-sim")
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(
            self.store.dashboard_count(environment="linux-sim"), before)
        self.assertEqual(self.store.environments(), ["linux-sim"])
        self.assertEqual(
            len(self.store.list_environment_expectations()), 1)

    def test_no_derived_row_is_left_pointing_at_a_deleted_run(self) -> None:
        """The failure that would actually break the dashboard."""
        self._seed()
        self.store.delete_environment("UNKNOWN")
        orphans = self.store._conn().execute(
            "SELECT COUNT(*) FROM latest_runs lr "
            "LEFT JOIN runs r ON r.id = lr.run_id WHERE r.id IS NULL"
        ).fetchone()[0]
        self.assertEqual(orphans, 0, "latest_runs references a deleted run")

    def test_no_output_is_left_without_its_run(self) -> None:
        self._seed()
        self.store.delete_environment("UNKNOWN")
        orphans = self.store._conn().execute(
            "SELECT COUNT(*) FROM run_outputs o "
            "LEFT JOIN runs r ON r.id = o.run_id WHERE r.id IS NULL"
        ).fetchone()[0]
        self.assertEqual(orphans, 0, "run_outputs outlived its run")

    def test_the_counts_returned_match_what_the_dry_run_reported(self) -> None:
        """The dry run has to be the same question as the delete."""
        self._seed()
        counted = self.store.count_environment_rows("UNKNOWN")
        deleted = self.store.delete_environment("UNKNOWN")
        self.assertEqual(counted, deleted)
        self.assertGreater(sum(deleted.values()), 0)

    def test_an_absent_environment_is_not_an_error(self) -> None:
        """Re-running a job that already succeeded must be quiet."""
        self._seed()
        self.store.delete_environment("UNKNOWN")
        again = self.store.delete_environment("UNKNOWN")
        self.assertEqual(sum(again.values()), 0)

    def test_the_match_is_exact_not_a_prefix_or_case_fold(self) -> None:
        """`UNKNOWN` must not take `UNKNOWN-2` or `unknown` with it."""
        self.store.upsert_runs([
            make_record(environment="UNKNOWN", test_name="a"),
            make_record(environment="UNKNOWN-2", test_name="a"),
            make_record(environment="unknown", test_name="a"),
        ])
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(
            sorted(self.store.environments()), ["UNKNOWN-2", "unknown"])

    # -- WP-39: the builds the environment was part of ------------------

    def _seed_builds(self) -> Dict[str, int]:
        """Mainline on both environments; a build that ran ONLY on
        UNKNOWN; a build that ran on both, with its UNKNOWN runs both
        older and newer than its linux-sim ones, so both ends of its
        clock have to move when they go. Returns name -> stream id."""
        self._seed()
        hour = datetime.timedelta(hours=1)
        second = datetime.timedelta(seconds=1)
        self.store.upsert_runs([
            make_record(environment="UNKNOWN", test_name="c",
                        build="only-here", start=BASE + 5 * hour),
            make_record(environment="UNKNOWN", test_name="c",
                        build="both", start=BASE + second),
            make_record(environment="linux-sim", test_name="c",
                        build="both", start=BASE + hour + second),
            make_record(environment="UNKNOWN", test_name="c",
                        build="both", start=BASE + 2 * hour + second),
        ])
        ids = {
            stream.name: stream.stream_id
            for stream in self.store.list_streams("")}
        # Posted FROM the only-here build, about a test on the OTHER
        # environment: the comment outlives the environment (a comment
        # on UNKNOWN's own tests goes with it, as it always has).
        self.store.add_comment(
            "linux-sim", "suite.py", "c", "alice", "from the build",
            CREATED, stream_id=ids["only-here"])
        return ids

    def test_a_build_that_only_ran_there_is_removed_with_it(self) -> None:
        """The production finding: the Build picker kept listing a
        build whose only environment had been dropped, as an empty
        build, for ever. `streams` is not keyed by environment, so
        the table-list guard above could not see it."""
        ids = self._seed_builds()
        deleted = self.store.delete_environment("UNKNOWN")
        self.assertEqual(deleted["streams"], 1)
        self.assertIsNone(self.store.get_stream(ids["only-here"]))
        self.assertEqual(
            [stream.name for stream in self.store.list_streams("")],
            ["both"])

    def test_a_build_that_also_ran_elsewhere_keeps_its_other_results(
            self) -> None:
        ids = self._seed_builds()
        self.store.delete_environment("UNKNOWN")
        both = self.store.get_stream(ids["both"])
        self.assertIsNotNone(both)
        self.assertEqual(
            self.store.environments_for_stream(ids["both"]),
            ["linux-sim"])
        # Both ends of its clock were UNKNOWN runs; re-derived from
        # the one linux-sim run that remains.
        remaining = BASE + datetime.timedelta(hours=1, seconds=1)
        self.assertEqual(both.first_seen, remaining)
        self.assertEqual(both.last_seen, remaining)

    def test_mainline_is_never_settled(self) -> None:
        """Mainline is not a build. Its row stays even when this was
        its only environment, and its clock is left alone."""
        self.store.upsert_runs([make_record(environment="UNKNOWN")])
        before = self.store.get_stream(storage.MAINLINE_STREAM_ID)
        deleted = self.store.delete_environment("UNKNOWN")
        self.assertEqual(deleted["streams"], 0)
        after = self.store.get_stream(storage.MAINLINE_STREAM_ID)
        self.assertIsNotNone(after)
        self.assertEqual(after.first_seen, before.first_seen)
        self.assertEqual(after.last_seen, before.last_seen)

    def test_a_removed_builds_comments_survive_untagged(self) -> None:
        """As with every other stream removal: the comment annotates
        the test, and a dangling origin id would differ between the
        backends (SQLite's FK would null it, MariaDB has none)."""
        self._seed_builds()
        self.store.delete_environment("UNKNOWN")
        comments = self.store._conn().execute(
            "SELECT text, stream_id FROM comments "
            "WHERE text = 'from the build'").fetchall()
        self.assertEqual(len(comments), 1)
        self.assertIsNone(comments[0][1])

    def test_the_dry_run_names_the_builds_that_would_go(self) -> None:
        """`test_the_counts_returned_match_what_the_dry_run_reported`
        holds for every key; this pins the one WP-39 added to a
        non-zero case, so the equality is not two zeros agreeing."""
        self._seed_builds()
        counted = self.store.count_environment_rows("UNKNOWN")
        self.assertEqual(counted["streams"], 1)
        self.assertEqual(
            self.store.delete_environment("UNKNOWN")["streams"], 1)

    def test_no_build_is_left_empty(self) -> None:
        """The invariant WP-39 restores, stated directly."""
        self._seed_builds()
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(self.store.prune_empty_streams(), [])

    def test_the_trend_cache_is_invalidated(self) -> None:
        """A memoized chart of an environment that no longer exists.

        The second argument is the ENVIRONMENT filter; an earlier
        version of this test passed ``7`` (a days count that the
        signature does not have), which matched no environment and made
        both calls empty — the assertion could never have failed.
        """
        self.store.upsert_runs([make_record(environment="UNKNOWN")])
        populated = self.store.daily_result_counts(BASE)
        self.assertGreater(
            sum(row.count for row in populated), 0,
            "the memo was never populated; the invalidation check below "
            "is vacuous")
        self.store.delete_environment("UNKNOWN")
        counts = self.store.daily_result_counts(BASE)
        self.assertEqual(
            sum(row.count for row in counts), 0,
            "the trend still reports runs from a deleted environment")


class PruneEmptyStreamsTest(StorageTestBase):
    """Storage.prune_empty_streams (WP-39): the start-up sweep for a
    build whose results went before its row did.

    An empty build cannot be made through this module any more, so the
    fixture plants one the way production got its own: a row in
    `streams` with no partition anywhere else.
    """

    def _plant(self, name: str) -> int:
        conn = self.store._conn()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO streams (product, kind, name, first_seen, "
            "last_seen) VALUES (?, 'build', ?, ?, ?)",
            ("", name, model.format_iso(BASE), model.format_iso(BASE)))
        conn.execute("COMMIT")
        return {
            stream.name: stream.stream_id
            for stream in self.store.list_streams("")}[name]

    def test_removes_the_empty_build_and_reports_it(self) -> None:
        self.store.upsert_runs([make_record(build="live")])
        orphan = self._plant("orphan")
        removed = self.store.prune_empty_streams()
        self.assertEqual(
            [(stream.stream_id, stream.name) for stream in removed],
            [(orphan, "orphan")])
        self.assertEqual(
            [stream.name for stream in self.store.list_streams("")],
            ["live"])
        self.assertIsNotNone(
            self.store.get_stream(storage.MAINLINE_STREAM_ID))

    def test_a_clean_database_is_a_no_op(self) -> None:
        self.store.upsert_runs([make_record(build="live")])
        self.assertEqual(self.store.prune_empty_streams(), [])
        self._plant("orphan")
        self.assertEqual(len(self.store.prune_empty_streams()), 1)
        self.assertEqual(self.store.prune_empty_streams(), [])

    def test_a_build_of_only_retired_tests_is_not_empty(self) -> None:
        """Retirement keeps the latest_runs row; the build still has
        results, they are just not in the estate views."""
        self.store.upsert_runs([make_record(build="parked")])
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "alice", "done",
            CREATED)
        self.assertEqual(self.store.prune_empty_streams(), [])
        self.assertEqual(
            [stream.name for stream in self.store.list_streams("")],
            ["parked"])

    def test_the_orphans_comments_survive_untagged(self) -> None:
        orphan = self._plant("orphan")
        self.store.upsert_runs([make_record()])
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "alice", "why", CREATED,
            stream_id=orphan)
        self.store.prune_empty_streams()
        comments = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual([c.text for c in comments], ["why"])
        self.assertIsNone(comments[0].stream_id)

    def test_mainline_with_no_results_is_kept(self) -> None:
        """A fresh database: mainline has no partition yet and must
        not be swept away before its first import."""
        self.assertEqual(self.store.prune_empty_streams(), [])
        self.assertIsNotNone(
            self.store.get_stream(storage.MAINLINE_STREAM_ID))


class ActivityHoursTest(StorageTestBase):
    """The third derived table cannot drift from `runs` (migration 6).

    The invariant is exact equality with the GROUP BY that
    `_rebuild_activity_hours` runs; `_invariant_diff` compares in both
    directions, and `test_the_comparison_itself_can_fail` plants a skew
    to prove the comparison is not vacuous. WIDENED at migration 10
    (WP-23) to include `stream_id` in both the SELECT and the GROUP BY —
    every test in this class still uses `make_record()` (mainline only,
    stream_id=1), so none of their expected values change, but the
    comparison itself now also catches a stream_id mistake, not only an
    environment/hour/result one. Branch-specific isolation has its own
    dedicated class (`DerivedTablePartitionIsolationTest`).
    """

    def _invariant_diff(self) -> int:
        # The AS t alias: SQLite tolerates an anonymous derived table,
        # MariaDB requires the alias — same reason the exporter's
        # distinct_tests verify query carries one (runbook §E.4).
        conn = self.store._conn()
        forward = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
            "         result, COUNT(*) FROM runs GROUP BY 1, 2, 3, 4"
            "  EXCEPT"
            "  SELECT stream_id, environment, hour, result, count "
            "         FROM activity_hours"
            ") AS t").fetchone()[0]
        backward = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT stream_id, environment, hour, result, count "
            "         FROM activity_hours"
            "  EXCEPT"
            "  SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
            "         result, COUNT(*) FROM runs GROUP BY 1, 2, 3, 4"
            ") AS t").fetchone()[0]
        return int(forward) + int(backward)

    def test_live_maintenance_matches_a_rebuild(self) -> None:
        """Inserts, re-imports and result flips across hours and envs."""
        hour = datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE),
            make_record(test_name="test_b", start=BASE,
                        result=Result.FAIL),
            make_record(test_name="test_a", start=BASE + hour),
            make_record(environment="win-sim", test_name="test_a",
                        start=BASE),
        ])
        # Unchanged re-import: no drift, no double counting.
        self.store.upsert_runs([make_record(test_name="test_a", start=BASE)])
        # Result flip: the run moves between cells of the same hour.
        self.store.upsert_runs([
            make_record(test_name="test_b", start=BASE,
                        result=Result.PASS),
        ])
        self.assertEqual(self._invariant_diff(), 0)

    def test_a_flip_that_empties_a_cell_deletes_the_row(self) -> None:
        """GROUP BY yields no zero groups, so neither may the table."""
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.store.upsert_runs([make_record(result=Result.PASS)])
        rows = self.store._conn().execute(
            "SELECT result, count FROM activity_hours").fetchall()
        self.assertEqual(rows, [("PASS", 1)])
        self.assertEqual(self._invariant_diff(), 0)

    def test_environment_delete_keeps_the_invariant(self) -> None:
        self.store.upsert_runs([
            make_record(environment="UNKNOWN"),
            make_record(environment="linux-sim"),
        ])
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(self._invariant_diff(), 0)
        rows = self.store._conn().execute(
            "SELECT DISTINCT environment FROM activity_hours").fetchall()
        self.assertEqual(rows, [("linux-sim",)])

    def test_prune_keeps_the_invariant(self) -> None:
        """Retention deletes history; the table must follow it."""
        day = datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(start=BASE - 400 * day),
            make_record(start=BASE),
        ])
        deleted = self.store.prune_runs_before(BASE - 300 * day)
        self.assertEqual(deleted, 1)
        self.assertEqual(self._invariant_diff(), 0)

    def test_a_stream_drop_keeps_the_invariant(self) -> None:
        """WP-23 (migration 10): tools/drop_stream.py deletes a stream's
        runs AND its activity_hours rows -- if it only did the former,
        the invariant would rot silently the moment the stream's runs
        vanished but its hour buckets did not."""
        self.store.upsert_runs([
            make_record(),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        branch_id = self.store.list_streams("")[0].stream_id
        self.store.delete_stream(branch_id)
        self.assertEqual(self._invariant_diff(), 0)
        rows = self.store._conn().execute(
            "SELECT COUNT(*) FROM activity_hours WHERE stream_id = ?",
            (branch_id,)).fetchone()
        self.assertEqual(rows[0], 0)

    def test_the_comparison_itself_can_fail(self) -> None:
        """Planted drift MUST be reported, or every pass above is noise."""
        self.store.upsert_runs([make_record()])
        self.store._conn().execute(
            "UPDATE activity_hours SET count = count + 1")
        self.assertGreater(self._invariant_diff(), 0)

    def test_reads_come_from_the_derived_table_not_from_runs(self) -> None:
        """Plant a skew in the table; the readers must report the skew.

        This is the cost assertion in disguise: a reader that still
        scanned `runs` would return the truth here and the test would
        fail, which is exactly what stops the O(history) query coming
        back quietly.
        """
        self.store.upsert_runs([make_record()])
        conn = self.store._conn()
        conn.execute("UPDATE activity_hours SET count = 41")
        buckets = self.store.activity_buckets(BASE - datetime.timedelta(1))
        self.assertEqual([b[2] for b in buckets], [41])
        self.store._invalidate_trend_cache()  # the skew was direct SQL
        trend = self.store.daily_result_counts(BASE)
        self.assertEqual([row.count for row in trend], [41])


class ScriptHoursTest(StorageTestBase):
    """The fourth derived table cannot drift from `runs` (migration 7).

    Same contract as ActivityHoursTest, with the extra columns in the
    invariant: `script_hours` also carries MIN(start_time) and
    MAX(end_time) per bucket, and a MIN/MAX cannot be decremented — so
    the shrink paths (an update changing a stored result or end time)
    go through exact recomputation and are what these tests lean on.
    WIDENED at migration 10 (WP-23) to include `stream_id`, same
    reasoning as `ActivityHoursTest`.
    """

    _GROUP_BY = (
        "SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
        "script, result, COUNT(*), MIN(start_time), MAX(end_time) "
        "FROM runs GROUP BY 1, 2, 3, 4, 5"
    )
    _TABLE = (
        "SELECT stream_id, environment, hour, script, result, count, "
        "first_start, last_end FROM script_hours"
    )

    def _invariant_diff(self) -> int:
        # AS t: MariaDB requires derived-table aliases; SQLite accepts.
        conn = self.store._conn()
        forward = conn.execute(
            "SELECT COUNT(*) FROM ({0} EXCEPT {1}) AS t".format(
                self._GROUP_BY, self._TABLE)
        ).fetchone()[0]
        backward = conn.execute(
            "SELECT COUNT(*) FROM ({0} EXCEPT {1}) AS t".format(
                self._TABLE, self._GROUP_BY)
        ).fetchone()[0]
        return int(forward) + int(backward)

    def test_live_maintenance_matches_a_rebuild(self) -> None:
        """Inserts, re-imports and result flips across scripts and hours."""
        hour = datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE),
            make_record(test_name="test_b", start=BASE,
                        result=Result.FAIL),
            make_record(script="other.py", test_name="test_a",
                        start=BASE + hour),
            make_record(environment="win-sim", test_name="test_a",
                        start=BASE),
        ])
        # Unchanged re-import: no drift, no double counting.
        self.store.upsert_runs([make_record(test_name="test_a", start=BASE)])
        # Result flip: the run moves between cells of the same bucket.
        self.store.upsert_runs([
            make_record(test_name="test_b", start=BASE,
                        result=Result.PASS),
        ])
        self.assertEqual(self._invariant_diff(), 0)

    def test_the_span_columns_are_exact_timestamps(self) -> None:
        """Sub-hour ordering is the point; hour edges would lose it."""
        minute = datetime.timedelta(minutes=1)
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE + 7 * minute),
            make_record(test_name="test_b", start=BASE + 21 * minute),
        ])
        rows = self.store._conn().execute(
            "SELECT first_start, last_end FROM script_hours").fetchall()
        self.assertEqual(rows, [(
            "2026-07-01T02:07:00.000000",
            "2026-07-01T02:21:03.000000",
        )])
        self.assertEqual(self._invariant_diff(), 0)

    def test_an_end_time_change_that_shrinks_a_bucket(self) -> None:
        """The MAX cannot be decremented, so this is the recompute path.

        test_b's end is the bucket's last_end; a re-import (different
        output, so not skipped) pulls it back before test_a's. A grown
        merge could only keep the stale MAX; only exact recomputation
        can shrink it.
        """
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE),
            make_record(test_name="test_b", start=BASE,
                        end=BASE + datetime.timedelta(minutes=9)),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_b", start=BASE,
                        end=BASE + datetime.timedelta(seconds=1),
                        output="reparsed\n"),
        ])
        self.assertEqual(self._invariant_diff(), 0)
        row = self.store._conn().execute(
            "SELECT last_end FROM script_hours").fetchone()
        self.assertEqual(row[0], "2026-07-01T02:00:03.000000")

    def test_a_grown_bucket_recomputed_in_the_same_batch(self) -> None:
        """Recompute must win over the merge, not double-count it.

        One batch both inserts into a bucket and shrinks it: the
        recomputation already sees the inserted row in `runs`, so
        applying the growth as well would count it twice.
        """
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE,
                        end=BASE + datetime.timedelta(minutes=9)),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", start=BASE,
                        end=BASE + datetime.timedelta(seconds=1),
                        output="reparsed\n"),
            make_record(test_name="test_b", start=BASE),
        ])
        self.assertEqual(self._invariant_diff(), 0)
        row = self.store._conn().execute(
            "SELECT count FROM script_hours").fetchone()
        self.assertEqual(int(row[0]), 2)

    def test_a_flip_that_empties_a_cell_deletes_the_row(self) -> None:
        """GROUP BY yields no zero groups, so neither may the table."""
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.store.upsert_runs([make_record(result=Result.PASS)])
        rows = self.store._conn().execute(
            "SELECT result, count FROM script_hours").fetchall()
        self.assertEqual(rows, [("PASS", 1)])
        self.assertEqual(self._invariant_diff(), 0)

    def test_environment_delete_keeps_the_invariant(self) -> None:
        self.store.upsert_runs([
            make_record(environment="UNKNOWN"),
            make_record(environment="linux-sim"),
        ])
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(self._invariant_diff(), 0)
        rows = self.store._conn().execute(
            "SELECT DISTINCT environment FROM script_hours").fetchall()
        self.assertEqual(rows, [("linux-sim",)])

    def test_prune_keeps_the_invariant(self) -> None:
        """Retention deletes history; the table must follow it."""
        day = datetime.timedelta(days=1)
        self.store.upsert_runs([
            make_record(start=BASE - 400 * day),
            make_record(start=BASE),
        ])
        deleted = self.store.prune_runs_before(BASE - 300 * day)
        self.assertEqual(deleted, 1)
        self.assertEqual(self._invariant_diff(), 0)

    def test_a_stream_drop_keeps_the_invariant(self) -> None:
        """WP-23 (migration 10): tools/drop_stream.py deletes a stream's
        runs AND its script_hours rows -- the script_hours twin of
        ActivityHoursTest's version of this test."""
        self.store.upsert_runs([
            make_record(),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        branch_id = self.store.list_streams("")[0].stream_id
        self.store.delete_stream(branch_id)
        self.assertEqual(self._invariant_diff(), 0)
        rows = self.store._conn().execute(
            "SELECT COUNT(*) FROM script_hours WHERE stream_id = ?",
            (branch_id,)).fetchone()
        self.assertEqual(rows[0], 0)

    def test_the_comparison_itself_can_fail(self) -> None:
        """Planted drift MUST be reported, or every pass above is noise."""
        self.store.upsert_runs([make_record()])
        self.store._conn().execute(
            "UPDATE script_hours SET count = count + 1")
        self.assertGreater(self._invariant_diff(), 0)

    def test_reads_come_from_the_derived_table_not_from_runs(self) -> None:
        """Plant a skew in the table; the reader must report the skew.

        The cost assertion in disguise, as in ActivityHoursTest: a
        reader that still scanned `runs` would return the truth here
        and the test would fail — which is what stops the O(history)
        query coming back quietly.
        """
        self.store.upsert_runs([make_record()])
        self.store._conn().execute("UPDATE script_hours SET count = 41")
        buckets = self.store.script_activity(
            "linux-sim", BASE - datetime.timedelta(hours=1),
            BASE + datetime.timedelta(hours=1))
        self.assertEqual([b.count for b in buckets], [41])

    def test_the_window_read_is_an_index_range_not_a_scan(self) -> None:
        """The PK leads (stream_id, environment, hour) so one block is
        one seek — widened at migration 10 (WP-23) to include stream_id,
        which is what lets a branch's own Timeline stay an index range
        too, not merely mainline's.

        Pinned because the column order is the entire reason the table
        can be read at request time: keyed (environment, script, hour)
        instead, the same query would walk the environment's whole
        history and grow with it — the O(history) shape migration 6
        was bought to end.
        """
        self.store.upsert_runs([make_record()])
        plan = self.store._conn().execute(
            "EXPLAIN QUERY PLAN SELECT script, hour, result, count, "
            "first_start, last_end FROM script_hours "
            "WHERE stream_id = ? AND environment = ? AND hour >= ? "
            "AND hour <= ? ORDER BY script, hour",
            (storage.MAINLINE_STREAM_ID, "linux-sim", "2026-07-01T00",
             "2026-07-01T23"),
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn("SEARCH", detail.upper(), detail)
        self.assertIn("hour", detail, detail)

    def test_script_activity_is_one_environment_one_window(self) -> None:
        hour = datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(start=BASE),
            make_record(environment="win-sim", start=BASE),
            make_record(test_name="test_b", start=BASE + 3 * hour),
        ])
        buckets = self.store.script_activity(
            "linux-sim", BASE, BASE + hour)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].script, "suite.py")
        self.assertEqual(buckets[0].result, Result.PASS)
        self.assertEqual(buckets[0].first_start, BASE)


class ScriptTestCountsTest(StorageTestBase):
    """The Timeline's "of N known tests" denominator."""

    def test_counts_per_script_exclude_retired(self) -> None:
        self.store.upsert_runs([
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
            make_record(script="other.py", test_name="test_c"),
            make_record(environment="win-sim", test_name="test_d"),
        ])
        self.store.set_retired(
            "linux-sim", "suite.py", "test_b", True, "amy",
            "left the suite", CREATED)
        self.assertEqual(
            self.store.script_test_counts("linux-sim"),
            {"suite.py": 1, "other.py": 1})

    def test_an_unknown_environment_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.store.script_test_counts("nowhere"), {})


class ScriptRunsUntilTest(StorageTestBase):
    """The window ceiling the Timeline's row expansion depends on."""

    def test_until_is_inclusive_and_bounds_the_window(self) -> None:
        hour = datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(start=BASE),
            make_record(start=BASE + hour),
            make_record(start=BASE + 5 * hour),
        ])
        runs = self.store.script_runs(
            "linux-sim", "suite.py", BASE, 100, until=BASE + hour)
        self.assertEqual(
            [run.start_time for run in runs], [BASE, BASE + hour])

    def test_omitting_until_keeps_the_old_shape(self) -> None:
        self.store.upsert_runs([make_record(start=BASE)])
        runs = self.store.script_runs("linux-sim", "suite.py", BASE, 100)
        self.assertEqual(len(runs), 1)

    def test_defaults_to_mainline(self) -> None:
        """F7 (docs/STREAMS_PLAN.md §5.2 "as built"): a branch run in
        the SAME window as a mainline one must not appear in the
        default (unscoped) read."""
        self.store.upsert_runs([make_record(
            test_name="mainline_only", start=BASE)])
        self.store.upsert_runs([make_record(
            test_name="branch_only", build="feat/x", start=BASE)])
        runs = self.store.script_runs("linux-sim", "suite.py", BASE, 100)
        self.assertEqual([run.test_name for run in runs], ["mainline_only"])

    def test_stream_id_selects_the_branch_own_runs(self) -> None:
        self.store.upsert_runs([make_record(
            test_name="mainline_only", start=BASE)])
        self.store.upsert_runs([make_record(
            test_name="branch_only", build="feat/x", start=BASE)])
        stream_id = self.store.list_streams("")[0].stream_id
        runs = self.store.script_runs(
            "linux-sim", "suite.py", BASE, 100, stream_id=stream_id)
        self.assertEqual([run.test_name for run in runs], ["branch_only"])


class ReimportSkipTest(StorageTestBase):
    """What counts as "unchanged", and what the skip must NOT skip."""

    def test_output_only_change_is_an_update(self) -> None:
        self.store.upsert_runs([make_record()])
        counts = self.store.upsert_runs(
            [make_record(output="the parser found more log\n")])
        self.assertEqual(
            counts,
            storage.UpsertCounts(inserted=0, updated=1, unchanged=0, rejections=[]),
        )
        run = self.store.latest_run("linux-sim", "suite.py", "test_a")
        assert run is not None
        stored = self.store.get_run(run.run_id)
        assert stored is not None
        self.assertEqual(stored.output, "the parser found more log\n")
        again = self.store.upsert_runs(
            [make_record(output="the parser found more log\n")])
        self.assertEqual(again.unchanged, 1)

    def test_a_null_fingerprint_takes_the_write_path_once(self) -> None:
        """Every pre-migration row self-heals on its first re-import."""
        self.store.upsert_runs([make_record()])
        self.store._conn().execute(
            "UPDATE runs SET output_fingerprint = NULL")
        first = self.store.upsert_runs([make_record()])
        self.assertEqual(first.updated, 1)  # stamps the fingerprint
        second = self.store.upsert_runs([make_record()])
        self.assertEqual(second.unchanged, 1)

    def test_an_unchanged_reimport_does_not_unretire(self) -> None:
        """The 10-minute re-push must not make retirement impossible.

        Before the skip, ANY re-import of a triple cleared its
        retirement — so with a feeder re-pushing its whole window every
        10 minutes, a human's approval could not survive to the next
        pass of the suite. An unchanged record is not the test
        "reporting a run again"; it is the same run being repeated.
        """
        self.store.upsert_runs([make_record()])
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "amy",
            "left the suite", BASE)
        self.store.upsert_runs([make_record()])
        self.assertTrue(
            self.store.is_retired("linux-sim", "suite.py", "test_a"),
            "an unchanged re-import cleared a human's retirement")

    def test_a_changed_reimport_still_unretires(self) -> None:
        """Pinned so the skip is the ONLY thing the last test proves.

        WIDENED for WP-21 (docs/STREAMS_PLAN.md §3.4): un-retirement is
        mainline-only, so this now also pins the branch case in the same
        test — a branch run reporting against a mainline-retired test
        must NOT clear the retirement, because the branch run may predate
        the decision to retire it. Never weakened: the original mainline
        assertion is untouched below.
        """
        self.store.upsert_runs([make_record()])
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "amy",
            "left the suite", BASE)
        self.store.upsert_runs([
            make_record(
                result=Result.FAIL, build="feat/x",
                start=BASE + datetime.timedelta(hours=1),
            )
        ])
        self.assertTrue(
            self.store.is_retired("linux-sim", "suite.py", "test_a"),
            "a branch import must never un-retire a mainline test")
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.assertFalse(
            self.store.is_retired("linux-sim", "suite.py", "test_a"))


class TestStreams(StorageTestBase):
    """WP-21: stream find-or-create, upsert scoping, migration 9 basics."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)

    def test_mainline_row_is_seeded_by_migration(self) -> None:
        stream = self.store.get_stream(storage.MAINLINE_STREAM_ID)
        assert stream is not None
        self.assertEqual((stream.product, stream.kind, stream.name),
                          ("", "mainline", ""))

    def test_mainline_records_default_to_stream_one(self) -> None:
        self.store.upsert_runs([make_record()])
        row = self.store._conn().execute(
            "SELECT stream_id FROM runs").fetchone()
        self.assertEqual(row[0], storage.MAINLINE_STREAM_ID)

    def test_mainlines_own_last_seen_advances_too(self) -> None:
        """Row 1 is not just a seeded constant: a mainline import widens
        its own first_seen/last_seen the same way a branch import widens
        its stream's -- otherwise "baseline last_seen" (the compare
        endpoint, the Watchlist's s: cards) would read the migration
        timestamp forever.

        The record's start_time is set safely in the future (well past
        the migration's real seed timestamp, whatever "now" happens to
        be when this test runs) so widening it can only be an increase.
        """
        before = self.store.get_stream(storage.MAINLINE_STREAM_ID)
        assert before is not None
        later = datetime.datetime(2099, 1, 1)
        self.store.upsert_runs([make_record(start=later)])
        after = self.store.get_stream(storage.MAINLINE_STREAM_ID)
        assert after is not None
        self.assertEqual(after.last_seen, later)
        self.assertGreater(after.last_seen, before.last_seen)

    def test_a_build_record_creates_a_stream_of_kind_build(self) -> None:
        self.store.upsert_runs([make_record(build="2026.9.1")])
        streams = self.store.list_streams("Atlas")
        self.assertEqual(len(streams), 1)
        self.assertEqual(
            (streams[0].kind, streams[0].name), ("build", "2026.9.1"))

    def test_reimporting_the_same_build_reuses_the_stream(self) -> None:
        self.store.upsert_runs([make_record(build="feat/x")])
        self.store.upsert_runs([make_record(
            test_name="test_b", build="feat/x",
            start=BASE + datetime.timedelta(hours=1))])
        streams = self.store.list_streams("Atlas")
        self.assertEqual(len(streams), 1, "a second push must not create "
                          "a second stream for the same name")

    def test_different_products_get_distinct_streams_of_the_same_name(
            self) -> None:
        self.store.set_environment_product(
            "other-env", "Zephyr", "alice", CREATED)
        self.store.upsert_runs([
            make_record(environment="linux-sim", build="feat/x"),
            make_record(environment="other-env", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        self.assertEqual(len(self.store.list_streams("Atlas")), 1)
        self.assertEqual(len(self.store.list_streams("Zephyr")), 1)

    def test_stream_product_is_fixed_at_creation(self) -> None:
        """Re-declaring the environment's product later must not move
        an already-created stream — resolved at creation, then fixed
        (docs/STREAMS_PLAN.md §3.3)."""
        self.store.upsert_runs([make_record(build="feat/x")])
        stream = self.store.list_streams("Atlas")[0]
        self.store.set_environment_product(
            "linux-sim", "Renamed", "alice", CREATED)
        still_there = self.store.get_stream(stream.stream_id)
        assert still_there is not None
        self.assertEqual(still_there.product, "Atlas")
        self.assertEqual(self.store.list_streams("Renamed"), [])

    def test_an_environment_with_no_declared_product_uses_empty_string(
            self) -> None:
        self.store.upsert_runs([make_record(
            environment="undeclared-env", build="feat/x")])
        streams = self.store.list_streams("")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].product, "")

    def test_last_seen_widens_across_a_batch(self) -> None:
        self.store.upsert_runs([
            make_record(build="feat/x", start=BASE),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=3)),
        ])
        stream = self.store.list_streams("Atlas")[0]
        self.assertEqual(stream.first_seen, BASE)
        self.assertEqual(
            stream.last_seen, BASE + datetime.timedelta(hours=3))

    def test_last_seen_widens_on_a_later_push(self) -> None:
        self.store.upsert_runs([make_record(build="feat/x", start=BASE)])
        first = self.store.list_streams("Atlas")[0]
        later = BASE + datetime.timedelta(days=1)
        self.store.upsert_runs([make_record(
            test_name="test_b", build="feat/x", start=later)])
        second = self.store.list_streams("Atlas")[0]
        self.assertEqual(second.stream_id, first.stream_id)
        self.assertEqual(second.first_seen, BASE)
        self.assertEqual(second.last_seen, later)


class LegacyUniqueCollisionTest(StorageTestBase):
    """docs/STREAMS_PLAN.md §3.2: the frozen v1 UNIQUE on ``runs``.

    A branch run whose (environment, script, test_name, start_time) is
    IDENTICAL to a run already stored on a DIFFERENT stream cannot be
    stored (the table-level UNIQUE has no stream column). It must be
    REJECTED, naming both streams, with the rest of the batch unaffected
    — never a silent wrong-stream overwrite, never an aborted batch.
    """

    def test_a_same_instant_build_run_is_rejected_not_overwritten(
            self) -> None:
        self.store.upsert_runs([make_record()])
        counts = self.store.upsert_runs(
            [make_record(build="feat/x")])  # identical start_time
        self.assertEqual(counts.inserted, 0)
        self.assertEqual(len(counts.rejections), 1)
        rejection = counts.rejections[0]
        self.assertEqual(rejection.index, 0)
        self.assertIn("mainline", rejection.message)
        self.assertIn("build:feat/x", rejection.message)
        self.assertEqual(rejection.environment, "linux-sim")
        self.assertEqual(rejection.test_name, "test_a")
        # The original mainline row must be untouched.
        row = self.store._conn().execute(
            "SELECT stream_id FROM runs").fetchone()
        self.assertEqual(row[0], storage.MAINLINE_STREAM_ID)

    def test_the_rest_of_the_batch_still_imports(self) -> None:
        """One bad record must never abort the batch (project-wide rule)."""
        self.store.upsert_runs([make_record()])
        counts = self.store.upsert_runs([
            make_record(build="feat/x"),          # collides, rejected
            make_record(test_name="test_b", build="feat/x"),  # fine
        ])
        self.assertEqual(len(counts.rejections), 1)
        self.assertEqual(counts.inserted, 1)
        streams = self.store.list_streams("")
        # test_b's stream was created even though test_a's record in the
        # same batch collided.
        self.assertEqual(len(streams), 1)

    def test_the_message_is_identical_regardless_of_which_side_is_mainline(
            self) -> None:
        """A collision between two NON-mainline streams also rejects and
        names both — the rule is about the legacy key, not about
        mainline specifically."""
        self.store.upsert_runs([make_record(build="feat/x")])
        counts = self.store.upsert_runs([make_record(build="feat/y")])
        self.assertEqual(len(counts.rejections), 1)
        message = counts.rejections[0].message
        self.assertIn("build:feat/x", message)
        self.assertIn("build:feat/y", message)


class DerivedTablePartitionIsolationTest(StorageTestBase):
    """docs/STREAMS_PLAN.md §5.1/§5.2 (WP-23): ``activity_hours``/
    ``script_hours`` are maintained for EVERY stream now (the WP-21 skip
    that kept them mainline-only is gone — see :meth:`upsert_runs`), so
    the invariant this class pins is no longer "a branch import touches
    neither table" — it is PARTITION ISOLATION: a write to one stream's
    partition (``stream_id`` leads both tables' PRIMARY KEY) can never
    change so much as one row of another stream's partition, in either
    direction. This is the guard test named in WP-23's spec, WIDENED
    rather than weakened per CLAUDE.md's rule — the commit message says
    so: the old assertion ("branch import leaves the tables unchanged")
    is now FALSE by design (a branch's own trend needs its own rows);
    what must still hold, and what these tests now check instead, is
    that each stream's rows are exactly what that stream's own runs
    would produce, and never leak into another stream's rows.
    """

    @staticmethod
    def _partition(
        conn: "sqlite3.Connection", stream_id: int
    ) -> "Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]]]":
        activity = conn.execute(
            "SELECT * FROM activity_hours WHERE stream_id = ? "
            "ORDER BY 1, 2, 3, 4", (stream_id,)).fetchall()
        script = conn.execute(
            "SELECT * FROM script_hours WHERE stream_id = ? "
            "ORDER BY 1, 2, 3, 4, 5", (stream_id,)).fetchall()
        return activity, script

    def test_a_branch_only_import_leaves_the_mainline_partition_untouched(
            self) -> None:
        """Mainline seeded first; a branch-only import must not add,
        remove or alter a single mainline row — but the branch DOES gain
        its own rows now (the point of WP-23), so this checks the
        mainline partition specifically, not "the tables" as a whole."""
        self.store.upsert_runs([make_record()])  # mainline seed
        conn = self.store._conn()
        before_mainline = self._partition(conn, storage.MAINLINE_STREAM_ID)
        self.store.upsert_runs([
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=2)),
            make_record(test_name="test_c", build="feat/x",
                        result=Result.FAIL,
                        start=BASE + datetime.timedelta(hours=3)),
        ])
        after_mainline = self._partition(conn, storage.MAINLINE_STREAM_ID)
        self.assertEqual(before_mainline, after_mainline)
        # And the branch's own partition is NOT empty -- WP-23 deleted
        # the skip specifically so this would be true.
        branch_id = self.store.list_streams("")[0].stream_id
        branch_activity, branch_script = self._partition(conn, branch_id)
        self.assertEqual(len(branch_activity), 2)  # two hours, two runs
        self.assertEqual(len(branch_script), 2)

    def test_a_mainline_import_leaves_an_existing_branch_partition_untouched(
            self) -> None:
        """The reverse direction: seed a branch first, then import more
        mainline data. The branch's own rows must be untouched."""
        self.store.upsert_runs([make_record(build="feat/x")])
        branch_id = self.store.list_streams("")[0].stream_id
        conn = self.store._conn()
        before_branch = self._partition(conn, branch_id)
        self.store.upsert_runs([
            make_record(test_name="test_mainline",
                        start=BASE + datetime.timedelta(hours=5)),
        ])
        after_branch = self._partition(conn, branch_id)
        self.assertEqual(before_branch, after_branch)

    def test_a_pure_branch_estate_has_an_empty_mainline_partition(
            self) -> None:
        """No mainline data at all: stream 1's partition of both tables
        stays at its seeded (empty) state -- not merely "unchanged from
        a snapshot" -- even though the branch's OWN partition is not
        empty (WP-23's whole point: the branch gets its own rows)."""
        self.store.upsert_runs([make_record(build="feat/x")])
        conn = self.store._conn()
        mainline_activity, mainline_script = self._partition(
            conn, storage.MAINLINE_STREAM_ID)
        self.assertEqual(mainline_activity, [])
        self.assertEqual(mainline_script, [])
        branch_id = self.store.list_streams("")[0].stream_id
        branch_activity, branch_script = self._partition(conn, branch_id)
        self.assertEqual(len(branch_activity), 1)
        self.assertEqual(len(branch_script), 1)

    def test_two_branches_do_not_leak_into_each_other(self) -> None:
        """Not just mainline vs. one branch: two SIBLING branches must
        stay isolated from each other too -- partition isolation is a
        property of stream_id in general, not a mainline special case."""
        self.store.upsert_runs([make_record(build="feat/x")])
        self.store.upsert_runs([
            make_record(test_name="test_b", build="feat/y",
                        result=Result.FAIL,
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        conn = self.store._conn()
        streams = {s.name: s.stream_id for s in self.store.list_streams("")}
        x_activity, x_script = self._partition(conn, streams["feat/x"])
        y_activity, y_script = self._partition(conn, streams["feat/y"])
        self.assertEqual(len(x_activity), 1)
        self.assertEqual(len(y_activity), 1)
        self.assertNotEqual(x_activity, y_activity)
        self.assertEqual(len(x_script), 1)
        self.assertEqual(len(y_script), 1)


class ComparePairsQueryPlanTest(StorageTestBase):
    """WP-23 perf pass: the compare pairs query must join ``latest_runs``
    to ITSELF on its PRIMARY KEY, never through a materialized
    ``(SELECT ... FROM latest_runs WHERE ...)`` derived table.

    A derived table has no index of its own, so SQLite could only
    nested-loop the two partitions against each other — the ORIGINAL
    shape, one ``(SELECT ...) s``/``(SELECT ...) m`` per side. MEASURED
    on the dev-scale seeded copy (``.../scratchpad/testboard-wp23.db``,
    NOT production — a copy was taken via ``sqlite3.Connection.backup``
    so the live server on port 8791 was never touched; build 2026.9.1
    vs build 2026.9.0, 2036 ``latest_runs`` rows each side), 15 samples
    each:

        compare_counts (headline)        622.8ms -> 4.4ms  median
        compare_category (one page)      643.6ms -> 4.1ms  median
        compare_category_count           643.0ms -> 3.7ms  median
        full page request (all three)   1942.5ms -> 12.2ms median

    Asserting on the PLAN rather than a duration, the same reasoning
    ``TestSortIndexesAreUsed`` above gives: a timing test on a fast dev
    machine proves nothing about a slower one, but the plan is the same
    on both. Calls :meth:`Storage._compare_pairs_sql` directly rather
    than tracing a public wrapper — it is the one method every public
    entry point (``compare_counts``, ``compare_category``,
    ``compare_category_count``) shares, so this is the real query, not
    a hand-rebuilt lookalike that could drift from it.
    """

    #: The four join partition aliases the pairs query builds — each
    #: must be a PRIMARY KEY SEARCH on ``latest_runs`` (or, for the two
    #: which are the "anchor" side of their half and read a RANGE over
    #: one stream_id rather than a single triple, a covering index
    #: search — see ``m2``'s "USING COVERING INDEX idx_latest_runs_result"
    #: below); none may ever be table-scanned.
    _JOIN_ALIASES = ("S", "M", "M2", "S2")

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([make_record(test_name="test_a")])
        self.store.upsert_runs([make_record(
            test_name="test_a", build="feat/x",
            start=BASE + datetime.timedelta(hours=1))])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def _plan(self) -> List[str]:
        environments = self.store.environments_for_product("Atlas")
        sql, params = self.store._compare_pairs_sql(
            self.stream_id, storage.MAINLINE_STREAM_ID, environments)
        rows = self.store._conn().execute(
            "EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return [str(row[-1]) for row in rows]

    @staticmethod
    def _alias_line(plan: List[str], alias: str) -> Optional[str]:
        """The plan line for *alias*, across EQP dialects.

        Modern SQLite prints ``SEARCH m USING ...``; the 3.26 bundled
        with RHEL 8's Python 3.6 — the PRODUCTION interpreter, and the
        two CI legs that caught this — prints
        ``SEARCH TABLE latest_runs AS m USING ...``. The first CI run
        of this test asserted the modern spelling only and failed on
        both 3.6 legs while the underlying PLAN was correct (fully
        indexed PK probes); this helper is the widening, matching the
        ALIAS rather than one version's phrasing. SCAN lines are
        matched the same two ways by the caller.
        """
        upper = alias.upper()
        for row in plan:
            text = row.strip().upper()
            for verb in ("SEARCH", "SCAN"):
                if (text.startswith("{0} {1} ".format(verb, upper))
                        or (text.startswith(verb + " TABLE ")
                            and " AS {0} ".format(upper) in text)):
                    return text
        return None

    def test_every_join_side_is_indexed_never_scanned(self) -> None:
        plan = self._plan()
        plan_text = " | ".join(plan).upper()
        for alias in self._JOIN_ALIASES:
            line = self._alias_line(plan, alias)
            self.assertIsNotNone(
                line, "no SEARCH/SCAN line for {0} — plan: {1}".format(
                    alias, plan_text))
            self.assertTrue(
                line.startswith("SEARCH") and "USING" in line,
                "{0} is not an indexed SEARCH — the regression this "
                "test exists to catch. Its line: {1}".format(
                    alias, line))

    def test_no_materialized_subquery_join(self) -> None:
        """The specific pre-fix shape, confirmed by reverting this
        commit's SQL change and re-running this exact test: each side
        was a parenthesised ``(SELECT ...)`` per partition, which
        SQLite ran by MATERIALIZING it, then joining the other side
        against that materialization with no index of its own —
        "MATERIALIZE m" / "MATERIALIZE s2" in the plan, followed by a
        "SCAN"/"AUTOMATIC COVERING INDEX" (built on the fly, over the
        already-materialized rows) rather than a SEARCH on the real
        table's PRIMARY KEY."""
        plan_text = " | ".join(self._plan()).upper()
        self.assertNotIn(
            "MATERIALIZE", plan_text,
            "a join side is being materialized as a derived table "
            "again — the exact pre-fix regression: " + plan_text)

    def test_the_joined_side_uses_the_latest_runs_primary_key(
        self
    ) -> None:
        """``m``/``s2`` (the LEFT JOIN target on each side) must probe
        the exact PRIMARY KEY — stream_id, environment, script,
        test_name — not a partial prefix of it."""
        plan = self._plan()
        for alias in ("m", "s2"):
            line = self._alias_line(plan, alias)
            self.assertIsNotNone(line, "no SEARCH line for " + alias)
            for column in ("STREAM_ID=?", "ENVIRONMENT=?",
                           "SCRIPT=?", "TEST_NAME=?"):
                self.assertIn(
                    column, line,
                    "{0} does not probe the full PRIMARY KEY — its "
                    "line: {1}".format(alias, line))


class CompareStreamsTest(StorageTestBase):
    """docs/STREAMS_PLAN.md §3.5: the five counts, and the paginated
    per-category listing, of a stream-vs-mainline comparison."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        branch_start = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.PASS),
            make_record(test_name="test_b", result=Result.FAIL),
            make_record(test_name="test_c", result=Result.PASS),
            make_record(test_name="test_e", result=Result.FAIL),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/x", start=branch_start),
            make_record(test_name="test_b", result=Result.PASS,
                        build="feat/x", start=branch_start),
            make_record(test_name="test_d", result=Result.PASS,
                        build="feat/x", start=branch_start),
            make_record(test_name="test_e", result=Result.FAIL,
                        build="feat/x", start=branch_start),
        ])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def test_the_five_counts(self) -> None:
        counts = self.store.compare_counts(self.stream_id)
        self.assertEqual(counts, storage.CompareCounts(
            new_failures=1,   # test_a: PASS on mainline, FAIL on branch
            new_passes=1,     # test_b: FAIL on mainline, PASS on branch
            both_failing=1,   # test_e: FAIL on both
            new_tests=1,      # test_d: only on the branch
            no_result=1,      # test_c: only on mainline
            agree=0,          # no pair is a non-FAIL match on both sides
        ))

    def test_new_failures_direction(self) -> None:
        rows = self.store.compare_category(self.stream_id, "new_failures")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].test_name, "test_a")
        self.assertEqual(rows[0].stream_result, Result.FAIL)
        self.assertEqual(rows[0].baseline_result, Result.PASS)

    def test_new_tests_direction_is_present_on_stream_absent_on_baseline(
            self) -> None:
        rows = self.store.compare_category(self.stream_id, "new_tests")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].test_name, "test_d")
        self.assertEqual(rows[0].stream_result, Result.PASS)
        self.assertIsNone(rows[0].baseline_result)

    def test_no_result_direction_is_absent_on_stream_present_on_baseline(
            self) -> None:
        rows = self.store.compare_category(self.stream_id, "no_result")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].test_name, "test_c")
        self.assertIsNone(rows[0].stream_result)
        self.assertEqual(rows[0].baseline_result, Result.PASS)
        # test_c has no run on the stream side at all -- nothing to
        # review there, a fact rather than a gap (WP-21 §3.6).
        self.assertIsNone(rows[0].stream_run_id)

    def test_stream_run_id_is_the_streams_own_run_never_the_baselines(
            self) -> None:
        rows = self.store.compare_category(self.stream_id, "new_failures")
        self.assertIsNotNone(rows[0].stream_run_id)
        branch_run = self.store.latest_run(
            "linux-sim", "suite.py", "test_a", stream_id=self.stream_id)
        self.assertEqual(rows[0].stream_run_id, branch_run.run_id)
        mainline_run = self.store.latest_run(
            "linux-sim", "suite.py", "test_a")
        self.assertNotEqual(rows[0].stream_run_id, mainline_run.run_id)
        self.assertEqual(rows[0].stream_start_time, branch_run.start_time)

    def test_assignee_is_the_triples_current_assignee_not_stream_scoped(
            self) -> None:
        """Assigning is not partitioned by stream — a delta row must
        show the SAME assignee the mainline dashboard shows for the
        same test (docs/STREAMS_PLAN.md §3.4)."""
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED)
        rows = self.store.compare_category(self.stream_id, "new_failures")
        self.assertEqual(rows[0].assignee, "alice")

    def test_assignee_is_none_when_unassigned(self) -> None:
        rows = self.store.compare_category(self.stream_id, "new_failures")
        self.assertIsNone(rows[0].assignee)

    def test_pagination_is_exact(self) -> None:
        total = self.store.compare_category_count(
            self.stream_id, "new_failures")
        self.assertEqual(total, 1)
        page = self.store.compare_category(
            self.stream_id, "new_failures", limit=0, offset=0)
        self.assertEqual(page, [])

    def test_a_retired_test_is_excluded_from_every_category(self) -> None:
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "amy", "gone", BASE)
        counts = self.store.compare_counts(self.stream_id)
        self.assertEqual(counts.new_failures, 0)

    def test_an_unknown_stream_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.compare_counts(999999)

    def test_an_unknown_category_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.compare_category(self.stream_id, "not_a_category")

    def test_comparison_is_scoped_to_the_streams_own_product(self) -> None:
        """An environment of a DIFFERENT product must never show up as a
        spurious no_result/new_tests row."""
        self.store.set_environment_product(
            "other-product-env", "Zephyr", "alice", CREATED)
        self.store.upsert_runs([make_record(
            environment="other-product-env", test_name="unrelated")])
        counts = self.store.compare_counts(self.stream_id)
        self.assertEqual(counts, storage.CompareCounts(
            new_failures=1, new_passes=1, both_failing=1, new_tests=1,
            no_result=1, agree=0,
        ))


class CompareEnvironmentFilterTest(StorageTestBase):
    """WP-33: a comparison narrowed to ONE of the product's environments.

    Two environments of one product, deliberately given DIFFERENT
    outcomes in every category, so a filter that leaked the other
    environment's rows (or dropped its own) moves a count."""

    def setUp(self) -> None:
        super().setUp()
        for environment in ("linux-sim", "win-sim"):
            self.store.set_environment_product(
                environment, "Atlas", "alice", CREATED)
        branch_start = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.PASS),
            make_record(test_name="test_c", result=Result.PASS),
            make_record(environment="win-sim", test_name="test_a",
                        result=Result.FAIL),
            make_record(environment="win-sim", test_name="test_b",
                        result=Result.FAIL),
            make_record(environment="win-sim", test_name="test_e",
                        result=Result.PASS),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/x", start=branch_start),
            make_record(environment="win-sim", test_name="test_a",
                        result=Result.FAIL, build="feat/x",
                        start=branch_start),
            make_record(environment="win-sim", test_name="test_b",
                        result=Result.PASS, build="feat/x",
                        start=branch_start),
            make_record(environment="win-sim", test_name="test_d",
                        result=Result.PASS, build="feat/x",
                        start=branch_start),
            make_record(environment="win-sim", test_name="test_e",
                        result=Result.PASS, build="feat/x",
                        start=branch_start),
        ])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def test_no_filter_spans_both_environments(self) -> None:
        self.assertEqual(
            self.store.compare_counts(self.stream_id),
            storage.CompareCounts(
                new_failures=1,   # linux-sim test_a
                new_passes=1,     # win-sim test_b
                both_failing=1,   # win-sim test_a
                new_tests=1,      # win-sim test_d
                no_result=1,      # linux-sim test_c
                agree=1,          # win-sim test_e
            ))

    def test_counts_are_narrowed_to_the_named_environment(self) -> None:
        self.assertEqual(
            self.store.compare_counts(
                self.stream_id, environment="linux-sim"),
            storage.CompareCounts(
                new_failures=1, new_passes=0, both_failing=0,
                new_tests=0, no_result=1, agree=0))
        self.assertEqual(
            self.store.compare_counts(
                self.stream_id, environment="win-sim"),
            storage.CompareCounts(
                new_failures=0, new_passes=1, both_failing=1,
                new_tests=1, no_result=0, agree=1))

    def test_the_two_environments_sum_to_the_unfiltered_counts(
            self) -> None:
        """The filter partitions the comparison: nothing counted twice,
        nothing dropped. Stated as a sum so it keeps holding when the
        fixture above changes."""
        whole = self.store.compare_counts(self.stream_id)
        parts = [
            self.store.compare_counts(self.stream_id, environment=name)
            for name in ("linux-sim", "win-sim")
        ]
        for field in storage.CompareCounts._fields:
            self.assertEqual(
                sum(getattr(part, field) for part in parts),
                getattr(whole, field), field)

    def test_rows_are_narrowed_on_both_halves_of_the_join(self) -> None:
        """both_failing comes from the stream-anchored half of the
        pairs SQL and no_result from the baseline-anchored half -- the
        filter is applied to each separately, so each needs its own
        check."""
        rows = self.store.compare_category(
            self.stream_id, "both_failing", environment="linux-sim")
        self.assertEqual(rows, [])
        rows = self.store.compare_category(
            self.stream_id, "both_failing", environment="win-sim")
        self.assertEqual(
            [(row.environment, row.test_name) for row in rows],
            [("win-sim", "test_a")])
        rows = self.store.compare_category(
            self.stream_id, "no_result", environment="win-sim")
        self.assertEqual(rows, [])
        rows = self.store.compare_category(
            self.stream_id, "no_result", environment="linux-sim")
        self.assertEqual(
            [(row.environment, row.test_name) for row in rows],
            [("linux-sim", "test_c")])

    def test_the_category_total_agrees_with_the_counts(self) -> None:
        for environment in (None, "linux-sim", "win-sim"):
            counts = self.store.compare_counts(
                self.stream_id, environment=environment)
            for category in storage.COMPARE_CATEGORIES:
                self.assertEqual(
                    self.store.compare_category_count(
                        self.stream_id, category,
                        environment=environment),
                    getattr(counts, category),
                    (environment, category))

    def test_an_environment_outside_the_product_matches_nothing(
            self) -> None:
        """Narrowing, never widening: naming another product's
        environment must not pull its rows into this comparison."""
        self.store.set_environment_product(
            "other-product-env", "Zephyr", "alice", CREATED)
        self.store.upsert_runs([make_record(
            environment="other-product-env", test_name="unrelated")])
        for environment in ("other-product-env", "no-such-environment"):
            self.assertEqual(
                self.store.compare_counts(
                    self.stream_id, environment=environment),
                storage.CompareCounts(
                    new_failures=0, new_passes=0, both_failing=0,
                    new_tests=0, no_result=0, agree=0),
                environment)
            for category in storage.COMPARE_CATEGORIES:
                self.assertEqual(
                    self.store.compare_category(
                        self.stream_id, category,
                        environment=environment),
                    [], (environment, category))

    def test_the_filter_applies_to_a_non_mainline_baseline_too(
            self) -> None:
        self.store.upsert_runs([
            make_record(environment="win-sim", test_name="test_a",
                        result=Result.PASS, build="feat/w",
                        start=BASE + datetime.timedelta(hours=2)),
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/w",
                        start=BASE + datetime.timedelta(hours=2)),
        ])
        other_id = [
            stream.stream_id for stream in self.store.list_streams("Atlas")
            if stream.name == "feat/w"][0]
        counts = self.store.compare_counts(
            self.stream_id, baseline_id=other_id, environment="win-sim")
        # On win-sim, feat/x FAILs test_a where feat/w PASSes it; the
        # other three win-sim tests on feat/x have no counterpart.
        self.assertEqual(counts.new_failures, 1)
        self.assertEqual(counts.new_tests, 3)
        self.assertEqual(counts.both_failing, 0)


class BuildCommentTest(StorageTestBase):
    """WP-35: a comment made from a build's page is recorded against
    that build however it is made, and the build's comparison shows it.

    Before this, only the test page's own comment box recorded where a
    comment came from. The note typed while bulk-assigning from a
    build's page was written with no origin at all.
    """

    LATER = CREATED + datetime.timedelta(minutes=5)

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        start = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/x", start=start),
            make_record(test_name="test_b", result=Result.FAIL,
                        build="feat/x", start=start),
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/y",
                        start=start + datetime.timedelta(seconds=1)),
        ])
        by_name = {
            stream.name: stream.stream_id
            for stream in self.store.list_streams("Atlas")}
        self.stream_id = by_name["feat/x"]
        self.other_id = by_name["feat/y"]

    def _row(self, test_name: str) -> storage.CompareRow:
        rows = self.store.compare_category(self.stream_id, "new_failures")
        return [row for row in rows if row.test_name == test_name][0]

    def _thread(self, test_name: str) -> List[Tuple[str, Optional[int]]]:
        return [
            (comment.text, comment.stream_id) for comment in
            self.store.comments("linux-sim", "suite.py", test_name)]

    # -- what the comparison shows ------------------------------------

    def test_no_comment_is_none(self) -> None:
        self.assertIsNone(self._row("test_a").stream_comment)

    def test_a_comment_posted_from_the_build_is_shown(self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "API changed",
            CREATED, stream_id=self.stream_id)
        self.assertEqual(
            self._row("test_a").stream_comment,
            storage.LatestComment(
                author="amy", created_at=CREATED, text="API changed"))
        self.assertIsNone(self._row("test_b").stream_comment)

    def test_the_newest_from_the_build_wins(self) -> None:
        for text, at in (("first", CREATED), ("second", self.LATER)):
            self.store.add_comment(
                "linux-sim", "suite.py", "test_a", "amy", text, at,
                stream_id=self.stream_id)
        self.assertEqual(self._row("test_a").stream_comment.text, "second")

    def test_a_comment_from_elsewhere_is_never_shown(self) -> None:
        """Newer than the build's own, and still not the build's."""
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "on this build",
            CREATED, stream_id=self.stream_id)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "bob", "on mainline",
            self.LATER)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "cat", "on the other",
            self.LATER, stream_id=self.other_id)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_b", "bob", "on mainline",
            self.LATER)
        self.assertEqual(
            self._row("test_a").stream_comment.text, "on this build")
        self.assertIsNone(self._row("test_b").stream_comment)

    def test_each_build_sees_its_own(self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "x says", CREATED,
            stream_id=self.stream_id)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "y says", CREATED,
            stream_id=self.other_id)
        other = self.store.compare_category(self.other_id, "new_failures")
        self.assertEqual(
            [(row.test_name, row.stream_comment.text) for row in other],
            [("test_a", "y says")])

    def test_the_filtered_page_carries_it_too(self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_b", "amy", "rig fault",
            CREATED, stream_id=self.stream_id)
        rows = self.store.compare_category(
            self.stream_id, "new_failures", environment="linux-sim",
            limit=1, offset=1)
        self.assertEqual(
            [(row.test_name, row.stream_comment.text) for row in rows],
            [("test_b", "rig fault")])

    # -- the bulk-assign note -----------------------------------------

    def test_a_bulk_assign_note_is_posted_from_the_rows_origin(
            self) -> None:
        self.store.bulk_set_assignee_for_triples(
            "alice", "amy", CREATED, [
                ("linux-sim", "suite.py", "test_a", self.stream_id),
                ("linux-sim", "suite.py", "test_b", None),
            ], comment_text="same cause")
        self.assertEqual(
            self._thread("test_a"), [("same cause", self.stream_id)])
        self.assertEqual(self._thread("test_b"), [("same cause", None)])
        self.assertEqual(
            self._row("test_a").stream_comment.text, "same cause")
        self.assertIsNone(self._row("test_b").stream_comment)

    def test_a_filter_mode_note_still_carries_no_origin(self) -> None:
        """Open Actions' "everything the filters match" is never made
        from a build's page; that has not changed."""
        self.store.bulk_set_assignee(
            "alice", "amy", CREATED, comment_text="sweep",
            environment="linux-sim")
        self.assertEqual(self._thread("test_a"), [("sweep", None)])

    # -- the bulk comment ---------------------------------------------

    def test_a_bulk_comment_lands_on_each_test_with_its_origin(
            self) -> None:
        result = self.store.bulk_add_comment(
            "amy", "flag off on this branch", CREATED, [
                ("linux-sim", "suite.py", "test_a", self.stream_id),
                ("linux-sim", "suite.py", "test_b", self.stream_id),
            ])
        self.assertEqual(result, (2, 0))
        for name in ("test_a", "test_b"):
            self.assertEqual(
                self._thread(name),
                [("flag off on this branch", self.stream_id)])
            comment = self._row(name).stream_comment
            self.assertEqual(comment.author, "amy")
            self.assertEqual(comment.created_at, CREATED)

    def test_a_bulk_comment_changes_no_assignment(self) -> None:
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED)
        self.store.bulk_add_comment(
            "amy", "noted", self.LATER, [
                ("linux-sim", "suite.py", "test_a", self.stream_id),
                ("linux-sim", "suite.py", "test_b", self.stream_id),
            ])
        self.assertEqual(
            self.store.current_assignee(
                "linux-sim", "suite.py", "test_a"), "alice")
        self.assertIsNone(self.store.current_assignee(
            "linux-sim", "suite.py", "test_b"))
        self.assertEqual(int(self.store._conn().execute(
            "SELECT COUNT(*) FROM assignments").fetchone()[0]), 1)

    def test_a_test_that_no_longer_exists_is_counted_and_skipped(
            self) -> None:
        result = self.store.bulk_add_comment(
            "amy", "noted", CREATED, [
                ("linux-sim", "suite.py", "test_a", self.stream_id),
                ("linux-sim", "suite.py", "gone", self.stream_id),
            ])
        self.assertEqual(result, (1, 1))
        self.assertEqual(int(self.store._conn().execute(
            "SELECT COUNT(*) FROM comments").fetchone()[0]), 1)

    def test_a_test_named_twice_gets_one_comment(self) -> None:
        result = self.store.bulk_add_comment(
            "amy", "noted", CREATED, [
                ("linux-sim", "suite.py", "test_a", self.stream_id),
                ("linux-sim", "suite.py", "test_a", self.stream_id),
            ])
        self.assertEqual(result, (1, 0))
        self.assertEqual(len(self._thread("test_a")), 1)

    def test_nothing_selected_writes_nothing(self) -> None:
        self.assertEqual(
            self.store.bulk_add_comment("amy", "noted", CREATED, []),
            (0, 0))
        self.assertIsNone(self.store.get_user("amy"))

    def test_nothing_found_creates_no_user(self) -> None:
        result = self.store.bulk_add_comment(
            "newcomer", "noted", CREATED,
            [("linux-sim", "suite.py", "gone", None)])
        self.assertEqual(result, (0, 1))
        self.assertIsNone(self.store.get_user("newcomer"))

    def test_the_author_becomes_a_user(self) -> None:
        self.store.bulk_add_comment(
            "newcomer", "noted", CREATED,
            [("linux-sim", "suite.py", "test_a", None)])
        self.assertIsNotNone(self.store.get_user("newcomer"))

    def test_more_tests_than_one_lookup_holds(self) -> None:
        count = storage._RECENT_CHUNK * 2 + 1
        start = BASE + datetime.timedelta(hours=2)
        self.store.upsert_runs([
            make_record(test_name="bulk_{}".format(index),
                        build="feat/x", start=start)
            for index in range(count)
        ])
        result = self.store.bulk_add_comment(
            "amy", "noted", CREATED, [
                ("linux-sim", "suite.py", "bulk_{}".format(index),
                 self.stream_id)
                for index in range(count)
            ])
        self.assertEqual(result, (count, 0))
        self.assertEqual(int(self.store._conn().execute(
            "SELECT COUNT(*) FROM comments WHERE stream_id = ?",
            (self.stream_id,)).fetchone()[0]), count)


class CompareCountsManyTest(StorageTestBase):
    """compare_counts_many: the Watchlist s: cards' batched path.

    Cross-checks against compare_counts (the single-stream SQL path) so
    the Python classification here cannot silently drift from it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.set_environment_product(
            "win-sim", "Borealis", "alice", CREATED)
        branch_start = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(environment="linux-sim", test_name="test_a",
                        result=Result.PASS),
            make_record(environment="linux-sim", test_name="test_b",
                        result=Result.FAIL),
            make_record(environment="win-sim", test_name="test_x",
                        result=Result.PASS),
        ])
        self.store.upsert_runs([
            make_record(environment="linux-sim", test_name="test_a",
                        result=Result.FAIL, build="feat/x",
                        start=branch_start),
            make_record(environment="linux-sim", test_name="test_c",
                        result=Result.PASS, build="feat/x",
                        start=branch_start),
        ])
        self.store.upsert_runs([
            make_record(environment="win-sim", test_name="test_x",
                        result=Result.FAIL, build="feat/y",
                        start=branch_start),
        ])
        self.stream_x = self.store.list_streams("Atlas")[0].stream_id
        self.stream_y = self.store.list_streams("Borealis")[0].stream_id

    def test_agrees_with_compare_counts_for_each_stream(self) -> None:
        many = self.store.compare_counts_many({
            self.stream_x: self.store.environments_for_product("Atlas"),
            self.stream_y: self.store.environments_for_product("Borealis"),
        })
        self.assertEqual(
            many[self.stream_x], self.store.compare_counts(self.stream_x))
        self.assertEqual(
            many[self.stream_y], self.store.compare_counts(self.stream_y))

    def test_a_stream_from_a_different_product_does_not_leak_environments(
            self) -> None:
        """test_x (win-sim) must never appear as no_result under
        stream_x (Atlas, linux-sim only), and vice versa."""
        many = self.store.compare_counts_many({
            self.stream_x: self.store.environments_for_product("Atlas"),
        })
        # Atlas has 2 mainline tests (test_a, test_b) and the branch has
        # 2 (test_a changed, test_c new): no_result=1 (test_b),
        # new_tests=1 (test_c), new_failures=1 (test_a) -- never touches
        # win-sim's test_x.
        self.assertEqual(many[self.stream_x].no_result, 1)
        self.assertEqual(many[self.stream_x].new_tests, 1)

    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(self.store.compare_counts_many({}), {})

    def test_a_stream_with_no_declared_product_environments_is_all_zero(
            self) -> None:
        many = self.store.compare_counts_many({self.stream_x: []})
        self.assertEqual(
            many[self.stream_x],
            storage.CompareCounts(0, 0, 0, 0, 0, 0),
        )

    def test_query_count_does_not_grow_with_the_number_of_streams(
            self) -> None:
        conn = self.store._conn()

        def query_count(
                stream_environments: Dict[int, Sequence[str]]) -> int:
            statements = []  # type: List[str]
            trace_sql_into(conn, statements)
            try:
                self.store.compare_counts_many(stream_environments)
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        one = query_count({
            self.stream_x: self.store.environments_for_product("Atlas"),
        })
        both = query_count({
            self.stream_x: self.store.environments_for_product("Atlas"),
            self.stream_y: self.store.environments_for_product("Borealis"),
        })
        self.assertEqual(one, both)


class CompareCountsManyBaselinesTest(StorageTestBase):
    """compare_counts_many's WP-22 *baselines* argument (docs/STREAMS_PLAN.md
    §4.1): a build compared against its predecessor build instead of
    mainline, still in ONE query."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.PASS),
            make_record(test_name="test_b", result=Result.FAIL),
        ])
        older = BASE + datetime.timedelta(hours=1)
        newer = BASE + datetime.timedelta(hours=2)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="1.0", start=older),
            make_record(test_name="test_b", result=Result.PASS,
                        build="1.0", start=older),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="1.1", start=newer),
            make_record(test_name="test_c", result=Result.PASS,
                        build="1.1", start=newer),
        ])
        builds = {s.name: s.stream_id
                  for s in self.store.list_streams("Atlas")}
        self.build_1_0 = builds["1.0"]
        self.build_1_1 = builds["1.1"]

    def test_agrees_with_compare_counts_for_a_non_mainline_baseline(
            self) -> None:
        envs = self.store.environments_for_product("Atlas")
        many = self.store.compare_counts_many(
            {self.build_1_1: envs},
            baselines={self.build_1_1: self.build_1_0},
        )
        self.assertEqual(
            many[self.build_1_1],
            self.store.compare_counts(
                self.build_1_1, baseline_id=self.build_1_0),
        )
        # 1.0: test_a FAIL, test_b PASS. 1.1: test_a FAIL, test_c PASS.
        # both_failing (test_a), no_result (test_b, absent from 1.1),
        # new_tests (test_c, absent from 1.0).
        counts = many[self.build_1_1]
        self.assertEqual(counts.both_failing, 1)
        self.assertEqual(counts.no_result, 1)
        self.assertEqual(counts.new_tests, 1)

    def test_a_stream_absent_from_baselines_still_compares_to_mainline(
            self) -> None:
        """Only streams NAMED in *baselines* change default -- everyone
        else keeps comparing to mainline, unaffected by this drop."""
        envs = self.store.environments_for_product("Atlas")
        many = self.store.compare_counts_many(
            {self.build_1_0: envs, self.build_1_1: envs},
            baselines={self.build_1_1: self.build_1_0},
        )
        self.assertEqual(
            many[self.build_1_0], self.store.compare_counts(self.build_1_0))

    def test_query_count_does_not_grow_with_a_baseline_override(
            self) -> None:
        conn = self.store._conn()
        envs = self.store.environments_for_product("Atlas")

        def query_count(baselines: Optional[Dict[int, int]]) -> int:
            statements = []  # type: List[str]
            trace_sql_into(conn, statements)
            try:
                self.store.compare_counts_many(
                    {self.build_1_1: envs}, baselines=baselines)
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        self.assertEqual(
            query_count(None),
            query_count({self.build_1_1: self.build_1_0}),
        )


class PreviousBuildsTest(StorageTestBase):
    """Storage.previous_builds: the WP-22 default comparison baseline for
    a build stream (docs/STREAMS_PLAN.md §4.1)."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.set_environment_product(
            "win-sim", "Borealis", "alice", CREATED)
        t0 = BASE
        t1 = BASE + datetime.timedelta(hours=1)
        t2 = BASE + datetime.timedelta(hours=2)
        self.store.upsert_runs([
            make_record(test_name="test_a", build="1.0", start=t0),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", build="1.1", start=t1),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", build="1.2", start=t2),
        ])
        self.store.upsert_runs([
            make_record(environment="win-sim", test_name="test_x",
                        build="9.0", start=t0),
        ])
        atlas = {s.name: s for s in self.store.list_streams("Atlas")}
        self.build_1_0 = atlas["1.0"]
        self.build_1_1 = atlas["1.1"]
        self.build_1_2 = atlas["1.2"]
        self.build_9_0 = self.store.list_streams("Borealis")[0]

    def test_the_newest_build_predecessor_is_the_one_just_before_it(
            self) -> None:
        result = self.store.previous_builds(
            [self.build_1_0, self.build_1_1, self.build_1_2])
        self.assertNotIn(self.build_1_0.stream_id, result)
        self.assertEqual(
            result[self.build_1_1.stream_id].name, "1.0")
        self.assertEqual(
            result[self.build_1_2.stream_id].name, "1.1")

    def test_a_different_products_build_never_becomes_the_predecessor(
            self) -> None:
        """Atlas's oldest build has no predecessor even though Borealis
        has an earlier one -- products never mix."""
        result = self.store.previous_builds(
            [self.build_1_0, self.build_9_0])
        self.assertNotIn(self.build_1_0.stream_id, result)
        self.assertNotIn(self.build_9_0.stream_id, result)

    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(self.store.previous_builds([]), {})

    def test_query_count_is_one_regardless_of_build_count(self) -> None:
        conn = self.store._conn()

        def query_count(streams: List["storage.Stream"]) -> int:
            statements = []  # type: List[str]
            trace_sql_into(conn, statements)
            try:
                self.store.previous_builds(streams)
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        self.assertEqual(
            query_count([self.build_1_1]),
            query_count([self.build_1_1, self.build_1_2]),
        )


class StreamResultsForTripleTest(StorageTestBase):
    """Storage.stream_results_for_triple: this triple's latest result on
    every stream that HAS one, newest first (docs/STREAMS_PLAN.md §4.1) —
    the test page's "Every build" table and its stream dropdown."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        t0 = BASE
        t1 = BASE + datetime.timedelta(hours=1)
        t2 = BASE + datetime.timedelta(hours=2)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.PASS, start=t0),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="1.0", start=t1),
        ])
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/x", start=t2),
        ])
        # A stream that exists but never ran test_a -- must never appear.
        self.store.upsert_runs([
            make_record(test_name="test_b", build="9.9", start=t0),
        ])

    def test_newest_first_across_every_stream_that_ran_it(self) -> None:
        results = self.store.stream_results_for_triple(
            "linux-sim", "suite.py", "test_a")
        self.assertEqual(
            [(r.stream.kind, r.stream.name) for r in results],
            [("build", "feat/x"), ("build", "1.0"), ("mainline", "")],
        )
        self.assertEqual(results[0].result, Result.FAIL)

    def test_a_stream_with_no_result_for_the_triple_is_absent(self) -> None:
        results = self.store.stream_results_for_triple(
            "linux-sim", "suite.py", "test_a")
        names = {r.stream.name for r in results}
        self.assertNotIn("9.9", names)

    def test_unknown_triple_returns_an_empty_list(self) -> None:
        self.assertEqual(
            self.store.stream_results_for_triple(
                "linux-sim", "suite.py", "no_such_test"),
            [],
        )


class StreamIdentitiesTest(StorageTestBase):
    """Batch stream metadata lookup for the Watchlist's s: cards."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([make_record(build="feat/x")])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def test_returns_the_requested_streams(self) -> None:
        result = self.store.stream_identities([self.stream_id])
        self.assertEqual(set(result), {self.stream_id})
        self.assertEqual(result[self.stream_id].kind, "build")
        self.assertEqual(result[self.stream_id].name, "feat/x")

    def test_unknown_ids_are_simply_absent(self) -> None:
        result = self.store.stream_identities([self.stream_id, 999999])
        self.assertEqual(set(result), {self.stream_id})

    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(self.store.stream_identities([]), {})

    def test_query_count_is_one_regardless_of_id_count(self) -> None:
        second_id = self.store._find_or_create_stream(
            self.store._conn(), "Atlas", "build", "rc1",
            "2026-07-01T00:00:00.000000")
        conn = self.store._conn()

        def query_count(ids: List[int]) -> int:
            statements = []  # type: List[str]
            trace_sql_into(conn, statements)
            try:
                self.store.stream_identities(ids)
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        self.assertEqual(
            query_count([self.stream_id]),
            query_count([self.stream_id, second_id]))


class EnvironmentsForStreamTest(StorageTestBase):
    """Storage.environments_for_stream (docs/ONE_KIND_PLAN.md §2b.1,
    WP-25): every environment a stream has at least one run on, sourced
    from its own latest_runs partition -- what the Time/Timeline empty
    state uses to say WHERE a stream's data actually is, rather than
    showing a bare empty page on an environment it never touched."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.set_environment_product(
            "win-sim", "Atlas", "alice", CREATED)

    def test_lists_every_environment_the_stream_ran_on(self) -> None:
        self.store.upsert_runs([
            make_record(environment="win-sim", build="feat/x"),
            make_record(environment="linux-sim", test_name="test_b",
                        build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        stream_id = self.store.list_streams("Atlas")[0].stream_id
        self.assertEqual(
            self.store.environments_for_stream(stream_id),
            ["linux-sim", "win-sim"])

    def test_an_environment_the_stream_never_ran_on_is_absent(
            self) -> None:
        self.store.upsert_runs([
            make_record(environment="win-sim", build="feat/x")])
        stream_id = self.store.list_streams("Atlas")[0].stream_id
        self.assertEqual(
            self.store.environments_for_stream(stream_id), ["win-sim"])
        self.assertNotIn(
            "linux-sim", self.store.environments_for_stream(stream_id))

    def test_mainline_is_scoped_the_same_way(self) -> None:
        """No special case: mainline is just stream id 1."""
        self.store.upsert_runs([make_record(environment="linux-sim")])
        self.assertEqual(
            self.store.environments_for_stream(storage.MAINLINE_STREAM_ID),
            ["linux-sim"])

    def test_an_unknown_stream_id_is_an_empty_list(self) -> None:
        self.assertEqual(self.store.environments_for_stream(999999), [])


class _StreamEnvironmentFixture(StorageTestBase):
    """Two builds and mainline across two environments, with history.

    Every partition the delete must leave alone has rows of its own in
    the SAME hours as the one it removes: mainline on the same
    environment, the same build on the other environment, and a second
    build on the same environment. A delete keyed on too little takes
    one of them with it.
    """

    HOUR = datetime.timedelta(hours=1)

    def setUp(self) -> None:
        super().setUp()
        for environment in ("linux-sim", "win-sim"):
            self.store.set_environment_product(
                environment, "Atlas", "alice", CREATED)
        records = []
        for environment in ("linux-sim", "win-sim"):
            for offset in (0, 1, 26):
                start = BASE + offset * self.HOUR
                for name in ("test_a", "test_b"):
                    records.append(make_record(
                        environment=environment, test_name=name,
                        start=start))
                    # One second later: the runs table's UNIQUE does
                    # not include the stream, so the builds cannot
                    # share mainline's start times.
                    records.append(make_record(
                        environment=environment, test_name=name,
                        build="feat/x",
                        result=Result.FAIL if name == "test_b"
                        else Result.PASS,
                        start=start + datetime.timedelta(seconds=1)))
        for offset in (0, 1):
            records.append(make_record(
                test_name="test_a", build="feat/y",
                start=BASE + offset * self.HOUR
                + datetime.timedelta(seconds=2)))
        self.store.upsert_runs(records)
        by_name = {
            stream.name: stream.stream_id
            for stream in self.store.list_streams("Atlas")}
        self.stream_id = by_name["feat/x"]
        self.other_id = by_name["feat/y"]

    def _count(self, table: str, stream_id: int,
               environment: str) -> int:
        return int(self.store._conn().execute(
            "SELECT COUNT(*) FROM {} WHERE stream_id = ? "
            "AND environment = ?".format(table),
            (stream_id, environment)).fetchone()[0])

    def _outputs(self, stream_id: int, environment: str) -> int:
        return int(self.store._conn().execute(
            "SELECT COUNT(*) FROM run_outputs o JOIN runs r "
            "ON r.id = o.run_id WHERE r.stream_id = ? "
            "AND r.environment = ?",
            (stream_id, environment)).fetchone()[0])

    def _snapshot(self) -> Dict[Tuple[int, str, str], int]:
        """Row counts for every (stream, environment, table)."""
        counts = {}  # type: Dict[Tuple[int, str, str], int]
        for stream_id in (storage.MAINLINE_STREAM_ID, self.stream_id,
                          self.other_id):
            for environment in ("linux-sim", "win-sim"):
                for table in ("runs", "latest_runs", "activity_hours",
                              "script_hours"):
                    counts[(stream_id, environment, table)] = self._count(
                        table, stream_id, environment)
                counts[(stream_id, environment, "run_outputs")] = (
                    self._outputs(stream_id, environment))
        return counts


class StreamEnvironmentsTest(_StreamEnvironmentFixture):
    """Storage.stream_environments — what a person is shown before
    deleting (WP-34)."""

    def test_reports_each_environment_of_the_build(self) -> None:
        self.assertEqual(
            self.store.stream_environments(self.stream_id),
            [
                storage.StreamEnvironment(
                    environment="linux-sim", tests=2, runs=6,
                    last_run=BASE + 26 * self.HOUR
                    + datetime.timedelta(seconds=1)),
                storage.StreamEnvironment(
                    environment="win-sim", tests=2, runs=6,
                    last_run=BASE + 26 * self.HOUR
                    + datetime.timedelta(seconds=1)),
            ])

    def test_the_run_count_is_what_the_delete_removes(self) -> None:
        before = self.store.stream_environments(self.stream_id)
        deleted = self.store.delete_stream_environment(
            self.stream_id, "win-sim")
        self.assertEqual(deleted["runs"], before[1].runs)
        self.assertEqual(deleted["latest_runs"], before[1].tests)

    def test_a_build_with_nothing_is_an_empty_list(self) -> None:
        self.assertEqual(self.store.stream_environments(999999), [])

    def test_it_never_reads_the_runs_table(self) -> None:
        """`runs` has no index leading with stream_id; a count there
        walks the environment's whole history. Asserted on the text of
        the statements issued, which is the same on both backends."""
        seen = []  # type: List[str]
        real_conn = self.store._conn()

        class Recorder(object):
            def execute(self, sql: str, *args: Any) -> Any:
                seen.append(sql)
                return real_conn.execute(sql, *args)

        self.store._conn = lambda: Recorder()  # type: ignore
        try:
            self.store.stream_environments(self.stream_id)
        finally:
            del self.store._conn
        self.assertEqual(len(seen), 2)
        for sql in seen:
            self.assertNotIn(
                " runs ", sql.replace("latest_runs", "") + " ", sql)


class DeleteStreamEnvironmentTest(_StreamEnvironmentFixture):
    """Storage.delete_stream_environment (WP-34): one build's results
    for one environment, and nothing else."""

    def test_refuses_mainline(self) -> None:
        with self.assertRaises(ValueError):
            self.store.delete_stream_environment(
                storage.MAINLINE_STREAM_ID, "linux-sim")
        self.assertEqual(
            self._count("runs", storage.MAINLINE_STREAM_ID, "linux-sim"),
            6)

    def test_removes_exactly_that_partition(self) -> None:
        before = self._snapshot()
        deleted = self.store.delete_stream_environment(
            self.stream_id, "win-sim")
        after = self._snapshot()
        for key in sorted(before):
            stream_id, environment, table = key
            if (stream_id, environment) == (self.stream_id, "win-sim"):
                self.assertEqual(after[key], 0, key)
                self.assertEqual(deleted[table], before[key], key)
                self.assertGreater(before[key], 0, key)
            else:
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(deleted["streams"], 0)

    def test_the_derived_tables_still_equal_a_group_by_over_runs(
            self) -> None:
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        conn = self.store._conn()
        pairs = [
            ("SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
             "result, COUNT(*) FROM runs GROUP BY 1, 2, 3, 4",
             "SELECT stream_id, environment, hour, result, count "
             "FROM activity_hours"),
            ("SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
             "script, result, COUNT(*), MIN(start_time), MAX(end_time) "
             "FROM runs GROUP BY 1, 2, 3, 4, 5",
             "SELECT stream_id, environment, hour, script, result, "
             "count, first_start, last_end FROM script_hours"),
        ]
        for grouped, table in pairs:
            for left, right in ((grouped, table), (table, grouped)):
                self.assertEqual(int(conn.execute(
                    "SELECT COUNT(*) FROM ({0} EXCEPT {1}) AS t".format(
                        left, right)).fetchone()[0]), 0, table)

    def test_the_surviving_build_reads_normally(self) -> None:
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertEqual(
            self.store.environments_for_stream(self.stream_id),
            ["linux-sim"])
        self.assertIsNone(self.store.latest_run(
            "win-sim", "suite.py", "test_a", stream_id=self.stream_id))
        kept = self.store.latest_run(
            "linux-sim", "suite.py", "test_b", stream_id=self.stream_id)
        self.assertEqual(kept.result, Result.FAIL)
        self.assertEqual(
            self.store.compare_counts(
                self.stream_id, environment="win-sim").no_result, 2)
        self.assertEqual(self.store.get_stream(self.stream_id).failing, 1)

    def test_the_builds_clock_is_rederived_from_what_remains(
            self) -> None:
        """A later upload to the deleted environment must not go on
        being quoted as when the build "last ran"."""
        late = BASE + 50 * self.HOUR
        early = BASE - 50 * self.HOUR
        self.store.upsert_runs([
            make_record(environment="win-sim", test_name="test_a",
                        build="feat/x", start=late),
            make_record(environment="win-sim", test_name="test_a",
                        build="feat/x", start=early),
        ])
        widened = self.store.get_stream(self.stream_id)
        self.assertEqual(widened.first_seen, early)
        self.assertEqual(widened.last_seen, late)
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        settled = self.store.get_stream(self.stream_id)
        self.assertEqual(
            settled.first_seen, BASE + datetime.timedelta(seconds=1))
        self.assertEqual(
            settled.last_seen,
            BASE + 26 * self.HOUR + datetime.timedelta(seconds=1))

    def test_the_last_environment_takes_the_build_with_it(self) -> None:
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertIsNotNone(self.store.get_stream(self.stream_id))
        deleted = self.store.delete_stream_environment(
            self.stream_id, "linux-sim")
        self.assertEqual(deleted["streams"], 1)
        self.assertIsNone(self.store.get_stream(self.stream_id))
        self.assertEqual(
            [stream.name for stream in self.store.list_streams("Atlas")],
            ["feat/y"])

    def test_tags_are_kept_while_the_build_survives(self) -> None:
        self.store.add_comment(
            "win-sim", "suite.py", "test_b", "amy", "broken rig", CREATED,
            stream_id=self.stream_id)
        self.store.set_assignee(
            "win-sim", "suite.py", "test_b", "alice", "amy", CREATED,
            stream_id=self.stream_id)
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        comments = self.store.comments("win-sim", "suite.py", "test_b")
        self.assertEqual(
            [(c.text, c.stream_id) for c in comments],
            [("broken rig", self.stream_id)])
        self.assertEqual(
            self.store.assignments_referencing_stream(self.stream_id), 1)
        self.assertEqual(
            self.store.current_assignee("win-sim", "suite.py", "test_b"),
            "alice")

    def test_tags_are_cleared_when_the_build_goes(self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "one-off", CREATED,
            stream_id=self.other_id)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "amy", CREATED,
            stream_id=self.other_id)
        deleted = self.store.delete_stream_environment(
            self.other_id, "linux-sim")
        self.assertEqual(deleted["streams"], 1)
        comments = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual(
            [(c.text, c.stream_id) for c in comments],
            [("one-off", None)])
        self.assertEqual(
            self.store.current_assignee(
                "linux-sim", "suite.py", "test_a"), "alice")
        self.assertEqual(int(self.store._conn().execute(
            "SELECT COUNT(*) FROM assignments WHERE stream_id = ?",
            (self.other_id,)).fetchone()[0]), 0)

    def test_an_environment_the_build_never_ran_on_deletes_nothing(
            self) -> None:
        before = self._snapshot()
        deleted = self.store.delete_stream_environment(
            self.other_id, "win-sim")
        self.assertEqual(sorted(set(deleted.values())), [0])
        self.assertEqual(self._snapshot(), before)
        self.assertIsNotNone(self.store.get_stream(self.other_id))

    def test_more_runs_than_one_statement_may_bind(self) -> None:
        """The ids are deleted in chunks; the boundary is where a chunk
        is dropped or sent twice."""
        chunk = storage.Storage._DELETE_CHUNK
        records = [
            make_record(
                environment="win-sim", test_name="bulk_{}".format(index),
                build="feat/x",
                start=BASE + datetime.timedelta(seconds=10 + index))
            for index in range(chunk * 2 + 1)
        ]
        self.store.upsert_runs(records)
        deleted = self.store.delete_stream_environment(
            self.stream_id, "win-sim")
        self.assertEqual(deleted["runs"], 6 + chunk * 2 + 1)
        self.assertEqual(deleted["run_outputs"], 6 + chunk * 2 + 1)
        self.assertEqual(self._count("runs", self.stream_id, "win-sim"), 0)
        self.assertEqual(
            self._count("runs", storage.MAINLINE_STREAM_ID, "win-sim"), 6)

    def test_the_same_records_can_be_imported_again(self) -> None:
        """Nothing here blocks a re-import -- stated in the docstring,
        the operator note and the confirmation text, and pinned here so
        that it stays a decision rather than becoming an accident."""
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        counts = self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_a", build="feat/x",
            start=BASE + datetime.timedelta(seconds=1))])
        self.assertEqual(counts.inserted, 1)
        self.assertEqual(
            self.store.environments_for_stream(self.stream_id),
            ["linux-sim", "win-sim"])

    def test_a_disagreeing_hour_count_deletes_nothing(self) -> None:
        conn = self.store._conn()
        conn.execute(
            "UPDATE activity_hours SET count = count + 1 "
            "WHERE stream_id = ? AND environment = ? AND hour = ? "
            "AND result = ?",
            (self.stream_id, "win-sim",
             model.format_iso(BASE)[:13], Result.PASS.value))
        before = self._snapshot()
        with self.assertRaises(storage.DerivedTablesDisagree) as raised:
            self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertIn("activity_hours", str(raised.exception))
        self.assertEqual(self._snapshot(), before)
        self.assertIsNotNone(self.store.get_stream(self.stream_id))

    def test_a_reference_from_another_partition_deletes_nothing(
            self) -> None:
        """Foreign keys are not enforced during the delete, so the one
        thing they would have caught is caught by hand: a latest_runs
        row ELSEWHERE pointing at a run about to go."""
        conn = self.store._conn()
        victim = self.store.latest_run(
            "win-sim", "suite.py", "test_a", stream_id=self.stream_id)
        conn.execute(
            "UPDATE latest_runs SET run_id = ? WHERE stream_id = ? "
            "AND environment = ? AND test_name = ?",
            (victim.run_id, self.other_id, "linux-sim", "test_a"))
        before = self._snapshot()
        with self.assertRaises(storage.DerivedTablesDisagree) as raised:
            self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertIn("outside", str(raised.exception))
        self.assertEqual(self._snapshot(), before)

    def test_foreign_keys_are_enforced_again_afterwards(self) -> None:
        """Both ways out: a delete that commits and one that raises."""
        if not self.store._backend.runs_migrations:
            self.skipTest("MariaDB's schema declares no foreign keys")
        conn = self.store._conn()
        self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertEqual(
            int(conn.execute("PRAGMA foreign_keys").fetchone()[0]), 1)
        conn.execute(
            "DELETE FROM activity_hours WHERE stream_id = ? "
            "AND environment = ?", (self.stream_id, "linux-sim"))
        with self.assertRaises(storage.DerivedTablesDisagree):
            self.store.delete_stream_environment(
                self.stream_id, "linux-sim")
        self.assertEqual(
            int(conn.execute("PRAGMA foreign_keys").fetchone()[0]), 1)

    def test_an_unaccounted_latest_run_deletes_nothing(self) -> None:
        """The other direction: runs the hour table does not know
        about. Its counts agree hour by hour with what it records, so
        only the latest_runs cross-check can see this."""
        conn = self.store._conn()
        conn.execute(
            "DELETE FROM activity_hours WHERE stream_id = ? "
            "AND environment = ? AND hour = ?",
            (self.stream_id, "win-sim",
             model.format_iso(BASE + 26 * self.HOUR)[:13]))
        before = self._snapshot()
        with self.assertRaises(storage.DerivedTablesDisagree) as raised:
            self.store.delete_stream_environment(self.stream_id, "win-sim")
        self.assertIn("latest_runs", str(raised.exception))
        self.assertEqual(self._snapshot(), before)


class DeleteStreamEnvironmentQueryPlanTest(_StreamEnvironmentFixture):
    """The delete may not walk an environment's history (WP-34).

    `runs` has two indexes: the frozen UNIQUE from migration 1, which
    leads with environment, and idx_runs_start_time_result. Found
    through the first, one build's runs cost every run the environment
    has recorded in a year. The statements are captured from the real
    method, then planned — so a rewrite that changes how the runs are
    found is what gets tested, not a copy of today's SQL.
    """

    def _runs_statements(self) -> List[Tuple[str, Any]]:
        seen = []  # type: List[Tuple[str, Any]]
        real_conn = self.store._conn()

        class Recorder(object):
            def execute(self, sql: str, *args: Any) -> Any:
                if "runs" in sql.replace("latest_runs", ""):
                    seen.append((sql, args[0] if args else ()))
                return real_conn.execute(sql, *args)

        self.store._conn = lambda: Recorder()  # type: ignore
        try:
            self.store.delete_stream_environment(self.stream_id, "win-sim")
        finally:
            del self.store._conn
        return seen

    def test_every_statement_touching_runs_is_a_bounded_search(
            self) -> None:
        statements = self._runs_statements()
        self.assertGreater(len(statements), 0)
        conn = self.store._conn()
        for sql, params in statements:
            plan = " | ".join(
                str(row[-1]) for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, params).fetchall())
            # The line for `runs` itself. A DELETE's plan also lists
            # the foreign-key lookups SQLite would make into the child
            # tables -- those are what suspend_foreign_keys() exists
            # for, and are measured, not planned.
            lines = [
                str(row[-1]).upper() for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, params).fetchall()]
            own = [
                line for line in lines
                if " RUNS " in line.replace("LATEST_RUNS", "") + " "]
            self.assertEqual(len(own), 1, sql + " -> " + plan)
            self.assertTrue(own[0].startswith("SEARCH"), own[0])
            self.assertNotIn("SQLITE_AUTOINDEX_RUNS_1", own[0])
            self.assertTrue(
                "IDX_RUNS_START_TIME_RESULT" in own[0]
                or "PRIMARY KEY" in own[0],
                sql + " -> " + plan)

    def test_a_delete_by_environment_alone_would_be_caught(self) -> None:
        """The detector can fail: the obvious statement, planned."""
        plan = " | ".join(
            str(row[-1]) for row in self.store._conn().execute(
                "EXPLAIN QUERY PLAN DELETE FROM runs WHERE stream_id = ? "
                "AND environment = ?",
                (self.stream_id, "win-sim")).fetchall()).upper()
        self.assertIn("SQLITE_AUTOINDEX_RUNS_1", plan)


class SizeReportTest(StorageTestBase):
    """Storage.size_report / memo_report on whichever backend is under
    test (WP-37). The engine-specific half — what the file weighs, what
    the server's catalogue says — comes through the backend seam, so
    this is the class that proves the MariaDB half runs at all."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
            make_record(test_name="test_a",
                        start=BASE + datetime.timedelta(days=1)),
            make_record(test_name="test_a", build="feat/x",
                        start=BASE + datetime.timedelta(days=2)),
        ])

    def test_the_counts_are_the_real_ones(self) -> None:
        report = self.store.size_report()
        conn = self.store._conn()
        self.assertEqual(report["rows"]["runs"], int(conn.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0]))
        self.assertEqual(report["rows"]["runs"], 4)
        self.assertEqual(report["rows"]["latest_runs"], 3)
        self.assertEqual(report["rows"]["streams"], 2)
        self.assertEqual(report["rows"]["environment_products"], 1)
        self.assertEqual(report["rows_not_counted"], ["run_outputs"])

    def test_the_range_and_the_streams(self) -> None:
        report = self.store.size_report()
        self.assertEqual(report["oldest_run"], model.format_iso(BASE))
        self.assertEqual(
            report["newest_run"],
            model.format_iso(BASE + datetime.timedelta(days=2)))
        self.assertEqual(
            [(s["kind"], s["name"], s["product"], s["tests"],
              s["environments"]) for s in report["streams"]],
            [("mainline", "", "", 2, 1), ("build", "feat/x", "Atlas", 1, 1)])

    def test_the_engine_describes_itself(self) -> None:
        report = self.store.size_report()
        self.assertIn(report["engine"], ("SQLite", "MariaDB"))
        self.assertEqual(
            report["engine"], self.store._backend.engine_name)
        self.assertTrue(report["version"])
        self.assertGreater(report["bytes"], 0)
        self.assertEqual(report["schema_version"], len(storage.MIGRATIONS))
        for part in report["parts"]:
            self.assertIsInstance(part["label"], str)
            self.assertGreaterEqual(part["bytes"], 0)
        for name, table in report["tables"].items():
            self.assertIsInstance(name, str)
            self.assertGreaterEqual(table["bytes"], 0)
            self.assertGreaterEqual(table["rows_estimate"], 0)
        if report["engine"] == "MariaDB":
            for name in ("runs", "run_outputs", "latest_runs"):
                self.assertIn(name, report["tables"])

    def test_it_is_plain_json(self) -> None:
        import json
        decoded = json.loads(json.dumps(self.store.size_report()))
        self.assertEqual(decoded["rows"]["runs"], 4)

    def test_an_empty_database_has_a_report_too(self) -> None:
        for environment in self.store.known_environments():
            self.store.delete_environment(environment)
        self.store._size_memo = None
        report = self.store.size_report()
        self.assertEqual(report["rows"]["runs"], 0)
        self.assertIsNone(report["oldest_run"])
        self.assertIsNone(report["newest_run"])

    def test_the_memo_report_counts(self) -> None:
        self.store.reset_memo_counts()
        self.store.summary_rollup(BASE)
        misses = self.store.memo_report()["misses"]
        self.assertGreater(misses, 0)
        self.assertEqual(self.store.memo_report()["entries"], misses)
        self.store.summary_rollup(BASE)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "seen", CREATED)
        self.assertEqual(
            self.store.memo_report(),
            {"hits": 1, "misses": misses, "clears": 1, "entries": 0})


class TargetedInvalidationTest(StorageTestBase):
    """WP-38: a push drops the memos of the streams it wrote, and no
    other. Mainline pushes through the morning and builds push well
    into the afternoon; a build's push used to put the home page back
    to its cold cost.

    Every "still served" assertion here is paired with a "and still
    right" one against a fresh computation, because a memo that
    survives a write it should not have survived is exactly the bug a
    narrowed invalidation can introduce.
    """

    CUTOFF = BASE - datetime.timedelta(hours=1)
    NOW = BASE + datetime.timedelta(hours=12)

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([
            make_record(test_name="test_a", result=Result.FAIL),
            make_record(test_name="test_b"),
            make_record(test_name="test_a", result=Result.FAIL,
                        build="feat/x",
                        start=BASE + datetime.timedelta(hours=2)),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=2)),
        ])
        self.build = self.store.list_streams("Atlas")[0].stream_id
        self.mainline = storage.MAINLINE_STREAM_ID

    def _warm(self, stream_id: int) -> Dict[str, Any]:
        """Every memoized component of one stream, and its values."""
        return {
            "rollup": self.store.summary_rollup(
                self.CUTOFF, stream_id=stream_id),
            "queue_counts": self.store.queue_counts(
                stale_before=self.CUTOFF, stream_id=stream_id),
            "last_by_env": self.store.latest_run_time_by_environment(
                stream_id),
            "test_counts": self.store.test_counts_by_environment(
                stream_id),
            "queue": self.store.status_queue(
                "new_failures", stale_before=self.CUTOFF,
                stream_id=stream_id),
            "trend": self.store.daily_result_counts(
                BASE - datetime.timedelta(days=1), stream_id=stream_id),
            # WIDENED for WP-43: the bars' assigned cells (per
            # environment) and the trend's split (whole stream) are
            # memoized components too, held to the same rules.
            "assigned_cells": self.store.assigned_cells(
                self.NOW, stream_id=stream_id),
            "assigned_trend": self.store.daily_assigned_failures(
                BASE - datetime.timedelta(days=1), self.NOW,
                stream_id=stream_id),
            "streaks": self.store.failure_streak_bounds_many(
                [("linux-sim", "suite.py", "test_a",
                  BASE + datetime.timedelta(
                      hours=2 if stream_id != self.mainline else 0))],
                stream_id=stream_id),
        }

    def _selects(self, call: Callable[[], Any]) -> int:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return len([
            s for s in seen if s.strip().upper().startswith("SELECT")])

    def _push(self, build: Optional[str], test_name: str = "test_new",
              result: Result = Result.FAIL) -> None:
        counts = self.store.upsert_runs([make_record(
            test_name=test_name, result=result, build=build,
            start=BASE + datetime.timedelta(hours=5))])
        self.assertEqual(counts.inserted + counts.updated, 1)

    # -- what survives ------------------------------------------------

    def test_a_builds_push_leaves_mainlines_memos_served(self) -> None:
        self._warm(self.mainline)
        self._warm(self.build)
        self._push("feat/x")
        self.assertEqual(self._selects(lambda: self._warm(self.mainline)), 0)

    def test_a_builds_push_drops_the_builds_own(self) -> None:
        self._warm(self.build)
        self._push("feat/x")
        self.assertGreater(self._selects(lambda: self._warm(self.build)), 0)

    def test_mainlines_push_leaves_the_builds_memos_served(self) -> None:
        self._warm(self.mainline)
        self._warm(self.build)
        self._push(None)
        self.assertEqual(self._selects(lambda: self._warm(self.build)), 0)
        self.assertGreater(
            self._selects(lambda: self._warm(self.mainline)), 0)

    def test_the_catalogues_are_mainlines(self) -> None:
        """environments() and scripts() read mainline only, and are
        tagged so: a build's push leaves them, mainline's drops them."""
        self.store.environments()
        self.store.scripts(None)
        self._push("feat/x", test_name="only_on_the_build")
        self.assertEqual(self._selects(lambda: (
            self.store.environments(), self.store.scripts(None))), 0)
        self._push(None, test_name="new_on_mainline")
        self.assertGreater(self._selects(lambda: (
            self.store.environments(), self.store.scripts(None))), 0)

    # -- and what is still right --------------------------------------

    def test_what_survives_is_still_true(self) -> None:
        """The values a build's push leaves memoized are the values a
        cold computation gives -- checked cell by cell, not by
        counting queries."""
        before = self._warm(self.mainline)
        self._push("feat/x")
        served = self._warm(self.mainline)
        self.store._invalidate_summary_cache()
        self.store._invalidate_trend_cache()
        cold = self._warm(self.mainline)
        self.assertEqual(served, cold)
        self.assertEqual(served, before)

    def test_what_is_dropped_is_recomputed_right(self) -> None:
        self._warm(self.build)
        self._push("feat/x")
        fresh = self._warm(self.build)
        self.assertEqual(fresh["queue_counts"]["new_failures"], 2)
        self.assertEqual(
            fresh["queue_counts"]["new_failures"],
            self.store.status_queue_count(
                "new_failures", stale_before=self.CUTOFF,
                stream_id=self.build))
        self.assertIn(
            "test_new", [row.test_name for row in fresh["queue"]])
        self.assertEqual(
            fresh["last_by_env"]["linux-sim"],
            BASE + datetime.timedelta(hours=5))
        self.assertEqual(sum(
            cell.count for cell in fresh["rollup"]), 3)

    # -- the writes that still drop everything -----------------------

    def test_a_change_with_no_cell_to_name_drops_everything(self) -> None:
        """A source link changed and nothing else: no hour cell moved,
        so the batch cannot say which stream it wrote, and drops all."""
        self._warm(self.mainline)
        self._warm(self.build)
        counts = self.store.upsert_runs([make_record(
            test_name="test_b", build="feat/x",
            start=BASE + datetime.timedelta(hours=2),
            source_link="https://example.com/moved")])
        self.assertEqual(counts.updated, 1)
        self.assertGreater(
            self._selects(lambda: self._warm(self.mainline)), 0)

    def test_an_unchanged_push_drops_nothing(self) -> None:
        self._warm(self.mainline)
        self._warm(self.build)
        counts = self.store.upsert_runs([make_record(
            test_name="test_b", build="feat/x",
            start=BASE + datetime.timedelta(hours=2))])
        self.assertEqual(counts.unchanged, 1)
        self.assertEqual(self._selects(lambda: (
            self._warm(self.mainline), self._warm(self.build))), 0)

    def test_an_assignment_a_comment_and_a_retirement_drop_everything(
            self) -> None:
        """Each reaches every stream's components (queue counts read
        current_assignments for any stream; a retirement flags a test
        on every stream; a comment is on the row of every list)."""
        writes = [
            lambda: self.store.set_assignee(
                "linux-sim", "suite.py", "test_a", "alice", "amy",
                CREATED, stream_id=self.build),
            lambda: self.store.add_comment(
                "linux-sim", "suite.py", "test_a", "amy", "seen",
                CREATED, stream_id=self.build),
            lambda: self.store.set_retired(
                "linux-sim", "suite.py", "test_b", True, "amy", "gone",
                CREATED),
        ]
        for write in writes:
            self._warm(self.mainline)
            self._warm(self.build)
            write()
            self.assertGreater(
                self._selects(lambda: self._warm(self.mainline)), 0)
            self.assertGreater(
                self._selects(lambda: self._warm(self.build)), 0)

    def test_deleting_a_builds_environment_drops_everything(self) -> None:
        """Rare, and the build's identity itself may go: not narrowed."""
        self._warm(self.mainline)
        self.store.delete_stream_environment(self.build, "linux-sim")
        self.assertGreater(
            self._selects(lambda: self._warm(self.mainline)), 0)

    def test_every_memo_entry_names_its_stream(self) -> None:
        """A two-argument _store_summary would be tagged mainline by
        default -- wrong for anything computed from a build, and a
        wrong tag is a stale page. Every call site names one."""
        import io
        import re
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "testboard", "storage.py")
        with io.open(path, encoding="utf-8") as handle:
            source = handle.read()
        calls = re.findall(r"_store_summary\(([^)]*)\)", source)
        self.assertGreater(len(calls), 5)
        for call in calls:
            if call.startswith("self, key"):
                continue     # the definition
            parts = [part for part in call.split(",") if part.strip()]
            self.assertIn(len(parts), (3, 4), call)
            self.assertNotIn("=", parts[2], call)

    def test_a_push_into_one_environment_leaves_the_others_cells(
            self) -> None:
        """WP-38's second level: mainline's morning pushes go into one
        environment at a time. The other environments' cells are
        served; the whole-stream entries (queue counts, the assembled
        pass, the test counts) are dropped and re-derived."""
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_w", result=Result.FAIL)])
        self._warm(self.mainline)
        self._push(None)     # linux-sim
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            after = self._warm(self.mainline)
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in seen if s.strip().upper().startswith("SELECT")]
        self.assertTrue(any("'linux-sim'" in s for s in selects), selects)
        self.assertFalse(
            any("'win-sim'" in s and "environment_rollup" not in s
                and "lr.environment = " in s for s in selects), selects)
        self.assertEqual(after["queue_counts"]["new_failures"], 3)
        self.store._invalidate_summary_cache()
        self.store._invalidate_trend_cache()
        self.assertEqual(after["rollup"], self._warm(self.mainline)["rollup"])

    def test_a_push_into_one_environment_leaves_the_others_assigned_cells(
            self) -> None:
        """WP-43: the assigned cells are stored per environment, tagged
        with it, so a push into linux-sim drops linux-sim's entry and
        leaves win-sim's, and what is served afterwards is what a cold
        computation gives."""
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="test_w", result=Result.FAIL)])
        self.store.set_assignee(
            "win-sim", "suite.py", "test_w", "alice", "amy", CREATED)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_new", "alice", "amy", CREATED)
        self._warm(self.mainline)
        win = ("assigned_cells", self.mainline, "win-sim", None)
        linux = ("assigned_cells", self.mainline, "linux-sim", None)
        self.assertIsNotNone(self.store._cached_summary(win))
        self.assertIsNotNone(self.store._cached_summary(linux))
        self._push(None)     # linux-sim
        self.assertIsNotNone(self.store._cached_summary(win))
        self.assertIsNone(self.store._cached_summary(linux))
        served = self._warm(self.mainline)["assigned_cells"]
        self.assertIn(
            ("linux-sim", "suite.py", False, 1),
            [(c.environment, c.script, c.muted, c.count) for c in served])
        self.store._invalidate_summary_cache()
        self.assertEqual(served, self._warm(self.mainline)["assigned_cells"])

    def test_the_memo_report_counts_a_narrowed_clear(self) -> None:
        self.store.reset_memo_counts()
        self._warm(self.mainline)
        self._push("feat/x")
        self.assertEqual(self.store.memo_report()["clears"], 1)
        self.assertGreater(self.store.memo_report()["entries"], 0)

    # -- WP-44 item 6: Q2's answers, _cadence_runs_cache ---------------

    def _fill_cadence_runs(self) -> None:
        """test_a FAIL then FAIL on both streams, marked at cadence 3, so
        a summary read puts one Q2 answer per stream in the cache."""
        # A run is unique by (triple, start): one start per stream.
        for build, hours in ((None, 3), ("feat/x", 4)):
            self.store.upsert_runs([make_record(
                test_name="test_a", result=Result.FAIL, build=build,
                start=BASE + datetime.timedelta(hours=hours))])
        self.store.ensure_user("alice", CREATED)
        self.store.mark_flaky(
            [("linux-sim", "suite.py", "test_a", None)], 3, "why", "alice",
            CREATED)
        for stream_id in (self.mainline, self.build):
            self.store.flaky_view(stream_id, self.NOW,
                                  marks=True)
        self.assertEqual(
            sorted(key[0] for key in self.store._cadence_runs_cache),
            sorted([self.mainline, self.build]))

    def test_acts_leave_the_cadence_runs_filled(self) -> None:
        """No act moves a window: an assignment, a comment, a mute, a
        retirement and a mark all drop the summary memo and leave Q2's
        answers, so the next cold read runs Q1 and not Q2."""
        self._fill_cadence_runs()
        before = dict(self.store._cadence_runs_cache)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "amy", CREATED)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "note", CREATED)
        self.store.mute_tests(
            [("linux-sim", "suite.py", "test_a", self.mainline)], "infra",
            24, "alice", "amy", self.NOW)
        self.store.set_retired(
            "linux-sim", "suite.py", "test_b", True, "amy", "gone", CREATED)
        self.store.mark_flaky(
            [("linux-sim", "suite.py", "test_b", None)], 4, "why", "alice",
            CREATED)
        self.store.unmark_flaky(
            [("linux-sim", "suite.py", "test_b")], "alice", CREATED)
        self.assertEqual(self.store._cadence_runs_cache, before)
        # ...and the summary memo did go.
        self.assertIsNone(self.store._cached_summary(
            ("flaky_states", self.mainline, "linux-sim")))

    def test_a_push_drops_its_streams_cadence_runs_and_no_other(
            self) -> None:
        """Correctness, not hygiene: a re-pushed or late run changes the
        window under the same key (design §3c)."""
        self._fill_cadence_runs()
        self._push("feat/x")
        self.assertEqual(
            [key[0] for key in self.store._cadence_runs_cache],
            [self.mainline])
        self._push(None, test_name="test_m")
        self.assertEqual(self.store._cadence_runs_cache, {})

    def test_a_late_older_run_is_read_again(self) -> None:
        """The case the push's drop is for: a run older than the latest,
        pushed late, lands inside the window of an unchanged key; the
        state must follow it."""
        self.store.upsert_runs([
            make_record(test_name="test_c", result=Result.FAIL,
                        start=BASE + datetime.timedelta(days=day))
            for day in (1, 2)])
        self.store.ensure_user("alice", CREATED)
        self.store.mark_flaky(
            [("linux-sim", "suite.py", "test_c", None)], 3, "why", "alice",
            CREATED)
        triple = ("linux-sim", "suite.py", "test_c")
        now = BASE + datetime.timedelta(days=5)
        # Two runs, both FAIL, cadence 3: every run it has failed.
        self.assertEqual(
            self.store.flaky_view(self.mainline, now).states[triple].state,
            "failing")
        # A PASS older than both, pushed late: the latest run and its
        # previous result are unchanged, so is Q2's key; the window is
        # not.
        self.store.upsert_runs([make_record(
            test_name="test_c", result=Result.PASS,
            start=BASE + datetime.timedelta(hours=12))])
        self.assertEqual(
            self.store.flaky_view(self.mainline, now).states[triple],
            storage.FlakyState(state="flaking", streak=2, runs=None))

    def test_a_delete_drops_every_cadence_run(self) -> None:
        self._fill_cadence_runs()
        self.store.delete_stream_environment(self.build, "linux-sim")
        self.assertEqual(self.store._cadence_runs_cache, {})
        self._fill_cadence_runs_mainline_only()
        self.store.delete_environment("linux-sim")
        self.assertEqual(self.store._cadence_runs_cache, {})

    def _fill_cadence_runs_mainline_only(self) -> None:
        self.store.flaky_view(self.mainline, self.NOW)
        self.assertEqual(
            [key[0] for key in self.store._cadence_runs_cache],
            [self.mainline])

    def test_the_cadence_runs_cache_has_the_streak_memos_limits(
            self) -> None:
        """60 s like the memos, and a cap that clears in full."""
        self._fill_cadence_runs()
        key = next(iter(self.store._cadence_runs_cache))
        stored, value = self.store._cadence_runs_cache[key]
        self.store._cadence_runs_cache[key] = (
            stored - storage._TREND_CACHE_TTL_SECONDS - 1, value)
        self.store._invalidate_summary_cache()
        conn = self.store._conn()
        if isinstance(self.store._backend, storage._SqliteBackend):
            seen = []  # type: List[str]
            trace_sql_into(conn, seen)
            try:
                self.store.flaky_view(key[0], self.NOW)
            finally:
                conn.set_trace_callback(None)
            self.assertEqual(
                len([s for s in seen if "FROM runs" in s]), 1, seen)
        else:
            self.store.flaky_view(key[0], self.NOW)
        self.assertGreater(self.store._cadence_runs_cache[key][0], stored)
        original = storage._CADENCE_RUNS_CACHE_MAX_ENTRIES
        storage._CADENCE_RUNS_CACHE_MAX_ENTRIES = 1
        try:
            self.store._invalidate_summary_cache()
            self.store._cadence_runs_cache.clear()
            self.store.flaky_view(self.mainline, self.NOW)
            self.store.flaky_view(self.build, self.NOW)
            self.assertEqual(len(self.store._cadence_runs_cache), 1)
        finally:
            storage._CADENCE_RUNS_CACHE_MAX_ENTRIES = original


class DropStreamTest(StorageTestBase):
    """count_stream_rows / delete_stream — the storage half of
    tools/drop_stream.py."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([
            make_record(),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def test_refuses_to_delete_mainline(self) -> None:
        with self.assertRaises(ValueError):
            self.store.delete_stream(storage.MAINLINE_STREAM_ID)

    def test_count_matches_delete(self) -> None:
        counts = self.store.count_stream_rows(self.stream_id)
        deleted = self.store.delete_stream(self.stream_id)
        self.assertEqual(counts["runs"], deleted["runs"])
        self.assertEqual(counts["latest_runs"], deleted["latest_runs"])
        self.assertEqual(counts["run_outputs"], deleted["run_outputs"])

    def test_deletes_runs_outputs_and_the_latest_runs_partition(
            self) -> None:
        self.store.delete_stream(self.stream_id)
        conn = self.store._conn()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM runs WHERE stream_id = ?",
            (self.stream_id,)).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM latest_runs WHERE stream_id = ?",
            (self.stream_id,)).fetchone()[0], 0)
        self.assertIsNone(self.store.get_stream(self.stream_id))
        # Mainline is untouched.
        self.assertIsNotNone(
            self.store.latest_run("linux-sim", "suite.py", "test_a"))

    def test_a_comment_posted_from_the_stream_survives_with_its_tag_cleared(
            self) -> None:
        self.store.add_comment(
            "linux-sim", "suite.py", "test_b", "amy", "looks fine", BASE,
            stream_id=self.stream_id)
        self.store.delete_stream(self.stream_id)
        comments = self.store.comments("linux-sim", "suite.py", "test_b")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].text, "looks fine")
        self.assertIsNone(comments[0].stream_id)

    def test_assignments_referencing_stream_is_zero_with_none_made(
            self) -> None:
        self.assertEqual(
            self.store.assignments_referencing_stream(self.stream_id), 0)

    def test_assignments_referencing_stream_counts_current_assignments(
            self) -> None:
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_b", "alice", "bob", CREATED,
            stream_id=self.stream_id)
        self.assertEqual(
            self.store.assignments_referencing_stream(self.stream_id), 1)

    def test_the_origin_tag_clears_on_delete(self) -> None:
        """The finding :meth:`Storage.assignments_referencing_stream`
        exists to surface, and the reason it must be read BEFORE
        :meth:`Storage.delete_stream`, not after: WP-27 gave
        ``current_assignments.stream_id`` and ``assignments.stream_id``
        the same explicit-UPDATE protection ``comments.stream_id`` has
        always had, in the SAME transaction as the delete -- identical
        on both backends (this class runs against MariaDB too via
        ``tests/test_mariadb_backend.py``'s generator; there is no
        longer an exclusion entry for this test, because there is
        nothing backend-specific left to exclude). Before WP-27, SQLite
        alone cleared it, via a declared ``ON DELETE SET NULL`` foreign
        key the migrated MariaDB schema never declared (runbook §B.6),
        so the id dangled there instead -- see
        ``Storage.assignments_referencing_stream``'s docstring for that
        history. The assignment survives either way (assignments are
        never deleted by a stream drop); only its origin annotation is
        cleared."""
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_b", "alice", "bob", CREATED,
            stream_id=self.stream_id)
        before = self.store.assignments_referencing_stream(self.stream_id)
        self.assertEqual(before, 1)
        self.store.delete_stream(self.stream_id)
        after = self.store.assignments_referencing_stream(self.stream_id)
        self.assertEqual(
            after, 0,
            "delete_stream's explicit UPDATE did not clear "
            "current_assignments.stream_id -- the column is dangling, "
            "the exact bug WP-27 closed")
        # The assignment itself survives the stream's deletion --
        # only the origin annotation is gone, not the assignee.
        self.assertEqual(
            self.store.current_assignee("linux-sim", "suite.py", "test_b"),
            "alice")
        self.assertIsNone(self.store.get_stream(self.stream_id))


class CommentStreamTagTest(StorageTestBase):
    """comments.stream_id — "posted from" (docs/STREAMS_PLAN.md §1)."""

    def test_defaults_to_none(self) -> None:
        self.store.upsert_runs([make_record()])
        comment = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "hi", BASE)
        self.assertIsNone(comment.stream_id)
        [stored] = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertIsNone(stored.stream_id)

    def test_round_trips_when_given(self) -> None:
        self.store.upsert_runs([make_record(build="feat/x")])
        stream_id = self.store.list_streams("")[0].stream_id
        comment = self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "from ci", BASE,
            stream_id=stream_id)
        self.assertEqual(comment.stream_id, stream_id)
        [stored] = self.store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual(stored.stream_id, stream_id)


class DashboardStreamScopingTest(StorageTestBase):
    """dashboard()/dashboard_count() default to mainline and can be
    scoped to any stream — mainline must be provably unaffected by a
    branch import (docs/STREAMS_PLAN.md §3.5)."""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_environment_product(
            "linux-sim", "Atlas", "alice", CREATED)
        self.store.upsert_runs([make_record()])
        self.store.upsert_runs([make_record(
            test_name="test_b", build="feat/x",
            start=BASE + datetime.timedelta(hours=1))])
        self.stream_id = self.store.list_streams("Atlas")[0].stream_id

    def test_default_dashboard_is_mainline_only(self) -> None:
        rows = self.store.dashboard()
        self.assertEqual([r.test_name for r in rows], ["test_a"])
        self.assertEqual(self.store.dashboard_count(), 1)

    def test_scoped_dashboard_sees_only_that_stream(self) -> None:
        rows = self.store.dashboard(stream_id=self.stream_id)
        self.assertEqual([r.test_name for r in rows], ["test_b"])
        self.assertEqual(
            self.store.dashboard_count(stream_id=self.stream_id), 1)

    def test_a_branch_import_does_not_change_the_mainline_count(
            self) -> None:
        before = self.store.dashboard_count()
        self.store.upsert_runs([make_record(
            test_name="test_c", build="feat/x",
            start=BASE + datetime.timedelta(hours=2))])
        after = self.store.dashboard_count()
        self.assertEqual(before, after)


class AssignmentOriginFilterTest(StorageTestBase):
    """dashboard()/dashboard_count()'s ``assignment_origin`` filter
    (WP-21, Open Actions §3.6) — WHERE the CURRENT assignment was made
    from, an axis entirely separate from ``stream_id`` (which scopes
    the test's own result, not who assigned it)."""

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
            make_record(test_name="test_c"),
        ])
        self.store.upsert_runs([make_record(build="feat/x")])
        self.stream_id = self.store.list_streams("")[0].stream_id
        # test_a: assigned from mainline (no stream_id)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED)
        # test_b: assigned from the build
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_b", "alice", "bob", CREATED,
            stream_id=self.stream_id)
        # test_c: never assigned at all

    def test_no_filter_returns_everything(self) -> None:
        self.assertEqual(self.store.dashboard_count(), 3)

    def test_build_origin_returns_only_the_build_made_assignment(
            self) -> None:
        rows = self.store.dashboard(assignment_origin="build")
        self.assertEqual([r.test_name for r in rows], ["test_b"])
        self.assertEqual(
            self.store.dashboard_count(assignment_origin="build"), 1)

    def test_the_dead_branch_spelling_raises(self) -> None:
        """WP-25 renamed the origin value branch->build before anything
        shipped; the old spelling must raise like any unknown value,
        never silently filter to nothing."""
        with self.assertRaises(ValueError):
            self.store.dashboard(assignment_origin="branch")

    def test_mainline_origin_includes_unassigned_and_mainline_made(
            self) -> None:
        """"mainline" reads as "not from a build" — an unassigned test
        was not made from anywhere, so it counts as mainline too, the
        same way it counts as mainline for un-retirement (§3.4)."""
        rows = self.store.dashboard(assignment_origin="mainline")
        self.assertEqual(
            sorted(r.test_name for r in rows), ["test_a", "test_c"])
        self.assertEqual(
            self.store.dashboard_count(assignment_origin="mainline"), 2)

    def test_an_unknown_origin_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.store.dashboard(assignment_origin="nonsense")

    def test_assignment_stream_ids_lists_only_non_mainline_ones(
            self) -> None:
        self.assertEqual(
            self.store.assignment_stream_ids(), [self.stream_id])


class AssignedOnlyFilterTest(StorageTestBase):
    """dashboard()/dashboard_count()'s ``assigned_only`` filter
    (2026-08-10, found in the first morning of build-verify manual
    testing): every row with a current assignee, whatever its result.
    Before it, an assignment on a mainline-PASSING test was visible
    NOWHERE — the three Open Actions result options and the
    "assigned"/"mine" queue predicates all gate on
    FAIL/UNEXPECTED_PASS, a mainline-triage assumption ("assigned"
    implied "because it is failing") that the build-verify flow broke:
    a test assigned to investigate why it did not run on an RC passes
    happily on mainline."""

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(test_name="test_passing_assigned"),
            make_record(test_name="test_failing_assigned",
                        result=Result.FAIL),
            make_record(test_name="test_passing_unassigned"),
        ])
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_passing_assigned",
            "alice", "bob", CREATED)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_failing_assigned",
            "carol", "bob", CREATED)

    def test_includes_a_passing_assigned_test(self) -> None:
        rows = self.store.dashboard(assigned_only=True)
        self.assertEqual(
            sorted(r.test_name for r in rows),
            ["test_failing_assigned", "test_passing_assigned"])
        self.assertEqual(
            self.store.dashboard_count(assigned_only=True), 2)

    def test_combines_with_assignees_by_narrowing(self) -> None:
        """AND-level with the owner OR-group: "Alice's assignments,
        any result" — never widened to assigned-to-anyone."""
        rows = self.store.dashboard(
            assigned_only=True, assignees=["alice"])
        self.assertEqual(
            [r.test_name for r in rows], ["test_passing_assigned"])

    def test_contradiction_with_unassigned_matches_nothing(self) -> None:
        """assigned AND unowned is empty by construction — never a
        silent reinterpretation of either filter (the frontend does not
        offer the combination; the storage layer must still be honest
        about what it would mean)."""
        self.assertEqual(
            self.store.dashboard_count(
                assigned_only=True, include_unassigned=True), 0)

    def test_off_by_default(self) -> None:
        self.assertEqual(self.store.dashboard_count(), 3)


class OpenItemsFilterTest(StorageTestBase):
    """dashboard()/dashboard_count()'s ``open_items`` composite
    (2026-08-10, the user's same-morning refinement of
    ``assigned_only`` above): "needs action" = failing, stale
    annotation, OR currently assigned — an assignment IS an open
    action whatever its result, which is what Open Actions' name
    always claimed. Server-side because the OR spans the result axis
    and the owner axis, which the AND-composed params cannot
    express."""

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(test_name="test_failing", result=Result.FAIL),
            make_record(test_name="test_stale_annotation",
                        result=Result.UNEXPECTED_PASS),
            make_record(test_name="test_passing_assigned"),
            make_record(test_name="test_passing_unassigned"),
        ])
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_passing_assigned",
            "alice", "bob", CREATED)

    def test_includes_all_three_kinds_of_open_item(self) -> None:
        rows = self.store.dashboard(open_items=True)
        self.assertEqual(
            sorted(r.test_name for r in rows),
            ["test_failing", "test_passing_assigned",
             "test_stale_annotation"])
        self.assertEqual(self.store.dashboard_count(open_items=True), 3)

    def test_a_plain_passing_unassigned_test_is_not_an_open_item(
            self) -> None:
        rows = self.store.dashboard(open_items=True)
        self.assertNotIn(
            "test_passing_unassigned", [r.test_name for r in rows])

    def test_off_by_default(self) -> None:
        self.assertEqual(self.store.dashboard_count(), 4)


class AssignmentStreamIdsEmptyTest(StorageTestBase):
    """assignment_stream_ids() on a database with no build-originated
    assignment at all — the signal Open Actions' filter reads to honour
    "zero visible change when no assignment carries a stream"."""

    def test_empty_with_only_mainline_assignments(self) -> None:
        self.store.upsert_runs([make_record(test_name="test_a")])
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED)
        self.assertEqual(self.store.assignment_stream_ids(), [])

    def test_empty_with_no_assignments_at_all(self) -> None:
        self.assertEqual(self.store.assignment_stream_ids(), [])


class SummaryCacheTest(StorageTestBase):
    """WP-23 "ONE MORE PERF SLICE": the shared summary/watch memo
    (``Storage._summary_cache``) -- same TTL-bounded, write-invalidated
    discipline as ``_trend_cache`` (:class:`EnvironmentDeleteTest`'s
    ``test_the_trend_cache_is_invalidated``, :class:`ActivityHoursTest`'s
    ``test_reads_come_from_the_derived_table_not_from_runs``), but one
    dict shared by several methods (``summary_rollup``, ``queue_counts``,
    ``status_queue``, ``test_counts_by_environment``,
    ``latest_run_time_by_environment``, ``environments``, ``scripts``,
    the per-entry ``failure_streak_bounds_many``) rather than one dict
    per method.

    Every "is a cache hit" assertion is a QUERY COUNT, not a value
    comparison -- a memo that silently recomputes every time would pass
    a value-only test while giving back none of the measured saving.
    """

    def _query_count(self, action: Callable[[], None]) -> int:
        statements = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, statements)
        try:
            action()
        finally:
            conn.set_trace_callback(None)
        return len(statements)

    def _expire_the_summary_cache(self) -> None:
        """Simulate every entry ageing past the TTL -- a write made by a
        DIFFERENT process, which this process's own invalidation calls
        cannot see, so only the TTL bound catches it."""
        too_old = storage._TREND_CACHE_TTL_SECONDS + 1.0
        for key, entry in list(self.store._summary_cache.items()):
            self.store._summary_cache[key] = (
                (entry[0] - too_old,) + tuple(entry[1:]))

    # -- repeat call is a genuine cache hit (no new SQL) -----------------

    def test_repeat_summary_rollup_is_a_cache_hit(self) -> None:
        self.store.upsert_runs([make_record()])
        cutoff = BASE - datetime.timedelta(hours=1)
        first = self.store.summary_rollup(cutoff)
        self.assertGreater(sum(c.count for c in first), 0)
        cost = self._query_count(lambda: self.store.summary_rollup(cutoff))
        self.assertEqual(
            cost, 0, "a repeat call for the SAME cutoff touched SQL")

    def test_repeat_queue_counts_is_a_cache_hit(self) -> None:
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        stale_before = BASE + datetime.timedelta(hours=1)
        self.store.queue_counts(stale_before=stale_before)
        cost = self._query_count(
            lambda: self.store.queue_counts(stale_before=stale_before))
        self.assertEqual(cost, 0)

    def test_repeat_status_queue_is_a_cache_hit(self) -> None:
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.store.status_queue("still_failing")
        cost = self._query_count(
            lambda: self.store.status_queue("still_failing"))
        self.assertEqual(cost, 0)

    def test_repeat_test_counts_by_environment_is_a_cache_hit(self) -> None:
        self.store.upsert_runs([make_record()])
        self.store.test_counts_by_environment()
        cost = self._query_count(
            lambda: self.store.test_counts_by_environment())
        self.assertEqual(cost, 0)

    def test_repeat_latest_run_time_by_environment_is_a_cache_hit(
        self,
    ) -> None:
        self.store.upsert_runs([make_record()])
        self.store.latest_run_time_by_environment()
        cost = self._query_count(
            lambda: self.store.latest_run_time_by_environment())
        self.assertEqual(cost, 0)

    def test_repeat_environments_and_scripts_are_cache_hits(self) -> None:
        self.store.upsert_runs([make_record()])
        self.store.environments()
        self.store.scripts()
        cost = self._query_count(lambda: (
            self.store.environments(), self.store.scripts()))
        self.assertEqual(cost, 0)

    def test_repeat_failure_streak_bounds_many_is_a_cache_hit(self) -> None:
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        entry = ("linux-sim", "suite.py", "test_a", BASE)
        self.store.failure_streak_bounds_many([entry])
        cost = self._query_count(
            lambda: self.store.failure_streak_bounds_many([entry]))
        self.assertEqual(cost, 0)

    # -- every listed mutator invalidates ---------------------------------

    def test_upsert_runs_invalidates(self) -> None:
        self.store.upsert_runs([make_record()])
        cutoff = BASE - datetime.timedelta(hours=1)
        before = self.store.summary_rollup(cutoff)
        self.assertEqual(sum(c.count for c in before), 1)
        self.store.upsert_runs([make_record(test_name="test_b")])
        after = self.store.summary_rollup(cutoff)
        self.assertEqual(
            sum(c.count for c in after), 2,
            "the memo kept serving the pre-import rollup")

    def test_delete_stream_invalidates(self) -> None:
        self.store.upsert_runs([
            make_record(),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        branch_id = self.store.list_streams("")[0].stream_id
        before = self.store.test_counts_by_environment(branch_id)
        self.assertEqual(before.get("linux-sim"), 1)
        self.store.delete_stream(branch_id)
        after = self.store.test_counts_by_environment(branch_id)
        self.assertEqual(
            after.get("linux-sim", 0), 0,
            "the memo kept serving the deleted stream's counts")

    def test_prune_runs_before_invalidates(self) -> None:
        """prune_runs_before only ever deletes NON-latest runs (see its
        own docstring), so a single-run estate has nothing observable
        for it to change -- this checks the memo is torn down (a fresh
        query, not a served value) rather than a rollup NUMBER, which
        would be the same before and after regardless of whether
        invalidation fired at all."""
        day = datetime.timedelta(days=1)
        self.store.upsert_runs([make_record(start=BASE - 400 * day)])
        cutoff = BASE
        self.store.summary_rollup(cutoff)
        self.store.prune_runs_before(BASE - 300 * day)
        cost = self._query_count(lambda: self.store.summary_rollup(cutoff))
        self.assertGreater(
            cost, 0, "the memo survived a prune untouched")

    def test_delete_environment_invalidates(self) -> None:
        self.store.upsert_runs([make_record(environment="UNKNOWN")])
        before = self.store.environments()
        self.assertIn("UNKNOWN", before)
        self.store.delete_environment("UNKNOWN")
        after = self.store.environments()
        self.assertNotIn(
            "UNKNOWN", after,
            "the memo kept serving a deleted environment")

    def test_set_assignee_invalidates_queue_counts(self) -> None:
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        stale_before = BASE + datetime.timedelta(hours=1)
        before = self.store.queue_counts(
            stale_before=stale_before, assignee="alice")
        self.assertEqual(before["mine"], 0)
        self.store.set_assignee(
            "linux-sim", "suite.py", "test_a", "alice", "bob", CREATED)
        after = self.store.queue_counts(
            stale_before=stale_before, assignee="alice")
        self.assertEqual(
            after["mine"], 1,
            "the memo kept serving zero assigned tests")

    def test_set_retired_invalidates_summary_rollup(self) -> None:
        self.store.upsert_runs([make_record()])
        cutoff = BASE - datetime.timedelta(hours=1)
        before = self.store.summary_rollup(cutoff)
        self.assertFalse(any(c.retired for c in before))
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "amy", "gone",
            CREATED)
        after = self.store.summary_rollup(cutoff)
        self.assertTrue(
            any(c.retired for c in after),
            "the memo kept serving the pre-retirement rollup")

    def test_set_retired_invalidates_test_counts(self) -> None:
        """test_counts_by_environment excludes retired tests -- the
        SAME rollup a retirement comment write must also invalidate."""
        self.store.upsert_runs([make_record()])
        before = self.store.test_counts_by_environment()
        self.assertEqual(before.get("linux-sim"), 1)
        self.store.set_retired(
            "linux-sim", "suite.py", "test_a", True, "amy", "gone",
            CREATED)
        after = self.store.test_counts_by_environment()
        self.assertEqual(
            after.get("linux-sim", 0), 0,
            "the memo kept counting a just-retired test")

    def test_add_comment_invalidates_status_queue_comment_payload(
        self,
    ) -> None:
        # Two FAILs, so prev_result is FAIL too and the row lands in
        # "still_failing" rather than "new_failures".
        self.store.upsert_runs([make_record(result=Result.FAIL)])
        self.store.upsert_runs([make_record(
            result=Result.FAIL,
            start=BASE + datetime.timedelta(hours=1),
            end=BASE + datetime.timedelta(hours=1, seconds=3))])
        (row_before,) = self.store.status_queue(
            "still_failing", with_latest_comment=True)
        self.assertIsNone(row_before.latest_comment)
        self.store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "note", CREATED)
        (row_after,) = self.store.status_queue(
            "still_failing", with_latest_comment=True)
        self.assertIsNotNone(
            row_after.latest_comment,
            "the memo kept serving the comment-less row")

    def test_environment_products_write_needs_no_invalidation(self) -> None:
        """Audited, not merely untested: every cached method takes its
        product/environment scope as an explicit `environments`
        allow-list argument rather than joining `environment_products`
        itself, so a remap changes which KEY a request computes rather
        than making an existing entry wrong. Proof: an environment's
        estate-wide (unscoped) `environments()` entry, cached BEFORE a
        product declaration, must still list it AFTER -- an unscoped
        read was never keyed by product in the first place."""
        self.store.upsert_runs([make_record()])
        before = self.store.environments()
        self.assertIn("linux-sim", before)
        self.store.set_environment_product(
            "linux-sim", "Atlas", "amy", CREATED)
        after = self.store.environments()
        self.assertIn("linux-sim", after)

    # -- scope-key isolation ----------------------------------------------

    def test_scope_key_isolation_by_environments_allow_list(self) -> None:
        """Product A's memo entry must never answer for product B."""
        self.store.upsert_runs([
            make_record(environment="linux-sim"),
            make_record(environment="win-sim"),
        ])
        only_linux = self.store.latest_run_time_by_environment(
            environments=["linux-sim"])
        only_win = self.store.latest_run_time_by_environment(
            environments=["win-sim"])
        self.assertEqual(sorted(only_linux), ["linux-sim"])
        self.assertEqual(sorted(only_win), ["win-sim"])

    def test_scope_key_isolation_by_stream(self) -> None:
        """A branch's memo entry must never answer for mainline's."""
        self.store.upsert_runs([
            make_record(),
            make_record(test_name="test_b", build="feat/x",
                        start=BASE + datetime.timedelta(hours=1)),
        ])
        branch_id = self.store.list_streams("")[0].stream_id
        mainline_counts = self.store.test_counts_by_environment()
        branch_counts = self.store.test_counts_by_environment(branch_id)
        self.assertEqual(mainline_counts.get("linux-sim"), 1)
        self.assertEqual(branch_counts.get("linux-sim"), 1)
        # Both counts happen to be 1 (one test each) -- the isolation
        # that matters is that a SUBSEQUENT write to only one stream
        # cannot leak into the other's still-cached entry.
        self.store.upsert_runs([
            make_record(test_name="test_c", build="feat/x",
                        start=BASE + datetime.timedelta(hours=2)),
        ])
        mainline_after = self.store.test_counts_by_environment()
        self.assertEqual(
            mainline_after.get("linux-sim"), 1,
            "a branch-only import changed mainline's cached count")

    def test_a_different_cutoff_is_a_different_key(self) -> None:
        """summary_rollup must never round/truncate the cutoff for
        caching purposes -- two distinct real cutoffs sharing one slot
        would serve counts computed for the wrong window."""
        self.store.upsert_runs([make_record()])
        self.store.summary_rollup(BASE - datetime.timedelta(hours=1))
        cost = self._query_count(
            lambda: self.store.summary_rollup(BASE))
        self.assertGreater(
            cost, 0,
            "a different cutoff reused another cutoff's cached rows")

    # -- TTL bound ----------------------------------------------------------

    def test_ttl_bound_expires_the_memo(self) -> None:
        self.store.upsert_runs([make_record()])
        cutoff = BASE - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        self._expire_the_summary_cache()
        cost = self._query_count(lambda: self.store.summary_rollup(cutoff))
        self.assertGreater(
            cost, 0, "an entry older than the TTL was still served")

    def test_within_ttl_still_hits(self) -> None:
        """The expiry helper itself must be capable of NOT expiring --
        otherwise test_ttl_bound_expires_the_memo above is vacuous."""
        self.store.upsert_runs([make_record()])
        cutoff = BASE - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        cost = self._query_count(lambda: self.store.summary_rollup(cutoff))
        self.assertEqual(cost, 0)


class MuteTest(StorageTestBase):
    """WP-40: muted failures (migration 11).

    The storage layer's contract: ONE predicate ("live" is
    ``expires_at > now``) read the same way by the dashboard, the
    queues, the counts and the rollup; a mute is OWNED, so
    muting assigns and unassigning drops it; hours are 1..168 on
    every stream; a stream's mutes go with the stream.
    """

    NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
    #: Every seeded run starts after this, so all of them "ran recently".
    CUTOFF = BASE - datetime.timedelta(hours=1)
    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def _seed(self) -> None:
        """Two new failures, two still-failing, one pass, one
        unexpected pass; all on mainline."""
        later = BASE + datetime.timedelta(hours=1)
        records = [
            make_record(test_name="f_new1", result=Result.FAIL, start=later),
            make_record(test_name="f_new2", result=Result.FAIL, start=later),
            make_record(test_name="f_still1", result=Result.FAIL),
            make_record(test_name="f_still1", result=Result.FAIL,
                        start=later),
            make_record(test_name="f_still2", result=Result.FAIL),
            make_record(test_name="f_still2", result=Result.FAIL,
                        start=later),
            make_record(test_name="p1", result=Result.PASS, start=later),
            make_record(test_name="up1", result=Result.UNEXPECTED_PASS,
                        start=later),
        ]
        self.store.upsert_runs(records)

    def _key(self, test: str, sid: int = storage.MAINLINE_STREAM_ID,
             env: Optional[str] = None) -> Tuple[str, str, str, int]:
        return (env or self.ENV, self.SCRIPT, test, sid)

    def _mute(
        self,
        tests: Sequence[str],
        reason: Optional[str] = "known issue",
        hours: int = 72,
        assignee: str = "alice",
        by: str = "bob",
        when: Optional[datetime.datetime] = None,
        sid: int = storage.MAINLINE_STREAM_ID,
    ) -> "storage.MuteResult":
        return self.store.mute_tests(
            [self._key(t, sid) for t in tests], reason, hours, assignee, by,
            when or self.NOW)

    def _build_id(self, name: str) -> int:
        return {
            stream.name: stream.stream_id
            for stream in self.store.list_streams("")}[name]

    def _count(self, table: str, where: str = "1 = 1",
               params: Sequence[Any] = ()) -> int:
        return int(self.store._conn().execute(
            "SELECT COUNT(*) FROM {0} WHERE {1}".format(table, where),
            tuple(params)).fetchone()[0])

    def _names(self, rows: Sequence[Any]) -> List[str]:
        return sorted(row.test_name for row in rows)

    def _cells(self, now: Optional[datetime.datetime] = None) -> Any:
        return self.store.muted_cells(now or self.NOW, self.CUTOFF)

    # -- muting ------------------------------------------------

    def test_mute_records_it_and_assigns_the_owner(self) -> None:
        self._seed()
        result = self._mute(["f_new1"], reason="flaky rig", hours=72)
        self.assertEqual(
            (result.muted, result.extended, result.unknown),
            (1, 0, 0))
        mute = self.store.mute_for(
            storage.MAINLINE_STREAM_ID, self.ENV, self.SCRIPT, "f_new1")
        assert mute is not None
        self.assertEqual(mute.reason, "flaky rig")
        self.assertEqual(mute.extensions, 0)
        self.assertEqual(mute.muted_by, "bob")
        self.assertEqual(mute.muted_at, self.NOW)
        self.assertEqual(
            mute.expires_at, self.NOW + datetime.timedelta(days=3))
        self.assertEqual(
            self.store.current_assignee(self.ENV, self.SCRIPT, "f_new1"),
            "alice")
        history = self.store.mute_history(
            self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual([h.action for h in history], ["mute"])
        self.assertEqual(history[0].actor, "bob")
        self.assertEqual(history[0].reason, "flaky rig")
        self.assertEqual(history[0].expires_at, mute.expires_at)

    def test_the_assignment_origin_is_the_stream_for_a_build_only(
        self,
    ) -> None:
        self.store.upsert_runs([
            make_record(test_name="m", result=Result.FAIL),
            make_record(test_name="b", result=Result.FAIL, build="b1"),
        ])
        sid = self._build_id("b1")
        self._mute(["m"])
        self._mute(["b"], sid=sid)
        rows = {
            row.test_name: row for row in self.store.dashboard(
                assigned_only=True, now=self.NOW)}
        self.assertIsNone(rows["m"].assignment_stream_id)
        build_rows = self.store.dashboard(
            assigned_only=True, stream_id=sid, now=self.NOW)
        self.assertEqual(
            [(r.test_name, r.assignment_stream_id) for r in build_rows],
            [("b", sid)])

    def test_hours_must_be_a_whole_number_of_one_to_168(self) -> None:
        self._seed()
        for bad in (0, 169, -1, True, False, 2.5, "3", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._mute(["f_new1"], hours=bad)  # type: ignore
        self.assertEqual(self._count("test_mutes"), 0)
        for good in (1, 12, 168):
            self._mute(["f_new1"], hours=good)

    def test_an_empty_assignee_is_refused(self) -> None:
        self._seed()
        for bad in ("", None):
            with self.assertRaises(ValueError):
                self._mute(["f_new1"], assignee=bad)  # type: ignore
        self.assertEqual(self._count("test_mutes"), 0)

    def test_a_fresh_key_with_no_reason_writes_nothing(self) -> None:
        self._seed()
        for bad in (None, ""):
            with self.assertRaises(ValueError):
                self._mute(["f_new1", "f_new2"], reason=bad)
        self.assertEqual(self._count("test_mutes"), 0)
        self.assertEqual(self._count("mute_history"), 0)
        self.assertIsNone(
            self.store.current_assignee(self.ENV, self.SCRIPT, "f_new1"))
        self.assertEqual(self._count("assignments"), 0)

    def test_one_fresh_key_without_a_reason_blocks_the_whole_batch(
        self,
    ) -> None:
        self._seed()
        self._mute(["f_new1"])
        before = self.store.mute_for(
            1, self.ENV, self.SCRIPT, "f_new1")
        with self.assertRaises(ValueError):
            self._mute(["f_new1", "f_new2"], reason=None, hours=120)
        self.assertEqual(
            self.store.mute_for(
                1, self.ENV, self.SCRIPT, "f_new1"), before)
        self.assertIsNone(self.store.mute_for(
            1, self.ENV, self.SCRIPT, "f_new2"))

    def test_unknown_keys_are_counted_and_skipped(self) -> None:
        self._seed()
        result = self.store.mute_tests(
            [self._key("f_new1"), self._key("no_such_test"),
             self._key("f_new2", sid=999)],
            "why", 48, "alice", "bob", self.NOW)
        self.assertEqual(
            (result.muted, result.extended, result.unknown),
            (1, 0, 2))
        self.assertEqual(self._count("test_mutes"), 1)
        self.assertEqual(self._count("assignments"), 1)
        only_unknown = self.store.mute_tests(
            [self._key("no_such_test")], "why", 48, "alice", "bob", self.NOW)
        self.assertEqual(
            (only_unknown.muted, only_unknown.unknown), (0, 1))

    def test_an_empty_selection_is_a_no_op(self) -> None:
        result = self.store.mute_tests(
            [], "why", 48, "alice", "bob", self.NOW)
        self.assertEqual(
            (result.muted, result.extended, result.unknown),
            (0, 0, 0))

    def test_muting_again_extends(self) -> None:
        self._seed()
        self._mute(["f_new1"], reason="first", hours=72, by="bob")
        later = self.NOW + datetime.timedelta(days=1)
        result = self._mute(
            ["f_new1"], reason=None, hours=48, by="carol", when=later)
        self.assertEqual(
            (result.muted, result.extended), (0, 1))
        mute = self.store.mute_for(1, self.ENV, self.SCRIPT,
                                            "f_new1")
        assert mute is not None
        self.assertEqual(mute.extensions, 1)
        self.assertEqual(mute.reason, "first")
        self.assertEqual(mute.muted_by, "carol")
        self.assertEqual(mute.expires_at, later + datetime.timedelta(days=2))
        # The original moment of mute is kept.
        self.assertEqual(mute.muted_at, self.NOW)
        self._mute(
            ["f_new1"], reason="second", hours=24, by="bob",
            when=later + datetime.timedelta(days=1))
        mute = self.store.mute_for(1, self.ENV, self.SCRIPT,
                                            "f_new1")
        assert mute is not None
        self.assertEqual((mute.extensions, mute.reason), (2, "second"))
        history = self.store.mute_history(
            self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual(
            [h.action for h in history], ["extend", "extend", "mute"])

    def test_a_mixed_batch_mutes_and_extends(self) -> None:
        self._seed()
        self._mute(["f_new1"])
        result = self._mute(["f_new1", "f_new2"], reason="both")
        self.assertEqual(
            (result.muted, result.extended), (1, 1))

    # -- the one predicate --------------------------------------------

    def _mute_some(self) -> None:
        self._mute(["f_new1", "f_still1"])

    def test_the_rollup_is_unchanged_and_the_cells_subtract_exactly(
        self,
    ) -> None:
        self._seed()
        base_cells = self.store.summary_rollup(self.CUTOFF)
        plain = analytics.summarize_rollup(base_cells)
        self.assertEqual(
            (plain.status.new_failures, plain.status.still_failing,
             plain.status.results[Result.FAIL]), (2, 2, 4))
        self._mute_some()
        self.assertEqual(self.store.summary_rollup(self.CUTOFF), base_cells)
        cells = self._cells()
        summary = analytics.summarize_rollup(base_cells, muted=cells)
        status = summary.status
        self.assertEqual(status.muted, 2)
        self.assertEqual(status.muted_total, 2)
        self.assertEqual(status.muted_new_failures, 1)
        self.assertEqual(status.muted_still_failing, 1)
        self.assertEqual(status.new_failures, 1)
        self.assertEqual(status.still_failing, 1)
        # Conserved against the unmuted estate: a muted failure moves
        # from the failing figure to the "+N muted" beside it ...
        self.assertEqual(
            status.new_failures + status.muted_new_failures,
            plain.status.new_failures)
        self.assertEqual(
            status.still_failing + status.muted_still_failing,
            plain.status.still_failing)
        # ... and is still a failure: the pass rate's inputs do not move.
        self.assertEqual(status.results, plain.status.results)
        self.assertEqual(
            status.recent_results, plain.status.recent_results)
        self.assertEqual(status.total_tests, plain.status.total_tests)
        self.assertEqual(summary.by_environment[0].muted, 2)

    def _scripts_estate(self) -> None:
        """big.py: 3 failing; two.py: 2 failing (all on mainline)."""
        self.store.upsert_runs([
            make_record(script=script, test_name=name,
                        result=Result.FAIL, start=BASE)
            for script, names in (
                ("big.py", ("b1", "b2", "b3")), ("two.py", ("t1", "t2")))
            for name in names])

    def test_top_failing_scripts_split_failing_from_muted(self) -> None:
        self._scripts_estate()
        top = lambda: [  # noqa: E731
            (s.script, s.failing, s.muted)
            for s in self.store.top_failing_scripts(now=self.NOW)]
        self.assertEqual(
            top(), [("big.py", 3, 0), ("two.py", 2, 0)])
        self.store.mute_tests(
            [(self.ENV, "big.py", "b1", 1), (self.ENV, "big.py", "b2", 1)],
            "known", 72, "alice", "bob", self.NOW)
        # 3 failing of which 2 muted: failing 1, muted 2 -- so it sorts
        # BELOW the script with 2 unmuted failures.
        self.assertEqual(
            top(), [("two.py", 2, 0), ("big.py", 1, 2)])

    def test_a_script_whose_failures_are_all_muted_still_appears(
        self,
    ) -> None:
        self._scripts_estate()
        self.store.mute_tests(
            [(self.ENV, "two.py", "t1", 1), (self.ENV, "two.py", "t2", 1)],
            "known", 72, "alice", "bob", self.NOW)
        self.assertEqual(
            [(s.script, s.failing, s.muted)
             for s in self.store.top_failing_scripts(now=self.NOW)],
            [("big.py", 3, 0), ("two.py", 0, 2)])

    def test_top_failing_scripts_follow_expiry_without_a_write(
        self,
    ) -> None:
        self._scripts_estate()
        self.store.mute_tests(
            [(self.ENV, "two.py", "t1", 1)],
            "known", 24, "alice", "bob", self.NOW)
        def two(when: datetime.datetime) -> Tuple[int, int]:
            row = [s for s in self.store.top_failing_scripts(now=when)
                   if s.script == "two.py"][0]
            return (row.failing, row.muted)
        self.assertEqual(two(self.NOW), (1, 1))
        # Memoized now; the epoch in the key still lets expiry through.
        self.assertEqual(two(self.NOW + datetime.timedelta(hours=1)), (1, 1))
        self.assertEqual(two(self.NOW + datetime.timedelta(hours=25)), (2, 0))
        # A write drops the memo: unmuting is seen at once.
        self.store.mute_tests(
            [(self.ENV, "two.py", "t2", 1)],
            "known", 24, "alice", "bob", self.NOW)
        self.assertEqual(two(self.NOW), (0, 2))
        self.store.unmute_tests(
            [(self.ENV, "two.py", "t2", 1)], "bob", self.NOW)
        self.assertEqual(two(self.NOW), (1, 1))

    def test_muted_total_counts_a_passing_mute_but_muted_does_not(
        self,
    ) -> None:
        self._seed()
        base_cells = self.store.summary_rollup(self.CUTOFF)
        plain = analytics.summarize_rollup(base_cells)
        self._mute(["p1", "f_new1"])
        summary = analytics.summarize_rollup(
            base_cells, muted=self._cells())
        status = summary.status
        # muted_total is every live mute; muted (the figure taken off
        # failing) is only the one that is failing.
        self.assertEqual((status.muted, status.muted_total), (1, 2))
        self.assertEqual(status.results, plain.status.results)
        self.assertEqual(
            status.recent_results, plain.status.recent_results)
        self.assertEqual(summary.by_environment[0].muted, 1)
        by_product = analytics.summarize_by_product(
            base_cells, {self.ENV: "prod"}, muted=self._cells())
        self.assertEqual(
            (by_product[0].failing, by_product[0].muted),
            (plain.status.results[Result.FAIL] - 1, 1))

    # -- the reason is also a comment ---------------------------------

    def test_a_fresh_mute_posts_its_reason_as_a_comment(self) -> None:
        self._seed()
        self._mute(["f_new1"], reason="flaky rig", by="bob")
        comments = self.store.comments(self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual(
            [(c.text, c.author, c.stream_id) for c in comments],
            [("flaky rig", "bob", None)])

    def test_a_plain_extension_posts_no_comment(self) -> None:
        self._seed()
        self._mute(["f_new1"], reason="first", by="bob")
        self._mute(["f_new1"], reason=None, hours=48, by="carol",
                   when=self.NOW + datetime.timedelta(hours=1))
        comments = self.store.comments(self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual([c.text for c in comments], ["first"])

    def test_an_extension_with_a_new_reason_posts_it(self) -> None:
        self._seed()
        self._mute(["f_new1"], reason="first", by="bob")
        self._mute(["f_new1"], reason="still broken", hours=48, by="carol",
                   when=self.NOW + datetime.timedelta(hours=1))
        comments = self.store.comments(self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual(
            [(c.text, c.author) for c in comments],
            [("first", "bob"), ("still broken", "carol")])

    def test_a_build_mutes_comment_is_tagged_with_that_build(self) -> None:
        self._seed()
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        build="b1")])
        sid = self._build_id("b1")
        self._mute(["f_new1"], reason="build only", sid=sid)
        comments = self.store.comments(self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual(
            [(c.text, c.stream_id) for c in comments],
            [("build only", sid)])

    def test_queue_counts_conserve_the_failures(self) -> None:
        self._seed()
        before = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(before["muted"], 0)
        self._mute_some()
        after = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(after["muted"], 2)
        self.assertEqual(after["new_failures"], 1)
        self.assertEqual(after["still_failing"], 1)
        self.assertEqual(
            after["new_failures"] + after["still_failing"]
            + after["muted"],
            before["new_failures"] + before["still_failing"])
        for kind in ("fixed", "unexpected_passes", "not_run"):
            self.assertEqual(after[kind], before[kind], kind)
        # Calling again (memo hit) gives the same answer, and the memo's
        # own dict was not corrupted by the subtraction.
        self.assertEqual(
            self.store.queue_counts(stale_before=self.CUTOFF, now=self.NOW),
            after)

    def test_the_queues_agree_with_the_counts_and_each_other(self) -> None:
        self._seed()
        self._mute_some()
        counts = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        names = {
            kind: self._names(self.store.status_queue(
                kind, stale_before=self.CUTOFF, now=self.NOW))
            for kind in storage.QUEUE_KINDS}
        self.assertEqual(names["muted"], ["f_new1", "f_still1"])
        self.assertEqual(names["new_failures"], ["f_new2"])
        self.assertEqual(names["still_failing"], ["f_still2"])
        for kind in storage.QUEUE_KINDS:
            self.assertEqual(
                self.store.status_queue_count(
                    kind, stale_before=self.CUTOFF, now=self.NOW),
                len(names[kind]), kind)
            self.assertEqual(counts[kind], len(names[kind]), kind)

    def test_the_dashboard_agrees_with_the_queues(self) -> None:
        self._seed()
        self._mute_some()
        yes = self.store.dashboard(muted=True, now=self.NOW)
        no = self.store.dashboard(muted=False, now=self.NOW)
        everything = self.store.dashboard(now=self.NOW)
        self.assertEqual(self._names(yes), ["f_new1", "f_still1"])
        self.assertEqual(len(yes) + len(no), len(everything))
        self.assertEqual(
            self.store.dashboard_count(muted=True, now=self.NOW),
            len(yes))
        self.assertEqual(
            self.store.dashboard_count(muted=False, now=self.NOW),
            len(no))
        self.assertEqual(
            self.store.dashboard_count(now=self.NOW), len(everything))
        queue = self._names(self.store.status_queue(
            "muted", stale_before=self.CUTOFF, now=self.NOW))
        self.assertEqual(self._names(yes), queue)
        self.assertNotIn("f_new1", self._names(no))

    def test_rows_carry_their_mute(self) -> None:
        self._seed()
        self._mute(["f_new1"], reason="rig", hours=96, by="bob")
        rows = {r.test_name: r for r in self.store.dashboard(now=self.NOW)}
        mute = rows["f_new1"].mute
        assert mute is not None
        self.assertEqual(
            (mute.reason, mute.muted_by, mute.extensions, mute.stream_id,
             mute.expires_at),
            ("rig", "bob", 0, storage.MAINLINE_STREAM_ID,
             self.NOW + datetime.timedelta(days=4)))
        self.assertIsNone(rows["f_new2"].mute)
        self.assertIsNone(rows["p1"].mute)
        queued = self.store.status_queue(
            "muted", stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].mute, mute)
        other = self.store.status_queue(
            "new_failures", stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(self._names(other), ["f_new2"])
        self.assertIsNone(other[0].mute)

    # -- expiry -------------------------------------------------------

    def test_an_expired_mute_stops_counting(self) -> None:
        self._seed()
        self._mute_some()
        later = self.NOW + datetime.timedelta(days=3, seconds=1)
        counts = self.store.queue_counts(
            stale_before=self.CUTOFF, now=later)
        self.assertEqual(
            (counts["muted"], counts["new_failures"],
             counts["still_failing"]), (0, 2, 2))
        self.assertEqual(
            self._names(self.store.status_queue(
                "new_failures", stale_before=self.CUTOFF, now=later)),
            ["f_new1", "f_new2"])
        self.assertEqual(
            self.store.status_queue(
                "muted", stale_before=self.CUTOFF, now=later), [])
        self.assertEqual(
            self.store.dashboard_count(muted=True, now=later), 0)
        self.assertEqual(self._cells(later), [])
        # On record, though expired.
        mute = self.store.mute_for(1, self.ENV, self.SCRIPT,
                                            "f_new1")
        assert mute is not None
        self.assertLess(mute.expires_at, later)
        row = [r for r in self.store.dashboard(now=later)
               if r.test_name == "f_new1"][0]
        self.assertEqual(row.mute, mute)

    def test_the_boundary_second_is_expired(self) -> None:
        """live iff expires_at > now: AT the expiry it is gone."""
        self._seed()
        self._mute(["f_new1"], hours=24)
        edge = self.NOW + datetime.timedelta(days=1)
        self.assertEqual(
            self.store.status_queue_count(
                "muted", stale_before=self.CUTOFF, now=edge), 0)
        just = edge - datetime.timedelta(seconds=1)
        self.assertEqual(
            self.store.status_queue_count(
                "muted", stale_before=self.CUTOFF, now=just), 1)

    def test_the_epoch_is_the_next_future_expiry(self) -> None:
        self._seed()
        sid = storage.MAINLINE_STREAM_ID
        self.assertIsNone(self.store.mute_epoch(sid, self.NOW))
        self._mute(["f_new1"], hours=120)
        self._mute(["f_new2"], hours=48)
        self.assertEqual(
            self.store.mute_epoch(sid, self.NOW),
            model.format_iso(self.NOW + datetime.timedelta(days=2)))
        mid = self.NOW + datetime.timedelta(days=3)
        self.assertEqual(
            self.store.mute_epoch(sid, mid),
            model.format_iso(self.NOW + datetime.timedelta(days=5)))
        end = self.NOW + datetime.timedelta(days=6)
        self.assertIsNone(self.store.mute_epoch(sid, end))
        self.assertIsNone(self.store.mute_epoch(999, self.NOW))

    def test_a_pass_in_between_changes_nothing(self) -> None:
        self._seed()
        self._mute(["f_new1"])
        hour = datetime.timedelta(hours=1)
        base = self.NOW - datetime.timedelta(hours=10)
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.PASS,
                        start=base)])
        # While it passes it is STILL muted (the Muted tab lists every
        # live mute), but its cell says PASS and nothing is subtracted.
        self.assertEqual(
            self.store.status_queue_count(
                "muted", stale_before=self.CUTOFF, now=self.NOW), 1)
        self.assertEqual(
            [(c.result, c.count) for c in self._cells()],
            [(Result.PASS, 1)])
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        start=base + hour)])
        self.assertEqual(
            self._names(self.store.status_queue(
                "muted", stale_before=self.CUTOFF, now=self.NOW)),
            ["f_new1"])
        self.assertEqual(
            [(c.result, c.count) for c in self._cells()],
            [(Result.FAIL, 1)])

    def test_a_passing_muted_test_is_listed_and_subtracts_nothing(
        self,
    ) -> None:
        self._seed()
        before = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self._mute(["p1", "f_new1"])
        # Every live mute is a cell, carrying its REAL result.
        self.assertEqual(
            sorted((c.result.value, c.count) for c in self._cells()),
            sorted([(Result.PASS.value, 1), (Result.FAIL.value, 1)]))
        counts = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        # The tab badge is the row count; only the FAIL one came off
        # the failing queue.
        self.assertEqual(counts["muted"], 2)
        self.assertEqual(
            counts["new_failures"], before["new_failures"] - 1)
        self.assertEqual(counts["still_failing"], before["still_failing"])
        rows = self.store.status_queue(
            "muted", stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(self._names(rows), ["f_new1", "p1"])
        self.assertEqual(
            {row.test_name: row.result for row in rows},
            {"f_new1": Result.FAIL, "p1": Result.PASS})
        self.assertEqual(
            counts["muted"],
            self.store.status_queue_count(
                "muted", stale_before=self.CUTOFF, now=self.NOW))
        self.assertEqual(counts["muted"], len(rows))

    def test_a_retired_test_is_not_counted(self) -> None:
        self._seed()
        self._mute_some()
        self.store.set_retired(
            self.ENV, self.SCRIPT, "f_new1", True, "alice", "gone", CREATED)
        self.assertEqual(sum(c.count for c in self._cells()), 1)
        counts = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(counts["muted"], 1)
        self.assertEqual(
            self._names(self.store.status_queue(
                "muted", stale_before=self.CUTOFF, now=self.NOW)),
            ["f_still1"])

    # -- streams ------------------------------------------------------

    def test_a_builds_mutes_do_not_touch_mainline(self) -> None:
        self._seed()
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        build="b1")])
        sid = self._build_id("b1")
        mainline_before = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self._mute(["f_new1"], sid=sid)
        # Assignment is per test, not per stream, so `assigned` moves;
        # every figure about FAILURES must not.
        after = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        for kind in ("new_failures", "still_failing", "muted",
                     "fixed", "unexpected_passes", "not_run"):
            self.assertEqual(after[kind], mainline_before[kind], kind)
        build = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW, stream_id=sid)
        self.assertEqual(
            (build["muted"], build["new_failures"]), (1, 0))
        self.assertIsNone(self.store.mute_for(
            1, self.ENV, self.SCRIPT, "f_new1"))
        self.assertEqual(self._cells(), [])
        self.assertEqual(
            sum(c.count for c in self.store.muted_cells(
                self.NOW, self.CUTOFF, stream_id=sid)), 1)

    def test_mainlines_mutes_do_not_touch_a_build(self) -> None:
        self._seed()
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        build="b1")])
        sid = self._build_id("b1")
        self._mute(["f_new1"])
        build = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW, stream_id=sid)
        self.assertEqual(
            (build["muted"], build["new_failures"]), (0, 1))
        self.assertEqual(
            self.store.dashboard_count(
                muted=True, now=self.NOW, stream_id=sid), 0)

    def _history_count(self, sid: int) -> int:
        return self._count("mute_history", "stream_id = ?", [sid])

    def test_delete_stream_removes_its_mutes_and_history(
        self,
    ) -> None:
        self._seed()
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        build="b1"),
            make_record(test_name="f_new2", result=Result.FAIL,
                        build="b1")])
        sid = self._build_id("b1")
        self._mute(["f_new1", "f_new2"], sid=sid)
        self._mute(["f_new1"])
        self.assertEqual(self._history_count(sid), 2)
        deleted = self.store.delete_stream(sid)
        self.assertEqual(deleted["test_mutes"], 2)
        self.assertEqual(
            self._count("test_mutes", "stream_id = ?", [sid]), 0)
        self.assertEqual(self._history_count(sid), 0)
        # Mainline's own mute and history are untouched.
        self.assertEqual(self._count("test_mutes"), 1)
        self.assertEqual(self._history_count(1), 1)

    def test_prune_empty_streams_takes_stray_mutes(self) -> None:
        self.store.upsert_runs([make_record(build="live")])
        conn = self.store._conn()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO streams (product, kind, name, first_seen, "
            "last_seen) VALUES (?, 'build', ?, ?, ?)",
            ("", "orphan", model.format_iso(BASE), model.format_iso(BASE)))
        conn.execute("COMMIT")
        orphan = self._build_id("orphan")
        self.store.ensure_user("bob", CREATED)
        stamp = model.format_iso(BASE)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO test_mutes (stream_id, environment, "
            "script, test_name, reason, muted_at, expires_at, "
            "muted_by, extensions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (orphan, self.ENV, self.SCRIPT, "gone", "r", stamp, stamp, "bob"))
        conn.execute(
            "INSERT INTO mute_history (stream_id, environment, "
            "script, test_name, action, reason, expires_at, actor, "
            "acted_at) VALUES (?, ?, ?, ?, 'mute', 'r', ?, 'bob', ?)",
            (orphan, self.ENV, self.SCRIPT, "gone", stamp, stamp))
        conn.execute("COMMIT")
        removed = self.store.prune_empty_streams()
        self.assertEqual([s.name for s in removed], ["orphan"])
        self.assertEqual(self._count("test_mutes"), 0)
        self.assertEqual(self._count("mute_history"), 0)

    # -- unassigning --------------------------------------------------

    def _two_streams(self) -> int:
        self.store.upsert_runs([
            make_record(test_name="x", result=Result.FAIL),
            make_record(test_name="x", result=Result.FAIL, build="b1",
                        start=BASE + datetime.timedelta(hours=2))])
        sid = self._build_id("b1")
        self._mute(["x"])
        self._mute(["x"], sid=sid)
        self.assertEqual(self._count("test_mutes"), 2)
        return sid

    def test_unassigning_drops_the_mute_on_every_stream(
        self,
    ) -> None:
        sid = self._two_streams()
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "x", None, "bob", self.NOW)
        self.assertEqual(self._count("test_mutes"), 0)
        clears = [
            h for h in self.store.mute_history(
                self.ENV, self.SCRIPT, "x") if h.action == "unmute"]
        self.assertEqual(sorted(h.stream_id for h in clears), [1, sid])
        for h in clears:
            self.assertEqual(h.reason, "unassigned")
            self.assertEqual(h.actor, "bob")
            self.assertIsNone(h.expires_at)

    def test_bulk_unassign_drops_it_too(self) -> None:
        self._two_streams()
        self.store.bulk_set_assignee_for_triples(
            None, "bob", self.NOW, [(self.ENV, self.SCRIPT, "x", None)])
        self.assertEqual(self._count("test_mutes"), 0)
        self.assertEqual(
            self._count("mute_history", "action = 'unmute'"), 2)

    def test_filtered_bulk_unassign_drops_it_too(self) -> None:
        self._seed()
        self._mute_some()
        count = self.store.bulk_set_assignee(
            None, "bob", self.NOW, assigned_only=True, now=self.NOW)
        self.assertEqual(count, 2)
        self.assertEqual(self._count("test_mutes"), 0)

    def test_reassigning_keeps_the_mute(self) -> None:
        self._two_streams()
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "x", "carol", "bob", self.NOW)
        self.assertEqual(self._count("test_mutes"), 2)
        self.store.bulk_set_assignee_for_triples(
            "dave", "bob", self.NOW, [(self.ENV, self.SCRIPT, "x", None)])
        self.assertEqual(self._count("test_mutes"), 2)
        self.assertEqual(
            self._count("mute_history", "action = 'unmute'"), 0)

    def test_unassigning_another_test_leaves_the_rest(self) -> None:
        self._seed()
        self._mute(["f_new1"])
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "f_new2", "alice", "bob", self.NOW)
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "f_new2", None, "bob", self.NOW)
        self.assertEqual(self._count("test_mutes"), 1)

    # -- clearing -----------------------------------------------------

    def test_clear_keeps_the_assignment_and_is_idempotent(self) -> None:
        self._seed()
        self._mute_some()
        keys = [self._key("f_new1")]
        self.assertEqual(
            self.store.unmute_tests(keys, "carol", self.NOW), 1)
        self.assertIsNone(self.store.mute_for(
            1, self.ENV, self.SCRIPT, "f_new1"))
        self.assertEqual(
            self.store.current_assignee(self.ENV, self.SCRIPT, "f_new1"),
            "alice")
        history = self.store.mute_history(
            self.ENV, self.SCRIPT, "f_new1")
        self.assertEqual(
            [(h.action, h.actor, h.reason) for h in history],
            [("unmute", "carol", None), ("mute", "bob", "known issue")])
        self.assertEqual(
            self.store.unmute_tests(keys, "carol", self.NOW), 0)
        self.assertEqual(len(self.store.mute_history(
            self.ENV, self.SCRIPT, "f_new1")), 2)
        # Cleared, so the failure is back in its queue; the other stays.
        counts = self.store.queue_counts(
            stale_before=self.CUTOFF, now=self.NOW)
        self.assertEqual(
            (counts["muted"], counts["new_failures"]), (1, 2))

    def test_clear_names_a_stream(self) -> None:
        sid = self._two_streams()
        removed = self.store.unmute_tests(
            [self._key("x", sid)], "carol", self.NOW)
        self.assertEqual(removed, 1)
        self.assertIsNotNone(self.store.mute_for(
            1, self.ENV, self.SCRIPT, "x"))
        self.assertIsNone(self.store.mute_for(
            sid, self.ENV, self.SCRIPT, "x"))

    # -- expiring -----------------------------------------------------

    def test_expiring_mutes(self) -> None:
        self._seed()
        day = datetime.timedelta(days=1)
        hours = datetime.timedelta(hours=2)
        # Expires in 2 hours.
        self._mute(["f_new1"], hours=72, when=self.NOW - 3 * day + hours)
        # Expired yesterday.
        self._mute(["f_new2"], hours=24, when=self.NOW - 2 * day, assignee="eve")
        # Expires in 3 days.
        self._mute(["f_still1"], hours=72)
        # Would be in the window, but the test passes now.
        self._mute(["p1"], hours=72, when=self.NOW - 3 * day + hours)
        expiring = self.store.expiring_mutes(self.NOW, 1 * day)
        self.assertEqual(
            [e.mute.test_name for e in expiring],
            ["f_new2", "f_new1"])
        first = expiring[0]
        self.assertEqual(
            (first.stream_kind, first.stream_name, first.result,
             first.assignee),
            ("mainline", "", Result.FAIL, "eve"))
        self.assertEqual(
            first.mute.expires_at, self.NOW - day)
        self.assertEqual(expiring[1].assignee, "alice")

    def test_expiring_filters_by_stream_and_carries_the_build(self) -> None:
        self._seed()
        self.store.upsert_runs([
            make_record(test_name="f_new1", result=Result.FAIL,
                        build="b1")])
        sid = self._build_id("b1")
        self._mute(["f_new1"], hours=24)
        self._mute(["f_new1"], hours=24, sid=sid)
        within = datetime.timedelta(days=2)
        both = self.store.expiring_mutes(self.NOW, within)
        self.assertEqual(sorted(e.mute.stream_id for e in both),
                         [1, sid])
        only = self.store.expiring_mutes(
            self.NOW, within, stream_id=sid)
        self.assertEqual(len(only), 1)
        self.assertEqual(
            (only[0].stream_kind, only[0].stream_name), ("build", "b1"))

    def test_expiring_leaves_out_a_retired_test(self) -> None:
        self._seed()
        self._mute(["f_new1"], hours=24)
        self.store.set_retired(
            self.ENV, self.SCRIPT, "f_new1", True, "alice", "gone", CREATED)
        self.assertEqual(self.store.expiring_mutes(
            self.NOW, datetime.timedelta(days=2)), [])

    # -- the memo: a repeat read is free -------------------------------

    def _statements(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return seen

    def _mute_cell_keys(self) -> List[Tuple[Any, ...]]:
        return sorted(
            key for key in self.store._summary_cache
            if key[0] == "mute_cells")

    def test_a_repeat_muted_cells_read_is_a_memo_hit(self) -> None:
        self._seed()
        self._mute_some()
        first = self._cells()
        self.assertEqual(sum(c.count for c in first), 2)
        self.assertEqual(self._statements(self._cells), [])
        self.assertEqual(self._cells(), first)
        # A scoped read is a filter of the same entries.
        self.assertEqual(
            self._statements(lambda: self.store.muted_cells(
                self.NOW, self.CUTOFF, environment=self.ENV)), [])
        self.assertEqual(
            self.store.muted_cells(
                self.NOW, self.CUTOFF, environment="elsewhere"), [])
        self.assertEqual(
            self.store.muted_cells(
                self.NOW, self.CUTOFF, environments=[]), [])

    def test_a_stream_with_no_mutes_costs_nothing_once_known(
        self,
    ) -> None:
        self._seed()
        sid = storage.MAINLINE_STREAM_ID
        self.assertEqual(self._cells(), [])
        self.assertIsNone(self.store.mute_epoch(sid, self.NOW))
        for call in (
                self._cells,
                lambda: self.store.mute_epoch(sid, self.NOW),
                lambda: self.store.muted_cells(
                    self.NOW + datetime.timedelta(days=30), self.CUTOFF)):
            self.assertEqual(self._statements(call), [])

    def test_the_epoch_read_is_a_memo_hit_and_never_serves_a_past_clock(
        self,
    ) -> None:
        self._seed()
        sid = storage.MAINLINE_STREAM_ID
        self._mute(["f_new1"], hours=48)
        expected = model.format_iso(self.NOW + datetime.timedelta(days=2))
        self.assertEqual(
            self.store.mute_epoch(sid, self.NOW), expected)
        later = self.NOW + datetime.timedelta(days=1)
        self.assertEqual(self._statements(
            lambda: self.store.mute_epoch(sid, later)), [])
        # Past the epoch: recomputed, and now there is none.
        past = self.NOW + datetime.timedelta(days=2, seconds=1)
        self.assertEqual(self._statements(
            lambda: self.assertIsNone(
                self.store.mute_epoch(sid, past))) != [], True)
        # An EARLIER clock than the one the memo holds is not served
        # the memo (it would say None while the row was still live).
        self.assertEqual(
            self.store.mute_epoch(sid, self.NOW), expected)

    def test_passing_the_epoch_recomputes_the_cells(self) -> None:
        self._seed()
        self._mute(["f_new1"], hours=48)
        self._mute(["f_still1"], hours=120)
        self.assertEqual(sum(c.count for c in self._cells()), 2)
        mid = self.NOW + datetime.timedelta(days=3)
        self.assertEqual(self._statements(lambda: self._cells(mid)) != [],
                         True)
        self.assertEqual(sum(c.count for c in self._cells(mid)), 1)
        self.assertEqual(self._statements(lambda: self._cells(mid)), [])
        end = self.NOW + datetime.timedelta(days=6)
        self.assertEqual(self._cells(end), [])
        # And back: a clock before the epoch the memo now holds.
        self.assertEqual(sum(c.count for c in self._cells()), 2)

    def test_every_mute_write_invalidates_the_cells(self) -> None:
        self._seed()
        total = lambda: sum(c.count for c in self._cells())  # noqa: E731
        self._mute(["f_new1"])
        self.assertEqual(total(), 1)
        self._mute(["f_still1"])                       # mute
        self.assertEqual(total(), 2)
        self._mute(["f_still1"], hours=168)               # extend
        self.assertEqual(total(), 2)
        self.assertEqual(
            self.store.mute_epoch(1, self.NOW),
            model.format_iso(self.NOW + datetime.timedelta(days=3)))
        self.store.unmute_tests(
            [self._key("f_new1")], "carol", self.NOW)  # clear
        self.assertEqual(total(), 1)
        self.assertEqual(
            self.store.mute_epoch(1, self.NOW),
            model.format_iso(self.NOW + datetime.timedelta(days=7)))
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "f_still1", None, "bob",
            self.NOW)                                  # unassign
        self.assertEqual(total(), 0)
        self.assertIsNone(self.store.mute_epoch(1, self.NOW))

    def test_a_push_into_one_environment_drops_only_its_cells(self) -> None:
        later = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(environment=env, test_name=name,
                        result=Result.FAIL, start=later)
            for env in ("env-x", "env-y") for name in ("a", "b")])
        self.store.mute_tests(
            [("env-x", self.SCRIPT, "a", 1), ("env-y", self.SCRIPT, "a", 1)],
            "why", 72, "alice", "bob", self.NOW)
        cells = self._cells()
        self.assertEqual(sum(c.count for c in cells), 2)
        self.assertEqual(
            [key[2] for key in self._mute_cell_keys()], ["env-x", "env-y"])
        # env-x's muted test now passes (still muted, but its cell is
        # different): a push into env-x only.
        self.store.upsert_runs([
            make_record(environment="env-x", test_name="a",
                        result=Result.PASS,
                        start=later + datetime.timedelta(hours=1))])
        self.assertEqual(
            [key[2] for key in self._mute_cell_keys()], ["env-y"])
        again = self._cells()
        self.assertEqual(
            [(c.environment, c.result, c.count) for c in again],
            [("env-x", Result.PASS, 1), ("env-y", Result.FAIL, 1)])
        self.assertEqual(
            [key[2] for key in self._mute_cell_keys()], ["env-x", "env-y"])
        self.assertEqual(self._statements(self._cells), [])

    # -- environment delete -------------------------------------------

    def test_deleting_an_environment_takes_its_mutes(self) -> None:
        for environment in (self.ENV, "UNKNOWN"):
            self.store.upsert_runs([
                make_record(environment=environment, test_name="f",
                            result=Result.FAIL)])
        self.store.mute_tests(
            [self._key("f"), self._key("f", env="UNKNOWN")],
            "why", 48, "alice", "bob", self.NOW)
        self.store.delete_environment("UNKNOWN")
        self.assertEqual(
            self._count("test_mutes", "environment = ?",
                        ["UNKNOWN"]), 0)
        self.assertEqual(
            self._count("mute_history", "environment = ?",
                        ["UNKNOWN"]), 0)
        self.assertEqual(
            self._count("test_mutes", "environment = ?",
                        [self.ENV]), 1)
        self.assertEqual(
            self._count("mute_history", "environment = ?",
                        [self.ENV]), 1)


class AssignedCellsTest(StorageTestBase):
    """WP-43: :meth:`Storage.assigned_cells`, the one statement behind
    both bar charts' ``assigned`` figure. Each rule of the design's
    B.1 (docs/design/charts-assigned-split-r0.md §2) is one test; the
    figure is checked against the Watch page's unassigned count, its
    bounds, and its memo."""

    NOW = BASE + datetime.timedelta(days=1)
    CUTOFF = BASE - datetime.timedelta(hours=1)
    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def setUp(self) -> None:
        super().setUp()
        fail = Result.FAIL
        self.store.upsert_runs([
            make_record(test_name="a_fail", result=fail),
            make_record(test_name="a_muted", result=fail),
            make_record(test_name="a_unassigned", result=fail),
            make_record(test_name="a_retired", result=fail),
            make_record(test_name="a_pass", result=Result.PASS),
            make_record(test_name="a_up", result=Result.UNEXPECTED_PASS),
            make_record(test_name="a_cleared", result=fail),
            make_record(script="other.py", test_name="o_fail", result=fail),
            make_record(environment="win-sim", test_name="w_fail",
                        result=fail),
            make_record(environment="win-sim", test_name="w_unassigned",
                        result=fail),
            # A build: assignments are the triple's, so a_fail is
            # assigned there too; a_unassigned passes there.
            make_record(test_name="a_fail", result=fail, build="b1",
                        start=BASE + datetime.timedelta(hours=2)),
            make_record(test_name="a_unassigned", result=Result.PASS,
                        build="b1",
                        start=BASE + datetime.timedelta(hours=2)),
        ])
        for env, script, name in (
                (self.ENV, self.SCRIPT, "a_fail"),
                (self.ENV, self.SCRIPT, "a_retired"),
                (self.ENV, self.SCRIPT, "a_pass"),
                (self.ENV, self.SCRIPT, "a_up"),
                (self.ENV, self.SCRIPT, "a_cleared"),
                (self.ENV, "other.py", "o_fail"),
                ("win-sim", self.SCRIPT, "w_fail")):
            self.store.set_assignee(
                env, script, name, "alice", "bob", CREATED)
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "a_cleared", None, "bob", CREATED)
        self.store.set_retired(
            self.ENV, self.SCRIPT, "a_retired", True, "bob", "gone",
            CREATED)
        self.store.mute_tests(
            [(self.ENV, self.SCRIPT, "a_muted", storage.MAINLINE_STREAM_ID)],
            "flaky rig", 72, "carol", "bob", self.NOW)
        self.build = self.store.list_streams("")[0].stream_id

    def _cells(self, **kwargs: Any) -> List[Tuple[str, str, bool, int]]:
        return sorted(
            (c.environment, c.script, c.muted, c.count)
            for c in self.store.assigned_cells(self.NOW, **kwargs))

    def _statements(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return [s for s in seen if s.strip().upper().startswith("SELECT")]

    def _summary(self, stream_id: int = storage.MAINLINE_STREAM_ID,
                 ) -> analytics.EstateSummary:
        return analytics.summarize_rollup(
            self.store.summary_rollup(self.CUTOFF, stream_id=stream_id),
            muted=self.store.muted_cells(
                self.NOW, self.CUTOFF, stream_id=stream_id),
            assigned=self.store.assigned_cells(
                self.NOW, stream_id=stream_id))

    # -- the rules ----------------------------------------------------

    def test_fail_assigned_unretired_split_by_live_mute(self) -> None:
        """a_fail counts; a_muted counts as muted (a mute assigns);
        a_unassigned, the retired, the passing, the unexpected pass and
        the cleared assignment do not."""
        self.assertEqual(self._cells(), [
            (self.ENV, "other.py", False, 1),
            (self.ENV, self.SCRIPT, False, 1),
            (self.ENV, self.SCRIPT, True, 1),
            ("win-sim", self.SCRIPT, False, 1),
        ])

    def test_an_expired_mute_counts_as_assigned(self) -> None:
        later = self.NOW + datetime.timedelta(hours=73)
        cells = sorted(
            (c.environment, c.script, c.muted, c.count)
            for c in self.store.assigned_cells(later))
        self.assertIn((self.ENV, self.SCRIPT, False, 2), cells)
        self.assertNotIn(True, [cell[2] for cell in cells])

    def test_a_build_reads_its_own_results_and_the_triples_assignee(
            self) -> None:
        self.assertEqual(
            self._cells(stream_id=self.build),
            [(self.ENV, self.SCRIPT, False, 1)])

    def test_the_scope_is_a_filter_of_the_stream(self) -> None:
        self.assertEqual(
            self._cells(environment="win-sim"),
            [("win-sim", self.SCRIPT, False, 1)])
        self.assertEqual(
            [cell[0] for cell in self._cells(environments=[self.ENV])],
            [self.ENV] * 3)
        self.assertEqual(self._cells(environments=[]), [])
        self.assertEqual(self._cells(environment="nowhere"), [])

    def test_failed_minus_assigned_is_watchs_unassigned_figure(
            self) -> None:
        """Watch counts FAIL, un-retired, unassigned, muted or not; it
        equals ``failed - assigned`` by the invariant "a live mute
        implies an assignee". Checked on mainline and on the build."""
        for stream_id in (storage.MAINLINE_STREAM_ID, self.build):
            watch = self.store.unassigned_failing_by_environment(stream_id)
            for rollup in self._summary(stream_id).by_environment:
                self.assertEqual(
                    rollup.failed - rollup.assigned,
                    watch.get(rollup.environment, 0),
                    (stream_id, rollup))
        linux = {r.environment: r for r in self._summary().by_environment}
        self.assertEqual(
            (linux[self.ENV].failed, linux[self.ENV].assigned,
             linux[self.ENV].muted), (4, 2, 1))

    def test_the_bounds(self) -> None:
        for stream_id in (storage.MAINLINE_STREAM_ID, self.build):
            for rollup in self._summary(stream_id).by_environment:
                self.assertGreaterEqual(rollup.assigned, 0)
                self.assertLessEqual(rollup.assigned, rollup.failed)
            by_script = {}  # type: Dict[Tuple[str, str], int]
            for cell in self.store.assigned_cells(
                    self.NOW, stream_id=stream_id):
                if not cell.muted:
                    key = (cell.environment, cell.script)
                    by_script[key] = by_script.get(key, 0) + cell.count
            scripts = self.store.top_failing_scripts(
                stream_id=stream_id, now=self.NOW)
            for entry in scripts:
                assigned = by_script.get(
                    (entry.environment, entry.script), 0)
                self.assertLessEqual(assigned, entry.failing, entry)
            self.assertLessEqual(
                set(by_script),
                set((e.environment, e.script) for e in scripts))

    # -- the cost -----------------------------------------------------

    def test_one_statement_cold_none_warm(self) -> None:
        """The headline pays one statement for both bar charts: the
        epoch and the environment names are already in hand there."""
        self.store.mute_epoch(storage.MAINLINE_STREAM_ID, self.NOW)
        self.store._stream_environment_names(storage.MAINLINE_STREAM_ID)
        cold = self._statements(lambda: self._cells())
        self.assertEqual(len(cold), 1, cold)
        self.assertIn(
            "FROM CURRENT_ASSIGNMENTS AS CA JOIN LATEST_RUNS",
            " ".join(cold[0].split()).upper())
        self.assertEqual(self._statements(lambda: self._cells()), [])
        self.assertEqual(
            self._statements(lambda: self._cells(environment="win-sim")),
            [])

    def test_no_live_mute_no_mute_join(self) -> None:
        self.store.unmute_tests(
            [(self.ENV, self.SCRIPT, "a_muted",
              storage.MAINLINE_STREAM_ID)], "bob", self.NOW)
        cold = self._statements(lambda: self._cells())
        own = [s for s in cold if "current_assignments" in s.lower()]
        self.assertEqual(len(own), 1, cold)
        self.assertNotIn("test_mutes", own[0].lower())
        self.store.mute_tests(
            [(self.ENV, self.SCRIPT, "a_muted", storage.MAINLINE_STREAM_ID)],
            "again", 24, "carol", "bob", self.NOW)
        own = [s for s in self._statements(lambda: self._cells())
               if "current_assignments" in s.lower()]
        self.assertEqual(len(own), 1)
        self.assertIn("test_mutes", own[0].lower())

    def test_an_assignment_is_seen_by_the_next_call(self) -> None:
        self.assertEqual(self._cells(environment="win-sim"),
                         [("win-sim", self.SCRIPT, False, 1)])
        self.store.set_assignee(
            "win-sim", self.SCRIPT, "w_unassigned", "dave", "bob", CREATED)
        self.assertEqual(self._cells(environment="win-sim"),
                         [("win-sim", self.SCRIPT, False, 2)])

    def test_a_push_drops_only_its_environments_cells(self) -> None:
        """Paired: the entry left served is still right afterwards."""
        self._cells()
        epoch = self.store.mute_epoch(storage.MAINLINE_STREAM_ID, self.NOW)
        keys = [("assigned_cells", storage.MAINLINE_STREAM_ID, env, epoch)
                for env in (self.ENV, "win-sim")]
        for key in keys:
            self.assertIsNotNone(self.store._cached_summary(key), key)
        self.store.upsert_runs([make_record(
            environment="win-sim", test_name="w_unassigned",
            result=Result.PASS, start=BASE + datetime.timedelta(hours=3))])
        self.assertIsNotNone(self.store._cached_summary(keys[0]))
        self.assertIsNone(self.store._cached_summary(keys[1]))
        served = self._cells()
        self.store._invalidate_summary_cache()
        self.assertEqual(served, self._cells())


class AssignedTrendTest(StorageTestBase):
    """WP-43: :meth:`Storage.daily_assigned_failures`, the trend's
    split. A day's FAIL runs are split by who owns each test NOW: an
    assignment recolours the past, an expiry recolours it back without
    a write, and the split never exceeds the day's FAIL."""

    D0 = BASE
    D1 = BASE + datetime.timedelta(days=1)
    D2 = BASE + datetime.timedelta(days=2)
    NOW = D2 + datetime.timedelta(hours=10)
    SINCE = datetime.datetime(2026, 7, 1)
    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def setUp(self) -> None:
        super().setUp()
        fail = Result.FAIL
        records = []  # type: List[RunRecord]
        for day, result in ((self.D0, fail), (self.D1, fail),
                            (self.D2, Result.PASS)):
            records.append(make_record(
                test_name="t_assigned", result=result, start=day))
        for day in (self.D0, self.D1, self.D2):
            records.append(make_record(
                test_name="t_muted", result=fail, start=day))
            records.append(make_record(
                test_name="t_nobody", result=fail, start=day))
        records.extend([
            make_record(test_name="t_retired", result=fail, start=self.D1),
            make_record(test_name="t_up", result=Result.UNEXPECTED_PASS,
                        start=self.D2),
            make_record(environment="win-sim", test_name="w_assigned",
                        result=fail, start=self.D2),
            make_record(test_name="t_assigned", result=fail, build="b1",
                        start=self.D2 + datetime.timedelta(hours=1)),
        ])
        self.store.upsert_runs(records)
        for env, name in ((self.ENV, "t_assigned"), (self.ENV, "t_retired"),
                          (self.ENV, "t_up"), ("win-sim", "w_assigned")):
            self.store.set_assignee(
                env, self.SCRIPT, name, "alice", "bob", CREATED)
        self.store.set_retired(
            self.ENV, self.SCRIPT, "t_retired", True, "bob", "gone",
            CREATED)
        self.store.mute_tests(
            [(self.ENV, self.SCRIPT, "t_muted", storage.MAINLINE_STREAM_ID)],
            "flaky", 2, "carol", "bob", self.NOW)
        self.build = self.store.list_streams("")[0].stream_id

    def _split(self, now: Optional[datetime.datetime] = None,
               **kwargs: Any) -> List[Tuple[str, int, int]]:
        return [
            (entry.day.isoformat(), entry.assigned, entry.muted)
            for entry in self.store.daily_assigned_failures(
                self.SINCE, now or self.NOW, **kwargs)]

    def _statements(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return [s for s in seen if s.strip().upper().startswith("SELECT")]

    def test_the_split(self) -> None:
        """t_assigned on D0 and D1; t_retired on D1 (no retirement
        filter: the day's FAIL has none); w_assigned on D2; t_muted
        every day; t_up and the build's run never."""
        self.assertEqual(self._split(), [
            ("2026-07-01", 1, 1),
            ("2026-07-02", 2, 1),
            ("2026-07-03", 1, 1),
        ])

    def test_disjoint_and_within_the_days_fail(self) -> None:
        for kwargs in ({}, {"environment": self.ENV},
                       {"stream_id": self.build}):
            fail = {
                entry.day.isoformat(): entry.count
                for entry in self.store.daily_result_counts(
                    self.SINCE, **kwargs)
                if entry.result is Result.FAIL}
            for day, assigned, muted in self._split(**kwargs):
                self.assertGreaterEqual(min(assigned, muted), 0)
                self.assertLessEqual(
                    assigned + muted, fail.get(day, 0), (kwargs, day))

    def test_an_assignment_recolours_the_next_call(self) -> None:
        self._split()
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "t_nobody", "dave", "bob", CREATED)
        self.assertEqual(self._split(), [
            ("2026-07-01", 2, 1),
            ("2026-07-02", 3, 1),
            ("2026-07-03", 2, 1),
        ])

    def test_an_expiry_recolours_without_a_write(self) -> None:
        """The mute was for 2 hours; past it the muted runs are the
        owner's assigned ones (a mute assigns)."""
        self._split()
        later = self.NOW + datetime.timedelta(hours=3)
        self.assertEqual(self._split(later), [
            ("2026-07-01", 2, 0),
            ("2026-07-02", 3, 0),
            ("2026-07-03", 2, 0),
        ])

    def test_the_scope(self) -> None:
        self.assertEqual(self._split(environment="win-sim"),
                         [("2026-07-03", 1, 0)])
        self.assertEqual(
            [day[1] for day in self._split(environments=[self.ENV])],
            [1, 2, 0])
        self.assertEqual(self._split(environments=[]), [])
        self.assertEqual(self._split(stream_id=self.build),
                         [("2026-07-03", 1, 0)])

    def test_one_statement_cold_none_warm(self) -> None:
        self.store.mute_epoch(storage.MAINLINE_STREAM_ID, self.NOW)
        cold = self._statements(lambda: self._split())
        self.assertEqual(len(cold), 1, cold)
        self.assertEqual(self._statements(lambda: self._split()), [])

    def test_no_live_mute_no_mute_join(self) -> None:
        later = self.NOW + datetime.timedelta(hours=3)
        own = [s for s in self._statements(lambda: self._split(later))
               if "CROSS JOIN" in s.upper()]
        self.assertEqual(len(own), 1)
        self.assertNotIn("test_mutes", own[0].lower())
        own = [s for s in self._statements(lambda: self._split())
               if "CROSS JOIN" in s.upper()]
        self.assertEqual(len(own), 1)
        self.assertIn("test_mutes", own[0].lower())

    def test_a_push_drops_it(self) -> None:
        self._split()
        self.store.upsert_runs([make_record(
            test_name="t_assigned", result=Result.FAIL,
            start=self.D2 + datetime.timedelta(hours=5))])
        self.assertEqual(self._split()[-1], ("2026-07-03", 2, 1))

    def test_the_plan_starts_from_the_assignments(self) -> None:
        """The CROSS JOIN is load-bearing: as a plain JOIN SQLite drove
        it from idx_runs_start_time_result, a scan of the window of
        runs. SQLite's planner, SQLite's words: excluded from the
        MariaDB variants by name (WP-43 decision 12)."""
        for now in (self.NOW, self.NOW + datetime.timedelta(hours=3)):
            self.store._invalidate_summary_cache()
            statement = [
                s for s in self._statements(lambda: self._split(now))
                if "CROSS JOIN" in s.upper()][0]
            plan = [
                str(row[-1]).upper() for row in self.store._conn().execute(
                    "EXPLAIN QUERY PLAN " + statement).fetchall()]
            steps = [line for line in plan
                     if line.startswith(("SCAN", "SEARCH"))]
            self.assertTrue(steps[0].startswith("SCAN"), plan)
            self.assertTrue(
                " CA" in steps[0] or "CURRENT_ASSIGNMENTS" in steps[0], plan)
            for line in plan:
                self.assertNotIn("IDX_RUNS_START_TIME_RESULT", line, plan)
            runs = [line for line in steps
                    if " R " in line + " " or " AS R " in line + " "]
            self.assertTrue(runs, plan)
            for line in runs:
                self.assertTrue(line.startswith("SEARCH"), plan)


class FlakyMarkWriteTest(StorageTestBase):
    """WP-44: the flaky mark's two acts (migration 12).

    A mark is per TRIPLE and covers every stream; the person marking
    owns it and nobody else can be named; a cadence change keeps the
    owner; anyone known and active may change or remove a mark; every
    act is a history row; the reason is a comment tagged with the stream
    the selection was made on; a dropped environment takes its marks,
    a retirement or a build's deletion does not.
    """

    NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
    LATER = NOW + datetime.timedelta(hours=1)
    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def setUp(self) -> None:
        super().setUp()
        later = BASE + datetime.timedelta(hours=1)
        self.store.upsert_runs([
            make_record(test_name="t1", result=Result.FAIL),
            make_record(test_name="t1", result=Result.FAIL, start=later),
            make_record(test_name="t2", result=Result.PASS, start=later),
            make_record(test_name="t3", result=Result.FAIL, start=later),
            make_record(test_name="t1", result=Result.FAIL, build="b1",
                        start=later + datetime.timedelta(hours=1)),
            make_record(environment="win-sim", test_name="w1",
                        result=Result.FAIL, start=later),
        ])
        for name in ("alice", "bob", "carol"):
            self.store.ensure_user(name, CREATED)
        self.store.set_user_active("carol", False, "bob", CREATED)
        self.build = self.store.list_streams("")[0].stream_id

    def _key(self, test: str, sid: Optional[int] = None,
             env: Optional[str] = None
             ) -> Tuple[str, str, str, Optional[int]]:
        return (env or self.ENV, self.SCRIPT, test, sid)

    def _mark(self, tests: Sequence[str], cadence: int = 3,
              reason: Optional[str] = "race in teardown",
              by: str = "alice", sid: Optional[int] = None,
              when: Optional[datetime.datetime] = None,
              ) -> Tuple[int, int, int, int]:
        result = self.store.mark_flaky(
            [self._key(t, sid) for t in tests], cadence, reason, by,
            when or self.NOW)
        return (result.marked, result.changed, result.unchanged,
                result.unknown)

    def _acts(self, test: str, env: Optional[str] = None
              ) -> "storage.TestActs":
        return self.store.test_acts(env or self.ENV, self.SCRIPT, test)

    def _count(self, table: str) -> int:
        return int(self.store._conn().execute(
            "SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0])

    def _written(self) -> Tuple[int, int, int, int]:
        return (self._count("test_flaky_marks"),
                self._count("flaky_mark_history"),
                self._count("comments"), self._count("users"))

    # -- marking ----------------------------------------------------

    def test_a_fresh_mark_is_recorded_owned_and_commented(self) -> None:
        self.assertEqual(self._mark(["t1"], cadence=3), (1, 0, 0, 0))
        acts = self._acts("t1")
        self.assertEqual(
            acts.flaky_mark,
            storage.FlakyMark(
                environment=self.ENV, script=self.SCRIPT, test_name="t1",
                cadence=3, reason="race in teardown", marked_by="alice",
                marked_at=self.NOW))
        self.assertEqual(
            [(a.action, a.cadence, a.reason, a.actor, a.acted_at)
             for a in acts.flaky_history],
            [("mark", 3, "race in teardown", "alice", self.NOW)])
        comments = self.store.comments(self.ENV, self.SCRIPT, "t1")
        self.assertEqual(
            [(c.author, c.text, c.stream_id, c.created_at)
             for c in comments],
            [("alice", "race in teardown", None, self.NOW)])
        # A mark is not an assignment.
        self.assertIsNone(
            self.store.current_assignee(self.ENV, self.SCRIPT, "t1"))

    def test_the_comment_is_tagged_with_the_stream_it_was_made_from(
        self,
    ) -> None:
        self._mark(["t1"], sid=self.build)
        self._mark(["t3"], sid=storage.MAINLINE_STREAM_ID)
        self.assertEqual(
            [c.stream_id for c in self.store.comments(
                self.ENV, self.SCRIPT, "t1")], [self.build])
        self.assertEqual(
            [c.stream_id for c in self.store.comments(
                self.ENV, self.SCRIPT, "t3")], [None])
        # The mark itself names no stream: it covers them all.
        self.assertEqual(self._acts("t1").flaky_mark.cadence, 3)

    def test_a_fresh_mark_needs_a_reason_and_writes_nothing_without(
        self,
    ) -> None:
        self._mark(["t2"])
        before = self._written()
        for reason in (None, "", "   "):
            with self.assertRaises(ValueError) as caught:
                self._mark(["t1", "t2"], cadence=4, reason=reason)
            self.assertTrue(
                str(caught.exception).startswith("reason is required"),
                caught.exception)
            self.assertIn("1 of these tests is not yet marked",
                          str(caught.exception))
        self.assertEqual(self._written(), before)
        self.assertEqual(self._acts("t2").flaky_mark.cadence, 3)

    def test_a_cadence_change_keeps_the_owner_and_needs_no_reason(
        self,
    ) -> None:
        self._mark(["t1"], cadence=3, by="alice")
        comments = len(self.store.comments(self.ENV, self.SCRIPT, "t1"))
        self.assertEqual(
            self._mark(["t1"], cadence=5, reason=None, by="bob",
                       when=self.LATER),
            (0, 1, 0, 0))
        mark = self._acts("t1").flaky_mark
        self.assertEqual(
            (mark.cadence, mark.marked_by, mark.marked_at, mark.reason),
            (5, "alice", self.NOW, "race in teardown"))
        self.assertEqual(
            [(a.action, a.cadence, a.reason, a.actor)
             for a in self._acts("t1").flaky_history],
            [("cadence", 5, None, "bob"),
             ("mark", 3, "race in teardown", "alice")])
        self.assertEqual(
            len(self.store.comments(self.ENV, self.SCRIPT, "t1")), comments)

    def test_a_cadence_change_with_a_reason_replaces_it_and_posts_it(
        self,
    ) -> None:
        self._mark(["t1"], cadence=3, by="alice")
        self._mark(["t1"], cadence=2, reason="worse now", by="bob",
                   sid=self.build, when=self.LATER)
        mark = self._acts("t1").flaky_mark
        self.assertEqual((mark.cadence, mark.marked_by, mark.reason),
                         (2, "alice", "worse now"))
        last = self.store.comments(self.ENV, self.SCRIPT, "t1")[-1]
        self.assertEqual((last.author, last.text, last.stream_id),
                         ("bob", "worse now", self.build))

    def test_the_same_cadence_writes_nothing(self) -> None:
        self._mark(["t1"], cadence=3)
        before = self._written()
        self.assertEqual(self._mark(["t1"], cadence=3, reason="again"),
                         (0, 0, 1, 0))
        self.assertEqual(self._written(), before)

    def test_one_selection_counts_every_case_once(self) -> None:
        self._mark(["t1", "t2"], cadence=3)
        result = self._mark(
            ["t1", "t2", "t3", "t3", "nope"], cadence=4, by="bob")
        self.assertEqual(result, (1, 2, 0, 1))
        result = self._mark(["t1", "t2", "t3"], cadence=4, by="bob")
        self.assertEqual(result, (0, 0, 3, 0))
        self.assertEqual(self._count("test_flaky_marks"), 3)

    def test_a_test_named_from_two_streams_is_one_mark_one_comment(
        self,
    ) -> None:
        result = self.store.mark_flaky(
            [self._key("t1", self.build),
             self._key("t1", storage.MAINLINE_STREAM_ID)],
            3, "why", "alice", self.NOW)
        self.assertEqual(result.marked, 1)
        self.assertEqual(
            [c.stream_id for c in self.store.comments(
                self.ENV, self.SCRIPT, "t1")], [self.build])
        self.assertEqual(len(self._acts("t1").flaky_history), 1)

    def test_the_cadence_is_two_to_ten(self) -> None:
        for bad in (1, 11, 0, -3, True, "3", 2.5, None):
            with self.assertRaises(ValueError) as caught:
                self.store.mark_flaky(
                    [self._key("t1")], bad, "why", "alice",  # type: ignore
                    self.NOW)
            self.assertTrue(str(caught.exception).startswith("cadence:"))
        self.assertEqual(self._count("test_flaky_marks"), 0)
        self.assertEqual(self._mark(["t1"], cadence=2), (1, 0, 0, 0))
        self.assertEqual(self._mark(["t1"], cadence=10), (0, 1, 0, 0))

    def test_an_unknown_or_deactivated_user_cannot_act(self) -> None:
        self._mark(["t1"])
        before = self._written()
        for who in ("nobody", "carol"):
            with self.assertRaises(ValueError) as caught:
                self._mark(["t3"], by=who)
            self.assertTrue(str(caught.exception).startswith("username:"))
            with self.assertRaises(ValueError):
                self._mark(["t1"], cadence=5, by=who)
            with self.assertRaises(ValueError):
                self.store.unmark_flaky(
                    [(self.ENV, self.SCRIPT, "t1")], who, self.NOW)
        # Nothing written, and nobody created.
        self.assertEqual(self._written(), before)
        self.assertIsNone(self.store.get_user("nobody"))

    # -- unmarking ----------------------------------------------------

    def test_anyone_active_may_unmark_and_it_is_recorded(self) -> None:
        self._mark(["t1", "t3"], cadence=4, by="alice")
        removed = self.store.unmark_flaky(
            [(self.ENV, self.SCRIPT, "t1"), (self.ENV, self.SCRIPT, "t1"),
             (self.ENV, self.SCRIPT, "t2")], "bob", self.LATER)
        self.assertEqual(removed, 1)
        acts = self._acts("t1")
        self.assertIsNone(acts.flaky_mark)
        self.assertEqual(
            [(a.action, a.cadence, a.reason, a.actor, a.acted_at)
             for a in acts.flaky_history],
            [("unmark", 4, None, "bob", self.LATER),
             ("mark", 4, "race in teardown", "alice", self.NOW)])
        self.assertIsNotNone(self._acts("t3").flaky_mark)
        history = self._count("flaky_mark_history")
        self.assertEqual(self.store.unmark_flaky(
            [(self.ENV, self.SCRIPT, "t1")], "bob", self.LATER), 0)
        self.assertEqual(self._count("flaky_mark_history"), history)

    # -- what the acts drop, and what they leave --------------------------

    def test_every_act_drops_the_memos_and_no_push_cost_is_added(
        self,
    ) -> None:
        def probe() -> None:
            self.store._store_summary(("probe",), 1, 1)

        probe()
        self._mark(["t1"])
        self.assertIsNone(self.store._cached_summary(("probe",)))
        probe()
        self._mark(["t1"], cadence=5)
        self.assertIsNone(self.store._cached_summary(("probe",)))
        probe()
        self.store.unmark_flaky([(self.ENV, self.SCRIPT, "t1")], "bob",
                                self.NOW)
        self.assertIsNone(self.store._cached_summary(("probe",)))
        # An act that writes nothing drops nothing.
        probe()
        self.store.unmark_flaky([(self.ENV, self.SCRIPT, "t1")], "bob",
                                self.NOW)
        self.assertEqual(self.store._cached_summary(("probe",)), 1)

    def test_a_push_leaves_the_mark_alone(self) -> None:
        self._mark(["t1"])
        self.store.upsert_runs([make_record(
            test_name="t1", result=Result.PASS,
            start=BASE + datetime.timedelta(days=1))])
        self.assertEqual(self._acts("t1").flaky_mark.cadence, 3)

    def test_retiring_a_marked_test_keeps_the_mark(self) -> None:
        self._mark(["t1"])
        self.store.set_retired(self.ENV, self.SCRIPT, "t1", True, "bob",
                               "gone", self.LATER)
        self.assertEqual(self._acts("t1").flaky_mark.cadence, 3)
        self.store.set_retired(self.ENV, self.SCRIPT, "t1", False, "bob",
                               "back", self.LATER)
        self.assertEqual(self._acts("t1").flaky_mark.cadence, 3)

    def test_deleting_a_build_touches_no_mark(self) -> None:
        self._mark(["t1"], sid=self.build)
        self.store.delete_stream_environment(self.build, self.ENV)
        self.assertEqual(self._acts("t1").flaky_mark.cadence, 3)
        self.store.delete_stream(self.build)
        acts = self._acts("t1")
        self.assertEqual(acts.flaky_mark.cadence, 3)
        self.assertEqual(len(acts.flaky_history), 1)

    def test_dropping_an_environment_takes_its_marks_and_history(
        self,
    ) -> None:
        self._mark(["t1"])
        self.store.mark_flaky(
            [("win-sim", self.SCRIPT, "w1", None)], 3, "why", "alice",
            self.NOW)
        self.store.unmark_flaky([("win-sim", self.SCRIPT, "w1")], "bob",
                                self.NOW)
        self.store.mark_flaky(
            [("win-sim", self.SCRIPT, "w1", None)], 2, "again", "alice",
            self.LATER)
        self.store.delete_environment("win-sim")
        acts = self._acts("w1", env="win-sim")
        self.assertIsNone(acts.flaky_mark)
        self.assertEqual(acts.flaky_history, [])
        kept = self._acts("t1")
        self.assertEqual(kept.flaky_mark.cadence, 3)
        self.assertEqual(len(kept.flaky_history), 1)

    def test_the_acts_read_returns_mutes_and_marks_apart(self) -> None:
        self.store.mute_tests(
            [(self.ENV, self.SCRIPT, "t1", storage.MAINLINE_STREAM_ID)],
            "infra", 24, "alice", "bob", self.NOW)
        self._mark(["t1"])
        self._mark(["t1"], cadence=6, when=self.LATER)
        acts = self._acts("t1")
        self.assertEqual([a.action for a in acts.mute_history], ["mute"])
        self.assertEqual(
            acts.mute_history,
            self.store.mute_history(self.ENV, self.SCRIPT, "t1"))
        self.assertEqual([a.action for a in acts.flaky_history],
                         ["cadence", "mark"])
        self.assertGreater(acts.flaky_history[0].id,
                           acts.flaky_history[1].id)
        self.assertEqual(acts.flaky_mark.cadence, 6)
        empty = self._acts("t2")
        self.assertEqual(
            (empty.mute_history, empty.flaky_mark, empty.flaky_history),
            ([], None, []))


class FlakyActStatementsTest(StorageTestBase):
    """WP-44 item 6, carried from item 3's measured pass: an unmark and a
    cadence change of many tests send a number of statements that grows
    by one per chunk of 100 tests, never by one per test.

    On MariaDB the count is the server's own (``Questions``, the
    statements the session sent): the vendored driver batches only an
    INSERT whose VALUES are all placeholders, so an executemany of a
    DELETE or an UPDATE was a statement per test (204 and 105 for 100
    tests before the fix). On SQLite, where every statement is in
    process, the trace pins the same shape: the DELETE and the UPDATE
    once per chunk (the history INSERT's executemany traces per row
    there, and is one statement on the server)."""

    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def setUp(self) -> None:
        super().setUp()
        self.store.ensure_user("alice", CREATED)
        self.store.upsert_runs([
            make_record(test_name="t{0:03d}".format(i), result=Result.FAIL)
            for i in range(205)])

    def _tests(self, count: int) -> List[Tuple[str, str, str]]:
        return [(self.ENV, self.SCRIPT, "t{0:03d}".format(i))
                for i in range(count)]

    def _mark(self, count: int, cadence: int) -> None:
        self.store.mark_flaky(
            [t + (None,) for t in self._tests(count)], cadence, None
            if cadence != 3 else "why", "alice", CREATED)

    def _counted(self, call: Callable[[], Any]) -> Tuple[int, List[str]]:
        """On MariaDB ``(server statements, [])``; on SQLite ``(0, the
        traced statements)``."""
        conn = self.store._conn()
        if isinstance(self.store._backend, storage._SqliteBackend):
            seen = []  # type: List[str]
            trace_sql_into(conn, seen)
            try:
                call()
            finally:
                conn.set_trace_callback(None)
            return 0, seen

        def questions() -> int:
            row = conn.execute(
                "SHOW SESSION STATUS LIKE 'Questions'").fetchone()
            return int(row[1])

        before = questions()
        call()
        # The second SHOW counts itself.
        return questions() - before - 1, []

    def _check(self, count: int, call: Callable[[], Any],
               verb: str) -> None:
        chunks = (count + 99) // 100
        server, traced = self._counted(call)
        if traced:
            verbs = [s for s in traced if s.lstrip().upper().startswith(verb)]
            self.assertEqual(len(verbs), chunks, verbs)
            others = [s for s in traced
                      if not s.lstrip().upper().startswith(("INSERT", verb))]
            # BEGIN, the user check, a SELECT per chunk, COMMIT.
            self.assertEqual(len(others), 3 + chunks, others)
            return
        # START TRANSACTION, the user check, a SELECT and a write per
        # chunk, the history INSERT as one statement, COMMIT.
        self.assertEqual(server, 4 + 2 * chunks, (count, verb))

    def test_an_unmark_is_a_statement_per_chunk(self) -> None:
        for count in (100, 205):
            self._mark(count, 3)
            self._check(count, lambda: self.store.unmark_flaky(
                self._tests(count), "alice", CREATED), "DELETE")
            self.assertEqual(int(self.store._conn().execute(
                "SELECT COUNT(*) FROM test_flaky_marks").fetchone()[0]), 0)
        unmarks = self.store._conn().execute(
            "SELECT COUNT(*), MIN(action), MAX(action), MIN(cadence), "
            "MAX(reason) FROM flaky_mark_history WHERE action = ?",
            ("unmark",)).fetchone()
        self.assertEqual(tuple(unmarks), (305, "unmark", "unmark", 3, None))

    def test_a_cadence_change_is_a_statement_per_chunk(self) -> None:
        for count, cadence in ((100, 4), (205, 5)):
            self._mark(205, 3)
            self._check(count, lambda: self._mark(count, cadence), "UPDATE")
            rows = self.store._conn().execute(
                "SELECT cadence, COUNT(*) FROM test_flaky_marks "
                "GROUP BY cadence ORDER BY cadence").fetchall()
            expected = [(cadence, count)]
            if count < 205:
                expected.insert(0, (3, 205 - count))
            self.assertEqual([tuple(r) for r in rows], expected)
            self.store.unmark_flaky(self._tests(205), "alice", CREATED)
        # The owner and the reason stay; the history says what moved.
        changes = self.store._conn().execute(
            "SELECT COUNT(*) FROM flaky_mark_history WHERE action = ? "
            "AND reason IS NULL", ("cadence",)).fetchone()
        self.assertEqual(int(changes[0]), 305)


class FlakyCadenceTest(StorageTestBase):
    """WP-44: the cadence rule (design §2 B.1), one pure function, and
    Q2, the bounded runs read it is fed from (:meth:`Storage.cadence_runs`).

    Every row of B.1's table; each non-FAIL result inside the window
    and just outside it; the floor (a stream with fewer runs than the
    cadence is failing only when every run it has failed) and Q2's
    floor bound on builds; Q2 over more than one statement's worth of
    tests, so the MariaDB variant executes a full statement and a
    second one (decision 22).
    """

    F = Result.FAIL
    P = Result.PASS
    UP = Result.UNEXPECTED_PASS
    FAE = Result.FAILED_AS_EXPECTED
    NON_FAIL = (Result.PASS, Result.UNEXPECTED_PASS,
                Result.FAILED_AS_EXPECTED)
    ENV = "linux-sim"
    SCRIPT = "suite.py"

    def _state(self, cadence: int, results: Sequence[Result]
               ) -> Optional[Tuple[str, int, Optional[int]]]:
        state = analytics.flaky_state(cadence, results)
        return None if state is None else tuple(state)  # type: ignore

    # -- the pure rule -------------------------------------------------

    def test_every_row_of_the_table(self) -> None:
        F, P = self.F, self.P
        for cadence in range(2, 11):
            # Latest not FAIL: passing, whatever came before.
            for latest in self.NON_FAIL:
                for history in ([], [F], [P], [F] * 12):
                    self.assertEqual(
                        self._state(cadence, [latest] + history),
                        ("passing", 0, None), (cadence, latest, history))
            # FAIL with no previous run: failing (verdict 8), 1 of 1.
            self.assertEqual(self._state(cadence, [F]), ("failing", 1, 1))
            # FAIL after a non-FAIL: flaking.
            for prev in self.NON_FAIL:
                self.assertEqual(self._state(cadence, [F, prev, F, F]),
                                 ("flaking", 1, None))
            # No run on the stream: absent there.
            self.assertIsNone(self._state(cadence, []))
        # FAIL, FAIL at cadence 2: failing, 2 of 2.
        self.assertEqual(self._state(2, [F, F, P]), ("failing", 2, 2))
        self.assertEqual(self._state(2, [F, F]), ("failing", 2, 2))
        # FAIL, FAIL at 3..10: flaking while one of the newest N is not
        # a FAIL, failing once all N are; streak is the leading FAILs.
        for cadence in range(3, 11):
            for streak in range(2, cadence):
                results = [F] * streak + [P] + [F] * 10
                self.assertEqual(self._state(cadence, results),
                                 ("flaking", streak, None))
            self.assertEqual(self._state(cadence, [F] * cadence + [P]),
                             ("failing", cadence, cadence))
            # More FAILs than N: capped at N.
            self.assertEqual(self._state(cadence, [F] * (cadence + 5)),
                             ("failing", cadence, cadence))

    def test_each_non_fail_inside_the_window_keeps_it_flaking(self) -> None:
        for cadence in range(2, 11):
            for other in self.NON_FAIL:
                for position in range(1, cadence):
                    results = [self.F] * (cadence + 1)
                    results[position] = other
                    state = self._state(cadence, results)
                    self.assertEqual(
                        state, ("flaking", position, None),
                        (cadence, other, position))
                # Just outside the window it no longer counts.
                results = [self.F] * cadence + [other]
                self.assertEqual(self._state(cadence, results),
                                 ("failing", cadence, cadence))

    def test_fewer_runs_than_the_cadence_fail_only_if_all_failed(
        self,
    ) -> None:
        for cadence in range(2, 11):
            for count in range(1, cadence):
                self.assertEqual(self._state(cadence, [self.F] * count),
                                 ("failing", count, count))
                if count > 1:
                    results = [self.F] * (count - 1) + [self.UP]
                    self.assertEqual(self._state(cadence, results),
                                     ("flaking", count - 1, None))

    def test_the_latest_two_decide_all_but_fail_fail_at_three_or_more(
        self,
    ) -> None:
        results = list(Result)
        for cadence in range(2, 11):
            for latest in results:
                for prev in results + [None]:  # type: ignore
                    if (cadence >= 3 and latest is self.F
                            and prev is self.F):
                        continue
                    # The latest two decide it, whatever came before.
                    pair = [latest] if prev is None else [latest, prev]
                    for older in ([], [self.F] * 10, [self.P] * 10):
                        full = pair + (older if prev is not None else [])
                        self.assertEqual(
                            self._state(cadence, pair),
                            self._state(cadence, full),
                            (cadence, latest, prev, older))

    def test_a_cadence_outside_two_to_ten_is_refused(self) -> None:
        for bad in (0, 1, 11):
            with self.assertRaises(ValueError):
                analytics.flaky_state(bad, [self.F])

    # -- Q2: the bounded runs read ------------------------------------

    def _night(self, day: int) -> datetime.datetime:
        return BASE + datetime.timedelta(days=day)

    def _seed_history(self) -> None:
        """t_hist on mainline: ten nights, newest first F F P F F F F F
        F F; a build that ran it twice (FAIL, FAIL) after mainline's
        sixth night; t_other untouched by the build."""
        nightly = [self.F, self.F, self.F, self.F, self.F, self.F, self.F,
                   self.P, self.F, self.F]  # oldest first
        records = [
            make_record(test_name="t_hist", result=result,
                        start=self._night(day))
            for day, result in enumerate(nightly)
        ]
        records.append(make_record(test_name="t_other", result=self.P,
                                   start=self._night(3)))
        records.extend([
            make_record(test_name="t_hist", result=self.F, build="b1",
                        start=self._night(5) + datetime.timedelta(hours=2)),
            make_record(test_name="t_hist", result=self.F, build="b1",
                        start=self._night(6) + datetime.timedelta(hours=2)),
        ])
        self.store.upsert_runs(records)

    def test_q2_reads_the_newest_n_no_later_than_the_latest_start(
        self,
    ) -> None:
        self._seed_history()
        mainline = storage.MAINLINE_STREAM_ID
        triple = (self.ENV, self.SCRIPT, "t_hist")
        got = self.store.cadence_runs(
            mainline, [triple + (self._night(9), 4)])
        self.assertEqual(got, {triple: [self.F, self.F, self.P, self.F]})
        # No later than the latest start the caller holds.
        got = self.store.cadence_runs(
            mainline, [triple + (self._night(6), 3)])
        self.assertEqual(got, {triple: [self.F, self.F, self.F]})
        # A test with no run on the stream is in the answer, empty.
        missing = (self.ENV, self.SCRIPT, "t_missing")
        other = (self.ENV, self.SCRIPT, "t_other")
        got = self.store.cadence_runs(
            mainline, [missing + (self._night(9), 3),
                       other + (self._night(9), 3),
                       triple + (self._night(9), 2),
                       triple + (self._night(9), 9)])
        self.assertEqual(got, {missing: [], other: [self.P],
                               triple: [self.F, self.F]})
        self.assertEqual(self.store.cadence_runs(mainline, []), {})
        with self.assertRaises(ValueError):
            self.store.cadence_runs(mainline, [triple + (self._night(9), 0)])

    def test_q2_reads_one_stream_and_the_floor_bounds_a_build(self) -> None:
        self._seed_history()
        build = self.store.list_streams("")[0]
        triple = (self.ENV, self.SCRIPT, "t_hist")
        latest = self._night(6) + datetime.timedelta(hours=2)
        without = self.store.cadence_runs(
            build.stream_id, [triple + (latest, 5)])
        with_floor = self.store.cadence_runs(
            build.stream_id, [triple + (latest, 5)], build.first_seen)
        # The floor never changes the answer: the build's own two runs.
        self.assertEqual(without, {triple: [self.F, self.F]})
        self.assertEqual(with_floor, without)
        self.assertEqual(build.first_seen,
                         self._night(5) + datetime.timedelta(hours=2))
        # ...and is applied: a floor past the first run cuts it.
        cut = self.store.cadence_runs(
            build.stream_id, [triple + (latest, 5)], latest)
        self.assertEqual(cut, {triple: [self.F]})
        # A build that ran a test fewer times than the cadence, every
        # run a FAIL, is over it (verdict 8); mainline is flaking.
        self.assertEqual(
            self._state(5, with_floor[triple]), ("failing", 2, 2))
        mainline = self.store.cadence_runs(
            storage.MAINLINE_STREAM_ID, [triple + (self._night(9), 5)])
        self.assertEqual(self._state(5, mainline[triple]),
                         ("flaking", 2, None))

    def _seed_many(self, count: int) -> List[Tuple[str, str, str]]:
        """*count* tests, three nights each: PASS, FAIL, FAIL."""
        records = []  # type: List[RunRecord]
        names = ["t{0:03d}".format(index) for index in range(count)]
        for name in names:
            for day, result in enumerate((self.P, self.F, self.F)):
                records.append(make_record(
                    test_name=name, result=result, start=self._night(day)))
        self.store.upsert_runs(records)
        return [(self.ENV, self.SCRIPT, name) for name in names]

    def test_q2_over_more_than_one_statement(self) -> None:
        """Decision 22: 105 tests is a full statement of 100 and a
        second of 5; on MariaDB both run, parenthesised."""
        triples = self._seed_many(105)
        got = self.store.cadence_runs(
            storage.MAINLINE_STREAM_ID,
            [t + (self._night(2), 3) for t in triples])
        self.assertEqual(len(got), 105)
        for triple in triples:
            self.assertEqual(got[triple], [self.F, self.F, self.P], triple)
            self.assertEqual(self._state(3, got[triple]),
                             ("flaking", 2, None))
            self.assertEqual(self._state(2, got[triple]),
                             ("failing", 2, 2))

    def test_q2_costs_one_statement_per_hundred_tests(self) -> None:
        triples = self._seed_many(205)
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            self.store.cadence_runs(
                storage.MAINLINE_STREAM_ID,
                [t + (self._night(2), 3) for t in triples])
            self.store.cadence_runs(
                storage.MAINLINE_STREAM_ID,
                [triples[0] + (self._night(2), 3)])
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in seen
                   if s.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 3 + 1, selects)
        self.assertEqual(selects[0].count("UNION ALL"), 99)
        self.assertEqual(selects[2].count("UNION ALL"), 4)
        self.assertNotIn("UNION", selects[3])

    def _q2_sql(self, members: int, floor: bool) -> Tuple[str, List[Any]]:
        """Q2's statement as cadence_runs builds it, with its params,
        captured through the connection rather than re-spelled here."""
        captured = []  # type: List[Tuple[str, List[Any]]]
        store = self.store
        real = store._conn

        class Spy(object):
            def __init__(self, conn: Any) -> None:
                self._conn = conn

            def execute(self, sql: str, params: Any = ()) -> Any:
                captured.append((sql, list(params)))
                return self._conn.execute(sql, params)

        store._conn = lambda: Spy(real())  # type: ignore
        try:
            triples = [(self.ENV, self.SCRIPT, "t{0:03d}".format(i))
                       for i in range(members)]
            store.cadence_runs(
                storage.MAINLINE_STREAM_ID,
                [t + (self._night(2), 3) for t in triples],
                self._night(0) if floor else None)
        finally:
            del store._conn  # type: ignore
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_q2_is_a_runs_unique_range_on_both_backends(self) -> None:
        """The plan guard (design §6): every member reads runs by its
        UNIQUE (environment, script, test_name, start_time) index, never
        by the start-time index and never by a scan. On SQLite by
        EXPLAIN QUERY PLAN, on MariaDB by EXPLAIN; CI's 10.3 legs are
        where production's plan is checked."""
        self._seed_many(105)
        mariadb = not isinstance(self.store._backend, storage._SqliteBackend)
        for members, floor in ((1, False), (3, False), (3, True),
                               (100, False)):
            sql, params = self._q2_sql(members, floor)
            if mariadb:
                self.assertTrue(sql.startswith("(") or members == 1, sql)
                rows = self.store._conn().execute(
                    "EXPLAIN " + sql, params).fetchall()
                runs_rows = [row for row in rows if row[2] == "runs"]
                self.assertEqual(len(runs_rows), members, rows)
                for row in runs_rows:
                    self.assertEqual(row[5], "uq_runs_identity", rows)
                continue
            plan = [str(row[-1]).upper() for row in self.store._conn().execute(
                "EXPLAIN QUERY PLAN " + sql, params).fetchall()]
            reads = [line for line in plan
                     if line.startswith(("SCAN", "SEARCH"))
                     and " RUNS" in line]
            self.assertEqual(len(reads), members, plan)
            for line in reads:
                self.assertTrue(line.startswith("SEARCH"), plan)
                self.assertIn("SQLITE_AUTOINDEX_RUNS_1", line, plan)
                self.assertNotIn("IDX_RUNS_START_TIME_RESULT", line, plan)

    def test_the_marks_reads_are_driven_by_their_keys(self) -> None:
        """Every statement on the marks table pins its driving side
        (design §6): the acts' existence read walks latest_runs by the
        triple index and reaches the mark by its key; the detail's acts
        read is three key reads. SQLite's plan (excluded on MariaDB)."""
        if not isinstance(self.store._backend, storage._SqliteBackend):
            self.skipTest("SQLite's planner")
        self._seed_many(3)
        seen = []  # type: List[str]
        conn = self.store._conn()
        self.store.ensure_user("alice", CREATED)
        trace_sql_into(conn, seen)
        try:
            self.store.mark_flaky(
                [(self.ENV, self.SCRIPT, "t000", None),
                 (self.ENV, self.SCRIPT, "t001", None)],
                3, "why", "alice", BASE)
            self.store.test_acts(self.ENV, self.SCRIPT, "t000")
        finally:
            conn.set_trace_callback(None)
        reads = [s for s in seen if s.strip().upper().startswith("SELECT")
                 and "flaky" in s.lower()]
        self.assertEqual(len(reads), 2, reads)
        for statement in reads:
            plan = [str(row[-1]).upper() for row in conn.execute(
                "EXPLAIN QUERY PLAN " + statement).fetchall()]
            for line in plan:
                self.assertFalse(line.startswith("SCAN"), plan)
            self.assertTrue(
                any("TEST_FLAKY_MARKS" in line and "INDEX" in line
                    for line in plan), plan)


class FlakyEstateBase(StorageTestBase):
    """WP-44 item 6: an estate with a marked test in every state the
    board acts on, and the unmarked tests beside them.

    Mainline, ``linux-sim``/``suite.py``, one run a night, the last on
    night 9; each history below is OLDEST FIRST. Cadence 3 unless named.
    ``env-many`` holds 101 more marked tests (decision 22: so Q2 runs a
    full statement and a second one, and the New failures queue binds
    more than 100 tests), alternately promoted and flaking.
    """

    ENV = "linux-sim"
    SCRIPT = "suite.py"
    MANY = "env-many"
    F = Result.FAIL
    P = Result.PASS
    UP = Result.UNEXPECTED_PASS
    #: name -> (cadence or None, results oldest first)
    HISTORIES = {
        # Flaking: a FAIL after a pass, and two FAILs under 3.
        "t_flaking_new": (3, [P, F]),
        "t_flaking_still": (3, [P, F, F]),
        # Over the cadence on this run: New tonight (decision 25).
        "t_promoted": (3, [P, F, F, F]),
        # Over the run before already: Still failing.
        "t_over": (3, [F, F, F, F, F]),
        # A first run that failed: failing, New, as any test.
        "t_first": (3, [F]),
        # Fewer runs than N+1, all FAIL: over already (verdict 8).
        "t_short": (5, [F, F]),
        "t_exact": (3, [F, F, F]),
        # Cadence 2.
        "t_c2_promoted": (2, [P, F, F]),
        "t_c2_over": (2, [F, F, F]),
        # Over at 2 since the night before; over at 3 only now.
        "t_c2_late": (2, [P, F, F, F]),
        # Decision 3: a pass after a failure.
        "t_tolerated": (3, [F, P, F, P]),
        "t_realfix": (3, [P, F, F, F, P]),
        "t_onlyfail_then_pass": (3, [F, P]),
        "t_marked_up": (3, [F, F, F, UP]),
        # Marked and also muted, assigned or retired.
        "t_muted": (3, [P, F, F]),
        "t_muted_tolerated": (3, [F, P, F, P]),
        "t_assigned": (3, [P, F]),
        "t_retired": (3, [P, F, F]),
        # A marked test that passes.
        "t_marked_pass": (4, [F, P, P]),
        # Unmarked.
        "t_new": (None, [P, F]),
        "t_still": (None, [F, F]),
        "t_fixed": (None, [F, P]),
        "t_up": (None, [UP]),
    }
    MUTED = ("t_muted", "t_muted_tolerated")
    ASSIGNED = ("t_assigned",)
    RETIRED = ("t_retired",)
    NIGHTS = 10
    NOW = BASE + datetime.timedelta(days=9, hours=10)
    STALE = BASE - datetime.timedelta(days=30)

    def _night(self, day: int) -> datetime.datetime:
        return BASE + datetime.timedelta(days=day)

    def _many(self) -> Dict[str, Tuple[Optional[int], List[Result]]]:
        P, F = self.P, self.F
        return {
            "m{0:03d}".format(index): (
                3, [P, F, F, F] if index % 2 == 0 else [P, F, F])
            for index in range(101)
        }

    def setUp(self) -> None:
        super().setUp()
        records = []  # type: List[RunRecord]
        marks = {}  # type: Dict[int, List[Tuple[str, str, str, Optional[int]]]]
        for environment, histories in ((self.ENV, self.HISTORIES),
                                       (self.MANY, self._many())):
            for name, (cadence, results) in histories.items():
                first = self.NIGHTS - len(results)
                for offset, result in enumerate(results):
                    records.append(make_record(
                        environment=environment, test_name=name,
                        result=result, start=self._night(first + offset)))
                if cadence is not None:
                    marks.setdefault(cadence, []).append(
                        (environment, self.SCRIPT, name, None))
        self.store.upsert_runs(records)
        for name in ("alice", "bob"):
            self.store.ensure_user(name, CREATED)
        for cadence, tests in sorted(marks.items()):
            self.store.mark_flaky(tests, cadence, "why", "alice", CREATED)
        self.store.mute_tests(
            [(self.ENV, self.SCRIPT, name, storage.MAINLINE_STREAM_ID)
             for name in self.MUTED],
            "infra", 24, "bob", "alice", self.NOW - datetime.timedelta(
                hours=1))
        for name in self.ASSIGNED:
            self.store.set_assignee(
                self.ENV, self.SCRIPT, name, "bob", "alice", CREATED)
        for name in self.RETIRED:
            self.store.set_retired(
                self.ENV, self.SCRIPT, name, True, "alice", "gone", CREATED)

    # -- the oracle: each test's whole history, analytics alone ----------

    def _marks(self) -> Dict[Tuple[str, str, str], int]:
        out = {}  # type: Dict[Tuple[str, str, str], int]
        for environment, histories in ((self.ENV, self.HISTORIES),
                                       (self.MANY, self._many())):
            for name, (cadence, _results) in histories.items():
                if cadence is not None:
                    out[(environment, self.SCRIPT, name)] = cadence
        return out

    def oracle(self, environment: Optional[str] = None) -> Dict[str, Any]:
        """Every figure the board shows, by test, from each test's
        stored history and the pure functions only: no flaky read of
        storage's is consulted."""
        marks = self._marks()
        out = {
            "new_failures": set(), "still_failing": set(), "fixed": set(),
            "flaking_new": set(), "flaking_still": set(),
            "flaky_total": set(), "promoted": set(), "tolerated": set(),
        }  # type: Dict[str, Set[Tuple[str, str, str]]]
        states = {}  # type: Dict[Tuple[str, str, str], Any]
        for row in self.store.dashboard(include_retired=True):
            if environment is not None and row.environment != environment:
                continue
            triple = (row.environment, row.script, row.test_name)
            runs = [run.result for run in self.store.run_history(
                row.environment, row.script, row.test_name, limit=1000)]
            latest = runs[0]
            prev = runs[1] if len(runs) > 1 else None
            cadence = marks.get(triple)
            if cadence is not None:
                states[triple] = analytics.flaky_state(cadence, runs)
            if row.test_name in self.RETIRED:
                continue
            if cadence is not None:
                out["flaky_total"].add(triple)
            muted = row.test_name in self.MUTED
            if latest is Result.FAIL:
                if muted:
                    continue
                if (cadence is not None
                        and states[triple].state == "flaking"):
                    out["flaking_still" if prev is Result.FAIL
                        else "flaking_new"].add(triple)
                    continue
                if prev is not Result.FAIL:
                    out["new_failures"].add(triple)
                elif (cadence is not None and analytics.flaky_went_over(
                        cadence, runs[:cadence + 1])):
                    out["promoted"].add(triple)
                    out["new_failures"].add(triple)
                else:
                    out["still_failing"].add(triple)
            elif prev is Result.FAIL:
                if (cadence is not None and not muted
                        and not analytics.flaky_fix_counts(
                            cadence, runs[:cadence + 1])):
                    out["tolerated"].add(triple)
                    continue
                out["fixed"].add(triple)
        result = dict(out)  # type: Dict[str, Any]
        result["states"] = states
        return result

    def _names(self, triples: Any) -> List[str]:
        return sorted(t[2] for t in triples if t[0] == self.ENV)


class FlakyAgreesWithAnalyticsTest(FlakyEstateBase):
    """WP-44 item 6: the SQL counts and lists agree with the pure
    functions over every test's whole history: queue_counts, each
    queue's count and its rows, the headline's figures from
    ``analytics.summarize_rollup``, and every row's state. On MariaDB
    with 101 marked tests in one environment (decision 22)."""

    def _cutoff(self) -> datetime.datetime:
        return self.NOW - datetime.timedelta(hours=36)

    def test_the_hand_reading_of_the_fixture(self) -> None:
        """The oracle itself, against the design's table, by name."""
        oracle = self.oracle(self.ENV)
        self.assertEqual(self._names(oracle["flaking_new"]),
                         ["t_assigned", "t_flaking_new"])
        self.assertEqual(self._names(oracle["flaking_still"]),
                         ["t_flaking_still"])
        self.assertEqual(self._names(oracle["promoted"]),
                         ["t_c2_promoted", "t_promoted"])
        self.assertEqual(
            self._names(oracle["new_failures"]),
            ["t_c2_promoted", "t_first", "t_new", "t_promoted"])
        self.assertEqual(
            self._names(oracle["still_failing"]),
            ["t_c2_late", "t_c2_over", "t_exact", "t_over", "t_short",
             "t_still"])
        self.assertEqual(self._names(oracle["tolerated"]), ["t_tolerated"])
        self.assertEqual(
            self._names(oracle["fixed"]),
            ["t_fixed", "t_marked_up", "t_muted_tolerated",
             "t_onlyfail_then_pass", "t_realfix"])

    def test_queue_counts_and_every_queue_agree(self) -> None:
        oracle = self.oracle()
        cutoff = self._cutoff()
        counts = self.store.queue_counts(stale_before=cutoff, now=self.NOW)
        for kind in ("new_failures", "still_failing", "fixed"):
            self.assertEqual(counts[kind], len(oracle[kind]), kind)
            self.assertEqual(
                self.store.status_queue_count(
                    kind, stale_before=cutoff, now=self.NOW),
                len(oracle[kind]), kind)
            rows = self.store.status_queue(
                kind, stale_before=cutoff, now=self.NOW)
            self.assertEqual(
                sorted((r.environment, r.script, r.test_name) for r in rows),
                sorted(oracle[kind]), kind)
        # The open-assignment figures do not move for a mark.
        self.assertEqual(counts["assigned"], 2)  # t_assigned + a mute's
        self.assertEqual(
            counts["muted"], self.store.status_queue_count(
                "muted", stale_before=cutoff, now=self.NOW))

    def test_the_headline_agrees(self) -> None:
        oracle = self.oracle()
        cutoff = self._cutoff()
        view = self.store.flaky_view(
            storage.MAINLINE_STREAM_ID, self.NOW, marks=True)
        estate = analytics.summarize_rollup(
            self.store.summary_rollup(cutoff), 0,
            muted=self.store.muted_cells(self.NOW, cutoff),
            assigned=self.store.assigned_cells(self.NOW),
            flaky=view.counted, flaky_total=view.marks)
        status = estate.status
        self.assertEqual(status.new_failures, len(oracle["new_failures"]))
        self.assertEqual(status.still_failing, len(oracle["still_failing"]))
        self.assertEqual(status.fixed, len(oracle["fixed"]))
        self.assertEqual(status.flaking_new_failures,
                         len(oracle["flaking_new"]))
        self.assertEqual(status.flaking_still_failing,
                         len(oracle["flaking_still"]))
        self.assertEqual(status.flaking, len(oracle["flaking_new"])
                         + len(oracle["flaking_still"]))
        self.assertEqual(status.flaky_total, len(oracle["flaky_total"]))
        # The pass rate's inputs do not change; the mutes are as before.
        self.assertEqual(status.results[Result.FAIL],
                         sum(1 for name, (_c, runs) in self.HISTORIES.items()
                             if runs[-1] is Result.FAIL
                             and name not in self.RETIRED) + 101)
        self.assertEqual(status.muted, 1)
        by_env = {row.environment: row for row in estate.by_environment}
        linux = by_env[self.ENV]
        flaking = [t for t in oracle["flaking_new"] | oracle["flaking_still"]
                   if t[0] == self.ENV]
        self.assertEqual(linux.flaking, len(flaking))
        fails = [name for name, (_c, runs) in self.HISTORIES.items()
                 if runs[-1] is Result.FAIL and name not in self.RETIRED]
        self.assertEqual(linux.failed, len(fails) - 1 - len(flaking))
        # t_assigned is flaking, so it leaves "assigned" (counted once);
        # t_muted's assignee is beside it, muted.
        self.assertEqual(linux.assigned, 0)
        self.assertEqual(
            linux.new_failures,
            len([t for t in oracle["new_failures"] if t[0] == self.ENV]))

    def test_every_rows_state_is_the_rules(self) -> None:
        oracle = self.oracle()
        cutoff = self._cutoff()
        seen = 0
        rows = list(self.store.dashboard(with_flaky=True, now=self.NOW,
                                         include_retired=True))
        for kind in storage.QUEUE_KINDS:
            rows.extend(self.store.status_queue(
                kind, stale_before=cutoff, now=self.NOW, marks=True))
        for row in rows:
            triple = (row.environment, row.script, row.test_name)
            if triple not in oracle["states"]:
                self.assertIsNone(row.flaky, triple)
                continue
            seen += 1
            self.assertEqual(row.flaky.state, oracle["states"][triple],
                             (triple, row))
            self.assertEqual(row.flaky.mark.cadence,
                             self._marks()[triple])
            self.assertEqual(row.flaky.mark.marked_by, "alice")
        self.assertGreater(seen, 101)

    def test_a_capped_queue_is_exact_at_every_cap(self) -> None:
        """Over-fetch and drop: the first *limit* rows of each queue are
        the first *limit* of the whole, in the queue's order."""
        cutoff = self._cutoff()
        for kind in ("new_failures", "still_failing", "fixed"):
            whole = [
                (r.environment, r.test_name) for r in self.store.status_queue(
                    kind, stale_before=cutoff, now=self.NOW)]
            for limit in (1, 2, 3, 5, len(whole)):
                self.store._invalidate_summary_cache()
                capped = [
                    (r.environment, r.test_name)
                    for r in self.store.status_queue(
                        kind, limit=limit, stale_before=cutoff,
                        now=self.NOW)]
                self.assertEqual(capped, whole[:limit], (kind, limit))

    def test_one_environment_in_scope(self) -> None:
        oracle = self.oracle(self.ENV)
        cutoff = self._cutoff()
        counts = self.store.queue_counts(
            environment=self.ENV, stale_before=cutoff, now=self.NOW)
        for kind in ("new_failures", "still_failing", "fixed"):
            self.assertEqual(counts[kind], len(oracle[kind]), kind)
        view = self.store.flaky_view(
            storage.MAINLINE_STREAM_ID, self.NOW, environment=self.ENV, marks=True)
        self.assertEqual(view.marks, len(oracle["flaky_total"]))
        self.assertTrue(all(entry.environment == self.ENV
                            for entry in view.counted))


class PromotionTest(FlakyEstateBase):
    """Decision 25 (design §1 "Going over the cadence", §2 B.3(d)): on
    the night a marked test goes over its cadence it is a New failure,
    and Still failing after; cadence 2; N+1 runs or fewer, all FAIL,
    was over already; a cadence change never makes a test new; the
    queue row says so with ``prev_result`` FAIL and state failing."""

    def _cutoff(self) -> datetime.datetime:
        return self.NOW - datetime.timedelta(hours=36)

    def _new(self) -> List[str]:
        return sorted(
            row.test_name for row in self.store.status_queue(
                "new_failures", environment=self.ENV,
                stale_before=self._cutoff(), now=self.NOW))

    def _still(self) -> List[str]:
        return sorted(
            row.test_name for row in self.store.status_queue(
                "still_failing", environment=self.ENV,
                stale_before=self._cutoff(), now=self.NOW))

    def test_new_that_night_and_still_after(self) -> None:
        self.assertIn("t_promoted", self._new())
        self.assertNotIn("t_promoted", self._still())
        self.store.upsert_runs([make_record(
            test_name="t_promoted", result=Result.FAIL,
            start=self._night(self.NIGHTS))])
        self.assertNotIn("t_promoted", self._new())
        self.assertIn("t_promoted", self._still())

    def test_cadence_two(self) -> None:
        self.assertIn("t_c2_promoted", self._new())
        self.assertIn("t_c2_over", self._still())

    def test_n_plus_one_runs_or_fewer_were_over_already(self) -> None:
        for name in ("t_short", "t_exact", "t_over"):
            self.assertIn(name, self._still())
            self.assertNotIn(name, self._new())

    def test_a_cadence_change_never_makes_a_test_new(self) -> None:
        """A cadence change brings no run, so it never makes a test New
        (design §1): a test it puts over its cadence is Still failing.
        Read as runs alone, "run N+1 exists and is not a FAIL" would call
        both of these New tonight: t_flaking_still (P F F) lowered from 3
        to 2, and t_c2_late (P F F F, over at 2 since the night before)
        raised to 3. Their latest runs are older than the change."""
        self.assertIn("t_c2_late", self._still())
        for name, cadence in (("t_flaking_still", 2), ("t_c2_late", 3)):
            self.store.mark_flaky(
                [(self.ENV, self.SCRIPT, name, None)], cadence, None, "bob",
                self.NOW)
            self.assertIn(name, self._still())
            self.assertNotIn(name, self._new())
        # The next run that keeps it over is not the night either...
        self.store.upsert_runs([make_record(
            test_name="t_c2_late", result=Result.FAIL,
            start=self._night(self.NIGHTS))])
        self.assertIn("t_c2_late", self._still())
        # ...and a test whose newest N+1 all failed is never New.
        for cadence in (4, 2):
            self.store.mark_flaky(
                [(self.ENV, self.SCRIPT, "t_over", None)], cadence, None,
                "bob", self.NOW)
            self.assertIn("t_over", self._still())
            self.assertNotIn("t_over", self._new())

    def test_a_mark_on_a_failing_test_does_not_make_it_new(self) -> None:
        """The same for a fresh mark: an unmarked test failing since the
        night before (t_still, F F) and one over a cadence it is now
        given (P F F at 2) stay where they were."""
        self.store.upsert_runs([
            make_record(test_name="t_late", result=result,
                        start=self._night(day))
            for day, result in ((6, self.P), (7, self.F), (8, self.F))])
        self.assertIn("t_late", self._still())
        self.store.mark_flaky(
            [(self.ENV, self.SCRIPT, name, None)
             for name in ("t_still", "t_late")], 2, "why", "bob", self.NOW)
        self.assertIn("t_late", self._still())
        self.assertIn("t_still", self._still())
        self.assertNotIn("t_late", self._new())

    def test_the_queue_row_says_flaking_to_fail(self) -> None:
        rows = {row.test_name: row for row in self.store.status_queue(
            "new_failures", environment=self.ENV,
            stale_before=self._cutoff(), now=self.NOW)}
        promoted = rows["t_promoted"]
        self.assertIs(promoted.prev_result, Result.FAIL)
        self.assertEqual(promoted.flaky.state,
                         storage.FlakyState("failing", 3, 3))
        first = rows["t_first"]
        self.assertIsNone(first.prev_result)
        self.assertEqual(first.flaky.state,
                         storage.FlakyState("failing", 1, 1))
        self.assertIsNone(rows["t_new"].flaky)

    def test_homes_delta_follows(self) -> None:
        """The delta is new_failures - fixed: a promotion moves it up,
        a flaking failure and a tolerated pass leave it alone."""
        cutoff = self._cutoff()
        counts = self.store.queue_counts(
            environment=self.ENV, stale_before=cutoff, now=self.NOW)
        self.assertEqual(counts["new_failures"] - counts["fixed"], 4 - 5)
        self.store.unmark_flaky(
            [(self.ENV, self.SCRIPT, name) for name in self.HISTORIES],
            "bob", self.NOW)
        counts = self.store.queue_counts(
            environment=self.ENV, stale_before=cutoff, now=self.NOW)
        # Unmarked: t_flaking_new and t_assigned are new; the promotions
        # are still failing; t_tolerated's pass is a fix.
        self.assertEqual(counts["new_failures"] - counts["fixed"],
                         (4 - 2 + 2) - (5 + 1))

    def test_the_bound_list_and_the_widened_read_agree(self) -> None:
        """Above the backend's parameter ceiling the New failures queue
        widens to every marked test and keeps P (design §6: a test
        plants the ceiling). Same rows, same order, at every cap."""
        cutoff = self._cutoff()

        def queue(limit: Optional[int]) -> List[str]:
            self.store._invalidate_summary_cache()
            return [row.test_name for row in self.store.status_queue(
                "new_failures", limit=limit, stale_before=cutoff,
                now=self.NOW)]

        bound = queue(None)
        self.assertEqual(
            len([n for n in bound if n.startswith("m")]), 51)
        self.store._backend.max_bound_parameters = 50
        try:
            self.assertEqual(queue(None), bound)
            for limit in (1, 4, 10, 60):
                self.assertEqual(queue(limit), bound[:limit], limit)
        finally:
            del self.store._backend.max_bound_parameters
        if isinstance(self.store._backend, storage._SqliteBackend):
            self.assertEqual(self.store._backend.max_bound_parameters,
                             storage._SqliteBackend.max_bound_parameters)


class FixedRuleTest(FlakyEstateBase):
    """Decision 3 (design §2 B.1): a marked, unmuted, unretired test
    passing after a FAIL is fixed only if runs 2 to N+1 of its newest
    N+1 all FAIL, or all it had; otherwise it is in no Fixed figure,
    queue or delta."""

    def _fixed(self) -> List[str]:
        return sorted(
            row.test_name for row in self.store.status_queue(
                "fixed", environment=self.ENV,
                stale_before=self.NOW - datetime.timedelta(hours=36),
                now=self.NOW))

    def test_a_pass_after_a_tolerated_failure_is_not_a_fix(self) -> None:
        self.assertNotIn("t_tolerated", self._fixed())

    def test_a_pass_after_being_over_is_a_fix(self) -> None:
        fixed = self._fixed()
        for name in ("t_realfix", "t_onlyfail_then_pass", "t_marked_up",
                     "t_fixed"):
            self.assertIn(name, fixed)

    def test_a_live_mute_wins(self) -> None:
        self.assertIn("t_muted_tolerated", self._fixed())
        # Once the mute has run out, the mark's rule applies.
        later = self.NOW + datetime.timedelta(days=2)
        self.assertNotIn("t_muted_tolerated", [
            row.test_name for row in self.store.status_queue(
                "fixed", environment=self.ENV,
                stale_before=later - datetime.timedelta(hours=36),
                now=later)])
        self.assertEqual(
            self.store.status_queue_count(
                "fixed", environment=self.ENV,
                stale_before=later - datetime.timedelta(hours=36),
                now=later), 4)

    def test_the_pure_rule(self) -> None:
        F, P, UP = self.F, self.P, self.UP
        for cadence in range(2, 11):
            self.assertTrue(analytics.flaky_fix_counts(cadence, [P, F]))
            self.assertTrue(analytics.flaky_fix_counts(
                cadence, [P] + [F] * cadence + [P]))
            self.assertFalse(analytics.flaky_fix_counts(
                cadence, [P] + [F] * (cadence - 1) + [P]))
            self.assertFalse(analytics.flaky_fix_counts(
                cadence, [UP, F, UP] + [F] * cadence))
        self.assertTrue(analytics.flaky_fix_counts(3, []))

    def test_the_went_over_rule(self) -> None:
        F, P = self.F, self.P
        for cadence in range(2, 11):
            self.assertTrue(analytics.flaky_went_over(
                cadence, [F] * cadence + [P]))
            self.assertFalse(analytics.flaky_went_over(
                cadence, [F] * (cadence + 1)))
            self.assertFalse(analytics.flaky_went_over(
                cadence, [F] * cadence))
            self.assertFalse(analytics.flaky_went_over(
                cadence, [F] * (cadence - 1) + [P, F]))


class FlakyStatementsTest(FlakyEstateBase):
    """The cache guards of the flaky reads (design §2 B.7), by
    sqlite3's trace (excluded on MariaDB, whose values the classes
    above compare): Q1, Qc and one Q2 per 100 tests cold, nothing warm;
    after an act Q1 and Qc again but no Q2; the rows' join only where a
    mark exists and never on a count."""

    def _statements(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        trace_sql_into(conn, seen)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return [" ".join(s.split()) for s in seen
                if s.strip().upper().startswith("SELECT")]

    def _view(self) -> Any:
        return self.store.flaky_view(
            storage.MAINLINE_STREAM_ID, self.NOW, marks=True)

    def _prime(self) -> None:
        """What every summary read has made before the view: the mute
        epoch (which carries the bit) and the stream's environments."""
        self.store.flaky_marks_exist(storage.MAINLINE_STREAM_ID, self.NOW)
        self.store._stream_environment_names(storage.MAINLINE_STREAM_ID)

    def test_cold_q1_qc_and_a_q2_per_hundred_warm_nothing(self) -> None:
        self.store._invalidate_summary_cache()
        self.store._cadence_runs_cache.clear()
        self._prime()
        cold = self._statements(self._view)
        q1 = [s for s in cold if "CROSS JOIN test_flaky_marks" in s]
        qc = [s for s in cold if "FROM test_flaky_marks AS fm CROSS" in s]
        q2 = [s for s in cold if "FROM runs" in s]
        self.assertEqual((len(q1), len(qc)), (1, 1), cold)
        # 101 in env-many, 7 on linux-sim and 4 fix candidates: two.
        self.assertEqual(len(q2), 2, cold)
        self.assertEqual(len(cold), 4, cold)
        self.assertEqual(self._statements(self._view), [])

    def test_after_an_act_q1_and_qc_but_no_q2(self) -> None:
        self._view()
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "t_new", "bob", "alice", CREATED)
        self._prime()
        after = self._statements(self._view)
        self.assertEqual(len(after), 2, after)
        self.assertFalse([s for s in after if "FROM runs" in s])

    def test_a_push_to_one_environment_rereads_that_one(self) -> None:
        self._view()
        self.store.upsert_runs([make_record(
            environment=self.MANY, test_name="m000", result=Result.FAIL,
            start=self._night(self.NIGHTS))])
        self._prime()
        after = self._statements(self._view)
        q1 = [s for s in after if "CROSS JOIN test_flaky_marks" in s]
        self.assertEqual(len(q1), 1, after)
        self.assertIn("lr.environment IN (", q1[0])
        # linux-sim's states are its memo's; env-many's 101 and the
        # candidates' are read again.
        self.assertEqual(len([s for s in after if "FROM runs" in s]), 2)

    def test_with_no_mark_nothing_is_read(self) -> None:
        self.store.unmark_flaky(
            [triple for triple in self._marks()], "bob", self.NOW)
        self._prime()
        self.assertEqual(self._statements(self._view), [])
        self.assertIsNone(self._view())

    def test_unfiltered_rows_never_run_q1(self) -> None:
        """The rows read their marks by a join and the state from their
        own result pair; only FAIL after FAIL at 3 to 10 reads runs, once
        for the page."""
        self.store._invalidate_summary_cache()
        self.store._cadence_runs_cache.clear()
        seen = self._statements(lambda: self.store.dashboard(
            with_flaky=True, limit=250, now=self.NOW))
        self.assertEqual(len(seen), 1 + 2, seen)
        self.assertIn("LEFT JOIN test_flaky_marks AS fm", seen[0])
        self.assertFalse([s for s in seen
                          if "CROSS JOIN test_flaky_marks" in s])
        count = self._statements(lambda: self.store.dashboard_count(
            now=self.NOW))
        self.assertEqual(len(count), 1)
        self.assertNotIn("flaky", count[0].lower())
        # The page's runs are memoized: a second page costs the rows.
        again = self._statements(lambda: self.store.dashboard(
            with_flaky=True, limit=250, now=self.NOW))
        self.assertEqual(len(again), 1, again)

    def test_the_new_failures_queue_binds_p(self) -> None:
        cutoff = self.NOW - datetime.timedelta(hours=36)
        self._view()
        rows = self._statements(lambda: self.store.status_queue(
            "new_failures", stale_before=cutoff, now=self.NOW, marks=True))
        self.assertEqual(len(rows), 1, rows)
        # The trace shows the statement with its values bound.
        self.assertIn(
            "(lr.environment, lr.script, lr.test_name) IN (VALUES (", rows[0])
        self.assertEqual(rows[0].count("', 'suite.py', '"), 51 + 2)
        counts = self._statements(lambda: self.store.status_queue_count(
            "new_failures", stale_before=cutoff, now=self.NOW))
        self.assertEqual(len(counts), 1, counts)
        self.assertNotIn("JOIN TEST_FLAKY_MARKS", counts[0].upper())

    def test_the_lists_read_q1_and_a_q2_per_hundred_cold_none_warm(
            self) -> None:
        """WP-44 item 8 (design §2 B.7): Open Actions and browse with
        Failing or Flaking, cold, are their two statements (rows and
        count; by keys, the keys and the page) plus Q1 and one Q2 per 100
        tests (here 101 in env-many and linux-sim's: two). Q1's memo is
        kept for the environments that hold a mark: called alone, one
        read of the marks' key finds them; after /api/dashboard's
        products read (which carries them) none does, and the stream's
        environment list is read by neither. Warm, the two. Failing and
        Flaking together read no Q1."""
        reads = {
            "open": lambda: self.store.dashboard_page(
                open_items=True, with_flaky=True, limit=100, now=self.NOW),
            "fail": lambda: self.store.dashboard_page(
                results=[Result.FAIL], flaking=False, with_flaky=True,
                limit=250, now=self.NOW),
            "flaking": lambda: self.store.dashboard_page(
                results=[Result.FAIL], flaking=True, with_flaky=True,
                limit=250, now=self.NOW),
        }  # type: Dict[str, Callable[[], Any]]
        for name, read in reads.items():
            self.store._invalidate_summary_cache()
            self.store._cadence_runs_cache.clear()
            cold = self._statements(read)
            q1 = [s for s in cold if "CROSS JOIN test_flaky_marks" in s]
            q2 = [s for s in cold if "FROM runs" in s]
            names = [s for s in cold
                     if s.startswith("SELECT DISTINCT environment FROM")]
            self.assertEqual((len(q1), len(q2), len(names)), (1, 2, 1),
                             (name, cold))
            self.assertEqual(
                names[0], "SELECT DISTINCT environment FROM test_flaky_marks")
            self.assertEqual(len(cold), 2 + 4, (name, cold))
            self.assertEqual(len(self._statements(read)), 2, name)
            # After the products read that carries the marked
            # environments: Q1 and the Q2s only.
            self.store._invalidate_summary_cache()
            self.store._cadence_runs_cache.clear()
            self.store.environment_products_and_marks()
            carried = self._statements(read)
            self.assertFalse([s for s in carried if "DISTINCT environment"
                              in s], (name, carried))
            self.assertEqual(len(carried), 2 + 3, (name, carried))
        self.store._invalidate_summary_cache()
        both = self._statements(lambda: self.store.dashboard_page(
            results=[Result.FAIL], with_flaky=True, limit=250, now=self.NOW))
        self.assertFalse([s for s in both
                          if "CROSS JOIN test_flaky_marks" in s], both)

    def test_after_a_home_load_the_lists_read_no_runs(self) -> None:
        """WP-44 item 8: the assigned queue, My actions and
        Open Actions, after the headline's view, read no Q2, a muted test
        (t_muted, P F F at 3, assigned by its mute) included: the view
        decides an un-retired muted test too, and counts it nowhere."""
        cutoff = self.NOW - datetime.timedelta(hours=36)
        self.store._invalidate_summary_cache()
        self.store._cadence_runs_cache.clear()
        self._prime()
        self._view()
        self.store._cadence_runs_cache.clear()  # the memo alone serves
        reads = (
            lambda: self.store.status_queue(
                "assigned", stale_before=cutoff, now=self.NOW, marks=True),
            lambda: self.store.status_queue(
                "assigned", assignee="bob", stale_before=cutoff,
                now=self.NOW, marks=True),
            lambda: self.store.dashboard_page(
                open_items=True, with_flaky=True, limit=100, now=self.NOW),
            lambda: self.store.dashboard_page(
                results=[Result.FAIL], flaking=True, with_flaky=True,
                limit=250, now=self.NOW),
        )
        for read in reads:
            seen = self._statements(read)
            self.assertFalse([s for s in seen if "FROM runs" in s], seen)
        muted = [row for row in self.store.status_queue(
            "assigned", stale_before=cutoff, now=self.NOW, marks=True)
            if row.test_name == "t_muted"]
        self.assertEqual(muted[0].flaky.state.state, "flaking")

    def test_the_lists_ask_no_runs_a_pair_decides(self) -> None:
        """Open Actions cold reads runs only for a FAIL after a FAIL at a
        cadence of 3 to 10: at 2 the pair says "failing", and only
        whether it went over tonight, which no list asks, needs runs."""
        asked = []  # type: List[Tuple[str, str, str]]
        real = self.store._cadence_runs_iso

        def spy(stream_id: int, tests: Any, floor: Any = None) -> Any:
            asked.extend((t[0], t[1], t[2]) for t in tests)
            return real(stream_id, tests, floor)

        self.store._cadence_runs_iso = spy  # type: ignore
        try:
            self.store._invalidate_summary_cache()
            self.store._cadence_runs_cache.clear()
            rows = self.store.dashboard_page(
                open_items=True, with_flaky=True, environment=self.ENV,
                now=self.NOW)[0]
        finally:
            del self.store._cadence_runs_iso  # type: ignore
        names = sorted(t[2] for t in asked)
        self.assertTrue(names)
        self.assertFalse([n for n in names if n.startswith("t_c2_")], names)
        self.assertNotIn("t_retired", names)
        states = {row.test_name: row.flaky.state for row in rows
                  if row.flaky is not None}
        oracle = self.oracle(self.ENV)["states"]
        for name, state in states.items():
            self.assertEqual(state, oracle[(self.ENV, self.SCRIPT, name)])

    def test_the_tab_reads_the_marks_once_and_twenty_runs_a_page(
            self) -> None:
        self.store._invalidate_summary_cache()
        self.store._cadence_runs_cache.clear()
        cold = self._statements(lambda: self.store.flaky_tab(
            storage.MAINLINE_STREAM_ID, self.NOW, limit=100))
        self.assertTrue(cold[0].startswith(
            "SELECT lr.environment, lr.script, lr.test_name, tr.retired_at"),
            cold)
        # Q2 at N+1 for the FAIL-after-FAIL tests (over 100: two), then
        # the page's 100 at 20 (one).
        self.assertEqual(len([s for s in cold if "LIMIT 4" in s]), 2, cold)
        self.assertEqual(len([s for s in cold if "LIMIT 20" in s]), 1, cold)
        self.assertEqual(len(cold), 4, cold)

    def test_the_trend_reads_once_and_the_runs_before_its_window(
            self) -> None:
        self._prime()
        cold = self._statements(lambda: self.store.daily_flaking_failures(
            BASE + datetime.timedelta(days=8), self.NOW))
        self.assertEqual(len([s for s in cold if "CROSS JOIN runs AS r"
                              in s]), 1, cold)
        # Every marked test whose first run in the window failed: over
        # 100 of them, two statements.
        self.assertEqual(len(cold), 3, cold)
        self.assertEqual(self._statements(
            lambda: self.store.daily_flaking_failures(
                BASE + datetime.timedelta(days=8), self.NOW)), [])


class FlakyPlanTest(FlakyEstateBase):
    """Every statement on the marks table pins its driving side (design
    §6): an inner join let SQLite scan latest_runs, 1.4 ms against
    0.024. On SQLite by EXPLAIN QUERY PLAN; on MariaDB by EXPLAIN, so a
    wrong plan on CI's 10.3 legs fails here by name (item 6h, the
    STRAIGHT_JOIN hook, exists only if one does).

    MariaDB plans by its estimates, so the estate is given production's
    shape first: far more tests than marks (1,200 more passing tests
    here, unmarked, against about 120 marks). On the bare fixture, with
    as many marks as tests, 12.3 reasonably reads latest_runs whole; at
    any real ratio that plan is a scan of the partition, and it is what
    these guards refuse."""

    WIDE = 1200

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(environment="env-wide", test_name="w{0:05d}".format(
                index), start=self._night(self.NIGHTS - 1))
            for index in range(self.WIDE)])

    #: How each statement may read latest_runs on MariaDB: by its key
    #: from a mark (eq_ref/ref on the primary key or the triple index),
    #: or, for Q1, the stream's FAIL range. Anything else (ALL, index,
    #: a range of the partition by its key) reads the stream whole.
    _BY_KEY = (("eq_ref", "PRIMARY"), ("eq_ref", "idx_latest_runs_triple"),
               ("ref", "idx_latest_runs_triple"))

    def _captured(self, call: Callable[[], Any]) -> List[Tuple[str, List[Any]]]:
        captured = []  # type: List[Tuple[str, List[Any]]]
        store = self.store
        real = store._conn

        class Spy(object):
            def __init__(self, conn: Any) -> None:
                self._conn = conn

            def execute(self, sql: str, params: Any = ()) -> Any:
                captured.append((sql, list(params)))
                return self._conn.execute(sql, params)

        store._conn = lambda: Spy(real())  # type: ignore
        try:
            call()
        finally:
            del store._conn  # type: ignore
        return captured

    def _statement(self, call: Callable[[], Any], marker: str
                   ) -> Tuple[str, List[Any]]:
        found = [c for c in self._captured(call) if marker in c[0]]
        self.assertEqual(len(found), 1, [c[0][:80] for c in found])
        return found[0]

    def _mariadb(self) -> bool:
        return not isinstance(self.store._backend, storage._SqliteBackend)

    def _explain(self, sql: str, params: List[Any]) -> List[Any]:
        if self._mariadb():
            return list(self.store._conn().execute(
                "EXPLAIN " + sql, params).fetchall())
        return [str(row[-1]).upper() for row in self.store._conn().execute(
            "EXPLAIN QUERY PLAN " + sql, params).fetchall()]

    def _q1(self) -> Tuple[str, List[Any]]:
        self.store._invalidate_summary_cache()
        return self._statement(
            lambda: self.store._flaky_failing(storage.MAINLINE_STREAM_ID),
            "CROSS JOIN test_flaky_marks")

    def _qc(self) -> Tuple[str, List[Any]]:
        self.store._invalidate_summary_cache()
        return self._statement(
            lambda: self.store._flaky_marked(storage.MAINLINE_STREAM_ID),
            "FROM test_flaky_marks AS fm CROSS JOIN")

    def test_q1_starts_from_the_streams_fail_rows(self) -> None:
        sql, params = self._q1()
        plan = self._explain(sql, params)
        if self._mariadb():
            for row in plan:
                table, kind, key = row[2], row[3], row[5]
                if table == "lr":
                    self.assertTrue(
                        (kind, key) in self._BY_KEY
                        or (kind in ("range", "ref")
                            and key == "idx_latest_runs_result"),
                        "Q1 reads latest_runs by {0} ({1}), neither the "
                        "stream's FAIL range nor its key from each mark: "
                        "{2}".format(key, kind, plan))
                if table == "fm":
                    self.assertTrue(
                        kind in ("eq_ref", "ALL", "index"),
                        "Q1 reaches the marks by {0}: {1}".format(
                            kind, plan))
            # The mark's newest act: its history by the triple index.
            history = [row for row in plan if row[2] == "h"]
            self.assertEqual(len(history), 1, plan)
            self.assertEqual(
                (history[0][3], history[0][5]),
                ("ref", "idx_flaky_mark_history_triple"),
                "Q1 reads the mark's history by {0} ({1}): {2}".format(
                    history[0][5], history[0][3], plan))
            return
        lr = [line for line in plan if " LR " in line + " "]
        fm = [line for line in plan if " FM " in line + " "]
        history = [line for line in plan if " H " in line + " "]
        self.assertEqual(len(history), 1, plan)
        self.assertTrue(history[0].startswith("SEARCH"), plan)
        self.assertIn("IDX_FLAKY_MARK_HISTORY_TRIPLE", history[0], plan)
        self.assertTrue(lr and lr[0].startswith("SEARCH"), plan)
        self.assertIn("IDX_LATEST_RUNS_RESULT", lr[0], plan)
        self.assertTrue(fm and fm[0].startswith("SEARCH"), plan)
        self.assertIn("TEST_FLAKY_MARKS", fm[0], plan)
        self.assertLess(plan.index(lr[0]), plan.index(fm[0]), plan)

    def test_qc_starts_from_the_marks(self) -> None:
        sql, params = self._qc()
        plan = self._explain(sql, params)
        if self._mariadb():
            lr_rows = [row for row in plan if row[2] == "lr"]
            self.assertEqual(len(lr_rows), 2, plan)
            for row in lr_rows:
                self.assertIn(
                    (row[3], row[5]), self._BY_KEY,
                    "Qc reads latest_runs by {0} ({1}), not by its key "
                    "from each mark: {2}".format(row[5], row[3], plan))
            return
        lr = [line for line in plan if " LR " in line + " "]
        self.assertEqual(len(lr), 2, plan)
        for line in lr:
            self.assertTrue(line.startswith("SEARCH"), plan)
            self.assertIn("SQLITE_AUTOINDEX_LATEST_RUNS_1", line, plan)

    def test_the_rows_join_reaches_each_mark_by_its_key(self) -> None:
        cutoff = self.NOW - datetime.timedelta(hours=36)
        reads = (
            (lambda: self.store.dashboard(
                with_flaky=True, limit=250, now=self.NOW)),
            (lambda: self.store.status_queue(
                "new_failures", stale_before=cutoff, now=self.NOW,
                marks=True)),
        )
        for read in reads:
            self.store._invalidate_summary_cache()
            sql, params = self._statement(
                read, "LEFT JOIN test_flaky_marks AS fm")
            plan = self._explain(sql, params)
            if self._mariadb():
                fm = [row for row in plan if row[2] == "fm"]
                self.assertEqual(len(fm), 1, plan)
                self.assertEqual((fm[0][3], fm[0][5]), ("eq_ref", "PRIMARY"),
                                 plan)
                continue
            fm = [line for line in plan if " FM " in line + " "]
            self.assertTrue(fm and fm[0].startswith("SEARCH"), plan)
            self.assertIn("TEST_FLAKY_MARKS", fm[0], plan)

    def test_the_bit_is_a_read_of_the_marks_key(self) -> None:
        for statement in (
                "SELECT MIN(environment) FROM test_flaky_marks",):
            plan = self._explain(statement, [])
            if self._mariadb():
                self.assertIn("optimized away", str(plan[0][-1]), plan)
                continue
            for line in plan:
                self.assertFalse(line.startswith("SCAN"), plan)

    # -- WP-44 item 8: the lists' reads ----------------------------------

    def _lr_by_key_or_range(self, plan: List[Any], what: str) -> None:
        """No read of latest_runs is a scan: on SQLite every ``lr`` line
        is a SEARCH; on MariaDB no ``lr`` row reads ALL or a full index."""
        if self._mariadb():
            for row in plan:
                if row[2] == "lr":
                    self.assertNotIn(
                        row[3], ("ALL", "index"),
                        "{0} reads latest_runs whole: {1}".format(
                            what, plan))
            return
        lr = [line for line in plan if " LR " in line + " "]
        self.assertTrue(lr, (what, plan))
        for line in lr:
            self.assertTrue(line.startswith("SEARCH"), (what, plan))

    def test_the_filter_terms_keep_the_fail_range(self) -> None:
        """Browse with Failing and with Flaking: the F term reads ``lr``
        alone and leaves the rows and the count on the stream's FAIL
        range (design §3c), and the count joins no mark."""
        for flaking in (False, True):
            for marker in ("SELECT COUNT(*)", "LEFT JOIN test_flaky_marks"):
                self.store._invalidate_summary_cache()
                sql, params = self._statement(
                    lambda: self.store.dashboard_page(
                        results=[Result.FAIL], flaking=flaking,
                        with_flaky=True, limit=250, now=self.NOW),
                    marker)
                self.assertIn("(lr.environment, lr.script, lr.test_name) IN",
                              sql)
                if marker == "SELECT COUNT(*)":
                    self.assertNotIn("TEST_FLAKY_MARKS", sql.upper())
                self._lr_by_key_or_range(
                    self._explain(sql, params), (flaking, marker))

    def test_open_actions_terms_and_keys_read_no_partition_whole(
            self) -> None:
        """Open Actions: the bound X term (MariaDB's form) on the rows and
        the count, and the keys form's two statements (SQLite's), on
        both engines: no read of latest_runs is a scan."""
        for by_keys in (False, True):
            self.store._backend.set_aside_by_keys = by_keys  # type: ignore
            try:
                self.store._invalidate_summary_cache()
                captured = self._captured(
                    lambda: self.store.dashboard_page(
                        open_items=True, with_flaky=True, limit=100,
                        now=self.NOW))
            finally:
                del self.store._backend.set_aside_by_keys  # type: ignore
            reads = [c for c in captured
                     if "FROM latest_runs AS lr" in c[0]
                     and "CROSS JOIN test_flaky_marks" not in c[0]]
            self.assertEqual(len(reads), 2, [c[0][:60] for c in reads])
            for sql, params in reads:
                if by_keys:
                    self.assertNotIn("ca.assignee IS NULL AND", sql)
                else:
                    self.assertIn("ca.assignee IS NULL AND (lr.environment",
                                  sql)
                self._lr_by_key_or_range(
                    self._explain(sql, params), (by_keys, sql[:60]))

    def test_the_flaky_tab_starts_from_the_marks(self) -> None:
        sql, params = self._statement(
            lambda: self.store.flaky_tab(
                storage.MAINLINE_STREAM_ID, self.NOW),
            "FROM test_flaky_marks AS fm CROSS JOIN latest_runs")
        plan = self._explain(sql, params)
        if self._mariadb():
            for row in plan:
                if row[2] == "lr":
                    self.assertIn(
                        (row[3], row[5]), self._BY_KEY,
                        "the tab reads latest_runs by {0} ({1}), not by "
                        "its key from each mark: {2}".format(
                            row[5], row[3], plan))
            return
        lr = [line for line in plan if " LR " in line + " "]
        fm = [line for line in plan if " FM " in line + " "]
        self.assertEqual(len(lr), 1, plan)
        self.assertTrue(lr[0].startswith("SEARCH"), plan)
        self.assertIn("SQLITE_AUTOINDEX_LATEST_RUNS_1", lr[0], plan)
        self.assertLess(plan.index(fm[0]), plan.index(lr[0]), plan)

    def test_the_trend_reaches_runs_from_each_mark(self) -> None:
        """SQLite's plan only: the runs member reads each mark's range of
        the runs UNIQUE index. On MariaDB this test is excluded by name:
        on this fixture's few-row runs table MariaDB reasonably scans
        runs, where at production scale it reads each mark's range, so
        its plan is checked by hand on a production-scale copy in the
        measured pass, as AssignedTrendTest's is (WP-43 decision 12)."""
        sql, params = self._statement(
            lambda: self.store.daily_flaking_failures(
                BASE, self.NOW),
            "CROSS JOIN runs AS r")
        plan = self._explain(sql, params)
        runs = [line for line in plan if " R " in line + " "]
        self.assertEqual(len(runs), 1, plan)
        self.assertTrue(runs[0].startswith("SEARCH"), plan)
        self.assertIn("START_TIME>?", runs[0].replace(" ", ""), plan)


class FlakyListsBase(FlakyEstateBase):
    """WP-44 item 8: FlakyEstateBase with the sets the lists act on,
    from the oracle (each test's whole history and the pure rule):

    - F: marked, flaking, under no live mute; retired ones only when the
      read keeps retired tests (they read FLAKING there);
    - X: F's members with no assignee, not retired.

    On linux-sim F is t_flaking_new, t_flaking_still and t_assigned
    (t_muted flakes under a mute, t_retired is retired) and X the first
    two; env-many adds its 50 odd tests (P F F at 3) to both."""

    def _f(self, include_retired: bool = False
           ) -> Set[Tuple[str, str, str]]:
        out = set()  # type: Set[Tuple[str, str, str]]
        for triple, state in self.oracle()["states"].items():
            if state is None or state.state != "flaking":
                continue
            if triple[2] in self.MUTED:
                continue
            if triple[2] in self.RETIRED and not include_retired:
                continue
            out.add(triple)
        return out

    def _x(self) -> Set[Tuple[str, str, str]]:
        return {t for t in self._f() if t[2] not in self.ASSIGNED}

    @staticmethod
    def _keys(rows: Sequence[Any]) -> List[Tuple[str, str, str]]:
        return [(row.environment, row.script, row.test_name) for row in rows]


class FlakyEnvironmentsTest(FlakyListsBase):
    """WP-44 item 8: the marks' memo is kept for the environments that
    hold a mark, not the stream's whole environment list. An environment
    with no mark answers as it did; a stream with marks in two
    environments decides both; a mark in a new environment and a push
    into a marked one are seen. On MariaDB too."""

    PLAIN = "env-plain"

    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_runs([
            make_record(environment=self.PLAIN, test_name=name,
                        result=result, start=self._night(night))
            for name, results in (("p_flaky", (self.P, self.F)),
                                  ("p_still", (self.F, self.F)),
                                  ("p_pass", (self.P, self.P)))
            for night, result in zip((8, 9), results)])
        self.store._invalidate_summary_cache()

    def _flaking(self, **scope: Any) -> List[Tuple[str, str, str]]:
        rows, total = self.store.dashboard_page(
            results=[Result.FAIL], flaking=True, with_flaky=True,
            now=self.NOW, **scope)
        self.assertEqual(total, len(rows))
        return self._keys(rows)

    def test_an_environment_with_no_mark_answers_as_before(self) -> None:
        plain = {"environment": self.PLAIN}
        today = self._keys(self.store.dashboard(
            results=[Result.FAIL], now=self.NOW, **plain))
        open_today = self._keys(self.store.dashboard(
            open_items=True, now=self.NOW, **plain))
        for first in ("lists", "headline"):
            self.store._invalidate_summary_cache()
            if first == "headline":
                # The stream's names memoized first, as Home's rollup
                # does: the memo then keeps an empty entry here.
                self.store._stream_environment_names(
                    storage.MAINLINE_STREAM_ID)
            self.assertEqual(self._flaking(**plain), [], first)
            fail, total = self.store.dashboard_page(
                results=[Result.FAIL], flaking=False, with_flaky=True,
                now=self.NOW, **plain)
            self.assertEqual((self._keys(fail), total),
                             (today, len(today)), first)
            rows, total = self.store.dashboard_page(
                open_items=True, with_flaky=True, now=self.NOW, **plain)
            self.assertEqual((self._keys(rows), total),
                             (open_today, len(open_today)), first)
            self.assertTrue(all(row.flaky is None for row in rows))
            view = self.store.flaky_view(
                storage.MAINLINE_STREAM_ID, self.NOW, environment=self.PLAIN,
                marks=True)
            self.assertEqual((view.counted, view.marks, view.states),
                             ([], 0, {}), first)
        # Read first through the marked environments, no entry is kept
        # for an environment with no mark.
        self.store._invalidate_summary_cache()
        self._flaking()
        self.assertIsNone(self.store._cached_summary(
            ("flaky_states", storage.MAINLINE_STREAM_ID, self.PLAIN)))

    def test_marks_in_two_environments_are_both_decided(self) -> None:
        self.store._invalidate_summary_cache()
        entries = self.store._flaky_failing(storage.MAINLINE_STREAM_ID)
        self.assertEqual(sorted(entries), [self.MANY, self.ENV])
        self.assertEqual(set(self._flaking()), self._f())
        self.assertEqual(
            {t[0] for t in self._flaking()}, {self.ENV, self.MANY})

    def test_a_mark_in_a_new_environment_is_seen(self) -> None:
        self.assertEqual(self._flaking(environment=self.PLAIN), [])
        self.store.mark_flaky([(self.PLAIN, self.SCRIPT, "p_flaky", None)],
                              3, "why", "bob", self.NOW)
        self.assertEqual(self._flaking(environment=self.PLAIN),
                         [(self.PLAIN, self.SCRIPT, "p_flaky")])
        self.store.unmark_flaky([(self.PLAIN, self.SCRIPT, "p_flaky")],
                                "bob", self.NOW)
        self.assertEqual(self._flaking(environment=self.PLAIN), [])

    def test_a_push_into_a_marked_environment_is_seen(self) -> None:
        before = self._flaking(environment=self.ENV)
        self.assertNotIn("t_marked_pass", [t[2] for t in before])
        # F P P then a FAIL, at a cadence of 4: flaking.
        self.store.upsert_runs([make_record(
            test_name="t_marked_pass", result=Result.FAIL,
            start=self._night(self.NIGHTS))])
        self.assertIn("t_marked_pass",
                      [t[2] for t in self._flaking(environment=self.ENV)])


class OpenActionsSetAsideTest(FlakyListsBase):
    """WP-44 item 8 (design §1 "Open Actions", §2 B.3(b), §9): ``open=1``
    leaves out X and nothing else, in every mode and owner filter; an
    assigned test is listed whatever its state, a marked test at an
    unexpected pass stays, an unassigned test over its cadence stays.
    The rows, their total and bulk assign agree, by both forms (SQLite's
    keys, MariaDB's bound term) on each engine. On MariaDB with 101
    marked tests in one environment (decision 22)."""

    def _page(self, **kwargs: Any) -> Tuple[List[Tuple[str, str, str]], int]:
        rows, total = self.store.dashboard_page(
            open_items=True, with_flaky=True, now=self.NOW, **kwargs)
        return self._keys(rows), total

    def _today(self, **kwargs: Any) -> List[Tuple[str, str, str]]:
        """Today's Open Actions: the read without the mark's bit."""
        return self._keys(self.store.dashboard(
            open_items=True, now=self.NOW, **kwargs))

    def test_only_x_leaves(self) -> None:
        x = self._x()
        self.assertEqual(self._names(x), ["t_flaking_new", "t_flaking_still"])
        self.assertEqual(len(x), 2 + 50)
        today = self._today()
        rows, total = self._page()
        self.assertEqual(rows, [t for t in today if t not in x])
        self.assertEqual(total, len(rows))
        self.assertEqual(len(today) - len(rows), len(x))
        # dashboard() and dashboard_count() alone agree with the page.
        self.assertEqual(self._keys(self.store.dashboard(
            open_items=True, with_flaky=True, now=self.NOW)), rows)
        self.assertEqual(self.store.dashboard_count(
            open_items=True, now=self.NOW), total)

    def test_assigned_flaking_and_marked_unexpected_pass_stay(self) -> None:
        rows = {row.test_name: row for row in self.store.dashboard_page(
            open_items=True, with_flaky=True, now=self.NOW,
            environment=self.ENV)[0]}
        self.assertEqual(rows["t_assigned"].assignee, "bob")
        self.assertEqual(rows["t_assigned"].flaky.state.state, "flaking")
        self.assertIs(rows["t_marked_up"].result, Result.UNEXPECTED_PASS)
        for name in ("t_promoted", "t_over", "t_first", "t_short",
                     "t_c2_over", "t_new", "t_still", "t_up"):
            self.assertIn(name, rows)
        for name in ("t_flaking_new", "t_flaking_still"):
            self.assertNotIn(name, rows)

    def test_a_live_mute_wins(self) -> None:
        """A test flaking under a live mute is no FLAKING row; it is
        listed (a mute is owned). Unassigned, which drops its mute, it is
        X and leaves."""
        names = [t[2] for t in self._page(environment=self.ENV)[0]]
        self.assertIn("t_muted", names)
        self.store.set_assignee(
            self.ENV, self.SCRIPT, "t_muted", None, "alice", CREATED)
        names = [t[2] for t in self._page(environment=self.ENV)[0]]
        self.assertNotIn("t_muted", names)

    def test_every_mode_and_owner_filter(self) -> None:
        x = self._x()
        cases = [
            {}, {"include_unassigned": True}, {"assignees": ["bob"]},
            {"assignees": ["bob"], "include_unassigned": True},
            {"assigned_only": True}, {"results": [Result.FAIL]},
            {"results": [Result.UNEXPECTED_PASS]},
            {"results": [Result.FAIL, Result.UNEXPECTED_PASS]},
            {"environment": self.ENV}, {"environments": [self.MANY]},
            {"script": self.SCRIPT}, {"include_retired": True},
            {"q": "flaking"},
        ]  # type: List[Dict[str, Any]]
        for case in cases:
            expected = [t for t in self._today(**case) if t not in x]
            rows, total = self._page(**case)
            self.assertEqual(rows, expected, case)
            self.assertEqual(total, len(expected), case)

    def test_failing_mode_sends_both_values(self) -> None:
        """Open Actions' Failing mode sends FAIL and FLAKING (flaking
        None): today's FAIL less X, t_assigned in it. FAIL alone would
        leave every flaking test out, an assigned one included (§2 B.9)."""
        x = self._x()
        today = self._today(results=[Result.FAIL])
        both, total = self._page(results=[Result.FAIL])
        self.assertEqual(both, [t for t in today if t not in x])
        self.assertEqual(total, len(both))
        self.assertIn("t_assigned", [t[2] for t in both])
        alone, total = self._page(results=[Result.FAIL], flaking=False)
        self.assertEqual(alone, [t for t in today if t not in self._f()])
        self.assertNotIn("t_assigned", [t[2] for t in alone])
        self.assertEqual(total, len(alone))

    def test_total_is_the_rows_paged_at_every_sort(self) -> None:
        x = self._x()
        for sort in storage.DASHBOARD_SORTS:
            for descending in (False, True):
                expected = [t for t in self._today(
                    sort=sort, descending=descending) if t not in x]
                paged = []  # type: List[Tuple[str, str, str]]
                for offset in range(0, len(expected) + 9, 9):
                    rows, total = self._page(
                        sort=sort, descending=descending, limit=9,
                        offset=offset)
                    self.assertEqual(total, len(expected), sort)
                    paged.extend(rows)
                self.assertEqual(paged, expected, (sort, descending))

    def test_both_forms_agree_on_either_engine(self) -> None:
        x = self._x()
        expected = [t for t in self._today(sort="test_name") if t not in x]
        for by_keys in (True, False):
            self.store._backend.set_aside_by_keys = by_keys  # type: ignore
            try:
                self.store._invalidate_summary_cache()
                rows, total = self._page(sort="test_name", limit=40,
                                         offset=20)
                count = self.store.dashboard_count(
                    open_items=True, now=self.NOW)
            finally:
                del self.store._backend.set_aside_by_keys  # type: ignore
            self.assertEqual(rows, expected[20:60], by_keys)
            self.assertEqual((total, count), (len(expected),) * 2, by_keys)

    def test_the_ceiling_counts_only_the_terms_bound(self) -> None:
        """With no FLAKING mode only X's term is bound, so F does not
        count toward the parameter ceiling: a ceiling above X's term and
        below X's and F's together keeps the bound form."""
        _values, aside = self.store._set_aside(
            storage.MAINLINE_STREAM_ID, None, None, None, None, False, [],
            False, False, True, None, self.NOW, None, True)
        assert aside is not None
        self.assertIsNone(aside.keep)
        self.assertEqual((len(aside.unowned), len(aside.flaking)),
                         (52, 53))
        bound = 3
        x_only = 1 + 3 * len(aside.unowned)
        self.store._backend.set_aside_by_keys = False  # type: ignore
        self.store._backend.max_bound_parameters = (  # type: ignore
            bound + x_only + 2 + 10)
        try:
            self.assertFalse(self.store._set_aside_by_keys(aside, bound))
            self.store._backend.max_bound_parameters = (  # type: ignore
                bound + x_only + 2 - 1)
            self.assertTrue(self.store._set_aside_by_keys(aside, bound))
        finally:
            del self.store._backend.set_aside_by_keys  # type: ignore
            del self.store._backend.max_bound_parameters  # type: ignore

    def test_bulk_assign_acts_on_the_same_set(self) -> None:
        rows, total = self._page(include_unassigned=True)
        updated = self.store.bulk_set_assignee(
            "carol", "alice", CREATED, open_items=True,
            include_unassigned=True, now=self.NOW)
        self.assertEqual(updated, total)
        carol = self._keys(self.store.dashboard(
            assignees=["carol"], include_retired=True))
        self.assertEqual(sorted(carol), sorted(rows))
        for triple in self._x():
            self.assertNotIn(triple, carol)

    def test_with_no_mark_today_exactly(self) -> None:
        self.store.unmark_flaky(list(self._marks()), "bob", self.NOW)
        today = self._today()
        self.assertEqual(self._page(), (today, len(today)))
        self.assertEqual(self.store.bulk_set_assignee(
            "carol", "alice", CREATED, open_items=True, now=self.NOW),
            len(today))


class OpenAssignmentFiguresUnchangedTest(FlakyListsBase):
    """WP-44 item 8 (design §1, §2 B.1, replacing DormantFiguresTest):
    every open-assignment figure and queue is the same with and without
    the marks, bar the rows' ``flaky``: ``assigned_open``, the assigned
    queue's count and rows, and one person's (My actions)."""

    def _figures(self) -> Dict[str, Any]:
        cutoff = self.NOW - datetime.timedelta(hours=36)
        self.store._invalidate_summary_cache()

        def bare(rows: Sequence[Any]) -> List[Any]:
            return [row._replace(flaky=None) for row in rows]

        counts = self.store.queue_counts(
            assignee="bob", stale_before=cutoff, now=self.NOW)
        return {
            "assigned_open": self.store.assigned_open_count(),
            "assigned": counts["assigned"],
            "mine": counts["mine"],
            "assigned_rows": bare(self.store.status_queue(
                "assigned", stale_before=cutoff, now=self.NOW,
                with_latest_comment=True)),
            "mine_rows": bare(self.store.status_queue(
                "assigned", assignee="bob", stale_before=cutoff,
                now=self.NOW, with_latest_comment=True)),
            "mine_count": self.store.status_queue_count(
                "assigned", assignee="bob", stale_before=cutoff,
                now=self.NOW),
            "open_bob": self._keys(self.store.dashboard_page(
                open_items=True, assignees=["bob"], with_flaky=True,
                now=self.NOW)[0]),
        }

    def test_byte_identical_with_and_without_the_marks(self) -> None:
        marked = self._figures()
        self.assertIn(("linux-sim", "suite.py", "t_assigned"),
                      [(r.environment, r.script, r.test_name)
                       for r in marked["assigned_rows"]])
        self.store.unmark_flaky(list(self._marks()), "bob", self.NOW)
        self.assertEqual(self._figures(), marked)


class BrowseFlakingFilterTest(FlakyListsBase):
    """WP-44 item 8 (design §1 "The browse Result filter", §2 B.3(c),
    decision 24): FLAKING matches F, FAIL alone every other FAIL row (a
    muted flaking one included), and the two partition today's FAIL,
    rows and total; paging across a boundary at every sort; with
    Passing; a planted parameter ceiling answers alike by keys; bulk
    agrees. On MariaDB with 101 marked tests (decision 22)."""

    def _read(self, flaking: Optional[bool], **kwargs: Any
              ) -> Tuple[List[Tuple[str, str, str]], int]:
        kwargs.setdefault("results", [Result.FAIL])
        rows, total = self.store.dashboard_page(
            flaking=flaking, with_flaky=True, now=self.NOW, **kwargs)
        return self._keys(rows), total

    def _today(self, **kwargs: Any) -> List[Tuple[str, str, str]]:
        kwargs.setdefault("results", [Result.FAIL])
        return self._keys(self.store.dashboard(now=self.NOW, **kwargs))

    def test_fail_and_flaking_partition_todays_fail(self) -> None:
        f = self._f()
        today = self._today()
        fail, fail_total = self._read(False)
        flaking, flaking_total = self._read(True)
        self.assertEqual(set(flaking), f)
        self.assertEqual(flaking, [t for t in today if t in f])
        self.assertEqual(fail, [t for t in today if t not in f])
        self.assertEqual((fail_total, flaking_total),
                         (len(fail), len(flaking)))
        self.assertEqual(fail_total + flaking_total, len(today))
        both, total = self._read(None)
        self.assertEqual((both, total), (today, len(today)))
        # The muted flaking test is under FAIL; the assigned one FLAKING.
        self.assertIn("t_muted", [t[2] for t in fail])
        self.assertIn("t_assigned", [t[2] for t in flaking])
        # Every row FLAKING returns reads FLAKING: a flaking state and no
        # live mute; its wire result stays FAIL.
        rows = self.store.dashboard_page(
            results=[Result.FAIL], flaking=True, with_flaky=True,
            now=self.NOW)[0]
        for row in rows:
            self.assertIs(row.result, Result.FAIL)
            self.assertEqual(row.flaky.state.state, "flaking")
            self.assertTrue(row.mute is None
                            or row.mute.expires_at <= self.NOW)

    def test_retired_flaking_reads_flaking_where_retired_are_kept(
            self) -> None:
        flaking, _total = self._read(True, include_retired=True)
        self.assertEqual(set(flaking), self._f(include_retired=True))
        self.assertIn("t_retired", [t[2] for t in flaking])
        fail, _total = self._read(False, include_retired=True)
        self.assertNotIn("t_retired", [t[2] for t in fail])

    def test_paging_across_a_boundary_at_every_sort(self) -> None:
        f = self._f()
        for sort in storage.DASHBOARD_SORTS:
            for descending in (False, True):
                today = self._today(sort=sort, descending=descending)
                for flaking, wanted in ((True, [t for t in today if t in f]),
                                        (False,
                                         [t for t in today if t not in f])):
                    paged = []  # type: List[Tuple[str, str, str]]
                    for offset in range(0, len(wanted) + 11, 11):
                        rows, total = self._read(
                            flaking, sort=sort, descending=descending,
                            limit=11, offset=offset)
                        self.assertEqual(total, len(wanted))
                        paged.extend(rows)
                    self.assertEqual(paged, wanted,
                                     (sort, descending, flaking))

    def test_with_passing_also_chosen(self) -> None:
        f = self._f()
        results = [Result.FAIL, Result.PASS]
        today = self._today(results=results)
        passing = [t for t in today if t not in self._today()]
        self.assertTrue(passing)
        flaking, total = self._read(True, results=results)
        self.assertEqual(flaking, [t for t in today
                                   if t in f or t in passing])
        self.assertEqual(total, len(flaking))
        fail, total = self._read(False, results=results)
        self.assertEqual(fail, [t for t in today if t not in f])
        self.assertEqual(total, len(fail))

    def test_scoped_to_the_environment_and_script(self) -> None:
        f = self._f()
        for scope in ({"environment": self.ENV}, {"script": self.SCRIPT},
                      {"environments": [self.MANY]},
                      {"environments": []}):
            today = self._today(**scope)
            self.assertEqual(self._read(True, **scope)[0],
                             [t for t in today if t in f], scope)
            self.assertEqual(self._read(False, **scope)[0],
                             [t for t in today if t not in f], scope)

    def test_a_planted_ceiling_answers_alike(self) -> None:
        """Above the backend's ceiling on bound parameters (999 before
        SQLite 3.32, about 330 of F) the terms give way to the keys form,
        which answers exactly (design §6: a test plants the ceiling)."""
        expected = {
            (flaking, offset): self._read(
                flaking, sort="start_time", descending=True, limit=13,
                offset=offset)
            for flaking in (True, False) for offset in (0, 13, 50)}
        self.store._backend.max_bound_parameters = 60  # type: ignore
        try:
            for (flaking, offset), answer in expected.items():
                self.store._invalidate_summary_cache()
                self.assertEqual(self._read(
                    flaking, sort="start_time", descending=True, limit=13,
                    offset=offset), answer, (flaking, offset))
                self.assertEqual(self.store.dashboard_count(
                    results=[Result.FAIL], flaking=flaking, now=self.NOW),
                    answer[1])
        finally:
            del self.store._backend.max_bound_parameters  # type: ignore

    def test_bulk_assign_agrees(self) -> None:
        f = self._f()
        flaking, total = self._read(True)
        updated = self.store.bulk_set_assignee(
            "carol", "alice", CREATED, results=[Result.FAIL], flaking=True,
            now=self.NOW)
        self.assertEqual(updated, total)
        carol = self._keys(self.store.dashboard(assignees=["carol"]))
        self.assertEqual(set(carol), f)

    def test_with_no_mark_flaking_alone_reads_nothing(self) -> None:
        self.store.unmark_flaky(list(self._marks()), "bob", self.NOW)
        self.assertEqual(self.store.dashboard_page(
            results=[Result.FAIL], flaking=True, with_flaky=False,
            now=self.NOW), ([], 0))
        self.assertEqual(self.store.dashboard_count(
            results=[Result.FAIL], flaking=True, now=self.NOW), 0)
        today = self._today()
        self.assertEqual(self._read(False)[0], today)
        # With Passing too, FLAKING adds nothing to PASS.
        passing = self._today(results=[Result.PASS])
        self.assertEqual(self._read(
            True, results=[Result.FAIL, Result.PASS])[0], passing)


class FlakyTabOrderTest(FlakyListsBase):
    """WP-44 item 8 (design §1 "The Flaky tab", decision 16): every mark
    whose test ran on the stream, failing first, then flaking, then
    passing, newest mark first, retired last; ``total`` apart from
    ``retired``; ``recent_failures`` of the last 20 runs; paged by the
    server. On MariaDB with 101 marked tests (decision 22)."""

    def _all(self, **kwargs: Any) -> Any:
        return self.store.flaky_tab(
            storage.MAINLINE_STREAM_ID, self.NOW, limit=1000, **kwargs)

    def test_the_order(self) -> None:
        # One mark made later than the rest: newest first in its state.
        self.store.unmark_flaky(
            [(self.ENV, self.SCRIPT, "t_short")], "bob", self.NOW)
        self.store.mark_flaky([(self.ENV, self.SCRIPT, "t_short", None)], 5,
                              "again", "bob", self.NOW)
        page = self._all(environment=self.ENV)
        states = self.oracle()["states"]
        rank = {"failing": 0, "flaking": 1, "passing": 2}
        live = [e for e in page.entries if not e.retired]
        retired = [e for e in page.entries if e.retired]
        self.assertEqual([e.mark.test_name for e in retired], ["t_retired"])
        self.assertEqual(page.entries[-len(retired):], retired)
        self.assertEqual((page.total, page.retired), (len(live), 1))
        expected = sorted(
            live, key=lambda e: (rank[e.state.state],
                                 -e.mark.marked_at.timestamp(),
                                 e.mark.test_name))
        self.assertEqual(live, expected)
        self.assertEqual(live[0].mark.test_name, "t_short")
        for entry in page.entries:
            triple = (entry.mark.environment, entry.mark.script,
                      entry.mark.test_name)
            self.assertEqual(entry.state, states[triple], triple)
        self.assertEqual(
            [e.mark.test_name for e in live
             if e.state.state == "flaking"],
            ["t_assigned", "t_flaking_new", "t_flaking_still", "t_muted"])

    def test_the_fields(self) -> None:
        entries = {e.mark.test_name: e
                   for e in self._all(environment=self.ENV).entries}
        over = entries["t_over"]
        self.assertEqual((over.recent_failures, over.recent_runs), (5, 5))
        self.assertIs(over.result, Result.FAIL)
        tolerated = entries["t_tolerated"]
        self.assertEqual(
            (tolerated.recent_failures, tolerated.recent_runs), (2, 4))
        self.assertEqual(tolerated.state.state, "passing")
        self.assertTrue(entries["t_muted"].muted)
        self.assertFalse(entries["t_flaking_new"].muted)
        self.assertEqual(entries["t_assigned"].assignee, "bob")
        self.assertIsNone(entries["t_flaking_new"].assignee)
        self.assertEqual(entries["t_short"].mark.cadence, 5)
        self.assertEqual(entries["t_short"].mark.reason, "why")
        self.assertEqual(entries["t_short"].mark.marked_by, "alice")
        self.assertNotIn("t_new", entries)

    def test_the_last_twenty_runs(self) -> None:
        self.store.upsert_runs([
            make_record(test_name="t_marked_pass", start=self._night(
                self.NIGHTS + day), result=(
                    Result.FAIL if day % 3 == 0 else Result.PASS))
            for day in range(30)])
        entry = [e for e in self._all(environment=self.ENV).entries
                 if e.mark.test_name == "t_marked_pass"][0]
        self.assertEqual(entry.recent_runs, 20)
        newest = [Result.FAIL if day % 3 == 0 else Result.PASS
                  for day in range(29, 9, -1)]
        self.assertEqual(entry.recent_failures,
                         newest.count(Result.FAIL))

    def test_paged_by_the_server(self) -> None:
        whole = self._all()
        self.assertEqual(whole.total + whole.retired, len(whole.entries))
        self.assertGreater(len(whole.entries), 100)
        paged = []  # type: List[Any]
        for offset in range(0, len(whole.entries) + 100, 100):
            page = self.store.flaky_tab(
                storage.MAINLINE_STREAM_ID, self.NOW, limit=100,
                offset=offset)
            self.assertEqual((page.total, page.retired),
                             (whole.total, whole.retired))
            self.assertLessEqual(len(page.entries), 100)
            paged.extend(page.entries)
        self.assertEqual(paged, whole.entries)

    def test_a_mark_with_no_run_on_the_stream_is_absent(self) -> None:
        self.store.upsert_runs([make_record(
            test_name="t_new", result=Result.FAIL, build="rc1",
            start=self._night(self.NIGHTS))])
        build = [s for s in self.store.list_streams("")
                 if s.stream_id != storage.MAINLINE_STREAM_ID][0]
        page = self.store.flaky_tab(build.stream_id, self.NOW)
        self.assertEqual((page.total, page.retired, page.entries),
                         (0, 0, []))
        self.store.mark_flaky([(self.ENV, self.SCRIPT, "t_new", None)], 3,
                              "why", "bob", self.NOW)
        page = self.store.flaky_tab(build.stream_id, self.NOW)
        self.assertEqual([e.mark.test_name for e in page.entries], ["t_new"])
        # Its one run on the build failed: over the cadence there.
        self.assertEqual(page.entries[0].state.state, "failing")
        self.assertEqual((page.entries[0].recent_failures,
                          page.entries[0].recent_runs), (1, 1))

    def test_scoped_by_environment_and_product(self) -> None:
        many = self._all(environments=[self.MANY])
        self.assertEqual((many.total, many.retired), (101, 0))
        self.assertTrue(all(e.mark.environment == self.MANY
                            for e in many.entries))
        self.assertEqual(self._all(environments=[]).entries, [])
        self.assertEqual(self._all(environment="nowhere").total, 0)


class FlakyTrendTest(FlakyListsBase):
    """WP-44 item 8 (design §1 "The trend", decision 2): each night's
    tolerated failures of marked tests as the night stood then, under
    today's marks, no live mute now, against the pure rule over every
    test's whole history; the runs before the window are read where the
    window's first run needs them; the assigned ones are counted once."""

    def _oracle(self, since: datetime.datetime
                ) -> Dict[datetime.date, Tuple[int, int]]:
        marks = self._marks()
        out = {}  # type: Dict[datetime.date, Tuple[int, int]]
        for triple, cadence in marks.items():
            if triple[2] in self.MUTED:
                continue
            runs = list(reversed(self.store.run_history(
                triple[0], triple[1], triple[2], limit=1000)))
            flags = analytics.flaking_that_night(
                cadence, [run.result for run in runs])
            for run, flaking in zip(runs, flags):
                if not flaking or run.start_time < since:
                    continue
                day = run.start_time.date()
                count, assigned = out.get(day, (0, 0))
                out[day] = (count + 1, assigned + (
                    1 if triple[2] in self.ASSIGNED else 0))
        return out

    def _read(self, since: datetime.datetime
              ) -> Dict[datetime.date, Tuple[int, int]]:
        self.store._invalidate_summary_cache()
        return {entry.day: (entry.flaking, entry.assigned)
                for entry in self.store.daily_flaking_failures(
                    since, self.NOW)}

    def test_each_night_as_it_stood(self) -> None:
        for days in (0, 6, 8, 9):
            since = BASE + datetime.timedelta(days=days)
            self.assertEqual(self._read(since), self._oracle(since), days)
        whole = self._read(BASE)
        self.assertTrue(any(assigned for _c, assigned in whole.values()))

    def test_the_window_reads_the_runs_before_it(self) -> None:
        """t_flaking_still (P F F) seen from night 8: alone, its two
        FAILs would be over a cadence of 3; with night 7's pass, both
        flake."""
        since = BASE + datetime.timedelta(days=8)
        self.store._invalidate_summary_cache()
        got = {e.day: e.flaking for e in self.store.daily_flaking_failures(
            since, self.NOW, environment=self.ENV)}
        oracle = self._oracle(since)
        linux = {}  # type: Dict[datetime.date, int]
        for triple, cadence in self._marks().items():
            if triple[0] != self.ENV or triple[2] in self.MUTED:
                continue
            runs = list(reversed(self.store.run_history(
                triple[0], triple[1], triple[2], limit=1000)))
            for run, flaking in zip(runs, analytics.flaking_that_night(
                    cadence, [run.result for run in runs])):
                if flaking and run.start_time >= since:
                    linux[run.start_time.date()] = linux.get(
                        run.start_time.date(), 0) + 1
        self.assertEqual(got, linux)
        self.assertLess(sum(linux.values()),
                        sum(count for count, _a in oracle.values()))
        night8 = (BASE + datetime.timedelta(days=8)).date()
        self.assertGreaterEqual(got[night8], 1)

    def test_a_live_mute_wins_and_expires(self) -> None:
        later = self.NOW + datetime.timedelta(days=2)
        self.store._invalidate_summary_cache()
        now_total = sum(e.flaking for e in self.store.daily_flaking_failures(
            BASE, self.NOW, environment=self.ENV))
        later_total = sum(
            e.flaking for e in self.store.daily_flaking_failures(
                BASE, later, environment=self.ENV))
        # t_muted (P F F) flakes on both its FAILs once the mute is out;
        # t_muted_tolerated (F P F P) on its second FAIL.
        self.assertEqual(later_total - now_total, 3)

    def test_with_no_mark_nothing(self) -> None:
        self.store.unmark_flaky(list(self._marks()), "bob", self.NOW)
        self.assertEqual(self.store.daily_flaking_failures(
            BASE, self.NOW), [])

    def test_the_pure_rule(self) -> None:
        F, P, UP = self.F, self.P, self.UP
        self.assertEqual(analytics.flaking_that_night(3, [P, F, F, F, P, F]),
                         [False, True, True, False, False, True])
        self.assertEqual(analytics.flaking_that_night(2, [F, F, UP, F]),
                         [False, False, False, True])
        # A window shorter than N, all FAIL: over, not flaking.
        self.assertEqual(analytics.flaking_that_night(3, [F, F]),
                         [False, False])
        self.assertEqual(analytics.flaking_that_night(
            3, [P, F, F, F], first=2), [True, False])
