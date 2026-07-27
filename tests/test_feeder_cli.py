"""Tests for the ``run_feeder.py`` CLI.

Covers the pure :func:`run_feeder.compute_since` matrix, the Python-version
guard, argument/reader failure exit codes (2), success/partial-failure exit
codes (0/1) with an injected fake Submitter (no network ever), dry-run
behaviour, and high-water-mark persistence rules in both modes.
"""

import contextlib
import datetime
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type
from unittest import mock

import run_feeder
import feeder.state
import feeder.submitter
from feeder.submitter import SubmitStats
from testboard import model

HWM = datetime.datetime(2026, 7, 20, 2, 0, 0)


def make_stats(read: int = 5, valid: int = 5, skipped: int = 0,
               sent: int = 5, inserted: int = 5, updated: int = 0,
               rejected: int = 0, failed_batches: int = 0,
               replay_files: Optional[List[str]] = None) -> SubmitStats:
    """Build a SubmitStats with success-shaped defaults."""
    return SubmitStats(
        read=read, valid=valid, skipped=skipped, sent=sent,
        inserted=inserted, updated=updated, rejected=rejected,
        failed_batches=failed_batches,
        replay_files=replay_files if replay_files is not None else [],
    )


def make_fake_submitter(
    stats: SubmitStats,
    hwm: Optional[datetime.datetime],
) -> Tuple[Type[Any], List[Any]]:
    """Build a fake Submitter class returning canned results.

    Returns the class (to patch in place of feeder.submitter.Submitter) and
    a list that will collect every instance constructed, for inspection.
    """
    created = []  # type: List[Any]

    class FakeSubmitter:
        """Test double recording constructor and submit() arguments."""

        def __init__(self, url: str, batch_size: int = 500,
                     replay_dir: str = ".", **options: Any) -> None:
            """Record construction arguments."""
            self.url = url
            self.batch_size = batch_size
            self.replay_dir = replay_dir
            self.options = options
            self.submit_calls = []  # type: List[Dict[str, Any]]
            created.append(self)

        def submit(self, records: Iterable[Dict[str, Any]],
                   dry_run: bool = False,
                   since: Optional[datetime.datetime] = None) -> SubmitStats:
            """Consume the record stream and return the canned stats."""
            self.submit_calls.append({
                "records": list(records),
                "dry_run": dry_run,
                "since": since,
            })
            return stats

        def max_accepted_start_time(self) -> Optional[datetime.datetime]:
            """Return the canned high-water mark."""
            return hwm

    return FakeSubmitter, created


class ComputeSinceTest(unittest.TestCase):
    """The compute_since matrix: backfill +/- since, daily +/- hwm, overlap."""

    def test_backfill_with_since_arg(self) -> None:
        """Backfill uses --since verbatim."""
        since = datetime.datetime(2026, 7, 1)
        self.assertEqual(
            run_feeder.compute_since("backfill", None, since, 1), since
        )

    def test_backfill_without_since_arg_imports_everything(self) -> None:
        """Backfill without --since has no lower bound."""
        self.assertIsNone(run_feeder.compute_since("backfill", None, None, 1))

    def test_backfill_ignores_hwm(self) -> None:
        """Backfill never consults the high-water mark."""
        since = datetime.datetime(2026, 7, 1)
        self.assertEqual(
            run_feeder.compute_since("backfill", HWM, since, 1), since
        )
        self.assertIsNone(run_feeder.compute_since("backfill", HWM, None, 1))

    def test_daily_without_hwm_imports_everything(self) -> None:
        """First daily run (no saved mark) has no lower bound."""
        self.assertIsNone(run_feeder.compute_since("daily", None, None, 1))

    def test_daily_ignores_since_arg(self) -> None:
        """Daily mode ignores --since even when given."""
        since = datetime.datetime(2026, 1, 1)
        self.assertIsNone(run_feeder.compute_since("daily", None, since, 1))
        self.assertEqual(
            run_feeder.compute_since("daily", HWM, since, 1),
            HWM - datetime.timedelta(days=1),
        )

    def test_daily_subtracts_overlap_days(self) -> None:
        """Daily mode rewinds the mark by --overlap-days."""
        for overlap in (0, 1, 3):
            self.assertEqual(
                run_feeder.compute_since("daily", HWM, None, overlap),
                HWM - datetime.timedelta(days=overlap),
            )


