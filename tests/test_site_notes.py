"""Site-specific What's new notes: the file, the endpoint, the CLI.

Two properties carry the weight here.

**A broken notes file must not break the page.** These notes annotate
release notes that already shipped inside the build, so an absent, empty
or malformed side-car has to degrade to "no notes" — never to an error on
the page a tester opens to find out what changed.

**A note is published the moment it is written.** The server re-reads the
file per request, so a typo is in front of everyone immediately. Editing
and removing by id are therefore not conveniences; they are the only way
to take something back without hand-editing JSON underneath a running
server, and the ids they address have to be stable.

Python 3.6 compatible; standard library only.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List

from testboard import api, site_notes
from testboard.storage import Storage
from tools import add_site_note

NOW = datetime.datetime(2026, 7, 30, 9, 0, 0)


class DefaultPathTest(unittest.TestCase):
    """Where the file lives when nobody says."""

    def test_it_sits_beside_the_database(self) -> None:
        """Not in the working directory: see the docstring in site_notes."""
        path = site_notes.default_path(os.path.join("var", "lib", "tb.db"))
        self.assertEqual(os.path.basename(path), "site_notes.json")
        self.assertEqual(
            os.path.dirname(path),
            os.path.dirname(os.path.abspath(os.path.join("var", "lib",
                                                         "tb.db"))))

    def test_it_is_absolute(self) -> None:
        """A relative default would move with the server's cwd."""
        self.assertTrue(os.path.isabs(site_notes.default_path("tb.db")))


