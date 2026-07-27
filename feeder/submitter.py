"""Generic batching HTTP submitter for the testboard ``/api/import`` endpoint.

Responsibilities:

- validate every raw record via :func:`testboard.model.parse_run_record`
  (invalid records are logged at WARNING with their identity and skipped —
  one bad record never aborts an import),
- optionally drop records older than a ``since`` lower bound,
- batch valid records and POST each batch as ``{"runs": [...]}``,
- retry failed batches with exponential backoff (injectable ``sleep`` for
  tests), never retrying client errors (HTTP 400),
- write permanently failed batches to replay files
  (``testboard_failed_batch_NNNN.json``) so no data is ever silently lost,
- report everything in a :class:`SubmitStats` plus a reason-grouped summary
  in the logs so a busy engineer can fix a broken reader from the log alone.

Python 3.6 compatible; standard library only.
"""

import datetime
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import (
    Any, Callable, Dict, Iterable, List, NamedTuple, Optional, TextIO,
    Tuple,
)

from feeder.identity import (
    MAX_LOGGED_CHARS, describe, identity_of, show_record, truncate,
)
from testboard import model

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS", "Opener", "SubmitStats", "Submitter",
    "describe_connection_error", "group_reason", "identity_of",
    "IMPORT_PATH", "normalize_url", "render_reasons",
    "urllib_opener",
]

logger = logging.getLogger(__name__)

#: HTTP transport hook: ``(url, body, headers) -> (status, response_body)``.
#: The default implementation uses urllib.request; tests inject fakes.
Opener = Callable[[str, bytes, Dict[str, str]], Tuple[int, bytes]]

DEFAULT_TIMEOUT_SECONDS = 60.0

_REPLAY_FILE_TEMPLATE = "testboard_failed_batch_{0:04d}.json"
IMPORT_PATH = "/api/import"

#: How often a long-running import reports progress (seconds).
_PROGRESS_INTERVAL_SECONDS = 30.0

#: Per-record warnings logged for each DISTINCT problem before falling
#: silent. One systematic mistake in a reader affects every record it
#: touches: over a year of history that is millions of near-identical
#: multi-line warnings, which buries the summary that actually explains
#: the problem and can fill the disk with log. The first few examples are
#: what a human needs; the exact total is in the summary either way.
_MAX_LOGGED_PER_REASON = 5


def _log_reason_suppressed(prefix: str) -> None:
    """Announce that further occurrences of one problem will be silent."""
    logger.warning(
        "[%s] has now affected more than %d records - further "
        "occurrences will be counted but not logged individually. The "
        "total and an example are in the summary at the end of this run.",
        prefix, _MAX_LOGGED_PER_REASON,
    )


def _format_duration(seconds: float) -> str:
    """Render an elapsed time as ``1h 02m 03s`` for progress lines."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "{0}h {1:02d}m {2:02d}s".format(hours, minutes, secs)
    if minutes:
        return "{0}m {1:02d}s".format(minutes, secs)
    return "{0}s".format(secs)


class SubmitStats(NamedTuple):
    """Counters for one :meth:`Submitter.submit` call.

    - ``read``: records pulled from the input iterable.
    - ``valid``: records that passed validation AND the ``since`` filter.
    - ``skipped``: records that failed validation (logged per record).
    - ``sent``: records in batches acknowledged by the server with HTTP 200
      (always 0 in dry-run mode).
    - ``inserted``/``updated``/``rejected``: totals accumulated from the
      server's ``/api/import`` responses.
    - ``failed_batches``: batches that exhausted retries (or hit a
      non-retryable client error).
    - ``replay_files``: paths of the replay files written for those batches.
    """

    read: int
    valid: int
    skipped: int
    sent: int
    inserted: int
    updated: int
    rejected: int
    failed_batches: int
    replay_files: List[str]


class _BatchResult(NamedTuple):
    """Outcome of one batch POST (internal)."""

    ok: bool
    inserted: int
    updated: int
    rejected: int
    replay_path: Optional[str]


def urllib_opener(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
    """Default :data:`Opener`: POST via urllib.request with a 60s timeout.

    HTTP error statuses (4xx/5xx) are returned as ``(status, body)`` rather
    than raised; transport-level failures (refused, timeout, DNS) propagate
    as exceptions for the retry logic to handle.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    try:
        return response.getcode(), response.read()
    finally:
        response.close()


