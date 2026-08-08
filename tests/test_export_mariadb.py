"""The MariaDB export must survive the values that are actually in there.

Test names contain spaces and brackets, captured output contains tabs
and newlines by definition, and the outputs are compressed binary. A
TSV exporter that is nearly right corrupts a subset of rows and reports
success — and the corruption is only discovered when somebody opens a
run in the migrated dashboard and finds the wrong text.

**What this file cannot cover:** the load. There is no MariaDB here or
in CI, so the escaping is round-tripped against the same rules
``LOAD DATA`` applies rather than against a real server. The runbook's
dry run (§E.1) is what verifies the other half; do not treat a green
suite as proof that the load works.

Python 3.6 compatible; standard library only.
"""

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
import zlib
from typing import Dict, List, Optional

from testboard.storage import MIGRATIONS, Storage, apply_migration_statement
from tools import export_for_mariadb as exporter


class EscapingTest(unittest.TestCase):
    """Round-trip the characters that break naive TSV."""

    def test_ordinary_text_is_unchanged(self) -> None:
        self.assertEqual(exporter.escape("test_ok"), "test_ok")

    def test_none_becomes_the_null_sentinel(self) -> None:
        self.assertEqual(exporter.escape(None), exporter.NULL)
        self.assertIsNone(exporter.unescape(exporter.NULL))

    def test_the_separators_survive(self) -> None:
        for value in ("a\tb", "a\nb", "a\r\nb", "a\\b", "\t\n\\",
                      "trailing\t", "\nleading"):
            self.assertEqual(
                exporter.unescape(exporter.escape(value)), value,
                repr(value))

    def test_a_backslash_is_escaped_before_the_others(self) -> None:
        r"""Order matters. Escaping tabs first would turn "a\tb" (a real
        backslash then a 't') into an escaped tab, silently changing the
        value."""
        literal = "a\\tb"          # backslash, t, b — NOT a tab
        self.assertEqual(exporter.escape(literal), "a\\\\tb")
        self.assertEqual(exporter.unescape(exporter.escape(literal)),
                         literal)

    def test_non_ascii_survives(self) -> None:
        for value in ("café", "日本語", "emoji 🙂", "naïve · test"):
            self.assertEqual(
                exporter.unescape(exporter.escape(value)), value)

    def test_blobs_become_hex(self) -> None:
        blob = zlib.compress(b"some output\twith\ttabs\nand newlines")
        hexed = exporter.escape(blob)
        self.assertTrue(all(c in "0123456789abcdef" for c in hexed))
        self.assertEqual(bytes.fromhex(hexed), blob)

    def test_an_empty_string_is_not_null(self) -> None:
        """`source_link` is NOT NULL and routinely empty. Confusing the
        two would turn every one of them into a constraint violation."""
        self.assertEqual(exporter.escape(""), "")
        self.assertEqual(exporter.unescape(""), "")


