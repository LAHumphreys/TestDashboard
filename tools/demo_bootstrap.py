#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One command from a clean clone to a browsable testboard dashboard.

Seeds the database directly (no HTTP round-trip) with:

1. 45 days of deterministic simulated history for the ``linux-sim``
   environment (see :mod:`tools.generate_demo_data` for the personas),
2. a real run of this repository's own unittest suite recorded under the
   ``local-unittest`` environment (skip with ``--skip-self-tests``),
3. a couple of demo users, comments and an assignment on the simulated
   regression test, so the triage UI has content too,

then serves the dashboard::

    python3 tools/demo_bootstrap.py
    # -> open http://127.0.0.1:8000

Everything is idempotent: re-running upserts the same runs and never
duplicates comments or assignments. ``--no-serve`` seeds and exits (useful
for scripting and tests). Exit codes: 0 = success / clean Ctrl+C shutdown;
2 = startup failure (one-line actionable error).

NOTE: this file intentionally contains no f-strings and only type
COMMENTS (PEP 484 style), so it still *parses* under Python 2 — on RHEL 8
someone will inevitably type ``python`` (2.7) instead of ``python3``, and
they must get the clear version message from main() instead of a bare
SyntaxError. Imports of testboard modules (Python-3-only syntax) are
deferred until after the version check.
"""

import argparse
import datetime
import errno
import logging
import os
import sys

try:
    from typing import Any, List, Optional  # noqa: F401
except ImportError:  # pragma: no cover - Python 2: main() exits before use
    pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the demo bootstrap CLI."""
    parser = argparse.ArgumentParser(
        prog="demo_bootstrap.py",
        description=(
            "Seed a testboard database with demo data (simulated "
            "'linux-sim' history plus this repository's own test suite) "
            "and serve the dashboard."
        ),
    )
    parser.add_argument(
        "--db", default="testboard.db", metavar="PATH",
        help="SQLite database file (created if absent; default: %(default)s)")
    parser.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help="interface to bind (default: %(default)s)")
    parser.add_argument(
        "--port", type=int, default=8000, metavar="N",
        help="TCP port to listen on (default: %(default)s)")
    parser.add_argument(
        "--days", type=int, default=None, metavar="N",
        help="days of simulated history (default: 45)")
    parser.add_argument(
        "--seed", type=int, default=None, metavar="N",
        help="random seed for the simulated history (default: fixed)")
    parser.add_argument(
        "--scale-tests", type=int, default=0, metavar="N",
        help=("also seed N filler tests (mostly passing, with realistic "
              "sprinkles of regressions/flaky/stale) to preview the "
              "dashboard at production scale, e.g. 12000 (default: 0)"))
    parser.add_argument(
        "--skip-self-tests", action="store_true",
        help="do not run this repository's own unittest suite")
    parser.add_argument(
        "--no-serve", action="store_true",
        help="seed the database and exit instead of serving")
    return parser


