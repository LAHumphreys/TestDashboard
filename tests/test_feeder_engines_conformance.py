"""Conformance suite for the single-file feeder engines (WP-29):
clients/feeder.py, clients/feeder.tcl, and the reduced
clients/feeder_micro.py (PythonMicroFeederConformanceTest below - NOT
a ScenarioMixin engine, because its semantics deliberately differ; its
class docstring says how, and its setUp splice of feeder.py's drop-in
section onto the micro engine is what holds the two Python engines to
an interchangeable IMPLEMENT THIS contract).

Each engine is driven as a SUBPROCESS - exactly how a contributing
product's test framework would invoke it from its own cleanup phase -
against either a real scratch dashboard server (booted in-process, the
same pattern tests/test_e2e.py uses) or a small controllable HTTP
stub, depending on what the scenario needs to force. The "stub reader"
both languages are driven with is not custom test code at all: it is
the shipped default read_records/--results JSON-lines reader that
ships in both engines as the worked "results-file reader" example, so
the fixtures are plain .jsonl files, identical in shape for both
languages.

SAME scenarios run for both engines via ScenarioMixin, which is
deliberately NOT a unittest.TestCase - only the two concrete
subclasses at the bottom (PythonFeederConformanceTest,
TclFeederConformanceTest) are, each supplying how to invoke its
engine. Splitting the scenario bodies into two independent test
classes would let them drift apart within a week; a shared mixin
cannot. TclFeederConformanceTest is skipped in its entirety when
tclsh is not on PATH - gated the same way the MariaDB dual-backend
suites are gated on TESTBOARD_TEST_DB_CNF (tests/backends.py): absent,
the Tcl variants simply do not exist, rather than reporting a wall of
skips.

Python 3.6 compatible; standard library only.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

from testboard import server
from testboard.storage import Storage

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDER_PY = os.path.join(REPO_ROOT, "clients", "feeder.py")
FEEDER_MICRO_PY = os.path.join(REPO_ROOT, "clients", "feeder_micro.py")
FEEDER_TCL = os.path.join(REPO_ROOT, "clients", "feeder.tcl")

TCLSH = shutil.which("tclsh")

#: A response the stub server hands back when a test does not care what
#: it says, as long as it is a valid, ack-carrying 200.
_DEFAULT_OK_PAYLOAD = {
    "inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0,
    "errors": [], "streams_seen": [],
}


# ----------------------------------------------------------------------
# Server fixtures
# ----------------------------------------------------------------------


class _StubControl:
    """Thread-safe script of canned responses plus a request log, shared
    between the test thread and the stub HTTP server's handler thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = []  # type: List[Tuple[Optional[str], bytes]]
        self._responses = []  # type: List[Tuple[int, Optional[Dict[str, Any]]]]

    def queue(self, status: int, payload: Optional[Dict[str, Any]]) -> None:
        """Add one scripted (status, payload) response. Once the queue is
        exhausted, the LAST entry repeats for every further request."""
        with self._lock:
            self._responses.append((status, payload))

    def next_response(self) -> Tuple[int, Optional[Dict[str, Any]]]:
        with self._lock:
            if not self._responses:
                return (200, _DEFAULT_OK_PAYLOAD)
            if len(self._responses) > 1:
                return self._responses.pop(0)
            return self._responses[0]

    def record(self, user_agent: Optional[str], body: bytes) -> None:
        with self._lock:
            self.requests.append((user_agent, body))

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        control = self.server.control  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        control.record(self.headers.get("User-Agent"), body)
        status, payload = control.next_response()
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep test output quiet


def _start_stub_server(control: _StubControl) -> Tuple[http.server.HTTPServer, int]:
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    httpd.control = control  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def _start_real_server(tmp_dir: str) -> Tuple[Any, Storage, int]:
    static_dir = os.path.join(tmp_dir, "static")
    os.makedirs(static_dir, exist_ok=True)
    storage = Storage(os.path.join(tmp_dir, "conformance.db"))
    srv = server.create_server("127.0.0.1", 0, storage, static_dir)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, storage, port


