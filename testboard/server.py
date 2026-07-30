"""HTTP server glue for testboard.

This module is the thin shell between raw sockets and the framework-free
API layer in :mod:`testboard.api`:

- A hand-composed :class:`ThreadingHTTPServer` (Python 3.6 has no
  ``http.server.ThreadingHTTPServer``) built on
  ``http.server.HTTPServer``, serving requests from a FIXED WORKER POOL.
  Deliberately **not** ``socketserver.ThreadingMixIn`` — see that class's
  docstring for the measurement that rules the mixin out, and
  ``tests/test_server_pool.py`` for the guard.
- A ``BaseHTTPRequestHandler`` subclass speaking HTTP/1.1 (so every
  response carries an accurate ``Content-Length``). Paths equal to
  ``/api`` or starting with ``/api/`` are turned into an
  :class:`testboard.api.Request` (body read per ``Content-Length``,
  capped at 256 MB -> 413) and delegated to
  :func:`testboard.api.handle_api`.
- Everything else is a GET-only static file space rooted at the server's
  ``static_dir`` (``/`` serves ``index.html``). Path traversal is blocked
  twice: decoded segments equal to ``..`` (or containing a path separator
  after decoding) are rejected outright, and the resolved
  ``os.path.realpath`` target must still live inside the resolved static
  root (compared case-insensitively via ``os.path.normcase``).

A worker serves a whole CONNECTION, not a single request, so keep-alive
and a fixed pool interact: see :data:`_KEEPALIVE_IDLE_SECONDS`.

Two transport-level behaviours apply to every response:

- **Validated caching of static files.** Each file is served with a
  strong ``ETag`` (its content hash) and ``Cache-Control: no-cache``,
  which asks the browser to revalidate rather than to skip caching:
  repeat requests answer ``304 Not Modified`` with no body. The bytes are
  cached in memory, keyed by the file's ``(mtime, size)``, so editing a
  file publishes the new version immediately. This is what stops a
  browser holding a stale ``app.js`` against a newer ``index.html`` —
  the failure mode of serving no cache headers at all.
- **gzip**, when the client offers it and the body is a compressible type
  above :data:`_MIN_GZIP_BYTES`. Responses then carry
  ``Vary: Accept-Encoding``.

There is no global mutable state: the :class:`~testboard.storage.Storage`
instance, the static directory and the static-file cache are attributes
of the server object (set by :func:`create_server`) and reached by the
handler through ``self.server``. Request logging is routed through
:mod:`logging`.

Python 3.6 compatible; standard library only.
"""

import gzip
import hashlib
import http.server
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Type, cast

from testboard import api
from testboard import perf as perf_module
from testboard.storage import DEFAULT_MAX_CONNECTIONS, Storage

__all__ = ["ThreadingHTTPServer", "create_server"]

_LOGGER = logging.getLogger(__name__)

#: Maximum accepted request body size (bytes); larger bodies get a 413.
_MAX_BODY_BYTES = 256 * 1024 * 1024

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: Below this size compression costs more than it saves (and a gzip
#: member has ~20 bytes of overhead of its own).
_MIN_GZIP_BYTES = 1024

#: Compression level: 6 is zlib's default; the payloads here are JSON and
#: source text, where the difference between 6 and 9 is a rounding error
#: on size but not on CPU.
_GZIP_LEVEL = 6

#: File-extension -> Content-Type map for static files. Text types carry
#: an explicit UTF-8 charset; anything unknown is served as octet-stream.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}  # type: Dict[str, str]

_DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: Content types worth gzipping. Everything else (PNG, ICO) is already
#: compressed, so a second pass would only burn CPU.
_COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "image/svg+xml",
)


class _CachedFile(NamedTuple):
    """A static file held in memory, with the stat fields that validate it."""

    mtime: float
    size: int
    etag: str
    content: bytes


#: Worker threads serving requests. Each holds one SQLite connection for
#: its lifetime, so this is also the connection count — which is why it
#: is the same number Storage divides a --cache-mb budget by, taken from
#: there rather than restated here so that the two cannot drift apart.
DEFAULT_WORKERS = DEFAULT_MAX_CONNECTIONS

#: Connections allowed to wait per worker before the accept loop blocks.
_QUEUE_DEPTH_PER_WORKER = 16

