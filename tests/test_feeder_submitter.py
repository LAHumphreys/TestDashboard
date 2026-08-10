"""Tests for :mod:`feeder.submitter`.

Everything runs against a fake opener and a fake sleep — no real network,
no real waiting. Covers validation/skip counting, since filtering, batching,
dry-run, retry with exponential backoff, the no-retry-on-400 rule, replay
file contents, high-water-mark tracking, URL normalization, server-side
reject accumulation, the reason-grouped summary, and the connection-error
remedy messages.
"""

import datetime
import json
import logging
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
from typing import Any, Dict, List, Optional, Tuple, Union

from feeder.submitter import (
    StreamsAckMissing, SubmitStats, Submitter, describe_connection_error,
)
from testboard import model

#: One outcome for the fake opener: an HTTP (status, body) or an exception.
Outcome = Union[Tuple[int, bytes], Exception]

BASE_TIME = datetime.datetime(2026, 7, 1, 2, 0, 0)
URL = "http://dash.example:8000"
IMPORT_URL = URL + "/api/import"


def make_raw(index: int = 0, minutes: int = 0, **overrides: Any) -> Dict[str, Any]:
    """Build a fully valid raw transport dict, offset by ``minutes``."""
    start = BASE_TIME + datetime.timedelta(minutes=minutes)
    end = start + datetime.timedelta(seconds=5)
    raw = {
        "environment": "linux-sim",
        "script": "regression/foo.py",
        "test_name": "test_{0}".format(index),
        "result": "PASS",
        "start_time": model.format_iso(start),
        "end_time": model.format_iso(end),
        "output": "ok",
        "source_link": "https://example.com/foo.py",
        "known_failure_reason": None,
    }  # type: Dict[str, Any]
    raw.update(overrides)
    return raw


def ok_body(inserted: int = 0, updated: int = 0, rejected: int = 0,
            errors: Optional[List[Dict[str, Any]]] = None,
            streams_seen: Optional[List[str]] = None) -> bytes:
    """Serialize a well-formed /api/import 200 response body.

    ``streams_seen`` defaults to ``[]`` (a WP-21 server's mainline
    shape). Pass ``streams_seen=None`` explicitly via
    :func:`ok_body_without_streams_seen` to simulate a pre-WP-21 server,
    whose response has no such key at all.
    """
    payload = {
        "inserted": inserted,
        "updated": updated,
        "rejected": rejected,
        "errors": errors if errors is not None else [],
        "streams_seen": streams_seen if streams_seen is not None else [],
    }
    return json.dumps(payload).encode("utf-8")


