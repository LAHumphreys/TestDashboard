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
from typing import List, Tuple

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
                      pages=pages, calls=6, known=False)

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
        self.assertFalse(rows["headline"].call.startswith("SLOWER"),
                         rows["headline"])
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
            conn.set_trace_callback(seen.append)
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
