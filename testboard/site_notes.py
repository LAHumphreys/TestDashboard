"""Site-specific "what's new" notes, kept in a JSON file.

`static/whatsnew.html` is testboard's own release notes: it ships with the
build and a deployment overwrites it. But a drop is not only testboard
changing — the same morning, the site's own reader may have been fixed, a
box rebuilt, an environment renamed. Those belong on the same page,
under the same date, because a tester reading "what changed" does not care
which repository it came from.

So they live HERE instead: a JSON file **outside the repository**, added
to with ``tools/add_site_note.py`` and shown on the What's new page under
the matching date. A ``git pull`` cannot touch it.

WHY A FILE AND NOT A TABLE
--------------------------

It needs no migration, which matters more than it sounds: migration
versions are claimed in ``docs/UPGRADE_PLAN.md`` and version 6 belongs to
WP-15, so a table here would either queue behind that work or take a
number out of turn. And these notes are not testboard's data — they are
one site's commentary on it — so keeping them out of the product's schema
is where they belong anyway. A MariaDB migration has one less table to
carry for it.

It is also re-read on every request, deliberately. Adding a note is then a
one-line command with no restart, and "restarting the server is not
optional after a Python change" has already cost this project time twice.
The file is small (a few notes) and the read is a few hundred
microseconds.

FAILURE IS SILENT, BY DESIGN
----------------------------

A missing, empty, unreadable or malformed file yields NO notes rather than
an error. These are additive garnish on a page whose real content ships
inside the build, and a broken side-car must never stop a tester reading
the release notes. What it does do is report the reason back through
:func:`load`, so the CLI and the tests can tell "there are none" from
"it is broken".

Python 3.6 compatible; standard library only.
"""

import datetime
import io
import json
import os
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from testboard import model

__all__ = [
    "SiteNote",
    "default_path",
    "load",
    "add",
    "edit",
    "remove",
    "MAX_TEXT",
    "MAX_NOTES",
]

#: Longest note accepted. A note is a sentence or two for a tester, and
#: the page is not a wiki; a runaway paste would push the release notes
#: off the screen it is meant to annotate.
MAX_TEXT = 2000

#: Most notes kept in the file. Old ones fall off the end rather than
#: growing it without limit — the page only ever shows the recent drops,
#: and the authority on what happened is the commit log.
MAX_NOTES = 500

#: Filename used beside the database when no path is given.
_FILENAME = "site_notes.json"


class SiteNote(NamedTuple):
    """One site-specific note, belonging to a dated drop.

    ``note_id`` is small, stable and shown by ``--list``, because a note
    published to every tester WILL sometimes need correcting — a typo, a
    wrong environment name, a claim that turned out to be wrong — and
    "delete the line from the JSON by hand" is not an answer for a file a
    running server is reading. See :func:`edit` and :func:`remove`.
    """

    note_id: int
    date: str            # "YYYY-MM-DD": which drop this belongs to
    text: str
    author: str
    added_at: str        # ISO-8601 UTC, when it was recorded


