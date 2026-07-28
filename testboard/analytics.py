"""Pure analytics over per-test run histories and over the whole estate.

Every function in this module is a pure function: no I/O, no clock reads
(``now`` is always passed in by the caller). The API layer feeds these with
runs from ``Storage.runs_since`` and serializes the result with
:func:`analytics_to_dict`.

Two halves:

- :func:`compute_analytics` and friends — one test's run history in
  detail (flakiness, day-of-week profile, durations, failure streak).
- :func:`summarize_rollup` — the home screen's headline counts, derived
  from ``Storage.summary_rollup``'s GROUP BY cells rather than from a row
  per test, so it costs the same at any estate size.

Semantics (repeated in the README):

- **Failure** means :attr:`~testboard.model.Result.FAIL` only.
  ``FAILED_AS_EXPECTED`` counts as non-failure; ``UNEXPECTED_PASS`` is
  non-failure but tracked separately in the day-of-week profile.
- **Window**: runs with ``start_time >= now - max_days`` days, then the
  newest ``max_runs`` of those. Defaults: ``max_days=90``, ``max_runs=200``.
- **Failure streak**: consecutive runs, newest backwards, with result FAIL.
  ``failing_since`` is the oldest run of that streak (``None`` if the latest
  run in the window is not FAIL). ``last_pass_before_failure`` is the most
  recent run older than the streak with result PASS (``None`` if there is no
  such run in the window, or no streak).
- **Flakiness**: each run's state is "failing" if FAIL else "passing".
  ``transitions`` counts adjacent (in time order) state changes in the
  window. ``score = transitions / run_count`` (0.0 with fewer than 2 runs).
  Classification: ``"no-data"`` for an empty window; ``"flaky"`` when
  ``score >= threshold`` (default 0.2); otherwise ``"stable-fail"`` when the
  newest run is FAIL, else ``"stable-pass"``.
- **Day-of-week profile**: always 7 entries, Monday first ("Mon".."Sun");
  per day: run count, failures (FAIL only), failure rate (0.0 when the day
  has no runs) and unexpected-pass count. Days are taken from each run's
  ``start_time`` (UTC).
- **By day**: one entry per calendar day, counting each result. Zero-filled
  between the first and last day that has a run, so a gap where the test did
  not run is visible; NOT padded to the full window length. Days can hold
  more than one run (a suite may run twice in a day).
- **Duration**: min/median/max of ``(end_time - start_time)`` in seconds
  over ALL runs in the window (``statistics.median``); ``None`` when the
  window is empty.

Python 3.6 compatible; standard library only.
"""

import collections
import datetime
import statistics
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from testboard.model import Result, StoredRun, duration_seconds, format_iso
from testboard.storage import RollupCount

__all__ = [
    "DAY_NAMES",
    "DayStats",
    "DayResults",
    "DurationStats",
    "Flakiness",
    "DEFAULT_FLAKY_THRESHOLD",
    "stability_of",
    "AnalyticsSummary",
    "select_window",
    "compute_analytics",
    "analytics_to_dict",
    "SummaryStatus",
    "EnvironmentRollup",
    "EstateSummary",
    "summarize_rollup",
    "Execution",
    "group_executions",
    "Pass",
    "find_passes",
    "recent_cutoff",
]

#: Day labels for the day-of-week profile, Monday first (index matches
#: ``datetime.date.weekday()``).
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class DayStats(NamedTuple):
    """Per-weekday aggregate over the analytics window."""

    day: str
    runs: int
    failures: int
    failure_rate: float
    unexpected_passes: int


class DurationStats(NamedTuple):
    """Min/median/max run duration in seconds over the window."""

    min: float
    median: float
    max: float


#: Transitions-per-run at or above which a test is called flaky rather
#: than stably passing or stably failing. One flip in five runs.
DEFAULT_FLAKY_THRESHOLD = 0.2


class Flakiness(NamedTuple):
    """Result-transition statistics and classification for the window."""

    transitions: int
    score: float
    classification: str


