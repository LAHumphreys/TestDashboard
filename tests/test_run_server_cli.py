"""The server CLI's backend selection, pinned at the flag level.

Every test here exercises a path that returns before a socket is bound
or a database is opened, so the suite stays fast and no server ever
starts. The principle under test: a flag combination that cannot mean
what the operator intended is REJECTED with the reason, never silently
reinterpreted — a --cache-mb quietly ignored under --db-config would
look applied, and that is how misconfiguration hides.

Python 3.6 compatible; standard library only.
"""

import datetime
import io
import os
import shutil
import sys
import tempfile
import unittest
from typing import List, Optional, Tuple

import run_server


def parse(argv: List[str]) -> "run_server.argparse.Namespace":
    return run_server.build_parser().parse_args(argv)


class BackendErrorTest(unittest.TestCase):
    """backend_error() is the whole flag-combination policy."""

    def test_sqlite_mode_needs_no_extras(self) -> None:
        self.assertIsNone(run_server.backend_error(parse([])))
        self.assertIsNone(run_server.backend_error(
            parse(["--db", "x.db", "--cache-mb", "64"])))

    def test_mariadb_mode_with_site_notes_is_accepted(self) -> None:
        self.assertIsNone(run_server.backend_error(parse(
            ["--db-config", "db.cnf", "--site-notes", "notes.json"])))

    def test_both_backends_at_once_is_refused(self) -> None:
        problem = run_server.backend_error(parse(
            ["--db", "x.db", "--db-config", "db.cnf",
             "--site-notes", "n.json"]))
        self.assertIsNotNone(problem)
        self.assertIn("exactly one", problem)

    def test_mariadb_without_site_notes_is_refused_with_the_reason(
            self) -> None:
        """The SQLite default is 'beside the --db file'; MariaDB has no
        file to be beside, and guessing a path would look like an empty
        notes file rather than a misconfiguration."""
        problem = run_server.backend_error(parse(
            ["--db-config", "db.cnf"]))
        self.assertIsNotNone(problem)
        self.assertIn("--site-notes", problem)

    def test_sqlite_tuning_flags_are_refused_not_ignored(self) -> None:
        for flag in (["--cache-mb", "64"], ["--mmap-mb", "256"]):
            problem = run_server.backend_error(parse(
                ["--db-config", "db.cnf", "--site-notes", "n.json"]
                + flag))
            self.assertIsNotNone(problem, flag[0])
            self.assertIn("SQLite", problem)

    def test_workers_is_honoured_in_both_modes(self) -> None:
        """--workers maps to the connection count on either backend, so
        it must not be rejected."""
        self.assertIsNone(run_server.backend_error(parse(
            ["--db-config", "db.cnf", "--site-notes", "n.json",
             "--workers", "4"])))

    def test_every_rejection_is_ascii(self) -> None:
        """These strings go to stderr possibly under LANG=C, where
        Python 3.6 cannot encode a section sign or an em-dash."""
        rejections = [
            run_server.backend_error(parse(
                ["--db", "x", "--db-config", "c", "--site-notes", "n"])),
            run_server.backend_error(parse(["--db-config", "c"])),
            run_server.backend_error(parse(
                ["--db-config", "c", "--site-notes", "n",
                 "--cache-mb", "1"])),
        ]
        for problem in rejections:
            self.assertIsNotNone(problem)
            problem.encode("ascii")   # raises if not

    def test_the_db_default_moved_into_main_not_the_parser(self) -> None:
        """The parser must leave --db as None so main() can tell 'the
        default' from 'explicitly testboard.db' when --db-config is
        also given."""
        self.assertIsNone(parse([]).db)


class MainRejectionTest(unittest.TestCase):
    """main() turns the policy into exit code 2 on stderr."""

    def run_main(self, argv: List[str]) -> Tuple[int, str]:
        captured = io.StringIO()
        original = sys.stderr
        sys.stderr = captured
        try:
            code = run_server.main(argv)
        finally:
            sys.stderr = original
        return code, captured.getvalue()

    def test_conflicting_flags_exit_2_with_the_reason(self) -> None:
        code, err = self.run_main(
            ["--db", "x.db", "--db-config", "db.cnf",
             "--site-notes", "n.json"])
        self.assertEqual(code, 2)
        self.assertIn("exactly one", err)

    def test_a_missing_config_file_exits_2_naming_the_runbook(
            self) -> None:
        """Exercises the DbConfigError branch end-to-end: parse passes,
        the backend check passes, reading the file fails helpfully."""
        tmp = tempfile.mkdtemp(prefix="testboard_cli_")
        self.addCleanup(shutil.rmtree, tmp, True)
        code, err = self.run_main(
            ["--db-config", os.path.join(tmp, "absent.cnf"),
             "--site-notes", os.path.join(tmp, "notes.json")])
        self.assertEqual(code, 2)
        self.assertIn("Cannot read --db-config", err)
        self.assertIn("A.9", err)


class StartupSweepTest(unittest.TestCase):
    """WP-39: the server removes empty builds before it serves.

    The one test in the suite that runs ``run_server.py`` as the
    operator does — a real process, its stdout read line by line —
    because the sweep lives in ``main()`` between opening the database
    and building the server, and nothing else exercises that stretch.
    """

    def _boot(self, db: str) -> List[str]:
        """Start the server on an ephemeral port and return its
        start-up lines up to the "serving at" line, then stop it."""
        import socket
        import subprocess
        import threading
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        process = subprocess.Popen(
            [sys.executable, os.path.join(root, "run_server.py"),
             "--db", db, "--port", str(port), "--host", "127.0.0.1",
             "--workers", "1"],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
        lines = []  # type: List[str]
        done = threading.Event()

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line.rstrip("\n"))
                if "serving at" in line:
                    break
            done.set()

        reader = threading.Thread(target=pump)
        reader.daemon = True
        reader.start()
        try:
            done.wait(30)
        finally:
            process.kill()
            process.wait()
        return lines

    def test_an_empty_build_is_removed_and_named(self) -> None:
        from testboard import model
        from testboard.model import Result, RunRecord
        from testboard.storage import Storage
        tmp = tempfile.mkdtemp(prefix="testboard_sweep_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "t.db")
        store = Storage(db)
        when = datetime.datetime(2026, 7, 1, 2, 0, 0)
        store.upsert_runs([RunRecord(
            environment="linux-sim", script="suite.py",
            test_name="test_a", result=Result.PASS, start_time=when,
            end_time=when + datetime.timedelta(seconds=1), output="",
            source_link="", known_failure_reason=None, build="live")])
        conn = store._conn()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO streams (product, kind, name, first_seen, "
            "last_seen) VALUES ('', 'build', 'orphan', ?, ?)",
            (model.format_iso(when), model.format_iso(when)))
        conn.execute("COMMIT")
        store.close()

        lines = self._boot(db)
        self.assertIn("empty builds: removed 1 (build:orphan)", lines,
                      "\n".join(lines))
        self.assertTrue(
            any("serving at" in line for line in lines),
            "the server did not come up:\n" + "\n".join(lines))
        store = Storage(db)
        self.addCleanup(store.close)
        self.assertEqual(
            [stream.name for stream in store.list_streams("")], ["live"])

    def test_a_clean_database_says_so(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_sweep_")
        self.addCleanup(shutil.rmtree, tmp, True)
        lines = self._boot(os.path.join(tmp, "t.db"))
        self.assertIn("empty builds: none", lines, "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
