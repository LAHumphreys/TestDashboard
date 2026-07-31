"""Tests for ``tools/diagnose_db.py`` and the page-cache settings.

This tool exists to settle an argument — *is the network mount slow, or
is a query wrong?* — and its whole value is that it answers correctly
when someone is about to spend a week moving a database. So the verdict
logic is tested directly rather than only observed.

That matters more than usual here: a development machine with a fast SSD
and plenty of RAM can only ever produce the "not the storage" answer, so
the branch that will actually fire in production is the one that would
otherwise never run before it ran for real.

Python 3.6 compatible; standard library only.
"""

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List, Tuple

from testboard.storage import DEFAULT_MAX_CONNECTIONS, Storage
from tools import diagnose_db


def verdict(remote: List[Tuple[str, float]],
            local: List[Tuple[str, float]]) -> str:
    """Run the comparison and return what it printed."""
    out = io.StringIO()
    diagnose_db._compare(out, remote, local)
    return out.getvalue()


class VerdictTest(unittest.TestCase):
    """Storage, or query? The answer someone acts on."""

    def test_a_slow_mount_is_named_as_the_storage(self) -> None:
        """The production case: same queries, same data, ten times slower."""
        text = verdict(
            [("home", 4.0), ("triage", 2.0), ("filters", 1.0)],
            [("home", 0.4), ("triage", 0.2), ("filters", 0.1)])
        self.assertIn("THE STORAGE", text)
        self.assertIn("10.0x", text)

    def test_a_partial_difference_is_not_oversold(self) -> None:
        """2x is worth acting on but is not "the filesystem is everything"."""
        text = verdict(
            [("home", 0.4), ("triage", 0.4)],
            [("home", 0.2), ("triage", 0.2)])
        self.assertIn("MOSTLY THE STORAGE", text)
        self.assertIn("page", text)

    def test_an_equally_slow_local_copy_acquits_the_storage(self) -> None:
        """Slow in both places means the query is wrong, and moving the
        database would be a week spent on the wrong thing."""
        text = verdict(
            [("home", 4.0), ("triage", 3.0)],
            [("home", 3.9), ("triage", 3.0)])
        self.assertIn("NOT THE STORAGE", text)
        self.assertIn("queries themselves", text)

    def test_instant_everywhere_says_look_somewhere_else_entirely(
        self
    ) -> None:
        """Neither answer is right when nothing measured is slow at all.

        Reporting "not the storage, so it's the queries" here would be a
        lie: the queries are not slow either, and the time is going into
        something this tool never looked at.
        """
        text = verdict(
            [("home", 0.001), ("triage", 0.002)],
            [("home", 0.001), ("triage", 0.002)])
        self.assertIn("NOTHING HERE IS SLOW", text)
        self.assertIn("curl", text)

    def test_noise_is_excluded_and_the_exclusion_is_declared(self) -> None:
        """A sub-millisecond pair must not outvote a four-second one."""
        text = verdict(
            [("slow", 4.0)] + [("fast%d" % i, 0.001) for i in range(10)],
            [("slow", 0.4)] + [("fast%d" % i, 0.002) for i in range(10)])
        self.assertIn("THE STORAGE", text)
        self.assertIn("10 queries excluded", text)

    def test_every_row_is_shown_even_when_excluded(self) -> None:
        """Excluding a measurement from the verdict is not hiding it."""
        text = verdict([("trivial", 0.001)], [("trivial", 0.001)])
        self.assertIn("trivial", text)

    def test_a_query_missing_from_one_side_is_skipped(self) -> None:
        text = verdict([("home", 4.0), ("gone", 1.0)], [("home", 0.4)])
        self.assertIn("THE STORAGE", text)


class PlanTest(unittest.TestCase):
    """Which query plans deserve an alarm."""

    def test_scanning_runs_is_flagged(self) -> None:
        self.assertTrue(diagnose_db._scans_a_big_table("SCAN runs"))
        self.assertTrue(diagnose_db._scans_a_big_table(
            "SCAN run_outputs USING INDEX x"))

    def test_scanning_latest_runs_is_not_flagged(self) -> None:
        """It holds one row per test and is designed to be scanned.

        Flagging it would send someone optimising the thing that already
        makes the dashboard fast.
        """
        self.assertFalse(diagnose_db._scans_a_big_table(
            "SCAN latest_runs USING INDEX sqlite_autoindex_latest_runs_1"))

    def test_a_search_is_never_a_scan(self) -> None:
        self.assertFalse(diagnose_db._scans_a_big_table(
            "SEARCH runs USING INDEX x (environment=?)"))


