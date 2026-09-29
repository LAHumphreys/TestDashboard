"""Counters kept in memory, for the Metrics page.

What the server has been doing since it started — which requests, how
long, how much of that was waiting for a worker, which storage methods
the time went into — readable from a web page rather than from a file
on the server.

This is NOT the performance log (:mod:`testboard.perf`), and does not
replace it. The log keeps every record, on disk, with its timestamp, so
a stall at 03:14 can be read back at 09:00; it is off unless asked for.
This keeps totals, in memory, on by default, and forgets them when the
process stops. The log answers "what happened then"; this answers "what
is it like".

WHAT IT MAY COST
----------------

The requirement it was built to: it must not cost response time.

- Nothing here does I/O, and nothing here runs a query. The database
  sizes on the Metrics page are read when that page asks for them, and
  at no other time (see ``Storage.size_report``).
- A timed storage call adds two clock reads and a few dictionary
  updates; a request adds the same again. Measured on the development
  machine, 400,000 calls of each: 1.3 millionths of a second a storage
  call, 1.3 a request. A cold home-page summary makes about sixteen
  storage calls and takes 35 thousandths.
- **No lock is taken to record anything.** Every worker thread tallies
  into its own dictionaries; only a reader merges them, and only a
  reader, a new thread's first call, or a request slower than the
  twentieth-slowest so far ever takes the lock. A counter every worker
  had to queue for would be a new way for requests to wait on each
  other, introduced by the thing meant to find those.

The price of that is exactness under concurrency: a reader merges
dictionaries other threads are still writing to, so a figure can be one
call behind. These are gauges for a person to look at, not accounts.

THE UNIT
--------

A storage METHOD, not a SQL statement — the decision
:mod:`testboard.perf` records and explains: ``sqlite3``'s ``execute()``
steps a statement once and most of a read's cost lands in the
``fetchall()`` after it, so timing statements under-reports exactly the
slow reads worth finding. A method's figure is INCLUSIVE: it contains
the time of any other storage method it called, so the column does not
sum to the time spent in storage. The per-request figure does — it
counts only the outermost call.

Python 3.6 compatible; standard library only. No global mutable state:
a :class:`Metrics` is created by the entry point and injected.
"""

import datetime
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from testboard import model
from testboard import perf as perf_module

__all__ = ["Metrics", "instrument_storage", "BUCKET_EDGES_MS"]

#: Upper edges of the request-duration histogram, in milliseconds. A
#: request slower than the last edge lands in one further bucket. Fixed
#: and few: a percentile is then a walk over thirteen integers, and a
#: route costs thirteen integers to remember.
BUCKET_EDGES_MS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)

#: How many of the slowest requests are remembered, with their targets.
_SLOWEST_KEPT = 20

#: A request target is kept for the slowest-requests list. It carries
#: test names and search text, so it is cut rather than kept whole.
_TARGET_KEPT = 300

#: Storage attributes that are not timed — the performance log's own
#: list, for its own reasons, plus the two reports this module's page
#: reads: timing the page's own questions would put the act of looking
#: at the top of what is being looked at.
_NOT_TIMED = frozenset([
    "close",
    "vacuum",
    "max_connections",
    "cache_bytes_per_connection",
    "size_report",
    "memo_report",
])

# Indexes into a request tally (a list, not an object: this is touched
# on every request and attribute access on an instance costs more than
# an index into a list).
_COUNT = 0
_SECONDS = 1
_SLOWEST = 2
_ERRORS = 3
_QUEUED = 4
_QUEUE_SECONDS = 5
_CALLS = 6
_STORAGE_SECONDS = 7
_BUCKETS = 8


class _ThreadState(object):
    """One worker thread's own tallies. Written by that thread only."""

    __slots__ = (
        "generation", "storage", "requests", "depth", "calls", "seconds",
    )

    def __init__(self, generation: int) -> None:
        self.generation = generation
        #: method name -> [calls, seconds, slowest]
        self.storage = {}  # type: Dict[str, List[float]]
        #: route label -> a request tally (see the _COUNT... indexes)
        self.requests = {}  # type: Dict[str, List[Any]]
        #: How many timed storage calls are on this thread's stack.
        self.depth = 0
        #: Outermost storage calls, and the seconds they took, since
        #: this thread started: what a request's own share is the
        #: difference of.
        self.calls = 0
        self.seconds = 0.0

    def clear(self, generation: int) -> None:
        self.generation = generation
        self.storage = {}
        self.requests = {}
        self.calls = 0
        self.seconds = 0.0