def describe_connection_error(url: str, exc: BaseException) -> str:
    """Turn a transport exception into one actionable, remedy-bearing line.

    Connection-refused, timeout and DNS failures each get their own message
    (per the project's dev-experience rules); anything else gets a generic
    but still actionable fallback.
    """
    reason = exc  # type: BaseException
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, BaseException):
            reason = inner
    host = urllib.parse.urlsplit(url).hostname or url
    if isinstance(reason, socket.gaierror):
        return (
            "Cannot reach the dashboard at {0} (DNS lookup failed for host "
            "'{1}'). Check the hostname in --url for typos; verify with: "
            "nslookup {1}".format(url, host)
        )
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return (
            "Cannot reach the dashboard at {0} (request timed out). The host "
            "did not answer - check network/firewall/VPN connectivity to "
            "'{1}'; verify with: ping {1}".format(url, host)
        )
    if isinstance(reason, ConnectionRefusedError):
        return (
            "Cannot reach the dashboard at {0} (connection refused). Is the "
            "server running on that host? Start it with: "
            "python3 run_server.py".format(url)
        )
    return (
        "Cannot reach the dashboard at {0} ({1}: {2}). Check the --url value "
        "and that the server is running: python3 run_server.py".format(
            url, type(reason).__name__, reason
        )
    )


def normalize_url(url: str) -> str:
    """Accept a dashboard base URL or a full /api/import URL; return the latter."""
    trimmed = url.rstrip("/")
    if not trimmed.endswith(IMPORT_PATH):
        trimmed += IMPORT_PATH
    return trimmed


def _truncate_bytes(data: bytes, limit: int = MAX_LOGGED_CHARS) -> str:
    """Decode response bytes leniently and truncate for logging."""
    return truncate(data.decode("utf-8", errors="replace"), limit)


def _reason_prefix(message: str) -> str:
    """Group key for a validation/rejection message.

    Cuts the message at the first quoted value or parenthesis so that
    per-record variants ("unknown value 'A'", "unknown value 'B'") collapse
    into one group ("result: unknown value").
    """
    cut = len(message)
    for marker in ("'", "("):
        index = message.find(marker)
        if index != -1 and index < cut:
            cut = index
    return message[:cut].rstrip()[:80]


def group_reason(
    reasons: "OrderedDict[str, List[Any]]", message: str, identity: str,
    example: Optional[Any] = None,
) -> Tuple[str, int]:
    """Count ``message`` under its group prefix, keeping the first example.

    Returns ``(prefix, count_so_far)`` so callers can log the first few
    occurrences of each distinct problem and stay quiet after that.

    ``example`` is the offending record itself. The identity alone is not
    always enough to act on: a record that is not a dict, or one whose
    identity fields are the very thing that is wrong, identifies as
    ``? / ? / ?`` and leaves nothing to look at. One truncated copy per
    distinct problem is cheap and is usually the whole diagnosis.
    """
    prefix = _reason_prefix(message)
    entry = reasons.get(prefix)
    if entry is None:
        reasons[prefix] = [
            1, identity, describe(example) if example is not None else None]
        return prefix, 1
    entry[0] += 1
    return prefix, entry[0]


def render_reasons(reasons: "OrderedDict[str, List[Any]]") -> List[str]:
    """Render grouped reasons as ``N x [reason] first: identity`` lines.

    Shared with :mod:`feeder.check` so that a reader checked offline and
    an import that rejected records report their problems identically.
    """
    lines = []  # type: List[str]
    for prefix, entry in reasons.items():
        line = "{0} x [{1}] first: {2}".format(entry[0], prefix, entry[1])
        if len(entry) > 2 and entry[2]:
            line += "\n      offending record: {0}".format(entry[2])
        lines.append(line)
    return lines


