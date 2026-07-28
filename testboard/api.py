"""Framework-free HTTP API routing and handlers for testboard.

This module owns everything between "an HTTP request arrived" and "here is
the response": the :class:`Request` / :class:`Response` transport
NamedTuples, the route table under ``/api``, per-endpoint validation, and
JSON serialization of the storage/analytics results.

Design rules (see the project brief):

- Handlers are plain functions taking a parsed :class:`Request` plus an
  injected :class:`~testboard.storage.Storage`; they are unit-tested
  directly, without a real HTTP server.
- ``Request.path`` is the RAW path (no query string, still
  percent-encoded). It is split on ``'/'`` FIRST and each segment is then
  decoded with :func:`urllib.parse.unquote` — so test names containing
  ``/`` arrive as ``%2F`` and survive routing. ``+`` is never decoded to a
  space (``unquote``, never ``unquote_plus``).
- Every response — success or error — is JSON with
  ``Content-Type: application/json; charset=utf-8``. Errors are
  ``{"error": "message"}``: 400 for validation, 404 for unknown resources,
  405 for a wrong method (with an ``Allow`` header listing the methods the
  path supports).
- ``/api/import`` never fails a whole batch because one record is bad:
  valid records are upserted, each rejected record produces an error
  object carrying the record's identity fields (``environment`` /
  ``script`` / ``test_name`` / ``start_time``, ``null`` when absent) so an
  engineer can grep their source data for the offending run.

Python 3.6 compatible; standard library only.
"""

import datetime
import json
import logging
import urllib.parse
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from testboard import analytics, model
from testboard.model import Result, RunRecord, StoredRun, ValidationError
from testboard.storage import (
    DASHBOARD_SORTS,
    QUEUE_KINDS,
    Comment,
    FailureStreak,
    Storage,
    TestStatusRow,
    TestSummaryRow,
    User,
)

__all__ = ["Request", "Response", "handle_api"]

_LOGGER = logging.getLogger(__name__)


#: The Content-Type header attached to every API response.
_CONTENT_TYPE = ("Content-Type", "application/json; charset=utf-8")

_MAX_USERNAME_LEN = 100
_MAX_COMMENT_LEN = 10000
_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 500
_ANALYTICS_MAX_DAYS = 90
_ANALYTICS_MAX_RUNS = 200

# /api/dashboard paging. The estate is far too large to serialize whole,
# so a request without a limit still gets one page; the response always
# carries the exact total for the filters so a caller knows what it is
# looking at a slice of.
_DEFAULT_SORT = "environment"
_DEFAULT_PAGE_LIMIT = 250
_MAX_PAGE_LIMIT = 1000
_MAX_OFFSET = 1000000

# /api/summary tuning: trend window bounds, the recency cutoff that
# separates "ran last night" from "not run", and the per-queue entry cap
# that bounds the payload at large estates (totals are always exact).
_SUMMARY_DEFAULT_TREND_DAYS = 14
_SUMMARY_MAX_TREND_DAYS = 90
_SUMMARY_RECENT_HOURS = 36
_SUMMARY_QUEUE_CAP = 500
_SUMMARY_TOP_SCRIPTS = 10

# /api/scripts/.../executions: how far back to look, how many runs to
# pull, and the quiet period that separates one execution of a suite from
# the next. An hour is far longer than the pause between tests in a run
# and far shorter than the gap between scheduled runs.
_EXECUTIONS_DEFAULT_DAYS = 14
_EXECUTIONS_MAX_DAYS = 90
_EXECUTIONS_MAX_RUNS = 20000
_EXECUTION_GAP_MINUTES = 60

#: Queues whose entries report failing_since / last_pass_time. Only these
#: pay for the per-row streak seeks; the others describe a test whose
#: latest run is not a failure, or show the previous result instead.
_STREAK_QUEUES = ("still_failing",)

#: Window used for the list-view stability signal ("broken since Tuesday"
#: vs "fails about one night in three"). Deliberately shorter than the
#: detail page's 90 days: this answers "what is it doing lately", and a
#: quarter of history would bury a test that broke this week.
_STABILITY_WINDOW_DAYS = 30
_STABILITY_RUNS = 20


class Request(NamedTuple):
    """A parsed-but-undecoded HTTP request handed to :func:`handle_api`.

    ``path`` is the raw request path without the query string, still
    percent-encoded. ``query`` is ``urllib.parse.parse_qs`` of the raw
    query string. ``method`` is upper-case.
    """

    method: str
    path: str
    query: Dict[str, List[str]]
    body: bytes


class Response(NamedTuple):
    """An HTTP response: status code, header list and body bytes.

    ``headers`` always includes
    ``('Content-Type', 'application/json; charset=utf-8')``.
    """

    status: int
    headers: List[Tuple[str, str]]
    body: bytes


