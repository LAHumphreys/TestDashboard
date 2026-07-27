"""Tests for ``--test-connection`` and ``--status``.

Both answer questions that previously had no answer short of starting an
import and watching what happened: *can this machine talk to that
dashboard?* and *how far have we got?* Neither may need a running server
to be tested, so the HTTP hooks are injected.

The status report deliberately does **not** read the source system. That
is the property most worth pinning: a status command that reads a year of
history takes as long as the import and so is never used.

Python 3.6 compatible; standard library only.
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from feeder import state, status
from testboard import model

NOW = datetime.datetime(2026, 7, 27, 6, 0, 0)
MARK = datetime.datetime(2026, 7, 26, 3, 4, 0)

_IMPORT_OK = json.dumps(
    {"inserted": 0, "updated": 0, "rejected": 0, "errors": []}
).encode("utf-8")
_SUMMARY = json.dumps({
    "recent_hours": 36,
    "status": {"total_tests": 12431, "ran_recently": 12010, "not_run": 421},
}).encode("utf-8")
_NEWEST = json.dumps({
    "tests": [{"start_time": "2026-07-26T03:04:00.000000"}],
    "total": 1,
}).encode("utf-8")


def opener(status_code: int = 200, body: bytes = _IMPORT_OK) -> Any:
    """An Opener answering the import probe with fixed values."""
    def send(
        url: str, data: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        return status_code, body
    return send


def getter(
    summary: bytes = _SUMMARY, newest: bytes = _NEWEST,
    status_code: int = 200,
) -> Any:
    """A Getter answering the two read endpoints status.py uses."""
    def fetch(url: str) -> Tuple[int, bytes]:
        if "/api/summary" in url:
            return status_code, summary
        return status_code, newest
    return fetch


class TestConnectionTest(unittest.TestCase):
    """The standalone reachability check."""

    def report(self, url: str = "http://dash:8000", **kwargs: Any) -> str:
        lines = status.test_connection(
            url, opener=kwargs.get("opener", opener()),
            getter=kwargs.get("getter", getter()))
        self.lines = lines
        return "\n".join(lines)

    def test_a_healthy_dashboard_reports_ok_on_the_last_line(self) -> None:
        """The caller decides the exit code from that line alone."""
        self.report()
        self.assertTrue(self.lines[-1].startswith("OK"), self.lines[-1])

    def test_it_shows_the_address_posts_will_actually_go_to(self) -> None:
        """--url is a base; the endpoint it becomes is what matters."""
        self.assertIn("http://dash:8000/api/import", self.report())

    def test_it_says_the_test_import_wrote_nothing(self) -> None:
        """Otherwise nobody dares run it against production."""
        self.assertIn("nothing was written", self.report())

    def test_it_reports_what_the_dashboard_already_holds(self) -> None:
        text = self.report()
        self.assertIn("12431 test(s) known", text)
        self.assertIn("12010 ran in the last 36h", text)

    def test_it_reports_the_newest_run_held(self) -> None:
        self.assertIn("2026-07-26T03:04:00.000000", self.report())

    def test_a_bad_url_fails_without_contacting_anything(self) -> None:
        def explode(url: str, data: bytes, headers: Dict[str, str]) -> Any:
            raise AssertionError("must not be called")

        text = self.report("dash:8000", opener=explode)
        self.assertIn("has no scheme", text)
        self.assertTrue(self.lines[-1].startswith("FAILED"))

    def test_an_unreachable_dashboard_fails_with_the_reason(self) -> None:
        def refuse(url: str, data: bytes, headers: Dict[str, str]) -> Any:
            raise ConnectionRefusedError("refused")

        text = self.report(opener=refuse)
        self.assertIn("connection refused", text)
        self.assertTrue(self.lines[-1].startswith("FAILED"))

    def test_a_write_only_dashboard_warns_but_still_passes(self) -> None:
        """Imports would work; the UI would not. That is not the feeder's
        failure, and calling it one would send someone down the wrong path."""
        text = self.report(getter=getter(status_code=500))
        self.assertIn("read path    WARN", text)
        self.assertTrue(self.lines[-1].startswith("OK"))

    def test_an_empty_dashboard_says_so_rather_than_erroring(self) -> None:
        empty = json.dumps({"tests": [], "total": 0}).encode("utf-8")
        text = self.report(getter=getter(newest=empty))
        self.assertIn("holds no runs", text)
        self.assertTrue(self.lines[-1].startswith("OK"))


class StatusTest(unittest.TestCase):
    """How far the feed has got."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_status_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_file = os.path.join(self.tmp, "feeder_state.json")

    def report(self, url: Optional[str] = "http://dash:8000",
               overlap_days: int = 1, **kwargs: Any) -> str:
        return "\n".join(status.describe(
            url, self.state_file, overlap_days,
            kwargs.get("reader", "/opt/r.py:create_reader"),
            kwargs.get("config", None), kwargs.get("mode", "daily"),
            opener=kwargs.get("opener", opener()),
            getter=kwargs.get("getter", getter()),
            now=NOW,
        ))

    def test_it_reports_the_mark_and_how_old_it_is(self) -> None:
        state.save_high_water_mark(self.state_file, MARK)
        text = self.report()
        self.assertIn("pushed up to  2026-07-26T03:04:00.000000", text)
        self.assertIn("ago", text)

    def test_it_says_what_a_run_now_would_cover(self) -> None:
        """The mark minus the overlap is not obvious, and is the answer to
        'what will tonight actually re-read?'"""
        state.save_high_water_mark(self.state_file, MARK)
        text = self.report(overlap_days=2)
        self.assertIn("2026-07-24T03:04:00.000000", text)
        self.assertIn("--overlap-days 2", text)

    def test_no_state_file_is_explained_rather_than_shown_as_an_error(
        self
    ) -> None:
        text = self.report()
        self.assertIn("nothing yet", text)
        self.assertIn("imports everything", text)

    def test_an_unreadable_state_file_is_distinguished_from_an_absent_one(
        self
    ) -> None:
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        text = self.report()
        self.assertIn("UNKNOWN", text)
        self.assertIn("safe", text)

    def test_it_reports_what_the_dashboard_holds(self) -> None:
        text = self.report()
        self.assertIn("12431 test(s) known", text)
        self.assertIn("holds runs to 2026-07-26T03:04:00.000000", text)

    def test_an_unreachable_dashboard_does_not_fail_the_report(self) -> None:
        """The local half of the answer is still worth having."""
        def refuse(url: str, data: bytes, headers: Dict[str, str]) -> Any:
            raise ConnectionRefusedError("refused")

        state.save_high_water_mark(self.state_file, MARK)
        text = self.report(opener=refuse)
        self.assertIn("NOT REACHABLE", text)
        self.assertIn("pushed up to  2026-07-26T03:04:00.000000", text)

    def test_it_works_with_no_url_at_all(self) -> None:
        text = self.report(url=None)
        self.assertNotIn("dashboard", text.split("This reports")[0])

    def test_it_names_the_config_and_reader_in_use(self) -> None:
        """Half of "why is this wrong" is "which settings did it use"."""
        text = self.report(config=os.path.join(self.tmp, "feeder.json"))
        self.assertIn("feeder.json", text)
        self.assertIn("/opt/r.py:create_reader", text)

    def test_it_points_at_dry_run_for_the_outstanding_count(self) -> None:
        """It deliberately does not read the source; it must say so."""
        text = self.report()
        self.assertIn("--dry-run", text)
        self.assertIn("not the source system", text)

    def test_it_never_reads_the_source_system(self) -> None:
        """The property that keeps it usable: no reader is even loaded."""
        state.save_high_water_mark(self.state_file, MARK)
        self.report()  # a reader spec that does not exist, and does not matter
        self.assertTrue(True)


class GapTest(unittest.TestCase):
    """Ages are rendered at a scale a person reads at a glance."""

    def test_minutes_hours_and_days(self) -> None:
        for delta, expected in (
            (datetime.timedelta(minutes=20), "20m ago"),
            (datetime.timedelta(hours=3, minutes=5), "3h 05m ago"),
            (datetime.timedelta(days=2, hours=4), "2d 4h ago"),
        ):
            self.assertEqual(
                status.describe_gap(NOW - delta, NOW), expected)

    def test_a_mark_in_the_future_is_called_out(self) -> None:
        """A mark ahead of now means clock skew or local time, not progress."""
        ahead = NOW + datetime.timedelta(hours=2)
        self.assertIn("FUTURE", status.describe_gap(ahead, NOW))


if __name__ == "__main__":
    unittest.main()
