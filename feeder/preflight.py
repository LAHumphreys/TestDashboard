"""Checks run *before* an import does any work.

A feeder run reads a year of history, batches it, and posts it. Every way
that run can fail for a reason having nothing to do with the data — a
typo in ``--url``, a state file the process cannot write, a dashboard
that is not running — is cheap to detect in the first second and
expensive to discover in the fortieth minute.

Two of these are not hypothetical. The feeder is expected to run on a
different host from the dashboard, often against a checkout it has only
read access to; the state file and replay directory both default to the
working directory, which in the documented cron layout *is* that
checkout. Finding that out after a successful import — the point at which
the high-water mark is written — costs the whole run.

Every function here returns ``None`` when the check passes, or a single
actionable sentence naming the remedy when it does not. None of them
raise; the caller decides whether a given failure is fatal.

Python 3.6 compatible; standard library only.
"""

import errno
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Tuple

from feeder.submitter import (
    DEFAULT_TIMEOUT_SECONDS, IMPORT_PATH, Opener,
    describe_connection_error, normalize_url, urllib_opener,
)

#: Read-side counterpart of :data:`feeder.submitter.Opener`:
#: ``url -> (status, body)``. Injected by tests.
Getter = Callable[[str], Tuple[int, bytes]]

#: Written and deleted to prove a directory is writable. Includes the pid
#: so two feeders starting at once cannot collide.
_PROBE_NAME = "testboard_write_test_{0}.tmp"

#: The body of the preflight request: a legal, empty import. It exercises
#: the exact URL, method and path a real batch will use, changes nothing,
#: and is answered with a JSON object whose keys identify the service as a
#: testboard rather than some other thing listening on that port.
_PROBE_BODY = json.dumps({"runs": []}).encode("utf-8")

#: Keys /api/import always returns. Their presence is what distinguishes
#: "a testboard answered" from "something answered with a 200".
_PROBE_KEYS = ("inserted", "updated", "rejected")


def base_url(url: str) -> str:
    """Return the dashboard root, whether or not ``url`` names the endpoint."""
    trimmed = normalize_url(url)
    return trimmed[:-len(IMPORT_PATH)] if trimmed.endswith(IMPORT_PATH) \
        else trimmed


