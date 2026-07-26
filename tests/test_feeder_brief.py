"""The Copilot brief must stay true, because nobody re-reads it.

``docs/FEEDER_BRIEF.md`` is handed to an AI assistant as the ONLY
description of what to build — it is attached to a prompt alongside two
samples of the internal data format, with no repository access assumed.
If it drifts from the code, the reader that comes back is wrong in ways
that are expensive to debug from the other side of that hand-off.

So the brief is tested like code:

- its worked example is extracted and executed verbatim, and must pass
  the very check the brief tells the reader's author to run;
- every command-line flag it mentions must actually exist.

Python 3.6 compatible; standard library only.
"""

import importlib.util
import logging
import os
import re
import tempfile
import unittest
from typing import Any, Dict, List

import run_feeder
from feeder.check import check_reader
from testboard.model import Result

BRIEF_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs", "FEEDER_BRIEF.md",
)

#: The CSV shown in the brief, plus rows that must be skipped rather than
#: raised on (an outcome the mapping does not know, an unparseable date).
_EXPORT_CSV = """\
run_id,suite,case,outcome,started,duration_ms,logfile,defect
88213,user_lifecycle,partial_update_retry,FAILED,2026-07-25 03:14:07,1878,,JIRA-4821
88214,user_lifecycle,cancel_retry,OK,2026-07-25 03:14:09,942,,
88215,user_lifecycle,mystery,NO_SUCH_OUTCOME,2026-07-25 03:14:11,100,,
88216,user_lifecycle,broken,OK,not-a-timestamp,100,,
"""


def read_brief() -> str:
    """Return the brief's text."""
    with open(BRIEF_PATH, encoding="utf-8") as handle:
        return handle.read()


class BriefWorkedExampleTest(unittest.TestCase):
    """The example reader printed in the brief must actually work."""

    def setUp(self) -> None:
        """Extract the example to an importable module and write inputs."""
        # The example deliberately logs warnings for the rows it skips;
        # they are the point of the test, not something to print.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp(prefix="testboard_brief_")
        blocks = re.findall(r"```python\n(.*?)```", read_brief(), re.S)
        examples = [
            block for block in blocks
            if "class InternalReader" in block and "csv" in block
        ]
        self.assertEqual(
            len(examples), 1,
            "the brief must contain exactly one complete worked example",
        )
        module_path = os.path.join(self.tmp, "brief_example_reader.py")
        with open(module_path, "w", encoding="utf-8") as handle:
            handle.write(examples[0])
        self.csv_path = os.path.join(self.tmp, "export.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(_EXPORT_CSV)

        spec = importlib.util.spec_from_file_location(
            "brief_example_reader", module_path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self) -> None:
        """Remove the scratch directory (best effort, Windows-safe)."""
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def records(self) -> List[Dict[str, Any]]:
        reader = self.module.create_reader([self.csv_path])
        return list(reader.read(None))

    def test_example_passes_the_check_the_brief_prescribes(self) -> None:
        """`--check-reader` on the example must report a clean reader."""
        report = check_reader(iter(self.records()))
        self.assertEqual(report.invalid, 0)
        self.assertEqual(report.read, 2)
        self.assertTrue(report.ok)
        self.assertEqual(report.warnings, [])

    def test_example_converts_local_time_to_utc(self) -> None:
        """The headline trap: 03:14:07 local (BST) is 02:14:07 UTC.

        The brief tells the author this conversion is their job; the
        example must demonstrate it rather than pass local time through.
        """
        starts = sorted(record["start_time"] for record in self.records())
        self.assertEqual(starts[0], "2026-07-25T02:14:07.000000")

    def test_example_maps_outcomes_rather_than_passing_them_through(
        self
    ) -> None:
        results = {record["result"] for record in self.records()}
        self.assertEqual(results, {"FAIL", "PASS"})
        for record in self.records():
            Result(record["result"])  # must be a real Result value

    def test_example_skips_bad_rows_instead_of_raising(self) -> None:
        """Two of the four rows are unusable; neither may kill the read."""
        records = self.records()
        self.assertEqual(len(records), 2)
        names = {record["test_name"] for record in records}
        self.assertNotIn("test_mystery", names)
        self.assertNotIn("test_broken", names)

    def test_example_carries_the_known_failure_reason(self) -> None:
        by_name = {r["test_name"]: r for r in self.records()}
        self.assertEqual(
            by_name["test_partial_update_retry"]["known_failure_reason"],
            "JIRA-4821",
        )
        self.assertIsNone(
            by_name["test_cancel_retry"]["known_failure_reason"])


class BriefAccuracyTest(unittest.TestCase):
    """Claims the brief makes about the CLI must be true."""

    def test_every_flag_the_brief_mentions_exists(self) -> None:
        brief = read_brief()
        parser = run_feeder.build_parser()
        known = set()
        for action in parser._actions:  # noqa: SLF001 - argparse has no API
            known.update(action.option_strings)
        mentioned = set(re.findall(r"`(--[a-z][a-z-]*)`", brief))
        # --source is documented with a value placeholder in places.
        missing = sorted(flag for flag in mentioned if flag not in known)
        self.assertEqual(
            missing, [],
            "the brief documents flags run_feeder.py does not accept",
        )

    def test_brief_names_the_reader_file_and_factory(self) -> None:
        brief = read_brief()
        self.assertIn("internal_reader.py", brief)
        self.assertIn("create_reader", brief)
        self.assertIn("internal_reader:create_reader", brief)

    def test_brief_states_the_transport_schema_fields(self) -> None:
        """Every field the server validates must be described."""
        brief = read_brief()
        for field in ("environment", "script", "test_name", "result",
                      "start_time", "end_time", "output", "source_link",
                      "known_failure_reason"):
            self.assertIn(field, brief, "brief omits field " + field)

    def test_brief_lists_every_result_value(self) -> None:
        brief = read_brief()
        for result in Result:
            self.assertIn(result.name, brief)

    def test_brief_is_self_contained_for_a_single_attachment(self) -> None:
        """It is attached alone, so it must say so and stand alone."""
        brief = read_brief()
        self.assertIn("self-contained", brief.lower())
        # The interface it must implement, and the check that proves it.
        self.assertIn("--check-reader", brief)
        self.assertIn("class InternalReader(Reader)", brief)


if __name__ == "__main__":
    unittest.main()
