"""Reader abstractions for the testboard feeder.

A :class:`Reader` turns some site-specific data source (log files, a CI
database, exported artifacts, ...) into an iterator of raw transport dicts in
the ``/api/import`` RunRecord schema. Validation happens later, in
:mod:`feeder.submitter` — a reader's only contract is "yield dicts, never let
one bad record kill the whole import".

This module ships the built-in :class:`JsonLinesReader` (one JSON object per
line) and :func:`load_reader`, which resolves the ``--reader`` CLI spec into a
Reader instance — either the built-in ``jsonl`` reader or a site-specific
``module.path:factory`` entry point.

Python 3.6 compatible; standard library only.
"""

import abc
import datetime
import glob
import importlib
import importlib.util
import json
import logging
import os
import sys
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

#: The contract a ``module.path:factory`` reader spec must satisfy. Quoted in
#: every load-failure message so the fix is obvious from the log alone.
FACTORY_SIGNATURE = "factory(sources: List[str]) -> feeder.reader.Reader"

#: The two accepted shapes of ``--reader``, quoted whenever one fails to
#: load. The file-path form exists because the feeder commonly runs from a
#: checkout it cannot write to, so dropping a module into the repo root —
#: the obvious answer, and the one the old message gave — is not an option.
SPEC_FORMS = (
    "'jsonl' (built-in), "
    "'/abs/path/to/internal_reader.py:create_reader' (a file anywhere on "
    "this machine), or "
    "'module.path:create_reader' (importable from the testboard checkout "
    "or PYTHONPATH)"
)

_MAX_LOGGED_CHARS = 200

#: File extensions collected when a ``--source`` names a directory.
_DATA_SUFFIXES = (".jsonl", ".json", ".ndjson")


class ReaderLoadError(Exception):
    """Raised when :func:`load_reader` cannot construct a Reader.

    The message is user-facing and actionable: it names the spec that was
    tried, what went wrong, and the expected factory contract.
    """


class Reader(abc.ABC):
    """Abstract source of raw run-record dicts.

    Site-specific readers subclass this. Implementations may *over-return*
    (yield records older than ``since``); the submitter/CLI filter by
    ``since`` again, so ``since`` is purely an optimization hint.
    """

    @abc.abstractmethod
    def read(self, since: Optional[datetime.datetime]) -> Iterator[Dict[str, Any]]:
        """Yield raw transport dicts (RunRecord schema, see /api/import).

        ``since`` is a lower bound hint (naive UTC): records with
        ``start_time`` earlier than it will be discarded downstream, so a
        reader may skip work by not yielding them — but it does not have to.

        Implementations must NOT raise on a bad record: log a WARNING with
        enough detail to find the record at its source, skip it, continue.
        """


