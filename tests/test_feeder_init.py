"""Tests for the ``--init`` setup wizard.

The wizard's whole justification is that it *validates* rather than
collects: a form that writes nine answers to a file has only moved the
mistakes into the config file, where they surface at 03:00 in a cron job
nobody is watching. So what is pinned here is that each answer is checked
against the real thing at the moment it is given — the URL is used, the
reader is loaded, the paths are written to — and that a bad answer is
refused and re-asked rather than saved.

The conversation is driven through injected streams, and the dashboard
probe through an injected opener, so nothing here needs a terminal or a
server.

Python 3.6 compatible; standard library only.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from feeder import config, init

_GOOD_BODY = json.dumps(
    {"inserted": 0, "updated": 0, "rejected": 0, "errors": []}
).encode("utf-8")

#: A reader file the wizard can actually load, written into the temp dir.
_READER_SOURCE = """\
from feeder.reader import Reader


class SiteReader(Reader):
    def read(self, since):
        return iter([{
            "environment": "prod", "script": "suite", "test_name": "t",
            "result": "PASS", "output": "",
            "start_time": "2026-07-25T01:00:00.000000",
            "end_time": "2026-07-25T01:00:02.000000",
        }])


def create_reader(sources):
    return SiteReader()
"""


def answering(status: int = 200, body: bytes = _GOOD_BODY) -> Any:
    """An Opener that answers every probe the same way."""
    def opener(
        url: str, data: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        return status, body
    return opener


class WizardTestBase(unittest.TestCase):
    """A temp directory, a loadable reader in it, and a scripted run."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_init_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_path = os.path.join(self.tmp, "feeder.config.json")
        self.reader_path = os.path.join(self.tmp, "internal_reader.py")
        with io.open(self.reader_path, "w", encoding="utf-8") as handle:
            handle.write(_READER_SOURCE)

    def run_wizard(
        self, answers: List[str], opener: Optional[Any] = None,
        config_path: Optional[str] = None,
    ) -> Tuple[int, str]:
        """Drive the wizard with *answers*; return (exit code, transcript)."""
        out = io.StringIO()
        inp = io.StringIO("\n".join(answers) + "\n")
        code = init.run_init(
            out, inp,
            config_path=config_path if config_path else self.config_path,
            opener=opener if opener is not None else answering(),
            require_tty=False,
        )
        return code, out.getvalue()

    def happy_answers(self, **overrides: str) -> List[str]:
        """The shortest complete conversation: reader from a file, no check."""
        answers = {
            "config": "",                       # accept default path
            "url": "http://dashboard:8000",
            "mode": "",                         # catchup
            "reader": self.reader_path + ":create_reader",
            "source": "",                       # reader finds its own data
            "check": "n",
            "state": "",                        # accept default
            "replay": "",                       # accept default
            "create_replay": "",                # yes, create it
        }
        answers.update(overrides)
        return [answers[key] for key in (
            "config", "url", "mode", "reader", "source", "check",
            "state", "replay", "create_replay")]

    def written(self) -> Dict[str, Any]:
        """The config file the wizard wrote, parsed."""
        return config.load_config(self.config_path)


class NonInteractiveTest(unittest.TestCase):
    """A wizard reached from cron must refuse, not hang or crash."""

    def test_no_terminal_is_refused_with_the_alternative(self) -> None:
        out = io.StringIO()
        code = init.run_init(out, io.StringIO(""), require_tty=True)
        self.assertEqual(code, 2)
        message = out.getvalue()
        self.assertIn("not a tty", message)
        self.assertIn("--config", message)

    def test_the_refusal_lists_every_key_so_the_file_can_be_hand_written(
        self
    ) -> None:
        out = io.StringIO()
        init.run_init(out, io.StringIO(""), require_tty=True)
        for key in config.valid_keys():
            self.assertIn(key, out.getvalue())


