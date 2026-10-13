"""The performance harness measures what it claims to (tools/dev/perf/).

A runner that cannot see a planted slow path, a "cold" call that is
secretly warm, or a seed that differs between two builds would produce
numbers that look like evidence and are not. Each test here is one of
those failure modes, run on a small seed (``--scale 0.03``) in a temp
directory, never inside the checkout.
"""

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import typing
import unittest
from typing import Dict, List, Set, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERF_DIR = os.path.join(REPO_ROOT, "tools", "dev", "perf")


def _load(name: str) -> types.ModuleType:
    """Import a harness script by path: tools/dev is not a package."""
    spec = importlib.util.spec_from_file_location(
        "dev_perf_" + name, os.path.join(PERF_DIR, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


seed = _load("seed")
ab = _load("ab")

#: Appended to a COPY of storage.py: every Browse page now sleeps 25 ms.
_PLANT = '''

_planted_original = Storage.dashboard


def _planted_slow_dashboard(self, *args, **kwargs):
    time.sleep(0.025)
    return _planted_original(self, *args, **kwargs)


Storage.dashboard = _planted_slow_dashboard
'''

_SMALL = {"scale": 0.03, "nights": 3}


class _SeededCase(unittest.TestCase):
    """One small seed per class, in a temp directory outside the repo."""

    scratch = ""
    db = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.mkdtemp(prefix="testboard-dev-perf-")
        cls.db = os.path.join(cls.scratch, "seed.db")
        seed.build(cls.db, None, **_SMALL)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)


class SeedTest(_SeededCase):

    #: fingerprint() of the default recipe at _SMALL, computed with the
    #: base code of WP-43 item 0 (a8013f1: its seed.py and testboard,
    #: extracted with ``git archive a8013f1 testboard
    #: tools/dev/perf/seed.py`` and built twice, identical both times).
    #: Recompute it the same way, on the commit that changes it, when a
    #: migration adds a table or a ``latest_runs`` column or the recipe
    #: deliberately changes, and say which in the commit message.
    PINNED = ({
        "activity_hours": 63, "assignments": 0, "comments": 2,
        "current_assignments": 0, "environment_expectations": 0,
        "environment_products": 7, "latest_runs": 974, "mute_history": 0,
        "run_outputs": 2790, "runs": 2790, "schema_version": 1,
        "script_hours": 182, "streams": 5, "test_mutes": 0,
        "test_retirements": 0, "users": 4},
        "0ae17dd0184f6878230e574fd129f5556fc9e979191875778d46530740bd252e")

    def test_the_default_recipe_is_pinned(self) -> None:
        """perf-seed/1 is unchanged by every option added beside it: the
        default build still gives the rows the base code gave."""
        self.assertEqual(seed.fingerprint(self.db), self.PINNED)

    def test_two_builds_are_identical(self) -> None:
        again = os.path.join(self.scratch, "again.db")
        seed.build(again, None, **_SMALL)
        self.assertEqual(seed.fingerprint(self.db), seed.fingerprint(again))

    def test_the_shape_is_the_recipe(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            per_stream = dict(conn.execute(
                "SELECT s.name, COUNT(*) FROM latest_runs l JOIN streams s "
                "ON s.id = l.stream_id GROUP BY s.name").fetchall())
            atlas_mainline = conn.execute(
                "SELECT COUNT(*) FROM latest_runs l JOIN environment_products"
                " ep ON ep.environment = l.environment WHERE l.stream_id = 1"
                " AND ep.product = 'Atlas'").fetchone()[0]
            environments = conn.execute(
                "SELECT COUNT(DISTINCT environment) FROM latest_runs "
                "WHERE stream_id = 1").fetchone()[0]
            assignments = conn.execute(
                "SELECT COUNT(*) FROM current_assignments").fetchone()[0]
        finally:
            conn.close()
        # The full-size build covers every test of the product's mainline.
        self.assertEqual(per_stream[seed.FULL_BUILD], atlas_mainline)
        self.assertEqual(environments, len(seed.ESTATE))
        # The runner's "no assignments" row depends on there being none.
        self.assertEqual(assignments, 0)

    def test_refuses_to_write_inside_a_checkout(self) -> None:
        target = os.path.join(REPO_ROOT, "testboard.db")
        existed = os.path.exists(target)
        with self.assertRaises(ValueError):
            seed.build(target, None, **_SMALL)
        self.assertEqual(os.path.exists(target), existed)
        with self.assertRaises(ValueError):
            seed.build(os.path.join(REPO_ROOT, ".scratch", "x", "p.db"),
                       None, **_SMALL)


class RunnerTest(_SeededCase):

    def _planted_tree(self) -> str:
        tree = os.path.join(self.scratch, "planted")
        shutil.copytree(os.path.join(REPO_ROOT, "testboard"),
                        os.path.join(tree, "testboard"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(tree, "testboard", "storage.py"), "a",
                  encoding="utf-8") as handle:
            handle.write(_PLANT)
        return tree

    def _run(self, head: str, pages: List[Tuple[str, str]]) -> object:
        scratch = tempfile.mkdtemp(dir=self.scratch)
        return ab.run(REPO_ROOT, head, self.db, scratch, ("A", "B"),
                      pages=pages, calls=8, known=False)

    def test_refuses_the_repo_root_database(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ab.check_db(os.path.join(REPO_ROOT, "testboard.db"))
        self.assertIn("repo-root", str(caught.exception))

    def test_a_planted_slow_path_shows_up_and_nothing_else_does(self) -> None:
        before = sys.modules.get("testboard.storage")
        result = self._run(self._planted_tree(), [
            ("browse", "/api/dashboard?limit=50&offset=0"),
            ("headline", "/api/summary?parts=headline"),
        ])
        rows = dict((row.name, ab.judge(row))
                    for row in getattr(result, "rows"))
        self.assertTrue(rows["browse"].call.startswith("SLOWER"),
                        rows["browse"])
        self.assertGreater(rows["browse"].delta, 20.0)
        # The plant does not leak into a row that never calls it. Not
        # the verdict word: on a loaded machine an untouched row can be
        # "SLOWER" by a millisecond; it cannot be by the plant's 25 ms.
        self.assertLess(abs(rows["headline"].delta), 10.0, rows["headline"])
        # The caller's testboard is the one it had before the run.
        self.assertIs(sys.modules.get("testboard.storage"), before)

    def test_the_trees_are_separate_modules(self) -> None:
        first = ab.load_tree("A", REPO_ROOT)
        second = ab.load_tree("B", REPO_ROOT)
        self.assertIsNot(first.modules["testboard.storage"],
                         second.modules["testboard.storage"])
        self.assertIsNot(first.modules["testboard.storage"],
                         sys.modules.get("testboard.storage"))

    def test_cold_means_cold(self) -> None:
        copy = os.path.join(self.scratch, "cold.db")
        ab.copy_db(self.db, copy)
        tree = ab.load_tree("A", REPO_ROOT)
        scope = ab.derive_scope(copy)
        activator = ab.Activator()
        activator.use(tree)
        store = getattr(tree.modules["testboard.storage"], "Storage")(copy)
        try:
            url = "/api/summary?parts=headline"
            tracer = ab._Tracer()
            cold = ab.count_statements(tree, store, url, scope.now, [],
                                       tracer)
            # Warm: the same call again WITHOUT clearing.
            conn = store._conn()
            seen = []  # type: List[str]

            def record(statement: str) -> None:
                seen.append(statement)

            # A plain function, not seen.append: 3.6's sqlite3 hashes the
            # callback, and a list's bound method is unhashable there.
            conn.set_trace_callback(record)
            try:
                getattr(tree.modules["testboard.api"], "handle_api")(
                    store, ab.make_request(tree, url),
                    now=lambda: scope.now)
            finally:
                conn.set_trace_callback(None)
            self.assertGreater(cold, len(seen))
            # And clearing really does bring the cold count back.
            self.assertEqual(
                ab.count_statements(tree, store, url, scope.now, [], tracer),
                cold)
        finally:
            store.close()
            activator.restore()

    def test_band_and_verdict(self) -> None:
        same = ab.Row("r", "/api/x", 200, 200, [10.0] * 12,
                      [10.0, 10.2] * 6, 3, 3)
        self.assertEqual(ab.judge(same).call, "same")
        slower = ab.Row("r", "/api/x", 200, 200, [10.0] * 12,
                        [14.0] * 12, 3, 4)
        self.assertEqual(ab.judge(slower).call, "SLOWER, MORE SQL")
        new = ab.Row("r", "/api/x", 404, 200, [1.0], [1.0], 0, 0)
        self.assertEqual(ab.judge(new).call, "status 404/200")


class HistoryVariantTest(_SeededCase):
    """The -hN variant: N nights of the largest environment's mainline,
    the rest as perf-seed/1 has it, and its last nights are perf-seed/1's
    runs exactly (the history has its own random stream)."""

    #: Two whole history blocks (14 nights) before the seed's three.
    HISTORY = _SMALL["nights"] + 2 * 7

    @classmethod
    def setUpClass(cls) -> None:
        super(HistoryVariantTest, cls).setUpClass()
        cls.variant = os.path.join(cls.scratch, "variant.db")
        seed.build(cls.variant, None, history_nights=cls.HISTORY, **_SMALL)

    def _days(self, path: str) -> Dict[str, int]:
        conn = sqlite3.connect(path)
        try:
            return dict(conn.execute(
                "SELECT environment, COUNT(DISTINCT SUBSTR(start_time, 1, 10))"
                " FROM runs WHERE stream_id = 1 GROUP BY environment"
            ).fetchall())
        finally:
            conn.close()

    def _runs_since(self, path: str, since: str) -> List[Tuple[object, ...]]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT stream_id, environment, script, test_name, "
                "start_time, end_time, result FROM runs WHERE start_time >= ?"
                " ORDER BY id", (since,)).fetchall()
        finally:
            conn.close()

    def test_only_the_largest_environment_goes_back(self) -> None:
        days = self._days(self.variant)
        largest = seed.ESTATE[0][1]
        self.assertEqual(days.pop(largest), self.HISTORY)
        self.assertEqual(set(days.values()), {_SMALL["nights"]})

    def test_the_last_nights_are_the_plain_seed(self) -> None:
        first = (seed.NEWEST_NIGHT - seed.datetime.timedelta(
            days=_SMALL["nights"] - 1)).strftime("%Y-%m-%d")
        plain = self._runs_since(self.db, first)
        self.assertEqual(len(plain), self._count(self.db))
        self.assertEqual(self._runs_since(self.variant, first), plain)

    def _count(self, path: str) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()

    def _turned(self, conn: sqlite3.Connection, block: List[str]
                ) -> Set[str]:
        """Tests that broke inside *block* (seven days): passing on its
        first night, failing on each of its last three. A flaky test
        does that about once in 80 blocks, a stable one practically
        never: these are the block's broken tests."""
        def failing(day: str) -> Set[str]:
            return set(row[0] for row in conn.execute(
                "SELECT script || '/' || test_name FROM runs "
                "WHERE stream_id = 1 AND environment = ? AND result = 'FAIL'"
                " AND SUBSTR(start_time, 1, 10) = ?",
                (seed.ESTATE[0][1], day)).fetchall())
        return (failing(block[4]) & failing(block[5]) & failing(block[6])
                ) - failing(block[0])

    def test_each_history_block_redraws_the_behaviours(self) -> None:
        # One static week repeated would break the SAME tests in every
        # block; the redraw breaks different ones. Deterministic seed:
        # this is a fixed outcome, not a probability.
        conn = sqlite3.connect(self.variant)
        try:
            days = [row[0] for row in conn.execute(
                "SELECT DISTINCT SUBSTR(start_time, 1, 10) AS day FROM runs "
                "WHERE stream_id = 1 AND environment = ? ORDER BY day",
                (seed.ESTATE[0][1],)).fetchall()]
            first = self._turned(conn, days[0:seed.HISTORY_BLOCK])
            second = self._turned(
                conn, days[seed.HISTORY_BLOCK:2 * seed.HISTORY_BLOCK])
        finally:
            conn.close()
        self.assertTrue(first and second, (first, second))
        self.assertFalse(first & second, (first, second))

    def test_two_builds_are_identical(self) -> None:
        again = os.path.join(self.scratch, "variant-again.db")
        seed.build(again, None, history_nights=self.HISTORY, **_SMALL)
        self.assertEqual(seed.fingerprint(self.variant),
                         seed.fingerprint(again))

    def test_the_variant_is_named(self) -> None:
        self.assertEqual(seed.recipe_name(7, 0), "perf-seed/1")
        self.assertEqual(seed.recipe_name(7, 90), "perf-seed/1-h90")
        with self.assertRaises(ValueError):
            seed.build(os.path.join(self.scratch, "bad.db"), None,
                       history_nights=_SMALL["nights"], **_SMALL)


class FixtureTest(_SeededCase):
    """The assignments fixture: N assigned, 10 live mutes, the Known slow
    row's 65 first, the same whether seed.py or ab.py writes it."""

    def _state(self, path: str) -> Tuple[int, int]:
        conn = sqlite3.connect(path)
        try:
            return (conn.execute(
                "SELECT COUNT(*) FROM current_assignments "
                "WHERE assignee IS NOT NULL").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM test_mutes").fetchone()[0])
        finally:
            conn.close()

    def test_onto_writes_it_once(self) -> None:
        copy = os.path.join(self.scratch, "fixture.db")
        ab.copy_db(self.db, copy)
        self.assertEqual(seed.apply_onto(copy, 40), (40, 10))
        self.assertEqual(self._state(copy), (40, 10))
        with self.assertRaises(ValueError):
            seed.apply_onto(copy, 40)
        with self.assertRaises(ValueError):
            seed.apply_onto(os.path.join(REPO_ROOT, "testboard.db"), 40)

    def test_current_failures_come_first(self) -> None:
        environment = seed.ESTATE[0][1]
        chosen, mutes = seed.fixture_triples(self.db, environment, 60)
        conn = sqlite3.connect(self.db)
        try:
            failing = [tuple(row) for row in conn.execute(
                "SELECT environment, script, test_name FROM latest_runs "
                "WHERE stream_id = 1 AND environment = ? AND result = 'FAIL'"
                " ORDER BY script, test_name", (environment,)).fetchall()]
        finally:
            conn.close()
        self.assertEqual(chosen[:len(failing)], failing[:60])
        self.assertEqual(len(set(chosen)), 60)
        self.assertEqual(len(set(mutes)), seed.FIXTURE_MUTES)
        self.assertTrue(set(mutes) <= set(chosen))

    def test_the_runner_applies_it_to_the_pages_rows(self) -> None:
        scratch = tempfile.mkdtemp(dir=self.scratch)
        result = ab.run(REPO_ROOT, REPO_ROOT, self.db, scratch, ("A", "B"),
                        pages=[("browse", "/api/dashboard?limit=50&offset=0")],
                        calls=2, known=False, assign=40)
        self.assertEqual(getattr(result, "fixture"), (40, 10))
        self.assertIn("40 assigned", "\n".join(ab.report(result)))
        # The seed itself is never written.
        self.assertEqual(self._state(self.db), (0, 0))


class AnnotationsTest(unittest.TestCase):
    """The harness is not a package, so the compat sweep parses it but
    cannot import it; evaluate its annotations here."""

    def test_every_function_annotation_evaluates(self) -> None:
        checked = 0
        for module in (seed, ab):
            for value in list(vars(module).values()):
                targets = [value]
                if isinstance(value, type) and value.__module__ == \
                        module.__name__:
                    targets.extend(vars(value).values())
                for target in targets:
                    if (isinstance(target, types.FunctionType)
                            and target.__module__ == module.__name__):
                        typing.get_type_hints(target)
                        checked += 1
        self.assertGreater(checked, 20)


if __name__ == "__main__":
    unittest.main()