def _start_blackhole() -> Tuple[socket.socket, int]:
    """A socket that is bound and LISTENING but never accept()s.

    A connect() to it still completes (the backlog absorbs it at the
    kernel level), so the client's request is "sent" successfully and
    then hangs waiting for a response that never comes - this is what
    actually exercises a client's read/response timeout, unlike an
    unused port (which answers ECONNREFUSED instantly and would prove
    nothing about the retry/timeout machinery).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    return sock, port


# ----------------------------------------------------------------------
# Shared scenario bodies
# ----------------------------------------------------------------------


class ScenarioMixin:
    """Test bodies shared by both engine languages. NOT a TestCase - see
    the module docstring. Subclasses provide ``self._invoke``."""

    ENGINE_LABEL = "override-me"

    def _invoke(
        self, args: List[str], cwd: str, timeout: float = 30.0,
    ) -> Tuple[int, str, str]:
        raise NotImplementedError

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="feeder_conformance_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.replay_dir = os.path.join(self.tmp, "replay")
        os.makedirs(self.replay_dir)

    # -- fixture helpers --------------------------------------------

    def _write_results(self, name: str, records: List[Dict[str, Any]]) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    @staticmethod
    def _record(test_name: str = "test_ok", **overrides: Any) -> Dict[str, Any]:
        base = {
            "script": "suite/alpha.py",
            "test_name": test_name,
            "result": "PASS",
            "start_time": "2026-08-10T02:00:00.000000",
            "end_time": "2026-08-10T02:00:01.000000",
            "output": "ok\n",
        }
        base.update(overrides)
        return base

    def _replay_files(self) -> List[str]:
        return sorted(
            os.path.join(self.replay_dir, name)
            for name in os.listdir(self.replay_dir)
            if name.startswith("testboard_feeder_replay_")
            and name.endswith(".json")
        )

    # -- scenarios (run once per concrete subclass / language) -------

    def test_bad_record_skipped_and_counted(self) -> None:
        path = self._write_results("mixed.jsonl", [
            self._record("test_ok"),
            self._record("test_bad", result="NOPE"),
        ])
        control = _StubControl()
        control.queue(200, dict(_DEFAULT_OK_PAYLOAD, inserted=1))
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn("skipped=1", err)
        self.assertEqual(control.request_count(), 1)
        sent = json.loads(control.requests[0][1].decode("utf-8"))
        self.assertEqual(len(sent["runs"]), 1)
        self.assertEqual(sent["runs"][0]["test_name"], "test_ok")

    def test_retry_then_replay_file_on_persistent_failure(self) -> None:
        path = self._write_results("one.jsonl", [self._record("test_a")])
        control = _StubControl()
        control.queue(500, {"error": "synthetic failure"})
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
            "--http-timeout", "2", "--time-budget", "20",
        ], self.tmp)
        self.assertEqual(rc, 1, err)
        # Three attempts (the documented MAX_ATTEMPTS), all against the
        # always-500 stub.
        self.assertGreaterEqual(control.request_count(), 3)
        replay_files = self._replay_files()
        self.assertEqual(len(replay_files), 1, replay_files)
        with open(replay_files[0], "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["runs"][0]["test_name"], "test_a")

    def test_later_invocation_drains_replay_file_first(self) -> None:
        path = self._write_results("one.jsonl", [self._record("test_a")])
        control = _StubControl()
        control.queue(500, {"error": "synthetic failure"})
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, _err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
            "--http-timeout", "2", "--time-budget", "20",
        ], self.tmp)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self._replay_files()), 1)

        # Second invocation: server now healthy, own batch is empty.
        empty_path = self._write_results("empty.jsonl", [])
        control.queue(200, dict(_DEFAULT_OK_PAYLOAD, updated=1))
        rc2, _out2, err2 = self._invoke([
            "--environment", "conf-env", "--results", empty_path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(self._replay_files(), [])
        # The FIRST request this second process makes must be the
        # drained replay file, not an empty own-batch.
        drained = json.loads(control.requests[-1][1].decode("utf-8"))
        self.assertEqual(drained["runs"][0]["test_name"], "test_a")

    def test_reinvoked_cleanup_is_a_noop_on_the_server(self) -> None:
        """Idempotency against a REAL dashboard: upsert + fingerprint
        skip mean a re-invoked cleanup writes nothing the second time."""
        srv, storage, port = _start_real_server(self.tmp)
        self.addCleanup(storage.close)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        path = self._write_results("one.jsonl", [self._record("test_repeat")])
        url = "http://127.0.0.1:{0}".format(port)

        rc1, _out1, err1 = self._invoke([
            "--environment", "conf-env-repeat", "--results", path,
            "--url", url, "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc1, 0, err1)
        self.assertIn("inserted=1", err1)

        rc2, _out2, err2 = self._invoke([
            "--environment", "conf-env-repeat", "--results", path,
            "--url", url, "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc2, 0, err2)
        self.assertIn("inserted=0", err2)
        self.assertIn("updated=1", err2)
        self.assertEqual(self._replay_files(), [])

    def test_two_concurrent_invocations_do_not_collide_on_replay_names(self) -> None:
        control = _StubControl()
        control.queue(500, {"error": "synthetic failure"})
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = "http://127.0.0.1:{0}".format(port)
        path_a = self._write_results("env-a.jsonl", [self._record("test_a")])
        path_b = self._write_results("env-b.jsonl", [self._record("test_b")])

        results = {}  # type: Dict[str, Tuple[int, str, str]]

        def run(name: str, environment: str, path: str) -> None:
            results[name] = self._invoke([
                "--environment", environment, "--results", path,
                "--url", url, "--replay-dir", self.replay_dir,
                "--http-timeout", "2", "--time-budget", "20",
            ], self.tmp)

        t1 = threading.Thread(target=run, args=("a", "conf-env-a", path_a))
        t2 = threading.Thread(target=run, args=("b", "conf-env-b", path_b))
        t1.start()
        t2.start()
        t1.join(60)
        t2.join(60)

        self.assertEqual(results["a"][0], 1, results["a"][2])
        self.assertEqual(results["b"][0], 1, results["b"][2])
        replay_files = self._replay_files()
        self.assertEqual(len(replay_files), 2, replay_files)
        bodies = []
        for path in replay_files:
            with open(path, "r", encoding="utf-8") as handle:
                bodies.append(json.load(handle)["runs"][0]["test_name"])
        self.assertEqual(sorted(bodies), ["test_a", "test_b"])

    def test_bounded_time_promise_against_a_blackholed_port(self) -> None:
        """A black-holed port must produce exit 1 within the documented
        ceiling (--time-budget + --http-timeout, plus process overhead),
        never a hang."""
        sock, port = _start_blackhole()
        self.addCleanup(sock.close)
        path = self._write_results("one.jsonl", [self._record("test_a")])
        http_timeout = 2.0
        time_budget = 3.0
        started = time.time()
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
            "--http-timeout", str(http_timeout),
            "--time-budget", str(time_budget),
        ], self.tmp, timeout=http_timeout + time_budget + 30.0)
        elapsed = time.time() - started
        self.assertEqual(rc, 1, err)
        ceiling = time_budget + http_timeout
        self.assertLess(
            elapsed, ceiling + 15.0,
            "{0}: black-holed port took {1:.1f}s, documented ceiling is "
            "{2:.1f}s (time-budget {3} + http-timeout {4}, plus slack "
            "for process startup)".format(
                self.ENGINE_LABEL, elapsed, ceiling, time_budget,
                http_timeout,
            ),
        )
        self.assertEqual(len(self._replay_files()), 1)
        # Recorded for the report - not asserted beyond the bound above.
        type(self).measured_blackhole_seconds = elapsed

    def test_build_and_streams_seen_handshake(self) -> None:
        srv, storage, port = _start_real_server(self.tmp)
        self.addCleanup(storage.close)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        path = self._write_results("one.jsonl", [self._record("test_build")])
        rc, _out, err = self._invoke([
            "--environment", "conf-env-build", "--build", "rc-conf-1",
            "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("no streams_seen key", err)
        self.assertEqual(self._replay_files(), [])

    def test_old_server_without_streams_seen_aborts_loudly(self) -> None:
        """A --build run against a server whose response has NO
        streams_seen key at all (simulating a pre-WP-21 server) must
        abort loudly: exit 1, replay saved, error names streams_seen."""
        control = _StubControl()
        control.queue(200, {
            "inserted": 1, "updated": 0, "unchanged": 0, "rejected": 0,
            "errors": [],
            # no "streams_seen" key - the old-server signature
        })
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        path = self._write_results("one.jsonl", [self._record("test_build")])
        rc, _out, err = self._invoke([
            "--environment", "conf-env-old", "--build", "rc-conf-2",
            "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--replay-dir", self.replay_dir,
        ], self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("streams_seen", err)
        replay_files = self._replay_files()
        self.assertEqual(len(replay_files), 1, replay_files)
        with open(replay_files[0], "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["runs"][0]["test_name"], "test_build")
        self.assertEqual(saved["runs"][0]["build"], "rc-conf-2")


# ----------------------------------------------------------------------
# Micro engine (clients/feeder_micro.py)
# ----------------------------------------------------------------------


def _split_on_banners(path: str) -> Tuple[str, str, str]:
    """(head, drop-in section, engine) around the two banner comment
    lines. Anchored to line-start comment form ("\\n# ...") because both
    files' module docstrings also SAY the banner phrases in prose."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("\n# IMPLEMENT THIS SECTION")
    end = text.index("\n# DO NOT EDIT BELOW THIS LINE")
    return text[:start], text[start:end], text[end:]


