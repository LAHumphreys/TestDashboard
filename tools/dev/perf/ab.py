"""The cold, in-process, alternated A/B of two code trees.

    python tools/dev/perf/ab.py --base SHA --head SHA --db <scratch>/perf.db
    python tools/dev/perf/ab.py --base SHA --head-tree <worktree> --db ...
        [--pages NAME=URL ...] [--calls 24] [--methods m1,m2] [--json OUT]

Both trees' ``testboard`` packages are loaded into ONE process, each
against its own copy of ``--db``. For every request the two are called
alternately (A then B, then B then A), with every storage memo cleared
before every call, so a timing is what the page costs while results are
being pushed. Never over HTTP: on the development machine an unchanged
request varies two to three times between runs.

Rows: the handover's Known slow table (``KNOWN_SLOW`` below), then each
``--pages`` URL. ``{product}``, ``{environment}`` and ``{build}`` in a
URL are filled from the seed. Per row it prints both medians, the
delta, the delta's 95% band (the noise), the statements per cold call
on each side, and a verdict. ``--methods`` adds statements per call for
named ``Storage`` methods. README.md says what each column means.

A SHA is materialised with ``git archive`` into a scratch directory (no
worktree is registered); ``--base-tree``/``--head-tree`` take a checkout
as it stands, uncommitted edits included. Nothing is written to ``--db``
or inside a checkout; the scratch directory is removed unless --keep.
"""

import argparse
import datetime
import io
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import urllib.parse
from typing import (Callable, Dict, List, NamedTuple, Optional, Sequence,
                    Tuple)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

#: The handover's "Known slow, measured, not changed" rows, as the
#: requests that measure them. Name -> (URL, state). State "assign65" is
#: measured after 65 of the largest environment's mainline failures are
#: assigned (the Browse count's known cost); "plain" is the seed as built.
KNOWN_SLOW = [
    ("watch 3 cards (incl. full build)",
     "/api/watch?c=p%3A{product}&c=e%3A{environment}&c=s%3A{build}",
     "plain"),
    ("compare full build, one category",
     "/api/compare?stream={build}&category=new_failures&limit=100&offset=0",
     "plain"),
    ("open actions rows",
     "/api/dashboard?open=1&with_comment=1&with_streak=1&sort=environment"
     "&order=asc&limit=100&offset=0",
     "plain"),
    ("home headline",
     "/api/summary?parts=headline&assignee=amy",
     "plain"),
    ("browse page, no assignments",
     "/api/dashboard?sort=environment&order=asc&limit=250&offset=0",
     "plain"),
    ("browse page, 65 assignments",
     "/api/dashboard?sort=environment&order=asc&limit=250&offset=0",
     "assign65"),
]  # type: List[Tuple[str, str, str]]

#: Below this a significant delta is immaterial: the verdict stays "same".
FLOOR_MS = 1.0
FLOOR_FRACTION = 0.05


def ro_uri(path: str) -> str:
    """A read-only SQLite URI for *path*, percent-quoted so a ``?`` or
    ``#`` in it is part of the name rather than the query."""
    return "file:{}?mode=ro".format(urllib.parse.quote(
        os.path.abspath(path).replace("\\", "/"), safe="/:"))


class Tree(NamedTuple):
    """One code tree loaded into this process."""

    label: str
    path: str
    modules: Dict[str, types.ModuleType]


class Scope(NamedTuple):
    """What the seed's rows are filled with, derived from the data."""

    product: str
    environment: str
    build: int
    now: datetime.datetime


class Row(NamedTuple):
    """One measured request: samples in ms, statements per cold call."""

    name: str
    url: str
    status_a: int
    status_b: int
    a_ms: List[float]
    b_ms: List[float]
    statements_a: int
    statements_b: int


class Verdict(NamedTuple):
    """A row's summary: medians, delta, its 95% band, and the call."""

    median_a: float
    median_b: float
    delta: float
    low: float
    high: float
    call: str


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------

def is_checkout(folder: str) -> bool:
    """True for a testboard checkout root (or worktree root)."""
    return (os.path.isfile(os.path.join(folder, "run_server.py"))
            and os.path.isdir(os.path.join(folder, "testboard")))


