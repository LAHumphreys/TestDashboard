"""``run_feeder.py --init``: an interactive, *validating* setup wizard.

The point is not to save typing. A form that collects nine answers and
writes them to a file has moved the mistakes from the command line into
the config file, where they surface at 03:00 as a cron job that failed
with no one watching.

So every answer is checked the moment it is given, against the real
thing:

- the dashboard URL is used to send an empty import, so a wrong host,
  port or path fails here rather than after a year of history has been
  read;
- the reader is actually loaded, and can be run over its data on the
  spot;
- the state file and replay directory are written to and cleaned up,
  which is where a read-only checkout — the normal deployment — is
  caught.

The wizard therefore does the work of half a dozen separate preflight
flags, and does it at the one moment the person can still fix the answer.
It ends by printing the exact scheduled-task line for the config it just
wrote, since that line is the remaining place to get the paths wrong.

Python 3.6 compatible; standard library only.
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional, TextIO

from feeder import check, config, preflight, reader as reader_module
from feeder.submitter import Opener

#: Offered as the reader when the site has no bespoke format yet.
_BUILTIN_READER = "jsonl"

_RULE = "-" * 68


class Abort(Exception):
    """Raised when the person answering asks to stop, or input ends."""


def run_init(
    out: TextIO,
    inp: TextIO,
    config_path: Optional[str] = None,
    opener: Optional[Opener] = None,
    require_tty: bool = True,
) -> int:
    """Run the wizard; return the process exit code (0 ok, 2 aborted).

    ``opener`` is injected by tests so the dashboard probe needs no
    server. ``require_tty`` is the guard against a wizard being reached
    from cron: an interactive prompt in a scheduled job either hangs or
    dies on EOF, and neither says why.
    """
    if require_tty and not _is_tty(inp):
        _say(out, (
            "--init is interactive and there is no terminal attached "
            "(stdin is not a tty).\n\n"
            "If you are running this from a scheduled task or a pipe, you "
            "want the config file itself rather than the wizard. It is a "
            "JSON object; these are the settings it may contain:\n\n"
            "{0}\n\n"
            "Write one by hand and pass it with --config PATH.".format(
                config.describe_keys())
        ))
        return 2

    try:
        return _wizard(out, inp, config_path, opener)
    except Abort as exc:
        _say(out, "\nStopped: {0}. Nothing was written.".format(exc))
        return 2
    except KeyboardInterrupt:
        _say(out, "\nInterrupted. Nothing was written.")
        return 2


def _wizard(
    out: TextIO, inp: TextIO, config_path: Optional[str],
    opener: Optional[Opener],
) -> int:
    """The wizard proper; see :func:`run_init`."""
    _say(out, _RULE)
    _say(out, "testboard feeder setup")
    _say(out, _RULE)
    _say(out, (
        "\nThis writes a config file and checks each answer as you give "
        "it, so a\nmistake shows up now rather than in a scheduled run. "
        "Press Ctrl-C to stop.\n"
    ))

    path = _ask_config_path(out, inp, config_path)
    home = os.path.dirname(os.path.abspath(path)) or "."

    settings = {}  # type: Dict[str, Any]
    settings["url"] = _ask_url(out, inp, opener)
    settings["mode"] = _ask_mode(out, inp)
    reader_spec, sources = _ask_reader(out, inp)
    settings["reader"] = reader_spec
    if sources:
        settings["source"] = sources
    settings["state_file"] = _ask_writable_file(
        out, inp, "state file",
        os.path.join(home, "feeder_state.json"),
        "catchup mode records how far it got here, so it does not "
        "re-read everything each time",
    )
    settings["replay_dir"] = _ask_writable_dir(
        out, inp, "replay directory", os.path.join(home, "replay"),
        "batches the dashboard could not accept are saved here instead of "
        "being lost",
    )

    _write(out, path, settings)
    _print_next_steps(out, path, settings)
    return 0


# ----------------------------------------------------------------------
# Individual questions
# ----------------------------------------------------------------------


def _ask_config_path(
    out: TextIO, inp: TextIO, config_path: Optional[str]
) -> str:
    """Choose where the config file goes, refusing to clobber silently."""
    default = config_path or os.path.join(os.getcwd(),
                                          config.DEFAULT_CONFIG_NAME)
    while True:
        path = config.resolve_path(
            _ask(out, inp, "Write the config file where?", default))
        if not os.path.exists(path):
            problem = preflight.check_writable_file(path, "the config file")
            if problem is not None:
                _say(out, "  " + problem)
                continue
            return path
        _say(out, "  {0} already exists.".format(os.path.abspath(path)))
        if _ask_yes_no(out, inp, "  Overwrite it?", default=False):
            return path


def _ask_url(out: TextIO, inp: TextIO, opener: Optional[Opener]) -> str:
    """Ask for the dashboard URL and prove it answers before accepting it."""
    _say(out, "\nThe dashboard")
    _say(out, (
        "  The feeder pushes results over HTTP, so this is the address of "
        "the machine\n  running run_server.py - not a path on this one."
    ))
    while True:
        url = _ask(out, inp, "Dashboard URL", "http://127.0.0.1:8000")
        problem = preflight.check_url(url)
        if problem is None:
            _say(out, "  checking {0} ...".format(url))
            problem = preflight.probe_dashboard(url, opener)
        if problem is None:
            _say(out, "  OK - a testboard dashboard answered.")
            return url
        _say(out, "  " + problem)
        if not _ask_yes_no(out, inp, "  Try a different URL?", default=True):
            _say(out, (
                "  Keeping it. The import will fail until the dashboard is "
                "reachable;\n  re-run it then - importing is safe to repeat."
            ))
            return url


def _ask_mode(out: TextIO, inp: TextIO) -> str:
    """Ask which of the two import modes this deployment schedules."""
    _say(out, "\nImport mode")
    _say(out, (
        "  catchup  - import everything since the newest run already "
        "pushed. This is\n             what you schedule: it resumes from "
        "where it got to, not from\n             today's date, so a "
        "machine that was off for a week catches up.\n"
        "  backfill - import history, bounded by --since / --until. "
        "Usually run once,\n             by hand, first."
    ))
    return _ask_choice(
        out, inp, "Mode", ["catchup", "backfill"], "catchup")


def _ask_reader(out: TextIO, inp: TextIO) -> Any:
    """Ask for the reader and load it; returns ``(spec, sources)``."""
    _say(out, "\nWhere the results come from")
    _say(out, (
        "  The reader is the one piece of code specific to your site. If "
        "you have not\n  written it yet, choose the built-in JSON-lines "
        "reader and change this later\n  (see docs/FEEDER_BRIEF.md)."
    ))
    while True:
        spec = _ask(
            out, inp,
            "Reader ('jsonl', or PATH.py:create_reader)", _BUILTIN_READER)
        sources = _ask_sources(out, inp, spec)
        try:
            loaded = reader_module.load_reader(spec, sources)
        except reader_module.ReaderLoadError as exc:
            _say(out, "  " + str(exc))
            continue
        _say(out, "  OK - reader loaded.")
        if _ask_yes_no(
            out, inp, "  Read the data now and check every record?",
            default=True,
        ):
            _check_reader(out, loaded)
        return spec, sources


def _ask_sources(out: TextIO, inp: TextIO, spec: str) -> List[str]:
    """Collect the ``--source`` values, which some readers do not need."""
    if spec == _BUILTIN_READER:
        _say(out, (
            "  The built-in reader needs to be told which files to read: a "
            "file, a glob\n  such as 'results/*.jsonl', or a directory to "
            "search."
        ))
    else:
        _say(out, (
            "  Your reader is handed these as its 'sources' argument. "
            "Leave blank if it\n  finds its own data."
        ))
    sources = []  # type: List[str]
    while True:
        prompt = "Source" if not sources else "Another source (blank to end)"
        value = _ask(out, inp, prompt, "" if sources or spec != _BUILTIN_READER
                     else None, allow_blank=True)
        if not value:
            if sources or spec != _BUILTIN_READER:
                return sources
            _say(out, "  The built-in reader has nothing to read without a "
                      "source.")
            continue
        sources.append(value)


def _check_reader(out: TextIO, loaded: Any) -> None:
    """Run the offline reader check and print its report to ``out``."""
    logger = _stream_logger(out)
    try:
        report = check.check_reader(loaded.read(None))
    except Exception as exc:
        _say(out, (
            "  the reader raised {0} while reading: {1}\n"
            "  read() must not raise on a bad record - log it, skip it, "
            "carry on.".format(type(exc).__name__, exc)
        ))
        return
    check.log_report(report, logger)


def _ask_writable_file(
    out: TextIO, inp: TextIO, label: str, default: str, why: str
) -> str:
    """Ask for a file path and prove it can be written."""
    _say(out, "\nThe {0}".format(label))
    _say(out, "  " + why + ".")
    _say(out, (
        "  It must be somewhere the scheduled user can write - not inside "
        "the testboard\n  checkout, which is often read-only."
    ))
    while True:
        path = _ask(out, inp, label.capitalize(), default)
        problem = preflight.check_writable_file(path, "the " + label)
        if problem is None:
            _say(out, "  OK - writable.")
            return path
        _say(out, "  " + problem)


def _ask_writable_dir(
    out: TextIO, inp: TextIO, label: str, default: str, why: str
) -> str:
    """Ask for a directory, offer to create it, and prove it can be written."""
    _say(out, "\nThe {0}".format(label))
    _say(out, "  " + why + ".")
    while True:
        path = _ask(out, inp, label.capitalize(), default)
        if not os.path.exists(path) and _ask_yes_no(
            out, inp, "  {0} does not exist. Create it?".format(
                os.path.abspath(path)), default=True,
        ):
            try:
                os.makedirs(path)
            except OSError as exc:
                _say(out, "  could not create it: {0}".format(exc))
                continue
        problem = preflight.check_writable_directory(path, "the " + label)
        if problem is None:
            _say(out, "  OK - writable.")
            return path
        _say(out, "  " + problem)


# ----------------------------------------------------------------------
# Finishing up
# ----------------------------------------------------------------------


def _write(out: TextIO, path: str, settings: Dict[str, Any]) -> None:
    """Write the config file, reporting where it went."""
    try:
        config.write_config(path, settings)
    except config.ConfigError as exc:
        raise Abort(str(exc))
    _say(out, "\n" + _RULE)
    _say(out, "Wrote {0}:".format(os.path.abspath(path)))
    _say(out, "")
    for line in config.dump_config(settings).rstrip("\n").split("\n"):
        _say(out, "  " + line)


def _print_next_steps(
    out: TextIO, path: str, settings: Dict[str, Any]
) -> None:
    """Print the commands this config is meant to be used with."""
    python = os.path.basename(sys.executable) or "python3"
    script = os.path.join(_repo_root(), "run_feeder.py")
    command = "{0} {1} --config {2}".format(
        python, _quote(script), _quote(os.path.abspath(path)))
    _say(out, "\n" + _RULE)
    _say(out, "Try it now, sending nothing:")
    _say(out, "\n  {0} --dry-run\n".format(command))
    _say(out, "Import the history once:")
    _say(out, "\n  {0} --mode backfill\n".format(command))
    if os.name == "nt":
        _say(out, "Then schedule the catchup import (runs at 06:30):")
        _say(out, (
            "\n  schtasks /Create /TN testboard-feeder /SC DAILY /ST 06:30 "
            "/TR \"{0}\"\n".format(command)
        ))
    else:
        _say(out, "Then schedule the catchup import (crontab -e):")
        _say(out, (
            "\n  30 6 * * * {0} >> /var/log/testboard-feeder.log "
            "2>&1\n".format(command)
        ))
    _say(out, (
        "The command carries no paths of its own - everything is in the "
        "config file,\nso it does not matter which directory the scheduler "
        "runs it from."
    ))
    _say(out, _RULE)


def _repo_root() -> str:
    """Absolute path of the directory holding ``run_feeder.py``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _quote(path: str) -> str:
    """Quote a path for a shell command line if it needs it."""
    return '"' + path + '"' if " " in path else path


