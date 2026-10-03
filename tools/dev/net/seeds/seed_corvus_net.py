"""Sanity-net seed, step 2: product Corvus, fed by an old client.

Talks to the net's server over HTTP (port NET_PORT, default 8931).
Three nights of "corvus-main" in the exact nine-field record shape a
pre-streams feeder sends, so the net exercises an old client's path.
"""
import datetime
import json
import os
import random
import urllib.request
from typing import Any, Optional

PORT = os.environ.get("NET_PORT", "8931")
BASE = "http://127.0.0.1:%s" % PORT


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


def main() -> None:
    random.seed(20260811)
    src = call("/api/dashboard?environment=beacon-perf&limit=500")["tests"]
    src += call("/api/dashboard?environment=beacon-perf&limit=500&offset=500"
                )["tests"]
    print("source tests:", len(src))
    for day in (7, 8, 9):
        clock = datetime.datetime(2026, 8, day, 5, 12, 0)
        records = []
        for t in src:
            dur = random.uniform(2.0, 20.0)
            result = t["result"] or "PASS"
            # The EXACT pre-WP-21 record shape, all nine fields, nothing
            # newer — see module docstring.
            records.append({
                "environment": "corvus-main", "script": t["script"],
                "test_name": t["test_name"], "result": result,
                "start_time": clock.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "end_time": (clock + datetime.timedelta(seconds=dur)
                             ).strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "output": "seeded failure output" if result == "FAIL" else "",
                "source_link": "https://git.example.com/corvus/"
                               + t["script"],
                "known_failure_reason": None,
            })
            clock += datetime.timedelta(seconds=dur * 0.25)
        total = {"inserted": 0, "updated": 0, "unchanged": 0}
        for i in range(0, len(records), 500):
            resp = call("/api/import", {"runs": records[i:i + 500]})
            for k in total:
                total[k] += resp.get(k, 0)
        print("corvus-main day %d:" % day, total)
    call("/api/environments/corvus-main/product",
         {"product": "Corvus", "username": "net-seeder"}, method="PUT")
    print("declared. streams for Corvus:",
          call("/api/streams?product=Corvus")["streams"])


if __name__ == "__main__":
    main()
