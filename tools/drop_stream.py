#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delete one stream, and everything belonging to it, from a database.

The `drop_environment.py` analogue for a typo'd or dead build stream
(docs/STREAMS_PLAN.md §3.8): a CI job pointed `--build` at the wrong
name, a one-off RC nobody needs kept, a spelling mistake that will
otherwise sit in the Build picker forever. It is NOT a lifecycle tool --
there are no declared stream states (docs/STREAMS_PLAN.md §6's 2026-08-08
decision) -- this is the manual pressure valve for a stream that should
simply not exist, the same relationship `drop_environment.py` has to
retirement. No `--kind` flag (WP-25, docs/ONE_KIND_PLAN.md): the
`branch` kind died before it ever shipped, so `--product` plus `--name`
identify a stream uniquely among the one kind left.

    python3 tools/drop_stream.py --db testboard.db \
        --product Atlas --name feat/typo
    python3 tools/drop_stream.py --db testboard.db \
        --product Atlas --name feat/typo --dry-run

Works against either backend: pass --db for SQLite (as above) or
--db-config plus --site-notes for MariaDB, the same flags run_server.py
and the other tools use.

**This cannot be undone.** So:

- `--dry-run` reports the row counts and changes nothing. Run it first.
- Without `--yes` the stream's "build:name" has to be typed back at the
  prompt.
- The mainline stream (id 1) is REFUSED unconditionally -- there is no
  flag that overrides this.
- The only rollback is a copy of the database file (SQLite) or the last
  data migration (MariaDB, which this tool does not run DDL against
  either way -- it only deletes rows through the same Storage a running
  server uses).

Run it with the server STOPPED. The delete takes one transaction, and the
derived table it rewrites (`latest_runs`) is what every estate-wide read
goes through.

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
    """Build the argument parser for the drop-stream CLI."""
    parser = argparse.ArgumentParser(
        prog="drop_stream.py",
        description=(
            "Delete one build stream (never mainline) and all of its "
            "runs, outputs and latest_runs partition from a testboard "
            "database. This cannot be undone."
        ),
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="SQLite database file (mutually exclusive with --db-config)")
    parser.add_argument(
        "--db-config", default=None, metavar="CNF",
        help=("mysql option file naming a MariaDB server (mutually "
              "exclusive with --db); requires --site-notes"))
    parser.add_argument(
        "--site-notes", default=None, metavar="PATH",
        help="site notes file, required alongside --db-config")
    parser.add_argument(
        "--product", required=True, metavar="NAME",
        help=("the stream's product, exact match, case sensitive -- "
              "\"\" for the implicit product (environments with no "
              "declared mapping)"))
    parser.add_argument(
        "--name", required=True, metavar="NAME",
        help=("the stream's name, exact match, case sensitive. "
              "--product plus --name identify it -- streams.kind is "
              "always 'build' (WP-25, docs/ONE_KIND_PLAN.md: the "
              "'branch' kind died before it ever shipped) and mainline "
              "is refused unconditionally, so there is nothing left for "
              "a --kind flag to select between"))
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be deleted, then exit without changing it")
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (for use from a script)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="print the full traceback on failure")
    return parser


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


def _confirm(label):
    # type: (str) -> bool
    """Ask the operator to type the stream's build:name label back."""
    try:
        typed = input(
            "\nType the stream's build:name to confirm deletion "
            "(anything else aborts): ")
    except EOFError:
        # No terminal (cron, a pipe). Refusing is the safe reading of
        # "nobody is there to confirm"; --yes is how a script says so
        # deliberately.
        print("\nNo input available; aborting. Pass --yes to skip the "
              "prompt deliberately.")
        return False
    return typed.strip() == label


