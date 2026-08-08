#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feeder CLI: read test-run records and push them into a testboard dashboard.

Examples::

    python3 run_feeder.py --url http://127.0.0.1:8000 --mode backfill \
        --source "results/*.jsonl"
    python3 run_feeder.py --url http://127.0.0.1:8000 --mode catchup \
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
import os
import re
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

#: The two import modes. ``catchup`` resumes from the high-water mark;
#: ``daily`` is accepted as an alias for it because that is what the mode
#: was called first, and scheduled commands already say it. The name was
#: wrong: it invites the reading "import today's runs", which is neither
#: what it does nor what anyone wants - a machine that was off for a week
#: must catch the week up, and nothing should depend on the hour a cron
#: job happens to fire.
BACKFILL = "backfill"
CATCHUP = "catchup"
MODES = (CATCHUP, BACKFILL, "daily")

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

setting an import up
  Check a reader against its data. No server, no network, no config; this
  is the loop to work in while writing one:

      python3 run_feeder.py --check-reader \\
          --reader /opt/testboard/internal_reader.py:create_reader

  Import history once, reading nothing older than a date:

      python3 run_feeder.py --url http://dashboard-host:8000 \\
          --mode backfill --since 2026-01-01T00:00:00 \\
          --reader /opt/testboard/internal_reader.py:create_reader

  The scheduled import, with everything in a config file so the command
  does not depend on the directory it runs in:

      python3 run_feeder.py --config /etc/testboard/feeder.json

importing only part of a long history
  --since and --until bound start_time: inclusive below, EXCLUSIVE above,
  so adjacent windows tile a history exactly once - no overlap, no gap.
  A source with years in it does not have to arrive all at once, and
  usually should not: the recent data is the data anyone wants first.

      # the last 12 months, which is what makes the dashboard useful
      python3 run_feeder.py --config ... --mode backfill \\
          --since 2025-08-01T00:00:00

      # then fill older years in, a window at a time
      python3 run_feeder.py --config ... --mode backfill \\
          --since 2024-08-01T00:00:00 --until 2025-08-01T00:00:00

  Importing an older window after a newer one does not rewind the feed:
  the high-water mark records the newest run ever pushed and only moves
  forwards. Once the windows are in, schedule --mode catchup and it
  resumes from there.

the source is enormous
  --limit N stops after N records, so a reader can be exercised against a
  huge source in seconds rather than hours:

      python3 run_feeder.py --check-reader --reader PATH.py:create_reader \\
          --limit 1000 --show-records 3

  Then time one real window with --dry-run before committing to a full
  backfill. A long import prints a progress line every 30 seconds with a
  records-per-second rate; if that rate says the backfill would take
  days, the reader needs work rather than patience - see "make it
  efficient" in docs/FEEDER_BRIEF.md, above all honouring the `since`
  hint so a nightly run does not re-read the whole history.

  Batches are flushed at whichever comes first, --batch-size records or
  --max-batch-bytes of encoded data. The byte cap matters because
  captured test output varies by orders of magnitude: without it, a
  handful of tests that dump megabytes produce a request the server
  refuses.

finding out what is going on
  Is the dashboard reachable, and is it really a testboard? Needs only
  --url; writes nothing:

      python3 run_feeder.py --test-connection --url http://dashboard-host:8000

  How far have we pushed, and what would run next?

      python3 run_feeder.py --config /etc/testboard/feeder.json --status

  What exactly does the reader produce? Prints each record as the reader
  yielded it and as it would be transmitted:

      python3 run_feeder.py --check-reader --reader PATH.py:create_reader \\
          --show-records 3

  How many records are outstanding right now? Read everything and count
  it without sending any of it:

      python3 run_feeder.py --config /etc/testboard/feeder.json --dry-run

