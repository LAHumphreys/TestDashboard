#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report the distribution of times in a --perf-log file.

    python3 tools/perf_report.py testboard-perf.log
    python3 tools/perf_report.py testboard-perf.log --since 2026-07-30T09:00:00
    python3 tools/perf_report.py testboard-perf.log --sort total --top 15

Written by ``run_server.py --perf-log PATH``. Reads the rolled-over
predecessor (``PATH.1``) too unless told not to, so a report spanning a
rollover is not silently half a report.

WHAT THE COLUMNS MEAN
---------------------

One row per *label*: a storage operation (a Storage method) or a request
route. For each:

    n       how many were seen
    mean    the arithmetic mean
    p50     the median: half of them were at or below this
    p25/p75 the quartiles -- how spread out they are
    p99     the slow tail: 99% were at or below this, so the remaining
            1% were WORSE than it. Not "the worst"; that is max
    p1      the fast end, which is roughly the warm-cache cost
    max     the worst single one
    total   n x mean, i.e. where the time actually went

``total`` is usually the column that answers "what should I fix". A 900 ms
query called once a minute costs less than a 9 ms query called ten
thousand times, and only ``total`` says so.

READING THE REQUEST SECTION
---------------------------

Requests carry two extra columns:

    q50/q99   time spent QUEUED for a worker, not being served
    qmax      the worst queue wait seen

That distinction is the reason this file exists. A slow request with a
near-zero queue wait is a slow query -- look for it in the storage
section. A fast request with a large queue wait is not slow at all: the
server had no free worker, and the fix is capacity or whatever was
holding the workers, not the query.

Percentiles are nearest-rank on the sorted samples (no interpolation),
which for a distribution read to two significant figures is the honest
method and needs no assumption about its shape.

NOTE: like the other entry-point scripts, this file contains no f-strings
and only type COMMENTS, so it still *parses* under Python 2 and prints a
clear version message instead of a SyntaxError.
"""

import argparse
import io
import json
import math
import os
import sys

try:
    from typing import Dict, List, Optional, Tuple  # noqa: F401
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the perf report CLI."""
    parser = argparse.ArgumentParser(
        prog="perf_report.py",
        description=(
            "Summarise a testboard performance log: per storage operation "
            "and per request route, how many, how slow, and where the time "
            "went."
        ),
    )
    parser.add_argument(
        "path", metavar="PATH",
        help="the log written by run_server.py --perf-log")
    parser.add_argument(
        "--top", type=int, default=25, metavar="N",
        help="show only the N heaviest rows per section (default: %(default)s)")
    parser.add_argument(
        "--sort", choices=("total", "mean", "p99", "max", "n"),
        default="total",
        help=("order rows by this column (default: %(default)s, which is "
              "where the time actually went rather than what is slowest "
              "once)"))
    parser.add_argument(
        "--since", default=None, metavar="TIMESTAMP",
        help=("ignore records before this ISO-8601 UTC timestamp, e.g. "
              "2026-07-30T09:00:00 -- use it to look at one stall rather "
              "than the whole file"))
    parser.add_argument(
        "--until", default=None, metavar="TIMESTAMP",
        help="ignore records at or after this ISO-8601 UTC timestamp")
    parser.add_argument(
        "--kind", choices=("storage", "request", "all"), default="all",
        help="report only one kind of record (default: %(default)s)")
    parser.add_argument(
        "--no-rollover", action="store_true",
        help="read only PATH, ignoring the rolled-over PATH.1")
    parser.add_argument(
        "--csv", action="store_true",
        help="emit CSV instead of a text table, for a spreadsheet")
    return parser


def percentile(ordered, fraction):
    # type: (List[float], float) -> float
    """Nearest-rank percentile of a pre-sorted list; 0.0 when empty.

    The textbook nearest-rank definition: the smallest value that at
    least *fraction* of the samples are less than or equal to, at rank
    ``ceil(fraction x n)`` counting from 1. No interpolation, so every
    number reported is a time that was really measured rather than an
    average of two that were not.
    """
    count = len(ordered)
    if count == 0:
        return 0.0
    rank = int(math.ceil(fraction * count))
    return ordered[min(max(rank - 1, 0), count - 1)]


def mean(values):
    # type: (List[float]) -> float
    """Arithmetic mean of a non-empty list."""
    return sum(values) / float(len(values)) if values else 0.0


