"""The storage and API suites, run a second time against real MariaDB.

This module defines tests ONLY when ``TESTBOARD_TEST_DB_CNF`` points at
a sacrificial MariaDB database (see ``tests/backends.py``): with the
variable unset — every ordinary local run — importing it defines
nothing, the collected count does not move, and no skip noise appears.
With it set (a local server, or CI's mariadb:10.3 service), every
concrete test class in ``tests/test_storage.py`` and
``tests/test_api.py`` is subclassed with the ``_make_storage`` hook
overridden, so the SAME assertions run against the schema the
migration tooling creates.

The exclusion table is the pressure valve for tests that are ABOUT
SQLite rather than about storage semantics. Every entry carries its
reason; an entry without one is a bug in this file.

At the bottom: integration tests for exactly the behaviours a fake
driver cannot prove (tests/test_mariadb_unit.py proves the decisions;
these prove a real server accepts them).

Python 3.6 compatible; standard library plus the vendored driver.
"""

import time
import unittest
from typing import Any, Dict, List, Optional, Tuple, Type

from tests import backends

#: Classes whose subject IS SQLite, with the reason each stays behind.
EXCLUDED_CLASSES = {
    "TestMigrations":
        "migrations are SQLite-owned by design; MariaDB's schema is "
        "created by the migration tooling and only verified "
        "(runbook section F). The version-refusal behaviour has its "
        "own MariaDB tests.",
    "TestSchemaVersionGuard":
        "re-opens Storage(self.db_path) to prove the SQLite refusal; "
        "the MariaDB refusals are covered in SchemaGuardMariaDBTest "
        "below and in the unit tests.",
    "TestSortIndexesAreUsed":
        "asserts EXPLAIN QUERY PLAN output — SQLite's planner, "
        "SQLite's syntax. MariaDB index use is a different question "
        "for a different tool.",
    "ComparePairsQueryPlanTest":
        "asserts EXPLAIN QUERY PLAN output (WP-23 perf pass, the "
        "compare pairs query) — SQLite's planner, SQLite's syntax, "
        "same reason as TestSortIndexesAreUsed above. MariaDB's own "
        "EXPLAIN was checked by hand against the local mariadbd "
        "(eq_ref on the latest_runs PRIMARY KEY) — see the commit "
        "message; there is no equivalent automated pin for it here.",
    "TestEnvironmentListingCost":
        "counts sqlite page reads to pin a query-shape regression; "
        "the instrument is engine-specific even though the shape "
        "under test is shared.",
    "TestLatestRunDuration":
        "constructs a second Storage over the same SQLite file to "
        "prove persistence across open/close; there is no equivalent "
        "'file' to re-open here and the duration logic itself runs in "
        "every other generated class.",
}  # type: Dict[str, str]

