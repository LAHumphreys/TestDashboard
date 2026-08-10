"""The performance log, and the report that reads it back.

Two properties matter more than the arithmetic here.

**It must never break the server.** It exists to diagnose a production
stall, so a full disk, a deleted file or a closed handle has to degrade
to "no records" rather than to a 500 on the dashboard. Anything that can
turn profiling into an outage makes it a thing nobody dares leave on,
and a profiler nobody dares leave on cannot catch an intermittent fault.

**It must not lie about the queue wait.** That single field is what
separates "the query is slow" from "there was no free worker", which are
opposite diagnoses with opposite fixes. A wait attributed to every
request on a keep-alive connection instead of the first would turn one
3-second stall into ten and point at the wrong one.

Python 3.6 compatible; standard library only.
"""

import datetime
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, List

from testboard import perf, server
from testboard.model import Result, RunRecord
from testboard.storage import Storage
from tools import perf_report

NOW = datetime.datetime(2026, 7, 30, 1, 0, 0)

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def read_records(path: str) -> List[Dict[str, Any]]:
    """Every JSON object in a log file, in order."""
    with io.open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class PerfLogTest(unittest.TestCase):
    """Writing records."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "perf.log")

    def make(self, **kwargs: Any) -> perf.PerfLog:
        log = perf.PerfLog(self.path, **kwargs)
        self.addCleanup(log.close)
        return log

    def test_a_record_is_one_json_line(self) -> None:
        log = self.make()
        log.record("storage", "dashboard", 0.012)
        log.close()
        records = read_records(self.path)
        self.assertEqual(len(records), 2, records)      # label + record
        self.assertEqual(records[0]["k"], "label")
        self.assertEqual(records[0]["n"], "dashboard")
        self.assertEqual(records[1]["k"], "storage")
        self.assertEqual(records[1]["ms"], 12.0)
        self.assertEqual(records[1]["l"], records[0]["i"])

    def test_a_label_is_defined_once_not_per_record(self) -> None:
        """The reason ids exist: a label per record is mostly label."""
        log = self.make()
        for _ in range(20):
            log.record("storage", "activity_buckets", 0.1)
        log.close()
        records = read_records(self.path)
        labels = [r for r in records if r["k"] == "label"]
        self.assertEqual(len(labels), 1, "label re-defined per record")
        self.assertEqual(len(records), 21)

    def test_extra_fields_ride_along(self) -> None:
        log = self.make()
        log.record("request", "GET /api/summary", 0.2,
                   {"qms": 3100.0, "qd": 4, "s": 200})
        log.close()
        record = read_records(self.path)[-1]
        self.assertEqual(record["qms"], 3100.0)
        self.assertEqual(record["qd"], 4)
        self.assertEqual(record["s"], 200)

    def test_it_appends_to_an_existing_log(self) -> None:
        """A restarted server must not truncate yesterday's evidence."""
        first = perf.PerfLog(self.path)
        first.record("storage", "dashboard", 0.01)
        first.close()
        second = self.make()
        second.record("storage", "dashboard", 0.02)
        second.close()
        kinds = [r["k"] for r in read_records(self.path)]
        self.assertEqual(kinds.count("storage"), 2)

    def test_it_rolls_over_at_the_cap(self) -> None:
        log = self.make(max_bytes=2048)
        for index in range(200):
            log.record("storage", "method_{0}".format(index % 3), 0.001)
        log.close()
        self.assertTrue(os.path.isfile(self.path + ".1"),
                        "nothing was rolled aside")
        self.assertLess(os.path.getsize(self.path), 2048 * 2)

    def test_a_rolled_file_redefines_its_labels(self) -> None:
        """Otherwise every record in the new file is unreadable.

        Label ids are only meaningful next to their definition. If the
        file holding the definitions rolls away, a report of the new file
        can name nothing it contains.
        """
        log = self.make(max_bytes=1500)
        for index in range(200):
            log.record("storage", "summary_rollup", 0.001)
        log.close()
        records = read_records(self.path)
        self.assertTrue(records, "the new file is empty")
        self.assertEqual(records[0]["k"], "label")
        self.assertEqual(records[0]["n"], "summary_rollup")

    def test_at_most_two_files_exist(self) -> None:
        """Leaving profiling on cannot fill a partition."""
        log = self.make(max_bytes=1024)
        for index in range(4000):
            log.record("storage", "dashboard", 0.001)
        log.close()
        present = sorted(name for name in os.listdir(self.tmp)
                         if name.startswith("perf.log"))
        self.assertEqual(present, ["perf.log", "perf.log.1"])

    def test_recording_after_close_is_silent(self) -> None:
        """Shutdown order must not matter. This is a diagnostic, not a duty."""
        log = perf.PerfLog(self.path)
        log.close()
        log.record("storage", "dashboard", 0.01)        # must not raise
        log.close()                                    # nor must this

    def test_a_broken_log_does_not_raise(self) -> None:
        """A full disk must not become a 500 on the dashboard."""
        log = self.make()

        class Exploding(object):
            def write(self, text: str) -> int:
                raise IOError("No space left on device")

            def close(self) -> None:
                pass

        log._handle.close()
        log._handle = Exploding()
        log.record("storage", "dashboard", 0.01)        # must not raise

    def test_it_is_safe_from_many_threads(self) -> None:
        """Eight workers share one log. Every line must be whole."""
        log = self.make()
        errors = []  # type: List[str]

        def hammer(index: int) -> None:
            try:
                for _ in range(200):
                    log.record("storage", "method_{0}".format(index), 0.001)
            except Exception as exc:                    # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=hammer, args=(i,))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        log.close()
        self.assertEqual(errors, [])
        records = read_records(self.path)               # parses = no torn lines
        self.assertEqual(
            len([r for r in records if r["k"] == "storage"]), 8 * 200)

    def test_the_directory_is_created(self) -> None:
        nested = os.path.join(self.tmp, "logs", "perf.log")
        log = perf.PerfLog(nested)
        self.addCleanup(log.close)
        log.record("storage", "dashboard", 0.01)
        log.close()
        self.assertTrue(os.path.isfile(nested))


