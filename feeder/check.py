"""Offline checking of a site-specific reader.

Writing the reader is the only bespoke work in a testboard rollout (see
``docs/FEEDER_BRIEF.md``), and it is written against an internal system
this repository knows nothing about. This module closes that loop without
needing a dashboard, a network, or a server: it runs the reader, puts
every record through the same validation the server applies, and reports
what is wrong in the same grouped form the import summary uses.

It also applies a few *sanity heuristics* — things that are not invalid
but are almost always a mistake, above all timestamps that look like
local time rather than UTC. That failure is silent by construction: the
records validate, import cleanly, and quietly put every run in the wrong
hour. A warning here is much cheaper than discovering it from a
"failing since" answer that is wrong by an hour.

Python 3.6 compatible; standard library only.
"""

import collections
import datetime
import logging
import time
from typing import Any, Dict, Iterator, List, NamedTuple, Optional

from feeder.submitter import group_reason, identity_of, render_reasons
from testboard import model
from testboard.model import ValidationError

__all__ = ["CheckReport", "check_reader", "log_report"]

_LOGGER = logging.getLogger(__name__)

#: Records past this far in the future are flagged: the most likely cause
#: is a reader emitting local time from a timezone ahead of UTC.
_FUTURE_TOLERANCE = datetime.timedelta(hours=1)

#: Runs older than this are flagged as probably-wrong timestamps.
_ANCIENT = datetime.timedelta(days=3650)

#: Sample size for the "what did the reader produce" digest.
_MAX_LISTED = 10

#: Below this, the machine is effectively on UTC and the local-time trap
#: cannot bite, so the note about it is noise.
_TRIVIAL_OFFSET_HOURS = 0.01

#: Heuristics about the SHAPE of the data ("everything is a PASS", "every
#: duration is zero") only mean something over a real sample — below this
#: many valid records they are as likely to be true as to be a bug.
_MIN_SAMPLE = 20


class CheckReport(NamedTuple):
    """Everything :func:`check_reader` observed about one reader."""

    read: int
    valid: int
    invalid: int
    reasons: "collections.OrderedDict"
    warnings: List[str]
    environments: "collections.Counter"
    scripts: "collections.Counter"
    results: "collections.Counter"
    earliest: Optional[datetime.datetime]
    latest: Optional[datetime.datetime]
    now: datetime.datetime

    @property
    def ok(self) -> bool:
        """True when every record the reader produced was valid."""
        return self.read > 0 and self.invalid == 0


def check_reader(
    records: Iterator[Dict[str, Any]],
    now: Optional[datetime.datetime] = None,
    max_records: Optional[int] = None,
) -> CheckReport:
    """Validate every record a reader yields and summarize the result.

    *records* is the reader's output (``reader.read(None)``). Nothing is
    sent anywhere. Invalid records are counted and grouped by reason
    rather than raising, exactly as an import would treat them, so one
    run of this reports every problem instead of the first.
    """
    if now is None:
        now = model.utcnow()
    reasons = collections.OrderedDict()  # type: collections.OrderedDict
    environments = collections.Counter()  # type: collections.Counter
    scripts = collections.Counter()  # type: collections.Counter
    results = collections.Counter()  # type: collections.Counter
    read = 0
    valid = 0
    invalid = 0
    earliest = None  # type: Optional[datetime.datetime]
    latest = None  # type: Optional[datetime.datetime]
    future = 0
    ancient = 0
    zero_duration = 0

    for raw in records:
        read += 1
        try:
            record = model.parse_run_record(raw)
        except ValidationError as exc:
            invalid += 1
            group_reason(reasons, str(exc), identity_of(raw))
            continue
        valid += 1
        environments[record.environment] += 1
        scripts[record.script] += 1
        results[record.result.name] += 1
        if earliest is None or record.start_time < earliest:
            earliest = record.start_time
        if latest is None or record.start_time > latest:
            latest = record.start_time
        if record.start_time > now + _FUTURE_TOLERANCE:
            future += 1
        if record.start_time < now - _ANCIENT:
            ancient += 1
        if record.end_time == record.start_time:
            zero_duration += 1
        if max_records is not None and read >= max_records:
            break

    warnings = _sanity_warnings(
        valid, future, ancient, zero_duration, results, environments
    )
    return CheckReport(
        read=read,
        valid=valid,
        invalid=invalid,
        reasons=reasons,
        warnings=warnings,
        environments=environments,
        scripts=scripts,
        results=results,
        earliest=earliest,
        latest=latest,
        now=now,
    )


