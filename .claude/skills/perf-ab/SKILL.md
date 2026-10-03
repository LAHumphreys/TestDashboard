---
name: perf-ab
description: The cold, in-process, alternated A/B of two code trees on the production-scale seed, run by performance-engineer for any change on the trigger paths (storage.py, api.py, the push path, first paint) or on request. Reports every touched endpoint and the Known slow table against the base with a noise band. Never run for a light change off the trigger paths.
---

# /perf-ab <base> <head>

The measured performance pass. The main session spawns `performance-engineer`
with the two commits, the implementer's worktree path and the brief; the
agent runs the harness and reports. The main session does not run the
harness itself and does not accept a regression on the implementer's behalf.

## When

- Any diff touching `testboard/storage.py`, `testboard/api.py`, the import
  path, or anything on a page's first paint; or any time the main session or
  the owner asks.
- Not for a diff off those paths whose brief answers Performance impact with
  "none, and here is why". Light changes are not held up.

## What the agent runs

```
python tools/dev/perf/seed.py --out <scratch>/perf.db      # once per seed recipe; production-scale
python tools/dev/perf/ab.py --base <base> --head <head> --db <scratch>/perf.db [--pages ...]
```

`ab.py` imports both trees into one process, clears the storage memos before
every call, alternates A and B, and prints medians with a noise band for:
every endpoint the diff touches (from `--pages` or inferred from the diff),
the Known slow table's rows, and the query count per call for each storage
method touched. `tools/dev/perf/README.md` is the recipe and is kept by the
agent in the same commit as any change to the method.

## What comes back (at most 25 lines)

A table: endpoint · base · head · delta · verdict, the database and the noise
band on this run, then findings. A regression beyond noise goes back to the
implementer by `SendMessage` from the main session, and `/review` does not
start until it is resolved; a number the owner must decide on starts with
`ESCALATE`. The handover's Known slow table is updated from these numbers
when a drop ships, never from a worktree run.

## Never

- Over HTTP on this machine (varies two to three times between runs).
- Warm (a push drops the memos; the cold cost is the cost).
- On the repo-root `testboard.db` (dev data, a quarter of production).
- Quoted without its database and noise band.
