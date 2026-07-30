#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add a note to a day's "What's new", for changes outside testboard.

`static/whatsnew.html` carries testboard's own release notes and ships
inside the build. This adds the SITE's notes alongside them, under the
same date -- the reader that was mis-filing runs, the box that was
rebuilt, the environment that was renamed. A tester reading "what changed
today" does not care which repository it came from.

    python3 tools/add_site_note.py --db testboard.db \\
        --text "Fixed the parser bug that was filing runs under UNKNOWN."

    python3 tools/add_site_note.py --db testboard.db --date 2026-07-28 \\
        --text "linux-uat rebuilt overnight; the first pass was short." \\
        --author luke

    python3 tools/add_site_note.py --db testboard.db --list
    python3 tools/add_site_note.py --db testboard.db --edit 3 \\
        --text "Corrected: it was linux-uat, not linux-sim."
    python3 tools/add_site_note.py --db testboard.db --remove 3

A note is PUBLISHED as soon as it is written -- the server re-reads the
file per request -- so --edit and --remove are how a typo gets fixed
rather than hand-editing JSON underneath a running server. ``--list``
shows the id each takes.

Defaults: today's date (UTC, matching the dashboard's clock), and the
notes file beside --db -- outside the repository, so a ``git pull`` cannot
overwrite it. Give --file to put it elsewhere; the server needs the same
path via ``run_server.py --site-notes``.

No restart is needed. The server reads this file per request.

NOTE: like the other entry-point scripts, this file contains no f-strings
and only type COMMENTS, so it still *parses* under Python 2 and prints a
clear version message instead of a SyntaxError.
"""

import argparse
import os
import sys
import traceback

try:
    from typing import List, Optional  # noqa: F401 (used in type comments)
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the add-site-note CLI."""
    parser = argparse.ArgumentParser(
        prog="add_site_note.py",
        description=(
            "Add a site-specific note to a dated drop on the What's new "
            "page, for changes that were not part of the testboard build."
        ),
    )
    parser.add_argument(
        "--text", "-t", default=None, metavar="TEXT",
        help="the note, written for a tester (required unless --list)")
    parser.add_argument(
        "--date", "-d", default=None, metavar="YYYY-MM-DD",
        help=("the drop this belongs to (default: today, UTC). Use an "
              "earlier date to file it under an earlier drop"))
    parser.add_argument(
        "--author", "-a", default=None, metavar="NAME",
        help="who to credit (default: the OS username)")
    parser.add_argument(
        "--db", default="testboard.db", metavar="PATH",
        help=("the database, used only to locate the notes file beside it "
              "(default: %(default)s)"))
    parser.add_argument(
        "--file", default=None, metavar="PATH",
        help="notes file to write (default: site_notes.json beside --db)")
    parser.add_argument(
        "--list", action="store_true",
        help="list the notes already on file (with their ids), then exit")
    parser.add_argument(
        "--edit", type=int, default=None, metavar="ID",
        help=("correct the note with this id: give --text and/or --date. "
              "The author and the time it was recorded are kept"))
    parser.add_argument(
        "--remove", type=int, default=None, metavar="ID",
        help="delete the note with this id (see --list for ids)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="print the full traceback on failure")
    return parser


def _default_author():
    # type: () -> str
    """Best guess at who is running this."""
    for name in ("USER", "USERNAME", "LOGNAME"):
        value = os.environ.get(name)
        if value:
            return value
    return "unknown"


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Add or list site notes; returns the exit code (0 = done, 2 = error)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ - you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/add_site_note.py\n".format(
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

    actions = [bool(args.list), args.edit is not None, args.remove is not None]
    if sum(1 for action in actions if action) > 1:
        sys.stderr.write("Pick one of --list, --edit or --remove.\n")
        return 2
    if not any(actions) and not args.text:
        sys.stderr.write(
            "--text is required (or pass --list, --edit or --remove).\n")
        return 2
    if args.edit is not None and args.text is None and args.date is None:
        sys.stderr.write("--edit needs --text and/or --date.\n")
        return 2
    if args.remove is not None and args.text is not None:
        sys.stderr.write("--remove takes no --text.\n")
        return 2

    # Imported only after the version check: Python-3-only syntax.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import testboard.model
    import testboard.site_notes

    path = args.file if args.file else testboard.site_notes.default_path(
        args.db)

    if args.list:
        notes, problem = testboard.site_notes.load(path)
        print("Notes file: {0}".format(os.path.abspath(path)))
        if problem:
            sys.stderr.write("Problem: {0}\n".format(problem))
        if not notes:
            print("(no notes yet)")
            return 0
        for note in notes:
            print("\n  id {0}   {1}   [{2}]".format(
                note.note_id, note.date, note.author))
            print("      {0}".format(note.text))
        print("\n{0} note(s). Correct one with --edit ID --text ..., or "
              "delete it with --remove ID.".format(len(notes)))
        return 0

    if args.remove is not None:
        try:
            removed = testboard.site_notes.remove(path, args.remove)
        except ValueError as exc:
            sys.stderr.write("{0}\n".format(exc))
            return 2
        if removed is None:
            sys.stderr.write(
                "No note with id {0} in {1}. Run --list to see the ids.\n"
                .format(args.remove, os.path.abspath(path)))
            return 2
        # Print what went. A wrong id is then obvious at once, rather than
        # when somebody notices the wrong note missing next week.
        print("Removed from {0}:".format(os.path.abspath(path)))
        print("  id {0}   {1}   [{2}]".format(
            removed.note_id, removed.date, removed.author))
        print("      {0}".format(removed.text))
        print("\nGone from What's new now. If that was the wrong one, add "
              "it back with --text (and --date if it was not today's).")
        return 0

    if args.edit is not None:
        try:
            updated = testboard.site_notes.edit(
                path, args.edit, text=args.text, date=args.date)
        except ValueError as exc:
            sys.stderr.write("{0}\n".format(exc))
            return 2
        if updated is None:
            sys.stderr.write(
                "No note with id {0} in {1}. Run --list to see the ids.\n"
                .format(args.edit, os.path.abspath(path)))
            return 2
        print("Corrected in {0}:".format(os.path.abspath(path)))
        print("  id {0}   {1}   [{2}]".format(
            updated.note_id, updated.date, updated.author))
        print("      {0}".format(updated.text))
        print("\nLive now -- the server re-reads this file per request.")
        return 0

    date = args.date
    if not date:
        # UTC, to agree with every other date the dashboard shows. Local
        # time would file an evening note under tomorrow's drop.
        date = testboard.model.utcnow().strftime("%Y-%m-%d")

    author = args.author if args.author else _default_author()

    try:
        note = testboard.site_notes.add(path, date, args.text, author)
    except ValueError as exc:
        sys.stderr.write("{0}\n".format(exc))
        return 2
    except Exception as exc:
        sys.stderr.write("Cannot write {0}: {1}\n".format(path, exc))
        if args.verbose:
            traceback.print_exc()
        return 2

    print("Added to {0}:".format(os.path.abspath(path)))
    print("  {0}  [{1}]".format(note.date, note.author))
    print("  {0}".format(note.text))
    print("\nIt is live now -- the server re-reads this file per request. "
          "It appears on What's new under {0}.".format(note.date))
    print("If the server was started with an explicit --site-notes PATH, "
          "make sure that is this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
