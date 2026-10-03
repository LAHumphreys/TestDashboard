"""Sanity-net seed, step 3: direct Storage writes, server stays up.

    python addendum3_net.py DB_PATH

An earlier version wrote into environment "linux-sim", which seed_wp23_v3
drops (it is replaced by product-prefixed environments). Repointed here
onto "atlas-lab-alpha" (already declared to product Atlas by
seed_wp23_v3_net) under a build named "feat/rc", on a script name that
does not collide with the real estate ("suite/net-addendum.py"), so
Open Actions' "assignment origin disagrees with mainline" case
(test_contradiction) and the "no result on the stream side"
case (test_noresult) exist inside a real, catalog-consistent product
instead of creating an unaffiliated environment. Imports the testboard
package of the checkout this file lives in.
"""
import datetime
import os
import sys
from typing import Optional

# tools/dev/net/seeds/ -> the checkout root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
from testboard.storage import Storage
from testboard.model import RunRecord, Result

DB = sys.argv[1]
store = Storage(DB)
BASE = datetime.datetime(2026, 8, 1, 2, 0, 0)


def rec(name: str, result: str, start: datetime.datetime,
        build: Optional[str] = None) -> RunRecord:
    return RunRecord(
        environment="atlas-lab-alpha", script="suite/net-addendum.py",
        test_name=name, result=Result[result], start_time=start,
        end_time=start + datetime.timedelta(seconds=3),
        output="ok\n", source_link="https://example.com#L1",
        known_failure_reason=None, build=build,
    )


# test_contradiction: mainline FAILS (so it shows up in Open Actions'
# own default "open" filter, which offers no PASS option at all -- FAIL
# / UNEXPECTED_PASS / "open" are the only choices), but the assignment
# was made from a build where it has since PASSED -- a real,
# reachable-through-the-page "these two disagree" case.
store.upsert_runs([rec("test_contradiction", "FAIL", BASE)])
store.upsert_runs([
    rec("test_contradiction", "PASS",
        BASE + datetime.timedelta(days=1, hours=3), build="feat/rc"),
])

# test_noresult: mainline FAILS (so it shows up in Open Actions), the
# build has never run it at all.
store.upsert_runs([rec("test_noresult", "FAIL", BASE)])
# Give feat/rc a run on a DIFFERENT test so the stream exists already
# (it does, from test_contradiction above).

# NET ADDITION (seed-gap fill, not in the original addendum3_seed.py):
# a test that exists ONLY on the build, never on mainline -- the other
# "no result" direction (compare's `new_tests` category is
# `baseline_result IS NULL`; nothing else in the estate produces it,
# since every build's test pool is otherwise a subset of mainline's).
# FAIL result so this doubles as the "new-tests-with-a-FAIL" shape
# class 5 asks for by name.
store.upsert_runs([
    rec("test_only_on_build", "FAIL",
        BASE + datetime.timedelta(days=1, hours=3), build="feat/rc"),
])

# NET ADDITION 2 (seed-gap fill): both_failing is empty on every stream
# in the estate as seeded (verified live: streams 2-5 all report
# both_failing=0) -- a test that fails on BOTH mainline and the build.
store.upsert_runs([rec("test_both_failing", "FAIL", BASE)])
store.upsert_runs([
    rec("test_both_failing", "FAIL",
        BASE + datetime.timedelta(days=1, hours=3), build="feat/rc"),
])

store.close()
streams_conn = Storage(DB)
streams = streams_conn.list_streams("Atlas")
print("Atlas streams after addendum3:",
      [(s.stream_id, s.kind, s.name) for s in streams])
streams_conn.close()
print("seeded OK")
