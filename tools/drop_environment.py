#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delete an environment, and everything belonging to it, from a database.

For an environment that should never have been imported -- a reader
mis-configuration that filed runs under a name like ``UNKNOWN``. It is
not the tool for an environment that has been decommissioned but whose
history is still worth reading; that is what retirement is for.

    python3 tools/drop_environment.py --db testboard.db --environment UNKNOWN
    python3 tools/drop_environment.py --db testboard.db -e UNKNOWN --dry-run

**This cannot be undone.** So:

- ``--dry-run`` reports the row counts and changes nothing. Run it first.
- Without ``--yes`` the environment name has to be typed back at the
  prompt. An environment name is the one thing a mistake here turns on,
  and ``prod`` is three characters away from a name you meant to drop.
- The only rollback is a copy of the database file. Take one.

Run it with the server STOPPED. The delete takes one transaction, and
the derived tables it rewrites (``latest_runs``,
``current_assignments``) are what every estate-wide read goes through.

Space is not returned to the file system until a ``VACUUM``; pass
``--vacuum`` to do that here, in a maintenance window (it rewrites the
whole file and takes an exclusive lock).

NOTE: like the other entry-point scripts, this file contains no
f-strings and only type COMMENTS, so it still *parses* under Python 2 and
prints a clear version message instead of a SyntaxError.
"""

import argparse
import os
import sys
import traceback

try:
    from typing import Dict, List, Optional  # noqa: F401 (used in comments)
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the drop-environment CLI."""
    parser = argparse.ArgumentParser(
        prog="drop_environment.py",
        description=(
            "Delete one environment and all of its runs, results, "
            "assignments, comments, retirements and expectations from a "
            "testboard database. This cannot be undone."
        ),
    )
    parser.add_argument(
        "--db", default="testboard.db", metavar="PATH",
        help="SQLite database file (default: %(default)s)")
    parser.add_argument(
        "--environment", "-e", required=True, metavar="NAME",
        help="the environment to delete (exact match, case sensitive)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be deleted, then exit without changing it")
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (for use from a script)")
    parser.add_argument(
        "--vacuum", action="store_true",
        help=("rebuild the file afterwards to return freed space to the "
              "disk (exclusive lock, rewrites the whole database)"))
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


def _report(counts):
    # type: (Dict[str, int]) -> int
    """Print a per-table row count table; return the total."""
    total = 0
    for table in sorted(counts):
        rows = counts[table]
        total += rows
        if rows:
            print("  {0:<26} {1:>12,}".format(table, rows))
    print("  {0:<26} {1:>12,}".format("TOTAL", total))
    return total


def _confirm(environment):
    # type: (str) -> bool
    """Ask the operator to type the environment name back."""
    try:
        typed = input(
            "\nType the environment name to confirm deletion "
            "(anything else aborts): ")
    except EOFError:
        # No terminal (cron, a pipe). Refusing is the safe reading of
        # "nobody is there to confirm"; --yes is how a script says so
        # deliberately.
        print("\nNo input available; aborting. Pass --yes to skip the "
              "prompt deliberately.")
        return False
    return typed.strip() == environment


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Delete an environment; returns the exit code (0 = done, 2 = error)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ - you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/drop_environment.py\n".format(
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

    if not args.environment.strip():
        sys.stderr.write("--environment must not be empty.\n")
        return 2
    if not os.path.isfile(args.db):
        sys.stderr.write(
            "Database not found: {0}\n".format(os.path.abspath(args.db)))
        return 2

    # Imported only after the version check: Python-3-only syntax.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import testboard.storage

    before = os.path.getsize(args.db)
    print("Database: {0} ({1})".format(
        os.path.abspath(args.db), _describe(before)))

    try:
        storage = testboard.storage.Storage(args.db)
    except Exception as exc:
        sys.stderr.write("Cannot open the database: {0}\n".format(exc))
        if args.verbose:
            traceback.print_exc()
        return 2

    try:
        counts = storage.count_environment_rows(args.environment)
        total = sum(counts.values())
        if total == 0:
            # Not an error: re-running a job that already succeeded
            # should be quiet and successful. But say what IS there,
            # because the usual cause is a typo or a case difference --
            # the match is exact, so "Unknown" is not "UNKNOWN".
            print("\nNothing in the database belongs to an environment "
                  "named {0!r}.".format(args.environment))
            known = storage.environments()
            print("Environments with runs: {0}".format(
                ", ".join(known) if known else "(none)"))
            return 0

        print("\nRows belonging to environment {0!r}:".format(
            args.environment))
        _report(counts)

        if args.dry_run:
            print("\nDry run: nothing was changed.")
            return 0

        if not args.yes and not _confirm(args.environment):
            print("Aborted; nothing was changed.")
            return 1

        print("\nDeleting...")
        deleted = storage.delete_environment(args.environment)
        removed = _report(deleted)
        if removed != total:
            # Worth saying rather than swallowing: it means something
            # wrote to the database between the count and the delete.
            print("\nNote: counted {0:,} rows, deleted {1:,}. The database "
                  "changed in between -- is the server or feeder "
                  "running?".format(total, removed))

        if args.vacuum:
            print("Rebuilding the database (VACUUM)...")
            storage.vacuum()
        after = os.path.getsize(args.db)
        print("\nDatabase is now {0} (was {1}).".format(
            _describe(after), _describe(before)))
        if not args.vacuum and removed:
            print("Freed pages stay in the file for reuse; pass --vacuum "
                  "to return them to the disk.")
        print("Restart the server so it is not serving a cached summary "
              "of an environment that no longer exists.")
    except Exception as exc:
        sys.stderr.write("Delete failed: {0}\n".format(exc))
        if args.verbose:
            traceback.print_exc()
        return 2
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
