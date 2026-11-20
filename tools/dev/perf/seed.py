"""Build the production-scale seed database the A/B runner measures on.

    python tools/dev/perf/seed.py --out <scratch>/perf.db
    python tools/dev/perf/seed.py --out <scratch>/perf.db --tree <checkout>
    python tools/dev/perf/seed.py --out <scratch>/h90.db --history-nights 90
    python tools/dev/perf/seed.py --onto <scratch>/copy.db --assignments 1000
    python tools/dev/perf/seed.py --onto <scratch>/copy.db --assignments 65 \
        --marks 50 --tree <checkout at migration 12 or later>
    python tools/dev/perf/seed.py --onto <scratch>/h90-copy.db \
        --assignments 65 --default-failing 50 --two-servers
    python tools/dev/perf/seed.py --onto <scratch>/copy.db \
        --assignments 65 --default-failing 500 --failing-mix latest
    python tools/dev/perf/seed.py --onto <scratch>/fixture.db \
        --flaky-default 2 --tree <checkout at WP-44 item 22 or later>
    python tools/dev/perf/seed.py --census <scratch>/fixture.db [--cadence 2]

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

``--default-failing N`` (with ``--onto``, after ``--assignments``) writes
the failing fixture the flaky default's two bars were measured on
(docs/design/flaky-default-r0.md §3a): the largest environment's
mainline rewritten to N tests failing, in the nightly mix
(``--two-servers``: every run again 3 h later) or the latest mix, with ``latest_runs`` and the hourly rollups rebuilt and
then checked against ``runs``. ``--flaky-default C`` sets that
environment's default through the tree's own endpoint (item 22 on).
``--census`` prints what a built fixture holds. README.md has the recipe
and each fixture's census.

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
import contextlib
import datetime
import hashlib
import io
import json
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


def marks_clock(path: str, environment: str) -> datetime.datetime:
    """When the marks fixture dates its marks: one hour before the hour
    of the first start among *environment*'s latest runs, on any stream.

    A mark is set in the day and the night's runs follow it. The product
    promotes a test only on a run that started at or after its cadence
    was last set (a mark or a cadence change never makes a test New), so
    marks dated at the seed's clock, after every run, would leave no
    test promoted and the promoted paths unmeasured. Dated here, every
    stream's latest run follows every mark, and the product's P is the
    census's. Read-only on *path*.
    """
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        first = conn.execute(
            "SELECT MIN(start_time) FROM latest_runs WHERE environment = ?",
            (environment,)).fetchone()[0]
    finally:
        conn.close()
    if first is None:
        raise ValueError("no runs of {} in {}".format(environment, path))
    started = datetime.datetime.strptime(first[:19], "%Y-%m-%dT%H:%M:%S")
    return (started.replace(minute=0, second=0)
            - datetime.timedelta(hours=1))


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
    marked by amy, never by a raw INSERT. The companions are written at
    *now*; the marks (their history rows and comments with them) are
    dated by :func:`marks_clock`, before the latest runs they are read
    against.

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
    dated = marks_clock(path, environment) if by_cadence else now
    for cadence in sorted(by_cadence):
        result = mark(by_cadence[cadence], cadence, MARK_REASON, MARK_USER,
                      dated)  # type: ignore
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


def _tally(counts: Dict[str, int], state: MarkState, cadence: int,
           muted: bool, assigned: bool, retired: bool) -> None:
    """Count one judged test into *counts*: the census's reading of
    §2 B.1, shared by the marks fixture and the default."""
    def bump(key: str, when: bool = True) -> None:
        if when:
            counts[key] = counts.get(key, 0) + 1

    bump(state.state)
    # "Newly fixed" concerns a marked, unmuted, unretired test
    # (§2 B.1), as P does.
    counted = not muted and not retired
    bump("candidates", state.candidate and counted)
    bump("real_fixes", state.real_fix and counted)
    bump("failing_short", state.state == "failing"
         and state.runs is not None and state.runs < cadence)
    bump("promoted", state.promoted and counted)
    flaking_unmuted = state.state == "flaking" and not muted
    bump("f", flaking_unmuted)
    bump("x", flaking_unmuted and not assigned and not retired)
    bump("muted", muted)
    bump("assigned", assigned)


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
            retired = row[8] is not None
            counts["marks"] = counts.get("marks", 0) + 1
            if retired:
                counts["retired"] = counts.get("retired", 0) + 1
            _tally(counts, state, cadence, _is_live(row[6], now),
                   row[7] is not None, retired)
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
                ("comments", "id"),
                ("environment_flaky_defaults", "environment"),
                ("flaky_default_history", "id")):
            if table not in tables:
                continue
            digest.update(table.encode("utf-8"))
            for row in conn.execute(
                    "SELECT * FROM {} ORDER BY {}".format(table, order)):
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# The failing fixture and the flaky default (docs/design/flaky-default-r0.md)
# ----------------------------------------------------------------------

#: The shapes of "N tests failing" in the largest environment that the
#: two bars of docs/design/flaky-default-r0.md §3a are held to.
#: ``nightly`` (the per-day chart's bar, "The trend, round 1", on -h90):
#: every mainline night rewritten. ``latest`` (the headline's bar, on
#: perf-seed/1): the newest three mainline runs of the planted tests
#: rewritten.
FAILING_MIXES = ("nightly", "latest")

#: The second server's run of a test, the same night, after the first's.
SECOND_SERVER_HOURS = 3

#: Nights in a row a broken test of the nightly mix fails, on both servers.
SPELL_NIGHTS = 5

#: The per-day chart's window, the only one a page asks for (nights).
TREND_NIGHTS = 14

DEFAULT_REASON = "perf fixture: two servers, investigate failures on both"


class FailingDone(NamedTuple):
    """What ``apply_failing`` changed."""

    rewritten: int    # existing runs whose result the mix changed
    doubled: int      # second-server runs added (0 without two servers)


class FailingCount(NamedTuple):
    """The failing fixture's census: the largest environment's mainline
    as the bars' fixtures hold it (``failing_census``)."""

    environment: str
    tests: int                 # mainline tests (latest_runs rows)
    nights: int                # distinct mainline nights
    runs: int                  # mainline runs
    fail_runs: int
    failing_tests: int         # tests with a FAIL on any night
    window_since: str          # first night of the newest TREND_NIGHTS
    window_fail_runs: int
    window_failing_tests: int
    latest_fail: int           # latest_runs: result FAIL
    fail_after_fail: int       # ... after a FAIL
    fail_first: int            # ... with no run before it
    candidates: int            # latest_runs: not a FAIL, after a FAIL
    estate_runs: int           # every run in the database
    outputs: int               # run_outputs rows