class ReportTest(unittest.TestCase):
    """The findings a person reads before deciding anything."""

    def test_a_small_cache_against_a_big_database_is_called_out(self) -> None:
        out = io.StringIO()
        diagnose_db._describe_cache(
            {"cache_size": -2000, "page_size": 4096,
             "size": 900 * 1024 * 1024}, out)
        text = out.getvalue()
        self.assertIn("PER CONNECTION", text)
        self.assertIn("2.0 MB", text)
        self.assertIn("900.0 MB", text)
        self.assertIn("--cache-mb", text)

    def test_an_adequate_cache_is_reported_without_alarm(self) -> None:
        out = io.StringIO()
        diagnose_db._describe_cache(
            {"cache_size": -524288, "page_size": 4096,
             "size": 900 * 1024 * 1024}, out)
        self.assertNotIn("--cache-mb", out.getvalue())

    def test_a_positive_cache_size_is_read_as_pages(self) -> None:
        """The sign is the unit, and getting it backwards is a 4096x error."""
        out = io.StringIO()
        diagnose_db._describe_cache(
            {"cache_size": 2000, "page_size": 4096, "size": 10 ** 9}, out)
        self.assertIn("7.8 MB", out.getvalue())

    def test_a_journal_that_is_not_wal_is_explained(self) -> None:
        """The headline finding on a network mount, and easy to miss.

        PRAGMA journal_mode does not fail when it cannot switch; it
        returns whatever mode you ended up in, so the server can be
        asking for WAL and running in delete without a word.
        """
        out = io.StringIO()
        diagnose_db._describe_journal({"journal_mode": "delete"}, out)
        text = out.getvalue()
        self.assertIn("NOT 'wal'", text)
        self.assertIn("network filesystems", text)
        self.assertIn("does not", text)
        self.assertIn("fail when it cannot switch", text)

    def test_wal_says_nothing(self) -> None:
        out = io.StringIO()
        diagnose_db._describe_journal({"journal_mode": "wal"}, out)
        self.assertEqual(out.getvalue(), "")

    def test_bytes_are_rendered_the_way_people_read_them(self) -> None:
        self.assertEqual(diagnose_db.human_bytes(900 * 1024 * 1024),
                         "900.0 MB")
        self.assertEqual(diagnose_db.human_bytes(512), "512.0 B")


class EndToEndTest(unittest.TestCase):
    """The tool runs against a real database and says something useful."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_diag_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        Storage(self.db).close()

    def run_tool(self, *extra: str) -> str:
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = diagnose_db.main(["--db", self.db, "--repeat", "1"] +
                                    list(extra))
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_it_reports_settings_queries_and_storage(self) -> None:
        text = self.run_tool()
        self.assertIn("journal_mode", text)
        self.assertIn("home: summary rollup", text)
        # SQLite 3.36 dropped the word TABLE from query-plan lines;
        # RHEL 8's 3.26 (the ubi8 CI leg) still prints it. The claim
        # being tested is the same on both: the run lookup is an index
        # SEARCH, not a scan.
        self.assertRegex(text, r"SEARCH (TABLE )?runs USING INDEX")

    def test_every_dashboard_query_actually_runs(self) -> None:
        """A query in the list with a stale signature would say FAILED."""
        self.assertNotIn("FAILED", self.run_tool())

    def test_a_missing_database_is_refused_not_created(self) -> None:
        missing = os.path.join(self.tmp, "absent.db")
        stderr = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(stderr):
            code = diagnose_db.main(["--db", missing])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(missing))

    def test_skip_queries_still_reports_the_settings(self) -> None:
        text = self.run_tool("--skip-queries")
        self.assertIn("journal_mode", text)
        self.assertNotIn("home: summary rollup", text)


class CacheBudgetTest(unittest.TestCase):
    """A cache budget is for the process, and is divided, never multiplied.

    Connections are thread-local, so a number handed straight to PRAGMA
    cache_size is taken by *every* thread. Asking for 512 MB on a
    sixteen-thread server and getting 8 GB is a memory-exhaustion bug
    wearing a performance fix's clothes.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_cache_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")

    def pragma(self, storage: Storage, name: str) -> Any:
        return storage._conn().execute("PRAGMA " + name).fetchone()[0]

    def test_the_default_is_sqlites_own(self) -> None:
        """Untuned behaviour must not change for anyone who did not ask."""
        storage = Storage(self.db)
        self.addCleanup(storage.close)
        self.assertEqual(self.pragma(storage, "cache_size"), -2000)
        self.assertEqual(self.pragma(storage, "mmap_size"), 0)

    def test_the_budget_is_divided_among_connections(self) -> None:
        storage = Storage(self.db, cache_mb=512, max_connections=16)
        self.addCleanup(storage.close)
        self.assertEqual(self.pragma(storage, "cache_size"), -32768)
        self.assertEqual(storage.cache_bytes_per_connection(),
                         32 * 1024 * 1024)

    def test_the_whole_budget_is_never_exceeded(self) -> None:
        """The property that keeps this from being a foot-gun."""
        for budget in (64, 512, 4096):
            for threads in (1, 4, 16, 64):
                storage = Storage(self.db, cache_mb=budget,
                                  max_connections=threads)
                total = storage.cache_bytes_per_connection() * threads
                storage.close()
                # The floor can push a tiny budget up; that is deliberate
                # and is the only case allowed to exceed.
                if budget * 1024 * 1024 // threads >= 2000 * 1024:
                    self.assertLessEqual(total, budget * 1024 * 1024,
                                         (budget, threads))

    def test_a_budget_never_shrinks_below_sqlites_default(self) -> None:
        """A misconfiguration must not make things worse than untuned."""
        storage = Storage(self.db, cache_mb=1, max_connections=64)
        self.addCleanup(storage.close)
        self.assertEqual(self.pragma(storage, "cache_size"), -2000)

    def test_mmap_is_off_unless_asked_for(self) -> None:
        """It is worth little on the network mount this exists to help."""
        storage = Storage(self.db, cache_mb=256)
        self.addCleanup(storage.close)
        self.assertEqual(self.pragma(storage, "mmap_size"), 0)

    def test_mmap_is_set_in_bytes_when_asked_for(self) -> None:
        storage = Storage(self.db, mmap_mb=64)
        self.addCleanup(storage.close)
        self.assertEqual(self.pragma(storage, "mmap_size"), 64 * 1024 * 1024)

    def test_the_default_connection_count_is_stated_not_guessed(self) -> None:
        self.assertGreaterEqual(DEFAULT_MAX_CONNECTIONS, 1)
        storage = Storage(self.db, cache_mb=DEFAULT_MAX_CONNECTIONS * 32)
        self.addCleanup(storage.close)
        self.assertEqual(storage.cache_bytes_per_connection(),
                         32 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
