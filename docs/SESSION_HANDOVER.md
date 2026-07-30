# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-07-30, late**, after building the 2026-07-31 drop overnight on
the user's instruction ("this will be it for tomorrow's drop").

---

## Where the code is

| | |
|---|---|
| `origin/master` | `982c28e` — **deployed**: the 2026-07-30 drop is live in production |
| `origin/candidate-keepalive-fix` | `3ce6b93` — tests+docs only, runtime-identical to master; **not merged yet** |
| `wp-17-summary-perf` | **the 2026-07-31 drop** — built on `3ce6b93`, so merging it also lands the candidate |
| `wp-14-in-run-progress` | parked WIP; see the migration renumbering warning below |

Suite: **1288 green** (skipped 1) on `wp-17-summary-perf`, up from 1268. Schema moves
to **migration 6** — the first migration since launch, so **the rollback is the
database copy**, not `git checkout`.

Production database is ~900 MB / ~4.4M runs on a network mount. The repo-root
`testboard.db` is generated dev data (218 MB, 540,192 runs); `validate.db` is the
local acceptance copy currently served on port 8000 **by the new build** (already
migrated to v6). Say which one any number came from.

## The immediate thing

**The 2026-07-31 drop awaits acceptance.** Read
[`docs/drops/2026-07-31.md`](drops/2026-07-31.md) — it has the deploy commands, the
migration expectations, the rollback, and the acceptance checklist. Headlines:

1. **Production's own perf log found the fault**: the staleness-cutoff bucket query
   was a full scan of the runs UNIQUE index — O(total history), 3.5 s mean in
   production, 70% of `/api/summary`, growing nightly. Fixed with `activity_hours`,
   the third derived table (migration 6). Dev-copy numbers: that query 607 ms →
   2.3 ms; `/api/summary` 751 → ~190 ms; `/api/time` 630 → 40 ms.
2. **The site feeder re-pushes ~10k records every 10 minutes** (user disclosure).
   Byte-identical re-imports now write NOTHING (was ~23 MB WAL per push) — and that
   same fix makes **retirement stick**; before it, any re-push un-retired every
   retired test within 10 minutes. Real production bug, testers will have seen it.
3. **`/api/summary` grew `parts=`** (headline / one queue) and the home page paints
   progressively; other queue tabs fetch on first click.

**Open on that drop, for a person:** the acceptance checklist (browser pass — nothing
has been seen by a human eye), the migration-duration probe on a prod copy (§1.2
requires the number before shipping; the note has the one-liner), and after a night
live, re-run `tools/perf_report.py` to re-rank what is slow NOW before doing Phase 2.

**Still open from the previous drop:** drop the `UNKNOWN` environment on the server
(tool is ready), fix the site reader that caused it, decide `--site-notes` visibility.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1288 OK (skipped=1) on wp-17-summary-perf
```

Local validation server: `http://127.0.0.1:8000/` is (or was) running the wp-17 build
against `validate.db` with `--perf-log validate-perf.log`. The pre-drop logs were set
aside as `validate-*-before.log`. If it is down:

```bash
python run_server.py --port 8000 --db validate.db --perf-log validate-perf.log
```

**If the UI looks wrong, check you restarted the server.** Static files are read per
request; the Python is whatever was imported at process start. Twice this has looked
exactly like a UI bug.

There is no browser here. Frontend changes are verified by driving the real ES modules
against a live server under a minimal DOM shim in node (three real bugs found that way
so far). It cannot catch layout, colour or contrast.

---

## The work waiting, in the order it wants doing

### 1. Ship the 2026-07-31 drop

Acceptance list in the operator note. Deploy merges `wp-17-summary-perf` to `master`
(which brings the candidate-keepalive-fix commits with it — they are runtime-identical
tests+docs).

### 2. Phase 2 of the performance plan — AFTER a night of production perf log

Deliberately parked, with prepared responses, until production numbers re-rank the
remaining terms (see the 2026-07-31 status-log entry): batch `failure_streak_bounds`
per queue the way `recent_results` chunks; audit queue payload size (6×500
comment-joined rows); the small latest_runs scans; worker count on the mount. Do not
build any of it from dev-copy numbers.

### 3. Merge the deferred work back — `wp-14-in-run-progress`

**It must renumber its migration from 6 to 7 before merging** — WP-17 took 6 (the
registry in `UPGRADE_PLAN.md` §1 records the swap and why). It is also now several
drops behind `main`-line history; expect real conflicts in `app.js` (progressive
loading landed), `storage.py` (upsert rewritten for the no-op skip), and the status
log. The branch's WP-14 progress bar remains useless until WP-15 lands.

### 4. WP-15 — progress pushes from a partial reader *(migration 7 now)*

Fully specified in `UPGRADE_PLAN.md` §WP-15; do not re-derive. One update to its
spec-reading: `latest_progress` building a row "from `run_progress` alone because the
environment has nothing in `activity_buckets`" — activity data now lives in
`activity_hours`, same shape, same question.

### 5. WP-16 — site-specific info tab

Still noted, not specified. Static-vs-data-driven is the deciding question.

---

## Needs a person, not a commit

1. **Acceptance-test and ship the 2026-07-31 drop** — everything else is behind it.
2. The migration probe on a production copy (number goes into the operator note).
3. Re-retire the tests the un-retire bug released (search comments for
   "Automatically un-retired").
4. Fix the site reader that files runs under `UNKNOWN`; then drop the environment.
5. `tools/diagnose_db.py --compare-local` on the production server — still never run.
6. The MariaDB migration dry run (§A of the runbook needs root).

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Add a dated section at the top of `static/whatsnew.html` — for a tester. Only what
   is actually in the build. `data-drop-date` must match the heading (`DropDateTest`).
3. **Write `docs/drops/YYYY-MM-DD.md`**, the operator note — required contents are in
   `CLAUDE.md`. If a migration runs, measure it on a prod copy first (§1.2).
4. Push the drop branch to `master` after acceptance.
5. On the box: stop the server, copy `testboard.db` + `-wal` + `-shm`, pull, start.
   The copy is the only rollback — a database at version N is refused by older code.
   Do it when the feeder is not importing.
6. Check the drop per the operator note, including `N of N counted` under Open
   actions → Environment expectations.
