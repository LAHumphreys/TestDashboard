# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-07-30**, end of the day the second post-launch drop was built.

---

## Where the code is

| | |
|---|---|
| `origin/master` | `0d15cb2` — **this is what production is running** |
| `main` | the 2026-07-30 drop, built and green, **not pushed** |
| `wp-14-in-run-progress` | 4 commits past the older `main`; holds everything deferred |

Suite: **1268 green on `main`** (skipped 1), up from 1137. Schema at **migration 5,
unchanged** — there is no migration in this drop.

Production database is ~900 MB / ~4.4M runs. The repo-root `testboard.db` is
**generated dev data** (218 MB, 540,192 runs, 12,008 tests) — useful, and not production.
Say which one any number came from.

## The immediate thing

**The 2026-07-30 drop is built and awaiting acceptance testing.** Read
[`docs/drops/2026-07-30.md`](drops/2026-07-30.md) — it is the operator note: what
changed, the exact deploy commands, the rollback, the two new optional flags, and an
acceptance checklist. Do not push to `master` until that checklist is signed off.

Every drop gets one of those now; it is in `CLAUDE.md` under Working practice.

What it contains, in one line each: the Time page crash fixed (a missing JS import, not
Python), the "stuck page" root-caused and fixed (idle browser connections were holding
every worker), the Time page redrawn as a treemap, clickable environment pills, a dated
"What's new" link with an unread marker, site-specific release notes, and three new
tools — `drop_environment.py`, `perf_report.py` + `--perf-log`, `add_site_note.py`.

**Still open from that drop, for a person:**

1. A browser pass. Nothing in it has been rendered by a browser — see below.
2. The `UNKNOWN` environment still needs dropping on the server, and the reader that
   caused it still needs fixing. `tools/drop_environment.py` is ready and tested.
3. Decide whether to run with `--perf-log` (recommended on, it is safe to leave) and
   whether testers should see `--site-notes`.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1268 OK (skipped=1) on main
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
bug.

There is no browser here. Frontend changes are verified by driving the real ES modules
against a live server under a minimal DOM shim in node. **That method has now earned its
keep twice in one session** — it found the `formatTime` crash and an
`Array.prototype.slice.call(map.keys())` bug that would have rendered no site notes at
all. It catches wrong field names, missing imports and DOM errors; **it cannot catch
layout, colour or contrast.** Nothing on `main` has been clicked through in a real
browser.

---

## The work waiting, in the order it wants doing

### 1. Merge the deferred work back — `wp-14-in-run-progress`

Three things were pulled off `main` an hour before the 2026-07-28 drop, deliberately, to
avoid two changes to the heaviest endpoint on deployment morning. They belong together:

- **The in-run progress bar** (WP-14, commits `1102463`, `b4b1030`). Finished and green.
  It counts *imported runs*, so it is useless until item 2 — against the real reader it
  would sit flat all night and then jump to 100%.
- **The shared last-pass field.** Open actions and the "Still failing" triage queue ask
  the same question and answered it two different ways. One `lastPassCell()` on the
  client, one `_stability_json()` on the server, batched-history test included.
- **The `/api/summary` cache.** See item 3.

**Merge `main` into the branch before doing anything on it.** It has `main` as of
`ed4a59a` only, so it is now missing considerably more than when this was last written —
the 2026-07-28 drop *and* the whole 2026-07-30 drop. Expect the conflicts to be larger
than the ones described below.

Previously-known conflicts, all still likely:

- `UPGRADE_PLAN_STATUS.md` — the branch holds the full WP-14 log entry; `main` holds a
  pointer that says to replace it with exactly that. Take the branch's entry.
- `style.css` — append-only by convention, so both sides added a block at the end. Keep
  both.
- `app.js` — the branch re-adds the shared last-pass cell to the triage queue, while
  `main` has since relabelled the tiles around it **and made the environment pills
  buttons**. Read that merge rather than accepting either side wholesale.
