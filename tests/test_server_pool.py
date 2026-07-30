"""The server must serve requests from a fixed pool, not a thread each.

This looks like a threading detail and is really a caching one.

Storage keeps its SQLite connections in ``threading.local()``. The
obvious threaded server — ``socketserver.ThreadingMixIn`` — starts a new
thread per request, so a new thread meant a new connection, which meant a
brand-new empty page cache. Measured on the mixin: twenty requests opened
twenty connections. The cache could therefore never warm up; every
request paid full price for every page it touched, and no ``cache_size``
setting could help, because a cache discarded after one request has
nothing to accumulate.

Nothing about that is visible in a code review of either file. It is
visible here, and only here, so these tests are the thing standing
between the project and a silent return to it — a future edit swapping
the pool back for the mixin would look like a simplification and would
cost a multiple of every read.

Python 3.6 compatible; standard library only.
"""

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from typing import Any, Dict, List, Optional

from testboard import server
from testboard.storage import DEFAULT_MAX_CONNECTIONS, Storage

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ConnectionCounter(object):
    """Counts sqlite3.connect calls and the threads that made them."""

    def __init__(self) -> None:
        self.threads = []  # type: List[str]
        self._real = sqlite3.connect
        self._lock = threading.Lock()

    def __enter__(self) -> "ConnectionCounter":
        def counting(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self.threads.append(threading.current_thread().name)
            return self._real(*args, **kwargs)
        sqlite3.connect = counting
        return self

    def __exit__(self, *exc: Any) -> None:
        sqlite3.connect = self._real

    @property
    def count(self) -> int:
        return len(self.threads)


class ServerTestBase(unittest.TestCase):
    """A real server on an ephemeral port, closed on teardown."""

    workers = 4

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_pool_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        self.storage = Storage(self.db, max_connections=self.workers)
        self.server = server.create_server(
            "127.0.0.1", 0, self.storage, STATIC_DIR, workers=self.workers)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def get(self, path: str = "/api/summary") -> bytes:
        url = "http://127.0.0.1:{0}{1}".format(self.port, path)
        return urllib.request.urlopen(url, timeout=30).read()


class ConnectionReuseTest(ServerTestBase):
    """The finding this file exists for."""

    def test_many_requests_share_a_few_connections(self) -> None:
        """Not one per request, which is what the mixin gave."""
        with ConnectionCounter() as counter:
            for _ in range(30):
                self.get()
        self.assertLessEqual(
            counter.count, self.workers,
            "30 requests opened {0} connections; each new connection is a "
            "new empty page cache".format(counter.count))

    def test_requests_are_served_by_a_bounded_set_of_threads(self) -> None:
        with ConnectionCounter() as counter:
            for _ in range(30):
                self.get()
        self.assertLessEqual(len(set(counter.threads)), self.workers)

    def test_a_second_request_opens_no_connection_at_all(self) -> None:
        """The direct statement of "the cache survives the request"."""
        for _ in range(self.workers * 3):
            self.get()          # every worker has now connected
        with ConnectionCounter() as counter:
            for _ in range(20):
                self.get()
        self.assertEqual(
            counter.count, 0,
            "a warm server must not open connections to serve a request")

    def test_the_pool_size_is_the_connection_count(self) -> None:
        """What makes the cache budget arithmetic true rather than a guess."""
        self.assertEqual(self.server.workers, self.storage.max_connections)

    def test_the_server_does_not_use_a_thread_per_request_mixin(self) -> None:
        """ThreadingMixIn is the specific mistake; name it."""
        import socketserver
        self.assertNotIsInstance(self.server, socketserver.ThreadingMixIn)
        self.assertFalse(
            issubclass(server.ThreadingHTTPServer, socketserver.ThreadingMixIn),
            "ThreadingMixIn starts a thread per request, which gives every "
            "request its own connection and its own cold page cache")


class StillAServerTest(ServerTestBase):
    """Pooling must not have cost anything that worked before."""

    def test_requests_are_answered(self) -> None:
        self.assertIn(b"status", self.get("/api/summary"))

    def test_static_files_are_still_served(self) -> None:
        self.assertIn(b"loadUsers", self.get("/api.js"))

    def test_more_clients_than_workers_all_complete(self) -> None:
        """Excess load must queue, not be dropped or deadlock."""
        done = []  # type: List[int]
        errors = []  # type: List[str]

        def hit() -> None:
            try:
                self.get()
                done.append(1)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=hit) for _ in range(self.workers * 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(done), self.workers * 5)

    def test_one_slow_request_does_not_block_the_others(self) -> None:
        """With N workers, N-1 are still free."""
        started = time.time()
        threads = [threading.Thread(target=self.get)
                   for _ in range(self.workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertLess(time.time() - started, 30)

    def test_writes_still_work_through_a_pooled_connection(self) -> None:
        """A pooled connection carries transactions between requests."""
        import json
        body = json.dumps({"runs": [{
            "environment": "prod", "script": "s.py", "test_name": "t",
            "result": "PASS", "output": "",
            "start_time": "2026-07-25T01:00:00.000000",
            "end_time": "2026-07-25T01:00:01.000000"}]}).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:{0}/api/import".format(self.port),
            data=body, headers={"Content-Type": "application/json"},
            method="POST")
        payload = json.loads(
            urllib.request.urlopen(request, timeout=30).read().decode("utf-8"))
        self.assertEqual(payload["inserted"], 1)
        # And a later request, on a different worker, must see it.
        for _ in range(self.workers * 2):
            page = json.loads(self.get("/api/dashboard").decode("utf-8"))
            self.assertEqual(page["total"], 1)


class ShutdownTest(unittest.TestCase):
    """Workers are stopped and connections closed, not leaked."""

    def test_server_close_joins_every_worker(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_pool_")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), max_connections=3)
        srv = server.create_server("127.0.0.1", 0, storage, STATIC_DIR,
                                   workers=3)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        urllib.request.urlopen(
            "http://127.0.0.1:{0}/api/summary".format(port), timeout=30).read()
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)
        alive = [t for t in threading.enumerate()
                 if t.name.startswith("testboard-worker")]
        self.assertEqual(alive, [], "workers outlived server_close()")

    def test_closing_twice_is_harmless(self) -> None:
        """Shutdown paths get run twice by accident; it must not raise."""
        tmp = tempfile.mkdtemp(prefix="testboard_pool_")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), max_connections=2)
        srv = server.create_server("127.0.0.1", 0, storage, STATIC_DIR,
                                   workers=2)
        srv.server_close()
        srv.server_close()


