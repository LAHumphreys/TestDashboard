#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the testboard dashboard server.

Examples::

    python3 run_server.py
    python3 run_server.py --port 8001 --db /var/lib/testboard/testboard.db
    python3 run_server.py --db-config /etc/testboard/db.cnf \
        --site-notes /var/lib/testboard/site_notes.json

Serves the JSON API under /api and the static frontend from --static.
Two equal backends, chosen at start: ``--db PATH`` is SQLite (the
zero-setup default — nothing to install, nothing to configure) and
``--db-config CNF`` is MariaDB via the credentials file the runbook's
§A.10 writes. Exactly one of the two.
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
            "static web UI. Backed by a single SQLite file (--db, the "
            "zero-setup default) or by MariaDB (--db-config) — one or "
            "the other, never both."
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
        "--db", default=None, metavar="PATH",
        help=("SQLite database file (created if absent; default: "
              "testboard.db). Mutually exclusive with --db-config"))
    parser.add_argument(
        "--db-config", default=None, metavar="CNF",
        help=("serve from MariaDB instead of SQLite, connecting with the "
              "credentials in CNF — a mysql option file, normally "
              "/etc/testboard/db.cnf (docs/MARIADB_MIGRATION.md, A.10). "
              "The schema must already have been loaded by the migration "
              "tooling; the server verifies its version and refuses a "
              "mismatch rather than migrating. Requires an explicit "
              "--site-notes (there is no database file to keep it "
              "beside). SQLite-only tuning flags (--cache-mb, --mmap-mb) "
              "are rejected rather than ignored"))
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
        "--perf-log", default=None, metavar="PATH",
        help=("append one timing record per request and per storage call "
              "to PATH, as newline-delimited JSON, for reading back later "
              "with: python3 tools/perf_report.py PATH. OFF unless given, "
              "so an intermittent stall has to be caught with this "
              "already running. The file is capped and rolled over, so "
              "leaving it on is safe"))
    parser.add_argument(
        "--perf-max-mb", type=int, default=None, metavar="MB",
        help=("roll --perf-log over at this size (default: 128). At most "
              "twice this is ever on disk: the live file and one "
              "predecessor"))
    parser.add_argument(
        "--site-notes", default=None, metavar="PATH",
        help=("JSON file of this SITE's own What's new notes, shown on the "
              "What's new page beside testboard's release notes (add one "
              "with: python3 tools/add_site_note.py). Default: "
              "site_notes.json beside --db, which is outside the "
              "repository so a deployment cannot overwrite it. Read per "
              "request, so a new note needs no restart"))
    parser.add_argument(
        "--url-prefix", default="testboard", metavar="PREFIX",
        help=("serve behind this path prefix, in ADDITION to the bare "
              "paths (/api/..., /index.html), which always keep working "
              "regardless of this flag -- for an nginx proxy that does "
              "NOT strip the prefix before forwarding (the tested shape: "
              "a 'location /PREFIX/ { proxy_pass http://127.0.0.1:PORT; }' "
              "block, no trailing slash on proxy_pass). Because bare "
              "paths always work too, the default is harmless wherever "
              "nginx is absent (dev, staging, a feeder posting straight "
              "to the backend port -- feeders should always use the bare "
              "path on the direct backend port, never this prefix). Give "
              "'' to disable prefix handling entirely, so only bare "
              "paths are served"))
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging plus full tracebacks on fatal errors")
    return parser


