"""Unit tests for testboard.analytics (pure functions, synthetic histories).

Covers the required checklist: steady pass, steady fail, alternating
(flaky), Monday-only failure day profile, FAILED_AS_EXPECTED not counted as
failure, UNEXPECTED_PASS tracked, streak/failing_since/
last_pass_before_failure, window truncation by days AND by count, empty
window, single run, duration median for even and odd counts, and the exact
JSON shape (including rounding) of analytics_to_dict.
"""

import datetime
import unittest
from typing import List, Optional, Sequence

from testboard import analytics
from testboard.analytics import (
    DurationStats,
    analytics_to_dict,
    compute_analytics,
    group_executions,
    select_window,
    summarize_rollup,
)
from testboard.model import Result, StoredRun
from testboard.storage import RollupCount

# A fixed, deterministic "now": Sunday 2026-07-26 12:00:00 UTC.
NOW = datetime.datetime(2026, 7, 26, 12, 0, 0)


def make_run(
    run_id: int,
    result: Result,
    start_time: datetime.datetime,
    duration: float = 1.0,
) -> StoredRun:
    """Build a StoredRun for one synthetic test with the given identity-free
    fields (list-context shape: output is None)."""
    return StoredRun(
        run_id=run_id,
        environment="linux-sim",
        script="regression/suite.py",
        test_name="test_thing",
        result=result,
        start_time=start_time,
        end_time=start_time + datetime.timedelta(seconds=duration),
        source_link="https://example.com/suite.py",
        known_failure_reason=None,
        output=None,
    )


def daily_history(
    results_newest_first: Sequence[Result],
    now: datetime.datetime = NOW,
    duration: float = 1.0,
) -> List[StoredRun]:
    """Build a newest-first history: one run per day ending yesterday.

    ``results_newest_first[0]`` is the newest run (now - 1 day), the next is
    two days ago, and so on. run_ids descend with age (newest has the
    highest id).
    """
    runs = []  # type: List[StoredRun]
    count = len(results_newest_first)
    for offset, result in enumerate(results_newest_first):
        start = now - datetime.timedelta(days=offset + 1)
        runs.append(make_run(count - offset, result, start, duration))
    return runs


class SelectWindowTest(unittest.TestCase):
    """select_window: truncation by age and by count."""

    def test_truncates_by_days(self) -> None:
        inside = make_run(2, Result.PASS, NOW - datetime.timedelta(days=89))
        outside = make_run(1, Result.PASS, NOW - datetime.timedelta(days=91))
        window = select_window([inside, outside], NOW)
        self.assertEqual(window, [inside])

    def test_day_boundary_is_inclusive(self) -> None:
        boundary = make_run(1, Result.PASS, NOW - datetime.timedelta(days=90))
        just_out = make_run(
            2,
            Result.PASS,
            NOW - datetime.timedelta(days=90, microseconds=1),
        )
        self.assertEqual(select_window([boundary], NOW), [boundary])
        self.assertEqual(select_window([just_out], NOW), [])

    def test_truncates_by_count_keeping_newest(self) -> None:
        runs = daily_history([Result.PASS] * 10)
        window = select_window(runs, NOW, max_runs=3)
        self.assertEqual(len(window), 3)
        self.assertEqual([r.run_id for r in window], [10, 9, 8])
        # Newest first is preserved.
        self.assertEqual(window, runs[:3])

    def test_age_filter_applies_before_count_cap(self) -> None:
        old = make_run(1, Result.PASS, NOW - datetime.timedelta(days=100))
        new = make_run(2, Result.PASS, NOW - datetime.timedelta(days=1))
        window = select_window([new, old], NOW, max_runs=1)
        self.assertEqual(window, [new])

    def test_custom_max_days(self) -> None:
        runs = daily_history([Result.PASS] * 10)
        window = select_window(runs, NOW, max_days=5)
        # Runs 1..5 days old are >= now - 5 days; the 5-day-old one is
        # exactly on the boundary and included.
        self.assertEqual([r.run_id for r in window], [10, 9, 8, 7, 6])

    def test_empty_input(self) -> None:
        self.assertEqual(select_window([], NOW), [])


class SteadyPassTest(unittest.TestCase):
    """A history of nothing but PASS."""

    def setUp(self) -> None:
        self.summary = compute_analytics(daily_history([Result.PASS] * 6), NOW)

    def test_no_failure_streak(self) -> None:
        self.assertIsNone(self.summary.failing_since)
        self.assertIsNone(self.summary.last_pass_before_failure)

    def test_flakiness_stable_pass(self) -> None:
        self.assertEqual(self.summary.flakiness.transitions, 0)
        self.assertEqual(self.summary.flakiness.score, 0.0)
        self.assertEqual(self.summary.flakiness.classification, "stable-pass")

    def test_window_bounds(self) -> None:
        self.assertEqual(self.summary.window_run_count, 6)
        self.assertEqual(self.summary.window_to, NOW)
        self.assertEqual(
            self.summary.window_from, NOW - datetime.timedelta(days=90)
        )


class SteadyFailTest(unittest.TestCase):
    """A history of nothing but FAIL."""

    def setUp(self) -> None:
        self.runs = daily_history([Result.FAIL] * 5)
        self.summary = compute_analytics(self.runs, NOW)

    def test_failing_since_is_oldest_run(self) -> None:
        assert self.summary.failing_since is not None
        self.assertEqual(self.summary.failing_since.run_id, 1)
        self.assertEqual(self.summary.failing_since, self.runs[-1])

    def test_no_last_pass_in_window(self) -> None:
        self.assertIsNone(self.summary.last_pass_before_failure)

    def test_flakiness_stable_fail(self) -> None:
        self.assertEqual(self.summary.flakiness.transitions, 0)
        self.assertEqual(self.summary.flakiness.score, 0.0)
        self.assertEqual(self.summary.flakiness.classification, "stable-fail")