#: Individual tests whose INSTRUMENT is SQLite even though their class
#: is not — skipped with the reason, so a green MariaDB run cannot be
#: read as having exercised them.
EXCLUDED_TESTS = {
    "TestPreviousResult.test_start_time_index_created":
        "asserts PRAGMA index_list output; MariaDB indexes are created "
        "by the migration DDL and asserted by the schema bootstrap.",
    "TestRunOutputs.test_output_column_removed_from_runs":
        "asserts PRAGMA table_info output; the MariaDB schema comes "
        "from the exporter's DDL, which the export tests pin.",
    "TestRunOutputs.test_plain_text_output_still_reads":
        "legacy pre-compression rows exist only in the production "
        "SQLite file; a MariaDB database is born from the export, "
        "where every output arrived compressed.",
    "EnvironmentDeleteTest.test_the_table_list_covers_the_whole_schema":
        "reads sqlite_master to prove _ENVIRONMENT_TABLES is complete; "
        "the completeness holds per-schema, not per-engine.",
    "TestRecentResults.test_duplicate_triples_are_not_fetched_twice":
        "counts queries via sqlite3's set_trace_callback.",
    "TestRecentResults.test_no_triples_issues_no_query_at_all":
        "counts queries via sqlite3's set_trace_callback.",
    "TestRecentResults.test_the_query_count_is_bounded_by_chunks_not_rows":
        "counts queries via sqlite3's set_trace_callback.",
    "TestDurationRollup.test_the_rollup_reads_one_row_per_test":
        "counts queries via sqlite3's set_trace_callback.",
    "TestLatestRunTimeByEnvironment.test_it_never_touches_the_runs_table":
        "counts queries via sqlite3's set_trace_callback.",
    "TestWatch.test_query_count_does_not_grow_with_card_count":
        "counts queries via sqlite3's set_trace_callback, and reaches "
        "into self.storage._conn() directly to register it.",
    "TestWatchStreamCards.test_query_count_does_not_grow_with_s_card_count":
        "counts queries via sqlite3's set_trace_callback.",
    "CompareCountsManyTest."
    "test_query_count_does_not_grow_with_the_number_of_streams":
        "counts queries via sqlite3's set_trace_callback.",
    "StreamIdentitiesTest.test_query_count_is_one_regardless_of_id_count":
        "counts queries via sqlite3's set_trace_callback.",
    "CompareCountsManyBaselinesTest."
    "test_query_count_does_not_grow_with_a_baseline_override":
        "counts queries via sqlite3's set_trace_callback.",
    "PreviousBuildsTest.test_query_count_is_one_regardless_of_build_count":
        "counts queries via sqlite3's set_trace_callback.",
    "ScriptHoursTest.test_the_window_read_is_an_index_range_not_a_scan":
        "asserts the SQLite query plan; MariaDB's planner is a "
        "different instrument for a different day.",
    "TestLargeBatch.test_5000_runs_import_under_10_seconds":
        "a perf pin calibrated on SQLite. On an emulated local server "
        "or a CI container the number measures the environment, not a "
        "regression; MariaDB perf comes from the real box (drop note).",
}  # type: Dict[str, str]