def ok_body_without_streams_seen(
    inserted: int = 0, updated: int = 0, rejected: int = 0,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    """A 200 response body shaped like a server that predates WP-21 —
    no ``streams_seen`` key at all (unknown keys are simply absent, not
    null)."""
    payload = {
        "inserted": inserted,
        "updated": updated,
        "rejected": rejected,
        "errors": errors if errors is not None else [],
    }
    return json.dumps(payload).encode("utf-8")


class FakeOpener:
    """Scripted Opener: pops one outcome per call, records every call."""

    def __init__(self, outcomes: Optional[List[Outcome]] = None) -> None:
        """``outcomes`` are consumed in order; afterwards return plain 200s."""
        self.outcomes = list(outcomes) if outcomes is not None else []
        self.calls = []  # type: List[Tuple[str, bytes, Dict[str, str]]]

    def __call__(self, url: str, body: bytes,
                 headers: Dict[str, str]) -> Tuple[int, bytes]:
        """Record the call and play back the next scripted outcome."""
        self.calls.append((url, body, headers))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = (200, ok_body())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def batch_sizes(self) -> List[int]:
        """Number of runs in each POSTed body, in call order."""
        return [
            len(json.loads(body.decode("utf-8"))["runs"])
            for _, body, _ in self.calls
        ]


class ExplodingOpener:
    """Opener that fails the test if any HTTP call is attempted."""

    def __init__(self, test: unittest.TestCase) -> None:
        """Remember the test to fail on call."""
        self._test = test

    def __call__(self, url: str, body: bytes,
                 headers: Dict[str, str]) -> Tuple[int, bytes]:
        """Any call is a bug (e.g. during dry-run)."""
        self._test.fail("opener must not be called")
        return (0, b"")  # pragma: no cover - unreachable


class FakeSleep:
    """Records requested backoff delays instead of sleeping."""

    def __init__(self) -> None:
        """Start with no recorded delays."""
        self.delays = []  # type: List[float]

    def __call__(self, seconds: float) -> None:
        """Record the delay."""
        self.delays.append(seconds)


class SubmitterTestBase(unittest.TestCase):
    """Shared fixtures: temp replay dir, quiet logging, builder helper."""

    def setUp(self) -> None:
        """Create a temp replay dir and silence lastResort log output."""
        self.replay_dir = tempfile.mkdtemp(prefix="testboard_replay_")
        self.addCleanup(shutil.rmtree, self.replay_dir, True)
        null_handler = logging.NullHandler()
        feeder_logger = logging.getLogger("feeder")
        feeder_logger.addHandler(null_handler)
        self.addCleanup(feeder_logger.removeHandler, null_handler)
        self.sleep = FakeSleep()

    def make_submitter(self, opener: Any, batch_size: int = 500,
                       max_retries: int = 3,
                       backoff_seconds: float = 2.0,
                       url: str = URL,
                       build: Optional[str] = None) -> Submitter:
        """Build a Submitter wired to the fakes and the temp replay dir."""
        return Submitter(
            url,
            batch_size=batch_size,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            opener=opener,
            sleep=self.sleep,
            replay_dir=self.replay_dir,
            build=build,
        )

    def replay_files_on_disk(self) -> List[str]:
        """Names of replay files currently in the temp replay dir."""
        return sorted(
            name for name in os.listdir(self.replay_dir)
            if name.startswith("testboard_failed_batch_")
        )


class ValidationAndCountingTest(SubmitterTestBase):
    """Per-record validation, skip counting and skip logging."""

    def test_valid_and_invalid_counts(self) -> None:
        """Invalid records are skipped+logged; valid ones are still sent."""
        records = [
            make_raw(0, minutes=0),
            make_raw(1, minutes=1, result="BROKE"),
            make_raw(2, minutes=2),
            {"environment": "linux-sim"},
            make_raw(4, minutes=4),
        ]
        opener = FakeOpener([(200, ok_body(inserted=3))])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO") as captured:
            stats = submitter.submit(records)
        self.assertEqual(stats.read, 5)
        self.assertEqual(stats.valid, 3)
        self.assertEqual(stats.skipped, 2)
        self.assertEqual(stats.sent, 3)
        self.assertEqual(stats.inserted, 3)
        self.assertEqual(stats.failed_batches, 0)
        self.assertEqual(stats.replay_files, [])
        warnings = [line for line in captured.output
                    if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)
        self.assertIn("result: unknown value 'BROKE'", warnings[0])
        self.assertIn("linux-sim / regression/foo.py / test_1", warnings[0])
        self.assertIn(
            model.format_iso(BASE_TIME + datetime.timedelta(minutes=1)),
            warnings[0],
        )

    def test_skip_log_truncates_record_repr(self) -> None:
        """The offending-record repr in skip logs is truncated."""
        bad = make_raw(0, result="NOPE", output="y" * 5000)
        opener = FakeOpener()
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="WARNING") as captured:
            stats = submitter.submit([bad], dry_run=True)
        self.assertEqual(stats.skipped, 1)
        skip_line = captured.output[0]
        self.assertIn("...[truncated]", skip_line)
        self.assertNotIn("y" * 1000, skip_line)

    def test_summary_groups_reasons_with_first_identity(self) -> None:
        """The final summary groups skip reasons and shows an example record."""
        records = [
            make_raw(0, minutes=0, result="BROKE"),
            make_raw(1, minutes=1, result="BUSTED"),
            make_raw(2, minutes=2),
        ]
        del records[2]["output"]
        opener = FakeOpener()
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO") as captured:
            stats = submitter.submit(records, dry_run=True)
        self.assertEqual(stats.skipped, 3)
        text = "\n".join(captured.output)
        self.assertIn("2 x [result: unknown value]", text)
        self.assertIn("first: linux-sim / regression/foo.py / test_0", text)
        self.assertIn("1 x [output: required field is missing]", text)

    def test_summary_line_contains_all_counters(self) -> None:
        """The final INFO summary reports every SubmitStats field."""
        opener = FakeOpener([(200, ok_body(inserted=1))])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO") as captured:
            submitter.submit([make_raw(0)])
        summary = [line for line in captured.output if "feeder summary" in line]
        self.assertEqual(len(summary), 1)
        for fragment in ("read=1", "valid=1", "skipped=0", "sent=1",
                         "inserted=1", "updated=0", "rejected=0",
                         "failed_batches=0", "replay_files=0"):
            self.assertIn(fragment, summary[0])