class AlternatingFlakyTest(unittest.TestCase):
    """Strictly alternating FAIL/PASS is maximally flaky."""

    def setUp(self) -> None:
        # Newest first: F P F P F P F P F P (10 runs).
        results = [Result.FAIL, Result.PASS] * 5
        self.runs = daily_history(results)
        self.summary = compute_analytics(self.runs, NOW)

    def test_transitions_and_score(self) -> None:
        self.assertEqual(self.summary.flakiness.transitions, 9)
        self.assertAlmostEqual(self.summary.flakiness.score, 0.9)

    def test_classified_flaky(self) -> None:
        self.assertEqual(self.summary.flakiness.classification, "flaky")

    def test_streak_is_just_the_newest_run(self) -> None:
        assert self.summary.failing_since is not None
        self.assertEqual(self.summary.failing_since.run_id, 10)
        assert self.summary.last_pass_before_failure is not None
        self.assertEqual(self.summary.last_pass_before_failure.run_id, 9)

    def test_threshold_is_inclusive(self) -> None:
        # 1 transition over 5 runs = 0.2 exactly -> flaky at the default
        # threshold (score >= threshold).
        results = [
            Result.PASS,
            Result.PASS,
            Result.PASS,
            Result.PASS,
            Result.FAIL,
        ]
        summary = compute_analytics(daily_history(results), NOW)
        self.assertAlmostEqual(summary.flakiness.score, 0.2)
        self.assertEqual(summary.flakiness.classification, "flaky")
        # Raising the threshold reclassifies as stable-pass (newest is PASS).
        summary = compute_analytics(
            daily_history(results), NOW, flaky_threshold=0.5
        )
        self.assertEqual(summary.flakiness.classification, "stable-pass")


class StreakTest(unittest.TestCase):
    """failing_since / last_pass_before_failure on mixed histories."""

    def test_streak_of_three_with_pass_before(self) -> None:
        # Newest first: F F F P F P
        results = [
            Result.FAIL,
            Result.FAIL,
            Result.FAIL,
            Result.PASS,
            Result.FAIL,
            Result.PASS,
        ]
        runs = daily_history(results)
        summary = compute_analytics(runs, NOW)
        assert summary.failing_since is not None
        self.assertEqual(summary.failing_since.run_id, 4)
        assert summary.last_pass_before_failure is not None
        self.assertEqual(summary.last_pass_before_failure.run_id, 3)

    def test_no_streak_when_latest_not_fail(self) -> None:
        # Newest first: P F F — latest run passed, so no current streak.
        results = [Result.PASS, Result.FAIL, Result.FAIL]
        summary = compute_analytics(daily_history(results), NOW)
        self.assertIsNone(summary.failing_since)
        self.assertIsNone(summary.last_pass_before_failure)

    def test_failed_as_expected_breaks_streak(self) -> None:
        # Newest first: F F X F — FAILED_AS_EXPECTED is not a failure, so
        # the streak is only the two newest runs.
        results = [
            Result.FAIL,
            Result.FAIL,
            Result.FAILED_AS_EXPECTED,
            Result.FAIL,
        ]
        summary = compute_analytics(daily_history(results), NOW)
        assert summary.failing_since is not None
        self.assertEqual(summary.failing_since.run_id, 3)
        # No PASS anywhere in the window.
        self.assertIsNone(summary.last_pass_before_failure)

    def test_last_pass_skips_non_pass_results(self) -> None:
        # Newest first: F X U P — the most recent PASS older than the streak
        # is the oldest run; FAILED_AS_EXPECTED and UNEXPECTED_PASS between
        # do not qualify (and also do not extend the streak).
        results = [
            Result.FAIL,
            Result.FAILED_AS_EXPECTED,
            Result.UNEXPECTED_PASS,
            Result.PASS,
        ]
        summary = compute_analytics(daily_history(results), NOW)
        assert summary.failing_since is not None
        self.assertEqual(summary.failing_since.run_id, 4)
        assert summary.last_pass_before_failure is not None
        self.assertEqual(summary.last_pass_before_failure.run_id, 1)

    def test_streak_confined_to_window(self) -> None:
        # A FAIL streak that extends past the count cap: failing_since is
        # the oldest FAIL *inside* the window.
        runs = daily_history([Result.FAIL] * 8)
        summary = compute_analytics(runs, NOW, max_runs=3)
        assert summary.failing_since is not None
        self.assertEqual(summary.failing_since.run_id, 6)
        self.assertIsNone(summary.last_pass_before_failure)


class NonFailureResultsTest(unittest.TestCase):
    """FAILED_AS_EXPECTED is non-failure; UNEXPECTED_PASS tracked."""

    def test_failed_as_expected_is_passing_for_flakiness(self) -> None:
        # Newest first: X X X — all "passing" states, zero transitions.
        results = [Result.FAILED_AS_EXPECTED] * 3
        summary = compute_analytics(daily_history(results), NOW)
        self.assertEqual(summary.flakiness.transitions, 0)
        self.assertEqual(summary.flakiness.classification, "stable-pass")
        self.assertIsNone(summary.failing_since)

    def test_transition_only_on_fail_boundary(self) -> None:
        # Newest first: F X P U — oldest-to-newest U P X F: the only state
        # change is X -> F (all of U, P, X are "passing").
        results = [
            Result.FAIL,
            Result.FAILED_AS_EXPECTED,
            Result.PASS,
            Result.UNEXPECTED_PASS,
        ]
        summary = compute_analytics(daily_history(results), NOW)
        self.assertEqual(summary.flakiness.transitions, 1)

    def test_day_profile_counts(self) -> None:
        # All runs on the same weekday: 1 FAIL, 1 FAILED_AS_EXPECTED,
        # 1 UNEXPECTED_PASS, 1 PASS -> 4 runs, 1 failure, rate 0.25,
        # 1 unexpected pass.
        monday = datetime.datetime(2026, 7, 20, 2, 0, 0)
        self.assertEqual(monday.weekday(), 0)
        runs = [
            make_run(4, Result.FAIL, monday + datetime.timedelta(hours=3)),
            make_run(
                3,
                Result.FAILED_AS_EXPECTED,
                monday + datetime.timedelta(hours=2),
            ),
            make_run(
                2,
                Result.UNEXPECTED_PASS,
                monday + datetime.timedelta(hours=1),
            ),
            make_run(1, Result.PASS, monday),
        ]
        summary = compute_analytics(runs, NOW)
        mon = summary.day_of_week[0]
        self.assertEqual(mon.day, "Mon")
        self.assertEqual(mon.runs, 4)
        self.assertEqual(mon.failures, 1)
        self.assertAlmostEqual(mon.failure_rate, 0.25)
        self.assertEqual(mon.unexpected_passes, 1)


