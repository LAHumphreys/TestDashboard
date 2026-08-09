"""Core domain model for testboard.

This module owns:

- the :class:`Result` enum (the four possible outcomes of a test run),
- :class:`ValidationError` (raised when transport data fails validation),
- the single ISO-8601 timestamp parse/format pair used everywhere in the
  project (naive UTC datetimes in Python, ``YYYY-MM-DDTHH:MM:SS.ffffff``
  strings in SQLite and JSON transport — no timezone suffix, so lexical
  string comparison equals time comparison),
- the :class:`RunRecord` / :class:`StoredRun` NamedTuples, and
- :func:`parse_run_record` / :func:`run_record_to_dict`, the strict
  validators for the ``/api/import`` transport schema shared with the feeder.

Python 3.6 compatible; standard library only.
"""

import datetime
import enum
import re
from typing import Any, Dict, NamedTuple, Optional

__all__ = [
    "Result",
    "ValidationError",
    "TIME_FORMAT",
    "utcnow",
    "parse_iso",
    "format_iso",
    "duration_seconds",
    "RunRecord",
    "StoredRun",
    "parse_run_record",
    "run_record_to_dict",
]


class Result(enum.Enum):
    """Outcome of a single test run.

    ``FAILED_AS_EXPECTED`` and ``UNEXPECTED_PASS`` exist because tests can
    carry a known-failure annotation: the interesting states for triage are
    ``FAIL`` (new breakage) and ``UNEXPECTED_PASS`` (a known failure that now
    passes — the annotation is probably stale).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    FAILED_AS_EXPECTED = "FAILED_AS_EXPECTED"
    UNEXPECTED_PASS = "UNEXPECTED_PASS"


class ValidationError(Exception):
    """Raised when transport data fails validation. Message is user-facing."""


TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

# Strict shape check for transport timestamps: date, 'T', time, optional
# fraction of 1-6 digits. Anything else (timezone suffixes, spaces, garbage)
# is rejected before strptime ever sees it.
_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.(\d{1,6}))?$"
)


def utcnow() -> datetime.datetime:
    """Return the current UTC time as a naive :class:`datetime.datetime`.

    All timestamps in testboard are naive UTC; this is the one sanctioned
    clock read (``datetime.utcnow()`` is deprecated on modern Pythons).
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def parse_iso(value: str) -> datetime.datetime:
    """Parse a strict ISO-8601 UTC timestamp into a naive datetime.

    Accepts ``YYYY-MM-DDTHH:MM:SS`` with an optional fractional part of one
    to six digits (e.g. ``2026-07-25T02:14:07.123456``). Timezone suffixes
    (``Z``, ``+00:00``, ...) are rejected: transport timestamps are always
    naive UTC.

    Raises:
        ValueError: if ``value`` is not a str or does not match the format.
    """
    if not isinstance(value, str):
        raise ValueError(
            "expected an ISO-8601 timestamp string, got {}".format(
                type(value).__name__
            )
        )
    match = _ISO_RE.match(value)
    if match is None:
        raise ValueError(
            "invalid timestamp {!r}: expected 'YYYY-MM-DDTHH:MM:SS[.ffffff]' "
            "(naive UTC, no timezone suffix)".format(value)
        )
    base = match.group(1)
    fraction = match.group(3)
    micro = int((fraction or "0").ljust(6, "0"))
    # The regex has already pinned every digit position, so the fields
    # can be sliced directly; the datetime constructor still rejects
    # impossible calendar values (month 13, Feb 30, hour 25, ...).
    # This is ~10x faster than strptime, which matters: list endpoints
    # parse two timestamps per run row, tens of thousands per request
    # at production scale.
    try:
        return datetime.datetime(
            int(base[0:4]), int(base[5:7]), int(base[8:10]),
            int(base[11:13]), int(base[14:16]), int(base[17:19]), micro,
        )
    except ValueError:
        raise ValueError(
            "invalid timestamp {!r}: not a real calendar date/time".format(
                value
            )
        )


