"""WP-28: the ``--url-prefix`` flag (docs/NIGHT_RUN_2026-08-10.md §5).

Boots a real threaded server (:func:`testboard.server.create_server`,
same pattern as ``tests/test_e2e.py``) and drives it over HTTP, because
the property under test — a request line arriving with a prefix in
front of it — only exists at that layer; :mod:`testboard.api`'s own
routing never sees the raw, undecoded path a prefix is stripped from.

The behaviour prod needs, restated: nginx proxies ``/testboard/`` to
this server WITHOUT stripping the prefix (confirmed with the user), so
``/testboard/api/summary`` arrives verbatim and must be handled
identically to ``/api/summary``. Bare paths must ALSO keep working
unconditionally — every dev/staging box, every diagnostic curl on the
server itself, and every feeder posting straight to the backend port
depend on it — which is what makes a default-ON flag zero risk.

Python 3.6 compatible; standard library only.
"""

import gc
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import warnings
from typing import Any, Dict, Optional, Tuple

from testboard import model, server
from testboard.storage import Storage

_SECRET_CONTENT = "TOP-SECRET outside the static root"
_INDEX_HTML = "<!DOCTYPE html><html><body>testboard index</body></html>"


class _PrefixServerTestCase(unittest.TestCase):
    """Boot a real server with a configurable ``url_prefix``; subclasses
    set :data:`URL_PREFIX` and get one shared server for every test
    (server boot + a temp DB are the expensive parts; every test here
    is read-only or independently idempotent, so sharing is safe and
    keeps this module quick)."""

    #: Overridden by subclasses. "" disables prefix handling entirely.
    URL_PREFIX = "testboard"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="testboard_urlprefix_")
        cls.static_dir = os.path.join(cls.tmp, "static")
        os.mkdir(cls.static_dir)
        with open(
            os.path.join(cls.static_dir, "index.html"), "wb"
        ) as handle:
            handle.write(_INDEX_HTML.encode("utf-8"))
        # A file OUTSIDE the static root: traversal must never reach it,
        # prefixed or not — the exact fixture tests/test_e2e.py uses.
        with open(os.path.join(cls.tmp, "CLAUDE.md"), "wb") as handle:
            handle.write(_SECRET_CONTENT.encode("utf-8"))

        cls.storage = Storage(os.path.join(cls.tmp, "urlprefix.db"))
        cls.server = server.create_server(
            "127.0.0.1", 0, cls.storage, cls.static_dir,
            url_prefix=cls.URL_PREFIX,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=10)
        cls.server.server_close()
        cls.storage.close()
        # Windows-safe cleanup, same reasoning as test_e2e.py: a lagging
        # handler-thread connection can only be reclaimed by the GC, which
        # (correctly) emits a ResourceWarning finalizing it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """One request on a FRESH connection (redirects/keep-alive edge
        cases are exactly what this module tests, so a shared
        keep-alive connection across tests would be the wrong economy
        here — test_e2e.py's module makes that trade because it is
        testing the API/static behaviour, not the connection itself)."""
        headers = {}  # type: Dict[str, str]
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            header_map = {
                key.lower(): value for key, value in response.getheaders()
            }
            return response.status, header_map, data
        finally:
            conn.close()

    def _json(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Any]:
        body = None  # type: Optional[bytes]
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        status, _headers, data = self._request(method, path, body)
        return status, json.loads(data.decode("utf-8")) if data else None


class DefaultPrefixAcceptsBothShapesTest(_PrefixServerTestCase):
    """The default (``"testboard"``): bare AND prefixed paths both work,
    identically — the property that makes a default-ON flag zero risk."""

    URL_PREFIX = "testboard"

    def test_bare_api_path_still_works(self) -> None:
        """Every existing caller (dev, staging, a feeder on the direct
        backend port, this module's own test client) keeps working
        completely unchanged."""
        status, body = self._json("GET", "/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)

    def test_prefixed_api_path_reaches_the_same_handler(self) -> None:
        """/testboard/api/... is handled AS /api/... -- the nginx
        pass-through shape (confirmed: nginx will NOT strip it).

        /api/streams (not /api/environments): a clock-free endpoint --
        /api/environments' payload carries a staleness cutoff computed
        fresh from now() on every call, which would make two otherwise-
        identical requests differ by the microseconds between them and
        assert nothing about ROUTING at all.
        """
        bare_status, bare_body = self._json("GET", "/api/streams")
        prefixed_status, prefixed_body = self._json(
            "GET", "/testboard/api/streams")
        self.assertEqual(prefixed_status, bare_status)
        self.assertEqual(prefixed_body, bare_body)

    def test_bare_root_serves_index(self) -> None:
        status, headers, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("content-type"), "text/html; charset=utf-8")
        self.assertEqual(data.decode("utf-8"), _INDEX_HTML)

    def test_prefixed_root_with_trailing_slash_serves_index(self) -> None:
        """/testboard/ (WITH the trailing slash) needs no redirect --
        it already resolves relative links correctly, exactly like
        bare "/" always has."""
        status, headers, data = self._request("GET", "/testboard/")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("content-type"), "text/html; charset=utf-8")
        self.assertEqual(data.decode("utf-8"), _INDEX_HTML)

    def test_prefixed_index_html_serves_directly(self) -> None:
        status, _headers, data = self._request(
            "GET", "/testboard/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(data.decode("utf-8"), _INDEX_HTML)

    def test_no_trailing_slash_redirects_to_the_trailing_slash_form(
        self
    ) -> None:
        """/testboard (bare, no trailing slash) is "the file testboard
        in the root directory" to a browser, NOT "the testboard
        directory" -- serving content there directly would make every
        relative href/fetch on the page resolve one level too high and
        silently drop the prefix. A redirect fixes the address bar
        before anything renders."""
        status, headers, body = self._request("GET", "/testboard")
        self.assertEqual(status, 307)
        self.assertEqual(headers.get("location"), "/testboard/")
        self.assertEqual(body, b"")

    def test_the_redirect_preserves_the_query_string(self) -> None:
        status, headers, _body = self._request(
            "GET", "/testboard?product=Atlas")
        self.assertEqual(status, 307)
        self.assertEqual(
            headers.get("location"), "/testboard/?product=Atlas")

    def test_redirect_is_307_not_a_cacheable_permanent_redirect(self) -> None:
        """307 preserves the method (like 308) but is never cached as
        PERMANENT (unlike 301/302's browser-side GET-downgrade, and
        unlike 308's permanence) -- a canonicalization redirect a
        browser caches forever is state a rollback cannot take back;
        this one costs nothing and stays fully reversible."""
        status, _headers, _body = self._request("GET", "/testboard")
        self.assertEqual(status, 307)

    def test_prefix_suffix_is_not_mistaken_for_the_prefix(self) -> None:
        """/testboardX/... must NOT match: the prefix is a full path
        SEGMENT, bounded by "/" on both sides, not a string prefix --
        "testboardX" sharing a spelling with "testboard" is coincidence,
        not the same resource."""
        status, _headers, _data = self._request(
            "GET", "/testboardXtra/api/environments")
        self.assertEqual(status, 404)

    def test_double_prefix_is_stripped_only_once(self) -> None:
        """/testboard/testboard/api/... has ONE "/testboard" removed;
        the remainder ("/testboard/api/...") is routed as an ordinary
        path -- not an API path (does not start with "/api/"), and no
        such static file exists, so 404 -- never stripped a second
        time to accidentally succeed."""
        status, _headers, _data = self._request(
            "GET", "/testboard/testboard/api/environments")
        self.assertEqual(status, 404)

    def test_percent_encoded_prefix_does_not_match(self) -> None:
        """A percent-encoded attempt at the prefix is matched against
        the RAW (still-encoded) path, so it simply fails to match the
        literal marker and falls through as an ordinary (here, 404)
        static path -- not a bypass of anything, just no match."""
        status, _headers, data = self._request(
            "GET", "/%74estboard/api/environments")
        self.assertEqual(status, 404)
        self.assertNotIn(_SECRET_CONTENT.encode("utf-8"), data)

    def test_traversal_under_the_prefix_is_still_blocked(self) -> None:
        """The traversal guard runs AFTER prefix stripping, on the
        stripped path -- so a prefixed traversal attempt is caught
        exactly like an unprefixed one, never bypassed by the prefix."""
        attempts = [
            "/testboard/../CLAUDE.md",
            "/testboard/%2e%2e/CLAUDE.md",
            "/testboard/..%2fCLAUDE.md",
            "/testboard/static/../../CLAUDE.md",
        ]
        for path in attempts:
            status, _headers, data = self._request("GET", path)
            self.assertEqual(
                status, 404, "expected 404 for {0!r}".format(path))
            self.assertNotIn(
                _SECRET_CONTENT.encode("utf-8"), data,
                "traversal leaked file content for {0!r}".format(path))

    def test_unprefixed_traversal_still_blocked_alongside_the_flag(
        self
    ) -> None:
        """WIDENED, not weakened (CLAUDE.md): the pre-existing
        unprefixed guard (tests/test_e2e.py) must still hold on a
        server that ALSO has a prefix configured."""
        status, _headers, data = self._request("GET", "/../CLAUDE.md")
        self.assertEqual(status, 404)
        self.assertNotIn(_SECRET_CONTENT.encode("utf-8"), data)

    def test_a_feeder_import_round_trips_on_the_bare_path(self) -> None:
        """Deliverable: a feeder POSTing straight to the backend port
        on the BARE path (never the prefix -- feeders bypass nginx
        entirely, docs/NIGHT_RUN_2026-08-10.md §5.5) still round-trips
        cleanly against a prefix-ENABLED server -- the accept-both-
        shapes rule is what makes that automatic, with no feeder-side
        prefix awareness needed at all."""
        base = model.utcnow()
        run = {
            "environment": "prefix-test-env",
            "script": "suite.py",
            "test_name": "test_feeder_survives_the_prefix",
            "result": "PASS",
            "start_time": model.format_iso(base),
            "end_time": model.format_iso(base),
            "output": "ok\n",
            "source_link": "",
            "known_failure_reason": None,
        }
        status, imported = self._json(
            "POST", "/api/import", {"runs": [run]})
        self.assertEqual(status, 200)
        self.assertEqual(imported["inserted"], 1)
        self.assertEqual(imported["rejected"], 0)

        status, dashboard = self._json(
            "GET", "/api/dashboard?environment=prefix-test-env")
        self.assertEqual(status, 200)
        names = [row["test_name"] for row in dashboard["tests"]]
        self.assertIn("test_feeder_survives_the_prefix", names)

    def test_non_get_prefixed_path_also_strips_correctly(self) -> None:
        """POST/PUT go through the SAME _resolve_path() as GET (one
        chokepoint, not two) -- a prefixed POST /api/import must work
        exactly like the bare one."""
        base = model.utcnow()
        run = {
            "environment": "prefix-post-env",
            "script": "suite.py",
            "test_name": "test_prefixed_post",
            "result": "PASS",
            "start_time": model.format_iso(base),
            "end_time": model.format_iso(base),
            "output": "ok\n",
            "source_link": "",
            "known_failure_reason": None,
        }
        status, imported = self._json(
            "POST", "/testboard/api/import", {"runs": [run]})
        self.assertEqual(status, 200)
        self.assertEqual(imported["inserted"], 1)


class CustomPrefixTest(_PrefixServerTestCase):
    """--url-prefix accepts any value, not just the default "testboard"
    (docs/NIGHT_RUN_2026-08-10.md §5: "configurable")."""

    URL_PREFIX = "dashboards/prod"

    def test_a_non_default_prefix_value_works(self) -> None:
        status, body = self._json(
            "GET", "/dashboards/prod/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)

    def test_the_default_prefix_string_no_longer_matches(self) -> None:
        """"testboard" is not special -- only the CONFIGURED value is."""
        status, _headers, _data = self._request(
            "GET", "/testboard/api/environments")
        self.assertEqual(status, 404)

    def test_bare_paths_still_work_under_a_custom_prefix(self) -> None:
        status, body = self._json("GET", "/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)


class DisabledPrefixTest(_PrefixServerTestCase):
    """--url-prefix "" disables prefix handling entirely: ONLY bare
    paths are served, exactly as before this flag existed."""

    URL_PREFIX = ""

    def test_bare_paths_work(self) -> None:
        status, body = self._json("GET", "/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)

    def test_the_default_prefix_string_does_not_work_when_disabled(
        self
    ) -> None:
        status, _headers, _data = self._request(
            "GET", "/testboard/api/environments")
        self.assertEqual(status, 404)

    def test_bare_no_trailing_slash_root_is_not_redirected(self) -> None:
        """With no prefix configured there is nothing to canonicalize
        -- "/" is already a directory URL and needs no redirect, same
        as always."""
        status, _headers, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(data.decode("utf-8"), _INDEX_HTML)


class SlashNormalizationTest(_PrefixServerTestCase):
    """create_server()'s url_prefix accepts a value with stray slashes
    -- "/testboard/", "/testboard", "testboard/" all mean the same
    thing as "testboard" (server.py's create_server() docstring)."""

    URL_PREFIX = "/testboard/"

    def test_a_slash_wrapped_prefix_value_normalizes(self) -> None:
        status, body = self._json("GET", "/testboard/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)

    def test_bare_paths_still_work(self) -> None:
        status, body = self._json("GET", "/api/environments")
        self.assertEqual(status, 200)
        self.assertIn("environments", body)


if __name__ == "__main__":
    unittest.main()
