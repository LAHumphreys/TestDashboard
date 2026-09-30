"""The in-memory counters behind the Metrics page (WP-37).

Three properties matter more than the arithmetic here.

**Keeping them must not cost response time.** That was the requirement
they were built to, so the structural half of it is asserted rather
than hoped for: nothing on the path a request takes may acquire the
lock. A counter every worker has to queue for is a new way for requests
to wait on each other, introduced by the thing meant to find those.

**They must never break the server.** A page of gauges is not worth a
500 on the dashboard.

**They must not lie about the queue wait**, for the reason
``tests/test_perf.py`` gives: it is what separates "the query is slow"
from "there was no free worker".

Python 3.6 compatible; standard library only.
"""

import datetime
import gc
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple

import run_server
from testboard import api, metrics, server
from testboard.model import Result, RunRecord
from testboard.storage import Storage

BASE = datetime.datetime(2026, 9, 28, 2, 0, 0)


class _CountingLock(object):
    """A lock that counts how often it is taken."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.taken = 0

    def __enter__(self) -> "_CountingLock":
        self._lock.acquire()
        self.taken += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()


class _Clock(object):
    """A clock and a timer a test can move by hand."""

    def __init__(self) -> None:
        self.wall = 1790000000.0
        self.ticks = 0.0

    def now(self) -> float:
        return self.wall

    def timer(self) -> float:
        return self.ticks


def make(clock: Optional[_Clock] = None) -> metrics.Metrics:
    if clock is None:
        return metrics.Metrics()
    return metrics.Metrics(clock=clock.now, timer=clock.timer)


def request(counters: metrics.Metrics, route: str, seconds: float,
            status: Optional[int] = 200,
            waited: Optional[float] = None,
            target: str = "/x") -> None:
    counters.record_request(
        route, seconds, status, waited, counters.mark(), target)


class RequestTallyTest(unittest.TestCase):
    """What a route's row says."""

    def test_nothing_recorded_is_an_empty_snapshot(self) -> None:
        snapshot = make().snapshot()
        self.assertTrue(snapshot["collecting"])
        self.assertEqual(snapshot["requests"], [])
        self.assertEqual(snapshot["storage"], [])
        self.assertEqual(snapshot["slowest"], [])
        self.assertEqual(snapshot["totals"]["requests"], 0)

    def test_a_route_is_counted_and_averaged(self) -> None:
        counters = make()
        for seconds in (0.010, 0.030, 0.020):
            request(counters, "GET /api/summary", seconds)
        row = counters.snapshot()["requests"][0]
        self.assertEqual(row["route"], "GET /api/summary")
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["mean_ms"], 20.0)
        self.assertEqual(row["max_ms"], 30.0)
        self.assertEqual(row["total_ms"], 60.0)
        self.assertEqual(row["errors"], 0)

    def test_routes_are_ordered_by_where_the_time_went(self) -> None:
        """Not by count: a thousand cheap requests are not the finding."""
        counters = make()
        for _ in range(50):
            request(counters, "GET /api/users", 0.001)
        request(counters, "GET /api/watch", 0.900)
        routes = [row["route"] for row in counters.snapshot()["requests"]]
        self.assertEqual(routes, ["GET /api/watch", "GET /api/users"])

    def test_only_a_server_error_is_an_error(self) -> None:
        """A 404 for a mistyped test name is the server working."""
        counters = make()
        for status in (200, 304, 400, 404, 409, 500, 503, None):
            request(counters, "GET /api/x", 0.001, status=status)
        self.assertEqual(counters.snapshot()["requests"][0]["errors"], 2)

    def test_the_percentile_is_the_bucket_the_request_fell_in(
            self) -> None:
        counters = make()
        for _ in range(95):
            request(counters, "GET /api/x", 0.004)      # within 5 ms
        for _ in range(5):
            request(counters, "GET /api/x", 0.150)      # within 200 ms
        row = counters.snapshot()["requests"][0]
        self.assertEqual(row["p95_ms"], 5.0)
        self.assertEqual(sum(row["buckets"]), 100)
        request(counters, "GET /api/x", 0.150)
        self.assertEqual(
            counters.snapshot()["requests"][0]["p95_ms"], 200.0)

    def test_a_bucket_edge_belongs_to_the_bucket_it_closes(self) -> None:
        counters = make()
        request(counters, "GET /api/x", 0.010)
        row = counters.snapshot()["requests"][0]
        self.assertEqual(row["p95_ms"], 10.0)

    def test_slower_than_every_edge_has_no_upper_bound(self) -> None:
        """The page says "over 5 s"; it must not be told a number."""
        counters = make()
        request(counters, "POST /api/import", 9.0)
        row = counters.snapshot()["requests"][0]
        self.assertIsNone(row["p95_ms"])
        self.assertEqual(row["buckets"][-1], 1)
        self.assertEqual(
            len(row["buckets"]), len(metrics.BUCKET_EDGES_MS) + 1)

    def test_the_edges_are_reported_not_assumed(self) -> None:
        self.assertEqual(
            make().snapshot()["bucket_edges_ms"],
            list(metrics.BUCKET_EDGES_MS))