class DayResults(NamedTuple):
    """Counts of each result on one calendar day of the window."""

    day: datetime.date
    results: Dict[Result, int]
    total: int


class AnalyticsSummary(NamedTuple):
    """Full analytics result for one test over one window."""

    window_run_count: int
    window_from: datetime.datetime
    window_to: datetime.datetime
    failing_since: Optional[StoredRun]
    last_pass_before_failure: Optional[StoredRun]
    flakiness: Flakiness
    day_of_week: List[DayStats]
    by_day: List[DayResults]
    duration: Optional[DurationStats]


def select_window(
    runs_newest_first: Sequence[StoredRun],
    now: datetime.datetime,
    max_days: int = 90,
    max_runs: int = 200,
) -> List[StoredRun]:
    """Select the analytics window from a newest-first run history.

    Keeps runs with ``start_time >= now - max_days`` days, then truncates to
    the newest ``max_runs`` of those. The returned list is newest first.
    """
    cutoff = now - datetime.timedelta(days=max_days)
    recent = [run for run in runs_newest_first if run.start_time >= cutoff]
    return recent[:max_runs]


def _find_streak(window: List[StoredRun]) -> int:
    """Return the length of the newest-first FAIL streak (0 if none)."""
    length = 0
    for run in window:
        if run.result is Result.FAIL:
            length += 1
        else:
            break
    return length


def _compute_flakiness(
    window: List[StoredRun], flaky_threshold: float
) -> Flakiness:
    """Compute transition count, score and classification for the window."""
    run_count = len(window)
    transitions = 0
    # window is newest first; adjacency is the same in either direction.
    for newer, older in zip(window, window[1:]):
        if (newer.result is Result.FAIL) != (older.result is Result.FAIL):
            transitions += 1
    if run_count < 2:
        score = 0.0
    else:
        score = transitions / run_count
    if run_count == 0:
        classification = "no-data"
    elif score >= flaky_threshold:
        classification = "flaky"
    elif window[0].result is Result.FAIL:
        classification = "stable-fail"
    else:
        classification = "stable-pass"
    return Flakiness(
        transitions=transitions, score=score, classification=classification
    )


def stability_of(
    results: Sequence[Result], flaky_threshold: float = DEFAULT_FLAKY_THRESHOLD
) -> Flakiness:
    """Classify a bare sequence of results, oldest first.

    Exists so a LIST view can say "broken since Tuesday" versus "fails
    about one night in three" without loading whole runs. It applies the
    same definition of a transition as :func:`_compute_flakiness` — a
    change in whether the result is FAIL — so the two cannot end up
    disagreeing about what flaky means. ``tests/test_analytics.py``
    asserts they agree on the same window.

    The distinction is the point: a last-pass date on its own cannot
    separate "this broke on the 14th and has failed every night since"
    from "this fails one night in three", and those need different
    responses.
    """
    ordered = list(results)
    run_count = len(ordered)
    transitions = 0
    for older, newer in zip(ordered, ordered[1:]):
        if (older is Result.FAIL) != (newer is Result.FAIL):
            transitions += 1
    score = 0.0 if run_count < 2 else transitions / run_count
    if run_count == 0:
        classification = "no-data"
    elif score >= flaky_threshold:
        classification = "flaky"
    elif ordered[-1] is Result.FAIL:
        classification = "stable-fail"
    else:
        classification = "stable-pass"
    return Flakiness(
        transitions=transitions, score=score, classification=classification
    )


def _compute_day_of_week(window: List[StoredRun]) -> List[DayStats]:
    """Aggregate runs/failures/unexpected-passes per weekday, Monday first."""
    runs = [0] * 7
    failures = [0] * 7
    unexpected = [0] * 7
    for run in window:
        day = run.start_time.weekday()
        runs[day] += 1
        if run.result is Result.FAIL:
            failures[day] += 1
        elif run.result is Result.UNEXPECTED_PASS:
            unexpected[day] += 1
    stats = []  # type: List[DayStats]
    for day in range(7):
        if runs[day]:
            rate = failures[day] / runs[day]
        else:
            rate = 0.0
        stats.append(
            DayStats(
                day=DAY_NAMES[day],
                runs=runs[day],
                failures=failures[day],
                failure_rate=rate,
                unexpected_passes=unexpected[day],
            )
        )
    return stats