def _truncate(text: str, limit: int = _MAX_LOGGED_CHARS) -> str:
    """Truncate ``text`` for logging, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


class JsonLinesReader(Reader):
    """Reads run records from JSON-lines files: one JSON object per line.

    ``sources`` is a list of file paths and/or glob patterns (e.g.
    ``results/*.jsonl``). Blank lines are skipped silently; malformed or
    non-object lines are logged at WARNING (with file name, line number and a
    truncated copy of the line) and skipped.
    """

    def __init__(self, sources: List[str]) -> None:
        """Remember the source paths/globs; nothing is opened until read()."""
        self._sources = list(sources)

    def read(self, since: Optional[datetime.datetime]) -> Iterator[Dict[str, Any]]:
        """Yield every parseable JSON object from every matched source file.

        ``since`` is ignored (this reader over-returns; the submitter filters
        by ``since``). Files are read in sorted order per source pattern.
        """
        for path in self._expand_sources():
            try:
                stream = open(path, "r", encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning(
                    "cannot open source file %s (%s); skipping it", path, exc
                )
                continue
            with stream:
                logger.debug("reading %s", path)
                for line_number, line in enumerate(stream, 1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except ValueError as exc:
                        logger.warning(
                            "%s:%d: skipping malformed JSON line (%s): %s",
                            path, line_number, exc, _truncate(stripped),
                        )
                        continue
                    if not isinstance(obj, dict):
                        logger.warning(
                            "%s:%d: skipping non-object JSON line (expected "
                            "one run-record object per line, got %s): %s",
                            path, line_number, type(obj).__name__,
                            _truncate(stripped),
                        )
                        continue
                    yield obj

    def _expand_sources(self) -> List[str]:
        """Expand sources to concrete file paths, warning on empty matches.

        A source may be a file, a glob, or a **directory** — a year of
        history usually arrives as a directory of per-night files, and
        pointing ``--source`` at it is the obvious thing to type. A
        directory is walked recursively for files with a
        :data:`_DATA_SUFFIXES` extension, sorted, so the import order is
        stable and reproducible.
        """
        if not self._sources:
            logger.warning(
                "JsonLinesReader has no sources - nothing to import. "
                "Pass one or more --source FILE_OR_GLOB_OR_DIRECTORY "
                "arguments."
            )
            return []
        paths = []  # type: List[str]
        for source in self._sources:
            matches = sorted(glob.glob(source))
            if not matches:
                logger.warning(
                    "source %r matched no files - check the path/glob "
                    "passed via --source", source
                )
                continue
            for match in matches:
                if os.path.isdir(match):
                    found = self._files_under(match)
                    if not found:
                        logger.warning(
                            "directory %r contains no %s files - check "
                            "the path, or pass a glob if the data files "
                            "have a different extension (e.g. "
                            "--source '%s/*.txt')",
                            match, "/".join(_DATA_SUFFIXES), match,
                        )
                    else:
                        logger.info(
                            "source %r is a directory: found %d data "
                            "file(s) under it", match, len(found),
                        )
                    paths.extend(found)
                else:
                    paths.append(match)
        return paths

    @staticmethod
    def _files_under(directory: str) -> List[str]:
        """Return the data files under *directory*, recursively, sorted."""
        found = []  # type: List[str]
        for root, dirnames, filenames in os.walk(directory):
            dirnames.sort()
            for name in sorted(filenames):
                if name.lower().endswith(_DATA_SUFFIXES):
                    found.append(os.path.join(root, name))
        return found


def _looks_like_a_path(text: str) -> bool:
    """True when *text* is more plausibly a file path than a dotted name."""
    return (
        "/" in text
        or "\\" in text
        or text.startswith(".")
        or (len(text) > 1 and text[1] == ":")  # a Windows drive letter
    )


def _load_module_from_file(path: str, spec: str) -> Any:
    """Import the Python file at *path* as a standalone module.

    The file's directory is prepended to ``sys.path`` first, so a reader
    split across a few files (``internal_reader.py`` plus its helpers) works
    without the author having to package it.
    """
    absolute = os.path.abspath(path)
    if not os.path.exists(absolute):
        raise ReaderLoadError(
            "cannot load reader '{spec}': no such file '{path}'. The part "
            "before the ':' must be the path to your reader's .py file and "
            "the part after it the factory function inside it, e.g. "
            "'{guess}:create_reader' where {sig}".format(
                spec=spec, path=absolute,
                guess=os.path.join(os.path.dirname(absolute) or ".",
                                   "internal_reader.py"),
                sig=FACTORY_SIGNATURE,
            )
        )
    if not os.path.isfile(absolute):
        raise ReaderLoadError(
            "cannot load reader '{spec}': '{path}' is a directory, not a "
            "Python file. Name the .py file itself, e.g. "
            "'{path}/internal_reader.py:create_reader'".format(
                spec=spec, path=absolute)
        )
    directory = os.path.dirname(absolute)
    if directory and directory not in sys.path:
        sys.path.insert(0, directory)
    module_name = os.path.splitext(os.path.basename(absolute))[0]
    if module_name in sys.modules:
        # A reader called json.py or logging.py would otherwise be
        # installed over the real one for everything imported after it.
        logger.debug(
            "a module named %r is already loaded; registering the reader "
            "under a private name instead", module_name)
        module_name = "_testboard_reader_" + module_name
    try:
        module_spec = importlib.util.spec_from_file_location(
            module_name, absolute)
        if module_spec is None or module_spec.loader is None:
            raise ImportError("not importable as a Python module")
        module = importlib.util.module_from_spec(module_spec)
        # Registered before exec so that a reader doing `import <itself>`
        # or using pickle/dataclass-style module lookups resolves to this
        # very object rather than importing a second copy.
        sys.modules[module_name] = module
        module_spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ReaderLoadError(
            "cannot load reader '{spec}': executing '{path}' raised "
            "{etype}: {err}. The file must import cleanly on its own - try "
            "'python3 {path}' to see the failure in isolation. Anything it "
            "imports must be installed for the Python running the feeder, "
            "or sit next to it (its directory is on the import "
            "path)".format(
                spec=spec, path=absolute, etype=type(exc).__name__, err=exc,
            )
        )
    return module


def _load_module_by_name(module_name: str, spec: str) -> Any:
    """Import a dotted module name, explaining the search path on failure."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ReaderLoadError(
            "cannot load reader '{spec}': importing module '{mod}' failed "
            "({err}). A dotted name is searched on Python's import path, "
            "which does NOT include the directory you happen to be in - it "
            "is the directory holding run_feeder.py, plus PYTHONPATH. If "
            "your reader lives elsewhere (for instance because the "
            "checkout is read-only), give its path instead: "
            "--reader /path/to/{mod}.py:{attr} - where {sig}".format(
                spec=spec, mod=module_name, err=exc,
                attr=spec.rpartition(":")[2].strip() or "create_reader",
                sig=FACTORY_SIGNATURE,
            )
        )


def load_reader(spec: str, sources: List[str]) -> Reader:
    """Resolve a ``--reader`` spec into a :class:`Reader` instance.

    ``spec`` is one of:

    - ``"jsonl"`` — the built-in :class:`JsonLinesReader`;
    - ``"/path/to/internal_reader.py:create_reader"`` — a Python file
      anywhere on this machine, loaded directly. This is the form to use
      when the feeder runs from a checkout it cannot write to, since the
      reader then has nowhere to live inside the repository;
    - ``"module.path:create_reader"`` — a dotted module name resolved on
      the normal import path.

    In both factory forms ``factory(sources)`` must return a :class:`Reader`.

    Raises:
        ReaderLoadError: with an actionable message on any failure (unknown
            spec, missing file, import failure, missing/uncallable
            attribute, factory error, or a factory returning something that
            is not a Reader).
    """
    if spec == "jsonl":
        return JsonLinesReader(sources)
    # Checked before the colon test below: a Windows path carries a colon
    # of its own ('C:\\readers\\r.py'), so "has a colon" does not mean "has
    # a factory name", and splitting one at the drive letter produces a
    # baffling error about a module called 'C'.
    if spec.lower().endswith(".py"):
        raise ReaderLoadError(
            "cannot load reader '{spec}': this is a file path with no "
            "factory function on the end. Append ':' and the name of the "
            "function in that file which builds the reader, e.g. "
            "'{spec}:create_reader' where {sig}".format(
                spec=spec, sig=FACTORY_SIGNATURE)
        )
    if ":" not in spec:
        hint = ""
        if _looks_like_a_path(spec):
            hint = (" It looks like you gave a path - name the .py file "
                    "itself and the factory in it, e.g. "
                    "'{0}.py:create_reader'.".format(spec))
        raise ReaderLoadError(
            "cannot load reader '{spec}': a reader spec is one of "
            "{forms}.{hint} The factory must be {sig}".format(
                spec=spec, forms=SPEC_FORMS, hint=hint,
                sig=FACTORY_SIGNATURE,
            )
        )
    # rpartition, not partition: a Windows path ('C:\\readers\\r.py') has a
    # colon of its own, and only the last one separates off the factory.
    module_name, _, attr_name = spec.rpartition(":")
    module_name = module_name.strip()
    attr_name = attr_name.strip()
    if not module_name or not attr_name:
        raise ReaderLoadError(
            "cannot load reader '{spec}': both a module/file and a factory "
            "name are required, e.g. "
            "'/opt/testboard/internal_reader.py:create_reader' where "
            "{sig}".format(spec=spec, sig=FACTORY_SIGNATURE)
        )
    if module_name.lower().endswith(".py"):
        module = _load_module_from_file(module_name, spec)
    elif _looks_like_a_path(module_name):
        # A path, but not at a .py file: almost always the extension
        # left off. Saying so beats trying to import it as a dotted name
        # and reporting that 'opt/readers/internal_reader' is not a module.
        raise ReaderLoadError(
            "cannot load reader '{spec}': '{mod}' looks like a file path "
            "but does not end in '.py'. Name the Python file itself, e.g. "
            "'{mod}.py:{attr}' where {sig}".format(
                spec=spec, mod=module_name, attr=attr_name,
                sig=FACTORY_SIGNATURE)
        )
    else:
        module = _load_module_by_name(module_name, spec)
    try:
        factory = getattr(module, attr_name)
    except AttributeError:
        available = ", ".join(
            sorted(name for name in dir(module) if not name.startswith("_"))[:15]
        ) or "<none>"
        raise ReaderLoadError(
            "cannot load reader '{spec}': module '{mod}' has no attribute "
            "'{attr}'. Define {sig} in that module. Available names: "
            "{names}".format(
                spec=spec, mod=module_name, attr=attr_name,
                sig=FACTORY_SIGNATURE, names=available,
            )
        )
    if not callable(factory):
        raise ReaderLoadError(
            "cannot load reader '{spec}': '{attr}' in module '{mod}' is "
            "not callable (it is {type}). It must be a factory function "
            "with signature {sig}".format(
                spec=spec, attr=attr_name, mod=module_name,
                type=type(factory).__name__, sig=FACTORY_SIGNATURE,
            )
        )
    try:
        reader = factory(list(sources))
    except Exception as exc:
        raise ReaderLoadError(
            "cannot load reader '{spec}': calling {attr}({sources!r}) "
            "raised {etype}: {err}. The factory must accept the list of "
            "--source values and return a Reader, i.e. {sig}".format(
                spec=spec, attr=attr_name, sources=list(sources),
                etype=type(exc).__name__, err=exc, sig=FACTORY_SIGNATURE,
            )
        )
    if not isinstance(reader, Reader):
        raise ReaderLoadError(
            "cannot load reader '{spec}': {attr}(...) returned {type} "
            "instead of a feeder.reader.Reader instance. The factory must "
            "satisfy {sig} (subclass feeder.reader.Reader and implement "
            "read(since))".format(
                spec=spec, attr=attr_name, type=type(reader).__name__,
                sig=FACTORY_SIGNATURE,
            )
        )
    return reader