def urllib_getter(url: str) -> Tuple[int, bytes]:
    """Default :data:`Getter`: GET via urllib.request, errors as statuses."""
    try:
        response = urllib.request.urlopen(
            url, timeout=DEFAULT_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    try:
        return response.getcode(), response.read()
    finally:
        response.close()


def fetch_json(
    url: str, path: str, getter: Optional[Getter] = None
) -> Optional[Any]:
    """GET ``path`` from the dashboard and return the parsed JSON, or None.

    Returns None on any failure. Callers use this to *enrich* a report —
    what the dashboard already holds — and a report that cannot be
    enriched should degrade quietly rather than turn into an error about
    a secondary detail.
    """
    fetch = getter if getter is not None else urllib_getter
    try:
        status, body = fetch(base_url(url) + path)
    except Exception:
        return None
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def check_url(url: str) -> Optional[str]:
    """Return a problem with the shape of ``--url``, or None.

    Only the parts urllib will reject outright: a missing scheme (the
    common paste of ``host:8000``) and a missing host.
    """
    # Tested before urlsplit, not after: it reads 'dashboard:8000' as the
    # scheme 'dashboard', so asking it about the scheme would answer a
    # missing one with a baffling complaint about the host name.
    if "://" not in url:
        return (
            "--url '{0}' has no scheme, so it is not a URL. Use "
            "'http://{0}' (or https:// if the dashboard is behind "
            "TLS)".format(url)
        )
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return (
            "--url '{0}' uses the '{1}' scheme; the dashboard speaks plain "
            "HTTP. Use http:// or https://".format(url, parts.scheme)
        )
    if not parts.netloc:
        return (
            "--url '{0}' names no host. It should look like "
            "http://dashboard-host:8000".format(url)
        )
    return None


def check_writable_directory(path: str, purpose: str) -> Optional[str]:
    """Return a problem with writing into directory ``path``, or None.

    Actually writes and removes a file rather than consulting
    ``os.access``, which reports the permission bits and not what the
    filesystem will do — a read-only mount, an exhausted quota and a
    Windows ACL all pass ``os.access`` and then fail the write.
    """
    directory = path or "."
    if not os.path.exists(directory):
        return (
            "the directory for {0} does not exist: {1}. Create it, or "
            "point at somewhere that exists".format(
                purpose, os.path.abspath(directory))
        )
    if not os.path.isdir(directory):
        return (
            "the path for {0} is a file, not a directory: {1}".format(
                purpose, os.path.abspath(directory))
        )
    probe = os.path.join(directory, _PROBE_NAME.format(os.getpid()))
    try:
        with open(probe, "w") as handle:
            handle.write("")
    except OSError as exc:
        return _describe_unwritable(directory, purpose, exc)
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return None


def check_writable_file(path: str, purpose: str) -> Optional[str]:
    """Return a problem with writing the file at ``path``, or None.

    Checks the containing directory (the file need not exist yet — the
    feeder creates it) and, when the file does exist, that it can be
    reopened for writing.
    """
    directory = os.path.dirname(os.path.abspath(path))
    problem = check_writable_directory(directory, purpose)
    if problem is not None:
        return problem
    if os.path.isdir(path):
        return (
            "the path given for {0} is a directory, not a file: {1}".format(
                purpose, os.path.abspath(path))
        )
    if os.path.exists(path):
        try:
            with open(path, "a"):
                pass
        except OSError as exc:
            return (
                "{0} already exists but cannot be written: {1} ({2})".format(
                    purpose, os.path.abspath(path), exc)
            )
    return None


def _describe_unwritable(
    directory: str, purpose: str, exc: OSError
) -> Optional[str]:
    """Turn a failed write probe into one sentence naming the remedy."""
    absolute = os.path.abspath(directory)
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return (
            "cannot write {0} into {1} ({2}). The feeder is often run from "
            "a checkout it only has read access to; point it somewhere it "
            "owns instead, e.g. a directory under /var/lib or the "
            "scheduled user's home".format(purpose, absolute, exc.strerror)
        )
    if exc.errno == errno.ENOSPC:
        return (
            "cannot write {0} into {1}: the filesystem is full".format(
                purpose, absolute)
        )
    return "cannot write {0} into {1} ({2})".format(purpose, absolute, exc)


def probe_dashboard(
    url: str, opener: Optional[Opener] = None
) -> Optional[str]:
    """Return a problem reaching the dashboard at ``url``, or None.

    Sends an empty import (``{"runs": []}``) to the very endpoint the real
    batches use — ``url`` may be the dashboard base or the full
    ``/api/import`` address, exactly as :class:`~feeder.submitter.Submitter`
    accepts it. The probe inserts nothing, so it is safe to run before
    every import, and its response identifies the service: anything else
    listening on that port answers with the wrong status, or with a 200
    that is not a testboard import result.
    """
    send = opener if opener is not None else urllib_opener
    url = normalize_url(url)
    headers = {"Content-Type": "application/json"}
    try:
        status, body = send(url, _PROBE_BODY, headers)
    except Exception as exc:
        return describe_connection_error(url, exc)
    if status == 404:
        return (
            "the server at {0} answered, but has no /api/import endpoint "
            "(HTTP 404). Either --url points at a different service, or it "
            "includes a path prefix that is not part of the dashboard's "
            "address".format(url)
        )
    if status in (401, 403):
        return (
            "the server at {0} refused the request (HTTP {1}). Something "
            "in front of the dashboard - a proxy or SSO gateway - is "
            "requiring authentication that the feeder cannot "
            "provide".format(url, status)
        )
    if status != 200:
        return (
            "the server at {0} answered HTTP {1} to an empty test import. "
            "The dashboard answers 200; check that --url points at a "
            "testboard server and not at something else".format(url, status)
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return (
            "something is listening at {0} and answered 200, but not with "
            "JSON - so it is not a testboard dashboard. Check the host and "
            "port in --url".format(url)
        )
    if not isinstance(payload, dict) or not all(
        key in payload for key in _PROBE_KEYS
    ):
        return (
            "something is listening at {0} and answered 200, but its reply "
            "is not a testboard import result (expected the keys {1}). "
            "Check the host and port in --url".format(
                url, ", ".join(_PROBE_KEYS))
        )
    return None
