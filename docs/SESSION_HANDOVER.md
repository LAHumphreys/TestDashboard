# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-07-28**, end of the day the first post-launch drop shipped.

---

## Where the code is

| | |
|---|---|
| `origin/master` | `0d15cb2` — **this is what production is running** |
| `main` | one commit further (`9937358`, a plan note only — no code) |
| `wp-14-in-run-progress` | 4 commits past `main`; holds everything deferred |

Suite: **1,137 green on `main`**. The branch was 1,145 green when it was parked, but
it has not been re-run since `main` moved on — re-run it after the merge below, do
not quote that number.  Schema at **migration 5**.

Production database is ~900 MB / ~4.4M runs. The repo-root `testboard.db` is
**generated dev data** (218 MB, 540,192 runs, 12,008 tests) — useful, and not production.
Say which one any number came from.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1137 OK (skipped=1) on main
```

Then read the state table at the top of `UPGRADE_PLAN_STATUS.md` and take the first row
that is not `done`.

To look at the running dashboard:

```bash
python run_server.py --port 8000 --db testboard.db      # migrates dev data if needed
```

**If the UI looks wrong, check you restarted the server.** Static files are read from
disk per request but the Python is whatever was imported at process start, so a stale
process serves new HTML against old handlers. That has twice looked exactly like a UI
bug: users all rendering as "Deactivated" (old `_user_json` had no `active` field), and
an empty table (the endpoint 404ing).

There is no browser here. Frontend changes have been verified by driving the real ES
modules against a live server under a minimal DOM shim in node — see the pattern in the
log's WP-13 entry. It catches wrong field names and DOM errors; it does not catch layout.
**Nothing on `main` has been clicked through in a real browser.**

---

## The work waiting, in the order it wants doing

### 1. Merge the deferred work back — `wp-14-in-run-progress`

Three things were pulled off `main` an hour before the drop, deliberately, to avoid two
changes to the heaviest endpoint on deployment morning. They belong together:

- **The in-run progress bar** (WP-14, commits `1102463`, `b4b1030`). Finished and green.
  It counts *imported runs*, so it is useless until item 2 — against the real reader it
  would sit flat all night and then jump to 100%.
- **The shared last-pass field.** Open actions and the "Still failing" triage queue ask
  the same question and answered it two different ways. One `lastPassCell()` on the
  client, one `_stability_json()` on the server, batched-history test included.
- **The `/api/summary` cache.** See item 3.

**Merge `main` into the branch before doing anything on it.** It has `main` as of
`ed4a59a` only, so it is missing the five commits that shipped after that — the
per-environment last-update line, the "Latest results" relabelling, and two corrections.
Its `app.js`, `api.py` and `whatsnew.html` all diverge from what production runs.

Expect two conflicts, both benign and both seen before:

- `UPGRADE_PLAN_STATUS.md` — the branch holds the full WP-14 log entry; `main` holds a
  pointer that says to replace it with exactly that. Take the branch's entry.
- `style.css` — append-only by convention, so both sides added a block at the end. Keep
  both.

And expect one that is not benign: the branch's `app.js` re-adds the shared last-pass
cell to the triage queue, while `main` has since relabelled the tiles around it. Read
that merge rather than accepting either side wholesale.

### 2. WP-15 — progress pushes from a partial reader *(migration 6, claimed)*

**Fully specified** in `UPGRADE_PLAN.md` §WP-15. Read it; do not re-derive it.

The short version: the in-house reader cannot produce a full run record mid-pass — it has
identities and results but **no per-test timings** until the final push. Those records
cannot go through `/api/import`, and not only because that contract is fixed: run identity
includes `start_time`, so a record without one has no identity, and synthesising one makes
the final push write a *second* row per test per night.

So: a `run_progress` table keyed by the test triple, a `POST /api/progress` carrying
`pass_started` and **completed tests only** (confirmed with the user — no
"started but not finished" state, the `Result` enum does not grow), and a delete inside
the import transaction so a real run supersedes its provisional row. A provisional result
is never "the latest result": it stays out of `latest_runs`, the queues, the staleness
cutoff and the coverage denominator. **That line is what keeps the package additive.**

Two things the spec calls out that are easy to miss: `latest_progress` must be able to
build a row from `run_progress` *alone* (an environment whose pass is entirely provisional
has nothing in `activity_buckets`, so today it would render no bar at all — precisely when
one is wanted), and `running` must key off `reported_at`.

### 3. `/api/summary` is ~890 ms and the cause is known

Decomposed on the dev database:

| | |
|---|---|
| `activity_buckets(14d)` | **682 ms** |
| `summary_rollup` | 41 ms |
| `recent_results(93)` | 24 ms |
| 93× `failure_streak_bounds` | 10 ms |

It is WP-12's, not the last-pass field's. The performance pass that recorded "~197 ms"
predates it, so nothing caught it.

**An index is not the answer, measured rather than assumed.** A covering index on
`runs(start_time, environment)` takes the query 683 → **318 ms, but only when forced**:
the planner keeps choosing the UNIQUE index and scanning all 540,192 entries instead of
the 168k in the window, and `ANALYZE` does not change its mind. 2.3 s to build on dev,
permanent cost on every import, for a gain the planner will not take.

The parked fix memoises the buckets beside the existing trend cache — same lock, same
60 s TTL, same invalidation — with the lookback floor truncated to the hour so the key
repeats between requests. **Not yet measured:** what that does to a warm response, and
whether the cold 682 ms first-request-of-each-minute is acceptable or the query wants
restructuring.

### 4. WP-16 — site-specific info tab

**Noted, not specified.** Do not build from the paragraph in the plan. The question that
decides its size is static text versus data-driven; see `UPGRADE_PLAN.md` §WP-16.

---

## Needs a person, not a commit

1. `tools/diagnose_db.py --compare-local` on the production server — still never run
   since the worker-pool fix, so every timing predating that fix describes a server that
   discarded its page cache on every request.
2. **The MariaDB migration.** `tools/migrate_to_mariadb.py` automates the unprivileged
   half and stops at the first failed gate. §A of the runbook needs whoever holds root,
   then the dry run against a copy of production. **Nothing in that tool that talks to
   MariaDB has ever been executed** — no server here or in CI, and those paths are driven
   by a fake client. The runbook's dry run is what proves it.
3. A browser pass over the shipped UI.

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Add a dated section at the top of `static/whatsnew.html` — for a tester, not for a
   developer. Only what is actually in the build.
3. `git push origin main:master` (local branch is `main`, remote is `master`).
4. On the box: pull, **stop the server**, copy `testboard.db` + `-wal` + `-shm`, start.
   The copy is the only rollback — a database at version N is refused by older code.
   Do it when the feeder is not importing; the migration holds one transaction.
5. Check the drop: each environment reads `N of N counted` under Open actions →
   Environment expectations. If one reads `0 of N`, set its expected count before anyone
   triages, or the cutoff silently falls back to the wall clock.