- **New:** `time.js` and `charts.js` have changed substantially (the treemap). If the
  branch touches either, read it carefully.

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

**Migration 6 is still claimed and still unwritten.** The 2026-07-30 drop deliberately
added no migration; `site_notes` uses a file precisely so it did not have to take a
version out of turn.

### 3. `/api/summary` — and now you can measure it in production

Decomposed on the dev database (this machine, warm, 20 concurrent requests against 2
workers, via the new `--perf-log`):

| | |
|---|---|
| `activity_buckets` | **139 ms** mean, 2.92 s total across 21 calls |
| `summary_rollup` | 10 ms |
| `status_queue` | 2 ms |

The earlier decomposition on the same database recorded `activity_buckets(14d)` at
**682 ms**. Both are real; they are different machines and cache states, which is exactly
why the numbers were never the argument. `activity_buckets` is the cost either way.

**An index is not the answer, measured rather than assumed.** A covering index on
`runs(start_time, environment)` takes the query 683 → **318 ms, but only when forced**:
the planner keeps choosing the UNIQUE index and scanning all 540,192 entries instead of
the 168k in the window, and `ANALYZE` does not change its mind. 2.3 s to build on dev,
permanent cost on every import, for a gain the planner will not take.

The parked fix memoises the buckets beside the existing trend cache — same lock, same
60 s TTL, same invalidation — with the lookback floor truncated to the hour so the key
repeats between requests. **Not yet measured:** what that does to a warm response, and
whether the cold first-request-of-each-minute is acceptable or the query wants
restructuring.

**New:** with `--perf-log` on the production server you can now answer this from
production rather than from here, and separate it from queue contention. Do that before
building the cache.

### 4. WP-16 — site-specific info tab

**Noted, not specified.** Do not build from the paragraph in the plan. The question that
decides its size is static text versus data-driven; see `UPGRADE_PLAN.md` §WP-16.

Note that the 2026-07-30 drop has answered a nearby question in passing: site-specific
*release notes* now exist as a JSON file plus `GET /api/site-notes`, with no migration.
If WP-16 turns out to be mostly declared text, that is a precedent worth reusing.

---

## Needs a person, not a commit

1. **Acceptance-test the 2026-07-30 drop** and work the checklist in
   `docs/drops/2026-07-30.md`. Everything else here is behind that.
2. `tools/diagnose_db.py --compare-local` on the production server — still never run
   since the worker-pool fix, so every timing predating that fix describes a server that
   discarded its page cache on every request.
3. **The MariaDB migration.** `tools/migrate_to_mariadb.py` automates the unprivileged
   half and stops at the first failed gate. §A of the runbook needs whoever holds root,
   then the dry run against a copy of production. **Nothing in that tool that talks to
   MariaDB has ever been executed** — no server here or in CI, and those paths are driven
   by a fake client. The runbook's dry run is what proves it.
4. A browser pass over the shipped UI, now including the treemap.

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Add a dated section at the top of `static/whatsnew.html` — for a tester, not for a
   developer. Only what is actually in the build. It needs
   `data-drop-date="YYYY-MM-DD"` matching its heading, or `DropDateTest` fails.
3. **Write `docs/drops/YYYY-MM-DD.md`**, the operator note — for whoever deploys it.
   Required contents are listed in `CLAUDE.md`; the 2026-07-30 one is the worked example.
4. `git push origin main:master` (local branch is `main`, remote is `master`).
5. On the box: pull, **stop the server**, copy `testboard.db` + `-wal` + `-shm`, start.
   The copy is the only rollback — a database at version N is refused by older code.
   Do it when the feeder is not importing; a migration holds one transaction.
6. Check the drop: each environment reads `N of N counted` under Open actions →
   Environment expectations. If one reads `0 of N`, set its expected count before anyone
   triages, or the cutoff silently falls back to the wall clock.
