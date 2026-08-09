#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic simulated test-run history for the testboard demo.

Generates one nightly run per test per day for a fake ``linux-sim``
environment, covering (by default) the last 45 days. The simulated tests
are chosen so every analytics feature of the dashboard has something to
show:

- two steady passers plus a steady smoke test,
- a regression (passed for weeks, failing for the last 9 days),
- a flaky test (seeded-random ~30% failure rate),
- a Monday-failer (fails if and only if the run starts on a Monday),
- a known failure (``FAILED_AS_EXPECTED`` with an annotation reason,
  turning ``UNEXPECTED_PASS`` for the two most recent days — a stale
  annotation, exactly what that result exists to surface),
- a slowing test (duration grows steadily from ~2s to ~9s).

Everything is derived from a single seeded ``random.Random``, so the same
``(days, seed, now)`` always produces byte-identical records. As a CLI it
writes transport-schema JSON lines (one run per line) ready for
``run_feeder.py --reader jsonl``::

    python3 tools/generate_demo_data.py --out demo.jsonl

``tools/demo_bootstrap.py`` imports :func:`generate_runs` instead and
seeds the database directly.

NOTE: this file intentionally contains no f-strings and only type
COMMENTS (PEP 484 style), so it still *parses* under Python 2 — on RHEL 8
someone will inevitably type ``python`` (2.7) instead of ``python3``, and
they must get the clear version message from main() instead of a bare
SyntaxError. Imports of testboard modules (Python-3-only syntax) are
deferred into function bodies for the same reason.
"""

import argparse
import datetime
import json
import os
import random
import sys

try:
    from typing import Any, Callable, List, Optional, Tuple  # noqa: F401
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: The fake environment all simulated runs belong to.
ENVIRONMENT = "linux-sim"

DEFAULT_DAYS = 45
DEFAULT_SEED = 20260701

#: Nightly batch start (UTC). Runs are spread over the following hour.
_BATCH_HOUR = 2

#: How many trailing days the regression persona fails for.
_REGRESSION_FAIL_DAYS = 9

#: How many trailing days the known-failure persona unexpectedly passes
#: for (its annotation has gone stale).
_STALE_ANNOTATION_DAYS = 2

_KNOWN_FAILURE_REASON = (
    "TB-217: FIX 4.2 sequence-gap replay broken upstream; "
    "vendor fix scheduled for the Q3 gateway release"
)


def _fake_source_link(script, line):
    # type: (str, int) -> str
    """Return a plausible (fake) source link for a simulated test."""
    return "https://git.example.com/qa/tests/{0}#L{1}".format(script, line)


def _output_text(result_name, script, test_name, day, duration, reason):
    # type: (str, str, str, datetime.date, float, Optional[str]) -> str
    """Build simulated captured output for one run.

    Passing runs get a short log; failing runs get the same log plus a
    fake traceback, so the run-detail page has something realistic to
    show. Deterministic (no randomness in here).
    """
    lines = [
        "[{0} 02:00 UTC] nightly batch: {1}".format(day.isoformat(), script),
        "collecting {0} ...".format(test_name),
        "fixture: sim service up, ref data loaded",
    ]
    if result_name == "PASS":
        lines.append(
            "PASS {0} ({1:.3f}s)".format(test_name, duration)
        )
    elif result_name == "FAIL":
        lines.extend(
            [
                "Traceback (most recent call last):",
                "  File \"{0}\", line 120, in {1}".format(script, test_name),
                "    self.assertEqual(expected_state, order.state)",
                "AssertionError: 'FILLED' != 'PENDING_REPLACE'",
                "FAIL {0} ({1:.3f}s)".format(test_name, duration),
            ]
        )
    elif result_name == "FAILED_AS_EXPECTED":
        lines.extend(
            [
                "expected failure ({0})".format(reason or "annotated"),
                "FAILED_AS_EXPECTED {0} ({1:.3f}s)".format(
                    test_name, duration
                ),
            ]
        )
    else:  # UNEXPECTED_PASS
        lines.extend(
            [
                "test is annotated as a known failure ({0}) "
                "but PASSED — annotation may be stale".format(
                    reason or "annotated"
                ),
                "UNEXPECTED_PASS {0} ({1:.3f}s)".format(test_name, duration),
            ]
        )
    return "\n".join(lines) + "\n"


def _steady(base_duration):
    # type: (float) -> Callable[[int, int, int, random.Random], Tuple[str, float, Optional[str]]]
    """Persona: always passes, mild duration jitter around *base_duration*."""

    def persona(day_index, day_count, weekday, rng):
        # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
        return ("PASS", base_duration + rng.random() * 0.4, None)

    return persona


def _regression(day_index, day_count, weekday, rng):
    # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
    """Persona: passed for weeks, failing for the trailing days."""
    failing = day_index >= day_count - _REGRESSION_FAIL_DAYS
    if failing:
        # Assertion failures die early, so failing runs are quicker.
        return ("FAIL", 0.8 + rng.random() * 0.3, None)
    return ("PASS", 3.1 + rng.random() * 0.5, None)


def _flaky(day_index, day_count, weekday, rng):
    # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
    """Persona: seeded-random ~30% failure rate — lots of transitions."""
    if rng.random() < 0.3:
        return ("FAIL", 1.9 + rng.random() * 0.4, None)
    return ("PASS", 2.0 + rng.random() * 0.4, None)


def _monday_failer(day_index, day_count, weekday, rng):
    # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
    """Persona: fails if and only if the run starts on a Monday."""
    if weekday == 0:
        return ("FAIL", 4.2 + rng.random() * 0.4, None)
    return ("PASS", 4.4 + rng.random() * 0.4, None)


def _known_failure(day_index, day_count, weekday, rng):
    # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
    """Persona: annotated known failure whose annotation went stale."""
    if day_index >= day_count - _STALE_ANNOTATION_DAYS:
        return ("UNEXPECTED_PASS", 1.1 + rng.random() * 0.2,
                _KNOWN_FAILURE_REASON)
    return ("FAILED_AS_EXPECTED", 1.0 + rng.random() * 0.2,
            _KNOWN_FAILURE_REASON)


def _slowing(day_index, day_count, weekday, rng):
    # type: (int, int, int, random.Random) -> Tuple[str, float, Optional[str]]
    """Persona: always passes but grows from ~2s to ~9s over the window."""
    if day_count > 1:
        progress = float(day_index) / float(day_count - 1)
    else:
        progress = 1.0
    return ("PASS", 2.0 + 7.0 * progress + rng.random() * 0.3, None)


#: The simulated test suite: (script, test_name, source line, persona).
#: Order matters — random draws happen in this order, so reordering
#: changes the generated data for a given seed.
_SPECS = [
    ("regression/user_lifecycle.py", "test_user_create", 42,
     _steady(1.5)),
    ("regression/user_lifecycle.py", "test_user_delete", 88,
     _steady(1.7)),
    ("regression/user_lifecycle.py", "test_partial_update_retry", 120,
     _regression),
    ("regression/market_data.py", "test_md_subscribe_flap", 57,
     _flaky),
    ("regression/market_data.py", "test_eod_rollover", 203,
     _monday_failer),
    ("regression/fix_gateway.py", "test_legacy_fix42_gap", 311,
     _known_failure),
    ("regression/market_data.py", "test_snapshot_load", 149,
     _slowing),
    ("smoke/startup.py", "test_process_boot", 17,
     _steady(0.6)),
]  # type: List[Tuple[str, str, int, Callable[[int, int, int, random.Random], Tuple[str, float, Optional[str]]]]]


def generate_runs(days=DEFAULT_DAYS, seed=DEFAULT_SEED, now=None):
    # type: (int, int, Optional[datetime.datetime]) -> List[Any]
    """Generate the simulated history as a list of ``RunRecord``.

    One run per test per day for *days* consecutive days ending at the
    most recent completed nightly batch relative to *now* (today's 02:00
    batch if *now* is past 03:00 UTC, else yesterday's). Deterministic:
    equal ``(days, seed, now)`` produce identical records.

    Raises:
        ValueError: if *days* < 1.
    """
    from testboard import model
    from testboard.model import Result, RunRecord

    if days < 1:
        raise ValueError("days must be >= 1, got {0}".format(days))
    if now is None:
        now = model.utcnow()

    last_day = now.date()
    if now.time() < datetime.time(_BATCH_HOUR + 1, 0):
        # The nightly batch that starts at 02:00 is not reliably finished
        # yet — end the history at yesterday's batch instead.
        last_day = last_day - datetime.timedelta(days=1)

    day_list = [
        last_day - datetime.timedelta(days=days - 1 - i)
        for i in range(days)
    ]

    rng = random.Random(seed)
    runs = []  # type: List[Any]
    for slot, (script, test_name, line, persona) in enumerate(_SPECS):
        for day_index, day in enumerate(day_list):
            weekday = day.weekday()
            result_name, duration, reason = persona(
                day_index, days, weekday, rng
            )
            # Stable 2-minute slot per test within the batch, plus jitter
            # seconds, so tests are interleaved but never collide.
            start = datetime.datetime(
                day.year, day.month, day.day, _BATCH_HOUR, 0, 0
            ) + datetime.timedelta(
                minutes=slot * 2,
                seconds=int(rng.random() * 60),
                microseconds=int(rng.random() * 1000000),
            )
            end = start + datetime.timedelta(seconds=duration)
            runs.append(
                RunRecord(
                    environment=ENVIRONMENT,
                    script=script,
                    test_name=test_name,
                    result=Result[result_name],
                    start_time=start,
                    end_time=end,
                    output=_output_text(
                        result_name, script, test_name, day, duration,
                        reason,
                    ),
                    source_link=_fake_source_link(script, line),
                    known_failure_reason=reason,
                    build=None,
                )
            )
    runs.sort(key=lambda rec: (rec.start_time, rec.script, rec.test_name))
    return runs


#: Environments the filler estate is spread across (weights sum to 1.0).
_FILLER_ENVS = [
    ("linux-sim", 0.60),
    ("linux-uat-sim", 0.25),
    ("win-sim", 0.15),
]

#: Tests per filler script (so 12,000 tests ~= 400 scripts).
_FILLER_TESTS_PER_SCRIPT = 30

_FILLER_AREAS = ["regression", "integration", "smoke", "nightly"]

_FILLER_VERBS = ["submit", "cancel", "replace", "load", "replay",
                 "reconcile", "snapshot", "publish", "expire", "match"]

_FILLER_NOUNS = ["record", "profile", "document", "message", "job",
                 "session", "batch", "index", "cache", "report"]


def _filler_env(rng):
    # type: (random.Random) -> str
    """Pick a filler environment by weight, deterministically."""
    roll = rng.random()
    cumulative = 0.0
    for env, weight in _FILLER_ENVS:
        cumulative += weight
        if roll < cumulative:
            return env
    return _FILLER_ENVS[-1][0]


def _filler_results(rng, days):
    # type: (random.Random, int) -> Tuple[List[str], Optional[str]]
    """Draw one filler test's per-day result names (oldest first).

    Distribution: ~96.5% steady passers, ~2% mildly flaky, ~0.9% live
    regressions (failing for the trailing 1-11 days), ~0.35% annotated
    known failures, ~0.25% known failures gone stale (UNEXPECTED_PASS
    for the trailing day or two). Returns the reason string for the
    annotated personas (None otherwise).
    """
    roll = rng.random()
    if roll < 0.965:
        return (["PASS"] * days, None)
    if roll < 0.985:
        return (
            ["FAIL" if rng.random() < 0.06 else "PASS"
             for _ in range(days)],
            None,
        )
    if roll < 0.994:
        fail_days = min(days, 1 + int(rng.random() * 11))
        return (
            ["PASS"] * (days - fail_days) + ["FAIL"] * fail_days,
            None,
        )
    reason = "TB-{0}: annotated known failure".format(
        100 + int(rng.random() * 900))
    if roll < 0.9975:
        return (["FAILED_AS_EXPECTED"] * days, reason)
    stale_days = min(days, 1 + int(rng.random() * 2))
    return (
        ["FAILED_AS_EXPECTED"] * (days - stale_days)
        + ["UNEXPECTED_PASS"] * stale_days,
        reason,
    )


def generate_filler_batches(
        total_tests, days=DEFAULT_DAYS, seed=DEFAULT_SEED, now=None):
    # type: (int, int, int, Optional[datetime.datetime]) -> Any
    """Yield batches of RunRecord for a large, realistic filler estate.

    Scales the demo to *total_tests* additional tests (on top of the
    eight hand-crafted personas) spread across several simulated
    environments, ~30 tests per script. Yields one batch per script so
    callers can upsert incrementally instead of holding
    ``total_tests * days`` records in memory. Deterministic for equal
    ``(total_tests, days, seed, now)``.
    """
    from testboard import model
    from testboard.model import Result, RunRecord

    if days < 1:
        raise ValueError("days must be >= 1, got {0}".format(days))
    if total_tests < 0:
        raise ValueError(
            "total_tests must be >= 0, got {0}".format(total_tests))
    if now is None:
        now = model.utcnow()

    last_day = now.date()
    if now.time() < datetime.time(_BATCH_HOUR + 1, 0):
        last_day = last_day - datetime.timedelta(days=1)
    day_list = [
        last_day - datetime.timedelta(days=days - 1 - i)
        for i in range(days)
    ]

    rng = random.Random(seed * 2 + 1)  # decoupled from the persona stream
    generated = 0
    script_index = 0
    while generated < total_tests:
        env = _filler_env(rng)
        area = _FILLER_AREAS[int(rng.random() * len(_FILLER_AREAS))]
        script = "{0}/suite_{1:03d}.py".format(area, script_index)
        in_script = min(
            _FILLER_TESTS_PER_SCRIPT, total_tests - generated)
        # A stable per-script slot inside the nightly batch window.
        slot_minutes = int(rng.random() * 170)
        batch = []  # type: List[Any]
        for test_index in range(in_script):
            verb = _FILLER_VERBS[int(rng.random() * len(_FILLER_VERBS))]
            noun = _FILLER_NOUNS[int(rng.random() * len(_FILLER_NOUNS))]
            test_name = "test_{0}_{1}_{2:02d}".format(
                verb, noun, test_index)
            result_names, reason = _filler_results(rng, days)
            # A few tests per thousand go silent (dropped from the
            # nightly batch days ago) so the "not run" tile and the
            # staleness filter have something real to show.
            silent_days = 0
            if rng.random() < 0.004:
                silent_days = min(days - 1, 2 + int(rng.random() * 4))
            base_duration = 0.4 + rng.random() * 4.0
            for day_index, day in enumerate(day_list):
                if day_index >= days - silent_days:
                    continue
                result_name = result_names[day_index]
                duration = base_duration + rng.random() * 0.4
                start = datetime.datetime(
                    day.year, day.month, day.day, _BATCH_HOUR, 0, 0
                ) + datetime.timedelta(
                    minutes=slot_minutes,
                    seconds=int(rng.random() * 60),
                    microseconds=int(rng.random() * 1000000),
                )
                batch.append(RunRecord(
                    environment=env,
                    script=script,
                    test_name=test_name,
                    result=Result[result_name],
                    start_time=start,
                    end_time=start + datetime.timedelta(seconds=duration),
                    output="{0} {1} ({2:.3f}s)\n".format(
                        result_name, test_name, duration),
                    source_link=_fake_source_link(
                        script, 10 + test_index * 12),
                    known_failure_reason=reason,
                    build=None,
                ))
        generated += in_script
        script_index += 1
        yield batch


def write_jsonl(runs, path):
    # type: (List[Any], str) -> None
    """Write *runs* to *path*, one transport-schema JSON object per line."""
    from testboard.model import run_record_to_dict

    with open(path, "w", encoding="utf-8") as handle:
        for rec in runs:
            handle.write(json.dumps(run_record_to_dict(rec)))
            handle.write("\n")


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the demo-data CLI."""
    parser = argparse.ArgumentParser(
        prog="generate_demo_data.py",
        description=(
            "Write deterministic simulated test-run history (environment "
            "'{0}') as transport-schema JSON lines, ready for "
            "run_feeder.py --reader jsonl.".format(ENVIRONMENT)
        ),
    )
    parser.add_argument(
        "--out", default="demo.jsonl", metavar="PATH",
        help="output JSON-lines file (default: %(default)s)")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, metavar="N",
        help="days of history to generate (default: %(default)s)")
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, metavar="N",
        help="random seed (default: %(default)s)")
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Run the demo-data CLI; returns the process exit code (0/2)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ — you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/generate_demo_data.py\n".format(
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

    try:
        runs = generate_runs(days=args.days, seed=args.seed)
    except ValueError as exc:
        sys.stderr.write("error: {0}\n".format(exc))
        return 2
    write_jsonl(runs, args.out)
    print(
        "Wrote {0} simulated runs ({1} tests x {2} days, environment "
        "'{3}') to {4}".format(
            len(runs), len(_SPECS), args.days, ENVIRONMENT, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
