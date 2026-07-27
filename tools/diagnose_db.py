#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Find out why the dashboard is slow, instead of guessing.

"Several seconds on some screens" has two completely different causes
with opposite fixes:

- **the storage is slow** — the database lives on a network mount, and
  every page SQLite cannot find in its own cache costs a round trip;
- **a query is slow** — something reads the whole of ``runs`` (millions
  of rows) where it should read ``latest_runs`` (one row per test).

Guessing between them is expensive, and the guess is usually the storage
because that is the thing everyone already distrusts. This tool
discriminates, in four widening steps:

1. Report what the database *actually is*: real PRAGMA values (not the
   ones the code asks for), size, page count, row counts.
2. Time every query the dashboard screens run, and print the plan for
   each. A plan containing ``SCAN runs`` is a bug, wherever the file
   lives.
3. Measure how fast the file itself can be read, cold, which
   characterizes the mount.
4. With ``--compare-local``, copy the database somewhere local and run
   step 2 again against it. If local is fast and the original is slow,
   it is the storage — definitively, with a number. If both are slow, it
   is the query, and no amount of moving the file will help.

Nothing here writes to the database. Step 4 copies it.

Usage::

    python3 tools/diagnose_db.py --db /mnt/share/testboard.db
    python3 tools/diagnose_db.py --db /mnt/share/testboard.db --compare-local

Python 3.6 compatible; standard library only.
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from testboard import model  # noqa: E402
from testboard.storage import Storage  # noqa: E402

#: PRAGMAs worth knowing the real value of. The code asks for some of
#: these at connect time; asking for one is not the same as getting it —
#: ``journal_mode=WAL`` in particular returns the mode you ended up with
#: rather than failing, and WAL cannot work on most network filesystems
#: because it needs a shared-memory file.
_PRAGMAS = (
    ("journal_mode", "WAL, or 'delete' if the mount would not allow it"),
    ("cache_size", "negative = KiB, positive = pages. PER CONNECTION"),
    ("mmap_size", "bytes mapped instead of read; 0 = off"),
    ("page_size", "bytes"),
    ("page_count", "pages in the database"),
    ("synchronous", "0=off 1=normal 2=full; fsyncs on write"),
    ("temp_store", "0=default 1=file 2=memory"),
    ("busy_timeout", "ms to wait on a locked database"),
)

#: How much of the file to read when characterizing the storage, and the
#: block size to read it in. Big enough to be a real measurement, small
#: enough not to take a coffee break on a bad mount.
_SAMPLE_BYTES = 32 * 1024 * 1024
_BLOCK_BYTES = 1024 * 1024

#: A query slower than this is worth a second look; slower than the next
#: one is why someone opened this tool.
_SLOW_SECONDS = 0.25
_VERY_SLOW_SECONDS = 1.0

#: Below this, a timing is dominated by measurement noise rather than by
#: anything the storage did, so comparing two of them says nothing.
_NOISE_FLOOR_SECONDS = 0.005