def format_iso(dt: datetime.datetime) -> str:
    """Format a naive UTC datetime as ``YYYY-MM-DDTHH:MM:SS.ffffff``.

    Always emits six fractional digits so lexical string comparison matches
    chronological comparison. (Direct formatting rather than strftime for
    the same list-endpoint hot-path reason as :func:`parse_iso`.)
    """
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}.{:06d}".format(
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
        dt.microsecond,
    )


def duration_seconds(start: datetime.datetime, end: datetime.datetime) -> float:
    """Return the duration ``end - start`` in seconds as a float."""
    return (end - start).total_seconds()


class RunRecord(NamedTuple):
    """A validated incoming test run (transport shape; no DB id yet).

    ``build`` is a WP-21 addition (docs/STREAMS_PLAN.md §3.3), narrowed
    to the only surviving non-mainline kind by WP-25 (docs/ONE_KIND_PLAN.md
    — the ``branch`` kind died before it ever shipped anywhere, so this
    is a deletion, not a migration): optional, non-empty-after-strip.
    ``None`` (every record from every feeder ever deployed) means
    mainline — the identity of the stream this run belongs to is
    resolved from this field plus the record's ``environment``, not
    carried as a separate id on the wire. A raw transport dict carrying
    a ``branch`` key is REJECTED by :func:`parse_run_record` before it
    ever reaches here — see that function.
    """

    environment: str
    script: str
    test_name: str
    result: Result
    start_time: datetime.datetime
    end_time: datetime.datetime
    output: str
    source_link: str
    known_failure_reason: Optional[str]
    build: Optional[str]


class StoredRun(NamedTuple):
    """A test run read back from storage.

    ``output`` is ``None`` when the run came from a list-context query that
    deliberately did not fetch the (potentially large) output column.
    """

    run_id: int
    environment: str
    script: str
    test_name: str
    result: Result
    start_time: datetime.datetime
    end_time: datetime.datetime
    source_link: str
    known_failure_reason: Optional[str]
    output: Optional[str]


def _require_identity_str(obj: Dict[str, Any], field: str) -> str:
    """Fetch a required, non-empty string field or raise ValidationError."""
    if field not in obj:
        raise ValidationError("{}: required field is missing".format(field))
    value = obj[field]
    if not isinstance(value, str):
        raise ValidationError(
            "{}: must be a string, got {}".format(field, type(value).__name__)
        )
    if not value.strip():
        raise ValidationError(
            "{}: must not be empty or whitespace-only".format(field)
        )
    return value


def _require_time(obj: Dict[str, Any], field: str) -> datetime.datetime:
    """Fetch a required ISO-8601 timestamp field or raise ValidationError."""
    if field not in obj:
        raise ValidationError("{}: required field is missing".format(field))
    try:
        return parse_iso(obj[field])
    except ValueError as exc:
        raise ValidationError("{}: {}".format(field, exc))