def check_db(path: str) -> None:
    """Raise ValueError for a database the runner must not measure on.

    The repo-root ``testboard.db`` of any checkout is generated dev data
    at a quarter of production; a number taken on it is not the bar.
    """
    full = os.path.abspath(path)
    if (os.path.basename(full) == "testboard.db"
            and is_checkout(os.path.dirname(full))):
        raise ValueError(
            "refusing the repo-root testboard.db ({}): it is dev data at a "
            "quarter of production. Build the seed: tools/dev/perf/seed.py "
            "--out <scratch>/perf.db".format(full))
    if not os.path.isfile(full):
        raise ValueError("no database at {}".format(full))


# ----------------------------------------------------------------------
# Two trees in one process
# ----------------------------------------------------------------------

def _testboard_names() -> List[str]:
    return [name for name in sys.modules
            if name == "testboard" or name.startswith("testboard.")]


def load_tree(label: str, path: str) -> Tree:
    """Import *path*'s ``testboard`` under its own module objects.

    Whatever ``testboard`` this process had before is put back after, so
    loading a tree never changes what the caller imported.
    """
    saved = dict((name, sys.modules.pop(name)) for name in _testboard_names())
    sys.path.insert(0, path)
    try:
        __import__("testboard.api")
        __import__("testboard.storage")
        __import__("testboard.model")
        modules = dict((name, sys.modules[name])
                       for name in _testboard_names())
    finally:
        sys.path.remove(path)
        for name in _testboard_names():
            del sys.modules[name]
        sys.modules.update(saved)
    origin = os.path.abspath(getattr(modules["testboard"], "__file__", ""))
    if not origin.startswith(os.path.abspath(path)):
        raise ValueError("{} imported testboard from {}, not from {}".format(
            label, origin, path))
    return Tree(label=label, path=path, modules=modules)


class Activator(object):
    """Puts one tree's modules in ``sys.modules`` at a time.

    Only matters for imports a tree makes lazily, inside a function: its
    module-level references are bound already. Restores the caller's
    ``testboard`` on :meth:`restore`.
    """

    def __init__(self) -> None:
        self._saved = dict(
            (name, sys.modules[name]) for name in _testboard_names())
        self._active = None  # type: Optional[Tree]

    def use(self, tree: Tree) -> None:
        """Make *tree* the importable ``testboard``."""
        if self._active is tree:
            return
        for name in _testboard_names():
            del sys.modules[name]
        if self._active is not None and self._active.path in sys.path:
            sys.path.remove(self._active.path)
        sys.modules.update(tree.modules)
        sys.path.insert(0, tree.path)
        self._active = tree

    def restore(self) -> None:
        """Put back whatever ``testboard`` the process had before."""
        for name in _testboard_names():
            del sys.modules[name]
        if self._active is not None and self._active.path in sys.path:
            sys.path.remove(self._active.path)
        sys.modules.update(self._saved)
        self._active = None