class MondayFailureProfileTest(unittest.TestCase):
    """Day-of-week profile: a test that only fails on Mondays."""

    def setUp(self) -> None:
        # Daily runs for 21 days ending yesterday (2026-07-25, a Saturday):
        # FAIL on Mondays, PASS otherwise. That is exactly 3 of each
        # weekday, 3 Monday failures.
        runs = []  # type: List[StoredRun]
        for offset in range(1, 22):
            start = NOW - datetime.timedelta(days=offset)
            result = Result.FAIL if start.weekday() == 0 else Result.PASS
            runs.append(make_run(100 - offset, result, start))
        self.summary = compute_analytics(runs, NOW)

    def test_seven_entries_monday_first(self) -> None:
        self.assertEqual(len(self.summary.day_of_week), 7)
        self.assertEqual(
            [d.day for d in self.summary.day_of_week],
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        )

    def test_monday_fails_other_days_pass(self) -> None:
        mon = self.summary.day_of_week[0]
        self.assertEqual(mon.runs, 3)
        self.assertEqual(mon.failures, 3)
        self.assertEqual(mon.failure_rate, 1.0)
        self.assertEqual(mon.unexpected_passes, 0)
        for day in self.summary.day_of_week[1:]:
            self.assertEqual(day.runs, 3)
            self.assertEqual(day.failures, 0)
            self.assertEqual(day.failure_rate, 0.0)

    def test_days_with_no_runs_have_zero_rate(self) -> None:
        # Single Monday run only: every other day has runs=0, rate 0.0.
        monday = datetime.datetime(2026, 7, 20, 2, 0, 0)
        summary = compute_analytics([make_run(1, Result.FAIL, monday)], NOW)
        self.assertEqual(summary.day_of_week[0].runs, 1)
        self.assertEqual(summary.day_of_week[0].failure_rate, 1.0)
        for day in summary.day_of_week[1:]:
            self.assertEqual(day.runs, 0)
            self.assertEqual(day.failures, 0)
            self.assertEqual(day.failure_rate, 0.0)


class WindowTruncationTest(unittest.TestCase):
    """compute_analytics applies the window before every statistic."""

    def test_old_runs_excluded_by_days(self) -> None:
        recent = make_run(
            2, Result.PASS, NOW - datetime.timedelta(days=1), duration=2.0
        )
        ancient = make_run(
            1, Result.FAIL, NOW - datetime.timedelta(days=120), duration=50.0
        )
        summary = compute_analytics([recent, ancient], NOW)
        self.assertEqual(summary.window_run_count, 1)
        self.assertIsNone(summary.failing_since)
        assert summary.duration is not None
        self.assertEqual(summary.duration.max, 2.0)
        self.assertEqual(sum(d.failures for d in summary.day_of_week), 0)

    def test_truncation_by_count(self) -> None:
        # 10 runs, newest 3 kept: the older FAILs fall outside the window.
        results = [Result.PASS] * 3 + [Result.FAIL] * 7
        summary = compute_analytics(daily_history(results), NOW, max_runs=3)
        self.assertEqual(summary.window_run_count, 3)
        self.assertEqual(summary.flakiness.transitions, 0)
        self.assertEqual(summary.flakiness.classification, "stable-pass")
        self.assertEqual(sum(d.runs for d in summary.day_of_week), 3)
        self.assertEqual(sum(d.failures for d in summary.day_of_week), 0)


class EmptyAndSingleWindowTest(unittest.TestCase):
    """Empty window and single-run window edge cases."""

    def test_empty_window(self) -> None:
        summary = compute_analytics([], NOW)
        self.assertEqual(summary.window_run_count, 0)
        self.assertIsNone(summary.failing_since)
        self.assertIsNone(summary.last_pass_before_failure)
        self.assertEqual(summary.flakiness.transitions, 0)
        self.assertEqual(summary.flakiness.score, 0.0)
        self.assertEqual(summary.flakiness.classification, "no-data")
        self.assertEqual(len(summary.day_of_week), 7)
        for day in summary.day_of_week:
            self.assertEqual(
                (day.runs, day.failures, day.failure_rate,
                 day.unexpected_passes),
                (0, 0, 0.0, 0),
            )
        self.assertIsNone(summary.duration)

    def test_all_runs_too_old_is_no_data(self) -> None:
        old = make_run(1, Result.FAIL, NOW - datetime.timedelta(days=365))
        summary = compute_analytics([old], NOW)
        self.assertEqual(summary.window_run_count, 0)
        self.assertEqual(summary.flakiness.classification, "no-data")
        self.assertIsNone(summary.duration)

    def test_single_passing_run(self) -> None:
        run = make_run(
            1, Result.PASS, NOW - datetime.timedelta(days=1), duration=2.5
        )
        summary = compute_analytics([run], NOW)
        self.assertEqual(summary.window_run_count, 1)
        self.assertEqual(summary.flakiness.transitions, 0)
        self.assertEqual(summary.flakiness.score, 0.0)
        self.assertEqual(summary.flakiness.classification, "stable-pass")
        self.assertIsNone(summary.failing_since)
        self.assertEqual(
            summary.duration, DurationStats(min=2.5, median=2.5, max=2.5)
        )

    def test_single_failing_run(self) -> None:
        run = make_run(7, Result.FAIL, NOW - datetime.timedelta(days=1))
        summary = compute_analytics([run], NOW)
        self.assertEqual(summary.flakiness.classification, "stable-fail")
        assert summary.failing_since is not None
        self.assertEqual(summary.failing_since.run_id, 7)
        self.assertIsNone(summary.last_pass_before_failure)