def _sanity_warnings(
    valid: int,
    future: int,
    ancient: int,
    zero_duration: int,
    results: "collections.Counter",
    environments: "collections.Counter",
) -> List[str]:
    """Flag records that are valid but almost certainly wrong."""
    warnings = []  # type: List[str]
    if valid == 0:
        return warnings
    if future:
        warnings.append(
            "{0} record(s) start in the FUTURE. testboard timestamps are "
            "naive UTC — if the internal system reports local time, "
            "convert it before formatting (see 'UTC conversion is YOUR "
            "job' in docs/FEEDER_BRIEF.md).".format(future)
        )
    if ancient:
        warnings.append(
            "{0} record(s) are more than 10 years old — check the date "
            "parsing in the reader.".format(ancient)
        )
    if zero_duration == valid and valid >= _MIN_SAMPLE:
        warnings.append(
            "every record has end_time == start_time, so every duration "
            "is zero. If the internal system reports a duration, add it "
            "to start_time to get end_time."
        )
    if len(results) == 1 and valid >= _MIN_SAMPLE:
        only = list(results)[0]
        warnings.append(
            "every one of {0} records has result '{1}'. Check the reader "
            "maps the internal system's outcomes onto all four values "
            "(PASS, FAIL, FAILED_AS_EXPECTED, UNEXPECTED_PASS).".format(
                valid, only)
        )
    for environment in environments:
        if environment != environment.strip():
            warnings.append(
                "environment {0!r} has leading/trailing whitespace — it "
                "will not match the same environment written without "
                "it.".format(environment)
            )
    return warnings


def local_utc_offset_hours() -> float:
    """This machine's current offset from UTC, in hours.

    ``time.timezone``/``time.altzone`` are seconds *west* of UTC, so the
    sign is flipped to the conventional one: UTC+1 returns ``1.0``.
    """
    if time.daylight and time.localtime().tm_isdst > 0:
        seconds = -time.altzone
    else:
        seconds = -time.timezone
    return seconds / 3600.0


def format_offset(hours: float) -> str:
    """Render a UTC offset the way a person writes it: ``UTC+1``, ``UTC-4:30``."""
    if abs(hours) < _TRIVIAL_OFFSET_HOURS:
        return "UTC"
    sign = "+" if hours > 0 else "-"
    whole = int(abs(hours))
    minutes = int(round((abs(hours) - whole) * 60))
    if minutes:
        return "UTC{0}{1}:{2:02d}".format(sign, whole, minutes)
    return "UTC{0}{1}".format(sign, whole)


def describe_age(latest: datetime.datetime, now: datetime.datetime) -> str:
    """Describe how far the newest record sits from UTC now."""
    seconds = (now - latest).total_seconds()
    direction = "before" if seconds >= 0 else "AFTER"
    seconds = abs(seconds)
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        size = "{0}d {1}h".format(days, hours)
    elif hours:
        size = "{0}h {1:02d}m".format(hours, minutes)
    else:
        size = "{0}m".format(minutes)
    return "{0} {1} UTC now".format(size, direction)


def _log_clock(report: CheckReport, log: logging.Logger) -> None:
    """Show the newest record against UTC now and this machine's offset.

    The local-time trap has no reliable automatic detector. A reader in a
    zone *ahead* of UTC produces future-dated records, which
    :func:`_sanity_warnings` catches outright; one in a zone *behind* UTC
    just makes every run look older than it is, which is indistinguishable
    from a suite that ran earlier. So the numbers are put side by side and
    named, because the person reading them knows when their suite last ran
    and can tell in a second.
    """
    if report.latest is None:
        return
    offset = local_utc_offset_hours()
    log.info(
        "  newest run:   %s (this machine's clock is %s)",
        describe_age(report.latest, report.now), format_offset(offset),
    )
    if abs(offset) >= _TRIVIAL_OFFSET_HOURS:
        log.info(
            "                If that is not when the suite actually ran, "
            "suspect the reader: times must be UTC, and local time passed "
            "through unchanged puts every run %s.", _shift_phrase(offset),
        )


def _shift_phrase(offset: float) -> str:
    """Describe the error un-converted local time would cause."""
    whole = abs(offset)
    amount = "{0:g} hour{1}".format(whole, "" if whole == 1 else "s")
    return amount + (" late" if offset > 0 else " early")


def log_report(report: CheckReport, log: logging.Logger) -> None:
    """Log a check report as an operator-readable verdict."""
    log.info(
        "reader check: read=%d valid=%d invalid=%d",
        report.read, report.valid, report.invalid,
    )
    if report.read == 0:
        log.error(
            "the reader produced NO records. Check that --source points "
            "at real data, and that the reader's read() yields dicts "
            "(a generator that never yields is silent)."
        )
        return

    log.info(
        "  environments: %s", _describe_counter(report.environments)
    )
    log.info("  scripts:      %d distinct", len(report.scripts))
    log.info("  results:      %s", _describe_counter(report.results))
    if report.earliest is not None and report.latest is not None:
        log.info(
            "  start_time:   %s .. %s (UTC)",
            model.format_iso(report.earliest),
            model.format_iso(report.latest),
        )
        _log_clock(report, log)

    if report.invalid:
        log.error(
            "%d of %d records would be REJECTED. Reasons:",
            report.invalid, report.read,
        )
        for line in render_reasons(report.reasons):
            log.error("  %s", line)
    for warning in report.warnings:
        log.warning("%s", warning)

    if report.ok and not report.warnings:
        log.info("reader OK: every record validates.")
    elif report.ok:
        log.info(
            "reader OK: every record validates, but see the warning(s) "
            "above before importing for real."
        )


def _describe_counter(counter: "collections.Counter") -> str:
    """Render the top entries of a counter as 'name (n)' text."""
    parts = [
        "{0} ({1})".format(name, count)
        for name, count in counter.most_common(_MAX_LISTED)
    ]
    if len(counter) > _MAX_LISTED:
        parts.append("... {0} more".format(len(counter) - _MAX_LISTED))
    return ", ".join(parts) if parts else "none"