class HappyPathTest(WizardTestBase):
    """A complete run writes a config file that the feeder can then load."""

    def test_the_wizard_writes_a_loadable_config(self) -> None:
        code, _ = self.run_wizard(self.happy_answers())
        self.assertEqual(code, 0)
        settings = self.written()
        self.assertEqual(settings["url"], "http://dashboard:8000")
        self.assertEqual(settings["mode"], "catchup")
        self.assertEqual(settings["reader"],
                         self.reader_path + ":create_reader")

    def test_the_paths_it_chooses_are_beside_the_config_not_the_code(
        self
    ) -> None:
        """The whole point: defaults must not land in a read-only checkout."""
        self.run_wizard(self.happy_answers())
        settings = self.written()
        self.assertEqual(
            os.path.dirname(os.path.abspath(settings["state_file"])),
            os.path.abspath(self.tmp))
        self.assertTrue(settings["replay_dir"].startswith(self.tmp))

    def test_it_creates_the_replay_directory_it_proposes(self) -> None:
        self.run_wizard(self.happy_answers())
        self.assertTrue(os.path.isdir(self.written()["replay_dir"]))

    def test_it_ends_with_the_command_to_schedule(self) -> None:
        """The scheduler line is the last place left to get a path wrong."""
        _, transcript = self.run_wizard(self.happy_answers())
        self.assertIn("--config", transcript)
        self.assertIn(os.path.abspath(self.config_path), transcript)
        self.assertIn("--dry-run", transcript)
        self.assertIn("--mode backfill", transcript)

    def test_it_shows_what_it_wrote(self) -> None:
        _, transcript = self.run_wizard(self.happy_answers())
        self.assertIn(os.path.abspath(self.config_path), transcript)
        self.assertIn('"url"', transcript)


class ValidationTest(WizardTestBase):
    """Each answer is checked against the real thing, then and there."""

    def test_a_wrong_url_is_caught_at_the_prompt_and_re_asked(self) -> None:
        """The 404 must arrive while the person is still typing the URL."""
        replies = [(404, b"nope"), (200, _GOOD_BODY)]

        def opener(
            url: str, data: bytes, headers: Dict[str, str]
        ) -> Tuple[int, bytes]:
            return replies.pop(0)

        answers = ["", "http://wrong:8000", "y",
                   "http://dashboard:8000"] + self.happy_answers()[2:]
        code, transcript = self.run_wizard(answers, opener=opener)
        self.assertIn("no /api/import endpoint", transcript)
        self.assertEqual(code, 0)
        self.assertEqual(self.written()["url"], "http://dashboard:8000")

    def test_a_scheme_less_url_never_reaches_the_network(self) -> None:
        """check_url runs first, so the fix is offered before any timeout."""
        answers = ["", "dashboard:8000", "y",
                   "http://dashboard:8000"] + self.happy_answers()[2:]
        _, transcript = self.run_wizard(answers)
        self.assertIn("has no scheme", transcript)
        self.assertIn("http://dashboard:8000", transcript)

    def test_a_bad_url_can_be_kept_after_being_warned(self) -> None:
        """The dashboard may simply not be running yet; that is allowed."""
        answers = ["", "http://dashboard:8000", "n"] + \
            self.happy_answers()[2:]
        code, transcript = self.run_wizard(
            answers, opener=answering(status=500, body=b""))
        self.assertEqual(code, 0)
        self.assertIn("safe to repeat", transcript)
        self.assertEqual(self.written()["url"], "http://dashboard:8000")

    def test_a_reader_that_will_not_load_is_re_asked(self) -> None:
        answers = self.happy_answers()
        answers[3:3] = [os.path.join(self.tmp, "absent.py") +
                        ":create_reader", ""]
        code, transcript = self.run_wizard(answers)
        self.assertIn("no such file", transcript)
        self.assertEqual(code, 0)
        self.assertEqual(self.written()["reader"],
                         self.reader_path + ":create_reader")

    def test_the_reader_can_be_run_over_its_data_on_the_spot(self) -> None:
        code, transcript = self.run_wizard(self.happy_answers(check="y"))
        self.assertEqual(code, 0)
        self.assertIn("reader check: read=1 valid=1 invalid=0", transcript)
        self.assertIn("reader OK", transcript)

    def test_a_state_file_somewhere_unwritable_is_re_asked(self) -> None:
        answers = self.happy_answers()
        answers[6:6] = [os.path.join(self.tmp, "absent-dir", "state.json")]
        code, transcript = self.run_wizard(answers)
        self.assertIn("does not exist", transcript)
        self.assertEqual(code, 0)

    def test_the_builtin_reader_insists_on_a_source(self) -> None:
        """jsonl with nothing to read is a setup that cannot work."""
        answers = self.happy_answers(reader="jsonl")
        answers[4:5] = ["", "results/*.jsonl", ""]
        code, transcript = self.run_wizard(answers)
        self.assertIn("nothing to read without a source", transcript)
        self.assertEqual(self.written()["source"], ["results/*.jsonl"])