class SinceFilterTest(SubmitterTestBase):
    """The since lower bound drops old records without counting them skipped."""

    def test_records_before_since_are_dropped(self) -> None:
        """start_time < since -> read but neither valid nor skipped nor sent."""
        records = [make_raw(i, minutes=10 * i) for i in range(4)]
        since = BASE_TIME + datetime.timedelta(minutes=15)
        opener = FakeOpener([(200, ok_body(inserted=2))])
        submitter = self.make_submitter(opener)
        stats = submitter.submit(records, since=since)
        self.assertEqual(stats.read, 4)
        self.assertEqual(stats.valid, 2)
        self.assertEqual(stats.skipped, 0)
        self.assertEqual(stats.sent, 2)
        sent_names = [
            run["test_name"]
            for run in json.loads(opener.calls[0][1].decode("utf-8"))["runs"]
        ]
        self.assertEqual(sent_names, ["test_2", "test_3"])

    def test_record_exactly_at_since_is_kept(self) -> None:
        """The bound is inclusive: start_time == since is imported."""
        records = [make_raw(0, minutes=0)]
        opener = FakeOpener([(200, ok_body(inserted=1))])
        submitter = self.make_submitter(opener)
        stats = submitter.submit(records, since=BASE_TIME)
        self.assertEqual(stats.sent, 1)


class BatchingTest(SubmitterTestBase):
    """Batch splitting and request shape."""

    def test_seven_records_batch_size_three_makes_three_posts(self) -> None:
        """7 records at batch_size 3 -> POSTs of 3, 3 and 1 records."""
        records = [make_raw(i, minutes=i) for i in range(7)]
        opener = FakeOpener([
            (200, ok_body(inserted=3)),
            (200, ok_body(inserted=2, updated=1)),
            (200, ok_body(updated=1)),
        ])
        submitter = self.make_submitter(opener, batch_size=3)
        stats = submitter.submit(records)
        self.assertEqual(opener.batch_sizes(), [3, 3, 1])
        self.assertEqual(stats.sent, 7)
        self.assertEqual(stats.inserted, 5)
        self.assertEqual(stats.updated, 2)
        self.assertEqual(stats.failed_batches, 0)

    def test_body_is_runs_envelope_of_transport_dicts(self) -> None:
        """Each POST body is exactly {"runs": [transport dicts...]}."""
        records = [make_raw(0), make_raw(1, minutes=1)]
        opener = FakeOpener([(200, ok_body(inserted=2))])
        submitter = self.make_submitter(opener)
        submitter.submit(records)
        url, body, headers = opener.calls[0]
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(sorted(payload.keys()), ["runs"])
        self.assertEqual(payload["runs"], records)
        self.assertEqual(headers.get("Content-Type"), "application/json")

    def test_url_normalization(self) -> None:
        """Base URLs get /api/import appended; full URLs pass through."""
        cases = [
            ("http://h:8000", "http://h:8000/api/import"),
            ("http://h:8000/", "http://h:8000/api/import"),
            ("http://h:8000/api/import", "http://h:8000/api/import"),
            ("http://h:8000/api/import/", "http://h:8000/api/import"),
        ]
        for given, expected in cases:
            opener = FakeOpener([(200, ok_body(inserted=1))])
            submitter = self.make_submitter(opener, url=given)
            submitter.submit([make_raw(0)])
            self.assertEqual(opener.calls[0][0], expected)


