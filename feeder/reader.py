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
import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

#: The contract a ``module.path:factory`` reader spec must satisfy. Quoted in
#: every load-failure message so the fix is obvious from the log alone.
FACTORY_SIGNATURE = "factory(sources: List[str]) -> feeder.reader.Reader"

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


def load_reader(spec: str, sources: List[str]) -> Reader:
    """Resolve a ``--reader`` spec into a :class:`Reader` instance.

    ``spec`` is either the built-in ``"jsonl"`` (returns
    ``JsonLinesReader(sources)``) or ``"module.path:factory"``, in which case
    ``module.path`` is imported and ``factory(sources)`` must return a
    :class:`Reader`.

    Raises:
        ReaderLoadError: with an actionable message on any failure (unknown
            spec, import failure, missing/uncallable attribute, factory
            error, or a factory returning something that is not a Reader).
    """
    if spec == "jsonl":
        return JsonLinesReader(sources)
    if ":" not in spec:
        raise ReaderLoadError(
            "cannot load reader '{spec}': unknown reader spec. Use the "
            "built-in 'jsonl' reader, or 'module.path:factory' naming a "
            "factory function with signature {sig}".format(
                spec=spec, sig=FACTORY_SIGNATURE
            )
        )
    module_name, _, attr_name = spec.partition(":")
    module_name = module_name.strip()
    attr_name = attr_name.strip()
    if not module_name or not attr_name:
        raise ReaderLoadError(
            "cannot load reader '{spec}': both a module and an attribute "
            "are required, e.g. 'internal_reader:create_reader' where "
            "{sig}".format(spec=spec, sig=FACTORY_SIGNATURE)
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ReaderLoadError(
            "cannot load reader '{spec}': importing module '{mod}' failed "
            "({err}). Check that the module is importable from the "
            "directory you run the feeder in (e.g. a file '{mod}.py' in "
            "the repo root) and that the spec is 'module.path:factory' "
            "where {sig}".format(
                spec=spec, mod=module_name, err=exc, sig=FACTORY_SIGNATURE
            )
        )
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
