"""tools/upgrade_mariadb_schema.py, against a real MariaDB server.

**Why this file exists.** Production is a live MariaDB database at
schema v7 with real data, and this tool is what makes it possible to
bring it to v10 without a full SQLite export/load — the app itself
refuses to serve a version mismatch in either direction
(``testboard/mariadb.py``), so without this tool tomorrow's code simply
does not start against prod.

**Gated exactly like ``tests/backends.py``** — module-level, not a
per-test skip: with ``TESTBOARD_TEST_DB_CNF`` unset this file defines
NOTHING beyond the version-pin test at the bottom (which needs no
server), so the collected count does not move and no skip noise
appears. Point it at your OWN sacrificial database, not the shared one
``tests/backends.py`` uses — this file builds a schema from scratch
(v7, then upgrades it) and would otherwise race or collide with every
other dual-backend test class sharing that database.

**The v7 fixture is derived, not hand-written, in two separate senses
that meet in the middle:**

1. The STARTING SQLite database is built by applying
   ``storage.MIGRATIONS`` entries 1..7 through the real
   ``apply_migration_statement`` (via ``tests.test_migrations.build_at``
   — the exact helper ``test_migrations.py`` itself uses to prove the
   stepwise and fresh-install paths agree), then seeded with a handful
   of rows via plain SQL matching that schema.
2. The MariaDB SCHEMA it is translated into comes from
   ``tests/mariadb_v7_fixture.py`` — a trimmed copy of
   ``tools/export_for_mariadb.py`` AS IT STOOD at the last commit before
   migration 8 (see that file's own docstring for the exact git
   command). It is the real exporter's own translation of the v7
   schema, not a guess at what v7 "should" look like.

**Why not ``LOAD DATA LOCAL INFILE``, which is how production actually
moves rows.** That path needs ``local_infile=ON`` on the server and
``local_infile=True`` on the driver; neither ``.scratch`` test cnf
configures it, and exercising it here would make this file's reliability
depend on a server setting outside of what it is testing. The row COUNT
that matters for schema testing is a handful, so this fixture reads rows
back out of the SQLite side with plain ``SELECT`` and INSERTs them into
MariaDB with a parameterized cursor instead — same data, a transport
that needs nothing configured. ``tools/migrate_to_mariadb.py`` (the bulk
tool) is what actually proves the LOAD DATA path, in production-shaped
volume, at cutover; this file is about the SCHEMA translation, which
is unaffected by which transport carried the rows.

Python 3.6 compatible; standard library plus the vendored driver.
"""

import datetime
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
import zlib
from typing import Any, Dict, List, Tuple

from tests import backends
from tests import mariadb_v7_fixture as v7
from tests.test_migrations import build_at
from testboard import dbconfig, model
from testboard.storage import MIGRATIONS, Storage
from tools import export_for_mariadb as exporter
from tools import migrate_to_mariadb as migrate
from tools import upgrade_mariadb_schema as upgrade

SIZES = v7.Sizes(64, 255, 255)

#: The database this file upgrades, kept entirely separate from
#: tests/backends.py's own (``testboard_test`` normally, or whatever
#: TESTBOARD_TEST_DB_CNF names) so the two never race or collide.
_SUFFIX = "_schema_upgrade_test"


class TargetVersionPinTest(unittest.TestCase):
    """Needs no server: fails loudly the day migration 11 ships without
    this tool being extended to match. See the module docstring of
    tools/upgrade_mariadb_schema.py."""

    def test_target_matches_the_latest_migration(self) -> None:
        self.assertEqual(
            upgrade.TARGET_VERSION, MIGRATIONS[-1][0],
            "storage.MIGRATIONS has grown past what "
            "tools/upgrade_mariadb_schema.py knows how to reach. Add a "
            "new step_N_to_M() mirroring the new SQLite migration, "
            "extend upgrade.plan()/EXPECTED_FROM_VERSIONS/_MARKERS, and "
            "raise TARGET_VERSION to match before this can pass again.")