class Submitter:
    """Validates, batches and POSTs run records to a testboard dashboard."""

    def __init__(self, url: str, batch_size: int = 500, max_retries: int = 3,
                 backoff_seconds: float = 2.0, opener: Optional[Opener] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 replay_dir: str = ".",
                 max_consecutive_failures: int = 3,
                 clock: Callable[[], float] = time.time) -> None:
        """Configure the submitter.

        ``url`` may be the dashboard base (e.g. ``http://host:8000``) or the
        full ``/api/import`` URL; the path is appended when missing.
        ``opener``, ``sleep`` and ``clock`` are injectable for tests; the
        default opener uses urllib.request with a 60 second timeout.
        ``max_retries`` is the TOTAL number of attempts per batch; between
        attempt N and N+1 the submitter sleeps
        ``backoff_seconds * 2**(N-1)`` seconds (N is 1-based), i.e.
        exponential backoff.

        ``max_consecutive_failures`` stops the whole import once that many
        batches fail back to back. Without it, a server that goes away
        during a large backfill produces one failed batch — and one
        replay file — for every remaining batch, which at a year of
        history means thousands of files and hours of retry backoff for
        an import that cannot succeed.
        """
        self._url = normalize_url(url)
        self._batch_size = max(1, int(batch_size))
        self._max_retries = max(1, int(max_retries))
        self._backoff_seconds = float(backoff_seconds)
        self._opener = opener if opener is not None else urllib_opener
        self._sleep = sleep
        self._replay_dir = replay_dir
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._clock = clock
        self._max_accepted = None  # type: Optional[datetime.datetime]

    def submit(self, records: Iterable[Dict[str, Any]],
               dry_run: bool = False,
               since: Optional[datetime.datetime] = None,
               show: int = 0,
               out: Optional[TextIO] = None) -> SubmitStats:
        """Validate, filter, batch and send ``records``; return the counters.

        Per record: :func:`testboard.model.parse_run_record`; invalid records
        are logged at WARNING (reason + identity + truncated repr) and
        counted as ``skipped``. When ``since`` is given, valid records with
        ``start_time < since`` are dropped silently (counted in ``read`` but
        neither ``valid`` nor ``skipped``). In dry-run mode no HTTP request
        is made — records are only validated and counted.

        Ends by logging a summary line with every counter plus a breakdown of
        skip/reject reasons grouped by message prefix, each with an example
        record identity.

        ``show`` prints the first N records in full to ``out`` (stdout by
        default), as yielded and as they would be transmitted, so a
        ``--dry-run`` can answer "what would this actually send?" and not
        only "how many".
        """
        read = 0
        valid = 0
        skipped = 0
        reasons = OrderedDict()  # type: OrderedDict[str, List[Any]]
        batch = []  # type: List[model.RunRecord]
        batch_number = 0
        outcomes = []  # type: List[Tuple[int, _BatchResult]]
        consecutive_failures = 0
        aborted = False
        started = self._clock()
        last_progress = started

        stream = out if out is not None else sys.stdout
        for raw in records:
            read += 1
            if read <= show:
                show_record(read, raw, stream)
            try:
                record = model.parse_run_record(raw)
            except model.ValidationError as exc:
                skipped += 1
                identity = identity_of(raw)
                prefix, count = group_reason(
                    reasons, str(exc), identity, raw)
                if count <= _MAX_LOGGED_PER_REASON:
                    logger.warning(
                        "skipping invalid record [%s] %s | record: %s",
                        identity, exc, describe(raw),
                    )
                elif count == _MAX_LOGGED_PER_REASON + 1:
                    _log_reason_suppressed(prefix)
                continue
            if since is not None and record.start_time < since:
                continue
            valid += 1
            if dry_run:
                continue
            batch.append(record)
            if len(batch) >= self._batch_size:
                batch_number += 1
                result = self._send_batch(batch_number, batch, reasons)
                outcomes.append((len(batch), result))
                batch = []
                consecutive_failures = (
                    0 if result.ok else consecutive_failures + 1
                )
                if consecutive_failures >= self._max_consecutive_failures:
                    self._log_abort(consecutive_failures, read)
                    aborted = True
                    break
            now = self._clock()
            if now - last_progress >= _PROGRESS_INTERVAL_SECONDS:
                last_progress = now
                self._log_progress(read, valid, skipped, now - started)

        if batch and not aborted:
            batch_number += 1
            outcomes.append(
                (len(batch), self._send_batch(batch_number, batch, reasons))
            )

        sent = 0
        inserted = 0
        updated = 0
        rejected = 0
        failed_batches = 0
        replay_files = []  # type: List[str]
        for size, result in outcomes:
            if result.ok:
                sent += size
                inserted += result.inserted
                updated += result.updated
                rejected += result.rejected
            else:
                failed_batches += 1
                if result.replay_path is not None:
                    replay_files.append(result.replay_path)

        stats = SubmitStats(
            read=read, valid=valid, skipped=skipped, sent=sent,
            inserted=inserted, updated=updated, rejected=rejected,
            failed_batches=failed_batches, replay_files=replay_files,
        )
        logger.info(
            "feeder summary%s: read=%d valid=%d skipped=%d sent=%d "
            "inserted=%d updated=%d rejected=%d failed_batches=%d "
            "replay_files=%d",
            " (dry run: nothing was sent)" if dry_run else "",
            stats.read, stats.valid, stats.skipped, stats.sent,
            stats.inserted, stats.updated, stats.rejected,
            stats.failed_batches, len(stats.replay_files),
        )
        if reasons:
            logger.info("skip/reject reasons (count x [reason] first-affected record):")
            for line in render_reasons(reasons):
                logger.info("  %s", line)
        return stats

    def _log_progress(
        self, read: int, valid: int, skipped: int, elapsed: float
    ) -> None:
        """Emit a periodic progress line during a long import.

        A year of history is millions of records and tens of minutes. A
        run that prints nothing for that long is indistinguishable from a
        hung one, and gets killed by an operator who then has no idea how
        far it got.
        """
        rate = read / elapsed if elapsed > 0 else 0.0
        logger.info(
            "progress: %d records read (%d valid, %d skipped) in %s, "
            "%.0f records/s",
            read, valid, skipped, _format_duration(elapsed), rate,
        )

    def _log_abort(self, consecutive: int, read: int) -> None:
        """Explain why the import stopped early, and what to do next."""
        logger.error(
            "ABORTING after %d batches failed in a row (%d records read "
            "so far). The remaining records were NOT sent and NOT saved "
            "to replay files - continuing would have written one replay "
            "file per batch for the rest of the import. Fix the problem "
            "reported above (usually: the server is down, unreachable, "
            "or out of disk), then re-run the same command - importing "
            "is safe to repeat, the server upserts. Raise "
            "--max-consecutive-failures if you really want it to keep "
            "going.",
            consecutive, read,
        )

    def max_accepted_start_time(self) -> Optional[datetime.datetime]:
        """Max ``start_time`` across records in batches that got HTTP 200.

        This is the value the CLI persists as the daily-mode high-water mark.
        ``None`` when no batch has been accepted yet (or in dry-run mode).
        """
        return self._max_accepted

    def _send_batch(self, batch_number: int, records: List[model.RunRecord],
                    reasons: "OrderedDict[str, List[Any]]") -> _BatchResult:
        """POST one batch with retry/backoff; write a replay file on failure."""
        body = json.dumps(
            {"runs": [model.run_record_to_dict(record) for record in records]}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        failure_reason = "unknown error"
        for attempt in range(1, self._max_retries + 1):
            if attempt > 1:
                delay = self._backoff_seconds * (2 ** (attempt - 2))
                logger.info(
                    "batch %d: retrying in %.1fs (attempt %d of %d)",
                    batch_number, delay, attempt, self._max_retries,
                )
                self._sleep(delay)
            try:
                status, response_body = self._opener(self._url, body, headers)
            except Exception as exc:
                failure_reason = describe_connection_error(self._url, exc)
                logger.warning(
                    "batch %d: attempt %d of %d failed: %s",
                    batch_number, attempt, self._max_retries, failure_reason,
                )
                continue
            if status == 200:
                return self._handle_success(
                    batch_number, records, response_body, reasons
                )
            if status >= 500:
                failure_reason = "server error HTTP {0} (response: {1})".format(
                    status, _truncate_bytes(response_body)
                )
                logger.warning(
                    "batch %d: attempt %d of %d failed: %s",
                    batch_number, attempt, self._max_retries, failure_reason,
                )
                continue
            if status == 413:
                # The batch itself is too big for the server to accept —
                # a handful of tests dumping megabytes of output is
                # enough. Retrying is pointless; sending fewer records
                # per request is the fix.
                failure_reason = (
                    "HTTP 413: this batch ({0} records, {1:.1f} MB) is "
                    "larger than the server accepts. Some of these tests "
                    "have very large output. Re-run with a smaller "
                    "batch, e.g. --batch-size {2}".format(
                        len(records), len(body) / (1024.0 * 1024.0),
                        max(1, len(records) // 10),
                    )
                )
            else:
                failure_reason = (
                    "HTTP {0} from the server (response: {1}). Client errors "
                    "are not retried - the request itself was rejected, "
                    "retrying cannot help".format(
                        status, _truncate_bytes(response_body))
                )
            logger.warning("batch %d: %s", batch_number, failure_reason)
            break
        return self._record_failure(batch_number, body, failure_reason)

    def _handle_success(self, batch_number: int, records: List[model.RunRecord],
                        response_body: bytes,
                        reasons: "OrderedDict[str, List[Any]]") -> _BatchResult:
        """Accumulate a 200 response: counts, per-record server errors, HWM."""
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning(
                "batch %d: server returned 200 but the response body was not "
                "valid JSON: %s", batch_number, _truncate_bytes(response_body),
            )
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        inserted = int(payload.get("inserted", 0) or 0)
        updated = int(payload.get("updated", 0) or 0)
        rejected = int(payload.get("rejected", 0) or 0)
        errors = payload.get("errors", [])
        if isinstance(errors, list):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                message = str(error.get("error", "unknown server-side error"))
                identity = identity_of(error)
                prefix, count = group_reason(reasons, message, identity)
                if count <= _MAX_LOGGED_PER_REASON:
                    logger.warning(
                        "batch %d: server rejected record index %s [%s]: %s",
                        batch_number, error.get("index"), identity, message,
                    )
                elif count == _MAX_LOGGED_PER_REASON + 1:
                    _log_reason_suppressed(prefix)
        batch_max = max(record.start_time for record in records)
        if self._max_accepted is None or batch_max > self._max_accepted:
            self._max_accepted = batch_max
        logger.info(
            "batch %d: sent %d records -> inserted=%d updated=%d rejected=%d",
            batch_number, len(records), inserted, updated, rejected,
        )
        return _BatchResult(True, inserted, updated, rejected, None)

    def _record_failure(self, batch_number: int, body: bytes,
                        failure_reason: str) -> _BatchResult:
        """Write the failed batch to a replay file and report the failure."""
        file_name = _REPLAY_FILE_TEMPLATE.format(batch_number)
        path = os.path.join(self._replay_dir, file_name)
        try:
            with open(path, "wb") as handle:
                handle.write(body)
        except OSError as exc:
            logger.error(
                "batch %d permanently failed (%s) AND the replay file %s "
                "could not be written (%s). This batch's data was NOT "
                "imported and NOT saved - re-run the feeder for this range "
                "once both problems are fixed (safe to repeat, the server "
                "upserts)", batch_number, failure_reason, path, exc,
            )
            return _BatchResult(False, 0, 0, 0, None)
        logger.error(
            "batch %d permanently failed: %s | the batch was saved to %s - "
            "once the problem is fixed, re-send it with: curl -X POST -H "
            "\"Content-Type: application/json\" --data-binary @%s %s "
            "(safe to repeat, the server upserts)",
            batch_number, failure_reason, path, path, self._url,
        )
        return _BatchResult(False, 0, 0, 0, path)