class Metrics:
    """What this process has served, tallied per thread and merged to read.

    One instance is shared by every worker. See the module docstring for
    what it may and may not cost.
    """

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        timer: Optional[Callable[[], float]] = None,
    ) -> None:
        """*clock* is wall time (for "since"); *timer* measures."""
        self._clock = clock or time.time
        self.timer = timer or time.perf_counter
        self._lock = threading.Lock()
        self._local = threading.local()
        self._states = []  # type: List[_ThreadState]
        self._generation = 0
        self._since = self._clock()
        self._slowest = []  # type: List[Dict[str, Any]]
        #: A request has to be slower than this to be worth the lock.
        self._slow_floor = 0.0

    # ------------------------------------------------------------------
    # Recording. No lock on these paths but the two named below.
    # ------------------------------------------------------------------

    def state(self) -> _ThreadState:
        """This thread's tallies, cleared if a reset has happened since."""
        found = getattr(self._local, "state", None)
        if found is None:
            found = _ThreadState(self._generation)
            self._local.state = found
            with self._lock:    # once per thread
                self._states.append(found)
        elif found.generation != self._generation:
            found.clear(self._generation)
        return found

    def record_storage(
        self, state: _ThreadState, label: str, seconds: float,
    ) -> None:
        """Tally one finished storage call on the calling thread.

        *state* is what :meth:`state` returned before the call began;
        ``state.depth`` has already been stepped back down, so zero
        means this call was the outermost.
        """
        tally = state.storage.get(label)
        if tally is None:
            state.storage[label] = [1, seconds, seconds]
        else:
            tally[0] += 1
            tally[1] += seconds
            if seconds > tally[2]:
                tally[2] = seconds
        if state.depth == 0:
            state.calls += 1
            state.seconds += seconds

    def mark(self) -> Tuple[int, float]:
        """Where this thread's storage totals stand, taken as a request
        begins; :meth:`record_request` takes the difference."""
        state = self.state()
        return state.calls, state.seconds

    def record_request(
        self,
        route: str,
        seconds: float,
        status: Optional[int],
        queue_seconds: Optional[float],
        mark: Optional[Tuple[int, float]],
        target: str,
    ) -> None:
        """Tally one finished request on the calling thread.

        *queue_seconds* is how long the CONNECTION waited for a worker,
        given for the first request served on it and ``None`` for the
        rest (see ``server._record_request`` for why). *mark* is what
        :meth:`mark` returned as the request began.
        """
        state = self.state()
        calls = 0
        storage_seconds = 0.0
        if mark is not None:
            calls = max(0, state.calls - mark[0])
            storage_seconds = max(0.0, state.seconds - mark[1])
        tally = state.requests.get(route)
        if tally is None:
            tally = [0, 0.0, 0.0, 0, 0, 0.0, 0, 0.0,
                     [0] * (len(BUCKET_EDGES_MS) + 1)]
            state.requests[route] = tally
        tally[_COUNT] += 1
        tally[_SECONDS] += seconds
        if seconds > tally[_SLOWEST]:
            tally[_SLOWEST] = seconds
        if status is not None and status >= 500:
            tally[_ERRORS] += 1
        if queue_seconds is not None:
            tally[_QUEUED] += 1
            tally[_QUEUE_SECONDS] += queue_seconds
        tally[_CALLS] += calls
        tally[_STORAGE_SECONDS] += storage_seconds
        milliseconds = seconds * 1000.0
        index = 0
        for edge in BUCKET_EDGES_MS:
            if milliseconds <= edge:
                break
            index += 1
        tally[_BUCKETS][index] += 1
        if seconds > self._slow_floor:
            self._remember_slow({
                "at": self._clock(),
                "route": route,
                "target": target[:_TARGET_KEPT],
                "status": status,
                "seconds": seconds,
                "queue_seconds": queue_seconds,
                "storage_calls": calls,
                "storage_seconds": storage_seconds,
                "generation": state.generation,
            })

    def _remember_slow(self, entry: Dict[str, Any]) -> None:
        """Keep *entry* if it is among the slowest. Takes the lock —
        reached only by a request slower than the slowest-kept floor."""
        with self._lock:
            if entry["generation"] != self._generation:
                return
            self._slowest.append(entry)
            self._slowest.sort(key=lambda item: -item["seconds"])
            del self._slowest[_SLOWEST_KEPT:]
            if len(self._slowest) >= _SLOWEST_KEPT:
                self._slow_floor = self._slowest[-1]["seconds"]

    def reset(self) -> None:
        """Start counting again from nothing.

        Other threads' tallies are not touched from here — they are
        theirs to write. The generation moves on, and each thread
        discards its own the next time it records anything; a reader
        ignores any that have not caught up.
        """
        with self._lock:
            self._generation += 1
            self._since = self._clock()
            self._slowest = []
            self._slow_floor = 0.0

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @staticmethod
    def _copied(tallies: Dict[str, List[Any]]) -> List[Tuple[str, List[Any]]]:
        """A thread's tallies, copied while that thread may be writing.

        A dictionary that gains a key mid-iteration raises; that is
        rare (a label's first call) and harmless to retry.
        """
        for _attempt in range(5):
            try:
                return [
                    (label, list(tally)) for label, tally in tallies.items()
                ]
            except RuntimeError:
                continue
        return []

    @staticmethod
    def _percentile(buckets: List[int], fraction: float) -> Optional[float]:
        """The upper edge, in ms, of the bucket holding that fraction of
        the requests — ``None`` when it is the open-ended last one."""
        total = sum(buckets)
        if total == 0:
            return None
        wanted = total * fraction
        seen = 0
        for index, count in enumerate(buckets):
            seen += count
            if seen >= wanted:
                if index < len(BUCKET_EDGES_MS):
                    return float(BUCKET_EDGES_MS[index])
                return None
        return None

    def _iso(self, when: float) -> str:
        return model.format_iso(datetime.datetime.utcfromtimestamp(when))

    def snapshot(self) -> Dict[str, Any]:
        """Everything counted since the start (or the last reset), as
        JSON-ready values. Takes the lock briefly, to copy two lists."""
        with self._lock:
            generation = self._generation
            since = self._since
            states = list(self._states)
            slowest = [dict(entry) for entry in self._slowest]
        requests = {}  # type: Dict[str, List[Any]]
        storage = {}  # type: Dict[str, List[float]]
        for state in states:
            if state.generation != generation:
                continue
            for label, tally in self._copied(state.requests):
                merged = requests.get(label)
                if merged is None:
                    requests[label] = [
                        tally[_COUNT], tally[_SECONDS], tally[_SLOWEST],
                        tally[_ERRORS], tally[_QUEUED],
                        tally[_QUEUE_SECONDS], tally[_CALLS],
                        tally[_STORAGE_SECONDS], list(tally[_BUCKETS]),
                    ]
                    continue
                for index in (_COUNT, _SECONDS, _ERRORS, _QUEUED,
                              _QUEUE_SECONDS, _CALLS, _STORAGE_SECONDS):
                    merged[index] += tally[index]
                merged[_SLOWEST] = max(merged[_SLOWEST], tally[_SLOWEST])
                merged[_BUCKETS] = [
                    mine + theirs for mine, theirs
                    in zip(merged[_BUCKETS], tally[_BUCKETS])
                ]
            for label, call in self._copied(state.storage):
                known = storage.get(label)
                if known is None:
                    storage[label] = list(call)
                    continue
                known[0] += call[0]
                known[1] += call[1]
                known[2] = max(known[2], call[2])

        def ms(seconds: float) -> float:
            return round(seconds * 1000.0, 2)

        request_rows = []  # type: List[Dict[str, Any]]
        for label in sorted(requests, key=lambda l: -requests[l][_SECONDS]):
            tally = requests[label]
            count = tally[_COUNT] or 1
            request_rows.append({
                "route": label,
                "count": tally[_COUNT],
                "errors": tally[_ERRORS],
                "total_ms": ms(tally[_SECONDS]),
                "mean_ms": ms(tally[_SECONDS] / count),
                "p95_ms": self._percentile(tally[_BUCKETS], 0.95),
                "max_ms": ms(tally[_SLOWEST]),
                # Waits are per CONNECTION, so the mean is over the
                # requests that carried one, not over all of them.
                "queue_mean_ms": (
                    None if not tally[_QUEUED]
                    else ms(tally[_QUEUE_SECONDS] / tally[_QUEUED])),
                "storage_calls_mean": round(tally[_CALLS] / count, 1),
                "storage_mean_ms": ms(tally[_STORAGE_SECONDS] / count),
                "buckets": tally[_BUCKETS],
            })
        storage_rows = [
            {
                "method": label,
                "calls": int(storage[label][0]),
                "total_ms": ms(storage[label][1]),
                "mean_ms": ms(storage[label][1] / (storage[label][0] or 1)),
                "max_ms": ms(storage[label][2]),
            }
            for label in sorted(storage, key=lambda l: -storage[l][1])
        ]
        now = self._clock()
        return {
            "collecting": True,
            "since": self._iso(since),
            "seconds": round(max(0.0, now - since), 1),
            "bucket_edges_ms": list(BUCKET_EDGES_MS),
            "totals": {
                "requests": sum(row["count"] for row in request_rows),
                "errors": sum(row["errors"] for row in request_rows),
                "request_ms": round(
                    sum(row["total_ms"] for row in request_rows), 2),
            },
            "requests": request_rows,
            "storage": storage_rows,
            "slowest": [
                {
                    "at": self._iso(entry["at"]),
                    "route": entry["route"],
                    "target": entry["target"],
                    "status": entry["status"],
                    "ms": ms(entry["seconds"]),
                    "queue_ms": (
                        None if entry["queue_seconds"] is None
                        else ms(entry["queue_seconds"])),
                    "storage_calls": entry["storage_calls"],
                    "storage_ms": ms(entry["storage_seconds"]),
                }
                for entry in slowest
            ],
        }


