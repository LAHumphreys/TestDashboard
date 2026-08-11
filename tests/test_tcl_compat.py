"""Static compatibility gate for clients/feeder.tcl: vanilla Tcl 8.5.

Mirrors tests/test_python36_compat.py in spirit and in the reason it
exists: the only Tcl interpreter available while writing this file is
tclsh 8.6.18 (2026-08-10 night run, docs/SESSION_HANDOVER.md), so "the
conformance tests pass" only proves the engine EXECUTES under 8.6,
never under 8.5 - the version it is actually written for and the
version a contributing site is expected to have (a bare tclsh, no
tcllib). This module is the static gate that stands in for an 8.5
interpreter nobody on this box has: it scans the source text for
constructs that only exist from Tcl 8.6 onward and fails the build if
any appear, whether or not a test run happens to exercise them.

Detection is a word-boundary regex scan over the source with
whole/trailing "#" comments stripped first (the two comment forms this
file actually uses: a line whose first non-blank character is "#", and
a ";# ..." trailing comment). It is NOT a Tcl parser: a quoted string
containing one of the blocked words verbatim would still be flagged.
That is the safe direction for a guard test - a false "regression"
costs one look at a diff; a missed one ships to a site running real
8.5. clients/feeder.tcl is written to never need these words in prose
(e.g. "retry"/"attempt" rather than a bare "try") specifically so the
guard stays clean without needing a real parser - the same trade
test_python36_compat.py makes explicitly for its own heuristic gaps
(see VendoredCodeTest's PEP 604 module-assignment arm there).

A gate that cannot fail is decoration: PlantedRegressionTest feeds
each blocked construct through the same detector and requires it to
fire, the same discipline test_python36_compat.py applies to itself.

Python 3.6 compatible; standard library only (this is ordinary project
Python that checks a Tcl file - it does not itself need tclsh, and
runs unconditionally, unlike the conformance suite's Tcl variants).
"""

import os
import re
import unittest
from typing import List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDER_TCL = os.path.join(REPO_ROOT, "clients", "feeder.tcl")

#: (construct name, regex). The three ordinary-English words among
#: these (try/throw/yield) are cross-checked in
#: test_ordinary_words_do_not_false_positive to prove the word
#: boundaries actually hold against "retry"/"overthrown"/"yielding".
_BLOCKED = [
    ("try/finally (8.6 only - use catch)", re.compile(r"\btry\b")),
    ("throw (8.6 only)", re.compile(r"\bthrow\b")),
    ("lmap (8.6 only)", re.compile(r"\blmap\b")),
    ("string cat (8.6 only)", re.compile(r"\bstring\s+cat\b")),
    # Deliberately narrow: clients/feeder.tcl has its OWN tb::json::
    # namespace (the hand-rolled parser this guard exists to keep
    # hand-rolled), and a naive "::json" substring match would flag
    # every reference to it. Only a "package require json" statement
    # or an ABSOLUTE ::json:: reference (not preceded by another
    # namespace segment, so "tb::json::" is excluded) means tcllib.
    ("package require json (tcllib, not bundled)",
     re.compile(r"\bpackage\s+require\s+json\b")),
    ("::json:: (tcllib namespace, not bundled)",
     re.compile(r"(?<![:\w])::json::")),
    ("chan pipe (8.6 only)", re.compile(r"\bchan\s+pipe\b")),
    ("dict getwithdefault (8.6 only)",
     re.compile(r"\bdict\s+getwithdefault\b")),
    ("dict map (8.6 only)", re.compile(r"\bdict\s+map\b")),
    ("file tempfile (8.6 only)", re.compile(r"\bfile\s+tempfile\b")),
    ("zlib (8.6 only)", re.compile(r"\bzlib\b")),
    ("binary encode/decode (8.6 only)",
     re.compile(r"\bbinary\s+(encode|decode)\b")),
    ("TclOO (oo::, 8.6 only)", re.compile(r"\boo::")),
    ("coroutine (8.6 only)", re.compile(r"\bcoroutine\b")),
    ("yield (8.6 coroutines only)", re.compile(r"\byield\b")),
]  # type: List[Tuple[str, "re.Pattern[str]"]]

#: Strips a whole-line "# ..." comment or a ";# ..." trailing comment,
#: keeping whatever precedes the "#" on the line (empty for a
#: whole-line comment, the code before ";" for a trailing one).
_COMMENT_RE = re.compile(r"(^|;)[ \t]*#.*$", re.MULTILINE)


def strip_comments(source: str) -> str:
    """Remove the two comment forms clients/feeder.tcl uses."""
    return _COMMENT_RE.sub(lambda m: m.group(1), source)


