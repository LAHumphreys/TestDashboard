#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the testboard dashboard server.

Examples::

    python3 run_server.py
    python3 run_server.py --port 8001 --db /var/lib/testboard/testboard.db

Serves the JSON API under /api and the static frontend from --static.
Exit codes: 0 = clean shutdown (Ctrl+C); 2 = startup failure (a one-line
actionable error is printed; re-run with --verbose for the traceback).

NOTE: this file intentionally contains no f-strings and only type COMMENTS
(PEP 484 style), so it still *parses* under Python 2 — on RHEL 8 someone
will inevitably type ``python run_server.py`` (2.7) instead of ``python3``,
and they must get the clear version message from main() instead of a bare
SyntaxError. Do not add f-strings or inline annotations to this file.
"""

import argparse
import errno
import logging
import os
import sys
import traceback

try:
    from typing import List, Optional  # noqa: F401 (used in type comments)
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the server CLI."""
    default_static = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static")
    parser = argparse.ArgumentParser(
        prog="run_server.py",
        description=(
            "Serve the testboard dashboard: JSON API under /api plus the "
            "static web UI, backed by a single SQLite file."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help=("interface to bind (default: %(default)s; use 0.0.0.0 to "
              "listen on all interfaces)"))
    parser.add_argument(
        "--port", type=int, default=8000, metavar="N",
        help="TCP port to listen on (default: %(default)s)")
    parser.add_argument(
        "--db", default="testboard.db", metavar="PATH",
        help="SQLite database file (created if absent; default: %(default)s)")
    parser.add_argument(
        "--static", default=default_static, metavar="DIR",
        help=("directory with the frontend files "
              "(default: the static/ folder next to run_server.py)"))
    parser.add_argument(
        "--cache-mb", type=int, default=None, metavar="MB",
        help=("page cache budget for the whole process, in MB. SQLite's "
              "default is 2 MB per connection, which against a database "
              "of a few hundred MB means nearly every read goes to the "
              "filesystem. That is invisible on local disk and expensive "
              "on a network mount. The budget is DIVIDED among "
              "connections, not given to each; measure the effect first "
              "with: python3 tools/diagnose_db.py --db PATH --cache-mb MB"))
    parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help=("worker threads serving requests (default: 8). Each holds "
              "one database connection for its whole life, so this is "
              "also the connection count and the number a --cache-mb "
              "budget is split between. Raise it if requests queue "
              "behind each other; lower it to give each connection a "
              "bigger share of the cache"))
    parser.add_argument(
        "--mmap-mb", type=int, default=None, metavar="MB",
        help=("map this much of the database instead of reading it, so "
              "the OS page cache serves pages with no copy. A large win "
              "on local disk; worth little or nothing on a network mount, "
              "where the pages are not local to cache. Off by default"))
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging plus full tracebacks on fatal errors")
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Run the dashboard server; returns the process exit code (0/2)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ — you are running {0}.{1}.{2}. "
            "Re-run with: python3 run_server.py\n".format(
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

    # Imports deliberately happen only after the version check above:
    # these modules use Python-3-only syntax.
    import testboard.server
    import testboard.storage

    if not os.path.isdir(args.static):
        sys.stderr.write(
            "Static directory not found: {0}. Expected the repository's "
            "static/ folder next to run_server.py — clone the repository "
            "intact, or point at it explicitly with --static DIR.\n".format(
                args.static))
        return 2

    try:
        storage = testboard.storage.Storage(
            args.db, cache_mb=args.cache_mb, mmap_mb=args.mmap_mb,
            max_connections=(
                args.workers if args.workers is not None
                else testboard.storage.DEFAULT_MAX_CONNECTIONS))
    except Exception as exc:
        sys.stderr.write("Cannot open the database:\n  {0}\n".format(
            testboard.storage.describe_open_error(args.db, exc)))
        if args.verbose:
            traceback.print_exc()
        return 2

    try:
        server = testboard.server.create_server(
            args.host, args.port, storage, args.static)
    except OSError as exc:
        storage.close()
        # Windows reports an in-use port as WSAEADDRINUSE (10048) or —
        # when the other listener holds it exclusively — WSAEACCES
        # (10013); POSIX uses EADDRINUSE.
        in_use = (exc.errno == errno.EADDRINUSE
                  or getattr(exc, "winerror", 0) in (10048, 10013))
        if in_use:
            sys.stderr.write(
                "Port {0} is already in use. Pick another port: "
                "python3 run_server.py --port {1}\n".format(
                    args.port, args.port + 1))
        else:
            sys.stderr.write(
                "Cannot bind to {0}:{1}: {2}. Check the --host address "
                "(is it an address of this machine?) and that the port is "
                "not restricted.\n".format(args.host, args.port, exc))
        if args.verbose:
            traceback.print_exc()
        return 2

    actual_port = server.server_address[1]
    print("testboard serving at http://{0}:{1}/ (Ctrl+C to stop)".format(
        args.host, actual_port))
    sys.stdout.flush()  # make the URL visible even when stdout is a pipe
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down.")
    finally:
        server.server_close()
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
