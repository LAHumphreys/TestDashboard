"""The vendored MySQL driver must be usable, and must cost nothing.

testboard vendors PyMySQL rather than installing it, so that a MariaDB
backend needs nothing set up on the deployment host (see
``third_party/README.md``). Vendoring moves two responsibilities onto
this project that pip would otherwise carry:

1. **The version is pinned by copying, so nothing warns us if the copy
   is wrong.** 1.0.2 is the last PyMySQL supporting Python 3.6; 1.1.0
   raised its floor to 3.7. An update that ignores that produces a suite
   that is green on every dev machine and an ImportError on the server.
2. **Importing it must be free.** It is not yet used by anything; the
   day it is, it will be imported by a web server at startup. A package
   that opened a socket or started a thread merely on import would do so
   in every process that touches storage.

The 3.6 *syntax* gate lives in ``test_python36_compat.py``
(``VendoredCodeTest``). This file covers the interface and the cost.

Python 3.6 compatible; standard library only.
"""

import io
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(REPO_ROOT, "third_party", "pymysql")

#: The version recorded in third_party/README.md. Both must move together.
PINNED_VERSION = (1, 0, 2)


class PresenceTest(unittest.TestCase):
    """The files are actually on disk, licence included."""

    def test_the_package_directory_exists(self) -> None:
        self.assertTrue(
            os.path.isdir(VENDOR_DIR),
            "third_party/pymysql is missing; see third_party/README.md")

    def test_the_licence_ships_with_the_code(self) -> None:
        """Vendoring redistributes it, so the licence has to travel."""
        path = os.path.join(VENDOR_DIR, "LICENSE")
        self.assertTrue(os.path.isfile(path), "vendored LICENSE is missing")
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", text)

    def test_the_readme_records_the_pinned_version(self) -> None:
        """Provenance that disagrees with the code is worse than none."""
        path = os.path.join(REPO_ROOT, "third_party", "README.md")
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
        version = ".".join(str(part) for part in PINNED_VERSION)
        self.assertIn(version, text)


class InterfaceTest(unittest.TestCase):
    """The DB-API surface the storage layer will actually use."""

    def setUp(self) -> None:
        from third_party import pymysql
        self.pymysql = pymysql

    def test_the_version_is_the_one_that_still_supports_36(self) -> None:
        self.assertEqual(
            tuple(self.pymysql.VERSION[:3]), PINNED_VERSION,
            "vendored PyMySQL is not the pinned version. 1.0.2 is the last "
            "release supporting Python 3.6 — a newer one imports fine here "
            "and fails on the RHEL 8 server")

    def test_it_exposes_the_dbapi_entry_points(self) -> None:
        for name in ("connect", "Connect"):
            self.assertTrue(
                callable(getattr(self.pymysql, name, None)), name)

    def test_the_exception_hierarchy_is_present(self) -> None:
        """Storage catches these; they must exist before it can."""
        for name in ("Error", "InterfaceError", "DatabaseError",
                     "IntegrityError", "OperationalError",
                     "ProgrammingError"):
            exc = getattr(self.pymysql, name, None)
            self.assertTrue(
                isinstance(exc, type) and issubclass(exc, Exception),
                "missing exception class " + name)
        self.assertTrue(
            issubclass(self.pymysql.IntegrityError, self.pymysql.Error))

    def test_the_placeholder_style_is_recorded(self) -> None:
        """The single most consequential difference from sqlite3.

        sqlite3 is ``qmark``: ``WHERE id = ?``. PyMySQL is ``pyformat``,
        which accepts ``%(name)s`` and — the form the port will use —
        positional ``%s``. Every parameterised statement in storage.py
        has to change, and a literal ``?`` reaches MariaDB as a literal
        question mark rather than failing loudly, so this is pinned here
        to be certain what the target style is before anything is
        rewritten against an assumption.
        """
        self.assertEqual(self.pymysql.paramstyle, "pyformat")

    def test_the_api_level_is_two_point_oh(self) -> None:
        self.assertEqual(self.pymysql.apilevel, "2.0")


