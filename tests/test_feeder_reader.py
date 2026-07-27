"""Tests for :mod:`feeder.reader`.

Covers the JSON-lines reader (plain files, glob patterns, blank lines,
malformed lines logged + skipped, unopenable sources) and
:func:`feeder.reader.load_reader` (built-in ``jsonl`` spec, ``module:factory``
specs, and every load-failure path with its actionable error message).
"""

import datetime
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, Iterator, List, Optional

from feeder.reader import (
    FACTORY_SIGNATURE,
    JsonLinesReader,
    Reader,
    ReaderLoadError,
    load_reader,
)


class RecordingReader(Reader):
    """Minimal Reader used as the product of the factory-spec tests."""

    def __init__(self, sources: List[str]) -> None:
        """Remember the sources the factory was called with."""
        self.sources = sources

    def read(self, since: Optional[datetime.datetime]) -> Iterator[Dict[str, Any]]:
        """Yield nothing; only construction is under test."""
        return iter([])


def make_reader(sources: List[str]) -> Reader:
    """Well-behaved factory for ``module:factory`` load_reader tests."""
    return RecordingReader(sources)


def raising_factory(sources: List[str]) -> Reader:
    """Factory that blows up, for the factory-error load_reader test."""
    raise RuntimeError("boom in factory")


def wrong_type_factory(sources: List[str]) -> Any:
    """Factory that returns something that is not a Reader."""
    return {"not": "a reader"}


#: Not callable at all; targeted by the not-callable load_reader test.
NOT_CALLABLE = 42