def materialise(sha: str, into: str) -> str:
    """Extract *sha*'s ``testboard`` package into *into*; return the root.

    ``git archive`` reads the object store, so no worktree is registered
    and nothing in any checkout changes.
    """
    proc = subprocess.Popen(
        ["git", "-C", REPO_ROOT, "archive", "--format=tar", sha, "testboard"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data, err = proc.communicate()
    if proc.returncode != 0:
        raise ValueError("git archive {} failed: {}".format(
            sha, err.decode("utf-8", "replace").strip()))
    os.makedirs(into)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(into)
    return into


# ----------------------------------------------------------------------
# Measuring
# ----------------------------------------------------------------------

def clear_memos(store: object) -> None:
    """Drop every storage memo, so the next call is cold.

    The two invalidators are what a push calls; the sweep after them
    clears any memo dict a later tree adds under the ``_*_cache`` /
    ``_*_memo`` convention, so a new memo can never turn a cold number
    warm unnoticed. Refuses a tree that has neither invalidator: its
    numbers would be warm and look cold.
    """
    found = False
    for name in ("_invalidate_summary_cache", "_invalidate_trend_cache"):
        method = getattr(store, name, None)
        if method is not None:
            method()
            found = True
    if not found:
        raise ValueError("this tree's Storage has no memo invalidator; "
                         "update clear_memos() before trusting a number")
    for name, value in list(vars(store).items()):
        if name.endswith("_cache") and isinstance(value, dict):
            value.clear()
        elif name.endswith("_memo") and not isinstance(value, dict):
            setattr(store, name, None)
        elif name.endswith("_memo") and isinstance(value, dict):
            value.clear()


def make_request(tree: Tree, url: str) -> object:
    """An ``api.Request`` of *tree*'s own type for a GET of *url*."""
    parts = urllib.parse.urlsplit(url)
    request_type = getattr(tree.modules["testboard.api"], "Request")
    return request_type(
        method="GET", path=parts.path,
        query=urllib.parse.parse_qs(parts.query, keep_blank_values=True),
        body=b"")


def call_once(tree: Tree, store: object, url: str,
              now: datetime.datetime) -> Tuple[float, int]:
    """One cold call: memos cleared, then the handler timed. (ms, status)"""
    handle = getattr(tree.modules["testboard.api"], "handle_api")
    request = make_request(tree, url)
    clear_memos(store)
    started = time.perf_counter()
    response = handle(store, request, now=lambda: now)
    elapsed = (time.perf_counter() - started) * 1000.0
    return elapsed, int(response.status)


class _Tracer(object):
    """Counts statements, attributing each to the wrapped methods open."""

    def __init__(self) -> None:
        self.total = 0
        self.open = []  # type: List[str]
        self.calls = {}  # type: Dict[str, int]
        self.statements = {}  # type: Dict[str, int]

    def __call__(self, _statement: str) -> None:
        self.total += 1
        for name in set(self.open):
            self.statements[name] = self.statements.get(name, 0) + 1


def count_statements(tree: Tree, store: object, url: str,
                     now: datetime.datetime, methods: Sequence[str],
                     tracer: _Tracer) -> int:
    """Statements one cold call executes; per-method into *tracer*.

    SQLite only: the trace callback is on the calling thread's
    connection, the one every handler uses here.
    """
    conn = getattr(store, "_conn")()
    wrapped = {}  # type: Dict[str, Callable[..., object]]

    def wrap(name: str, original: Callable[..., object]
             ) -> Callable[..., object]:
        def counted(*args: object, **kwargs: object) -> object:
            tracer.calls[name] = tracer.calls.get(name, 0) + 1
            tracer.open.append(name)
            try:
                return original(*args, **kwargs)
            finally:
                tracer.open.pop()
        return counted

    for name in methods:
        original = getattr(store, name, None)
        if original is not None:
            wrapped[name] = original
            setattr(store, name, wrap(name, original))
    before = tracer.total
    clear_memos(store)
    conn.set_trace_callback(tracer)
    try:
        handle = getattr(tree.modules["testboard.api"], "handle_api")
        handle(store, make_request(tree, url), now=lambda: now)
    finally:
        conn.set_trace_callback(None)
        for name in wrapped:
            delattr(store, name)
    return tracer.total - before


def alternate(trees: Tuple[Tree, Tree], stores: Tuple[object, object],
              activator: Activator, name: str, url: str,
              now: datetime.datetime, calls: int,
              methods: Sequence[str], tracers: Tuple[_Tracer, _Tracer]
              ) -> Row:
    """Measure one row: one warm-up each, then *calls* alternated pairs."""
    samples = ([], [])  # type: Tuple[List[float], List[float]]
    statuses = [0, 0]
    for side in (0, 1):
        activator.use(trees[side])
        statuses[side] = call_once(trees[side], stores[side], url, now)[1]
    for index in range(calls):
        order = (0, 1) if index % 2 == 0 else (1, 0)
        for side in order:
            activator.use(trees[side])
            elapsed, status = call_once(trees[side], stores[side], url, now)
            samples[side].append(elapsed)
            statuses[side] = status
    counted = [0, 0]
    for side in (0, 1):
        activator.use(trees[side])
        counted[side] = count_statements(
            trees[side], stores[side], url, now, methods, tracers[side])
    return Row(name=name, url=url, status_a=statuses[0],
               status_b=statuses[1], a_ms=samples[0], b_ms=samples[1],
               statements_a=counted[0], statements_b=counted[1])


def band(a_ms: Sequence[float], b_ms: Sequence[float]
         ) -> Tuple[float, float, float]:
    """Median paired difference B-A and its distribution-free 95% band.

    Pairs are the calls made back to back, so a slow moment of the
    machine lands on both sides of one pair. The band is the order-
    statistic interval for the median of the differences (normal
    approximation to the binomial); with fewer than six pairs it is the
    whole range.
    """
    diffs = sorted(b - a for a, b in zip(a_ms, b_ms))
    count = len(diffs)
    middle = statistics.median(diffs)
    if count < 6:
        return middle, diffs[0], diffs[-1]
    k = int(math.floor((count - 1.96 * math.sqrt(count)) / 2.0))
    k = max(1, k)
    return middle, diffs[k - 1], diffs[count - k]


def judge(row: Row) -> Verdict:
    """The row's verdict: slower / faster only when beyond the band AND
    past the materiality floor (1 ms or 5% of A, whichever is larger)."""
    if row.status_a != 200 or row.status_b != 200:
        return Verdict(0.0, 0.0, 0.0, 0.0, 0.0, "status {}/{}".format(
            row.status_a, row.status_b))
    median_a = statistics.median(row.a_ms)
    median_b = statistics.median(row.b_ms)
    delta, low, high = band(row.a_ms, row.b_ms)
    floor = max(FLOOR_MS, FLOOR_FRACTION * median_a)
    if low > 0 and delta > floor:
        call = "SLOWER"
    elif high < 0 and -delta > floor:
        call = "faster"
    else:
        call = "same"
    if row.statements_b > row.statements_a:
        call += ", MORE SQL"
    return Verdict(median_a, median_b, delta, low, high, call)


# ----------------------------------------------------------------------
# The seed
# ----------------------------------------------------------------------

def derive_scope(path: str) -> Scope:
    """Product, environment, build and clock for the rows, from the data.

    The largest product's largest mainline environment, that product's
    largest build, and a clock one hour after the last run ended. Read
    from the copy, read-only; the same seed always gives the same scope.
    """
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        row = conn.execute(
            "SELECT ep.product, l.environment, COUNT(*) FROM latest_runs l "
            "JOIN environment_products ep ON ep.environment = l.environment "
            "WHERE l.stream_id = 1 GROUP BY ep.product, l.environment"
        ).fetchall()
        if not row:
            raise ValueError("no product-declared mainline environment in "
                             "{}; build it with seed.py".format(path))
        per_product = {}  # type: Dict[str, int]
        for product, _environment, count in row:
            per_product[product] = per_product.get(product, 0) + count
        product = sorted(per_product.items(), key=lambda p: (-p[1], p[0]))[0][0]
        environment = sorted(
            [r for r in row if r[0] == product],
            key=lambda r: (-r[2], r[1]))[0][1]
        build = conn.execute(
            "SELECT s.id FROM streams s JOIN latest_runs l "
            "ON l.stream_id = s.id WHERE s.product = ? AND s.id <> 1 "
            "GROUP BY s.id ORDER BY COUNT(*) DESC, s.id LIMIT 1",
            (product,)).fetchone()
        if build is None:
            raise ValueError("no build of {} in {}".format(product, path))
        last = conn.execute("SELECT MAX(end_time) FROM runs").fetchone()[0]
    finally:
        conn.close()
    ended = datetime.datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")
    return Scope(product=product, environment=environment,
                 build=int(build[0]),
                 now=ended.replace(minute=0, second=0)
                 + datetime.timedelta(hours=2))


def fill(url: str, scope: Scope) -> str:
    """Substitute the scope's names into a row URL, quoted."""
    return url.format(
        product=urllib.parse.quote(scope.product, safe=""),
        environment=urllib.parse.quote(scope.environment, safe=""),
        build=scope.build)


def assign_65(tree: Tree, store: object, copy: str, scope: Scope) -> int:
    """Assign 65 of the scope environment's mainline failures to amy,
    through *tree*'s own Storage, as the Browse row's measured state."""
    conn = sqlite3.connect(copy)
    try:
        triples = conn.execute(
            "SELECT environment, script, test_name FROM latest_runs "
            "WHERE stream_id = 1 AND environment = ? AND result = 'FAIL' "
            "ORDER BY script, test_name LIMIT 65",
            (scope.environment,)).fetchall()
    finally:
        conn.close()
    method = getattr(store, "bulk_set_assignee_for_triples", None)
    if method is None:
        return 0
    method("amy", "amy", scope.now,
           [(row[0], row[1], row[2], None) for row in triples])
    return len(triples)


def copy_db(source: str, target: str) -> None:
    """Copy a database and any WAL/SHM beside it."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(source + suffix):
            shutil.copyfile(source + suffix, target + suffix)


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------

class Result(NamedTuple):
    """Everything one A/B produced, for printing and for the tests."""

    labels: Tuple[str, str]
    db: str
    scope: Scope
    calls: int
    rows: List[Row]
    methods: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]]


def run(base_tree: str, head_tree: str, db: str, scratch: str,
        labels: Tuple[str, str], pages: Sequence[Tuple[str, str]] = (),
        calls: int = 24, methods: Sequence[str] = (),
        known: bool = True,
        log: Optional[Callable[[str], None]] = None) -> Result:
    """Measure every row on both trees; see the module docstring."""
    check_db(db)
    trees = (load_tree(labels[0], base_tree), load_tree(labels[1], head_tree))
    copies = (os.path.join(scratch, "a.db"), os.path.join(scratch, "b.db"))
    for copy in copies:
        copy_db(db, copy)
    scope = derive_scope(copies[0])
    activator = Activator()
    stores = []  # type: List[object]
    try:
        for side in (0, 1):
            activator.use(trees[side])
            storage_type = getattr(trees[side].modules["testboard.storage"],
                                   "Storage")
            try:
                stores.append(storage_type(copies[side]))
            except Exception as exc:
                raise ValueError(
                    "{} cannot open the seed ({}); build the seed with the "
                    "older tree".format(labels[side], exc))
        pair = (stores[0], stores[1])
        tracers = (_Tracer(), _Tracer())
        wanted = [(name, url, state) for name, url, state in KNOWN_SLOW
                  if known] + [(name, url, "plain") for name, url in pages]
        rows = []  # type: List[Row]
        for state in ("plain", "assign65"):
            todo = [w for w in wanted if w[2] == state]
            if not todo:
                continue
            if state == "assign65":
                for side in (0, 1):
                    activator.use(trees[side])
                    assign_65(trees[side], stores[side], copies[side], scope)
            for name, url, _state in todo:
                row = alternate(trees, pair, activator, name,
                                fill(url, scope), scope.now, calls,
                                methods, tracers)
                rows.append(row)
                if log is not None:
                    log("  measured {}".format(name))
        per_method = {}  # type: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]]
        for name in methods:
            per_method[name] = (
                (tracers[0].calls.get(name, 0),
                 tracers[0].statements.get(name, 0)),
                (tracers[1].calls.get(name, 0),
                 tracers[1].statements.get(name, 0)))
    finally:
        for side, store in enumerate(stores):
            activator.use(trees[side])
            getattr(store, "close")()
        activator.restore()
    return Result(labels=labels, db=os.path.abspath(db), scope=scope,
                  calls=calls, rows=rows, methods=per_method)


def report(result: Result) -> List[str]:
    """The table /perf-ab describes, as lines."""
    lines = []  # type: List[str]
    a, b = result.labels
    lines.append("A={}  B={}  db={}".format(a, b, result.db))
    lines.append(
        "cold (memos cleared before every call), in-process, alternated; "
        "{} calls per side; scope {} / {} / build {} at {}".format(
            result.calls, result.scope.product, result.scope.environment,
            result.scope.build, result.scope.now.isoformat()))
    lines.append("{:36s} {:>9s} {:>9s} {:>8s} {:>18s} {:>9s}  {}".format(
        "request", "A ms", "B ms", "B-A ms", "95% band of B-A", "stmts A/B",
        "verdict"))
    widest = 0.0
    for row in result.rows:
        verdict = judge(row)
        if verdict.call.startswith("status"):
            lines.append("{:36s} {:>9s} {:>9s} {:>8s} {:>18s} {:>9s}  {}"
                         .format(row.name, "-", "-", "-", "-", "-",
                                 verdict.call))
            continue
        widest = max(widest, (verdict.high - verdict.low) / 2.0)
        lines.append(
            "{:36s} {:9.1f} {:9.1f} {:+8.1f} {:>18s} {:>9s}  {}".format(
                row.name, verdict.median_a, verdict.median_b, verdict.delta,
                "[{:+.1f}, {:+.1f}]".format(verdict.low, verdict.high),
                "{}/{}".format(row.statements_a, row.statements_b),
                verdict.call))
    lines.append("noise: widest half-band on this run {:.1f} ms; a verdict "
                 "needs the band clear of zero and |B-A| over max(1 ms, 5% "
                 "of A)".format(widest))
    if result.methods:
        lines.append("{:36s} {:>16s} {:>16s}".format(
            "storage method (all rows, cold)", "A calls/stmts",
            "B calls/stmts"))
        for name in sorted(result.methods):
            (ca, sa), (cb, sb) = result.methods[name]
            flag = "  MORE SQL PER CALL" if (
                cb and ca and float(sb) / cb > float(sa) / ca) else ""
            lines.append("{:36s} {:>16s} {:>16s}{}".format(
                name, "{}/{}".format(ca, sa), "{}/{}".format(cb, sb), flag))
    return lines


def _parse_pages(values: Sequence[str]) -> List[Tuple[str, str]]:
    pages = []  # type: List[Tuple[str, str]]
    for value in values:
        if "=" in value.split("?")[0]:
            name, url = value.split("=", 1)
        else:
            name, url = value, value
        if not url.startswith("/api/"):
            raise ValueError("a page is an /api/ URL, got {}".format(url))
        pages.append((name, url))
    return pages


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--base", help="base commit (git archive)")
    side.add_argument("--base-tree", help="base checkout, as it stands")
    other = parser.add_mutually_exclusive_group(required=True)
    other.add_argument("--head", help="head commit (git archive)")
    other.add_argument("--head-tree", help="head checkout, as it stands")
    parser.add_argument("--db", required=True,
                        help="the seed (tools/dev/perf/seed.py); copied, "
                             "never written")
    parser.add_argument("--pages", nargs="*", default=[],
                        help="extra rows: NAME=/api/... or /api/...; "
                             "{product} {environment} {build} are filled")
    parser.add_argument("--calls", type=int, default=24,
                        help="alternated calls per side per row")
    parser.add_argument("--methods", default="",
                        help="comma-separated Storage methods to count "
                             "statements per call for")
    parser.add_argument("--no-known", action="store_true",
                        help="skip the Known slow rows (--pages only)")
    parser.add_argument("--json", help="also write raw samples here")
    parser.add_argument("--keep", action="store_true",
                        help="keep the scratch directory")
    args = parser.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="testboard-ab-")
    try:
        check_db(args.db)
        pages = _parse_pages(args.pages)
        trees = []  # type: List[Tuple[str, str]]
        for index, (sha, tree) in enumerate(((args.base, args.base_tree),
                                             (args.head, args.head_tree))):
            tag = "AB"[index]
            if sha:
                path = materialise(sha, os.path.join(scratch, "tree-" + tag))
                trees.append(("{}:{}".format(tag, sha), path))
            else:
                trees.append(("{}:{}".format(tag, tree), os.path.abspath(tree)))
        result = run(trees[0][1], trees[1][1], args.db, scratch,
                     (trees[0][0], trees[1][0]), pages, args.calls,
                     [m for m in args.methods.split(",") if m],
                     known=not args.no_known,
                     log=lambda text: print(text, file=sys.stderr))
    except ValueError as exc:
        print("ab.py: {}".format(exc), file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print("scratch kept: {}".format(scratch), file=sys.stderr)
    for line in report(result):
        print(line)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({
                "labels": list(result.labels), "db": result.db,
                "rows": [row._asdict() for row in result.rows],
            }, handle, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