class TimeCallTest(unittest.TestCase):
    """Timing a call."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = perf.PerfLog(os.path.join(self.tmp, "perf.log"))
        self.addCleanup(self.log.close)

    def test_it_returns_the_result(self) -> None:
        self.assertEqual(
            self.log.time_call("storage", "x", lambda a, b: a + b, 2, 3), 5)

    def test_a_failing_call_is_still_timed_and_still_raises(self) -> None:
        """A query that fails slowly is a finding, not a gap."""
        def boom() -> None:
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            self.log.time_call("storage", "boom", boom)
        self.log.close()
        records = read_records(self.log._path)
        self.assertEqual([r["k"] for r in records if r["k"] == "storage"],
                         ["storage"])


class RouteLabelTest(unittest.TestCase):
    """Labels must be bounded, or the report is a list of test names."""

    def test_identity_segments_collapse(self) -> None:
        self.assertEqual(
            perf.route_label(
                "GET", "/api/tests/linux-sim/suite.py/test_a/history"),
            "GET /api/tests/*/*/*/history")

    def test_the_collection_and_action_survive(self) -> None:
        self.assertEqual(
            perf.route_label("PUT", "/api/users/alice/active"),
            "PUT /api/users/*/active")
        self.assertEqual(
            perf.route_label("PUT", "/api/environments/win-sim/expectation"),
            "PUT /api/environments/*/expectation")
        self.assertEqual(
            perf.route_label(
                "GET", "/api/scripts/win-sim/suite.py/executions"),
            "GET /api/scripts/*/*/executions")

    def test_a_run_id_collapses(self) -> None:
        self.assertEqual(perf.route_label("GET", "/api/runs/914238"),
                         "GET /api/runs/*")

    def test_flat_routes_are_unchanged(self) -> None:
        self.assertEqual(perf.route_label("GET", "/api/summary"),
                         "GET /api/summary")

    def test_static_paths_keep_their_name(self) -> None:
        """There are about fifteen; which file is slow is the point."""
        self.assertEqual(perf.route_label("GET", "/app.js"), "GET /app.js")

    def test_ten_thousand_tests_produce_one_label(self) -> None:
        """The property, stated directly."""
        labels = set()
        for index in range(10000):
            labels.add(perf.route_label(
                "GET", "/api/tests/env/suite.py/test_{0}".format(index)))
        self.assertEqual(labels, set(["GET /api/tests/*/*/*"]))


class InstrumentStorageTest(unittest.TestCase):
    """Wrapping a real Storage."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "perf.log")
        self.log = perf.PerfLog(self.path)
        self.addCleanup(self.log.close)
        self.store = Storage(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.store.close)

    def test_it_wraps_a_useful_number_of_methods(self) -> None:
        """A reflective wrapper's failure mode is wrapping nothing."""
        wrapped = perf.instrument_storage(self.store, self.log)
        self.assertGreater(len(wrapped), 20, wrapped)
        for expected in ("dashboard", "summary_rollup", "upsert_runs",
                         "activity_buckets", "duration_rollup"):
            self.assertIn(expected, wrapped)

    def test_it_leaves_close_alone(self) -> None:
        """close() runs on the worker-exit path, into a log being closed."""
        wrapped = perf.instrument_storage(self.store, self.log)
        self.assertNotIn("close", wrapped)
        self.assertNotIn("vacuum", wrapped)

    def test_properties_are_not_called(self) -> None:
        """max_connections is a property; wrapping it would invoke it."""
        wrapped = perf.instrument_storage(self.store, self.log)
        self.assertNotIn("max_connections", wrapped)
        self.assertEqual(self.store.max_connections, 8)

    def test_the_method_still_works_and_is_recorded(self) -> None:
        perf.instrument_storage(self.store, self.log)
        counts = self.store.upsert_runs([RunRecord(
            environment="linux-sim", script="s.py", test_name="t",
            result=Result.PASS, start_time=NOW,
            end_time=NOW + datetime.timedelta(seconds=1),
            output="out", source_link="", known_failure_reason=None,
            build=None)])
        self.assertEqual(counts.inserted, 1)            # behaviour preserved
        page = self.store.dashboard(limit=10, offset=0)
        self.assertEqual(len(page), 1)
        self.log.close()
        names = {r["i"]: r["n"] for r in read_records(self.path)
                 if r["k"] == "label"}
        timed = {names[r["l"]] for r in read_records(self.path)
                 if r["k"] == "storage"}
        self.assertIn("upsert_runs", timed)
        self.assertIn("dashboard", timed)

    def test_an_uninstrumented_storage_is_unaffected(self) -> None:
        """Per instance, not per class: a tool in the same process is free."""
        other = Storage(os.path.join(self.tmp, "other.db"))
        self.addCleanup(other.close)
        perf.instrument_storage(self.store, self.log)
        other.environments()
        self.log.close()
        self.assertEqual(
            [r for r in read_records(self.path) if r["k"] == "storage"], [])


