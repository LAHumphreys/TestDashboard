#!/usr/bin/env python3
"""Sends one test run's results to a testboard dashboard.

This is a single-file client: your test framework invokes it once, in
its cleanup step, right after a suite execution finishes, and it POSTs
that run's results to the dashboard so they show up on the team's
board. It is deliberately small and self-contained so it can live in
this repository like any other piece of test tooling.

For the reviewer: what this file does, in full
-----------------------------------------------
- Reads this run's results using the two functions in the IMPLEMENT
  THIS section below - this site's own command-line flags and its own
  reader, filled in where the shipped stubs are.
- POSTs them, in batches, to the ONE URL given on the command line as
  ``--url``. Plain HTTP on the internal network; nothing else is
  contacted, and no URL is baked into the file.
- Uses only the Python 3.6+ standard library. Nothing to install, no
  build step, no imports beyond the stdlib.
- Writes no files, keeps no state, starts no background work. Its only
  outputs are log lines (stderr) and an exit code, and it finishes in
  well under a minute even when the dashboard is down.

If the dashboard cannot be reached, the feeder retries a couple of
times, then gives up with exit code 1 - and nothing is lost. The
dashboard treats a re-send of the same results as a harmless no-op
(records are keyed by test identity and start time), so re-running
the feeder later - your framework retrying its cleanup step, or a
person re-running the same command - delivers them. That idempotency
is why this file can stay small: it does not need its own queue,
state file, or any persistence of its own.

How the file is laid out
-------------------------
1. IMPLEMENT THIS (between the two banners): an
   ``add_site_arguments()`` hook for this site's command-line flags,
   and a ``read_records()`` function that yields this run's results.
   Shipped as stubs - this is the only part written per site.
2. DO NOT EDIT BELOW THIS LINE: the engine - argument parsing, a
   shallow sanity check, batching, retries, exit codes. To take a
   newer engine release later, paste it over everything below that
   banner; the site's section above is untouched by the upgrade.

Invoking it
------------
    python3 feeder_micro.py --url http://dashboard-host:8000 \\
        --environment NAME [--build NAME] [this site's own flags]

``--url`` (required) is the dashboard's backend host:port - always
given on the command line, never hardcoded. ``--environment``
(required) names the test environment the suite ran on. ``--build``
marks the whole run as belonging to a release/RC build stream instead
of mainline - pass the framework's branch/release parameter here. The
engine stamps environment and build onto every record; the reader
never sets them.

Exit codes (safe for a cleanup step to rely on):
  0  the server accepted every batch (individually rejected records
     are reported in the log and never abort an import), or --dry-run
     finished cleanly
  1  a batch was not accepted - the server was unreachable or kept
     failing. Nothing is saved locally; re-invoking this feeder
     re-sends everything, safely.
  2  the invocation itself was wrong (missing or bad arguments, or
     read_records() crashed) - nothing was sent.

One convention worth knowing before reading the code: the engine's
version travels as the ``User-Agent`` header rather than inside the
JSON, because the dashboard rejects records that carry unknown
fields - a header identifies the sender without touching the wire
contract.

This is the reduced sibling of the full engine (``clients/feeder.py``
in the testboard repository), which adds on-disk replay files, a
wall-clock time budget, full client-side wire validation, and a check
that the server acknowledged ``--build`` records, for sites that want
them - this engine simply trusts the server it was pointed at. The
IMPLEMENT THIS contract is shared: a section written for the full
engine drops in here unchanged - a conformance test in the testboard
repository transplants it on every push - though its
``DASHBOARD_URL`` constant goes unused, because this engine always
takes ``--url``.
"""

import argparse
import json
import logging
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

#: This engine's own version and the /api/import wire-contract version
#: it was written against. Sent as the User-Agent header on every
#: import - see the note at the end of the module docstring.
ENGINE_VERSION = "1.0.0"
CONTRACT_VERSION = "1"


