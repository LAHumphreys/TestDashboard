"""The feeder's optional JSON config file.

A working daily import is a long command line — a URL, a reader spec, one
or more sources, a state file and a replay directory, none of which have
a sensible default once the feeder stops running from the directory it
was installed in. Kept in a scheduler entry that line is write-only: it
is impossible to review, easy to mistype, and it silently ties the import
to whatever working directory the scheduler happened to use.

So the same settings can live in a JSON file, and the scheduled command
shrinks to ``run_feeder.py --config /etc/testboard/feeder.json``.

Precedence is the usual one: a flag typed on the command line beats the
config file, which beats the built-in default. That makes the file the
description of a deployment and flags the way to vary a single run
(``--dry-run``, ``--since``, a one-off ``--url``).

Keys are the long option names with dashes replaced by underscores, so
``--batch-size`` is ``"batch_size"``. Anything else is refused by name,
with the nearest valid key suggested: a config file whose typo'd key is
quietly ignored is worse than no config file, because the setting appears
to be applied and is not.

Python 3.6 compatible; standard library only.
"""

import difflib
import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

#: The file ``--config`` looks for when given a directory, and the name
#: :mod:`feeder.init` proposes.
DEFAULT_CONFIG_NAME = "feeder.config.json"

#: Every settable key: name -> (JSON type name, one-line description).
#: This is the authority for what a config file may contain; it is used to
#: validate a file, to write one, and to explain a rejected key.
CONFIG_KEYS = (
    ("url", "string", "dashboard base URL, e.g. http://dashboard-host:8000"),
    ("mode", "string", "'backfill' or 'daily'"),
    ("reader", "string",
     "'jsonl', or 'PATH.py:create_reader' for a site-specific reader"),
    ("source", "list of strings",
     "input files, globs or directories for the reader"),
    ("batch_size", "integer", "records per POST batch"),
    ("state_file", "string",
     "where daily mode remembers how far it got; must be writable"),
    ("replay_dir", "string",
     "where failed batches are saved; must be writable"),
    ("max_consecutive_failures", "integer",
     "give up after this many batches fail in a row"),
    ("overlap_days", "integer",
     "daily mode: how far before the high-water mark to re-import"),
    ("allow_empty", "boolean", "treat reading zero records as success"),
    ("verbose", "boolean", "DEBUG logging"),
)

#: Keys that must be a JSON string when present.
_STRING_KEYS = frozenset(
    name for name, kind, _ in CONFIG_KEYS if kind == "string")
_INT_KEYS = frozenset(
    name for name, kind, _ in CONFIG_KEYS if kind == "integer")
_BOOL_KEYS = frozenset(
    name for name, kind, _ in CONFIG_KEYS if kind == "boolean")
_LIST_KEYS = frozenset(
    name for name, kind, _ in CONFIG_KEYS if kind.startswith("list"))

_VALID_MODES = ("backfill", "daily")


class ConfigError(Exception):
    """Raised when a config file is missing, unparseable or wrong.

    The message is user-facing and names the file, the key and the fix.
    """


def valid_keys() -> List[str]:
    """Return every accepted config key, in declaration order."""
    return [name for name, _, _ in CONFIG_KEYS]


def describe_keys() -> str:
    """Render the accepted keys as an indented, readable block."""
    width = max(len(name) for name, _, _ in CONFIG_KEYS)
    return "\n".join(
        "  {0}  {1} ({2})".format(name.ljust(width), description, kind)
        for name, kind, description in CONFIG_KEYS
    )


def resolve_path(path: str) -> str:
    """Return the config file named by ``path``, which may be a directory."""
    if os.path.isdir(path):
        return os.path.join(path, DEFAULT_CONFIG_NAME)
    return path