class SafetyTest(WizardTestBase):
    """Nothing is written by surprise."""

    def test_an_existing_config_is_not_overwritten_without_consent(
        self
    ) -> None:
        config.write_config(self.config_path, {"mode": "backfill"})
        answers = ["", "n", os.path.join(self.tmp, "other.json")] + \
            self.happy_answers()[1:]
        code, transcript = self.run_wizard(answers)
        self.assertEqual(code, 0)
        self.assertIn("already exists", transcript)
        self.assertEqual(config.load_config(self.config_path),
                         {"mode": "backfill"})

    def test_running_out_of_input_writes_nothing(self) -> None:
        code, transcript = self.run_wizard(["", "http://dashboard:8000"])
        self.assertEqual(code, 2)
        self.assertIn("Nothing was written", transcript)
        self.assertFalse(os.path.exists(self.config_path))

    def test_a_reader_that_raises_while_reading_is_reported_not_fatal(
        self
    ) -> None:
        """read() must not raise; if it does, say so rather than traceback."""
        exploding = os.path.join(self.tmp, "exploding.py")
        with io.open(exploding, "w", encoding="utf-8") as handle:
            handle.write(
                "from feeder.reader import Reader\n\n\n"
                "class R(Reader):\n"
                "    def read(self, since):\n"
                "        raise RuntimeError('the database is down')\n\n\n"
                "def create_reader(sources):\n"
                "    return R()\n"
            )
        answers = self.happy_answers(
            reader=exploding + ":create_reader", check="y")
        code, transcript = self.run_wizard(answers)
        self.assertEqual(code, 0)
        self.assertIn("the database is down", transcript)
        self.assertIn("must not raise", transcript)


class PromptTest(unittest.TestCase):
    """The prompting primitives, which every question above depends on."""

    def ask(self, script: str, **kwargs: Any) -> Tuple[str, str]:
        out = io.StringIO()
        answer = init._ask(out, io.StringIO(script), "Question", **kwargs)
        return answer, out.getvalue()

    def test_a_blank_answer_takes_the_default(self) -> None:
        answer, shown = self.ask("\n", default="fallback")
        self.assertEqual(answer, "fallback")
        self.assertIn("[fallback]", shown)

    def test_a_blank_answer_with_no_default_is_re_asked(self) -> None:
        answer, shown = self.ask("\nreal\n")
        self.assertEqual(answer, "real")
        self.assertIn("An answer is needed here", shown)

    def test_end_of_input_aborts_rather_than_looping(self) -> None:
        with self.assertRaises(init.Abort):
            self.ask("")

    def test_yes_no_accepts_the_usual_spellings(self) -> None:
        for text, expected in (("y\n", True), ("Yes\n", True),
                               ("n\n", False), ("NO\n", False)):
            out = io.StringIO()
            self.assertEqual(
                init._ask_yes_no(out, io.StringIO(text), "?", default=True),
                expected, text)

    def test_yes_no_re_asks_on_anything_else(self) -> None:
        out = io.StringIO()
        result = init._ask_yes_no(
            out, io.StringIO("maybe\ny\n"), "?", default=False)
        self.assertTrue(result)
        self.assertIn("Please answer y or n", out.getvalue())

    def test_a_choice_outside_the_list_is_re_asked(self) -> None:
        out = io.StringIO()
        answer = init._ask_choice(
            out, io.StringIO("nightly\nbackfill\n"), "Mode",
            ["daily", "backfill"], "daily")
        self.assertEqual(answer, "backfill")
        self.assertIn("Please answer one of", out.getvalue())


if __name__ == "__main__":
    unittest.main()