class SizingTest(unittest.TestCase):
    """The InnoDB index-key limit is a hard stop, not a warning."""

    def test_the_default_sizes_fit(self) -> None:
        sizes = exporter.Sizes(64, 255, 255)
        self.assertLessEqual(sizes.index_bytes(), 3072)

    def test_oversized_columns_are_refused_before_any_work(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_export_")
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(SystemExit) as caught:
            exporter.export(
                ":memory:", os.path.join(tmp, "out"),
                exporter.Sizes(255, 255, 255))
        self.assertIn("3072", str(caught.exception))


class ExportTest(unittest.TestCase):
    """A real database in, a loadable directory out."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_export_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        self.out = os.path.join(self.tmp, "out")
        self._seed()

    def _seed(self) -> None:
        import datetime
        from testboard.model import Result, RunRecord
        store = Storage(self.db)
        self.addCleanup(store.close)
        start = datetime.datetime(2026, 7, 25, 2, 0, 0)
        store.upsert_runs([
            RunRecord(
                environment="linux sim",
                script="suite/a b.py",
                test_name="test_weird [1]\twith tab",
                result=Result.FAIL,
                start_time=start,
                end_time=start + datetime.timedelta(seconds=3),
                output="line one\nline two\ttabbed\nback\\slash\ncafé 🙂",
                source_link="",
                known_failure_reason=None,
                branch=None,
                build=None,
            ),
            RunRecord(
                environment="linux sim", script="suite/a b.py",
                test_name="plain", result=Result.PASS,
                start_time=start,
                end_time=start + datetime.timedelta(seconds=1),
                output="", source_link="http://x/y",
                known_failure_reason="known",
                branch=None,
                build=None,
            ),
        ])
        store.add_comment(
            "linux sim", "suite/a b.py", "plain", "alice",
            "a comment\twith a tab", start)
        store.set_assignee(
            "linux sim", "suite/a b.py", "plain", "alice", "alice", start)

    def _export(self) -> Dict[str, int]:
        return exporter.export(
            self.db, self.out, exporter.Sizes(64, 255, 255))

    def test_it_writes_the_documented_files(self) -> None:
        self._export()
        for name in ("schema.sql", "load.sql", "verify_source.txt",
                     "verify.sql", "runs.tsv", "run_outputs.tsv",
                     "users.tsv", "latest_runs.tsv"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.out, name)), name)

    def test_it_refuses_to_overwrite_an_existing_export(self) -> None:
        self._export()
        with self.assertRaises(SystemExit) as caught:
            self._export()
        self.assertIn("not empty", str(caught.exception))

    def test_every_row_is_written(self) -> None:
        counts = self._export()
        self.assertEqual(counts["runs"], 2)
        self.assertEqual(counts["run_outputs"], 2)
        self.assertEqual(counts["comments"], 1)
        self.assertEqual(counts["users"], 1)

    def test_the_hostile_row_round_trips(self) -> None:
        """The whole point of the file: tabs, newlines, backslashes,
        brackets and non-ASCII in one identity, exported and parsed back
        with the rules LOAD DATA applies."""
        self._export()
        with io.open(os.path.join(self.out, "runs.tsv"),
                     encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        rows = [
            [exporter.unescape(field) for field in line.split("\t")]
            for line in lines if line
        ]
        self.assertEqual(len(rows), 2)
        names = sorted(row[3] for row in rows)
        self.assertEqual(names, ["plain", "test_weird [1]\twith tab"])
        for row in rows:
            self.assertEqual(row[1], "linux sim")
            self.assertEqual(row[2], "suite/a b.py")

    def test_the_blob_round_trips_through_hex(self) -> None:
        """The likeliest thing to be subtly wrong, and the least likely
        to be noticed: output is read by exactly one endpoint."""
        self._export()
        with io.open(os.path.join(self.out, "run_outputs.tsv"),
                     encoding="utf-8") as handle:
            rows = [line.split("\t") for line in
                    handle.read().split("\n") if line]
        self.assertEqual(len(rows), 2)
        restored = set()
        for _run_id, hexed in rows:
            raw = bytes.fromhex(hexed)
            restored.add(zlib.decompress(raw).decode("utf-8"))
        self.assertIn("line one\nline two\ttabbed\nback\\slash\ncafé 🙂",
                      restored)

    def test_a_null_is_distinguishable_from_an_empty_string(self) -> None:
        self._export()
        with io.open(os.path.join(self.out, "runs.tsv"),
                     encoding="utf-8") as handle:
            rows = [
                [exporter.unescape(f) for f in line.split("\t")]
                for line in handle.read().split("\n") if line
            ]
        # Column 8 = known_failure_reason. Positional on purpose (the
        # TSV has no header), but not row[-1]: migration 6 appended
        # output_fingerprint, and "the last column" silently became a
        # different question.
        reasons = sorted(
            ("<null>" if row[8] is None else row[8]) for row in rows)
        self.assertEqual(reasons, ["<null>", "known"])
        # And the empty source_link must NOT have become a NULL: the
        # column is NOT NULL and empty links are routine, so confusing
        # the two would fail every one of them on load.
        links = sorted(row[7] for row in rows)
        self.assertEqual(links, ["", "http://x/y"])


class LoadOrderTest(unittest.TestCase):
    """Parents before children — kept although the generated schema
    declares no foreign keys (runbook §B.6): the order costs nothing,
    and any future adoption of real constraints depends on it."""

    def test_parents_load_before_children(self) -> None:
        order = list(exporter.TABLE_ORDER)
        for parent, child in (
            ("users", "comments"),
            ("users", "assignments"),
            ("users", "test_retirements"),
            ("runs", "run_outputs"),
            ("runs", "latest_runs"),
        ):
            self.assertLess(
                order.index(parent), order.index(child),
                "{0} must load before {1}".format(parent, child))

    def test_every_exported_table_is_in_the_order(self) -> None:
        """A table missing from TABLE_ORDER is silently not exported."""
        tmp = tempfile.mkdtemp(prefix="testboard_export_")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Storage(os.path.join(tmp, "t.db"))
        store.close()
        conn = sqlite3.connect(os.path.join(tmp, "t.db"))
        try:
            actual = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")
            }
        finally:
            conn.close()
        self.assertEqual(
            actual - set(exporter.TABLE_ORDER), set(),
            "a table exists that the exporter would skip entirely")

    def test_the_blob_column_is_unhexed_on_load(self) -> None:
        sql = exporter.load_sql(
            {"run_outputs": ["run_id", "output"]})
        self.assertIn("@hex_output", sql)
        self.assertIn("UNHEX(@hex_output)", sql)

    def test_indexes_are_created_after_the_load(self) -> None:
        sql = exporter.load_sql({"runs": ["id"]})
        self.assertLess(sql.index("LOAD DATA"), sql.index("CREATE INDEX"))


class VerificationQueriesTest(unittest.TestCase):
    """The checks must run identically on both engines."""

    def test_they_all_run_on_sqlite(self) -> None:
        tmp = tempfile.mkdtemp(prefix="testboard_export_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "t.db")
        store = Storage(path)
        store.close()
        conn = sqlite3.connect(path)
        try:
            for name, sql in exporter.VERIFY_QUERIES:
                conn.execute(sql).fetchall()   # must not raise
        finally:
            conn.close()

    def test_no_engine_specific_construct_is_used(self) -> None:
        """COUNT(DISTINCT a, b, c) is MariaDB-only and DATE() parses an
        ISO string on one engine and not the other. Either would compare
        two different questions and report the agreement as meaningful."""
        for name, sql in exporter.VERIFY_QUERIES:
            upper = sql.upper()
            self.assertNotIn("DATE(", upper, name)
            self.assertNotIn("JULIANDAY", upper, name)
            self.assertNotIn("STRFTIME", upper, name)
            if "COUNT(DISTINCT" in upper:
                self.fail(
                    name + ": COUNT(DISTINCT a, b, c) is MariaDB-only; "
                    "use SELECT COUNT(*) FROM (SELECT DISTINCT ...)")

    def test_both_sides_come_from_one_list(self) -> None:
        """Two hand-written variants would drift."""
        sql_text = exporter.verify_sql()
        for name, _query in exporter.VERIFY_QUERIES:
            self.assertIn(name, sql_text)

    def test_the_runbook_checks_are_all_present(self) -> None:
        names = {name for name, _ in exporter.VERIFY_QUERIES}
        for expected in ("runs_total", "output_bytes", "distinct_tests",
                         "by_env_result", "by_day_result",
                         "schema_version", "run_span"):
            self.assertIn(expected, names)


class SchemaTest(unittest.TestCase):
    """The generated DDL, against the runbook's rules."""

    def setUp(self) -> None:
        self.ddl = exporter.ddl(exporter.Sizes(64, 255, 255))

    def test_identity_columns_are_bounded(self) -> None:
        """TEXT cannot be indexed in full by InnoDB."""
        self.assertNotIn("environment          TEXT", self.ddl)
        self.assertIn("VARCHAR(64)", self.ddl)
        self.assertIn("VARCHAR(255)", self.ddl)

    def test_the_output_column_is_a_longblob(self) -> None:
        """MEDIUMBLOB caps at 16 MB and output is not bounded."""
        self.assertIn("LONGBLOB", self.ddl)

    def test_ids_are_bigint(self) -> None:
        self.assertIn("BIGINT NOT NULL AUTO_INCREMENT", self.ddl)
        self.assertNotIn("AUTOINCREMENT", self.ddl)   # the SQLite spelling

    def test_it_carries_every_migration_so_far(self) -> None:
        """The DDL must describe the CURRENT schema, not the launch one.

        Migration 2 added the deactivation columns and 3 added
        duration_seconds; exporting into a schema without them loses the
        data silently.
        """
        self.assertIn("deactivated_at", self.ddl)
        self.assertIn("deactivated_by", self.ddl)
        self.assertIn("duration_seconds", self.ddl)

    def test_it_warns_about_collation_and_strict_mode(self) -> None:
        """The two settings that silently corrupt rather than fail."""
        self.assertIn("collation", self.ddl.lower())
        self.assertIn("strict", self.ddl.lower())


if __name__ == "__main__":
    unittest.main()