class PercentileTest(unittest.TestCase):
    """Nearest-rank, stated as examples so the definition cannot drift."""

    def test_a_single_sample(self) -> None:
        self.assertEqual(perf_report.percentile([7.0], 0.99), 7.0)

    def test_an_empty_sample(self) -> None:
        self.assertEqual(perf_report.percentile([], 0.5), 0.0)

    def test_median_and_ends(self) -> None:
        """1..100, where every answer is a whole number by construction."""
        values = [float(n) for n in range(1, 101)]
        self.assertEqual(perf_report.percentile(values, 0.01), 1.0)
        self.assertEqual(perf_report.percentile(values, 0.25), 25.0)
        self.assertEqual(perf_report.percentile(values, 0.50), 50.0)
        self.assertEqual(perf_report.percentile(values, 0.75), 75.0)
        self.assertEqual(perf_report.percentile(values, 0.99), 99.0)

    def test_every_value_reported_was_really_measured(self) -> None:
        """No interpolation: an even-sized sample must not average two."""
        self.assertEqual(perf_report.percentile([10.0, 20.0], 0.50), 10.0)
        self.assertIn(perf_report.percentile([10.0, 20.0], 0.75), (10.0, 20.0))

    def test_p99_is_a_boundary_not_the_worst(self) -> None:
        """The distinction the column header has to get right.

        99 samples at 1ms and one at 1000ms: p99 is 1ms, because 99% of
        them WERE at or below 1ms. The 1000ms one is what `max` is for.
        Reading p99 as "the worst 1%" is how a report gets quoted as
        evidence of a problem it does not show.
        """
        values = sorted([1.0] * 99 + [1000.0])
        self.assertEqual(perf_report.percentile(values, 0.99), 1.0)
        self.assertEqual(max(values), 1000.0)

    def test_the_tail_is_not_the_mean(self) -> None:
        """The point of reporting both: slow calls hide in the mean."""
        values = sorted([1.0] * 98 + [1000.0, 1000.0])
        self.assertEqual(perf_report.percentile(values, 0.99), 1000.0)
        self.assertLess(perf_report.mean(values), 25.0)


