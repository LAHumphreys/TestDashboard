"""HTTP server glue for testboard.

This module is the thin shell between raw sockets and the framework-free
API layer in :mod:`testboard.api`:

- A hand-composed :class:`ThreadingHTTPServer` (Python 3.6 has no
  ``http.server.ThreadingHTTPServer``) built from
  ``socketserver.ThreadingMixIn`` + ``http.server.HTTPServer`` with
  ``daemon_threads = True``.
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
import socketserver
import threading
import urllib.parse
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Type, cast

from testboard import api
from testboard.storage import Storage

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


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """A threaded HTTP server (one thread per request, daemon threads).

    Python 3.6's :mod:`http.server` does not ship
    ``ThreadingHTTPServer`` (added in 3.7), so it is composed here from
    ``ThreadingMixIn`` + ``HTTPServer``. Instances created via
    :func:`create_server` additionally carry the injected ``storage``
    and ``static_dir`` attributes used by the request handler, plus the
    static-file cache those handlers share.
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
        storage = cast(ThreadingHTTPServer, self.server).storage
        response = api.handle_api(storage, request)
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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route per-request log lines through the module logger (INFO)."""
        _LOGGER.info("%s %s", self.address_string(), format % args)


def create_server(
    host: str,
    port: int,
    storage: Storage,
    static_dir: str,
) -> ThreadingHTTPServer:
    """Create (and bind) the dashboard HTTP server; caller serves/closes it.

    *port* may be 0 for an ephemeral port; read the real port back from
    ``server.server_address[1]``. *storage*, *static_dir* and a fresh
    static-file cache are attached to the server instance for the request
    handler to use — nothing is stored at module level.
    """
    handler = cast(
        Type[http.server.BaseHTTPRequestHandler], _DashboardRequestHandler
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.storage = storage
    server.static_dir = static_dir
    server.static_cache = {}
    server.static_cache_lock = threading.Lock()
    return server
