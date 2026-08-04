# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-04**, after building WP-18 (Timeline) as the
2026-08-04 drop.

---

## Where the code is

| | |
|---|---|
| `origin/master` | `ea15ccc` — **deployed**: the 2026-07-31 drop is live; its perf fix is confirmed good in production and Phase 2 is explicitly not wanted right now |
| `wp-18-timeline` | **the 2026-08-04 drop** — one feature (Timeline page), migration 7, awaiting acceptance |
| `wp-14-in-run-progress` | parked WIP; **its migration is now 8, not 7** — renumber before merging (registry §1) |

Suite: **1328 green** (skipped 1) on `wp-18-timeline`, up from 1288. Schema
moves to **migration 7** (`script_hours`), so **the rollback is the database
copy**, not `git checkout`.

The repo-root `testboard.db` is generated dev data (218 MB, 540,192 runs,
still at schema v5 — current code migrates it on open, so COPY FIRST, always).
Production is ~900 MB / ~4.4M runs on a network mount. Say which one any
number came from.

## The immediate thing

**The 2026-08-04 drop awaits acceptance.** Read
[`docs/drops/2026-08-04.md`](drops/2026-08-04.md) — deploy commands, migration
expectations, rollback, acceptance checklist. Headlines:

1. **Timeline** (WP-18): new page showing one environment's script running
   order — rows are script executions on a time axis, expandable to tests,
   partial runs flagged, jump-to-first-failure. Built for the
   poisoned-static-data hunt (a script corrupts shared state; a later script's
   test fails; the culprit is above it in the list).
2. **`script_hours`** — fourth derived table, migration 7 with a backfill:
   3.2 s on the dev copy; **must be measured on a prod copy before shipping**
   (the note has the one-liner and a blank for the number).
3. **Perf pass done on the dev copy**: new endpoints ~37 ms / ~1.5 ms; zero
   regression on existing endpoints; the feeder's no-op re-push still writes
   nothing. Numbers in the status log's 2026-08-04 entry.
4. **WP-15's migration claim moved again**: 7 → 8. The registry records the
   pattern (a parked claim is a reservation, not a number).

**Open on that drop, for a person:** the acceptance checklist (browser pass —
the DOM shim verified behaviour, no human eye has seen the layout), and the
migration probe on a prod copy.

**Still open from earlier drops:** re-retire the tests the un-retire bug
released (search comments for "Automatically un-retired"). The `UNKNOWN`
environment has been dropped and its reader fixed (user-confirmed, 2026-08-04).
Performance Phase 2 is parked indefinitely — production is fast enough and the
user has said no further perf work is wanted right now.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1328 OK (skipped=1) on wp-18-timeline
```

Local validation: `.scratch/` (gitignored) holds this session's tooling —
`perf.db` (dev copy migrated to v7, served on 8901 during the session),
`perf-before.db` + a `master-wt` git worktree (baseline on 8902),
`domshim.mjs` + `drive_timeline.mjs` + `drive_deeplink.mjs` (the node
DOM-shim harness that verified the Timeline page — 41 + 5 checks against a
live server; reusable next session), and the two bench scripts behind the
status-log numbers. Servers do not
survive the session; restart with
`python run_server.py --port 8901 --db .scratch/perf.db` if needed.
Remove the worktree with `git worktree remove .scratch/master-wt` when done.

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here. Frontend changes are verified by driving the real ES
modules against a live server under a minimal DOM shim in node. It cannot
catch layout, colour or contrast.

---

## The work waiting, in the order it wants doing

### 1. Ship the 2026-08-04 drop

Acceptance list in the operator note. Deploy pushes `wp-18-timeline` to
`master`.

### 2. Merge the deferred work back — `wp-14-in-run-progress`

**It must renumber its migration from its old claim to 8 before merging**
(WP-17 took 6, WP-18 took 7; registry §1 records both swaps). It is now
several drops behind: expect real conflicts in `app.js`, `storage.py` (the
upsert now maintains `script_hours` too), and the status log. Its WP-14
progress bar remains useless until WP-15 lands.

### 3. WP-15 — progress pushes from a partial reader *(migration 8 now)*

Fully specified in `UPGRADE_PLAN.md` §WP-15; do not re-derive. Its
`latest_progress` reading of "activity" data maps to `activity_hours`
(same shape, same question) — and note `script_hours` now exists if it needs
script-grain activity.

### 4. WP-16 — site-specific info tab

Still noted, not specified. Static-vs-data-driven is the deciding question.

---

## Needs a person, not a commit

1. **Acceptance-test and ship the 2026-08-04 drop** — everything else is
   behind it.
2. The migration-7 probe on a production copy (number goes into the operator
   note).
3. Re-retire the tests the un-retire bug released (search comments for
   "Automatically un-retired") — still outstanding from 07-31.
4. `tools/diagnose_db.py --compare-local` on the production server — still
   never run.
5. The MariaDB migration dry run (§A of the runbook needs root).

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Add a dated section at the top of `static/whatsnew.html` — for a tester.
   Only what is actually in the build. `data-drop-date` must match the
   heading (`DropDateTest`).
3. **Write `docs/drops/YYYY-MM-DD.md`**, the operator note — required contents
   are in `CLAUDE.md`. If a migration runs, measure it on a prod copy first
   (§1.2).
4. Push the drop branch to `master` after acceptance.
5. On the box: stop the server, copy `testboard.db` + `-wal` + `-shm`, pull,
   start. The copy is the only rollback — a database at version N is refused
   by older code. Do it when the feeder is not importing.
6. Check the drop per the operator note.
