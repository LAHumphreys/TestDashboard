"""Build the production-scale seed database the A/B runner measures on.

    python tools/dev/perf/seed.py --out <scratch>/perf.db
    python tools/dev/perf/seed.py --out <scratch>/perf.db --tree <checkout>
    python tools/dev/perf/seed.py --out <scratch>/h90.db --history-nights 90
    python tools/dev/perf/seed.py --onto <scratch>/copy.db --assignments 1000
    python tools/dev/perf/seed.py --onto <scratch>/copy.db --assignments 65 \
        --marks 50 --tree <checkout at migration 12 or later>

The shape is the largest product's, as production has it: about 12,000
tests over five environments (the largest 8,000), run SEQUENTIALLY and
hours apart, several nights of mainline history, and one full-size build
("nightly/full") that runs every one of those tests again each night
beside mainline. Around it: three smaller builds of the same product and
a second, small product, so a product scope and an unscoped request
differ. Comments, no assignments (the runner adds those itself, as a
measured state). See README.md for what it is NOT: a year of history.

``--history-nights N`` builds the variant ``perf-seed/1-hN``: the same
seed with N - 7 older nights of MAINLINE for the largest environment
written before it, so a request that reads a window of ``runs`` can be
timed over N days. The history has its own random stream: the last seven
nights are perf-seed/1's runs exactly.

``--assignments N`` (with ``--onto`` an existing seed) writes the
assignments fixture: N current assignments in the largest environment, 10
of them live mutes. ``ab.py --assign N`` applies the same fixture to its
own copies instead, through each tree's own Storage.

``--marks N`` (with ``--onto``, alone or after ``--assignments``) writes
the marks fixture: N flaky marks in the largest environment through the
tree's own ``Storage.mark_flaky``, cadences 2 to 10, with its companions
(one marked test muted, one assigned, two retired). ``ab.py --marks N``
applies it to its own copies. README.md says what it holds at 0, 50 and
1,000 on both seeds.

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
from typing import (Callable, Dict, List, NamedTuple, Optional, Sequence,
                    Set, TextIO, Tuple)

#: Bump when the recipe changes, so a number can say which seed it is from.
RECIPE = "perf-seed/1"

#: Nights in one block of the history: each block redraws the largest
#: environment's behaviours, so 90 nights carry the week's flips and mix.
HISTORY_BLOCK = 7

#: The assignments fixture's live mutes, whatever its size.
FIXTURE_MUTES = 10

#: The marks fixture's cadences, by a candidate's place in the design-time
#: order (docs/design/flaky-mark-r0.md §2 B.5): that fixture's cycle of
#: eight, except that its 10-slot takes 10, 6, 7, 8 and 9 in turn, so
#: every cadence from 2 to 10 is marked and the shares of 2, 3, 4 and 5
#: are the design's.
MARK_CYCLE = [3, 3, 2, 5, 3, 4, 10, 3]
MARK_ROTATION = [10, 6, 7, 8, 9]

#: The prefix of the marked order the companions are chosen from, so they
#: are marked at every N from it up: the design's smaller size.
MARK_CORE = 50

#: Marked tests the fixture retires (failing ones, from the core).
MARK_RETIRED = 2

#: The assignments fixture at the reference estate's figure (``ab.py
#: --assign 65``, the Known slow row's ``assign_65``): the design-time
#: marks were measured on top of it.
REFERENCE_ASSIGNMENTS = 65

MARK_REASON = "perf fixture: accepted as flaky"
MARK_USER = "amy"

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


def kind_for(roll: float) -> str:
    """A test's behaviour from one uniform draw: the recipe's mix."""
    if roll < 0.010:
        return "broken"
    if roll < 0.013:
        return "fixed"
    if roll < 0.020:
        return "flaky"
    if roll < 0.025:
        return "expected"
    return "stable"


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
                kind = kind_for(rng.random())
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


def recipe_name(nights: int, history_nights: int) -> str:
    """The name a number quotes: perf-seed/1, or the -hN variant."""
    if history_nights > nights:
        return "{}-h{}".format(RECIPE, history_nights)
    return RECIPE


def write_history(
    model: object, store: object, specs: Sequence[TestSpec], seed: int,
    nights: int, history_nights: int, say: Callable[[str], None],
) -> None:
    """The variant's older mainline nights of one environment, oldest
    first, before the seed's own *nights*.

    Its own ``random.Random`` (derived from *seed*), so the seed's
    stream, and with it every run of the last *nights*, is untouched.
    Every :data:`HISTORY_BLOCK` nights the behaviours are redrawn with
    the recipe's mix, broken and fixed tests turning inside the block,
    so each week of history looks like the seed's week. Mainline only:
    see README.md for why the builds are not extended.
    """
    rng = random.Random(seed * 7919 + history_nights)
    extra = history_nights - nights
    block = []  # type: List[TestSpec]
    for night in range(extra):
        if night % HISTORY_BLOCK == 0:
            block = [spec._replace(
                kind=kind_for(rng.random()),
                turn_night=rng.randint(1, HISTORY_BLOCK - 1))
                for spec in specs]
        day = NEWEST_NIGHT - datetime.timedelta(
            days=history_nights - 1 - night)
        results = {}  # type: Dict[Tuple[str, str, str], str]
        for spec in block:
            results[(spec.environment, spec.script, spec.test_name)] = (
                mainline_result(rng, spec, night % HISTORY_BLOCK))
        records, _ended = records_for(
            model, rng, block, day + datetime.timedelta(minutes=30),
            night, None, results)
        for at in range(0, len(records), BATCH):
            getattr(store, "upsert_runs")(records[at:at + BATCH])
        say("  {:40s} {:7d} runs".format(
            "{} history {}".format(day.date(), specs[0].environment),
            len(records)))


def build(out: str, tree: Optional[str] = None, scale: float = 1.0,
          nights: int = 7, seed: int = 20261003,
          log: Optional[TextIO] = None,
          history_nights: int = 0) -> Dict[str, int]:
    """Write the seed to *out* (which must not exist) and return counts.

    *tree* is the checkout whose ``testboard`` writes it; ``None`` uses
    whichever ``testboard`` is importable already (the tests do that).
    *history_nights* above *nights* builds the ``-hN`` variant: the
    largest environment's mainline goes back that many nights.
    """
    refuse_inside_checkout(out)
    if os.path.exists(out):
        raise ValueError("{} exists; remove it first".format(out))
    if nights < 2:
        raise ValueError("nights must be at least 2")
    if history_nights and history_nights <= nights:
        raise ValueError("history nights ({}) must exceed nights ({})".format(
            history_nights, nights))
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
    declared_at = NEWEST_NIGHT - datetime.timedelta(
        days=max(nights, history_nights) + 1)
    for product, environment, _count in ESTATE:
        store.set_environment_product(environment, product, "amy", declared_at)
    for user in USERS:
        store.ensure_user(user, declared_at)

    def write(records: List[object], label: str) -> None:
        for at in range(0, len(records), BATCH):
            store.upsert_runs(records[at:at + BATCH])  # type: ignore
        say("  {:40s} {:7d} runs".format(label, len(records)))

    if history_nights:
        write_history(model, store, by_env[ESTATE[0][1]], seed, nights,
                      history_nights, say)
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


def seed_clock(path: str) -> datetime.datetime:
    """The clock a seed is read at: two hours past the hour of its last
    run's end (``ab.derive_scope`` derives the same)."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        last = conn.execute("SELECT MAX(end_time) FROM runs").fetchone()[0]
    finally:
        conn.close()
    if last is None:
        raise ValueError("no runs in {}".format(path))
    ended = datetime.datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")
    return (ended.replace(minute=0, second=0)
            + datetime.timedelta(hours=2))


