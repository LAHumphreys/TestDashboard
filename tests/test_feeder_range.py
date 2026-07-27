"""Tests for bounding an import, and for the two modes.

A site with years of history does not want all of it at once — the recent
data is the data anyone looks at — so ``--since``/``--until`` cut a window
out of it. The upper bound is *exclusive* precisely so adjacent windows
tile a history exactly once: no run imported twice, none missed between
chunks. That property is what makes "bring in a year at a time" a safe
procedure rather than a thing to be careful about, so it is pinned first.

Chunked importing also makes the high-water mark's direction matter.
Bringing in 2024 after 2026 — the natural order, newest first — must not
rewind the mark, or the next scheduled run silently re-reads two years.

Python 3.6 compatible; standard library only.
"""

import datetime
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional

import run_feeder
from feeder import state
from feeder.submitter import Submitter
from testboard import model


def record(start: datetime.datetime) -> Dict[str, Any]:
    """A valid transport record starting at ``start``."""
    return {
        "environment": "prod", "script": "nightly.py",
        "test_name": "test_" + start.strftime("%Y%m%d"),
        "result": "PASS", "output": "",
        "start_time": model.format_iso(start),
        "end_time": model.format_iso(start),
    }


def monthly(count: int, first: datetime.datetime) -> List[Dict[str, Any]]:
    """``count`` records, one every 30 days from ``first``."""
    return [
        record(first + datetime.timedelta(days=30 * index))
        for index in range(count)
    ]


class CapturingSubmitter(Submitter):
    """A Submitter that records batches instead of sending them."""

    def __init__(self, **options: Any) -> None:
        Submitter.__init__(self, "http://dash:8000", **options)
        self.batches = []  # type: List[List[Any]]

    def _send_batch(self, batch_number: int, records: List[Any],
                    reasons: Any) -> Any:
        self.batches.append(list(records))
        from feeder.submitter import _BatchResult
        self._max_accepted = max(r.start_time for r in records)
        return _BatchResult(True, len(records), 0, 0, None)

    def sent(self) -> List[datetime.datetime]:
        """Every start_time that reached a batch, in order."""
        return [r.start_time for batch in self.batches for r in batch]


class WindowTest(unittest.TestCase):
    """What --since and --until include, and what they leave out."""

    def setUp(self) -> None:
        self.records = monthly(36, datetime.datetime(2023, 8, 1))

    def send(self, **bounds: Optional[datetime.datetime]) -> List[Any]:
        submitter = CapturingSubmitter()
        submitter.submit(iter(self.records), **bounds)
        return submitter.sent()

    def test_no_bounds_sends_everything(self) -> None:
        self.assertEqual(len(self.send()), 36)

    def test_since_is_inclusive(self) -> None:
        """A run exactly at the bound belongs to the window above it."""
        edge = model.parse_iso(self.records[12]["start_time"])
        sent = self.send(since=edge)
        self.assertIn(edge, sent)
        self.assertEqual(len(sent), 24)

    def test_until_is_exclusive(self) -> None:
        """A run exactly at the bound belongs to the NEXT window."""
        edge = model.parse_iso(self.records[12]["start_time"])
        sent = self.send(until=edge)
        self.assertNotIn(edge, sent)
        self.assertEqual(len(sent), 12)

    def test_adjacent_windows_tile_the_history_exactly_once(self) -> None:
        """The property the whole chunking procedure rests on.

        Every run lands in exactly one window: none imported twice, none
        lost in the seam. Anything else would make "bring in a year at a
        time" a thing to be careful about rather than a procedure.
        """
        cuts = [model.parse_iso(self.records[index]["start_time"])
                for index in (12, 24)]
        first = self.send(until=cuts[0])
        middle = self.send(since=cuts[0], until=cuts[1])
        last = self.send(since=cuts[1])
        together = first + middle + last
        self.assertEqual(len(together), 36)
        self.assertEqual(len(set(together)), 36)

    def test_an_empty_window_sends_nothing_rather_than_erroring(self) -> None:
        """The CLI refuses this, but the submitter must not misbehave."""
        moment = model.parse_iso(self.records[5]["start_time"])
        self.assertEqual(self.send(since=moment, until=moment), [])

    def test_out_of_window_records_are_not_counted_as_skipped(self) -> None:
        """They are not bad data; the run must not look like it had errors."""
        submitter = CapturingSubmitter()
        stats = submitter.submit(
            iter(self.records),
            since=model.parse_iso(self.records[30]["start_time"]))
        self.assertEqual(stats.read, 36)
        self.assertEqual(stats.valid, 6)
        self.assertEqual(stats.skipped, 0)