class NotesFileTest(unittest.TestCase):
    """Reading and writing the file."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_notes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "site_notes.json")

    def write_raw(self, text: str) -> None:
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_missing_file_is_empty_and_not_a_problem(self) -> None:
        """Nothing written yet is the normal state, not an error."""
        notes, problem = site_notes.load(self.path)
        self.assertEqual(notes, [])
        self.assertIsNone(problem)

    def test_no_path_configured_is_empty_and_not_a_problem(self) -> None:
        self.assertEqual(site_notes.load(None), ([], None))

    def test_add_then_load_round_trips(self) -> None:
        site_notes.add(self.path, "2026-07-30", "Parser fixed.", "luke",
                       now=NOW)
        notes, problem = site_notes.load(self.path)
        self.assertIsNone(problem)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].date, "2026-07-30")
        self.assertEqual(notes[0].text, "Parser fixed.")
        self.assertEqual(notes[0].author, "luke")
        self.assertEqual(notes[0].note_id, 1)

    def test_notes_come_back_newest_date_first(self) -> None:
        site_notes.add(self.path, "2026-07-28", "older", "a", now=NOW)
        site_notes.add(self.path, "2026-07-30", "newer", "a", now=NOW)
        notes, _ = site_notes.load(self.path)
        self.assertEqual([note.text for note in notes], ["newer", "older"])

    def test_ids_are_unique_and_ascending(self) -> None:
        for index in range(5):
            site_notes.add(self.path, "2026-07-30", "n%d" % index, "a",
                           now=NOW)
        notes, _ = site_notes.load(self.path)
        ids = sorted(note.note_id for note in notes)
        self.assertEqual(ids, [1, 2, 3, 4, 5])

    def test_an_id_is_not_reused_after_a_removal(self) -> None:
        """Reuse would make a stale id delete the wrong note."""
        first = site_notes.add(self.path, "2026-07-30", "one", "a", now=NOW)
        second = site_notes.add(self.path, "2026-07-30", "two", "a", now=NOW)
        site_notes.remove(self.path, first.note_id)
        third = site_notes.add(self.path, "2026-07-30", "three", "a", now=NOW)
        self.assertNotEqual(third.note_id, first.note_id)
        self.assertGreater(third.note_id, second.note_id)

    def test_a_bad_date_is_refused(self) -> None:
        for bad in ("30-07-2026", "2026-7-30", "2026-13-01", "today", ""):
            with self.assertRaises(ValueError, msg=bad):
                site_notes.add(self.path, bad, "text", "a", now=NOW)

    def test_empty_text_is_refused(self) -> None:
        for bad in ("", "   ", "\n"):
            with self.assertRaises(ValueError):
                site_notes.add(self.path, "2026-07-30", bad, "a", now=NOW)

    def test_long_text_is_truncated_not_refused(self) -> None:
        note = site_notes.add(
            self.path, "2026-07-30", "x" * 5000, "a", now=NOW)
        self.assertEqual(len(note.text), site_notes.MAX_TEXT)

    def test_malformed_json_yields_no_notes_and_a_reason(self) -> None:
        """The page must survive it; somebody must be able to see why."""
        self.write_raw("{not json at all")
        notes, problem = site_notes.load(self.path)
        self.assertEqual(notes, [])
        self.assertIsNotNone(problem)
        self.assertIn("not valid JSON", problem)

    def test_one_bad_record_does_not_lose_the_good_ones(self) -> None:
        self.write_raw(json.dumps({"notes": [
            {"date": "2026-07-30", "text": "good"},
            {"date": "nonsense", "text": "bad date"},
            {"date": "2026-07-29", "text": ""},
            "not even an object",
            {"date": "2026-07-28", "text": "also good"},
        ]}))
        notes, problem = site_notes.load(self.path)
        self.assertEqual([note.text for note in notes], ["good", "also good"])
        self.assertIn("skipped 3", problem)

    def test_a_bare_list_is_tolerated(self) -> None:
        """It is the shape a person writes by hand."""
        self.write_raw(json.dumps([{"date": "2026-07-30", "text": "hi"}]))
        notes, problem = site_notes.load(self.path)
        self.assertEqual(len(notes), 1)
        self.assertIsNone(problem)

    def test_a_hand_written_file_gets_stable_ids(self) -> None:
        """--remove has to work on a file nobody gave ids to."""
        self.write_raw(json.dumps({"notes": [
            {"date": "2026-07-30", "text": "one"},
            {"date": "2026-07-29", "text": "two"},
        ]}))
        first, _ = site_notes.load(self.path)
        second, _ = site_notes.load(self.path)
        self.assertEqual([n.note_id for n in first],
                         [n.note_id for n in second])
        self.assertEqual(len(set(n.note_id for n in first)), 2)

    def test_writing_refuses_to_clobber_a_file_it_cannot_read(self) -> None:
        """The rewrite would silently drop whatever failed to parse."""
        self.write_raw("{{{ broken")
        with self.assertRaises(ValueError):
            site_notes.add(self.path, "2026-07-30", "text", "a", now=NOW)
        with self.assertRaises(ValueError):
            site_notes.remove(self.path, 1)
        # And the broken file is still there to be looked at.
        with io.open(self.path, encoding="utf-8") as handle:
            self.assertIn("broken", handle.read())

    def test_the_file_is_capped(self) -> None:
        for index in range(site_notes.MAX_NOTES + 20):
            site_notes.add(self.path, "2026-07-30", "n%d" % index, "a",
                           now=NOW)
        notes, _ = site_notes.load(self.path)
        self.assertEqual(len(notes), site_notes.MAX_NOTES)

    def test_a_write_leaves_no_temporary_file_behind(self) -> None:
        site_notes.add(self.path, "2026-07-30", "text", "a", now=NOW)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["site_notes.json"])


class EditAndRemoveTest(unittest.TestCase):
    """Taking a mistake back.

    A note is in front of every tester as soon as it is written, so this
    is the retraction path, and it has to work by id rather than by
    editing the JSON under a running server.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_notes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "site_notes.json")
        self.first = site_notes.add(
            self.path, "2026-07-30", "linux-sim rebuilt", "luke", now=NOW)
        self.second = site_notes.add(
            self.path, "2026-07-28", "parser fixed", "luke", now=NOW)

    def texts(self) -> List[str]:
        notes, _ = site_notes.load(self.path)
        return [note.text for note in notes]

    def test_edit_replaces_the_text(self) -> None:
        updated = site_notes.edit(
            self.path, self.first.note_id, text="linux-uat-sim rebuilt")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.text, "linux-uat-sim rebuilt")
        self.assertIn("linux-uat-sim rebuilt", self.texts())
        self.assertNotIn("linux-sim rebuilt", self.texts())

    def test_edit_keeps_the_author_and_the_time_it_was_recorded(self) -> None:
        """It is the same note corrected, not a new one."""
        updated = site_notes.edit(
            self.path, self.first.note_id, text="corrected")
        self.assertEqual(updated.author, self.first.author)
        self.assertEqual(updated.added_at, self.first.added_at)
        self.assertEqual(updated.note_id, self.first.note_id)

    def test_edit_can_move_a_note_to_another_date(self) -> None:
        """Filed against the wrong drop is a mistake worth fixing."""
        updated = site_notes.edit(
            self.path, self.second.note_id, date="2026-07-30")
        self.assertEqual(updated.date, "2026-07-30")
        self.assertEqual(updated.text, "parser fixed")

    def test_edit_leaves_the_other_notes_alone(self) -> None:
        site_notes.edit(self.path, self.first.note_id, text="changed")
        self.assertIn("parser fixed", self.texts())

    def test_edit_of_an_unknown_id_changes_nothing(self) -> None:
        before = self.texts()
        self.assertIsNone(site_notes.edit(self.path, 9999, text="nope"))
        self.assertEqual(self.texts(), before)

    def test_edit_refuses_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            site_notes.edit(self.path, self.first.note_id, text="  ")

    def test_edit_refuses_a_bad_date(self) -> None:
        with self.assertRaises(ValueError):
            site_notes.edit(self.path, self.first.note_id, date="30/07/2026")

    def test_remove_deletes_only_that_note(self) -> None:
        removed = site_notes.remove(self.path, self.first.note_id)
        self.assertEqual(removed.text, "linux-sim rebuilt")
        self.assertEqual(self.texts(), ["parser fixed"])

    def test_remove_of_an_unknown_id_changes_nothing(self) -> None:
        before = self.texts()
        self.assertIsNone(site_notes.remove(self.path, 9999))
        self.assertEqual(self.texts(), before)

    def test_removing_every_note_leaves_a_valid_empty_file(self) -> None:
        site_notes.remove(self.path, self.first.note_id)
        site_notes.remove(self.path, self.second.note_id)
        notes, problem = site_notes.load(self.path)
        self.assertEqual(notes, [])
        self.assertIsNone(problem)