class DefaultCount(NamedTuple):
    """One stream of the defaulted environment, judged at the default's
    cadence by ``mark_state`` (``default_census``)."""

    stream: str
    cadence: int
    tests: int             # the environment's tests on the stream, unmarked
    marked: int            # tests a mark judges instead (the mark wins)
    passing: int
    candidates: int        # of passing: after a FAIL; unmuted, unretired
    real_fixes: int
    flaking: int
    f: int                 # flaking, under no live mute
    x: int                 # F with no assignee, unretired
    failing: int
    failing_short: int     # of failing: every run a FAIL, fewer than N
    promoted: int          # P: unmuted, unretired
    muted: int
    assigned: int
    retired: int


def mix_hash(*parts: int) -> int:
    """The failing mixes' draw: the first 32 bits of the MD5 of the
    parts' ``repr``: the same on every interpreter, so the same seed gives
    the same rows, and at 8,000 tests the fourteen nights §3a's round 1
    figures quote (1,038 and 9,286 FAIL runs; tests/test_dev_perf.py)."""
    return int(hashlib.md5(repr(parts).encode("utf-8")).hexdigest()[:8], 16)


def nightly_pair(index: int, night: int, count: int, tests: int
                 ) -> Tuple[str, str]:
    """The nightly mix: test *index* of *tests* (in (script, test) order) on
    *night* (0 is the oldest), first server then second.

    30% of *count* are broken in spells of :data:`SPELL_NIGHTS` nights
    and fail on both servers; the other 70% fail on one server, drawn
    each night with odds of one in four from a pool four times their
    number. About *count* tests fail a night, on one server or both.
    """
    broken = (3 * count) // 10
    pool = 4 * (count - broken)
    if mix_hash(index, night // SPELL_NIGHTS) % tests < broken:
        return "FAIL", "FAIL"
    if mix_hash(index) % tests < pool and mix_hash(index, night) % 4 == 0:
        if mix_hash(index, night, 1) % 2:
            return "FAIL", "PASS"
        return "PASS", "FAIL"
    return "PASS", "PASS"


def latest_plan(tests: Sequence[Tuple[str, str, str, Optional[str]]],
                count: int
                ) -> Dict[Tuple[str, str], Tuple[str, str, Optional[str]]]:
    """The latest mix (the one §3a's headline bar names) over *tests*, the
    environment's mainline ``(script, test_name, result, prev_result)``
    in (script, test) order: the newest three results to write per
    planted test, newest first (None leaves a run as it is).

    Of the tests whose latest result is PASS or FAIL, a fixed stride
    picks *count* failing now and 0.3 x *count* fix candidates: half FAIL
    after a PASS; half FAIL after a FAIL, run 3 a PASS for one in seven
    (promoted at cadence 2), else a FAIL; the candidates PASS after a
    FAIL, run 3 a FAIL for one in five, else a PASS. Every other test
    whose latest two include a FAIL passes twice.
    """
    eligible = [(t[0], t[1]) for t in tests if t[2] in ("PASS", "FAIL")]
    n_new, n_cand = count // 2, (3 * count) // 10
    n_still = count - n_new
    want = n_new + n_still + n_cand
    if want > len(eligible):
        raise ValueError("{} failing and {} candidates need {} tests; the "
                         "environment has {}".format(count, n_cand, want,
                                                     len(eligible)))
    stride = len(eligible) // want
    plan = {}  # type: Dict[Tuple[str, str], Tuple[str, str, Optional[str]]]
    for i in range(want):
        key = eligible[i * stride]
        if i < n_new:
            plan[key] = ("FAIL", "PASS", None)
        elif i < n_new + n_still:
            j = i - n_new
            plan[key] = ("FAIL", "FAIL", "PASS" if j % 7 == 0 else "FAIL")
        else:
            j = i - n_new - n_still
            plan[key] = ("PASS", "FAIL", "FAIL" if j % 5 == 0 else "PASS")
    for script, test_name, result, prev in tests:
        key = (script, test_name)
        if key not in plan and result in ("PASS", "FAIL") and (
                result == "FAIL" or prev == "FAIL"):
            plan[key] = ("PASS", "PASS", None)
    return plan


def _mainline_tests(conn: sqlite3.Connection, environment: str
                    ) -> List[Tuple[str, str, str, Optional[str]]]:
    return [(str(row[0]), str(row[1]), str(row[2]),
             None if row[3] is None else str(row[3]))
            for row in conn.execute(
        "SELECT script, test_name, result, prev_result FROM latest_runs "
        "WHERE stream_id = 1 AND environment = ? ORDER BY script, test_name",
        (environment,)).fetchall()]


def _later(column: str) -> str:
    """SQL for *column* (an ISO timestamp) moved to the second server's
    hour: whole seconds through ``strftime``, which carries a day over,
    then the original fraction. Only the whole seconds go in: with a
    modifier, some SQLite versions (3.45) round the time to the
    millisecond, so a fraction of .9995 or more came out a second late."""
    return ("strftime('%Y-%m-%dT%H:%M:%S', SUBSTR({0}, 1, 19), "
            "'+{1} hours') "
            "|| SUBSTR({0}, 20)".format(column, SECOND_SERVER_HOURS))


def apply_failing(conn: sqlite3.Connection, environment: str, count: int,
                  mix: str, two_servers: bool) -> FailingDone:
    """Rewrite *environment*'s mainline to *count* tests failing, in
    *mix*, inside the caller's transaction on *conn*; with *two_servers*
    (the nightly mix only) every mainline run of it gets a second run
    :data:`SECOND_SERVER_HOURS` later, with its own result and a copy of
    the first run's output (an import writes an output for every run, and
    a test's run history reads it). ``latest_runs`` and the hourly rollups are
    NOT touched here: ``rederive`` does that, and ``check_derived``
    proves it."""
    if mix not in FAILING_MIXES:
        raise ValueError("--failing-mix is one of {}".format(
            ", ".join(FAILING_MIXES)))
    if count < 1:
        raise ValueError("--default-failing N needs N >= 1")
    if two_servers and mix != "nightly":
        raise ValueError("--two-servers goes with the nightly mix: a "
                         "second run of the latest mix's results would "
                         "make every planted failure a FAIL after a FAIL")
    tests = _mainline_tests(conn, environment)
    if count > len(tests):
        raise ValueError("{} has {} mainline tests, fewer than {}".format(
            environment, len(tests), count))
    if conn.execute(
            "SELECT 1 FROM runs WHERE environment = ? AND stream_id = 1 "
            "GROUP BY script, test_name, SUBSTR(start_time, 1, 10) "
            "HAVING COUNT(*) > 1 LIMIT 1", (environment,)).fetchone():
        raise ValueError("{} already runs a test twice a night: apply the "
                         "failing fixture to a fresh copy of the seed"
                         .format(environment))
    changes = []  # type: List[Tuple[str, int]]
    second = []  # type: List[Tuple[int, str]]
    if mix == "nightly":
        index = dict(((t[0], t[1]), i) for i, t in enumerate(tests))
        days = sorted(str(row[0]) for row in conn.execute(
            "SELECT DISTINCT SUBSTR(start_time, 1, 10) FROM runs "
            "WHERE environment = ? AND stream_id = 1", (environment,)))
        night = dict((day, i) for i, day in enumerate(days))
        for run_id, script, test_name, day, result in conn.execute(
                "SELECT id, script, test_name, SUBSTR(start_time, 1, 10), "
                "result FROM runs WHERE environment = ? AND stream_id = 1",
                (environment,)):
            first, other = nightly_pair(index[(script, test_name)],
                                        night[day], count, len(tests))
            if first != result:
                changes.append((first, int(run_id)))
            if two_servers:
                second.append((int(run_id), other))
    else:
        for (script, test_name), results in sorted(
                latest_plan(tests, count).items()):
            newest = conn.execute(
                "SELECT id, result FROM runs WHERE environment = ? "
                "AND script = ? AND test_name = ? AND stream_id = 1 "
                "ORDER BY start_time DESC LIMIT 3",
                (environment, script, test_name)).fetchall()
            for (run_id, old), new in zip(newest, results):
                if new is not None and new != old:
                    changes.append((new, int(run_id)))
    conn.executemany("UPDATE runs SET result = ? WHERE id = ?", changes)
    if second:
        conn.execute("CREATE TEMP TABLE fixture_second "
                     "(rid INTEGER PRIMARY KEY, result TEXT NOT NULL)")
        conn.executemany("INSERT INTO fixture_second (rid, result) "
                         "VALUES (?, ?)", second)
        conn.execute(
            "INSERT INTO runs (environment, script, test_name, result, "
            "start_time, end_time, source_link, known_failure_reason, "
            "output_fingerprint, stream_id) "
            "SELECT r.environment, r.script, r.test_name, s.result, "
            "{}, {}, r.source_link, r.known_failure_reason, "
            "r.output_fingerprint, r.stream_id "
            "FROM fixture_second AS s JOIN runs AS r ON r.id = s.rid "
            "ORDER BY r.start_time, r.id".format(
                _later("r.start_time"), _later("r.end_time")))
        conn.execute(
            "INSERT INTO run_outputs (run_id, output) "
            "SELECT n.id, o.output FROM fixture_second AS s "
            "JOIN runs AS r ON r.id = s.rid "
            "JOIN runs AS n ON n.environment = r.environment "
            " AND n.script = r.script AND n.test_name = r.test_name "
            " AND n.start_time = {} "
            "JOIN run_outputs AS o ON o.run_id = r.id".format(
                _later("r.start_time")))
        conn.execute("DROP TABLE fixture_second")
    return FailingDone(rewritten=len(changes), doubled=len(second))


def rederive(conn: sqlite3.Connection, storage_module: object,
             environment: str) -> List[str]:
    """After ``apply_failing``, in the same transaction: *environment*'s
    mainline ``latest_runs`` rows re-pointed at their newest run with the
    run before's result, then ``activity_hours`` and ``script_hours``
    rebuilt from ``runs`` by the tree's own rebuild functions (the ones a
    prune runs). Returns what those printed."""
    rows = conn.execute(
        "SELECT script, test_name FROM latest_runs WHERE stream_id = 1 "
        "AND environment = ?", (environment,)).fetchall()
    for script, test_name in rows:
        newest = conn.execute(
            "SELECT id, start_time, result FROM runs WHERE environment = ? "
            "AND script = ? AND test_name = ? AND stream_id = 1 "
            "ORDER BY start_time DESC LIMIT 2",
            (environment, script, test_name)).fetchall()
        conn.execute(
            "UPDATE latest_runs SET run_id = ?, start_time = ?, result = ?, "
            "prev_result = ? WHERE stream_id = 1 AND environment = ? "
            "AND script = ? AND test_name = ?",
            (newest[0][0], newest[0][1], newest[0][2],
             newest[1][2] if len(newest) > 1 else None,
             environment, script, test_name))
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        for name in ("_rebuild_activity_hours_all_streams",
                     "_rebuild_script_hours_all_streams"):
            rebuild = getattr(storage_module, name, None)
            if rebuild is None:
                raise ValueError("this tree's storage has no {}: the "
                                 "rollups cannot be rebuilt".format(name))
            rebuild(conn)
    return [line for line in printed.getvalue().splitlines() if line]


def check_derived(path: str) -> List[str]:
    """Every derived row read against ``runs``, independently of the
    product's code: ``activity_hours`` and ``script_hours`` equal to
    their GROUP BY, one ``latest_runs`` row per (stream, test) that has
    runs, each pointing at its newest run with that run's time and
    result and the result of the run before, and an output for every
    run. Returns the disagreements; empty means every read is valid."""
    problems = []  # type: List[str]
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0] or 0)

        activity = (
            "SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
            "result, COUNT(*) FROM runs GROUP BY stream_id, environment, "
            "SUBSTR(start_time, 1, 13), result")
        stored = ("SELECT stream_id, environment, hour, result, count "
                  "FROM activity_hours")
        scripts = (
            "SELECT stream_id, environment, SUBSTR(start_time, 1, 13), "
            "script, result, COUNT(*), MIN(start_time), MAX(end_time) "
            "FROM runs GROUP BY stream_id, environment, "
            "SUBSTR(start_time, 1, 13), script, result")
        stored_scripts = (
            "SELECT stream_id, environment, hour, script, result, count, "
            "first_start, last_end FROM script_hours")
        for name, derived, kept in (("activity_hours", activity, stored),
                                    ("script_hours", scripts,
                                     stored_scripts)):
            missing = count("SELECT COUNT(*) FROM ({} EXCEPT {})".format(
                derived, kept))
            extra = count("SELECT COUNT(*) FROM ({} EXCEPT {})".format(
                kept, derived))
            if missing or extra:
                problems.append("{}: {} rows missing or wrong, {} not in "
                                "runs".format(name, missing, extra))
        keys = count("SELECT COUNT(*) FROM (SELECT DISTINCT stream_id, "
                     "environment, script, test_name FROM runs)")
        latest = count("SELECT COUNT(*) FROM latest_runs")
        if keys != latest:
            problems.append("latest_runs: {} rows for {} (stream, test) "
                            "keys in runs".format(latest, keys))
        newest = (
            "SELECT {} FROM runs AS r WHERE r.environment = l.environment "
            "AND r.script = l.script AND r.test_name = l.test_name "
            "AND r.stream_id = l.stream_id ORDER BY r.start_time DESC "
            "LIMIT 1 OFFSET {}")
        wrong = count(
            "SELECT COUNT(*) FROM latest_runs AS l WHERE "
            "l.run_id IS NOT ({}) OR l.prev_result IS NOT ({}) "
            "OR l.start_time IS NOT (SELECT start_time FROM runs "
            " WHERE id = l.run_id) "
            "OR l.result IS NOT (SELECT result FROM runs WHERE id = l.run_id)"
            .format(newest.format("r.id", 0), newest.format("r.result", 1)))
        if wrong:
            problems.append("latest_runs: {} rows not pointing at their "
                            "newest run and the one before".format(wrong))
        bare = count("SELECT COUNT(*) FROM runs AS r WHERE NOT EXISTS "
                     "(SELECT 1 FROM run_outputs AS o WHERE o.run_id = r.id)")
        if bare:
            problems.append("run_outputs: {} runs without an output".format(
                bare))
    finally:
        conn.close()
    return problems