#: How long server_close() waits for a worker to finish its request.
_WORKER_JOIN_SECONDS = 10.0

#: How long a worker waits for the NEXT request on a connection it has
#: already answered, before closing it and going back to the pool.
#:
#: This number is load-bearing, and its absence was a production stall.
#: A worker serves a whole connection, and with ``protocol_version =
#: "HTTP/1.1"`` the client decides when that connection ends. Python's
#: default handler timeout is ``None``, so a worker that had answered a
#: request sat in ``readline()`` on an idle socket FOREVER — until the
#: browser at the other end felt like closing it.
#:
#: Browsers hold keep-alive sockets open long after they are done with
#: them, and open several per origin (Chrome: up to six). Two tabs can
#: therefore hold every worker in an eight-worker pool without a single
#: request in flight, and the next person to load the page waits for a
#: browser on someone else's desk. Reproduced, and pinned by
#: ``tests/test_server_pool.py::KeepAliveTest``: two idle connections
#: against two workers, and the third request never arrived.
#:
#: 5s is Apache's ``KeepAliveTimeout`` default. It keeps connection reuse
#: across the ~10 files of one page load (a page load is milliseconds
#: apart, not seconds) while bounding how long an idle client can hold a
#: worker.
_KEEPALIVE_IDLE_SECONDS = 5.0

#: How often a worker waiting on an idle connection looks up to see
#: whether another connection is queued behind it. Bounds how long a
#: queued request waits for a worker that is doing nothing; four wakeups
#: a second per idle connection costs nothing measurable.
_IDLE_POLL_SECONDS = 0.25

#: Socket timeout for a request already in progress — per blocking read
#: or write, not per request. Generous on purpose: it also covers reading
#: an import body (up to 256 MB) and writing a large test output to a
#: slow client, and dropping either of those to free a worker sooner
#: would trade a stall for a failed import.
_ACTIVE_SECONDS = 60.0


