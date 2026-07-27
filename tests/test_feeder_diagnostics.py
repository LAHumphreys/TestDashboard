"""Tests for what the feeder says when the reader is the thing that broke.

The reader is written on site, increasingly by an assistant, against a
system this repository knows nothing about. Whoever debugs it has the
feeder's output and nothing else — so the difference between
``TypeError: 'NoneType' object is not iterable`` and a sentence naming
which of three distinct mistakes was made is the difference between a
minute and an afternoon.

Three failures are worth telling apart, and each is pinned here:

- ``read()`` never returned anything iterable (usually a missing
  ``return``);
- ``read()`` raised before producing a record (its source is unreachable);
- ``read()`` raised part-way through (one row it could not handle) — the
  case where *how many records came first* is the whole diagnosis.

Python 3.6 compatible; standard library only.
"""

import io
import unittest
from typing import Any, Dict, Iterator, List, Optional

from feeder import identity
from feeder.reader import Reader, ReaderFailed, iter_records


def record(name: str = "test_one", **overrides: Any) -> Dict[str, Any]:
    """A valid transport record."""
    base = {
        "environment": "prod", "script": "suite.py", "test_name": name,
        "result": "PASS", "output": "",
        "start_time": "2026-07-26T03:00:00.000000",
        "end_time": "2026-07-26T03:00:02.000000",
    }
    base.update(overrides)
    return base


class _Reader(Reader):
    """A reader whose read() behaviour is supplied by the test."""

    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    def read(self, since: Any) -> Any:
        return self._behaviour()


def failing(message: str = "the results database is down") -> Any:
    """A read() that raises immediately (not a generator)."""
    def behaviour() -> Any:
        raise RuntimeError(message)
    return behaviour


def crashing_after(count: int) -> Any:
    """A read() that yields ``count`` records and then raises KeyError."""
    def behaviour() -> Iterator[Dict[str, Any]]:
        for index in range(count):
            yield record("test_{0:02d}".format(index))
        raise KeyError("started")
    return behaviour


class NotIterableTest(unittest.TestCase):
    """read() that returned nothing there is anything to read."""

    def diagnose(self, value: Any) -> str:
        reader = _Reader(lambda: value)
        with self.assertRaises(ReaderFailed) as caught:
            list(iter_records(reader, None))
        return str(caught.exception)

    def test_returning_none_names_the_missing_return(self) -> None:
        """The commonest first-draft slip, and the least legible error."""
        message = self.diagnose(None)
        self.assertIn("read() returned None", message)
        self.assertIn("return records", message)

    def test_returning_none_does_not_blame_a_bad_record(self) -> None:
        """The old message sent people looking through their record loop."""
        self.assertNotIn("must never let one", self.diagnose(None))

    def test_returning_some_other_object_names_its_type(self) -> None:
        self.assertIn("returned a int", self.diagnose(42))

    def test_the_message_states_the_contract(self) -> None:
        message = self.diagnose(None)
        self.assertIn("generator", message)
        self.assertIn("yield", message)

    def test_a_plain_list_is_perfectly_acceptable(self) -> None:
        """A reader need not be a generator; anything iterable will do."""
        reader = _Reader(lambda: [record("a"), record("b")])
        self.assertEqual(len(list(iter_records(reader, None))), 2)


class CrashTest(unittest.TestCase):
    """read() that raised, and where in its own progress it did so."""

    def diagnose(self, behaviour: Any) -> ReaderFailed:
        reader = _Reader(behaviour)
        with self.assertRaises(ReaderFailed) as caught:
            list(iter_records(reader, None))
        return caught.exception

    def test_raising_immediately_says_no_records_were_produced(self) -> None:
        message = str(self.diagnose(failing()))
        self.assertIn("raised before it returned anything", message)

    def test_a_crash_reports_how_far_it_got(self) -> None:
        """Record 3 of 5 and record 3 of 900,000 are different bugs."""
        message = str(self.diagnose(crashing_after(4)))
        self.assertIn("already produced 4 record(s)", message)

    def test_a_crash_names_the_last_good_record(self) -> None:
        """So the next row in the source system is the one to look at."""
        message = str(self.diagnose(crashing_after(3)))
        self.assertIn("prod / suite.py / test_02", message)

    def test_crashing_on_the_first_record_says_so_plainly(self) -> None:
        """'? / ? / ?' would be worse than useless here."""
        message = str(self.diagnose(crashing_after(0)))
        self.assertIn("before producing a single record", message)
        self.assertNotIn("? / ?", message)

    def test_the_readers_own_traceback_is_carried(self) -> None:
        """It names the file and line to fix; nothing else does."""
        failure = self.diagnose(crashing_after(2))
        self.assertIn("KeyError", failure.traceback_text)
        self.assertIn("test_feeder_diagnostics.py", failure.traceback_text)

    def test_records_before_the_crash_were_still_delivered(self) -> None:
        """The wrapper must not swallow work already done."""
        reader = _Reader(crashing_after(3))
        seen = []  # type: List[Dict[str, Any]]
        with self.assertRaises(ReaderFailed):
            for item in iter_records(reader, None):
                seen.append(item)
        self.assertEqual(len(seen), 3)