class PythonMicroFeederConformanceTest(unittest.TestCase):
    """clients/feeder_micro.py conformance, driven as a subprocess like
    the ScenarioMixin engines - but deliberately NOT via the mixin: the
    micro engine has no replay files and no --time-budget, so most
    shared scenarios assert behaviour it does not (and must not) have.
    What it shares with the full engines - skip-don't-abort, server
    idempotency, a bounded exit against a dead server - is asserted
    here with micro semantics: exit 1 leaves NOTHING on disk,
    re-invocation is the recovery, and --build is stamped but never
    acknowledged (the streams_seen trust cut is pinned below).

    The micro file ships its IMPLEMENT THIS section as STUBS (a site
    always writes its own flags and reader), so setUp builds the
    site-implemented copy every scenario drives: feeder.py's actual
    drop-in section (--results JSON-lines reader) spliced onto the
    micro engine. That splice doubles as the transplant guard for the
    micro docstring's promise that a section written for the full
    engine drops in unchanged - every scenario here re-proves it.
    Byte-equality of the sections is deliberately not asserted:
    feeder.py's section defines a DASHBOARD_URL the micro engine
    ignores (it requires --url) and ships the worked reader the micro
    file deliberately does not; what is pinned is the contract - same
    symbols, interchangeable code."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="feeder_micro_conformance_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        micro_head, _, micro_engine = _split_on_banners(FEEDER_MICRO_PY)
        _, full_dropin, _ = _split_on_banners(FEEDER_PY)
        self.site_engine = os.path.join(self.tmp, "feeder_site.py")
        with open(self.site_engine, "w", encoding="utf-8") as handle:
            handle.write(micro_head + full_dropin + micro_engine)

    def _invoke(
        self, args: List[str], cwd: str, timeout: float = 30.0,
        engine: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        cmd = [sys.executable, engine or self.site_engine] + args
        completed = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )

    def _write_results(self, name: str, records: List[Dict[str, Any]]) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def _files_created(self) -> List[str]:
        """Everything in the working directory that is not a fixture
        (.jsonl results, the spliced feeder_site.py) - the micro engine
        must never create ANY file, replay or other."""
        return sorted(
            name for name in os.listdir(self.tmp)
            if not name.endswith(".jsonl") and name != "feeder_site.py"
        )

    def test_bad_record_skipped_and_counted(self) -> None:
        path = self._write_results("mixed.jsonl", [
            ScenarioMixin._record("test_ok"),
            ScenarioMixin._record("test_bad", result="NOPE"),
        ])
        control = _StubControl()
        control.queue(200, dict(_DEFAULT_OK_PAYLOAD, inserted=1))
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
        ], self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn("skipped=1", err)
        self.assertEqual(control.request_count(), 1)
        sent = json.loads(control.requests[0][1].decode("utf-8"))
        self.assertEqual(len(sent["runs"]), 1)
        self.assertEqual(sent["runs"][0]["test_name"], "test_ok")

    def test_persistent_failure_exits_1_and_creates_no_files(self) -> None:
        path = self._write_results("one.jsonl", [ScenarioMixin._record("test_a")])
        control = _StubControl()
        control.queue(500, {"error": "synthetic failure"})
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--http-timeout", "2",
        ], self.tmp)
        self.assertEqual(rc, 1, err)
        # Exactly the documented MAX_ATTEMPTS, all against the
        # always-500 stub, then give up - and the giving up must leave
        # no file of any kind behind: exit 1 IS the persistence.
        self.assertEqual(control.request_count(), 3)
        self.assertEqual(self._files_created(), [])
        self.assertIn("re-invoke", err)

    def test_reinvocation_after_failure_recovers(self) -> None:
        """The micro recovery story end-to-end: a failed invocation
        saves nothing, and simply re-running the same command line
        against a healthy (real) server delivers the results."""
        path = self._write_results("one.jsonl", [ScenarioMixin._record("test_a")])
        control = _StubControl()
        control.queue(500, {"error": "synthetic failure"})
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--http-timeout", "2",
        ], self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertEqual(self._files_created(), [])

        srv, storage, real_port = _start_real_server(self.tmp)
        self.addCleanup(storage.close)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        rc2, _out2, err2 = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(real_port),
        ], self.tmp)
        self.assertEqual(rc2, 0, err2)
        self.assertIn("inserted=1", err2)

    def test_reinvoked_cleanup_is_a_noop_on_the_server(self) -> None:
        srv, storage, port = _start_real_server(self.tmp)
        self.addCleanup(storage.close)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        path = self._write_results(
            "one.jsonl", [ScenarioMixin._record("test_repeat")]
        )
        url = "http://127.0.0.1:{0}".format(port)

        rc1, _out1, err1 = self._invoke([
            "--environment", "conf-env-repeat", "--results", path,
            "--url", url,
        ], self.tmp)
        self.assertEqual(rc1, 0, err1)
        self.assertIn("inserted=1", err1)

        rc2, _out2, err2 = self._invoke([
            "--environment", "conf-env-repeat", "--results", path,
            "--url", url,
        ], self.tmp)
        self.assertEqual(rc2, 0, err2)
        self.assertIn("inserted=0", err2)
        self.assertIn("updated=1", err2)

    def test_build_records_accepted(self) -> None:
        srv, storage, port = _start_real_server(self.tmp)
        self.addCleanup(storage.close)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        path = self._write_results(
            "one.jsonl", [ScenarioMixin._record("test_build")]
        )
        rc, _out, err = self._invoke([
            "--environment", "conf-env-build", "--build", "rc-conf-1",
            "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
        ], self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn("inserted=1", err)

    def test_build_trusts_the_server_response(self) -> None:
        """The agreed micro cut (2026-08-12): NO streams_seen
        acknowledgement check. A --build run against a server whose
        response has no streams_seen key at all - the old-server
        signature the full engine refuses loudly - is trusted and
        exits 0 here. Sites that need the old-server guard use the
        full engine; this test pins the trust as a decision, not an
        oversight, so a reappearing check is as visible as a missing
        one."""
        control = _StubControl()
        control.queue(200, {
            "inserted": 1, "updated": 0, "unchanged": 0, "rejected": 0,
            "errors": [],
            # no "streams_seen" key - the old-server signature
        })
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        path = self._write_results(
            "one.jsonl", [ScenarioMixin._record("test_build")]
        )
        rc, _out, err = self._invoke([
            "--environment", "conf-env-old", "--build", "rc-conf-2",
            "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
        ], self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("streams_seen", err)
        self.assertEqual(control.request_count(), 1)
        sent = json.loads(control.requests[0][1].decode("utf-8"))
        self.assertEqual(sent["runs"][0]["build"], "rc-conf-2")

    def test_bounded_time_promise_against_a_blackholed_port(self) -> None:
        """With no --time-budget, the micro engine's documented ceiling
        is MAX_ATTEMPTS * --http-timeout plus the two fixed pauses."""
        sock, port = _start_blackhole()
        self.addCleanup(sock.close)
        path = self._write_results("one.jsonl", [ScenarioMixin._record("test_a")])
        http_timeout = 2.0
        ceiling = 3 * http_timeout + 2 * 2.0
        started = time.time()
        rc, _out, err = self._invoke([
            "--environment", "conf-env", "--results", path,
            "--url", "http://127.0.0.1:{0}".format(port),
            "--http-timeout", str(http_timeout),
        ], self.tmp, timeout=ceiling + 30.0)
        elapsed = time.time() - started
        self.assertEqual(rc, 1, err)
        self.assertLess(
            elapsed, ceiling + 15.0,
            "micro: black-holed port took {0:.1f}s, documented ceiling "
            "is {1:.1f}s (3 attempts x http-timeout {2} + two 2s "
            "pauses, plus slack for process startup)".format(
                elapsed, ceiling, http_timeout,
            ),
        )
        self.assertEqual(self._files_created(), [])

    def test_full_engine_flags_are_refused(self) -> None:
        """--replay-dir and --time-budget belong to the full engine. An
        invocation carrying them must fail with a usage error (exit 2),
        not silently ignore a durability flag the caller believed in."""
        path = self._write_results("one.jsonl", [ScenarioMixin._record("test_a")])
        for flag, value in (("--replay-dir", self.tmp), ("--time-budget", "20")):
            rc, _out, _err = self._invoke([
                "--environment", "conf-env", "--results", path,
                "--url", "http://127.0.0.1:1", flag, value,
            ], self.tmp)
            self.assertEqual(rc, 2, flag)

    def test_shipped_file_requires_url(self) -> None:
        """No DASHBOARD_URL constant is honoured and no default exists:
        an invocation without --url is a usage error, exit 2."""
        rc, _out, err = self._invoke(
            ["--environment", "conf-env"], self.tmp, engine=FEEDER_MICRO_PY,
        )
        self.assertEqual(rc, 2, err)
        self.assertIn("--url", err)

    def test_shipped_reader_stub_warns_and_sends_nothing(self) -> None:
        """The file as shipped (IMPLEMENT THIS still stubs) must say so
        and exit 0 without a single request - a copied-but-unfinished
        feeder in a cleanup step is loud in the log, never a crash and
        never a stray POST."""
        control = _StubControl()
        httpd, port = _start_stub_server(control)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, _out, err = self._invoke([
            "--environment", "conf-env",
            "--url", "http://127.0.0.1:{0}".format(port),
        ], self.tmp, engine=FEEDER_MICRO_PY)
        self.assertEqual(rc, 0, err)
        self.assertIn("not been implemented", err)
        self.assertEqual(control.request_count(), 0)


# ----------------------------------------------------------------------
# Concrete per-language classes
# ----------------------------------------------------------------------


class PythonFeederConformanceTest(ScenarioMixin, unittest.TestCase):
    ENGINE_LABEL = "python"

    def _invoke(
        self, args: List[str], cwd: str, timeout: float = 30.0,
    ) -> Tuple[int, str, str]:
        cmd = [sys.executable, FEEDER_PY] + args
        completed = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )


# Gated exactly like the MariaDB dual-backend suites (tests/backends.py,
# TESTBOARD_TEST_DB_CNF): when tclsh is absent, these classes are never
# DEFINED at all - not defined-then-skipped. unittest's discovery finds
# TestCase subclasses by inspecting module globals after import, so a
# class that was never assigned to a module-level name is invisible to
# it, the same way tests/backends.py generates zero MariaDB subclasses
# rather than skipping ones it did generate. That distinction matters
# here specifically: a class-level @unittest.skipUnless still shows up
# as "skipped" in the run summary, which is the wrong signal for "this
# variant does not exist on this host" and, more importantly, would
# have made it easy to not notice if a Tcl scenario were silently never
# exercised anywhere.
if TCLSH:

    class TclFeederConformanceTest(ScenarioMixin, unittest.TestCase):
        ENGINE_LABEL = "tcl"

        def _invoke(
            self, args: List[str], cwd: str, timeout: float = 30.0,
        ) -> Tuple[int, str, str]:
            cmd = [TCLSH, FEEDER_TCL] + args
            completed = subprocess.run(
                cmd, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout,
            )
            return (
                completed.returncode,
                completed.stdout.decode("utf-8", "replace"),
                completed.stderr.decode("utf-8", "replace"),
            )

    class TclSelfTestInvocationTest(unittest.TestCase):
        """Runs clients/feeder.tcl --self-test as one conformance check:
        it is how the hand-built JSON encoder/parser (advisor-flagged as
        needing unit tests, but with no Tcl test framework assumed
        present) actually gets exercised on every push."""

        def test_self_test_passes(self) -> None:
            completed = subprocess.run(
                [TCLSH, FEEDER_TCL, "--self-test"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30.0,
            )
            out = completed.stdout.decode("utf-8", "replace")
            self.assertEqual(completed.returncode, 0, out)
            self.assertIn("all checks passed", out)


if __name__ == "__main__":
    unittest.main()