# ============================================================================
# IMPLEMENT THIS SECTION - the only part of this file you write.
# See docs/FEEDER_TEMPLATE.md (testboard repo) for the full contract,
# two worked examples, and the acceptance checklist.
# ============================================================================


def add_site_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the command-line flags read_records() needs to find this
    run's results - a log directory, a results file, a run id,
    whatever fits this site's test framework. Called once, before
    argument parsing - argparse validates for you, so read_records()
    can assume args is well-formed.

    Example:
        parser.add_argument("--log-dir", required=True, metavar="DIR",
                            help="directory holding this run's logs")
    """
    pass


def read_records(args: argparse.Namespace) -> Iterator[Dict[str, Any]]:
    """Yield one dict per test run for THIS invocation, in the
    /api/import RunRecord schema: script, test_name, result,
    start_time, end_time, output, and optionally source_link /
    known_failure_reason. Do NOT set "environment" or "build" - the
    engine stamps --environment (and --build, if given) onto every
    record after this function returns, overriding anything set here.

    Must be a generator (``yield``) or return an iterator, and must
    never raise because ONE record is bad: log a warning, skip it, and
    keep going - the engine checks each record independently anyway,
    so over-reporting (yielding something malformed) is fine and
    expected. What must never happen is this function itself crashing;
    if your source cannot be opened at all, log the problem and return
    without yielding anything rather than raising.

    See docs/FEEDER_TEMPLATE.md in the testboard repository for the
    full schema and two complete worked readers.
    """
    logging.getLogger("testboard_feeder").warning(
        "read_records() has not been implemented for this site yet; "
        "nothing to read"
    )
    return iter(())


# ============================================================================
# DO NOT EDIT BELOW THIS LINE - engine machinery.
# To pick up a new engine version, replace everything from here to the
# end of the file with the new release. Your IMPLEMENT THIS section
# above is untouched by that.
# ============================================================================

#: Socket timeout for each HTTP call, in seconds (--http-timeout
#: overrides it). This is the engine's only time knob: a dashboard
#: that never answers costs at most MAX_ATTEMPTS of these plus the
#: pauses between them - about 49 seconds with the defaults.
HTTP_TIMEOUT_SECONDS = 15.0

#: How many times to try each batch (first try included), pausing a
#: flat RETRY_PAUSE_SECONDS between tries. Enough to ride out a blip;
#: a real outage becomes exit code 1 and a re-invocation later.
MAX_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 2.0

#: Records per POST. 500 keeps request bodies comfortably sized while
#: still sending a typical suite execution in one round trip.
BATCH_SIZE = 500

#: Also flush a batch early once its estimated encoded size reaches
#: this many bytes: captured test output varies by orders of
#: magnitude, and one giant-output run must not push a request past
#: what the server will accept.
MAX_BATCH_BYTES = 8 * 1024 * 1024

#: Rough per-record size (identity fields, two timestamps) used only
#: in the early-flush arithmetic above - approximate is fine.
_RECORD_OVERHEAD_BYTES = 400

#: How many per-record rejections to quote in full in the log before
#: summarising the rest as a count.
_MAX_LOGGED_REJECTIONS = 5

#: Longest piece of a server error response quoted into one log line.
_MAX_ERROR_TEXT_CHARS = 200

#: The four result values the dashboard understands, and the fields
#: every record must carry as non-empty strings.
_RESULT_VALUES = ("PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS")
_REQUIRED_STRING_FIELDS = (
    "environment", "script", "test_name", "start_time", "end_time",
)


def _validate_record(raw: Any) -> Optional[str]:
    """Return why ``raw`` cannot be sent, or None if it looks sendable.

    Deliberately shallow: required fields present, result value
    recognised, nothing more. The server validates every record in
    full on arrival and reports back per-record errors (which this
    engine logs), so deeper checking here would only be a second copy
    of the server's rules to keep in sync. This check exists to stop
    an obviously broken record from wasting a whole batch's round
    trip.
    """
    if not isinstance(raw, dict):
        return f"record must be a JSON object, got {type(raw).__name__}"

    for field in _REQUIRED_STRING_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field}: required and must be a non-empty string"

    if raw.get("result") not in _RESULT_VALUES:
        expected = ", ".join(_RESULT_VALUES)
        return (
            f"result: unknown value {raw.get('result')!r} "
            f"(expected one of {expected})"
        )

    if not isinstance(raw.get("output"), str):
        return "output: required and must be a string"
    return None


# ----------------------------------------------------------------------
# HTTP transport
# ----------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """Accept the bare host:port form and append the import endpoint."""
    trimmed = url.rstrip("/")
    if not trimmed.endswith("/api/import"):
        trimmed += "/api/import"
    return trimmed


def _post(url: str,
          body: bytes,
          headers: Dict[str, str],
          timeout: float) -> Tuple[int, bytes]:
    """POST once. HTTP error statuses are returned like any other
    response; only transport failures (refused, timed out) raise."""

    request = (
        urllib.request.Request(url, data=body, headers=headers, method="POST"))

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()

    try:
        return response.getcode(), response.read()
    finally:
        response.close()


def _describe_connection_error(url: str, exc: BaseException) -> str:
    """One readable sentence for a failed connection, naming the two
    commonest causes (timeout, DNS) directly instead of via a nested
    exception repr."""
    # urllib never raises the underlying OS error directly: a refused
    # connection, timeout or DNS failure arrives wrapped in URLError,
    # with the real cause attached as its .reason - usually the
    # original exception, occasionally a plain string. Unwrap it only
    # when it IS an exception, so the isinstance checks below see the
    # real socket error. HTTPError (a URLError subclass) is excluded:
    # its .reason is just the status phrase ("Not Found"), and _post()
    # returns HTTP error statuses rather than raising them anyway.
    reason: BaseException = exc

    if (isinstance(exc, urllib.error.URLError) and
        not isinstance(exc, urllib.error.HTTPError)):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, BaseException):
            reason = inner

    if isinstance(reason, socket.timeout):
        return f"request to {url} timed out"
    if isinstance(reason, socket.gaierror):
        return f"DNS lookup failed for the host in {url}"
    return f"cannot reach {url} ({type(reason).__name__}: {reason})"


def _describe_http_error(status: int, response_body: bytes) -> str:
    """One readable sentence for a non-200 response, quoting what the
    server said, cut to a size that keeps the log line useful."""
    text = response_body.decode("utf-8", errors="replace")
    if len(text) > _MAX_ERROR_TEXT_CHARS:
        text = text[:_MAX_ERROR_TEXT_CHARS] + "...[truncated]"

    return f"HTTP {status} from the server (response: {text})"


def _decode_response(response_body: bytes,
                     log: logging.Logger,
                     label: str) -> Dict[str, Any]:
    """The 200 response decoded, or {} - with a warning - when the
    body was not a JSON object. Accepted-but-unreadable is still
    accepted; it only costs the per-batch counts in the log."""
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None

    if not isinstance(payload, dict):
        log.warning(
            "%s: server returned 200 but the response body was not a "
            "usable JSON object", label,
        )
        return {}
    return payload


class _Attempt(NamedTuple):
    """What one POST attempt came back with.

    ``payload`` is set - possibly to {} - when the batch was accepted.
    Otherwise ``failure`` says what went wrong, and ``retryable``
    whether another try could change the answer.
    """
    payload: Optional[Dict[str, Any]]
    failure: str
    retryable: bool


def _attempt_post(url: str,
                  body: bytes,
                  headers: Dict[str, str],
                  timeout: float,
                  log: logging.Logger,
                  label: str) -> _Attempt:
    """Make one POST and classify what came back.

    Connection errors and HTTP 5xx are worth retrying; a 4xx is not,
    because the request itself was rejected and re-sending the same
    request cannot help.
    """
    try:
        status, response_body = _post(url, body, headers, timeout)
    except Exception as exc:
        return _Attempt(
            payload=None,
            failure=_describe_connection_error(url, exc),
            retryable=True,
        )

    if status == 200:
        payload = _decode_response(response_body, log, label)
        return _Attempt(payload=payload, failure="", retryable=False)
    return _Attempt(
        payload=None,
        failure=_describe_http_error(status, response_body),
        retryable=status >= 500,
    )


def _send_batch(url: str,
                body: bytes,
                headers: Dict[str, str],
                http_timeout: float,
                log: logging.Logger,
                label: str) -> Optional[Dict[str, Any]]:
    """Deliver one batch, riding out brief trouble.

    Returns the server's response object once accepted, or None once
    out of attempts (or on a failure a retry cannot fix).
    """
    attempts_used = 0
    payload: Optional[Dict[str, Any]] = None
    keep_trying = True

    while payload is None and keep_trying:
        attempts_used += 1
        attempt = _attempt_post(url, body, headers, http_timeout, log, label)

        if attempt.payload is not None:
            payload = attempt.payload
        elif attempts_used >= MAX_ATTEMPTS or not attempt.retryable:
            log.error("%s: giving up (%s)", label, attempt.failure)
            keep_trying = False
        else:
            log.warning(
                "%s: attempt %d of %d failed: %s - retrying in %.0fs",
                label, attempts_used, MAX_ATTEMPTS, attempt.failure,
                RETRY_PAUSE_SECONDS)
            time.sleep(RETRY_PAUSE_SECONDS)

    return payload


class _BatchCounts(NamedTuple):
    """The server's verdict on one accepted batch."""

    inserted: int
    updated: int
    rejected: int