def route_label(method: str, path: str) -> str:
    """A bounded label for one request.

    The performance log's own (:func:`testboard.perf.route_label`), so a
    route reads the same in both places — with one difference: every
    static file is one label here. The log keeps them apart because
    which file was slow is a finding; here, a page of counters has no
    use for fifteen rows that are each a file read.
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[0] != "api":
        return "{0} (static files)".format(method)
    return perf_module.route_label(method, path)


def instrument_storage(storage: Any, metrics: Metrics) -> List[str]:
    """Wrap *storage*'s public methods to tally themselves into *metrics*.

    Per INSTANCE, by ``setattr`` on the object, exactly as
    :func:`testboard.perf.instrument_storage` does and for its reasons.
    Apply this BEFORE that one when both are wanted: the log then times
    the tallied call, and its record includes these few microseconds
    rather than this tally including a write to disk.

    Returns the names wrapped, so the caller can say what is measured
    and a test can assert the list is not empty.
    """
    wrapped = []  # type: List[str]
    for name in sorted(dir(type(storage))):
        if name.startswith("_") or name in _NOT_TIMED:
            continue
        attribute = getattr(type(storage), name, None)
        if isinstance(attribute, property) or not callable(attribute):
            continue
        setattr(storage, name, _tallied(metrics, name, getattr(storage, name)))
        wrapped.append(name)
    return wrapped


def _tallied(metrics: Metrics, label: str,
             method: Callable[..., Any]) -> Callable[..., Any]:
    """Return *method* wrapped to tally itself into *metrics*.

    ``Any`` for the reason :func:`testboard.perf._timed` gives: the
    signatures wrapped are every signature in Storage, and the
    arguments are passed straight through.
    """
    timer = metrics.timer

    def tallied(*args: Any, **kwargs: Any) -> Any:
        state = metrics.state()
        state.depth += 1
        started = timer()
        try:
            return method(*args, **kwargs)
        finally:
            elapsed = timer() - started
            state.depth -= 1
            metrics.record_storage(state, label, elapsed)

    tallied.__name__ = label
    tallied.__doc__ = getattr(method, "__doc__", None)
    return tallied