class JsonLinesReaderTest(unittest.TestCase):
    """Behaviour of the built-in JSON-lines reader."""

    def setUp(self) -> None:
        """Create a temp dir for source files, removed on cleanup."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_reader_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name: str, lines: List[str]) -> str:
        """Write ``lines`` to ``name`` inside the temp dir; return the path."""
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def test_reads_one_object_per_line(self) -> None:
        """Valid JSON objects come back in file order; blanks are skipped."""
        path = self._write("runs.jsonl", [
            '{"test_name": "one"}',
            "",
            "   ",
            '{"test_name": "two"}',
        ])
        reader = JsonLinesReader([path])
        records = list(reader.read(None))
        self.assertEqual(records, [{"test_name": "one"}, {"test_name": "two"}])

    def test_malformed_lines_logged_and_skipped(self) -> None:
        """Broken JSON logs a WARNING naming file + line and is skipped."""
        path = self._write("runs.jsonl", [
            '{"test_name": "good"}',
            "{this is not json",
            '{"test_name": "also good"}',
        ])
        reader = JsonLinesReader([path])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(
            records, [{"test_name": "good"}, {"test_name": "also good"}]
        )
        self.assertEqual(len(captured.output), 1)
        self.assertIn("malformed JSON", captured.output[0])
        self.assertIn(path, captured.output[0])
        self.assertIn(":2:", captured.output[0])

    def test_non_object_lines_logged_and_skipped(self) -> None:
        """Valid JSON that is not an object (list/number) is skipped."""
        path = self._write("runs.jsonl", [
            "[1, 2, 3]",
            "42",
            '{"test_name": "kept"}',
        ])
        reader = JsonLinesReader([path])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(records, [{"test_name": "kept"}])
        self.assertEqual(len(captured.output), 2)
        self.assertIn("non-object JSON", captured.output[0])
        self.assertIn("list", captured.output[0])
        self.assertIn("int", captured.output[1])

    def test_long_malformed_line_is_truncated_in_log(self) -> None:
        """Malformed-line logging truncates huge lines."""
        path = self._write("runs.jsonl", ["{" + "x" * 1000])
        reader = JsonLinesReader([path])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(records, [])
        self.assertIn("...[truncated]", captured.output[0])
        self.assertNotIn("x" * 500, captured.output[0])

    def test_glob_pattern_expands_sorted(self) -> None:
        """A glob source matches multiple files, read in sorted order."""
        self._write("b.jsonl", ['{"n": 2}'])
        self._write("a.jsonl", ['{"n": 1}'])
        pattern = os.path.join(self.tmp, "*.jsonl")
        reader = JsonLinesReader([pattern])
        self.assertEqual(list(reader.read(None)), [{"n": 1}, {"n": 2}])

    def test_multiple_sources_in_argument_order(self) -> None:
        """Multiple --source values are read in the order given."""
        first = self._write("z_first.jsonl", ['{"n": 1}'])
        second = self._write("a_second.jsonl", ['{"n": 2}'])
        reader = JsonLinesReader([first, second])
        self.assertEqual(list(reader.read(None)), [{"n": 1}, {"n": 2}])

    def test_source_matching_no_files_warns(self) -> None:
        """A pattern matching nothing warns (mentioning --source) and yields nothing."""
        missing = os.path.join(self.tmp, "nope-*.jsonl")
        reader = JsonLinesReader([missing])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(records, [])
        self.assertIn("matched no files", captured.output[0])
        self.assertIn("--source", captured.output[0])

    def test_no_sources_at_all_warns(self) -> None:
        """An empty source list warns and yields nothing."""
        reader = JsonLinesReader([])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(records, [])
        self.assertIn("no sources", captured.output[0])

    def test_empty_directory_source_is_reported(self) -> None:
        """A directory with no data files says so, and names the fix."""
        sub_dir = os.path.join(self.tmp, "iamadir")
        os.mkdir(sub_dir)
        good = self._write("good.jsonl", ['{"n": 1}'])
        reader = JsonLinesReader([sub_dir, good])
        with self.assertLogs("feeder.reader", level="WARNING") as captured:
            records = list(reader.read(None))
        self.assertEqual(records, [{"n": 1}])
        self.assertIn("contains no", captured.output[0])

    def test_directory_source_reads_the_files_under_it(self) -> None:
        """A year of history arrives as a directory, so accept one.

        Files are read in sorted order, recursively, so an import is
        reproducible and per-night files land in date order.
        """
        nights = os.path.join(self.tmp, "history")
        os.makedirs(os.path.join(nights, "2026-07"))
        for name, value in (
            ("2026-07/02.jsonl", 2), ("2026-07/01.jsonl", 1),
        ):
            path = os.path.join(nights, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"n": %d}\n' % value)
        # A non-data file in the same tree must be ignored, not parsed.
        with open(os.path.join(nights, "README.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("not a data file\n")

        reader = JsonLinesReader([nights])
        self.assertEqual(list(reader.read(None)), [{"n": 1}, {"n": 2}])

    def test_since_is_ignored(self) -> None:
        """The jsonl reader over-returns; since filtering happens downstream."""
        path = self._write("runs.jsonl", [
            '{"start_time": "2020-01-01T00:00:00.000000"}',
        ])
        reader = JsonLinesReader([path])
        since = datetime.datetime(2026, 1, 1)
        self.assertEqual(len(list(reader.read(since))), 1)


class LoadReaderTest(unittest.TestCase):
    """Behaviour of load_reader for both spec forms and all failure paths."""

    def test_jsonl_spec_returns_jsonl_reader(self) -> None:
        """'jsonl' resolves to a JsonLinesReader over the given sources."""
        reader = load_reader("jsonl", ["a.jsonl"])
        self.assertIsInstance(reader, JsonLinesReader)

    def test_module_factory_spec_success(self) -> None:
        """'module:factory' imports the module and calls factory(sources)."""
        reader = load_reader(
            "tests.test_feeder_reader:make_reader", ["x.log", "y.log"]
        )
        self.assertIsInstance(reader, RecordingReader)
        self.assertEqual(reader.sources, ["x.log", "y.log"])

    def test_spec_without_colon_rejected(self) -> None:
        """A non-jsonl spec without ':' gets the how-to-fix message."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("mystery", [])
        message = str(ctx.exception)
        self.assertIn("mystery", message)
        self.assertIn("jsonl", message)
        self.assertIn(FACTORY_SIGNATURE, message)

    def test_spec_with_empty_module_or_attribute_rejected(self) -> None:
        """':factory' and 'module:' are both rejected with the contract."""
        for spec in (":create_reader", "tests.test_feeder_reader:"):
            with self.assertRaises(ReaderLoadError) as ctx:
                load_reader(spec, [])
            self.assertIn(FACTORY_SIGNATURE, str(ctx.exception))

    def test_import_failure_names_module_and_contract(self) -> None:
        """An unimportable module produces an actionable error."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("no_such_module_xyz:create_reader", [])
        message = str(ctx.exception)
        self.assertIn("no_such_module_xyz", message)
        self.assertIn("import", message)
        self.assertIn(FACTORY_SIGNATURE, message)

    def test_missing_attribute_names_module_attribute_and_contract(self) -> None:
        """A missing factory name reports module, attribute and contract."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("tests.test_feeder_reader:no_such_factory", [])
        message = str(ctx.exception)
        self.assertIn("tests.test_feeder_reader", message)
        self.assertIn("no_such_factory", message)
        self.assertIn(FACTORY_SIGNATURE, message)

    def test_not_callable_attribute_rejected(self) -> None:
        """A non-callable attribute is rejected, naming its type."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("tests.test_feeder_reader:NOT_CALLABLE", [])
        message = str(ctx.exception)
        self.assertIn("not callable", message)
        self.assertIn("int", message)
        self.assertIn(FACTORY_SIGNATURE, message)

    def test_factory_raising_is_wrapped(self) -> None:
        """An exception inside the factory is reported with type + message."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("tests.test_feeder_reader:raising_factory", ["s"])
        message = str(ctx.exception)
        self.assertIn("RuntimeError", message)
        self.assertIn("boom in factory", message)
        self.assertIn(FACTORY_SIGNATURE, message)

    def test_factory_returning_non_reader_rejected(self) -> None:
        """A factory that returns a non-Reader is rejected, naming the type."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("tests.test_feeder_reader:wrong_type_factory", [])
        message = str(ctx.exception)
        self.assertIn("dict", message)
        self.assertIn("Reader", message)
        self.assertIn(FACTORY_SIGNATURE, message)


#: A reader that imports a sibling module, so the file-path loader is shown
#: to put the reader's own directory on the import path.
_SITE_READER = """\
from site_helper import ENVIRONMENT
from feeder.reader import Reader