def human_bytes(count: float) -> str:
    """Render a byte count the way a person reads it."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024.0 or unit == "TB":
            return "{0:.1f} {1}".format(count, unit)
        count /= 1024.0
    return "{0:.1f} TB".format(count)


def timed(work: Callable[[], Any]) -> Tuple[float, Any, Optional[str]]:
    """Run ``work``; return (seconds, result, error message or None)."""
    started = time.time()
    try:
        result = work()
    except Exception as exc:
        return time.time() - started, None, "{0}: {1}".format(
            type(exc).__name__, exc)
    return time.time() - started, result, None


# ----------------------------------------------------------------------
# Step 1: what the database actually is
# ----------------------------------------------------------------------


def describe_database(path: str, out: Any) -> Dict[str, Any]:
    """Print the real PRAGMA values and size; return them as a dict."""
    heading(out, "1. What this database actually is")
    out.write("  (a fresh connection's settings - what the server gets "
              "unless it was\n   started with --cache-mb / --mmap-mb)\n\n")
    facts = {}  # type: Dict[str, Any]
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        out.write("  cannot stat {0}: {1}\n".format(path, exc))
        return facts
    facts["size"] = size
    out.write("  file          {0}\n".format(os.path.abspath(path)))
    out.write("  size          {0}\n".format(human_bytes(size)))
    for sidecar in ("-wal", "-shm"):
        side = path + sidecar
        if os.path.exists(side):
            out.write("  {0:<13} {1}\n".format(
                sidecar[1:], human_bytes(os.path.getsize(side))))

    # A bare connection, so the values reported are SQLite's own defaults
    # plus whatever the file itself carries - not what Storage asks for.
    conn = sqlite3.connect(path)
    try:
        for name, note in _PRAGMAS:
            try:
                row = conn.execute("PRAGMA " + name).fetchone()
            except sqlite3.Error as exc:
                out.write("  {0:<13} <error: {1}>\n".format(name, exc))
                continue
            value = row[0] if row else None
            facts[name] = value
            out.write("  {0:<13} {1:<12} ({2})\n".format(name, value, note))
        _describe_cache(facts, out)
        _describe_journal(facts, out)
        heading(out, "   row counts")
        for table in ("runs", "run_outputs", "latest_runs", "comments",
                      "assignments", "current_assignments"):
            seconds, row, error = timed(
                lambda t=table: conn.execute(
                    "SELECT COUNT(*) FROM " + t).fetchone())
            if error:
                out.write("  {0:<20} <{1}>\n".format(table, error))
            else:
                out.write("  {0:<20} {1:>12,}   ({2:.2f}s to count)\n".format(
                    table, row[0], seconds))
    finally:
        conn.close()
    return facts


def _describe_cache(facts: Dict[str, Any], out: Any) -> None:
    """Say plainly how much cache there is against how much database."""
    cache = facts.get("cache_size")
    page_size = facts.get("page_size") or 4096
    size = facts.get("size") or 0
    if cache is None:
        return
    cache_bytes = -cache * 1024 if cache < 0 else cache * page_size
    out.write("\n  => page cache is {0} PER CONNECTION, against a {1} "
              "database.\n".format(human_bytes(cache_bytes),
                                   human_bytes(size)))
    if size and cache_bytes < size * 0.1:
        out.write(
            "     That is under a tenth of it, so most reads miss the cache\n"
            "     and go to the filesystem. On local disk the OS page cache\n"
            "     absorbs that; on a network mount it is a round trip each\n"
            "     time. See --help of run_server.py for --cache-mb.\n")


def _describe_journal(facts: Dict[str, Any], out: Any) -> None:
    """Flag a journal mode that is not what the code asked for."""
    mode = str(facts.get("journal_mode", "")).lower()
    if mode == "wal":
        return
    out.write(
        "\n  => journal_mode is '{0}', NOT 'wal', even though the server\n"
        "     asks for WAL at connect time. PRAGMA journal_mode does not\n"
        "     fail when it cannot switch - it returns what you got. WAL\n"
        "     needs a shared-memory file, which most network filesystems\n"
        "     do not support, so this is the expected result on a network\n"
        "     mount. It costs read concurrency: readers and writers block\n"
        "     each other, which shows up as pauses under load.\n".format(mode))


# ----------------------------------------------------------------------
# Step 2: time the queries the screens actually run
# ----------------------------------------------------------------------


def dashboard_queries(
    storage: Storage, environment: Optional[str], script: Optional[str]
) -> Sequence[Tuple[str, Callable[[], Any]]]:
    """The storage calls behind each dashboard screen, named by screen."""
    now = model.utcnow()
    recent = now - datetime.timedelta(hours=36)
    since = now - datetime.timedelta(days=90)
    return (
        ("home: summary rollup",
         lambda: storage.summary_rollup(recent, environment)),
        ("home: 90-day trend",
         lambda: storage.daily_result_counts(since, environment)),
        ("home: failing scripts",
         lambda: storage.top_failing_scripts(environment, 10)),
        ("home: new-failure queue",
         lambda: storage.status_queue("new_failures", environment, 50)),
        ("home: still-failing queue",
         lambda: storage.status_queue("still_failing", environment, 50)),
        ("home: not-run queue",
         lambda: storage.status_queue(
             "not_run", environment, 50, None, recent)),
        ("triage: first page",
         lambda: storage.dashboard(
             environment=environment, script=script, limit=250, offset=0)),
        ("triage: total for paging",
         lambda: storage.dashboard_count(
             environment=environment, script=script)),
        ("triage: page 10 (offset 2250)",
         lambda: storage.dashboard(
             environment=environment, script=script,
             limit=250, offset=2250)),
        ("triage: sorted by start_time",
         lambda: storage.dashboard(
             environment=environment, script=script, limit=250, offset=0,
             sort="start_time", descending=True)),
        ("triage: name search",
         lambda: storage.dashboard(
             environment=environment, limit=250, offset=0, q="retry")),
        ("triage: page with comments",
         lambda: storage.dashboard(
             environment=environment, limit=250, offset=0,
             with_latest_comment=True)),
        ("filters: environments",
         lambda: storage.environments()),
        ("filters: scripts",
         lambda: storage.scripts(environment)),
        ("filters: assignees",
         lambda: storage.assignees()),
    )


def time_queries(
    storage: Storage, out: Any, environment: Optional[str],
    script: Optional[str], repeat: int, title: Optional[str] = None,
) -> List[Tuple[str, float]]:
    """Time every dashboard query; return [(name, best seconds)]."""
    heading(out, title or "2. How long each screen's queries take")
    out.write("  Best of {0} run(s). The first run of each is cold, so a "
              "much\n  larger first number is itself a finding: it means "
              "the cache is\n  doing the work, and an idle server loses "
              "it.\n\n".format(repeat))
    results = []  # type: List[Tuple[str, float]]
    for name, work in dashboard_queries(storage, environment, script):
        best = None  # type: Optional[float]
        first = None  # type: Optional[float]
        error = None  # type: Optional[str]
        for attempt in range(repeat):
            seconds, _, error = timed(work)
            if error:
                break
            if first is None:
                first = seconds
            best = seconds if best is None else min(best, seconds)
        if error or best is None:
            out.write("  {0:<34} FAILED  {1}\n".format(name, error))
            continue
        flag = ""
        if best >= _VERY_SLOW_SECONDS:
            flag = "  <== SLOW"
        elif best >= _SLOW_SECONDS:
            flag = "  <-- worth a look"
        cold = ""
        if first is not None and repeat > 1 and first > best * 3:
            cold = "   (first run {0:.2f}s - cold)".format(first)
        out.write("  {0:<34} {1:7.3f}s{2}{3}\n".format(
            name, best, flag, cold))
        results.append((name, best))
    return results


def explain_slow(
    storage: Storage, out: Any, results: List[Tuple[str, float]]
) -> None:
    """Print query plans for the dashboard's two heaviest reads.

    A plan is the difference between "the storage is slow" and "this
    query is wrong": ``SCAN runs`` over millions of rows is a bug on any
    filesystem, and no amount of cache or local disk will fix it.
    """
    heading(out, "3. Query plans for the two big estate-wide reads")
    plans = (
        ("triage page", "SELECT * FROM latest_runs ORDER BY environment "
                        "LIMIT 250"),
        ("run lookup", "SELECT * FROM runs WHERE environment = ? AND "
                       "script = ? AND test_name = ? "
                       "ORDER BY start_time DESC LIMIT 200"),
    )
    conn = storage._conn()  # noqa: SLF001 - a diagnostic, by definition
    scanned_big = False
    for name, sql in plans:
        out.write("  {0}:\n".format(name))
        try:
            rows = conn.execute(
                "EXPLAIN QUERY PLAN " + sql, ("e", "s", "t")[:sql.count("?")]
            ).fetchall()
        except sqlite3.Error as exc:
            out.write("    <error: {0}>\n".format(exc))
            continue
        for row in rows:
            detail = row[-1]
            marker = ""
            # A SCAN is only bad over a table that grows with the
            # estate. latest_runs holds one row per test and is meant to
            # be scanned - that is the whole reason it exists.
            if detail.startswith("SCAN") and _scans_a_big_table(detail):
                marker = "  <== SCANS A BIG TABLE"
                scanned_big = True
            out.write("    {0}{1}\n".format(detail, marker))
    if scanned_big:
        out.write(
            "\n  => A SCAN over 'runs' or 'run_outputs' reads every row in a\n"
            "     table that grows with the whole history. That is slow\n"
            "     wherever the file lives: moving the database will not help,\n"
            "     the query has to change.\n")
    else:
        out.write(
            "\n  => No estate-sized scans. Reads go through 'latest_runs',\n"
            "     which holds one row per test rather than one per run, and\n"
            "     is meant to be scanned. So the query shapes are right, and\n"
            "     any slowness is in getting the pages off the disk - which\n"
            "     is what steps 4 and 5 measure.\n")


def _scans_a_big_table(detail: str) -> bool:
    """True when a plan line scans a table that grows with the history.

    Self-contained rather than relying on the caller to have checked for
    SCAN first: a SEARCH over ``runs`` is exactly what the run-history
    query is supposed to do, and reporting it as a full scan would send
    someone off to fix the one query that is already right.
    """
    if not detail.startswith("SCAN"):
        return False
    head = detail.split(" USING ")[0]
    names = head.split()[1:]
    return any(name in ("runs", "run_outputs") for name in names)


# ----------------------------------------------------------------------
# Step 4: characterize the storage itself
# ----------------------------------------------------------------------


def measure_storage(path: str, out: Any) -> Optional[float]:
    """Read part of the file and report the throughput; MB/s or None."""
    heading(out, "4. How fast the storage itself is")
    size = os.path.getsize(path)
    want = min(_SAMPLE_BYTES, size)
    started = time.time()
    read = 0
    try:
        with open(path, "rb") as handle:
            while read < want:
                block = handle.read(min(_BLOCK_BYTES, want - read))
                if not block:
                    break
                read += len(block)
    except OSError as exc:
        out.write("  could not read the file: {0}\n".format(exc))
        return None
    elapsed = time.time() - started
    if elapsed <= 0:
        out.write("  read {0} too fast to measure (it was already "
                  "cached)\n".format(human_bytes(read)))
        return None
    rate = read / elapsed / (1024.0 * 1024.0)
    out.write("  read {0} in {1:.2f}s = {2:.1f} MB/s\n".format(
        human_bytes(read), elapsed, rate))
    out.write("  (whatever the OS had cached is included, so this is a "
              "FLOOR on how\n   bad the storage is - never an "
              "exaggeration)\n\n")
    if rate > 1000:
        out.write(
            "  => That is RAM speed, not disk speed: the file was already "
            "in the OS\n     page cache, so this measured nothing about the "
            "storage. On the real\n     server, run this as the first thing "
            "after a reboot, or just use\n     --compare-local, which does "
            "not depend on cache state.\n")
        return rate
    if rate < 20:
        out.write(
            "  => That is slow enough to explain multi-second screens on "
            "its own.\n     A local SSD does hundreds of MB/s; a healthy "
            "network mount does\n     tens. Confirm with --compare-local "
            "before acting on it.\n")
    elif rate < 100:
        out.write(
            "  => Middling. Enough to hurt when the page cache is small, "
            "which it\n     is by default. Try --cache-mb before moving "
            "anything.\n")
    else:
        out.write(
            "  => Fast. The storage is probably NOT your problem; look at "
            "the query\n     timings in step 2 instead.\n")
    return rate


def compare_local(
    path: str, out: Any, environment: Optional[str], script: Optional[str],
    repeat: int, remote: List[Tuple[str, float]], cache_mb: Optional[int],
) -> None:
    """Copy the database somewhere local and re-time; the decisive test."""
    heading(out, "5. The same queries against a local copy")
    size = os.path.getsize(path)
    out.write("  Copying {0} to local temporary space. This is the "
              "measurement\n  that settles it: if local is fast and the "
              "original is slow, the\n  storage is the problem and nothing "
              "else is.\n\n".format(human_bytes(size)))
    tmp_dir = tempfile.mkdtemp(prefix="testboard_diag_")
    local_path = os.path.join(tmp_dir, "copy.db")
    seconds, _, error = timed(lambda: shutil.copyfile(path, local_path))
    if error:
        out.write("  copy failed: {0}\n".format(error))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return
    out.write("  copied in {0:.1f}s ({1:.1f} MB/s)\n\n".format(
        seconds, size / max(seconds, 0.001) / (1024.0 * 1024.0)))
    try:
        storage = Storage(local_path, cache_mb=cache_mb) \
            if cache_mb is not None else Storage(local_path)
        local = time_queries(
            storage, out, environment, script, repeat,
            title="   the same queries, on local disk")
        storage.close()
        _compare(out, remote, local)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compare(
    out: Any, remote: List[Tuple[str, float]], local: List[Tuple[str, float]]
) -> None:
    """Print the verdict: storage, or query."""
    by_name = dict(local)
    ratios = []  # type: List[float]
    heading(out, "   verdict")
    out.write("  {0:<34} {1:>9} {2:>9} {3:>8}\n".format(
        "", "original", "local", "faster"))
    ignored = 0
    for name, slow in remote:
        fast = by_name.get(name)
        if fast is None or fast <= 0:
            continue
        ratio = slow / fast
        out.write("  {0:<34} {1:8.3f}s {2:8.3f}s {3:7.1f}x\n".format(
            name, slow, fast, ratio))
        # A pair where both sides are already instant carries no signal:
        # the ratio is measurement noise, and averaging it in would let a
        # query that takes no time either way outvote the one that takes
        # four seconds.
        if max(slow, fast) < _NOISE_FLOOR_SECONDS:
            ignored += 1
            continue
        ratios.append(ratio)
    if ignored:
        out.write(
            "\n  ({0} quer{1} excluded from the verdict: both sides under "
            "{2:.0f}ms, so the\n   ratio there is measurement noise rather "
            "than signal)\n".format(
                ignored, "y" if ignored == 1 else "ies",
                _NOISE_FLOOR_SECONDS * 1000))
    if not ratios:
        out.write(
            "\n  => NOTHING HERE IS SLOW. Every query is instant against "
            "both copies,\n     so the database is not what a slow screen "
            "is waiting for. Time the\n     endpoint itself instead, on the "
            "web server:\n"
            "       curl -o /dev/null -w '%{time_total}\\n' "
            "http://HOST:8000/api/summary\n")
        return
    median = sorted(ratios)[len(ratios) // 2]
    out.write("\n")
    if median >= 3:
        out.write(
            "  => THE STORAGE. The same queries against the same data are\n"
            "     {0:.1f}x faster on local disk, so the queries are fine "
            "and the\n     filesystem is the whole problem.\n".format(median))
    elif median >= 1.5:
        out.write(
            "  => MOSTLY THE STORAGE ({0:.1f}x), but not all of it. Raising "
            "the page\n     cache is likely to recover most of the "
            "difference without\n     moving anything.\n".format(median))
    else:
        out.write(
            "  => NOT THE STORAGE. Local disk is only {0:.1f}x faster, so "
            "the time is\n     going into the queries themselves. Look at "
            "step 2 for which one,\n     and step 3 for whether it is "
            "scanning a table it should not.\n".format(median))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def heading(out: Any, title: str) -> None:
    """Write a section heading."""
    out.write("\n" + title + "\n" + "-" * 68 + "\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="diagnose_db.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Measure why the dashboard is slow: report the database's real "
            "settings, time every screen's queries, characterize the "
            "storage, and - with --compare-local - say definitively "
            "whether the filesystem or the queries are at fault."
        ),
        epilog=__doc__.split("Usage::")[-1],
    )
    parser.add_argument("--db", default="testboard.db",
                        help="database to diagnose (default: %(default)s)")
    parser.add_argument("--repeat", type=int, default=3, metavar="N",
                        help=("time each query N times and keep the best "
                              "(default: %(default)s). The first run is "
                              "cold; the best is what a warm server does"))
    parser.add_argument("--environment", default=None,
                        help="filter queries by environment, as a screen would")
    parser.add_argument("--script", default=None,
                        help="filter queries by script")
    parser.add_argument("--cache-mb", type=int, default=None, metavar="MB",
                        help=("open with this much page cache, to see what "
                              "the fix would buy before deploying it"))
    parser.add_argument("--compare-local", action="store_true",
                        help=("copy the database to local temporary space "
                              "and re-run the timings against it. Needs "
                              "free space equal to the database, and is "
                              "the only test that settles storage vs query"))
    parser.add_argument("--skip-queries", action="store_true",
                        help="report settings and storage speed only")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run the diagnosis; 0 unless the database could not be opened."""
    args = build_parser().parse_args(argv)
    out = sys.stdout
    if not os.path.exists(args.db):
        sys.stderr.write(
            "no database at {0}. Point --db at the file the server is "
            "running against.\n".format(os.path.abspath(args.db)))
        return 2

    out.write("testboard database diagnosis\n")
    out.write("=" * 68 + "\n")
    describe_database(args.db, out)

    results = []  # type: List[Tuple[str, float]]
    if not args.skip_queries:
        storage = Storage(args.db, cache_mb=args.cache_mb) \
            if args.cache_mb is not None else Storage(args.db)
        if args.cache_mb is not None:
            out.write("\n  (opened with --cache-mb {0})\n".format(
                args.cache_mb))
        results = time_queries(
            storage, out, args.environment, args.script, args.repeat)
        explain_slow(storage, out, results)
        storage.close()

    measure_storage(args.db, out)

    if args.compare_local and results:
        compare_local(args.db, out, args.environment, args.script,
                      args.repeat, results, args.cache_mb)
    elif args.compare_local:
        out.write("\n(--compare-local needs the query timings; it does "
                  "nothing with --skip-queries)\n")
    else:
        out.write(
            "\nRun again with --compare-local for the decisive answer: the "
            "same\nqueries, the same data, on local disk.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
