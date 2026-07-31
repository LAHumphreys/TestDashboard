"""Tests for the ``tools/`` demo-data package.

Covers the deterministic demo generator (personas, transport-schema
validity, identity uniqueness, CLI), the in-process self-test collector
(outcome mapping against a throwaway fixture suite — never the real repo
suite, which would recurse), and ``demo_bootstrap`` in ``--no-serve``
mode including idempotent re-runs.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Dict, List, Tuple
from unittest import mock

from testboard.model import (
    Result,
    RunRecord,
    parse_run_record,
    run_record_to_dict,
    utcnow,
)
from testboard.storage import Storage
from tools import (
    demo_bootstrap,
    drop_environment,
    generate_demo_data,
    prune_runs,
    run_self_tests,
)

# A Friday, well past the 03:00 batch cutoff, so the generated history
# ends on this same date.
NOW = datetime.datetime(2026, 7, 24, 12, 0, 0)


def runs_for(records: List[RunRecord], test_name: str) -> List[RunRecord]:
    """Return the records for one simulated test, oldest first."""
    picked = [rec for rec in records if rec.test_name == test_name]
    return sorted(picked, key=lambda rec: rec.start_time)


class TestGenerateDemoData(unittest.TestCase):
    """The simulated history: determinism, personas, schema validity."""

    def generate(self, days: int = 45,
                 seed: int = generate_demo_data.DEFAULT_SEED,
                 now: datetime.datetime = NOW) -> List[RunRecord]:
        """Generate with fixed defaults so every test is deterministic."""
        return generate_demo_data.generate_runs(
            days=days, seed=seed, now=now)

    def test_deterministic_for_equal_inputs(self) -> None:
        """Equal (days, seed, now) produce byte-identical records."""
        self.assertEqual(self.generate(days=20, seed=7),
                         self.generate(days=20, seed=7))

    def test_different_seed_changes_data(self) -> None:
        """A different seed produces different data."""
        self.assertNotEqual(self.generate(days=20, seed=7),
                            self.generate(days=20, seed=8))

    def test_shape_one_run_per_test_per_day(self) -> None:
        """days=10 yields 10 runs for each simulated test, all linux-sim."""
        records = self.generate(days=10)
        self.assertEqual(
            len(records), 10 * len(generate_demo_data._SPECS))
        self.assertEqual({rec.environment for rec in records},
                         {generate_demo_data.ENVIRONMENT})
        days = {rec.start_time.date() for rec in records}
        self.assertEqual(len(days), 10)

    def test_identity_keys_are_unique(self) -> None:
        """No two records collide on the upsert key (would silently merge)."""
        records = self.generate()
        keys = {
            (rec.environment, rec.script, rec.test_name, rec.start_time)
            for rec in records
        }
        self.assertEqual(len(keys), len(records))

    def test_all_records_survive_transport_round_trip(self) -> None:
        """Every record validates through the strict transport parser."""
        for rec in self.generate(days=5):
            self.assertEqual(parse_run_record(run_record_to_dict(rec)), rec)

    def test_outputs_and_source_links_populated(self) -> None:
        """Runs carry non-empty simulated output and a fake source link."""
        for rec in self.generate(days=3):
            self.assertTrue(rec.output)
            self.assertIn(rec.test_name, rec.output)
            self.assertTrue(
                rec.source_link.startswith("https://git.example.com/"))

    def test_history_ends_yesterday_before_batch_cutoff(self) -> None:
        """At 02:30 the 02:00 batch is still running: end yesterday."""
        records = self.generate(
            days=5, now=datetime.datetime(2026, 7, 24, 2, 30, 0))
        last = max(rec.start_time for rec in records)
        self.assertEqual(last.date(), datetime.date(2026, 7, 23))

    def test_history_ends_today_after_batch_cutoff(self) -> None:
        """Past 03:00 today's batch is complete: end today."""
        records = self.generate(
            days=5, now=datetime.datetime(2026, 7, 24, 4, 0, 0))
        last = max(rec.start_time for rec in records)
        self.assertEqual(last.date(), datetime.date(2026, 7, 24))

    def test_regression_persona(self) -> None:
        """The regression passes for weeks, then fails every trailing day."""
        history = runs_for(self.generate(), "test_partial_update_retry")
        tail = history[-generate_demo_data._REGRESSION_FAIL_DAYS:]
        head = history[:-generate_demo_data._REGRESSION_FAIL_DAYS]
        self.assertTrue(all(rec.result is Result.FAIL for rec in tail))
        self.assertTrue(all(rec.result is Result.PASS for rec in head))
        self.assertTrue(head)  # sanity: there was a passing era

    def test_monday_failer_persona(self) -> None:
        """test_eod_rollover fails exactly when the run starts on Monday."""
        history = runs_for(self.generate(), "test_eod_rollover")
        for rec in history:
            if rec.start_time.weekday() == 0:
                self.assertIs(rec.result, Result.FAIL)
            else:
                self.assertIs(rec.result, Result.PASS)
        mondays = [r for r in history if r.start_time.weekday() == 0]
        self.assertTrue(mondays)  # the window must contain Mondays

    def test_known_failure_persona(self) -> None:
        """Annotated known failure, gone stale for the trailing days."""
        history = runs_for(self.generate(), "test_legacy_fix42_gap")
        stale = history[-generate_demo_data._STALE_ANNOTATION_DAYS:]
        annotated = history[:-generate_demo_data._STALE_ANNOTATION_DAYS]
        self.assertTrue(
            all(rec.result is Result.UNEXPECTED_PASS for rec in stale))
        self.assertTrue(
            all(rec.result is Result.FAILED_AS_EXPECTED
                for rec in annotated))
        for rec in history:
            self.assertTrue(rec.known_failure_reason)

    def test_flaky_persona_scores_as_flaky(self) -> None:
        """The flaky test's transition score clears the 0.2 threshold."""
        history = runs_for(self.generate(), "test_md_subscribe_flap")
        results = {rec.result for rec in history}
        self.assertEqual(results, {Result.PASS, Result.FAIL})
        transitions = 0
        for prev, cur in zip(history, history[1:]):
            if (prev.result is Result.FAIL) != (cur.result is Result.FAIL):
                transitions += 1
        self.assertGreaterEqual(transitions / len(history), 0.2)

    def test_slowing_persona(self) -> None:
        """test_snapshot_load always passes but grows from ~2s to ~9s."""
        history = runs_for(self.generate(), "test_snapshot_load")
        durations = [
            (rec.end_time - rec.start_time).total_seconds()
            for rec in history
        ]
        self.assertTrue(
            all(rec.result is Result.PASS for rec in history))
        self.assertLess(durations[0], 3.0)
        self.assertGreater(durations[-1], 8.0)

    def test_days_below_one_rejected(self) -> None:
        """days < 1 raises ValueError."""
        with self.assertRaises(ValueError):
            generate_demo_data.generate_runs(days=0)


