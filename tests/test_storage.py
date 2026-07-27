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
from typing import List, Optional

from testboard import analytics, model, storage
from testboard.model import Result, RunRecord
from testboard.storage import Storage

BASE = datetime.datetime(2026, 7, 1, 2, 0, 0)
CREATED = datetime.datetime(2026, 7, 1, 9, 0, 0)


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
    )


class StorageTestBase(unittest.TestCase):
    """Creates a Storage on a temp-file database with safe cleanup."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="testboard_storage_")
        # LIFO cleanup: connections are closed before the dir is removed.
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = Storage(self.db_path)
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
        self.assertEqual(counts, storage.UpsertCounts(inserted=1, updated=0))
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
        self.assertEqual(counts, storage.UpsertCounts(inserted=2, updated=0))

    def test_mixed_insert_and_update_counts(self) -> None:
        self.store.upsert_runs([make_record(test_name="test_a")])
        counts = self.store.upsert_runs(
            [
                make_record(test_name="test_a", result=Result.FAIL),
                make_record(test_name="test_b"),
            ]
        )
        self.assertEqual(counts, storage.UpsertCounts(inserted=1, updated=1))

    def test_reimport_identical_batch_all_updated_no_duplicates(self) -> None:
        batch = [
            make_record(test_name="test_a"),
            make_record(test_name="test_b"),
            make_record(test_name="test_c"),
        ]
        first = self.store.upsert_runs(batch)
        second = self.store.upsert_runs(batch)
        self.assertEqual(first, storage.UpsertCounts(inserted=3, updated=0))
        self.assertEqual(second, storage.UpsertCounts(inserted=0, updated=3))
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
        self.assertEqual(counts, storage.UpsertCounts(inserted=0, updated=1))
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
        self.assertEqual(counts, storage.UpsertCounts(inserted=2, updated=0))
        history = self.store.run_history("linux-sim", "suite.py", "test_a")
        self.assertEqual(len(history), 2)

    def test_empty_batch(self) -> None:
        counts = self.store.upsert_runs([])
        self.assertEqual(counts, storage.UpsertCounts(inserted=0, updated=0))


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
        conn.set_trace_callback(seen.append)
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
        """
        import ast
        import io
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "testboard", "storage.py")
        with io.open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr):
                    docstrings.add(id(body[0].value))

        found = []  # type: List[str]
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.append(node.value)
        return found

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
            counts, storage.UpsertCounts(inserted=5000, updated=0)
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
        """Read prev_result straight out of the table, bypassing the API."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT prev_result FROM latest_runs"
            ).fetchone()[0]
        finally:
            conn.close()

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


class TestLatestRunsMaintenance(StorageTestBase):
    """latest_runs stays in lockstep with upserts, including backfills."""

    def latest_pointer(self) -> Optional[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT run_id, start_time FROM latest_runs WHERE "
                "environment = 'linux-sim' AND script = 'suite.py' "
                "AND test_name = 'test_a'"
            ).fetchone()
        finally:
            conn.close()

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
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM run_outputs"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_output_is_stored_compressed(self) -> None:
        """Log text is ~75% of the database at scale, so it is deflated."""
        text = "2026-07-26 02:14:33 INFO harness: step done\n" * 400
        self.store.upsert_runs([make_record(output=text)])
        run_id = self.store.dashboard()[0].run_id
        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT output FROM run_outputs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        finally:
            conn.close()
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
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE run_outputs SET output = ? WHERE run_id = ?",
                ("legacy uncompressed text\n", run_id),
            )
            conn.commit()
        finally:
            conn.close()
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
        conn = sqlite3.connect(self.db_path)
        try:
            outputs = conn.execute(
                "SELECT COUNT(*) FROM run_outputs"
            ).fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(outputs, runs)
        finally:
            conn.close()

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
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertIsNone(
                conn.execute(
                    "SELECT prev_result FROM latest_runs"
                ).fetchone()[0]
            )
        finally:
            conn.close()
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
