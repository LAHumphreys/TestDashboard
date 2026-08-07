"""Read MariaDB connection settings from a mysql option file.

This is the ONE credentials format in the project: the same
``[client]`` option file the ``mysql`` command-line client reads, the
same file the migration runbook has the administrator write
(``docs/MARIADB_MIGRATION.md`` §A.9 for the operator's migration
credentials, §A.10 for the application's ``/etc/testboard/db.cnf``).
The migration tool and the dashboard both parse it through this module,
so a file that works for one works for the other.

It lives in ``testboard/`` rather than ``tools/`` because the server
reads it too (``run_server.py --db-config``), and the serving path must
not depend on the tools directory. It imports nothing but the standard
library — parsing a credentials file needs no database driver.

``configparser`` is deliberately not used: my.cnf allows bare keys with
no ``=`` (``local-infile``) and ``!includedir`` directives, both of
which it rejects outright.

Passwords never come from a command line. Anything on a command line is
visible to every user on the box through ``ps``.

Errors are :class:`DbConfigError`, not ``SystemExit``: the migration
tool turns them into exit codes, the server turns them into a startup
message, and a library that kills the process decides that for both.

Python 3.6 compatible; standard library only.
"""

import io
import os
import stat
from typing import Dict, NamedTuple, Optional

__all__ = ["DbConfigError", "Settings", "read_option_file"]


class DbConfigError(Exception):
    """An option file that is missing, unreadable, or incomplete."""


class Settings(NamedTuple):
    """Connection details, read from a mysql option file."""

    host: str
    port: int
    user: str
    password: str
    database: str
    unix_socket: Optional[str]

    def describe(self) -> str:
        """One line for the log. Never contains the password."""
        where = self.unix_socket or "{0}:{1}".format(self.host, self.port)
        return "{0}@{1}/{2}".format(self.user, where, self.database)


def read_option_file(path: str) -> Settings:
    """Parse a mysql ``[client]`` option file into :class:`Settings`.

    Sections read: ``[client]``, ``[mysql]``, ``[testboard]``. Bare
    keys (``local-infile``) become ``"1"``. One layer of matching
    quotes is stripped from values, as the mysql client strips them.
    ``!include`` directives are ignored, not followed.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        # ASCII only: this line is printed by run_server possibly under
        # LANG=C, where Python 3.6 cannot encode a section sign.
        raise DbConfigError(
            "no option file at {0}. Create one as shown in "
            "docs/MARIADB_MIGRATION.md section A.9 and chmod it "
            "600.".format(expanded))
    _warn_if_world_readable(expanded)

    values = {}  # type: Dict[str, str]
    section = ""
    # utf-8-sig, not utf-8: an option file written by a Windows editor
    # (or PowerShell's Out-File) starts with a BOM, which would glue
    # itself to "[client]" and silently skip every key in the file.
    # For BOM-less files the two codecs read identically.
    with io.open(expanded, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line[0] in "#;!":
                continue
            if line.startswith("["):
                section = line.strip("[]").strip().lower()
                continue
            if section not in ("client", "mysql", "testboard"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip().lower().replace("_", "-")] = _unquote(
                    value.strip())
            else:
                values[line.lower().replace("_", "-")] = "1"

    missing = [k for k in ("user", "password", "database")
               if not values.get(k)]
    if missing:
        raise DbConfigError(
            "option file {0} is missing: {1}. It needs host, user, "
            "password and database under a [client] section.".format(
                expanded, ", ".join(missing)))

    port_text = values.get("port", "3306")
    try:
        port = int(port_text)
    except ValueError:
        raise DbConfigError(
            "option file {0} has port = {1!r}, which is not a "
            "number.".format(expanded, port_text))

    return Settings(
        host=values.get("host", "127.0.0.1"),
        port=port,
        user=values["user"],
        password=values["password"],
        database=values["database"],
        unix_socket=values.get("socket") or None,
    )


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, as the mysql client does."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _warn_if_world_readable(path: str) -> None:
    """A credentials file readable by everyone is a finding, not style.

    Not fatal — refusing to run would block a dry run for no safety
    gain — and POSIX-only, because on Windows every file looks
    group-readable and a warning that always fires is one nobody reads.
    """
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:  # pragma: no cover - unreadable file already failed
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print("WARNING: {0} is readable beyond its owner. It holds a "
              "database password: chmod 600 it.".format(path))