class ThreadingHTTPServer(http.server.HTTPServer):
    """A threaded HTTP server serving requests from a FIXED worker pool.

    Python 3.6 does not ship ``http.server.ThreadingHTTPServer`` (added
    in 3.7), so the threading is hand-built here. It is a pool rather
    than the obvious ``socketserver.ThreadingMixIn`` for one reason, and
    it is a large one:

    ``ThreadingMixIn`` starts a NEW THREAD PER REQUEST. Storage keeps its
    SQLite connections in ``threading.local()``, so a new thread means a
    new connection, which means a brand-new empty page cache. Measured on
    the mixin: twenty requests opened twenty connections. The cache
    therefore never warmed - every request paid full price for every page
    it touched, and ``--cache-mb`` could not help, because a cache that
    is discarded after one request has nothing to accumulate.

    A fixed pool of long-lived worker threads fixes that without Storage
    changing at all: the same handful of threads serve every request, so
    each keeps its connection and its cache across them. It also bounds
    what was unbounded - connections, threads and memory now have a
    ceiling instead of growing with concurrent load - and makes the
    "budget divided by connection count" arithmetic in Storage exact
    rather than a guess.

    Instances created via :func:`create_server` additionally carry the
    injected ``storage`` and ``static_dir`` attributes used by the
    request handler, plus the static-file cache those handlers share.
    """

    daemon_threads = True

    # socketserver defaults this to True, which on Windows lets a second
    # server bind a port that is already being served — two processes,
    # one port, requests split between them and no error anywhere. A
    # busy port must fail loudly on every platform.
    allow_reuse_address = False

    # Injected by create_server(); annotated here so the handler can rely
    # on them without any module-level mutable state.
    storage = None  # type: Storage
    static_dir = ""  # type: str
    static_cache = None  # type: Dict[str, _CachedFile]
    static_cache_lock = None  # type: threading.Lock
    #: Optional performance log; None means nothing is measured at all.
    perf = None  # type: Optional[perf_module.PerfLog]
    #: Optional path to this site's own What's new notes; None disables
    #: them (the endpoint then reports an empty list, not a 404).
    site_notes_path = None  # type: Optional[str]

    def __init__(self, server_address: Any, handler: Any,
                 workers: int = DEFAULT_WORKERS) -> None:
        """Bind the socket and start the worker pool."""
        http.server.HTTPServer.__init__(self, server_address, handler)
        self.workers = max(1, int(workers))
        # Bounded on purpose. A full queue blocks the accept loop, which
        # is back-pressure: the alternative to making a client wait is
        # accepting work the server has no capacity for and running out
        # of memory instead.
        self._pending = queue.Queue(
            maxsize=self.workers * _QUEUE_DEPTH_PER_WORKER
        )  # type: Any
        self._threads = []  # type: List[threading.Thread]
        # How long the connection this worker is serving waited for a
        # worker, and how deep the queue was when it got one. Per thread
        # because a worker serves one connection at a time, so there is
        # no sharing to lock and nothing at module level.
        self._arrival = threading.local()
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._serve_from_queue,
                name="testboard-worker-{0}".format(index),
            )
            thread.daemon = True
            thread.start()
            self._threads.append(thread)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Hand the connection to the pool instead of spawning a thread."""
        # The arrival time rides with the connection so a worker can say
        # how long it waited. Unconditional and cheap: one clock read per
        # connection, whether or not anything is logging.
        self._pending.put((request, client_address, time.time()))

    def has_pending(self) -> bool:
        """True when at least one connection is waiting for a worker.

        Read by the handler to decide whether to keep a connection alive
        (see :data:`_KEEPALIVE_IDLE_SECONDS`). Deliberately approximate —
        ``Queue.empty()`` takes no lock and the answer can be stale by
        the time it is used. That is fine: this only chooses between
        "reuse this connection" and "close it and take the next", and
        being wrong either way costs one TCP handshake, not a request.
        """
        return not self._pending.empty()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log a failed request instead of printing it to stderr.

        The base implementation prints a traceback surrounded by dashes,
        which is how a client that navigated away mid-response ends up
        looking like a server fault in the log. A dropped connection is
        the client's decision and is expected; anything else is a real
        error and keeps its traceback.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            _LOGGER.debug("client %s disconnected: %r", client_address, exc)
            return
        _LOGGER.exception("error serving %s", client_address)

    def _serve_from_queue(self) -> None:
        """One worker: serve requests until the server closes.

        The connection this thread's Storage opens on its first request
        is still there on its thousandth, which is the entire point.
        """
        while True:
            item = self._pending.get()
            if item is None:
                break
            request, client_address, arrived = item
            # Queue depth is read AFTER dequeuing, so it is how many
            # connections are still waiting behind this one.
            self._arrival.waited = time.time() - arrived
            self._arrival.depth = self._pending.qsize()
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
        # Close this worker's database connection deterministically,
        # rather than leaving it to whenever the thread's locals are
        # collected.
        storage = self.storage
        if storage is not None:
            try:
                storage.close()
            except Exception:  # pragma: no cover - shutdown is best-effort
                pass

    def server_close(self) -> None:
        """Stop the workers, then close the listening socket."""
        for _ in self._threads:
            self._pending.put(None)
        for thread in self._threads:
            thread.join(timeout=_WORKER_JOIN_SECONDS)
        self._threads = []
        http.server.HTTPServer.server_close(self)


def _is_api_path(raw_path: str) -> bool:
    """Return True when *raw_path* (undecoded, no query) is an API path."""
    return raw_path == "/api" or raw_path.startswith("/api/")


class _DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    """Thin HTTP shell: routes ``/api`` to handle_api, serves static files.

    Speaks HTTP/1.1, therefore EVERY response written by this class goes
    through :meth:`_write_response`, which always sends an accurate
    ``Content-Length`` header (persistent connections would otherwise
    hang the client).
    """

    protocol_version = "HTTP/1.1"

    #: Applied by ``socketserver`` in ``setup()``. Non-None is what makes
    #: every socket operation here interruptible at all.
    timeout = _ACTIVE_SECONDS

    #: Status of the response just written, for the perf log. Set by
    #: :meth:`log_request`; None until a response has been sent.
    _perf_status = None  # type: Optional[int]

    # ------------------------------------------------------------------
    # Connection lifetime
    # ------------------------------------------------------------------

    def handle(self) -> None:
        """Serve requests on this connection, releasing the worker when idle.

        Same shape as the base implementation, with the waiting made
        explicit. A worker is bound to a connection for as long as this
        method runs, so "wait for the client to say something" and "do
        what the client asked" cannot share one timeout:

        - waiting is bounded by :data:`_KEEPALIVE_IDLE_SECONDS` and is
          abandoned sooner if another connection is queued for a worker;
        - working is bounded by the much longer :data:`_ACTIVE_SECONDS`,
          because it has to cover reading an import body and writing a
          large output to a slow client.

        One short timeout for both would abort a slow import. One long
        one for both is what caused the stall.
        """
        self.close_connection = True
        served = 0
        while True:
            if not self._wait_for_request(served > 0):
                self.close_connection = True
                return
            # A request is arriving: this is work, not waiting.
            self.connection.settimeout(_ACTIVE_SECONDS)
            started = time.time()
            self.handle_one_request()
            self._record_request(started, first=(served == 0))
            served += 1
            if self.close_connection:
                return

    def _wait_for_request(self, may_yield: bool) -> bool:
        """Wait for the next request on this connection.

        Returns True when one has begun to arrive, False when this
        connection should be given up — the client closed it, it went
        quiet for :data:`_KEEPALIVE_IDLE_SECONDS`, or (when *may_yield*)
        another connection is queued and needs this worker more than an
        idle client does.

        *may_yield* is False until this connection has had a response,
        and that distinction is load-bearing. A connection that has been
        accepted but not yet served is not an idle client holding a
        worker hostage — it is the queue. Yielding on contention there
        drops it without a reply, which under any real load is most of
        them: caught by
        ``StillAServerTest.test_more_clients_than_workers_all_complete``,
        as 15 of 20 clients getting "remote end closed connection
        without response".

        The wait is a poll rather than one long blocking read so that
        contention is noticed while it is happening. A worker blocked for
        the full idle timeout is a worker that cannot be recalled, and a
        request queued behind it waits out the whole of it: measured at
        5.00s before this loop existed, ~0.2s after.

        ``peek`` rather than ``select``: it answers "is there a request
        to read" for bytes already sitting in ``rfile``'s buffer as well
        as bytes still on the socket. Selecting on the socket alone would
        miss a pipelined request that the previous ``readline`` had
        already pulled into that buffer, and would then close the
        connection with the request unanswered. It consumes nothing, and
        a timed-out read leaves the stream exactly as it was.
        """
        deadline = time.monotonic() + _KEEPALIVE_IDLE_SECONDS
        while True:
            if may_yield and self._pool_is_contended():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.connection.settimeout(min(_IDLE_POLL_SECONDS, remaining))
            try:
                # b"" is EOF: the client has closed the connection.
                return bool(self.rfile.peek(1))
            except socket.timeout:
                continue
            except OSError:
                return False        # connection reset while idle

    def _record_request(self, started: float, first: bool) -> None:
        """Log how long this request took, and how long it queued.

        The queue wait belongs to the CONNECTION, so it is attributed to
        the first request served on it and omitted from the rest —
        repeating it on every request of a keep-alive connection would
        multiply one 3-second wait into ten and make the report describe
        a stall that happened once as one that happened constantly.

        It is the field worth having. "This request took 4 seconds" does
        not distinguish a slow query from a server with no free worker;
        "3.9 of those 4 seconds were spent queued" does.
        """
        server = cast(ThreadingHTTPServer, self.server)
        log = getattr(server, "perf", None)
        if log is None:
            return
        extra = {}  # type: Dict[str, Any]
        if first:
            arrival = getattr(server, "_arrival", None)
            waited = getattr(arrival, "waited", None) if arrival else None
            if waited is not None:
                extra["qms"] = round(waited * 1000.0, 3)
                extra["qd"] = getattr(arrival, "depth", 0)
        status = getattr(self, "_perf_status", None)
        if status is not None:
            extra["s"] = status
        # command/path are unset if the request line never parsed.
        method = getattr(self, "command", None) or "?"
        raw_path = getattr(self, "path", None) or "/"
        path = raw_path.split("?", 1)[0]
        log.record("request", perf_module.route_label(method, path),
                   time.time() - started, extra)

    def _pool_is_contended(self) -> bool:
        """True when another connection is queued for a worker."""
        server = cast(ThreadingHTTPServer, self.server)
        has_pending = getattr(server, "has_pending", None)
        # A plain HTTPServer (some tests build one) has no pool to be
        # contended, so keep-alive is unconditional there.
        return bool(has_pending()) if callable(has_pending) else False

    # ------------------------------------------------------------------
    # HTTP verb entry points
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        """Serve a GET: API paths via handle_api, the rest as static files."""
        raw_path, raw_query = self._split_target()
        if _is_api_path(raw_path):
            self._handle_api(raw_path, raw_query)
        else:
            self._serve_static(raw_path)

    def do_POST(self) -> None:
        """Serve a POST: API paths only; static paths answer 405."""
        self._handle_mutation()

    def do_PUT(self) -> None:
        """Serve a PUT: API paths only; static paths answer 405."""
        self._handle_mutation()

    # ------------------------------------------------------------------
    # API plumbing
    # ------------------------------------------------------------------

    def _handle_mutation(self) -> None:
        """Route a non-GET request: /api -> handler, static -> 405."""
        raw_path, raw_query = self._split_target()
        if _is_api_path(raw_path):
            self._handle_api(raw_path, raw_query)
        else:
            # Drain (or refuse to drain) the request body so the 405 does
            # not leave unread bytes desynchronizing a keep-alive socket.
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header else 0
            except (TypeError, ValueError):
                length = -1
            if 0 < length <= _MAX_BODY_BYTES:
                self.rfile.read(length)
            elif length != 0:
                self.close_connection = True
            self._write_plain(
                405,
                "405 Method Not Allowed\n",
                [("Allow", "GET")],
            )

    def _split_target(self) -> Tuple[str, str]:
        """Split the raw request target into (path, query-string)."""
        target = self.path
        if "?" in target:
            path, query = target.split("?", 1)
        else:
            path, query = target, ""
        return path, query

    def _handle_api(self, raw_path: str, raw_query: str) -> None:
        """Build an api.Request, delegate to handle_api, write the result."""
        length_header = self.headers.get("Content-Length")
        length = 0
        if length_header is not None:
            try:
                length = int(length_header)
            except (TypeError, ValueError):
                self.close_connection = True
                self._write_json_error(
                    400, "invalid Content-Length header"
                )
                return
        if length < 0:
            self.close_connection = True
            self._write_json_error(400, "invalid Content-Length header")
            return
        if length > _MAX_BODY_BYTES:
            # The body is deliberately NOT read; the connection must
            # close because unread bytes remain on the socket.
            self.close_connection = True
            self._write_json_error(
                413,
                "request body too large (limit is 256 MB)",
            )
            return
        body = self.rfile.read(length) if length > 0 else b""
        request = api.Request(
            method=self.command.upper(),
            path=raw_path,
            query=urllib.parse.parse_qs(raw_query),
            body=body,
        )
        server = cast(ThreadingHTTPServer, self.server)
        response = api.handle_api(
            server.storage, request,
            site_notes_path=getattr(server, "site_notes_path", None))
        self._write_response(response.status, response.headers, response.body)

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def _serve_static(self, raw_path: str) -> None:
        """Serve a file from static_dir; any suspicious path is a 404.

        Guard 1: each percent-decoded segment is rejected if it equals
        ``..`` or contains a path separator (``/`` or ``\\``) after
        decoding. Guard 2 (defense in depth): the ``os.path.realpath``
        of the target must still be inside the ``os.path.realpath`` of
        the static root, compared with ``os.path.normcase``.
        """
        segments = [seg for seg in raw_path.split("/") if seg]
        decoded = []  # type: List[str]
        for segment in segments:
            piece = urllib.parse.unquote(segment)
            if piece == ".." or "/" in piece or "\\" in piece:
                self._write_not_found()
                return
            decoded.append(piece)
        if not decoded:
            decoded = ["index.html"]

        static_dir = cast(ThreadingHTTPServer, self.server).static_dir
        root = os.path.realpath(static_dir)
        target = os.path.realpath(os.path.join(root, *decoded))
        norm_root = os.path.normcase(root)
        norm_target = os.path.normcase(target)
        if norm_target != norm_root and not norm_target.startswith(
            norm_root + os.sep
        ):
            self._write_not_found()
            return
        cached = self._load_file(target)
        if cached is None:
            self._write_not_found()
            return

        extension = os.path.splitext(target)[1].lower()
        content_type = _CONTENT_TYPES.get(extension, _DEFAULT_CONTENT_TYPE)
        # gzip is a different representation of the same file, so it gets
        # its own entity tag; the predicate matches the one _write_response
        # applies to the same content type and length.
        etag = cached.etag
        if self._will_gzip(content_type, cached.size):
            etag += "-gzip"

        headers = [
            ("Content-Type", content_type),
            ("ETag", etag),
            # "no-cache" means "cache it, but revalidate before reuse" —
            # the browser keeps the bytes and we answer 304, so an edited
            # file is never served stale.
            ("Cache-Control", "no-cache"),
        ]
        if self._if_none_match(etag):
            self._write_response(304, headers, b"")
            return
        self._write_response(200, headers, cached.content)

    def _load_file(self, target: str) -> Optional[_CachedFile]:
        """Return *target*'s bytes from the server's cache, or None.

        The cache entry is valid while the file's ``(mtime, size)`` are
        unchanged, so editing a static file takes effect on the next
        request without restarting the server.
        """
        try:
            stat = os.stat(target)
        except OSError:
            return None
        server = cast(ThreadingHTTPServer, self.server)
        with server.static_cache_lock:
            cached = server.static_cache.get(target)
        if (
            cached is not None
            and cached.mtime == stat.st_mtime
            and cached.size == stat.st_size
        ):
            return cached
        try:
            with open(target, "rb") as handle:
                content = handle.read()
        except OSError:
            return None
        entry = _CachedFile(
            mtime=stat.st_mtime,
            size=len(content),
            etag='"{}"'.format(hashlib.sha256(content).hexdigest()[:32]),
            content=content,
        )
        with server.static_cache_lock:
            server.static_cache[target] = entry
        return entry

    def _if_none_match(self, etag: str) -> bool:
        """True when the request's If-None-Match covers *etag* (-> 304)."""
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        candidates = [item.strip() for item in header.split(",")]
        return etag in candidates or "*" in candidates

    # ------------------------------------------------------------------
    # Response writing (always with Content-Length)
    # ------------------------------------------------------------------

    def _accepts_gzip(self) -> bool:
        """True when the client's Accept-Encoding offers gzip."""
        header = self.headers.get("Accept-Encoding") or ""
        return "gzip" in header.lower()

    def _will_gzip(self, content_type: str, length: int) -> bool:
        """Decide whether a body of this type and size gets compressed."""
        if length < _MIN_GZIP_BYTES:
            return False
        lowered = content_type.lower()
        if not lowered.startswith(_COMPRESSIBLE_PREFIXES):
            return False
        return self._accepts_gzip()

    def _write_response(
        self,
        status: int,
        headers: List[Tuple[str, str]],
        body: bytes,
    ) -> None:
        """Write a complete response, compressed when worthwhile.

        Always sends an accurate ``Content-Length`` (HTTP/1.1 keep-alive
        would otherwise hang the client). Compressible types carry
        ``Vary: Accept-Encoding`` whether or not this particular client
        got gzip, so a shared cache cannot serve one client's encoding to
        another.
        """
        # Adaptive keep-alive: when the pool has queued work, this
        # connection ends with this response.
        #
        # THIS AND THE POLL IN _wait_for_request ARE ONE MECHANISM. Do
        # not remove either alone. The poll reclaims a worker from an
        # idle connection, and reclaiming means CLOSING a connection the
        # client still believes is open — so the client has to be told,
        # and the only place to tell it is a response header. That is
        # what this is: the announcement half.
        #
        # Removing it while leaving the poll was tried, on the reasoning
        # that the poll alone fixes the starvation and this only closes
        # connections a page load would rather keep. Both halves of that
        # were true and the result was still much worse, because the
        # poll then reclaimed connections SILENTLY and clients sent their
        # next request into a socket the server had already closed.
        # Measured with a strict client (http.client, which unlike a
        # browser does not retry), against a 210 MB copy of the dev data:
        #
        #     users   announced closes / failed requests
        #     2       16 / 0        silent: 0 / 6
        #     4       37 / 0        silent: 0 / 16
        #     6       60 / 0        silent: 0 / 27
        #
        # Announced, nothing ever fails. Silent, 40% of requests on
        # reused connections die. A browser retries an idempotent GET and
        # would mostly paper over it; a POST — a comment, an assignment —
        # is not retried and simply fails in front of the user.
        #
        # The cost of announcing is one TCP handshake per closed
        # connection, which is a fraction of a millisecond on a LAN. That
        # is the right side of this trade, and `--workers` is the knob for
        # reducing how often it fires (24 workers took 4-user page loads
        # from 37 closes to 8).
        if not self.close_connection and self._pool_is_contended():
            self.close_connection = True

        content_type = ""
        for name, value in headers:
            if name.lower() == "content-type":
                content_type = value
        compressible = content_type.lower().startswith(
            _COMPRESSIBLE_PREFIXES
        )
        if compressible and self._will_gzip(content_type, len(body)):
            body = gzip.compress(body, _GZIP_LEVEL)
            headers = headers + [("Content-Encoding", "gzip")]

        self.send_response(status)
        for name, value in headers:
            if name.lower() == "content-length":
                continue  # always recomputed below
            self.send_header(name, value)
        if compressible:
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _write_json_error(self, status: int, message: str) -> None:
        """Write a JSON ``{"error": ...}`` body (used for 400/413)."""
        body = json.dumps({"error": message}).encode("utf-8")
        self._write_response(
            status, [("Content-Type", _JSON_CONTENT_TYPE)], body
        )

    def _write_not_found(self) -> None:
        """Write the static-space 404 response."""
        self._write_plain(404, "404 Not Found\n", [])

    def _write_plain(
        self,
        status: int,
        message: str,
        extra_headers: List[Tuple[str, str]],
    ) -> None:
        """Write a small plain-text response (static-space errors)."""
        body = message.encode("utf-8")
        headers = [("Content-Type", "text/plain; charset=utf-8")]
        headers.extend(extra_headers)
        self._write_response(status, headers, body)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """Note the status for the perf log, then log the line as usual.

        Hooked here rather than in :meth:`_write_response` because
        ``send_error`` writes its own response and never goes through it,
        so a 404 or 405 would otherwise be recorded with no status at
        all. Every response passes through ``send_response``, and
        ``send_response`` calls this.
        """
        self._perf_status = getattr(code, "value", code)
        http.server.BaseHTTPRequestHandler.log_request(self, code, size)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route per-request log lines through the module logger (INFO)."""
        _LOGGER.info("%s %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route error lines through the module logger.

        Closing an idle keep-alive connection is now the NORMAL end of
        every connection (see :data:`_KEEPALIVE_IDLE_SECONDS`), and
        ``handle_one_request`` reports it via ``log_error``. Logged at
        error level it would put several lines in the log per page load
        and make a healthy server look broken, so the expected case
        drops to DEBUG. Matching the stdlib's message text is the only
        way to tell it apart; if that text ever changes the line comes
        back as a WARNING, which is noisy but not wrong.
        """
        message = format % args
        if message.startswith("Request timed out"):
            _LOGGER.debug("%s idle connection closed: %s",
                          self.address_string(), message)
            return
        _LOGGER.warning("%s %s", self.address_string(), message)