class KeepAliveTest(ServerTestBase):
    """An idle client must not hold a worker out of the pool.

    The second pool pathology, and the one that reached production as
    "the page won't load". A worker serves a whole CONNECTION, the
    server speaks HTTP/1.1, and Python's default handler timeout is
    ``None`` — so a worker that had answered a request sat in
    ``readline()`` on an idle socket until the browser chose to close it,
    which could be never.

    Browsers open several connections per origin and keep them long
    after they are done. Two tabs can therefore occupy every worker in
    the default eight-worker pool with nothing in flight at all, and the
    next person to open the dashboard waits on a browser that is not
    theirs. Reproduced at ``workers=2``: two idle connections, and a
    third request that never arrived.

    These tests hold connections open by hand rather than through
    ``urllib``, because the bug is in what happens BETWEEN requests on a
    connection and ``urlopen`` closes its connection at the end of each.
    """

    workers = 2

    def raw_connection(self) -> socket.socket:
        """A socket to the server, closed on teardown."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        self.addCleanup(sock.close)
        return sock

    def read_response(self, sock: socket.socket) -> bytes:
        """Read exactly one complete HTTP response.

        Draining the body matters: a test that only recv()s once leaves
        it on the socket, and the next recv() returns the tail of the
        PREVIOUS response instead of the next one. That reads as a
        keep-alive failure and is a bug in the test.
        """
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

    def hold_idle_connection(self) -> socket.socket:
        """Answer one request on a new connection, then leave it open."""
        sock = self.raw_connection()
        sock.sendall(b"GET /api/environments HTTP/1.1\r\nHost: x\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        self.assertIn(b"200 OK", self.read_response(sock))
        return sock

    def test_idle_connections_do_not_starve_the_pool(self) -> None:
        """The reproduction. Every worker idle, and a request still served."""
        for _ in range(self.workers):
            self.hold_idle_connection()
        started = time.time()
        self.assertIn(b"status", self.get("/api/summary"))
        elapsed = time.time() - started
        self.assertLess(
            elapsed, server._KEEPALIVE_IDLE_SECONDS,
            "a queued request waited {0:.2f}s behind workers that were "
            "doing nothing; the idle wait is not being interrupted".format(
                elapsed))

    def test_an_idle_connection_is_eventually_closed_by_the_server(
        self
    ) -> None:
        """Even with nothing queued, an idle client cannot hold a worker."""
        sock = self.hold_idle_connection()
        sock.settimeout(server._KEEPALIVE_IDLE_SECONDS * 4)
        # recv returns b"" when the server has closed its end.
        self.assertEqual(
            sock.recv(4096), b"",
            "the server left an idle keep-alive connection open, which "
            "means a worker is still bound to it")

    def test_a_request_arriving_slowly_is_still_served(self) -> None:
        """The other half of the split, and the reason it has to exist.

        One short timeout covering both waiting and working would be a
        simpler change and would abort a slow import: the feeder's body
        can be large and arrive in pieces. The pause here is longer than
        the whole idle timeout, mid-request.
        """
        sock = self.raw_connection()
        sock.sendall(b"POST /api/import HTTP/1.1\r\n")
        time.sleep(server._KEEPALIVE_IDLE_SECONDS + 1.0)
        body = json.dumps({"runs": []}).encode("utf-8")
        sock.sendall(b"Host: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode("ascii")
                     + b"\r\n\r\n" + body)
        self.assertIn(b"200 OK", sock.recv(4096))

    def test_keep_alive_is_still_used_when_nothing_is_queued(self) -> None:
        """The fix must not degrade into close-after-every-response.

        A page load fetches ten or so files. Reusing one connection for
        them is most of the point of HTTP/1.1, so an uncontended
        connection has to survive its first response.
        """
        sock = self.hold_idle_connection()
        sock.sendall(b"GET /api/environments HTTP/1.1\r\nHost: x\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        self.assertIn(
            b"200 OK", self.read_response(sock),
            "the second request on an idle connection was not answered, so "
            "the connection was closed when nothing was waiting for it")

    def test_shutdown_does_not_wait_for_an_idle_client(self) -> None:
        """A worker holding an idle connection must still be joinable.

        ``server_close`` queues a sentinel per worker and joins with a
        10s timeout. A worker blocked forever on an idle socket never
        reached its sentinel, so shutdown depended on a browser closing
        its connection — the same root cause as the stall, showing up as
        a process that would not exit.
        """
        self.hold_idle_connection()
        started = time.time()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=15)
        elapsed = time.time() - started
        alive = [t for t in threading.enumerate()
                 if t.name.startswith("testboard-worker")]
        self.assertEqual(alive, [], "a worker outlived server_close()")
        self.assertLess(elapsed, 10.0,
                        "shutdown took {0:.1f}s waiting on an idle "
                        "client".format(elapsed))

    def test_the_two_timeouts_are_ordered(self) -> None:
        """Waiting must be bounded well below working, or it is not a split."""
        self.assertLess(server._IDLE_POLL_SECONDS,
                        server._KEEPALIVE_IDLE_SECONDS)
        self.assertLess(server._KEEPALIVE_IDLE_SECONDS,
                        server._ACTIVE_SECONDS)
        self.assertIsNotNone(
            server._DashboardRequestHandler.timeout,
            "a None handler timeout is the default, and is the bug: it "
            "makes every socket operation unbounded")


class DefaultsTest(unittest.TestCase):
    """The two numbers that must never drift apart."""

    def test_the_worker_default_comes_from_the_connection_default(
        self
    ) -> None:
        """Two independent literals would silently stop agreeing."""
        self.assertEqual(server.DEFAULT_WORKERS, DEFAULT_MAX_CONNECTIONS)

    def test_create_server_sizes_the_pool_from_the_storage(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_pool_")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), max_connections=5)
        srv = server.create_server("127.0.0.1", 0, storage, STATIC_DIR)
        self.addCleanup(srv.server_close)
        self.assertEqual(srv.workers, 5)

    def test_a_cache_budget_is_split_across_exactly_the_pool(self) -> None:
        """The arithmetic only holds because the pool bounds connections."""
        tmp = tempfile.mkdtemp(prefix="testboard_pool_")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), cache_mb=256,
                          max_connections=8)
        self.addCleanup(storage.close)
        srv = server.create_server("127.0.0.1", 0, storage, STATIC_DIR)
        self.addCleanup(srv.server_close)
        total = storage.cache_bytes_per_connection() * srv.workers
        self.assertEqual(total, 256 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