def _compute_by_day(window: List[StoredRun]) -> List[DayResults]:
    """Count each result per calendar day across the window, oldest first.

    Zero-filled between the first and last day that actually has a run —
    a gap in the middle means the test did not run, which is itself worth
    seeing, but padding out to the full 90-day window would bury a
    fortnight of history in empty columns.
    """
    if not window:
        return []
    counts = {}  # type: Dict[datetime.date, Dict[Result, int]]
    for run in window:
        day = run.start_time.date()
        if day not in counts:
            counts[day] = {result: 0 for result in Result}
        counts[day][run.result] += 1

    first, last = min(counts), max(counts)
    days = []  # type: List[DayResults]
    current = first
    while current <= last:
        per_result = counts.get(
            current, {result: 0 for result in Result}
        )
        days.append(DayResults(
            day=current,
            results=dict(per_result),
            total=sum(per_result.values()),
        ))
        current += datetime.timedelta(days=1)
    return days


def _compute_duration(window: List[StoredRun]) -> Optional[DurationStats]:
    """Min/median/max duration over the window; None when it is empty."""
    if not window:
        return None
    durations = [
        duration_seconds(run.start_time, run.end_time) for run in window
    ]
    return DurationStats(
        min=min(durations),
        median=float(statistics.median(durations)),
        max=max(durations),
    )


def compute_analytics(
    runs_newest_first: Sequence[StoredRun],
    now: datetime.datetime,
    max_days: int = 90,
    max_runs: int = 200,
    flaky_threshold: float = DEFAULT_FLAKY_THRESHOLD,
) -> AnalyticsSummary:
    """Compute the full analytics summary for one test.

    ``runs_newest_first`` is the test's run history, newest first (it may
    extend beyond the window; :func:`select_window` is applied first).
    ``now`` is the caller's clock reading — this function never reads a
    clock itself. See the module docstring for the exact semantics of each
    statistic.
    """
    window = select_window(runs_newest_first, now, max_days, max_runs)

    streak_len = _find_streak(window)
    failing_since = None  # type: Optional[StoredRun]
    last_pass_before_failure = None  # type: Optional[StoredRun]
    if streak_len > 0:
        failing_since = window[streak_len - 1]
        for run in window[streak_len:]:
            if run.result is Result.PASS:
                last_pass_before_failure = run
                break

    return AnalyticsSummary(
        window_run_count=len(window),
        window_from=now - datetime.timedelta(days=max_days),
        window_to=now,
        failing_since=failing_since,
        last_pass_before_failure=last_pass_before_failure,
        flakiness=_compute_flakiness(window, flaky_threshold),
        day_of_week=_compute_day_of_week(window),
        by_day=_compute_by_day(window),
        duration=_compute_duration(window),
    )


def _run_ref(run: Optional[StoredRun]) -> Optional[Dict[str, Any]]:
    """Serialize a run reference for failing_since/last_pass_before_failure."""
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "result": run.result.value,
        "start_time": format_iso(run.start_time),
    }