class CliTestBase(unittest.TestCase):
    """Shared fixtures: temp dir, quiet + restored logging, jsonl helper."""

    def setUp(self) -> None:
        """Isolate root logging state and create a temp working dir."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_file = os.path.join(self.tmp, "feeder_state.json")
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level

        def restore() -> None:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    @contextlib.contextmanager
    def capture_logs(self, level: str = "INFO") -> Any:
        """assertLogs on run_feeder, with setUp's global mute lifted.

        setUp silences logging so the suite is quiet; a test that is
        *about* what the feeder tells someone has to hear it again.
        """
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("run_feeder", level=level) as caught:
                yield caught
        finally:
            logging.disable(logging.CRITICAL)

    def write_jsonl(self, records: List[Dict[str, Any]]) -> str:
        """Write records to a JSON-lines file in the temp dir."""
        path = os.path.join(self.tmp, "runs.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def base_args(self, mode: str = "backfill") -> List[str]:
        """Common CLI arguments pointing all file outputs at the temp dir.

        ``--skip-preflight`` because these tests are about what main()
        does with a Submitter, and preflight would try to reach a
        dashboard that deliberately is not there. The preflight itself is
        covered by :class:`PreflightTest`.
        """
        return [
            "--url", "http://127.0.0.1:9",
            "--mode", mode,
            "--state-file", self.state_file,
            "--replay-dir", self.tmp,
            "--skip-preflight",
        ]

    def run_main_with_fake(
        self,
        argv: List[str],
        stats: SubmitStats,
        hwm: Optional[datetime.datetime] = None,
    ) -> Tuple[int, List[Any]]:
        """Run main() with a fake Submitter; return (exit code, instances)."""
        fake_cls, created = make_fake_submitter(stats, hwm)
        with mock.patch("feeder.submitter.Submitter", fake_cls):
            code = run_feeder.main(argv)
        return code, created


class VersionGuardTest(CliTestBase):
    """The Python-2 guard prints the remedy and exits 2."""

    def test_old_python_exits_2_with_remedy(self) -> None:
        """Simulated 2.7.18 gets the exact upgrade message, exit 2."""
        stderr = io.StringIO()
        with mock.patch.object(sys, "version_info", (2, 7, 18)):
            with contextlib.redirect_stderr(stderr):
                code = run_feeder.main([])
        self.assertEqual(code, 2)
        message = stderr.getvalue()
        self.assertIn("testboard requires Python 3.6+", message)
        self.assertIn("2.7.18", message)
        self.assertIn("python3 run_feeder.py", message)


class FatalArgumentTest(CliTestBase):
    """Bad arguments and reader load failures exit 2."""

    def test_missing_required_args_exit_2(self) -> None:
        """argparse errors (no --url/--mode) surface as exit code 2."""
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_feeder.main([]), 2)

    def test_bad_mode_exit_2(self) -> None:
        """An unknown --mode value is an argparse error, exit 2."""
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_feeder.main(
                ["--url", "http://x", "--mode", "sometimes"]
            )
        self.assertEqual(code, 2)

    def test_bad_since_exit_2(self) -> None:
        """An unparseable --since exits 2 before any submission."""
        code = run_feeder.main(
            self.base_args() + ["--since", "yesterday"]
        )
        self.assertEqual(code, 2)

    def test_reader_load_failure_exit_2(self) -> None:
        """An unloadable --reader spec exits 2 before any submission."""
        code = run_feeder.main(
            self.base_args() + ["--reader", "no_such_module_xyz:factory"]
        )
        self.assertEqual(code, 2)


class ExitCodeTest(CliTestBase):
    """0 = everything accepted; 1 = rejects or failed batches."""

    def test_success_exits_0_and_saves_hwm(self) -> None:
        """Clean run: exit 0 and the high-water mark is persisted."""
        code, created = self.run_main_with_fake(
            self.base_args(), make_stats(), hwm=HWM
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(created), 1)
        self.assertEqual(feeder.state.load_high_water_mark(self.state_file), HWM)

    def test_rejected_records_exit_1_but_hwm_still_saved(self) -> None:
        """Rejects mean exit 1, but with no failed batches the mark advances."""
        code, _ = self.run_main_with_fake(
            self.base_args(), make_stats(rejected=2), hwm=HWM
        )
        self.assertEqual(code, 1)
        self.assertEqual(feeder.state.load_high_water_mark(self.state_file), HWM)

    def test_failed_batches_exit_1_and_no_hwm_saved(self) -> None:
        """A failed batch means exit 1 and the mark must NOT move."""
        code, _ = self.run_main_with_fake(
            self.base_args(),
            make_stats(failed_batches=1, replay_files=["x.json"]),
            hwm=HWM,
        )
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state_file))

    def test_a_few_skipped_records_still_exit_0(self) -> None:
        """A handful of bad records must not fail a nightly import."""
        code, _ = self.run_main_with_fake(
            self.base_args(),
            make_stats(read=1000, valid=997, skipped=3),
            hwm=HWM,
        )
        self.assertEqual(code, 0)

    def test_mostly_skipped_records_fail_the_run(self) -> None:
        """A large fraction of invalid records is a broken reader.

        The summary says so either way, but a scheduled task only
        reports its exit code — so this case has to be a failure.
        """
        code, _ = self.run_main_with_fake(
            self.base_args(),
            make_stats(read=1000, valid=800, skipped=200),
            hwm=HWM,
        )
        self.assertEqual(code, 1)

    def test_reading_nothing_is_a_failure(self) -> None:
        """An import that read no records must not look like success.

        A scheduled daily import whose --source stopped matching, or
        whose reader broke, would otherwise report "Last Result 0" while
        the dashboard silently went stale — the one failure mode nobody
        notices until the data is days old.
        """
        code, _ = self.run_main_with_fake(
            self.base_args(),
            make_stats(read=0, valid=0, sent=0, inserted=0),
            hwm=None,
        )
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state_file))

    def test_reading_nothing_explains_the_likely_causes(self) -> None:
        # setUp silences logging globally (logging.disable suppresses
        # record creation, so assertLogs would capture nothing).
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("run_feeder", level="ERROR") as captured:
            self.run_main_with_fake(
                self.base_args(),
                make_stats(read=0, valid=0, sent=0, inserted=0),
                hwm=None,
            )
        message = "\n".join(captured.output)
        self.assertIn("no records were read", message)
        self.assertIn("--source", message)
        self.assertIn("--allow-empty", message)

    def test_allow_empty_opts_back_in_to_exit_0(self) -> None:
        """The legitimate empty import stays expressible."""
        code, _ = self.run_main_with_fake(
            self.base_args() + ["--allow-empty"],
            make_stats(read=0, valid=0, sent=0, inserted=0),
            hwm=None,
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.state_file))


class DryRunTest(CliTestBase):
    """--dry-run validates but never sends and never saves state."""

    def test_dry_run_flag_is_passed_and_state_not_saved(self) -> None:
        """dry_run reaches submit(); the mark is not persisted."""
        code, created = self.run_main_with_fake(
            self.base_args() + ["--dry-run"], make_stats(sent=0), hwm=HWM
        )
        self.assertEqual(code, 0)
        self.assertTrue(created[0].submit_calls[0]["dry_run"])
        self.assertFalse(os.path.exists(self.state_file))

    def test_dry_run_end_to_end_with_real_submitter(self) -> None:
        """A real Submitter in dry-run mode touches no network: exit 0."""
        start = "2026-07-25T02:00:00.000000"
        end = "2026-07-25T02:00:05.000000"
        path = self.write_jsonl([{
            "environment": "e", "script": "s", "test_name": "t",
            "result": "PASS", "start_time": start, "end_time": end,
            "output": "",
        }])
        code = run_feeder.main(
            self.base_args() + ["--source", path, "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.state_file))


class WiringTest(CliTestBase):
    """Arguments flow into the Submitter and the since computation."""

    def test_submitter_receives_url_batch_size_replay_dir(self) -> None:
        """--url/--batch-size/--replay-dir are forwarded to the Submitter."""
        code, created = self.run_main_with_fake(
            self.base_args() + ["--batch-size", "7"], make_stats(), hwm=None
        )
        self.assertEqual(code, 0)
        fake = created[0]
        self.assertEqual(fake.url, "http://127.0.0.1:9")
        self.assertEqual(fake.batch_size, 7)
        self.assertEqual(fake.replay_dir, self.tmp)

    def test_backfill_since_is_forwarded(self) -> None:
        """--since is parsed and passed to submit() in backfill mode."""
        code, created = self.run_main_with_fake(
            self.base_args() + ["--since", "2026-07-01T00:00:00"],
            make_stats(), hwm=None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            created[0].submit_calls[0]["since"],
            datetime.datetime(2026, 7, 1),
        )

    def test_backfill_without_since_sends_none(self) -> None:
        """No --since means no lower bound is applied."""
        code, created = self.run_main_with_fake(
            self.base_args(), make_stats(), hwm=None
        )
        self.assertEqual(code, 0)
        self.assertIsNone(created[0].submit_calls[0]["since"])

    def test_daily_uses_saved_hwm_minus_overlap(self) -> None:
        """Daily mode loads the state file and rewinds by --overlap-days."""
        feeder.state.save_high_water_mark(self.state_file, HWM)
        code, created = self.run_main_with_fake(
            self.base_args(mode="daily") + ["--overlap-days", "2"],
            make_stats(), hwm=None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            created[0].submit_calls[0]["since"],
            HWM - datetime.timedelta(days=2),
        )

    def test_daily_first_run_has_no_lower_bound(self) -> None:
        """Daily mode with no state file imports everything."""
        code, created = self.run_main_with_fake(
            self.base_args(mode="daily"), make_stats(), hwm=None
        )
        self.assertEqual(code, 0)
        self.assertIsNone(created[0].submit_calls[0]["since"])

    def test_daily_success_advances_hwm(self) -> None:
        """A clean daily run saves the new max accepted start time."""
        feeder.state.save_high_water_mark(self.state_file, HWM)
        newer = HWM + datetime.timedelta(days=1)
        code, _ = self.run_main_with_fake(
            self.base_args(mode="daily"), make_stats(), hwm=newer
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            feeder.state.load_high_water_mark(self.state_file), newer
        )

    def test_reader_records_flow_to_submitter(self) -> None:
        """Records read from --source jsonl files reach submit()."""
        record = {
            "environment": "e", "script": "s", "test_name": "t",
            "result": "PASS",
            "start_time": "2026-07-25T02:00:00.000000",
            "end_time": "2026-07-25T02:00:05.000000",
            "output": "",
        }
        path = self.write_jsonl([record])
        code, created = self.run_main_with_fake(
            self.base_args() + ["--source", path], make_stats(), hwm=None
        )
        self.assertEqual(code, 0)
        self.assertEqual(created[0].submit_calls[0]["records"], [record])


class FirstRunFeedbackTest(CliTestBase):
    """What someone who does not yet know the tool is told.

    Running it wrong is the most likely first interaction, and argparse's
    own answer to it — "the following arguments are required" — names the
    flags without saying what they are for or how to find out.
    """

    def stderr_of(self, argv: List[str]) -> str:
        """Run main() with argv and return what it wrote to stderr."""
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            run_feeder.main(argv)
        return stream.getvalue()

    def test_no_arguments_shows_usage_and_a_command_to_copy(self) -> None:
        message = self.stderr_of([])
        self.assertIn("usage:", message)
        self.assertIn("--url http://dashboard-host:8000 --mode daily",
                      message)

    def test_no_arguments_points_at_the_wizard_and_the_help(self) -> None:
        message = self.stderr_of([])
        self.assertIn("--init", message)
        self.assertIn("--help", message)

    def test_no_arguments_exits_2(self) -> None:
        self.assertEqual(run_feeder.main([]), 2)

    def test_a_missing_url_explains_what_a_url_is_here(self) -> None:
        """'--url is required' does not help someone who has two servers."""
        with self.capture_logs("ERROR") as captured:
            code = run_feeder.main(["--reader", "jsonl", "--source", "x"])
        self.assertEqual(code, 2)
        message = "\n".join(captured.output)
        self.assertIn("--url and --mode are required", message)
        self.assertIn("running the dashboard", message)
        self.assertIn("--init", message)

    def test_the_help_carries_the_timezone_rule(self) -> None:
        """The one mistake that produces no error message at all."""
        epilog = run_feeder.EPILOG
        self.assertIn("timestamps are UTC", epilog)
        self.assertIn("local time", epilog)
        self.assertIn("--check-reader", epilog)

    def test_the_help_carries_complete_commands(self) -> None:
        self.assertIn("--mode backfill", run_feeder.EPILOG)
        self.assertIn("--config", run_feeder.EPILOG)
        self.assertIn(":create_reader", run_feeder.EPILOG)

    def test_the_help_explains_the_exit_codes(self) -> None:
        """A scheduled task reports only this number."""
        for code in ("0", "1", "2"):
            self.assertIn("  " + code + "  ", run_feeder.EPILOG)

    def test_version_is_reported(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = run_feeder.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("testboard feeder", stream.getvalue())


class PreflightTest(CliTestBase):
    """The checks that run before a single record is read.

    Each of these would otherwise be discovered only after the reader had
    been run over the whole estate — and, for the state file, only after
    a successful import.
    """

    def run_with_probe(
        self, argv: List[str], problem: Optional[str] = None
    ) -> Tuple[int, str]:
        """Run main() with the dashboard probe stubbed; return (code, log)."""
        stats = make_stats(read=1, valid=1, sent=1, inserted=1)
        fake_cls, _ = make_fake_submitter(stats, None)
        with mock.patch("feeder.preflight.probe_dashboard",
                        return_value=problem):
            with mock.patch("feeder.submitter.Submitter", fake_cls):
                with self.capture_logs() as caught:
                    code = run_feeder.main(argv)
        return code, "\n".join(caught.output)

    def live_args(self, mode: str = "backfill") -> List[str]:
        """base_args without the --skip-preflight that suppresses it."""
        return [arg for arg in self.base_args(mode)
                if arg != "--skip-preflight"]

    def test_an_unreachable_dashboard_stops_before_reading(self) -> None:
        source = self.write_jsonl([{"environment": "e"}])
        code, log = self.run_with_probe(
            self.live_args() + ["--source", source],
            problem="Cannot reach the dashboard at http://x (refused).")
        self.assertEqual(code, 2)
        self.assertIn("Cannot reach the dashboard", log)
        self.assertIn("stopping before reading anything", log)

    def test_a_malformed_url_never_reaches_the_network(self) -> None:
        code, log = self.run_with_probe(
            ["--url", "dashboard:8000", "--mode", "backfill",
             "--replay-dir", self.tmp, "--source", "x"])
        self.assertEqual(code, 2)
        self.assertIn("has no scheme", log)

    def test_an_unwritable_state_file_is_caught_before_the_import(
        self
    ) -> None:
        """The failure that otherwise appears only after success."""
        code, log = self.run_with_probe(
            ["--url", "http://127.0.0.1:9", "--mode", "daily",
             "--replay-dir", self.tmp, "--source", "x",
             "--state-file", os.path.join(self.tmp, "absent", "s.json")])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", log)

    def test_an_unwritable_replay_directory_is_caught(self) -> None:
        code, log = self.run_with_probe(
            ["--url", "http://127.0.0.1:9", "--mode", "backfill",
             "--replay-dir", os.path.join(self.tmp, "absent"),
             "--source", "x"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", log)

    def test_a_good_setup_says_so_and_proceeds(self) -> None:
        source = self.write_jsonl([{"environment": "e"}])
        code, log = self.run_with_probe(
            self.live_args() + ["--source", source])
        self.assertEqual(code, 0)
        self.assertIn("preflight OK", log)

    def test_skip_preflight_does_not_contact_the_dashboard(self) -> None:
        source = self.write_jsonl([{"environment": "e"}])
        with mock.patch("feeder.preflight.probe_dashboard") as probe:
            self.run_main_with_fake(
                self.base_args() + ["--source", source],
                make_stats(read=1, valid=1, sent=1, inserted=1))
        probe.assert_not_called()

    def test_a_dry_run_needs_no_dashboard_and_no_writable_paths(self) -> None:
        """--dry-run sends nothing and writes nothing, so it checks neither."""
        source = self.write_jsonl([{"environment": "e"}])
        with mock.patch("feeder.preflight.probe_dashboard") as probe:
            code, log = self.run_with_probe(
                ["--url", "http://127.0.0.1:9", "--mode", "daily",
                 "--dry-run", "--source", source,
                 "--replay-dir", os.path.join(self.tmp, "absent"),
                 "--state-file", os.path.join(self.tmp, "absent", "s.json")])
        self.assertEqual(code, 0)
        self.assertIn("dry run", log)
        probe.assert_not_called()


class ConfigFileTest(CliTestBase):
    """Settings read from a file rather than typed on the command line."""

    def write_config(self, settings: Dict[str, Any]) -> str:
        """Write a config file into the temp dir and return its path."""
        path = os.path.join(self.tmp, "feeder.config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        return path

    def test_a_config_file_can_replace_every_flag(self) -> None:
        source = self.write_jsonl([{"environment": "e"}])
        path = self.write_config({
            "url": "http://127.0.0.1:9", "mode": "backfill",
            "source": [source], "state_file": self.state_file,
            "replay_dir": self.tmp,
        })
        code, created = self.run_main_with_fake(
            ["--config", path, "--skip-preflight"],
            make_stats(read=1, valid=1, sent=1, inserted=1))
        self.assertEqual(code, 0)
        self.assertEqual(created[0].url, "http://127.0.0.1:9")

    def test_a_flag_overrides_the_config_file(self) -> None:
        source = self.write_jsonl([{"environment": "e"}])
        path = self.write_config({
            "url": "http://from-config:8000", "mode": "backfill",
            "source": [source], "replay_dir": self.tmp,
        })
        _, created = self.run_main_with_fake(
            ["--config", path, "--url", "http://from-flag:8000",
             "--skip-preflight"],
            make_stats(read=1, valid=1, sent=1, inserted=1))
        self.assertEqual(created[0].url, "http://from-flag:8000")

    def test_a_source_flag_replaces_rather_than_extends_the_config(
        self
    ) -> None:
        """--source is an append option; a default would silently add to it."""
        from_config = self.write_jsonl([{"environment": "config"}])
        from_flag = os.path.join(self.tmp, "flag.jsonl")
        with open(from_flag, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"environment": "flag", "script": "s", "test_name": "t",
                 "result": "PASS", "output": "",
                 "start_time": "2026-07-25T01:00:00.000000",
                 "end_time": "2026-07-25T01:00:01.000000"}) + "\n")
        path = self.write_config({
            "url": "http://127.0.0.1:9", "mode": "backfill",
            "source": [from_config], "replay_dir": self.tmp,
        })
        _, created = self.run_main_with_fake(
            ["--config", path, "--source", from_flag, "--skip-preflight"],
            make_stats(read=1, valid=1, sent=1, inserted=1))
        environments = {record["environment"]
                        for record in created[0].submit_calls[0]["records"]}
        self.assertEqual(environments, {"flag"})

    def test_a_broken_config_stops_the_run_with_the_reason(self) -> None:
        path = self.write_config({"batchsize": 100})
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = run_feeder.main(["--config", path])
        self.assertEqual(code, 2)
        self.assertIn("batchsize", stream.getvalue())
        self.assertIn("batch_size", stream.getvalue())

    def test_check_reader_uses_the_config_so_it_need_not_be_retyped(
        self
    ) -> None:
        """A deployed site has a config; re-checking its reader should use it."""
        source = self.write_jsonl([{
            "environment": "prod", "script": "s", "test_name": "t",
            "result": "PASS", "output": "",
            "start_time": "2026-07-25T01:00:00.000000",
            "end_time": "2026-07-25T01:00:01.000000"}])
        path = self.write_config({
            "url": "http://127.0.0.1:9", "mode": "daily",
            "reader": "jsonl", "source": [source],
        })
        with self.capture_logs() as caught:
            code = run_feeder.main(["--config", path, "--check-reader"])
        self.assertEqual(code, 0)
        self.assertIn("read=1 valid=1 invalid=0", "\n".join(caught.output))

    def test_init_is_reached_and_carries_the_config_path(self) -> None:
        """--init must route through main() before --url/--mode are demanded."""
        with mock.patch("feeder.init.run_init", return_value=0) as wizard:
            code = run_feeder.main(["--init", "--config", "/tmp/x.json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            wizard.call_args[1]["config_path"], "/tmp/x.json")

    def test_init_without_a_terminal_refuses_rather_than_hanging(self) -> None:
        """It is an interactive mode in a tool designed for cron."""
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            with mock.patch.object(sys, "stdin", io.StringIO("")):
                code = run_feeder.main(["--init"])
        self.assertEqual(code, 2)
        self.assertIn("not a tty", stream.getvalue())

    def test_a_missing_config_names_the_wizard(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = run_feeder.main(
                ["--config", os.path.join(self.tmp, "absent.json")])
        self.assertEqual(code, 2)
        self.assertIn("--init", stream.getvalue())


class SourceFileHygieneTest(unittest.TestCase):
    """run_feeder.py stays Python-2 parseable (no f-strings)."""

    def test_no_fstrings_in_run_feeder(self) -> None:
        """The entry script must contain no f-string literals."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "run_feeder.py",
        )
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('f"', source)
        self.assertNotIn("f'", source)


if __name__ == "__main__":
    unittest.main()
