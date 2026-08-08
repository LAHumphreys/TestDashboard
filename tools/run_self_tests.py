#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run this repository's own unittest suite and record it as test runs.

The suite is discovered and run in-process with a recording
``unittest.TestResult``; every test becomes one run record for the
environment ``local-unittest``:

- ``script`` is the test module as a path (``tests/test_storage.py``),
- ``test_name`` is ``TestClass.test_method``,
- outcomes map success -> ``PASS``, failure/error -> ``FAIL`` (with the
  captured traceback as the run output), ``@expectedFailure`` ->
  ``FAILED_AS_EXPECTED`` and unexpected success -> ``UNEXPECTED_PASS``,
- skipped tests produce no record (they never ran) but are counted.

This gives the demo dashboard a second, *real* environment next to the
simulated ``linux-sim`` data, and doubles as dogfooding: testboard
displaying its own test results. As a CLI::

    python3 tools/run_self_tests.py                  # run + summary only
    python3 tools/run_self_tests.py --db testboard.db  # also seed a db
    python3 tools/run_self_tests.py --out selftests.jsonl

NOTE: this file intentionally contains no f-strings and only type
COMMENTS (PEP 484 style), so it still *parses* under Python 2 — on RHEL 8
someone will inevitably type ``python`` (2.7) instead of ``python3``, and
they must get the clear version message from main() instead of a bare
SyntaxError. Imports of testboard modules (Python-3-only syntax) are
deferred into function bodies for the same reason.
"""

import argparse
import json
import logging
import os
import sys
import unittest

try:
    from typing import Any, Dict, List, Optional, Tuple  # noqa: F401
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: The environment name all self-test runs are recorded under.
ENVIRONMENT = "local-unittest"

#: known_failure_reason attached to expected-failure / unexpected-pass
#: records (their annotation is the ``@unittest.expectedFailure``
#: decorator in the test source).
_EXPECTED_FAILURE_REASON = "annotated @unittest.expectedFailure in the suite"


def split_test_id(test_id):
    # type: (str) -> Tuple[str, str]
    """Split a unittest id into ``(script, test_name)``.

    ``tests.test_storage.TestUpsert.test_foo`` becomes
    ``("tests/test_storage.py", "TestUpsert.test_foo")``. Degenerate ids
    (fewer than three dotted parts) fall back to using the whole id for
    both fields rather than raising — one odd loader-generated test must
    never abort the collection.
    """
    parts = test_id.split(".")
    if len(parts) >= 3:
        return ("/".join(parts[:-2]) + ".py",
                "{0}.{1}".format(parts[-2], parts[-1]))
    return (test_id + ".py", test_id)


class RecordingResult(unittest.TestResult):
    """A ``TestResult`` that captures per-test timing, outcome and output.

    Buffering is enabled so stdout/stderr printed by tests is captured
    (and appended to failure tracebacks by unittest) instead of leaking
    to the console. Each finished test appends a plain dict to
    ``self.records``; conversion to typed ``RunRecord`` objects happens
    later in :func:`records_to_runs` (keeping this class free of
    testboard imports so the module parses everywhere).
    """

    def __init__(self):
        # type: () -> None
        unittest.TestResult.__init__(self)
        self.buffer = True
        self.records = []  # type: List[Dict[str, Any]]
        self.skip_count = 0
        self.error_count = 0
        self._start = None  # type: Optional[Any]
        self._outcome = None  # type: Optional[str]
        self._output_parts = []  # type: List[str]

    # -- unittest hooks -------------------------------------------------

    def startTest(self, test):
        # type: (unittest.TestCase) -> None
        unittest.TestResult.startTest(self, test)
        from testboard import model
        self._start = model.utcnow()
        self._outcome = None
        self._output_parts = []

    def addSuccess(self, test):
        # type: (unittest.TestCase) -> None
        unittest.TestResult.addSuccess(self, test)
        self._outcome = "PASS"

    def addFailure(self, test, err):
        # type: (unittest.TestCase, Any) -> None
        unittest.TestResult.addFailure(self, test, err)
        self._outcome = "FAIL"
        self._output_parts.append(self.failures[-1][1])

    def addError(self, test, err):
        # type: (unittest.TestCase, Any) -> None
        unittest.TestResult.addError(self, test, err)
        self._outcome = "FAIL"
        self.error_count += 1
        self._output_parts.append(self.errors[-1][1])

    def addSubTest(self, test, subtest, outcome):
        # type: (unittest.TestCase, unittest.TestCase, Any) -> None
        unittest.TestResult.addSubTest(self, test, subtest, outcome)
        if outcome is not None:
            self._outcome = "FAIL"
            self._output_parts.append(
                "subtest {0} failed:\n{1}".format(
                    subtest.id(), self._exc_info_to_string(outcome, test))
            )

    def addExpectedFailure(self, test, err):
        # type: (unittest.TestCase, Any) -> None
        unittest.TestResult.addExpectedFailure(self, test, err)
        self._outcome = "FAILED_AS_EXPECTED"
        self._output_parts.append(self.expectedFailures[-1][1])

    def addUnexpectedSuccess(self, test):
        # type: (unittest.TestCase) -> None
        unittest.TestResult.addUnexpectedSuccess(self, test)
        self._outcome = "UNEXPECTED_PASS"
        self._output_parts.append(
            "test is decorated @expectedFailure but passed — the "
            "annotation may be stale\n"
        )

    def addSkip(self, test, reason):
        # type: (unittest.TestCase, str) -> None
        unittest.TestResult.addSkip(self, test, reason)
        self._outcome = "SKIP"
        self.skip_count += 1

    def stopTest(self, test):
        # type: (unittest.TestCase) -> None
        from testboard import model
        end = model.utcnow()
        start = self._start if self._start is not None else end
        outcome = self._outcome
        if outcome is None:
            # No hook fired (defensive; should not happen) — the test
            # completed without unittest reporting anything, so treat it
            # as a pass rather than losing the record.
            outcome = "PASS"
        if outcome != "SKIP":
            self.records.append(
                {
                    "id": test.id(),
                    "outcome": outcome,
                    "start": start,
                    "end": end,
                    "output": "".join(self._output_parts),
                }
            )
        self._start = None
        self._outcome = None
        self._output_parts = []
        # Base-class stopTest LAST: with buffer=True it restores
        # sys.stdout/sys.stderr and discards the captured text that
        # _exc_info_to_string needed above.
        unittest.TestResult.stopTest(self, test)


def records_to_runs(records):
    # type: (List[Dict[str, Any]]) -> List[Any]
    """Convert :class:`RecordingResult` dicts into ``RunRecord`` objects."""
    from testboard.model import Result, RunRecord

    runs = []  # type: List[Any]
    for rec in records:
        script, test_name = split_test_id(rec["id"])
        outcome = rec["outcome"]
        if outcome in ("FAILED_AS_EXPECTED", "UNEXPECTED_PASS"):
            reason = _EXPECTED_FAILURE_REASON  # type: Optional[str]
        else:
            reason = None
        end = rec["end"]
        if end < rec["start"]:  # clock hiccup; keep the record valid
            end = rec["start"]
        runs.append(
            RunRecord(
                environment=ENVIRONMENT,
                script=script,
                test_name=test_name,
                result=Result[outcome],
                start_time=rec["start"],
                end_time=end,
                output=rec["output"],
                source_link="",
                known_failure_reason=reason,
                branch=None,
                build=None,
            )
        )
    return runs


def collect_self_test_runs(start_dir=None, pattern="test*.py"):
    # type: (Optional[str], str) -> Tuple[List[Any], Dict[str, int]]
    """Discover and run a unittest suite; return ``(runs, summary)``.

    *start_dir* defaults to this repository's root (i.e. the repo's own
    ``tests/`` package). ``runs`` is a list of ``RunRecord``; ``summary``
    counts ``ran`` (excluding skips), ``passed``, ``failed`` (assertion
    failures + errors), ``errors`` (subset of failed), ``expected_failures``,
    ``unexpected_passes`` and ``skipped``.
    """
    if start_dir is None:
        start_dir = _REPO_ROOT
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir, pattern=pattern)
    result = RecordingResult()
    # Detach root log handlers while the suite runs: the caller (e.g.
    # demo_bootstrap) may have configured console logging at INFO, and
    # tests exercising chatty code would spew into its output. A swap —
    # not logging.disable() — because disable() would break the suite's
    # own assertLogs assertions (it suppresses record creation).
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    null_handler = logging.NullHandler()
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.addHandler(null_handler)
    try:
        suite.run(result)
    finally:
        root.removeHandler(null_handler)
        for handler in saved_handlers:
            root.addHandler(handler)
    runs = records_to_runs(result.records)

    passed = 0
    failed = 0
    expected = 0
    unexpected = 0
    for rec in result.records:
        outcome = rec["outcome"]
        if outcome == "PASS":
            passed += 1
        elif outcome == "FAIL":
            failed += 1
        elif outcome == "FAILED_AS_EXPECTED":
            expected += 1
        elif outcome == "UNEXPECTED_PASS":
            unexpected += 1
    summary = {
        "ran": len(result.records),
        "passed": passed,
        "failed": failed,
        "errors": result.error_count,
        "expected_failures": expected,
        "unexpected_passes": unexpected,
        "skipped": result.skip_count,
    }
    return runs, summary


def format_summary(summary):
    # type: (Dict[str, int]) -> str
    """One human line: '303 tests: 303 passed, 0 failed, ... '."""
    return (
        "{ran} tests: {passed} passed, {failed} failed "
        "({errors} errored), {expected_failures} expected failures, "
        "{unexpected_passes} unexpected passes, {skipped} skipped".format(
            **summary)
    )


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the self-test collector CLI."""
    parser = argparse.ArgumentParser(
        prog="run_self_tests.py",
        description=(
            "Run this repository's unittest suite in-process and convert "
            "every test into a testboard run record (environment "
            "'{0}').".format(ENVIRONMENT)
        ),
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="upsert the collected runs into this SQLite database")
    parser.add_argument(
        "--out", default=None, metavar="PATH",
        help="write the collected runs to this JSON-lines file")
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Run the collector CLI; returns the process exit code (0/2)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ — you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/run_self_tests.py\n".format(
                sys.version_info[0], sys.version_info[1],
                sys.version_info[2]))
        return 2

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2

    print("Running the repository test suite (this takes a few seconds)...")
    sys.stdout.flush()
    runs, summary = collect_self_test_runs()
    print(format_summary(summary))

    if args.out is not None:
        from testboard.model import run_record_to_dict
        with open(args.out, "w", encoding="utf-8") as handle:
            for rec in runs:
                handle.write(json.dumps(run_record_to_dict(rec)))
                handle.write("\n")
        print("Wrote {0} runs to {1}".format(len(runs), args.out))

    if args.db is not None:
        from testboard.storage import Storage
        try:
            storage = Storage(args.db)
        except Exception as exc:
            sys.stderr.write(
                "Cannot open the database at {0}: {1}\n".format(
                    os.path.abspath(args.db), exc))
            return 2
        try:
            counts = storage.upsert_runs(runs)
        finally:
            storage.close()
        print(
            "Seeded {0} runs into {1} ({2} inserted, {3} updated, "
            "{4} unchanged)".format(
                len(runs), args.db, counts.inserted, counts.updated,
                counts.unchanged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
