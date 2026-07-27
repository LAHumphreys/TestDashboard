"""Answering "where are we?" without running an import.

Two questions come up constantly once a feed is scheduled, and neither
had an answer that did not involve reading a JSON file by hand or
starting an import and watching what happened:

- *Can this machine talk to that dashboard?* — :func:`test_connection`,
  which needs nothing but a URL.
- *How far have we got, and what is outstanding?* — :func:`describe`,
  which puts the local high-water mark, what the dashboard already holds,
  and what a run right now would cover, side by side.

Both return plain lines rather than logging, because both are things a
person asked for and expects to read, not events in a log.

Neither reads the source system. That is deliberate: the reader may be an
hour of work to run, and a status command that takes an hour is a status
command nobody runs. What is genuinely outstanding is answered by
``--dry-run``, and the report says so.

Python 3.6 compatible; standard library only.
"""

import datetime
import os
from typing import Any, Dict, List, Optional

from feeder import preflight, state
from feeder.preflight import Getter
from feeder.submitter import Opener, normalize_url
from testboard import model

#: What the dashboard is asked for. ``limit=1`` on the newest-first page
#: is the cheapest way to learn the newest run it holds.
_SUMMARY_PATH = "/api/summary"
_NEWEST_PATH = "/api/dashboard?sort=start_time&order=desc&limit=1"


def describe_gap(then: datetime.datetime, now: datetime.datetime) -> str:
    """Render the distance between two times as ``2d 3h ago``."""
    seconds = (now - then).total_seconds()
    suffix = "ago" if seconds >= 0 else "in the FUTURE"
    seconds = abs(seconds)
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        size = "{0}d {1}h".format(days, hours)
    elif hours:
        size = "{0}h {1:02d}m".format(hours, minutes)
    else:
        size = "{0}m".format(minutes)
    return size + " " + suffix


def test_connection(
    url: str, opener: Optional[Opener] = None,
    getter: Optional[Getter] = None,
) -> List[str]:
    """Check a dashboard end to end; return report lines.

    The last line always begins ``OK`` or ``FAILED`` so a caller can
    decide the exit code without re-deriving the verdict.

    Both directions are exercised. The import path is what the feeder
    uses, and is checked with an empty import that writes nothing. The
    read path is checked too, because a dashboard that accepts writes but
    cannot serve them is still broken from the point of view of whoever
    is going to look at the results, and finding that out here is much
    cheaper than after the first backfill.
    """
    lines = ["connection test: " + url]
    problem = preflight.check_url(url)
    if problem is not None:
        lines.append("  URL          FAILED  " + problem)
        lines.append("FAILED - nothing was contacted; --url is not a URL.")
        return lines
    lines.append("  URL          OK      posts will go to " +
                 normalize_url(url))

    problem = preflight.probe_dashboard(url, opener)
    if problem is not None:
        lines.append("  import path  FAILED  " + problem)
        lines.append(
            "FAILED - the feeder cannot deliver to this dashboard.")
        return lines
    lines.append(
        "  import path  OK      an empty test import was accepted "
        "(nothing was written)")

    summary = preflight.fetch_json(url, _SUMMARY_PATH, getter)
    if not isinstance(summary, dict):
        lines.append(
            "  read path    WARN    the dashboard accepts imports but did "
            "not answer " + _SUMMARY_PATH + ". Imports will work; the web "
            "UI may not.")
        lines.append("OK - imports will reach this dashboard.")
        return lines
    lines.append("  read path    OK      " + _describe_estate(summary))

    newest = _newest_run(url, getter)
    if newest is not None:
        lines.append("  newest run   {0}  ({1})".format(
            model.format_iso(newest),
            describe_gap(newest, model.utcnow())))
    else:
        lines.append(
            "  newest run   none yet - this dashboard holds no runs")
    lines.append("OK - this is a testboard dashboard and the feeder can "
                 "reach it.")
    return lines


