"""Tests for ``--check-reader``: offline validation of a site's reader.

Writing the reader is the only bespoke work in a rollout, and it is
written against a system this repository knows nothing about. This is the
loop its author works in, so what it reports has to be right, and — for
the sanity heuristics — has to be worth reporting at all.

The heuristics earn their place by catching mistakes that are *silent*:
records that validate, import cleanly, and are wrong. The largest of
those is local time passed through as if it were UTC, which shifts every
run by a whole number of hours and quietly moves "failing since", the
day-of-week profile and the trend.

Python 3.6 compatible; standard library only.
"""

import datetime
import logging
import unittest
from typing import Any, Dict, Iterator, List, Optional

from feeder import check

NOW = datetime.datetime(2026, 7, 26, 6, 0, 0)


def record(**overrides: Any) -> Dict[str, Any]:
    """A valid transport record, with fields overridden as asked."""
    base = {
        "environment": "prod",
        "script": "nightly_suite",
        "test_name": "test_one",
        "result": "PASS",
        "output": "",
        "start_time": "2026-07-26T03:00:00.000000",
        "end_time": "2026-07-26T03:00:02.000000",
    }
    base.update(overrides)
    return base


def many(count: int, **overrides: Any) -> List[Dict[str, Any]]:
    """``count`` distinct valid records sharing the given overrides."""
    return [
        record(test_name="test_{0}".format(index), **overrides)
        for index in range(count)
    ]


class CollectingLogger(logging.Logger):
    """A logger that keeps the formatted lines written to it."""

    def __init__(self) -> None:
        logging.Logger.__init__(self, "collecting", logging.INFO)
        self.lines = []  # type: List[str]

    def handle(self, record_: logging.LogRecord) -> None:
        self.lines.append(record_.getMessage())

    def text(self) -> str:
        """Everything logged, as one blob to search."""
        return "\n".join(self.lines)


def run(records: List[Dict[str, Any]],
        now: Optional[datetime.datetime] = None) -> check.CheckReport:
    """Check ``records`` at a fixed 'now' so the tests do not drift."""
    return check.check_reader(iter(records), now=now if now else NOW)


class CountingTest(unittest.TestCase):
    """What the reader produced."""

    def test_valid_records_are_counted_and_summarized(self) -> None:
        report = run(many(3))
        self.assertEqual((report.read, report.valid, report.invalid),
                         (3, 3, 0))
        self.assertTrue(report.ok)
        self.assertEqual(report.environments["prod"], 3)
        self.assertEqual(report.results["PASS"], 3)

    def test_invalid_records_are_grouped_not_raised(self) -> None:
        """One run of this must report every problem, not the first."""
        report = run([record(result="WHATEVER"), record(result="ALSO_WRONG"),
                      record(environment=None)])
        self.assertEqual(report.invalid, 3)
        self.assertFalse(report.ok)
        # The two unknown results collapse into one reason; the missing
        # environment is its own.
        self.assertEqual(len(report.reasons), 2)

    def test_a_reader_producing_nothing_is_not_ok(self) -> None:
        report = run([])
        self.assertFalse(report.ok)

    def test_max_records_stops_early(self) -> None:
        report = check.check_reader(iter(many(100)), now=NOW, max_records=10)
        self.assertEqual(report.read, 10)


class SanityHeuristicTest(unittest.TestCase):
    """Records that are valid but almost certainly wrong."""

    def test_future_records_are_called_out_as_a_timezone_error(self) -> None:
        """A zone ahead of UTC produces this, and it is proof of a bug."""
        report = run([record(start_time="2026-07-26T09:00:00.000000",
                             end_time="2026-07-26T09:00:01.000000")])
        self.assertTrue(any("FUTURE" in w for w in report.warnings))
        self.assertTrue(any("UTC" in w for w in report.warnings))

    def test_ancient_records_are_flagged(self) -> None:
        report = run([record(start_time="1970-01-01T00:00:00.000000",
                             end_time="1970-01-01T00:00:01.000000")])
        self.assertTrue(any("10 years" in w for w in report.warnings))

    def test_every_duration_zero_is_flagged_on_a_real_sample(self) -> None:
        report = run(many(25, end_time="2026-07-26T03:00:00.000000"))
        self.assertTrue(any("duration is zero" in w for w in report.warnings))

    def test_one_zero_duration_record_is_not_flagged(self) -> None:
        """Below a real sample the heuristic is as likely right as wrong."""
        report = run([record(end_time="2026-07-26T03:00:00.000000")])
        self.assertEqual(report.warnings, [])

    def test_a_single_result_value_suggests_an_unmapped_outcome(self) -> None:
        report = run(many(25))
        self.assertTrue(any("all four values" in w for w in report.warnings))

    def test_whitespace_in_an_environment_is_flagged(self) -> None:
        """' prod' and 'prod' are two estates that look like one."""
        report = run([record(environment=" prod")])
        self.assertTrue(any("whitespace" in w for w in report.warnings))