def backend_error(args):
    # type: (argparse.Namespace) -> Optional[str]
    """The reason this flag combination cannot start, or None.

    Rejections, not silent choices: a --cache-mb quietly ignored under
    --db-config would look applied, and a guessed site-notes location
    would look empty — both are misconfigurations that should say so at
    startup, when they cost one command to fix.
    """
    if not args.db_config:
        return None
    if args.db is not None:
        return ("--db and --db-config choose different backends; give "
                "exactly one. (--db PATH is SQLite; --db-config CNF is "
                "MariaDB.)")
    if args.site_notes is None:
        return ("--db-config needs an explicit --site-notes PATH: the "
                "default location is 'beside the --db file' and a MariaDB "
                "server has no such file. Somewhere the service account "
                "can write, e.g. /var/lib/testboard/site_notes.json.")
    if args.cache_mb is not None or args.mmap_mb is not None:
        return ("--cache-mb and --mmap-mb tune SQLite's per-connection "
                "page cache and do not apply to MariaDB (the buffer pool "
                "is server-side: innodb_buffer_pool_size, runbook A.5/"
                "B.4). Remove the flag rather than believing it took "
                "effect.")
    return None


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

    problem = backend_error(args)
    if problem is not None:
        sys.stderr.write(problem + "\n")
        return 2

    workers = (args.workers if args.workers is not None
               else testboard.storage.DEFAULT_MAX_CONNECTIONS)

    if args.db_config:
        import testboard.dbconfig
        import testboard.mariadb
        try:
            settings = testboard.dbconfig.read_option_file(args.db_config)
        except testboard.dbconfig.DbConfigError as exc:
            sys.stderr.write("Cannot read --db-config:\n  {0}\n".format(exc))
            return 2
        print("database: MariaDB, {0}".format(settings.describe()))
        try:
            storage = testboard.storage.Storage.mariadb(
                settings, max_connections=workers)
        except Exception as exc:
            # The backend's own refusals (schema version, strict mode)
            # are already actionable prose; only genuine connect
            # failures get the host-matching / auth-plugin advice.
            if isinstance(exc, RuntimeError):
                message = str(exc)
            else:
                message = testboard.mariadb.describe_connect_error(
                    settings, exc)
            sys.stderr.write(
                "Cannot open the database:\n  {0}\n".format(message))
            if args.verbose:
                traceback.print_exc()
            return 2
    else:
        if args.db is None:
            args.db = "testboard.db"
        try:
            storage = testboard.storage.Storage(
                args.db, cache_mb=args.cache_mb, mmap_mb=args.mmap_mb,
                max_connections=workers)
        except Exception as exc:
            sys.stderr.write("Cannot open the database:\n  {0}\n".format(
                testboard.storage.describe_open_error(args.db, exc)))
            if args.verbose:
                traceback.print_exc()
            return 2

    perf_log = None
    if args.perf_log:
        import testboard.perf
        max_bytes = (
            testboard.perf.DEFAULT_MAX_BYTES if args.perf_max_mb is None
            else max(1, args.perf_max_mb) * 1024 * 1024)
        try:
            perf_log = testboard.perf.PerfLog(
                args.perf_log, max_bytes=max_bytes)
        except Exception as exc:
            storage.close()
            sys.stderr.write(
                "Cannot open the performance log {0}: {1}\n".format(
                    args.perf_log, exc))
            if args.verbose:
                traceback.print_exc()
            return 2
        wrapped = testboard.perf.instrument_storage(storage, perf_log)
        print("performance log: {0} ({1} storage methods timed, rolling "
              "at {2} MB)".format(
                  os.path.abspath(args.perf_log), len(wrapped),
                  max_bytes // (1024 * 1024)))

    import testboard.site_notes
    site_notes_path = (
        args.site_notes if args.site_notes
        else testboard.site_notes.default_path(args.db))
    notes, notes_problem = testboard.site_notes.load(site_notes_path)
    # Say the resolved path and what was found there. A notes file that is
    # silently in the wrong place looks exactly like a site that has not
    # written any, and someone would go looking in the code for the bug.
    print("site notes: {0} ({1})".format(
        os.path.abspath(site_notes_path),
        notes_problem if notes_problem
        else "{0} note(s)".format(len(notes))))

    url_prefix = args.url_prefix.strip("/")
    print("url prefix: {0}".format(
        "/" + url_prefix + " (and bare paths, always)" if url_prefix
        else "(disabled -- only bare paths are served)"))

    try:
        server = testboard.server.create_server(
            args.host, args.port, storage, args.static, perf=perf_log,
            site_notes_path=site_notes_path, url_prefix=url_prefix)
    except OSError as exc:
        storage.close()
        if perf_log is not None:
            perf_log.close()
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
        if perf_log is not None:
            perf_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