def analytics_to_dict(
    summary: AnalyticsSummary, max_runs: int = 200
) -> Dict[str, Any]:
    """Serialize an :class:`AnalyticsSummary` to the analytics JSON shape.

    Rounding: flakiness score and day-of-week failure rates to 4 decimal
    places, durations to 3. ``window.max_days`` is derived exactly from
    ``window_to - window_from`` (``compute_analytics`` sets them exactly
    ``max_days`` days apart); ``window.max_runs`` echoes the run cap and
    defaults to the standard 200 — pass ``max_runs`` when a non-default cap
    was used.
    """
    max_days = (summary.window_to - summary.window_from).days
    if summary.duration is None:
        duration_json = None  # type: Optional[Dict[str, Any]]
    else:
        duration_json = {
            "min": round(summary.duration.min, 3),
            "median": round(summary.duration.median, 3),
            "max": round(summary.duration.max, 3),
        }
    return {
        "window": {
            "max_days": max_days,
            "max_runs": max_runs,
            "run_count": summary.window_run_count,
            "from": format_iso(summary.window_from),
            "to": format_iso(summary.window_to),
        },
        "failing_since": _run_ref(summary.failing_since),
        "last_pass_before_failure": _run_ref(summary.last_pass_before_failure),
        "flakiness": {
            "transitions": summary.flakiness.transitions,
            "score": round(summary.flakiness.score, 4),
            "classification": summary.flakiness.classification,
        },
        "by_day": [
            {
                "date": day.day.isoformat(),
                "total": day.total,
                **{
                    result.name: day.results[result]
                    for result in Result
                },
            }
            for day in summary.by_day
        ],
        "day_of_week": [
            {
                "day": day.day,
                "runs": day.runs,
                "failures": day.failures,
                "failure_rate": round(day.failure_rate, 4),
                "unexpected_passes": day.unexpected_passes,
            }
            for day in summary.day_of_week
        ],
        "duration_seconds": duration_json,
    }


# ----------------------------------------------------------------------
# Estate summary (the home-screen /api/summary payload)
# ----------------------------------------------------------------------


class SummaryStatus(NamedTuple):
    """Headline counts over the whole test estate.

    Tests retired as no longer in the suite are counted ONLY in
    ``retired``; they are absent from every other number here, so
    approving a disappeared test really does take it off the board.

    ``results`` counts every test by its latest result (the current
    state of the estate); ``recent_results`` counts only tests whose
    latest run started within the recency window — the "last night"
    view. ``new_failures``/``still_failing``/``fixed`` come from the
    latest-vs-previous result pair; ``assigned_open`` counts tests with
    an assignee whose latest result is FAIL or UNEXPECTED_PASS.
    """

    total_tests: int
    ran_recently: int
    not_run: int
    retired: int
    results: Dict[Result, int]
    recent_results: Dict[Result, int]
    new_failures: int
    still_failing: int
    fixed: int
    assigned_open: int


class EnvironmentRollup(NamedTuple):
    """Per-environment slice of the headline counts."""

    environment: str
    total_tests: int
    failed: int
    new_failures: int
    unexpected_passes: int
    not_run: int


class Execution(NamedTuple):
    """One run of a whole script — a batch of test runs that went together."""

    started: datetime.datetime
    ended: datetime.datetime
    total: int
    results: Dict[Result, int]

    @property
    def failed(self) -> int:
        """Runs that FAILED (the only result that counts as a failure)."""
        return self.results[Result.FAIL]

    @property
    def duration_seconds(self) -> float:
        """Wall-clock span from the first run starting to the last ending."""
        return duration_seconds(self.started, self.ended)


class Pass(NamedTuple):
    """One contiguous block of activity in one environment.

    Inferred from timing, like :func:`group_executions`, and for the
    same reason: the import contract carries no batch identifier.

    ``covered`` is True when the block ran enough of the environment to
    count as a real pass of the suite rather than an ad-hoc re-run.
    """

    environment: str
    started: datetime.datetime
    ended: datetime.datetime
    runs: int
    covered: bool