class DurationTest(unittest.TestCase):
    """Duration min/median/max, including even/odd median counts."""

    def _history(self, durations: Sequence[float]) -> List[StoredRun]:
        runs = []  # type: List[StoredRun]
        for offset, dur in enumerate(durations):
            start = NOW - datetime.timedelta(days=offset + 1)
            runs.append(
                make_run(len(durations) - offset, Result.PASS, start, dur)
            )
        return runs

    def test_median_odd_count(self) -> None:
        summary = compute_analytics(self._history([9.0, 1.0, 3.0]), NOW)
        self.assertEqual(
            summary.duration, DurationStats(min=1.0, median=3.0, max=9.0)
        )

    def test_median_even_count(self) -> None:
        # statistics.median averages the middle two: (2.0 + 4.0) / 2 = 3.0.
        summary = compute_analytics(
            self._history([10.0, 2.0, 4.0, 1.0]), NOW
        )
        self.assertEqual(
            summary.duration, DurationStats(min=1.0, median=3.0, max=10.0)
        )

    def test_all_results_count_toward_duration(self) -> None:
        # Duration covers ALL runs in the window, failures included.
        runs = [
            make_run(
                2,
                Result.FAIL,
                NOW - datetime.timedelta(days=1),
                duration=100.0,
            ),
            make_run(
                1,
                Result.PASS,
                NOW - datetime.timedelta(days=2),
                duration=1.0,
            ),
        ]
        summary = compute_analytics(runs, NOW)
        self.assertEqual(
            summary.duration,
            DurationStats(min=1.0, median=50.5, max=100.0),
        )


class AnalyticsToDictTest(unittest.TestCase):
    """Exact JSON shape and rounding of analytics_to_dict."""

    def test_empty_window_json(self) -> None:
        payload = analytics_to_dict(compute_analytics([], NOW))
        self.assertEqual(
            sorted(payload.keys()),
            [
                "by_day",
                "day_of_week",
                "duration_seconds",
                "failing_since",
                "flakiness",
                "last_pass_before_failure",
                "window",
            ],
        )
        self.assertEqual(payload["by_day"], [])
        self.assertEqual(
            payload["window"],
            {
                "max_days": 90,
                "max_runs": 200,
                "run_count": 0,
                "from": "2026-04-27T12:00:00.000000",
                "to": "2026-07-26T12:00:00.000000",
            },
        )
        self.assertIsNone(payload["failing_since"])
        self.assertIsNone(payload["last_pass_before_failure"])
        self.assertEqual(
            payload["flakiness"],
            {"transitions": 0, "score": 0.0, "classification": "no-data"},
        )
        self.assertIsNone(payload["duration_seconds"])
        self.assertEqual(len(payload["day_of_week"]), 7)
        self.assertEqual(
            payload["day_of_week"][0],
            {
                "day": "Mon",
                "runs": 0,
                "failures": 0,
                "failure_rate": 0.0,
                "unexpected_passes": 0,
            },
        )

    def test_run_refs_and_rounding(self) -> None:
        # Newest first: F F P — streak of 2, one transition over 3 runs.
        start_newest = datetime.datetime(2026, 7, 25, 2, 0, 0)
        runs = [
            make_run(30, Result.FAIL, start_newest, duration=9.8765),
            make_run(
                20,
                Result.FAIL,
                start_newest - datetime.timedelta(days=1),
                duration=3.4,
            ),
            make_run(
                10,
                Result.PASS,
                start_newest - datetime.timedelta(days=2),
                duration=1.2344,
            ),
        ]
        payload = analytics_to_dict(compute_analytics(runs, NOW))
        self.assertEqual(
            payload["failing_since"],
            {
                "run_id": 20,
                "result": "FAIL",
                "start_time": "2026-07-24T02:00:00.000000",
            },
        )
        self.assertEqual(
            payload["last_pass_before_failure"],
            {
                "run_id": 10,
                "result": "PASS",
                "start_time": "2026-07-23T02:00:00.000000",
            },
        )
        # score = 1/3 -> 0.3333 at 4 dp.
        self.assertEqual(payload["flakiness"]["score"], 0.3333)
        self.assertEqual(payload["flakiness"]["transitions"], 1)
        self.assertEqual(payload["flakiness"]["classification"], "flaky")
        # Durations rounded to 3 dp.
        self.assertEqual(
            payload["duration_seconds"],
            {"min": 1.234, "median": 3.4, "max": 9.877},
        )

    def test_failure_rate_rounded_to_4dp(self) -> None:
        # 7 runs on one weekday, 2 failures newest-first -> 2/7 = 0.2857.
        monday = datetime.datetime(2026, 7, 20, 2, 0, 0)
        self.assertEqual(monday.weekday(), 0)
        runs = []  # type: List[StoredRun]
        for i in range(7):
            result = Result.FAIL if i < 2 else Result.PASS
            runs.append(
                make_run(
                    7 - i,
                    result,
                    monday + datetime.timedelta(hours=6 - i),
                )
            )
        payload = analytics_to_dict(compute_analytics(runs, NOW))
        mon = payload["day_of_week"][0]
        self.assertEqual(mon["runs"], 7)
        self.assertEqual(mon["failures"], 2)
        self.assertEqual(mon["failure_rate"], 0.2857)
        # score = 1 transition / 7 runs -> 0.1429.
        self.assertEqual(payload["flakiness"]["score"], 0.1429)

    def test_non_default_window_echo(self) -> None:
        summary = compute_analytics([], NOW, max_days=30, max_runs=50)
        payload = analytics_to_dict(summary, max_runs=50)
        self.assertEqual(payload["window"]["max_days"], 30)
        self.assertEqual(payload["window"]["max_runs"], 50)
        self.assertEqual(payload["window"]["from"], "2026-06-26T12:00:00.000000")