class ImportCostTest(unittest.TestCase):
    """Importing it must not do anything.

    Checked in a subprocess because the module is already imported by
    the time an in-process assertion could run — and an import that has
    already happened cannot be observed.
    """

    def _run(self, body: str) -> str:
        proc = subprocess.Popen(
            [sys.executable, "-c", body],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out, _ = proc.communicate(timeout=60)
        text = out.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, text)
        return text

    def test_importing_touches_the_network_in_no_way(self) -> None:
        """Uses an audit hook, not a monkeypatch.

        Replacing ``socket.socket`` looks like the obvious way to do
        this and does not work: ``ssl.SSLSocket`` subclasses it at
        import, so the patch breaks the standard library before the code
        under test is ever reached — the test then fails for a reason
        that has nothing to do with the driver. Audit hooks observe the
        real calls without substituting anything, and cannot be bypassed
        by the module being watched.
        """
        if sys.version_info < (3, 8):
            self.skipTest("sys.addaudithook needs Python 3.8+")
        text = self._run(
            "import sys\n"
            "def hook(event, args):\n"
            "    if event.startswith('socket.'):\n"
            "        raise AssertionError('network at import: ' + event)\n"
            "sys.addaudithook(hook)\n"
            "from third_party import pymysql\n"
            "print('ok', pymysql.__version__)\n"
        )
        self.assertIn("ok", text)

    def test_the_network_probe_can_actually_fail(self) -> None:
        """A hook that never fires would pass the test above forever."""
        if sys.version_info < (3, 8):
            self.skipTest("sys.addaudithook needs Python 3.8+")
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys\n"
             "def hook(event, args):\n"
             "    if event.startswith('socket.'):\n"
             "        raise AssertionError('network: ' + event)\n"
             "sys.addaudithook(hook)\n"
             "import socket\n"
             "socket.getaddrinfo('localhost', 80)\n"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out, _ = proc.communicate(timeout=60)
        self.assertNotEqual(
            proc.returncode, 0,
            "the audit hook did not fire on a real network call, so the "
            "test above proves nothing")
        self.assertIn(b"network", out)

    def test_importing_starts_no_thread(self) -> None:
        text = self._run(
            "import threading\n"
            "before = threading.active_count()\n"
            "from third_party import pymysql\n"
            "after = threading.active_count()\n"
            "assert after == before, 'started %d thread(s) at import' % ("
            "after - before)\n"
            "print('ok')\n"
        )
        self.assertIn("ok", text)

    def test_importing_needs_no_third_party_package(self) -> None:
        """`cryptography` is optional and deliberately not vendored.

        It is a compiled package, so vendoring it would give up the
        "nothing to build on the server" property that is the whole
        reason for this directory. PyMySQL only needs it for sha256
        auth plugins; MariaDB's default does not use them. The import
        must therefore survive its absence — which is what makes the
        omission safe.
        """
        text = self._run(
            "import sys\n"
            "sys.modules['cryptography'] = None\n"
            "from third_party import pymysql\n"
            "from third_party.pymysql import _auth\n"
            "print('ok', _auth._have_cryptography)\n"
        )
        self.assertIn("ok", text)


class NotYetWiredTest(unittest.TestCase):
    """The driver lands on its own, wired to nothing.

    If it turns out to be the wrong choice, reverting must be one commit
    that touches no storage code. That is only true while nothing
    imports it.
    """

    def test_no_shipped_module_imports_the_driver_yet(self) -> None:
        import re

        offenders = []
        for package in ("testboard", "feeder", "tools"):
            directory = os.path.join(REPO_ROOT, package)
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with io.open(path, encoding="utf-8") as handle:
                    source = handle.read()
                if re.search(r"\bpymysql\b", source):
                    offenders.append(package + "/" + name)
        self.assertEqual(
            offenders, [],
            "the vendored driver is wired in, but the MariaDB backend is "
            "not built yet. When it is, delete this test and say so in the "
            "commit message")


if __name__ == "__main__":
    unittest.main()