class SiteNotesEndpointTest(unittest.TestCase):
    """GET /api/site-notes. Never an error, whatever the file is doing."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_notes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "site_notes.json")
        self.store = Storage(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.store.close)

    def get(self, path: Any = None) -> Dict[str, Any]:
        request = api.Request(
            method="GET", path="/api/site-notes", query={}, body=b"")
        response = api.handle_api(self.store, request, site_notes_path=path)
        self.assertEqual(response.status, 200)
        return json.loads(response.body.decode("utf-8"))

    def test_it_returns_the_notes(self) -> None:
        site_notes.add(self.path, "2026-07-30", "Parser fixed.", "luke",
                       now=NOW)
        payload = self.get(self.path)
        self.assertEqual(len(payload["notes"]), 1)
        note = payload["notes"][0]
        self.assertEqual(note["date"], "2026-07-30")
        self.assertEqual(note["text"], "Parser fixed.")
        self.assertEqual(note["author"], "luke")
        self.assertEqual(note["id"], 1)
        self.assertTrue(payload["configured"])
        self.assertIsNone(payload["problem"])

    def test_no_path_configured_is_an_empty_list_not_a_404(self) -> None:
        """One shape for the frontend to handle, not two."""
        payload = self.get(None)
        self.assertEqual(payload["notes"], [])
        self.assertFalse(payload["configured"])

    def test_a_missing_file_is_an_empty_list(self) -> None:
        payload = self.get(os.path.join(self.tmp, "absent.json"))
        self.assertEqual(payload["notes"], [])
        self.assertTrue(payload["configured"])

    def test_a_broken_file_is_still_a_200(self) -> None:
        """The release notes must not go down with the side-car."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write("}{ nonsense")
        payload = self.get(self.path)
        self.assertEqual(payload["notes"], [])
        self.assertIsNotNone(payload["problem"])

    def test_the_method_is_checked(self) -> None:
        request = api.Request(
            method="POST", path="/api/site-notes", query={}, body=b"{}")
        response = api.handle_api(
            self.store, request, site_notes_path=self.path)
        self.assertEqual(response.status, 405)

    def test_the_endpoint_exists_without_the_parameter(self) -> None:
        """Every existing caller passes no site_notes_path at all."""
        request = api.Request(
            method="GET", path="/api/site-notes", query={}, body=b"")
        response = api.handle_api(self.store, request)
        self.assertEqual(response.status, 200)


class AddSiteNoteCliTest(unittest.TestCase):
    """The command line an operator actually types."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_notes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "notes.json")

    def run_cli(self, argv: List[str]) -> Any:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(err):
                code = add_site_note.main(["--file", self.path] + argv)
        return code, out.getvalue(), err.getvalue()

    def texts(self) -> List[str]:
        notes, _ = site_notes.load(self.path)
        return [note.text for note in notes]

    def test_it_adds_a_note_dated_today(self) -> None:
        code, out, _ = self.run_cli(["--text", "Parser fixed.", "-a", "luke"])
        self.assertEqual(code, 0)
        self.assertIn("Parser fixed.", out)
        self.assertEqual(self.texts(), ["Parser fixed."])

    def test_an_explicit_date_files_it_under_that_drop(self) -> None:
        code, _, _ = self.run_cli(
            ["--text", "older", "--date", "2026-07-28"])
        self.assertEqual(code, 0)
        notes, _ = site_notes.load(self.path)
        self.assertEqual(notes[0].date, "2026-07-28")

    def test_list_shows_the_ids_needed_to_correct_a_note(self) -> None:
        self.run_cli(["--text", "first"])
        code, out, _ = self.run_cli(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("id 1", out)
        self.assertIn("--remove", out)

    def test_edit_corrects_a_note(self) -> None:
        self.run_cli(["--text", "linux-sim rebuilt"])
        code, out, _ = self.run_cli(["--edit", "1", "--text", "linux-uat"])
        self.assertEqual(code, 0)
        self.assertIn("Corrected", out)
        self.assertEqual(self.texts(), ["linux-uat"])

    def test_remove_deletes_a_note_and_prints_what_went(self) -> None:
        self.run_cli(["--text", "a mistake"])
        code, out, _ = self.run_cli(["--remove", "1"])
        self.assertEqual(code, 0)
        self.assertIn("a mistake", out)
        self.assertEqual(self.texts(), [])

    def test_an_unknown_id_exits_2_and_says_to_list(self) -> None:
        self.run_cli(["--text", "one"])
        for argv in (["--remove", "42"], ["--edit", "42", "--text", "x"]):
            code, _, err = self.run_cli(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("--list", err)

    def test_no_text_and_no_action_exits_2(self) -> None:
        code, _, err = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("--text is required", err)

    def test_edit_without_anything_to_change_exits_2(self) -> None:
        self.run_cli(["--text", "one"])
        code, _, err = self.run_cli(["--edit", "1"])
        self.assertEqual(code, 2)
        self.assertIn("--text and/or --date", err)

    def test_two_actions_at_once_exits_2(self) -> None:
        code, _, err = self.run_cli(["--list", "--remove", "1"])
        self.assertEqual(code, 2)
        self.assertIn("Pick one", err)

    def test_a_bad_date_exits_2(self) -> None:
        code, _, err = self.run_cli(["--text", "x", "--date", "30/07/2026"])
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err)

    def test_listing_an_empty_file_is_not_an_error(self) -> None:
        code, out, _ = self.run_cli(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("no notes yet", out)


if __name__ == "__main__":
    unittest.main()