class DryRunTest(SubmitterTestBase):
    """Dry-run validates and counts but never touches HTTP."""

    def test_dry_run_sends_nothing(self) -> None:
        """No opener calls, sent == 0, valid/skipped still counted."""
        records = [make_raw(0), make_raw(1, result="BROKE"), make_raw(2)]
        submitter = self.make_submitter(ExplodingOpener(self))
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit(records, dry_run=True)
        self.assertEqual(stats.read, 3)
        self.assertEqual(stats.valid, 2)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.sent, 0)
        self.assertEqual(stats.failed_batches, 0)
        self.assertIsNone(submitter.max_accepted_start_time())
        self.assertEqual(self.replay_files_on_disk(), [])


class RetryTest(SubmitterTestBase):
    """Retry/backoff behaviour and the no-retry-on-400 rule."""

    def test_retry_then_success_with_exponential_backoff(self) -> None:
        """Two transport failures then 200: sleeps are 2.0 then 4.0."""
        opener = FakeOpener([
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
            (200, ok_body(inserted=1)),
        ])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit([make_raw(0)])
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(self.sleep.delays, [2.0, 4.0])
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.failed_batches, 0)
        self.assertEqual(self.replay_files_on_disk(), [])

    def test_500_is_retried_then_succeeds(self) -> None:
        """A 5xx status is retryable just like a transport exception."""
        opener = FakeOpener([
            (503, b"upstream sad"),
            (200, ok_body(inserted=1)),
        ])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit([make_raw(0)])
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.sleep.delays, [2.0])
        self.assertEqual(stats.failed_batches, 0)
        self.assertEqual(stats.sent, 1)

    def test_400_is_not_retried(self) -> None:
        """HTTP 400 fails the batch immediately: one call, no sleeps."""
        opener = FakeOpener([(400, b'{"error": "bad envelope"}')])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit([make_raw(0)])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.sleep.delays, [])
        self.assertEqual(stats.failed_batches, 1)
        self.assertEqual(stats.sent, 0)
        self.assertEqual(len(stats.replay_files), 1)

    def test_final_failure_writes_replay_file_with_exact_body(self) -> None:
        """Exhausted retries -> replay file holding {"runs": [...]} verbatim."""
        records = [make_raw(0), make_raw(1, minutes=1)]
        opener = FakeOpener([
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
        ])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO") as captured:
            stats = submitter.submit(records)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(self.sleep.delays, [2.0, 4.0])
        self.assertEqual(stats.sent, 0)
        self.assertEqual(stats.failed_batches, 1)
        self.assertEqual(len(stats.replay_files), 1)
        path = stats.replay_files[0]
        self.assertEqual(
            os.path.basename(path), "testboard_failed_batch_0001.json"
        )
        self.assertEqual(os.path.dirname(path), self.replay_dir)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload, {"runs": records})
        self.assertIsNone(submitter.max_accepted_start_time())
        text = "\n".join(captured.output)
        self.assertIn("permanently failed", text)
        self.assertIn(path, text)

    def test_failed_batch_number_is_global_and_one_based(self) -> None:
        """Replay NNNN reflects the overall 1-based batch number."""
        records = [make_raw(i, minutes=i) for i in range(4)]
        opener = FakeOpener([
            (200, ok_body(inserted=2)),
            (400, b'{"error": "nope"}'),
        ])
        submitter = self.make_submitter(opener, batch_size=2)
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit(records)
        self.assertEqual(stats.failed_batches, 1)
        self.assertEqual(
            [os.path.basename(p) for p in stats.replay_files],
            ["testboard_failed_batch_0002.json"],
        )
        self.assertEqual(
            self.replay_files_on_disk(), ["testboard_failed_batch_0002.json"]
        )

    def test_later_batches_continue_after_a_failure(self) -> None:
        """A permanently failed batch does not abort the remaining batches."""
        records = [make_raw(i, minutes=i) for i in range(6)]
        opener = FakeOpener([
            (400, b'{"error": "nope"}'),
            (200, ok_body(inserted=2)),
            (200, ok_body(inserted=2)),
        ])
        submitter = self.make_submitter(opener, batch_size=2)
        with self.assertLogs("feeder.submitter", level="INFO"):
            stats = submitter.submit(records)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(stats.sent, 4)
        self.assertEqual(stats.inserted, 4)
        self.assertEqual(stats.failed_batches, 1)