def default_path(db_path: str) -> str:
    """Where the notes file lives when nobody says.

    Beside the DATABASE, not in the current directory. The database path
    is the one path an operator already gets right, and it is not inside
    the repository, so a deployment cannot overwrite the notes. A default
    relative to the working directory would silently find nothing when
    the server was started from somewhere else, with no way to tell that
    apart from "no notes yet".
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(directory, _FILENAME)


def _valid_date(text: str) -> bool:
    """True when *text* is a plain YYYY-MM-DD calendar date."""
    if not isinstance(text, str) or len(text) != 10:
        return False
    try:
        datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _note_from(raw: Any, fallback_id: int) -> Optional[SiteNote]:
    """Convert one parsed object to a SiteNote, or None if unusable.

    One bad record must not lose the good ones — the same rule the feeder
    follows for run records, and for the same reason: a hand-edited file
    will eventually have a typo in it, and losing five good notes to one
    bad one is the wrong trade.

    A note with no usable id gets *fallback_id*, which the caller derives
    from file order. That keeps ids stable for a given file (so the id
    ``--list`` printed is the id ``--remove`` deletes) and lets a
    hand-written file, which will have no ids at all, still be edited by
    number. The next write persists them.
    """
    if not isinstance(raw, dict):
        return None
    date = raw.get("date")
    text = raw.get("text")
    if not _valid_date(date):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    author = raw.get("author")
    added_at = raw.get("added_at")
    note_id = raw.get("id")
    if not isinstance(note_id, int) or isinstance(note_id, bool) \
            or note_id < 1:
        note_id = fallback_id
    return SiteNote(
        note_id=note_id,
        date=date,
        text=text.strip()[:MAX_TEXT],
        author=author if isinstance(author, str) and author.strip()
        else "unknown",
        added_at=added_at if isinstance(added_at, str) else "",
    )


def load(path: Optional[str]) -> Tuple[List[SiteNote], Optional[str]]:
    """Read the notes file.

    Returns ``(notes, problem)``. *notes* is newest-date first, then
    newest-added first within a date. *problem* is None when all is well,
    or a one-line human-readable reason — the caller decides whether that
    is worth showing. Notes are never an error path for the page.
    """
    if not path:
        return [], None
    if not os.path.isfile(path):
        return [], None            # not yet created is not a problem
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except ValueError as exc:
        return [], "{0} is not valid JSON: {1}".format(path, exc)
    except OSError as exc:
        return [], "cannot read {0}: {1}".format(path, exc)

    if isinstance(payload, list):
        # Tolerate a bare list: it is the shape a person writes by hand.
        raw_notes = payload
    elif isinstance(payload, dict):
        raw_notes = payload.get("notes", [])
    else:
        return [], "{0} should hold an object with a 'notes' list".format(path)
    if not isinstance(raw_notes, list):
        return [], "{0}: 'notes' should be a list".format(path)

    # Ids already in the file are honoured; anything missing one is
    # numbered above the highest that is there, in file order, so the
    # assignment is deterministic for a given file.
    used = set()  # type: set
    for raw in raw_notes:
        if isinstance(raw, dict):
            candidate = raw.get("id")
            if isinstance(candidate, int) and not isinstance(candidate, bool) \
                    and candidate >= 1:
                used.add(candidate)
    next_id = (max(used) + 1) if used else 1

    notes = []  # type: List[SiteNote]
    skipped = 0
    for raw in raw_notes:
        note = _note_from(raw, next_id)
        if note is None:
            skipped += 1
            continue
        if note.note_id == next_id:
            next_id += 1
        notes.append(note)
    notes.sort(key=lambda note: (note.date, note.added_at), reverse=True)
    problem = None
    if skipped:
        problem = "{0}: skipped {1} unusable note(s)".format(path, skipped)
    return notes, problem


def _load_for_write(path: str) -> List[SiteNote]:
    """Load notes, refusing to proceed if the file cannot be understood.

    A read for DISPLAY tolerates a broken file and shows nothing. A read
    that is about to rewrite it must not: the rewrite would drop whatever
    could not be parsed, silently and permanently.
    """
    existing, problem = load(path)
    if problem is not None and os.path.isfile(path):
        raise ValueError(
            "{0} -- fix or move it before changing notes".format(problem))
    return existing


def _write(path: str, notes: List[SiteNote]) -> None:
    """Write the whole notes file, atomically.

    To a temporary file in the same directory, then renamed over the
    original: a failure part-way through leaves the previous notes intact
    rather than a half-written file that :func:`load` would reject and the
    server would then show as no notes at all.
    """
    ordered = sorted(
        notes, key=lambda item: (item.date, item.added_at), reverse=True
    )[:MAX_NOTES]
    payload = {
        "notes": [
            {
                "id": item.note_id,
                "date": item.date,
                "text": item.text,
                "author": item.author,
                "added_at": item.added_at,
            }
            for item in ordered
        ]
    }  # type: Dict[str, Any]

    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = path + ".tmp"
    with io.open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True))
        handle.write("\n")
    if os.path.exists(path):
        os.remove(path)            # os.rename onto an existing file fails
    os.rename(temporary, path)     # on Windows


def add(
    path: str,
    date: str,
    text: str,
    author: str,
    now: Optional[datetime.datetime] = None,
) -> SiteNote:
    """Add a note and write the file back.

    Raises ValueError for a bad date, empty text, or a notes file that
    cannot be parsed.
    """
    if not _valid_date(date):
        raise ValueError(
            "date must be YYYY-MM-DD, got {0!r}".format(date))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")

    existing = _load_for_write(path)
    highest = max([note.note_id for note in existing] + [0])
    note = SiteNote(
        note_id=highest + 1,
        date=date,
        text=text.strip()[:MAX_TEXT],
        author=(author or "unknown").strip() or "unknown",
        added_at=model.format_iso(now or model.utcnow()),
    )
    _write(path, [note] + list(existing))
    return note


def edit(
    path: str,
    note_id: int,
    text: Optional[str] = None,
    date: Optional[str] = None,
) -> Optional[SiteNote]:
    """Correct a note in place; returns the new version, or None if absent.

    This exists because a note is PUBLISHED the moment it is written — the
    server re-reads the file per request, so every tester sees a typo, a
    wrong environment name or a claim that turned out to be wrong as soon
    as it is made. Correcting it must not mean hand-editing JSON that a
    running server is reading.

    ``added_at`` and the author are kept: this is the same note, corrected,
    and rewriting when it was recorded would move it under a different
    drop. Pass *date* to move it deliberately.
    """
    if text is not None and not text.strip():
        raise ValueError("text must not be empty")
    if date is not None and not _valid_date(date):
        raise ValueError("date must be YYYY-MM-DD, got {0!r}".format(date))

    existing = _load_for_write(path)
    updated = None  # type: Optional[SiteNote]
    kept = []  # type: List[SiteNote]
    for note in existing:
        if note.note_id == note_id and updated is None:
            updated = note._replace(
                text=note.text if text is None else text.strip()[:MAX_TEXT],
                date=note.date if date is None else date,
            )
            kept.append(updated)
        else:
            kept.append(note)
    if updated is None:
        return None
    _write(path, kept)
    return updated


def remove(path: str, note_id: int) -> Optional[SiteNote]:
    """Delete a note; returns the one removed, or None if there was no such id.

    The other half of :func:`edit`. A note that should never have been
    published has to be retractable, and by number rather than by editing
    the file underneath the server.
    """
    existing = _load_for_write(path)
    removed = None  # type: Optional[SiteNote]
    kept = []  # type: List[SiteNote]
    for note in existing:
        if note.note_id == note_id and removed is None:
            removed = note
        else:
            kept.append(note)
    if removed is None:
        return None
    _write(path, kept)
    return removed