def create_server(
    host: str,
    port: int,
    storage: Storage,
    static_dir: str,
    workers: Optional[int] = None,
    perf: Optional[perf_module.PerfLog] = None,
    site_notes_path: Optional[str] = None,
) -> ThreadingHTTPServer:
    """Create (and bind) the dashboard HTTP server; caller serves/closes it.

    *port* may be 0 for an ephemeral port; read the real port back from
    ``server.server_address[1]``. *storage*, *static_dir* and a fresh
    static-file cache are attached to the server instance for the request
    handler to use — nothing is stored at module level.

    *perf*, when given, receives a record per request (see
    :mod:`testboard.perf`). Instrumenting *storage* is the caller's job,
    not done here: a Storage may be shared with something that should not
    be measured, and wrapping it twice would double every record.

    *site_notes_path* points at this site's own What's new notes (see
    :mod:`testboard.site_notes`). It is read per request, so a note added
    by ``tools/add_site_note.py`` appears without a restart.
    """
    handler = cast(
        Type[http.server.BaseHTTPRequestHandler], _DashboardRequestHandler
    )
    # The pool size IS the connection count, so it comes from the same
    # place the cache budget was divided by - otherwise the two drift and
    # the arithmetic silently stops being true.
    if workers is None:
        workers = storage.max_connections
    server = ThreadingHTTPServer((host, port), handler, workers=workers)
    server.storage = storage
    server.static_dir = static_dir
    server.static_cache = {}
    server.static_cache_lock = threading.Lock()
    server.perf = perf
    server.site_notes_path = site_notes_path
    return server