class PassThroughTest(unittest.TestCase):
    """A working reader must be entirely unaffected by the wrapper."""

    def test_records_arrive_unchanged_and_in_order(self) -> None:
        wanted = [record("a"), record("b"), record("c")]
        reader = _Reader(lambda: iter(wanted))
        self.assertEqual(list(iter_records(reader, None)), wanted)

    def test_an_empty_reader_is_not_an_error(self) -> None:
        """Nothing to import is a question for the CLI, not a reader fault."""
        reader = _Reader(lambda: iter([]))
        self.assertEqual(list(iter_records(reader, None)), [])

    def test_laziness_is_preserved(self) -> None:
        """A year of history must not be pulled into memory to be wrapped."""
        produced = []  # type: List[str]

        def behaviour() -> Iterator[Dict[str, Any]]:
            for index in range(1000):
                produced.append(str(index))
                yield record(str(index))

        stream = iter_records(_Reader(behaviour), None)
        next(stream)
        self.assertEqual(len(produced), 1)

    def test_since_reaches_the_reader(self) -> None:
        seen = []  # type: List[Any]

        class Recording(Reader):
            def read(self, since: Any) -> Iterator[Dict[str, Any]]:
                seen.append(since)
                return iter([])

        list(iter_records(Recording(), "a-lower-bound"))
        self.assertEqual(seen, ["a-lower-bound"])


class ShowRecordTest(unittest.TestCase):
    """Printing a record as yielded and as it would be transmitted.

    Aggregates say a reader is self-consistent. Only the records say it is
    correct, and until this existed there was no way to see them short of
    importing for real and looking at the dashboard.
    """

    def shown(self, raw: Any) -> str:
        out = io.StringIO()
        identity.show_record(1, raw, out)
        return out.getvalue()

    def test_the_raw_record_is_printed_as_json(self) -> None:
        text = self.shown(record())
        self.assertIn("as your reader yielded it", text)
        self.assertIn('"test_name": "test_one"', text)

    def test_defaulted_fields_are_called_out(self) -> None:
        """A field the reader omitted is filled in silently at import."""
        text = self.shown(record())
        self.assertIn("as it would be sent", text)
        self.assertIn("filled in with defaults", text)
        self.assertIn("source_link", text)
        self.assertIn("known_failure_reason", text)

    def test_a_field_the_schema_does_not_have_is_called_out(self) -> None:
        """Inventing 'duration_ms' is silent otherwise: it is just dropped."""
        text = self.shown(record(duration_ms=1878))
        self.assertIn("IGNORED (not part of the transport schema)", text)
        self.assertIn("duration_ms", text)

    def test_a_complete_record_says_it_travels_unchanged(self) -> None:
        text = self.shown(record(source_link="", known_failure_reason=None))
        self.assertIn("transmitted unchanged", text)

    def test_an_invalid_record_shows_why_rather_than_a_transport_form(
        self
    ) -> None:
        text = self.shown(record(result="BROKE"))
        self.assertIn("would be REJECTED", text)
        self.assertIn("result", text)

    def test_a_record_that_is_not_a_dict_is_still_shown(self) -> None:
        """The value is the diagnosis when the type is the problem."""
        text = self.shown("prod/suite/test_one PASS")
        self.assertIn("prod/suite/test_one PASS", text)
        self.assertIn("would be REJECTED", text)


class DescribeTest(unittest.TestCase):
    """The helpers every failure message is built from must never raise."""

    def test_a_record_whose_repr_explodes_is_still_described(self) -> None:
        class Hostile(object):
            def __repr__(self) -> str:
                raise RuntimeError("no")

        self.assertIn("unprintable", identity.describe(Hostile()))

    def test_a_long_record_is_truncated(self) -> None:
        text = identity.describe({"output": "x" * 5000})
        self.assertIn("...[truncated]", text)
        self.assertLess(len(text), 400)

    def test_identity_of_a_non_dict_names_its_type(self) -> None:
        self.assertIn("str", identity.identity_of("nope"))

    def test_identity_falls_back_to_question_marks_field_by_field(
        self
    ) -> None:
        self.assertEqual(
            identity.identity_of({"script": "s.py"}), "? / s.py / ?")


if __name__ == "__main__":
    unittest.main()