class ByDayTest(unittest.TestCase):
    """The per-day result series behind the second detail-page chart."""

    def test_counts_each_result_on_its_own_day(self) -> None:
        history = daily_history(
            [Result.FAIL, Result.PASS, Result.PASS]
        )
        by_day = compute_analytics(history, NOW).by_day
        self.assertEqual(len(by_day), 3)
        # Oldest first, so time reads left to right in the chart.
        self.assertEqual(
            [d.day for d in by_day], sorted(d.day for d in by_day)
        )
        self.assertEqual(by_day[-1].results[Result.FAIL], 1)
        self.assertEqual(by_day[0].results[Result.PASS], 1)
        self.assertTrue(all(d.total == 1 for d in by_day))

    def test_several_runs_in_one_day_share_a_column(self) -> None:
        """A suite that runs twice a day gives one column with both runs."""
        morning = NOW - datetime.timedelta(days=1, hours=10)
        evening = NOW - datetime.timedelta(days=1, hours=2)
        runs = [
            make_run(run_id=2, result=Result.PASS, start_time=evening),
            make_run(run_id=1, result=Result.FAIL, start_time=morning),
        ]
        by_day = compute_analytics(runs, NOW).by_day
        self.assertEqual(len(by_day), 1)
        self.assertEqual(by_day[0].total, 2)
        self.assertEqual(by_day[0].results[Result.FAIL], 1)
        self.assertEqual(by_day[0].results[Result.PASS], 1)

    def test_days_with_no_run_are_visible_gaps(self) -> None:
        """A test that stopped running for a week should show the hole."""
        runs = [
            make_run(run_id=2, result=Result.PASS,
                     start_time=NOW - datetime.timedelta(days=1)),
            make_run(run_id=1, result=Result.PASS,
                     start_time=NOW - datetime.timedelta(days=5)),
        ]
        by_day = compute_analytics(runs, NOW).by_day
        self.assertEqual(len(by_day), 5)          # inclusive span
        self.assertEqual([d.total for d in by_day], [1, 0, 0, 0, 1])

    def test_window_is_not_padded_to_its_full_length(self) -> None:
        """Only the observed span is filled, not all 90 days."""
        runs = [make_run(run_id=1, result=Result.PASS,
                         start_time=NOW - datetime.timedelta(days=2))]
        self.assertEqual(len(compute_analytics(runs, NOW).by_day), 1)

    def test_empty_window(self) -> None:
        self.assertEqual(compute_analytics([], NOW).by_day, [])


class GroupExecutionsTest(unittest.TestCase):
    """Inferring "the suite ran" from the timings of individual runs."""

    BASE = datetime.datetime(2026, 7, 25, 2, 0, 0)

    def runs(self, offsets_minutes: Sequence[float],
             results: Optional[Sequence[Result]] = None,
             duration_seconds: float = 30.0) -> List[StoredRun]:
        """Build runs starting at BASE + each offset, in the given order."""
        built = []  # type: List[StoredRun]
        for index, offset in enumerate(offsets_minutes):
            built.append(make_run(
                run_id=index + 1,
                result=(results[index] if results else Result.PASS),
                start_time=self.BASE + datetime.timedelta(minutes=offset),
                duration=duration_seconds,
            ))
        return built

    def test_runs_close_together_are_one_execution(self) -> None:
        executions = group_executions(self.runs([0, 5, 10, 15]))
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].total, 4)
        self.assertEqual(executions[0].started, self.BASE)

    def test_a_long_quiet_period_starts_a_new_execution(self) -> None:
        """The case the daily chart cannot show: two runs in one day."""
        executions = group_executions(
            self.runs([0, 5, 10, 720, 725, 730])   # 02:00 and 14:00
        )
        self.assertEqual(len(executions), 2)
        self.assertEqual([e.total for e in executions], [3, 3])
        self.assertEqual(
            executions[1].started,
            self.BASE + datetime.timedelta(minutes=720),
        )

    def test_a_slow_test_does_not_split_its_own_execution(self) -> None:
        """The gap is measured from the last END, not the last start.

        A test that runs for two hours would otherwise look like a quiet
        period and cut the execution in half.
        """
        runs = self.runs([0, 5], duration_seconds=2 * 3600)
        runs.extend(self.runs([130]))
        executions = group_executions(runs)
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].total, 3)

    def test_execution_span_covers_first_start_to_last_end(self) -> None:
        executions = group_executions(
            self.runs([0, 5, 10], duration_seconds=60)
        )
        self.assertEqual(executions[0].started, self.BASE)
        self.assertEqual(
            executions[0].ended,
            self.BASE + datetime.timedelta(minutes=11),
        )
        self.assertEqual(executions[0].duration_seconds, 660.0)

    def test_results_are_counted_per_execution(self) -> None:
        executions = group_executions(self.runs(
            [0, 5, 10, 720, 725],
            results=[Result.PASS, Result.FAIL, Result.FAIL,
                     Result.PASS, Result.UNEXPECTED_PASS],
        ))
        self.assertEqual(executions[0].failed, 2)
        self.assertEqual(executions[0].results[Result.PASS], 1)
        self.assertEqual(executions[1].failed, 0)
        self.assertEqual(
            executions[1].results[Result.UNEXPECTED_PASS], 1)

    def test_gap_is_configurable(self) -> None:
        runs = self.runs([0, 90])
        self.assertEqual(len(group_executions(runs)), 2)
        self.assertEqual(len(group_executions(runs, gap_minutes=120)), 1)

    def test_single_run(self) -> None:
        executions = group_executions(self.runs([0]))
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].total, 1)

    def test_no_runs(self) -> None:
        self.assertEqual(group_executions([]), [])