an import failed overnight
  Exit 1 means records were rejected or batches failed - the data that
  did get through is safely in. The log ends with the reasons grouped by
  rule, each with a count and one affected test to look up; only the
  first few of each distinct problem are logged in full, so a systematic
  fault reads as one line rather than six thousand.

  Batches the server never accepted are written to
  testboard_failed_batch_NNNN.json in --replay-dir, along with the exact
  curl command to send one again. Nothing is discarded. If the server
  went away mid-import the run stops after --max-consecutive-failures
  rather than writing one file per remaining batch.

  In every case the fix is the same: sort out what the log names, then
  run the same command again. Re-running is always safe.

re-importing after fixing a reader
  Importing the same run twice never duplicates it: the server upserts on
  (environment, script, test_name, start_time), so a corrected record
  REPLACES the wrong one that is already stored. Repairing bad data is
  therefore just importing it again.

      python3 run_feeder.py --config ... --mode backfill \\
          --since 2026-06-01T00:00:00        # re-do a known range

      python3 run_feeder.py --config ... --forget-state   # re-do everything

  --forget-state deletes the high-water mark, which is what otherwise
  stops catchup mode from looking back at runs it has already seen.

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

when the reader itself breaks
  A reader that crashes, or that returns nothing iterable, is reported
  separately from a bad record - with how many records it had already
  produced, which one was last, and its own traceback, printed whether or
  not --verbose is given. That traceback names the file and line to fix.
"""

#: Shown when the feeder is run with no arguments at all.
NO_ARGUMENTS = """\
run_feeder.py imports test results into a testboard dashboard. It needs to
know, at least, which dashboard and which mode:

    python3 run_feeder.py --url http://dashboard-host:8000 --mode catchup

Setting this up for the first time? The wizard asks for what it needs,
checks each answer, and writes a config file:

    python3 run_feeder.py --init

Writing the reader for your site? Check it on its own, with no server,
and print what it actually produces:

    python3 run_feeder.py --check-reader --reader PATH.py:create_reader \\
        --show-records 3

Just want to know whether you can reach the dashboard?

    python3 run_feeder.py --test-connection --url http://dashboard-host:8000

Run 'python3 run_feeder.py --help' for every option, worked examples, and
the rule about timestamps.
"""


def compute_since(mode, hwm, since_arg, overlap_days):
    # type: (str, Optional[datetime.datetime], Optional[datetime.datetime], int) -> Optional[datetime.datetime]
    """Return the lower bound (naive UTC) for this import, or None for all.

    - ``backfill``: exactly ``since_arg`` (which may be None = everything).
    - ``catchup``: ``hwm - overlap_days`` days; None when there is no saved
      high-water mark yet (the first catchup run imports everything).
      ``since_arg`` is ignored in catchup mode. The overlap is free
      because the server upserts on
      (environment, script, test_name, start_time).

    Note what catchup is NOT: it has nothing to do with today's date, and
    no part of it depends on when in the day it runs. It resumes from the
    newest run previously accepted, so a machine that was off for a week
    catches up the week.
    """
    if mode == BACKFILL:
        return since_arg
    if hwm is None:
        return None
    return hwm - datetime.timedelta(days=overlap_days)


def stream_state_path(base_path, branch, build):
    # type: (str, Optional[str], Optional[str]) -> str
    """The high-water-mark file for one invocation (WP-21, docs/STREAMS_PLAN.md
    section 3.7).

    Mainline (neither --branch nor --build given) uses ``base_path``
    unchanged - every existing deployment keeps its current state file.
    A --branch/--build invocation gets its OWN file, derived from
    ``base_path``: without this, a branch catchup run would read and
    advance the SAME high-water mark as the mainline nightly, either
    fast-forwarding mainline past runs it has never actually seen or
    making the branch skip runs mainline already claimed.

    Naming: ``<base>.<kind>.<sanitized-name><ext>``, e.g.
    ``feeder_state.json`` + ``--branch feature/foo`` becomes
    ``feeder_state.branch.feature-foo.json``. The name is sanitized to
    filesystem-safe characters (``[A-Za-z0-9_.-]``, everything else
    collapsed to ``-``) because branch names commonly contain ``/``.
    """
    if branch is None and build is None:
        return base_path
    kind = "branch" if branch is not None else "build"
    name = branch if branch is not None else build
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "unnamed"
    root, ext = os.path.splitext(base_path)
    return "{0}.{1}.{2}{3}".format(root, kind, safe, ext or ".json")


def stamped(records, branch, build):
    # type: (Any, Optional[str], Optional[str]) -> Any
    """Wrap a raw-record stream, stamping ``branch``/``build`` onto each one.

    WP-21: the reader is site-specific and knows nothing about CLI
    flags, so the stamp is applied here, between the reader and the
    submitter, on the raw dict (before ``model.parse_run_record`` ever
    sees it) — the same "wrap the stream" shape as :func:`limited`.
    Records that are not dicts pass through untouched; validation
    (which will reject them anyway) is the submitter's job, not this
    function's.
    """
    if branch is None and build is None:
        return records
    return _stamp(records, branch, build)


def _stamp(records, branch, build):
    # type: (Any, Optional[str], Optional[str]) -> Any
    """Generator behind stamped(); see there."""
    for raw in records:
        if isinstance(raw, dict):
            raw = dict(raw)
            if branch is not None:
                raw["branch"] = branch
            else:
                raw["build"] = build
        yield raw


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


def feeder_default_batch_bytes():
    # type: () -> int
    """The submitter's default batch byte ceiling, imported lazily."""
    try:
        import feeder.submitter
        return feeder.submitter.DEFAULT_MAX_BATCH_BYTES
    except Exception:  # pragma: no cover - only if the package is broken
        return 8 * 1024 * 1024


