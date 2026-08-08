"""What in storage.py is SQLite-specific, and what it does today.

The database is moving to MariaDB (``docs/MARIADB_MIGRATION.md``). The
port itself cannot be verified here — there is no MariaDB in this
environment and none in CI — so what this file does instead is the part
that CAN be verified: make the SQLite-specific surface explicit, keep it
from growing unnoticed, and write down the behaviours the port has to
reproduce.

Two kinds of assertion:

1. **An inventory.** Every SQLite-specific construct, counted, against a
   committed expectation. When somebody adds a tenth, this says so, and
   the runbook's translation table stays true instead of quietly going
   stale.
2. **Behaviour pins.** ``INSERT OR REPLACE`` deletes and re-inserts;
   MariaDB's ``ON DUPLICATE KEY UPDATE`` updates in place. That is a
   difference in behaviour, not syntax, and the only way to port it
   safely is to know what the current behaviour actually is rather than
   what the code looks like it does.

Python 3.6 compatible; standard library only.
"""

import ast
import io
import os
import re
import unittest
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_PATH = os.path.join(REPO_ROOT, "testboard", "storage.py")

#: SQLite-specific constructs, and how many times each appears in the SQL
#: that storage.py actually sends. Committed so a new one is a test
#: failure rather than a surprise during the port.
#:
#: Update these numbers WITH the runbook's §B translation table, never
#: on their own — the point of the number is that the two agree.
EXPECTED = {
    # Delete-and-reinsert upsert. See RewriteSemanticsTest for what that
    # means and why these two sites are safe.
    "INSERT OR REPLACE": 2,
    # SQLite spelling; MariaDB wants AUTO_INCREMENT. WP-21 adds one:
    # streams.id (migration 9).
    "AUTOINCREMENT": 4,
    # Date functions: removed by migration 3 and must stay removed.
    "julianday": 0,
    "strftime": 0,
}  # type: Dict[str, int]

#: The 3.6 parser emits ``ast.Str`` for string literals; 3.8+ emits
#: ``ast.Constant``; 3.12 removed ``ast.Str``. A Constant-only scan finds
#: NOTHING on the deployment interpreter, and every count then "passes"
#: over an empty list — which is what test_the_scan_finds_the_sql exists
#: to catch, and did, on the ubi8 CI leg.
_AST_STR = getattr(ast, "Str", None)


def _string_value(node: ast.AST) -> Optional[str]:
    """The text of a string literal, across ast.Str and ast.Constant."""
    if _AST_STR is not None and isinstance(node, _AST_STR):
        return node.s
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def sql_literals() -> List[str]:
    """Every non-docstring string literal in storage.py.

    Only string literals reach the database. Scanning raw file text
    instead would count the prose that explains why a construct was
    removed as a use of it.
    """
    with io.open(STORAGE_PATH, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=STORAGE_PATH)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                docstrings.add(id(body[0].value))
    found = []  # type: List[str]
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        value = _string_value(node)
        if value is not None:
            found.append(value)
    return found


def count_construct(construct: str, literals: List[str]) -> int:
    pattern = re.compile(re.escape(construct), re.IGNORECASE)
    return sum(len(pattern.findall(text)) for text in literals)


class InventoryTest(unittest.TestCase):
    """The SQLite-specific surface, counted."""

    def setUp(self) -> None:
        self.literals = sql_literals()

    def test_the_scan_finds_the_sql(self) -> None:
        """A scan matching nothing would pass every count below."""
        self.assertGreater(len(self.literals), 100)
        self.assertTrue(
            any("SELECT" in t and "latest_runs" in t for t in self.literals))

    def test_the_inventory_matches(self) -> None:
        actual = {
            construct: count_construct(construct, self.literals)
            for construct in EXPECTED
        }
        self.assertEqual(
            actual, EXPECTED,
            "the SQLite-specific surface of storage.py changed. That is "
            "allowed, but docs/MARIADB_MIGRATION.md §B describes this "
            "exact set — update both together, or the runbook is lying "
            "to whoever runs the migration at 3am")

    def test_no_sqlite_date_function_is_used(self) -> None:
        """Migration 3 removed the last one; it must stay removed."""
        for banned in ("julianday", "strftime", "unixepoch"):
            pattern = re.compile(r"\b" + banned + r"\s*\(", re.IGNORECASE)
            offenders = [t for t in self.literals if pattern.search(t)]
            self.assertEqual(
                offenders, [],
                banned + "() has no MariaDB equivalent with the same "
                "semantics; compute it in Python and store it")

    def test_placeholders_are_qmark_everywhere(self) -> None:
        """Qmark-in-source is permanent policy, not a pre-port state.

        sqlite3 is ``qmark`` (``?``); PyMySQL is ``pyformat`` (``%s``).
        The port (WP-19) deliberately did NOT rewrite the SQL: the
        source stays qmark-canonical and ``testboard/mariadb.py``
        translates at execute time, doubling any literal ``%`` first.
        So a ``%s`` appearing in storage.py is wrong on BOTH engines
        now — SQLite would bind it as a literal, and the MariaDB
        translation would double its ``%``.
        """
        qmark = sum(text.count("?") for text in self.literals)
        self.assertGreater(qmark, 50)
        percent_s = sum(
            len(re.findall(r"(?<!%)%s", text)) for text in self.literals)
        self.assertEqual(
            percent_s, 0,
            "storage.py's SQL is qmark-canonical by policy; the MariaDB "
            "backend translates at execute time. Write ? and let "
            "testboard/mariadb.py do the spelling")