class ClockReportTest(unittest.TestCase):
    """The two-sided timezone report.

    The future check above only fires for a reader in a zone *ahead* of
    UTC. One behind it — the whole of the Americas — produces records that
    merely look older than they are, which no rule can tell from a suite
    that ran earlier. So the numbers are printed side by side and named
    instead, because the person reading them knows when their suite ran.
    """

    def report_text(self, records: List[Dict[str, Any]]) -> str:
        log = CollectingLogger()
        check.log_report(run(records), log)
        return log.text()

    def test_the_newest_record_is_reported_against_utc_now(self) -> None:
        text = self.report_text([record()])
        self.assertIn("newest run:", text)
        self.assertIn("3h 00m before UTC now", text)

    def test_a_future_record_is_reported_as_after_utc_now(self) -> None:
        text = self.report_text([
            record(start_time="2026-07-26T09:00:00.000000",
                   end_time="2026-07-26T09:00:01.000000")])
        self.assertIn("AFTER UTC now", text)

    def test_this_machines_offset_is_shown_beside_it(self) -> None:
        """Without the offset the gap cannot be interpreted."""
        text = self.report_text([record()])
        self.assertIn("this machine's clock is UTC", text)

    def test_an_empty_read_reports_no_clock_line(self) -> None:
        self.assertNotIn("newest run:", self.report_text([]))

    def test_offsets_render_the_way_people_write_them(self) -> None:
        self.assertEqual(check.format_offset(0.0), "UTC")
        self.assertEqual(check.format_offset(1.0), "UTC+1")
        self.assertEqual(check.format_offset(-5.0), "UTC-5")
        self.assertEqual(check.format_offset(5.5), "UTC+5:30")
        self.assertEqual(check.format_offset(-3.5), "UTC-3:30")

    def test_the_local_offset_is_read_from_the_machine(self) -> None:
        """It has to be a real number of hours, whatever this machine is."""
        offset = check.local_utc_offset_hours()
        self.assertIsInstance(offset, float)
        self.assertTrue(-14.0 <= offset <= 14.0, offset)

    def test_the_gap_is_described_at_a_readable_scale(self) -> None:
        latest = datetime.datetime(2026, 7, 26, 5, 30, 0)
        self.assertIn("30m", check.describe_age(latest, NOW))
        self.assertIn(
            "2d", check.describe_age(datetime.datetime(2026, 7, 24), NOW))

    def test_a_zone_behind_utc_gets_the_shift_named_the_right_way(
        self
    ) -> None:
        """Local time from UTC-5 makes every run look five hours early."""
        self.assertIn("5 hours early", check._shift_phrase(-5.0))
        self.assertIn("1 hour late", check._shift_phrase(1.0))


class LogReportTest(unittest.TestCase):
    """The verdict a reader's author reads."""

    def report_text(self, records: List[Dict[str, Any]]) -> str:
        log = CollectingLogger()
        check.log_report(run(records), log)
        return log.text()

    def test_a_clean_reader_says_so_plainly(self) -> None:
        self.assertIn("reader OK: every record validates",
                      self.report_text(many(3)))

    def test_warnings_are_not_hidden_by_an_ok_verdict(self) -> None:
        text = self.report_text(many(25))
        self.assertIn("see the warning(s) above", text)

    def test_rejections_name_the_rule_and_an_example(self) -> None:
        text = self.report_text([record(result="NOPE")])
        self.assertIn("would be REJECTED", text)
        self.assertIn("test_one", text)

    def test_a_silent_reader_is_diagnosed(self) -> None:
        """A generator that never yields looks identical to success."""
        text = self.report_text([])
        self.assertIn("produced NO records", text)


if __name__ == "__main__":
    unittest.main()
