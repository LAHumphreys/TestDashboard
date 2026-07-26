"""End-to-end tests: a real threaded server exercised over HTTP.

Boots :func:`testboard.server.create_server` on an ephemeral port
(``port=0``) with a temp-file SQLite database and a temp static
directory, then drives it with :mod:`http.client`:

- the full API flow: import -> dashboard -> test detail -> history ->
  run output -> comment -> assignee -> users (with a percent-encoded
  ``script`` containing ``/``),
- static serving: ``/`` serves ``index.html`` with the right
  content type, ``.js`` gets ``application/javascript``,
- path traversal attempts (``/../CLAUDE.md`` and ``%2e%2e`` variants)
  answer 404 and never leak the file outside the static root,
- unknown static paths answer 404, non-GET static answers 405.

Cleanup is Windows-safe: connections and the server are closed before
the temp directory is removed with ``ignore_errors=True`` (WAL side
files or a lagging handler-thread connection must not fail the suite).

Python 3.6 compatible; standard library only.
"""

import datetime
import gc
import gzip
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import warnings
from typing import Any, Dict, Optional, Tuple

from testboard import model, server
from testboard.storage import Storage

_SECRET_CONTENT = "TOP-SECRET outside the static root"
_INDEX_HTML = "<!DOCTYPE html><html><body>testboard index</body></html>"
_APP_JS = "console.log('testboard');\n"