def fixture_triples(path: str, environment: str, count: int
                    ) -> Tuple[List[Tuple[str, str, str]],
                               List[Tuple[str, str, str]]]:
    """The assignments fixture's *count* triples and its mutes, in order.

    Order: the environment's current mainline failures by (script,
    test_name), so the first 65 are exactly the Known slow row's
    ``assign_65``; then tests that failed on mainline before, most
    recent failure first (on the -hN variant there are thousands); then
    the rest. Mutes: :data:`FIXTURE_MUTES` of the selection, spread
    evenly over its current failures (over the selection when it has
    fewer). Read-only on *path*.
    """
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        failing = [(str(row[0]), str(row[1]), str(row[2]))
                   for row in conn.execute(
            "SELECT environment, script, test_name FROM latest_runs "
            "WHERE stream_id = 1 AND environment = ? AND result = 'FAIL' "
            "ORDER BY script, test_name", (environment,)).fetchall()]
        failed_before = [(str(row[0]), str(row[1]), str(row[2]))
                         for row in conn.execute(
            "SELECT environment, script, test_name, MAX(start_time) AS last "
            "FROM runs WHERE stream_id = 1 AND environment = ? "
            "AND result = 'FAIL' GROUP BY environment, script, test_name "
            "ORDER BY last DESC, script, test_name",
            (environment,)).fetchall()]
        rest = [(str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
            "SELECT environment, script, test_name FROM latest_runs "
            "WHERE stream_id = 1 AND environment = ? "
            "ORDER BY script, test_name", (environment,)).fetchall()]
    finally:
        conn.close()
    chosen = []  # type: List[Tuple[str, str, str]]
    seen = set()  # type: Set[Tuple[str, str, str]]
    for triple in failing + failed_before + rest:
        if len(chosen) >= count:
            break
        if triple not in seen:
            seen.add(triple)
            chosen.append(triple)
    if len(chosen) < count:
        raise ValueError("{} has {} tests, fewer than {} assignments".format(
            environment, len(chosen), count))
    failing_set = set(failing)
    now_failing = [t for t in chosen if t in failing_set]
    pool = now_failing if len(now_failing) >= FIXTURE_MUTES else chosen
    mutes = []  # type: List[Tuple[str, str, str]]
    if count >= FIXTURE_MUTES:
        step = float(len(pool)) / FIXTURE_MUTES
        mutes = [pool[int(index * step)] for index in range(FIXTURE_MUTES)]
    return chosen, mutes