def cell(
    result: Result,
    prev_result: Optional[Result] = None,
    count: int = 1,
    environment: str = "linux-sim",
    recent: bool = True,
    retired: bool = False,
) -> RollupCount:
    """Build one GROUP BY cell of the estate rollup."""
    return RollupCount(
        environment=environment,
        result=result,
        prev_result=prev_result,
        recent=recent,
        retired=retired,
        count=count,
    )


class SummarizeRollupTest(unittest.TestCase):
    """summarize_rollup: headline counts derived from grouped counts."""

    def counts(self) -> List[RollupCount]:
        """A small estate exercising every classification at once.

        Seven tests, one per cell: steady pass, a new failure, a
        first-run failure, a still-failing test, a fixed test, a stale
        annotation, and one test in another environment that has not run.
        """
        return [
            cell(Result.PASS, Result.PASS),
            cell(Result.FAIL, Result.PASS),
            cell(Result.FAIL, None),
            cell(Result.FAIL, Result.FAIL),
            cell(Result.PASS, Result.FAIL),
            cell(Result.UNEXPECTED_PASS, Result.FAILED_AS_EXPECTED),
            cell(Result.PASS, Result.PASS, environment="win-sim",
                 recent=False),
        ]

    def test_status_counts(self) -> None:
        status = summarize_rollup(self.counts(), assigned_open=2).status
        self.assertEqual(status.total_tests, 7)
        self.assertEqual(status.ran_recently, 6)
        self.assertEqual(status.not_run, 1)
        self.assertEqual(status.results[Result.PASS], 3)
        self.assertEqual(status.results[Result.FAIL], 3)
        self.assertEqual(status.results[Result.UNEXPECTED_PASS], 1)
        self.assertEqual(status.recent_results[Result.PASS], 2)
        self.assertEqual(status.recent_results[Result.FAIL], 3)
        self.assertEqual(status.new_failures, 2)
        self.assertEqual(status.still_failing, 1)
        self.assertEqual(status.fixed, 1)
        self.assertEqual(status.assigned_open, 2)

    def test_cells_contribute_their_whole_count(self) -> None:
        """A cell stands for many tests — every bucket adds count, not 1."""
        status = summarize_rollup([
            cell(Result.FAIL, Result.PASS, count=40),
            cell(Result.FAIL, Result.FAIL, count=25),
            cell(Result.PASS, Result.FAIL, count=7),
            cell(Result.PASS, Result.PASS, count=8000, recent=False),
        ]).status
        self.assertEqual(status.total_tests, 8072)
        self.assertEqual(status.new_failures, 40)
        self.assertEqual(status.still_failing, 25)
        self.assertEqual(status.fixed, 7)
        self.assertEqual(status.ran_recently, 72)
        self.assertEqual(status.not_run, 8000)
        self.assertEqual(status.results[Result.FAIL], 65)

    def test_fixed_counts_any_non_fail_result(self) -> None:
        """Fixed means "was FAIL, now isn't" — not necessarily a PASS."""
        status = summarize_rollup([
            cell(Result.FAILED_AS_EXPECTED, Result.FAIL),
            cell(Result.UNEXPECTED_PASS, Result.FAIL),
        ]).status
        self.assertEqual(status.fixed, 2)
        self.assertEqual(status.new_failures, 0)

    def test_by_environment_rollup(self) -> None:
        rollups = summarize_rollup(self.counts()).by_environment
        self.assertEqual(
            [r.environment for r in rollups], ["linux-sim", "win-sim"]
        )
        linux = rollups[0]
        self.assertEqual(linux.total_tests, 6)
        self.assertEqual(linux.failed, 3)
        self.assertEqual(linux.new_failures, 2)
        self.assertEqual(linux.unexpected_passes, 1)
        self.assertEqual(linux.not_run, 0)
        win = rollups[1]
        self.assertEqual((win.total_tests, win.failed, win.not_run),
                         (1, 0, 1))

    def test_retired_tests_are_counted_only_as_retired(self) -> None:
        """An approved-gone test leaves the board entirely."""
        status = summarize_rollup([
            cell(Result.PASS, Result.PASS, count=10),
            # Retired tests: stale, failing, whatever — none of it counts.
            cell(Result.FAIL, Result.FAIL, count=3, recent=False,
                 retired=True),
            cell(Result.PASS, Result.PASS, count=2, recent=False,
                 retired=True),
        ]).status
        self.assertEqual(status.retired, 5)
        self.assertEqual(status.total_tests, 10)
        self.assertEqual(status.not_run, 0)
        self.assertEqual(status.still_failing, 0)
        self.assertEqual(status.results[Result.FAIL], 0)

    def test_retired_tests_leave_the_environment_rollup(self) -> None:
        rollups = summarize_rollup([
            cell(Result.PASS, Result.PASS, count=4),
            cell(Result.FAIL, Result.PASS, count=9, environment="win-sim",
                 retired=True),
        ]).by_environment
        self.assertEqual([r.environment for r in rollups], ["linux-sim"])

    def test_empty_estate(self) -> None:
        summary = summarize_rollup([])
        self.assertEqual(summary.status.total_tests, 0)
        self.assertEqual(summary.status.retired, 0)
        self.assertEqual(summary.status.results[Result.FAIL], 0)
        self.assertEqual(summary.status.assigned_open, 0)
        self.assertEqual(summary.by_environment, [])


if __name__ == "__main__":
    unittest.main()


