"""Sanity-net seed, step 1: products Atlas and Beacon over the dev estate.

Talks to the net's server over HTTP (port NET_PORT, default 8931).
Splits the generated estate's environments into four product
environments, runs three mainline nights on each, and adds a
long-running build, a short build and two release builds.
"""
import datetime
import json
import os
import random
import sys
import urllib.request
from typing import Any, Callable, Dict, List, Optional

PORT = os.environ.get("NET_PORT", "8931")
BASE = "http://127.0.0.1:%s" % PORT

#: The user the seed acts as.
SEED_USER = "net-seeder"


def call(path: str, data: Any = None, method: Optional[str] = None
         ) -> Any:
    """Decoded JSON body; Any is the JSON boundary."""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def import_batches(records: List[Dict[str, Any]]) -> Dict[str, int]:
    total = {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    for i in range(0, len(records), 500):
        resp = call("/api/import", {"runs": records[i:i + 500]})
        for key in total:
            total[key] += resp.get(key, 0)
        if resp.get("errors"):
            print("ERRORS:", resp["errors"][:3])
    return total


def iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def fetch_all(env: str) -> List[Dict[str, Any]]:
    out, offset = [], 0
    while True:
        page = call("/api/dashboard?environment=%s&limit=500&offset=%d"
                    % (env, offset))["tests"]
        out.extend(page)
        if len(page) < 500:
            return out
        offset += 500


def night(tests: List[Dict[str, Any]], env: str, day: int, start_hour: int,
          stream_key: Optional[str] = None,
          stream_val: Optional[str] = None,
          result_for: Optional[Callable[[Dict[str, Any]], str]] = None
          ) -> List[Dict[str, Any]]:
    records = []
    clock = datetime.datetime(2026, 8, day, start_hour, 5, 0)
    by_script = {}
    for t in tests:
        by_script.setdefault(t["script"], []).append(t)
    for script in sorted(by_script):
        clock += datetime.timedelta(seconds=random.uniform(20, 90))
        for t in by_script[script]:
            dur = random.uniform(2.0, 25.0)
            result = result_for(t) if result_for else (t["result"] or "PASS")
            rec = {
                "environment": env, "script": t["script"],
                "test_name": t["test_name"], "result": result,
                "start_time": iso(clock),
                "end_time": iso(clock + datetime.timedelta(seconds=dur)),
                "output": "seeded failure output" if result == "FAIL" else "",
            }
            if stream_key:
                rec[stream_key] = stream_val
            records.append(rec)
            clock += datetime.timedelta(seconds=dur * 0.25)
    return records


def main() -> None:
    random.seed(20260810)
    try:
        call("/api/users", {"username": SEED_USER})
    except Exception:
        pass

    linux = fetch_all("linux-sim")
    uat = fetch_all("linux-uat-sim")
    win = fetch_all("win-sim")
    print("sources:", len(linux), len(uat), len(win))
    estates = {
        "atlas-lab-alpha": linux,
        "atlas-lab-bravo": uat,
        "beacon-stage": win[: len(win) // 2],
        "beacon-perf": win[len(win) // 2:],
    }

    # Three mainline nights per environment, staggered start hours so
    # environments visibly run one after another (the estate rule).
    for day in (7, 8, 9):
        for idx, (env, tests) in enumerate(sorted(estates.items())):
            recs = night(tests, env, day, 1 + idx * 3)
            print("mainline %s day %d:" % (env, day), import_batches(recs))

    for env, product in [("atlas-lab-alpha", "Atlas"),
                         ("atlas-lab-bravo", "Atlas"),
                         ("beacon-stage", "Beacon"),
                         ("beacon-perf", "Beacon")]:
        call("/api/environments/%s/product" % env,
             {"product": product, "username": SEED_USER}, method="PUT")
    print("products declared")

    # Long-running build: 5 nights over 60% of atlas-lab-alpha.
    pool = estates["atlas-lab-alpha"][: int(len(linux) * 0.6)]
    persistent_fail = pool[:8]
    fixed_on_build = pool[8:12]

    def build_result(t: Dict[str, Any]) -> str:
        if t in persistent_fail:
            return "FAIL"
        if t in fixed_on_build:
            return "PASS"
        return "FAIL" if random.random() < 0.004 else "PASS"

    for day in (5, 6, 7, 8, 9):
        recs = night(pool, "atlas-lab-alpha", day, 2, "build",
                     "feature/checkout-rewrite", build_result)
        print("LR night %d:" % day, import_batches(recs))

    short = night(pool[20:22], "atlas-lab-alpha", 9, 0, "build",
                  "feat/payment-retry-backoff", lambda t: "FAIL")
    for rec in short:
        rec["output"] = "AssertionError: build-only failure"
    short += night([persistent_fail[0]], "atlas-lab-alpha", 9, 0, "build",
                   "feat/payment-retry-backoff", lambda t: "PASS")
    print("short build:", import_batches(short))

    bpool = estates["atlas-lab-bravo"][: int(len(uat) * 0.7)]
    rc_fail_old = bpool[:12]
    rc_fixed_new = bpool[:7]
    rc_new_fail = bpool[15:17]
    recs = night(bpool, "atlas-lab-bravo", 6, 14, "build", "2026.9.0",
                 lambda t: "FAIL" if t in rc_fail_old else "PASS")
    print("build 2026.9.0:", import_batches(recs))

    def rc1_result(t: Dict[str, Any]) -> str:
        if t in rc_new_fail:
            return "FAIL"
        if t in rc_fail_old and t not in rc_fixed_new:
            return "FAIL"
        return "PASS"

    recs = night(bpool, "atlas-lab-bravo", 8, 21, "build", "2026.9.1",
                 rc1_result)
    print("build 2026.9.1:", import_batches(recs))

    print("streams:", [(s["kind"], s["name"], s.get("failing"))
                       for s in call("/api/streams?product=Atlas")["streams"]])


if __name__ == "__main__":
    main()
