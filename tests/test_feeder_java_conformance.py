"""Conformance smoke tests for clients/FeederMicro.java (WP-30's Java
sibling of clients/feeder_micro.py).

Gated exactly like TclFeederConformanceTest in
test_feeder_engines_conformance.py: when javac/java are not both on
PATH, this class is never DEFINED (not defined-then-skipped), so a
host without a JDK gets zero rows in the run summary rather than a
wall of skips.

This is a smaller smoke test than the Python/Tcl engines get, not full
ScenarioMixin parity: the Java engine has no replay files and no
--time-budget (same reduced contract as the Python micro engine), so
most of that mixin's scenarios assert behaviour it deliberately does
not have. What is proven here: the file as shipped still ships its
IMPLEMENT THIS section as a stub; a spliced-in reader (the same
--results JSON-lines shape the Python/Tcl engines ship as their worked
default) actually compiles and runs; and the same server-facing
contract the Python micro engine promises - skip-don't-abort, real
idempotency, a bounded ceiling against a dead server, --build trusted
without a streams_seen check - holds for the Java build too. It reuses
the stub/real server fixtures from test_feeder_engines_conformance.py
rather than re-implementing them.

Python 3.6 compatible; standard library only.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from tests.test_feeder_engines_conformance import (
    ScenarioMixin,
    _DEFAULT_OK_PAYLOAD,
    _StubControl,
    _start_blackhole,
    _start_real_server,
    _start_stub_server,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDER_JAVA = os.path.join(REPO_ROOT, "clients", "FeederMicro.java")

JAVAC = shutil.which("javac")
JAVA = shutil.which("java")

_IMPLEMENT_BANNER = "\n    // IMPLEMENT THIS"
_ENGINE_BANNER = "\n    // DO NOT EDIT BELOW THIS LINE"

#: Spliced in place of the shipped stub - the same worked "--results
#: JSON-lines" shape the Python/Tcl engines ship as their default
#: reader, translated into the Java IMPLEMENT THIS contract. Fully
#: qualifies java.nio.file.* rather than adding an import, since the
#: splice only touches text between the two banners.
_TEST_READER = """
    // IMPLEMENT THIS (spliced in by tests/test_feeder_java_conformance.py):
    // a --results JSON-lines reader - not carried in the file as
    // shipped (the stub convention feeder_micro.py also uses), only
    // for this suite.
    static List<Map<String, String>> readRecords(List<String> siteArgs) throws Exception {
        List<Map<String, String>> out = new ArrayList<>();
        for (int i = 0; i < siteArgs.size(); i++) {
            if (!siteArgs.get(i).equals("--results") || i + 1 >= siteArgs.size()) {
                continue;
            }
            for (String line : java.nio.file.Files.readAllLines(
                    java.nio.file.Paths.get(siteArgs.get(i + 1)), StandardCharsets.UTF_8)) {
                if (line.trim().isEmpty()) continue;
                Object parsed = MiniJson.parse(line);
                if (!(parsed instanceof Map)) continue;
                Map<String, String> record = new LinkedHashMap<>();
                for (Object key : ((Map<?, ?>) parsed).keySet()) {
                    Object v = ((Map<?, ?>) parsed).get(key);
                    record.put((String) key, v == null ? null : String.valueOf(v));
                }
                out.add(record);
            }
        }
        return out;
    }