class HighWaterMarkTest(SubmitterTestBase):
    """max_accepted_start_time tracks only batches that got a 200."""

    def test_none_before_any_submit(self) -> None:
        """Fresh submitter has no accepted start time."""
        submitter = self.make_submitter(FakeOpener())
        self.assertIsNone(submitter.max_accepted_start_time())

    def test_only_accepted_batches_count(self) -> None:
        """A failed batch's (newer) records do not advance the mark."""
        records = [make_raw(i, minutes=10 * i) for i in range(4)]
        opener = FakeOpener([
            (200, ok_body(inserted=2)),
            (400, b'{"error": "nope"}'),
        ])
        submitter = self.make_submitter(opener, batch_size=2)
        with self.assertLogs("feeder.submitter", level="INFO"):
            submitter.submit(records)
        self.assertEqual(
            submitter.max_accepted_start_time(),
            BASE_TIME + datetime.timedelta(minutes=10),
        )

    def test_mark_is_max_across_all_accepted_batches(self) -> None:
        """The mark is the max start_time over every accepted batch."""
        records = [make_raw(i, minutes=10 * i) for i in range(4)]
        opener = FakeOpener([
            (200, ok_body(inserted=2)),
            (200, ok_body(inserted=2)),
        ])
        submitter = self.make_submitter(opener, batch_size=2)
        submitter.submit(records)
        self.assertEqual(
            submitter.max_accepted_start_time(),
            BASE_TIME + datetime.timedelta(minutes=30),
        )


class ServerRejectTest(SubmitterTestBase):
    """Server-side per-record rejects are accumulated and logged."""

    def test_rejects_accumulated_and_logged_with_identity(self) -> None:
        """errors[] entries log a WARNING with identity + join the summary."""
        error_obj = {
            "index": 1,
            "error": "result: unknown value 'BROKE'",
            "environment": "linux-sim",
            "script": "regression/foo.py",
            "test_name": "test_1",
            "start_time": "2026-07-01T02:01:00.000000",
        }
        opener = FakeOpener([
            (200, ok_body(inserted=1, rejected=1, errors=[error_obj])),
        ])
        submitter = self.make_submitter(opener)
        with self.assertLogs("feeder.submitter", level="INFO") as captured:
            stats = submitter.submit([make_raw(0), make_raw(1, minutes=1)])
        self.assertEqual(stats.rejected, 1)
        self.assertEqual(stats.inserted, 1)
        text = "\n".join(captured.output)
        self.assertIn("server rejected record index 1", text)
        self.assertIn("linux-sim / regression/foo.py / test_1", text)
        self.assertIn("1 x [result: unknown value]", text)


class DescribeConnectionErrorTest(unittest.TestCase):
    """Each transport failure class gets its own remedy message."""

    def test_connection_refused(self) -> None:
        """Refused -> 'is the server running' + run_server remedy."""
        message = describe_connection_error(
            IMPORT_URL, ConnectionRefusedError("refused")
        )
        self.assertIn("connection refused", message)
        self.assertIn(IMPORT_URL, message)
        self.assertIn("Is the server running", message)
        self.assertIn("python3 run_server.py", message)

    def test_timeout(self) -> None:
        """Timeout -> its own message mentioning network connectivity."""
        message = describe_connection_error(IMPORT_URL, socket.timeout())
        self.assertIn("timed out", message)
        self.assertIn("dash.example", message)

    def test_dns_failure(self) -> None:
        """DNS -> its own message naming the host to check."""
        message = describe_connection_error(
            IMPORT_URL, socket.gaierror(8, "nodename nor servname provided")
        )
        self.assertIn("DNS", message)
        self.assertIn("dash.example", message)

    def test_urlerror_wrapping_is_unwrapped(self) -> None:
        """urllib wraps the real reason in URLError; it is unwrapped."""
        wrapped = urllib.error.URLError(ConnectionRefusedError("refused"))
        message = describe_connection_error(IMPORT_URL, wrapped)
        self.assertIn("connection refused", message)

    def test_generic_fallback_is_still_actionable(self) -> None:
        """Unknown exceptions still name the URL and a remedy."""
        message = describe_connection_error(IMPORT_URL, ValueError("weird"))
        self.assertIn(IMPORT_URL, message)
        self.assertIn("ValueError", message)
        self.assertIn("python3 run_server.py", message)


