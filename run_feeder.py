#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feeder CLI: read test-run records and push them into a testboard dashboard.

Examples::

    python3 run_feeder.py --url http://127.0.0.1:8000 --mode backfill \
        --source "results/*.jsonl"
    python3 run_feeder.py --url http://127.0.0.1:8000 --mode daily \
        --reader internal_reader:create_reader

Exit codes: 0 = all valid records accepted; 1 = some records were rejected
or some batches failed (see the logs and replay files); 2 = fatal error
(bad arguments, unusable --since, reader load failure, wrong Python).

NOTE: this file intentionally contains no f-strings and only type COMMENTS
(PEP 484 style), so it still *parses* under Python 2 — on RHEL 8 someone
will inevitably type ``python run_feeder.py`` (2.7) instead of ``python3``,
and they must get the clear version message from main() instead of a bare
SyntaxError. Do not add f-strings or inline annotations to this file.
"""

import argparse
import datetime
import logging
import sys
import traceback

try:
    from typing import List, Optional  # noqa: F401 (used in type comments)
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass

#: Fraction of invalid records above which an import is treated as a
#: failure rather than a run with some bad data in it. One malformed
#: record must not fail a nightly import; a reader that mis-maps a field
#: for a tenth of the estate must not report success.
_SKIP_RATE_LIMIT = 0.10


def compute_since(mode, hwm, since_arg, overlap_days):
    # type: (str, Optional[datetime.datetime], Optional[datetime.datetime], int) -> Optional[datetime.datetime]
    """Return the lower bound (naive UTC) for this import, or None for all.

    - ``backfill``: exactly ``since_arg`` (which may be None = everything).
    - ``daily``: ``hwm - overlap_days`` days; None when there is no saved
      high-water mark yet (first daily run imports everything). ``since_arg``
      is ignored in daily mode. The overlap is free because the server
      upserts on (environment, script, test_name, start_time).
    """
    if mode == "backfill":
        return since_arg
    if hwm is None:
        return None
    return hwm - datetime.timedelta(days=overlap_days)


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the feeder CLI."""
    parser = argparse.ArgumentParser(
        prog="run_feeder.py",
        description=(
            "Read test-run records via a pluggable reader and push them to "
            "a testboard dashboard (POST /api/import). Safe to re-run: the "
            "server upserts, duplicates are impossible."
        ),
    )
    parser.add_argument(
        "--url", default=None,
        help=("dashboard base URL, e.g. http://127.0.0.1:8000 "
              "(/api/import is appended automatically). Required unless "
              "--check-reader is given"))
    parser.add_argument(
        "--mode", default=None, choices=["backfill", "daily"],
        help=("backfill: import history (optionally bounded by --since); "
              "daily: import everything after the saved high-water mark "
              "minus --overlap-days. Required unless --check-reader is "
              "given"))
    parser.add_argument(
        "--check-reader", action="store_true",
        help=("check a reader on its own: load it, read every record, "
              "validate each one, and report what is wrong. Needs no "
              "server and no --url. Use this while writing a reader"))
    parser.add_argument(
        "--since", default=None, metavar="ISO",
        help=("backfill lower bound on start_time, ISO-8601 naive UTC, e.g. "
              "2026-07-01T00:00:00 (ignored in daily mode)"))
    parser.add_argument(
        "--reader", default="jsonl", metavar="SPEC",
        help=("reader spec: 'jsonl' (built-in JSON-lines reader) or "
              "'module.path:factory' where factory(sources) returns a "
              "feeder.reader.Reader (default: %(default)s)"))
    parser.add_argument(
        "--source", action="append", default=None, metavar="PATH_OR_GLOB",
        help="input file or glob pattern for the reader; may be repeated")
    parser.add_argument(
        "--batch-size", type=int, default=500, metavar="N",
        help="records per POST batch (default: %(default)s)")
    parser.add_argument(
        "--state-file", default="feeder_state.json", metavar="PATH",
        help="daily-mode high-water-mark file (default: %(default)s)")
    parser.add_argument(
        "--replay-dir", default=".", metavar="DIR",
        help=("directory for testboard_failed_batch_NNNN.json replay files "
              "(default: current directory)"))
    parser.add_argument(
        "--max-consecutive-failures", type=int, default=3, metavar="N",
        help=("stop the import after N batches fail back to back "
              "(default: %(default)s). Prevents a large backfill from "
              "writing one replay file per batch when the server has "
              "gone away"))
    parser.add_argument(
        "--overlap-days", type=int, default=1, metavar="N",
        help=("daily mode: re-import this many days before the high-water "
              "mark as a safety overlap (default: %(default)s)"))
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate and count records but send nothing over HTTP")
    parser.add_argument(
        "--allow-empty", action="store_true",
        help=("treat an import that read zero records as success. Without "
              "this, reading nothing is an error: a scheduled import that "
              "silently stops feeding must not report success"))
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging plus full tracebacks on fatal errors")
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Run the feeder CLI; returns the process exit code (0/1/2)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ — you are running {0}.{1}.{2}. "
            "Re-run with: python3 run_feeder.py\n".format(
                sys.version_info[0], sys.version_info[1], sys.version_info[2]))
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("run_feeder")

    # Imports deliberately happen only after the version check above:
    # these modules use Python-3-only syntax.
    import testboard.model
    import feeder.check
    import feeder.reader
    import feeder.state
    import feeder.submitter

    # --url/--mode are required for a real import but meaningless when
    # only checking a reader, so they are validated here rather than by
    # argparse.
    if not args.check_reader:
        missing = [
            name for name, value in (("--url", args.url),
                                     ("--mode", args.mode))
            if value is None
        ]
        if missing:
            log.error(
                "%s is required (or use --check-reader to check a reader "
                "on its own, with no server)", " and ".join(missing))
            return 2

    since_arg = None  # type: Optional[datetime.datetime]
    if args.since is not None:
        try:
            since_arg = testboard.model.parse_iso(args.since)
        except ValueError as exc:
            log.error(
                "bad --since value: %s. Expected ISO-8601 naive UTC like "
                "2026-07-01T00:00:00", exc)
            return 2

    sources = args.source if args.source is not None else []
    try:
        reader = feeder.reader.load_reader(args.reader, sources)
    except feeder.reader.ReaderLoadError as exc:
        log.error("%s", exc)
        if args.verbose:
            traceback.print_exc()
        return 2

    if args.check_reader:
        # Reader development loop: no server, no network, no state file.
        # Read everything, validate it the way the server would, and say
        # what is wrong.
        try:
            report = feeder.check.check_reader(reader.read(None))
        except Exception as exc:
            log.error(
                "the reader raised %s while reading: %s. read() must not "
                "raise on a bad record — log a warning and continue "
                "(re-run with --verbose for the full traceback)",
                type(exc).__name__, exc)
            if args.verbose:
                traceback.print_exc()
            return 2
        feeder.check.log_report(report, log)
        return 0 if report.ok else 1

    hwm = None  # type: Optional[datetime.datetime]
    if args.mode == "daily":
        hwm = feeder.state.load_high_water_mark(args.state_file)
    since = compute_since(args.mode, hwm, since_arg, args.overlap_days)
    if since is not None:
        log.info("importing runs with start_time >= %s",
                 testboard.model.format_iso(since))
    else:
        log.info("importing all available runs (no lower bound)")

    submitter = feeder.submitter.Submitter(
        args.url, batch_size=args.batch_size, replay_dir=args.replay_dir,
        max_consecutive_failures=args.max_consecutive_failures)
    try:
        stats = submitter.submit(
            reader.read(since), dry_run=args.dry_run, since=since)
    except Exception as exc:
        log.error(
            "import aborted by an unexpected error from the reader or "
            "submitter: %s: %s (re-run with --verbose for the full "
            "traceback)", type(exc).__name__, exc)
        if args.verbose:
            traceback.print_exc()
        return 1

    if not args.dry_run and stats.failed_batches == 0:
        new_hwm = submitter.max_accepted_start_time()
        if new_hwm is not None:
            feeder.state.save_high_water_mark(args.state_file, new_hwm)
            log.info("saved high-water mark %s to %s",
                     testboard.model.format_iso(new_hwm), args.state_file)

    # An import that read nothing is almost always a broken setup, not a
    # quiet night: a --source glob that stopped matching, a reader that
    # returned nothing, or a high-water mark ahead of the data. Left as
    # exit 0 it looks identical to success, so a scheduled task keeps
    # reporting "Last Result 0" while the dashboard silently goes stale.
    if stats.read == 0 and not args.allow_empty:
        log.error(
            "no records were read, so nothing was imported. This usually "
            "means one of:\n"
            "  - --source matched no files (check the path/glob above)\n"
            "  - the reader returned nothing (run with --check-reader "
            "--verbose to see what it yields)\n"
            "  - in daily mode, the high-water mark in %s is at or ahead "
            "of the newest available run\n"
            "If an empty import is expected here, pass --allow-empty.",
            args.state_file)
        return 1

    # A handful of bad records is normal and must not fail a nightly
    # import. A large FRACTION of them is a broken reader — the summary
    # above says so, but a scheduled task only reports its exit code, so
    # that case has to be a failure too.
    if stats.read and stats.skipped:
        skip_rate = float(stats.skipped) / float(stats.read)
        if skip_rate >= _SKIP_RATE_LIMIT:
            log.error(
                "%d of %d records (%.0f%%) were invalid and NOT imported. "
                "That is too many to be bad luck - it usually means the "
                "reader is mapping a field wrongly. The grouped reasons "
                "above name the rule and an affected test; fix the "
                "reader, check it with --check-reader, then re-run "
                "(importing is safe to repeat).",
                stats.skipped, stats.read, skip_rate * 100.0)
            return 1

    if stats.rejected == 0 and stats.failed_batches == 0:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
