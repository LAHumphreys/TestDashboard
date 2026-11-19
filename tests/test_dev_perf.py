"""The performance harness measures what it claims to (tools/dev/perf/).

A runner that cannot see a planted slow path, a "cold" call that is
secretly warm, or a seed that differs between two builds would produce
numbers that look like evidence and are not. Each test here is one of
those failure modes, run on a small seed (``--scale 0.03``) in a temp
directory, never inside the checkout.
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import typing
import unittest
from typing import Callable, Dict, List, Set, Tuple

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
mariadb_load = _load("mariadb_load")

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
        "environment_flaky_defaults": 0, "environment_products": 7,
        "flaky_default_history": 0, "flaky_mark_history": 0,
        "latest_runs": 974, "mute_history": 0,
        "run_outputs": 2790, "runs": 2790, "schema_version": 1,
        "script_hours": 182, "streams": 5, "test_flaky_marks": 0,
        "test_mutes": 0, "test_retirements": 0, "users": 4},
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


#: Appended to a COPY of storage.py: a tree from before migration 12, as
#: far as the harness can tell (it asks for ``Storage.mark_flaky``).
_NO_MARKS = '''

del Storage.mark_flaky
'''

#: Appended to a COPY of storage.py: the same marks and cadences, a
#: different reason, so the census agrees and the rows do not.
_OTHER_REASON = '''

_planted_mark_flaky = Storage.mark_flaky


def _other_reason(self, tests, cadence, reason, marked_by, when):
    return _planted_mark_flaky(self, tests, cadence, "another reason",
                               marked_by, when)


Storage.mark_flaky = _other_reason
'''

#: Appended to a COPY of storage.py: every Browse page raises.
_RAISE = '''

def _planted_raising_dashboard(self, *args, **kwargs):
    raise RuntimeError("planted: browse is broken")


Storage.dashboard = _planted_raising_dashboard
'''


#: Appended to a COPY of api.py: Browse raises OUTSIDE the handler's own
#: catch, as a broken tree or harness would.
_ESCAPE = '''

_planted_handle_api = handle_api


def handle_api(storage, request, *args, **kwargs):
    if request.path == "/api/dashboard":
        raise RuntimeError("planted: escaped the handler")
    return _planted_handle_api(storage, request, *args, **kwargs)
'''


def _planted_copy(scratch: str, name: str, plant: str) -> str:
    """A copy of the package with *plant* appended to its storage.py."""
    tree = os.path.join(scratch, name)
    shutil.copytree(os.path.join(REPO_ROOT, "testboard"),
                    os.path.join(tree, "testboard"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(tree, "testboard", "storage.py"), "a",
              encoding="utf-8") as handle:
        handle.write(plant)
    return tree


class MarkStateTest(unittest.TestCase):
    """``seed.mark_state`` is §2 B.1's table, row by row: the census the
    README quotes and a measured pass reads the build against."""

    F, P, U = "FAIL", "PASS", "UNEXPECTED_PASS"

    def _state(self, results: List[str], cadence: int) -> Tuple[object, ...]:
        state = seed.mark_state(results, cadence)
        return (state.state, state.streak, state.runs, state.promoted,
                state.candidate, state.real_fix)

    def test_the_table(self) -> None:
        F, P, U = self.F, self.P, self.U
        cases = [
            # latest not FAIL: passing; after a FAIL a candidate, a real
            # fix when runs 2..N+1 (or all it had) all failed
            ([P, P, F], 3, ("passing", 0, None, False, False, False)),
            ([P, F, P, F], 3, ("passing", 0, None, False, True, False)),
            ([P, F, F, F, P], 3, ("passing", 0, None, False, True, True)),
            ([P, F, F], 3, ("passing", 0, None, False, True, True)),
            # a FAIL with none before: failing, as today
            ([F], 5, ("failing", 1, 1, False, False, False)),
            # a FAIL after anything else (an unexpected pass included)
            ([F, U, F], 2, ("flaking", 1, None, False, False, False)),
            # cadence 2, two FAILs running: failing; promoted when run 3
            # exists and is not a FAIL
            ([F, F, P], 2, ("failing", 2, 2, True, False, False)),
            ([F, F, F], 2, ("failing", 2, 2, False, False, False)),
            ([F, F], 2, ("failing", 2, 2, False, False, False)),
            # cadence 3..10: flaking until the newest N all failed
            ([F, F, P, F], 3, ("flaking", 2, None, False, False, False)),
            ([F, F, F, P], 3, ("failing", 3, 3, True, False, False)),
            ([F, F, F, F], 3, ("failing", 3, 3, False, False, False)),
            # fewer than N runs, every one a FAIL: over already (verdict 8)
            ([F, F, F], 5, ("failing", 3, 3, False, False, False)),
        ]
        for results, cadence, expected in cases:
            self.assertEqual(self._state(results, cadence), expected,
                             (results, cadence))

    def test_every_cadence_from_two_to_ten(self) -> None:
        cadences = [seed.mark_cadence(i) for i in range(80)]
        self.assertEqual(sorted(set(cadences)), list(range(2, 11)))
        # The design-time cycle, bar its 10-slot.
        for index in range(80):
            if index % 8 != 6:
                self.assertEqual(cadences[index], seed.MARK_CYCLE[index % 8])
        self.assertEqual(cadences[6], 10)


class MarksFixtureTest(unittest.TestCase):
    """The marks fixture on a seed big enough to hold every state: N
    marks through the product's own ``mark_flaky``, its companions at
    every N, the counts pinned, the same twice, composing with the
    assignments fixture, refused by a tree without marks."""

    #: Seven nights so a cadence-3 test can be promoted; 0.3 so the core
    #: of 50 holds every state of the rule.
    SEED = {"scale": 0.3, "nights": 7}

    #: mark_census at N on SEED, mainline then the two builds the
    #: largest environment runs on: (marks, flaky_total, retired,
    #: passing, candidates, real_fixes, flaking, F, X, failing,
    #: failing with fewer than N runs, P, muted, assigned). Recompute
    #: (and say so in the commit) when the recipe or the rule changes.
    PINNED = {
        0: [],
        40: [("mainline", (40, 38, 2, 19, 12, 4, 11, 10, 9, 10, 0, 3, 1, 2)),
             ("nightly/full",
              (40, 38, 2, 19, 12, 4, 10, 10, 9, 11, 1, 3, 0, 2)),
             ("feature/checkout-rewrite",
              (25, 24, 1, 11, 7, 4, 7, 7, 6, 7, 3, 0, 0, 2))],
        120: [("mainline",
               (120, 118, 2, 99, 21, 4, 11, 10, 9, 10, 0, 3, 1, 2)),
              ("nightly/full",
               (120, 118, 2, 99, 22, 4, 10, 10, 9, 11, 1, 3, 0, 2)),
              ("feature/checkout-rewrite",
               (62, 61, 1, 47, 10, 4, 8, 8, 7, 7, 3, 0, 0, 2))],
    }  # type: Dict[int, List[Tuple[str, Tuple[int, ...]]]]

    scratch = ""
    db = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.mkdtemp(prefix="testboard-dev-perf-marks-")
        cls.db = os.path.join(cls.scratch, "seed.db")
        seed.build(cls.db, None, **cls.SEED)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def _copy(self, name: str) -> str:
        copy = os.path.join(self.scratch, name)
        ab.copy_db(self.db, copy)
        return copy

    def _census(self, path: str) -> List[Tuple[str, Tuple[int, ...]]]:
        return [(c.stream, (c.marks, c.flaky_total, c.retired, c.passing,
                            c.candidates, c.real_fixes, c.flaking, c.f, c.x,
                            c.failing, c.failing_short, c.promoted, c.muted,
                            c.assigned))
                for c in seed.mark_census(path, seed.seed_clock(path))]

    def _count(self, path: str, sql: str) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def test_the_counts_per_n_are_pinned(self) -> None:
        for count in sorted(self.PINNED):
            copy = self._copy("pinned-{}.db".format(count))
            _assigned, done = seed.apply_onto_marks(copy, 0, None, count)
            self.assertEqual(tuple(done), (count, 1, 1, 2), count)
            self.assertEqual(self._census(copy), self.PINNED[count], count)
            self.assertEqual(self._count(
                copy, "SELECT COUNT(*) FROM test_flaky_marks"), count)

    def test_every_state_and_cadence_is_marked(self) -> None:
        copy = self._copy("states.db")
        seed.apply_onto_marks(copy, 0, None, 40)
        census = dict((c.stream, c) for c in seed.mark_census(
            copy, seed.seed_clock(copy)))
        main = census["mainline"]
        self.assertEqual(main.cadences, list(range(2, 11)))
        for field in ("passing", "candidates", "real_fixes", "flaking", "f",
                      "x", "failing", "promoted", "retired"):
            self.assertGreater(getattr(main, field), 0, field)
        # Failing with fewer than N runs: on a build.
        self.assertGreater(census["feature/checkout-rewrite"].failing_short, 0)
        # flaky_total is every mark in scope, the candidates included.
        self.assertEqual(main.flaky_total, main.marks - main.retired)

    def test_one_marked_test_is_muted_and_one_assigned(self) -> None:
        copy = self._copy("companions.db")
        seed.apply_onto_marks(copy, 0, None, 40)
        plan = seed.plan_marks(copy, seed.ESTATE[0][1])
        marked = set(t for t, _c in plan.marks[:40])
        self.assertIn(plan.muted, marked)
        self.assertIn(plan.assigned, marked)
        self.assertNotEqual(plan.muted, plan.assigned)
        conn = sqlite3.connect(copy)
        try:
            muted = conn.execute(
                "SELECT environment, script, test_name FROM test_mutes "
                "WHERE stream_id = 1").fetchall()
            assigned = conn.execute(
                "SELECT environment, script, test_name FROM "
                "current_assignments WHERE assignee IS NOT NULL").fetchall()
            retired = conn.execute(
                "SELECT environment, script, test_name FROM "
                "test_retirements").fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in muted], [plan.muted])
        # A mute is owned: the muted test is assigned too.
        self.assertEqual(sorted(tuple(r) for r in assigned),
                         sorted([plan.muted, plan.assigned]))
        self.assertEqual(sorted(tuple(r) for r in retired),
                         sorted(plan.retired))
        self.assertTrue(set(plan.retired) <= marked)

    def test_written_through_the_storage_call(self) -> None:
        copy = self._copy("history.db")
        seed.apply_onto_marks(copy, 0, None, 40)
        # mark_flaky's own trail: one history row and one comment per
        # mark, which a raw INSERT would not leave.
        self.assertEqual(self._count(
            copy, "SELECT COUNT(*) FROM flaky_mark_history "
                  "WHERE action = 'mark'"), 40)
        self.assertEqual(self._count(
            copy, "SELECT COUNT(*) FROM comments WHERE text = '{}'".format(
                seed.MARK_REASON)), 40)

    def test_the_marks_precede_the_runs_they_are_read_against(self) -> None:
        """A test is promoted only on a run that started at or after its
        cadence was set: marks dated after the runs leave P at 0 in the
        product whatever the census says. Every mark, its history and its
        comment come before every latest run of the environment, and the
        product's own P on mainline is the census's."""
        copy = self._copy("dated.db")
        seed.apply_onto_marks(copy, 0, None, 40)
        environment = seed.ESTATE[0][1]
        dated = seed.marks_clock(copy, environment)
        conn = sqlite3.connect(copy)
        try:
            first = conn.execute(
                "SELECT MIN(start_time) FROM latest_runs "
                "WHERE environment = ?", (environment,)).fetchone()[0]
            latest_act = conn.execute(
                "SELECT MAX(marked_at) FROM test_flaky_marks").fetchone()[0]
            latest_history = conn.execute(
                "SELECT MAX(acted_at) FROM flaky_mark_history").fetchone()[0]
            latest_comment = conn.execute(
                "SELECT MAX(created_at) FROM comments WHERE text = ?",
                (seed.MARK_REASON,)).fetchone()[0]
        finally:
            conn.close()
        for stamp in (latest_act, latest_history, latest_comment):
            self.assertLess(stamp, first)
            self.assertEqual(stamp[:19], dated.strftime("%Y-%m-%dT%H:%M:%S"))
        census = seed.mark_census(copy, seed.seed_clock(copy))[0]
        self.assertEqual(census.stream, "mainline")
        self.assertGreater(census.promoted, 0)
        sys.path.insert(0, REPO_ROOT)
        try:
            from testboard import storage as product
        finally:
            sys.path.remove(REPO_ROOT)
        store = product.Storage(copy)
        try:
            view_of = getattr(store, "flaky_view", None)
            if view_of is None:
                return  # a tree before WP-44 item 6 has no promotion
            view = view_of(1, seed.seed_clock(copy), environment=environment,
                           marks=True, failing=True)
        finally:
            store.close()
        promoted = [c for c in getattr(view, "counted")
                    if getattr(c, "role") == "promoted"]
        self.assertEqual(len(promoted), census.promoted)

    def test_same_seed_same_marks_same_digest(self) -> None:
        first, second = self._copy("first.db"), self._copy("second.db")
        seed.apply_onto_marks(first, 0, None, 120)
        seed.apply_onto_marks(second, 0, None, 120)
        self.assertEqual(seed.fixture_digest(first),
                         seed.fixture_digest(second))
        self.assertEqual(self._census(first), self._census(second))
        self.assertNotEqual(seed.fixture_digest(first),
                            seed.fixture_digest(self.db))
        # The plan does not depend on N: the fixture at 40 is the first
        # 40 of the fixture at 120.
        plan = seed.plan_marks(first, seed.ESTATE[0][1])
        self.assertEqual(len(plan.marks), len(set(t for t, _ in plan.marks)))
        with self.assertRaises(ValueError):
            seed.apply_onto_marks(first, 0, None, 120)

    def test_it_composes_with_the_assignments_fixture(self) -> None:
        copy = self._copy("composed.db")
        (assigned, muted), done = seed.apply_onto_marks(
            copy, seed.REFERENCE_ASSIGNMENTS, None, 40)
        self.assertEqual((assigned, muted),
                         (seed.REFERENCE_ASSIGNMENTS, seed.FIXTURE_MUTES))
        self.assertEqual(done.marks if done else None, 40)
        # The companions coincide with the reference fixture's own: the
        # marks add no assignment and no mute to --assign 65.
        self.assertEqual(self._count(
            copy, "SELECT COUNT(*) FROM current_assignments "
                  "WHERE assignee IS NOT NULL"), seed.REFERENCE_ASSIGNMENTS)
        self.assertEqual(self._count(
            copy, "SELECT COUNT(*) FROM test_mutes"), seed.FIXTURE_MUTES)

    def test_the_runner_applies_it_and_reports_the_census(self) -> None:
        scratch = tempfile.mkdtemp(dir=self.scratch)
        result = ab.run(REPO_ROOT, REPO_ROOT, self.db, scratch, ("A", "B"),
                        pages=[("browse", "/api/dashboard?limit=50&offset=0")],
                        calls=2, known=False, assign=40, marks=40)
        self.assertEqual(getattr(result, "fixture"), (40, 10))
        self.assertEqual(getattr(result, "marks"), (40, 1, 1, 2))
        text = "\n".join(ab.report(result))
        self.assertIn("40 marked", text)
        self.assertIn("mainline", text)
        self.assertEqual(self._count(
            self.db, "SELECT COUNT(*) FROM test_flaky_marks"), 0)

    def test_newly_fixed_counts_only_unmuted_unretired(self) -> None:
        """§2 B.1: "Newly fixed" concerns a marked, unmuted, unretired
        test; a retired candidate leaves both counts."""
        copy = self._copy("retired-candidate.db")
        seed.apply_onto_marks(copy, 0, None, 40)
        now = seed.seed_clock(copy)
        before = seed.mark_census(copy, now)[0]
        plan = seed.plan_marks(copy, seed.ESTATE[0][1])
        conn = sqlite3.connect(copy)
        try:
            fix = None
            for triple, cadence in plan.marks[:40]:
                state = seed.mark_state(
                    seed._newest(conn, 1, triple, cadence + 1), cadence)
                if state.real_fix:
                    fix = triple
                    break
            self.assertIsNotNone(fix)
            # Setting up the state the census reads, in a test copy.
            conn.execute(
                "INSERT INTO test_retirements VALUES (?, ?, ?, ?, ?)",
                tuple(fix or ()) + ("2026-09-28T22:00:00", "amy"))
            conn.commit()
        finally:
            conn.close()
        after = seed.mark_census(copy, now)[0]
        self.assertEqual(after.stream, "mainline")
        self.assertEqual(after.candidates, before.candidates - 1)
        self.assertEqual(after.real_fixes, before.real_fixes - 1)
        self.assertEqual(after.passing, before.passing)

    def test_the_sides_must_write_the_same_rows(self) -> None:
        """The census holds the set of cadences, not each test's; the
        digest holds every row. A tree writing the same marks with a
        different reason agrees on the census and is refused."""
        tree = _planted_copy(self.scratch, "other-reason", _OTHER_REASON)
        with self.assertRaises(ValueError) as caught:
            ab.run(REPO_ROOT, tree, self.db,
                   tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                   pages=[("browse", "/api/dashboard?limit=50&offset=0")],
                   calls=2, known=False, marks=40)
        self.assertIn("rows differ", str(caught.exception))

    def test_run_refuses_a_fixture_with_nothing_to_measure(self) -> None:
        for kwargs in ({"marks": 40}, {"marks": 0}, {"assign": 40}):
            with self.assertRaises(ValueError) as caught:
                ab.run(REPO_ROOT, REPO_ROOT, self.db,
                       tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                       calls=2, known=True, **kwargs)
            self.assertIn("--pages", str(caught.exception))

    def test_a_tree_without_marks_refuses_unless_n_is_zero(self) -> None:
        tree = os.path.join(self.scratch, "no-marks")
        shutil.copytree(os.path.join(REPO_ROOT, "testboard"),
                        os.path.join(tree, "testboard"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(tree, "testboard", "storage.py"), "a",
                  encoding="utf-8") as handle:
            handle.write(_NO_MARKS)
        page = [("browse", "/api/dashboard?limit=50&offset=0")]
        with self.assertRaises(ValueError) as caught:
            ab.run(tree, REPO_ROOT, self.db,
                   tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                   pages=page, calls=2, known=False, marks=40)
        self.assertIn("migration 12", str(caught.exception))
        result = ab.run(tree, REPO_ROOT, self.db,
                        tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                        pages=page, calls=2, known=False, marks=0)
        self.assertEqual(getattr(result, "marks"), (0, 1, 1, 2))


#: Appended to a COPY of api.py: a stand-in for WP-44 item 22's
#: ``PUT /api/environments/{environment}/flaky-default``, writing the two
#: tables of the design's §2 B.2 through the storage's own connection.
#: ``{reason}`` is the reason it stores (the request's, or a planted one).
_DEFAULT_ENDPOINT = '''

import json as _planted_json
from urllib.parse import unquote as _planted_unquote

_planted_default_handle = handle_api


def handle_api(storage, request, *args, **kwargs):
    parts = request.path.split("/")
    if request.method == "PUT" and parts[-1] == "flaky-default":
        body = _planted_json.loads(request.body.decode("utf-8"))
        reason = {reason}
        when = kwargs["now"]().isoformat()
        environment = _planted_unquote(parts[3])
        conn = storage._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS environment_flaky_defaults ("
            "environment TEXT NOT NULL PRIMARY KEY, cadence INTEGER NOT "
            "NULL, reason TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT "
            "NOT NULL, cadence_at TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS flaky_default_history (id INTEGER "
            "PRIMARY KEY AUTOINCREMENT, environment TEXT NOT NULL, action "
            "TEXT NOT NULL, cadence INTEGER, reason TEXT NOT NULL, actor "
            "TEXT NOT NULL, acted_at TEXT NOT NULL)")
        conn.execute(
            "INSERT OR REPLACE INTO environment_flaky_defaults "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (environment, body["cadence"], reason, body["username"], when,
             when))
        conn.execute(
            "INSERT INTO flaky_default_history (environment, action, "
            "cadence, reason, actor, acted_at) VALUES (?, 'set', ?, ?, ?, ?)",
            (environment, body["cadence"], reason, body["username"], when))
        conn.commit()
        return Response(200, [("Content-Type",
                               "application/json; charset=utf-8")], b"{{}}")
    return _planted_default_handle(storage, request, *args, **kwargs)
'''

#: Appended to a COPY of api.py: a tree from before item 22, as far as
#: the harness can tell (the endpoint answers 404), whatever the tree is.
_NO_DEFAULT_ENDPOINT = '''

_planted_no_default_handle = handle_api


def handle_api(storage, request, *args, **kwargs):
    if request.path.endswith("/flaky-default"):
        return Response(404, [("Content-Type",
                               "application/json; charset=utf-8")],
                        b'{"error": "planted: no such route"}')
    return _planted_no_default_handle(storage, request, *args, **kwargs)
'''


def _api_planted_copy(scratch: str, name: str, plant: str) -> str:
    """A copy of the package with *plant* appended to its api.py."""
    tree = _planted_copy(scratch, name, "")
    with open(os.path.join(tree, "testboard", "api.py"), "a",
              encoding="utf-8") as handle:
        handle.write(plant)
    return tree


class FailingFixtureTest(unittest.TestCase):
    """The failing fixture the flaky default's two bars were measured on
    (docs/design/flaky-default-r0.md §3a), at a tiny size: the nightly
    mix over every night, with and without the second server, the latest
    mix, the derived tables rebuilt and the check that proves it, and the
    refusals."""

    #: Seventeen nights of the largest environment (240 tests at 0.03):
    #: more than the per-day chart's fourteen.
    SEED = {"scale": 0.03, "nights": 3, "history_nights": 17}
    FAILING = 20

    scratch = ""
    db = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.mkdtemp(prefix="testboard-dev-perf-failing-")
        cls.db = os.path.join(cls.scratch, "seed.db")
        seed.build(cls.db, None, **cls.SEED)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def _copy(self, name: str) -> str:
        copy = os.path.join(self.scratch, name)
        ab.copy_db(self.db, copy)
        return copy

    def _rows(self, path: str, sql: str, *params: object
              ) -> List[Tuple[object, ...]]:
        conn = sqlite3.connect(path)
        try:
            return [tuple(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def _environment_runs(self, path: str) -> List[Tuple[object, ...]]:
        return self._rows(
            path, "SELECT script, test_name, start_time, end_time, result, "
            "id FROM runs WHERE environment = ? AND stream_id = 1 "
            "ORDER BY script, test_name, start_time", seed.ESTATE[0][1])

    def test_the_nightly_mix_gives_the_bars_nights(self) -> None:
        """At 8,000 tests the draw holds the fourteen nights the per-day
        chart's bar quotes (§3a, "The trend, round 1": 1,038 FAIL runs
        over 204 tests at 50, 9,286 over 1,790 at 500)."""
        import hashlib
        self.assertEqual(seed.mix_hash(7, 3), int(
            hashlib.md5(b"(7, 3)").hexdigest()[:8], 16))
        for count, runs, tests in ((50, 1038, 204), (500, 9286, 1790)):
            failed = 0
            failing = set()  # type: Set[int]
            for index in range(8000):
                for night in range(90 - seed.TREND_NIGHTS, 90):
                    pair = seed.nightly_pair(index, night, count, 8000)
                    if "FAIL" in pair:
                        failed += pair.count("FAIL")
                        failing.add(index)
            self.assertEqual((failed, len(failing)), (runs, tests), count)

    def test_two_servers_run_every_test_twice_a_night(self) -> None:
        copy = self._copy("two-servers.db")
        before = self._rows(copy, "SELECT * FROM runs WHERE environment <> ? "
                            "OR stream_id <> 1 ORDER BY id",
                            seed.ESTATE[0][1])
        done = seed.apply_onto_all(copy, 0, None, None, self.FAILING,
                                   "nightly", True)
        runs = self._environment_runs(copy)
        nights = sorted(set(str(row[2])[:10] for row in runs))
        tests = sorted(set((row[0], row[1]) for row in runs))
        self.assertEqual(len(nights), self.SEED["history_nights"])
        self.assertEqual(len(runs), 2 * len(nights) * len(tests))
        self.assertEqual(done.failing.doubled, len(runs) // 2)
        parse = seed.datetime.datetime.strptime
        for at in range(0, len(runs), 2):
            first, second = runs[at], runs[at + 1]
            self.assertEqual(first[:2], second[:2])
            started = [parse(str(row[2]), "%Y-%m-%dT%H:%M:%S.%f")
                       for row in (first, second)]
            ended = [parse(str(row[3]), "%Y-%m-%dT%H:%M:%S.%f")
                     for row in (first, second)]
            self.assertEqual(started[1] - started[0],
                             seed.datetime.timedelta(hours=3))
            self.assertEqual(ended[1] - started[1], ended[0] - started[0])
            pair = seed.nightly_pair(
                tests.index((first[0], first[1])),
                nights.index(str(first[2])[:10]), self.FAILING, len(tests))
            self.assertEqual((first[4], second[4]), pair)
        # Every second run carries the first's output, and nothing else
        # in the estate moved.
        self.assertEqual(self._rows(
            copy, "SELECT COUNT(*) FROM runs AS r JOIN runs AS n "
            "ON n.environment = r.environment AND n.script = r.script "
            "AND n.test_name = r.test_name AND n.stream_id = r.stream_id "
            "AND n.id > r.id AND SUBSTR(n.start_time, 1, 10) = "
            "SUBSTR(r.start_time, 1, 10) JOIN run_outputs AS a "
            "ON a.run_id = r.id JOIN run_outputs AS b ON b.run_id = n.id "
            "WHERE r.environment = ? AND a.output <> b.output",
            seed.ESTATE[0][1]), [(0,)])
        self.assertEqual(self._rows(
            copy, "SELECT * FROM runs WHERE environment <> ? OR "
            "stream_id <> 1 ORDER BY id", seed.ESTATE[0][1]), before)
        self.assertEqual(seed.check_derived(copy), [])
        census = seed.failing_census(copy, seed.ESTATE[0][1])
        self.assertEqual(census.runs, len(runs))
        self.assertEqual(census.estate_runs, census.outputs)
        self.assertEqual(census.window_since, nights[-seed.TREND_NIGHTS])

    def test_one_server_rewrites_and_adds_nothing(self) -> None:
        copy = self._copy("one-server.db")
        count = len(self._environment_runs(copy))
        seed.apply_onto_all(copy, 0, None, None, self.FAILING, "nightly",
                            False)
        runs = self._environment_runs(copy)
        self.assertEqual(len(runs), count)
        nights = sorted(set(str(row[2])[:10] for row in runs))
        tests = sorted(set((row[0], row[1]) for row in runs))
        for row in runs:
            self.assertEqual(row[4], seed.nightly_pair(
                tests.index((row[0], row[1])),
                nights.index(str(row[2])[:10]), self.FAILING,
                len(tests))[0])
        self.assertEqual(seed.check_derived(copy), [])

    def test_the_latest_mix_is_the_plan(self) -> None:
        """The latest mix: N failing now, half after a pass; the newest
        three runs of each planted test are the plan's, and the census's
        reading at cadence 2 follows from it (one in seven of the FAIL
        after a FAIL promoted, one in five candidates a real fix)."""
        copy = self._copy("latest.db")
        environment = seed.ESTATE[0][1]
        conn = sqlite3.connect(copy)
        try:
            plan = seed.latest_plan(seed._mainline_tests(conn, environment),
                                    self.FAILING)
        finally:
            conn.close()
        seed.apply_onto_all(copy, 0, None, None, self.FAILING, "latest")
        for (script, test_name), results in plan.items():
            newest = [row[0] for row in self._rows(
                copy, "SELECT result FROM runs WHERE environment = ? AND "
                "script = ? AND test_name = ? AND stream_id = 1 "
                "ORDER BY start_time DESC LIMIT 3", environment, script,
                test_name)]
            for got, want in zip(newest, results):
                if want is not None:
                    self.assertEqual(got, want)
        census = seed.failing_census(copy, environment)
        half, candidates = self.FAILING // 2, (3 * self.FAILING) // 10
        self.assertEqual((census.latest_fail, census.fail_after_fail,
                          census.fail_first, census.candidates),
                         (self.FAILING, half, 0, candidates))
        main = seed.default_census(copy, environment, 2,
                                   seed.seed_clock(copy))[0]
        self.assertEqual(main.stream, "mainline")
        promoted = len([j for j in range(half) if j % 7 == 0])
        fixes = len([j for j in range(candidates) if j % 5 == 0])
        self.assertEqual(
            (main.flaking, main.failing, main.promoted, main.candidates,
             main.real_fixes, main.tests),
            (half, half, promoted, candidates, fixes,
             census.tests))
        self.assertEqual(seed.check_derived(copy), [])

    def test_the_check_finds_what_a_rewrite_leaves_stale(self) -> None:
        """A rewrite of runs that skips the rebuild leaves the rollups and
        latest_runs describing runs that are gone, and every read of them
        invalid. The check reads every derived row against runs and finds
        each."""
        environment = seed.ESTATE[0][1]
        stale = self._copy("stale.db")
        conn = sqlite3.connect(stale)
        try:
            seed.apply_failing(conn, environment, self.FAILING, "nightly",
                               True)
            conn.commit()
        finally:
            conn.close()
        found = " ".join(seed.check_derived(stale))
        for table in ("activity_hours", "script_hours", "latest_runs"):
            self.assertIn(table, found)
        for name, sql in (
                ("activity_hours",
                 "UPDATE activity_hours SET count = count + 1 WHERE rowid = "
                 "(SELECT MIN(rowid) FROM activity_hours)"),
                ("latest_runs",
                 "UPDATE latest_runs SET prev_result = 'SKIP' WHERE rowid = "
                 "(SELECT MIN(rowid) FROM latest_runs)"),
                ("run_outputs",
                 "DELETE FROM run_outputs WHERE run_id = "
                 "(SELECT MAX(run_id) FROM run_outputs)")):
            copy = self._copy("planted-{}.db".format(name))
            seed.apply_onto_all(copy, 0, None, None, self.FAILING, "nightly",
                                True)
            self.assertEqual(seed.check_derived(copy), [], name)
            conn = sqlite3.connect(copy)
            try:
                conn.execute(sql)
                conn.commit()
            finally:
                conn.close()
            problems = seed.check_derived(copy)
            self.assertEqual(len(problems), 1, (name, problems))
            self.assertIn(name, problems[0])

    def test_same_seed_same_fixture(self) -> None:
        prints = []  # type: List[object]
        for name in ("same-a.db", "same-b.db"):
            copy = self._copy(name)
            seed.apply_onto_all(copy, 10, None, None, self.FAILING,
                                "nightly", True)
            prints.append(seed.fingerprint(copy))
        self.assertEqual(prints[0], prints[1])

    def test_refusals(self) -> None:
        environment = seed.ESTATE[0][1]
        cases = (({"failing": self.FAILING, "mix": "latest",
                   "two_servers": True}, "nightly mix"),
                 ({"failing": self.FAILING, "marks": 10}, "separate"),
                 ({"failing": 100000}, "fewer than"),
                 ({"failing": self.FAILING, "mix": "weekly"}, "one of"),
                 ({"flaky_default": 1}, "2 to 10"))
        for kwargs, text in cases:
            copy = self._copy("refused.db")
            with self.assertRaises(ValueError) as caught:
                seed.apply_onto_all(copy, 0, None, **kwargs)
            self.assertIn(text, str(caught.exception), kwargs)
        twice = self._copy("twice.db")
        seed.apply_onto_all(twice, 0, None, None, self.FAILING, "nightly",
                            True)
        with self.assertRaises(ValueError) as caught:
            seed.apply_onto_all(twice, 0, None, None, self.FAILING,
                                "nightly", True)
        self.assertIn("twice a night", str(caught.exception))
        self.assertEqual(seed.check_derived(twice), [])
        with self.assertRaises(ValueError):
            seed.apply_onto_all(
                os.path.join(REPO_ROOT, "testboard.db"), 0, None, None,
                self.FAILING)
        self.assertEqual(seed.failing_census(twice, environment).runs,
                         2 * len(self._environment_runs(self.db)))

    def test_the_cli_writes_it_and_prints_the_census(self) -> None:
        copy = self._copy("cli.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = seed.main(["--onto", copy, "--assignments", "10",
                              "--default-failing", str(self.FAILING),
                              "--two-servers", "--tree", REPO_ROOT])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("derived tables: valid", out.getvalue())
        self.assertIn("2.0 a test a night", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = seed.main(["--census", copy, "--cadence", "3"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("as if one were at cadence 3", out.getvalue())
        self.assertIn("default  3", out.getvalue())
        conn = sqlite3.connect(copy)
        try:
            conn.execute("DELETE FROM activity_hours WHERE rowid = "
                         "(SELECT MIN(rowid) FROM activity_hours)")
            conn.commit()
        finally:
            conn.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seed.main(["--census", copy]), 1)
        self.assertIn("INVALID", out.getvalue())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(seed.main(["--onto", copy, "--two-servers"]), 2)
            self.assertEqual(seed.main(["--out", os.path.join(
                self.scratch, "x.db"), "--default-failing", "5"]), 2)


class FlakyDefaultStepTest(unittest.TestCase):
    """The one step that sets a default once WP-44 item 22 is in: through
    the tree's own endpoint, dated before every run, its census, and the
    runner applying it to both sides or refusing."""

    scratch = ""
    db = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.mkdtemp(prefix="testboard-dev-perf-default-")
        cls.db = os.path.join(cls.scratch, "seed.db")
        seed.build(cls.db, None, scale=0.03, nights=3)
        seed.apply_onto_all(cls.db, 0, None, None, 20, "latest")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)

    class _Api(object):
        """The two names put_flaky_default asks of a tree's api module."""

        def __init__(self, status: int) -> None:
            self.status = status
            self.seen = []  # type: List[Tuple[object, datetime.datetime]]
            from testboard import api
            self.Request = api.Request
            self.Response = api.Response

        def handle_api(self, store: object, request: object,
                       now: Callable[[], datetime.datetime]) -> object:
            self.seen.append((request, now()))
            return self.Response(self.status, [], b'{"error": "planted"}')

    def test_the_request_is_the_contract(self) -> None:
        when = datetime.datetime(2026, 9, 1, 3)
        api = self._Api(200)
        seed.put_flaky_default(api, object(), "atlas-lab-alpha", 3, when)
        request, now = api.seen[0]
        self.assertEqual(now, when)
        self.assertEqual(getattr(request, "method"), "PUT")
        self.assertEqual(getattr(request, "path"),
                         "/api/environments/atlas-lab-alpha/flaky-default")
        body = json.loads(getattr(request, "body").decode("utf-8"))
        self.assertEqual(body, {"username": "amy", "cadence": 3,
                                "reason": seed.DEFAULT_REASON})
        with self.assertRaises(ValueError) as caught:
            seed.put_flaky_default(self._Api(404), object(),
                                   "atlas-lab-alpha", 3, when)
        self.assertIn("item 22", str(caught.exception))
        self.assertIn("planted", str(caught.exception))

    def test_dated_before_every_run(self) -> None:
        environment = seed.ESTATE[0][1]
        when = seed.default_clock(self.db, environment)
        conn = sqlite3.connect(self.db)
        try:
            first = conn.execute(
                "SELECT MIN(start_time) FROM runs WHERE environment = ?",
                (environment,)).fetchone()[0]
        finally:
            conn.close()
        self.assertLess(when.isoformat(), first)
        self.assertLess(first, (when + datetime.timedelta(hours=2))
                        .isoformat())

    def test_a_marked_test_is_left_to_its_mark(self) -> None:
        copy = os.path.join(self.scratch, "marked.db")
        ab.copy_db(self.db, copy)
        environment = seed.ESTATE[0][1]
        before = seed.default_census(copy, environment, 2,
                                     seed.seed_clock(copy))[0]
        conn = sqlite3.connect(copy)
        try:
            conn.execute(
                "INSERT INTO test_flaky_marks SELECT environment, script, "
                "test_name, 5, 'marked', 'amy', '2026-09-01T00:00:00' FROM "
                "latest_runs WHERE stream_id = 1 AND environment = ? AND "
                "result = 'FAIL' AND prev_result <> 'FAIL' LIMIT 1",
                (environment,))
            conn.commit()
        finally:
            conn.close()
        after = seed.default_census(copy, environment, 2,
                                    seed.seed_clock(copy))[0]
        self.assertEqual((after.marked, after.tests, after.flaking),
                         (1, before.tests - 1, before.flaking - 1))

    def test_the_census_reads_the_default_held(self) -> None:
        copy = os.path.join(self.scratch, "held.db")
        ab.copy_db(self.db, copy)
        self.assertEqual(seed.flaky_defaults(copy), {})
        conn = sqlite3.connect(copy)
        try:
            if conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'environment_flaky_defaults'").fetchone()[0]:
                # A tree at migration 13 built the seed: its own table.
                conn.execute(
                    "INSERT INTO environment_flaky_defaults (environment, "
                    "cadence, reason, set_by, set_at, cadence_at) VALUES "
                    "(?, 3, 'held', 'amy', '2026-09-01T00:00:00', "
                    "'2026-09-01T00:00:00')", (seed.ESTATE[0][1],))
            else:
                # An older tree: a stand-in with the two columns read.
                conn.execute("CREATE TABLE environment_flaky_defaults ("
                             "environment TEXT NOT NULL PRIMARY KEY, cadence "
                             "INTEGER NOT NULL)")
                conn.execute("INSERT INTO environment_flaky_defaults VALUES "
                             "(?, 3)", (seed.ESTATE[0][1],))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(seed.flaky_defaults(copy), {seed.ESTATE[0][1]: 3})
        lines = seed.census_report(copy, 2)
        self.assertIn("the default held at cadence 3", "\n".join(lines))

    def _run(self, base: str, head: str, **kwargs: object) -> object:
        return ab.run(base, head, self.db,
                      tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                      pages=[("browse", "/api/dashboard?limit=50&offset=0")],
                      calls=2, known=False, **kwargs)

    def test_the_runner_sets_it_on_both_sides(self) -> None:
        tree = _api_planted_copy(self.scratch, "with-default",
                                 _DEFAULT_ENDPOINT.format(
                                     reason='body["reason"]'))
        result = self._run(tree, tree, flaky_default=2, assign=10)
        self.assertEqual(getattr(result, "flaky_default"), 2)
        census = getattr(result, "default_census")
        self.assertTrue(census and "default  2" in census[0], census)
        report = "\n".join(ab.report(result))
        self.assertIn("flaky default of 2", report)
        # The seed itself is never written.
        self.assertEqual(seed.flaky_defaults(self.db), {})

    def test_the_runner_refuses_a_tree_without_it(self) -> None:
        with_it = _api_planted_copy(self.scratch, "has-default",
                                    _DEFAULT_ENDPOINT.format(
                                        reason='body["reason"]'))
        without = _api_planted_copy(self.scratch, "no-default",
                                    _NO_DEFAULT_ENDPOINT)
        with self.assertRaises(ValueError) as caught:
            self._run(without, with_it, flaky_default=2)
        self.assertIn("item 22", str(caught.exception))
        other = _api_planted_copy(self.scratch, "other-default-reason",
                                  _DEFAULT_ENDPOINT.format(
                                      reason='"another reason"'))
        with self.assertRaises(ValueError) as caught:
            self._run(with_it, other, flaky_default=2)
        self.assertIn("rows differ", str(caught.exception))
        for kwargs, text in (({"flaky_default": 1}, "2 to 10"),):
            with self.assertRaises(ValueError) as caught:
                self._run(with_it, with_it, **kwargs)
            self.assertIn(text, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            ab.run(with_it, with_it, self.db,
                   tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                   calls=2, known=True, flaky_default=2)
        self.assertIn("--pages", str(caught.exception))


class MariaDBLoadTest(unittest.TestCase):
    """The MariaDB recipe's loader, without a server: what it refuses
    before touching anything, what it measures from the fixture, and that
    it reaches the server only through the migration tool."""

    def setUp(self) -> None:
        self.scratch = tempfile.mkdtemp(prefix="testboard-dev-perf-mdb-")
        self.db = os.path.join(self.scratch, "fixture.db")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE run_outputs (run_id INTEGER PRIMARY "
                         "KEY, output BLOB NOT NULL)")
            conn.execute("INSERT INTO run_outputs VALUES (1, ?), (2, ?)",
                         (b"x" * 40, b"y" * 181))
            conn.commit()
        finally:
            conn.close()
        self.cnf = os.path.join(self.scratch, "sacrificial.cnf")
        with open(self.cnf, "w") as handle:
            handle.write("[client]\nuser = u\npassword = p\n"
                         "database = scratch\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_the_plan_measures_the_largest_output(self) -> None:
        plan = mariadb_load.plan_load(self.cnf, self.db, REPO_ROOT, True)
        self.assertEqual(plan.max_blob_bytes, 181)
        self.assertEqual(plan.tool, os.path.join(
            REPO_ROOT, "tools", "migrate_to_mariadb.py"))

    def test_it_refuses_before_touching_anything(self) -> None:
        for args, text in (
                ((self.cnf, self.db, REPO_ROOT, False), "--recreate"),
                ((self.cnf, os.path.join(REPO_ROOT, "testboard.db"),
                  REPO_ROOT, True), "repo-root"),
                ((self.cnf, self.db + ".missing", REPO_ROOT, True),
                 "no fixture"),
                ((self.cnf + ".missing", self.db, REPO_ROOT, True),
                 "no option file"),
                ((self.cnf, self.db, self.scratch, True), "not a checkout")):
            with self.assertRaises(ValueError) as caught:
                mariadb_load.plan_load(*args)
            self.assertIn(text, str(caught.exception))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(mariadb_load.main(
                ["--config", self.cnf, "--db", self.db]), 2)
        self.assertIn("--recreate", errors.getvalue())

    class _Server(object):
        """A server whose restoring SET can be made to fail."""

        def __init__(self, restore_fails: bool) -> None:
            self.restore_fails = restore_fails
            self.statements = []  # type: List[str]

        def run(self, sql: str) -> None:
            self.statements.append(sql)
            if self.restore_fails and sql.endswith("= 16"):
                raise RuntimeError("planted: restore refused")

        def rows(self, sql: str) -> List[Tuple[object, ...]]:
            return [(16,)]

    def test_the_packet_is_put_back_without_masking_the_load(self) -> None:
        def broken_load() -> None:
            raise RuntimeError("planted: load failed")

        said = []  # type: List[str]
        server = self._Server(restore_fails=False)
        with self.assertRaises(RuntimeError) as caught:
            mariadb_load.with_packet(server, 16, broken_load, said.append)
        self.assertIn("load failed", str(caught.exception))
        self.assertEqual(server.statements, [
            "SET GLOBAL max_allowed_packet = {}".format(
                mariadb_load.PACKET_BYTES),
            "SET GLOBAL max_allowed_packet = 16"])
        # A failed restore is reported, and the load's failure is still
        # the one raised.
        said = []
        with self.assertRaises(RuntimeError) as caught:
            mariadb_load.with_packet(self._Server(restore_fails=True), 16,
                                     broken_load, said.append)
        self.assertIn("load failed", str(caught.exception))
        self.assertIn("could NOT put", said[-1])
        # With the load fine, a failed restore is the error.
        with self.assertRaises(RuntimeError) as caught:
            mariadb_load.with_packet(self._Server(restore_fails=True), 16,
                                     lambda: None, said.append)
        self.assertIn("restore refused", str(caught.exception))

    def test_it_installs_nothing_and_shells_out_to_nothing(self) -> None:
        """The driver stays behind the migration tool (on the driver's
        allowlist), and the only program it starts is this interpreter."""
        import re
        with io.open(os.path.join(PERF_DIR, "mariadb_load.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        self.assertIsNone(re.search(r"\bpymysql\b", source))
        calls = re.findall(r"subprocess\.(\w+)\(\s*\[\s*([\w.]+)", source)
        self.assertTrue(calls)
        self.assertEqual(set(program for _call, program in calls),
                         {"sys.executable"})


class PageModeTest(_SeededCase):
    """The page mode: a page's first-paint requests on long-lived
    workers, cold, with what the page waits for; a planted slow request
    shows up as the page's, and as what it waits for."""

    def _planted_tree(self) -> str:
        tree = os.path.join(self.scratch, "planted-page")
        shutil.copytree(os.path.join(REPO_ROOT, "testboard"),
                        os.path.join(tree, "testboard"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(tree, "testboard", "storage.py"), "a",
                  encoding="utf-8") as handle:
            handle.write(_PLANT)
        return tree

    def test_the_shape(self) -> None:
        result = ab.run(REPO_ROOT, REPO_ROOT, self.db,
                        tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                        calls=4, known=False,
                        page_modes=["open-actions", "home"])
        pages = dict((page.name, page)
                     for page in getattr(result, "page_rows"))
        self.assertEqual(sorted(pages), ["home", "open-actions"])
        for name, page in pages.items():
            self.assertEqual(page.requests,
                             [label for label, _ in ab.FIRST_PAINT[name]])
            self.assertEqual((page.status_a, page.status_b), (200, 200))
            self.assertEqual(page.workers, 2)
            self.assertEqual(len(page.a_ms), 4)
            for side_ms, legs in ((page.a_ms, page.legs_a),
                                  (page.b_ms, page.legs_b)):
                self.assertEqual(len(legs), len(page.requests))
                # The page waits for its last answer, no longer.
                for load, page_ms in enumerate(side_ms):
                    self.assertAlmostEqual(
                        page_ms, max(leg[load] for leg in legs), places=6)
            self.assertGreater(page.statements_a, 0)
            self.assertIn(ab.waits_for(page, 1)[0], page.requests)
        text = "\n".join(ab.report(result))
        self.assertIn("page home (2 workers)", text)
        self.assertIn("waits for", text)

    def test_a_planted_slow_request_is_what_the_page_waits_for(self) -> None:
        before = sys.modules.get("testboard.storage")
        result = ab.run(REPO_ROOT, self._planted_tree(), self.db,
                        tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                        calls=8, known=False, page_modes=["home"])
        page = getattr(result, "page_rows")[0]
        verdict = ab.judge_page(page)
        self.assertTrue(verdict.call.startswith("SLOWER"), verdict)
        self.assertGreater(verdict.delta, 15.0)
        self.assertEqual(ab.waits_for(page, 1)[0], "browse")
        self.assertIs(sys.modules.get("testboard.storage"), before)

    def test_a_failing_handler_is_a_status_not_a_timing(self) -> None:
        """A handler that raises is a 500 from ``handle_api`` (it catches,
        as the server does): the page is reported by that status with no
        timing, as a row is. An exception that escapes the handler fails
        the run instead (``PoolTest``)."""
        before = sys.modules.get("testboard.storage")
        tree = _planted_copy(self.scratch, "raising-page", _RAISE)
        result = ab.run(REPO_ROOT, tree, self.db,
                        tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                        calls=2, known=False, page_modes=["home"])
        page = getattr(result, "page_rows")[0]
        self.assertEqual((page.status_a, page.status_b), (200, 500))
        self.assertEqual(ab.judge_page(page).call, "status 200/500")
        line = [text for text in ab.report(result)
                if text.startswith("page home")][0]
        self.assertIn("status 200/500", line)
        self.assertIs(sys.modules.get("testboard.storage"), before)

    def test_a_raising_request_fails_the_run_with_its_text(self) -> None:
        """An exception that escapes the handler, raised in a worker,
        ends the run naming the request and carrying its text."""
        before = sys.modules.get("testboard.storage")
        tree = _planted_copy(self.scratch, "escaping-page", "")
        with open(os.path.join(tree, "testboard", "api.py"), "a",
                  encoding="utf-8") as handle:
            handle.write(_ESCAPE)
        with self.assertRaises(ValueError) as caught:
            ab.run(REPO_ROOT, tree, self.db,
                   tempfile.mkdtemp(dir=self.scratch), ("A", "B"),
                   calls=2, known=False, page_modes=["home"])
        text = str(caught.exception)
        self.assertIn("browse of home on B", text)
        self.assertIn("planted: escaped the handler", text)
        self.assertIs(sys.modules.get("testboard.storage"), before)


class PoolTest(unittest.TestCase):
    """The page mode's pool: one setup job per worker, and an exception
    in a worker fails the load with its text, never a status 0."""

    def test_every_worker_runs_the_setup_once(self) -> None:
        pool = ab.Pool(3)
        seen = []  # type: List[int]

        def record() -> None:
            seen.append(ab.threading.get_ident())

        try:
            pool.on_every_worker(record)
        finally:
            pool.stop()
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)

    def test_an_exception_fails_the_load_with_its_text(self) -> None:
        pool = ab.Pool(2)

        def fine() -> int:
            return 200

        def broken() -> int:
            raise RuntimeError("the text of the failure")

        try:
            with self.assertRaises(ValueError) as caught:
                pool.load([fine, broken], ["fine", "the broken one"])
            # The pool is still usable after a failed load.
            self.assertEqual(pool.load([fine, fine])[2], [200, 200])
        finally:
            pool.stop()
        self.assertIn("the broken one", str(caught.exception))
        self.assertIn("the text of the failure", str(caught.exception))

    def test_a_raising_setup_does_not_leave_workers_waiting(self) -> None:
        pool = ab.Pool(3)
        calls = []  # type: List[int]

        def setup() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("setup failed")

        try:
            with self.assertRaises(ValueError) as caught:
                pool.on_every_worker(setup)
        finally:
            pool.stop()
        self.assertIn("setup failed", str(caught.exception))


class NamedMemoTest(unittest.TestCase):
    """The cold sweep clears ``_cadence_runs_cache`` by name, whatever
    its type, and does not fail on a tree that has none."""

    class _Memo(object):
        """Not a dict: the generic sweep would not see it."""

        def __init__(self) -> None:
            self.entries = {"k": 1}

        def clear(self) -> None:
            self.entries = {}

    class _Store(object):
        def __init__(self) -> None:
            self.invalidated = 0

        def _invalidate_summary_cache(self) -> None:
            self.invalidated += 1

    def test_cleared_by_name_when_present(self) -> None:
        store = self._Store()
        memo = self._Memo()
        setattr(store, "_cadence_runs_cache", memo)
        ab.clear_memos(store)
        self.assertEqual(memo.entries, {})
        self.assertEqual(store.invalidated, 1)

    def test_absent_is_fine(self) -> None:
        store = self._Store()
        ab.clear_memos(store)
        self.assertEqual(store.invalidated, 1)

    def test_a_named_memo_that_cannot_be_cleared_is_refused(self) -> None:
        store = self._Store()
        setattr(store, "_cadence_runs_cache", object())
        with self.assertRaises(ValueError):
            ab.clear_memos(store)


class AnnotationsTest(unittest.TestCase):
    """The harness is not a package, so the compat sweep parses it but
    cannot import it; evaluate its annotations here."""

    def test_every_function_annotation_evaluates(self) -> None:
        checked = 0
        for module in (seed, ab, mariadb_load):
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
