"""tools/upgrade_mariadb_schema.py, against a real MariaDB server.

**Why this file exists.** Production is a live MariaDB database with
real data, and this tool is what moves it to each newer schema version
without a full SQLite export/load — the app itself refuses to serve a
version mismatch in either direction (``testboard/mariadb.py``), so
without the tool's step for a migration, the code that carries that
migration simply does not start against prod. ``LedgerTest`` (server-
free, runs on every suite run) is what makes a missing step fail
before that: see its docstring.

**Gated exactly like ``tests/backends.py``** — module-level, not a
per-test skip: with ``TESTBOARD_TEST_DB_CNF`` unset this file defines
NOTHING beyond ``LedgerTest`` (which needs no server), so the collected
count does not move and no skip noise appears. Point it at your OWN
sacrificial database, not the shared one ``tests/backends.py`` uses —
this file builds a schema from scratch (v7, then upgrades it through
EVERY step of the ledger) and would otherwise race or collide with
every other dual-backend test class sharing that database.

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


class LedgerTest(unittest.TestCase):
    """Needs no server: the MariaDB half of every migration exists.

    WP-40 (2026-10-01). Before this, a single pin compared the tool's
    hard-coded target with ``MIGRATIONS[-1][0]`` and failed with
    "extend the tool" — true, but it named no shape to extend into,
    and the tool's own docstring called itself a one-off. Now the
    tool is a ledger (``upgrade.LEDGER``) and ``upgrade.ledger_gaps``
    is the whole rule: every SQLite migration above the cutover has a
    step, the steps are contiguous, each declares what it creates, and
    the declaration matches its own DDL. A migration written for
    SQLite without its MariaDB step fails HERE, on the developer's
    machine, on the same commit — not at deployment, when
    ``testboard/mariadb.py`` refuses to start against the version gap.
    """

    def _versions(self) -> List[int]:
        return [version for version, _ in MIGRATIONS]

    def test_the_ledger_matches_the_sqlite_migrations(self) -> None:
        self.assertEqual(upgrade.ledger_gaps(self._versions()), [])

    def test_target_is_derived_and_matches_the_latest_migration(
            self) -> None:
        self.assertEqual(upgrade.TARGET_VERSION, MIGRATIONS[-1][0])
        self.assertEqual(
            upgrade.EXPECTED_FROM_VERSIONS,
            tuple(range(upgrade.CUTOVER_VERSION, upgrade.TARGET_VERSION)))

    def test_plan_ends_every_step_with_its_version_bump(self) -> None:
        """The consistency check's whole premise: the bump is the LAST
        statement of a step, appended by plan(), never by a step."""
        steps = upgrade.plan(exporter.Sizes(64, 255, 255),
                             "2026-01-01T00:00:00.000000")
        self.assertEqual([v for v, _ in steps],
                         list(upgrade.EXPECTED_FROM_VERSIONS))
        for from_version, statements in steps:
            self.assertEqual(
                statements[-1],
                "UPDATE schema_version SET version = {0}".format(
                    from_version + 1))
            self.assertEqual(
                [s for s in statements[:-1]
                 if s.lower().startswith("update schema_version")], [])

    def test_whether_the_server_must_stop_is_read_from_the_ledger(
            self) -> None:
        """Runbook §G.3: the feeders are never stopped (a dozen servers;
        their contract defers a push that meets a stopped server), and
        the SERVER stops only for a step that rewrites an existing
        table. The dry run's advice comes from the steps' own
        declarations, so a creates-only step says "may keep running"
        and the streams steps say "stop it first", naming the tables."""
        self.assertEqual(upgrade.rewritten_tables([]), [])
        creates_only = upgrade.Step(
            from_version=upgrade.TARGET_VERSION, package="planted",
            summary="planted",
            statements=lambda sizes, now: ["CREATE TABLE planted (x INT)"],
            probes=(upgrade.Probe("planted table", "planted", None),),
            alters=())
        self.assertEqual(upgrade.rewritten_tables([creates_only]), [])
        streams = [s for s in upgrade.LEDGER if s.from_version == 8][0]
        self.assertIn((9, "latest_runs"),
                      upgrade.rewritten_tables([streams]))
        self.assertIn((9, "runs"), upgrade.rewritten_tables([streams]))

    def test_row_count_tables_are_the_altered_ones_runs_first(
            self) -> None:
        self.assertEqual(upgrade._ROW_COUNT_TABLES[0], "runs")
        altered = {t for step in upgrade.LEDGER for t in step.alters}
        self.assertEqual(set(upgrade._ROW_COUNT_TABLES), altered | {"runs"})

    # -- planted regressions: the guard can actually fail ----------------

    def test_a_migration_without_a_step_is_named(self) -> None:
        missing = upgrade.TARGET_VERSION + 1
        problems = upgrade.ledger_gaps(self._versions() + [missing])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("migration {0}".format(missing), problems[0])
        self.assertIn("no step from {0} to {1}".format(
            missing - 1, missing), problems[0])

    def test_a_step_without_a_migration_is_named(self) -> None:
        extra = upgrade.Step(
            from_version=upgrade.TARGET_VERSION, package="planted",
            summary="planted",
            statements=lambda sizes, now: ["CREATE TABLE planted (x INT)"],
            probes=(upgrade.Probe("planted table", "planted", None),),
            alters=())
        problems = upgrade.ledger_gaps(
            self._versions(), ledger=upgrade.LEDGER + (extra,))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no migration {0}".format(
            upgrade.TARGET_VERSION + 1), problems[0])

    def test_a_gap_in_the_steps_is_named(self) -> None:
        problems = upgrade.ledger_gaps(
            self._versions(), ledger=upgrade.LEDGER[1:])
        self.assertTrue(
            any("no step from 7 to 8" in p for p in problems), problems)
        self.assertTrue(
            any("not contiguous" in p for p in problems), problems)

    def test_a_probe_the_ddl_does_not_mention_is_named(self) -> None:
        last = upgrade.LEDGER[-1]
        broken = last._replace(probes=last.probes + (
            upgrade.Probe("phantom.column column", "phantom", "column"),))
        problems = upgrade.ledger_gaps(
            self._versions(), ledger=upgrade.LEDGER[:-1] + (broken,))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("never mention table phantom", problems[0])

    def test_a_step_with_no_probe_is_named(self) -> None:
        last = upgrade.LEDGER[-1]
        problems = upgrade.ledger_gaps(
            self._versions(),
            ledger=upgrade.LEDGER[:-1] + (last._replace(probes=()),))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("declares no probe", problems[0])

    def test_a_step_that_bumps_the_version_itself_is_named(self) -> None:
        last = upgrade.LEDGER[-1]
        original = last.statements
        broken = last._replace(statements=lambda sizes, now: (
            original(sizes, now)
            + ["UPDATE schema_version SET version = 99"]))
        problems = upgrade.ledger_gaps(
            self._versions(), ledger=upgrade.LEDGER[:-1] + (broken,))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("bumps schema_version itself", problems[0])

    def test_the_tool_refuses_to_run_a_ledger_with_gaps(self) -> None:
        """cmd_upgrade/cmd_verify self-check before connecting, so a
        checkout nobody ran the suite on still cannot half-upgrade."""
        import contextlib
        original = upgrade.LEDGER
        upgrade.LEDGER = original[:-1]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = upgrade.cmd_upgrade(_args(config="unused.cnf"))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("ledger does not match", buf.getvalue())
            self.assertIn("no step from {0} to {1}".format(
                upgrade.TARGET_VERSION - 1, upgrade.TARGET_VERSION),
                buf.getvalue())
        finally:
            upgrade.LEDGER = original


def _args(**kwargs: Any) -> Any:
    class _NS(object):
        pass
    ns = _NS()
    ns.dry_run = False
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


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
        """The whole path from the cutover version to the latest, then
        a functional smoke test."""

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

        def test_upgrade_reaches_the_latest_version_and_verifies_clean(
                self) -> None:
            before = _table_counts(self.settings)
            args = _ns(config=self._cnf_path(), dry_run=False)
            code = upgrade.cmd_upgrade(args)
            self.assertEqual(code, 0)

            db = migrate.connect(self.settings)
            try:
                self.assertEqual(upgrade.current_version(db),
                                 MIGRATIONS[-1][0])
                self.assertEqual(
                    upgrade.consistency_check(db, MIGRATIONS[-1][0]), [])
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
            test_upgrade_reaches_the_latest_version_and_verifies_clean
            above proves
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

        def test_refuses_a_version_below_the_cutover(self) -> None:
            db = migrate.connect(self.settings)
            try:
                migrate.execute(
                    db, "UPDATE schema_version SET version = {0}".format(
                        upgrade.CUTOVER_VERSION - 1))
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("only resumes from", output)

        def test_refuses_a_version_above_the_target(self) -> None:
            """Newer code wrote this database; the answer is to deploy
            that code, and the refusal says so."""
            db = migrate.connect(self.settings)
            try:
                migrate.execute(
                    db, "UPDATE schema_version SET version = {0}".format(
                        upgrade.TARGET_VERSION + 1))
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("NEWER code", output)

        def test_refuses_already_at_the_target(self) -> None:
            # Reuse the SAME exporter DDL tests/backends.py uses: a
            # fresh schema at the latest version is the state this
            # refusal must recognise.
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
                        "VALUES ({0})".format(upgrade.TARGET_VERSION))
            finally:
                db.close()
            code, output = _run_capturing(
                upgrade.cmd_upgrade,
                _ns(config=self._cnf_path(), dry_run=False))
            self.assertEqual(code, upgrade.EXIT_GATE_FAILED)
            self.assertIn("already at schema version {0}".format(
                upgrade.TARGET_VERSION), output)

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

    class InstantAddColumnTest(unittest.TestCase):
        """Does THIS server accept ALGORITHM=INSTANT for the runs.stream_id
        ADD COLUMN -- the single claim that decides whether tomorrow's
        runs step (~4.4M rows in production) is ~0 seconds or a table
        rebuild with a downtime window sized for it.

        Deliberately NOT a timing assertion. On a fixture-sized table (a
        handful of rows, same as every other test in this file) INSTANT
        and a full COPY rebuild both finish in milliseconds -- a clock
        cannot tell them apart at this scale, which is exactly the gap
        this test closes. Forcing ALGORITHM=INSTANT explicitly asks the
        SERVER to declare its own capability: it either accepts the
        statement (instant IS available for this exact ADD COLUMN, on
        this exact server version, full stop) or refuses it with an
        error naming the reason (instant is NOT available, whatever the
        row count). That is a statement about the server, not about how
        long anything took here.

        Session-local MariaDB proved this on 12.3.2 (this package's own
        commit history: an explicit ALGORITHM=INSTANT force-succeeded at
        500,000 synthetic rows). This test is what makes the SAME claim
        true of whatever server actually runs it -- in particular CI's
        `python36-mariadb`/`python36-mariadb-upgraded` legs, which run
        against `mariadb:10.3`, production's actual stream. A failure
        here on that leg is not a test bug to work around; it is the
        finding the whole probe existed to surface, and the drop note's
        timing expectation and the operator's downtime window both need
        to change if it fires.
        """

        def setUp(self) -> None:
            self.tmp = tempfile.mkdtemp(prefix="testboard_instant_")
            self.addCleanup(shutil.rmtree, self.tmp, True)
            _recreate_database()
            self.settings = _derived_settings()
            db = migrate.connect(self.settings)
            try:
                for statement in migrate.split_statements(v7.ddl(SIZES)):
                    migrate.execute(db, statement)
                # A handful of rows, not many: the point is the server's
                # capability, not a timing comparison at scale (that
                # comparison is exactly what this test replaces with a
                # real yes/no from the server).
                conn = _connect_derived()
                try:
                    cur = conn.cursor()
                    cur.executemany(
                        "INSERT INTO runs (environment, script, "
                        "test_name, result, start_time, end_time, "
                        "source_link, known_failure_reason, "
                        "output_fingerprint) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [("linux-sim", "suite/a.py",
                          "test_{0}".format(n), "PASS",
                          model.format_iso(
                              START + datetime.timedelta(seconds=n)),
                          model.format_iso(
                              START + datetime.timedelta(seconds=n + 1)),
                          "", None, None)
                         for n in range(20)])
                    cur.close()
                finally:
                    conn.close()
            finally:
                db.close()

        def test_algorithm_instant_is_accepted_for_the_runs_add_column(
                self) -> None:
            # The EXACT statement tools/upgrade_mariadb_schema.py's
            # step_8_to_9() emits for runs -- found by the same
            # predicate the tool itself uses to recognise it
            # (upgrade._touches_runs), not retyped by hand, so this
            # test cannot silently drift from what a live upgrade
            # actually runs.
            now_iso = "2026-08-11T00:00:00.000000"
            runs_statement = None
            for statement in upgrade.step_8_to_9(now_iso):
                if upgrade._touches_runs(statement):
                    runs_statement = statement
                    break
            self.assertIsNotNone(
                runs_statement,
                "step_8_to_9() no longer emits a statement "
                "upgrade._touches_runs recognises -- this test cannot "
                "find what to probe; fix the mismatch before trusting "
                "either side")

            forced = runs_statement + ", ALGORITHM=INSTANT"
            db = migrate.connect(self.settings)
            try:
                try:
                    migrate.execute(db, forced)
                except migrate.DatabaseError as exc:
                    self.fail(
                        "CRITICAL FINDING: this MariaDB server REFUSED "
                        "ALGORITHM=INSTANT for the runs.stream_id ADD "
                        "COLUMN -- {0!r}. This means the runs step of "
                        "tools/upgrade_mariadb_schema.py's upgrade (~4.4M "
                        "rows in production) will NOT complete in "
                        "roughly zero seconds as this tool's DEV timing "
                        "on a newer local server suggested. It will fall "
                        "back to a table rebuild, and per "
                        "docs/MARIADB_MIGRATION.md sections 0/E.1 "
                        "('tens of minutes at best' for a full load of a "
                        "table this size), the drop note's timing "
                        "expectation and the operator's downtime window "
                        "both need to change before this migration runs "
                        "against production. Do not soften or retry "
                        "this finding away.".format(exc))
            finally:
                db.close()

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