def _seed_triage_content(storage, regression):
    # type: (Any, Any) -> None
    """Add demo users, comments and an assignment for *regression*.

    *regression* is the RunRecord of the simulated regression test (any
    day of it — only the identity triple is used). Idempotent: comments
    are only added while the test has none, and the assignment only when
    it has no current assignee, so re-running the bootstrap never piles
    up duplicates.
    """
    from testboard import model

    triple = (regression.environment, regression.script,
              regression.test_name)
    now = model.utcnow()
    storage.ensure_user("alice", now)
    storage.ensure_user("bob", now)
    if not storage.comments(*triple):
        storage.add_comment(
            triple[0], triple[1], triple[2], "bob",
            "Started failing after the weekend gateway deploy — "
            "bisecting the replace-request path.",
            now - datetime.timedelta(days=6),
        )
        storage.add_comment(
            triple[0], triple[1], triple[2], "alice",
            "Reproduced locally: the partial fill is applied twice when "
            "the replace ack races the fill. Fix in review.",
            now - datetime.timedelta(days=1),
        )
    if storage.current_assignee(*triple) is None:
        storage.set_assignee(
            triple[0], triple[1], triple[2], "alice", "bob",
            now - datetime.timedelta(days=6),
        )


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    """Run the demo bootstrap; returns the process exit code (0/2)."""
    if sys.version_info < (3, 6):
        sys.stderr.write(
            "testboard requires Python 3.6+ — you are running {0}.{1}.{2}. "
            "Re-run with: python3 tools/demo_bootstrap.py\n".format(
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Imports deliberately happen only after the version check above:
    # these modules use Python-3-only syntax.
    import testboard.server
    import testboard.storage
    from tools import generate_demo_data
    from tools import run_self_tests

    static_dir = os.path.join(_REPO_ROOT, "static")
    if not os.path.isdir(static_dir):
        sys.stderr.write(
            "Static directory not found: {0}. Clone the repository "
            "intact — the demo needs the static/ frontend folder.\n".format(
                static_dir))
        return 2

    try:
        storage = testboard.storage.Storage(args.db)
    except Exception as exc:
        sys.stderr.write(
            "Cannot open the database at {0}: {1}. Check that the "
            "directory exists and is writable, or pick another location "
            "with: python3 tools/demo_bootstrap.py --db "
            "/path/to/testboard.db\n".format(os.path.abspath(args.db), exc))
        return 2

    try:
        gen_kwargs = {}
        if args.days is not None:
            gen_kwargs["days"] = args.days
        if args.seed is not None:
            gen_kwargs["seed"] = args.seed
        try:
            demo_runs = generate_demo_data.generate_runs(**gen_kwargs)
        except ValueError as exc:
            sys.stderr.write("error: {0}\n".format(exc))
            return 2
        counts = storage.upsert_runs(demo_runs)
        print(
            "Seeded {0} simulated runs (environment '{1}', {2} inserted, "
            "{3} updated, {4} unchanged) into {5}".format(
                len(demo_runs), generate_demo_data.ENVIRONMENT,
                counts.inserted, counts.updated, counts.unchanged,
                args.db))

        if args.scale_tests:
            print(
                "Seeding {0} filler tests across {1} days (this can take "
                "a minute at production scale)...".format(
                    args.scale_tests, args.days
                    if args.days is not None else 45))
            sys.stdout.flush()
            filler_inserted = 0
            filler_updated = 0
            filler_unchanged = 0
            filler_kwargs = {}
            if args.days is not None:
                filler_kwargs["days"] = args.days
            if args.seed is not None:
                filler_kwargs["seed"] = args.seed
            try:
                for batch in generate_demo_data.generate_filler_batches(
                        args.scale_tests, **filler_kwargs):
                    batch_counts = storage.upsert_runs(batch)
                    filler_inserted += batch_counts.inserted
                    filler_updated += batch_counts.updated
                    filler_unchanged += batch_counts.unchanged
            except ValueError as exc:
                sys.stderr.write("error: {0}\n".format(exc))
                return 2
            print(
                "Seeded filler estate: {0} inserted, {1} updated, "
                "{2} unchanged".format(
                    filler_inserted, filler_updated, filler_unchanged))

        regression = None
        for rec in demo_runs:
            if rec.test_name == "test_partial_update_retry":
                regression = rec
                break
        if regression is not None:
            _seed_triage_content(storage, regression)
            print(
                "Seeded demo users, comments and an assignment on "
                "{0} / {1}".format(regression.script,
                                   regression.test_name))

        if not args.skip_self_tests:
            print(
                "Running this repository's own test suite (skip with "
                "--skip-self-tests)...")
            sys.stdout.flush()
            self_runs, summary = run_self_tests.collect_self_test_runs()
            self_counts = storage.upsert_runs(self_runs)
            print(run_self_tests.format_summary(summary))
            print(
                "Seeded {0} self-test runs (environment '{1}', {2} "
                "inserted, {3} updated, {4} unchanged)".format(
                    len(self_runs), run_self_tests.ENVIRONMENT,
                    self_counts.inserted, self_counts.updated,
                    self_counts.unchanged))

        if args.no_serve:
            print(
                "Database ready. Serve it with: python3 run_server.py "
                "--db {0} --port {1}".format(args.db, args.port))
            return 0

        try:
            server = testboard.server.create_server(
                args.host, args.port, storage, static_dir)
        except OSError as exc:
            # Windows reports an in-use port as WSAEADDRINUSE (10048) or —
            # when the other listener holds it exclusively — WSAEACCES
            # (10013); POSIX uses EADDRINUSE.
            in_use = (exc.errno == errno.EADDRINUSE
                      or getattr(exc, "winerror", 0) in (10048, 10013))
            if in_use:
                sys.stderr.write(
                    "Port {0} is already in use. Pick another port: "
                    "python3 tools/demo_bootstrap.py --port {1}\n".format(
                        args.port, args.port + 1))
            else:
                sys.stderr.write(
                    "Cannot bind to {0}:{1}: {2}. Check the --host "
                    "address (is it an address of this machine?) and that "
                    "the port is not restricted.\n".format(
                        args.host, args.port, exc))
            return 2

        actual_port = server.server_address[1]
        print(
            "testboard demo serving at http://{0}:{1}/ "
            "(Ctrl+C to stop)".format(args.host, actual_port))
        sys.stdout.flush()  # make the URL visible even when stdout is a pipe
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Shutting down.")
        finally:
            server.server_close()
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())