def apply_fixture(store: object, path: str, environment: str, count: int,
                  now: datetime.datetime) -> Tuple[int, int]:
    """Write the assignments fixture through *store* (any tree's
    ``Storage`` on *path*): *count* tests of *environment* assigned to
    amy, :data:`FIXTURE_MUTES` of them muted on mainline for a week from
    *now*. Returns (assigned, muted); muted is 0 on a tree without
    mutes. Idempotent over ``ab.assign_65``'s 65: same triples, same
    assignee, so the result is *count* current assignments either way.
    """
    chosen, mutes = fixture_triples(path, environment, count)
    assign = getattr(store, "bulk_set_assignee_for_triples")
    assign("amy", "amy", now,
           [(triple[0], triple[1], triple[2], None) for triple in chosen])
    mute = getattr(store, "mute_tests", None)
    if mute is None or not mutes:
        return len(chosen), 0
    mute([(triple[0], triple[1], triple[2], 1) for triple in mutes],
         "perf fixture", 168, "amy", "amy", now)
    return len(chosen), len(mutes)


# ----------------------------------------------------------------------
# The marks fixture (WP-44 item 4)
# ----------------------------------------------------------------------

Triple = Tuple[str, str, str]


class MarkState(NamedTuple):
    """A marked test on one stream, by the rule of the design's §2 B.1."""

    state: str            # passing | flaking | failing
    streak: int           # FAILs ending at the latest run, capped at N
    runs: Optional[int]   # runs the stream has, capped at N, when failing
    promoted: bool        # over its cadence on this run, not the one before
    candidate: bool       # passing after a FAIL (decision 3's candidates)
    real_fix: bool        # a candidate that had been over its cadence


class MarksPlan(NamedTuple):
    """Which tests the marks fixture marks, at which cadence, and its
    companions. Independent of N: ``marks[:N]`` is the fixture at N."""

    marks: List[Tuple[Triple, int]]
    fillers: List[Triple]
    muted: Optional[Triple]
    assigned: Optional[Triple]
    retired: List[Triple]


class MarksDone(NamedTuple):
    """What ``apply_marks`` left in the copy: marks written, and the
    companions present (written now, or there already)."""

    marks: int
    muted: int
    assigned: int
    retired: int