def describe_window(since, until, model):
    # type: (Optional[datetime.datetime], Optional[datetime.datetime], Any) -> str
    """Describe the start_time window this run will import."""
    if since is None and until is None:
        return "any start_time (no bounds given)"
    if until is None:
        return "start_time >= " + model.format_iso(since)
    if since is None:
        return "start_time < " + model.format_iso(until)
    return "start_time in [{0}, {1})".format(
        model.format_iso(since), model.format_iso(until))


def limited(records, limit, log):
    # type: (Any, Optional[int], logging.Logger) -> Any
    """Yield at most ``limit`` records, saying so when it cuts the stream.

    A silent cap would be indistinguishable from a source that ran out,
    which is the one thing a sampled run must not look like.
    """
    if limit is None:
        return records
    return _take(records, limit, log)


def _take(records, limit, log):
    # type: (Any, int, logging.Logger) -> Any
    """Generator behind limited(); see there."""
    count = 0
    for record in records:
        if count >= limit:
            log.warning(
                "stopping after %d records because --limit %d was given. "
                "This is a SAMPLE: there is more in the source that has "
                "not been looked at, and which records you got is "
                "whatever the reader happened to yield first",
                count, limit)
            return
        count += 1
        yield record


def report_reader_failure(exc, log):
    # type: (Any, logging.Logger) -> None
    """Report a broken reader: the diagnosis, then its own traceback.

    The traceback is printed unconditionally rather than behind
    --verbose. A reader that crashes is code someone has just written -
    often generated - and the file and line it died on is the single most
    useful fact available. Withholding it to keep the log tidy optimizes
    for the run that works.
    """
    log.error("%s", exc)
    text = getattr(exc, "traceback_text", "")
    if text:
        sys.stderr.write("\n" + text.rstrip() + "\n\n")
        sys.stderr.flush()


