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

#: Printed after the option list by --help, and on its own when the feeder
#: is run with no arguments at all. It carries the two things the option
#: list cannot: complete commands to copy, and the timezone rule, which is
#: the one mistake that produces no error at all.
EPILOG = """\
first time here
  Run the setup wizard. It asks for the dashboard, the reader and the
  paths, checks each answer against the real thing as you give it, writes
  a config file, and prints the scheduled-task line to copy:

      python3 run_feeder.py --init

examples
  Check a reader against its data. No server, no network, no config; this
  is the loop to work in while writing one:

      python3 run_feeder.py --check-reader \\
          --reader /opt/testboard/internal_reader.py:create_reader

  Import history once, reading nothing older than a date:

      python3 run_feeder.py --url http://dashboard-host:8000 \\
          --mode backfill --since 2026-01-01T00:00:00 \\
          --reader /opt/testboard/internal_reader.py:create_reader

  The nightly import, with everything in a config file so the command
  does not depend on the directory it runs in:

      python3 run_feeder.py --config /etc/testboard/feeder.json

timestamps are UTC
  Every time the feeder sends is UTC, written with no timezone suffix:
  2026-07-25T02:14:07.000000. So is --since.

  If your test system records local time, converting it is the reader's
  job. Nothing downstream can tell the difference: the records validate,
  import cleanly, and put every run in the wrong hour - which silently
  shifts "failing since", the day-of-week profile and the trend. Running
  --check-reader compares the newest record with UTC now and with this
  machine's own offset, and says so when they look like local time.

the reader
  The reader is the only site-specific code. Give it as PATH:FUNCTION -
  the path to a .py file anywhere on this machine, and the function in it
  that builds the reader. A dotted module name also works, but is only
  found on Python's import path, which does not include the directory you
  happen to be standing in. See docs/FEEDER_BRIEF.md.

exit codes
  0  every valid record was accepted
  1  some records were rejected, or some batches failed - see the replay
     files named in the log; re-running is always safe, the server upserts
  2  the run never started: bad arguments, an unreachable dashboard, a
     reader that would not load, or an unwritable path
"""

#: Shown when the feeder is run with no arguments at all.
NO_ARGUMENTS = """\
run_feeder.py imports test results into a testboard dashboard. It needs to
know, at least, which dashboard and which mode:

    python3 run_feeder.py --url http://dashboard-host:8000 --mode daily

Setting this up for the first time? The wizard asks for what it needs,
checks each answer, and writes a config file:

    python3 run_feeder.py --init

Writing the reader for your site? Check it on its own, with no server:

    python3 run_feeder.py --check-reader --reader PATH.py:create_reader

Run 'python3 run_feeder.py --help' for every option, worked examples, and
the rule about timestamps.
"""


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


def feeder_version():
    # type: () -> str
    """Return the feeder version, or a placeholder if it cannot be read.

    Imported lazily: this module is parsed by Python 2 on RHEL 8 often
    enough that nothing outside main() may depend on a Python 3 import.
    """
    try:
        import feeder
        return feeder.__version__
    except Exception:  # pragma: no cover - only if the package is broken
        return "unknown"


