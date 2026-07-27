"""Tests for :mod:`feeder.state` — the daily-mode high-water-mark file.

Covers the save/load round trip, the exact on-disk JSON shape, and every
"treat as absent" path: missing file, unparseable JSON, wrong document
shape, wrong value type, and an invalid timestamp string (each corrupt case
must log a WARNING and return None).
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from feeder.state import load_high_water_mark, save_high_water_mark
from testboard import model


class StateFileTest(unittest.TestCase):
    """Behaviour of load_high_water_mark / save_high_water_mark."""

    def setUp(self) -> None:
        """Create a temp dir for state files, removed on cleanup."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_state_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "feeder_state.json")

    def _write_text(self, text: str) -> None:
        """Write raw text to the state file path."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_round_trip(self) -> None:
        """save then load returns the identical datetime (microseconds kept)."""
        hwm = datetime.datetime(2026, 7, 25, 2, 14, 7, 123456)
        save_high_water_mark(self.path, hwm)
        self.assertEqual(load_high_water_mark(self.path), hwm)

    def test_on_disk_shape_is_exact(self) -> None:
        """The file is JSON {"high_water_mark": "<ISO>"} and nothing else."""
        hwm = datetime.datetime(2026, 7, 25, 2, 14, 7, 123456)
        save_high_water_mark(self.path, hwm)
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload, {"high_water_mark": model.format_iso(hwm)})

    def test_save_leaves_no_temp_file_behind(self) -> None:
        """The atomic-write temp sibling is gone after a successful save."""
        save_high_water_mark(self.path, datetime.datetime(2026, 1, 1))
        self.assertEqual(
            sorted(os.listdir(self.tmp)), ["feeder_state.json"]
        )

    def test_save_overwrites_previous_mark(self) -> None:
        """A second save replaces the first mark."""
        save_high_water_mark(self.path, datetime.datetime(2026, 1, 1))
        newer = datetime.datetime(2026, 7, 1, 2, 0, 0)
        save_high_water_mark(self.path, newer)
        self.assertEqual(load_high_water_mark(self.path), newer)

    def test_absent_file_returns_none(self) -> None:
        """A missing state file means 'no mark yet' (first run)."""
        self.assertIsNone(load_high_water_mark(self.path))

    def test_unparseable_json_returns_none_and_logs(self) -> None:
        """Garbage content -> WARNING naming the path, and None."""
        self._write_text("{not json at all")
        with self.assertLogs("feeder.state", level="WARNING") as captured:
            self.assertIsNone(load_high_water_mark(self.path))
        self.assertIn(self.path, captured.output[0])
        self.assertIn("corrupt", captured.output[0])

    def test_wrong_document_shape_returns_none_and_logs(self) -> None:
        """Valid JSON that is not the expected object -> None + WARNING."""
        for content in ('[1, 2, 3]', '"just a string"', '{}',
                        '{"other_key": "2026-01-01T00:00:00.000000"}'):
            self._write_text(content)
            with self.assertLogs("feeder.state", level="WARNING"):
                self.assertIsNone(load_high_water_mark(self.path))

    def test_wrong_value_type_returns_none_and_logs(self) -> None:
        """A non-string mark value -> None + WARNING."""
        self._write_text('{"high_water_mark": 12345}')
        with self.assertLogs("feeder.state", level="WARNING"):
            self.assertIsNone(load_high_water_mark(self.path))

    def test_invalid_timestamp_string_returns_none_and_logs(self) -> None:
        """A mark that does not parse as strict ISO -> None + WARNING."""
        for value in ("yesterday", "2026-07-25T02:14:07.123456Z",
                      "2026-07-25 02:14:07"):
            self._write_text(json.dumps({"high_water_mark": value}))
            with self.assertLogs("feeder.state", level="WARNING"):
                self.assertIsNone(load_high_water_mark(self.path))


class UnwritableStateFileTest(unittest.TestCase):
    """Saving the mark must never turn a good import into a bad exit code.

    This is called only *after* an import has succeeded, so the records
    are already in the dashboard. The feeder is also expected to run from
    a checkout it cannot write, with the state file defaulting to the
    working directory — so this path is reached in the ordinary
    deployment, not in some corner case. Raising here would report a
    successful nightly import as a failed scheduled task, and would do it
    with a traceback.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_state_ro_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hwm = datetime.datetime(2026, 7, 25, 2, 14, 7)

    def test_a_good_save_reports_success(self) -> None:
        path = os.path.join(self.tmp, "state.json")
        self.assertTrue(save_high_water_mark(path, self.hwm))

    def test_an_unwritable_path_is_survived_rather_than_raised(self) -> None:
        path = os.path.join(self.tmp, "no-such-directory", "state.json")
        with self.assertLogs("feeder.state", level="ERROR"):
            self.assertFalse(save_high_water_mark(path, self.hwm))

    def test_the_message_says_the_import_itself_worked(self) -> None:
        """Otherwise this reads as data loss, which it is not."""
        path = os.path.join(self.tmp, "no-such-directory", "state.json")
        with self.assertLogs("feeder.state", level="ERROR") as captured:
            save_high_water_mark(path, self.hwm)
        message = "\n".join(captured.output)
        self.assertIn("SUCCEEDED", message)
        self.assertIn("Nothing is lost", message)
        self.assertIn("--state-file", message)

    def test_a_failed_save_leaves_no_temporary_file(self) -> None:
        path = os.path.join(self.tmp, "state.json")
        os.mkdir(path)  # a directory where the file should be
        with self.assertLogs("feeder.state", level="ERROR"):
            self.assertFalse(save_high_water_mark(path, self.hwm))
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_a_failed_save_leaves_the_previous_mark_intact(self) -> None:
        """A lost write must not also destroy what was there before."""
        path = os.path.join(self.tmp, "state.json")
        save_high_water_mark(path, self.hwm)
        blocked = os.path.join(path, "deeper.json")
        with self.assertLogs("feeder.state", level="ERROR"):
            save_high_water_mark(blocked, self.hwm)
        self.assertEqual(load_high_water_mark(path), self.hwm)


if __name__ == "__main__":
    unittest.main()