def failing_census(path: str, environment: str) -> FailingCount:
    """What the failing fixture holds in *environment*'s mainline: the
    figures README.md quotes for each bar's fixture, and what a measured
    pass checks before it measures. Read-only."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        def one(sql: str, *params: object) -> Tuple[int, ...]:
            row = conn.execute(sql, params).fetchone()
            return tuple(int(value or 0) for value in row)

        days = sorted(str(row[0]) for row in conn.execute(
            "SELECT DISTINCT SUBSTR(start_time, 1, 10) FROM runs "
            "WHERE environment = ? AND stream_id = 1", (environment,)))
        if not days:
            raise ValueError("no mainline runs of {} in {}".format(
                environment, path))
        since = days[-TREND_NIGHTS] if len(days) >= TREND_NIGHTS else days[0]
        runs, fails = one(
            "SELECT COUNT(*), SUM(result = 'FAIL') FROM runs "
            "WHERE environment = ? AND stream_id = 1", environment)
        failing_tests, = one(
            "SELECT COUNT(*) FROM (SELECT DISTINCT script, test_name "
            "FROM runs WHERE environment = ? AND stream_id = 1 "
            "AND result = 'FAIL')", environment)
        window_fails, = one(
            "SELECT COUNT(*) FROM runs WHERE environment = ? "
            "AND stream_id = 1 AND result = 'FAIL' AND start_time >= ?",
            environment, since)
        window_tests, = one(
            "SELECT COUNT(*) FROM (SELECT DISTINCT script, test_name "
            "FROM runs WHERE environment = ? AND stream_id = 1 "
            "AND result = 'FAIL' AND start_time >= ?)", environment, since)
        tests, latest_fail, after_fail, first, candidates = one(
            "SELECT COUNT(*), SUM(result = 'FAIL'), "
            "SUM(result = 'FAIL' AND prev_result = 'FAIL'), "
            "SUM(result = 'FAIL' AND prev_result IS NULL), "
            "SUM(result <> 'FAIL' AND prev_result = 'FAIL') "
            "FROM latest_runs WHERE stream_id = 1 AND environment = ?",
            environment)
        estate, = one("SELECT COUNT(*) FROM runs")
        outputs, = one("SELECT COUNT(*) FROM run_outputs")
    finally:
        conn.close()
    return FailingCount(
        environment=environment, tests=tests, nights=len(days), runs=runs,
        fail_runs=fails, failing_tests=failing_tests, window_since=since,
        window_fail_runs=window_fails, window_failing_tests=window_tests,
        latest_fail=latest_fail, fail_after_fail=after_fail,
        fail_first=first, candidates=candidates, estate_runs=estate,
        outputs=outputs)


def failing_lines(census: FailingCount, problems: Sequence[str]
                  ) -> List[str]:
    """The failing census and the derived-table check, as printed."""
    per = (float(census.runs) / (census.tests * census.nights)
           if census.tests and census.nights else 0.0)
    lines = [
        "  {} mainline: {} tests, {} nights, {:,} runs ({:.1f} a test a "
        "night), {:,} FAIL over {} tests".format(
            census.environment, census.tests, census.nights, census.runs,
            per, census.fail_runs, census.failing_tests),
        "  newest {} nights (from {}): {:,} FAIL runs over {} tests".format(
            TREND_NIGHTS, census.window_since, census.window_fail_runs,
            census.window_failing_tests),
        "  latest: {} FAIL ({} after a FAIL, {} first runs); {} candidates "
        "(not a FAIL, after one)".format(
            census.latest_fail, census.fail_after_fail, census.fail_first,
            census.candidates),
        "  estate: {:,} runs, {:,} outputs".format(census.estate_runs,
                                                   census.outputs)]
    if problems:
        lines.append("  derived tables: INVALID")
        lines.extend("    " + problem for problem in problems)
    else:
        lines.append("  derived tables: valid (activity_hours, script_hours, "
                     "latest_runs and run_outputs read against runs)")
    return lines


def flaky_defaults(path: str) -> Dict[str, int]:
    """environment -> cadence, from migration 13's
    ``environment_flaky_defaults``; empty on an older schema."""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        if not conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'environment_flaky_defaults'").fetchone()[0]:
            return {}
        return dict((str(row[0]), int(row[1])) for row in conn.execute(
            "SELECT environment, cadence FROM environment_flaky_defaults"))
    finally:
        conn.close()


def default_clock(path: str, environment: str) -> datetime.datetime:
    """When the default is set: one hour before the hour of the
    environment's first run on any stream.

    The product promotes no run before the default's ``cadence_at``, so a
    default dated at the seed's clock, after every run, would leave P at
    0 and the promoted paths unmeasured (as for the marks, see
    ``marks_clock``). Dated here, every run follows it, and the census
    below reads the rule as the product does. Read-only on *path*.
    """
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        first = conn.execute(
            "SELECT MIN(start_time) FROM runs WHERE environment = ?",
            (environment,)).fetchone()[0]
    finally:
        conn.close()
    if first is None:
        raise ValueError("no runs of {} in {}".format(environment, path))
    started = datetime.datetime.strptime(first[:19], "%Y-%m-%dT%H:%M:%S")
    return (started.replace(minute=0, second=0)
            - datetime.timedelta(hours=1))


def put_flaky_default(api: object, store: object, environment: str,
                      cadence: int, when: datetime.datetime) -> None:
    """Set *environment*'s default to *cadence* through the tree's own
    ``PUT /api/environments/{environment}/flaky-default`` (the design's
    §1a contract), in-process, as amy, at *when*: the handler validates
    it and writes it, with its history row, through that tree's Storage,
    as a person's edit on the Admin page would. Raises ValueError, with
    the answer, on a tree without the endpoint (before item 22)."""
    body = json.dumps({"username": MARK_USER, "cadence": cadence,
                       "reason": DEFAULT_REASON}).encode("utf-8")
    path = "/api/environments/{}/flaky-default".format(
        urllib.parse.quote(environment, safe=""))
    request = getattr(api, "Request")(method="PUT", path=path, query={},
                                      body=body)
    response = getattr(api, "handle_api")(store, request, now=lambda: when)
    status = int(getattr(response, "status"))
    if not 200 <= status < 300:
        raise ValueError(
            "PUT {} answered {} ({}): --flaky-default needs a tree with the "
            "flaky default (WP-44 item 22, migration 13)".format(
                path, status, bytes(getattr(response, "body"))[:200]
                .decode("utf-8", "replace")))


def default_census(path: str, environment: str, cadence: int,
                   now: datetime.datetime) -> List[DefaultCount]:
    """*environment*'s tests on every stream judged by ``mark_state`` at
    *cadence*, as a default judges them: a marked test is left to its
    mark, a muted one is muted, a retired one uncounted. The harness's
    own reading, independent of the product's; it assumes the default's
    ``cadence_at`` precedes every run (``default_clock``). A test that
    neither failed last nor the run before is passing without a read."""
    has_marks = has_marks_table(path)
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        marked_sql = (
            "(SELECT COUNT(*) FROM test_flaky_marks AS fm "
            " WHERE fm.environment = lr.environment "
            " AND fm.script = lr.script AND fm.test_name = lr.test_name)"
            if has_marks else "0")
        rows = conn.execute(
            "SELECT lr.stream_id, s.name, lr.script, lr.test_name, "
            "lr.result, lr.prev_result, "
            "(SELECT MAX(tm.expires_at) FROM test_mutes AS tm "
            " WHERE tm.stream_id = lr.stream_id "
            " AND tm.environment = lr.environment "
            " AND tm.script = lr.script AND tm.test_name = lr.test_name), "
            "ca.assignee, tr.retired_at, {} "
            "FROM latest_runs AS lr JOIN streams AS s ON s.id = lr.stream_id "
            "LEFT JOIN current_assignments AS ca "
            " ON ca.environment = lr.environment AND ca.script = lr.script "
            " AND ca.test_name = lr.test_name "
            "LEFT JOIN test_retirements AS tr "
            " ON tr.environment = lr.environment AND tr.script = lr.script "
            " AND tr.test_name = lr.test_name "
            "WHERE lr.environment = ? "
            "ORDER BY lr.stream_id, lr.script, lr.test_name".format(
                marked_sql), (environment,)).fetchall()
        per = {}  # type: Dict[int, Dict[str, int]]
        names = {}  # type: Dict[int, str]
        quiet = MarkState("passing", 0, None, False, False, False)
        for row in rows:
            stream_id = int(row[0])
            names[stream_id] = str(row[1]) or "mainline"
            counts = per.setdefault(stream_id, {})
            if int(row[9]):
                counts["marked"] = counts.get("marked", 0) + 1
                continue
            counts["tests"] = counts.get("tests", 0) + 1
            if row[4] != "FAIL" and row[5] != "FAIL":
                state = quiet
            else:
                state = mark_state(_newest(
                    conn, stream_id, (environment, str(row[2]), str(row[3])),
                    cadence + 1), cadence)
            retired = row[8] is not None
            if retired:
                counts["retired"] = counts.get("retired", 0) + 1
            _tally(counts, state, cadence, _is_live(row[6], now),
                   row[7] is not None, retired)
    finally:
        conn.close()
    return [DefaultCount(
        stream=names[stream_id], cadence=cadence,
        tests=per[stream_id].get("tests", 0),
        marked=per[stream_id].get("marked", 0),
        passing=per[stream_id].get("passing", 0),
        candidates=per[stream_id].get("candidates", 0),
        real_fixes=per[stream_id].get("real_fixes", 0),
        flaking=per[stream_id].get("flaking", 0),
        f=per[stream_id].get("f", 0), x=per[stream_id].get("x", 0),
        failing=per[stream_id].get("failing", 0),
        failing_short=per[stream_id].get("failing_short", 0),
        promoted=per[stream_id].get("promoted", 0),
        muted=per[stream_id].get("muted", 0),
        assigned=per[stream_id].get("assigned", 0),
        retired=per[stream_id].get("retired", 0)) for stream_id in sorted(per)]


def default_lines(census: Sequence[DefaultCount]) -> List[str]:
    """The default's census as ab.py and ``seed.py`` print it."""
    return [
        "  {:26s} default {:2d}: {:5d} tests ({} marked) | passing {:5d} "
        "(candidates {}, real fixes {}) | flaking {:4d} (F {}, X {}) | "
        "failing {:4d} ({} with fewer than N runs) | P {} | muted {}, "
        "assigned {}, retired {}".format(
            count.stream, count.cadence, count.tests, count.marked,
            count.passing, count.candidates, count.real_fixes,
            count.flaking, count.f, count.x, count.failing,
            count.failing_short, count.promoted, count.muted,
            count.assigned, count.retired)
        for count in census]


