#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""testboard single-file feeder (Python).

Copy this ONE file into your product's own repository. It is the whole
client: nothing else from the testboard checkout is needed, and nothing
outside the Python 3.6+ standard library is imported.

Structure of this file
-----------------------
1. IMPLEMENT THIS (below, between the two banners): a ``DASHBOARD_URL``
   constant, an ``add_site_arguments()`` hook and a ``read_records()``
   function. This is the only part you write. See
   ``docs/FEEDER_TEMPLATE.md`` in the testboard repository for the full
   contract, two worked examples and an acceptance checklist.
2. DO NOT EDIT BELOW THIS LINE: the engine - argument parsing, wire
   validation, batching, retries, replay files, exit codes. Upgrading
   the engine later is: paste a newer copy of everything from the
   banner onward over what you have. Your IMPLEMENT THIS section is
   untouched by that, and nothing about the wire contract requires you
   to re-paste at all - old engines are tolerated forever.

Invocation model
-----------------
This feeder is PUSHED, not polled: your test framework invokes it once,
in its own CLEANUP phase, right after one suite execution finishes,
passing ``--environment`` (required) and optionally ``--build`` (the
one non-mainline stream kind - pass your framework's branch/release
name here; there is no separate ``--branch``, and a record carrying
that key is rejected) plus whatever ``read_records()`` needs to find
this run's results (``--results PATH`` is the worked convention below).
There is no daily mode, no high-water mark, no scanning window:
``read_records()`` is handed only the arguments for THIS invocation and
yields records for THIS suite execution alone. Re-invoking is always
safe - the server upserts on (environment, script, test_name,
start_time) and skips a byte-identical re-send - so a framework that
retries its cleanup step on failure costs nothing extra.

``--environment`` and ``--build`` are stamped onto every record by the
engine (after ``read_records()`` returns them); your reader does not
set either field itself, and any value it does set there is
overridden.

Exit codes (a contract your framework's cleanup step can rely on):
  0  every valid record was accepted (or --dry-run validated cleanly)
  1  the server was unreachable, refused a batch, or an old server did
     not acknowledge --build: the run's results are SAFE (written to a
     replay file next to this script) and the NEXT invocation resends
     them first, before its own batch. Treat this as "deferred", not as
     a broken suite.
  2  usage or validation error (bad arguments, unreadable results,
     no dashboard URL configured, or read_records() crashed outright):
     NOTHING was sent. Fix the invocation or the reader.

Transport: this feeder POSTs directly to the dashboard's backend port,
plain HTTP. It never goes through nginx and never uses a URL prefix -
see the testboard README's "Feeding in your own results" section.

Time bound: every HTTP call carries a --http-timeout (default 15s)
socket timeout. A --time-budget (default 100s) wall-clock deadline is
checked before every attempt - resending a queued replay file, or
sending this run's own batch - and once it is crossed, everything
still pending is deferred to (or left in) a replay file without being
attempted further. One attempt already in flight when the deadline is
crossed is allowed to run to its own timeout, so the whole process
finishes in at most (--time-budget + --http-timeout) seconds - about
115s with the defaults, comfortably inside the ~2 minute ceiling a
cleanup step needs.

Header, not a wire field: this engine's version and the wire-contract
version it speaks are sent as the ``User-Agent`` header on every
import, never as a JSON field - the dashboard's contract makes an
unrecognised field a loud per-record rejection, so a header is the
only change-nothing way to say "here is which engine sent this". No
site is ever required to update this file for the dashboard to keep
accepting it.

