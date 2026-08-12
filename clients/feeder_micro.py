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
     failing, or (with --build) it was too old to understand build
     streams. Nothing is saved locally; re-invoking this feeder
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
wall-clock time budget and full client-side wire validation for sites
that want them. The IMPLEMENT THIS contract is shared: a section
written for the full engine drops in here unchanged - a conformance
test in the testboard repository transplants it on every push -
though its ``DASHBOARD_URL`` constant goes unused, because this
engine always takes ``--url``.
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
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

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

#: The four result values the dashboard understands, and the fields
#: every record must carry as non-empty strings.
_RESULT_VALUES = ("PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS")
_REQUIRED_STRING_FIELDS = (
    "environment", "script", "test_name", "start_time", "end_time",
)


def sanity_error(raw: Any) -> Optional[str]:
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
        return f"result: unknown value {raw.get('result')!r} (expected one of {expected})"
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


def _post(
    url: str, body: bytes, headers: Dict[str, str], timeout: float,
) -> Tuple[int, bytes]:
    """POST once. HTTP error statuses are returned like any other
    response; only transport failures (refused, timed out) raise."""
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


def _describe_connection_error(url: str, exc: BaseException) -> str:
    """One readable sentence for a failed connection, naming the two
    commonest causes (timeout, DNS) directly instead of via a nested
    exception repr."""
    reason: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and not isinstance(
        exc, urllib.error.HTTPError
    ):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, BaseException):
            reason = inner
    if isinstance(reason, socket.timeout):
        return f"request to {url} timed out"
    if isinstance(reason, socket.gaierror):
        return f"DNS lookup failed for the host in {url}"
    return f"cannot reach {url} ({type(reason).__name__}: {reason})"


def _send_batch(
    url: str,
    body: bytes,
    headers: Dict[str, str],
    http_timeout: float,
    sleep: Callable[[float], None],
    log: logging.Logger,
    label: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """POST ``body``, up to MAX_ATTEMPTS times; return (ok, payload).

    ``payload`` is the decoded 200 response object, or None when the
    response body was not usable JSON. Connection errors and HTTP 5xx
    get another try after a flat pause; a 4xx means the request itself
    was rejected, so it is final on first sight.
    """
    reason = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            log.info(
                "%s: retrying in %.0fs (attempt %d of %d)",
                label, RETRY_PAUSE_SECONDS, attempt, MAX_ATTEMPTS,
            )
            sleep(RETRY_PAUSE_SECONDS)
        try:
            status, response_body = _post(url, body, headers, http_timeout)
        except Exception as exc:
            reason = _describe_connection_error(url, exc)
            log.warning(
                "%s: attempt %d of %d failed: %s",
                label, attempt, MAX_ATTEMPTS, reason,
            )
            continue
        if status == 200:
            try:
                payload = json.loads(response_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            return (True, payload if isinstance(payload, dict) else None)
        text = response_body.decode("utf-8", errors="replace")
        if len(text) > 200:
            text = text[:200] + "...[truncated]"
        reason = f"HTTP {status} from the server (response: {text})"
        if status >= 500:
            log.warning(
                "%s: attempt %d of %d failed: %s",
                label, attempt, MAX_ATTEMPTS, reason,
            )
            continue
        log.warning("%s: %s - not retrying a client error", label, reason)
        break
    log.error("%s: giving up (%s)", label, reason)
    return (False, None)


def _report_payload(
    payload: Optional[Dict[str, Any]], label: str, log: logging.Logger,
) -> Tuple[int, int, int]:
    """Log what the server said about an accepted batch and return
    (inserted, updated, rejected). The first few per-record rejections
    are logged in full - enough to see what was wrong from the log
    alone - and the rest are summarised as a count.
    """
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


def _batches(
    records: List[Dict[str, Any]], batch_size: int, max_bytes: int,
) -> Iterator[List[Dict[str, Any]]]:
    """Group records into lists of at most ``batch_size``, flushing a
    batch early when its estimated encoded size reaches ``max_bytes``."""
    batch: List[Dict[str, Any]] = []
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
            "here (there is no separate --branch). The server must "
            "acknowledge builds; one too old to do so is a loud exit "
            "1, never a silent misfile into mainline"
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


def main(argv: Optional[List[str]] = None) -> int:
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

    raw_url = args.url.strip()
    if not raw_url:
        log.error("--url must not be empty or whitespace-only")
        return 2
    url = _normalize_url(raw_url)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            f"testboard-feeder-python-micro/{ENGINE_VERSION} "
            f"(contract {CONTRACT_VERSION})"
        ),
    }

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
    records: List[Dict[str, Any]] = []
    try:
        for raw in raw_iterator:
            read += 1
            if isinstance(raw, dict):
                # The engine owns these two fields: the command line is
                # their single source of truth, whatever the reader set.
                raw = dict(raw)
                raw["environment"] = environment
                if build is not None:
                    raw["build"] = build
            reason = sanity_error(raw)
            if reason is not None:
                skipped += 1
                log.warning("skipping record %d: %s", read, reason)
                continue
            valid += 1
            records.append(raw)
    except Exception as exc:
        log.error(
            "read_records() crashed after producing %d record(s): %s: "
            "%s", read, type(exc).__name__, exc,
        )
        if args.verbose:
            traceback.print_exc()
        return 2

    if args.dry_run:
        for index, record in enumerate(records[:3], 1):
            sys.stdout.write(f"\n--- record {index} would be sent as ---\n")
            sys.stdout.write(json.dumps(record, indent=2, sort_keys=True))
            sys.stdout.write("\n")
        sys.stdout.flush()
        log.info(
            "dry run: read=%d valid=%d skipped=%d - nothing was sent",
            read, valid, skipped,
        )
        return 0

    sent = inserted = updated = rejected = failed_batches = 0
    for batch in _batches(records, BATCH_SIZE, MAX_BATCH_BYTES):
        body = json.dumps({"runs": batch}).encode("utf-8")
        label = f"batch of {len(batch)} records"
        ok, payload = _send_batch(
            url, body, headers, args.http_timeout, time.sleep, log, label,
        )
        if not ok:
            log.error(
                "%s was not accepted; nothing is saved locally - "
                "re-invoke this feeder to re-send it (safe: the server "
                "skips anything it already has)", label,
            )
            failed_batches += 1
            continue
        if build is not None and (
            payload is None or "streams_seen" not in payload
        ):
            # A server that understands builds always echoes a
            # streams_seen list. Silence means it is too old for
            # builds and has filed these records into mainline.
            log.error(
                "this run used --build %r but the server did not "
                "acknowledge it (no streams_seen in the response): the "
                "server is too old for build streams and has filed "
                "these records into mainline. Update the dashboard and "
                "re-invoke this feeder, or drop --build to import as "
                "mainline deliberately.", build,
            )
            failed_batches += 1
            continue
        counts = _report_payload(payload, label, log)
        sent += len(batch)
        inserted += counts[0]
        updated += counts[1]
        rejected += counts[2]

    log.info(
        "feeder summary: read=%d valid=%d skipped=%d sent=%d inserted=%d "
        "updated=%d rejected=%d failed_batches=%d",
        read, valid, skipped, sent, inserted, updated, rejected,
        failed_batches,
    )

    if failed_batches:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