class BatchSizingTest(unittest.TestCase):
    """Batches are flushed by bytes as well as by record count.

    Captured test output varies by orders of magnitude between a quiet
    pass and a failure that dumps a build log, so a fixed record count
    alone produces batches that are sometimes enormous — which the server
    refuses and which pins the whole batch in memory.
    """

    def test_record_count_still_flushes(self) -> None:
        submitter = CapturingSubmitter(batch_size=10)
        submitter.submit(iter(monthly(25, datetime.datetime(2026, 1, 1))))
        self.assertEqual([len(b) for b in submitter.batches], [10, 10, 5])

    def test_a_byte_ceiling_flushes_early(self) -> None:
        """Ten records of 100 kB each must not wait for the 500th."""
        big = []  # type: List[Dict[str, Any]]
        for index, base in enumerate(monthly(10, datetime.datetime(2026, 1, 1))):
            base["output"] = "x" * 100000
            big.append(base)
        submitter = CapturingSubmitter(
            batch_size=500, max_batch_bytes=250000)
        submitter.submit(iter(big))
        self.assertGreater(len(submitter.batches), 1)
        for batch in submitter.batches[:-1]:
            self.assertLessEqual(len(batch), 3)

    def test_every_record_still_arrives(self) -> None:
        """Flushing early must not drop the tail."""
        big = []  # type: List[Dict[str, Any]]
        for base in monthly(7, datetime.datetime(2026, 1, 1)):
            base["output"] = "y" * 100000
            big.append(base)
        submitter = CapturingSubmitter(max_batch_bytes=150000)
        stats = submitter.submit(iter(big))
        self.assertEqual(len(submitter.sent()), 7)
        self.assertEqual(stats.sent, 7)

    def test_one_oversized_record_is_still_sent(self) -> None:
        """Splitting a record is impossible; refusing it would lose data."""
        huge = monthly(1, datetime.datetime(2026, 1, 1))
        huge[0]["output"] = "z" * 500000
        submitter = CapturingSubmitter(max_batch_bytes=1000)
        submitter.submit(iter(huge))
        self.assertEqual(len(submitter.sent()), 1)