Python 3.6+, standard library only - nothing to install on whatever
machine your framework's cleanup step runs on. This file also PARSES
(though it does not run) under Python 2, so invoking it with the wrong
interpreter prints this file's own clear version message instead of a
bare SyntaxError - which is why every function below uses type
COMMENTS rather than inline annotations, and there are no f-strings
anywhere in the file. Do not add either to this file, above or below
the line.
"""

import argparse
import datetime
import glob
import json
import logging
import os
import random
import re
import socket
import sys
import time
import traceback

try:
    from typing import Any, Dict, Iterator, List, Optional, Tuple  # noqa: F401
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass

#: This engine's own version and the /api/import wire-contract version it
#: was written against. Sent as the User-Agent header, never as a JSON
#: field - see the module docstring.
ENGINE_VERSION = "1.0.0"
CONTRACT_VERSION = "1"


# ============================================================================
# IMPLEMENT THIS SECTION - the only part of this file you write.
# See docs/FEEDER_TEMPLATE.md (testboard repo) for the full contract,
# two worked examples, and the acceptance checklist.
# ============================================================================

#: Your dashboard's backend host:port - the DIRECT port, never an nginx
#: front door and never a URL prefix (feeders always speak bare paths).
#: Override at invocation time with --url, mainly useful for testing
#: against a scratch server without editing this constant.
DASHBOARD_URL = "http://localhost:8000"  # CHANGE ME


def add_site_arguments(parser):
    # type: (argparse.ArgumentParser) -> None
    """Add whatever arguments read_records() needs to find this run's
    results. The worked convention is a single ``--results PATH``
    (repeatable); add more if your source needs them (e.g. --log-dir,
    --build-id). Called once, before argument parsing - argparse
    validates for you, so read_records() can assume args is well-formed.
    """
    parser.add_argument(
        "--results", action="append", default=None, metavar="PATH",
        help=(
            "a results file to read (repeatable). The shipped default "
            "reader below treats each as JSON-lines: one run-record "
            "object per line, in the exact /api/import transport "
            "schema (see docs/FEEDER_TEMPLATE.md). Replace "
            "read_records() to read your own format instead"
        ),
    )


def read_records(args):
    # type: (argparse.Namespace) -> Iterator[Dict[str, Any]]
    """Yield one raw transport dict per test run for THIS invocation.

    Plain dicts in the /api/import RunRecord schema: result,
    start_time, end_time, output, and optionally source_link /
    known_failure_reason. Do NOT set "environment" or "build" - the
    engine stamps --environment (and --build, if given) onto every
    record after this function returns, overriding anything set here.

    Must be a generator (``yield``) or return an iterator, and must
    never raise because ONE record is bad: log a warning and skip it -
    the engine validates each record independently anyway, so
    over-reporting (yielding something malformed) is fine and expected.
    What must never happen is this function itself crashing; if your
    source cannot be opened at all, log the problem and return without
    yielding anything rather than raising.

    The shipped default below is the "results-file reader" worked
    example from docs/FEEDER_TEMPLATE.md: JSON-lines from --results,
    passed straight through with no site-specific mapping at all. A
    second worked example (scraping a plain-text test log) is in the
    template; replace this function with whichever shape - or neither -
    fits your test system.
    """
    log = logging.getLogger("testboard_feeder")
    if not args.results:
        log.warning(
            "no --results given and read_records() was not customized; "
            "nothing to read"
        )
        return
    for path in args.results:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("cannot open %s (%s); skipping it", path, exc)
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError as exc:
                    log.warning(
                        "%s:%d: skipping malformed JSON line (%s)",
                        path, line_number, exc,
                    )
                    continue
                if not isinstance(obj, dict):
                    log.warning(
                        "%s:%d: skipping non-object JSON line (got %s)",
                        path, line_number, type(obj).__name__,
                    )
                    continue
                yield obj


# ============================================================================
# DO NOT EDIT BELOW THIS LINE - engine machinery.
# To pick up a new engine version, replace everything from here to the
# end of the file with the new release. Your IMPLEMENT THIS section
# above is untouched by that.
# ============================================================================

#: Per-HTTP-call socket timeout, in seconds. Overridable with
#: --http-timeout (mainly for tests that want a fast black-hole check).
HTTP_TIMEOUT_SECONDS = 15.0

#: Total attempts per batch/replay-file (the first try plus retries).
MAX_ATTEMPTS = 3

#: Exponential backoff base between attempts: 2s then 4s (see
#: _send_with_retry). With HTTP_TIMEOUT_SECONDS=15 that is a worst case
#: of 3*15 + (2+4) = 51s for one batch/replay-file that never answers.
BACKOFF_BASE_SECONDS = 2.0

#: Wall-clock budget for the WHOLE invocation (draining replay files
#: plus sending this run's own batches), in seconds. Checked before
#: every attempt, not just once per unit, so the actual worst case is
#: this plus at most one HTTP_TIMEOUT_SECONDS (the attempt already in
#: flight when the deadline is crossed is allowed to finish) - about
#: 115s with the defaults. Overridable with --time-budget.
TIME_BUDGET_SECONDS = 100.0

#: Records per POST batch, same default as the deployed feeder.
BATCH_SIZE = 500

#: Flush a batch early once its encoded size reaches this many bytes,
#: even short of BATCH_SIZE records - captured test output varies by
#: orders of magnitude, and there is no operator present to react to a
#: 413. Deliberately a constant, not a flag: a site invoking this once
#: per suite execution does not need to tune it.
MAX_BATCH_BYTES = 8 * 1024 * 1024

#: Assumed per-record overhead (identity fields, two timestamps) used
#: only to decide when to flush a batch - approximately right is enough.
_RECORD_OVERHEAD_BYTES = 400

_REPLAY_PREFIX = "testboard_feeder_replay_"
_REPLAY_SUFFIX = ".json"
_CLAIM_SUFFIX = ".sending"

_RESULT_VALUES = ("PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS")
_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$"
)


class ValidationError(Exception):
    """One record failed the /api/import wire-schema rules."""


# ----------------------------------------------------------------------
# Wire-schema validation - a standalone reimplementation of the same
# rules testboard.model.parse_run_record enforces server-side (this
# file cannot import that module: it has to run with nothing but the
# stdlib, from inside a completely different repository).
# ----------------------------------------------------------------------


def _require_str(obj, field):
    # type: (Dict[str, Any], str) -> str
    if field not in obj:
        raise ValidationError("{0}: required field is missing".format(field))
    value = obj[field]
    if not isinstance(value, str):
        raise ValidationError(
            "{0}: must be a string, got {1}".format(field, type(value).__name__)
        )
    if not value.strip():
        raise ValidationError(
            "{0}: must not be empty or whitespace-only".format(field)
        )
    return value


def _parse_timestamp(obj, field):
    # type: (Dict[str, Any], str) -> datetime.datetime
    if field not in obj:
        raise ValidationError("{0}: required field is missing".format(field))
    value = obj[field]
    if not isinstance(value, str):
        raise ValidationError(
            "{0}: expected an ISO-8601 timestamp string, got {1}".format(
                field, type(value).__name__
            )
        )
    match = _TIMESTAMP_RE.match(value)
    if match is None:
        raise ValidationError(
            "{0}: invalid timestamp {1!r}: expected "
            "'YYYY-MM-DDTHH:MM:SS[.ffffff]' (naive UTC, no timezone "
            "suffix)".format(field, value)
        )
    year, month, day, hour, minute, second = (
        int(match.group(i)) for i in range(1, 7)
    )
    fraction = match.group(7) or "0"
    micro = int(fraction.ljust(6, "0"))
    try:
        return datetime.datetime(year, month, day, hour, minute, second, micro)
    except ValueError:
        raise ValidationError(
            "{0}: invalid timestamp {1!r}: not a real calendar "
            "date/time".format(field, value)
        )


def _format_timestamp(dt):
    # type: (datetime.datetime) -> str
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def validate_record(raw):
    # type: (Any) -> Dict[str, Any]
    """Validate one raw transport dict; return the canonical wire dict.

    Raises ValidationError (message names the offending field) on any
    violation. Mirrors testboard.model.parse_run_record exactly,
    including the ``branch`` key rejection (WP-25: that stream kind
    never shipped, so tolerating the key would silently misfile a
    stale client's runs into mainline).
    """
    if not isinstance(raw, dict):
        raise ValidationError(
            "run record must be a JSON object, got {0}".format(
                type(raw).__name__
            )
        )
    environment = _require_str(raw, "environment")
    script = _require_str(raw, "script")
    test_name = _require_str(raw, "test_name")

    if "result" not in raw:
        raise ValidationError("result: required field is missing")
    result = raw["result"]
    if not isinstance(result, str) or result not in _RESULT_VALUES:
        raise ValidationError(
            "result: unknown value {0!r} (expected one of {1})".format(
                result, ", ".join(_RESULT_VALUES)
            )
        )

    start_time = _parse_timestamp(raw, "start_time")
    end_time = _parse_timestamp(raw, "end_time")
    if end_time < start_time:
        raise ValidationError(
            "end_time: must be >= start_time ({0} < {1})".format(
                _format_timestamp(end_time), _format_timestamp(start_time)
            )
        )

    if "output" not in raw:
        raise ValidationError("output: required field is missing")
    output = raw["output"]
    if not isinstance(output, str):
        raise ValidationError(
            "output: must be a string, got {0}".format(type(output).__name__)
        )

    source_link = raw.get("source_link", "")
    if not isinstance(source_link, str):
        raise ValidationError(
            "source_link: must be a string, got {0}".format(
                type(source_link).__name__
            )
        )

    known_failure_reason = raw.get("known_failure_reason")
    if known_failure_reason is not None and not isinstance(
        known_failure_reason, str
    ):
        raise ValidationError(
            "known_failure_reason: must be a string or null, got "
            "{0}".format(type(known_failure_reason).__name__)
        )
    if known_failure_reason is not None and not known_failure_reason.strip():
        known_failure_reason = None

    # Checked by PRESENCE, not value - {"branch": null} still carries
    # the key. Before "build" is even read, so the rejection reads the
    # same regardless of what else the record carries.
    if "branch" in raw:
        raise ValidationError(
            "branch: removed before this contract ever shipped — use "
            "build:"
        )

    build = raw.get("build")
    if build is not None:
        if not isinstance(build, str):
            raise ValidationError(
                "build: must be a string or null, got {0}".format(
                    type(build).__name__
                )
            )
        build = build.strip() or None

    record = {
        "environment": environment,
        "script": script,
        "test_name": test_name,
        "result": result,
        "start_time": _format_timestamp(start_time),
        "end_time": _format_timestamp(end_time),
        "output": output,
        "source_link": source_link,
        "known_failure_reason": known_failure_reason,
    }  # type: Dict[str, Any]
    if build is not None:
        record["build"] = build
    return record


def _identity_of(raw):
    # type: (Any) -> str
    """Best-effort ``environment / script / test_name [@ start_time]``."""
    if not isinstance(raw, dict):
        return "<no identity: record is {0}>".format(type(raw).__name__)
    parts = []  # type: List[str]
    for field in ("environment", "script", "test_name"):
        value = raw.get(field)
        parts.append(value if isinstance(value, str) and value.strip() else "?")
    identity = " / ".join(parts)
    start = raw.get("start_time")
    if isinstance(start, str) and start.strip():
        identity += " @ " + start
    return identity


# ----------------------------------------------------------------------
# HTTP transport
# ----------------------------------------------------------------------


def _normalize_url(url):
    # type: (str) -> str
    trimmed = url.rstrip("/")
    if not trimmed.endswith("/api/import"):
        trimmed += "/api/import"
    return trimmed


def _post(url, body, headers, timeout):
    # type: (str, bytes, Dict[str, str], float) -> Tuple[int, bytes]
    """POST once; HTTP error statuses return, transport failures raise."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    try:
        return response.getcode(), response.read()
    finally:
        response.close()


def _describe_connection_error(url, exc):
    # type: (str, BaseException) -> str
    import urllib.error

    reason = exc  # type: BaseException
    if isinstance(exc, urllib.error.URLError) and not isinstance(
        exc, urllib.error.HTTPError
    ):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, BaseException):
            reason = inner
    if isinstance(reason, socket.timeout):
        return "request to {0} timed out".format(url)
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed for the host in {0}".format(url)
    return "cannot reach {0} ({1}: {2})".format(
        url, type(reason).__name__, reason
    )