class QueueWaitTest(unittest.TestCase):
    """The wait belongs to the connection."""

    def test_the_mean_is_over_the_requests_that_carried_a_wait(
            self) -> None:
        """Nine requests on one keep-alive connection that waited three
        seconds once did not each wait a third of a second."""
        counters = make()
        request(counters, "GET /api/x", 3.1, waited=3.0)
        for _ in range(8):
            request(counters, "GET /api/x", 0.1, waited=None)
        row = counters.snapshot()["requests"][0]
        self.assertEqual(row["queue_mean_ms"], 3000.0)

    def test_no_wait_recorded_is_not_a_wait_of_zero(self) -> None:
        counters = make()
        request(counters, "GET /api/x", 0.1, waited=None)
        self.assertIsNone(
            counters.snapshot()["requests"][0]["queue_mean_ms"])

    def test_a_wait_of_zero_is_a_wait(self) -> None:
        counters = make()
        request(counters, "GET /api/x", 0.1, waited=0.0)
        self.assertEqual(
            counters.snapshot()["requests"][0]["queue_mean_ms"], 0.0)


class StorageTallyTest(unittest.TestCase):
    """Storage methods, and a request's own share of them."""

    class Fake(object):
        """Stands in for Storage: public methods, one calling another."""

        def __init__(self, clock: _Clock) -> None:
            self._clock = clock

        def inner(self) -> str:
            self._clock.ticks += 0.004
            return "inner"

        def outer(self) -> str:
            self._clock.ticks += 0.001
            return self.inner()

        def fails(self) -> None:
            self._clock.ticks += 0.002
            raise ValueError("no")

        def close(self) -> None:
            pass

        def size_report(self) -> Dict[str, Any]:
            return {}

        def _private(self) -> None:
            pass

        @property
        def max_connections(self) -> int:
            return 8

    def setUp(self) -> None:
        self.clock = _Clock()
        self.counters = make(self.clock)
        self.fake = self.Fake(self.clock)
        self.wrapped = metrics.instrument_storage(self.fake, self.counters)

    def _storage(self) -> Dict[str, Dict[str, Any]]:
        return {
            row["method"]: row
            for row in self.counters.snapshot()["storage"]}

    def test_it_wraps_the_public_methods_and_says_which(self) -> None:
        """A reflective wrapper's failure mode is wrapping nothing."""
        self.assertEqual(self.wrapped, ["fails", "inner", "outer"])

    def test_it_leaves_the_pages_own_questions_untimed(self) -> None:
        """Timing size_report would put the act of looking at the top
        of what is being looked at."""
        for name in ("close", "size_report", "memo_report",
                     "max_connections", "cache_bytes_per_connection",
                     "vacuum"):
            self.assertNotIn(name, self.wrapped)
        self.fake.size_report()
        self.fake.close()
        self.assertEqual(self._storage(), {})

    def test_a_wrapped_method_still_returns_and_still_raises(
            self) -> None:
        self.assertEqual(self.fake.outer(), "inner")
        with self.assertRaises(ValueError):
            self.fake.fails()
        self.assertEqual(self._storage()["fails"]["calls"], 1)
        self.assertEqual(self._storage()["fails"]["total_ms"], 2.0)

    def test_a_methods_time_includes_what_it_called(self) -> None:
        self.fake.outer()
        rows = self._storage()
        self.assertEqual(rows["outer"]["total_ms"], 5.0)
        self.assertEqual(rows["inner"]["total_ms"], 4.0)
        self.assertEqual(rows["outer"]["calls"], 1)
        self.assertEqual(rows["inner"]["calls"], 1)

    def test_a_requests_share_counts_the_outermost_call_once(
            self) -> None:
        """Summing the column above would say 9 ms were spent in
        storage by a request that spent 5."""
        mark = self.counters.mark()
        self.fake.outer()
        self.fake.inner()
        self.counters.record_request(
            "GET /api/x", 0.020, 200, None, mark, "/api/x")
        row = self.counters.snapshot()["requests"][0]
        self.assertEqual(row["storage_calls_mean"], 2.0)
        self.assertEqual(row["storage_mean_ms"], 9.0)

    def test_a_raising_call_does_not_leave_the_depth_raised(self) -> None:
        """Or every later call on that thread would look nested and no
        request would ever be charged for storage again."""
        with self.assertRaises(ValueError):
            self.fake.fails()
        mark = self.counters.mark()
        self.fake.inner()
        self.counters.record_request(
            "GET /api/x", 0.010, 200, None, mark, "/api/x")
        row = self.counters.snapshot()["requests"][0]
        self.assertEqual(row["storage_calls_mean"], 1.0)

    def test_one_requests_storage_is_not_charged_to_the_next(
            self) -> None:
        self.fake.inner()
        mark = self.counters.mark()
        self.counters.record_request(
            "GET /static", 0.001, 200, None, mark, "/style.css")
        row = self.counters.snapshot()["requests"][0]
        self.assertEqual(row["storage_calls_mean"], 0.0)
        self.assertEqual(row["storage_mean_ms"], 0.0)

    def test_a_request_with_no_mark_is_still_counted(self) -> None:
        self.counters.record_request(
            "GET /api/x", 0.010, 200, None, None, "/api/x")
        self.assertEqual(
            self.counters.snapshot()["requests"][0]["count"], 1)

    def test_the_real_storage_is_wrapped_too(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_metrics_")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Storage(os.path.join(tmp, "m.db"))
        self.addCleanup(store.close)
        counters = make()
        wrapped = metrics.instrument_storage(store, counters)
        self.assertGreater(len(wrapped), 40, wrapped)
        for name in ("summary_rollup", "upsert_runs", "dashboard"):
            self.assertIn(name, wrapped)
        self.assertNotIn("size_report", wrapped)
        store.known_environments()
        rows = counters.snapshot()["storage"]
        self.assertEqual(
            [row["method"] for row in rows], ["known_environments"])


class SlowestRequestsTest(unittest.TestCase):
    """The list a person reads first."""

    def test_it_keeps_the_slowest_and_no_more(self) -> None:
        counters = make()
        for index in range(60):
            request(counters, "GET /api/x", 0.001 * (index + 1),
                    target="/api/x?n={}".format(index))
        slowest = counters.snapshot()["slowest"]
        self.assertEqual(len(slowest), 20)
        self.assertEqual(slowest[0]["ms"], 60.0)
        self.assertEqual(slowest[-1]["ms"], 41.0)
        self.assertEqual(slowest[0]["target"], "/api/x?n=59")

    def test_it_says_what_was_asked_for_and_how_it_went(self) -> None:
        counters = make()
        request(counters, "GET /api/dashboard", 0.250, status=200,
                waited=0.2, target="/api/dashboard?q=cancel&limit=250")
        entry = counters.snapshot()["slowest"][0]
        self.assertEqual(entry["route"], "GET /api/dashboard")
        self.assertEqual(entry["target"], "/api/dashboard?q=cancel&limit=250")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["queue_ms"], 200.0)
        self.assertRegex(entry["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def test_a_long_target_is_cut(self) -> None:
        """It carries search text and test names, so it is bounded."""
        counters = make()
        request(counters, "GET /api/dashboard", 0.250,
                target="/api/dashboard?q=" + "x" * 5000)
        self.assertEqual(len(counters.snapshot()["slowest"][0]["target"]), 300)


class ResetTest(unittest.TestCase):
    """Start counting again from nothing."""

    def test_everything_goes_back_to_nothing(self) -> None:
        clock = _Clock()
        counters = make(clock)
        request(counters, "GET /api/x", 0.5)
        clock.wall += 3600
        counters.reset()
        snapshot = counters.snapshot()
        self.assertEqual(snapshot["requests"], [])
        self.assertEqual(snapshot["slowest"], [])
        self.assertEqual(snapshot["seconds"], 0.0)
        request(counters, "GET /api/x", 0.1)
        self.assertEqual(counters.snapshot()["requests"][0]["count"], 1)
        self.assertEqual(len(counters.snapshot()["slowest"]), 1)

    def test_another_threads_tallies_are_dropped_by_that_thread(
            self) -> None:
        """reset() cannot clear what another thread is writing to. It
        moves the generation on; the reader ignores the stale tallies
        at once, and their owner discards them on its next record."""
        counters = make()
        release = threading.Event()
        recorded = threading.Event()
        done = threading.Event()

        def worker() -> None:
            request(counters, "GET /api/x", 0.1)
            recorded.set()
            release.wait(10)
            request(counters, "GET /api/x", 0.1)
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.assertTrue(recorded.wait(10))
        self.assertEqual(counters.snapshot()["requests"][0]["count"], 1)
        counters.reset()
        self.assertEqual(counters.snapshot()["requests"], [])
        release.set()
        self.assertTrue(done.wait(10))
        self.assertEqual(counters.snapshot()["requests"][0]["count"], 1)

    def test_a_request_in_flight_across_a_reset_charges_nothing_odd(
            self) -> None:
        clock = _Clock()
        counters = make(clock)
        fake = StorageTallyTest.Fake(clock)
        metrics.instrument_storage(fake, counters)
        fake.inner()
        fake.inner()
        mark = counters.mark()
        counters.reset()
        counters.record_request(
            "GET /api/x", 0.010, 200, None, mark, "/api/x")
        row = counters.snapshot()["requests"][0]
        self.assertEqual(row["storage_calls_mean"], 0.0)
        self.assertEqual(row["storage_mean_ms"], 0.0)


class ManyThreadsTest(unittest.TestCase):
    """Eight workers, one page of figures."""

    def test_every_threads_tallies_are_merged(self) -> None:
        counters = make()

        def worker(index: int) -> None:
            for _ in range(200):
                request(counters, "GET /api/x", 0.001 * (index + 1))
                request(counters, "GET /api/t{}".format(index), 0.001)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        snapshot = counters.snapshot()
        by_route = {row["route"]: row for row in snapshot["requests"]}
        self.assertEqual(by_route["GET /api/x"]["count"], 1600)
        self.assertEqual(by_route["GET /api/x"]["max_ms"], 8.0)
        self.assertEqual(len(by_route), 9)
        self.assertEqual(snapshot["totals"]["requests"], 3200)

    def test_reading_while_others_write_does_not_raise(self) -> None:
        counters = make()
        stop = threading.Event()
        failures = []  # type: List[BaseException]

        def writer(index: int) -> None:
            count = 0
            while not stop.is_set():
                count += 1
                request(counters, "GET /api/w{}/{}".format(
                    index, count % 400), 0.001)

        threads = [
            threading.Thread(target=writer, args=(index,))
            for index in range(4)]
        for thread in threads:
            thread.start()
        try:
            for _ in range(200):
                try:
                    json.dumps(counters.snapshot())
                except Exception as exc:  # noqa: BLE001
                    failures.append(exc)
                    break
        finally:
            stop.set()
            for thread in threads:
                thread.join(30)
        self.assertEqual(failures, [])


class NoLockOnTheRequestPathTest(unittest.TestCase):
    """The requirement, as structure.

    Asserted by replacing the lock with one that counts: a test that
    timed the calls instead would pass on a quiet machine whatever the
    code did.
    """

    def setUp(self) -> None:
        self.counters = make()
        self.lock = _CountingLock()
        self.counters._lock = self.lock  # type: ignore

    def test_a_threads_first_record_takes_it_once(self) -> None:
        request(self.counters, "GET /api/x", 0.0001)
        # Registering the thread, and the first request is by
        # definition the slowest so far.
        self.assertEqual(self.lock.taken, 2)

    def test_recording_a_storage_call_never_takes_it(self) -> None:
        clock = _Clock()
        fake = StorageTallyTest.Fake(clock)
        self.counters.timer = clock.timer
        metrics.instrument_storage(fake, self.counters)
        fake.inner()
        before = self.lock.taken
        for _ in range(500):
            fake.outer()
        self.assertEqual(self.lock.taken, before)

    def test_an_ordinary_request_never_takes_it(self) -> None:
        """Ordinary: not among the twenty slowest so far."""
        for index in range(20):
            request(self.counters, "GET /api/x", 1.0 + index)
        before = self.lock.taken
        for _ in range(500):
            request(self.counters, "GET /api/x", 0.005)
        self.assertEqual(self.lock.taken, before)

    def test_only_a_request_among_the_slowest_takes_it(self) -> None:
        for index in range(20):
            request(self.counters, "GET /api/x", 1.0 + index)
        before = self.lock.taken
        request(self.counters, "GET /api/x", 50.0)
        self.assertEqual(self.lock.taken, before + 1)

    def test_a_planted_lock_would_be_caught(self) -> None:
        """The detector can fail: a record path that did take it."""
        request(self.counters, "GET /api/x", 0.001)
        before = self.lock.taken
        with self.counters._lock:
            pass
        self.assertEqual(self.lock.taken, before + 1)


class RouteLabelTest(unittest.TestCase):
    """Bounded labels, the performance log's own."""

    def test_an_api_route_reads_as_it_does_in_the_log(self) -> None:
        self.assertEqual(
            metrics.route_label(
                "GET", "/api/tests/linux/suite%2Fa.py/test_x/history"),
            "GET /api/tests/*/*/*/history")
        self.assertEqual(
            metrics.route_label("GET", "/api/summary"),
            "GET /api/summary")

    def test_every_static_file_is_one_row(self) -> None:
        for path in ("/", "/index.html", "/app.js", "/style.css", ""):
            self.assertEqual(
                metrics.route_label("GET", path), "GET (static files)")

    def test_a_path_that_only_looks_like_the_api_is_static(self) -> None:
        self.assertEqual(
            metrics.route_label("GET", "/apiary.html"),
            "GET (static files)")


def _records(count: int, build: Optional[str] = None) -> List[RunRecord]:
    return [
        RunRecord(
            environment="linux-sim" if index % 2 else "win-sim",
            script="suite.py", test_name="test_{}".format(index),
            result=Result.FAIL if index % 5 == 0 else Result.PASS,
            start_time=BASE + datetime.timedelta(
                seconds=index + (500 if build else 0)),
            end_time=BASE + datetime.timedelta(
                seconds=index + 2 + (500 if build else 0)),
            output="out\n", source_link="https://example.com",
            known_failure_reason=None, build=build,
        )
        for index in range(count)
    ]


class SizeAndMemoReportTest(unittest.TestCase):
    """What the page is told about the database. SQLite's own test;
    the same two reports run against MariaDB in
    tests/test_mariadb_backend.py."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_metrics_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Storage(os.path.join(self.tmp, "m.db"))
        self.addCleanup(self.store.close)
        self.store.upsert_runs(_records(12))
        self.store.upsert_runs(_records(4, build="feat/x"))

    def _traced(self, call: Callable[[], Any]) -> List[str]:
        seen = []  # type: List[str]
        conn = self.store._conn()
        conn.set_trace_callback(lambda statement: seen.append(statement))
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return seen

    def test_the_run_count_is_the_real_one(self) -> None:
        report = self.store.size_report()
        real = self.store._conn().execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(report["rows"]["runs"], real)
        self.assertEqual(report["rows"]["runs"], 16)
        self.assertEqual(report["rows"]["latest_runs"], 16)
        self.assertEqual(report["rows"]["streams"], 2)

    def test_it_never_counts_or_scans_the_runs(self) -> None:
        """656,680 runs took 55 ms to count on the dev-scale estate,
        and the two ends of the date range asked for TOGETHER took 95.
        Production holds several times that."""
        statements = [
            " ".join(statement.upper().split())
            for statement in self._traced(self.store.size_report)]
        self.assertGreater(len(statements), 5)
        for statement in statements:
            self.assertNotIn("COUNT(*) FROM RUNS", statement)
            self.assertNotIn("RUN_OUTPUTS", statement)
            self.assertFalse(
                "MIN(START_TIME)" in statement
                and "MAX(START_TIME)" in statement, statement)
        self.assertIn("run_outputs", self.store.size_report()[
            "rows_not_counted"])

    def test_both_ends_of_the_range_are_single_seeks(self) -> None:
        conn = self.store._conn()
        for end in ("MIN", "MAX"):
            plan = " | ".join(
                str(row[-1]) for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT {}(start_time) "
                    "FROM runs".format(end)).fetchall()).upper()
            self.assertIn("IDX_RUNS_START_TIME_RESULT", plan)

    def test_the_range_is_the_runs_own(self) -> None:
        report = self.store.size_report()
        self.assertEqual(report["oldest_run"], "2026-09-28T02:00:00.000000")
        self.assertEqual(report["newest_run"], "2026-09-28T02:08:23.000000")

    def test_each_stream_is_listed_with_what_it_holds(self) -> None:
        streams = self.store.size_report()["streams"]
        self.assertEqual(
            [(s["kind"], s["name"], s["tests"], s["environments"])
             for s in streams],
            [("mainline", "", 12, 2), ("build", "feat/x", 4, 2)])

    def test_it_says_what_engine_and_what_it_weighs(self) -> None:
        report = self.store.size_report()
        self.assertEqual(report["engine"], "SQLite")
        self.assertGreater(report["bytes"], 0)
        self.assertEqual(report["schema_version"], len(
            __import__("testboard.storage", fromlist=["x"]).MIGRATIONS))
        self.assertEqual(report["tables"], {})
        self.assertEqual(
            sorted(part["label"][:8] for part in report["parts"]),
            ["Database", "Write-ah", "of which"])
        json.dumps(report)

    def test_a_second_look_within_the_minute_reads_nothing(self) -> None:
        first = self.store.size_report()
        self.assertEqual(self._traced(self.store.size_report), [])
        self.assertEqual(self.store.size_report(), first)

    def test_a_write_does_not_make_it_read_again(self) -> None:
        """Deliberately: sizes move slowly, and a page left open during
        a run would otherwise re-read on every refresh."""
        self.store.size_report()
        self.store.upsert_runs(_records(1, build="feat/y"))
        self.assertEqual(self._traced(self.store.size_report), [])

    def test_it_is_read_again_once_the_minute_is_up(self) -> None:
        self.store.size_report()
        stored_at, kept = self.store._size_memo  # type: ignore
        self.store._size_memo = (stored_at - 61, kept)
        self.assertGreater(len(self._traced(self.store.size_report)), 5)

    def test_the_memo_counts_what_it_served_and_what_it_could_not(
            self) -> None:
        self.store.reset_memo_counts()
        cutoff = BASE - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        after_miss = self.store.memo_report()
        self.assertEqual(after_miss["hits"], 0)
        self.assertEqual(after_miss["misses"], 1)
        self.assertEqual(after_miss["entries"], 1)
        self.store.summary_rollup(cutoff)
        self.store.summary_rollup(cutoff, environment="win-sim")
        self.assertEqual(self.store.memo_report()["hits"], 2)
        self.assertEqual(self.store.memo_report()["misses"], 1)

    def test_every_clearing_write_is_counted(self) -> None:
        self.store.reset_memo_counts()
        cutoff = BASE - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        counts = self.store.upsert_runs([RunRecord(
            environment="win-sim", script="suite.py",
            test_name="test_new", result=Result.PASS,
            start_time=BASE + datetime.timedelta(hours=3),
            end_time=BASE + datetime.timedelta(hours=3, seconds=2),
            output="out\n", source_link="https://example.com",
            known_failure_reason=None, build=None)])
        self.assertEqual(counts.inserted, 1)
        self.store.add_comment(
            "win-sim", "suite.py", "test_0", "amy", "seen", BASE)
        report = self.store.memo_report()
        self.assertEqual(report["clears"], 2)
        self.assertEqual(report["entries"], 0)

    def test_an_unchanged_import_clears_nothing(self) -> None:
        """The feeder re-pushes its whole window every ten minutes; if
        that cleared the memo there would be no memo."""
        self.store.reset_memo_counts()
        self.store.upsert_runs(_records(12))
        self.assertEqual(self.store.memo_report()["clears"], 0)

    def test_resetting_the_counts_leaves_the_memo_alone(self) -> None:
        cutoff = BASE - datetime.timedelta(hours=1)
        self.store.summary_rollup(cutoff)
        self.store.reset_memo_counts()
        self.assertEqual(
            self.store.memo_report(),
            {"hits": 0, "misses": 0, "clears": 0, "entries": 1})
        self.assertEqual(self._traced(
            lambda: self.store.summary_rollup(cutoff)), [])


class MetricsEndpointTest(unittest.TestCase):
    """GET /api/metrics and POST /api/metrics/reset, as handlers."""

    def setUp(self) -> None:
        self.store = Storage(":memory:")
        self.addCleanup(self.store.close)
        self.store.upsert_runs(_records(6))

    def _call(self, method: str, path: str,
              counters: Optional[metrics.Metrics],
              expect: int = 200) -> Dict[str, Any]:
        response = api.handle_api(
            self.store,
            api.Request(method=method, path=path, query={}, body=b"{}"),
            metrics=counters)
        self.assertEqual(response.status, expect, response.body)
        return json.loads(response.body.decode("utf-8"))

    def test_without_counters_it_says_so_and_still_sizes(self) -> None:
        data = self._call("GET", "/api/metrics", None)
        self.assertEqual(data["activity"], {"collecting": False})
        self.assertEqual(data["database"]["rows"]["runs"], 6)
        self.assertIn("hits", data["memo"])

    def test_with_counters_it_reports_them(self) -> None:
        counters = make()
        request(counters, "GET /api/summary", 0.030)
        data = self._call("GET", "/api/metrics", counters)
        self.assertTrue(data["activity"]["collecting"])
        self.assertEqual(
            data["activity"]["requests"][0]["route"], "GET /api/summary")

    def test_reset_zeroes_both_and_changes_no_data(self) -> None:
        counters = make()
        request(counters, "GET /api/summary", 0.030)
        self.store.summary_rollup(BASE)
        self.assertEqual(
            self._call("POST", "/api/metrics/reset", counters),
            {"reset": True})
        data = self._call("GET", "/api/metrics", counters)
        self.assertEqual(data["activity"]["requests"], [])
        self.assertEqual(data["memo"]["misses"], 0)
        self.assertEqual(data["database"]["rows"]["runs"], 6)

    def test_reset_without_counters_is_not_an_error(self) -> None:
        self._call("POST", "/api/metrics/reset", None)

    def test_each_accepts_its_own_method_only(self) -> None:
        self._call("POST", "/api/metrics", None, expect=405)
        self._call("GET", "/api/metrics/reset", None, expect=405)


class ServedMetricsTest(unittest.TestCase):
    """A real server, real requests, and what it then says it did."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_metrics_e2e_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._collect)
        static = os.path.join(self.tmp, "static")
        os.mkdir(static)
        with open(os.path.join(static, "index.html"), "wb") as handle:
            handle.write(b"<!DOCTYPE html><html></html>")
        self.storage = Storage(os.path.join(self.tmp, "e2e.db"))
        self.storage.upsert_runs(_records(10))
        self.counters = metrics.Metrics()
        self.wrapped = metrics.instrument_storage(
            self.storage, self.counters)
        self.server = server.create_server(
            "127.0.0.1", 0, self.storage, static, metrics=self.counters)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.storage.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self._stop)
        self.conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(self.conn.close)

    def _stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=10)

    @staticmethod
    def _collect() -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()

    def _get(self, path: str, method: str = "GET") -> Tuple[int, bytes]:
        self.conn.request(
            method, path, body=b"{}" if method == "POST" else None,
            headers={"Content-Type": "application/json"}
            if method == "POST" else {})
        response = self.conn.getresponse()
        return response.status, response.read()

    def _metrics(self) -> Dict[str, Any]:
        status, body = self._get("/api/metrics")
        self.assertEqual(status, 200, body)
        return json.loads(body.decode("utf-8"))

    def test_what_was_served_is_what_is_reported(self) -> None:
        for _ in range(3):
            self.assertEqual(
                self._get("/api/summary?parts=headline")[0], 200)
        self.assertEqual(self._get("/index.html")[0], 200)
        self.assertEqual(self._get(
            "/api/tests/win-sim/suite.py/no_such_test")[0], 404)
        activity = self._metrics()["activity"]
        by_route = {row["route"]: row for row in activity["requests"]}
        self.assertEqual(by_route["GET /api/summary"]["count"], 3)
        self.assertEqual(by_route["GET (static files)"]["count"], 1)
        self.assertEqual(by_route["GET /api/tests/*/*/*"]["count"], 1)
        self.assertEqual(by_route["GET /api/tests/*/*/*"]["errors"], 0)
        self.assertGreater(
            by_route["GET /api/summary"]["storage_calls_mean"], 3)
        self.assertGreater(
            by_route["GET /api/summary"]["storage_mean_ms"], 0)
        self.assertEqual(
            by_route["GET (static files)"]["storage_calls_mean"], 0)
        methods = [row["method"] for row in activity["storage"]]
        self.assertIn("summary_rollup", methods)
        targets = [entry["target"] for entry in activity["slowest"]]
        self.assertIn("/api/summary?parts=headline", targets)

    def test_the_wait_is_counted_once_per_connection(self) -> None:
        """Five requests down one keep-alive connection."""
        for _ in range(5):
            self.assertEqual(self._get("/api/users")[0], 200)
        row = [
            entry for entry in self._metrics()["activity"]["requests"]
            if entry["route"] == "GET /api/users"][0]
        self.assertEqual(row["count"], 5)
        self.assertIsNotNone(row["queue_mean_ms"])
        waits = [
            entry["queue_ms"]
            for entry in self._metrics()["activity"]["slowest"]
            if entry["route"] == "GET /api/users"]
        self.assertEqual(
            len([wait for wait in waits if wait is not None]), 1, waits)

    def test_looking_is_itself_counted_and_is_not_charged_a_query(
            self) -> None:
        self._metrics()
        self._metrics()
        row = [
            entry for entry in self._metrics()["activity"]["requests"]
            if entry["route"] == "GET /api/metrics"][0]
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["storage_calls_mean"], 0)

    def test_reset_over_http(self) -> None:
        self._get("/api/users")
        self.assertEqual(self._get("/api/metrics/reset", "POST")[0], 200)
        routes = [
            row["route"]
            for row in self._metrics()["activity"]["requests"]]
        self.assertEqual(routes, ["POST /api/metrics/reset"])

    def test_a_server_without_counters_serves_as_before(self) -> None:
        plain = server.create_server(
            "127.0.0.1", 0, self.storage,
            os.path.join(self.tmp, "static"))
        thread = threading.Thread(target=plain.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", plain.server_address[1], timeout=10)
            try:
                conn.request("GET", "/api/metrics")
                response = conn.getresponse()
                body = json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()
        finally:
            plain.shutdown()
            thread.join(timeout=10)
            plain.server_close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["activity"], {"collecting": False})

    def test_a_counter_that_raises_does_not_fail_the_request(
            self) -> None:
        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("counters are broken")

        self.counters.record_request = broken  # type: ignore
        self.assertEqual(self._get("/api/users")[0], 200)
        self.assertEqual(self._get("/index.html")[0], 200)


class CommandLineTest(unittest.TestCase):
    """--no-metrics: the one decision an operator has."""

    def test_the_counters_are_on_unless_asked_not_to_be(self) -> None:
        parser = run_server.build_parser()
        self.assertFalse(parser.parse_args([]).no_metrics)
        self.assertTrue(parser.parse_args(["--no-metrics"]).no_metrics)

    def test_the_help_says_what_it_costs(self) -> None:
        text = run_server.build_parser().format_help()
        self.assertIn("--no-metrics", text)
        self.assertIn("no lock", " ".join(text.split()))


if __name__ == "__main__":
    unittest.main()