def census_report(path: str, cadence: Optional[int] = None) -> List[str]:
    """``seed.py --census``: the failing census, the derived-table check
    and, for the default the copy holds (else at *cadence*), the
    default's census. The lines a measured pass compares with README.md
    before it measures. Read-only."""
    environment = ESTATE[0][1]
    lines = ["failing fixture, {}:".format(path)]
    lines.extend(failing_lines(failing_census(path, environment),
                               check_derived(path)))
    held = flaky_defaults(path).get(environment)
    judged = held if held is not None else cadence
    if judged is not None:
        lines.append("{} at cadence {} (the census's reading of the rule):"
                     .format("the default held" if held is not None
                             else "no default held; as if one were",
                             judged))
        lines.extend(default_lines(default_census(
            path, environment, judged, seed_clock(path))))
    return lines


class OntoDone(NamedTuple):
    """What ``apply_onto_all`` wrote into the copy."""

    fixture: Tuple[int, int]
    marks: Optional[MarksDone]
    failing: Optional[FailingDone]
    default: Optional[int]


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
    done = apply_onto_all(path, count, tree, marks)
    return done.fixture, done.marks


def apply_onto_all(path: str, count: int, tree: Optional[str] = None,
                   marks: Optional[int] = None, failing: int = 0,
                   mix: str = "nightly", two_servers: bool = False,
                   flaky_default: Optional[int] = None) -> OntoDone:
    """``--onto``, every fixture, in this order: the assignments fixture
    (*count*), the marks fixture (*marks*), the failing fixture
    (*failing* tests in *mix*, *two_servers*) and the flaky default
    (*flaky_default*, through the tree's endpoint, so the tree must be at
    item 22 or later; it migrates the copy to its schema)."""
    refuse_inside_checkout(path)
    if not os.path.isfile(path):
        raise ValueError("no seed at {}".format(path))
    if not count and marks is None and not failing and flaky_default is None:
        raise ValueError("--onto needs --assignments N, --marks N, "
                         "--default-failing N or --flaky-default C")
    if failing and marks is not None:
        raise ValueError("--marks and --default-failing are separate "
                         "fixtures: the marks are chosen by the failures "
                         "the failing fixture rewrites")
    if flaky_default is not None and not 2 <= flaky_default <= 10:
        raise ValueError("--flaky-default C takes 2 to 10")
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
    from testboard import api
    from testboard import storage as storage_module
    from testboard.storage import Storage
    environment = ESTATE[0][1]
    now = seed_clock(path)
    store = Storage(path)
    done = (0, 0)
    marks_done = None  # type: Optional[MarksDone]
    try:
        if count:
            done = apply_fixture(store, path, environment, count, now)
        if marks is not None:
            marks_done = apply_marks(store, path, environment, marks, now)
    finally:
        store.close()
    failing_done = None  # type: Optional[FailingDone]
    if failing:
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("BEGIN")
            try:
                failing_done = apply_failing(conn, environment, failing, mix,
                                             two_servers)
                rederive(conn, storage_module, environment)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    if flaky_default is not None:
        when = default_clock(path, environment)
        store = Storage(path)
        try:
            put_flaky_default(api, store, environment, flaky_default, when)
        finally:
            store.close()
        if flaky_defaults(path).get(environment) != flaky_default:
            raise ValueError("the default of {} answered success but is not "
                             "in environment_flaky_defaults".format(
                                 environment))
    checkpoint = sqlite3.connect(path)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    return OntoDone(fixture=done, marks=marks_done, failing=failing_done,
                    default=flaky_default)


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
    parser.add_argument("--default-failing", type=int, default=0,
                        metavar="N",
                        help="with --onto: rewrite the largest "
                             "environment's mainline to N tests failing "
                             "(after --assignments), the derived tables "
                             "rebuilt; README.md's flaky default fixtures")
    parser.add_argument("--failing-mix", choices=FAILING_MIXES,
                        default="nightly",
                        help="nightly: every night (the per-day chart's "
                             "bar, on -h90); latest: the newest three runs "
                             "(the headline's bar, on perf-seed/1); see "
                             "docs/design/flaky-default-r0.md §3a")
    parser.add_argument("--two-servers", action="store_true",
                        help="with the nightly mix: every mainline run of "
                             "the environment run again 3 h later")
    parser.add_argument("--flaky-default", type=int, default=None,
                        metavar="C",
                        help="with --onto: set the largest environment's "
                             "flaky default to C (2 to 10) through the "
                             "tree's own endpoint; the tree must be at "
                             "WP-44 item 22 or later (migration 13)")
    parser.add_argument("--cadence", type=int, default=None,
                        help="with --census on a copy holding no default: "
                             "judge the environment as if its default "
                             "were this")
    target.add_argument("--census", metavar="PATH",
                        help="print a built fixture's census and check its "
                             "derived tables; writes nothing")
    args = parser.parse_args(argv)
    try:
        if args.census:
            lines = census_report(args.census, args.cadence)
            for line in lines:
                print(line)
            return 1 if "  derived tables: INVALID" in lines else 0
        if args.onto:
            if args.assignments < 0 or (args.marks is not None
                                        and args.marks < 0):
                raise ValueError("--assignments and --marks take N >= 0")
            if args.two_servers and not args.default_failing:
                raise ValueError("--two-servers goes with --default-failing")
            onto = apply_onto_all(
                args.onto, args.assignments, args.tree, args.marks,
                args.default_failing, args.failing_mix, args.two_servers,
                args.flaky_default)
            if args.assignments:
                print("fixture: {} assigned, {} muted -> {}".format(
                    onto.fixture[0], onto.fixture[1], args.onto))
            marked = onto.marks
            if marked is not None:
                print("marks fixture: {} marked; companions {} muted, {} "
                      "assigned, {} retired -> {}".format(
                          marked.marks, marked.muted, marked.assigned,
                          marked.retired, args.onto))
                for line in census_lines(
                        mark_census(args.onto, seed_clock(args.onto))):
                    print(line)
            if onto.failing is not None:
                print("failing fixture: {} tests failing, {} mix{}: {} runs "
                      "rewritten, {} added -> {}".format(
                          args.default_failing, args.failing_mix,
                          ", two servers" if args.two_servers else "",
                          onto.failing.rewritten, onto.failing.doubled,
                          args.onto))
            if onto.failing is not None or onto.default is not None:
                lines = census_report(args.onto)
                for line in lines:
                    print(line)
                if "  derived tables: INVALID" in lines:
                    return 1
            return 0
        if (args.assignments or args.marks is not None
                or args.default_failing or args.flaky_default is not None):
            raise ValueError("--assignments, --marks, --default-failing and "
                             "--flaky-default go with --onto a copy of a "
                             "built seed (or ab.py --assign, --marks, "
                             "--flaky-default)")
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