def _truncate(data, limit=200):
    # type: (Any, int) -> str
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _decode_json_object(data):
    # type: (bytes) -> Optional[Dict[str, Any]]
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _send_with_retry(
    url, body, headers, deadline, http_timeout, max_attempts,
    backoff_base, clock, sleep, log, label,
):
    # type: (str, bytes, Dict[str, str], float, float, int, float, Any, Any, logging.Logger, str) -> Dict[str, Any]
    """POST ``body`` with retry/backoff, bounded by ``deadline``.

    Returns a dict: {"ok": bool, "deferred": bool, "reason": str,
    "payload": Optional[dict], "streams_seen_present": bool}.
    ``deferred`` means the time budget ran out before any attempt was
    even made - as distinct from an attempt that was made and failed.
    """
    reason = "unknown error"
    for attempt in range(1, max_attempts + 1):
        if clock() >= deadline:
            return {
                "ok": False, "deferred": True,
                "reason": (
                    "time budget exhausted before attempt {0} of {1} "
                    "for {2}".format(attempt, max_attempts, label)
                ),
                "payload": None, "streams_seen_present": False,
            }
        if attempt > 1:
            delay = backoff_base * (2 ** (attempt - 2))
            log.info(
                "%s: retrying in %.1fs (attempt %d of %d)",
                label, delay, attempt, max_attempts,
            )
            sleep(delay)
        try:
            status, response_body = _post(url, body, headers, http_timeout)
        except Exception as exc:
            reason = _describe_connection_error(url, exc)
            log.warning(
                "%s: attempt %d of %d failed: %s",
                label, attempt, max_attempts, reason,
            )
            continue
        if status == 200:
            payload = _decode_json_object(response_body)
            return {
                "ok": True, "deferred": False, "reason": "",
                "payload": payload,
                "streams_seen_present": (
                    payload is not None and "streams_seen" in payload
                ),
            }
        if status >= 500:
            reason = "server error HTTP {0} (response: {1})".format(
                status, _truncate(response_body)
            )
            log.warning(
                "%s: attempt %d of %d failed: %s",
                label, attempt, max_attempts, reason,
            )
            continue
        # 4xx: the request itself was rejected - retrying cannot help.
        reason = "HTTP {0} from the server (response: {1})".format(
            status, _truncate(response_body)
        )
        log.warning("%s: %s - not retrying a client error", label, reason)
        break
    return {
        "ok": False, "deferred": False, "reason": reason,
        "payload": None, "streams_seen_present": False,
    }