class Group(object):
    """Accumulated samples for one label."""

    def __init__(self, label):
        # type: (str) -> None
        self.label = label
        self.times = []        # type: List[float]
        self.queued = []       # type: List[float]
        self.statuses = {}     # type: Dict[int, int]

    def add(self, milliseconds, queued=None, status=None):
        # type: (float, Optional[float], Optional[int]) -> None
        """Record one sample."""
        self.times.append(milliseconds)
        if queued is not None:
            self.queued.append(queued)
        if status is not None:
            self.statuses[status] = self.statuses.get(status, 0) + 1

    def stats(self):
        # type: () -> Dict[str, float]
        """The distribution, as the report's columns."""
        ordered = sorted(self.times)
        queued = sorted(self.queued)
        return {
            "n": float(len(ordered)),
            "mean": mean(ordered),
            "p1": percentile(ordered, 0.01),
            "p25": percentile(ordered, 0.25),
            "p50": percentile(ordered, 0.50),
            "p75": percentile(ordered, 0.75),
            "p99": percentile(ordered, 0.99),
            "max": ordered[-1] if ordered else 0.0,
            "total": sum(ordered),
            "q50": percentile(queued, 0.50),
            "q99": percentile(queued, 0.99),
            "qmax": queued[-1] if queued else 0.0,
        }

    def bad_statuses(self):
        # type: () -> str
        """Non-2xx statuses seen, as a short string ("" when all fine)."""
        bad = sorted(code for code in self.statuses if code >= 400)
        return " ".join(
            "{0}x{1}".format(code, self.statuses[code]) for code in bad)


def read_files(path, include_rollover):
    # type: (str, bool) -> List[str]
    """Log files to read, oldest first."""
    files = []
    previous = path + ".1"
    if include_rollover and os.path.isfile(previous):
        files.append(previous)
    files.append(path)
    return files


def load(paths, since=None, until=None, kind="all"):
    # type: (List[str], Optional[str], Optional[str], str) -> Tuple[Dict[str, Dict[str, Group]], Dict[str, int]]
    """Read the logs and group the records.

    Returns ({kind: {label: Group}}, counters). Malformed lines are
    counted, not fatal: the last line of a file a running server is still
    appending to is routinely half-written, and refusing to report
    because of it would make the tool useless exactly when it is needed.

    Labels are interned per file, so the id -> name map resets at each
    file boundary.
    """
    groups = {"storage": {}, "request": {}}  \
        # type: Dict[str, Dict[str, Group]]
    counters = {"lines": 0, "records": 0, "malformed": 0, "unlabelled": 0}

    for path in paths:
        names = {}  # type: Dict[int, str]
        with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                counters["lines"] += 1
                try:
                    record = json.loads(line)
                except ValueError:
                    counters["malformed"] += 1
                    continue
                record_kind = record.get("k")
                if record_kind == "label":
                    names[record.get("i")] = record.get("n", "?")
                    continue
                if record_kind not in groups:
                    counters["malformed"] += 1
                    continue
                if kind != "all" and record_kind != kind:
                    continue
                stamp = record.get("t", "")
                if since is not None and stamp < since:
                    continue
                if until is not None and stamp >= until:
                    continue
                label = names.get(record.get("l"))
                if label is None:
                    # A record whose definition line was in the file that
                    # has already rolled away. Countable, not droppable
                    # silently.
                    counters["unlabelled"] += 1
                    label = "(unknown label {0})".format(record.get("l"))
                counters["records"] += 1
                bucket = groups[record_kind]
                if label not in bucket:
                    bucket[label] = Group(label)
                bucket[label].add(
                    float(record.get("ms", 0.0)),
                    queued=record.get("qms"),
                    status=record.get("s"))
    return groups, counters


def _fmt(milliseconds):
    # type: (float) -> str
    """Format a duration in ms, readably across five orders of magnitude."""
    if milliseconds >= 100000:
        return "{0:.0f}s".format(milliseconds / 1000.0)
    if milliseconds >= 1000:
        return "{0:.2f}s".format(milliseconds / 1000.0)
    if milliseconds >= 10:
        return "{0:.0f}ms".format(milliseconds)
    return "{0:.2f}ms".format(milliseconds)


_STORAGE_COLUMNS = ("n", "mean", "p50", "p25", "p75", "p99", "p1", "max",
                    "total")
_REQUEST_COLUMNS = _STORAGE_COLUMNS + ("q50", "q99", "qmax")


def print_section(title, note, groups, columns, sort_key, top, out):
    # type: (str, str, Dict[str, Group], Tuple[str, ...], str, int, object) -> None
    """Print one section as an aligned text table."""
    write = getattr(out, "write")
    write("\n" + title + "\n" + "=" * len(title) + "\n")
    if not groups:
        write("  (nothing recorded)\n")
        return
    write(note + "\n\n")

    rows = []
    for group in groups.values():
        rows.append((group.label, group.stats(), group.bad_statuses()))
    rows.sort(key=lambda row: row[1][sort_key], reverse=True)
    hidden = max(0, len(rows) - top)
    rows = rows[:top]

    label_width = max([len(row[0]) for row in rows] + [len("operation")])
    header = "{0:<{1}}".format("operation", label_width)
    for column in columns:
        header += "{0:>9}".format(column)
    header += "  errors"
    write(header + "\n")
    write("-" * len(header) + "\n")

    for label, stats, bad in rows:
        line = "{0:<{1}}".format(label, label_width)
        for column in columns:
            if column == "n":
                line += "{0:>9,}".format(int(stats["n"]))
            else:
                line += "{0:>9}".format(_fmt(stats[column]))
        line += "  " + (bad if bad else "")
        write(line.rstrip() + "\n")

    if hidden:
        # Never let a --top cap look like the whole picture.
        write("\n  ... {0} further rows not shown (--top {1}); "
              "raise --top to see them\n".format(hidden, top))