class TestStabilityOf(unittest.TestCase):
    """The list-view stability signal (WP-8).

    It exists so a row can say "broken since Tuesday" rather than "last
    passed Tuesday" — a date alone cannot separate a regression from a
    test that fails one night in three, and those need different
    responses.
    """

    P = Result.PASS
    F = Result.FAIL

    def test_a_clean_break_is_not_flaky(self) -> None:
        stability = analytics.stability_of(
            [self.P] * 10 + [self.F] * 10)
        self.assertEqual(stability.classification, "stable-fail")
        self.assertEqual(stability.transitions, 1)

    def test_alternating_results_are_flaky(self) -> None:
        stability = analytics.stability_of([self.P, self.F] * 8)
        self.assertEqual(stability.classification, "flaky")

    def test_all_passing_is_stable(self) -> None:
        self.assertEqual(
            analytics.stability_of([self.P] * 5).classification,
            "stable-pass")

    def test_no_runs_is_no_data(self) -> None:
        stability = analytics.stability_of([])
        self.assertEqual(stability.classification, "no-data")
        self.assertEqual(stability.score, 0.0)

    def test_a_single_run_scores_zero(self) -> None:
        self.assertEqual(analytics.stability_of([self.F]).score, 0.0)

    def test_it_agrees_with_the_detail_page_calculation(self) -> None:
        """Two definitions of "flaky" that can disagree is worse than
        one that is imperfect. This asserts they cannot.

        _compute_flakiness takes whole runs newest-first; stability_of
        takes bare results oldest-first. Same window, same verdict.
        """
        base = datetime.datetime(2026, 7, 1, 2, 0, 0)
        for pattern in (
            [self.P] * 6,
            [self.F] * 6,
            [self.P, self.F] * 4,
            [self.P] * 4 + [self.F] * 4,
            [self.P, self.P, self.F, self.P, self.F, self.F],
        ):
            runs = [
                StoredRun(
                    run_id=index,
                    environment="e", script="s", test_name="t",
                    result=result,
                    start_time=base + datetime.timedelta(days=index),
                    end_time=base + datetime.timedelta(
                        days=index, seconds=1),
                    source_link="", known_failure_reason=None,
                    output="",
                )
                for index, result in enumerate(pattern)
            ]
            newest_first = list(reversed(runs))
            detail = analytics._compute_flakiness(
                newest_first, analytics.DEFAULT_FLAKY_THRESHOLD)
            listed = analytics.stability_of(pattern)
            self.assertEqual(
                (detail.transitions, detail.classification),
                (listed.transitions, listed.classification),
                "disagreement on pattern {0}".format(
                    [r.name for r in pattern]))


def hourly(
    environment: str,
    start: datetime.datetime,
    hours: int,
    per_hour: int,
) -> List[tuple]:
    """Activity buckets for one contiguous block of running."""
    return [
        (environment, start + datetime.timedelta(hours=offset), per_hour)
        for offset in range(hours)
    ]


class TestFindPasses(unittest.TestCase):
    """Blocks of activity, per environment, and whether each is a pass.

    Both halves come from how the suite really runs: environments go
    SEQUENTIALLY, so they cannot share one timeline; and a failed run is
    followed by ad-hoc re-runs, which are blocks of activity that are
    not passes of the suite.
    """

    def _nights(self, environment: str, nights: int, per_night: int,
                hour: int = 2) -> List[tuple]:
        buckets = []  # type: List[tuple]
        for night in range(nights):
            start = (NOW - datetime.timedelta(days=nights - night)).replace(
                hour=hour, minute=0, second=0, microsecond=0)
            buckets.extend(hourly(environment, start, 2, per_night // 2))
        return buckets

    def test_a_quiet_gap_starts_a_new_pass(self) -> None:
        passes = analytics.find_passes(
            self._nights("linux", 3, 100), {"linux": 100},
            gap_hours=6.0, coverage=0.5)
        self.assertEqual(len(passes), 3)
        self.assertTrue(all(entry.covered for entry in passes))
        self.assertEqual([entry.runs for entry in passes], [100, 100, 100])

    def test_a_pause_shorter_than_the_gap_stays_one_pass(self) -> None:
        start = NOW - datetime.timedelta(hours=10)
        buckets = [
            ("linux", start, 50),
            ("linux", start + datetime.timedelta(hours=5), 50),
        ]
        passes = analytics.find_passes(
            buckets, {"linux": 100}, gap_hours=6.0, coverage=0.5)
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].runs, 100)

    def test_environments_are_grouped_separately(self) -> None:
        """They run sequentially. On one shared timeline whichever ran
        first looks stale for the rest of the morning."""
        buckets = (
            self._nights("linux", 2, 100, hour=1)
            + self._nights("win", 2, 100, hour=5)
        )
        passes = analytics.find_passes(
            buckets, {"linux": 100, "win": 100},
            gap_hours=6.0, coverage=0.5)
        self.assertEqual(
            sorted({entry.environment for entry in passes}),
            ["linux", "win"])
        self.assertEqual(len(passes), 4)

    def test_an_ad_hoc_re_run_is_not_a_pass(self) -> None:
        """The failure mode that made coverage necessary: without it, a
        twenty-test re-run after a fix drags the line to this afternoon
        and flags the entire estate."""
        buckets = self._nights("linux", 2, 100)
        buckets.append(("linux", NOW - datetime.timedelta(hours=1), 20))
        passes = analytics.find_passes(
            buckets, {"linux": 100}, gap_hours=6.0, coverage=0.5)
        self.assertFalse(passes[-1].covered)
        self.assertEqual(passes[-1].runs, 20)

    def test_an_environment_with_no_recorded_tests_still_needs_one_run(
        self
    ) -> None:
        """max(1, ...) - a zero denominator must not make an empty block
        a covered pass by arithmetic."""
        passes = analytics.find_passes(
            [("new-env", NOW - datetime.timedelta(hours=2), 1)],
            {}, gap_hours=6.0, coverage=0.5)
        self.assertTrue(passes[0].covered)

    def test_no_activity_is_no_passes(self) -> None:
        self.assertEqual(
            analytics.find_passes([], {"linux": 10}, 6.0, 0.5), [])


