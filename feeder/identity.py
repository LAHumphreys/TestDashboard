"""Describing a raw record — in a log line, or in full.

Every part of the feeder that reports a problem — the reader wrapper, the
offline check, the submitter, the server's own rejections — has to answer
the same question: *which* record, and what did it look like? They must
answer it the same way, or a problem found by ``--check-reader`` reads
differently from the same problem found during an import, and the two
cannot be matched up.

Both helpers here are deliberately total: they never raise, whatever they
are handed. They are called from failure paths, and a diagnostic that
fails while explaining a failure is worse than no diagnostic.

Python 3.6 compatible; standard library only.
"""

import json
from typing import Any, List, TextIO

from testboard import model

#: Enough of a record to recognize it; more than this buries the message.
MAX_LOGGED_CHARS = 200


def truncate(text: str, limit: int = MAX_LOGGED_CHARS) -> str:
    """Shorten ``text`` for logging, marking that it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def identity_of(raw: Any) -> str:
    """Best-effort ``environment / script / test_name [@ start_time]``.

    Works on raw (possibly invalid) record dicts and on the server's error
    objects; missing or unusable fields become ``?`` so the line is always
    greppable, and a record that is not a dict at all says what it is
    instead.
    """
    if not isinstance(raw, dict):
        return "<no identity: record is {0}>".format(type(raw).__name__)
    parts = []  # type: List[str]
    for field in ("environment", "script", "test_name"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        else:
            parts.append("?")
    identity = " / ".join(parts)
    start = raw.get("start_time")
    if isinstance(start, str) and start.strip():
        identity += " @ " + start
    return identity


def describe(raw: Any, limit: int = MAX_LOGGED_CHARS) -> str:
    """Return a truncated repr of ``raw``, never raising.

    A record's own ``__repr__`` is the reader's code, which is exactly the
    code under suspicion when this is called.
    """
    try:
        return truncate(repr(raw), limit)
    except Exception as exc:  # pragma: no cover - a __repr__ that throws
        return "<unprintable {0}: repr() raised {1}>".format(
            type(raw).__name__, type(exc).__name__)


def _as_json(value: Any) -> str:
    """Pretty-print ``value`` as JSON, falling back to repr."""
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=repr)
    except Exception:  # pragma: no cover - defensive
        return describe(value, limit=4000)


def show_record(index: int, raw: Any, out: TextIO) -> None:
    """Print one record as yielded *and* as it would be transmitted.

    The pair is the point. A reader is usually wrong in one of two ways
    that a single view hides: it emits a field the transport does not
    have (visible only in the raw dict), or it emits something the
    transport silently normalizes — a missing optional filled with its
    default, a value coerced. Showing what went in beside what would go
    out makes both obvious without anyone having to know the schema.

    Written straight to ``out`` rather than through ``logging``, which
    would stamp a timestamp on every line of a JSON block and make it
    useless to copy.
    """
    out.write("\n--- record {0} -- as your reader yielded it ---\n".format(
        index))
    out.write(_as_json(raw) + "\n")
    try:
        record = model.parse_run_record(raw)
    except model.ValidationError as exc:
        out.write("--- would be REJECTED: {0}\n".format(exc))
        out.flush()
        return
    sent = model.run_record_to_dict(record)
    if sent == raw:
        out.write("--- transmitted unchanged\n")
    else:
        out.write("--- as it would be sent to /api/import ---\n")
        out.write(_as_json(sent) + "\n")
        added = sorted(set(sent) - set(raw if isinstance(raw, dict) else {}))
        dropped = sorted(
            set(raw if isinstance(raw, dict) else {}) - set(sent))
        if added:
            out.write("--- filled in with defaults: {0}\n".format(
                ", ".join(added)))
        if dropped:
            out.write(
                "--- IGNORED (not part of the transport schema): {0}\n".format(
                    ", ".join(dropped)))
    out.flush()
