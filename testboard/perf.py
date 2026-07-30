"""Optional performance logging to disk, for reading back later.

The stalls this exists for are intermittent, and an intermittent stall is
exactly what a live `top` session never sees. So the server can be asked
to write one line per timed thing to a file, and
``tools/perf_report.py`` reads that file afterwards and reports the
distribution.

**Off unless asked for.** ``run_server.py --perf-log PATH`` turns it on.
Nothing is written and nothing is wrapped without it, so the cost on a
server nobody is investigating is zero.

WHAT IS TIMED, and why that unit
--------------------------------

Two kinds of record:

- ``storage`` — one per call to a public :class:`~testboard.storage.Storage`
  method, labelled with the method name.
- ``request`` — one per HTTP request, carrying the total, the **time it
  spent queued for a worker**, and how many connections were waiting
  behind it.

A storage method, not a SQL statement, is the unit. That is deliberate
and it is not just convenience: ``sqlite3``'s ``execute()`` steps a
statement once, so for a SELECT most of the cost lands in the following
``fetchall()`` or iteration. Timing statements would therefore
systematically under-report precisely the slow reads worth finding, while
timing the method captures the whole operation. It is also the unit this
project already reasons in — the decomposition in the upgrade log reads
``activity_buckets(14d) 682 ms``, not a SQL string.

The consequence to be honest about: a method issuing several statements
is one number, so ``upsert_runs`` answers "how long did the import hold a
worker" and not "which statement inside it was slow".

The queue-wait field is the one that identifies a stall as contention
rather than slowness. A request that took 4s of which 3.9s was queued is
not a slow query; it is a server with no free worker.

FORMAT
------

Newline-delimited JSON — one self-contained object per line, appended.
Chosen because it survives truncation (a half-written last line costs one
record, not the file), needs no schema migration when a field is added,
and can be read with ``json.loads`` per line by anything.

Keys are short because there are a lot of them. Long SQL-free labels are
interned: the first time a label is seen in a file, a ``{"k": "label"}``
definition line is written, and records afterwards reference its id.

Size is capped (:data:`DEFAULT_MAX_BYTES`); at the cap the file is rolled
to ``<path>.1`` and a new one started, so at most twice the cap is ever
on disk and leaving this on permanently cannot fill a partition.

Python 3.6 compatible; standard library only. No global mutable state: a
:class:`PerfLog` is created by the entry point and injected.
"""

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from testboard import model

__all__ = [
    "PerfLog",
    "instrument_storage",
    "route_label",
    "DEFAULT_MAX_BYTES",
]

#: Trailing path segments that name an ACTION rather than an identity, so
#: they are kept in a request's label. Everything else after the
#: collection is replaced by a placeholder.
#:
#: The label set has to be BOUNDED. Paths carry environment, script and
#: test names, so labelling requests by raw path would produce one label
#: per test — tens of thousands of them — and a report grouped by that
#: says nothing while the file it came from is mostly path strings.
_ROUTE_ACTIONS = frozenset([
    "active",
    "assignee",
    "comments",
    "executions",
    "expectation",
    "history",
    "retired",
])

_PLACEHOLDER = "*"

#: Roll the file over at this size. A record is ~90 bytes, so this holds
#: on the order of a million of them — days of dashboard traffic, or a
#: few large imports.
DEFAULT_MAX_BYTES = 128 * 1024 * 1024

#: Storage attributes that are not timed. ``close`` and ``vacuum`` are
#: not queries anyone is investigating, and timing ``close`` from the
#: worker-exit path would write to a log the process is finishing with.
_NOT_TIMED = frozenset([
    "close",
    "vacuum",
    "max_connections",
    "cache_bytes_per_connection",
])


def route_label(method: str, path: str) -> str:
    """A bounded label for one request, e.g. ``GET /api/tests/*/*/*/history``.

    Identity segments (environment, script, test name, run id) collapse to
    ``*``; the collection and any trailing action word survive, because
    those are what distinguish one query shape from another. Static paths
    are kept whole — there are about fifteen of them and knowing which
    file is slow to serve is the point.
    """
    segments = [seg for seg in path.split("/") if seg]
    if not segments or segments[0] != "api":
        return "{0} {1}".format(method, path if path else "/")
    kept = ["api"]
    for index, segment in enumerate(segments[1:], start=1):
        if index == 1 or segment in _ROUTE_ACTIONS:
            kept.append(segment)
        else:
            kept.append(_PLACEHOLDER)
    return "{0} /{1}".format(method, "/".join(kept))