if backends.MARIADB_AVAILABLE:

    from tests import test_api as _api_module
    from tests import test_storage as _storage_module

    def _mariadb_make_storage(self: Any) -> Any:
        return backends.mariadb_storage()

    def _skip_stub(reason: str) -> Any:
        def stub(self: Any) -> None:
            self.skipTest(reason)
        return stub

    def _generate(module: Any, base: type) -> List[Type[unittest.TestCase]]:
        """Subclass every concrete descendant of *base* in *module*."""
        generated = []  # type: List[Type[unittest.TestCase]]
        for name in dir(module):
            cls = getattr(module, name)
            if not (isinstance(cls, type) and issubclass(cls, base)):
                continue
            if cls is base or name in EXCLUDED_CLASSES:
                continue
            if not any(attr.startswith("test") for attr in dir(cls)):
                continue
            namespace = {"_make_storage": _mariadb_make_storage
                         }  # type: Dict[str, Any]
            for key, reason in EXCLUDED_TESTS.items():
                owner, _, test_name = key.partition(".")
                if owner == name:
                    namespace[test_name] = _skip_stub(reason)
            sub = type(name + "MariaDB", (cls,), namespace)
            sub.__module__ = __name__
            generated.append(sub)
        return generated

    for _cls in _generate(_storage_module, _storage_module.StorageTestBase):
        globals()[_cls.__name__] = _cls
    for _cls in _generate(_api_module, _api_module.ApiCase):
        globals()[_cls.__name__] = _cls

    class MariaDBIntegrationBase(unittest.TestCase):
        """A fresh MariaDB-backed Storage per test."""

        def setUp(self) -> None:
            self.store = backends.mariadb_storage()
            self.addCleanup(self.store.close)

        def raw(self, sql: str) -> List[Tuple[Any, ...]]:
            return list(self.store._conn().execute(sql).fetchall())

    class RunIdStabilityMariaDBTest(MariaDBIntegrationBase):
        """The section-B.5 contract, on the engine it was written for.

        runs.id must survive a re-import that changes the result: the
        feeder re-pushes its window every 10 minutes and
        run_outputs/latest_runs reference the id.
        """

        def _seed(self) -> Any:
            import datetime
            from testboard.model import Result, RunRecord
            start = datetime.datetime(2026, 7, 25, 2, 0, 0)
            return RunRecord(
                environment="linux sim", script="suite/a b.py",
                test_name="test_weird [1]\twith tab", result=Result.PASS,
                start_time=start,
                end_time=start + datetime.timedelta(seconds=3),
                output="line one\ncafé 🙂", source_link="",
                known_failure_reason=None, branch=None, build=None)

        def test_reimport_updates_in_place_and_keeps_the_id(self) -> None:
            from testboard.model import Result
            record = self._seed()
            counts = self.store.upsert_runs([record])
            self.assertEqual((counts.inserted, counts.updated), (1, 0))
            before = self.raw("SELECT id FROM runs")[0][0]
            changed = record._replace(result=Result.FAIL,
                                      output="now failing")
            counts = self.store.upsert_runs([changed])
            self.assertEqual((counts.inserted, counts.updated), (0, 1))
            after = self.raw("SELECT id FROM runs")[0][0]
            self.assertEqual(before, after, "REPLACE-style id churn")
            latest = self.raw("SELECT run_id, result FROM latest_runs")[0]
            self.assertEqual(latest[0], after)
            self.assertEqual(latest[1], "FAIL")

        def test_a_byte_identical_reimport_writes_nothing(self) -> None:
            record = self._seed()
            self.store.upsert_runs([record])
            counts = self.store.upsert_runs([record])
            self.assertEqual(counts.inserted, 0)
            self.assertEqual(counts.unchanged, 1)

    class FoundRowsMariaDBTest(MariaDBIntegrationBase):
        """The client_flag the fake driver could only record."""

        def test_redeclaring_an_identical_expectation_is_not_an_error(
                self) -> None:
            """UPDATE-with-identical-values reports 0 rows CHANGED but
            must report rows MATCHED, or the code INSERTs a duplicate
            key. This is the FOUND_ROWS flag doing its one job."""
            import datetime
            when = datetime.datetime(2026, 7, 25, 2, 0, 0)
            self.store.set_environment_expectation(
                "linux sim", 100, "alice", when)
            self.store.set_environment_expectation(
                "linux sim", 100, "alice", when)   # must not raise
            rows = self.raw("SELECT expected_tests "
                            "FROM environment_expectations")
            self.assertEqual(rows, [(100,)])

    class BlobMariaDBTest(MariaDBIntegrationBase):
        """binary_prefix, proven by bytes that are not valid utf8mb4."""

        def test_a_compressed_output_round_trips(self) -> None:
            import datetime
            from testboard.model import Result, RunRecord
            start = datetime.datetime(2026, 7, 25, 2, 0, 0)
            text = "output with\ttabs\nand café 🙂 and \x00-adjacent noise"
            self.store.upsert_runs([RunRecord(
                environment="e", script="s", test_name="t",
                result=Result.PASS, start_time=start,
                end_time=start + datetime.timedelta(seconds=1),
                output=text, source_link="", known_failure_reason=None,
                branch=None, build=None)])
            run_id = self.raw("SELECT id FROM runs")[0][0]
            stored = self.store.get_run(int(run_id))
            self.assertIsNotNone(stored)
            self.assertEqual(stored.output, text)

    class SearchParityMariaDBTest(MariaDBIntegrationBase):
        """The LIKE fragment: case folding and the escape chain."""

        def _seed_names(self, names: List[str]) -> None:
            import datetime
            from testboard.model import Result, RunRecord
            start = datetime.datetime(2026, 7, 25, 2, 0, 0)
            self.store.upsert_runs([
                RunRecord(environment="e", script="s", test_name=name,
                          result=Result.PASS, start_time=start,
                          end_time=start + datetime.timedelta(seconds=1),
                          output="", source_link="",
                          known_failure_reason=None, branch=None, build=None)
                for name in names])

        def names_for(self, q: str) -> List[str]:
            return sorted(row.test_name
                          for row in self.store.dashboard(q=q))

        def test_search_is_case_insensitive_like_sqlite(self) -> None:
            self._seed_names(["Login test", "logout", "unrelated"])
            self.assertEqual(self.names_for("LOGIN"), ["Login test"])
            self.assertEqual(self.names_for("log"),
                             ["Login test", "logout"])

        def test_wildcards_in_the_query_are_literal(self) -> None:
            """A % or _ typed into the search box is a character, not a
            wildcard — _escape_like's contract, now through MariaDB's
            escape parsing."""
            self._seed_names(["100% done", "under_score", "underXscore",
                              "back\\slash"])
            self.assertEqual(self.names_for("%"), ["100% done"])
            self.assertEqual(self.names_for("_"), ["under_score"])
            self.assertEqual(self.names_for("\\"), ["back\\slash"])

    class LimitOffsetMariaDBTest(MariaDBIntegrationBase):
        """The one composed-SQL fragment with no SQLite spelling."""

        def test_offset_without_limit_uses_the_mariadb_idiom(self) -> None:
            self._seed()
            rows = self.store.dashboard(limit=None, offset=2)
            self.assertEqual(len(rows), 3)

        def _seed(self) -> None:
            import datetime
            from testboard.model import Result, RunRecord
            start = datetime.datetime(2026, 7, 25, 2, 0, 0)
            self.store.upsert_runs([
                RunRecord(environment="e", script="s",
                          test_name="t{0}".format(index),
                          result=Result.PASS, start_time=start,
                          end_time=start + datetime.timedelta(seconds=1),
                          output="", source_link="",
                          known_failure_reason=None, branch=None, build=None)
                for index in range(5)])

    class SchemaGuardMariaDBTest(unittest.TestCase):
        """The version refusals, against the real server."""

        def test_an_older_schema_is_refused(self) -> None:
            from testboard.storage import MIGRATIONS, Storage
            backends.reset_database()
            backends._run("UPDATE schema_version SET version = {0}".format(
                MIGRATIONS[-1][0] - 1))
            with self.assertRaises(RuntimeError) as caught:
                Storage.mariadb(backends.settings())
            self.assertIn("never migrates", str(caught.exception))

        def test_a_newer_schema_is_refused(self) -> None:
            from testboard.storage import MIGRATIONS, Storage
            backends.reset_database()
            backends._run("UPDATE schema_version SET version = {0}".format(
                MIGRATIONS[-1][0] + 1))
            with self.assertRaises(RuntimeError) as caught:
                Storage.mariadb(backends.settings())
            self.assertIn("NEWER version", str(caught.exception))

    class ReconnectMariaDBTest(MariaDBIntegrationBase):
        """Ping-on-borrow against a genuinely dead connection."""

        def test_a_timed_out_connection_recovers_on_next_borrow(
                self) -> None:
            """The server kills the session; the next use after the
            idle threshold must reconnect instead of raising. This is
            the overnight wait_timeout story compressed to two
            seconds."""
            conn = self.store._conn()
            cursor = conn._raw.cursor()
            cursor.execute("SET SESSION wait_timeout = 1")
            cursor.close()
            time.sleep(2.0)
            # Fake the idle-threshold passage rather than sleeping 60s;
            # the DEADNESS is real, only the clock is compressed.
            conn._last_used -= 61.0
            environments = self.store.environments()
            self.assertEqual(environments, [])


if __name__ == "__main__":
    unittest.main()
