"""Unit tests for testboard.model.

Covers: parse_iso good/bad inputs (no fraction, short fraction, timezone
suffixes rejected, garbage), format round-trips, parse_run_record for every
validation rule plus defaults and extra-key tolerance, and end < start.
"""

import datetime
import unittest
from typing import Any, Dict

from testboard import model
from testboard.model import (
    Result,
    RunRecord,
    StoredRun,
    ValidationError,
    duration_seconds,
    format_iso,
    parse_iso,
    parse_run_record,
    run_record_to_dict,
    utcnow,
)


def valid_record() -> Dict[str, Any]:
    """Return a fresh, fully-populated valid transport dict."""
    return {
        "environment": "linux-prod-sim",
        "script": "regression/user_lifecycle.py",
        "test_name": "test_partial_update_retry",
        "result": "FAIL",
        "start_time": "2026-07-25T02:14:07.123456",
        "end_time": "2026-07-25T02:14:09.001000",
        "output": "Traceback (most recent call last):\n  boom\n",
        "source_link": "https://git.example.com/tests/user_lifecycle.py#L120",
        "known_failure_reason": None,
    }


class TestParseIso(unittest.TestCase):
    """parse_iso accepts exactly the transport format, nothing else."""

    def test_full_six_digit_fraction(self) -> None:
        dt = parse_iso("2026-07-25T02:14:07.123456")
        self.assertEqual(
            dt, datetime.datetime(2026, 7, 25, 2, 14, 7, 123456)
        )

    def test_no_fraction(self) -> None:
        dt = parse_iso("2026-07-25T02:14:07")
        self.assertEqual(dt, datetime.datetime(2026, 7, 25, 2, 14, 7))
        self.assertEqual(dt.microsecond, 0)

    def test_short_fractions_right_padded(self) -> None:
        # 1..5 digit fractions are legal and are right-padded with zeros.
        cases = [
            ("2026-01-02T03:04:05.1", 100000),
            ("2026-01-02T03:04:05.12", 120000),
            ("2026-01-02T03:04:05.123", 123000),
            ("2026-01-02T03:04:05.1234", 123400),
            ("2026-01-02T03:04:05.12345", 123450),
        ]
        for text, micro in cases:
            dt = parse_iso(text)
            self.assertEqual(dt.microsecond, micro, msg=text)

    def test_seven_digit_fraction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_iso("2026-01-02T03:04:05.1234567")

    def test_timezone_suffixes_rejected(self) -> None:
        for text in (
            "2026-07-25T02:14:07Z",
            "2026-07-25T02:14:07.123456Z",
            "2026-07-25T02:14:07+00:00",
            "2026-07-25T02:14:07.123456+00:00",
            "2026-07-25T02:14:07-05:00",
        ):
            with self.assertRaises(ValueError, msg=text):
                parse_iso(text)

    def test_garbage_rejected(self) -> None:
        for text in (
            "",
            "yesterday",
            "2026-07-25",
            "02:14:07",
            "2026-07-25 02:14:07",
            "2026-7-25T02:14:07",
            "2026-07-25T02:14:07.",
            "26-07-25T02:14:07",
            " 2026-07-25T02:14:07",
            "2026-07-25T02:14:07 ",
        ):
            with self.assertRaises(ValueError, msg=repr(text)):
                parse_iso(text)

    def test_impossible_calendar_values_rejected(self) -> None:
        for text in (
            "2026-13-01T00:00:00",
            "2026-02-30T00:00:00",
            "2026-07-25T24:00:00",
            "2026-07-25T02:60:07",
        ):
            with self.assertRaises(ValueError, msg=text):
                parse_iso(text)

    def test_non_str_rejected(self) -> None:
        for bad in (None, 1234567890, 12.5, b"2026-07-25T02:14:07", ["x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_iso(bad)  # type: ignore[arg-type]

    def test_error_message_mentions_value(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_iso("nonsense")
        self.assertIn("nonsense", str(ctx.exception))


class TestFormatIso(unittest.TestCase):
    """format_iso always emits six fractional digits and round-trips."""

    def test_six_digit_microseconds_always(self) -> None:
        text = format_iso(datetime.datetime(2026, 7, 25, 2, 14, 7, 0))
        self.assertEqual(text, "2026-07-25T02:14:07.000000")

    def test_round_trip_parse_then_format(self) -> None:
        original = "2026-07-25T02:14:07.123456"
        self.assertEqual(format_iso(parse_iso(original)), original)

    def test_round_trip_format_then_parse(self) -> None:
        dt = datetime.datetime(2026, 12, 31, 23, 59, 59, 999999)
        self.assertEqual(parse_iso(format_iso(dt)), dt)

    def test_short_fraction_normalizes_via_round_trip(self) -> None:
        self.assertEqual(
            format_iso(parse_iso("2026-01-02T03:04:05.5")),
            "2026-01-02T03:04:05.500000",
        )

    def test_lexical_ordering_matches_chronological(self) -> None:
        earlier = datetime.datetime(2026, 7, 25, 2, 14, 7, 999999)
        later = datetime.datetime(2026, 7, 25, 2, 14, 8, 0)
        self.assertLess(format_iso(earlier), format_iso(later))


class TestClockAndDuration(unittest.TestCase):
    """utcnow returns a naive datetime; duration_seconds is end - start."""

    def test_utcnow_is_naive(self) -> None:
        now = utcnow()
        self.assertIsInstance(now, datetime.datetime)
        self.assertIsNone(now.tzinfo)

    def test_utcnow_close_to_real_utc(self) -> None:
        real = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        )
        delta = abs((utcnow() - real).total_seconds())
        self.assertLess(delta, 5.0)

    def test_duration_seconds(self) -> None:
        start = datetime.datetime(2026, 7, 25, 2, 14, 7, 123456)
        end = datetime.datetime(2026, 7, 25, 2, 14, 9, 1000)
        self.assertAlmostEqual(duration_seconds(start, end), 1.877544)

    def test_duration_zero(self) -> None:
        dt = datetime.datetime(2026, 7, 25, 2, 14, 7)
        self.assertEqual(duration_seconds(dt, dt), 0.0)


class TestResultEnum(unittest.TestCase):
    """Result enum names and values are pinned by the transport contract."""

    def test_members_and_values(self) -> None:
        expected = {
            "PASS": "PASS",
            "FAIL": "FAIL",
            "FAILED_AS_EXPECTED": "FAILED_AS_EXPECTED",
            "UNEXPECTED_PASS": "UNEXPECTED_PASS",
        }
        self.assertEqual({r.name: r.value for r in Result}, expected)


class TestParseRunRecordHappyPath(unittest.TestCase):
    """Valid records parse into fully-typed RunRecords."""

    def test_full_record(self) -> None:
        rec = parse_run_record(valid_record())
        self.assertIsInstance(rec, RunRecord)
        self.assertEqual(rec.environment, "linux-prod-sim")
        self.assertEqual(rec.script, "regression/user_lifecycle.py")
        self.assertEqual(rec.test_name, "test_partial_update_retry")
        self.assertIs(rec.result, Result.FAIL)
        self.assertEqual(
            rec.start_time,
            datetime.datetime(2026, 7, 25, 2, 14, 7, 123456),
        )
        self.assertEqual(
            rec.end_time, datetime.datetime(2026, 7, 25, 2, 14, 9, 1000)
        )
        self.assertTrue(rec.output.startswith("Traceback"))
        self.assertEqual(
            rec.source_link,
            "https://git.example.com/tests/user_lifecycle.py#L120",
        )
        self.assertIsNone(rec.known_failure_reason)

    def test_every_result_value_accepted(self) -> None:
        for name in ("PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS"):
            raw = valid_record()
            raw["result"] = name
            self.assertIs(parse_run_record(raw).result, Result[name], name)

    def test_source_link_defaults_to_empty_string(self) -> None:
        raw = valid_record()
        del raw["source_link"]
        self.assertEqual(parse_run_record(raw).source_link, "")

    def test_known_failure_reason_defaults_to_none(self) -> None:
        raw = valid_record()
        del raw["known_failure_reason"]
        self.assertIsNone(parse_run_record(raw).known_failure_reason)

    def test_known_failure_reason_string_kept(self) -> None:
        raw = valid_record()
        raw["result"] = "FAILED_AS_EXPECTED"
        raw["known_failure_reason"] = "JIRA-123: upstream flaky fixture"
        rec = parse_run_record(raw)
        self.assertEqual(
            rec.known_failure_reason, "JIRA-123: upstream flaky fixture"
        )

    def test_empty_output_allowed(self) -> None:
        raw = valid_record()
        raw["output"] = ""
        self.assertEqual(parse_run_record(raw).output, "")

    def test_extra_keys_ignored(self) -> None:
        raw = valid_record()
        raw["future_field"] = {"nested": True}
        raw["hostname"] = "runner-07"
        rec = parse_run_record(raw)
        self.assertEqual(rec.environment, "linux-prod-sim")
        self.assertFalse(hasattr(rec, "future_field"))

    def test_end_equal_to_start_allowed(self) -> None:
        raw = valid_record()
        raw["end_time"] = raw["start_time"]
        rec = parse_run_record(raw)
        self.assertEqual(rec.start_time, rec.end_time)

    def test_identity_whitespace_preserved_not_stripped(self) -> None:
        # Strip is only used for the emptiness check; the value is kept as-is.
        raw = valid_record()
        raw["test_name"] = "  test_padded  "
        self.assertEqual(parse_run_record(raw).test_name, "  test_padded  ")

    def test_short_fraction_timestamps_accepted(self) -> None:
        raw = valid_record()
        raw["start_time"] = "2026-07-25T02:14:07.1"
        raw["end_time"] = "2026-07-25T02:14:08"
        rec = parse_run_record(raw)
        self.assertEqual(rec.start_time.microsecond, 100000)
        self.assertEqual(rec.end_time.microsecond, 0)


class TestParseRunRecordRejections(unittest.TestCase):
    """Every validation rule rejects with a field-naming ValidationError."""

    def assert_rejects(self, raw: Any, expected_fragment: str) -> None:
        """Assert parse_run_record raises naming the offending field."""
        with self.assertRaises(ValidationError) as ctx:
            parse_run_record(raw)
        self.assertIn(expected_fragment, str(ctx.exception))

    def test_non_dict_rejected(self) -> None:
        for bad in (None, [], "record", 42, ("environment",)):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                parse_run_record(bad)

    def test_identity_fields_missing(self) -> None:
        for field in ("environment", "script", "test_name"):
            raw = valid_record()
            del raw[field]
            self.assert_rejects(raw, field)

    def test_identity_fields_wrong_type(self) -> None:
        for field in ("environment", "script", "test_name"):
            raw = valid_record()
            raw[field] = 7
            self.assert_rejects(raw, field)

    def test_identity_fields_empty(self) -> None:
        for field in ("environment", "script", "test_name"):
            raw = valid_record()
            raw[field] = ""
            self.assert_rejects(raw, field)

    def test_identity_fields_whitespace_only(self) -> None:
        for field in ("environment", "script", "test_name"):
            raw = valid_record()
            raw[field] = "  \t \n "
            self.assert_rejects(raw, field)

    def test_result_missing(self) -> None:
        raw = valid_record()
        del raw["result"]
        self.assert_rejects(raw, "result")

    def test_result_unknown_value(self) -> None:
        raw = valid_record()
        raw["result"] = "BROKE"
        self.assert_rejects(raw, "result: unknown value 'BROKE'")

    def test_result_wrong_case_rejected(self) -> None:
        raw = valid_record()
        raw["result"] = "pass"
        self.assert_rejects(raw, "result")

    def test_result_wrong_type(self) -> None:
        raw = valid_record()
        raw["result"] = 1
        self.assert_rejects(raw, "result")

    def test_times_missing(self) -> None:
        for field in ("start_time", "end_time"):
            raw = valid_record()
            del raw[field]
            self.assert_rejects(raw, field)

    def test_times_unparseable(self) -> None:
        for field in ("start_time", "end_time"):
            for bad in ("not-a-time", "2026-07-25T02:14:07Z", 1234, None):
                raw = valid_record()
                raw[field] = bad
                self.assert_rejects(raw, field)

    def test_end_before_start_rejected(self) -> None:
        raw = valid_record()
        raw["start_time"] = "2026-07-25T02:14:09.000000"
        raw["end_time"] = "2026-07-25T02:14:07.000000"
        self.assert_rejects(raw, "end_time")

    def test_output_missing(self) -> None:
        raw = valid_record()
        del raw["output"]
        self.assert_rejects(raw, "output")

    def test_output_wrong_type(self) -> None:
        for bad in (None, 3, ["line"]):
            raw = valid_record()
            raw["output"] = bad
            self.assert_rejects(raw, "output")

    def test_source_link_wrong_type(self) -> None:
        for bad in (None, 3, ["url"]):
            raw = valid_record()
            raw["source_link"] = bad
            self.assert_rejects(raw, "source_link")

    def test_known_failure_reason_wrong_type(self) -> None:
        for bad in (3, ["reason"], {"why": "x"}):
            raw = valid_record()
            raw["known_failure_reason"] = bad
            self.assert_rejects(raw, "known_failure_reason")


class TestRunRecordToDict(unittest.TestCase):
    """run_record_to_dict emits the exact transport shape and round-trips."""

    def test_exact_shape(self) -> None:
        rec = parse_run_record(valid_record())
        out = run_record_to_dict(rec)
        self.assertEqual(out, valid_record())

    def test_keys_exact(self) -> None:
        out = run_record_to_dict(parse_run_record(valid_record()))
        self.assertEqual(
            set(out.keys()),
            {
                "environment",
                "script",
                "test_name",
                "result",
                "start_time",
                "end_time",
                "output",
                "source_link",
                "known_failure_reason",
            },
        )

    def test_result_serialized_as_value_string(self) -> None:
        out = run_record_to_dict(parse_run_record(valid_record()))
        self.assertEqual(out["result"], "FAIL")
        self.assertIsInstance(out["result"], str)

    def test_round_trip_dict_record_dict(self) -> None:
        raw = valid_record()
        raw["result"] = "FAILED_AS_EXPECTED"
        raw["known_failure_reason"] = "known since 2025"
        first = parse_run_record(raw)
        again = parse_run_record(run_record_to_dict(first))
        self.assertEqual(first, again)

    def test_defaults_appear_in_dict(self) -> None:
        raw = valid_record()
        del raw["source_link"]
        del raw["known_failure_reason"]
        out = run_record_to_dict(parse_run_record(raw))
        self.assertEqual(out["source_link"], "")
        self.assertIsNone(out["known_failure_reason"])

    def test_timestamps_normalized_to_six_digits(self) -> None:
        raw = valid_record()
        raw["start_time"] = "2026-07-25T02:14:07.5"
        raw["end_time"] = "2026-07-25T02:14:08"
        out = run_record_to_dict(parse_run_record(raw))
        self.assertEqual(out["start_time"], "2026-07-25T02:14:07.500000")
        self.assertEqual(out["end_time"], "2026-07-25T02:14:08.000000")


class TestStoredRun(unittest.TestCase):
    """StoredRun carries the DB id and an optional output field."""

    def test_fields_and_optional_output(self) -> None:
        run = StoredRun(
            run_id=7,
            environment="linux-prod-sim",
            script="regression/user_lifecycle.py",
            test_name="test_partial_update_retry",
            result=Result.PASS,
            start_time=datetime.datetime(2026, 7, 25, 2, 14, 7),
            end_time=datetime.datetime(2026, 7, 25, 2, 14, 9),
            source_link="",
            known_failure_reason=None,
            output=None,
        )
        self.assertEqual(run.run_id, 7)
        self.assertIsNone(run.output)
        detailed = run._replace(output="full output here")
        self.assertEqual(detailed.output, "full output here")

    def test_field_order_pinned(self) -> None:
        self.assertEqual(
            StoredRun._fields,
            (
                "run_id",
                "environment",
                "script",
                "test_name",
                "result",
                "start_time",
                "end_time",
                "source_link",
                "known_failure_reason",
                "output",
            ),
        )


class TestModuleMeta(unittest.TestCase):
    """Package metadata and pinned constants."""

    def test_version(self) -> None:
        import testboard

        self.assertEqual(testboard.__version__, "1.0.0")

    def test_time_format_constant(self) -> None:
        self.assertEqual(model.TIME_FORMAT, "%Y-%m-%dT%H:%M:%S.%f")


if __name__ == "__main__":
    unittest.main()
