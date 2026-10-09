"""Build the production-scale seed database the A/B runner measures on.

    python tools/dev/perf/seed.py --out <scratch>/perf.db
    python tools/dev/perf/seed.py --out <scratch>/perf.db --tree <checkout>

The shape is the largest product's, as production has it: about 12,000
tests over five environments (the largest 8,000), run SEQUENTIALLY and
hours apart, several nights of mainline history, and one full-size build
("nightly/full") that runs every one of those tests again each night
beside mainline. Around it: three smaller builds of the same product and
a second, small product, so a product scope and an unscoped request
differ. Comments, no assignments (the runner adds those itself, as a
measured state). See README.md for what it is NOT: a year of history.

Deterministic: one seeded ``random.Random``, fixed dates, no wall clock
in anything the runner reads. Two builds of the same arguments with the
same tree give the same rows (``fingerprint`` below is what the test
compares).

It writes through ``Storage.upsert_runs`` of the tree it is given, so
the derived tables are exactly what that tree's import maintains. Build
the seed with the BASE tree of an A/B: a database is refused by code
older than its schema, never by newer code (which migrates its copy).

Never writes inside a checkout: the repo-root ``testboard.db`` is dev
data at a quarter of production and opening it with current code
migrates it.
"""

import argparse
import datetime
import hashlib
import os
import random
import sqlite3
import sys
import urllib.parse
from typing import Dict, List, NamedTuple, Optional, Sequence, TextIO, Tuple

#: Bump when the recipe changes, so a number can say which seed it is from.
RECIPE = "perf-seed/1"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

#: The newest mainline night. The runner's clock is derived from the data.
NEWEST_NIGHT = datetime.datetime(2026, 9, 28)

#: (product, environment, tests at scale 1.0), in the order they run.
ESTATE = [
    ("Atlas", "atlas-lab-alpha", 8000),
    ("Atlas", "atlas-lab-bravo", 1600),
    ("Atlas", "atlas-lab-charlie", 1000),
    ("Atlas", "atlas-lab-delta", 800),
    ("Atlas", "atlas-lab-echo", 600),
    ("Beacon", "beacon-stage", 500),
    ("Beacon", "beacon-perf", 300),
]  # type: List[Tuple[str, str, int]]

FULL_BUILD = "nightly/full"
USERS = ["amy", "ben", "chen", "dana"]
BATCH = 2000


def ro_uri(path: str) -> str:
    """A read-only SQLite URI for *path*, percent-quoted so a ``?`` or
    ``#`` in it is part of the name rather than the query."""
    return "file:{}?mode=ro".format(urllib.parse.quote(
        os.path.abspath(path).replace("\\", "/"), safe="/:"))


class TestSpec(NamedTuple):
    """One test and how it behaves, fixed when the estate is drawn."""

    product: str
    environment: str
    script: str
    test_name: str
    kind: str          # stable | broken | fixed | flaky | expected
    turn_night: int    # broken: fails from here on; fixed: passes from here


def refuse_inside_checkout(path: str) -> None:
    """Raise ValueError if *path* lies inside a testboard checkout.

    A checkout is recognised by ``run_server.py`` beside a ``testboard``
    package. That covers the repo-root ``testboard.db``, every worktree,
    and ``.scratch/``: the seed is scratch output and belongs in a temp
    directory.
    """
    folder = os.path.dirname(os.path.abspath(path))
    while True:
        if (os.path.isfile(os.path.join(folder, "run_server.py"))
                and os.path.isdir(os.path.join(folder, "testboard"))):
            raise ValueError(
                "refusing to write {} inside the checkout at {}: build the "
                "seed in a temp directory".format(path, folder))
        parent = os.path.dirname(folder)
        if parent == folder:
            return
        folder = parent


def draw_estate(rng: random.Random, scale: float, nights: int
                ) -> List[TestSpec]:
    """Every test of the estate, with its behaviour, in run order."""
    tests = []  # type: List[TestSpec]
    for product, environment, full_count in ESTATE:
        count = max(20, int(round(full_count * scale)))
        made = 0
        script_no = 0
        while made < count:
            script_no += 1
            area = "area{:02d}".format(script_no % 37)
            script = "suite/{}/{}/test_{:04d}.py".format(
                product.lower(), area, script_no)
            for case in range(min(rng.randint(8, 40), count - made)):
                roll = rng.random()
                if roll < 0.010:
                    kind = "broken"
                elif roll < 0.013:
                    kind = "fixed"
                elif roll < 0.020:
                    kind = "flaky"
                elif roll < 0.025:
                    kind = "expected"
                else:
                    kind = "stable"
                tests.append(TestSpec(
                    product=product, environment=environment, script=script,
                    test_name="test_{}_{:03d}".format(area, case),
                    kind=kind, turn_night=rng.randint(1, max(1, nights - 1))))
                made += 1
    return tests