class ReportLoadTest(unittest.TestCase):
    """Reading a log back."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "perf.log")

    def write(self, lines: List[str], suffix: str = "") -> None:
        with io.open(self.path + suffix, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_it_groups_by_label(self) -> None:
        self.write([
            '{"k":"label","i":1,"n":"dashboard"}',
            '{"k":"storage","l":1,"ms":10,"t":"2026-07-30T01:00:00.000000"}',
            '{"k":"storage","l":1,"ms":30,"t":"2026-07-30T01:00:01.000000"}',
        ])
        groups, counters = perf_report.load([self.path])
        self.assertEqual(counters["records"], 2)
        stats = groups["storage"]["dashboard"].stats()
        self.assertEqual(stats["n"], 2.0)
        self.assertEqual(stats["mean"], 20.0)
        self.assertEqual(stats["total"], 40.0)

    def test_a_half_written_last_line_is_survivable(self) -> None:
        """The normal state of a log a running server is appending to."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"k":"label","i":1,"n":"dashboard"}\n')
            handle.write('{"k":"storage","l":1,"ms":10,"t":"2026-07-30T01"}\n')
            handle.write('{"k":"storage","l":1,"ms":2')     # torn
        groups, counters = perf_report.load([self.path])
        self.assertEqual(counters["malformed"], 1)
        self.assertEqual(counters["records"], 1)

    def test_the_window_filters_records(self) -> None:
        self.write([
            '{"k":"label","i":1,"n":"dashboard"}',
            '{"k":"storage","l":1,"ms":10,"t":"2026-07-30T08:00:00.000000"}',
            '{"k":"storage","l":1,"ms":20,"t":"2026-07-30T10:00:00.000000"}',
        ])
        groups, _ = perf_report.load([self.path], since="2026-07-30T09:00:00")
        self.assertEqual(groups["storage"]["dashboard"].stats()["n"], 1.0)
        groups, _ = perf_report.load([self.path], until="2026-07-30T09:00:00")
        self.assertEqual(groups["storage"]["dashboard"].stats()["n"], 1.0)

    def test_labels_are_scoped_to_their_file(self) -> None:
        """Two files can use id 1 for different things."""
        self.write([
            '{"k":"label","i":1,"n":"old_method"}',
            '{"k":"storage","l":1,"ms":10,"t":"2026-07-30T01:00:00.000000"}',
        ], suffix=".1")
        self.write([
            '{"k":"label","i":1,"n":"new_method"}',
            '{"k":"storage","l":1,"ms":20,"t":"2026-07-30T02:00:00.000000"}',
        ])
        paths = perf_report.read_files(self.path, True)
        self.assertEqual(len(paths), 2)
        groups, _ = perf_report.load(paths)
        self.assertEqual(sorted(groups["storage"]), ["new_method",
                                                     "old_method"])

    def test_a_record_with_no_definition_is_counted_not_dropped(self) -> None:
        self.write([
            '{"k":"storage","l":9,"ms":10,"t":"2026-07-30T01:00:00.000000"}',
        ])
        groups, counters = perf_report.load([self.path])
        self.assertEqual(counters["unlabelled"], 1)
        self.assertEqual(len(groups["storage"]), 1)

    def test_queue_waits_and_statuses_are_kept_apart(self) -> None:
        self.write([
            '{"k":"label","i":1,"n":"GET /api/summary"}',
            '{"k":"request","l":1,"ms":50,"qms":3000,"s":200,'
            '"t":"2026-07-30T01:00:00.000000"}',
            '{"k":"request","l":1,"ms":40,"s":500,'
            '"t":"2026-07-30T01:00:01.000000"}',
        ])
        groups, _ = perf_report.load([self.path])
        group = groups["request"]["GET /api/summary"]
        stats = group.stats()
        self.assertEqual(stats["n"], 2.0)
        # Only one carried a wait; it must not be averaged over both.
        self.assertEqual(stats["qmax"], 3000.0)
        self.assertEqual(group.queued, [3000.0])
        self.assertEqual(group.bad_statuses(), "500x1")