def _count(payload: Dict[str, Any], key: str) -> int:
    """A count from the server response, read defensively: absent or
    not a number means zero - a count is never worth crashing over."""
    value = payload.get(key)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _report_payload(payload: Dict[str, Any],
                    label: str,
                    log: logging.Logger) -> _BatchCounts:
    """Log what the server said about an accepted batch.

    The first few per-record rejections are quoted in full - enough to
    see what was wrong from the log alone - and the rest are
    summarised as a count.
    """
    counts = _BatchCounts(
        inserted=_count(payload, "inserted"),
        updated=_count(payload, "updated"),
        rejected=_count(payload, "rejected"),
    )

    errors = payload.get("errors", [])
    if isinstance(errors, list):
        for error in errors[:_MAX_LOGGED_REJECTIONS]:
            if isinstance(error, dict):
                log.warning(
                    "%s: server rejected record index %s: %s",
                    label, error.get("index"), error.get("error"),
                )
        unlogged = len(errors) - _MAX_LOGGED_REJECTIONS
        if unlogged > 0:
            log.warning(
                "%s: %d more rejected record(s) not shown individually",
                label, unlogged,
            )

    log.info(
        "%s: inserted=%d updated=%d rejected=%d",
        label, counts.inserted, counts.updated, counts.rejected,
    )
    return counts