class RewriteSemanticsTest(unittest.TestCase):
    """What ``INSERT OR REPLACE`` does here, written down.

    SQLite's OR REPLACE **deletes the existing row and inserts a new
    one**; MariaDB's ``ON DUPLICATE KEY UPDATE`` **updates in place**.
    Where a table has an AUTOINCREMENT id, or something references its
    rows, that difference is observable.

    The reassuring finding, recorded so nobody has to re-derive it: it
    is NOT observable at either of the two sites here.
    """

    def test_both_uses_are_on_tables_without_an_autoincrement_id(
        self
    ) -> None:
        """run_outputs is keyed by run_id, test_retirements by the test
        triple. Neither has a generated id to churn and nothing holds a
        foreign key to either, so delete-and-reinsert and update-in-place
        are indistinguishable from outside."""
        literals = sql_literals()
        targets = []
        for text in literals:
            for match in re.finditer(
                    r"INSERT OR REPLACE INTO\s+(\w+)", text, re.IGNORECASE):
                targets.append(match.group(1))
        self.assertEqual(
            sorted(targets), ["run_outputs", "test_retirements"],
            "a new INSERT OR REPLACE appeared. If its table has an "
            "AUTOINCREMENT id or anything references it, MariaDB's "
            "ON DUPLICATE KEY UPDATE is NOT an equivalent — see "
            "docs/MARIADB_MIGRATION.md §B.5")

    def test_run_import_deliberately_avoids_it(self) -> None:
        """`runs` is the table where it WOULD matter, and it is the one
        table that does not use it."""
        with io.open(STORAGE_PATH, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("INSERT OR REPLACE INTO runs", source)


class RunIdStabilityTest(unittest.TestCase):
    """Re-importing a run must not change its id.

    This is the contract the MariaDB port has to reproduce, and the
    reason `upsert_runs` uses SELECT-then-UPDATE rather than OR REPLACE.
    ``run_outputs.run_id`` and ``latest_runs.run_id`` both reference it,
    and re-import is what the feeder does every night — and what the
    force-reload flag does deliberately when a reader was wrong.
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        import datetime
        from testboard.storage import Storage
        from testboard.model import Result, RunRecord
        self.Result = Result
        self.RunRecord = RunRecord
        self.datetime = datetime
        self.tmp = tempfile.mkdtemp(prefix="testboard_portability_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Storage(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.store.close)

    def _record(self, result, output):
        start = self.datetime.datetime(2026, 7, 1, 2, 0, 0)
        return self.RunRecord(
            environment="linux", script="s.py", test_name="t",
            result=result, start_time=start,
            end_time=start + self.datetime.timedelta(seconds=2),
            output=output, source_link="", known_failure_reason=None,
            branch=None, build=None,
        )

    def _run_id(self):
        return self.store._conn().execute(
            "SELECT id FROM runs").fetchone()[0]

    def test_reimport_updates_in_place_and_keeps_the_id(self) -> None:
        self.store.upsert_runs([self._record(self.Result.PASS, "first")])
        first_id = self._run_id()

        counts = self.store.upsert_runs(
            [self._record(self.Result.FAIL, "corrected")])
        self.assertEqual((counts.inserted, counts.updated), (0, 1))
        self.assertEqual(
            self._run_id(), first_id,
            "re-importing a run changed its id. run_outputs and "
            "latest_runs both reference it, and the feeder re-imports "
            "every night")

    def test_the_referencing_rows_follow_the_repair(self) -> None:
        self.store.upsert_runs([self._record(self.Result.PASS, "first")])
        self.store.upsert_runs(
            [self._record(self.Result.FAIL, "corrected")])
        run_id = self._run_id()
        stored = self.store.get_run(run_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.result, self.Result.FAIL)
        self.assertEqual(stored.output, "corrected")
        pointer = self.store._conn().execute(
            "SELECT run_id, result FROM latest_runs").fetchone()
        self.assertEqual(pointer[0], run_id)
        self.assertEqual(pointer[1], "FAIL")

    def test_there_is_still_exactly_one_row(self) -> None:
        """Idempotent means repaired, not duplicated."""
        for _ in range(4):
            self.store.upsert_runs([self._record(self.Result.FAIL, "x")])
        count = self.store._conn().execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(count, 1)


class PlantedRegressionTest(unittest.TestCase):
    """Prove the inventory can fail."""

    def test_a_new_construct_would_be_counted(self) -> None:
        literals = ["SELECT julianday(x) FROM runs"]
        self.assertEqual(count_construct("julianday", literals), 1)

    def test_the_expectation_is_not_vacuous(self) -> None:
        """A dict of zeroes would pass however much SQLite-ism appeared."""
        self.assertTrue(any(value > 0 for value in EXPECTED.values()))


if __name__ == "__main__":
    unittest.main()