def load_config_settings(argv, parser, config_module):
    # type: (Optional[List[str]], argparse.ArgumentParser, Any) -> dict
    """Read --config (if given) and install its values as parser defaults.

    Returns the settings the file supplied, so the caller can report them
    and apply the one that cannot be a default (``source``). Raises
    ``config_module.ConfigError`` when the file is unusable.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre.add_argument("--init", action="store_true")
    try:
        known, _ = pre.parse_known_args(argv)
    except SystemExit:
        # A malformed --config is reported properly by the real parse.
        return {}
    # 'run --init and write it to here' is the whole point of combining the
    # two, so the file must not have to exist yet.
    if known.config is None or known.init:
        return {}
    settings = config_module.load_config(known.config)
    config_module.apply_to_parser(parser, settings)
    return settings


def run_preflight(args, log, preflight_module):
    # type: (argparse.Namespace, logging.Logger, Any) -> bool
    """Check the URL and the writable paths; True when it is safe to go.

    Every problem found here would otherwise surface only after the
    reader had been run over the whole estate - and in the case of the
    state file, only after a successful import. Each is one cheap check.
    """
    problems = []  # type: List[str]
    problem = preflight_module.check_url(args.url)
    if problem is not None:
        problems.append(problem)
    if not args.dry_run:
        # A dry run writes no replay files and saves no mark, so an
        # unwritable path is not its problem.
        problem = preflight_module.check_writable_directory(
            args.replay_dir, "failed-batch replay files")
        if problem is not None:
            problems.append(problem)
        if args.mode == "daily":
            problem = preflight_module.check_writable_file(
                args.state_file, "the daily-mode state file")
            if problem is not None:
                problems.append(problem)
    if not problems and not args.dry_run:
        problem = preflight_module.probe_dashboard(args.url)
        if problem is not None:
            problems.append(problem)
    if not problems:
        if args.dry_run:
            log.info("preflight: --url is well-formed (dry run: the "
                     "dashboard was not contacted)")
        else:
            log.info("preflight OK: dashboard reachable, paths writable")
        return True
    for problem in problems:
        log.error("%s", problem)
    log.error(
        "stopping before reading anything, because none of the above gets "
        "better once the import has started. Fix them and re-run, or pass "
        "--skip-preflight to try anyway")
    return False


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argument parser for the feeder CLI."""
    parser = argparse.ArgumentParser(
        prog="run_feeder.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Read test-run records via a pluggable reader and push them to "
            "a testboard dashboard (POST /api/import). Safe to re-run: the "
            "server upserts, duplicates are impossible."
        ),
        epilog=EPILOG,
    )
    parser.add_argument(
        "--version", action="version",
        version="testboard feeder " + feeder_version(),
        help="print the feeder version and exit")
    parser.add_argument(
        "--init", action="store_true",
        help=("interactive setup: asks for the dashboard, reader and "
              "paths, CHECKS each answer against the real thing, writes a "
              "config file and prints the scheduled-task line. Start here"))
    parser.add_argument(
        "--config", default=None, metavar="PATH",
        help=("read settings from a JSON config file (see --init). Any "
              "flag given on the command line overrides the file"))
    parser.add_argument(
        "--url", default=None,
        help=("dashboard base URL, e.g. http://127.0.0.1:8000 "
              "(/api/import is appended automatically). This is the "
              "machine running run_server.py, which need not be this one. "
              "Required unless --check-reader is given"))
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
        help=("backfill lower bound on start_time, as UTC with no timezone "
              "suffix: 2026-07-01T00:00:00. Ignored in daily mode"))
    parser.add_argument(
        "--reader", default="jsonl", metavar="SPEC",
        help=("where records come from: 'jsonl' for the built-in "
              "JSON-lines reader, or 'PATH.py:factory' naming your site's "
              "reader file and the function in it that builds one "
              "(a dotted 'module.path:factory' also works). "
              "Default: %(default)s"))
    parser.add_argument(
        "--source", action="append", default=None, metavar="PATH_OR_GLOB",
        help=("what the reader should read: a file, a glob such as "
              "'results/*.jsonl', or a directory to search. May be "
              "repeated. Quote globs so the shell does not expand them"))
    parser.add_argument(
        "--batch-size", type=int, default=500, metavar="N",
        help="records per POST batch (default: %(default)s)")
    parser.add_argument(
        "--state-file", default="feeder_state.json", metavar="PATH",
        help=("daily-mode high-water-mark file; must be writable by "
              "whoever runs the feeder, so do not leave it inside a "
              "read-only checkout (default: %(default)s, i.e. in the "
              "current directory)"))
    parser.add_argument(
        "--replay-dir", default=".", metavar="DIR",
        help=("directory for testboard_failed_batch_NNNN.json replay "
              "files; must be writable (default: current directory)"))
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help=("do not check the dashboard and the writable paths before "
              "importing. The checks cost a second and turn most setup "
              "mistakes into one sentence, so skip them only if something "
              "in front of the dashboard rejects the empty test request"))
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

    # Imports deliberately happen only after the version check above:
    # these modules use Python-3-only syntax.
    import testboard.model
    import feeder.check
    import feeder.config
    import feeder.init
    import feeder.preflight
    import feeder.reader
    import feeder.state
    import feeder.submitter

    parser = build_parser()

    # Nothing at all on the command line is the first-run case, and the
    # one that most deserves an answer. argparse would say only that
    # --url is missing.
    if not (argv if argv is not None else sys.argv[1:]):
        parser.print_usage(sys.stderr)
        sys.stderr.write("\n" + NO_ARGUMENTS)
        return 2

    # A config file supplies defaults, so it has to be read before the
    # real parse. set_defaults is the right hook: argparse only falls
    # back to a default when the flag was absent, so the command line
    # still wins.
    config_settings = {}
    try:
        config_settings = load_config_settings(argv, parser, feeder.config)
    except feeder.config.ConfigError as exc:
        sys.stderr.write("run_feeder.py: {0}\n".format(exc))
        return 2

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2

    # --source is an 'append' option, so seeding it as a default would
    # add to the command line rather than replace it. Applied here.
    if args.source is None and "source" in config_settings:
        args.source = list(config_settings["source"])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("run_feeder")

    if args.init:
        return feeder.init.run_init(
            sys.stdout, sys.stdin, config_path=args.config)

    if args.config is not None:
        log.info("read settings from %s: %s", args.config,
                 ", ".join(sorted(config_settings)) or "(nothing set)")

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
                "%s required. --url is the address of the machine running "
                "the dashboard and --mode is 'daily' or 'backfill'. To set "
                "these up once and for all, run: python3 run_feeder.py "
                "--init. To check a reader on its own, with no server, use "
                "--check-reader",
                " and ".join(missing) + (
                    " are" if len(missing) > 1 else " is"))
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

    # Before anything is read: a wrong URL, an unreachable dashboard or a
    # path this process cannot write. --dry-run sends nothing, so it needs
    # no dashboard, but its paths are still worth checking.
    if not args.skip_preflight:
        if not run_preflight(args, log, feeder.preflight):
            return 2

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
            # Failure here is reported and survived, not raised: the data
            # is already in the dashboard, and a traceback would turn a
            # successful import into a failed scheduled task.
            if feeder.state.save_high_water_mark(args.state_file, new_hwm):
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