def _batches(records: List[Dict[str, Any]],
             batch_size: int,
             max_bytes: int) -> Iterator[List[Dict[str, Any]]]:
    """Group records into lists of at most ``batch_size``, flushing a
    batch early when its estimated encoded size reaches ``max_bytes``."""
    batch: List[Dict[str, Any]] = []
    batch_bytes = 0

    for record in records:
        batch.append(record)
        output_size = len(record.get("output", ""))
        batch_bytes += output_size + _RECORD_OVERHEAD_BYTES

        if len(batch) >= batch_size or batch_bytes >= max_bytes:
            yield batch
            batch = []
            batch_bytes = 0

    if batch:
        yield batch


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

_EPILOG = """\
exit codes
  0  the server accepted every batch (per-record rejections are
     logged, never fatal); or --dry-run finished cleanly
  1  a batch was not accepted - nothing is saved locally, and
     re-invoking this feeder re-sends everything, safely
  2  the invocation itself was wrong - nothing was sent

This is the micro engine. The full engine (clients/feeder.py in the
testboard repository) adds replay files, a wall-clock time budget and
full client-side validation; docs/FEEDER_TEMPLATE.md there has the
wire schema and two worked reader examples.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feeder_micro.py",
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
        "--url", required=True, metavar="URL",
        help=(
            "the dashboard's backend host:port (required) - the DIRECT "
            "port, never an nginx front door and never a URL prefix "
            "(feeders always speak bare paths)"
        ),
    )
    parser.add_argument(
        "--build", default=None, metavar="NAME",
        help=(
            "file every record under build stream NAME instead of "
            "mainline - pass your framework's branch/release parameter "
            "here (there is no separate --branch)"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="check records and print what would be sent; POST nothing",
    )
    parser.add_argument(
        "--http-timeout", type=float, default=HTTP_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="per-HTTP-call socket timeout (default: %(default)s)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging plus full tracebacks on fatal errors",
    )
    return parser


# ----------------------------------------------------------------------
# One invocation, start to finish
# ----------------------------------------------------------------------


class _UsageError(Exception):
    """The invocation itself is wrong - exit code 2, nothing sent."""


class _Arguments(NamedTuple):
    """The engine-owned arguments, cleaned: stripped, never blank, and
    the URL already pointing at the import endpoint."""

    environment: str
    build: Optional[str]
    url: str


class _Gathered(NamedTuple):
    """Everything the site's reader produced, already screened."""

    read: int
    skipped: int
    records: List[Dict[str, Any]]