def _report_batch_payload(payload, label, log):
    # type: (Optional[Dict[str, Any]], str, logging.Logger) -> Tuple[int, int, int]
    if payload is None:
        log.warning(
            "%s: server returned 200 but the response body was not a "
            "usable JSON object", label,
        )
        return (0, 0, 0)
    inserted = int(payload.get("inserted", 0) or 0)
    updated = int(payload.get("updated", 0) or 0)
    rejected = int(payload.get("rejected", 0) or 0)
    errors = payload.get("errors", [])
    if isinstance(errors, list):
        for error in errors[:5]:
            if isinstance(error, dict):
                log.warning(
                    "%s: server rejected record index %s: %s",
                    label, error.get("index"), error.get("error"),
                )
        if len(errors) > 5:
            log.warning(
                "%s: %d more rejected record(s) not shown individually",
                label, len(errors) - 5,
            )
    log.info(
        "%s: inserted=%d updated=%d rejected=%d", label, inserted, updated,
        rejected,
    )
    return (inserted, updated, rejected)


# ----------------------------------------------------------------------
# Replay files - the only persistence this feeder has. Names are
# per-invocation (pid + millisecond timestamp + random suffix) so
# concurrent cleanups from different environments on one host never
# collide, and a fresh name is reserved with O_CREAT|O_EXCL so two
# processes racing to allocate one can never both win.
# ----------------------------------------------------------------------