class _HttpError(Exception):
    """Internal control-flow exception carrying an HTTP error response."""

    def __init__(
        self,
        status: int,
        message: str,
        headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        """Store *status*, user-facing *message* and extra *headers*."""
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers if headers is not None else []


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def _split_path(raw_path: str) -> List[str]:
    """Split a raw path into decoded segments.

    The RAW (still percent-encoded) path is split on ``'/'`` first, empty
    segments (leading slash, trailing slash, doubled slashes) are dropped,
    and THEN each segment is decoded with :func:`urllib.parse.unquote`.
    This order is what lets test names containing ``/`` travel as ``%2F``
    without being confused with a path separator. ``+`` is left alone
    (never ``unquote_plus``).
    """
    return [
        urllib.parse.unquote(segment)
        for segment in raw_path.split("/")
        if segment != ""
    ]


def _json_response(
    status: int,
    payload: Any,
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> Response:
    """Build a :class:`Response` with a JSON body and Content-Type header."""
    headers = [_CONTENT_TYPE]  # type: List[Tuple[str, str]]
    if extra_headers:
        headers.extend(extra_headers)
    body = json.dumps(payload).encode("utf-8")
    return Response(status=status, headers=headers, body=body)


def _check_method(method: str, allowed: Sequence[str]) -> None:
    """Raise a 405 :class:`_HttpError` (with Allow header) on a bad method."""
    if method not in allowed:
        allow = ", ".join(allowed)
        raise _HttpError(
            405,
            "method {} not allowed (allowed: {})".format(method, allow),
            headers=[("Allow", allow)],
        )


def _query_single(
    query: Dict[str, List[str]], name: str
) -> Optional[str]:
    """Return the first value of query parameter *name*, or None."""
    values = query.get(name)
    if not values:
        return None
    return values[0]


def _parse_json_body(body: bytes) -> Any:
    """Decode and JSON-parse a request body; 400 on any failure."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _HttpError(400, "request body is not valid UTF-8")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _HttpError(400, "invalid JSON body: {}".format(exc))


def _parse_json_object(body: bytes) -> Dict[str, Any]:
    """JSON-parse a request body and require a JSON object; 400 otherwise."""
    obj = _parse_json_body(body)
    if not isinstance(obj, dict):
        raise _HttpError(
            400,
            "request body must be a JSON object, got {}".format(
                type(obj).__name__
            ),
        )
    return obj


def _validate_username(obj: Dict[str, Any], field: str) -> str:
    """Validate a username-like *field* of *obj*; return it stripped.

    Rules: required, must be a string, non-empty after stripping
    whitespace, at most 100 characters after stripping. Violations raise a
    400 :class:`_HttpError` naming the field.
    """
    if field not in obj:
        raise _HttpError(
            400, "{}: required field is missing".format(field)
        )
    value = obj[field]
    if not isinstance(value, str):
        raise _HttpError(
            400,
            "{}: must be a string, got {}".format(
                field, type(value).__name__
            ),
        )
    stripped = value.strip()
    if not stripped:
        raise _HttpError(
            400, "{}: must not be empty or whitespace-only".format(field)
        )
    if len(stripped) > _MAX_USERNAME_LEN:
        raise _HttpError(
            400,
            "{}: must be at most {} characters (got {})".format(
                field, _MAX_USERNAME_LEN, len(stripped)
            ),
        )
    return stripped


def _validate_comment_text(
    obj: Dict[str, Any], field: str = "text"
) -> str:
    """Validate a comment body field; 400 on violation."""
    if field not in obj:
        raise _HttpError(
            400, "{}: required field is missing".format(field)
        )
    value = obj[field]
    if not isinstance(value, str):
        raise _HttpError(
            400,
            "{}: must be a string, got {}".format(
                field, type(value).__name__
            ),
        )
    if not value.strip():
        raise _HttpError(
            400, "{}: must not be empty".format(field)
        )
    if len(value) > _MAX_COMMENT_LEN:
        raise _HttpError(
            400,
            "{}: must be at most {} characters (got {})".format(
                field, _MAX_COMMENT_LEN, len(value)
            ),
        )
    return value


def _identity_field(raw: Any, key: str) -> Optional[str]:
    """Extract a string identity field from a raw import record, if possible.

    Used to annotate ``/api/import`` error objects so the offending run can
    be located in the source data. Returns None (JSON null) when the raw
    record is not a dict, the key is absent, or the value is not a string.
    """
    if isinstance(raw, dict):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return None


def _unknown_test(environment: str, script: str, test_name: str) -> _HttpError:
    """Build the 404 error for a test triple with no recorded runs."""
    return _HttpError(
        404,
        "unknown test: no runs recorded for {} / {} / {}".format(
            environment, script, test_name
        ),
    )


def _require_test(
    storage: Storage, environment: str, script: str, test_name: str
) -> None:
    """Raise 404 unless at least one run exists for the triple."""
    if not storage.test_exists(environment, script, test_name):
        raise _unknown_test(environment, script, test_name)


# ----------------------------------------------------------------------
# JSON serializers
# ----------------------------------------------------------------------


def _run_json(run: StoredRun) -> Dict[str, Any]:
    """Serialize a run to the RunOut shape (no ``output``, no identity)."""
    return {
        "run_id": run.run_id,
        "result": run.result.value,
        "start_time": model.format_iso(run.start_time),
        "end_time": model.format_iso(run.end_time),
        "duration_seconds": round(
            model.duration_seconds(run.start_time, run.end_time), 3
        ),
        "known_failure_reason": run.known_failure_reason,
        "source_link": run.source_link,
    }


def _summary_row_json(row: TestSummaryRow) -> Dict[str, Any]:
    """Serialize one dashboard row to its JSON shape."""
    payload = {
        "environment": row.environment,
        "script": row.script,
        "test_name": row.test_name,
        "run_id": row.run_id,
        "result": row.result.value,
        "start_time": model.format_iso(row.start_time),
        "end_time": model.format_iso(row.end_time),
        "duration_seconds": round(
            model.duration_seconds(row.start_time, row.end_time), 3
        ),
        "known_failure_reason": row.known_failure_reason,
        "source_link": row.source_link,
        "assignee": row.assignee,
        "retired_at": (
            None if row.retired_at is None
            else model.format_iso(row.retired_at)
        ),
        "retired_by": row.retired_by,
    }  # type: Dict[str, Any]
    if row.latest_comment is not None:
        payload["latest_comment"] = {
            "author": row.latest_comment.author,
            "created_at": model.format_iso(row.latest_comment.created_at),
            "text": row.latest_comment.text,
        }
    return payload


def _comment_json(comment: Comment) -> Dict[str, Any]:
    """Serialize a comment to its JSON shape."""
    return {
        "id": comment.comment_id,
        "author": comment.author,
        "created_at": model.format_iso(comment.created_at),
        "text": comment.text,
    }


def _user_json(user: User) -> Dict[str, Any]:
    """Serialize a user to its JSON shape.

    ``active`` is sent as well as ``deactivated_at`` even though one
    implies the other: every consumer wants the boolean, and deriving it
    from a timestamp in four different places is how three of them end
    up agreeing and one does not.
    """
    return {
        "username": user.username,
        "created_at": model.format_iso(user.created_at),
        "active": user.active,
        "deactivated_at": (
            None if user.deactivated_at is None
            else model.format_iso(user.deactivated_at)
        ),
        "deactivated_by": user.deactivated_by,
    }


# ----------------------------------------------------------------------
# Endpoint handlers
# ----------------------------------------------------------------------


def _handle_import(storage: Storage, request: Request) -> Response:
    """POST /api/import — bulk idempotent upsert of run records.

    400 only when the envelope itself is malformed (bad JSON, ``runs``
    missing or not a list). Individual bad records never abort the batch:
    valid records are upserted and each reject is reported with its index,
    a field-specific message and the record's identity fields (null when
    not extractable).
    """
    envelope = _parse_json_body(request.body)
    if not isinstance(envelope, dict):
        raise _HttpError(
            400,
            "request body must be a JSON object with a 'runs' list, "
            "got {}".format(type(envelope).__name__),
        )
    if "runs" not in envelope:
        raise _HttpError(400, "runs: required field is missing")
    raw_runs = envelope["runs"]
    if not isinstance(raw_runs, list):
        raise _HttpError(
            400,
            "runs: must be a list, got {}".format(type(raw_runs).__name__),
        )

    valid = []  # type: List[RunRecord]
    errors = []  # type: List[Dict[str, Any]]
    for index, raw in enumerate(raw_runs):
        try:
            valid.append(model.parse_run_record(raw))
        except ValidationError as exc:
            errors.append(
                {
                    "index": index,
                    "error": str(exc),
                    "environment": _identity_field(raw, "environment"),
                    "script": _identity_field(raw, "script"),
                    "test_name": _identity_field(raw, "test_name"),
                    "start_time": _identity_field(raw, "start_time"),
                }
            )
    counts = storage.upsert_runs(valid)
    return _json_response(
        200,
        {
            "inserted": counts.inserted,
            "updated": counts.updated,
            "rejected": len(errors),
            "errors": errors,
        },
    )


def _parse_int_param(
    request: Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    """Parse an optional integer query parameter, 400 on bad value/range."""
    raw = _query_single(request.query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _HttpError(
            400, "{}: must be an integer, got '{}'".format(name, raw)
        )
    if value < minimum or value > maximum:
        raise _HttpError(
            400,
            "{}: must be between {} and {} (got {})".format(
                name, minimum, maximum, value
            ),
        )
    return value


def _parse_results_param(request: Request) -> Optional[List[Result]]:
    """Parse repeated ``result=`` params into Result members, 400 on unknown."""
    raw_results = request.query.get("result")
    if raw_results is None:
        return None
    results = []  # type: List[Result]
    for raw in raw_results:
        try:
            results.append(Result[raw])
        except KeyError:
            raise _HttpError(
                400,
                "result: unknown value '{}' (expected one of {})".format(
                    raw, ", ".join(r.name for r in Result)
                ),
            )
    return results


#: How long one environment must be quiet before its next block of runs
#: counts as a separate pass. Environments run sequentially and a whole
#: cycle takes hours, so this has to be longer than any pause WITHIN a
#: pass and shorter than the gap between consecutive passes. Not a time
#: of day, and nothing here knows the schedule.
_PASS_GAP_HOURS = 6.0

#: Share of an environment's tests a block must run before it counts as
#: a pass rather than an ad-hoc re-run after a fix.
_PASS_COVERAGE = 0.5

#: How far back to look for passes, and the oldest a derived cutoff may
#: be. The floor matters: if the feeder stops, the newest pass stops
#: moving, and without a floor the cutoff would slide backwards for ever
#: and nothing would ever look stale again.
_PASS_LOOKBACK_DAYS = 14


class _PassView(NamedTuple):
    """Everything derived from the suite's own rhythm, computed once.

    One call path, deliberately. If the admin page worked out its passes
    separately from the cutoff, a declared expectation could change what
    the page shows and not what the estate is judged by — worse than not
    having the feature, because it would look like it worked.
    """

    passes: List[analytics.Pass]
    cutoff: analytics.Cutoff
    inferred: Dict[str, int]
    declared: Dict[str, int]
    effective: Dict[str, int]
    fallback: datetime.datetime
    floor: datetime.datetime


def _pass_view(
    storage: Storage, now_value: datetime.datetime
) -> _PassView:
    """Group recent activity into passes and derive the staleness line.

    Derived from when the suite actually ran, not from the wall clock.

    The wall-clock window this replaces was right from Tuesday to Friday
    and wrong every Monday: with the last run on Friday night, a 36-hour
    window makes every test in the estate look abandoned. It was also
    wrong every morning for whichever environment runs first, because
    environments run sequentially and the last of them reports hours
    after the first.

    Both failures had the same three consequences: a "not run" queue
    full of healthy tests, a headline claiming nothing ran, and — worst
    — the review panel offering to RETIRE thousands of tests that were
    simply waiting their turn.

    Falls back to the wall-clock window when there is not enough history
    to infer anything, and can never be stricter than it.
    """
    fallback = now_value - datetime.timedelta(hours=_SUMMARY_RECENT_HOURS)
    floor = now_value - datetime.timedelta(days=_PASS_LOOKBACK_DAYS)
    inferred = storage.test_counts_by_environment()
    declared = storage.declared_test_counts()
    effective = analytics.effective_test_counts(inferred, declared)
    passes = analytics.find_passes(
        storage.activity_buckets(floor),
        effective,
        gap_hours=_PASS_GAP_HOURS,
        coverage=_PASS_COVERAGE,
    )
    return _PassView(
        passes=passes,
        cutoff=analytics.recent_cutoff(passes, fallback, floor),
        inferred=inferred,
        declared=declared,
        effective=effective,
        fallback=fallback,
        floor=floor,
    )


def _recent_cutoff(
    storage: Storage, now_value: datetime.datetime
) -> datetime.datetime:
    """The staleness line alone, for the endpoints that only need it."""
    return _pass_view(storage, now_value).cutoff.when


def _handle_dashboard(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/dashboard — ONE PAGE of the latest run per test, no outputs.

    The estate can hold tens of thousands of tests, so this endpoint is
    paginated: it answers with the rows for the requested window plus the
    exact ``total`` for the filters, and the caller pages through with
    ``limit``/``offset``. Filtering, sorting and searching all happen in
    SQL — no client is expected to hold the whole estate to do them.
    """
    environment = _query_single(request.query, "environment")
    script = _query_single(request.query, "script")
    q = _query_single(request.query, "q")
    results = _parse_results_param(request)

    stale_before = None  # type: Optional[datetime.datetime]
    if _query_single(request.query, "stale") in ("1", "true"):
        stale_before = _recent_cutoff(storage, now())
    include_retired = _query_single(
        request.query, "retired") in ("1", "true")
    with_comment = _query_single(
        request.query, "with_comment") in ("1", "true")
    # Failure streaks and recent-result history cost extra seeks, so they
    # are opt-in and computed for the RETURNED PAGE ONLY — never for the
    # count query, and never for rows nobody asked to see.
    with_streak = _query_single(
        request.query, "with_streak") in ("1", "true")
    include_unassigned = _query_single(
        request.query, "unassigned") in ("1", "true")
    assignees = request.query.get("assignee") or []

    sort = _query_single(request.query, "sort") or _DEFAULT_SORT
    if sort not in DASHBOARD_SORTS:
        raise _HttpError(
            400,
            "sort: unknown value '{}' (expected one of {})".format(
                sort, ", ".join(sorted(DASHBOARD_SORTS))
            ),
        )
    order = _query_single(request.query, "order") or "asc"
    if order not in ("asc", "desc"):
        raise _HttpError(
            400,
            "order: must be 'asc' or 'desc', got '{}'".format(order),
        )

    limit = _parse_int_param(
        request, "limit", _DEFAULT_PAGE_LIMIT, 1, _MAX_PAGE_LIMIT
    )
    offset = _parse_int_param(request, "offset", 0, 0, _MAX_OFFSET)

    filters = {
        "environment": environment,
        "script": script,
        "results": results,
        "q": q,
        "stale_before": stale_before,
        "include_retired": include_retired,
        "assignees": assignees,
        "include_unassigned": include_unassigned,
    }  # type: Dict[str, Any]
    rows = storage.dashboard(
        sort=sort, descending=(order == "desc"), limit=limit,
        offset=offset, with_latest_comment=with_comment, **filters
    )
    payload = [_summary_row_json(row) for row in rows]
    if with_streak:
        _add_streaks(storage, rows, payload, now())
    return _json_response(
        200,
        {
            "tests": payload,
            "total": storage.dashboard_count(**filters),
            "limit": limit,
            "offset": offset,
            "with_streak": with_streak,
        },
    )


def _add_streaks(
    storage: Storage,
    rows: Sequence[TestSummaryRow],
    payload: List[Dict[str, Any]],
    now_value: datetime.datetime,
) -> None:
    """Attach failing-since, last-pass and stability to a PAGE of rows.

    Two questions a triage list has to answer and could not: when did
    this last pass, and did it break on that date or does it just fail
    sometimes? A last-pass date alone cannot tell those apart, and they
    need different responses.

    Cost is bounded by the page, not the estate. The streak bounds are
    three index seeks and are looked up only for rows whose latest run
    is a FAIL — the others have no streak to report. The result history
    is fetched for the whole page in a handful of batched queries (see
    Storage.recent_results), never one per row.
    """
    triples = [
        (row.environment, row.script, row.test_name) for row in rows
    ]
    since = now_value - datetime.timedelta(days=_STABILITY_WINDOW_DAYS)
    history = storage.recent_results(
        triples, since, per_test_limit=_STABILITY_RUNS)
    for row, item in zip(rows, payload):
        key = (row.environment, row.script, row.test_name)
        if row.result is Result.FAIL:
            streak = storage.failure_streak_bounds(
                row.environment, row.script, row.test_name, row.start_time
            )
            item["failing_since"] = (
                None if streak.failing_since is None
                else model.format_iso(streak.failing_since)
            )
            item["last_pass_time"] = (
                None if streak.last_pass_before is None
                else model.format_iso(streak.last_pass_before)
            )
        else:
            item["failing_since"] = None
            item["last_pass_time"] = None
        results = history.get(key, [])
        stability = analytics.stability_of(results)
        item["stability"] = {
            "classification": stability.classification,
            "transitions": stability.transitions,
            "score": round(stability.score, 3),
            "runs": len(results),
            "recent_results": [result.value for result in results],
        }


def _result_counts_json(counts: Dict[Result, int]) -> Dict[str, int]:
    """Serialize a per-Result count dict with stable enum-name keys."""
    return {result.name: counts[result] for result in Result}


def _status_row_json(
    row: TestStatusRow, streak: Optional[FailureStreak]
) -> Dict[str, Any]:
    """Serialize one queue entry (a status row plus optional streak info)."""
    failing_since = None  # type: Optional[datetime.datetime]
    last_pass = None  # type: Optional[datetime.datetime]
    if streak is not None:
        failing_since = streak.failing_since
        last_pass = streak.last_pass_before
    return {
        "environment": row.environment,
        "script": row.script,
        "test_name": row.test_name,
        "run_id": row.run_id,
        "result": row.result.value,
        "prev_result": (
            None if row.prev_result is None else row.prev_result.value
        ),
        "start_time": model.format_iso(row.start_time),
        "duration_seconds": round(
            model.duration_seconds(row.start_time, row.end_time), 3
        ),
        "known_failure_reason": row.known_failure_reason,
        "assignee": row.assignee,
        # What somebody already found out about this failure. The first
        # question a person triaging asks is "has anyone looked at this?".
        "latest_comment": (
            None if row.latest_comment is None else {
                "author": row.latest_comment.author,
                "created_at": model.format_iso(
                    row.latest_comment.created_at),
                "text": row.latest_comment.text,
            }
        ),
        "failing_since": (
            None if failing_since is None
            else model.format_iso(failing_since)
        ),
        "last_pass_time": (
            None if last_pass is None else model.format_iso(last_pass)
        ),
    }


def _handle_summary(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/summary — the home-screen estate summary.

    Query parameters: ``environment`` (optional exact match) scopes
    everything; ``days`` (1..90, default 14) sets the trend window;
    ``assignee`` adds the ``mine`` queue for that user.

    Nothing here is proportional to the size of the estate. The headline
    counts come from a GROUP BY (a few dozen rows however many tests
    exist), each triage queue is a separate indexed query capped at
    ``_SUMMARY_QUEUE_CAP`` entries with its exact total alongside, and
    failure streaks are looked up only for the entries actually shown.
    """
    environment = _query_single(request.query, "environment")
    assignee = _query_single(request.query, "assignee")
    days = _parse_int_param(
        request, "days", _SUMMARY_DEFAULT_TREND_DAYS, 1,
        _SUMMARY_MAX_TREND_DAYS,
    )

    current = now()
    recent_cutoff = _recent_cutoff(storage, current)
    # Reported so a stalled feeder is visible AS a stalled feeder,
    # rather than as every test in the estate quietly going stale — the
    # failure mode a data-derived cutoff would otherwise hide.
    latest_run = storage.latest_run_time()
    estate = analytics.summarize_rollup(
        storage.summary_rollup(recent_cutoff, environment),
        storage.assigned_open_count(environment),
    )

    # Trend: per-night result counts, zero-filled over the window so the
    # chart's x-axis is continuous even on nights nothing ran.
    first_day = current.date() - datetime.timedelta(days=days - 1)
    since = datetime.datetime.combine(first_day, datetime.time())
    counts = {}  # type: Dict[Tuple[datetime.date, Result], int]
    for entry in storage.daily_result_counts(since, environment):
        counts[(entry.day, entry.result)] = entry.count
    nights = []  # type: List[Dict[str, Any]]
    for offset in range(days):
        day = first_day + datetime.timedelta(days=offset)
        night = {"date": day.isoformat()}  # type: Dict[str, Any]
        total = 0
        for result in Result:
            count = counts.get((day, result), 0)
            night[result.name] = count
            total += count
        night["total"] = total
        nights.append(night)

    # Failure streaks, for the queue entries that will actually be shown.
    # A test can sit in several queues; look each one up once.
    streaks = {}  # type: Dict[Tuple[str, str, str], FailureStreak]

    def streak_for(row: TestStatusRow) -> Optional[FailureStreak]:
        """Streak info for a FAIL row (cached); None for non-FAIL rows."""
        if row.result is not Result.FAIL:
            return None
        key = (row.environment, row.script, row.test_name)
        if key not in streaks:
            streaks[key] = storage.failure_streak_bounds(
                row.environment, row.script, row.test_name, row.start_time
            )
        return streaks[key]

    def queue_json(
        kind: str,
        queue_assignee: Optional[str] = None,
        with_streaks: bool = False,
    ) -> Dict[str, Any]:
        """Serialize one queue: exact total plus capped, enriched entries.

        Streak bounds cost three index seeks per row, so *with_streaks*
        is set only for the queues that report ``failing_since`` /
        ``last_pass_time`` — the others would pay for values nothing
        reads.
        """
        rows = storage.status_queue(
            kind, environment, limit=_SUMMARY_QUEUE_CAP,
            assignee=queue_assignee, stale_before=recent_cutoff,
            with_latest_comment=True,
        )
        entries = [
            _status_row_json(row, streak_for(row) if with_streaks else None)
            for row in rows
        ]
        if kind == "still_failing":
            # Oldest neglected regression first — the point of the queue.
            entries.sort(key=lambda entry: entry["failing_since"] or "")
        return {
            "total": storage.status_queue_count(
                kind, environment, assignee=queue_assignee,
                stale_before=recent_cutoff,
            ),
            "tests": entries,
        }

    queues = {
        kind: queue_json(kind, with_streaks=(kind in _STREAK_QUEUES))
        for kind in QUEUE_KINDS
    }
    # "My actions" must be filtered in SQL: picking a user's tests out of
    # a queue already capped at _SUMMARY_QUEUE_CAP would hide their work
    # behind other people's once the estate has more open items than the
    # cap. It reports streaks, so it goes through the same path.
    queues["mine"] = (
        queue_json("assigned", assignee, with_streaks=True) if assignee
        else {"total": 0, "tests": []}
    )

    status = estate.status
    return _json_response(
        200,
        {
            "generated_at": model.format_iso(current),
            "environment": environment,
            "environments": storage.environments(),
            "scripts": storage.scripts(environment),
            "assignees": storage.assignees(),
            "recent_hours": _SUMMARY_RECENT_HOURS,
            # The cutoff ITSELF, so the frontend stops recomputing it
            # from a fixed number of hours. It is what gates the offer
            # to retire a test, and that must never be based on a
            # window the server has already stopped using.
            "stale_before": model.format_iso(recent_cutoff),
            "latest_run_time": (
                None if latest_run is None
                else model.format_iso(latest_run)
            ),
            # Per environment, because they run SEQUENTIALLY and hours
            # apart: one estate-wide "last updated" is the newest of
            # them and says nothing about whichever one has not reported
            # yet. That is the case people actually want to check.
            "environment_updated": {
                environment_name: model.format_iso(when)
                for environment_name, when
                in storage.latest_run_time_by_environment().items()
            },
            "queue_cap": _SUMMARY_QUEUE_CAP,
            "status": {
                "total_tests": status.total_tests,
                "ran_recently": status.ran_recently,
                "not_run": status.not_run,
                "retired": status.retired,
                "results": _result_counts_json(status.results),
                "recent_results": _result_counts_json(
                    status.recent_results
                ),
                "new_failures": status.new_failures,
                "still_failing": status.still_failing,
                "fixed": status.fixed,
                "assigned_open": status.assigned_open,
            },
            "trend": {
                "days": days,
                "from": first_day.isoformat(),
                "to": current.date().isoformat(),
                "nights": nights,
            },
            "by_environment": [
                {
                    "environment": rollup.environment,
                    "total_tests": rollup.total_tests,
                    "failed": rollup.failed,
                    "new_failures": rollup.new_failures,
                    "unexpected_passes": rollup.unexpected_passes,
                    "not_run": rollup.not_run,
                }
                for rollup in estate.by_environment
            ],
            "top_failing_scripts": [
                {
                    "environment": entry.environment,
                    "script": entry.script,
                    "failing": entry.failing,
                }
                for entry in storage.top_failing_scripts(
                    environment, _SUMMARY_TOP_SCRIPTS
                )
            ],
            "queues": queues,
        },
    )


def _handle_test_detail(
    storage: Storage,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/tests/{env}/{script}/{test} — state + analytics summary."""
    latest = storage.latest_run(environment, script, test_name)
    if latest is None:
        raise _unknown_test(environment, script, test_name)
    now_dt = now()
    since = now_dt - datetime.timedelta(days=_ANALYTICS_MAX_DAYS)
    runs = storage.runs_since(
        environment, script, test_name, since, _ANALYTICS_MAX_RUNS
    )
    summary = analytics.compute_analytics(
        runs,
        now_dt,
        max_days=_ANALYTICS_MAX_DAYS,
        max_runs=_ANALYTICS_MAX_RUNS,
    )
    return _json_response(
        200,
        {
            "environment": environment,
            "script": script,
            "test_name": test_name,
            "source_link": latest.source_link,
            "assignee": storage.current_assignee(
                environment, script, test_name
            ),
            "latest": _run_json(latest),
            "analytics": analytics.analytics_to_dict(
                summary, max_runs=_ANALYTICS_MAX_RUNS
            ),
        },
    )


def _handle_history(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
) -> Response:
    """GET .../history — paginated run history, newest first, no outputs."""
    _require_test(storage, environment, script, test_name)
    limit = _DEFAULT_HISTORY_LIMIT
    raw_limit = _query_single(request.query, "limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise _HttpError(
                400,
                "limit: must be an integer, got '{}'".format(raw_limit),
            )
        if limit < 1 or limit > _MAX_HISTORY_LIMIT:
            raise _HttpError(
                400,
                "limit: must be between 1 and {} (got {})".format(
                    _MAX_HISTORY_LIMIT, limit
                ),
            )
    before = None  # type: Optional[datetime.datetime]
    raw_before = _query_single(request.query, "before")
    if raw_before is not None:
        try:
            before = model.parse_iso(raw_before)
        except ValueError as exc:
            raise _HttpError(400, "before: {}".format(exc))
    runs = storage.run_history(
        environment, script, test_name, limit=limit, before=before
    )
    return _json_response(200, {"runs": [_run_json(run) for run in runs]})


def _handle_run(storage: Storage, run_id_segment: str) -> Response:
    """GET /api/runs/{run_id} — one run INCLUDING its full output."""
    digits = (
        run_id_segment[1:]
        if run_id_segment.startswith("-")
        else run_id_segment
    )
    if not digits.isdigit():
        raise _HttpError(
            404,
            "unknown run id: '{}' (not an integer)".format(run_id_segment),
        )
    run = storage.get_run(int(run_id_segment))
    if run is None:
        raise _HttpError(
            404, "unknown run id: {}".format(run_id_segment)
        )
    payload = _run_json(run)
    payload["environment"] = run.environment
    payload["script"] = run.script
    payload["test_name"] = run.test_name
    payload["output"] = run.output
    return _json_response(200, payload)


def _handle_comments_list(
    storage: Storage, environment: str, script: str, test_name: str
) -> Response:
    """GET .../comments — the test's comment thread, oldest first."""
    _require_test(storage, environment, script, test_name)
    comments = storage.comments(environment, script, test_name)
    return _json_response(
        200, {"comments": [_comment_json(c) for c in comments]}
    )


def _handle_comment_create(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """POST .../comments — add a comment, implicitly creating the author."""
    _require_test(storage, environment, script, test_name)
    obj = _parse_json_object(request.body)
    username = _validate_username(obj, "username")
    text = _validate_comment_text(obj)
    comment = storage.add_comment(
        environment, script, test_name, username, text, now()
    )
    return _json_response(201, {"comment": _comment_json(comment)})


def _handle_assignee(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """PUT .../assignee — set or clear (null) the test's assignee."""
    _require_test(storage, environment, script, test_name)
    obj = _parse_json_object(request.body)
    if "username" not in obj:
        raise _HttpError(
            400,
            "username: required field is missing "
            "(use null to clear the assignee)",
        )
    assignee = None  # type: Optional[str]
    if obj["username"] is not None:
        assignee = _validate_username(obj, "username")
        # The picker will not offer a deactivated user, but the picker is
        # not the boundary — a stale page or a script would still get
        # through, and the resulting assignment is invisible to everyone.
        if not storage.is_active_user(assignee):
            raise _HttpError(
                400,
                "{} has been deactivated and cannot be assigned work. "
                "Reactivate the account first if this is "
                "intended.".format(assignee),
            )
    assigned_by = _validate_username(obj, "assigned_by")
    storage.set_assignee(
        environment, script, test_name, assignee, assigned_by, now()
    )
    return _json_response(200, {"assignee": assignee})


def _handle_script_executions(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/scripts/{env}/{script}/executions — recent runs of a suite.

    The estate is organised around individual tests, but people run and
    reason about whole scripts: "did last night's regression suite pass,
    and how did it compare with the night before?". A run record carries
    no batch id, so executions are inferred from the run timings — see
    :func:`testboard.analytics.group_executions`.

    Query parameters: ``days`` (1..90, default 14).
    """
    if not storage.script_exists(environment, script):
        raise _HttpError(
            404,
            "unknown script: no runs recorded for {} / {}".format(
                environment, script
            ),
        )
    days = _parse_int_param(
        request, "days", _EXECUTIONS_DEFAULT_DAYS, 1,
        _EXECUTIONS_MAX_DAYS,
    )
    since = now() - datetime.timedelta(days=days)
    runs = storage.script_runs(
        environment, script, since, _EXECUTIONS_MAX_RUNS
    )
    executions = analytics.group_executions(
        runs, gap_minutes=_EXECUTION_GAP_MINUTES
    )
    return _json_response(
        200,
        {
            "environment": environment,
            "script": script,
            "days": days,
            "gap_minutes": _EXECUTION_GAP_MINUTES,
            "run_count": len(runs),
            "truncated": len(runs) >= _EXECUTIONS_MAX_RUNS,
            # Newest execution first: that is the one being looked at.
            "executions": [
                {
                    "started": model.format_iso(execution.started),
                    "ended": model.format_iso(execution.ended),
                    "duration_seconds": round(
                        execution.duration_seconds, 3
                    ),
                    "total": execution.total,
                    "failed": execution.failed,
                    "results": _result_counts_json(execution.results),
                }
                for execution in reversed(executions)
            ],
        },
    )


def _handle_retired(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """PUT .../retired — approve a test as no longer in the suite, or undo it.

    Requires ``username`` and a non-empty ``comment``: a test vanishing
    from the suite is a decision someone made, and the next person to
    look needs to know who and why. The comment is appended to the
    test's normal thread.

    Retiring hides the test from the estate views — the headline counts,
    the triage queues and the default test list — so that a test which
    is genuinely gone stops being reported as missing. It does NOT hide
    its history: the detail page, run history and comments are
    unchanged. If the test reports a run again it is un-retired
    automatically.
    """
    _require_test(storage, environment, script, test_name)
    obj = _parse_json_object(request.body)
    username = _validate_username(obj, "username")
    if "retired" not in obj:
        raise _HttpError(400, "retired: required field is missing")
    retired = obj["retired"]
    if not isinstance(retired, bool):
        raise _HttpError(
            400,
            "retired: must be true or false, got {}".format(
                type(retired).__name__
            ),
        )
    comment = _validate_comment_text(obj, field="comment")
    record = storage.set_retired(
        environment, script, test_name, retired, username, comment, now()
    )
    return _json_response(
        200,
        {
            "retired": retired,
            "retired_by": username if retired else None,
            "comment": _comment_json(record),
        },
    )


def _handle_time(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/time — where the suite's runtime went, one level at a time.

    Query parameters: ``group_by`` (``environment`` | ``script`` |
    ``test_name``, default ``environment``), plus ``environment`` and
    ``script`` to scope a drill-down.

    Aggregates the newest run of each test, so it answers "where did the
    last run of the suite spend its time" — not a historical window.
    Retired tests and tests that have stopped reporting are excluded;
    the count of the latter is returned so the page can say so rather
    than quietly presenting a smaller number as the whole.
    """
    group_by = _query_single(request.query, "group_by") or "environment"
    environment = _query_single(request.query, "environment")
    script = _query_single(request.query, "script")
    # Off by default: counting a test that last ran three weeks ago as
    # part of "where the time went" claims time that was not spent. But
    # an all-or-nothing cutoff empties the page after any quiet day, so
    # it can be turned off deliberately and the page says which it is.
    include_stale = _query_single(
        request.query, "include_stale") in ("1", "true")
    cutoff = None if include_stale else _recent_cutoff(storage, now())
    try:
        rollup = storage.duration_rollup(
            group_by, cutoff, environment=environment, script=script
        )
    except ValueError as exc:
        raise _HttpError(400, "group_by: {}".format(exc))
    return _json_response(
        200,
        {
            "group_by": group_by,
            "environment": environment,
            "script": script,
            "items": [
                {
                    "key": item.key,
                    "total_seconds": round(item.total_seconds, 3),
                    "test_count": item.test_count,
                }
                for item in rollup.slices
            ],
            "total_seconds": round(rollup.total_seconds, 3),
            "test_count": rollup.test_count,
            "excluded_tests": rollup.excluded_tests,
            "include_stale": include_stale,
            "recent_hours": _SUMMARY_RECENT_HOURS,
            # The cutoff this page ACTUALLY filtered on. `recent_hours`
            # is the wall-clock fallback and is usually not the answer:
            # since WP-12 the line is derived from when the suite ran,
            # so a caption quoting 36 hours describes a window the
            # server stopped using.
            "stale_before": (
                None if cutoff is None else model.format_iso(cutoff)
            ),
        },
    )


def _pass_json(entry: analytics.Pass) -> Dict[str, Any]:
    """One inferred pass, as the environments page shows it."""
    return {
        "started": model.format_iso(entry.started),
        "ended": model.format_iso(entry.ended),
        "runs": entry.runs,
        "covered": entry.covered,
    }


def _handle_environments_list(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/environments — declared expectations, against reality.

    The declaration on its own is a number in a box. What makes it
    usable is the echo beside it: how many of the recent blocks of
    activity actually counted as passes of the suite, and whether the
    staleness line came from a pass at all.

    That is the whole point. A declared count that is too high fails
    SILENTLY — nothing clears the coverage bar, no pass counts, and the
    cutoff drops back to the 36-hour wall clock, which is the
    Monday-morning bug the derived cutoff exists to fix. ``covered: 0 of
    14`` and ``cutoff_from_passes: false`` are what that looks like from
    the outside, and without them nobody could tell.

    Costs one hour-bucketed query over a fortnight (a few hundred rows),
    the same one ``/api/summary`` already runs.
    """
    view = _pass_view(storage, now())
    shown = analytics.complete_passes(
        view.passes, view.floor, _PASS_GAP_HOURS
    )
    by_env = {}  # type: Dict[str, List[analytics.Pass]]
    for entry in shown:
        by_env.setdefault(entry.environment, []).append(entry)
    declared_rows = {
        row.environment: row
        for row in storage.list_environment_expectations()
    }

    items = []  # type: List[Dict[str, Any]]
    for environment in storage.known_environments():
        found = by_env.get(environment, [])
        row = declared_rows.get(environment)
        items.append({
            "environment": environment,
            "tests_seen": view.inferred.get(environment, 0),
            "expected_tests": None if row is None else row.expected_tests,
            "effective_expected": view.effective.get(environment, 0),
            "updated_at": (
                None if row is None else model.format_iso(row.updated_at)
            ),
            "updated_by": None if row is None else row.updated_by,
            "passes_total": len(found),
            "passes_covered": len([e for e in found if e.covered]),
            "latest_pass": _pass_json(found[-1]) if found else None,
        })

    return _json_response(
        200,
        {
            "environments": items,
            "cutoff": model.format_iso(view.cutoff.when),
            "cutoff_from_passes": view.cutoff.from_passes,
            "fallback": model.format_iso(view.fallback),
            "recent_hours": _SUMMARY_RECENT_HOURS,
            "coverage": _PASS_COVERAGE,
            "lookback_days": _PASS_LOOKBACK_DAYS,
        },
    )


def _parse_expected_tests(obj: Dict[str, Any]) -> Optional[int]:
    """Validate ``expected_tests``; None means "clear the declaration".

    ``bool`` is rejected explicitly because it is an ``int`` in Python,
    so ``{"expected_tests": true}`` would otherwise declare that the
    environment runs one test.
    """
    if "expected_tests" not in obj:
        raise _HttpError(400, "expected_tests: required field is missing")
    value = obj["expected_tests"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _HttpError(
            400,
            "expected_tests: must be a whole number or null, got "
            "{}".format(type(value).__name__),
        )
    if value < 1:
        raise _HttpError(
            400,
            "expected_tests: must be at least 1, got {} (send null to "
            "go back to inferring it)".format(value),
        )
    return value


def _handle_environment_expectation(
    storage: Storage,
    request: Request,
    environment: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """PUT /api/environments/{environment}/expectation — declare or clear.

    Body: ``{"expected_tests": int|null, "changed_by": str}``. ``null``
    deletes the declaration and returns to the inferred count.

    An environment that has never reported a run is a 404. Declaring one
    would affect nothing and a typo would leave a row nobody can see the
    purpose of; the listing includes declarations whose environment has
    since disappeared precisely so a rename can be cleaned up.
    """
    obj = _parse_json_object(request.body)
    expected = _parse_expected_tests(obj)
    changed_by = _validate_username(obj, "changed_by")

    if not storage.environment_exists(environment):
        raise _HttpError(
            404, "unknown environment: {}".format(environment)
        )

    if expected is None:
        cleared = storage.clear_environment_expectation(environment)
        return _json_response(
            200,
            {
                "environment": environment,
                "expected_tests": None,
                "cleared": cleared,
            },
        )
    record = storage.set_environment_expectation(
        environment, expected, changed_by, now()
    )
    return _json_response(
        200,
        {
            "environment": record.environment,
            "expected_tests": record.expected_tests,
            "updated_at": model.format_iso(record.updated_at),
            "updated_by": record.updated_by,
            "cleared": False,
        },
    )


def _handle_users_list(storage: Storage, request: Request) -> Response:
    """GET /api/users — users ordered by username.

    ACTIVE users only, unless ``include_inactive=1``. That default is
    what makes deactivation work with no frontend change at all: the
    assignee pickers already read this endpoint, so a deactivated user
    simply stops being offered.
    """
    include_inactive = _query_single(
        request.query, "include_inactive") in ("1", "true")
    users = storage.list_users(include_inactive=include_inactive)
    return _json_response(
        200,
        {
            "users": [_user_json(u) for u in users],
            "include_inactive": include_inactive,
        },
    )


def _handle_user_active(
    storage: Storage,
    request: Request,
    username: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """PUT /api/users/{username}/active — deactivate or reactivate.

    Body: ``{"active": bool, "changed_by": str}``.

    Deactivating a user who still owns live tests answers **409** and
    lists them. Letting it through would leave work assigned to a name
    that no picker offers — an invisible queue, which is the single most
    likely way to lose track of open work here. Reassigning first is a
    deliberate step, not a nuisance.

    Retired tests do not count: they are not in the suite, and
    retirement deliberately leaves an assignment in place, so counting
    them would block deactivation over work that no longer exists.
    """
    obj = _parse_json_object(request.body)
    if "active" not in obj:
        raise _HttpError(400, "active: required field is missing")
    if not isinstance(obj["active"], bool):
        raise _HttpError(
            400,
            "active: must be true or false, got {}".format(
                type(obj["active"]).__name__
            ),
        )
    active = obj["active"]
    changed_by = _validate_username(obj, "changed_by")

    if storage.get_user(username) is None:
        raise _HttpError(404, "unknown user: {}".format(username))

    if not active:
        total, sample = storage.open_assignments_held_by(username)
        if total:
            listed = ", ".join(
                "{}/{}/{}".format(*triple) for triple in sample
            )
            more = "" if total <= len(sample) else " (and {} more)".format(
                total - len(sample)
            )
            raise _HttpError(
                409,
                "{} still owns {} test{} that {} not been reassigned: "
                "{}{}. Reassign them first — otherwise the work stays "
                "assigned to a name nobody can select.".format(
                    username, total, "" if total == 1 else "s",
                    "has" if total == 1 else "have", listed, more,
                ),
            )

    user = storage.set_user_active(username, active, changed_by, now())
    return _json_response(200, {"user": _user_json(user)})


def _handle_users_create(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """POST /api/users — explicit (idempotent) user creation.

    A new user answers 201; an existing user answers 200 with its original
    ``created_at`` and ``"created": false``.
    """
    obj = _parse_json_object(request.body)
    username = _validate_username(obj, "username")
    user, created = storage.create_user(username, now())
    return _json_response(
        201 if created else 200,
        {"user": _user_json(user), "created": created},
    )


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------


def _route(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """Match the decoded path segments to a handler and dispatch.

    Raises :class:`_HttpError` for 404 (unknown route) and 405 (known
    route, wrong method — with an Allow header).
    """
    segments = _split_path(request.path)
    if not segments or segments[0] != "api":
        raise _HttpError(404, "not found")
    rest = segments[1:]

    if rest == ["import"]:
        _check_method(request.method, ("POST",))
        return _handle_import(storage, request)

    if rest == ["dashboard"]:
        _check_method(request.method, ("GET",))
        return _handle_dashboard(storage, request, now)

    if rest == ["summary"]:
        _check_method(request.method, ("GET",))
        return _handle_summary(storage, request, now)

    if rest == ["time"]:
        _check_method(request.method, ("GET",))
        return _handle_time(storage, request, now)

    if rest == ["environments"]:
        _check_method(request.method, ("GET",))
        return _handle_environments_list(storage, request, now)

    if (len(rest) == 3 and rest[0] == "environments"
            and rest[2] == "expectation"):
        _check_method(request.method, ("PUT",))
        return _handle_environment_expectation(
            storage, request, rest[1], now
        )

    if rest == ["users"]:
        _check_method(request.method, ("GET", "POST"))
        if request.method == "GET":
            return _handle_users_list(storage, request)
        return _handle_users_create(storage, request, now)

    if len(rest) == 3 and rest[0] == "users" and rest[2] == "active":
        _check_method(request.method, ("PUT",))
        return _handle_user_active(storage, request, rest[1], now)

    if len(rest) == 2 and rest[0] == "runs":
        _check_method(request.method, ("GET",))
        return _handle_run(storage, rest[1])

    if len(rest) == 4 and rest[0] == "scripts" and rest[3] == "executions":
        _check_method(request.method, ("GET",))
        return _handle_script_executions(
            storage, request, rest[1], rest[2], now
        )

    if len(rest) == 4 and rest[0] == "tests":
        _check_method(request.method, ("GET",))
        return _handle_test_detail(storage, rest[1], rest[2], rest[3], now)

    if len(rest) == 5 and rest[0] == "tests":
        environment, script, test_name, action = (
            rest[1],
            rest[2],
            rest[3],
            rest[4],
        )
        if action == "history":
            _check_method(request.method, ("GET",))
            return _handle_history(
                storage, request, environment, script, test_name
            )
        if action == "comments":
            _check_method(request.method, ("GET", "POST"))
            if request.method == "GET":
                return _handle_comments_list(
                    storage, environment, script, test_name
                )
            return _handle_comment_create(
                storage, request, environment, script, test_name, now
            )
        if action == "assignee":
            _check_method(request.method, ("PUT",))
            return _handle_assignee(
                storage, request, environment, script, test_name, now
            )
        if action == "retired":
            _check_method(request.method, ("PUT",))
            return _handle_retired(
                storage, request, environment, script, test_name, now
            )

    raise _HttpError(404, "not found")


def handle_api(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime] = model.utcnow,
) -> Response:
    """Handle one API request and ALWAYS return a JSON :class:`Response`.

    This is the single entry point the HTTP server calls for every path
    under ``/api``. *storage* is the injected storage layer; *now* is the
    clock (injectable for tests). Errors are JSON ``{"error": "message"}``
    bodies — never HTML: 400 for validation failures, 404 for unknown
    routes/resources, 405 for a known path with the wrong method (the
    response then carries an ``Allow`` header listing permitted methods),
    and 500 for unexpected internal failures (logged with traceback).
    """
    try:
        return _route(storage, request, now)
    except _HttpError as exc:
        return _json_response(
            exc.status, {"error": exc.message}, exc.headers
        )
    except Exception as exc:  # defensive: /api/* must never emit HTML
        _LOGGER.exception(
            "unhandled error handling %s %s", request.method, request.path
        )
        return _json_response(
            500, {"error": "internal server error: {}".format(exc)}
        )
