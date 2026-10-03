"""The publication gate's mechanical layer and its hook installer.

Every planted hit is a fixture: the term list is written to a temp
directory and passed with ``--terms``, never the real
``.claude/private/terms.txt``. Planted emails and addresses are built by
concatenation so this file does not itself trip the gate when committed.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Optional, Sequence, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "dev"))

import install_hooks  # noqa: E402
import publication_gate  # noqa: E402

#: Built by concatenation: see the module docstring.
PLANTED_EMAIL = "someone" + "@" + "example.org"
PLANTED_IP = "10.20.30." + "40"
ALLOWED_EMAIL = "noreply" + "@" + "anthropic.com"
FIXTURE_EMAIL = "fixture" + "@" + "fixture.invalid"

TERMS = (
    "# fixture term list\n"
    "\n"
    "Zebracorn\n"
    "re:\\bproj-\\d{3}\\b\n"
)

HAVE_GIT = shutil.which("git") is not None


def run_gate(argv: Sequence[str]) -> Tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = publication_gate.main(list(argv))
    return code, out.getvalue()


def git(args: Sequence[str], cwd: str) -> Tuple[int, str]:
    proc = subprocess.Popen(
        ["git"] + list(args), cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


class _TempDirCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="gate-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.terms = self.write("terms.txt", TERMS)

    def write(self, name: str, text: str, root: Optional[str] = None) -> str:
        path = os.path.join(root or self.tmp, name)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class MessageModeTest(_TempDirCase):

    def gate_message(self, text: str) -> Tuple[int, str]:
        path = self.write("COMMIT_EDITMSG", text)
        return run_gate(["--message", path, "--terms", self.terms])

    def test_a_planted_term_is_a_hit_case_insensitively(self) -> None:
        code, out = self.gate_message("Subject\n\nfixed for the ZEBRACORN team\n")
        self.assertEqual(code, 1)
        self.assertIn("COMMIT_EDITMSG:3: Zebracorn", out)

    def test_a_planted_regex_is_a_hit(self) -> None:
        code, out = self.gate_message("Fix PROJ-123 regression\n")
        self.assertEqual(code, 1)
        self.assertIn(":1: re:\\bproj-\\d{3}\\b", out)

    def test_a_planted_email_is_a_hit(self) -> None:
        code, out = self.gate_message("Reported-by: <%s>\n" % PLANTED_EMAIL)
        self.assertEqual(code, 1)
        self.assertIn(":1: email address " + PLANTED_EMAIL, out)

    def test_a_planted_ip_is_a_hit(self) -> None:
        code, out = self.gate_message("Subject\n\nserver at %s:8000\n"
                                      % PLANTED_IP)
        self.assertEqual(code, 1)
        self.assertIn(":3: IPv4 address " + PLANTED_IP, out)

    def test_a_clean_message_passes(self) -> None:
        code, out = self.gate_message(
            "Gate: scan added lines\n\nServes on 127.0.0.1 and 0.0.0.0; "
            "version 1.0.2.3.4 is not an address.\n\n"
            "Co-Authored-By: Claude <%s>\n" % ALLOWED_EMAIL)
        self.assertEqual((code, out), (0, ""))

    def test_git_commentary_and_the_verbose_diff_are_not_scanned(self) -> None:
        code, _ = self.gate_message(
            "Clean subject\n# On branch zebracorn\n"
            "# ------------------------ >8 ------------------------\n"
            "+ a diff line naming %s\n" % PLANTED_EMAIL)
        self.assertEqual(code, 0)

    def test_an_impossible_octet_is_not_an_address(self) -> None:
        code, _ = self.gate_message("see 300.1.2.3 and 1.2.3\n")
        self.assertEqual(code, 0)

    def test_a_missing_term_list_warns_once_and_passes(self) -> None:
        path = self.write("COMMIT_EDITMSG", "Clean subject\n")
        missing = os.path.join(self.tmp, "absent", "terms.txt")
        code, out = run_gate(["--message", path, "--terms", missing])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertIn("warning", lines[0])

    def test_built_ins_still_apply_without_a_term_list(self) -> None:
        path = self.write("COMMIT_EDITMSG", "mail %s\n" % PLANTED_EMAIL)
        missing = os.path.join(self.tmp, "absent", "terms.txt")
        code, _ = run_gate(["--message", path, "--terms", missing])
        self.assertEqual(code, 1)

    def test_an_invalid_regex_fails_closed(self) -> None:
        bad = self.write("bad.txt", "re:(unclosed\n")
        path = self.write("COMMIT_EDITMSG", "Clean subject\n")
        code, out = run_gate(["--message", path, "--terms", bad])
        self.assertEqual(code, 2)
        self.assertIn("bad.txt:1", out)


class TextModeTest(_TempDirCase):

    def test_every_file_is_scanned_and_named(self) -> None:
        clean = self.write("clean.md", "nothing here\n")
        dirty = self.write("body.md", "line one\nline two zebracorn\n")
        code, out = run_gate(["--text", clean, dirty, "--terms", self.terms])
        self.assertEqual(code, 1)
        self.assertIn("body.md:2: Zebracorn", out)
        self.assertNotIn("clean.md", out)


class DiffParserTest(unittest.TestCase):

    def test_added_lines_carry_new_file_line_numbers(self) -> None:
        diff = (
            "diff --git a/x.txt b/x.txt\n"
            "--- a/x.txt\n"
            "+++ b/x.txt\n"
            "@@ -3 +3,2 @@\n"
            "-old\n"
            "+new three\n"
            "+++ four, not a header\n"
            "@@ -10,0 +12 @@\n"
            "+twelve\n"
            "diff --git a/gone.txt b/gone.txt\n"
            "Binary files a/gone.png and b/gone.png differ\n"
        )
        self.assertEqual(publication_gate.parse_added_lines(diff), [
            ("x.txt", 3, "new three"),
            ("x.txt", 4, "++ four, not a header"),
            ("x.txt", 12, "twelve"),
        ])


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
class StagedModeTest(_TempDirCase):

    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.assertEqual(git(["init", "-q"], self.repo)[0], 0)
        self.write("notes.txt", "one\ntwo\nthree\n", self.repo)
        git(["add", "notes.txt"], self.repo)
        code, out = git(["-c", "user.name=fixture", "-c",
                         "user.email=" + FIXTURE_EMAIL,
                         "-c", "commit.gpgsign=false", "-c",
                         "core.hooksPath=" + os.path.join(self.tmp, "nohooks"),
                         "commit", "-q", "-m", "base"], self.repo)
        self.assertEqual(code, 0, out)

    def test_a_planted_added_line_is_a_hit_at_its_line(self) -> None:
        self.write("notes.txt", "one\ntwo\nZebracorn here\nthree\n", self.repo)
        self.write("new.txt", "fresh\nwith %s\n" % PLANTED_IP, self.repo)
        git(["add", "notes.txt", "new.txt"], self.repo)
        code, out = run_gate(["--staged", "--repo", self.repo,
                              "--terms", self.terms])
        self.assertEqual(code, 1, out)
        self.assertIn("notes.txt:3: Zebracorn", out)
        self.assertIn("new.txt:2: IPv4 address " + PLANTED_IP, out)

    def test_removed_and_unstaged_lines_are_not_scanned(self) -> None:
        self.write("notes.txt", "one\nthree\n", self.repo)
        git(["add", "notes.txt"], self.repo)
        self.write("notes.txt", "one\nthree\nzebracorn unstaged\n", self.repo)
        code, out = run_gate(["--staged", "--repo", self.repo,
                              "--terms", self.terms])
        self.assertEqual((code, out), (0, ""))


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
class InstallHooksTest(_TempDirCase):

    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.assertEqual(git(["init", "-q"], self.repo)[0], 0)
        self.hooks = os.path.join(self.repo, ".git", "hooks")

    def install(self, *extra: str) -> Tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install_hooks.main(["--repo", self.repo] + list(extra))
        return code, out.getvalue()

    def read_hook(self, name: str) -> str:
        with open(os.path.join(self.hooks, name), "rb") as handle:
            return handle.read().decode("utf-8")

    def test_writes_both_hooks_and_says_so(self) -> None:
        code, out = self.install()
        self.assertEqual(code, 0, out)
        commit_msg = self.read_hook("commit-msg")
        pre_commit = self.read_hook("pre-commit")
        self.assertIn('tools/dev/publication_gate.py --message "$1"',
                      commit_msg)
        self.assertIn("tools/dev/publication_gate.py --staged", pre_commit)
        self.assertNotIn("\r", commit_msg + pre_commit)
        self.assertTrue(commit_msg.startswith("#!/bin/sh\n"))
        self.assertIn("wrote " + os.path.join(self.hooks, "commit-msg"), out)
        self.assertIn("wrote " + os.path.join(self.hooks, "pre-commit"), out)

    def test_is_idempotent(self) -> None:
        self.install()
        first = (self.read_hook("commit-msg"), self.read_hook("pre-commit"))
        listing = sorted(os.listdir(self.hooks))
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            (self.read_hook("commit-msg"), self.read_hook("pre-commit")),
            first)
        self.assertEqual(sorted(os.listdir(self.hooks)), listing)
        self.assertEqual(out.count("unchanged"), 2, out)

    def test_leaves_other_hooks_alone(self) -> None:
        self.write("pre-push", "#!/bin/sh\nexit 0\n", self.hooks)
        self.write("commit-msg", "#!/bin/sh\necho mine\n", self.hooks)
        code, out = self.install()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.read_hook("pre-push"), "#!/bin/sh\nexit 0\n")
        self.assertEqual(self.read_hook("commit-msg"),
                         "#!/bin/sh\necho mine\n")
        self.assertIn("publication_gate.py --staged",
                      self.read_hook("pre-commit"))
        self.assertIn("not ours", out)

    def test_a_worktree_installs_into_the_shared_hooks(self) -> None:
        self.write("a.txt", "a\n", self.repo)
        git(["add", "a.txt"], self.repo)
        git(["-c", "user.name=fixture", "-c",
             "user.email=" + FIXTURE_EMAIL,
             "-c", "commit.gpgsign=false", "-c",
             "core.hooksPath=" + os.path.join(self.tmp, "nohooks"),
             "commit", "-q", "-m", "base"], self.repo)
        linked = os.path.join(self.tmp, "linked")
        code, out = git(["worktree", "add", "-q", "--detach", linked],
                        self.repo)
        self.assertEqual(code, 0, out)
        out_buffer = io.StringIO()
        with contextlib.redirect_stdout(out_buffer):
            code = install_hooks.main(["--repo", linked])
        self.assertEqual(code, 0, out_buffer.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(self.hooks, "pre-commit")))

    def test_the_installed_hooks_block_a_real_commit(self) -> None:
        """End to end: git runs the hook, the hook runs the gate."""
        code, configured = git(["config", "--get", "core.hooksPath"],
                               self.repo)
        if code == 0 and configured.strip():
            self.skipTest("a global core.hooksPath overrides .git/hooks")
        gate_dir = os.path.join(self.repo, "tools", "dev")
        os.makedirs(gate_dir)
        shutil.copy(publication_gate.__file__, gate_dir)
        self.write(os.path.join(".claude", "private", "terms.txt"), TERMS,
                   self.repo)
        self.write(".gitignore", ".claude/\n", self.repo)
        code, out = self.install("--python", sys.executable)
        self.assertEqual(code, 0, out)
        identity = ["-c", "user.name=fixture", "-c",
                    "user.email=" + FIXTURE_EMAIL,
                    "-c", "commit.gpgsign=false"]
        git(["add", ".gitignore", "tools"], self.repo)
        code, out = git(identity + ["commit", "-q", "-m",
                                    "Zebracorn subject"], self.repo)
        self.assertNotEqual(code, 0, "commit-msg hook did not block: " + out)
        self.assertIn("Zebracorn", out)
        code, out = git(identity + ["commit", "-q", "-m", "Clean subject"],
                        self.repo)
        self.assertEqual(code, 0, out)
        self.write("leak.txt", "zebracorn\n", self.repo)
        git(["add", "leak.txt"], self.repo)
        code, out = git(identity + ["commit", "-q", "-m", "Add a file"],
                        self.repo)
        self.assertNotEqual(code, 0, "pre-commit hook did not block: " + out)
        self.assertIn("leak.txt:1: Zebracorn", out)


if __name__ == "__main__":
    unittest.main()