class ReportCliTest(unittest.TestCase):
    """The command line."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "perf.log")
        log = perf.PerfLog(self.path)
        for index in range(30):
            log.record("storage", "activity_buckets", 0.1 + index / 1000.0)
            log.record("request", "GET /api/summary", 0.2,
                       {"qms": 2000.0 + index, "qd": 3, "s": 200})
        log.close()

    def run_cli(self, argv: List[str]) -> str:
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = perf_report.main([self.path] + argv)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def test_it_reports_both_sections(self) -> None:
        text = self.run_cli([])
        self.assertIn("Storage operations", text)
        self.assertIn("activity_buckets", text)
        self.assertIn("Requests", text)
        self.assertIn("GET /api/summary", text)

    def test_it_names_the_stall_as_contention(self) -> None:
        """The whole reason the queue wait is recorded."""
        text = self.run_cli([])
        self.assertIn("waited a second or more for a worker", text)
        self.assertIn("contention, not query time", text)

    def test_a_capped_listing_says_it_is_capped(self) -> None:
        """A --top cap must never read as the whole picture."""
        log = perf.PerfLog(self.path)
        for index in range(5):
            log.record("storage", "method_{0}".format(index), 0.01)
        log.close()
        text = self.run_cli(["--top", "2"])
        self.assertIn("further rows not shown", text)

    def test_csv_is_machine_readable(self) -> None:
        text = self.run_cli(["--csv"])
        rows = [line for line in text.strip().split("\n") if line]
        self.assertTrue(rows[0].startswith("kind,operation,"))
        self.assertTrue(any("activity_buckets" in row for row in rows[1:]))

    def test_one_kind_only(self) -> None:
        text = self.run_cli(["--kind", "storage"])
        self.assertIn("activity_buckets", text)
        self.assertNotIn("GET /api/summary", text)

    def test_a_missing_file_exits_2_with_the_reason(self) -> None:
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = perf_report.main([os.path.join(self.tmp, "no.log")])
        self.assertEqual(code, 2)
        self.assertIn("--perf-log", err.getvalue())

    def test_an_empty_window_says_so_rather_than_printing_zeros(self) -> None:
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = perf_report.main([self.path, "--since", "2099-01-01T00:00:00"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing to report", out.getvalue())


class ServerPerfTest(unittest.TestCase):
    """The server end: what a request actually records."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_perf_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "perf.log")
        self.storage = Storage(os.path.join(self.tmp, "t.db"),
                               max_connections=2)
        self.addCleanup(self.storage.close)

    def serve(self, log: Any) -> int:
        """Start a server with *log* attached; return its port."""
        srv = server.create_server(
            "127.0.0.1", 0, self.storage, STATIC_DIR, workers=2, perf=log)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)
        return srv.server_address[1]

    def records(self, kind: str) -> List[Dict[str, Any]]:
        """Records of one kind, with labels resolved."""
        parsed = read_records(self.path)
        names = {r["i"]: r["n"] for r in parsed if r["k"] == "label"}
        out = []
        for record in parsed:
            if record["k"] == kind:
                copy = dict(record)
                copy["label"] = names.get(record["l"])
                out.append(copy)
        return out

    def await_records(self, kind: str, count: int,
                      timeout: float = 15.0) -> List[Dict[str, Any]]:
        """Wait for *count* records of *kind*, then return them.

        A request record is written AFTER the response has been sent —
        the timing has to include writing it — so a client can have its
        answer in hand before the worker has logged it. Reading the file
        the instant the response arrives is therefore a race, and it is
        one that only loses under load: these tests passed alone and
        failed two-at-a-time in the full suite, reporting zero records
        for a request that plainly happened.

        Waiting for the record rather than sleeping a fixed amount keeps
        the test fast when the machine is idle and correct when it is not.
        """
        deadline = time.time() + timeout
        found = self.records(kind)
        while len(found) < count and time.time() < deadline:
            time.sleep(0.05)
            found = self.records(kind)
        return found

    def test_nothing_is_written_when_no_log_is_given(self) -> None:
        """Off by default has to mean off, not "on to a default path"."""
        port = self.serve(None)
        urllib.request.urlopen(
            "http://127.0.0.1:{0}/api/summary".format(port), timeout=30).read()
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         sorted(["t.db", "t.db-shm", "t.db-wal"]))

    def test_a_request_is_recorded_with_its_route_and_status(self) -> None:
        log = perf.PerfLog(self.path)
        self.addCleanup(log.close)
        port = self.serve(log)
        urllib.request.urlopen(
            "http://127.0.0.1:{0}/api/summary?environment=x".format(port),
            timeout=30).read()
        requests = self.await_records("request", 1)
        log.close()
        self.assertEqual(len(requests), 1, requests)
        self.assertEqual(requests[0]["label"], "GET /api/summary")
        self.assertEqual(requests[0]["s"], 200)
        self.assertGreaterEqual(requests[0]["ms"], 0.0)

    def test_a_404_keeps_its_status(self) -> None:
        """send_error writes its own response and bypasses _write_response."""
        log = perf.PerfLog(self.path)
        self.addCleanup(log.close)
        port = self.serve(log)
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:{0}/nope.js".format(port), timeout=30).read()
        except urllib.error.HTTPError:
            pass
        statuses = [r["s"] for r in self.await_records("request", 1)]
        log.close()
        self.assertIn(404, statuses)

    def test_the_queue_wait_belongs_to_the_first_request_only(self) -> None:
        """Repeating it per request would multiply one stall into many.

        A worker is held for a whole connection, so the wait for a worker
        happened once. Attributing it to each of ten keep-alive requests
        would report ten stalls where there was one, and the report's
        "N connections waited a second or more" would count the same wait
        N times.
        """
        log = perf.PerfLog(self.path)
        self.addCleanup(log.close)
        port = self.serve(log)
        sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.addCleanup(sock.close)
        for _ in range(3):
            sock.sendall(b"GET /api/environments HTTP/1.1\r\nHost: x\r\n"
                         b"Connection: keep-alive\r\n\r\n")
            self.assertIn(b"200 OK", self._read_response(sock))
        requests = self.await_records("request", 3)
        log.close()
        self.assertEqual(len(requests), 3, requests)
        with_wait = [r for r in requests if "qms" in r]
        self.assertEqual(
            len(with_wait), 1,
            "the connection's queue wait was attributed to {0} of its 3 "
            "requests".format(len(with_wait)))
        self.assertIn("qms", requests[0], "it belongs to the FIRST request")
        self.assertIn("qd", requests[0], "the queue depth goes with it")

    def test_storage_calls_are_recorded_alongside_requests(self) -> None:
        log = perf.PerfLog(self.path)
        self.addCleanup(log.close)
        perf.instrument_storage(self.storage, log)
        port = self.serve(log)
        urllib.request.urlopen(
            "http://127.0.0.1:{0}/api/summary".format(port), timeout=30).read()
        self.await_records("request", 1)
        log.close()
        labels = {r["label"] for r in self.records("storage")}
        self.assertIn("summary_rollup", labels)
        self.assertTrue(self.records("request"))

    def _read_response(self, sock: "socket.socket") -> bytes:
        """Read one whole HTTP response, body included."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return data
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        return head


if __name__ == "__main__":
    unittest.main()
