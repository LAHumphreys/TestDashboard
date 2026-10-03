"""The sanity net's own safety properties (tools/dev/net/run_net.py).

The net itself needs a seeded server and optionally node, so it is not
run here; what is pinned is that it can never open a checkout's
repo-root testboard.db, and that it never trusts a fixed port.
"""
import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "dev", "net"))

import run_net  # noqa: E402
import server_util  # noqa: E402


class RepoRootDatabaseGuardTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="net-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A fake checkout root: what is_checkout recognises.
        open(os.path.join(self.tmp, "run_server.py"), "w").close()
        os.makedirs(os.path.join(self.tmp, "testboard"))
        self.root_db = os.path.join(self.tmp, "testboard.db")
        open(self.root_db, "wb").close()

    def test_refuses_a_checkout_root_testboard_db(self) -> None:
        with self.assertRaises(ValueError) as caught:
            run_net.check_db(self.root_db)
        self.assertIn("refusing the repo-root testboard.db", str(caught.exception))

    def test_main_refuses_before_booting_anything(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_net.main(["--db", self.root_db])
        self.assertEqual(code, 2)
        self.assertIn("refusing the repo-root testboard.db", out.getvalue())

    def test_a_copy_elsewhere_is_accepted(self) -> None:
        nested = os.path.join(self.tmp, "scratch")
        os.makedirs(nested)
        copy = os.path.join(nested, "testboard.db")
        open(copy, "wb").close()
        run_net.check_db(copy)  # not a checkout root: allowed

    def test_a_missing_database_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_net.check_db(os.path.join(self.tmp, "absent.db"))


class FreePortTest(unittest.TestCase):

    def test_the_port_is_free_and_not_the_fixed_default(self) -> None:
        port = server_util.free_port()
        self.assertNotEqual(port, server_util.PORT)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((server_util.HOST, port))  # raises if taken
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