def find_passes(
    buckets: Sequence[Tuple[str, datetime.datetime, int]],
    test_counts: Dict[str, int],
    gap_hours: float,
    coverage: float,
) -> List[Pass]:
    """Group per-environment activity hours into passes, oldest first.

    A block ends when that environment has been quiet for *gap_hours*.
    It counts as ``covered`` when it ran at least *coverage* of that
    environment's tests.

    Both halves are load-bearing, and each is here because of a way the
    suite actually runs:

    - **Per environment**, because environments run SEQUENTIALLY: the
      first reports in the small hours and the last hours later. Judged
      against one shared clock, whichever ran first looks stale for the
      remainder of the morning.
    - **Coverage**, because a failed run is followed by ad-hoc re-runs
      once it is fixed. Those are blocks of activity too, and treating
      them as passes would drag the "everything has reported by now"
      line forward to this afternoon and flag the entire estate. A
      twenty-test re-run is not a pass of the suite.

    Nothing here knows what time the suite runs. Every boundary comes
    from observed gaps, so a schedule change needs no code change.
    """
    grouped = {}  # type: Dict[str, List[Tuple[datetime.datetime, int]]]
    for environment, hour, count in buckets:
        grouped.setdefault(environment, []).append((hour, count))

    gap = datetime.timedelta(hours=gap_hours)
    passes = []  # type: List[Pass]
    for environment in sorted(grouped):
        needed = max(1, int(test_counts.get(environment, 0) * coverage))
        started = None  # type: Optional[datetime.datetime]
        previous = None  # type: Optional[datetime.datetime]
        runs = 0
        for hour, count in sorted(grouped[environment]):
            if started is None:
                started = hour
            elif hour - previous > gap:
                passes.append(Pass(
                    environment=environment, started=started,
                    ended=previous, runs=runs, covered=runs >= needed))
                started = hour
                runs = 0
            previous = hour
            runs += count
        if started is not None:
            passes.append(Pass(
                environment=environment, started=started, ended=previous,
                runs=runs, covered=runs >= needed))
    passes.sort(key=lambda entry: (entry.started, entry.environment))
    return passes


def recent_cutoff(
    passes: Sequence[Pass],
    fallback: datetime.datetime,
    floor: datetime.datetime,
) -> datetime.datetime:
    """When a test's silence starts being suspicious.

    Per environment, the start of the PREVIOUS covered pass: one whole
    pass of grace, so a test the currently-running pass has not reached
    yet is not called missing. Then the oldest across environments,
    because a single cutoff has to serve the estate and the two errors
    are not equal - being too lenient only delays a report, while being
    too strict accuses thousands of healthy tests and offers to retire
    them.

    Never stricter than *fallback* (the old wall-clock window), so this
    can only ever flag FEWER tests than before; never older than
    *floor*, so a stalled feeder cannot slide the line back for ever.
    """
    cutoff = fallback
    by_env = {}  # type: Dict[str, List[Pass]]
    for entry in passes:
        if entry.covered:
            by_env.setdefault(entry.environment, []).append(entry)
    for environment in sorted(by_env):
        covered = by_env[environment]
        chosen = covered[-2] if len(covered) >= 2 else covered[-1]
        cutoff = min(cutoff, chosen.started)
    return max(cutoff, floor)


def group_executions(
    runs_oldest_first: Sequence[StoredRun],
    gap_minutes: int = 60,
) -> List[Execution]:
    """Group a script's runs into the executions they belong to.

    A test result carries no batch identifier — the import contract is a
    flat list of runs — so an execution has to be inferred from timing.
    That is exactly what a human does looking at the timestamps: runs
    that follow one another belong to the same execution, and a long
    quiet period means the suite ran again later.

    A new execution starts when a run begins more than *gap_minutes*
    after the LATEST END seen so far, not after the previous start, so a
    single slow test cannot split its own execution in two.

    This matters because a suite does not necessarily run once a night:
    grouping by calendar day would silently merge a morning run and an
    evening re-run into one misleading row.

    *runs_oldest_first* must be ordered by ``start_time`` ascending.
    Returns executions in the same order (oldest first).
    """
    gap = datetime.timedelta(minutes=gap_minutes)
    executions = []  # type: List[Execution]
    started = None  # type: Optional[datetime.datetime]
    ended = None  # type: Optional[datetime.datetime]
    counts = {}  # type: Dict[Result, int]
    total = 0

    def flush() -> None:
        """Close the execution being accumulated, if there is one."""
        if started is not None and ended is not None:
            executions.append(Execution(
                started=started, ended=ended, total=total,
                results=dict(counts),
            ))

    for run in runs_oldest_first:
        if started is None or ended is None:
            started, ended = run.start_time, run.end_time
            counts = {result: 0 for result in Result}
            total = 0
        elif run.start_time - ended > gap:
            flush()
            started, ended = run.start_time, run.end_time
            counts = {result: 0 for result in Result}
            total = 0
        counts[run.result] += 1
        total += 1
        if run.end_time > ended:
            ended = run.end_time
    flush()
    return executions