if backends.MARIADB_AVAILABLE:

    def _derived_database_name() -> str:
        return backends.settings().database + _SUFFIX

    def _derived_settings() -> dbconfig.Settings:
        return backends.settings()._replace(database=_derived_database_name())

    def _admin_connect() -> Any:
        """A server-level connection (no ``database=``), for
        CREATE/DROP DATABASE — the same shape as
        ``tests.backends._admin_conn``, kept separate because that one
        is scoped to backends' own database name."""
        from third_party import pymysql
        cfg = backends.settings()
        kwargs = {
            "user": cfg.user, "password": cfg.password,
            "charset": "utf8mb4", "autocommit": True,
        }  # type: Dict[str, Any]
        if cfg.unix_socket:
            kwargs["unix_socket"] = cfg.unix_socket
        else:
            kwargs["host"] = cfg.host
            kwargs["port"] = cfg.port
        return pymysql.connect(**kwargs)

    def _recreate_database() -> None:
        name = _derived_database_name()
        conn = _admin_connect()
        try:
            cur = conn.cursor()
            cur.execute("DROP DATABASE IF EXISTS `{0}`".format(name))
            cur.execute(
                "CREATE DATABASE `{0}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_nopad_bin".format(name))
            cur.close()
        finally:
            conn.close()

    def _connect_derived() -> Any:
        """A raw pymysql connection into the derived database, for
        seeding data with parameterized INSERTs (schema/DDL statements
        go through migrate.connect()'s Database wrapper instead — see
        the callers)."""
        from third_party import pymysql
        cfg = _derived_settings()
        kwargs = {
            "user": cfg.user, "password": cfg.password,
            "database": cfg.database, "charset": "utf8mb4",
            "autocommit": True,
        }  # type: Dict[str, Any]
        if cfg.unix_socket:
            kwargs["unix_socket"] = cfg.unix_socket
        else:
            kwargs["host"] = cfg.host
            kwargs["port"] = cfg.port
        return pymysql.connect(**kwargs)

    START = datetime.datetime(2026, 7, 20, 1, 0, 0)

    #: The seed dataset, defined once as plain Python values and loaded
    #: into BOTH the SQLite v7 fixture and the MariaDB v7 fixture from
    #: the same source, so the two cannot silently diverge.
    def _seed_rows() -> Dict[str, List[Tuple[Any, ...]]]:
        iso = model.format_iso
        runs = [
            (1, "linux-sim", "suite/a.py", "test_one", "PASS",
             iso(START), iso(START + datetime.timedelta(seconds=2)),
             "", None, None),
            (2, "linux-sim", "suite/a.py", "test_two", "FAIL",
             iso(START + datetime.timedelta(seconds=5)),
             iso(START + datetime.timedelta(seconds=8)),
             "http://x/y", "flaky", "abc123"),
            (3, "win-sim", "suite/b.py", "test_three", "PASS",
             iso(START + datetime.timedelta(hours=1)),
             iso(START + datetime.timedelta(hours=1, seconds=1)),
             "", None, None),
            (4, "win-sim", "suite/b.py", "test_four", "FAILED_AS_EXPECTED",
             iso(START + datetime.timedelta(hours=1, seconds=2)),
             iso(START + datetime.timedelta(hours=1, seconds=3)),
             "", "known", None),
        ]
        outputs = [(rid, zlib.compress("log for run {0}".format(rid)
                                       .encode("utf-8")))
                   for rid, _e, _s, _t, _r, _st, _en, _l, _k, _f in runs]
        latest = [
            (row[1], row[2], row[3], row[0], row[5], row[4], None,
             (datetime.datetime.strptime(row[6], "%Y-%m-%dT%H:%M:%S.%f")
              - datetime.datetime.strptime(
                  row[5], "%Y-%m-%dT%H:%M:%S.%f")).total_seconds())
            for row in runs
        ]
        users = [
            ("alice", iso(START - datetime.timedelta(days=1)), None, None),
            ("bob", iso(START - datetime.timedelta(days=1)),
             iso(START), "alice"),
        ]
        comments = [
            ("linux-sim", "suite/a.py", "test_one", "alice",
             iso(START + datetime.timedelta(minutes=1)), "looks fine"),
        ]
        assignments = [
            ("linux-sim", "suite/a.py", "test_two", "alice", "bob",
             iso(START + datetime.timedelta(minutes=2))),
        ]
        current_assignments = [
            ("linux-sim", "suite/a.py", "test_two", "alice"),
        ]
        expectations = [
            ("linux-sim", 2, iso(START), "alice"),
            ("win-sim", 2, iso(START), "alice"),
        ]
        activity_hours = [
            ("linux-sim", "2026-07-20T01", "PASS", 1),
            ("linux-sim", "2026-07-20T01", "FAIL", 1),
            ("win-sim", "2026-07-20T02", "PASS", 1),
            ("win-sim", "2026-07-20T02", "FAILED_AS_EXPECTED", 1),
        ]
        script_hours = [
            ("linux-sim", "2026-07-20T01", "suite/a.py", "PASS", 1,
             iso(START), iso(START + datetime.timedelta(seconds=2))),
            ("linux-sim", "2026-07-20T01", "suite/a.py", "FAIL", 1,
             iso(START + datetime.timedelta(seconds=5)),
             iso(START + datetime.timedelta(seconds=8))),
            ("win-sim", "2026-07-20T02", "suite/b.py", "PASS", 1,
             iso(START + datetime.timedelta(hours=1)),
             iso(START + datetime.timedelta(hours=1, seconds=1))),
            ("win-sim", "2026-07-20T02", "suite/b.py",
             "FAILED_AS_EXPECTED", 1,
             iso(START + datetime.timedelta(hours=1, seconds=2)),
             iso(START + datetime.timedelta(hours=1, seconds=3))),
        ]
        return {
            "runs": runs, "run_outputs": outputs, "latest_runs": latest,
            "users": users, "comments": comments,
            "assignments": assignments,
            "current_assignments": current_assignments,
            "environment_expectations": expectations,
            "activity_hours": activity_hours, "script_hours": script_hours,
        }

    def _build_v7_sqlite(path: str) -> Dict[str, List[Tuple[Any, ...]]]:
        """Migrations 1..7 via the real apply_migration_statement, then
        the seed rows via plain SQL matching that exact schema."""
        build_at(path, 7)
        rows = _seed_rows()
        conn = sqlite3.connect(path)
        try:
            conn.executemany(
                "INSERT INTO runs (id, environment, script, test_name, "
                "result, start_time, end_time, source_link, "
                "known_failure_reason, output_fingerprint) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows["runs"])
            conn.executemany(
                "INSERT INTO run_outputs (run_id, output) VALUES (?, ?)",
                rows["run_outputs"])
            conn.executemany(
                "INSERT INTO latest_runs (environment, script, "
                "test_name, run_id, start_time, result, prev_result, "
                "duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows["latest_runs"])
            conn.executemany(
                "INSERT INTO users (username, created_at, "
                "deactivated_at, deactivated_by) VALUES (?, ?, ?, ?)",
                rows["users"])
            conn.executemany(
                "INSERT INTO comments (environment, script, test_name, "
                "author, created_at, text) VALUES (?, ?, ?, ?, ?, ?)",
                rows["comments"])
            conn.executemany(
                "INSERT INTO assignments (environment, script, "
                "test_name, assignee, assigned_by, assigned_at) VALUES "
                "(?, ?, ?, ?, ?, ?)", rows["assignments"])
            conn.executemany(
                "INSERT INTO current_assignments (environment, script, "
                "test_name, assignee) VALUES (?, ?, ?, ?)",
                rows["current_assignments"])
            conn.executemany(
                "INSERT INTO environment_expectations (environment, "
                "expected_tests, updated_at, updated_by) VALUES "
                "(?, ?, ?, ?)", rows["environment_expectations"])
            conn.executemany(
                "INSERT INTO activity_hours (environment, hour, "
                "result, count) VALUES (?, ?, ?, ?)",
                rows["activity_hours"])
            conn.executemany(
                "INSERT INTO script_hours (environment, hour, script, "
                "result, count, first_start, last_end) VALUES "
                "(?, ?, ?, ?, ?, ?, ?)", rows["script_hours"])
            conn.commit()
        finally:
            conn.close()
        return rows

    def _load_v7_mariadb(rows: Dict[str, List[Tuple[Any, ...]]]) -> None:
        """Real DDL (tests/mariadb_v7_fixture.py) + parameterized
        INSERTs of the SAME rows the SQLite fixture holds."""
        _recreate_database()
        settings = _derived_settings()
        db = migrate.connect(settings)
        try:
            for statement in migrate.split_statements(v7.ddl(SIZES)):
                migrate.execute(db, statement)
            for statement in migrate.split_statements(v7.INDEXES):
                migrate.execute(db, statement)
            migrate.execute(db, "INSERT INTO schema_version (version) "
                                "VALUES (7)")
        finally:
            db.close()

        conn = _connect_derived()
        try:
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO runs (id, environment, script, test_name, "
                "result, start_time, end_time, source_link, "
                "known_failure_reason, output_fingerprint) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows["runs"])
            cur.executemany(
                "INSERT INTO run_outputs (run_id, output) VALUES "
                "(%s, %s)", rows["run_outputs"])
            cur.executemany(
                "INSERT INTO latest_runs (environment, script, "
                "test_name, run_id, start_time, result, prev_result, "
                "duration_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                "%s)", rows["latest_runs"])
            cur.executemany(
                "INSERT INTO users (username, created_at, "
                "deactivated_at, deactivated_by) VALUES "
                "(%s, %s, %s, %s)", rows["users"])
            cur.executemany(
                "INSERT INTO comments (environment, script, test_name, "
                "author, created_at, text) VALUES "
                "(%s, %s, %s, %s, %s, %s)", rows["comments"])
            cur.executemany(
                "INSERT INTO assignments (environment, script, "
                "test_name, assignee, assigned_by, assigned_at) VALUES "
                "(%s, %s, %s, %s, %s, %s)", rows["assignments"])
            cur.executemany(
                "INSERT INTO current_assignments (environment, script, "
                "test_name, assignee) VALUES (%s, %s, %s, %s)",
                rows["current_assignments"])
            cur.executemany(
                "INSERT INTO environment_expectations (environment, "
                "expected_tests, updated_at, updated_by) VALUES "
                "(%s, %s, %s, %s)", rows["environment_expectations"])
            cur.executemany(
                "INSERT INTO activity_hours (environment, hour, "
                "result, count) VALUES (%s, %s, %s, %s)",
                rows["activity_hours"])
            cur.executemany(
                "INSERT INTO script_hours (environment, hour, script, "
                "result, count, first_start, last_end) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s)", rows["script_hours"])
            cur.close()
        finally:
            conn.close()

    def _table_counts(settings: dbconfig.Settings) -> Dict[str, int]:
        db = migrate.connect(settings)
        try:
            out = {}  # type: Dict[str, int]
            for table in v7.TABLE_ORDER:
                out[table] = int(
                    migrate.query(
                        db, "SELECT COUNT(*) FROM {0}".format(table)
                    )[0][0])
            return out
        finally:
            db.close()

    class UpgradeTestBase(unittest.TestCase):
        """A v7 MariaDB fixture, freshly built, per test."""

        def setUp(self) -> None:
            self.tmp = tempfile.mkdtemp(prefix="testboard_v7_")
            self.addCleanup(shutil.rmtree, self.tmp, True)
            self.sqlite_path = os.path.join(self.tmp, "v7.db")
            self.rows = _build_v7_sqlite(self.sqlite_path)
            _load_v7_mariadb(self.rows)
            self.settings = _derived_settings()

        def _cnf_path(self) -> str:
            """A real option file on disk — cmd_upgrade/cmd_verify read
            credentials through testboard.dbconfig exactly like a real
            operator run, not injected as an object."""
            path = os.path.join(self.tmp, "upgrade.cnf")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("[client]\n")
                if self.settings.unix_socket:
                    handle.write(
                        "socket = {0}\n".format(self.settings.unix_socket))
                else:
                    handle.write("host = {0}\n".format(self.settings.host))
                    handle.write("port = {0}\n".format(self.settings.port))
                handle.write("user = {0}\n".format(self.settings.user))
                handle.write(
                    "password = {0}\n".format(self.settings.password))
                handle.write(
                    "database = {0}\n".format(self.settings.database))
            return path

    class FullUpgradeTest(UpgradeTestBase):
        """The whole 7 -> 10 path, then a functional smoke test."""

        def test_dry_run_changes_nothing(self) -> None:
            args = _ns(config=self._cnf_path(), dry_run=True)
            code = upgrade.cmd_upgrade(args)
            self.assertEqual(code, 0)
            db = migrate.connect(self.settings)
            try:
                self.assertEqual(upgrade.current_version(db), 7)
                self.assertFalse(upgrade._table_exists(db, "streams"))
                self.assertFalse(
                    upgrade._table_exists(db, "environment_products"))
            finally:
                db.close()

        def test_upgrade_reaches_v10_and_verifies_clean(self) -> None:
            before = _table_counts(self.settings)
            args = _ns(config=self._cnf_path(), dry_run=False)
            code = upgrade.cmd_upgrade(args)
            self.assertEqual(code, 0)

            db = migrate.connect(self.settings)
            try:
                self.assertEqual(upgrade.current_version(db), 10)
                self.assertEqual(upgrade.consistency_check(db, 10), [])
            finally:
                db.close()

            # Every pre-existing row must survive a schema-only change.
            after = _table_counts(self.settings)
            for table in v7.TABLE_ORDER:
                self.assertEqual(
                    after[table], before[table],
                    "row count changed for {0}: {1} -> {2}".format(
                        table, before[table], after[table]))
            # Migration 9 adds exactly one new row: the seeded mainline
            # stream. Not in v7.TABLE_ORDER (streams did not exist yet).
            db = migrate.connect(self.settings)
            try:
                streams = migrate.query(db, "SELECT COUNT(*) FROM streams")
                self.assertEqual(int(streams[0][0]), 1)
                mainline = migrate.query(
                    db, "SELECT product, kind, name FROM streams "
                        "WHERE id = 1")
                self.assertEqual(mainline[0], ("", "mainline", ""))
            finally:
                db.close()

        def test_verify_standalone_after_upgrade(self) -> None:
            self.assertEqual(
                upgrade.cmd_upgrade(_ns(config=self._cnf_path(),
                                        dry_run=False)), 0)
            self.assertEqual(
                upgrade.cmd_verify(_ns(config=self._cnf_path())), 0)

        def test_functional_smoke_through_storage(self) -> None:
            """Schema identity (proved above) is not the same claim as
            "the app actually works against it" — a wrong DEFAULT or a
            collation slip could pass a text diff and still break a
            write. This is a targeted smoke test through the real
            Storage class, not a second full run of the ~2,900-case
            dual-backend suite: that suite already runs, in CI, against
            a schema this SAME exporter DDL produces (tests/backends.py
            uses the identical tools.export_for_mariadb.ddl()), and
            test_upgrade_reaches_v10_and_verifies_clean above proves
            the upgraded schema is structurally identical to that. This
            test's job is the part that proof does not cover: real
            writes through real Storage code, on the actual upgraded
            database, exercising the streams machinery migrations 9/10
            introduced.
            """
            self.assertEqual(
                upgrade.cmd_upgrade(_ns(config=self._cnf_path(),
                                        dry_run=False)), 0)
            store = Storage.mariadb(self.settings)
            self.addCleanup(store.close)

            from testboard.model import Result, RunRecord
            now = START + datetime.timedelta(days=1)
            store.upsert_runs([
                RunRecord(
                    environment="linux-sim", script="suite/a.py",
                    test_name="test_one", result=Result.PASS,
                    start_time=now,
                    end_time=now + datetime.timedelta(seconds=1),
                    output="ok", source_link="",
                    known_failure_reason=None, build="rc1"),
            ])
            streams = store.list_streams("")
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0].name, "rc1")

            store.ensure_user("carol", now)
            store.add_comment("linux-sim", "suite/a.py", "test_one",
                              "carol", "seen on upgraded schema", now)
            store.set_assignee("linux-sim", "suite/a.py", "test_one",
                               "carol", "carol", now)

            summary = store.environments()
            self.assertIn("linux-sim", summary)
            self.assertIn("win-sim", summary)

    class RefusalTest(UpgradeTestBase):
        """Every way a live run must stop rather than guess."""

        def test_refuses_v6(self) -> None:
            db = migrate.connect(self.settings)
            try:
                migrate.execute(
                    db, "UPDATE schema_version SET version = 6")
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("only resumes from", output)

        def test_refuses_already_v10(self) -> None:
            # Reuse the SAME exporter DDL tests/backends.py uses: a
            # fresh v10 schema is the state this refusal must recognise.
            _recreate_database()
            db = migrate.connect(self.settings)
            try:
                for statement in migrate.split_statements(
                        exporter.ddl(SIZES)):
                    migrate.execute(db, statement)
                for statement in migrate.split_statements(
                        exporter.INDEXES):
                    migrate.execute(db, statement)
                migrate.execute(
                    db, "INSERT INTO schema_version (version) "
                        "VALUES (10)")
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("already at schema version 10", output)

        def test_refuses_half_upgraded_state(self) -> None:
            """DDL autocommits and the version bump is the LAST
            statement of a step — so a run interrupted mid-step leaves
            artifacts of N+1 with schema_version still saying N. Here:
            `streams` created (the first statement of step 8->9) but
            schema_version never bumped past 7. Re-running must refuse,
            not attempt `CREATE TABLE streams` a second time."""
            db = migrate.connect(self.settings)
            try:
                migrate.execute(
                    db, upgrade.step_8_to_9("2026-07-20T00:00:00.000000")[0])
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("mysqldump", output)
            self.assertIn("streams table", output)

    def _ns(**kwargs: Any) -> Any:
        class _NS(object):
            pass
        ns = _NS()
        for key, value in kwargs.items():
            setattr(ns, key, value)
        return ns

    def _run_capturing(func: Any, args: Any) -> Tuple[int, str]:
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = func(args)
        return code, buf.getvalue()


if __name__ == "__main__":
    unittest.main()