def mainline_result(rng: random.Random, spec: TestSpec, night: int) -> str:
    """The mainline result of *spec* on *night*, by its kind."""
    if spec.kind == "broken":
        return "FAIL" if night >= spec.turn_night else "PASS"
    if spec.kind == "fixed":
        return "PASS" if night >= spec.turn_night else "FAIL"
    if spec.kind == "flaky":
        return "FAIL" if rng.random() < 0.25 else "PASS"
    if spec.kind == "expected":
        return ("UNEXPECTED_PASS" if rng.random() < 0.05
                else "FAILED_AS_EXPECTED")
    return "FAIL" if rng.random() < 0.002 else "PASS"


def output_for(spec: TestSpec, result: str, night: int) -> str:
    """A plausible output body: a traceback for a failure, a line else."""
    if result == "FAIL":
        frames = "".join(
            '  File "{}", line {}, in {}\n'.format(
                spec.script, 40 + 7 * depth, spec.test_name)
            for depth in range(12))
        return ("Traceback (most recent call last):\n" + frames
                + "AssertionError: {} night {} expected 200, got 503\n"
                .format(spec.test_name, night))
    if result in ("FAILED_AS_EXPECTED", "UNEXPECTED_PASS"):
        return "known issue BUG-{}\n".format(len(spec.test_name) * 97)
    return "ok\n"


def records_for(
    model: object, rng: random.Random, tests: Sequence[TestSpec],
    start: datetime.datetime, night: int, build: Optional[str],
    results: Dict[Tuple[str, str, str], str],
) -> Tuple[List[object], datetime.datetime]:
    """RunRecords for *tests* run back to back from *start*.

    *results* maps a triple to its result; a triple missing from it is
    drawn from its kind and written back (that is mainline). Returns the
    records and the time the last one ended.
    """
    run_record = getattr(model, "RunRecord")
    result_enum = getattr(model, "Result")
    records = []  # type: List[object]
    clock = start
    for spec in tests:
        triple = (spec.environment, spec.script, spec.test_name)
        result = results.get(triple)
        if result is None:
            result = mainline_result(rng, spec, night)
            results[triple] = result
        duration = datetime.timedelta(seconds=rng.uniform(0.4, 2.0))
        records.append(run_record(
            environment=spec.environment, script=spec.script,
            test_name=spec.test_name, result=result_enum[result],
            start_time=clock, end_time=clock + duration,
            output=output_for(spec, result, night),
            source_link="https://git.example.com/{}/{}#L40".format(
                spec.product.lower(), spec.script),
            known_failure_reason=(
                "BUG-1234" if spec.kind == "expected" else None),
            build=build))
        clock += datetime.timedelta(
            microseconds=int(duration.total_seconds() * 600000) + 1)
    return records, clock