class TestEffectiveTestCounts(unittest.TestCase):
    """Declared beats inferred, in both directions."""

    def test_a_declaration_wins_even_when_it_is_larger(self) -> None:
        """The case inference cannot reach: an environment that has
        never once reported in full would otherwise be judged against
        its own shortfall, and every partial run would be a pass."""
        self.assertEqual(
            analytics.effective_test_counts(
                {"linux": 400}, {"linux": 900}),
            {"linux": 900})

    def test_a_declaration_wins_when_it_is_smaller(self) -> None:
        self.assertEqual(
            analytics.effective_test_counts(
                {"linux": 400}, {"linux": 100}),
            {"linux": 100})

    def test_an_undeclared_environment_is_untouched(self) -> None:
        """What makes this additive: it cannot change the behaviour of
        an environment nobody has configured."""
        self.assertEqual(
            analytics.effective_test_counts(
                {"linux": 400, "win": 20}, {"linux": 900}),
            {"linux": 900, "win": 20})

    def test_a_declaration_for_an_unseen_environment_is_kept(self) -> None:
        self.assertEqual(
            analytics.effective_test_counts({}, {"new": 5}), {"new": 5})

    def test_the_inputs_are_not_mutated(self) -> None:
        inferred = {"linux": 400}
        analytics.effective_test_counts(inferred, {"linux": 900})
        self.assertEqual(inferred, {"linux": 400})

    def test_a_declaration_changes_which_blocks_are_passes(self) -> None:
        """The whole point, end to end: the same activity, judged
        against a declared denominator, stops counting as a pass."""
        buckets = [("linux", NOW - datetime.timedelta(hours=3), 300)]
        inferred = {"linux": 400}
        self.assertTrue(analytics.find_passes(
            buckets, inferred, 6.0, 0.5)[0].covered)
        declared = analytics.effective_test_counts(inferred, {"linux": 900})
        self.assertFalse(analytics.find_passes(
            buckets, declared, 6.0, 0.5)[0].covered)


class TestRecentCutoff(unittest.TestCase):
    """The staleness line, and the two clamps that bound it.

    The clamps are load-bearing. Everything feeding this is derived or
    declared, and they are what keep a wrong derivation a slightly-off
    cutoff rather than the review panel offering to retire thousands of
    healthy tests.
    """

    FALLBACK = NOW - datetime.timedelta(hours=36)
    FLOOR = NOW - datetime.timedelta(days=14)

    def _pass(self, environment: str, days_ago: float,
              covered: bool = True) -> analytics.Pass:
        started = NOW - datetime.timedelta(days=days_ago)
        return analytics.Pass(
            environment=environment, started=started,
            ended=started + datetime.timedelta(hours=2),
            runs=100, covered=covered)

    def test_it_takes_the_previous_covered_pass(self) -> None:
        """One whole pass of grace, so a test the currently-running pass
        has not reached yet is never called missing."""
        passes = [self._pass("linux", 3), self._pass("linux", 2),
                  self._pass("linux", 1)]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, passes[1].started)
        self.assertTrue(cutoff.from_passes)
        self.assertEqual(cutoff.environments, ["linux"])

    def test_one_covered_pass_is_used_rather_than_ignored(self) -> None:
        passes = [self._pass("linux", 5)]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, passes[0].started)

    def test_the_oldest_across_environments_wins(self) -> None:
        """Being too lenient only delays a report; being too strict
        accuses thousands of healthy tests."""
        passes = [
            self._pass("linux", 4), self._pass("linux", 3),
            self._pass("win", 3), self._pass("win", 2),
        ]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        # linux's previous covered pass is 4 days old, win's is 3; the
        # older of the two serves the estate.
        self.assertEqual(cutoff.when, NOW - datetime.timedelta(days=4))
        self.assertEqual(cutoff.environments, ["linux", "win"])

    def test_uncovered_passes_are_ignored(self) -> None:
        passes = [self._pass("linux", 9, covered=False),
                  self._pass("linux", 2)]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, passes[1].started)

    def test_no_covered_pass_falls_back_to_the_wall_clock(self) -> None:
        """The silent failure a too-high declaration causes. It is
        reported rather than derived from the timestamp, because the
        caller cannot tell these two cases apart from the value."""
        cutoff = analytics.recent_cutoff(
            [self._pass("linux", 3, covered=False)],
            self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, self.FALLBACK)
        self.assertFalse(cutoff.from_passes)
        self.assertEqual(cutoff.environments, [])

    def test_it_is_never_stricter_than_the_fallback(self) -> None:
        """So this can only ever flag FEWER tests than the wall clock."""
        passes = [self._pass("linux", 0.5), self._pass("linux", 0.2)]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, self.FALLBACK)
        self.assertFalse(cutoff.from_passes)

    def test_it_is_never_older_than_the_floor(self) -> None:
        """So a stalled feeder cannot slide the line back for ever."""
        passes = [self._pass("linux", 40), self._pass("linux", 39)]
        cutoff = analytics.recent_cutoff(passes, self.FALLBACK, self.FLOOR)
        self.assertEqual(cutoff.when, self.FLOOR)


class TestCompletePasses(unittest.TestCase):
    """Passes the lookback window cannot have cut short."""

    FLOOR = NOW - datetime.timedelta(days=14)

    def _pass(self, started: datetime.datetime) -> analytics.Pass:
        return analytics.Pass(
            environment="linux", started=started,
            ended=started + datetime.timedelta(hours=1),
            runs=10, covered=False)

    def test_a_pass_starting_at_the_window_edge_is_dropped(self) -> None:
        """Its run count is whatever fell inside the window, so its
        coverage verdict means nothing - and shown on a page it is a
        permanently red row that is an artefact of the edge."""
        entry = self._pass(self.FLOOR + datetime.timedelta(hours=1))
        self.assertEqual(
            analytics.complete_passes([entry], self.FLOOR, 6.0), [])

    def test_a_pass_after_a_full_quiet_gap_is_kept(self) -> None:
        """The environment was demonstrably quiet for a whole gap, so
        nothing outside the window could have belonged to it."""
        entry = self._pass(self.FLOOR + datetime.timedelta(hours=7))
        self.assertEqual(
            analytics.complete_passes([entry], self.FLOOR, 6.0), [entry])
