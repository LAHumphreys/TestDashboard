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
import re
import urllib.parse
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from testboard import analytics, model, site_notes
from testboard.model import Result, RunRecord, StoredRun, ValidationError
from testboard.storage import (
    COMPARE_CATEGORIES,
    DASHBOARD_SORTS,
    MAINLINE_STREAM_ID,
    QUEUE_KINDS,
    Comment,
    CompareCounts,
    CompareRow,
    FailureStreak,
    RollupCount,
    Storage,
    Stream,
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

# /api/timeline: how far back the block picker looks, and the cap on the
# per-execution run listing (a script execution is at most a few
# thousand runs; the cap is a backstop against a mis-sized window, and
# the response says when it bit).
#
# The max matches retention (~a year), so "view any recorded run's
# night" is a true sentence: the picker's "Earlier runs" reaches the
# whole file. Affordable because the block list reads activity_hours,
# never runs — a year of buckets is ~100k tiny rows, read only when
# somebody explicitly asks for the long view.
_TIMELINE_DEFAULT_DAYS = 14
_TIMELINE_MAX_DAYS = 365
_TIMELINE_MAX_RUNS = 5000

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


def _summary_row_json(
    row: TestSummaryRow, product: str = ""
) -> Dict[str, Any]:
    """Serialize one dashboard row to its JSON shape.

    *product* (WP-20) is the row's environment joined against
    ``environment_products`` by the caller — ``""`` for the implicit
    product. It is NOT a column on ``runs`` or ``latest_runs``
    (docs/STREAMS_PLAN.md §1 keeps product a read-time grouping of
    environments); this is a cheap dict lookup the caller does once per
    page, not a new join per row.
    """
    payload = {
        "environment": row.environment,
        "product": product,
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
        # WHERE the current assignment was made from (WP-21) — null for
        # mainline/pre-existing. Open Actions is the one place that
        # renders it (docs/STREAMS_PLAN.md §3.6); every other consumer
        # of this endpoint ignores the field, same as any additive one.
        "assignment_stream_id": row.assignment_stream_id,
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
    """Serialize a comment to its JSON shape.

    ``stream_id`` (WP-21) is the "posted from" tag — null for a comment
    with no stream context (posted before migration 9, or a plain
    triage comment never tied to an import).
    """
    return {
        "id": comment.comment_id,
        "author": comment.author,
        "created_at": model.format_iso(comment.created_at),
        "text": comment.text,
        "stream_id": comment.stream_id,
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

    WP-21 (docs/STREAMS_PLAN.md §3.3): a record may carry ``branch`` or
    ``build`` (mutually exclusive — a record with both is rejected at
    parse time by :func:`model.parse_run_record`, before storage ever
    sees it). The response gains ``streams_seen``: the sorted
    ``"{kind}:{name}"`` of every non-mainline stream this batch NAMED
    (whether or not every individual record on it was stored — see
    below), ``[]`` for a pure-mainline batch. A ``--branch``/``--build``
    feeder invocation must treat a response with no ``streams_seen`` key
    at all as a hard failure: an old server ignores unknown keys and
    would silently file branch runs into mainline, and this is the only
    signal that tells a new feeder it is talking to one.

    A second rejection channel, storage-side, joins the same ``errors``
    array: :meth:`Storage.upsert_runs` can refuse a record whose exact
    ``(environment, script, test_name, start_time)`` is already claimed
    by a DIFFERENT stream (the frozen v1 UNIQUE on ``runs`` has no
    stream column — docs/STREAMS_PLAN.md §3.2). ``UpsertRejection.index``
    is the record's position within the VALID list handed to storage,
    not the original ``runs`` index, so it is mapped back here.
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
    valid_indices = []  # type: List[int]
    errors = []  # type: List[Dict[str, Any]]
    for index, raw in enumerate(raw_runs):
        try:
            record = model.parse_run_record(raw)
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
            continue
        valid.append(record)
        valid_indices.append(index)

    streams_seen = set()  # type: Set[str]
    for record in valid:
        if record.branch:
            streams_seen.add("branch:" + record.branch)
        elif record.build:
            streams_seen.add("build:" + record.build)

    counts = storage.upsert_runs(valid)
    for rejection in counts.rejections:
        errors.append(
            {
                "index": valid_indices[rejection.index],
                "error": rejection.message,
                "environment": rejection.environment,
                "script": rejection.script,
                "test_name": rejection.test_name,
                "start_time": model.format_iso(rejection.start_time),
            }
        )

    return _json_response(
        200,
        {
            "inserted": counts.inserted,
            # On the wire, "updated" keeps meaning "accepted and already
            # known" — the sum every deployed feeder logs and reasons
            # about. "unchanged" REFINES it: the subset that was
            # byte-identical and wrote nothing (see UpsertCounts). The
            # site feeder re-pushes its window every 10 minutes, so
            # unchanged == updated is the healthy steady state, not a
            # stall.
            "updated": counts.updated + counts.unchanged,
            "unchanged": counts.unchanged,
            "rejected": len(errors),
            "errors": errors,
            "streams_seen": sorted(streams_seen),
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


def _filter_buckets(
    buckets: List[Tuple[str, datetime.datetime, int]],
    environments: Optional[Sequence[str]],
) -> List[Tuple[str, datetime.datetime, int]]:
    """Keep only the WP-20 ``product=`` scope's environments, in Python.

    ``storage.activity_buckets`` is deliberately NOT given a SQL filter
    for this: its whole result is a few hundred rows for a fortnight
    (see its docstring), so fetching it once and slicing the Python list
    is cheaper than adding another IN-clause plumbing path to a query
    every summary/dashboard/timeline request already runs unscoped.
    This is also what makes ``/api/watch`` affordable — one fetch, N
    cards each filtering their own slice (see :func:`_handle_watch`).

    ``None`` means "no filter", matching :meth:`Storage._environments_clause`.
    """
    if environments is None:
        return list(buckets)
    allowed = set(environments)
    return [bucket for bucket in buckets if bucket[0] in allowed]


def _pass_view(
    storage: Storage,
    now_value: datetime.datetime,
    environments: Optional[Sequence[str]] = None,
    stream_id: int = MAINLINE_STREAM_ID,
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

    *environments*, the WP-20 ``product=`` filter, scopes the passes
    (and therefore the cutoff) to one product's own environments — see
    :func:`_filter_buckets`. ``analytics.recent_cutoff`` already takes
    "the oldest across environments" from whatever passes it is given,
    so restricting the input passes is the whole mechanism: no change to
    that pure function, and a product's ``stale_before`` is provably its
    own rather than the whole estate's (docs/STREAMS_PLAN.md §2.3).

    *stream_id* (WP-23, migration 10, default mainline) scopes the SAME
    way: :func:`analytics.find_passes`/:func:`analytics.recent_cutoff`
    need no change at all — they are pure functions over whatever
    buckets and test counts they are handed, and restricting those
    inputs to one stream's own ``activity_hours``/``script_hours``
    partition (:meth:`Storage.activity_buckets`,
    :meth:`Storage.test_counts_by_environment`) is the entire mechanism,
    exactly as *environments* already demonstrates for products. This is
    what gives a long-running branch its OWN passes, its OWN cutoff, and
    (via the two clamps inside :func:`analytics.recent_cutoff`, both
    UNCHANGED) the same 36-hour floor and 14-day ceiling mainline has —
    per-stream, not shared, so a stream with sparse history cannot make
    another stream's cutoff stricter or looser.
    """
    fallback = now_value - datetime.timedelta(hours=_SUMMARY_RECENT_HOURS)
    floor = now_value - datetime.timedelta(days=_PASS_LOOKBACK_DAYS)
    inferred = storage.test_counts_by_environment(stream_id)
    declared = storage.declared_test_counts()
    effective = analytics.effective_test_counts(inferred, declared)
    buckets = _filter_buckets(
        storage.activity_buckets(floor, stream_id), environments
    )
    passes = analytics.find_passes(
        buckets,
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
    storage: Storage,
    now_value: datetime.datetime,
    environments: Optional[Sequence[str]] = None,
    stream_id: int = MAINLINE_STREAM_ID,
) -> datetime.datetime:
    """The staleness line alone, for the endpoints that only need it."""
    return _pass_view(storage, now_value, environments, stream_id).cutoff.when


def _resolve_product_environments(
    storage: Storage, product: Optional[str]
) -> Optional[List[str]]:
    """Turn a ``product=`` query parameter into an environment allow-list.

    ``None`` (no ``product=`` given) means "no filter" and is passed
    straight through as ``environments=None`` — every reader this
    touches treats that as "unscoped", the same as before WP-20. A given
    product resolves via :meth:`Storage.environments_for_product`,
    which is ``[]`` for an unknown product — and per
    docs/STREAMS_PLAN.md §2.6 that must read as an EMPTY RESULT, never a
    404: a product exists by having environments, the same rule
    :meth:`Storage.environment_exists` already applies.
    """
    if product is None:
        return None
    return storage.environments_for_product(product)


def _resolve_stream_id(
    storage: Storage, request: Request, param: str = "stream"
) -> int:
    """Parse an optional ``stream=`` query param into a stream id.

    WP-21 (docs/STREAMS_PLAN.md §3.5). Absent means mainline —
    ``/api/dashboard``, test detail and history all default there, so
    every deployed client that has never heard of streams keeps working
    unchanged. 400 on a non-integer value; 404 on an integer that names
    no stream (an id is opaque and typed by the caller, unlike a
    product/environment NAME, so there is no "empty result" reading —
    a stream either exists or the request is wrong).
    """
    raw = _query_single(request.query, param)
    if raw is None:
        return MAINLINE_STREAM_ID
    try:
        stream_id = int(raw)
    except ValueError:
        raise _HttpError(
            400, "{}: must be an integer, got '{}'".format(param, raw)
        )
    if storage.get_stream(stream_id) is None:
        raise _HttpError(
            404, "unknown stream: {}".format(stream_id)
        )
    return stream_id


def _validate_optional_stream_id(
    storage: Storage, obj: Dict[str, Any]
) -> Optional[int]:
    """Parse an optional ``stream_id`` body field — an ANNOTATION, not a
    scope (WP-21). Shared by comment creation and assignment: both
    record "posted/made from" this stream, never partition by it.
    Absent or ``null`` means no declared context, the same reading as
    every comment/assignment made before this feature. 400 on a
    non-integer, 404 on an id that names no stream.
    """
    raw_stream_id = obj.get("stream_id")
    if raw_stream_id is None:
        return None
    if not isinstance(raw_stream_id, int) or isinstance(
            raw_stream_id, bool):
        raise _HttpError(
            400,
            "stream_id: must be an integer or null, got {}".format(
                type(raw_stream_id).__name__
            ),
        )
    if storage.get_stream(raw_stream_id) is None:
        raise _HttpError(
            404, "unknown stream: {}".format(raw_stream_id)
        )
    return raw_stream_id


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
    product = _query_single(request.query, "product")
    environments = _resolve_product_environments(storage, product)
    stream_id = _resolve_stream_id(storage, request)

    stale_before = None  # type: Optional[datetime.datetime]
    if _query_single(request.query, "stale") in ("1", "true"):
        stale_before = _recent_cutoff(storage, now(), environments)
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
    # WP-21, Open Actions' branch/mainline origin filter — WHERE the
    # current assignment was made from, an axis entirely separate from
    # `stream=` above (which scopes the test's own result).
    assignment_origin = _query_single(request.query, "origin")
    if assignment_origin is not None and assignment_origin not in (
            "branch", "mainline"):
        raise _HttpError(
            400,
            "origin: must be 'branch' or 'mainline', got '{}'".format(
                assignment_origin),
        )

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
        "environments": environments,
        "stream_id": stream_id,
        "assignment_origin": assignment_origin,
    }  # type: Dict[str, Any]
    rows = storage.dashboard(
        sort=sort, descending=(order == "desc"), limit=limit,
        offset=offset, with_latest_comment=with_comment, **filters
    )
    # One tiny lookup, not a join per row: the same map every OTHER
    # product-aware endpoint reads from environment_products.
    env_to_product = storage.environment_products_map()
    payload = [
        _summary_row_json(row, env_to_product.get(row.environment, ""))
        for row in rows
    ]
    if with_streak:
        _add_streaks(storage, rows, payload, now(), stream_id)
    # WP-21: batch-resolve every DISTINCT assignment_stream_id on THIS
    # PAGE to its identity — the same shape the comments endpoint uses,
    # needed here so Open Actions can render a "branch feat/x" tag
    # without a lookup per row.
    assignment_stream_ids = sorted({
        row.assignment_stream_id for row in rows
        if row.assignment_stream_id is not None
    })
    streams = (
        storage.stream_identities(assignment_stream_ids)
        if assignment_stream_ids else {}
    )
    return _json_response(
        200,
        {
            "tests": payload,
            "total": storage.dashboard_count(**filters),
            "limit": limit,
            "offset": offset,
            "with_streak": with_streak,
            "product": product,
            "stream": stream_id,
            "streams": {
                str(sid): _stream_json(s) for sid, s in streams.items()
            },
        },
    )


def _add_streaks(
    storage: Storage,
    rows: Sequence[TestSummaryRow],
    payload: List[Dict[str, Any]],
    now_value: datetime.datetime,
    stream_id: int = MAINLINE_STREAM_ID,
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
    Storage.recent_results), never one per row. *stream_id* (WP-21,
    default mainline) scopes both to the SAME stream the page came from
    — a branch-scoped dashboard's streaks must be that branch's own
    history, never mainline's.
    """
    triples = [
        (row.environment, row.script, row.test_name) for row in rows
    ]
    since = now_value - datetime.timedelta(days=_STABILITY_WINDOW_DAYS)
    history = storage.recent_results(
        triples, since, per_test_limit=_STABILITY_RUNS,
        stream_id=stream_id)
    for row, item in zip(rows, payload):
        key = (row.environment, row.script, row.test_name)
        if row.result is Result.FAIL:
            streak = storage.failure_streak_bounds(
                row.environment, row.script, row.test_name, row.start_time,
                stream_id=stream_id,
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
    row: TestStatusRow,
    streak: Optional[FailureStreak],
    product: str = "",
) -> Dict[str, Any]:
    """Serialize one queue entry (a status row plus optional streak info).

    *product* — see :func:`_summary_row_json`.
    """
    failing_since = None  # type: Optional[datetime.datetime]
    last_pass = None  # type: Optional[datetime.datetime]
    if streak is not None:
        failing_since = streak.failing_since
        last_pass = streak.last_pass_before
    return {
        "environment": row.environment,
        "product": product,
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


def _summary_queue_json(
    storage: Storage,
    kind: str,
    environment: Optional[str],
    assignee: Optional[str],
    recent_cutoff: datetime.datetime,
    streaks: Dict[Tuple[str, str, str], FailureStreak],
    environments: Optional[Sequence[str]] = None,
    env_to_product: Optional[Dict[str, str]] = None,
    stream_id: int = MAINLINE_STREAM_ID,
    queue_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Serialize one triage queue: exact total plus capped, enriched entries.

    *kind* is any storage queue kind, or ``"mine"`` — the assignee's
    open items, which resolves to the ``assigned`` predicate filtered to
    *assignee* in SQL (picking a user's tests out of an already-capped
    queue would hide their work behind other people's) and is empty
    without an assignee.

    Streak bounds cost three index seeks per row; computed for the
    queues that report ``failing_since``/``last_pass_time``
    (``still_failing`` and ``mine``) in ONE batched call
    (:meth:`Storage.failure_streak_bounds_many`, WP-23 perf pass) rather
    than one round trip per FAIL row — a queue page is capped at
    ``_SUMMARY_QUEUE_CAP`` rows, so the batch is bounded the same way. A
    test can sit in both queues; *streaks* is the caller's cache so one
    request looks each test up once.

    *env_to_product* — the caller's own :meth:`Storage.environment_products_map`
    call, threaded through rather than re-fetched per queue (there are up
    to seven of these in one ``/api/summary`` response).

    *stream_id* (WP-23, default mainline): a long-running branch's "own
    results" tab reads its OWN triage queues by passing its stream id —
    never merged with mainline's (docs/STREAMS_PLAN.md §5.2). Streak
    bounds are scoped the same way, so a branch's failing-since is never
    inherited from mainline's history of the same triple.

    *queue_counts* (WP-23 perf pass): the caller's own precomputed
    :meth:`Storage.queue_counts` result, when it already has one —
    ``_handle_summary``'s full/headline payload fetches every kind's
    total in ONE grouped query and threads it through here instead of a
    second, per-kind :meth:`Storage.status_queue_count` call for the
    ``"total"`` field. Absent (the single-queue ``parts=queue`` path,
    which only ever wants ONE kind's total), this falls back to the
    original single-kind query — computing all six kinds' counts there
    would cost MORE than the one count actually needed.
    """
    products = env_to_product or {}
    queue_assignee = None  # type: Optional[str]
    storage_kind = kind
    if kind == "mine":
        if not assignee:
            return {"total": 0, "tests": []}
        storage_kind = "assigned"
        queue_assignee = assignee
    with_streaks = kind in _STREAK_QUEUES or kind == "mine"

    rows = storage.status_queue(
        storage_kind, environment, limit=_SUMMARY_QUEUE_CAP,
        assignee=queue_assignee, stale_before=recent_cutoff,
        with_latest_comment=True, environments=environments,
        stream_id=stream_id,
    )
    if with_streaks:
        needed = [
            (row.environment, row.script, row.test_name, row.start_time)
            for row in rows
            if row.result is Result.FAIL
            and (row.environment, row.script, row.test_name) not in streaks
        ]
        if needed:
            streaks.update(
                storage.failure_streak_bounds_many(needed, stream_id)
            )
    entries = [
        _status_row_json(
            row,
            (
                streaks.get((row.environment, row.script, row.test_name))
                if with_streaks and row.result is Result.FAIL else None
            ),
            products.get(row.environment, ""),
        )
        for row in rows
    ]
    if kind == "still_failing":
        # Oldest neglected regression first — the point of the queue.
        entries.sort(key=lambda entry: entry["failing_since"] or "")
    total = (
        queue_counts[kind] if queue_counts is not None
        else storage.status_queue_count(
            storage_kind, environment, assignee=queue_assignee,
            stale_before=recent_cutoff, environments=environments,
            stream_id=stream_id,
        )
    )
    return {
        "total": total,
        "tests": entries,
    }


def _summary_queue_totals(
    storage: Storage,
    environment: Optional[str],
    assignee: Optional[str],
    recent_cutoff: datetime.datetime,
    environments: Optional[Sequence[str]] = None,
    stream_id: int = MAINLINE_STREAM_ID,
) -> Dict[str, int]:
    """Exact size of every queue, without fetching a single row.

    This is what lets the headline part paint the triage tab counts
    while the row payloads are still loading. WP-23 perf pass: used to
    be seven separate indexed COUNT queries over ``latest_runs``; now
    one grouped pass (:meth:`Storage.queue_counts`) — see its docstring
    for the measured before/after. *stream_id* (WP-23, default
    mainline) — see :func:`_summary_queue_json`.
    """
    return storage.queue_counts(
        environment, assignee, stale_before=recent_cutoff,
        environments=environments, stream_id=stream_id,
    )


#: Valid values of /api/summary's ``parts`` parameter.
_SUMMARY_PARTS = ("headline", "queue")


def _products_summary(
    storage: Storage,
    recent_cutoff: datetime.datetime,
    rollup_rows: Optional[List[RollupCount]] = None,
) -> List[Dict[str, Any]]:
    """The ``products[]`` breakdown of ``/api/summary`` (WP-20 §2.2).

    ALWAYS estate-wide, regardless of the request's own ``product=``/
    ``environment=`` scope: a request scoped to product A must still be
    told product B exists, or the switcher has no way back to "All
    products" (the frontend's ``>= 2`` visibility test reads this list).
    Empty when nobody has declared a product — the frontend's key for
    "do not show any of this" (docs/STREAMS_PLAN.md §2.2).

    *recent_cutoff* only affects a column :func:`analytics.summarize_by_product`
    ignores (none of its four counts is recency-gated), so the caller's
    own cutoff is fine to reuse here — no second cutoff computation.

    *rollup_rows* (WP-23 perf pass): this needs the estate-wide MAINLINE
    rollup — unconditionally, regardless of the request's own scope. The
    caller's own ``summary_rollup`` call already fetched exactly that
    same, byte-identical row set whenever the REQUEST is itself unscoped
    (no ``environment=``, no ``product=``, mainline ``stream=`` — the
    common case, and what a plain ``GET /api/summary`` load actually is);
    threading those rows through here avoids a second, redundant query
    that measured as literally the same SQL statement with the same
    parameters. A scoped request's own rollup is NOT the same data (a
    different environment/stream partition), so the caller passes
    ``None`` there and this fetches its own, exactly as before — see
    ``_handle_summary``'s call site for which case is which.
    """
    products = storage.distinct_products()
    if not products:
        return []
    rows = (
        rollup_rows if rollup_rows is not None
        else storage.summary_rollup(recent_cutoff)
    )
    by_product = {
        row.product: row
        for row in analytics.summarize_by_product(
            rows, storage.environment_products_map(),
        )
    }
    zero = analytics.ProductRollup(
        product="", failing=0, new_failures=0, fixed=0, unexpected_passes=0
    )
    return [
        {
            "product": product,
            "failing": by_product.get(product, zero).failing,
            "new_failures": by_product.get(product, zero).new_failures,
            "fixed": by_product.get(product, zero).fixed,
            "unexpected_passes": by_product.get(
                product, zero).unexpected_passes,
        }
        for product in products
    ]


def _handle_summary(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/summary — the home-screen estate summary.

    Query parameters: ``environment`` (optional exact match) scopes
    everything; ``days`` (1..90, default 14) sets the trend window;
    ``assignee`` adds the ``mine`` queue for that user. ``stream=`` (an
    id, WP-23, default mainline) scopes ``status``/``trend``/every
    queue/``queue_totals``/``top_failing_scripts``/
    ``environment_updated``/``latest_run_time`` to one stream's OWN
    partition — the "own results" tab of a long-running branch stream's
    dashboard (docs/STREAMS_PLAN.md §5.2) is this same endpoint with
    ``stream=<its id>``. ``products``/``assignees``/``assignment_streams``
    stay ALWAYS estate-wide regardless — they are catalogs of every
    product/assignee that exists, not one stream's or one product's
    results, and the switcher/filters they feed need the full list to
    offer a way back to "All products"/"Everyone". ``environments``/
    ``scripts``/``environment_updated``, by contrast, ARE narrowed by
    ``product=`` (and, absent that, by a scoped ``stream=``'s own
    declared product) — this was a real bug, found live:
    ``?product=Atlas`` and ``?product=Beacon`` returned the identical
    ``environments`` list, offering each other's environments in the
    picker. Absent both, every deployed caller from before this drop
    sees no change.

    ``parts`` slices the payload so the home screen can paint
    progressively instead of waiting for its slowest piece:

    - absent — the full payload, exactly the pre-split shape plus
      ``queue_totals``;
    - ``parts=headline`` — everything EXCEPT the queue row payloads
      (status, trend, rollups, filters, ``queue_totals`` for the tab
      badges);
    - ``parts=queue&queue=<kind>`` — one queue's rows. ``kind`` is a
      :data:`testboard.storage.QUEUE_KINDS` entry or ``mine``.

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
    part = _query_single(request.query, "parts")
    if part is not None and part not in _SUMMARY_PARTS:
        raise _HttpError(
            400,
            "parts: unknown value '{}' (expected one of {})".format(
                part, ", ".join(_SUMMARY_PARTS)
            ),
        )
    product = _query_single(request.query, "product")
    environments = _resolve_product_environments(storage, product)
    # WP-23: a long-running branch's "own results" tab is this same
    # endpoint, scoped — absent, the default (mainline) means every
    # deployed caller from before this drop sees no change at all.
    stream_id = _resolve_stream_id(storage, request)

    # WP-23 fix: the CATALOG fields below (environments/scripts/
    # environment_updated) must be scoped the same way product=
    # already scopes the estate counts — found live, "product=Atlas
    # and product=Beacon return the same environments list", because
    # environments()/scripts()/latest_run_time_by_environment() never
    # looked at product scope at all. A stream scoped with no explicit
    # product= resolves to ITS OWN declared product (fixed at stream
    # creation, WP-21) rather than staying unscoped — a branch
    # dashboard's own tab must not offer every product's environments
    # either. Deliberately kept SEPARATE from `environments` above:
    # that variable also scopes the numeric estate rollups below, which
    # are already correctly narrowed by stream_id alone for a
    # stream-scoped request — widening THAT filter too would be a
    # different, unrequested behaviour change.
    catalog_environments = environments
    if catalog_environments is None and stream_id != MAINLINE_STREAM_ID:
        scoped_stream = storage.get_stream(stream_id)
        if scoped_stream is not None:
            catalog_environments = storage.environments_for_product(
                scoped_stream.product
            )
    # One lookup, threaded through every queue below rather than
    # re-fetched per queue (see _summary_queue_json's docstring).
    env_to_product = storage.environment_products_map()

    current = now()
    # A product's own window, never the whole estate's: `environments`
    # scopes which passes count, so a scoped request's stale_before is
    # provably that product's own (docs/STREAMS_PLAN.md §2.3 — "never
    # one wall-clock phrase across products"). *stream_id* scopes the
    # same way, one level further: a branch's stale_before is provably
    # its OWN, never mainline's or another branch's (§5.2) — the two
    # clamps inside analytics.recent_cutoff (36h floor, 14-day ceiling)
    # apply to this stream's own passes, unchanged.
    pass_view = _pass_view(storage, current, environments, stream_id)
    recent_cutoff = pass_view.cutoff.when
    # WP-23: how many COVERED passes this stream has completed in the
    # lookback window — the data behind the branch dashboard's
    # own-results-vs-diff default (docs/STREAMS_PLAN.md §5.2 says the
    # heuristic "must be stated in the UI caption, not buried"; this is
    # the number that caption is built from, never a constant the
    # frontend invents). Estate-wide/mainline callers get it too (it
    # costs nothing extra — pass_view is already computed) but have no
    # use for it.
    covered_passes = sum(1 for entry in pass_view.passes if entry.covered)

    if part == "queue":
        kind = _query_single(request.query, "queue")
        valid_kinds = QUEUE_KINDS + ("mine",)
        if kind not in valid_kinds:
            raise _HttpError(
                400,
                "queue: unknown value '{}' (expected one of {})".format(
                    kind, ", ".join(valid_kinds)
                ),
            )
        return _json_response(
            200,
            {
                "generated_at": model.format_iso(current),
                "environment": environment,
                "product": product,
                "stream": stream_id,
                "stale_before": model.format_iso(recent_cutoff),
                "queue_cap": _SUMMARY_QUEUE_CAP,
                "kind": kind,
                "queue": _summary_queue_json(
                    storage, kind, environment, assignee, recent_cutoff,
                    {}, environments=environments,
                    env_to_product=env_to_product, stream_id=stream_id,
                ),
            },
        )
    # Reported so a stalled feeder is visible AS a stalled feeder,
    # rather than as every test in the estate quietly going stale — the
    # failure mode a data-derived cutoff would otherwise hide.
    latest_run = storage.latest_run_time(stream_id)
    estate_rollup = storage.summary_rollup(
        recent_cutoff, environment, environments=environments,
        stream_id=stream_id,
    )
    estate = analytics.summarize_rollup(
        estate_rollup,
        storage.assigned_open_count(
            environment, environments=environments, stream_id=stream_id,
        ),
    )
    # WP-23 perf pass: _products_summary always needs the estate-wide
    # MAINLINE rollup, regardless of this request's own scope. When the
    # request itself IS that exact scope (no environment=, no product=,
    # mainline stream=) -- the common, unscoped home-screen load --
    # estate_rollup above IS that same query, so it is threaded through
    # rather than fetched a second time. A genuinely scoped request
    # (environment=/product=/stream=) reads different data here, so
    # _products_summary fetches its own (pass None -- see its docstring).
    products_rollup = (
        estate_rollup
        if environment is None and environments is None
        and stream_id == MAINLINE_STREAM_ID
        else None
    )

    # Trend: per-night result counts, zero-filled over the window so the
    # chart's x-axis is continuous even on nights nothing ran.
    first_day = current.date() - datetime.timedelta(days=days - 1)
    since = datetime.datetime.combine(first_day, datetime.time())
    counts = {}  # type: Dict[Tuple[datetime.date, Result], int]
    for entry in storage.daily_result_counts(
        since, environment, environments=environments, stream_id=stream_id,
    ):
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

    status = estate.status
    # WP-21: every non-mainline stream currently annotating an
    # assignment, resolved to its identity and ordered by id (dict
    # iteration order is not a promised sort) — Open Actions' origin
    # filter reads this the same way it already reads `assignees`
    # below: the available VALUES to filter by, and (empty list) the
    # "zero visible change" signal that no assignment carries a stream.
    assignment_stream_ids = storage.assignment_stream_ids()
    assignment_streams_by_id = storage.stream_identities(
        assignment_stream_ids)
    assignment_streams = [
        _stream_json(assignment_streams_by_id[sid])
        for sid in assignment_stream_ids
        if sid in assignment_streams_by_id
    ]
    # WP-23 perf pass: every queue's exact total, ONE grouped query
    # (Storage.queue_counts) — reused below for both the headline
    # tab-badge field and, in the full-payload branch, every queue's own
    # "total" (queue_counts= on _summary_queue_json) instead of each one
    # re-querying its count a second time. See _summary_queue_totals's
    # and Storage.queue_counts's docstrings for the measured before/after.
    queue_totals = _summary_queue_totals(
        storage, environment, assignee, recent_cutoff,
        environments=environments, stream_id=stream_id,
    )
    payload = {
            "generated_at": model.format_iso(current),
            "environment": environment,
            "product": product,
            # WP-23: absent (mainline) for every deployed client. The
            # "own results" tab's own request carries its stream's id
            # and reads EVERY field below (status/trend/queues) scoped
            # to it; the cross-stream/catalog fields just below
            # (products, environments, scripts, assignees,
            # assignment_streams) are ALWAYS estate-wide regardless —
            # see their own comments.
            "stream": stream_id,
            # WP-23: covered passes THIS STREAM completed in the 14-day
            # lookback — the branch dashboard's own-results-vs-diff
            # default tab is built from this number, stated plainly in
            # its caption (docs/STREAMS_PLAN.md §5.2).
            "covered_passes": covered_passes,
            # ALWAYS estate-wide (see _products_summary) — this is what
            # lets a request scoped to one product still offer the
            # switcher's way back to "All products". Empty list = no
            # products declared, the frontend's signal to show nothing
            # product-shaped at all.
            "products": _products_summary(
                storage, recent_cutoff, products_rollup),
            "environments": storage.environments(
                environments=catalog_environments),
            "scripts": storage.scripts(
                environment, environments=catalog_environments),
            "assignees": storage.assignees(),
            # WP-21: every non-mainline stream currently annotating an
            # assignment, resolved to its identity — Open Actions' origin
            # filter reads this the same way it already reads
            # `assignees` above: the available VALUES to filter by, and
            # (empty list) the "zero visible change" signal that no
            # assignment carries a stream at all.
            "assignment_streams": assignment_streams,
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
                in storage.latest_run_time_by_environment(
                    stream_id, environments=catalog_environments).items()
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
                    environment, _SUMMARY_TOP_SCRIPTS,
                    environments=environments, stream_id=stream_id,
                )
            ],
            # Every queue's exact size, row payloads not included. The
            # headline part's reason to exist: tab badges paint from
            # these while the rows are still being fetched. WP-23 perf
            # pass: this SAME dict is threaded into the full payload's
            # per-queue "total" fields below too (queue_counts=), rather
            # than each one re-querying its own count a second time.
            "queue_totals": queue_totals,
        }  # type: Dict[str, Any]
    if part == "headline":
        return _json_response(200, payload)

    # Full payload: the pre-split shape (plus queue_totals above), for
    # callers that want one round trip — and for the contract tests,
    # which pin it so the split cannot drift from the whole.
    streaks = {}  # type: Dict[Tuple[str, str, str], FailureStreak]
    queues = {
        kind: _summary_queue_json(
            storage, kind, environment, None, recent_cutoff, streaks,
            environments=environments, env_to_product=env_to_product,
            stream_id=stream_id, queue_counts=queue_totals,
        )
        for kind in QUEUE_KINDS
    }
    queues["mine"] = _summary_queue_json(
        storage, "mine", environment, assignee, recent_cutoff, streaks,
        environments=environments, env_to_product=env_to_product,
        stream_id=stream_id, queue_counts=queue_totals,
    )
    payload["queues"] = queues
    return _json_response(200, payload)


def _handle_test_detail(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/tests/{env}/{script}/{test} — state + analytics summary.

    ``stream=`` (WP-21, default mainline) scopes BOTH the latest run and
    the analytics window to one stream's own runs of the triple — a
    branch's "latest" must be a branch run, and its failing-since must
    not be inherited from mainline's history of the same test
    (docs/STREAMS_PLAN.md §3.5).
    """
    stream_id = _resolve_stream_id(storage, request)
    # Only fetched when scoped away from mainline: the compare strip
    # needs a stream's kind/name to LABEL itself, and mainline's own
    # detail view never draws one — so the hot, unscoped path (every
    # test-detail visit that never touches WP-21) costs no extra query.
    stream = (
        None if stream_id == MAINLINE_STREAM_ID
        else storage.get_stream(stream_id)
    )
    latest = storage.latest_run(
        environment, script, test_name, stream_id=stream_id)
    if latest is None:
        raise _unknown_test(environment, script, test_name)
    now_dt = now()
    since = now_dt - datetime.timedelta(days=_ANALYTICS_MAX_DAYS)
    runs = storage.runs_since(
        environment, script, test_name, since, _ANALYTICS_MAX_RUNS,
        stream_id=stream_id,
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
            "stream": stream_id,
            "stream_identity": None if stream is None else _stream_json(
                stream),
        },
    )


def _handle_test_streams(
    storage: Storage, environment: str, script: str, test_name: str,
) -> Response:
    """GET .../streams — this triple's latest result on every stream that
    has one, newest first (WP-22, docs/STREAMS_PLAN.md §4.1).

    Two frontend consumers share this one payload rather than each
    getting a bespoke shape: the test page's "Every build" disclosure
    (which additionally unions in the product's FULL stream list,
    client-side, to render NO RESULT rows for the streams absent here
    — see :meth:`Storage.stream_results_for_triple`'s docstring for why
    that union is the caller's job, not this endpoint's) and the stream
    dropdown next to it, which wants exactly this list and nothing more
    — a dropdown entry with no result to show is not useful.

    ``product`` (WP-22) is the ENVIRONMENT's own declared product — not
    necessarily any entry in ``results``, since a triple with no
    non-mainline runs yet has only a mainline row, and the mainline
    stream's own ``product`` is always ``""`` regardless of what this
    environment is actually mapped to. Without this field the "Every
    build" table would have no way to ask ``GET /api/streams?product=``
    for the union it needs.

    404 if the triple has never run ANYWHERE — the same rule
    :func:`_handle_history` already follows, so a typo'd triple reads
    as "no such test" rather than "ran nowhere".
    """
    _require_test(storage, environment, script, test_name)
    results = storage.stream_results_for_triple(
        environment, script, test_name
    )
    return _json_response(
        200,
        {
            "environment": environment,
            "script": script,
            "test_name": test_name,
            "product": storage.product_for_environment(environment),
            "results": [
                {
                    "stream": _stream_json(entry.stream),
                    "result": entry.result.value,
                    "run_id": entry.run_id,
                    "start_time": model.format_iso(entry.start_time),
                }
                for entry in results
            ],
        },
    )


def _handle_history(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
) -> Response:
    """GET .../history — paginated run history, newest first, no outputs.

    ``stream=`` (WP-21, default mainline) — the history table is one
    stream's runs of the triple, never a mix.
    """
    _require_test(storage, environment, script, test_name)
    stream_id = _resolve_stream_id(storage, request)
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
        environment, script, test_name, limit=limit, before=before,
        stream_id=stream_id,
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
    """GET .../comments — the test's comment thread, oldest first.

    Comments are never filtered by the page's current ``stream=`` scope
    (docs/STREAMS_PLAN.md §3.6: "comments always shown in full with
    their posted-from tag") — a thread is one conversation regardless of
    which stream someone was looking at when they wrote a line of it.
    ``streams`` resolves every DISTINCT non-null ``comment.stream_id``
    on the thread to its identity in ONE extra query
    (:meth:`Storage.stream_identities`, the same batched read the
    Watchlist's ``s:`` cards use) — the "posted from mainline" tag needs
    a stream's kind/name, not just its id, and a per-comment lookup
    would cost one query per comment on a thread with mixed history.
    Absent from the map: a comment with ``stream_id: null`` (posted
    before WP-21, or with no declared context) — the frontend renders
    no tag at all for those, never a fabricated "mainline".
    """
    _require_test(storage, environment, script, test_name)
    comments = storage.comments(environment, script, test_name)
    stream_ids = sorted({
        c.stream_id for c in comments if c.stream_id is not None
    })
    streams = storage.stream_identities(stream_ids) if stream_ids else {}
    return _json_response(
        200,
        {
            "comments": [_comment_json(c) for c in comments],
            "streams": {
                str(sid): _stream_json(stream)
                for sid, stream in streams.items()
            },
        },
    )


def _handle_comment_create(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
    test_name: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """POST .../comments — add a comment, implicitly creating the author.

    Body gains an optional ``stream_id`` (WP-21) — "posted from", not
    part of the comment's identity. Absent or ``null`` means no stream
    context, the same as every comment posted before this migration.
    """
    _require_test(storage, environment, script, test_name)
    obj = _parse_json_object(request.body)
    username = _validate_username(obj, "username")
    text = _validate_comment_text(obj)
    stream_id = _validate_optional_stream_id(storage, obj)
    comment = storage.add_comment(
        environment, script, test_name, username, text, now(),
        stream_id=stream_id,
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
    """PUT .../assignee — set or clear (null) the test's assignee.

    Body gains an optional ``stream_id`` (WP-21) — WHERE the assignment
    was made from, not part of its identity: the frontend sends the
    page's current stream scope when set (docs/STREAMS_PLAN.md §3.6's
    "triage still works from a branch"), never a partition of who owns
    the test. Absent or ``null`` means mainline, or a client that has
    not heard of streams — every assignment made before this feature.
    """
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
    stream_id = _validate_optional_stream_id(storage, obj)
    storage.set_assignee(
        environment, script, test_name, assignee, assigned_by, now(),
        stream_id=stream_id,
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

    Query parameters: ``days`` (1..90, default 14), ``stream=`` (script-
    page parity, default mainline — the same param `/api/scripts/{env}/
    {script}/runs` has accepted since F7). ``storage.script_runs()``
    already carries a ``stream_id`` predicate in its SQL for every
    caller, mainline included — passing the resolved value through here
    changes what that predicate matches, not the query's shape at all,
    so this endpoint's own EXPLAIN QUERY PLAN is unaffected by this
    change (measured, see docs/STREAMS_PLAN.md §5.4 "as built").
    ``script_exists()`` deliberately stays UNSCOPED (matches ``/runs``'s
    own existing behaviour): a script's identity is not partitioned by
    stream, only its runs are.
    """
    if not storage.script_exists(environment, script):
        raise _HttpError(
            404,
            "unknown script: no runs recorded for {} / {}".format(
                environment, script
            ),
        )
    stream_id = _resolve_stream_id(storage, request)
    # Same reason /api/time and /api/timeline fetch this (F7): the page
    # needs a stream's kind/name to render its own branch band, only
    # paid when scoped away from mainline.
    stream = (
        None if stream_id == MAINLINE_STREAM_ID
        else storage.get_stream(stream_id)
    )
    days = _parse_int_param(
        request, "days", _EXECUTIONS_DEFAULT_DAYS, 1,
        _EXECUTIONS_MAX_DAYS,
    )
    since = now() - datetime.timedelta(days=days)
    runs = storage.script_runs(
        environment, script, since, _EXECUTIONS_MAX_RUNS,
        stream_id=stream_id,
    )
    executions = analytics.group_executions(
        runs, gap_minutes=_EXECUTION_GAP_MINUTES
    )
    return _json_response(
        200,
        {
            "environment": environment,
            "script": script,
            "stream": stream_id,
            "stream_identity": None if stream is None else _stream_json(
                stream),
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


def _parse_iso_param(request: Request, name: str) -> datetime.datetime:
    """Parse a REQUIRED ISO-8601 query parameter, 400 when absent or bad."""
    raw = _query_single(request.query, name)
    if raw is None:
        raise _HttpError(400, "{}: required parameter is missing".format(
            name))
    try:
        return model.parse_iso(raw)
    except ValueError:
        raise _HttpError(
            400,
            "{}: must be an ISO-8601 UTC timestamp "
            "(YYYY-MM-DDTHH:MM:SS.ffffff), got '{}'".format(name, raw),
        )


def _handle_timeline(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/timeline — one environment's script running order.

    The problem this answers: a script misbehaves, leaves shared state
    dirty, and the visible symptom is a DIFFERENT script failing later.
    Walking backwards from the failure needs the night's running order,
    script by script — which the test-centric views cannot show.

    Query parameters: ``environment`` (required, UNLESS ``product``
    resolves to exactly one environment — see below), ``days`` (1..90,
    default 14, how far back the block picker looks), and ``from`` /
    ``to`` (optional ISO timestamps selecting the block of activity to
    expand into rows; both or neither). Without ``from``/``to`` the
    newest block is selected.

    ``product`` (WP-20) exists here only through the existing
    ``environment`` semantics, per docs/STREAMS_PLAN.md §2.3: this page
    shows ONE environment's running order, so a product with several
    environments still needs one picked. When ``environment`` is absent
    and ``product`` resolves to exactly one environment, that one is
    used; otherwise the ordinary "environment: required" 400 stands —
    there is no 400 for "environment does not belong to product": an
    explicit ``environment`` always wins, matching how the switcher and
    the environment filter already coexist everywhere else.

    ``blocks`` are the same inferred blocks of activity the
    environments page shows (:func:`analytics.find_passes` — ad-hoc
    re-run blocks included, labelled by ``covered``, because a
    twenty-test re-run after a fix is often exactly the
    state-poisoning suspect). ``rows`` are script executions from
    ``script_hours`` (migration 7) — never from a scan of ``runs``.

    Wording note for callers: blocks are labelled by their actual
    times, never "last night" — a suite can run twice a day or skip
    days, and this project has been burned by window wording three
    times (see WindowWordingTest).

    ``stream=`` (an id, WP-23, default mainline): the branch/build hour
    tables are now maintained for every stream (migration 10), so this
    page reads a long-running branch's OWN running order the same way
    it reads mainline's — zero visible change when the param is absent
    (docs/STREAMS_PLAN.md §5.2).
    """
    environment = _query_single(request.query, "environment")
    product = _query_single(request.query, "product")
    if not environment and product is not None:
        product_environments = storage.environments_for_product(product)
        if len(product_environments) == 1:
            environment = product_environments[0]
    if not environment:
        raise _HttpError(400, "environment: required parameter is missing")
    if environment not in storage.known_environments():
        raise _HttpError(
            404,
            "unknown environment: no runs recorded for {}".format(
                environment
            ),
        )
    stream_id = _resolve_stream_id(storage, request)
    # F7 (docs/STREAMS_PLAN.md §5.2 "as built"): the Timeline page needs
    # a stream's kind/name to render the branch band, same reason test
    # detail fetches this — only paid when scoped away from mainline,
    # so the hot unscoped path costs no extra query.
    stream = (
        None if stream_id == MAINLINE_STREAM_ID
        else storage.get_stream(stream_id)
    )
    days = _parse_int_param(
        request, "days", _TIMELINE_DEFAULT_DAYS, 1, _TIMELINE_MAX_DAYS
    )
    now_value = now()
    floor = now_value - datetime.timedelta(days=days)

    # The same pass inference the environments page runs, over this
    # page's own lookback, scoped to *stream_id*. Coverage needs every
    # environment's denominator ON THAT STREAM, so the inputs are
    # stream-wide (not estate-wide across streams); only the blocks
    # shown are this environment's.
    effective = analytics.effective_test_counts(
        storage.test_counts_by_environment(stream_id),
        storage.declared_test_counts(),
    )
    passes = analytics.find_passes(
        storage.activity_buckets(floor, stream_id),
        effective,
        gap_hours=_PASS_GAP_HOURS,
        coverage=_PASS_COVERAGE,
    )
    shown = analytics.complete_passes(passes, floor, _PASS_GAP_HOURS)
    blocks = [
        entry for entry in shown if entry.environment == environment
    ]

    window_from = None  # type: Optional[datetime.datetime]
    window_to = None  # type: Optional[datetime.datetime]
    has_from = _query_single(request.query, "from") is not None
    has_to = _query_single(request.query, "to") is not None
    if has_from != has_to:
        raise _HttpError(
            400, "from/to: provide both edges of the window, or neither"
        )
    if has_from:
        window_from = _parse_iso_param(request, "from")
        window_to = _parse_iso_param(request, "to")
        if window_to < window_from:
            raise _HttpError(400, "from/to: the window ends before it starts")
    elif blocks:
        window_from = blocks[-1].started
        window_to = blocks[-1].ended

    rows = []  # type: List[analytics.ScriptExecution]
    known = {}  # type: Dict[str, int]
    if window_from is not None and window_to is not None:
        executions = analytics.group_script_executions(
            storage.script_activity(
                environment, window_from, window_to, stream_id,
            ),
            gap_minutes=_EXECUTION_GAP_MINUTES,
        )
        # The window is INCLUSIVE AT HOUR RESOLUTION, deliberately:
        # block edges from find_passes are bucket starts (a block
        # "ending" at 04:00 ran tests until 04:59), so trimming
        # against the exact edge would silently drop whatever ran in
        # the block's final hour. Widening both edges to their hour
        # matches what script_activity read; it cannot pull in a
        # neighbouring block, because blocks are by construction
        # separated by six quiet hours.
        from_floor = window_from.replace(
            minute=0, second=0, microsecond=0
        )
        to_ceiling = window_to.replace(
            minute=0, second=0, microsecond=0
        ) + datetime.timedelta(hours=1)
        rows = [
            execution for execution in executions
            if execution.started < to_ceiling
            and execution.ended >= from_floor
        ]
        known = storage.script_test_counts(environment, stream_id)

    return _json_response(
        200,
        {
            "environment": environment,
            "product": product,
            "stream": stream_id,
            "stream_identity": None if stream is None else _stream_json(
                stream),
            "days": days,
            "gap_minutes": _EXECUTION_GAP_MINUTES,
            # Newest block first: that is the one being looked at.
            "blocks": [_pass_json(entry) for entry in reversed(blocks)],
            "window": (
                None if window_from is None or window_to is None else {
                    "from": model.format_iso(window_from),
                    "to": model.format_iso(window_to),
                }
            ),
            # Running order — the page renders these top to bottom.
            "rows": [
                {
                    "script": execution.script,
                    "started": model.format_iso(execution.started),
                    "ended": model.format_iso(execution.ended),
                    "duration_seconds": round(
                        execution.duration_seconds, 3
                    ),
                    "total": execution.total,
                    "failed": execution.failed,
                    "results": _result_counts_json(execution.results),
                    # High-water mark from latest_runs ("known", not
                    # "expected"): a short total against it is what a
                    # partial run looks like.
                    "known_tests": known.get(execution.script, 0),
                }
                for execution in rows
            ],
        },
    )


def _handle_script_window_runs(
    storage: Storage,
    request: Request,
    environment: str,
    script: str,
) -> Response:
    """GET /api/scripts/{env}/{script}/runs — one window's runs, in order.

    The Timeline row expansion: every run of one script between
    ``from`` and ``to`` (required, inclusive), oldest first, no
    outputs. Bounded by the script's index range and capped at
    ``_TIMELINE_MAX_RUNS`` — the same cost profile as the executions
    endpoint, paid one script at a time on demand.

    ``stream=`` (F7, default mainline): the window edges themselves
    come from a `/api/timeline` block, which has been stream-scoped
    since migration 10 — this sub-endpoint had fallen behind it,
    always reading mainline's own ``runs`` regardless of which
    stream's block the caller expanded.
    """
    if not storage.script_exists(environment, script):
        raise _HttpError(
            404,
            "unknown script: no runs recorded for {} / {}".format(
                environment, script
            ),
        )
    stream_id = _resolve_stream_id(storage, request)
    window_from = _parse_iso_param(request, "from")
    window_to = _parse_iso_param(request, "to")
    if window_to < window_from:
        raise _HttpError(400, "from/to: the window ends before it starts")
    runs = storage.script_runs(
        environment, script, window_from, _TIMELINE_MAX_RUNS,
        until=window_to, stream_id=stream_id,
    )
    return _json_response(
        200,
        {
            "environment": environment,
            "script": script,
            "stream": stream_id,
            "from": model.format_iso(window_from),
            "to": model.format_iso(window_to),
            "truncated": len(runs) >= _TIMELINE_MAX_RUNS,
            "runs": [
                {
                    "test_name": run.test_name,
                    "run_id": run.run_id,
                    "result": run.result.value,
                    "start_time": model.format_iso(run.start_time),
                    "end_time": model.format_iso(run.end_time),
                    "duration_seconds": round(
                        model.duration_seconds(
                            run.start_time, run.end_time
                        ), 3
                    ),
                }
                for run in runs
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
    ``script`` to scope a drill-down, and ``product`` (WP-20) to scope
    to a declared product's environments — resolved server-side to
    ``environment IN (...)``, same as every other filter here.
    ``stream=`` (an id, WP-23, default mainline) scopes to one stream's
    own latest-run durations — zero visible change when absent
    (docs/STREAMS_PLAN.md §5.2).

    Aggregates the newest run of each test, so it answers "where did the
    last run of the suite spend its time" — not a historical window.
    Retired tests and tests that have stopped reporting are excluded;
    the count of the latter is returned so the page can say so rather
    than quietly presenting a smaller number as the whole.
    """
    group_by = _query_single(request.query, "group_by") or "environment"
    environment = _query_single(request.query, "environment")
    script = _query_single(request.query, "script")
    product = _query_single(request.query, "product")
    environments = _resolve_product_environments(storage, product)
    stream_id = _resolve_stream_id(storage, request)
    # F7 (docs/STREAMS_PLAN.md §5.2 "as built"): same reason
    # _handle_timeline fetches this — the Time page needs a stream's
    # kind/name to render the branch band; only paid when scoped away
    # from mainline.
    stream = (
        None if stream_id == MAINLINE_STREAM_ID
        else storage.get_stream(stream_id)
    )
    # Off by default: counting a test that last ran three weeks ago as
    # part of "where the time went" claims time that was not spent. But
    # an all-or-nothing cutoff empties the page after any quiet day, so
    # it can be turned off deliberately and the page says which it is.
    include_stale = _query_single(
        request.query, "include_stale") in ("1", "true")
    cutoff = (
        None if include_stale
        else _recent_cutoff(storage, now(), environments, stream_id)
    )
    try:
        rollup = storage.duration_rollup(
            group_by, cutoff, environment=environment, script=script,
            environments=environments, stream_id=stream_id,
        )
    except ValueError as exc:
        raise _HttpError(400, "group_by: {}".format(exc))
    return _json_response(
        200,
        {
            "group_by": group_by,
            "environment": environment,
            "script": script,
            "product": product,
            "stream": stream_id,
            "stream_identity": None if stream is None else _stream_json(
                stream),
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
    # WP-20: which product each environment belongs to, "" (the implicit
    # product) when nobody has declared one. Cheap (a handful of rows,
    # same shape as declared_rows above) and read on every load of this
    # page regardless of whether products are in use.
    products = storage.environment_products_map()

    items = []  # type: List[Dict[str, Any]]
    for environment in storage.known_environments():
        found = by_env.get(environment, [])
        row = declared_rows.get(environment)
        items.append({
            "environment": environment,
            "product": products.get(environment, ""),
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


#: Generous but bounded — a product is a display name, not free text; see
#: docs/STREAMS_PLAN.md §2.1.
_MAX_PRODUCT_LEN = 200


def _parse_product(obj: Dict[str, Any]) -> str:
    """Validate the ``product`` field of a PUT .../product body.

    Required, must be a string. Trimmed of leading/trailing whitespace;
    an empty result is not an error — it CLEARS the mapping, because
    ``""`` is the implicit product's own name (docs/STREAMS_PLAN.md
    §2.1), not a third state alongside a declared name and absence.
    """
    if "product" not in obj:
        raise _HttpError(400, "product: required field is missing")
    value = obj["product"]
    if not isinstance(value, str):
        raise _HttpError(
            400,
            "product: must be a string, got {}".format(
                type(value).__name__
            ),
        )
    stripped = value.strip()
    if len(stripped) > _MAX_PRODUCT_LEN:
        raise _HttpError(
            400,
            "product: must be at most {} characters (got {})".format(
                _MAX_PRODUCT_LEN, len(stripped)
            ),
        )
    return stripped


def _handle_environment_product(
    storage: Storage,
    request: Request,
    environment: str,
    now: Callable[[], datetime.datetime],
) -> Response:
    """PUT /api/environments/{environment}/product — declare or clear.

    Body: ``{"product": str, "username": str}``. Mirrors
    :func:`_handle_environment_expectation`'s shape (one endpoint,
    declare or clear), but the clear signal is an EMPTY STRING rather
    than ``null``: ``""`` already means "the implicit product" everywhere
    else in this feature (docs/STREAMS_PLAN.md §2.1), so a third
    "no field" case would just be a second spelling of the same thing.

    An environment that has never reported a run and carries no other
    declaration is a 404 — same reasoning as the expectation endpoint:
    declaring one would affect nothing visible and a typo would leave a
    row nobody can see the purpose of.
    """
    obj = _parse_json_object(request.body)
    product = _parse_product(obj)
    username = _validate_username(obj, "username")

    if not storage.environment_exists(environment):
        raise _HttpError(
            404, "unknown environment: {}".format(environment)
        )

    if product == "":
        cleared = storage.clear_environment_product(environment)
        return _json_response(
            200,
            {"environment": environment, "product": "", "cleared": cleared},
        )
    record = storage.set_environment_product(
        environment, product, username, now()
    )
    return _json_response(
        200,
        {
            "environment": record.environment,
            "product": record.product,
            "updated_at": model.format_iso(record.updated_at),
            "updated_by": record.updated_by,
            "cleared": False,
        },
    )


def _stream_json(stream: Stream) -> Dict[str, Any]:
    """Serialize a stream to its JSON shape (WP-21, docs/STREAMS_PLAN.md
    §3.5). Timestamps only — no hidden staleness constant; the caller
    (the Build picker) folds by age from ``last_seen`` itself."""
    return {
        "id": stream.stream_id,
        "product": stream.product,
        "kind": stream.kind,
        "name": stream.name,
        "first_seen": model.format_iso(stream.first_seen),
        "last_seen": model.format_iso(stream.last_seen),
        "failing": stream.failing,
    }


def _handle_streams_list(storage: Storage, request: Request) -> Response:
    """GET /api/streams?product=… — the Build picker's data.

    ``product`` defaults to ``""`` (the implicit "no product declared"
    grouping, same reading as everywhere else in this feature). Never a
    404 for an unknown product — a product exists by having
    environments/streams, and an unknown one simply has none
    (docs/STREAMS_PLAN.md §2.6's rule, extended to streams). Mainline
    itself is never listed — it is the picker's default, not one of its
    entries.
    """
    product = _query_single(request.query, "product") or ""
    streams = storage.list_streams(product)
    return _json_response(
        200,
        {
            "product": product,
            "streams": [_stream_json(stream) for stream in streams],
        },
    )


def _handle_compare(storage: Storage, request: Request) -> Response:
    """GET /api/compare?stream=&baseline=&category=&limit=&offset=

    docs/STREAMS_PLAN.md §3.5/§4.1. ``stream`` is required. ``baseline``
    defaults to mainline; since WP-22 it may also be any stream of the
    SAME product as ``stream`` — a build judged against the build
    before it, or one branch against another, not only mainline. A
    baseline naming a DIFFERENT product is a clear 400 naming both
    products, never a query that silently compares against the wrong
    environments (mainline is exempt from the product check on either
    side — it is shared by every product by construction).
    ``category``, when given, selects ONE of the five counts to return
    as a paginated list (``limit``/``offset``); when absent the response
    carries the counts alone and an empty list.

    The response carries both sides' identity and freshness
    (``last_seen``) so the UI can build its own honesty line ("baseline
    N days old") from data, never from a constant.
    """
    raw_stream = _query_single(request.query, "stream")
    if raw_stream is None:
        raise _HttpError(400, "stream: required query parameter is missing")
    try:
        stream_id = int(raw_stream)
    except ValueError:
        raise _HttpError(
            400, "stream: must be an integer, got '{}'".format(raw_stream)
        )
    stream = storage.get_stream(stream_id)
    if stream is None:
        raise _HttpError(404, "unknown stream: {}".format(stream_id))

    raw_baseline = _query_single(request.query, "baseline")
    baseline_id = MAINLINE_STREAM_ID
    if raw_baseline is not None:
        try:
            baseline_id = int(raw_baseline)
        except ValueError:
            raise _HttpError(
                400, "baseline: must be an integer, got '{}'".format(
                    raw_baseline)
            )
    baseline = storage.get_stream(baseline_id)
    if baseline is None:
        raise _HttpError(404, "unknown baseline stream: {}".format(
            baseline_id))
    # WP-22 (docs/STREAMS_PLAN.md §4.1): baseline= now accepts any stream
    # of the SAME product as *stream* — a build judged against the build
    # before it, or one branch against another. Mainline is the one
    # universal exception, but ONLY on the baseline side of this check:
    # the environments filter both sides of the SQL join share
    # (_compare_partition_sql) is resolved from *stream*'s own product
    # ALWAYS, regardless of which side of the URL happens to be
    # mainline — so `stream=<mainline>&baseline=<a real product's
    # branch>` is refused exactly like the reverse would be, rather
    # than silently scoping to mainline's own product ('', matching no
    # real environment) and returning a confusing "everything is
    # new_tests" instead of a clear refusal. (No shipped frontend ever
    # constructs `stream=` as mainline explicitly — getSelectedStreamId()
    # is null for it — but the API is a documented contract regardless.)
    if baseline.kind != "mainline" and baseline.product != stream.product:
        raise _HttpError(
            400,
            "cannot compare across products: stream '{}:{}' is in "
            "product '{}', baseline '{}:{}' is in product '{}'".format(
                stream.kind, stream.name, stream.product or "(none)",
                baseline.kind, baseline.name, baseline.product or "(none)",
            ),
        )

    category = _query_single(request.query, "category")
    if category is not None and category not in COMPARE_CATEGORIES:
        raise _HttpError(
            400,
            "category: unknown value '{}' (expected one of {})".format(
                category, ", ".join(COMPARE_CATEGORIES)
            ),
        )

    limit = _parse_int_param(
        request, "limit", _DEFAULT_PAGE_LIMIT, 1, _MAX_PAGE_LIMIT
    )
    offset = _parse_int_param(request, "offset", 0, 0, _MAX_OFFSET)

    counts = storage.compare_counts(stream_id, baseline_id=baseline_id)
    tests = []  # type: List[Dict[str, Any]]
    total = 0
    if category is not None:
        rows = storage.compare_category(
            stream_id, category, baseline_id=baseline_id, limit=limit,
            offset=offset,
        )
        # WP-23 perf pass: every /api/compare?category= request used to
        # run the expensive pairs SQL (_compare_pairs_sql) THREE times —
        # once each for compare_counts above, compare_category's page
        # just fetched, and compare_category_count here recomputing the
        # very count compare_counts already returned. `counts` above and
        # `storage.compare_category_count` both derive from the SAME
        # pairs SQL with the SAME stream_id/baseline_id/environments (see
        # both methods' docstrings) and `category` is validated against
        # COMPARE_CATEGORIES a few lines up, which is EXACTLY
        # CompareCounts's five per-category field names — so
        # getattr(counts, category) IS that count, not an approximation
        # of it. compare_category_count itself is unchanged and kept for
        # any caller that wants a total without also paying for a page
        # (tests/test_storage.py's own tests are the oracle that it still
        # agrees with compare_counts).
        total = getattr(counts, category)
        tests = [
            {
                "environment": row.environment,
                "script": row.script,
                "test_name": row.test_name,
                "stream_result": (
                    None if row.stream_result is None
                    else row.stream_result.value
                ),
                "baseline_result": (
                    None if row.baseline_result is None
                    else row.baseline_result.value
                ),
                # WP-21: what the frontend's Review expander and
                # assignee select need — the branch's own run (null
                # exactly when there is nothing on the stream side to
                # review) and the triple's CURRENT assignee (never
                # partitioned by stream — see CompareRow).
                "stream_run_id": row.stream_run_id,
                "stream_start_time": (
                    None if row.stream_start_time is None
                    else model.format_iso(row.stream_start_time)
                ),
                "assignee": row.assignee,
            }
            for row in rows
        ]

    return _json_response(
        200,
        {
            "stream": _stream_json(stream),
            "baseline": _stream_json(baseline),
            "counts": {
                "new_failures": counts.new_failures,
                "new_passes": counts.new_passes,
                "both_failing": counts.both_failing,
                "new_tests": counts.new_tests,
                "no_result": counts.no_result,
                "agree": counts.agree,
            },
            "category": category,
            "tests": tests,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


#: Cards per /api/watch request. A URL this long is a mistake, not a use
#: case (docs/STREAMS_PLAN.md §2.4) — stated in the refusal and the README.
_WATCH_MAX_CARDS = 50


#: The declared-staleness suffix (docs/STREAMS_PLAN.md §2.4): a plain
#: integer count of hours or days, e.g. "1d", "36h". Anything else after
#: the last "@" is part of the name, not a suffix — see
#: :func:`_parse_watch_spec`.
_EXPECTED_SUFFIX = re.compile(r"^\d+[hd]$")


def _parse_watch_spec(spec: str) -> Tuple[str, str, Optional[str]]:
    """Split one ``c=`` value into ``(kind, name, expected)``.

    docs/STREAMS_PLAN.md §2.4: "a one-letter kind, a colon, then the
    name". *spec* has already been through the query-string decoder
    (``urllib.parse.parse_qs``) by the time it reaches here, so no
    further unquoting happens — doing it twice would corrupt a name
    that itself contains a ``%`` sequence. A spec with no colon at all
    has no valid kind and is handled the same as any other unrecognised
    kind — an ``ok: false`` card, not a parse error, because a
    stale or hand-edited URL should degrade to "this one card is
    wrong", never to a broken page.

    The name may carry an OPTIONAL declared-staleness suffix,
    ``@<n>h`` or ``@<n>d``, split at the LAST ``@`` in the name — but
    only when the text after it matches :data:`_EXPECTED_SUFFIX`.
    Names are free text and may themselves contain ``@`` (a branch or
    build name), so an invalid or absent tail is part of the name, not
    an error: ``"e:release@2026"`` has the plain name
    ``"release@2026"`` and no declared expectation; ``"s:2@1d"`` has
    name ``"2"`` and expectation ``"1d"``.
    """
    if ":" not in spec:
        return spec, "", None
    kind, name = spec.split(":", 1)
    at = name.rfind("@")
    if at == -1:
        return kind, name, None
    tail = name[at + 1:]
    if not _EXPECTED_SUFFIX.match(tail):
        return kind, name, None
    return kind, name[:at], tail


def _parse_expected_age(expected: str) -> datetime.timedelta:
    """``"1d"`` -> one day, ``"36h"`` -> 36 hours.

    *expected* is assumed already validated by :data:`_EXPECTED_SUFFIX`
    (every caller gets it from :func:`_parse_watch_spec`, which never
    hands back a tail that does not match).
    """
    unit = expected[-1]
    count = int(expected[:-1])
    if unit == "d":
        return datetime.timedelta(days=count)
    return datetime.timedelta(hours=count)


def _apply_staleness(
    card: Dict[str, Any],
    expected: Optional[str],
    last_reported: Optional[datetime.datetime],
    now_value: datetime.datetime,
) -> None:
    """Add the DECLARED staleness verdict to *card*, in place.

    docs/STREAMS_PLAN.md §2.4: staleness is judged only when the URL
    itself declares an expectation — no *expected* means no judgment
    at all, and both keys are omitted (today's behaviour, byte for
    byte: the "zero visible change" rule applies to a card with no
    ``@`` suffix same as it does to one with zero unassigned
    failures). A card whose freshness timestamp is ``None`` (never
    reported) is stale by definition — absence of data cannot read as
    fresher than old data.
    """
    if expected is None:
        return
    card["expected"] = expected
    if last_reported is None:
        card["stale"] = True
        return
    card["stale"] = last_reported < now_value - _parse_expected_age(expected)


def _watch_card_error(
    spec: str, kind: str, name: str, message: str
) -> Dict[str, Any]:
    """One ``ok: false`` card — the page still answers 200 around it."""
    return {
        "spec": spec, "kind": kind, "name": name, "ok": False,
        "error": message,
    }


def _product_laggard(
    environments: List[str],
    latest_by_env: Dict[str, datetime.datetime],
) -> Optional[Dict[str, Any]]:
    """The product's furthest-behind environment, for its watch card.

    See the product-card comment in :func:`_handle_watch` for why this
    exists instead of a single ``last_reported`` timestamp. ``None``
    only when the product has no environments at all (a mapping row
    can outlive its environment's data). An environment with no
    recorded run at all is the worst laggard and wins outright, with
    ``last_reported`` null — absence of data must outrank old data,
    never hide behind it.
    """
    if not environments:
        return None
    silent = [env for env in environments if env not in latest_by_env]
    if silent:
        return {"environment": sorted(silent)[0], "last_reported": None}
    oldest = min(environments, key=lambda env: latest_by_env[env])
    return {
        "environment": oldest,
        "last_reported": model.format_iso(latest_by_env[oldest]),
    }


def _handle_watch(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
) -> Response:
    """GET /api/watch?c=…&c=… — the whole Watchlist page in one request.

    docs/STREAMS_PLAN.md §2.4. ``c`` is repeated, ORDER PRESERVED — the
    URL is the entire configuration, there is no server-side saved view,
    so the response's card order is exactly the request's. Each value is
    ``kind:name``, with an optional ``@<n>h``/``@<n>d`` declared-
    staleness suffix on the name (see :func:`_parse_watch_spec`).

    Every ok card carries ``unassigned_failing`` (WP-23): the count of
    tests in the card's own scope whose latest result is FAIL and which
    have no current assignee — the highlight the morning scan needs,
    computed from two aggregate queries total (see the
    ``unassigned_by_env``/``unassigned_by_stream`` fetches below), never
    per card. A card whose spec carried an ``@`` suffix also carries
    ``expected`` (the suffix, echoed) and ``stale`` (bool) — the card's
    own freshness timestamp (environment: ``last_reported``; product:
    its laggard's; stream: ``last_seen``) compared against the declared
    age. No suffix means neither key is present at all — declaring
    nothing is not the same as declaring "never stale".

    ``p`` (product) and ``e`` (environment) resolve to verdict cards.
    ``s`` (stream, WP-21) resolves to a branch/build VERDICT card — the
    compare-vs-mainline headline, both sides' freshness — built from the
    same :meth:`Storage.compare_counts_many` reads
    :func:`_handle_compare` uses for one stream, batched here across
    every ``s:`` card in the request so the O(cards)-in-Python property
    below still holds (docs/STREAMS_PLAN.md §3.6).

    An unrecognised kind, or a ``p``/``e``/``s`` name that resolves to
    nothing, is an ``ok: false`` CARD — the page still answers 200,
    because a shared URL outlives renames and deletions and a missing
    scope must say so plainly rather than silently vanish
    (docs/STREAMS_PLAN.md's "silently missing data is worse than an
    unexpected row" rule, restated for this page as "a missing scope is
    an explicit error card, not a gap"). An ``s:`` value that is not an
    integer is the same case — a stream id is opaque, not a name, so a
    non-integer cannot be a stream that once existed.

    Fetches every derived table EXACTLY ONCE regardless of how many
    cards are requested — ``activity_buckets``, the pass list,
    ``summary_rollup``, ``environment_products_map``,
    :meth:`Storage.stream_identities` and
    :meth:`Storage.compare_counts_many` are each one query; every card
    after that is a pure-Python slice of those fetches (see
    :func:`analytics.summarize_by_product`, reused here with an identity
    mapping to group by ENVIRONMENT the same way it groups by product).
    This is what keeps the endpoint O(cards in Python) rather than
    O(cards) round trips — ``tests/test_api.py`` pins the query count as
    flat from 1 card to the 50-card cap, for every mix of kinds.
    """
    specs = request.query.get("c") or []
    if len(specs) > _WATCH_MAX_CARDS:
        raise _HttpError(
            413,
            "too many cards: {} requested, {} is the limit for one "
            "watch request".format(len(specs), _WATCH_MAX_CARDS),
        )
    parsed = [
        (spec,) + _parse_watch_spec(spec) for spec in specs
    ]  # type: List[Tuple[str, str, str, Optional[str]]]

    # WP-21: every s: card's stream id, valid-integer ones only (a
    # non-integer becomes an error card in the main loop below, same as
    # any other unresolved name) - resolved and compared in bulk, BEFORE
    # the per-card loop, so this stays O(1) queries regardless of how
    # many s: cards the request names.
    requested_stream_ids = set()  # type: Set[int]
    for _spec, kind, name, _expected in parsed:
        if kind != "s":
            continue
        try:
            stream_id_candidate = int(name)
        except ValueError:
            continue
        if stream_id_candidate != MAINLINE_STREAM_ID:
            requested_stream_ids.add(stream_id_candidate)

    now_value = now()
    fallback = now_value - datetime.timedelta(hours=_SUMMARY_RECENT_HOURS)
    floor = now_value - datetime.timedelta(days=_PASS_LOOKBACK_DAYS)
    inferred = storage.test_counts_by_environment()
    declared = storage.declared_test_counts()
    effective = analytics.effective_test_counts(inferred, declared)
    all_passes = analytics.find_passes(
        storage.activity_buckets(floor), effective,
        gap_hours=_PASS_GAP_HOURS, coverage=_PASS_COVERAGE,
    )
    known_environments = set(storage.known_environments())
    env_to_product = storage.environment_products_map()
    product_to_envs = {}  # type: Dict[str, List[str]]
    for environment, product in env_to_product.items():
        product_to_envs.setdefault(product, []).append(environment)
    declared_products = set(product_to_envs)
    latest_by_env = storage.latest_run_time_by_environment()
    # Unassigned-failure highlight (WP-23, docs/STREAMS_PLAN.md §2.4):
    # two aggregate queries total, regardless of card count. "e"/"p"
    # cards are always mainline (there is no such thing as a branch
    # environment or a branch product), so one per-environment
    # aggregate on mainline covers both; "s" cards get their own
    # per-stream aggregate, batched across every requested id.
    unassigned_by_env = storage.unassigned_failing_by_environment()
    unassigned_by_stream = storage.unassigned_failing_by_stream(
        list(requested_stream_ids)
    )

    # WP-21: identities + compare counts for every requested s: card, in
    # three queries total (mainline's own clock included) regardless of
    # how many s: cards were named — and none at all when there are none.
    #
    # A stream's product can be "" (the implicit grouping: environments
    # nobody has mapped to a product — the common case on a deployment
    # that has never declared any, per WP-20). `product_to_envs` above
    # is built from `environment_products_map()` alone, so it NEVER
    # contains an "" entry — that is correct for "p:" cards (there is no
    # such thing as a product card for "no product"), but wrong here: a
    # stream literally does carry product "". Resolving it the same way
    # `Storage.environments_for_product("")` does (every known
    # environment nobody has mapped) rather than through
    # `product_to_envs.get("", [])`, which is always empty, is what fixed
    # a real bug caught only by driving this endpoint against a database
    # with no declared products at all — every s: card silently came
    # back all-zero, "wrong and looks right" in exactly the way this
    # project's own house rules warn about.
    mapped_environments = set(env_to_product)
    implicit_environments = [
        e for e in known_environments if e not in mapped_environments
    ]
    stream_identities = storage.stream_identities(requested_stream_ids)
    # WP-22 (docs/STREAMS_PLAN.md §4.1): a BUILD-kind card's default
    # baseline is the build before it, not mainline, when one exists —
    # "failing in <name>" plus its vs-previous-build delta, the same
    # default the build-scoped dashboard's "Compare to" control opens
    # on. ONE query (bounded to the distinct products among the
    # requested build cards), independent of how many s: cards the URL
    # carries — see :meth:`Storage.previous_builds`.
    predecessor_builds = storage.previous_builds(
        list(stream_identities.values())
    )
    stream_baselines = {
        stream_id: predecessor.stream_id
        for stream_id, predecessor in predecessor_builds.items()
    }
    stream_counts = storage.compare_counts_many({
        stream_id: (
            implicit_environments if stream.product == ""
            else product_to_envs.get(stream.product, [])
        )
        for stream_id, stream in stream_identities.items()
    }, baselines=stream_baselines)
    mainline_last_seen = (
        storage.latest_run_time() if requested_stream_ids else None
    )
    # ONE estate-wide rollup. The recent_cutoff argument only feeds a
    # column the verdict below never reads — none of failing/
    # new_failures/fixed/unexpected_passes is recency-gated (see
    # analytics.summarize_by_product) — so any value works and no
    # per-card (or per-scope) query is needed for it.
    rollup_counts = storage.summary_rollup(now_value)
    by_environment = {
        row.product: row
        for row in analytics.summarize_by_product(
            rollup_counts, {e: e for e in known_environments}
        )
    }
    by_product = {
        row.product: row
        for row in analytics.summarize_by_product(
            rollup_counts, env_to_product
        )
    }
    zero = analytics.ProductRollup(
        product="", failing=0, new_failures=0, fixed=0, unexpected_passes=0
    )

    def card_cutoff(card_environments: Sequence[str]) -> datetime.datetime:
        """This card's OWN staleness line — never the whole estate's."""
        scoped = [
            entry for entry in all_passes
            if entry.environment in card_environments
        ]
        return analytics.recent_cutoff(scoped, fallback, floor).when

    cards = []  # type: List[Dict[str, Any]]
    for spec, kind, name, expected in parsed:
        if kind == "e":
            if name not in known_environments:
                cards.append(_watch_card_error(
                    spec, "environment", name,
                    "nothing under this name — removed or renamed?"))
                continue
            verdict = by_environment.get(name, zero)
            env_last_reported = latest_by_env.get(name)
            card = {
                "spec": spec, "kind": "environment", "name": name,
                "ok": True,
                # WP-23 bugfix: the card's link must be scope-
                # self-sufficient (docs/STREAMS_PLAN.md §0.9) — an
                # unmapped environment's product is "" by construction,
                # same as env_to_product.get(...) everywhere else in
                # this handler, and the frontend sends it through
                # exactly as-is, even when empty.
                "product": env_to_product.get(name, ""),
                "failing": verdict.failing,
                "new_failures": verdict.new_failures,
                "fixed": verdict.fixed,
                "unexpected_passes": verdict.unexpected_passes,
                "stale_before": model.format_iso(card_cutoff([name])),
                "last_reported": (
                    None if env_last_reported is None
                    else model.format_iso(env_last_reported)
                ),
                "unassigned_failing": unassigned_by_env.get(name, 0),
            }  # type: Dict[str, Any]
            _apply_staleness(card, expected, env_last_reported, now_value)
            cards.append(card)
        elif kind == "p":
            if name not in declared_products:
                cards.append(_watch_card_error(
                    spec, "product", name,
                    "nothing under this name — removed or renamed?"))
                continue
            verdict = by_product.get(name, zero)
            product_envs = product_to_envs.get(name, [])
            laggard = _product_laggard(product_envs, latest_by_env)
            # The card's OWN freshness timestamp is the laggard's — the
            # oldest-across-environments environment, deliberately: "1d"
            # declared on a product means every one of its environments
            # reported within a day, and the laggard is exactly the
            # timestamp that answers that.
            laggard_last_reported = (
                None if laggard is None
                else latest_by_env.get(laggard["environment"])
            )
            card = {
                "spec": spec, "kind": "product", "name": name,
                "ok": True,
                "failing": verdict.failing,
                "new_failures": verdict.new_failures,
                "fixed": verdict.fixed,
                "unexpected_passes": verdict.unexpected_passes,
                "stale_before": model.format_iso(
                    card_cutoff(product_envs)),
                # No single "last reported" for a multi-environment
                # product — env_updated already answers that per
                # environment, and this page must never invent one
                # truthful-looking timestamp out of several.
                "last_reported": None,
                # What a manager scanning the morning card actually
                # needs instead: the LAGGARD — which of this product's
                # environments is furthest behind, and since when. The
                # oldest-across-environments principle recent_cutoff
                # already uses, applied to reporting: it NAMES the
                # environment, so unlike a single product timestamp it
                # cannot mask a stale one (a newest-report figure is
                # exactly the "says nothing about the one you are
                # waiting on" trap the handover documents). An
                # environment that has never reported is the worst
                # laggard of all and wins with last_reported null.
                "laggard": laggard,
                "unassigned_failing": sum(
                    unassigned_by_env.get(e, 0) for e in product_envs
                ),
            }  # type: Dict[str, Any]
            _apply_staleness(
                card, expected, laggard_last_reported, now_value)
            cards.append(card)
        elif kind == "s":
            try:
                stream_id = int(name)
            except ValueError:
                cards.append(_watch_card_error(
                    spec, "stream", name,
                    "not a stream id (expected an integer)"))
                continue
            if stream_id == MAINLINE_STREAM_ID:
                # Mainline is never a Build picker entry (list_streams
                # excludes it for the same reason); a card comparing it
                # to itself would be a trivially-all-zero verdict, not a
                # useful one, so this reads as "no such stream" too.
                cards.append(_watch_card_error(
                    spec, "stream", name,
                    "nothing under this id — removed or never existed?"))
                continue
            stream = stream_identities.get(stream_id)
            if stream is None:
                cards.append(_watch_card_error(
                    spec, "stream", name,
                    "nothing under this id — removed or never existed?"))
                continue
            counts = stream_counts.get(stream_id, CompareCounts(
                new_failures=0, new_passes=0, both_failing=0,
                new_tests=0, no_result=0, agree=0,
            ))
            # WP-22: the baseline actually used (a predecessor build) is
            # named explicitly rather than assumed to be mainline — the
            # card's wording (watch.js) must say what it is really being
            # compared against, not a hardcoded "mainline".
            predecessor = predecessor_builds.get(stream_id)
            baseline_kind = "mainline" if predecessor is None else "build"
            baseline_name = "" if predecessor is None else predecessor.name
            baseline_last_seen = (
                mainline_last_seen if predecessor is None
                else predecessor.last_seen
            )
            card = {
                "spec": spec, "kind": "stream", "name": stream.name,
                "ok": True,
                "id": stream.stream_id,
                "stream_kind": stream.kind,
                "product": stream.product,
                "new_failures": counts.new_failures,
                "new_passes": counts.new_passes,
                "both_failing": counts.both_failing,
                "new_tests": counts.new_tests,
                "no_result": counts.no_result,
                "agree": counts.agree,
                "last_seen": model.format_iso(stream.last_seen),
                "baseline_kind": baseline_kind,
                "baseline_name": baseline_name,
                "baseline_last_seen": (
                    None if baseline_last_seen is None
                    else model.format_iso(baseline_last_seen)
                ),
                "unassigned_failing": unassigned_by_stream.get(
                    stream_id, 0),
            }  # type: Dict[str, Any]
            _apply_staleness(card, expected, stream.last_seen, now_value)
            cards.append(card)
        else:
            cards.append(_watch_card_error(
                spec, kind, name,
                "unknown card kind {!r} (expected 'p', 'e' or "
                "'s')".format(kind),
            ))

    return _json_response(
        200, {"cards": cards, "cap": _WATCH_MAX_CARDS}
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


def _handle_site_notes(path: Optional[str]) -> Response:
    """GET /api/site-notes — this site's own notes for the What's new page.

    Not testboard's release notes: those ship inside the build, in
    ``static/whatsnew.html``. These are the local ones a site adds beside
    them — "the reader that was filing runs under UNKNOWN is fixed" —
    keyed by the date of the drop they belong to.

    Never an error. A file that is absent, empty or unreadable yields an
    empty list, because these annotate a page whose real content is
    already on screen, and failing the request would take the release
    notes down with the side-car. A malformed file does report ``problem``
    so somebody can see WHY it is empty rather than assuming nobody has
    written any; the page shows that to nobody but does not swallow it
    either — it is in the payload for whoever is debugging.
    """
    notes, problem = site_notes.load(path)
    if problem is not None:
        _LOGGER.warning("site notes: %s", problem)
    return _json_response(
        200,
        {
            "notes": [
                {
                    # The id is what `tools/add_site_note.py --edit/--remove`
                    # addresses a note by; carried here so a correction can
                    # be traced to what is on screen.
                    "id": note.note_id,
                    "date": note.date,
                    "text": note.text,
                    "author": note.author,
                    "added_at": note.added_at or None,
                }
                for note in notes
            ],
            "configured": bool(path),
            "problem": problem,
        },
    )


def _route(
    storage: Storage,
    request: Request,
    now: Callable[[], datetime.datetime],
    site_notes_path: Optional[str] = None,
) -> Response:
    """Match the decoded path segments to a handler and dispatch.

    Raises :class:`_HttpError` for 404 (unknown route) and 405 (known
    route, wrong method — with an Allow header).

    *site_notes_path* is optional and defaults to None, which makes
    ``/api/site-notes`` answer with an empty list rather than 404. That
    keeps every existing caller — and every existing test — working
    unchanged, and means the frontend has one shape to handle instead of
    two: a deployment that has not configured notes is the same case as
    one that has none yet.
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

    if rest == ["timeline"]:
        _check_method(request.method, ("GET",))
        return _handle_timeline(storage, request, now)

    if rest == ["environments"]:
        _check_method(request.method, ("GET",))
        return _handle_environments_list(storage, request, now)

    if rest == ["site-notes"]:
        _check_method(request.method, ("GET",))
        return _handle_site_notes(site_notes_path)

    if (len(rest) == 3 and rest[0] == "environments"
            and rest[2] == "expectation"):
        _check_method(request.method, ("PUT",))
        return _handle_environment_expectation(
            storage, request, rest[1], now
        )

    if (len(rest) == 3 and rest[0] == "environments"
            and rest[2] == "product"):
        _check_method(request.method, ("PUT",))
        return _handle_environment_product(
            storage, request, rest[1], now
        )

    if rest == ["watch"]:
        _check_method(request.method, ("GET",))
        return _handle_watch(storage, request, now)

    if rest == ["streams"]:
        _check_method(request.method, ("GET",))
        return _handle_streams_list(storage, request)

    if rest == ["compare"]:
        _check_method(request.method, ("GET",))
        return _handle_compare(storage, request)

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

    if len(rest) == 4 and rest[0] == "scripts" and rest[3] == "runs":
        _check_method(request.method, ("GET",))
        return _handle_script_window_runs(
            storage, request, rest[1], rest[2]
        )

    if len(rest) == 4 and rest[0] == "tests":
        _check_method(request.method, ("GET",))
        return _handle_test_detail(
            storage, request, rest[1], rest[2], rest[3], now)

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
        if action == "streams":
            _check_method(request.method, ("GET",))
            return _handle_test_streams(
                storage, environment, script, test_name
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
    site_notes_path: Optional[str] = None,
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
        return _route(storage, request, now, site_notes_path)
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