class SiteReader(Reader):
    def __init__(self, sources):
        self.sources = sources

    def read(self, since):
        return iter([{"environment": ENVIRONMENT}])


def create_reader(sources):
    return SiteReader(sources)
"""


class LoadReaderFromFileTest(unittest.TestCase):
    """Loading a reader from a path rather than an importable module.

    This is not a convenience. The feeder is expected to run against a
    checkout it has only read access to, and the site-specific reader is
    the one piece of code a rollout must supply — so if it can only be
    loaded from somewhere inside the repository, there is nowhere to put
    it and the tool cannot be deployed as specified.
    """

    def setUp(self) -> None:
        """A scratch directory holding a reader and its helper."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_readerfile_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "internal_reader.py")
        self._write(self.path, _SITE_READER)
        self._write(os.path.join(self.tmp, "site_helper.py"),
                    "ENVIRONMENT = 'prod'\n")

    def _write(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_file_path_spec_loads_the_reader(self) -> None:
        reader = load_reader(self.path + ":create_reader", ["a", "b"])
        self.assertIsInstance(reader, Reader)
        self.assertEqual(reader.sources, ["a", "b"])

    def test_the_readers_own_directory_is_importable_from_it(self) -> None:
        """A real reader is rarely one file; its helpers must resolve."""
        reader = load_reader(self.path + ":create_reader", [])
        self.assertEqual(list(reader.read(None)), [{"environment": "prod"}])

    def test_a_relative_path_works_too(self) -> None:
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        reader = load_reader("internal_reader.py:create_reader", [])
        self.assertIsInstance(reader, Reader)

    def test_a_missing_file_names_the_absolute_path_it_tried(self) -> None:
        missing = os.path.join(self.tmp, "absent.py")
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(missing + ":create_reader", [])
        message = str(ctx.exception)
        self.assertIn(os.path.abspath(missing), message)
        self.assertIn("no such file", message)

    def test_a_path_with_no_factory_says_which_half_is_missing(self) -> None:
        """A Windows path has a colon of its own, so this needs its own test."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(self.path, [])
        message = str(ctx.exception)
        self.assertIn("no factory function", message)
        self.assertIn(self.path + ":create_reader", message)

    def test_a_windows_style_path_is_not_split_at_the_drive_letter(
        self
    ) -> None:
        """Splitting at the first colon reports a module called 'C'."""
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(r"C:\readers\internal_reader.py:create_reader", [])
        message = str(ctx.exception)
        self.assertIn("internal_reader.py", message)
        self.assertNotIn("module 'C'", message)

    def test_a_path_missing_the_py_extension_is_told_so(self) -> None:
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(os.path.join(self.tmp, "internal_reader") +
                        ":create_reader", [])
        self.assertIn("does not end in '.py'", str(ctx.exception))

    def test_a_directory_is_not_a_reader(self) -> None:
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(self.tmp + ".py:create_reader", [])
        self.assertIn("no such file", str(ctx.exception))

    def test_a_reader_that_fails_to_import_reports_the_real_error(
        self
    ) -> None:
        broken = os.path.join(self.tmp, "broken_reader.py")
        self._write(broken, "import a_module_that_is_not_installed\n")
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader(broken + ":create_reader", [])
        message = str(ctx.exception)
        self.assertIn("a_module_that_is_not_installed", message)
        self.assertIn(os.path.abspath(broken), message)

    def test_a_dotted_spec_that_fails_offers_the_file_path_form(self) -> None:
        """The old message told people to put it in the repo root.

        That is the one place a read-only checkout forbids, and it was
        wrong about the working directory besides. The remedy has to be
        the form that actually works.
        """
        with self.assertRaises(ReaderLoadError) as ctx:
            load_reader("no_such_site_reader:create_reader", [])
        message = str(ctx.exception)
        self.assertIn(
            "--reader /path/to/no_such_site_reader.py:create_reader", message)
        self.assertIn("read-only", message)

    def test_a_reader_named_after_a_stdlib_module_does_not_shadow_it(
        self
    ) -> None:
        """Registering it as 'json' would replace the real one for good."""
        impostor = os.path.join(self.tmp, "json.py")
        self._write(impostor,
                    "from feeder.reader import Reader\n\n\n"
                    "class R(Reader):\n"
                    "    def read(self, since):\n"
                    "        return iter([])\n\n\n"
                    "def create_reader(sources):\n"
                    "    return R()\n")
        load_reader(impostor + ":create_reader", [])
        import json as real_json
        self.assertTrue(hasattr(real_json, "loads"))
        self.assertIs(sys.modules["json"], real_json)


if __name__ == "__main__":
    unittest.main()