def describe(
    url: Optional[str], state_file: str, overlap_days: int,
    reader_spec: str, config_path: Optional[str],
    mode: Optional[str] = None,
    opener: Optional[Opener] = None, getter: Optional[Getter] = None,
    now: Optional[datetime.datetime] = None,
) -> List[str]:
    """Report how far the feed has got and what a run now would cover."""
    if now is None:
        now = model.utcnow()
    lines = ["feeder status"]
    if config_path:
        lines.append("  config        " + os.path.abspath(config_path))
    lines.append("  reader        " + reader_spec)
    if mode:
        lines.append("  mode          " + mode)

    hwm = state.load_high_water_mark(state_file)
    lines.append("  state file    " + os.path.abspath(state_file))
    if hwm is None:
        lines.extend(_no_mark_lines(state_file))
    else:
        lines.append("  pushed up to  {0}  ({1})".format(
            model.format_iso(hwm), describe_gap(hwm, now)))
        floor = hwm - datetime.timedelta(days=overlap_days)
        lines.append(
            "                a daily run now would import every run at or "
            "after")
        lines.append(
            "                {0} - the mark, less --overlap-days "
            "{1}.".format(model.format_iso(floor), overlap_days))

    if url:
        lines.extend(_dashboard_lines(url, now, opener, getter))
    lines.append("")
    lines.append(
        "  This reports the marks, not the source system - reading a year "
        "of history to")
    lines.append(
        "  answer a status question would take as long as the import. To "
        "count what is")
    lines.append(
        "  actually outstanding without sending it, add --dry-run to your "
        "normal command.")
    return lines


def _no_mark_lines(state_file: str) -> List[str]:
    """Explain an absent high-water mark, which is not necessarily wrong."""
    if os.path.exists(state_file):
        return [
            "  pushed up to  UNKNOWN - the state file exists but could not "
            "be read",
            "                (see the warning above). The next daily run "
            "will import",
            "                everything, which is safe: the server "
            "upserts.",
        ]
    return [
        "  pushed up to  nothing yet - no state file, so nothing has been "
        "recorded",
        "                as pushed. The next daily run imports everything "
        "the reader",
        "                offers. This is also what you see if the feed has "
        "only ever",
        "                been run with --mode backfill --dry-run.",
    ]


def _dashboard_lines(
    url: str, now: datetime.datetime,
    opener: Optional[Opener], getter: Optional[Getter],
) -> List[str]:
    """Report what the dashboard itself holds, or why that is unknown."""
    problem = preflight.check_url(url)
    if problem is None:
        problem = preflight.probe_dashboard(url, opener)
    if problem is not None:
        return [
            "  dashboard     " + url,
            "                NOT REACHABLE: " + problem,
        ]
    lines = ["  dashboard     " + url + "  (reachable)"]
    summary = preflight.fetch_json(url, _SUMMARY_PATH, getter)
    if isinstance(summary, dict):
        lines.append("                " + _describe_estate(summary))
    newest = _newest_run(url, getter)
    if newest is not None:
        lines.append("  holds runs to {0}  ({1})".format(
            model.format_iso(newest), describe_gap(newest, now)))
    else:
        lines.append("  holds runs to nothing - this dashboard is empty")
    return lines


def _describe_estate(summary: Dict[str, Any]) -> str:
    """One line about the size and freshness of what the dashboard holds."""
    status = summary.get("status")
    if not isinstance(status, dict):
        return "the dashboard answered, but not with a status summary"
    total = status.get("total_tests")
    recent = status.get("ran_recently")
    stale = status.get("not_run")
    parts = []  # type: List[str]
    if isinstance(total, int):
        parts.append("{0} test(s) known".format(total))
    if isinstance(recent, int):
        parts.append("{0} ran in the last {1}h".format(
            recent, summary.get("recent_hours", 36)))
    if isinstance(stale, int) and stale:
        parts.append("{0} have not".format(stale))
    return ", ".join(parts) if parts else "the dashboard holds no tests yet"


def _newest_run(
    url: str, getter: Optional[Getter]
) -> Optional[datetime.datetime]:
    """The newest ``start_time`` the dashboard holds, or None."""
    page = preflight.fetch_json(url, _NEWEST_PATH, getter)
    if not isinstance(page, dict):
        return None
    tests = page.get("tests")
    if not isinstance(tests, list) or not tests:
        return None
    first = tests[0]
    if not isinstance(first, dict):
        return None
    try:
        return model.parse_iso(first.get("start_time"))
    except (ValueError, TypeError):
        return None