class MarkCount(NamedTuple):
    """The marks fixture's census on one stream (``mark_census``)."""

    stream: str
    marks: int             # marked tests with a row on the stream
    retired: int
    flaky_total: int       # every mark in scope: marks - retired
    passing: int
    candidates: int        # of passing: after a FAIL; unmuted, unretired
    real_fixes: int        # of candidates: had been over the cadence
    flaking: int
    failing: int
    failing_short: int     # of failing: every run a FAIL, fewer than N
    promoted: int          # P: promoted, unmuted, unretired
    f: int                 # F: flaking, under no live mute
    x: int                 # X: F with no assignee, unretired
    muted: int             # marked and under a live mute here
    assigned: int          # marked and assigned
    cadences: List[int]    # the distinct cadences in scope


def mark_cadence(index: int) -> int:
    """The cadence of the candidate at *index* of the design-time order."""
    slot = index % len(MARK_CYCLE)
    if MARK_CYCLE[slot] == 10:
        return MARK_ROTATION[(index // len(MARK_CYCLE)) % len(MARK_ROTATION)]
    return MARK_CYCLE[slot]


def mark_state(newest_first: Sequence[str], cadence: int) -> MarkState:
    """§2 B.1 on a stream's newest runs of one test (results, newest
    first, at least N+1 of them where the stream has them).

    A failure is ``FAIL`` and nothing else. Latest not a FAIL: passing,
    a candidate when the run before failed, a real fix when runs 2 to N+1
    (or all it had) failed. Latest a FAIL with none before: failing, as
    today. A FAIL after anything else: flaking. Two FAILs running:
    failing when its newest N (or all it has, fewer) are FAILs, promoted
    when run N+1 exists and is not; flaking otherwise.
    """
    results = list(newest_first)
    if not results:
        raise ValueError("a marked test with no run on the stream has no "
                         "state there")
    if results[0] != "FAIL":
        candidate = len(results) > 1 and results[1] == "FAIL"
        window = results[1:cadence + 1]
        real = candidate and all(r == "FAIL" for r in window)
        return MarkState("passing", 0, None, False, candidate, real)
    if len(results) == 1:
        return MarkState("failing", 1, 1, False, False, False)
    if results[1] != "FAIL":
        return MarkState("flaking", 1, None, False, False, False)
    head = results[:cadence]
    if all(r == "FAIL" for r in head):
        promoted = len(results) > cadence and results[cadence] != "FAIL"
        return MarkState("failing", len(head), len(head), promoted,
                         False, False)
    streak = 0
    for result in head:
        if result != "FAIL":
            break
        streak += 1
    return MarkState("flaking", streak, None, False, False, False)


def _newest(conn: sqlite3.Connection, stream_id: int, triple: Triple,
            limit: int) -> List[str]:
    return [str(row[0]) for row in conn.execute(
        "SELECT result FROM runs WHERE environment = ? AND script = ? "
        "AND test_name = ? AND stream_id = ? "
        "ORDER BY start_time DESC, id DESC LIMIT ?",
        (triple[0], triple[1], triple[2], stream_id, limit)).fetchall()]


def marks_order(path: str, environment: str) -> List[Triple]:
    """Every mainline test of *environment* in the design-time order
    (§2 B.5's script): failing now first, then by mainline failures over
    the whole history, most first, then by (script, test). Read-only."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        return [(str(row[0]), str(row[1]), str(row[2])) for row in
                conn.execute(
            "SELECT lr.environment, lr.script, lr.test_name "
            "FROM latest_runs AS lr WHERE lr.stream_id = 1 "
            "AND lr.environment = ? "
            "ORDER BY (lr.result = 'FAIL') DESC, "
            "(SELECT COUNT(*) FROM runs AS r "
            " WHERE r.environment = lr.environment AND r.script = lr.script"
            " AND r.test_name = lr.test_name AND r.stream_id = 1 "
            " AND r.result = 'FAIL') DESC, lr.script, lr.test_name",
            (environment,)).fetchall()]
    finally:
        conn.close()


def plan_marks(path: str, environment: str) -> MarksPlan:
    """The marks fixture for *environment*, at every N at once.

    The order is the design-time fixture's (``marks_order``), each test
    keeping that order's cadence (``mark_cadence``), with up to three
    **fillers** moved to the front so every state of the rule is marked
    from N = 3: the first test passing after a pass, the first passing
    after a tolerated failure and the first real fix. On both seeds the
    fillers lie inside the design's first 1,000, so the 1,000 marks are
    the design's set exactly; at :data:`MARK_CORE` they replace its last
    plainly failing tests (failing, not promoted), which follow it.

    Companions, from the first :data:`MARK_CORE` marks on mainline: the
    first flaking test the reference assignments fixture mutes (else the
    first flaking one) is muted; the next flaking one it assigns (else
    the next flaking one) is assigned; the first :data:`MARK_RETIRED`
    plainly failing tests are retired. So F, X and P with ``--assign 65``
    are the design's. Read-only.
    """
    order = marks_order(path, environment)
    cadence = dict((triple, mark_cadence(index))
                   for index, triple in enumerate(order))
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        def state_of(triple: Triple) -> MarkState:
            return mark_state(_newest(conn, 1, triple, cadence[triple] + 1),
                              cadence[triple])

        states = {}  # type: Dict[Triple, MarkState]
        for triple in order[:MARK_CORE]:
            states[triple] = state_of(triple)
        wanted = ["pass", "tolerated", "fix"]
        found = {}  # type: Dict[str, Triple]
        for triple in order:
            if len(found) == len(wanted):
                break
            state = states.get(triple) or state_of(triple)
            if state.state != "passing":
                continue
            kind = ("fix" if state.real_fix else
                    "tolerated" if state.candidate else "pass")
            if kind not in found:
                found[kind] = triple
    finally:
        conn.close()
    fillers = [found[kind] for kind in wanted if kind in found]
    core = order[:MARK_CORE]
    # The fillers take the places of the core's last plainly failing
    # tests (failing, not promoted), which follow the core instead: F, X
    # and P at MARK_CORE are the design's, and 1,000 is its set.
    plain = [t for t in core
             if states[t].state == "failing" and not states[t].promoted]
    room = len([t for t in fillers if t not in states])
    displaced = plain[max(0, len(plain) - room):] if room else []
    moved = set(fillers) | set(displaced)
    marked = (fillers + [t for t in core if t not in moved] + displaced
              + [t for t in order[MARK_CORE:] if t not in moved])
    in_core = marked[:MARK_CORE]
    flaking = [t for t in in_core
               if t in states and states[t].state == "flaking"]
    # The companions coincide with the reference assignments fixture's
    # (65 assigned, 10 muted) where they can, so --assign 65 composed
    # with the marks adds no assignment or mute the design did not have.
    reference, reference_mutes = fixture_triples(
        path, environment, min(REFERENCE_ASSIGNMENTS, len(order)))
    muted = _first([t for t in flaking if t in set(reference_mutes)]
                   + flaking)
    assigned = _first([t for t in flaking
                       if t in set(reference) and t != muted]
                      + [t for t in flaking if t != muted])
    retired = [t for t in in_core if t in states and t not in displaced
               and t in set(plain)][:MARK_RETIRED]
    return MarksPlan(
        marks=[(triple, cadence[triple]) for triple in marked],
        fillers=fillers, muted=muted, assigned=assigned, retired=retired)


def _first(items: Sequence[Triple]) -> Optional[Triple]:
    return items[0] if items else None


def has_marks_table(path: str) -> bool:
    """True when the database at *path* has migration 12's marks."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'test_flaky_marks'").fetchone()[0] == 1
    finally:
        conn.close()


def apply_marks(store: object, path: str, environment: str, count: int,
                now: datetime.datetime) -> MarksDone:
    """Write the marks fixture through *store* (any tree's ``Storage`` on
    *path*): the companions first, at every N including 0, so 0, 50 and
    1,000 differ only in the marks; then the first *count* of
    ``plan_marks`` through ``Storage.mark_flaky``, one call per cadence,
    marked by amy at *now*, never by a raw INSERT.

    A companion already in its state (an assignment or a mute the
    assignments fixture wrote) is left as it is, so the fixture composes
    with ``apply_fixture``. Raises ValueError for N > 0 on a tree without
    marks (before migration 12).
    """
    if count < 0:
        raise ValueError("--marks N needs N >= 0")
    mark = getattr(store, "mark_flaky", None)
    if count and mark is None:
        raise ValueError(
            "this tree has no flaky marks (Storage.mark_flaky, migration "
            "12): --marks {} needs both trees at WP-44 item 3 or later; "
            "--marks 0 measures the companions alone".format(count))
    plan = plan_marks(path, environment)
    if count > len(plan.marks):
        raise ValueError("{} has {} tests, fewer than {} marks".format(
            environment, len(plan.marks), count))
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        def present(sql: str, triple: Triple) -> bool:
            return conn.execute(sql, triple).fetchone() is not None

        assigned_sql = (
            "SELECT 1 FROM current_assignments WHERE environment = ? "
            "AND script = ? AND test_name = ? AND assignee IS NOT NULL")
        need_assign = (plan.assigned is not None
                       and not present(assigned_sql, plan.assigned))
        need_mute = (plan.muted is not None and not present(
            "SELECT 1 FROM test_mutes WHERE stream_id = 1 AND "
            "environment = ? AND script = ? AND test_name = ?",
            plan.muted))
        need_retire = [t for t in plan.retired if not present(
            "SELECT 1 FROM test_retirements WHERE environment = ? "
            "AND script = ? AND test_name = ?", t)]
    finally:
        conn.close()
    if need_assign and plan.assigned is not None:
        getattr(store, "bulk_set_assignee_for_triples")(
            MARK_USER, MARK_USER, now,
            [plan.assigned + (None,)])
    if need_mute and plan.muted is not None:
        getattr(store, "mute_tests")(
            [plan.muted + (1,)], "perf fixture: muted and marked", 168,
            MARK_USER, MARK_USER, now)
    for triple in need_retire:
        getattr(store, "set_retired")(
            triple[0], triple[1], triple[2], True, MARK_USER,
            "perf fixture: retired and marked", now)
    by_cadence = {}  # type: Dict[int, List[Tuple[str, str, str, Optional[int]]]]
    for triple, cadence in plan.marks[:count]:
        by_cadence.setdefault(cadence, []).append(triple + (None,))
    written = 0
    for cadence in sorted(by_cadence):
        result = mark(by_cadence[cadence], cadence, MARK_REASON, MARK_USER,
                      now)  # type: ignore
        written += int(getattr(result, "marked"))
    return MarksDone(
        marks=written,
        muted=1 if plan.muted is not None else 0,
        assigned=1 if plan.assigned is not None else 0,
        retired=len(plan.retired))


def _is_live(expires_at: Optional[str], now: datetime.datetime) -> bool:
    if expires_at is None:
        return False
    return datetime.datetime.strptime(
        expires_at[:19], "%Y-%m-%dT%H:%M:%S") > now


def mark_census(path: str, now: datetime.datetime) -> List[MarkCount]:
    """What the marks in *path* are, per stream, by ``mark_state``: the
    figures the README quotes and the tests pin. Independent of the
    product's own evaluation (it reads the tables), so a measured pass
    can hold the build's counts against it. Empty without marks."""
    if not has_marks_table(path):
        return []
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        rows = conn.execute(
            "SELECT lr.stream_id, s.name, fm.environment, fm.script, "
            "fm.test_name, fm.cadence, "
            "(SELECT MAX(tm.expires_at) FROM test_mutes AS tm "
            " WHERE tm.stream_id = lr.stream_id "
            " AND tm.environment = fm.environment "
            " AND tm.script = fm.script AND tm.test_name = fm.test_name), "
            "ca.assignee, tr.retired_at "
            "FROM test_flaky_marks AS fm "
            "JOIN latest_runs AS lr ON lr.environment = fm.environment "
            " AND lr.script = fm.script AND lr.test_name = fm.test_name "
            "JOIN streams AS s ON s.id = lr.stream_id "
            "LEFT JOIN current_assignments AS ca "
            " ON ca.environment = fm.environment AND ca.script = fm.script "
            " AND ca.test_name = fm.test_name "
            "LEFT JOIN test_retirements AS tr "
            " ON tr.environment = fm.environment AND tr.script = fm.script "
            " AND tr.test_name = fm.test_name "
            "ORDER BY lr.stream_id, fm.environment, fm.script, fm.test_name"
        ).fetchall()
        per = {}  # type: Dict[int, Dict[str, int]]
        names = {}  # type: Dict[int, str]
        cadences = {}  # type: Dict[int, Set[int]]
        for row in rows:
            stream_id, cadence = int(row[0]), int(row[5])
            names[stream_id] = str(row[1]) or "mainline"
            counts = per.setdefault(stream_id, {})
            cadences.setdefault(stream_id, set()).add(cadence)
            triple = (str(row[2]), str(row[3]), str(row[4]))
            state = mark_state(_newest(conn, stream_id, triple, cadence + 1),
                               cadence)
            muted = _is_live(row[6], now)
            assigned = row[7] is not None
            retired = row[8] is not None

            def bump(key: str, when: bool = True) -> None:
                if when:
                    counts[key] = counts.get(key, 0) + 1

            bump("marks")
            bump("retired", retired)
            bump(state.state)
            # "Newly fixed" concerns a marked, unmuted, unretired test
            # (§2 B.1), as P does.
            counted = not muted and not retired
            bump("candidates", state.candidate and counted)
            bump("real_fixes", state.real_fix and counted)
            bump("failing_short", state.state == "failing"
                 and state.runs is not None and state.runs < cadence)
            bump("promoted", state.promoted and not muted and not retired)
            flaking_unmuted = state.state == "flaking" and not muted
            bump("f", flaking_unmuted)
            bump("x", flaking_unmuted and not assigned and not retired)
            bump("muted", muted)
            bump("assigned", assigned)
    finally:
        conn.close()
    out = []  # type: List[MarkCount]
    for stream_id in sorted(per):
        counts = per[stream_id]
        out.append(MarkCount(
            stream=names[stream_id], marks=counts.get("marks", 0),
            retired=counts.get("retired", 0),
            flaky_total=counts.get("marks", 0) - counts.get("retired", 0),
            passing=counts.get("passing", 0),
            candidates=counts.get("candidates", 0),
            real_fixes=counts.get("real_fixes", 0),
            flaking=counts.get("flaking", 0),
            failing=counts.get("failing", 0),
            failing_short=counts.get("failing_short", 0),
            promoted=counts.get("promoted", 0), f=counts.get("f", 0),
            x=counts.get("x", 0), muted=counts.get("muted", 0),
            assigned=counts.get("assigned", 0),
            cadences=sorted(cadences[stream_id])))
    return out


def census_lines(census: Sequence[MarkCount]) -> List[str]:
    """The census as the lines ab.py and ``seed.py --onto`` print."""
    lines = []  # type: List[str]
    for count in census:
        lines.append(
            "  {:26s} marks {:4d} (total {:4d}, {} retired) | passing {:4d}"
            " (candidates {}, real fixes {}) | flaking {:3d} (F {}, X {}) |"
            " failing {:3d} ({} with fewer than N runs) | P {} | muted {}, "
            "assigned {} | cadences {}".format(
                count.stream, count.marks, count.flaky_total, count.retired,
                count.passing, count.candidates, count.real_fixes,
                count.flaking, count.f, count.x, count.failing,
                count.failing_short, count.promoted, count.muted,
                count.assigned, "-".join(str(c) for c in count.cadences)
                if len(count.cadences) < 3 else "{}..{} ({})".format(
                    count.cadences[0], count.cadences[-1],
                    len(count.cadences))))
    return lines


def fixture_digest(path: str) -> str:
    """A digest of every row the fixtures write (marks and their history,
    assignments, mutes, retirements, comments): the determinism test's
    "same seed, same marks, same digest"."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        tables = set(str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"))
        digest = hashlib.sha256()
        for table, order in (
                ("test_flaky_marks", "environment, script, test_name"),
                ("flaky_mark_history", "id"),
                ("current_assignments", "environment, script, test_name"),
                ("test_mutes", "stream_id, environment, script, test_name"),
                ("test_retirements", "environment, script, test_name"),
                ("comments", "id")):
            if table not in tables:
                continue
            digest.update(table.encode("utf-8"))
            for row in conn.execute(
                    "SELECT * FROM {} ORDER BY {}".format(table, order)):
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()
    finally:
        conn.close()


def apply_onto(path: str, count: int, tree: Optional[str] = None,
               marks: Optional[int] = None) -> Tuple[int, int]:
    """``--onto``: write the fixtures into an existing seed (a copy, never
    inside a checkout), through *tree*'s Storage, at the seed's clock:
    the assignments fixture when *count*, then the marks fixture when
    *marks* is not None. Returns (assigned, muted) of the assignments
    fixture; ``apply_onto_marks`` is the same with the marks' result."""
    return apply_onto_marks(path, count, tree, marks)[0]


def apply_onto_marks(path: str, count: int, tree: Optional[str] = None,
                     marks: Optional[int] = None
                     ) -> Tuple[Tuple[int, int], Optional[MarksDone]]:
    """``apply_onto``, returning the marks fixture's result as well."""
    refuse_inside_checkout(path)
    if not os.path.isfile(path):
        raise ValueError("no seed at {}".format(path))
    if not count and marks is None:
        raise ValueError("--onto needs --assignments N or --marks N")
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM current_assignments "
            "WHERE assignee IS NOT NULL").fetchone()[0]
        marked = 0
        if marks is not None and conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'test_flaky_marks'").fetchone()[0]:
            marked = conn.execute(
                "SELECT COUNT(*) FROM test_flaky_marks").fetchone()[0]
    finally:
        conn.close()
    if count and existing:
        raise ValueError("{} already has {} assignments; apply the fixture "
                         "to a fresh copy of the seed".format(path, existing))
    if marked:
        raise ValueError("{} already has {} flaky marks; apply the fixture "
                         "to a fresh copy of the seed".format(path, marked))
    if tree is not None:
        sys.path.insert(0, os.path.abspath(tree))
    from testboard.storage import Storage
    now = seed_clock(path)
    store = Storage(path)
    done = (0, 0)
    marks_done = None  # type: Optional[MarksDone]
    try:
        if count:
            done = apply_fixture(store, path, ESTATE[0][1], count, now)
        if marks is not None:
            marks_done = apply_marks(store, path, ESTATE[0][1], marks, now)
    finally:
        store.close()
    checkpoint = sqlite3.connect(path)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    return done, marks_done


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
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--out",
                        help="database to create (outside any checkout)")
    target.add_argument("--onto",
                        help="an existing seed (a copy) to write the "
                             "--assignments fixture into; builds nothing")
    parser.add_argument("--tree", default=REPO_ROOT,
                        help="checkout whose testboard writes the seed "
                             "(default: this one); use the A/B's base")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="tests per environment x this (tests use 0.03)")
    parser.add_argument("--nights", type=int, default=7,
                        help="mainline nights (the full build runs the "
                             "last five of them)")
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("--history-nights", type=int, default=0,
                        help="build the perf-seed/1-hN variant: the "
                             "largest environment's mainline goes back N "
                             "nights (90 for a 90-day window)")
    parser.add_argument("--assignments", type=int, default=0,
                        help="with --onto: the fixture's size (65 is "
                             "the reference estate's figure, 1000 the "
                             "stress); 10 of them muted")
    parser.add_argument("--marks", type=int, default=None,
                        help="with --onto: the marks fixture's size (0, "
                             "50 or 1000), after --assignments when both "
                             "are given; the tree must have migration 12")
    args = parser.parse_args(argv)
    try:
        if args.onto:
            if args.assignments < 0 or (args.marks is not None
                                        and args.marks < 0):
                raise ValueError("--assignments and --marks take N >= 0")
            (assigned, muted), marked = apply_onto_marks(
                args.onto, args.assignments, args.tree, args.marks)
            if args.assignments:
                print("fixture: {} assigned, {} muted -> {}".format(
                    assigned, muted, args.onto))
            if marked is not None:
                print("marks fixture: {} marked; companions {} muted, {} "
                      "assigned, {} retired -> {}".format(
                          marked.marks, marked.muted, marked.assigned,
                          marked.retired, args.onto))
                for line in census_lines(
                        mark_census(args.onto, seed_clock(args.onto))):
                    print(line)
            return 0
        if args.assignments or args.marks is not None:
            raise ValueError("--assignments and --marks go with --onto a "
                             "copy of a built seed (or ab.py --assign, "
                             "--marks)")
        counts = build(args.out, args.tree, args.scale, args.nights,
                       args.seed, log=sys.stdout,
                       history_nights=args.history_nights)
    except ValueError as exc:
        print("seed.py: {}".format(exc), file=sys.stderr)
        return 2
    print("{} scale={} nights={} -> {}".format(
        recipe_name(args.nights, args.history_nights), args.scale,
        args.nights, args.out))
    for table in sorted(counts):
        print("  {:28s} {:9d}".format(table, counts[table]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