def load_config(path: str) -> Dict[str, Any]:
    """Read and validate the config file at ``path``.

    Returns a dict of the keys it sets, ready to hand to
    ``ArgumentParser.set_defaults``. Keys beginning with ``_`` are ignored,
    which is how a JSON file without comment syntax carries a note.

    Raises:
        ConfigError: the file is missing, is not JSON, is not a JSON
            object, or contains an unknown key or a value of the wrong
            type.
    """
    resolved = resolve_path(path)
    if not os.path.exists(resolved):
        raise ConfigError(
            "no config file at {0}. Create one with: python3 "
            "run_feeder.py --init".format(os.path.abspath(resolved))
        )
    try:
        with io.open(resolved, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ConfigError(
            "cannot read the config file {0} ({1})".format(
                os.path.abspath(resolved), exc)
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(
            "the config file {0} is not valid JSON ({1}). It must be a "
            "single JSON object, e.g. {{\"url\": \"http://host:8000\", "
            "\"mode\": \"daily\"}}. Note JSON has no comments and needs "
            "double quotes around every key and string".format(
                os.path.abspath(resolved), exc)
        )
    if not isinstance(data, dict):
        raise ConfigError(
            "the config file {0} must contain a JSON object (a {{...}} "
            "mapping of setting to value), not a {1}".format(
                os.path.abspath(resolved), type(data).__name__)
        )
    return _validate(data, os.path.abspath(resolved))


def _validate(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Check every key and value; return the settings to apply."""
    settings = {}  # type: Dict[str, Any]
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key not in _STRING_KEYS | _INT_KEYS | _BOOL_KEYS | _LIST_KEYS:
            raise ConfigError(_unknown_key_message(key, path))
        if value is None:
            continue
        settings[key] = _coerce(key, value, path)
    mode = settings.get("mode")
    if mode is not None and mode not in _VALID_MODES:
        raise ConfigError(
            "in {0}: \"mode\" is '{1}', but must be one of {2}".format(
                path, mode, " or ".join(
                    "'" + valid + "'" for valid in _VALID_MODES))
        )
    return settings


def _coerce(key: str, value: Any, path: str) -> Any:
    """Return ``value`` in the type ``key`` requires, or raise ConfigError."""
    if key in _STRING_KEYS:
        if not isinstance(value, str):
            raise _wrong_type(key, value, "a string", path)
        return value
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise _wrong_type(key, value, "true or false", path)
        return value
    if key in _INT_KEYS:
        # bool is an int in Python; "batch_size": true is a mistake.
        if isinstance(value, bool) or not isinstance(value, int):
            raise _wrong_type(key, value, "a whole number", path)
        if value < 1:
            raise ConfigError(
                "in {0}: \"{1}\" is {2}, but must be 1 or more".format(
                    path, key, value)
            )
        return value
    # A list key. A bare string is accepted as a one-element list: writing
    # "source": "results/*.jsonl" is the natural mistake and the intent is
    # never in doubt.
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise _wrong_type(
            key, value, "a list of strings, e.g. [\"results/*.jsonl\"]", path)
    return list(value)


def _wrong_type(
    key: str, value: Any, expected: str, path: str
) -> ConfigError:
    """Build the message for a config value of the wrong JSON type."""
    return ConfigError(
        "in {0}: \"{1}\" must be {2}, but is {3} ({4})".format(
            path, key, expected, json.dumps(value), _json_type(value))
    )


def _json_type(value: Any) -> str:
    """Name the JSON type of ``value`` the way a JSON author would."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, (int, float)):
        return "a number"
    return "null"


def _unknown_key_message(key: str, path: str) -> str:
    """Explain an unrecognized key, suggesting the nearest real one."""
    suggestion = ""
    # The dashed form is checked first: it is a certain diagnosis (the key
    # was copied from the --help output) rather than a guess, and it
    # explains the rule instead of just naming one key.
    if key.replace("-", "_") in valid_keys():
        suggestion = (
            " Config keys use underscores, not dashes: \"{0}\".".format(
                key.replace("-", "_"))
        )
    else:
        close = difflib.get_close_matches(key, valid_keys(), n=1, cutoff=0.6)
        if close:
            suggestion = " Did you mean \"{0}\"?".format(close[0])
    return (
        "in {0}: \"{1}\" is not a setting the feeder understands.{2}\n"
        "The settings a config file may contain are:\n{3}".format(
            path, key, suggestion, describe_keys())
    )


def dump_config(settings: Dict[str, Any]) -> str:
    """Render ``settings`` as the text of a config file.

    Keys come out in :data:`CONFIG_KEYS` order rather than however the
    dict happens to iterate, so two configs written from the same answers
    are identical files and a diff between deployments is readable.
    """
    ordered = []  # type: List[Tuple[str, Any]]
    for name, _, _ in CONFIG_KEYS:
        if name in settings and settings[name] is not None:
            ordered.append((name, settings[name]))
    body = ",\n".join(
        "  {0}: {1}".format(json.dumps(name), json.dumps(value))
        for name, value in ordered
    )
    return "{\n" + body + "\n}\n"


def write_config(path: str, settings: Dict[str, Any]) -> None:
    """Write ``settings`` to ``path``, creating parent directories.

    Raises:
        ConfigError: the file could not be written, naming the path.
    """
    directory = os.path.dirname(os.path.abspath(path))
    try:
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(dump_config(settings))
    except OSError as exc:
        raise ConfigError(
            "cannot write the config file {0} ({1})".format(
                os.path.abspath(path), exc)
        )


def apply_to_parser(parser: Any, settings: Dict[str, Any]) -> List[str]:
    """Install config values as parser defaults; return the keys applied.

    ``set_defaults`` is exactly the right hook: argparse only falls back
    to a default when the option was not given, so a flag on the command
    line still wins.

    ``source`` is deliberately excluded. It is an ``append`` option, and an
    append option seeded with a default *adds to* it rather than replacing
    it — a config listing two directories plus a one-off ``--source`` on
    the command line would import all three. The caller applies it after
    parsing instead.
    """
    applied = {
        key: value for key, value in settings.items() if key != "source"
    }
    if applied:
        parser.set_defaults(**applied)
    return sorted(settings)
