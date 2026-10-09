#!/usr/bin/env python3
"""The publication gate's mechanical layer (PROCESS.md section 6).

This repository is public: whatever reaches origin is published. This
script scans text that is about to be published for terms that must not
be, and exits 1 if it finds one, so the ``commit-msg`` and ``pre-commit``
hooks written by ``tools/dev/install_hooks.py`` block the commit.

    python tools/dev/publication_gate.py --message FILE   # a commit message
    python tools/dev/publication_gate.py --staged         # git diff --cached
    python tools/dev/publication_gate.py --text FILE...   # a PR body, any file

What it looks for:

* every entry of the term list, ``.claude/private/terms.txt`` (gitignored,
  never committed): one term per line, matched as a case-insensitive
  substring; a line starting ``re:`` is a case-insensitive regular
  expression instead; blank lines and lines starting ``#`` are ignored;
* an email address, other than the commit-attribution address
  ``noreply@anthropic.com``;
* an IPv4 address, other than loopback (127.0.0.0/8) and 0.0.0.0.

Each hit prints as ``file:line: <term>``. Exit 0 when clean, 1 on any hit,
2 on a usage error or an unreadable term list (a typo in the list must
not silently turn the gate off).

The term list is looked for beside this checkout first and then in the
main checkout of the same repository (found through
``git rev-parse --git-common-dir``), because a linked worktree does not
carry gitignored files and the hooks are shared by every worktree. If no
list is found the gate prints one warning line and still applies the
built-in patterns, so a clone without the list commits normally.

Standard library only, Python 3.6; git is the only program it runs, and
only for ``--staged`` and for locating the main checkout.
"""
import argparse
import os
import re
import subprocess
import sys
from typing import List, Match, NamedTuple, Optional, Pattern, Sequence, Tuple

#: Repository root of the checkout this file lives in (tools/dev/ -> root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Where the term list lives, relative to a checkout's root.
TERMS_RELATIVE = os.path.join(".claude", "private", "terms.txt")

#: The one address the built-in email pattern lets through: the
#: attribution line every agent-made commit carries.
ALLOWED_EMAILS = frozenset(["noreply@anthropic.com"])

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
                    r"\.[A-Za-z]{2,}")

#: Four dotted octets, not embedded in a longer dotted number (a version
#: string such as 1.2.3.4.5 is not an address).
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})"
                   r"(?!\.?\d)")

#: Git's scissors line: everything below it in a commit message file is
#: removed by git before the commit is made (``commit --verbose``).
_SCISSORS = "------------------------ >8 ------------------------"

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Term(NamedTuple):
    """One entry of the term list.

    ``label`` is what a hit prints; ``needle`` is the lower-cased
    substring for a plain term and empty for a regex; ``regex`` is the
    compiled expression for an ``re:`` entry and None otherwise.
    """
    label: str
    needle: str
    regex: Optional[Pattern]


class Hit(NamedTuple):
    """One finding: where it is and which term it matched."""
    path: str
    line: int
    term: str


class TermListError(Exception):
    """The term list exists but cannot be used (unreadable, bad regex)."""