class StreamsAckTest(SubmitterTestBase):
    """WP-21 (docs/STREAMS_PLAN.md section 3.3/3.7): a --build submitter
    must abort if the server's response has no streams_seen key at all —
    the sign of a server that predates WP-21 and would silently file the
    runs into mainline. (--branch died with WP-25, docs/ONE_KIND_PLAN.md,
    before it ever shipped.)"""

    def test_mainline_never_checks_for_the_key(self) -> None:
        """No build given: an old-shaped response is fine."""
        opener = FakeOpener([(200, ok_body_without_streams_seen())])
        submitter = self.make_submitter(opener)
        stats = submitter.submit([make_raw(0)])
        self.assertEqual(stats.sent, 1)

    def test_build_with_the_key_present_succeeds(self) -> None:
        opener = FakeOpener([(200, ok_body(inserted=1,
                                            streams_seen=["build:feat/x"]))])
        submitter = self.make_submitter(opener, build="feat/x")
        stats = submitter.submit([make_raw(0)])
        self.assertEqual(stats.sent, 1)

    def test_build_with_an_empty_list_still_succeeds(self) -> None:
        """The key must be PRESENT; an empty list is a legitimate answer
        (e.g. every record in this particular batch got rejected before
        being counted, though the key itself was echoed back)."""
        opener = FakeOpener([(200, ok_body(inserted=0, streams_seen=[]))])
        submitter = self.make_submitter(opener, build="feat/x")
        stats = submitter.submit([make_raw(0)])
        self.assertEqual(stats.sent, 1)

    def test_build_with_the_key_absent_raises(self) -> None:
        opener = FakeOpener([(200, ok_body_without_streams_seen())])
        submitter = self.make_submitter(opener, build="feat/x")
        with self.assertRaises(StreamsAckMissing) as caught:
            submitter.submit([make_raw(0)])
        self.assertIn("streams_seen", str(caught.exception))
        self.assertIn("--build", str(caught.exception))

    def test_the_triggering_batch_still_counts_as_sent_not_replayed(
            self) -> None:
        """The batch DID get a real 200 - its data is stored server-side.
        No replay file should be written for it; the exception is the
        signal, not a batch failure."""
        opener = FakeOpener([(200, ok_body_without_streams_seen())])
        submitter = self.make_submitter(opener, build="feat/x")
        with self.assertRaises(StreamsAckMissing):
            submitter.submit([make_raw(0)])
        self.assertEqual(self.replay_files_on_disk(), [])

    def test_a_later_batch_missing_the_ack_aborts_mid_run(self) -> None:
        """The first batch acks fine; the second does not (e.g. a
        rolling deploy landed an old node mid-import) - still aborts."""
        opener = FakeOpener([
            (200, ok_body(inserted=1, streams_seen=["build:feat/x"])),
            (200, ok_body_without_streams_seen()),
        ])
        submitter = self.make_submitter(
            opener, batch_size=1, build="feat/x")
        with self.assertRaises(StreamsAckMissing):
            submitter.submit([make_raw(0, minutes=0), make_raw(1, minutes=1)])


class StatsShapeTest(unittest.TestCase):
    """SubmitStats exposes exactly the spec'd fields."""

    def test_field_names(self) -> None:
        """Field order/names match the cross-module contract."""
        self.assertEqual(
            SubmitStats._fields,
            ("read", "valid", "skipped", "sent", "inserted", "updated",
             "rejected", "failed_batches", "replay_files"),
        )


if __name__ == "__main__":
    unittest.main()
