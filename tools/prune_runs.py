#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retention: delete run history older than a cutoff.

The dashboard accumulates one row per test per night forever. At 12,000
tests a night that is ~4.4 million runs a year, and the test output that
comes with them is the bulk of the file on disk. This is the maintenance
job that keeps that bounded — run it from cron after the nightly import::

    python3 tools/prune_runs.py --db testboard.db --keep-days 365
    python3 tools/prune_runs.py --db testboard.db --keep-days 365 --vacuum

What it will NOT delete: the newest run of each test, however old. That
row is what the dashboard shows, and removing it would make a test that
stopped running vanish instead of appearing under "Not run".

``--dry-run`` reports what would go without touching the database.

Space freed by a delete is reused by SQLite for subsequent imports but is
not returned to the file system until a ``VACUUM``. Pass ``--vacuum`` to
do that here — it rewrites the whole file, needs free space roughly equal
to the final database size, and takes an exclusive lock, so run it in a
maintenance window rather than while the server is serving.

NOTE: like the other entry-point scripts, this file contains no f-strings
and only type COMMENTS, so it still *parses* under Python 2 and prints a
clear version message instead of a SyntaxError.
"""

import argparse
import datetime
import os
import sys
import traceback

try:
    from typing import List, Optional  # noqa: F401 (used in type comments)
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the prune CLI."""
    parser = argparse.ArgumentParser(
        prog="prune_runs.py",
        description=(
            "Delete run history older than --keep-days from a testboard "
            "database. Each test's newest run is always kept."
        ),
    )
    parser.add_argument(
        "--db", default="testboard.db", metavar="PATH",
        help="SQLite database file (default: %(default)s)")
    parser.add_argument(
        "--keep-days", type=int, default=365, metavar="N",
        help=("keep runs from the last N days (default: %(default)s); "
              "older runs are deleted"))
    parser.add_argument(
        "--vacuum", action="store_true",
        help=("rebuild the file afterwards to return freed space to the "
              "disk (exclusive lock, rewrites the whole database)"))
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be deleted, then exit without changing it")
    parser.add_argument(
        "--verbose", action="store_true",
        help="print the full traceback on failure")
    return parser


def _describe(size_bytes):
    # type: (float) -> str
    """Format a byte count for the human reading the log."""
    megabytes = size_bytes / (1024.0 * 1024.0)
    if megabytes >= 1024:
        return "{0:.2f} GB".format(megabytes / 1024.0)
    return "{0:.1f} MB".format(megabytes)


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Prune old runs; returns the process exit code (0 = done, 2 = error)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ - you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/prune_runs.py\n".format(
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

    if args.keep_days < 1:
        sys.stderr.write("--keep-days must be at least 1.\n")
        return 2
    if not os.path.isfile(args.db):
        sys.stderr.write(
            "Database not found: {0}\n".format(os.path.abspath(args.db)))
        return 2

    # Imported only after the version check: Python-3-only syntax.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import testboard.model
    import testboard.storage

    cutoff = (testboard.model.utcnow()
              - datetime.timedelta(days=args.keep_days))
    before = os.path.getsize(args.db)
    print("Database: {0} ({1})".format(
        os.path.abspath(args.db), _describe(before)))
    print("Deleting runs started before {0} (keeping {1} days, and every "
          "test's newest run).".format(
              cutoff.strftime("%Y-%m-%d %H:%M:%S"), args.keep_days))

    try:
        storage = testboard.storage.Storage(args.db)
    except Exception as exc:
        sys.stderr.write("Cannot open the database: {0}\n".format(exc))
        if args.verbose:
            traceback.print_exc()
        return 2

    try:
        if args.dry_run:
            print("Dry run: {0} runs would be deleted.".format(
                storage.count_runs_before(cutoff)))
            return 0
        deleted = storage.prune_runs_before(cutoff)
        print("Deleted {0} runs.".format(deleted))
        if args.vacuum:
            print("Rebuilding the database (VACUUM)...")
            storage.vacuum()
        after = os.path.getsize(args.db)
        print("Database is now {0} (was {1}).".format(
            _describe(after), _describe(before)))
        if not args.vacuum and deleted:
            print("Freed pages stay in the file for reuse; pass --vacuum "
                  "to return them to the disk.")
    except Exception as exc:
        sys.stderr.write("Prune failed: {0}\n".format(exc))
        if args.verbose:
            traceback.print_exc()
        return 2
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
