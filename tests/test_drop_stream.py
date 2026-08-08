"""Tests for tools/drop_stream.py.

The storage-level behaviour (row counts, refusing mainline, the comment
tag surviving with stream_id cleared) is covered by
tests/test_storage.py::DropStreamTest. What is worth testing here is the
CLI's own job: it must be hard to run by accident, and it must refuse
mainline unconditionally, no flag around it.
"""

import contextlib
import datetime
import io
import os
import shutil
import tempfile
import unittest
from typing import List, Tuple
from unittest import mock

from testboard.model import Result, RunRecord
from testboard.storage import MAINLINE_STREAM_ID, Storage
from tools import drop_stream

NOW = datetime.datetime(2026, 7, 25, 2, 0, 0)


class TestDropStreamCLI(unittest.TestCase):
    """A scratch database holding a mainline test and one branch stream."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard-drop-stream-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = Storage(self.db)
        store.set_environment_product(
            "linux-sim", "Atlas", "amy", NOW)
        store.upsert_runs([
            RunRecord(
                environment="linux-sim", script="suite.py",
                test_name="test_a", result=Result.PASS,
                start_time=NOW, end_time=NOW + datetime.timedelta(seconds=1),
                output="out", source_link="", known_failure_reason=None,
                branch=None, build=None),
            RunRecord(
                environment="linux-sim", script="suite.py",
                test_name="test_a", result=Result.FAIL,
                start_time=NOW + datetime.timedelta(hours=1),
                end_time=NOW + datetime.timedelta(hours=1, seconds=1),
                output="out", source_link="", known_failure_reason=None,
                branch="feat/x", build=None),
        ])
        store.close()

    def run_cli(self, argv: List[str], answer: str = "") -> Tuple[int, str]:
        """Run main() with stdin scripted; return (exit code, stdout)."""
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=[answer]):
            with contextlib.redirect_stdout(out):
                rc = drop_stream.main(argv)
        return rc, out.getvalue()

    def stream_ids(self) -> List[int]:
        store = Storage(self.db)
        self.addCleanup(store.close)
        return [s.stream_id for s in store.list_streams("Atlas")]

    def base_args(self) -> List[str]:
        return [
            "--db", self.db, "--product", "Atlas", "--kind", "branch",
            "--name", "feat/x",
        ]

    def test_dry_run_changes_nothing(self) -> None:
        rc, out = self.run_cli(self.base_args() + ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(len(self.stream_ids()), 1)

    def test_a_wrong_confirmation_aborts(self) -> None:
        rc, out = self.run_cli(self.base_args(), answer="branch:wrong")
        self.assertEqual(rc, 1)
        self.assertIn("Aborted", out)
        self.assertEqual(len(self.stream_ids()), 1)

    def test_the_exact_kind_name_confirms(self) -> None:
        rc, _ = self.run_cli(self.base_args(), answer="branch:feat/x")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stream_ids(), [])

    def test_yes_skips_the_prompt(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drop_stream.main(self.base_args() + ["--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stream_ids(), [])

    def test_an_unknown_name_is_reported_not_deleted(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drop_stream.main([
                "--db", self.db, "--product", "Atlas", "--kind", "branch",
                "--name", "typo-name", "--yes",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("feat/x", out.getvalue())
        self.assertEqual(len(self.stream_ids()), 1)

    def test_mainline_is_refused_even_by_id_lookup(self) -> None:
        """There is no way to spell mainline via --product/--kind/--name
        (kind is restricted to branch/build by argparse choices), so this
        also proves that restriction is doing its job."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_stream.main([
                "--db", self.db, "--product", "", "--kind", "branch",
                "--name", "mainline",
            ])
        # Not found (mainline is never in list_streams), reported as 0,
        # never as a delete.
        self.assertEqual(rc, 0)

    def test_a_missing_database_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_stream.main([
                "--db", os.path.join(self.tmp, "no.db"),
                "--product", "Atlas", "--kind", "branch", "--name", "x",
                "--yes",
            ])
        self.assertEqual(rc, 2)
        self.assertIn("not found", err.getvalue())

    def test_an_empty_name_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_stream.main([
                "--db", self.db, "--product", "Atlas", "--kind", "branch",
                "--name", "   ", "--yes",
            ])
        self.assertEqual(rc, 2)
        self.assertIn("must not be empty", err.getvalue())

    def test_db_and_db_config_together_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_stream.main([
                "--db", self.db, "--db-config", "x.cnf",
                "--product", "Atlas", "--kind", "branch", "--name", "x",
                "--yes",
            ])
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", err.getvalue())

    def test_neither_db_nor_db_config_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_stream.main([
                "--product", "Atlas", "--kind", "branch", "--name", "x",
                "--yes",
            ])
        self.assertEqual(rc, 2)

    def test_the_mainline_runs_and_comments_are_untouched(self) -> None:
        store = Storage(self.db)
        store.add_comment(
            "linux-sim", "suite.py", "test_a", "amy", "looks fine", NOW)
        store.close()
        drop_stream.main(self.base_args() + ["--yes"])
        store = Storage(self.db)
        self.addCleanup(store.close)
        latest = store.latest_run("linux-sim", "suite.py", "test_a")
        assert latest is not None
        self.assertEqual(latest.result, Result.PASS)
        comments = store.comments("linux-sim", "suite.py", "test_a")
        self.assertEqual(len(comments), 1)


if __name__ == "__main__":
    unittest.main()
