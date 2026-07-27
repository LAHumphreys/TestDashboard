"""High-water-mark state file for the feeder's catchup mode.

The state file is a tiny JSON document ``{"high_water_mark": "<ISO>"}``
recording the newest ``start_time`` the dashboard has accepted. Catchup
runs import everything from that mark minus a safety overlap; because the server
upserts, the overlap is free and a lost/corrupt state file only means a
harmless full re-import.

Python 3.6 compatible; standard library only.
"""

import datetime
import json
import logging
import os
from typing import Optional

from testboard import model

logger = logging.getLogger(__name__)

_KEY = "high_water_mark"


def load_high_water_mark(path: str) -> Optional[datetime.datetime]:
    """Load the saved high-water mark from ``path``.

    Returns ``None`` when the file is absent (first run) or unreadable /
    corrupt / not in the expected shape — in the corrupt case a WARNING is
    logged and the feeder simply re-imports from scratch (safe: the server
    upserts).
    """
    if not os.path.exists(path):
        logger.debug("state file %s does not exist yet (first run?)", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return model.parse_iso(data[_KEY])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "state file %s is corrupt or unreadable (%s); ignoring it - "
            "this run will import without a high-water mark (safe: the "
            "server upserts, no duplicates are possible)", path, exc,
        )
        return None


def advance_high_water_mark(path: str, hwm: datetime.datetime) -> bool:
    """Save ``hwm`` only if it is newer than what is already recorded.

    The mark means "the newest run we have successfully pushed", so it can
    only ever move forwards. Saving unconditionally is wrong as soon as
    history is imported in pieces: bringing in 2024 after 2026 — the
    natural order for a large backfill, newest first so the dashboard is
    useful immediately — would rewind the mark by two years, and the next
    catchup run would silently re-read everything since. Harmless, because
    the server upserts, but slow and baffling.

    Returns True when the file was written.
    """
    existing = load_high_water_mark(path)
    if existing is not None and existing >= hwm:
        logger.info(
            "the saved high-water mark is %s, which is at or after the "
            "newest run this import accepted (%s), so it was left alone - "
            "the mark records the newest run ever pushed and only moves "
            "forwards",
            model.format_iso(existing), model.format_iso(hwm),
        )
        return False
    return save_high_water_mark(path, hwm)


def save_high_water_mark(path: str, hwm: datetime.datetime) -> bool:
    """Atomically write ``{"high_water_mark": iso}`` to ``path``.

    Writes to a temporary sibling file first and then replaces the target,
    so a crash mid-write cannot corrupt the previous state.

    Callers that are recording progress want
    :func:`advance_high_water_mark`, which will not move the mark
    backwards. This one writes whatever it is given.

    Returns True on success. A failure to write is reported at ERROR and
    returns False rather than raising: by the time this is called the
    import has already succeeded, and the records are safely in the
    dashboard. Losing the mark costs one redundant re-import tomorrow —
    turning that into an uncaught traceback would instead make a
    successful run look like a failed one to whatever scheduled it.
    """
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump({_KEY: model.format_iso(hwm)}, handle)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error(
            "the import SUCCEEDED, but the high-water mark %s could not be "
            "saved to %s (%s). Nothing is lost - the next catchup run will "
            "simply re-import from further back, and the server upserts so "
            "no duplicates are possible. To stop this recurring, point "
            "--state-file at a file the feeder can write (the checkout it "
            "runs from is often read-only)",
            model.format_iso(hwm), os.path.abspath(path), exc,
        )
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
    return True