def build(out: str, tree: Optional[str] = None, scale: float = 1.0,
          nights: int = 7, seed: int = 20261003,
          log: Optional[TextIO] = None) -> Dict[str, int]:
    """Write the seed to *out* (which must not exist) and return counts.

    *tree* is the checkout whose ``testboard`` writes it; ``None`` uses
    whichever ``testboard`` is importable already (the tests do that).
    """
    refuse_inside_checkout(out)
    if os.path.exists(out):
        raise ValueError("{} exists; remove it first".format(out))
    if nights < 2:
        raise ValueError("nights must be at least 2")
    if tree is not None:
        sys.path.insert(0, os.path.abspath(tree))
    from testboard import model
    from testboard.storage import Storage

    def say(text: str) -> None:
        if log is not None:
            print(text, file=log)

    rng = random.Random(seed)
    tests = draw_estate(rng, scale, nights)
    by_env = {}  # type: Dict[str, List[TestSpec]]
    for spec in tests:
        by_env.setdefault(spec.environment, []).append(spec)
    store = Storage(out)
    declared_at = NEWEST_NIGHT - datetime.timedelta(days=nights + 1)
    for product, environment, _count in ESTATE:
        store.set_environment_product(environment, product, "amy", declared_at)
    for user in USERS:
        store.ensure_user(user, declared_at)

    def write(records: List[object], label: str) -> None:
        for at in range(0, len(records), BATCH):
            store.upsert_runs(records[at:at + BATCH])  # type: ignore
        say("  {:40s} {:7d} runs".format(label, len(records)))

    mainline = {}  # type: Dict[int, Dict[Tuple[str, str, str], str]]
    full_nights = min(5, nights)
    for night in range(nights):
        day = NEWEST_NIGHT - datetime.timedelta(days=nights - 1 - night)
        mainline[night] = {}
        clock = day + datetime.timedelta(minutes=30)
        starts = {}  # type: Dict[str, datetime.datetime]
        for _product, environment, _count in ESTATE:
            starts[environment] = clock
            records, ended = records_for(
                model, rng, by_env[environment], clock, night, None,
                mainline[night])
            write(records, "{} mainline {}".format(day.date(), environment))
            clock = ended + datetime.timedelta(minutes=20)
        if night >= nights - full_nights:
            # The full-size build: every Atlas test again, twelve hours
            # after mainline's own slot, mostly agreeing with it.
            for product, environment, _count in ESTATE:
                if product != "Atlas":
                    continue
                agreed = {}  # type: Dict[Tuple[str, str, str], str]
                for spec in by_env[environment]:
                    triple = (spec.environment, spec.script, spec.test_name)
                    result = mainline[night][triple]
                    roll = rng.random()
                    if roll < 0.004:
                        result = "FAIL"
                    elif roll < 0.006 and result == "FAIL":
                        result = "PASS"
                    agreed[triple] = result
                records, _ended = records_for(
                    model, rng, by_env[environment],
                    starts[environment] + datetime.timedelta(hours=12),
                    night, FULL_BUILD, agreed)
                write(records, "{} {} {}".format(
                    day.date(), FULL_BUILD, environment))
        if night >= nights - 3:
            # A long-running branch over 60% of the largest environment.
            pool = by_env["atlas-lab-alpha"][:int(
                len(by_env["atlas-lab-alpha"]) * 0.6)]
            branch = {}  # type: Dict[Tuple[str, str, str], str]
            for spec in pool:
                triple = (spec.environment, spec.script, spec.test_name)
                branch[triple] = (
                    "FAIL" if rng.random() < 0.004
                    else mainline[night][triple])
            records, _ended = records_for(
                model, rng, pool, starts["atlas-lab-alpha"]
                + datetime.timedelta(hours=15), night,
                "feature/checkout-rewrite", branch)
            write(records, "{} feature/checkout-rewrite".format(day.date()))
        for name, at_night in (("2026.9.0", nights - 3),
                               ("2026.9.1", nights - 1)):
            if night != at_night:
                continue
            pool = by_env["atlas-lab-bravo"][:int(
                len(by_env["atlas-lab-bravo"]) * 0.7)]
            release = {}  # type: Dict[Tuple[str, str, str], str]
            for spec in pool:
                triple = (spec.environment, spec.script, spec.test_name)
                release[triple] = mainline[night][triple]
            records, _ended = records_for(
                model, rng, pool, starts["atlas-lab-bravo"]
                + datetime.timedelta(hours=18), night, name, release)
            write(records, "{} {}".format(day.date(), name))

    # Comments on today's mainline failures, and a few from the build.
    last = nights - 1
    failing = [
        spec for spec in tests
        if mainline[last][(spec.environment, spec.script, spec.test_name)]
        == "FAIL"]
    full_id = None  # type: Optional[int]
    for stream in store.list_streams("Atlas"):
        if stream.name == FULL_BUILD:
            full_id = stream.stream_id
    when = NEWEST_NIGHT + datetime.timedelta(hours=20)
    for index, spec in enumerate(failing[:200]):
        store.add_comment(
            spec.environment, spec.script, spec.test_name,
            USERS[index % len(USERS)],
            "looked at this: {} on night {}".format(spec.kind, last),
            when + datetime.timedelta(seconds=index),
            stream_id=(full_id if index % 7 == 0 and spec.product == "Atlas"
                       else None))
    store.close()
    checkpoint = sqlite3.connect(out)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    counts, _digest = fingerprint(out)
    return counts


def fingerprint(path: str) -> Tuple[Dict[str, int], str]:
    """Row counts of every table, and a digest of runs and latest_runs.

    What the determinism test compares: two builds of the same recipe
    must agree on both.
    """
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        counts = {}  # type: Dict[str, int]
        for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            counts[table] = conn.execute(
                "SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
        digest = hashlib.sha256()
        for row in conn.execute(
                "SELECT stream_id, environment, script, test_name, "
                "start_time, result FROM runs ORDER BY id"):
            digest.update(repr(tuple(row)).encode("utf-8"))
        for row in conn.execute(
                "SELECT * FROM latest_runs ORDER BY stream_id, environment, "
                "script, test_name"):
            digest.update(repr(tuple(row)).encode("utf-8"))
        return counts, digest.hexdigest()
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True,
                        help="database to create (outside any checkout)")
    parser.add_argument("--tree", default=REPO_ROOT,
                        help="checkout whose testboard writes the seed "
                             "(default: this one); use the A/B's base")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="tests per environment x this (tests use 0.03)")
    parser.add_argument("--nights", type=int, default=7,
                        help="mainline nights (the full build runs the "
                             "last five of them)")
    parser.add_argument("--seed", type=int, default=20261003)
    args = parser.parse_args(argv)
    try:
        counts = build(args.out, args.tree, args.scale, args.nights,
                       args.seed, log=sys.stdout)
    except ValueError as exc:
        print("seed.py: {}".format(exc), file=sys.stderr)
        return 2
    print("{} scale={} nights={} -> {}".format(
        RECIPE, args.scale, args.nights, args.out))
    for table in sorted(counts):
        print("  {:28s} {:9d}".format(table, counts[table]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