class TestFillerEstate(unittest.TestCase):
    """generate_filler_batches: the scaled, mostly-green estate."""

    def flatten(self, total: int = 60, days: int = 3) -> List[RunRecord]:
        """Collect all filler batches into one list."""
        records: List[RunRecord] = []
        for batch in generate_demo_data.generate_filler_batches(
                total, days=days, seed=5, now=NOW):
            records.extend(batch)
        return records

    def test_deterministic(self) -> None:
        """Equal inputs produce identical batches."""
        self.assertEqual(self.flatten(), self.flatten())

    def test_test_count_and_batch_shape(self) -> None:
        """Exactly N distinct tests, ~30 per script, ~days runs each.

        A rare test goes silent (its trailing days have no runs), so
        per-test run counts are days or slightly fewer — never zero.
        """
        records = self.flatten(total=65, days=3)
        per_test: Dict[Tuple[str, str, str], int] = {}
        for r in records:
            key = (r.environment, r.script, r.test_name)
            per_test[key] = per_test.get(key, 0) + 1
        self.assertEqual(len(per_test), 65)
        for count in per_test.values():
            self.assertGreaterEqual(count, 1)
            self.assertLessEqual(count, 3)
        per_script: Dict[str, set] = {}
        for r in records:
            per_script.setdefault(r.script, set()).add(r.test_name)
        sizes = sorted(len(names) for names in per_script.values())
        self.assertEqual(sizes, [5, 30, 30])

    def test_mostly_passing_with_problem_sprinkle(self) -> None:
        """The estate is overwhelmingly green (it must look realistic)."""
        records = self.flatten(total=400, days=2)
        latest: Dict[Tuple[str, str, str], RunRecord] = {}
        for r in records:
            key = (r.environment, r.script, r.test_name)
            if key not in latest or r.start_time > latest[key].start_time:
                latest[key] = r
        results = [r.result for r in latest.values()]
        pass_share = results.count(Result.PASS) / len(results)
        self.assertGreater(pass_share, 0.9)

    def test_environments_and_validity(self) -> None:
        """Filler runs use the known sim environments and round-trip."""
        records = self.flatten(total=40, days=2)
        allowed = {env for env, _weight in
                   generate_demo_data._FILLER_ENVS}
        self.assertLessEqual({r.environment for r in records}, allowed)
        for rec in records[:20]:
            self.assertEqual(parse_run_record(run_record_to_dict(rec)),
                             rec)

    def test_invalid_arguments(self) -> None:
        """days < 1 and negative totals raise ValueError."""
        with self.assertRaises(ValueError):
            list(generate_demo_data.generate_filler_batches(
                10, days=0, now=NOW))
        with self.assertRaises(ValueError):
            list(generate_demo_data.generate_filler_batches(
                -1, now=NOW))