class EstateSummary(NamedTuple):
    """Everything :func:`summarize_rollup` derives from the rollup counts."""

    status: SummaryStatus
    by_environment: List[EnvironmentRollup]


def summarize_rollup(
    counts: Sequence[RollupCount], assigned_open: int = 0
) -> EstateSummary:
    """Derive the estate headline from grouped counts.

    Pure, and deliberately cheap: *counts* is the handful of GROUP BY
    cells returned by ``Storage.summary_rollup`` — one per
    (environment, result, previous result, ran-recently) combination —
    rather than one row per test, so this stays the same amount of work
    whether the estate holds 100 tests or 100,000. Each cell contributes
    its ``count`` to every bucket it belongs to.

    The latest/previous result pair is what separates the three states
    that matter overnight, and these are the definitions the SQL queue
    predicates in :mod:`testboard.storage` mirror:

    - **new failure** — now FAIL, previously not FAIL (or no previous run)
    - **still failing** — now FAIL, previously FAIL
    - **fixed** — previously FAIL, now anything else

    *assigned_open* is counted separately (it depends on the assignment
    tables, not on the result pair) and is passed straight through.
    """
    results = {result: 0 for result in Result}
    recent_results = {result: 0 for result in Result}
    total = 0
    ran_recently = 0
    new_failures = 0
    still_failing = 0
    fixed = 0
    retired = 0

    # Per environment: [total, failed, new_failures, unexpected, not_run]
    env_totals = collections.OrderedDict()  # type: Dict[str, List[int]]

    for cell in counts:
        if cell.retired:
            # Someone has approved this test as no longer in the suite.
            # It is counted here and NOWHERE else — not in the totals,
            # not in "not run", not in any queue — which is the entire
            # point of retiring it.
            retired += cell.count
            continue
        is_fail = cell.result is Result.FAIL
        was_fail = cell.prev_result is Result.FAIL
        is_unexpected = cell.result is Result.UNEXPECTED_PASS

        total += cell.count
        results[cell.result] += cell.count
        if cell.recent:
            ran_recently += cell.count
            recent_results[cell.result] += cell.count
        if is_fail and not was_fail:
            new_failures += cell.count
        elif is_fail and was_fail:
            still_failing += cell.count
        elif was_fail:
            fixed += cell.count

        if cell.environment not in env_totals:
            env_totals[cell.environment] = [0, 0, 0, 0, 0]
        bucket = env_totals[cell.environment]
        bucket[0] += cell.count
        if is_fail:
            bucket[1] += cell.count
            if not was_fail:
                bucket[2] += cell.count
        if is_unexpected:
            bucket[3] += cell.count
        if not cell.recent:
            bucket[4] += cell.count

    status = SummaryStatus(
        total_tests=total,
        ran_recently=ran_recently,
        not_run=total - ran_recently,
        retired=retired,
        results=results,
        recent_results=recent_results,
        new_failures=new_failures,
        still_failing=still_failing,
        fixed=fixed,
        assigned_open=assigned_open,
    )
    by_environment = [
        EnvironmentRollup(
            environment=environment,
            total_tests=bucket[0],
            failed=bucket[1],
            new_failures=bucket[2],
            unexpected_passes=bucket[3],
            not_run=bucket[4],
        )
        for environment, bucket in sorted(env_totals.items())
    ]
    return EstateSummary(status=status, by_environment=by_environment)