def _record(
    environment: str,
    script: str,
    test_name: str,
    result: str,
    start: datetime.datetime,
    duration: float,
    output: str,
    known_failure_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one /api/import transport dict for the tests."""
    end = start + datetime.timedelta(seconds=duration)
    return {
        "environment": environment,
        "script": script,
        "test_name": test_name,
        "result": result,
        "start_time": model.format_iso(start),
        "end_time": model.format_iso(end),
        "output": output,
        "source_link": "https://example.com/src/{0}".format(test_name),
        "known_failure_reason": known_failure_reason,
    }


class EndToEndTest(unittest.TestCase):
    """Boot the real server once per test and talk to it via http.client."""

    def setUp(self) -> None:
        """Create temp db + static dir, start the server, open a client."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_e2e_")
        # Removed LAST (addCleanup is LIFO); ignore_errors because WAL
        # side files / handler-thread connections may lag on Windows.
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._collect_thread_local_connections)

        self.static_dir = os.path.join(self.tmp, "static")
        os.mkdir(self.static_dir)
        # Binary writes: static bytes must round-trip exactly (Windows
        # text mode would rewrite \n as \r\n).
        with open(
            os.path.join(self.static_dir, "index.html"), "wb"
        ) as handle:
            handle.write(_INDEX_HTML.encode("utf-8"))
        with open(os.path.join(self.static_dir, "app.js"), "wb") as handle:
            handle.write(_APP_JS.encode("utf-8"))
        # A file OUTSIDE the static root: traversal must never reach it.
        with open(os.path.join(self.tmp, "CLAUDE.md"), "wb") as handle:
            handle.write(_SECRET_CONTENT.encode("utf-8"))

        self.storage = Storage(os.path.join(self.tmp, "e2e.db"))
        self.server = server.create_server(
            "127.0.0.1", 0, self.storage, self.static_dir
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

        self.addCleanup(self.storage.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self._stop_serving)

        self.conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10
        )
        self.addCleanup(self.conn.close)

    def _stop_serving(self) -> None:
        """Stop serve_forever and join the server thread."""
        self.server.shutdown()
        self.thread.join(timeout=10)

    @staticmethod
    def _collect_thread_local_connections() -> None:
        """Garbage-collect before removing the temp dir (Windows-safe).

        Storage keeps one sqlite connection per thread in a
        ``threading.local()``; connections owned by finished handler
        threads can only be reclaimed by the garbage collector, which
        (correctly) emits a ResourceWarning as it finalizes them. That
        is expected here, so it is silenced for this one collection.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Send one request on the shared keep-alive connection.

        Asserts that the response carries a Content-Length matching the
        body actually read (the handler speaks HTTP/1.1, so a missing or
        wrong Content-Length would hang or corrupt the connection).
        """
        headers = {}  # type: Dict[str, str]
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        self.conn.request(method, path, body=body, headers=headers)
        response = self.conn.getresponse()
        data = response.read()
        header_map = {
            key.lower(): value for key, value in response.getheaders()
        }
        self.assertIn("content-length", header_map)
        self.assertEqual(int(header_map["content-length"]), len(data))
        return response.status, header_map, data

    def _json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Send a JSON request and decode the JSON response body."""
        body = None  # type: Optional[bytes]
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        status, headers, data = self._request(method, path, body)
        self.assertEqual(
            headers.get("content-type"),
            "application/json; charset=utf-8",
        )
        decoded = json.loads(data.decode("utf-8"))
        self.assertIsInstance(decoded, dict)
        return status, decoded

    # ------------------------------------------------------------------
    # Full API flow over real HTTP
    # ------------------------------------------------------------------

    def test_full_api_flow(self) -> None:
        """import -> dashboard -> detail -> history -> output -> comment
        -> assignee -> users, with a script segment containing '/'."""
        environment = "linux-prod-sim"
        script = "regression/user_lifecycle.py"  # needs %2F in URLs
        test_name = "test_partial_update_retry"
        base = model.utcnow() - datetime.timedelta(days=1)

        runs = [
            _record(environment, script, test_name, "PASS",
                    base, 1.5, "all good\n"),
            _record(environment, script, test_name, "FAIL",
                    base + datetime.timedelta(hours=2), 2.5,
                    "Traceback (most recent call last):\n  boom\n"),
            _record(environment, script, "test_other", "PASS",
                    base, 0.5, "ok\n"),
        ]
        status, imported = self._json(
            "POST", "/api/import", {"runs": runs}
        )
        self.assertEqual(status, 200)
        self.assertEqual(imported["inserted"], 3)
        self.assertEqual(imported["updated"], 0)
        self.assertEqual(imported["rejected"], 0)
        self.assertEqual(imported["errors"], [])

        status, dashboard = self._json("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        rows = dashboard["tests"]
        self.assertEqual(len(rows), 2)
        by_name = {row["test_name"]: row for row in rows}
        self.assertEqual(by_name[test_name]["result"], "FAIL")
        self.assertEqual(by_name["test_other"]["result"], "PASS")
        self.assertNotIn("output", by_name[test_name])

        triple_path = "/api/tests/{0}/{1}/{2}".format(
            urllib.parse.quote(environment, safe=""),
            urllib.parse.quote(script, safe=""),
            urllib.parse.quote(test_name, safe=""),
        )
        status, detail = self._json("GET", triple_path)
        self.assertEqual(status, 200)
        self.assertEqual(detail["environment"], environment)
        self.assertEqual(detail["script"], script)
        self.assertEqual(detail["test_name"], test_name)
        self.assertEqual(detail["latest"]["result"], "FAIL")
        self.assertIn("analytics", detail)
        self.assertEqual(
            detail["analytics"]["window"]["run_count"], 2
        )

        status, history = self._json("GET", triple_path + "/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["runs"]), 2)
        self.assertEqual(history["runs"][0]["result"], "FAIL")
        self.assertEqual(history["runs"][1]["result"], "PASS")
        self.assertNotIn("output", history["runs"][0])

        run_id = history["runs"][0]["run_id"]
        status, run_detail = self._json(
            "GET", "/api/runs/{0}".format(run_id)
        )
        self.assertEqual(status, 200)
        self.assertEqual(run_detail["test_name"], test_name)
        self.assertIn("boom", run_detail["output"])

        status, comment = self._json(
            "POST",
            triple_path + "/comments",
            {"username": "alice", "text": "investigating this one"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(comment["comment"]["author"], "alice")

        status, comments = self._json("GET", triple_path + "/comments")
        self.assertEqual(status, 200)
        self.assertEqual(len(comments["comments"]), 1)
        self.assertEqual(
            comments["comments"][0]["text"], "investigating this one"
        )

        status, assigned = self._json(
            "PUT",
            triple_path + "/assignee",
            {"username": "bob", "assigned_by": "alice"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(assigned["assignee"], "bob")

        status, detail = self._json("GET", triple_path)
        self.assertEqual(status, 200)
        self.assertEqual(detail["assignee"], "bob")

        status, users = self._json("GET", "/api/users")
        self.assertEqual(status, 200)
        usernames = [user["username"] for user in users["users"]]
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    def test_reimport_is_idempotent_over_http(self) -> None:
        """POSTing the same batch twice updates instead of duplicating."""
        base = model.utcnow() - datetime.timedelta(hours=3)
        runs = [_record("env", "script.py", "test_a", "PASS",
                        base, 1.0, "ok\n")]
        status, first = self._json("POST", "/api/import", {"runs": runs})
        self.assertEqual(status, 200)
        self.assertEqual(first["inserted"], 1)
        status, second = self._json("POST", "/api/import", {"runs": runs})
        self.assertEqual(status, 200)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 1)
        status, dashboard = self._json("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(len(dashboard["tests"]), 1)

    def test_summary_over_http(self) -> None:
        """/api/summary reflects imported runs end-to-end."""
        base = model.utcnow() - datetime.timedelta(days=1, hours=2)
        runs = [
            _record("env", "suite.py", "test_a", "PASS",
                    base, 1.0, "ok\n"),
            _record("env", "suite.py", "test_a", "FAIL",
                    base + datetime.timedelta(days=1), 1.0, "boom\n"),
        ]
        status, imported = self._json("POST", "/api/import", {"runs": runs})
        self.assertEqual(status, 200)
        self.assertEqual(imported["inserted"], 2)

        status, summary = self._json("GET", "/api/summary?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(summary["status"]["total_tests"], 1)
        self.assertEqual(summary["status"]["new_failures"], 1)
        self.assertEqual(summary["environments"], ["env"])
        self.assertEqual(len(summary["trend"]["nights"]), 7)
        entry = summary["queues"]["new_failures"]["tests"][0]
        self.assertEqual(entry["test_name"], "test_a")
        self.assertEqual(entry["prev_result"], "PASS")

    def test_api_error_responses_are_json(self) -> None:
        """Unknown API routes / triples answer JSON errors, never HTML."""
        status, body = self._json("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})
        status, body = self._json("GET", "/api/tests/none/none/none")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def test_root_serves_index_html_with_content_type(self) -> None:
        """GET / returns index.html as text/html; charset=utf-8."""
        status, headers, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("content-type"), "text/html; charset=utf-8"
        )
        self.assertEqual(data.decode("utf-8"), _INDEX_HTML)

    def test_named_static_files_and_js_content_type(self) -> None:
        """Explicit static paths serve with the mapped content types."""
        status, headers, data = self._request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("content-type"), "text/html; charset=utf-8"
        )
        status, headers, data = self._request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("content-type"),
            "application/javascript; charset=utf-8",
        )
        self.assertEqual(data.decode("utf-8"), _APP_JS)

    def test_unknown_static_path_is_404(self) -> None:
        """A missing static file answers 404."""
        status, _headers, _data = self._request("GET", "/no-such-file.css")
        self.assertEqual(status, 404)

    def test_path_traversal_attempts_are_404(self) -> None:
        """Traversal via .., %2e%2e and %2f variants never escapes root."""
        attempts = [
            "/../CLAUDE.md",
            "/%2e%2e/CLAUDE.md",
            "/%2E%2E/CLAUDE.md",
            "/..%2fCLAUDE.md",
            "/%2e%2e%2fCLAUDE.md",
            "/static/../../CLAUDE.md",
            "/..%5cCLAUDE.md",
        ]
        for path in attempts:
            status, _headers, data = self._request("GET", path)
            self.assertEqual(
                status, 404, "expected 404 for {0!r}".format(path)
            )
            self.assertNotIn(
                _SECRET_CONTENT.encode("utf-8"),
                data,
                "traversal leaked file content for {0!r}".format(path),
            )

    def test_non_get_on_static_path_is_405(self) -> None:
        """POST to a static path answers 405 with an Allow: GET header."""
        status, headers, _data = self._request("POST", "/", b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("allow"), "GET")

    # ------------------------------------------------------------------
    # Caching and compression
    # ------------------------------------------------------------------

    def test_static_files_revalidate_with_an_etag(self) -> None:
        """A cached asset is confirmed with a 304, never served stale.

        Without this the browser is free to keep an old app.js against a
        newly deployed index.html, which shows up as a blank page.
        """
        status, headers, data = self._request("GET", "/app.js")
        self.assertEqual(status, 200)
        etag = headers.get("etag")
        self.assertTrue(etag, "static responses must carry an ETag")
        self.assertEqual(headers.get("cache-control"), "no-cache")
        self.assertEqual(data.decode("utf-8"), _APP_JS)

        status, _headers, data = self._request(
            "GET", "/app.js", extra_headers={"If-None-Match": etag}
        )
        self.assertEqual(status, 304)
        self.assertEqual(data, b"")

    def test_edited_static_file_gets_a_new_etag(self) -> None:
        """The in-memory cache is keyed on the file, not on the URL."""
        _status, headers, _data = self._request("GET", "/app.js")
        first = headers.get("etag")
        path = os.path.join(self.static_dir, "app.js")
        # Binary mode: Windows text mode would rewrite \n as \r\n.
        with open(path, "wb") as handle:
            handle.write(b"console.log('edited');\n")
        # Force a different (mtime, size) even on a coarse clock.
        os.utime(path, (0, 0))

        status, headers, data = self._request(
            "GET", "/app.js", extra_headers={"If-None-Match": first}
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("etag"), first)
        self.assertEqual(data.decode("utf-8"), "console.log('edited');\n")

    def test_json_is_gzipped_when_offered(self) -> None:
        """Large JSON compresses; the tiny response next to it does not."""
        self._seed_runs(count=60)
        status, headers, data = self._request(
            "GET", "/api/dashboard",
            extra_headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-encoding"), "gzip")
        self.assertEqual(headers.get("vary"), "Accept-Encoding")
        payload = json.loads(
            gzip.decompress(data).decode("utf-8")
        )
        self.assertEqual(payload["total"], 60)
        self.assertLess(
            len(data), len(json.dumps(payload).encode("utf-8"))
        )

    def test_identity_encoding_when_gzip_not_offered(self) -> None:
        self._seed_runs(count=60)
        status, headers, data = self._request(
            "GET", "/api/dashboard",
            extra_headers={"Accept-Encoding": "identity"},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("content-encoding"))
        self.assertEqual(json.loads(data.decode("utf-8"))["total"], 60)

    def test_dashboard_pages_over_http(self) -> None:
        """The list endpoint answers a window plus the exact total."""
        self._seed_runs(count=60)
        status, _headers, data = self._request(
            "GET", "/api/dashboard?limit=25&offset=50"
        )
        self.assertEqual(status, 200)
        page = json.loads(data.decode("utf-8"))
        self.assertEqual(len(page["tests"]), 10)
        self.assertEqual(page["total"], 60)
        self.assertEqual(page["offset"], 50)

    def _seed_runs(self, count: int) -> None:
        """Import *count* distinct tests, one run each."""
        runs = [
            _record(
                environment="linux-sim",
                script="suite/alpha.py",
                test_name="test_{0:03d}".format(index),
                result="PASS",
                start=datetime.datetime(2026, 7, 26, 2, 0, 0),
                duration=1.5,
                output="ok\n",
            )
            for index in range(count)
        ]
        status, _payload = self._json(
            "POST", "/api/import", {"runs": runs}
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