def parse_terms(text: str, source: str) -> List[Term]:
    """Parse the term list's text; raise TermListError on a bad regex."""
    terms = []  # type: List[Term]
    for number, raw in enumerate(text.splitlines(), 1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("re:"):
            expression = entry[3:].strip()
            if not expression:
                raise TermListError("%s:%d: empty regex" % (source, number))
            try:
                compiled = re.compile(expression, re.IGNORECASE)
            except re.error as exc:
                raise TermListError("%s:%d: invalid regex %r: %s"
                                    % (source, number, expression, exc))
            terms.append(Term("re:" + expression, "", compiled))
        else:
            terms.append(Term(entry, entry.lower(), None))
    return terms


def load_terms(path: str) -> List[Term]:
    """Read and parse the term list at ``path``."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise TermListError("%s: cannot read the term list: %s" % (path, exc))
    return parse_terms(text, path)


def _git(args: Sequence[str], cwd: str) -> Tuple[int, bytes]:
    """Run git; return (exit code, stdout bytes). Never raises on exit."""
    try:
        proc = subprocess.Popen(
            ["git"] + list(args), cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return 127, b""
    out, _err = proc.communicate()
    return proc.returncode, out


def main_checkout_root(cwd: str) -> Optional[str]:
    """The main checkout's root for the repository at ``cwd``, or None.

    ``--git-common-dir`` is the main checkout's ``.git`` from any linked
    worktree; its parent is the main checkout. A bare repository has no
    such parent worth looking in, which simply finds nothing.
    """
    code, out = _git(["rev-parse", "--git-common-dir"], cwd)
    if code != 0:
        return None
    common = out.decode("utf-8", "replace").strip()
    if not common:
        return None
    return os.path.dirname(os.path.abspath(os.path.join(cwd, common)))


def find_terms_file(cwd: str) -> Optional[str]:
    """The term list to use: this checkout's, else the main checkout's."""
    candidates = [os.path.join(REPO_ROOT, TERMS_RELATIVE)]
    for start in (REPO_ROOT, cwd):
        main_root = main_checkout_root(start)
        if main_root is not None:
            candidates.append(os.path.join(main_root, TERMS_RELATIVE))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _is_public_ipv4(match: Match) -> bool:
    octets = [int(group) for group in match.groups()]
    if any(octet > 255 for octet in octets):
        return False
    if octets[0] == 127 or octets == [0, 0, 0, 0]:
        return False
    return True


def scan_line(text: str, terms: Sequence[Term]) -> List[str]:
    """Every term (labels) and built-in pattern found in one line."""
    found = []  # type: List[str]
    lowered = text.lower()
    for term in terms:
        if term.regex is not None:
            if term.regex.search(text):
                found.append(term.label)
        elif term.needle in lowered:
            found.append(term.label)
    for email in _EMAIL.finditer(text):
        if email.group(0).lower() not in ALLOWED_EMAILS:
            found.append("email address " + email.group(0))
    for address in _IPV4.finditer(text):
        if _is_public_ipv4(address):
            found.append("IPv4 address " + address.group(0))
    return found


def scan_lines(path: str, lines: Sequence[Tuple[int, str]],
               terms: Sequence[Term]) -> List[Hit]:
    """Scan (line number, text) pairs belonging to ``path``."""
    hits = []  # type: List[Hit]
    for number, text in lines:
        for label in scan_line(text, terms):
            hits.append(Hit(path, number, label))
    return hits


def _read_text(path: str) -> str:
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def message_lines(text: str) -> List[Tuple[int, str]]:
    """A commit message's lines, up to git's ``--verbose`` scissors line.

    Only what git strips in EVERY cleanup mode is skipped: the scissors
    line and the diff below it. Lines starting ``#`` are scanned:
    ``git commit -m`` and ``-F`` use whitespace cleanup, which keeps
    them, so ``# Reported by <name>`` would otherwise be published
    unchecked. In editor mode they include git's own commentary (the
    branch name), which is a false positive the author can reword, not
    a hole. The diff's added lines are scanned by the ``pre-commit`` hook.
    """
    kept = []  # type: List[Tuple[int, str]]
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#") and _SCISSORS in line:
            break
        kept.append((number, line))
    return kept


def text_lines(text: str) -> List[Tuple[int, str]]:
    """Every line of a file, numbered from 1."""
    return list(enumerate(text.splitlines(), 1))


def parse_added_lines(diff: str) -> List[Tuple[str, int, str]]:
    """(path, new line number, text) for every added line of a diff.

    Expects ``git diff -U0`` output. Hunk sizes are counted rather than
    trusting a leading ``+``, so an added line that itself begins with
    ``++`` is not mistaken for a file header. Binary files have no
    hunks and therefore contribute nothing.
    """
    added = []  # type: List[Tuple[str, int, str]]
    path = ""
    old_left = 0
    new_left = 0
    new_line = 0
    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                added.append((path, new_line, line[1:]))
                new_line += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                old_left = 0
                new_left = 0
            continue
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@"):
            match = _HUNK.match(line)
            if match is None:
                continue
            old_left = int(match.group(1)) if match.group(1) is not None else 1
            new_line = int(match.group(2))
            new_left = int(match.group(3)) if match.group(3) is not None else 1
    return added


def staged_hits(repo: str, terms: Sequence[Term]) -> Tuple[List[Hit], str]:
    """Hits in the added lines of the index; (hits, error message)."""
    code, out = _git(["-c", "core.quotepath=off", "diff", "--cached",
                      "-U0", "--no-color", "--no-ext-diff",
                      "--diff-filter=ACMR"], repo)
    if code != 0:
        return [], "git diff --cached failed in %s (exit %d)" % (repo, code)
    diff = out.decode("utf-8", "replace").replace("\r\n", "\n")
    hits = []  # type: List[Hit]
    for path, number, text in parse_added_lines(diff):
        for label in scan_line(text, terms):
            hits.append(Hit(path, number, label))
    return hits, ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Block publication of private terms (PROCESS.md 6).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--message", metavar="FILE",
                      help="a commit message file (the commit-msg hook)")
    mode.add_argument("--staged", action="store_true",
                      help="added lines of git diff --cached (pre-commit)")
    mode.add_argument("--text", metavar="FILE", nargs="+",
                      help="any files, scanned whole (e.g. a PR body)")
    parser.add_argument("--terms", metavar="FILE", default=None,
                        help="term list to use instead of the default "
                             ".claude/private/terms.txt lookup")
    parser.add_argument("--repo", metavar="DIR", default=None,
                        help="repository for --staged (default: cwd)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo = os.path.abspath(args.repo or os.getcwd())

    terms_path = args.terms
    if terms_path is None:
        terms_path = find_terms_file(repo)
    terms = []  # type: List[Term]
    if terms_path is None or not os.path.isfile(terms_path):
        print("publication_gate: warning: no term list (%s); built-in "
              "patterns only" % (terms_path or TERMS_RELATIVE))
    else:
        try:
            terms = load_terms(terms_path)
        except TermListError as exc:
            print("publication_gate: error: %s" % exc)
            return 2

    hits = []  # type: List[Hit]
    try:
        if args.message is not None:
            hits = scan_lines(args.message,
                              message_lines(_read_text(args.message)), terms)
        elif args.staged:
            hits, error = staged_hits(repo, terms)
            if error:
                print("publication_gate: error: %s" % error)
                return 2
        else:
            for path in args.text:
                hits.extend(scan_lines(path, text_lines(_read_text(path)),
                                       terms))
    except OSError as exc:
        print("publication_gate: error: %s" % exc)
        return 2

    for hit in hits:
        print("%s:%d: %s" % (hit.path, hit.line, hit.term))
    if hits:
        print("publication_gate: %d hit(s); this repository is public "
              "(PROCESS.md section 6). Reword, then commit again."
              % len(hits))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