def _open_storage(args):
    # type: (argparse.Namespace) -> Any
    """Open the storage backend named by --db or --db-config."""
    import testboard.storage
    if args.db and args.db_config:
        sys.stderr.write("--db and --db-config are mutually exclusive.\n")
        return None
    if args.db:
        if not os.path.isfile(args.db):
            sys.stderr.write(
                "Database not found: {0}\n".format(os.path.abspath(args.db)))
            return None
        return testboard.storage.Storage(args.db)
    if args.db_config:
        if not args.site_notes:
            sys.stderr.write(
                "--db-config requires --site-notes (the same pair "
                "run_server.py needs for a MariaDB deployment).\n")
            return None
        import testboard.dbconfig
        settings = testboard.dbconfig.read_option_file(args.db_config)
        return testboard.storage.Storage.mariadb(settings)
    sys.stderr.write("One of --db or --db-config is required.\n")
    return None


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Delete a stream; returns the exit code (0 = done, 2 = error)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ - you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/drop_stream.py\n".format(
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

    if not args.name.strip():
        sys.stderr.write("--name must not be empty.\n")
        return 2

    # Imported only after the version check: Python-3-only syntax.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        storage = _open_storage(args)
    except Exception as exc:
        sys.stderr.write("Cannot open the database: {0}\n".format(exc))
        if args.verbose:
            traceback.print_exc()
        return 2
    if storage is None:
        return 2

    label = "build:{0}".format(args.name)

    try:
        streams = storage.list_streams(args.product)
        match = None
        for stream in streams:
            # kind == "build" defensively, even though list_streams()
            # already excludes mainline (id != MAINLINE_STREAM_ID) and
            # every non-mainline stream is 'build' post-WP-25 -- this is
            # what keeps product+name unable to ever select the mainline
            # row, the same guarantee --kind used to provide by never
            # accepting 'mainline' as a choice.
            if stream.kind == "build" and stream.name == args.name:
                match = stream
                break
        if match is None:
            print(
                "\nNo stream named {0!r} exists under product "
                "{1!r}.".format(args.name, args.product))
            if streams:
                print("Streams that DO exist under this product:")
                for stream in streams:
                    print("  {0}:{1} (id {2}, last seen {3})".format(
                        stream.kind, stream.name, stream.stream_id,
                        stream.last_seen))
            else:
                print("No streams at all exist under this product.")
            return 0

        print("Database: {0}".format(
            os.path.abspath(args.db) if args.db else args.db_config))
        print("\nStream {0!r} (id {1}, product {2!r}): first seen {3}, "
              "last seen {4}, currently failing {5}.".format(
                  label, match.stream_id, args.product, match.first_seen,
                  match.last_seen, match.failing))

        counts = storage.count_stream_rows(match.stream_id)
        total = sum(counts.values())
        print("\nRows belonging to this stream:")
        _report(counts)

        # current_assignments.stream_id: since WP-27, delete_stream
        # clears this with an explicit UPDATE in the same transaction
        # as the delete -- identical on both backends, the same
        # protection comments.stream_id has always had (see
        # Storage.assignments_referencing_stream's docstring for the
        # pre-WP-27 divergence this closed). Read BEFORE the delete
        # regardless: afterwards it is always 0 on both backends, which
        # tells the operator nothing about what is ABOUT to be lost.
        referencing = storage.assignments_referencing_stream(
            match.stream_id)
        if referencing:
            print(
                "\n{0:,} assignment(s) were made from this stream. "
                "Deleting it clears their origin tag: they keep their "
                "Build-originated filter grouping in Open actions, but "
                "lose the name.".format(referencing))

        if args.dry_run:
            print("\nDry run: nothing was changed.")
            return 0

        if not args.yes and not _confirm(label):
            print("Aborted; nothing was changed.")
            return 1

        print("\nDeleting...")
        deleted = storage.delete_stream(match.stream_id)
        removed = _report(deleted)
        if removed != total + 1:  # +1 for the streams row itself
            print("\nNote: counted {0:,} rows (plus the stream row), "
                  "deleted {1:,}. The database changed in between - is "
                  "the server or feeder running?".format(
                      total + 1, removed))
        print("\nRestart the server so it is not serving a cached list "
              "that still includes this stream.")
    except ValueError as exc:
        sys.stderr.write("Refused: {0}\n".format(exc))
        return 2
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