class TestGenerateDemoDataCLI(unittest.TestCase):
    """The generate_demo_data CLI writes feeder-ready JSON lines."""

    def setUp(self) -> None:
        """Create a scratch directory."""
        self.tmp = tempfile.mkdtemp(prefix="testboard-tools-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_writes_valid_jsonl(self) -> None:
        """--out writes one valid transport object per line."""
        path = os.path.join(self.tmp, "demo.jsonl")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = generate_demo_data.main(
                ["--out", path, "--days", "3", "--seed", "1"])
        self.assertEqual(rc, 0)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 3 * len(generate_demo_data._SPECS))
        for line in lines:
            parse_run_record(json.loads(line))  # must not raise
        self.assertIn("simulated runs", out.getvalue())

    def test_bad_days_exits_2(self) -> None:
        """--days 0 fails with exit code 2 and a named error."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = generate_demo_data.main(
                ["--out", os.path.join(self.tmp, "x.jsonl"), "--days", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("days must be >= 1", err.getvalue())


_FIXTURE_SOURCE = '''\
"""Throwaway suite exercising every unittest outcome."""

import unittest


class SampleTests(unittest.TestCase):
    """One test per unittest outcome."""

    def test_pass(self):
        """Plain success."""
        self.assertTrue(True)

    def test_fail(self):
        """Assertion failure."""
        self.assertEqual(1, 2)

    def test_error(self):
        """Unhandled exception."""
        raise RuntimeError("boom")

    @unittest.expectedFailure
    def test_expected_failure(self):
        """Annotated known failure that does fail."""
        self.fail("known broken")

    @unittest.expectedFailure
    def test_unexpected_success(self):
        """Annotated known failure that passes (stale annotation)."""
        self.assertTrue(True)

    @unittest.skip("not today")
    def test_skipped(self):
        """Skipped: must produce no run record."""

    def test_prints(self):
        """Success that prints — output must be buffered, not leaked."""
        print("this must not leak to the console")
        self.assertTrue(True)
'''


class TestSplitTestId(unittest.TestCase):
    """Unittest-id to (script, test_name) mapping."""

    def test_standard_id(self) -> None:
        """package.module.Class.method splits at the last two dots."""
        self.assertEqual(
            run_self_tests.split_test_id(
                "tests.test_storage.TestUpsert.test_foo"),
            ("tests/test_storage.py", "TestUpsert.test_foo"))

    def test_nested_package(self) -> None:
        """Deeper packages become deeper script paths."""
        self.assertEqual(
            run_self_tests.split_test_id("a.b.test_mod.Cls.test_m"),
            ("a/b/test_mod.py", "Cls.test_m"))

    def test_degenerate_id_does_not_raise(self) -> None:
        """Loader-generated oddball ids fall back instead of raising."""
        self.assertEqual(
            run_self_tests.split_test_id("weird"),
            ("weird.py", "weird"))


class TestRunSelfTests(unittest.TestCase):
    """Outcome mapping, run against a throwaway fixture suite."""

    def setUp(self) -> None:
        """Write the fixture suite into a scratch directory."""
        self.tmp = tempfile.mkdtemp(prefix="testboard-selftest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "test_toolsample.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(_FIXTURE_SOURCE)
        # Discovery imports the fixture module and extends sys.path;
        # undo both so repeated tests re-import a fresh copy.
        self.addCleanup(sys.modules.pop, "test_toolsample", None)
        path_before = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(
            slice(None), path_before))

    def collect(self) -> Tuple[List[RunRecord], Dict[str, int], str]:
        """Run the fixture suite; return (runs, summary, leaked stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runs, summary = run_self_tests.collect_self_test_runs(self.tmp)
        return runs, summary, out.getvalue()

    def by_name(self, runs: List[RunRecord]) -> Dict[str, RunRecord]:
        """Index records by bare method name."""
        return {rec.test_name.split(".")[-1]: rec for rec in runs}

    def test_summary_counts(self) -> None:
        """7 defined tests: 6 ran (1 skipped), one of each outcome."""
        _runs, summary, _out = self.collect()
        self.assertEqual(summary, {
            "ran": 6,
            "passed": 2,
            "failed": 2,
            "errors": 1,
            "expected_failures": 1,
            "unexpected_passes": 1,
            "skipped": 1,
        })

    def test_outcome_mapping_and_outputs(self) -> None:
        """Each unittest outcome maps to the right Result and output."""
        runs, _summary, _out = self.collect()
        recs = self.by_name(runs)
        self.assertNotIn("test_skipped", recs)  # skips produce no record
        self.assertIs(recs["test_pass"].result, Result.PASS)
        self.assertIs(recs["test_prints"].result, Result.PASS)
        self.assertIs(recs["test_fail"].result, Result.FAIL)
        self.assertIn("AssertionError", recs["test_fail"].output)
        self.assertIs(recs["test_error"].result, Result.FAIL)
        self.assertIn("RuntimeError: boom", recs["test_error"].output)
        self.assertIs(recs["test_expected_failure"].result,
                      Result.FAILED_AS_EXPECTED)
        self.assertTrue(recs["test_expected_failure"].known_failure_reason)
        self.assertIs(recs["test_unexpected_success"].result,
                      Result.UNEXPECTED_PASS)
        self.assertTrue(recs["test_unexpected_success"].known_failure_reason)

    def test_identity_and_transport_validity(self) -> None:
        """Records use the fixture module as script and round-trip."""
        runs, _summary, _out = self.collect()
        for rec in runs:
            self.assertEqual(rec.environment, run_self_tests.ENVIRONMENT)
            self.assertEqual(rec.script, "test_toolsample.py")
            self.assertTrue(rec.test_name.startswith("SampleTests."))
            self.assertGreaterEqual(rec.end_time, rec.start_time)
            self.assertEqual(
                parse_run_record(run_record_to_dict(rec)), rec)

    def test_test_stdout_is_buffered(self) -> None:
        """print() inside a test never leaks to the console."""
        _runs, _summary, out = self.collect()
        self.assertNotIn("this must not leak", out)


class TestDemoBootstrap(unittest.TestCase):
    """demo_bootstrap --no-serve: seeding, triage content, idempotency."""

    def setUp(self) -> None:
        """Create a scratch directory and db path."""
        self.tmp = tempfile.mkdtemp(prefix="testboard-bootstrap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "demo.db")

    def run_main(self, *extra: str) -> Tuple[int, str]:
        """Run demo_bootstrap.main seeded, no server, at a frozen now."""
        argv = ["--db", self.db, "--days", "5", "--seed", "3",
                "--skip-self-tests", "--no-serve"] + list(extra)
        out = io.StringIO()
        with mock.patch("testboard.model.utcnow", return_value=NOW):
            with contextlib.redirect_stdout(out):
                rc = demo_bootstrap.main(argv)
        return rc, out.getvalue()

    def open_storage(self) -> Storage:
        """Open the seeded database, closing it on test teardown."""
        storage = Storage(self.db)
        self.addCleanup(storage.close)
        return storage

    def regression_triple(self) -> Tuple[str, str, str]:
        """Identity triple of the simulated regression test."""
        return (generate_demo_data.ENVIRONMENT,
                "regression/user_lifecycle.py",
                "test_partial_update_retry")

    def test_no_serve_seeds_everything(self) -> None:
        """One --no-serve run seeds runs, users, comments, assignment."""
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("Seeded", out)
        storage = self.open_storage()
        rows = storage.dashboard(
            environment=generate_demo_data.ENVIRONMENT)
        self.assertEqual(len(rows), len(generate_demo_data._SPECS))
        env, script, test = self.regression_triple()
        history = storage.run_history(env, script, test, limit=500)
        self.assertEqual(len(history), 5)
        comments = storage.comments(env, script, test)
        self.assertEqual([c.author for c in comments], ["bob", "alice"])
        self.assertEqual(storage.current_assignee(env, script, test),
                         "alice")
        usernames = {user.username for user in storage.list_users()}
        self.assertEqual(usernames, {"alice", "bob"})

    def test_rerun_is_idempotent(self) -> None:
        """A second run duplicates nothing — and now WRITES nothing.

        The seeded records are byte-identical the second time, so the
        upsert reports them all as unchanged rather than rewriting them
        in place (the same skip that makes the site feeder's 10-minute
        re-push free).
        """
        self.run_main()
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        expected_runs = 5 * len(generate_demo_data._SPECS)
        self.assertIn(
            "0 inserted, 0 updated, {} unchanged".format(expected_runs),
            out)
        storage = self.open_storage()
        env, script, test = self.regression_triple()
        self.assertEqual(
            len(storage.run_history(env, script, test, limit=500)), 5)
        self.assertEqual(len(storage.comments(env, script, test)), 2)
        self.assertEqual(len(storage.list_users()), 2)

    def test_bad_days_exits_2(self) -> None:
        """--days 0 fails with exit code 2 and a named error."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _out = self.run_main("--days", "0")
        self.assertEqual(rc, 2)
        self.assertIn("days must be >= 1", err.getvalue())

    def test_scale_tests_seeds_filler_estate(self) -> None:
        """--scale-tests adds N filler tests on top of the personas."""
        rc, out = self.run_main("--scale-tests", "45")
        self.assertEqual(rc, 0)
        self.assertIn("filler", out)
        storage = self.open_storage()
        rows = storage.dashboard()
        self.assertEqual(
            len(rows), 45 + len(generate_demo_data._SPECS))

    def test_unknown_flag_exits_2(self) -> None:
        """argparse errors surface as exit code 2, not SystemExit."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = demo_bootstrap.main(["--frobnicate"])
        self.assertEqual(rc, 2)


class TestPruneRunsCli(unittest.TestCase):
    """tools/prune_runs.py: the retention job run from cron."""

    def setUp(self) -> None:
        """Seed a database with 40 nights of one test plus one stale test."""
        self.tmp = tempfile.mkdtemp(prefix="testboard-prune-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "prune.db")
        storage = Storage(self.db)
        self.addCleanup(storage.close)
        now = utcnow()
        records = [
            RunRecord(
                environment="linux-sim", script="suite.py",
                test_name="test_daily",
                result=Result.PASS,
                start_time=now - datetime.timedelta(days=day),
                end_time=(now - datetime.timedelta(days=day)
                          + datetime.timedelta(seconds=1)),
                output="log line\n" * 20,
                source_link="https://example.com/suite.py",
                known_failure_reason=None,
            )
            for day in range(40)
        ]
        records.append(RunRecord(
            environment="linux-sim", script="suite.py",
            test_name="test_retired", result=Result.PASS,
            start_time=now - datetime.timedelta(days=200),
            end_time=(now - datetime.timedelta(days=200)
                      + datetime.timedelta(seconds=1)),
            output="log\n", source_link="https://example.com/suite.py",
            known_failure_reason=None,
        ))
        storage.upsert_runs(records)
        storage.close()

    def run_main(self, *args: str) -> Tuple[int, str]:
        """Run prune_runs.main, capturing stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = prune_runs.main(["--db", self.db] + list(args))
        return rc, out.getvalue()

    def open_storage(self) -> Storage:
        storage = Storage(self.db)
        self.addCleanup(storage.close)
        return storage

    def test_dry_run_changes_nothing(self) -> None:
        rc, out = self.run_main("--keep-days", "10", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would be deleted", out)
        storage = self.open_storage()
        self.assertEqual(
            len(storage.run_history(
                "linux-sim", "suite.py", "test_daily", limit=100)),
            40,
        )

    def test_prune_keeps_the_window_and_every_latest_run(self) -> None:
        rc, out = self.run_main("--keep-days", "10")
        self.assertEqual(rc, 0)
        self.assertIn("Deleted", out)
        storage = self.open_storage()
        remaining = storage.run_history(
            "linux-sim", "suite.py", "test_daily", limit=100)
        self.assertEqual(len(remaining), 10)
        # The retired test is 200 days old but is still listed: its only
        # run is its latest run.
        self.assertIn(
            "test_retired",
            [row.test_name for row in storage.dashboard()],
        )

    def test_vacuum_shrinks_the_file(self) -> None:
        before = os.path.getsize(self.db)
        rc, out = self.run_main("--keep-days", "1", "--vacuum")
        self.assertEqual(rc, 0)
        self.assertIn("VACUUM", out)
        self.assertLess(os.path.getsize(self.db), before)

    def test_missing_database_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = prune_runs.main(
                ["--db", os.path.join(self.tmp, "nope.db")])
        self.assertEqual(rc, 2)
        self.assertIn("not found", err.getvalue())

    def test_zero_keep_days_rejected(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = prune_runs.main(
                ["--db", self.db, "--keep-days", "0"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()


class TestDropEnvironmentCLI(unittest.TestCase):
    """The drop-environment CLI: it must be hard to run by accident.

    The storage-level behaviour is covered by
    ``tests/test_storage.py::EnvironmentDeleteTest``. What is worth
    testing here is the refusals, because the whole risk of this tool is
    someone typing the wrong environment name at a prompt that then
    deletes a year of history with no rollback.
    """

    def setUp(self) -> None:
        """A scratch database holding two environments."""
        self.tmp = tempfile.mkdtemp(prefix="testboard-drop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        store = Storage(self.db)
        store.upsert_runs([
            RunRecord(
                environment=environment, script="suite.py",
                test_name="test_a", result=Result.PASS,
                start_time=NOW, end_time=NOW + datetime.timedelta(seconds=1),
                output="out", source_link="", known_failure_reason=None)
            for environment in ("linux-sim", "UNKNOWN")
        ])
        store.close()

    def run_cli(self, argv: List[str], answer: str = "") -> Tuple[int, str]:
        """Run main() with stdin scripted; return (exit code, stdout)."""
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=[answer]):
            with contextlib.redirect_stdout(out):
                rc = drop_environment.main(argv)
        return rc, out.getvalue()

    def environments(self) -> List[str]:
        store = Storage(self.db)
        self.addCleanup(store.close)
        return sorted(store.environments())

    def test_dry_run_changes_nothing(self) -> None:
        rc, out = self.run_cli(
            ["--db", self.db, "-e", "UNKNOWN", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.environments(), ["UNKNOWN", "linux-sim"])

    def test_a_wrong_confirmation_aborts(self) -> None:
        """The point of the prompt. Typing anything else must not delete."""
        rc, out = self.run_cli(
            ["--db", self.db, "-e", "UNKNOWN"], answer="unknown")
        self.assertEqual(rc, 1)
        self.assertIn("Aborted", out)
        self.assertEqual(self.environments(), ["UNKNOWN", "linux-sim"])

    def test_the_exact_name_confirms(self) -> None:
        rc, _ = self.run_cli(
            ["--db", self.db, "-e", "UNKNOWN"], answer="UNKNOWN")
        self.assertEqual(rc, 0)
        self.assertEqual(self.environments(), ["linux-sim"])

    def test_yes_skips_the_prompt(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drop_environment.main(
                ["--db", self.db, "-e", "UNKNOWN", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.environments(), ["linux-sim"])

    def test_an_unknown_name_is_reported_not_deleted(self) -> None:
        """Says what IS there, because the usual cause is a typo."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drop_environment.main(
                ["--db", self.db, "-e", "nope", "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("linux-sim", out.getvalue())
        self.assertEqual(self.environments(), ["UNKNOWN", "linux-sim"])

    def test_a_missing_database_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_environment.main(
                ["--db", os.path.join(self.tmp, "no.db"), "-e", "x", "--yes"])
        self.assertEqual(rc, 2)
        self.assertIn("Database not found", err.getvalue())

    def test_an_empty_environment_name_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drop_environment.main(
                ["--db", self.db, "-e", "  ", "--yes"])
        self.assertEqual(rc, 2)
        self.assertIn("must not be empty", err.getvalue())
