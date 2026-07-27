"""The migration list describes a database that exists in production.

testboard went live on 2026-07-26. From that moment ``MIGRATIONS[0]``
stopped being "the schema" and became "a description of what is already
on disk in production". Editing it does not change that database. It
changes what this code *believes* about that database, which is worse
than a wrong schema: it is a wrong schema that nothing detects until a
query fails against a column the code is certain exists.

So the rules this file enforces are:

1. Entry 1 is frozen. A change to the schema is a NEW entry, appended.
2. Versions are unique, contiguous and ascending — because several
   packages want schema changes at once, and two of them independently
   writing "entry 2" produces a MIGRATIONS list that merges cleanly,
   passes review, and silently applies one migration where two were
   meant.
3. A database built by applying every migration in order must be
   identical to one built at version N-1 and then stepped forward.
   Production takes the second path and every developer takes the first,
   so they are the two paths that must not drift.
4. A database from a newer build is refused, not used.

Python 3.6 compatible; standard library only.
"""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from typing import Dict, List, Optional, Tuple

from testboard.storage import MIGRATIONS, Storage

#: SHA-256 of migration 1's normalised DDL, recorded when testboard was
#: deployed. Whitespace and comments are normalised away first, so this
#: pins the SCHEMA rather than the formatting — a reflow or a better
#: comment is allowed, an added column is not.
#:
#: If this test fails, the fix is essentially never to update this
#: constant. It is to move the change into a new migration entry.
DEPLOYED_MIGRATION_1_SHA256 = (
    "9b9dd4d02b02bd2a01a3fc42cab58dbd15c83059818380184049191f85fa1412"
)

#: Statement count of entry 1 as deployed. Belt and braces: a hash tells
#: you something changed, this tells you roughly what.
DEPLOYED_MIGRATION_1_STATEMENTS = 14


def normalise(statement: str) -> str:
    """Strip SQL comments and normalise whitespace in one DDL statement.

    Spacing *around punctuation* is normalised too, not just runs of
    whitespace. Collapsing whitespace alone is not enough: it leaves
    ``CREATE TABLE t (\\n  a TEXT\\n)`` and ``CREATE TABLE t (a TEXT)``
    with different digests, so re-indenting the DDL would read as a
    change to the production schema. That is how a freeze stops meaning
    anything — the constant gets updated as routine maintenance, and by
    the time it matters nobody trusts it.
    """
    text = " ".join(re.sub(r"--[^\n]*", "", statement).split())
    return re.sub(r"\s*([(),])\s*", r"\1", text)


def fingerprint(statements: List[str]) -> str:
    """A whitespace- and comment-insensitive digest of a DDL list."""
    body = ";".join(normalise(statement) for statement in statements)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def schema_of(path: str) -> List[Tuple[str, str, str]]:
    """The normalised (type, name, sql) of every object in a database.

    ``sqlite_master.sql`` is None for auto-created indexes (those backing
    a UNIQUE or PRIMARY KEY constraint), which is meaningful — it means
    the object exists because of a constraint rather than a CREATE INDEX
    — so it is kept, as the empty string.
    """
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_stat%' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1], normalise(row[2])) for row in rows]


def build_at(path: str, version: int) -> None:
    """Create a database at exactly *version*, bypassing Storage.

    This is how a production database is simulated: it was created by an
    older build that knew nothing of the migrations added since.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL)"
        )
        applied = 0
        for entry_version, statements in MIGRATIONS:
            if entry_version > version:
                break
            for statement in statements:
                conn.execute(statement)
            applied = entry_version
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (applied,)
        )
        conn.commit()
    finally:
        conn.close()


def version_of(path: str) -> int:
    """Read a database's recorded schema version."""
    conn = sqlite3.connect(path)
    try:
        return int(
            conn.execute("SELECT version FROM schema_version").fetchone()[0]
        )
    finally:
        conn.close()


