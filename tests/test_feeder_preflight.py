"""Tests for the checks run before an import does any work.

These exist to convert three expensive, late failures into one cheap
early sentence: a URL that is not a URL, a dashboard that is not there
(or is not a dashboard), and a path this process cannot write. The last
is the one that bites hardest in the documented deployment, where the
feeder runs from a checkout it only has read access to and the state
file is written *after* a successful import.

Nothing here touches the network: the dashboard probe takes an injected
opener, the same hook :class:`feeder.submitter.Submitter` uses.

Python 3.6 compatible; standard library only.
"""

import errno
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

from feeder import preflight

#: What a real dashboard answers an empty import with.
_GOOD_BODY = json.dumps(
    {"inserted": 0, "updated": 0, "rejected": 0, "errors": []}
).encode("utf-8")


def fake_opener(
    status: int = 200, body: bytes = _GOOD_BODY,
    raises: Optional[Exception] = None,
    seen: Optional[List[Tuple[str, bytes]]] = None,
) -> Any:
    """Build an Opener that answers with fixed values and records calls."""
    def opener(
        url: str, data: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        if seen is not None:
            seen.append((url, data))
        if raises is not None:
            raise raises
        return status, body
    return opener


class CheckUrlTest(unittest.TestCase):
    """The shape of --url, before anything is sent to it."""

    def test_a_normal_url_passes(self) -> None:
        self.assertIsNone(preflight.check_url("http://127.0.0.1:8000"))
        self.assertIsNone(preflight.check_url("https://dash.example/tb"))

    def test_a_missing_scheme_is_named_and_corrected(self) -> None:
        """Pasting host:port out of a browser bar is the common slip."""
        problem = preflight.check_url("127.0.0.1:8000")
        self.assertIsNotNone(problem)
        self.assertIn("no scheme", problem)
        self.assertIn("http://127.0.0.1:8000", problem)

    def test_a_wrong_scheme_is_rejected(self) -> None:
        problem = preflight.check_url("ftp://dash:8000")
        self.assertIn("'ftp' scheme", problem)

    def test_a_missing_host_is_rejected(self) -> None:
        problem = preflight.check_url("http:///api/import")
        self.assertIn("names no host", problem)


class WritableTest(unittest.TestCase):
    """Proving a path can be written, rather than asking the permission bits."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_preflight_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_writable_directory_passes_and_leaves_nothing_behind(
        self
    ) -> None:
        before = os.listdir(self.tmp)
        self.assertIsNone(
            preflight.check_writable_directory(self.tmp, "replay files"))
        self.assertEqual(os.listdir(self.tmp), before)

    def test_a_missing_directory_says_so(self) -> None:
        problem = preflight.check_writable_directory(
            os.path.join(self.tmp, "absent"), "replay files")
        self.assertIn("does not exist", problem)
        self.assertIn("replay files", problem)

    def test_a_file_where_a_directory_belongs_says_so(self) -> None:
        path = os.path.join(self.tmp, "a-file")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("")
        problem = preflight.check_writable_directory(path, "replay files")
        self.assertIn("is a file, not a directory", problem)

    def test_a_writable_file_passes_before_it_exists(self) -> None:
        """The state file is created by the feeder; only its home must exist."""
        self.assertIsNone(preflight.check_writable_file(
            os.path.join(self.tmp, "state.json"), "the state file"))

    def test_a_state_file_in_a_missing_directory_is_caught(self) -> None:
        problem = preflight.check_writable_file(
            os.path.join(self.tmp, "nope", "state.json"), "the state file")
        self.assertIn("does not exist", problem)

    def test_a_directory_given_where_a_file_belongs_is_caught(self) -> None:
        problem = preflight.check_writable_file(self.tmp, "the state file")
        self.assertIn("is a directory, not a file", problem)

    def test_a_read_only_directory_names_the_deployment_it_bites(
        self
    ) -> None:
        """The message has to mention the read-only checkout by name.

        This is the actual failure the feeder hits in the documented
        layout, and "Permission denied" alone does not lead anyone to
        --state-file.
        """
        message = preflight._describe_unwritable(
            self.tmp, "the state file",
            OSError(errno.EACCES, "Permission denied"),
        )
        self.assertIn("read access", message)
        self.assertIn(os.path.abspath(self.tmp), message)

    def test_a_full_filesystem_says_full(self) -> None:
        message = preflight._describe_unwritable(
            self.tmp, "replay files", OSError(errno.ENOSPC, "No space"))
        self.assertIn("filesystem is full", message)

    @unittest.skipIf(os.name == "nt",
                     "a read-only bit does not stop writes on Windows")
    def test_an_unwritable_directory_is_actually_detected(self) -> None:
        """os.access lies about read-only mounts; the probe writes for real."""
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        problem = preflight.check_writable_directory(locked, "replay files")
        self.assertIsNotNone(problem)
        self.assertIn(locked, problem)


class ProbeDashboardTest(unittest.TestCase):
    """The empty import that proves the dashboard is there and is a dashboard."""

    def test_a_real_dashboard_passes(self) -> None:
        self.assertIsNone(
            preflight.probe_dashboard("http://d:8000", fake_opener()))

    def test_the_probe_posts_an_empty_import_to_api_import(self) -> None:
        """It must exercise the exact path a real batch uses, and change nothing."""
        seen = []  # type: List[Tuple[str, bytes]]
        preflight.probe_dashboard("http://d:8000", fake_opener(seen=seen))
        self.assertEqual(len(seen), 1)
        url, body = seen[0]
        self.assertEqual(url, "http://d:8000/api/import")
        self.assertEqual(json.loads(body.decode("utf-8")), {"runs": []})

    def test_a_url_already_naming_the_endpoint_is_not_doubled(self) -> None:
        seen = []  # type: List[Tuple[str, bytes]]
        preflight.probe_dashboard(
            "http://d:8000/api/import", fake_opener(seen=seen))
        self.assertEqual(seen[0][0], "http://d:8000/api/import")

    def test_connection_refused_says_start_the_server(self) -> None:
        problem = preflight.probe_dashboard(
            "http://d:8000",
            fake_opener(raises=urllib.error.URLError(
                ConnectionRefusedError("refused"))),
        )
        self.assertIn("connection refused", problem)
        self.assertIn("run_server.py", problem)

    def test_a_bad_hostname_says_dns(self) -> None:
        problem = preflight.probe_dashboard(
            "http://nosuchhost:8000",
            fake_opener(raises=urllib.error.URLError(
                socket.gaierror("name resolution failed"))),
        )
        self.assertIn("DNS", problem)

    def test_a_404_means_the_wrong_service_or_a_path_prefix(self) -> None:
        problem = preflight.probe_dashboard(
            "http://d:8000", fake_opener(status=404, body=b"not found"))
        self.assertIn("no /api/import endpoint", problem)

    def test_an_authenticating_proxy_is_called_out(self) -> None:
        problem = preflight.probe_dashboard(
            "http://d:8000", fake_opener(status=403, body=b""))
        self.assertIn("proxy or SSO", problem)

    def test_html_from_some_other_server_is_not_mistaken_for_success(
        self
    ) -> None:
        """Something else on port 8000 answering 200 must still fail."""
        problem = preflight.probe_dashboard(
            "http://d:8000",
            fake_opener(status=200, body=b"<html>hello</html>"),
        )
        self.assertIn("not with JSON", problem)

    def test_json_that_is_not_an_import_result_is_rejected(self) -> None:
        problem = preflight.probe_dashboard(
            "http://d:8000",
            fake_opener(status=200, body=b'{"status": "ok"}'),
        )
        self.assertIn("not a testboard import result", problem)

    def test_an_unexpected_status_is_reported_with_its_code(self) -> None:
        problem = preflight.probe_dashboard(
            "http://d:8000", fake_opener(status=503, body=b""))
        self.assertIn("503", problem)


if __name__ == "__main__":
    unittest.main()