class _SendOutcome(NamedTuple):
    """What became of the batches."""

    sent: int
    inserted: int
    updated: int
    rejected: int
    failed_batches: int


def _argparse_exit_code(exc: SystemExit) -> int:
    """argparse exits rather than returns - code 0 for --help, code 2
    for usage errors. main() converts the exit back into a return
    value, so callers of main() always get an int."""
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 2


def _configured_logger(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger("testboard_feeder")


def _cleaned_args(args: argparse.Namespace) -> _Arguments:
    """argparse guarantees the required flags are present; this guards
    against the present-but-blank, and normalizes the URL."""
    environment = args.environment.strip()
    if not environment:
        raise _UsageError(
            "--environment must not be empty or whitespace-only"
        )
    build = None
    if args.build is not None:
        build = args.build.strip()
        if not build:
            raise _UsageError(
                "--build must not be empty or whitespace-only"
            )
    url = args.url.strip()
    if not url:
        raise _UsageError(
            "--url must not be empty or whitespace-only"
        )
    return _Arguments(
        environment=environment,
        build=build,
        url=_normalize_url(url),
    )


def _stamped(raw: Any, parameters: _Arguments) -> Any:
    """A copy of ``raw`` with the engine-owned fields applied - the
    command line is their single source of truth, whatever the reader
    set. Non-dicts pass through for _validate_record() to describe."""
    if not isinstance(raw, dict):
        return raw
    record = dict(raw)
    record["environment"] = parameters.environment
    if parameters.build is not None:
        record["build"] = parameters.build
    return record


def _screened_records(args: argparse.Namespace,
                      parameters: _Arguments,
                      log: logging.Logger) -> _Gathered:
    """Run the site's read_records(), stamp each record, and screen it
    through _validate_record(). A bad record is logged and skipped,
    never fatal; the reader itself crashing is fatal, because a
    half-read results source cannot be told apart from a complete
    one."""
    try:
        # iter() both accepts whatever iterable read_records() chose to
        # return and fails HERE, catchably, if it returned something
        # that is not iterable at all.
        raw_iterator = iter(read_records(args))
    except Exception as exc:
        if args.verbose:
            traceback.print_exc()
        raise _UsageError(
            "read_records() raised before producing anything: "
            f"{type(exc).__name__}: {exc}"
        )

    read = 0
    skipped = 0
    records: List[Dict[str, Any]] = []

    try:
        for raw in raw_iterator:
            read += 1
            record = _stamped(raw, parameters)
            problem = _validate_record(record)

            if problem is not None:
                skipped += 1
                log.warning("skipping record %d: %s", read, problem)
            else:
                records.append(record)

    except Exception as exc:
        if args.verbose:
            traceback.print_exc()
        raise _UsageError(
            f"read_records() crashed after producing {read} record(s): "
            f"{type(exc).__name__}: {exc}"
        )

    return _Gathered(read=read, skipped=skipped, records=records)


def _dry_run_report(gathered: _Gathered, log: logging.Logger) -> int:
    """Print the first records verbatim, count the rest, send nothing."""
    for index, record in enumerate(gathered.records[:3], 1):
        sys.stdout.write(f"\n--- record {index} would be sent as ---\n")
        sys.stdout.write(json.dumps(record, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    sys.stdout.flush()
    log.info(
        "dry run: read=%d valid=%d skipped=%d - nothing was sent",
        gathered.read, len(gathered.records), gathered.skipped,
    )
    return 0


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": (
            f"testboard-feeder-python-micro/{ENGINE_VERSION} "
            f"(contract {CONTRACT_VERSION})"
        ),
    }


def _send_all(records: List[Dict[str, Any]],
              url: str,
              http_timeout: float,
              log: logging.Logger) -> _SendOutcome:
    """Batch and send everything. Each batch gets its own retries, and
    one failed batch never stops the next - whatever the server did
    accept stays accepted."""

    headers = _headers()
    sent = 0
    inserted = 0
    updated = 0
    rejected = 0
    failed_batches = 0

    for batch in _batches(records, BATCH_SIZE, MAX_BATCH_BYTES):
        body = json.dumps({"runs": batch}).encode("utf-8")
        label = f"batch of {len(batch)} records"

        payload = _send_batch(url, body, headers, http_timeout, log, label)

        if payload is None:
            log.error(
                "%s was not accepted; nothing is saved locally - "
                "re-invoke this feeder to re-send it (safe: the server "
                "skips anything it already has)", label,
            )
            failed_batches += 1
        else:
            counts = _report_payload(payload, label, log)
            sent += len(batch)
            inserted += counts.inserted
            updated += counts.updated
            rejected += counts.rejected

    return _SendOutcome(sent=sent,
                        inserted=inserted,
                        updated=updated,
                        rejected=rejected,
                        failed_batches=failed_batches)


def _log_summary(gathered: _Gathered,
                 outcome: _SendOutcome,
                 log: logging.Logger) -> None:
    log.info(
        "feeder summary: read=%d valid=%d skipped=%d sent=%d inserted=%d "
        "updated=%d rejected=%d failed_batches=%d",
        gathered.read, len(gathered.records), gathered.skipped,
        outcome.sent, outcome.inserted, outcome.updated, outcome.rejected,
        outcome.failed_batches,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """One invocation, start to finish - parse, read, send. The exit
    codes are the contract in the module docstring."""
    parser = build_parser()
    add_site_arguments(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _argparse_exit_code(exc)

    log = _configured_logger(args.verbose)
    try:
        parameters = _cleaned_args(args)
        gathered = _screened_records(args, parameters, log)
    except _UsageError as error:
        log.error("%s", error)
        return 2

    if args.dry_run:
        return _dry_run_report(gathered, log)

    outcome = _send_all(gathered.records,
                        parameters.url,
                        args.http_timeout,
                        log)
    _log_summary(gathered, outcome, log)

    if outcome.failed_batches:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
