"""High-water-mark state file for the feeder's daily mode.

The state file is a tiny JSON document ``{"high_water_mark": "<ISO>"}``
recording the newest ``start_time`` the dashboard has accepted. Daily runs
import everything from that mark minus a safety overlap; because the server
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


def save_high_water_mark(path: str, hwm: datetime.datetime) -> None:
    """Atomically write ``{"high_water_mark": iso}`` to ``path``.

    Writes to a temporary sibling file first and then replaces the target,
    so a crash mid-write cannot corrupt the previous state.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump({_KEY: model.format_iso(hwm)}, handle)
    os.replace(tmp_path, path)