class TempDirTest(unittest.TestCase):
    """A scratch directory, removed on teardown."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_migrations_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class RegistryShapeTest(unittest.TestCase):
    """The list itself, before anything is applied."""

    def test_versions_are_unique(self) -> None:
        versions = [version for version, _ in MIGRATIONS]
        duplicates = sorted(
            {v for v in versions if versions.count(v) > 1}
        )
        self.assertEqual(
            duplicates, [],
            "duplicate migration version(s) {0} — two changes claimed the "
            "same number, and only one of them will ever be applied. Claim "
            "versions from the registry in docs/UPGRADE_PLAN.md "
            "§1".format(duplicates))

    def test_versions_are_ascending(self) -> None:
        versions = [version for version, _ in MIGRATIONS]
        self.assertEqual(
            versions, sorted(versions),
            "migrations must be listed in ascending version order; they are "
            "applied in list order and compared by number, so an out-of-order "
            "entry is skipped on some databases and not others")

    def test_versions_are_contiguous_from_one(self) -> None:
        versions = [version for version, _ in MIGRATIONS]
        self.assertEqual(
            versions, list(range(1, len(versions) + 1)),
            "migration versions must run 1..N with no gaps: a gap means a "
            "database can record a version that no entry produces")

    def test_every_migration_has_statements(self) -> None:
        for version, statements in MIGRATIONS:
            self.assertTrue(
                statements, "migration {0} is empty".format(version))


class FrozenEntryOneTest(unittest.TestCase):
    """Entry 1 describes production. It is not editable."""

    def test_migration_one_matches_what_was_deployed(self) -> None:
        actual = fingerprint(MIGRATIONS[0][1])
        self.assertEqual(
            actual, DEPLOYED_MIGRATION_1_SHA256,
            "migration 1 has changed.\n\n"
            "It describes a database that EXISTS IN PRODUCTION, so editing "
            "it does not migrate anything — it only makes this code believe "
            "something untrue about a live database.\n\n"
            "Add a new entry instead, claiming its version from the registry "
            "in docs/UPGRADE_PLAN.md §1. Update this constant only if you "
            "are certain production was rebuilt from scratch.\n\n"
            "expected {0}\ngot      {1}".format(
                DEPLOYED_MIGRATION_1_SHA256, actual))

    def test_migration_one_statement_count_is_unchanged(self) -> None:
        self.assertEqual(
            len(MIGRATIONS[0][1]), DEPLOYED_MIGRATION_1_STATEMENTS,
            "a statement was added to or removed from migration 1")

    def test_migration_one_is_version_one(self) -> None:
        self.assertEqual(MIGRATIONS[0][0], 1)

    def test_the_fingerprint_ignores_formatting_but_not_content(
        self
    ) -> None:
        """Proves the freeze is on the schema, not on the whitespace.

        Without this, every reflow of the DDL would look like a
        production-schema change, the constant would be updated as a
        matter of routine, and the check would stop meaning anything.
        """
        original = ["CREATE TABLE t (\n  a TEXT\n)  -- a comment\n"]
        reflowed = ["CREATE TABLE t (a TEXT) -- different comment"]
        changed = ["CREATE TABLE t (a TEXT, b TEXT)"]
        self.assertEqual(fingerprint(original), fingerprint(reflowed))
        self.assertNotEqual(fingerprint(original), fingerprint(changed))


class ApplicationTest(TempDirTest):
    """Applying migrations to a real database."""

    def test_a_fresh_database_reaches_the_latest_version(self) -> None:
        path = self.path("fresh.db")
        storage = Storage(path)
        self.addCleanup(storage.close)
        self.assertEqual(version_of(path), MIGRATIONS[-1][0])

    def test_opening_twice_applies_nothing_the_second_time(self) -> None:
        path = self.path("twice.db")
        first = Storage(path)
        first.close()
        before = schema_of(path)
        second = Storage(path)
        self.addCleanup(second.close)
        self.assertEqual(schema_of(path), before)

    def test_stepwise_and_fresh_schemas_are_identical(self) -> None:
        """The two paths that must never drift.

        Production upgrades one version at a time. Developers create the
        database from nothing. If migration N does not reproduce exactly
        what entry 1..N would have created outright, those two
        populations run different schemas — and the difference shows up
        as a query that works on every laptop and fails on the server.
        """
        fresh = self.path("fresh.db")
        fresh_storage = Storage(fresh)
        fresh_storage.close()

        for target in [version for version, _ in MIGRATIONS][:-1]:
            stepped = self.path("stepped_{0}.db".format(target))
            build_at(stepped, target)
            self.assertEqual(version_of(stepped), target)
            storage = Storage(stepped)
            storage.close()
            self.assertEqual(
                version_of(stepped), MIGRATIONS[-1][0],
                "stepping up from version {0} did not reach the "
                "latest".format(target))
            self.assertEqual(
                schema_of(stepped), schema_of(fresh),
                "a database upgraded from version {0} has a DIFFERENT "
                "schema from one created fresh. Production takes the "
                "upgrade path; every dev machine takes the fresh "
                "one.".format(target))

    def test_a_newer_database_is_refused(self) -> None:
        path = self.path("newer.db")
        build_at(path, MIGRATIONS[-1][0])
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE schema_version SET version = ?",
            (MIGRATIONS[-1][0] + 5,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError) as caught:
            Storage(path)
        self.assertIn("NEWER version", str(caught.exception))

    def test_a_failing_migration_leaves_the_schema_untouched(self) -> None:
        """Migrations run in one transaction; a half-applied schema is
        the one state nothing downstream is written to cope with."""
        path = self.path("rollback.db")
        build_at(path, MIGRATIONS[-1][0])
        before = schema_of(path)
        broken = list(MIGRATIONS) + [
            (MIGRATIONS[-1][0] + 1,
             ["CREATE TABLE ok_so_far (a TEXT)",
              "THIS IS NOT SQL"]),
        ]
        import testboard.storage as storage_module
        original = storage_module.MIGRATIONS
        storage_module.MIGRATIONS = broken
        try:
            with self.assertRaises(sqlite3.Error):
                Storage(path)
        finally:
            storage_module.MIGRATIONS = original
        self.assertEqual(
            schema_of(path), before,
            "a failed migration left objects behind")
        self.assertEqual(version_of(path), MIGRATIONS[-1][0])


class DataSurvivesTest(TempDirTest):
    """Migrations run against databases that already hold data.

    An empty-database test proves the DDL parses. It does not prove the
    migration is safe to run against production, which is the only place
    it will ever actually run.
    """

    def test_existing_rows_survive_every_migration(self) -> None:
        path = self.path("populated.db")
        build_at(path, 1)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO runs (environment, script, test_name, result, "
            "start_time, end_time, source_link) VALUES "
            "('prod', 's.py', 't', 'FAIL', "
            "'2026-07-25T01:00:00.000000', '2026-07-25T01:00:02.000000', '')"
        )
        run_id = conn.execute("SELECT id FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO latest_runs (environment, script, test_name, "
            "run_id, start_time, result) VALUES "
            "('prod', 's.py', 't', ?, '2026-07-25T01:00:00.000000', 'FAIL')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO users (username, created_at) "
            "VALUES ('someone', '2026-07-25T00:00:00.000000')"
        )
        conn.commit()
        conn.close()

        storage = Storage(path)
        self.addCleanup(storage.close)

        conn = sqlite3.connect(path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM latest_runs").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT username FROM users").fetchone()[0], "someone")
        finally:
            conn.close()


class PlantedRegressionTest(unittest.TestCase):
    """Prove each detector can fail, rather than that it passes today.

    A guard that has never been seen to fail is not evidence of
    anything.
    """

    def test_an_edited_entry_one_is_detected(self) -> None:
        edited = list(MIGRATIONS[0][1]) + [
            "CREATE TABLE sneaked_in (a TEXT)"]
        self.assertNotEqual(
            fingerprint(edited), DEPLOYED_MIGRATION_1_SHA256)

    def test_a_column_added_to_entry_one_is_detected(self) -> None:
        """The realistic mistake: not a new statement, an edited one."""
        edited = [
            statement.replace(
                "known_failure_reason TEXT,",
                "known_failure_reason TEXT, extra TEXT,")
            for statement in MIGRATIONS[0][1]
        ]
        self.assertNotEqual(edited, list(MIGRATIONS[0][1]),
                            "the planted edit did not apply")
        self.assertNotEqual(
            fingerprint(edited), DEPLOYED_MIGRATION_1_SHA256)

    def test_a_duplicate_version_is_detected(self) -> None:
        versions = [1, 2, 2, 3]
        duplicates = sorted({v for v in versions if versions.count(v) > 1})
        self.assertEqual(duplicates, [2])

    def test_a_gap_in_the_sequence_is_detected(self) -> None:
        versions = [1, 2, 4]
        self.assertNotEqual(versions, list(range(1, len(versions) + 1)))

    def test_out_of_order_entries_are_detected(self) -> None:
        versions = [1, 3, 2]
        self.assertNotEqual(versions, sorted(versions))


if __name__ == "__main__":
    unittest.main()