def print_csv(groups, out):
    # type: (Dict[str, Dict[str, Group]], object) -> None
    """Emit every row of every section as CSV."""
    write = getattr(out, "write")
    columns = _REQUEST_COLUMNS
    write("kind,operation," + ",".join(columns) + ",errors\n")
    for kind in sorted(groups):
        for label in sorted(groups[kind]):
            group = groups[kind][label]
            stats = group.stats()
            cells = [kind, '"' + label.replace('"', '""') + '"']
            for column in columns:
                if column == "n":
                    cells.append(str(int(stats["n"])))
                else:
                    cells.append("{0:.3f}".format(stats[column]))
            cells.append('"' + group.bad_statuses() + '"')
            write(",".join(cells) + "\n")


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Report on a perf log; returns the exit code (0 = done, 2 = error)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ - you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/perf_report.py\n".format(
                sys.version_info[0], sys.version_info[1],
                sys.version_info[2]))
        return 2

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2

    if not os.path.isfile(args.path):
        sys.stderr.write(
            "Performance log not found: {0}\n"
            "It is written only when the server is started with "
            "--perf-log PATH.\n".format(os.path.abspath(args.path)))
        return 2
    if args.top < 1:
        sys.stderr.write("--top must be at least 1.\n")
        return 2

    paths = read_files(args.path, not args.no_rollover)
    try:
        groups, counters = load(
            paths, since=args.since, until=args.until, kind=args.kind)
    except Exception as exc:
        sys.stderr.write("Cannot read the log: {0}\n".format(exc))
        return 2

    out = sys.stdout
    if args.csv:
        print_csv(groups, out)
        return 0

    out.write("Performance log: {0}\n".format(
        ", ".join(os.path.abspath(path) for path in paths)))
    out.write("{0:,} lines, {1:,} records reported".format(
        counters["lines"], counters["records"]))
    if args.since or args.until:
        out.write(" (window {0} .. {1})".format(
            args.since or "start", args.until or "end"))
    out.write("\n")
    if counters["malformed"]:
        out.write("{0:,} unreadable line(s) skipped -- normal for the last "
                  "line of a log a server is still writing.\n".format(
                      counters["malformed"]))
    if counters["unlabelled"]:
        out.write("{0:,} record(s) whose label definition had already "
                  "rolled away.\n".format(counters["unlabelled"]))
    if not counters["records"]:
        out.write("\nNothing to report in that window.\n")
        return 0

    if args.kind in ("all", "storage"):
        print_section(
            "Storage operations",
            "One row per Storage method. A method may issue several SQL\n"
            "statements, so this is the cost of the whole operation.\n"
            "'total' is where the time went; 'p99' is the slow tail.",
            groups["storage"], _STORAGE_COLUMNS, args.sort, args.top, out)

    if args.kind in ("all", "request"):
        print_section(
            "Requests",
            "q50/q99/qmax are time spent QUEUED for a worker, not being\n"
            "served -- a large queue wait with a small mean is contention\n"
            "for workers, not a slow query.",
            groups["request"], _REQUEST_COLUMNS, args.sort, args.top, out)

    _print_verdict(groups, out)
    return 0


def _print_verdict(groups, out):
    # type: (Dict[str, Dict[str, Group]], object) -> None
    """Say what the queue waits add up to, since that is the whole point."""
    queued = []  # type: List[float]
    for group in groups.get("request", {}).values():
        queued.extend(group.queued)
    if not queued:
        return
    queued.sort()
    out.write("\nQueue waits across all {0:,} connections: median {1}, "
              "p99 {2}, worst {3}.\n".format(
                  len(queued), _fmt(percentile(queued, 0.50)),
                  _fmt(percentile(queued, 0.99)), _fmt(queued[-1])))
    stalled = [value for value in queued if value >= 1000.0]
    if stalled:
        out.write(
            "{0:,} connection(s) waited a second or more for a worker. "
            "That is contention, not query time -- look at what was "
            "running then (an import?) and at --workers.\n".format(
                len(stalled)))
    else:
        out.write("No connection waited a second for a worker.\n")


if __name__ == "__main__":
    sys.exit(main())