# ----------------------------------------------------------------------
# Prompting
# ----------------------------------------------------------------------


def _is_tty(stream: TextIO) -> bool:
    """True when ``stream`` is an interactive terminal."""
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _say(out: TextIO, message: str) -> None:
    """Write one line to ``out`` and flush, so prompts are not buffered."""
    out.write(message + "\n")
    out.flush()


def _ask(
    out: TextIO, inp: TextIO, question: str, default: Optional[str] = None,
    allow_blank: bool = False,
) -> str:
    """Prompt for a value, offering ``default`` and refusing empty answers."""
    while True:
        if default:
            out.write("{0} [{1}]: ".format(question, default))
        else:
            out.write("{0}: ".format(question))
        out.flush()
        line = inp.readline()
        if not line:
            raise Abort("no more input")
        answer = line.strip()
        if answer:
            return answer
        if default:
            return default
        if allow_blank:
            return ""
        _say(out, "  An answer is needed here.")


def _ask_choice(
    out: TextIO, inp: TextIO, question: str, choices: List[str], default: str
) -> str:
    """Prompt until the answer is one of ``choices``."""
    while True:
        answer = _ask(out, inp, question, default).lower()
        if answer in choices:
            return answer
        _say(out, "  Please answer one of: {0}".format(", ".join(choices)))


def _ask_yes_no(
    out: TextIO, inp: TextIO, question: str, default: bool
) -> bool:
    """Prompt for a yes/no answer, with ``default`` on a bare Return."""
    hint = "Y/n" if default else "y/N"
    while True:
        out.write("{0} [{1}]: ".format(question, hint))
        out.flush()
        line = inp.readline()
        if not line:
            raise Abort("no more input")
        answer = line.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        _say(out, "  Please answer y or n.")


def _stream_logger(out: TextIO) -> logging.Logger:
    """A logger that writes plain indented lines to ``out``.

    The reader check reports through a logger; inside the wizard its
    output should look like the rest of the conversation rather than like
    a log file, so it gets a private logger with no timestamps that does
    not propagate to the root handler.
    """
    logger = logging.getLogger("feeder.init.check")
    logger.handlers = []
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("  %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
