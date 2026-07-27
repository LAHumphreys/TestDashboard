"""Tests for the feeder's JSON config file.

The config file exists so a scheduled import is not a 200-character
command line. That only helps if a wrong file is *refused* rather than
half-applied: a typo'd key which is silently ignored is worse than no
config file at all, because the setting looks applied and is not. So most
of what is checked here is rejection, and the quality of the message that
comes with it.

Python 3.6 compatible; standard library only.
"""

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict

from feeder import config


class ConfigTestBase(unittest.TestCase):
    """A temp directory to write config files into."""

    def setUp(self) -> None:
        """Create a scratch directory removed on teardown."""
        self.tmp = tempfile.mkdtemp(prefix="testboard_config_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text: str, name: str = "feeder.config.json") -> str:
        """Write raw text as a config file and return its path."""
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_json(self, data: Any) -> str:
        """Write a JSON document as a config file and return its path."""
        return self.write(json.dumps(data))


class LoadConfigTest(ConfigTestBase):
    """Reading a valid config file."""

    def test_every_documented_key_is_accepted(self) -> None:
        """CONFIG_KEYS is the contract; a file using all of it must load."""
        data = {
            "url": "http://dashboard:8000",
            "mode": "daily",
            "reader": "/opt/r.py:create_reader",
            "source": ["/data/results"],
            "batch_size": 250,
            "state_file": "/var/lib/testboard/state.json",
            "replay_dir": "/var/lib/testboard/replay",
            "max_consecutive_failures": 5,
            "overlap_days": 2,
            "allow_empty": True,
            "verbose": False,
        }
        self.assertEqual(sorted(data), sorted(config.valid_keys()))
        self.assertEqual(config.load_config(self.write_json(data)), data)

    def test_underscore_keys_are_notes_and_are_ignored(self) -> None:
        """JSON has no comments, so a leading underscore stands in."""
        settings = config.load_config(self.write_json(
            {"_note": "the nightly import", "mode": "daily"}))
        self.assertEqual(settings, {"mode": "daily"})

    def test_a_directory_resolves_to_the_default_file_name(self) -> None:
        """--config /etc/testboard finds feeder.config.json inside it."""
        self.write_json({"mode": "daily"})
        self.assertEqual(
            config.load_config(self.tmp), {"mode": "daily"})

    def test_a_bare_string_source_is_accepted_as_one_source(self) -> None:
        """Writing "source": "x.jsonl" is the natural slip; the intent is clear."""
        settings = config.load_config(
            self.write_json({"source": "results/*.jsonl"}))
        self.assertEqual(settings["source"], ["results/*.jsonl"])

    def test_a_null_value_means_unset_rather_than_wrong(self) -> None:
        """A key left as null falls through to the CLI default."""
        self.assertEqual(
            config.load_config(self.write_json({"url": None})), {})


class RejectionTest(ConfigTestBase):
    """Every way a config file can be wrong, and what it says about it."""

    def message(self, data: Any) -> str:
        """Return the ConfigError text for a config file holding *data*."""
        with self.assertRaises(config.ConfigError) as caught:
            config.load_config(self.write_json(data))
        return str(caught.exception)

    def test_a_missing_file_points_at_the_wizard(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            config.load_config(os.path.join(self.tmp, "absent.json"))
        message = str(caught.exception)
        self.assertIn("absent.json", message)
        self.assertIn("--init", message)

    def test_invalid_json_explains_json_itself(self) -> None:
        """The likely author has just written an INI file or added a comment."""
        path = self.write("# the nightly import\nurl = http://x\n")
        with self.assertRaises(config.ConfigError) as caught:
            config.load_config(path)
        message = str(caught.exception)
        self.assertIn(path, message)
        self.assertIn("no comments", message)
        self.assertIn("double quotes", message)

    def test_a_json_list_is_not_a_config_file(self) -> None:
        self.assertIn("must contain a JSON object", self.message(["url"]))

    def test_an_unknown_key_is_named_and_never_ignored(self) -> None:
        message = self.message({"batchsize": 100})
        self.assertIn("batchsize", message)
        self.assertIn("not a setting the feeder understands", message)

    def test_an_unknown_key_suggests_the_nearest_real_one(self) -> None:
        self.assertIn('Did you mean "batch_size"?',
                      self.message({"batchsize": 100}))
        self.assertIn('Did you mean "state_file"?',
                      self.message({"statefile": "x"}))

    def test_a_dashed_key_is_told_about_underscores(self) -> None:
        """Copying --batch-size straight out of the help is the obvious slip."""
        message = self.message({"batch-size": 100})
        self.assertIn("underscores, not dashes", message)
        self.assertIn('"batch_size"', message)

    def test_an_unknown_key_lists_the_ones_that_exist(self) -> None:
        message = self.message({"nonsense": 1})
        for key in config.valid_keys():
            self.assertIn(key, message)

    def test_wrong_types_are_named_in_the_authors_language(self) -> None:
        self.assertIn('"batch_size" must be a whole number',
                      self.message({"batch_size": "lots"}))
        self.assertIn('"url" must be a string', self.message({"url": 8000}))
        self.assertIn('"verbose" must be true or false',
                      self.message({"verbose": "yes"}))
        self.assertIn('"source" must be a list of strings',
                      self.message({"source": [1, 2]}))

    def test_a_boolean_is_not_a_number(self) -> None:
        """bool is an int in Python; "batch_size": true is still a mistake."""
        self.assertIn("must be a whole number",
                      self.message({"batch_size": True}))

    def test_counts_must_be_positive(self) -> None:
        self.assertIn("must be 1 or more", self.message({"batch_size": 0}))

    def test_an_unknown_mode_lists_the_two_that_exist(self) -> None:
        message = self.message({"mode": "nightly"})
        self.assertIn("'backfill'", message)
        self.assertIn("'daily'", message)


class WriteConfigTest(ConfigTestBase):
    """Writing a config file, as --init does."""

    def test_a_written_config_reads_back_unchanged(self) -> None:
        settings = {"url": "http://h:8000", "mode": "daily",
                    "source": ["a", "b"], "batch_size": 100}
        path = os.path.join(self.tmp, "out.json")
        config.write_config(path, settings)
        self.assertEqual(config.load_config(path), settings)

    def test_keys_come_out_in_a_fixed_order(self) -> None:
        """Two configs from the same answers must be the same file."""
        settings = {"mode": "daily", "url": "http://h:8000"}
        text = config.dump_config(settings)
        self.assertLess(text.index('"url"'), text.index('"mode"'))

    def test_missing_parent_directories_are_created(self) -> None:
        path = os.path.join(self.tmp, "deep", "deeper", "feeder.json")
        config.write_config(path, {"mode": "daily"})
        self.assertTrue(os.path.exists(path))

    def test_an_unwritable_path_names_the_file(self) -> None:
        """A path under a *file* cannot be created; the error must say where."""
        blocker = os.path.join(self.tmp, "not-a-dir")
        with io.open(blocker, "w", encoding="utf-8") as handle:
            handle.write("")
        with self.assertRaises(config.ConfigError) as caught:
            config.write_config(
                os.path.join(blocker, "feeder.json"), {"mode": "daily"})
        self.assertIn("cannot write the config file", str(caught.exception))


class ParserDefaultsTest(ConfigTestBase):
    """How config values reach argparse, and what still beats them."""

    def parser(self) -> argparse.ArgumentParser:
        """A small parser shaped like the real one's relevant options."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--url", default=None)
        parser.add_argument("--mode", default=None)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--source", action="append", default=None)
        return parser

    def test_config_supplies_defaults(self) -> None:
        parser = self.parser()
        config.apply_to_parser(
            parser, {"url": "http://cfg:8000", "batch_size": 100})
        args = parser.parse_args([])
        self.assertEqual(args.url, "http://cfg:8000")
        self.assertEqual(args.batch_size, 100)

    def test_a_command_line_flag_beats_the_config(self) -> None:
        parser = self.parser()
        config.apply_to_parser(parser, {"url": "http://cfg:8000"})
        args = parser.parse_args(["--url", "http://flag:8000"])
        self.assertEqual(args.url, "http://flag:8000")

    def test_source_is_deliberately_not_a_default(self) -> None:
        """An append option seeded with a default ADDS to the command line.

        Left as a default, a config listing two directories plus one
        ``--source`` on the command line would import all three. The CLI
        applies ``source`` after parsing instead; this pins the reason.
        """
        parser = self.parser()
        applied = config.apply_to_parser(
            parser, {"source": ["from-config"], "url": "http://cfg:8000"})
        self.assertIn("source", applied)
        args = parser.parse_args(["--source", "from-flag"])
        self.assertEqual(args.source, ["from-flag"])


class DocumentationTest(unittest.TestCase):
    """CONFIG_KEYS is what the wizard, the validator and --init all quote."""

    def test_every_key_is_described(self) -> None:
        for name, kind, description in config.CONFIG_KEYS:
            self.assertTrue(name and kind and description, name)
            self.assertFalse(name.startswith("_"), name)
            self.assertNotIn("-", name, "config keys use underscores")

    def test_described_keys_render_one_per_line(self) -> None:
        lines = config.describe_keys().split("\n")
        self.assertEqual(len(lines), len(config.CONFIG_KEYS))


if __name__ == "__main__":
    unittest.main()