"""


if JAVAC and JAVA:

    class JavaFeederConformanceTest(unittest.TestCase):

        @classmethod
        def _compile(cls, source_text: str, dest_name: str) -> str:
            src_dir = tempfile.mkdtemp(prefix="feeder_java_src_", dir=cls.tmp_root)
            src_path = os.path.join(src_dir, "FeederMicro.java")
            with open(src_path, "w", encoding="utf-8") as handle:
                handle.write(source_text)
            classes_dir = os.path.join(cls.tmp_root, dest_name)
            os.makedirs(classes_dir)
            result = subprocess.run(
                [JAVAC, "-d", classes_dir, src_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                raise AssertionError(
                    "javac failed for {0}:\n{1}".format(
                        dest_name, result.stderr.decode("utf-8", "replace"),
                    )
                )
            return classes_dir

        @classmethod
        def setUpClass(cls) -> None:
            cls.tmp_root = tempfile.mkdtemp(prefix="feeder_java_conformance_")
            with open(FEEDER_JAVA, "r", encoding="utf-8") as handle:
                text = handle.read()
            start = text.index(_IMPLEMENT_BANNER)
            end = text.index(_ENGINE_BANNER)
            cls.stub_classes = cls._compile(text, "classes_stub")
            spliced = text[:start] + _TEST_READER + text[end:]
            cls.site_classes = cls._compile(spliced, "classes_site")

        @classmethod
        def tearDownClass(cls) -> None:
            shutil.rmtree(cls.tmp_root, ignore_errors=True)

        def setUp(self) -> None:
            self.tmp = tempfile.mkdtemp(prefix="feeder_java_case_")
            self.addCleanup(shutil.rmtree, self.tmp, True)

        def _invoke(
            self, args: List[str], cwd: str, classes: Optional[str] = None,
            timeout: float = 30.0,
        ) -> Tuple[int, str, str]:
            cmd = [JAVA, "-cp", classes or self.site_classes, "FeederMicro"] + args
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
            import json
            path = os.path.join(self.tmp, name)
            with open(path, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            return path

        # -- scenarios ------------------------------------------------

        def test_shipped_reader_stub_warns_and_sends_nothing(self) -> None:
            control = _StubControl()
            httpd, port = _start_stub_server(control)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            rc, _out, err = self._invoke([
                "--environment", "conf-env",
                "--url", "http://127.0.0.1:{0}".format(port),
            ], self.tmp, classes=self.stub_classes)
            self.assertEqual(rc, 0, err)
            self.assertIn("not been implemented", err)
            self.assertEqual(control.request_count(), 0)

        def test_bad_record_skipped_and_counted(self) -> None:
            import json
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

        def test_retry_then_give_up_on_persistent_failure(self) -> None:
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
            self.assertEqual(control.request_count(), 3)
            self.assertIn("re-invoke", err)

        def test_reinvoked_cleanup_is_a_noop_on_the_server(self) -> None:
            srv, storage, port = _start_real_server(self.tmp)
            self.addCleanup(storage.close)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            path = self._write_results(
                "one.jsonl", [ScenarioMixin._record("test_repeat")],
            )
            url = "http://127.0.0.1:{0}".format(port)

            rc1, _out1, err1 = self._invoke([
                "--environment", "conf-env-repeat", "--results", path, "--url", url,
            ], self.tmp)
            self.assertEqual(rc1, 0, err1)
            self.assertIn("inserted=1", err1)

            rc2, _out2, err2 = self._invoke([
                "--environment", "conf-env-repeat", "--results", path, "--url", url,
            ], self.tmp)
            self.assertEqual(rc2, 0, err2)
            self.assertIn("inserted=0", err2)
            self.assertIn("updated=1", err2)

        def test_build_stamped_and_trusted_without_streams_seen(self) -> None:
            """The agreed micro cut: no streams_seen acknowledgement
            check - a --build run against a response with no
            streams_seen key at all (the old-server signature the full
            engine refuses loudly) is trusted here, matching the
            Python micro engine's documented trust decision."""
            import json
            control = _StubControl()
            control.queue(200, {
                "inserted": 1, "updated": 0, "unchanged": 0, "rejected": 0,
                "errors": [],
            })
            httpd, port = _start_stub_server(control)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            path = self._write_results(
                "one.jsonl", [ScenarioMixin._record("test_build")],
            )
            rc, _out, err = self._invoke([
                "--environment", "conf-env-old", "--build", "rc-conf-1",
                "--results", path,
                "--url", "http://127.0.0.1:{0}".format(port),
            ], self.tmp)
            self.assertEqual(rc, 0, err)
            self.assertEqual(control.request_count(), 1)
            sent = json.loads(control.requests[0][1].decode("utf-8"))
            self.assertEqual(sent["runs"][0]["build"], "rc-conf-1")

        def test_bounded_time_promise_against_a_blackholed_port(self) -> None:
            import time
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
                "java: black-holed port took {0:.1f}s, documented ceiling "
                "is {1:.1f}s (3 attempts x http-timeout {2} + two 2s "
                "pauses, plus slack for process startup)".format(
                    elapsed, ceiling, http_timeout,
                ),
            )

        def test_usage_error_missing_required_flags_exits_2(self) -> None:
            rc, _out, err = self._invoke(["--environment", "conf-env"], self.tmp)
            self.assertEqual(rc, 2, err)
            self.assertIn("--url", err)


if __name__ == "__main__":
    unittest.main()