class HighWaterMarkDirectionTest(unittest.TestCase):
    """The mark records the newest run ever pushed, and only advances."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_hwm_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state.json")

    def test_a_newer_mark_is_saved(self) -> None:
        state.advance_high_water_mark(self.path, datetime.datetime(2026, 1, 1))
        self.assertTrue(state.advance_high_water_mark(
            self.path, datetime.datetime(2026, 6, 1)))
        self.assertEqual(state.load_high_water_mark(self.path),
                         datetime.datetime(2026, 6, 1))

    def test_an_older_mark_is_refused(self) -> None:
        """Importing 2024 after 2026 must not rewind the feed by two years."""
        newest = datetime.datetime(2026, 6, 1)
        state.advance_high_water_mark(self.path, newest)
        with self.assertLogs("feeder.state", level="INFO"):
            self.assertFalse(state.advance_high_water_mark(
                self.path, datetime.datetime(2024, 1, 15)))
        self.assertEqual(state.load_high_water_mark(self.path), newest)

    def test_refusing_says_why_rather_than_failing_silently(self) -> None:
        state.advance_high_water_mark(self.path, datetime.datetime(2026, 6, 1))
        with self.assertLogs("feeder.state", level="INFO") as caught:
            state.advance_high_water_mark(
                self.path, datetime.datetime(2024, 1, 15))
        message = "\n".join(caught.output)
        self.assertIn("only moves forwards", message)

    def test_an_equal_mark_is_a_no_op(self) -> None:
        same = datetime.datetime(2026, 6, 1)
        state.advance_high_water_mark(self.path, same)
        with self.assertLogs("feeder.state", level="INFO"):
            self.assertFalse(state.advance_high_water_mark(self.path, same))

    def test_the_first_mark_is_always_saved(self) -> None:
        self.assertTrue(state.advance_high_water_mark(
            self.path, datetime.datetime(2024, 1, 1)))


class ModeTest(unittest.TestCase):
    """``catchup`` resumes from the mark; it has nothing to do with today.

    The mode was called ``daily`` first, which invited exactly the wrong
    reading — "import today's runs" — and made it look as though the hour
    a cron job fired could matter. It cannot: the lower bound comes from
    the newest run previously accepted, so a machine that was off for a
    week catches the week up.
    """

    def test_catchup_resumes_from_the_mark(self) -> None:
        mark = datetime.datetime(2026, 7, 20, 2, 0, 0)
        self.assertEqual(
            run_feeder.compute_since("catchup", mark, None, 1),
            mark - datetime.timedelta(days=1))

    def test_catchup_does_not_depend_on_the_current_date(self) -> None:
        """Nothing in the computation reads a clock."""
        mark = datetime.datetime(2020, 1, 1)
        self.assertEqual(
            run_feeder.compute_since("catchup", mark, None, 0), mark)

    def test_a_machine_that_was_off_for_a_week_catches_the_week_up(
        self
    ) -> None:
        """The bound is the old mark, not seven days ago and not today."""
        stale = datetime.datetime(2026, 7, 1)
        self.assertEqual(
            run_feeder.compute_since("catchup", stale, None, 0), stale)

    def test_daily_is_still_accepted_as_an_alias(self) -> None:
        """Scheduled commands already say it; renaming must not break them."""
        mark = datetime.datetime(2026, 7, 20)
        self.assertIn("daily", run_feeder.MODES)
        self.assertEqual(
            run_feeder.compute_since("daily", mark, None, 1),
            run_feeder.compute_since("catchup", mark, None, 1))

    def test_backfill_never_consults_the_mark(self) -> None:
        mark = datetime.datetime(2026, 7, 20)
        self.assertIsNone(run_feeder.compute_since("backfill", mark, None, 1))

    def test_the_first_catchup_run_imports_everything(self) -> None:
        self.assertIsNone(run_feeder.compute_since("catchup", None, None, 1))


class DescribeWindowTest(unittest.TestCase):
    """The log line that states what a run is about to import."""

    def describe(self, since: Any, until: Any) -> str:
        return run_feeder.describe_window(since, until, model)

    def test_both_bounds_show_the_half_open_interval(self) -> None:
        """The bracket notation says which end is exclusive without prose."""
        text = self.describe(datetime.datetime(2024, 8, 1),
                             datetime.datetime(2025, 8, 1))
        self.assertIn("[2024-08-01T00:00:00.000000", text)
        self.assertIn("2025-08-01T00:00:00.000000)", text)

    def test_a_lower_bound_alone(self) -> None:
        self.assertIn(">=", self.describe(datetime.datetime(2024, 8, 1), None))

    def test_an_upper_bound_alone(self) -> None:
        self.assertIn("<", self.describe(None, datetime.datetime(2025, 8, 1)))

    def test_no_bounds_says_so_rather_than_printing_nothing(self) -> None:
        self.assertIn("no bounds", self.describe(None, None))


if __name__ == "__main__":
    unittest.main()