def _sanitize(text):
    # type: (str) -> str
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "unnamed"


def _new_replay_path(replay_dir, environment):
    # type: (str, str) -> str
    safe_env = _sanitize(environment)
    for _attempt in range(50):
        token = "{0}-{1}-{2:04d}".format(
            os.getpid(), int(time.time() * 1000), random.randint(0, 9999)
        )
        name = "{0}{1}_{2}{3}".format(
            _REPLAY_PREFIX, safe_env, token, _REPLAY_SUFFIX
        )
        path = os.path.join(replay_dir, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            continue  # name collision (astronomically unlikely) - retry
        os.close(fd)
        return path
    raise OSError(
        "could not allocate a unique replay file name in {0}".format(
            replay_dir
        )
    )


def _write_replay(path, body):
    # type: (str, bytes) -> None
    with open(path, "wb") as handle:
        handle.write(body)


def _pending_replay_files(replay_dir):
    # type: (str) -> List[str]
    pattern = os.path.join(replay_dir, _REPLAY_PREFIX + "*" + _REPLAY_SUFFIX)
    return sorted(glob.glob(pattern))


def _claim(path):
    # type: (str) -> Optional[str]
    """Atomically rename ``path`` to reserve it for this process.

    Returns the claimed path, or None if another invocation already
    claimed or removed it first (a lost race, not an error).
    """
    claimed = path + _CLAIM_SUFFIX
    try:
        os.rename(path, claimed)
    except OSError:
        return None
    return claimed


def _release_claim(claimed, original, log):
    # type: (str, str, logging.Logger) -> None
    """Give a claimed-but-still-failing replay file back its real name
    so a later invocation will pick it up again."""
    try:
        os.rename(claimed, original)
    except OSError as exc:
        log.warning(
            "could not restore replay file %s for a later retry (%s); "
            "if this persists, check %s by hand",
            original, exc, os.path.dirname(original) or ".",
        )


def _body_expects_streams_ack(body):
    # type: (bytes) -> bool
    """True if any record in ``body`` carries a build (needs streams_seen).

    Computed from the OUTGOING body rather than from this invocation's
    own --build flag: a queued replay file may have been produced by a
    build-carrying invocation and drained by a later mainline one (or
    vice versa), and the correctness of the streams_seen check depends
    on what is actually being sent, not on who happens to be sending it.
    """
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    runs = envelope.get("runs") if isinstance(envelope, dict) else None
    if not isinstance(runs, list):
        return False
    return any(isinstance(r, dict) and "build" in r for r in runs)


def _drain_replay_files(
    url, headers, replay_dir, deadline, http_timeout, clock, sleep, log,
):
    # type: (str, Dict[str, str], str, float, float, Any, Any, logging.Logger) -> int
    """Resend every pending replay file; return the count still pending
    afterwards (failed again, or never attempted for lack of time)."""
    still_pending = 0
    for path in _pending_replay_files(replay_dir):
        if clock() >= deadline:
            log.warning(
                "time budget exhausted; leaving %s (and any later replay "
                "files) for the next invocation", path,
            )
            still_pending += 1
            continue
        claimed = _claim(path)
        if claimed is None:
            continue  # another invocation is already handling this one
        try:
            with open(claimed, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            log.error(
                "could not read replay file %s (%s); leaving it for a "
                "later retry", claimed, exc,
            )
            _release_claim(claimed, path, log)
            still_pending += 1
            continue
        expect_ack = _body_expects_streams_ack(body)
        result = _send_with_retry(
            url, body, headers, deadline, http_timeout, MAX_ATTEMPTS,
            BACKOFF_BASE_SECONDS, clock, sleep, log,
            "replay file {0}".format(os.path.basename(path)),
        )
        if result["ok"]:
            if expect_ack and not result["streams_seen_present"]:
                log.error(
                    "replay file %s: this batch carries --build records "
                    "but the server's response has no streams_seen key "
                    "at all - an old server would have filed it into "
                    "mainline. The batch WAS accepted (HTTP 200), so it "
                    "is not resent - check the dashboard by hand.", path,
                )
            else:
                _report_batch_payload(
                    result["payload"], "replay file " + path, log
                )
            try:
                os.remove(claimed)
            except OSError:
                pass
            log.info("replay file %s: resent successfully", path)
        else:
            log.error(
                "replay file %s: still failing (%s)", path, result["reason"]
            )
            _release_claim(claimed, path, log)
            still_pending += 1
    return still_pending


# ----------------------------------------------------------------------
# Batching and sending this invocation's own records
# ----------------------------------------------------------------------


def _batches(records, batch_size, max_bytes):
    # type: (List[Dict[str, Any]], int, int) -> Iterator[List[Dict[str, Any]]]
    batch = []  # type: List[Dict[str, Any]]
    batch_bytes = 0
    for record in records:
        batch.append(record)
        batch_bytes += len(record.get("output", "")) + _RECORD_OVERHEAD_BYTES
        if len(batch) >= batch_size or batch_bytes >= max_bytes:
            yield batch
            batch = []
            batch_bytes = 0
    if batch:
        yield batch


def _send_own_records(
    records, url, environment, build, replay_dir, deadline, http_timeout,
    clock, sleep, log, headers,
):
    # type: (List[Dict[str, Any]], str, str, Optional[str], str, float, float, Any, Any, logging.Logger, Dict[str, str]) -> Tuple[int, int, int, int, int]
    """Batch and send ``records``; return (sent, inserted, updated,
    rejected, failed_batches). Every failed batch is saved to a fresh
    replay file before this returns - nothing is ever only in memory."""
    sent = inserted = updated = rejected = failed_batches = 0
    for batch in _batches(records, BATCH_SIZE, MAX_BATCH_BYTES):
        body = json.dumps({"runs": batch}).encode("utf-8")
        expect_ack = any("build" in record for record in batch)
        if clock() >= deadline:
            path = _new_replay_path(replay_dir, environment)
            _write_replay(path, body)
            log.error(
                "time budget exhausted before this batch of %d records "
                "could be sent; saved to %s for the next invocation",
                len(batch), path,
            )
            failed_batches += 1
            continue
        result = _send_with_retry(
            url, body, headers, deadline, http_timeout, MAX_ATTEMPTS,
            BACKOFF_BASE_SECONDS, clock, sleep, log,
            "batch of {0} records".format(len(batch)),
        )
        if result["ok"] and expect_ack and not result["streams_seen_present"]:
            path = _new_replay_path(replay_dir, environment)
            _write_replay(path, body)
            log.error(
                "this run used --build %r but the server's response has "
                "no streams_seen key at all - it does not understand "
                "builds and would have silently filed these into "
                "mainline. The batch WAS accepted server-side; its body "
                "is ALSO saved to %s so the mismatch can be "
                "investigated. Update the dashboard, or drop --build to "
                "import as mainline deliberately.", build, path,
            )
            failed_batches += 1
            continue
        if result["ok"]:
            counts = _report_batch_payload(result["payload"], "this run", log)
            sent += len(batch)
            inserted += counts[0]
            updated += counts[1]
            rejected += counts[2]
            continue
        path = _new_replay_path(replay_dir, environment)
        _write_replay(path, body)
        log.error(
            "batch of %d records failed (%s); saved to %s - the NEXT "
            "invocation resends it before its own batch",
            len(batch), result["reason"], path,
        )
        failed_batches += 1
    return sent, inserted, updated, rejected, failed_batches


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

_EPILOG = """\
exit codes
  0  every valid record was accepted (or --dry-run validated cleanly)
  1  the server was unreachable/refused a batch, or an old server did
     not acknowledge --build - results are SAFE in a replay file next
     to this script; the next invocation resends them first
  2  usage/validation error - nothing was sent

See docs/FEEDER_TEMPLATE.md in the testboard repository for the wire
schema, the invocation contract and two worked reader examples.
"""


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        prog="feeder.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Push this suite execution's results into a testboard "
            "dashboard. Invoked once per suite execution, from your "
            "test framework's cleanup phase."
        ),
        epilog=_EPILOG,
    )
    parser.add_argument(
        "--environment", required=True, metavar="NAME",
        help="the environment this suite execution ran on (required)",
    )
    parser.add_argument(
        "--build", default=None, metavar="NAME",
        help=(
            "stamp every record as belonging to build stream NAME "
            "instead of mainline - pass your framework's branch/release "
            "parameter here (there is no separate --branch). Requires "
            "the server to acknowledge it via streams_seen; an old "
            "server's silence is treated as fatal, loudly, before "
            "anything is left unresent"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate records and print what would be sent; POST nothing",
    )
    parser.add_argument(
        "--url", default=None, metavar="URL",
        help="override the DASHBOARD_URL constant above (mainly for testing)",
    )
    parser.add_argument(
        "--replay-dir", default=".", metavar="DIR",
        help="directory for replay files (default: current directory)",
    )
    parser.add_argument(
        "--http-timeout", type=float, default=HTTP_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="per-HTTP-call socket timeout (default: %(default)s)",
    )
    parser.add_argument(
        "--time-budget", type=float, default=TIME_BUDGET_SECONDS,
        metavar="SECONDS",
        help=(
            "wall-clock budget for the whole invocation (default: "
            "%(default)s) - see the module docstring for the exact "
            "worst-case arithmetic"
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging plus full tracebacks on fatal errors",
    )
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "this feeder requires Python 3.6+ - you are running "
            "{0}.{1}.{2}. Re-run with: python3 feeder.py\n".format(
                sys.version_info[0], sys.version_info[1], sys.version_info[2]
            )
        )
        return 2

    parser = build_parser()
    add_site_arguments(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("testboard_feeder")

    environment = args.environment.strip()
    if not environment:
        log.error("--environment must not be empty or whitespace-only")
        return 2

    build = args.build
    if build is not None:
        build = build.strip() or None
        if build is None:
            log.error("--build must not be empty or whitespace-only")
            return 2

    raw_url = (args.url or DASHBOARD_URL or "").strip()
    if not raw_url:
        log.error(
            "no dashboard URL: set DASHBOARD_URL at the top of this file, "
            "or pass --url"
        )
        return 2
    url = _normalize_url(raw_url)

    replay_dir = args.replay_dir
    if not args.dry_run and not os.path.isdir(replay_dir):
        log.error(
            "--replay-dir %s does not exist or is not a directory",
            replay_dir,
        )
        return 2

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "testboard-feeder-python/{0} (contract {1})".format(
            ENGINE_VERSION, CONTRACT_VERSION
        ),
    }

    clock = time.time
    sleep = time.sleep
    deadline = clock() + args.time_budget

    still_pending = 0
    if not args.dry_run:
        still_pending = _drain_replay_files(
            url, headers, replay_dir, deadline, args.http_timeout,
            clock, sleep, log,
        )

    try:
        raw_iterator = iter(read_records(args))
    except Exception as exc:
        log.error(
            "read_records() raised before producing anything: %s: %s",
            type(exc).__name__, exc,
        )
        if args.verbose:
            traceback.print_exc()
        return 2

    read = 0
    valid = 0
    skipped = 0
    reasons = {}  # type: Dict[str, int]
    canonical = []  # type: List[Dict[str, Any]]
    try:
        for raw in raw_iterator:
            read += 1
            if isinstance(raw, dict):
                raw = dict(raw)
                raw["environment"] = environment
                if build is not None:
                    raw["build"] = build
            try:
                record = validate_record(raw)
            except ValidationError as exc:
                skipped += 1
                reason = str(exc)
                count = reasons.get(reason, 0) + 1
                reasons[reason] = count
                if count <= 5:
                    log.warning(
                        "skipping invalid record [%s] %s",
                        _identity_of(raw), reason,
                    )
                elif count == 6:
                    log.warning(
                        "[%s] has now affected more than 5 records - "
                        "further occurrences will be counted but not "
                        "logged individually", reason,
                    )
                continue
            valid += 1
            canonical.append(record)
    except Exception as exc:
        log.error(
            "read_records() crashed after producing %d record(s): %s: "
            "%s", read, type(exc).__name__, exc,
        )
        if args.verbose:
            traceback.print_exc()
        return 2

    if args.dry_run:
        for index, record in enumerate(canonical[:3], 1):
            sys.stdout.write(
                "\n--- record {0} would be sent as ---\n".format(index)
            )
            sys.stdout.write(json.dumps(record, indent=2, sort_keys=True))
            sys.stdout.write("\n")
        sys.stdout.flush()
        log.info(
            "dry run: read=%d valid=%d skipped=%d - nothing was sent",
            read, valid, skipped,
        )
        return 0

    sent, inserted, updated, rejected, failed_batches = _send_own_records(
        canonical, url, environment, build, replay_dir, deadline,
        args.http_timeout, clock, sleep, log, headers,
    )

    log.info(
        "feeder summary: read=%d valid=%d skipped=%d sent=%d inserted=%d "
        "updated=%d rejected=%d failed_batches=%d replay_files_pending=%d",
        read, valid, skipped, sent, inserted, updated, rejected,
        failed_batches, still_pending,
    )

    if failed_batches or still_pending:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