class PerfLog:
    """Append timing records to a file, safely from many threads.

    One instance is shared by every worker. The only shared mutable
    state is behind :attr:`_lock`; callers accumulate nothing, so a
    record is written and forgotten.
    """

    def __init__(
        self,
        path: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Open (or create) the log at *path*, appending to it."""
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._handle = None  # type: Any
        self._written = 0
        self._labels = {}  # type: Dict[str, int]
        # Which label ids have been DEFINED in the current file. Cleared
        # on rollover, so the new file redefines the labels it uses and
        # stays readable on its own.
        self._defined = set()  # type: set
        self._open()

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """Open the log for appending, line buffered."""
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        # Line buffered: a stall is investigated from a file the stalled
        # process is still holding open, so records have to be there
        # before it exits. One write syscall per record, no fsync.
        self._handle = open(self._path, "a", buffering=1, encoding="utf-8")
        try:
            self._written = os.path.getsize(self._path)
        except OSError:                              # pragma: no cover
            self._written = 0
        self._defined = set()

    def _roll(self) -> None:
        """Move the full log aside and start a new one. Caller holds the lock."""
        self._handle.close()
        previous = self._path + ".1"
        try:
            if os.path.exists(previous):
                os.remove(previous)
            os.rename(self._path, previous)
        except OSError:                              # pragma: no cover
            # Losing the rollover is not worth losing the server for;
            # the new open() below appends to the existing file instead.
            pass
        self._open()

    def close(self) -> None:
        """Flush and close. Further records are dropped, not an error."""
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _label_id(self, label: str) -> int:
        """Intern *label*; caller holds the lock."""
        known = self._labels.get(label)
        if known is None:
            known = len(self._labels) + 1
            self._labels[label] = known
        if known not in self._defined:
            self._defined.add(known)
            self._write_line({"k": "label", "i": known, "n": label})
        return known

    def _write_line(self, record: Dict[str, Any]) -> None:
        """Write one JSON line; caller holds the lock."""
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        self._handle.write(line + "\n")
        self._written += len(line) + 1

    def record(self, kind: str, label: str, seconds: float,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """Append one timing record.

        *kind* is ``"storage"`` or ``"request"``, *label* the method name
        or request route, *seconds* the elapsed wall time. Failures are
        swallowed: a full disk must not turn a working dashboard into a
        broken one because someone left profiling on.
        """
        try:
            with self._lock:
                if self._handle is None:
                    return
                record = {
                    "k": kind,
                    "l": self._label_id(label),
                    # Milliseconds, rounded: the questions are "is this
                    # 5ms or 500ms" and micros only make the file bigger.
                    "ms": round(seconds * 1000.0, 3),
                    "t": model.format_iso(model.utcnow()),
                }
                if extra:
                    record.update(extra)
                self._write_line(record)
                if self._written >= self._max_bytes:
                    self._roll()
        except Exception:                            # pragma: no cover
            pass

    def time_call(self, kind: str, label: str, func: Callable[..., Any],
                  *args: Any, **kwargs: Any) -> Any:
        """Call *func*, record how long it took, and return its result.

        The timing is recorded whether or not the call raised — a query
        that fails slowly is a finding, and losing it would make the log
        quietly disagree with the request count.
        """
        started = time.time()
        try:
            return func(*args, **kwargs)
        finally:
            self.record(kind, label, time.time() - started)


def instrument_storage(storage: Any, perf: PerfLog) -> List[str]:
    """Wrap *storage*'s public methods to time them into *perf*.

    Applied per INSTANCE, by ``setattr`` on the object, rather than by
    patching the class: the instance is already injected into the
    handlers, so this adds no module-level state and an un-instrumented
    Storage in the same process (a test, a tool) is unaffected.

    Returns the method names wrapped, so a caller can log what it is
    measuring — and so a test can assert the list is not empty, which is
    the failure mode a reflective wrapper has (silently wrapping nothing
    after a rename).
    """
    wrapped = []  # type: List[str]
    for name in sorted(dir(type(storage))):
        if name.startswith("_") or name in _NOT_TIMED:
            continue
        attribute = getattr(type(storage), name, None)
        if isinstance(attribute, property) or not callable(attribute):
            continue
        setattr(storage, name, _timed(perf, name, getattr(storage, name)))
        wrapped.append(name)
    return wrapped


def _timed(perf: PerfLog, label: str,
           method: Callable[..., Any]) -> Callable[..., Any]:
    """Return *method* wrapped to time itself into *perf*.

    ``Any`` here and in :func:`instrument_storage` is the one place this
    module cannot avoid it: the signatures being wrapped are every
    signature in Storage. Nothing is inspected or converted — the
    arguments are passed straight through — so the annotation is honest
    about what the wrapper knows rather than claiming more.
    """

    def timed(*args: Any, **kwargs: Any) -> Any:
        started = time.time()
        try:
            return method(*args, **kwargs)
        finally:
            perf.record("storage", label, time.time() - started)

    timed.__name__ = label
    timed.__doc__ = getattr(method, "__doc__", None)
    return timed