def parse_run_record(obj: Any) -> RunRecord:
    """Validate a raw transport dict and return a :class:`RunRecord`.

    Rules (all violations raise :class:`ValidationError` with a message
    naming the offending field):

    - ``obj`` must be a dict.
    - ``environment``/``script``/``test_name``: required, str, non-empty
      after stripping whitespace.
    - ``result``: required, must be a valid :class:`Result` name.
    - ``start_time``/``end_time``: required, parseable per
      :func:`parse_iso`; ``end_time`` must be >= ``start_time``.
    - ``output``: required key, must be a str (may be ``""``).
    - ``source_link``: optional, defaults to ``""``; must be str if present.
    - ``known_failure_reason``: optional, defaults to ``None``; must be str
      or null. A run MAY carry one (typically alongside
      ``FAILED_AS_EXPECTED``) and it is surfaced on the test page; a run
      without one imports exactly as before. Blank/whitespace-only is
      normalised to ``None``.
      or ``None``.
    - ``build``: optional, defaults to ``None``; must be str or null if
      present. Blank/whitespace-only is normalised to ``None`` — the
      same rule as ``known_failure_reason``, so a feeder that sends
      ``build: ""`` imports as mainline rather than as a stream named
      the empty string.
    - ``branch``: REJECTED outright, present or not, whatever its value
      (docs/ONE_KIND_PLAN.md §1.2) — the ``branch`` kind died before it
      ever shipped anywhere, so tolerating the key would silently file a
      stale script's runs into mainline once ``branch``'s handling was
      removed, the exact "old-server trap" §3.3 documents for an
      unknown-key-tolerant server. A loud per-record rejection costs
      nothing: ``"branch: removed before this contract ever shipped —
      use build:"``.
    - Unknown extra keys (other than ``branch``) are ignored (forward
      compatibility).
    """
    if not isinstance(obj, dict):
        raise ValidationError(
            "run record must be a JSON object, got {}".format(
                type(obj).__name__
            )
        )

    environment = _require_identity_str(obj, "environment")
    script = _require_identity_str(obj, "script")
    test_name = _require_identity_str(obj, "test_name")

    if "result" not in obj:
        raise ValidationError("result: required field is missing")
    raw_result = obj["result"]
    if not isinstance(raw_result, str):
        raise ValidationError(
            "result: must be a string, got {}".format(type(raw_result).__name__)
        )
    try:
        result = Result[raw_result]
    except KeyError:
        raise ValidationError(
            "result: unknown value '{}' (expected one of {})".format(
                raw_result, ", ".join(r.name for r in Result)
            )
        )

    start_time = _require_time(obj, "start_time")
    end_time = _require_time(obj, "end_time")
    if end_time < start_time:
        raise ValidationError(
            "end_time: must be >= start_time ({} < {})".format(
                format_iso(end_time), format_iso(start_time)
            )
        )

    if "output" not in obj:
        raise ValidationError("output: required field is missing")
    output = obj["output"]
    if not isinstance(output, str):
        raise ValidationError(
            "output: must be a string, got {}".format(type(output).__name__)
        )

    source_link = obj.get("source_link", "")
    if not isinstance(source_link, str):
        raise ValidationError(
            "source_link: must be a string, got {}".format(
                type(source_link).__name__
            )
        )

    known_failure_reason = obj.get("known_failure_reason", None)
    if known_failure_reason is not None and not isinstance(
        known_failure_reason, str
    ):
        raise ValidationError(
            "known_failure_reason: must be a string or null, got {}".format(
                type(known_failure_reason).__name__
            )
        )
    # A whitespace-only reason is no reason; normalise it away so the UI
    # has one thing to test for. Never rejected: a run that does not
    # carry a reason still imports.
    if known_failure_reason is not None and not known_failure_reason.strip():
        known_failure_reason = None

    # WP-25 (docs/ONE_KIND_PLAN.md §1.2): checked by PRESENCE, not value —
    # {"branch": null} still carries the field, and a type/value dance
    # here would just be extra code protecting a key that must never be
    # accepted at all. Deliberately before "build" is even read: the
    # rejection reads the same regardless of what else the record carries.
    if "branch" in obj:
        raise ValidationError(
            "branch: removed before this contract ever shipped — use "
            "build:"
        )

    build = obj.get("build", None)
    if build is not None:
        if not isinstance(build, str):
            raise ValidationError(
                "build: must be a string or null, got {}".format(
                    type(build).__name__
                )
            )
        build = build.strip() or None

    return RunRecord(
        environment=environment,
        script=script,
        test_name=test_name,
        result=result,
        start_time=start_time,
        end_time=end_time,
        output=output,
        source_link=source_link,
        known_failure_reason=known_failure_reason,
        build=build,
    )


def run_record_to_dict(rec: RunRecord) -> Dict[str, Any]:
    """Serialize a :class:`RunRecord` to the exact transport dict shape.

    Round-trips with :func:`parse_run_record`. ``build`` is included
    only when set, so a mainline record serializes to exactly the shape
    every feeder deployed before WP-21 already sends — back compat is
    free (docs/STREAMS_PLAN.md §0.2).
    """
    out = {
        "environment": rec.environment,
        "script": rec.script,
        "test_name": rec.test_name,
        "result": rec.result.value,
        "start_time": format_iso(rec.start_time),
        "end_time": format_iso(rec.end_time),
        "output": rec.output,
        "source_link": rec.source_link,
        "known_failure_reason": rec.known_failure_reason,
    }  # type: Dict[str, Any]
    if rec.build is not None:
        out["build"] = rec.build
    return out
