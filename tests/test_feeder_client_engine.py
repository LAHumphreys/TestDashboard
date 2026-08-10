"""Direct unit tests for clients/feeder.py's internal functions (WP-29).

clients/feeder.tcl carries a ``--self-test`` mode (run as one scenario in
tests/test_feeder_engines_conformance.py) because Tcl has no assumed test
framework and the file has to be self-contained anyway - that gives its
hand-built JSON encoder/parser and validate_record direct, fine-grained
coverage independent of the conformance suite's black-box scenarios.
clients/feeder.py has no equivalent of its own: this module is it, loading
the file directly (it is plain, importable Python 3.6 once the module-level
guard is respected) and exercising validate_record and the JSON helpers the
same way the Tcl self-test does, so neither engine's internals are checked
only end-to-end through a subprocess.

Loaded via importlib rather than a normal ``import`` - "clients" is
deliberately not a package (no ``__init__.py``; see
tests/test_python36_compat.py's CLIENT_ENGINES comment) and is not one of
the packages tests.test_python36_compat.AnnotationsEvaluateTest sweeps, so
this is the one place the file's functions are exercised at all beyond
static analysis and subprocess conformance.

Python 3.6 compatible; standard library only.
"""

import importlib.util
import os
import unittest
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDER_PY = os.path.join(REPO_ROOT, "clients", "feeder.py")


def _load_client_feeder() -> Any:
    spec = importlib.util.spec_from_file_location("client_feeder", FEEDER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidateRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.feeder = _load_client_feeder()

    def _good(self, **overrides: Any) -> dict:
        base = {
            "environment": "e", "script": "s", "test_name": "t",
            "result": "PASS",
            "start_time": "2026-01-01T00:00:00.000000",
            "end_time": "2026-01-01T00:00:01.000000",
            "output": "",
        }
        base.update(overrides)
        return base

    def test_accepts_a_good_record(self) -> None:
        record = self.feeder.validate_record(self._good())
        self.assertEqual(record["environment"], "e")
        self.assertNotIn("build", record)

    def test_rejects_unknown_result(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(self._good(result="NOPE"))

    def test_rejects_branch_key_present_or_null(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(self._good(branch="x"))
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(self._good(branch=None))

    def test_rejects_end_before_start(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(self._good(
                start_time="2026-01-01T00:00:02.000000",
                end_time="2026-01-01T00:00:01.000000",
            ))

    def test_rejects_timezone_suffixed_timestamp(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(
                self._good(start_time="2026-01-01T00:00:00.000000Z")
            )

    def test_rejects_impossible_calendar_date(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(
                self._good(start_time="2026-02-30T00:00:00.000000")
            )

    def test_normalizes_blank_known_failure_reason_to_none(self) -> None:
        record = self.feeder.validate_record(
            self._good(known_failure_reason="   ")
        )
        self.assertIsNone(record["known_failure_reason"])

    def test_carries_a_real_known_failure_reason(self) -> None:
        record = self.feeder.validate_record(
            self._good(known_failure_reason="JIRA-1")
        )
        self.assertEqual(record["known_failure_reason"], "JIRA-1")

    def test_build_present_only_when_set(self) -> None:
        mainline = self.feeder.validate_record(self._good())
        self.assertNotIn("build", mainline)
        build = self.feeder.validate_record(self._good(build="rc1"))
        self.assertEqual(build["build"], "rc1")
        blank_build = self.feeder.validate_record(self._good(build="   "))
        self.assertNotIn("build", blank_build)

    def test_rejects_missing_required_field(self) -> None:
        raw = self._good()
        del raw["script"]
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(raw)

    def test_rejects_non_dict_record(self) -> None:
        with self.assertRaises(self.feeder.ValidationError):
            self.feeder.validate_record(["not", "a", "dict"])


class TimestampNormalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.feeder = _load_client_feeder()

    def test_fractional_seconds_padded_to_six_digits(self) -> None:
        record = self.feeder.validate_record({
            "environment": "e", "script": "s", "test_name": "t",
            "result": "PASS",
            "start_time": "2026-01-01T00:00:00.5",
            "end_time": "2026-01-01T00:00:01",
            "output": "",
        })
        self.assertEqual(record["start_time"], "2026-01-01T00:00:00.500000")
        self.assertEqual(record["end_time"], "2026-01-01T00:00:01.000000")


class BatchingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.feeder = _load_client_feeder()

    def test_flushes_at_batch_size(self) -> None:
        records = [{"output": ""} for _ in range(5)]
        batches = list(self.feeder._batches(records, batch_size=2, max_bytes=10 ** 9))
        self.assertEqual([len(b) for b in batches], [2, 2, 1])

    def test_flushes_early_on_byte_ceiling(self) -> None:
        records = [{"output": "x" * 100} for _ in range(3)]
        batches = list(self.feeder._batches(records, batch_size=100, max_bytes=250))
        self.assertGreater(len(batches), 1)


class ReplayFileNamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.feeder = _load_client_feeder()

    def test_sanitize_collapses_unsafe_characters(self) -> None:
        self.assertEqual(
            self.feeder._sanitize("env/with spaces!"), "env-with-spaces"
        )

    def test_sanitize_never_returns_empty(self) -> None:
        self.assertEqual(self.feeder._sanitize("///"), "unnamed")


if __name__ == "__main__":
    unittest.main()