def find_violations(source: str) -> List[Tuple[str, str]]:
    """Return (construct, matched text) for every blocked construct
    found in ``source`` (comments stripped first)."""
    code = strip_comments(source)
    found = []  # type: List[Tuple[str, str]]
    for name, pattern in _BLOCKED:
        match = pattern.search(code)
        if match is not None:
            found.append((name, match.group(0)))
    return found


class Tcl85CompatibilityTest(unittest.TestCase):
    """The real gate: clients/feeder.tcl itself must be clean."""

    def test_feeder_tcl_exists(self) -> None:
        self.assertTrue(
            os.path.isfile(FEEDER_TCL), "clients/feeder.tcl is missing"
        )

    def test_feeder_tcl_uses_no_86_only_constructs(self) -> None:
        with open(FEEDER_TCL, "r", encoding="utf-8") as handle:
            source = handle.read()
        violations = find_violations(source)
        self.assertEqual(
            violations, [],
            "clients/feeder.tcl uses Tcl 8.6-only constructs, but it "
            "must run on vanilla 8.5: " + repr(violations),
        )


class PlantedRegressionTest(unittest.TestCase):
    """A gate that cannot fail is decoration. Make it fail on purpose."""

    def test_detects_try_finally(self) -> None:
        violations = find_violations(
            "try {\n    foo\n} finally {\n    bar\n}\n"
        )
        names = [name for name, _ in violations]
        self.assertIn("try/finally (8.6 only - use catch)", names)

    def test_detects_throw(self) -> None:
        violations = find_violations('throw {MY ERR} "boom"\n')
        self.assertTrue(any("throw" in name for name, _ in violations))

    def test_detects_lmap(self) -> None:
        violations = find_violations(
            "set r [lmap x $list {expr {$x * 2}}]\n"
        )
        self.assertTrue(any("lmap" in name for name, _ in violations))

    def test_detects_string_cat(self) -> None:
        violations = find_violations("set s [string cat $a $b]\n")
        self.assertTrue(
            any("string cat" in name for name, _ in violations)
        )

    def test_detects_tcllib_json(self) -> None:
        violations = find_violations(
            "package require json\nset d [::json::json2dict $s]\n"
        )
        self.assertTrue(any("json" in name for name, _ in violations))

    def test_detects_chan_pipe(self) -> None:
        violations = find_violations("lassign [chan pipe] r w\n")
        self.assertTrue(
            any("chan pipe" in name for name, _ in violations)
        )

    def test_detects_dict_getwithdefault(self) -> None:
        violations = find_violations(
            "set v [dict getwithdefault $d k def]\n"
        )
        self.assertTrue(
            any("getwithdefault" in name for name, _ in violations)
        )

    def test_detects_dict_map(self) -> None:
        violations = find_violations(
            "set d2 [dict map {k v} $d {expr {$v + 1}}]\n"
        )
        self.assertTrue(any("dict map" in name for name, _ in violations))

    def test_detects_file_tempfile(self) -> None:
        violations = find_violations("set path [file tempfile]\n")
        self.assertTrue(any("tempfile" in name for name, _ in violations))

    def test_detects_zlib(self) -> None:
        violations = find_violations("set c [zlib compress $data]\n")
        self.assertTrue(any("zlib" in name for name, _ in violations))

    def test_detects_binary_encode(self) -> None:
        violations = find_violations("set e [binary encode hex $data]\n")
        self.assertTrue(
            any("binary encode" in name for name, _ in violations)
        )

    def test_detects_tcloo(self) -> None:
        violations = find_violations("oo::class create Foo\n")
        self.assertTrue(any("TclOO" in name for name, _ in violations))

    def test_detects_coroutine(self) -> None:
        violations = find_violations("coroutine c1 myproc\n")
        self.assertTrue(
            any("coroutine" in name for name, _ in violations)
        )

    def test_detects_yield(self) -> None:
        violations = find_violations("proc gen {} {\n    yield 1\n}\n")
        self.assertTrue(
            any(name == "yield (8.6 coroutines only)"
                for name, _ in violations)
        )

    def test_ordinary_words_do_not_false_positive(self) -> None:
        """retry/overthrown/yielding must not trip try/throw/yield."""
        source = (
            'puts "we retry with backoff, log any overthrown '
            'expectation, and report yielding results"\n'
        )
        self.assertEqual(find_violations(source), [], repr(source))

    def test_comments_are_stripped(self) -> None:
        """A construct mentioned only in prose must not trip the gate -
        the risk is code, not a comment describing what NOT to do."""
        source = (
            "# do not use lmap or try/finally here, use foreach and "
            "catch\nputs ok\n"
        )
        self.assertEqual(find_violations(source), [])

    def test_trailing_semicolon_comment_is_stripped(self) -> None:
        source = "puts ok ;# lmap is not allowed in comments either\n"
        self.assertEqual(find_violations(source), [])


if __name__ == "__main__":
    unittest.main()