def forget_state(args, log, state_module):
    # type: (argparse.Namespace, logging.Logger, Any) -> int
    """Delete the high-water mark so the next daily run re-imports all.

    This is the repair path for a reader that has been producing wrong
    data. Re-importing does not duplicate anything - the server upserts
    on (environment, script, test_name, start_time) - so the corrected
    records overwrite the bad ones in place.
    """
    import os
    import testboard.model
    path = args.state_file
    hwm = state_module.load_high_water_mark(path)
    if not os.path.exists(path):
        log.info(
            "there is no state file at %s, so there is no high-water mark "
            "to forget - the next daily run already imports everything",
            os.path.abspath(path))
        return 0
    try:
        os.remove(path)
    except OSError as exc:
        log.error(
            "could not delete the state file %s (%s). Delete it by hand, "
            "or use --mode backfill --since <date> to re-import a range "
            "without touching it", os.path.abspath(path), exc)
        return 2
    if hwm is not None:
        log.info("forgot the high-water mark of %s",
                 testboard.model.format_iso(hwm))
    log.info(
        "deleted %s. The next daily run will import everything the reader "
        "offers, and the server will UPDATE the runs it already has rather "
        "than duplicate them - so this repairs bad data rather than "
        "doubling it. To re-import only part of the history instead, use "
        "--mode backfill --since <ISO>.", os.path.abspath(path))
    return 0


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
        if args.mode != BACKFILL:
            problem = preflight_module.check_writable_file(
                args.state_file, "the catchup-mode state file")
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
        "--mode", default=None, choices=list(MODES),
        help=("catchup: import everything since the newest run previously "
              "accepted (less --overlap-days). This is what you schedule; "
              "it resumes from where it got to, not from today's date, so "
              "a machine that was off for a week catches the week up. "
              "backfill: import history, bounded by --since/--until. "
              "'daily' is accepted as an alias for catchup. Required "
              "unless --check-reader is given"))
    parser.add_argument(
        "--status", action="store_true",
        help=("report how far the feed has got: the high-water mark and "
              "its age, what the dashboard already holds, and what a run "
              "now would cover. Sends nothing and reads no source data"))
    parser.add_argument(
        "--test-connection", action="store_true",
        help=("check this machine can reach the dashboard and that it is "
              "one: sends an empty test import that writes nothing, then "
              "reads it back. Needs only --url"))
    parser.add_argument(
        "--forget-state", action="store_true",
        help=("delete the high-water mark, so the next daily run imports "
              "everything again. Use after fixing a reader that has been "
              "producing wrong data - re-importing repairs the runs in "
              "place, because the server upserts"))
    parser.add_argument(
        "--check-reader", action="store_true",
        help=("check a reader on its own: load it, read every record, "
              "validate each one, and report what is wrong. Needs no "
              "server and no --url. Use this while writing a reader"))
    parser.add_argument(
        "--since", default=None, metavar="ISO",
        help=("backfill lower bound on start_time (inclusive), as UTC with "
              "no timezone suffix: 2026-07-01T00:00:00. Ignored in catchup "
              "mode"))
    parser.add_argument(
        "--until", default=None, metavar="ISO",
        help=("backfill upper bound on start_time, EXCLUSIVE, same format "
              "as --since. Adjacent windows therefore tile a history "
              "exactly once, with no overlap and no gap, which is how a "
              "large estate is brought in one manageable chunk at a time. "
              "Ignored in catchup mode"))
    parser.add_argument(
        "--branch", default=None, metavar="NAME",
        help=("stamp every record of this run as belonging to branch "
              "stream NAME instead of mainline (WP-21). Mutually "
              "exclusive with --build. REQUIRES the dashboard server to "
              "understand streams_seen in its /api/import response - an "
              "older server's silence is treated as a fatal error, "
              "before any high-water mark is saved, because it means "
              "these runs would otherwise land silently in mainline. "
              "Also keys a SEPARATE high-water-mark state file, so a "
              "branch catchup run never shares mainline's progress "
              "(see --state-file)"))
    parser.add_argument(
        "--build", default=None, metavar="NAME",
        help=("stamp every record of this run as belonging to build "
              "stream NAME instead of mainline (WP-21) - for RC/release "
              "builds, re-cut under the same name. Mutually exclusive "
              "with --branch; see it for the streams_seen requirement "
              "and the per-stream state file"))
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help=("stop after reading N records. For sizing up a reader "
              "against a large source without waiting for all of it; use "
              "with --check-reader or --dry-run. Not for real imports - "
              "which records you get is whatever the reader yields first"))
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
        "--max-batch-bytes", type=int,
        default=feeder_default_batch_bytes(), metavar="BYTES",
        help=("send a batch as soon as it reaches this encoded size, even "
              "if it holds fewer than --batch-size records (default: "
              "%(default)s). Captured test output varies by orders of "
              "magnitude, so a fixed record count alone gives batches that "
              "are sometimes enormous"))
    parser.add_argument(
        "--state-file", default="feeder_state.json", metavar="PATH",
        help=("daily-mode high-water-mark file; must be writable by "
              "whoever runs the feeder, so do not leave it inside a "
              "read-only checkout (default: %(default)s, i.e. in the "
              "current directory). With --branch/--build, this is the "
              "BASE path: the actual file used is derived from it (e.g. "
              "feeder_state.branch.feat-x.json), so a branch/build "
              "invocation never shares mainline's high-water mark"))
    parser.add_argument(
        "--replay-dir", default=".", metavar="DIR",
        help=("directory for testboard_failed_batch_NNNN.json replay "
              "files; must be writable (default: current directory)"))
    parser.add_argument(
        "--show-records", type=int, default=0, metavar="N",
        help=("print the first N records in full - as your reader yielded "
              "them and as they would be sent to the server - then carry "
              "on. Use with --check-reader or --dry-run to see exactly "
              "what the reader produces"))
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
    import feeder.status
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

    # --branch/--build: mutually exclusive, non-empty after stripping
    # (mirrors the server's own validation, WP-21 docs/STREAMS_PLAN.md
    # section 3.7). Validated and normalized BEFORE anything below reads
    # args.branch/args.build or args.state_file, so every later use -
    # forget-state, status, preflight, the state file itself - sees the
    # same, already-checked values.
    if args.branch is not None and args.build is not None:
        log.error(
            "--branch and --build are mutually exclusive: a run belongs "
            "to at most one stream")
        return 2
    if args.branch is not None:
        args.branch = args.branch.strip() or None
        if args.branch is None:
            log.error("--branch must not be empty or whitespace-only")
            return 2
    if args.build is not None:
        args.build = args.build.strip() or None
        if args.build is None:
            log.error("--build must not be empty or whitespace-only")
            return 2
    # The effective state file for THIS invocation - unchanged for a
    # mainline run, derived (and therefore distinct from mainline's) for
    # a --branch/--build one. Every later use of args.state_file already
    # gets this via the mutated namespace: forget_state(), --status,
    # run_preflight()'s writability check, and the hwm load/save below.
    args.state_file = stream_state_path(
        args.state_file, args.branch, args.build)

    # --url/--mode are required for a real import but meaningless when
    # only checking a reader, so they are validated here rather than by
    # argparse.
    if args.test_connection:
        if args.url is None:
            log.error(
                "--test-connection needs --url: the address of the machine "
                "running the dashboard, e.g. http://dashboard-host:8000")
            return 2
        lines = feeder.status.test_connection(args.url)
        for line in lines:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0 if lines[-1].startswith("OK") else 2

    if args.forget_state:
        return forget_state(args, log, feeder.state)

    if args.status:
        for line in feeder.status.describe(
            args.url, args.state_file, args.overlap_days, args.reader,
            args.config, args.mode,
        ):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0

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
    until = None  # type: Optional[datetime.datetime]
    for name, raw in (("--since", args.since), ("--until", args.until)):
        if raw is None:
            continue
        try:
            parsed = testboard.model.parse_iso(raw)
        except ValueError as exc:
            log.error(
                "bad %s value: %s. Expected ISO-8601 naive UTC like "
                "2026-07-01T00:00:00 (a bare date is not enough - write "
                "2026-07-01T00:00:00)", name, exc)
            return 2
        if name == "--since":
            since_arg = parsed
        else:
            until = parsed
    if since_arg is not None and until is not None and until <= since_arg:
        log.error(
            "--until %s is not after --since %s, so the window is empty "
            "and nothing could be imported. --until is exclusive: to "
            "import a single day, use --since <day>T00:00:00 --until "
            "<next day>T00:00:00",
            testboard.model.format_iso(until),
            testboard.model.format_iso(since_arg))
        return 2
    if until is not None and args.mode != BACKFILL:
        log.error(
            "--until only applies to --mode backfill. Catchup mode imports "
            "everything new since the last run, and an upper bound would "
            "silently stop it ever moving past that date")
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
            report = feeder.check.check_reader(
                feeder.reader.iter_records(reader, since_arg, until),
                max_records=args.limit, show=args.show_records)
        except feeder.reader.ReaderFailed as exc:
            report_reader_failure(exc, log)
            return 2
        feeder.check.log_report(report, log)
        if args.limit is not None and report.read >= args.limit:
            # A cap that does not announce itself reads as "this is all
            # the reader produced", which is the one conclusion a sampled
            # run must not support: 'reader OK' over 1000 of 4,000,000
            # records says almost nothing about the other 3,999,000.
            log.warning(
                "this was a SAMPLE: reading stopped at --limit %d, so "
                "anything wrong with records the reader would have "
                "produced later has not been looked at. Re-run without "
                "--limit before trusting a clean result",
                args.limit)
        return 0 if report.ok else 1

    # Before anything is read: a wrong URL, an unreachable dashboard or a
    # path this process cannot write. --dry-run sends nothing, so it needs
    # no dashboard, but its paths are still worth checking.
    if not args.skip_preflight:
        if not run_preflight(args, log, feeder.preflight):
            return 2

    if args.branch is not None:
        log.info(
            "stream: branch %r (state file %s) - the server's "
            "/api/import response MUST acknowledge this in "
            "streams_seen, or the run aborts before saving a "
            "high-water mark", args.branch, args.state_file)
    elif args.build is not None:
        log.info(
            "stream: build %r (state file %s) - the server's "
            "/api/import response MUST acknowledge this in "
            "streams_seen, or the run aborts before saving a "
            "high-water mark", args.build, args.state_file)

    hwm = None  # type: Optional[datetime.datetime]
    if args.mode != BACKFILL:
        hwm = feeder.state.load_high_water_mark(args.state_file)
    since = compute_since(args.mode, hwm, since_arg, args.overlap_days)
    log.info("importing runs with %s", describe_window(
        since, until, testboard.model))

    submitter = feeder.submitter.Submitter(
        args.url, batch_size=args.batch_size, replay_dir=args.replay_dir,
        max_consecutive_failures=args.max_consecutive_failures,
        max_batch_bytes=args.max_batch_bytes,
        branch=args.branch, build=args.build)
    try:
        stats = submitter.submit(
            limited(
                stamped(
                    feeder.reader.iter_records(reader, since, until),
                    args.branch, args.build),
                args.limit, log),
            dry_run=args.dry_run, since=since, until=until,
            show=args.show_records)
    except feeder.reader.ReaderFailed as exc:
        report_reader_failure(exc, log)
        return 1
    except feeder.submitter.StreamsAckMissing as exc:
        # A distinct, non-retryable class of failure: the SERVER does
        # not speak WP-21, not a transient network/data problem. No
        # high-water mark is saved (we return before that code runs) -
        # a normal --branch/--build re-run against a fixed server picks
        # up from wherever the state file already was.
        log.error("%s", exc)
        return 1
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
            if feeder.state.advance_high_water_mark(
                    args.state_file, new_hwm):
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
