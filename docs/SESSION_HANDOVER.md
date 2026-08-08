# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-08**, after WP-21 (branches/builds beside mainline,
migration 9) was built on `wp-21-streams`, then — the same day — put in
front of a real browser for the first time. Four gaps found in that first
use are now fixed on the same branch, in the same still-unshipped drop.
**Built and green on both backends, not yet accepted, not yet deployed** —
the 2026-08-07 drop is still the one running in production.

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7). Confirm the box runs the FINAL drop commit `310f1c0` — What's new saying "7 August 2026" is the tell. Neither WP-20 nor WP-21 below has been deployed |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` so the recorded state matches the deployed one |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. All four CI legs green |
| `wp-20-products` | WP-20 (products + Watchlist), migration 8. **Superseded as a ship candidate by `wp-21-streams` below**, which contains it — ship the combined drop, not this branch alone, unless WP-21 is deliberately deferred |
| `wp-21-streams` | WP-21 (branches/builds beside mainline), migration 9, built on top of WP-20, then extended with four fixes found in first human use (branch band on the test page, triage-from-a-branch, assignment origin annotation, Open actions' origin filter/tag — see below). Suite green on BOTH backends. Operator note is `docs/drops/2026-08-14.md` — **date provisional**, re-date before pushing if it ships on a different day. Acceptance list is at the bottom of that note, and is now longer than any previous drop's |
| `wp-14-in-run-progress` | parked WIP; its migration is now **10** (WP-20 took 8, WP-21 took 9 — the registry note in `UPGRADE_PLAN.md` §1 tracks this renumbering) — renumber before merging |

Suite on `wp-21-streams`: **1688 green** (skipped 1) SQLite-only; **2228
green** (skipped 16) with `TESTBOARD_TEST_DB_CNF` set against this dev
machine's local `mariadbd` (port 3307, `.scratch/mariadb-test.cnf`) — both
on the FINAL tree, re-run after every change this session. **CI's own
`python36-mariadb` leg (mariadb:10.3, prod's actual stream) has not been
observed against this branch** — the local server is a newer MariaDB,
functional evidence only, never a perf number.

**This is now the first branch in this project's history to have been
opened in a real browser** — still the only page ever to receive that, and
it found real bugs immediately: `eb05c7a` (a delta-table row's link forgot
its own stream id and landed on the mainline test page — one line, now
guard-tested). Working through it with the user surfaced four more, all
fixed on this same branch, same drop:

1. **The branch band was dashboard-only.** `test.html` had the full
   `?stream=` scoping (history, analytics, the compare strip) but nothing
   loud saying so. `renderBranchBand` is now exported from `compare.js` and
   shared by both pages; "Back to mainline" generalised to "the current URL
   with only `stream` removed" (preserves the test page's own
   environment/script/test_name instead of bouncing to a fixed
   `index.html`).
2. **Triage didn't actually work from a branch.** The delta table was
   chips-only. It now carries the same assignee select and inline Review
   expander every other list in the app has — assigning from a branch row
   assigns the SAME test everyone sees, retirement is refused by
   construction (the shared panel is simply never given a staleness cutoff
   there). `/api/compare` rows gained `stream_run_id`/`stream_start_time`
   (the branch's own run) and the triple's current, unpartitioned
   `assignee` — no new query shape, both already live on `latest_runs`/
   `current_assignments`.
3. **Assignment origin folded into migration 9** (still unshipped when
   found, so no database anywhere has the narrower shape) —
   `assignments`/`current_assignments` gained a nullable `stream_id`, the
   exact shape `comments.stream_id` already established. `PUT
   .../assignee` accepts it optionally.
4. **Open actions shows the origin**: a "branch feat/x" tag per row and a
   server-side `origin=branch`/`origin=mainline` filter — absent entirely
   (not just empty) when no assignment anywhere carries a stream.

All four were driven against a real scratch server through the project's
node DOM-shim harness, not just source-scanned — the harness itself grew
(`insertBefore`/`nextSibling`/`remove()`, `document.createDocumentFragment
()`, a `find()` guard against text-node leaves; `actions.js` had never been
driven through row-rendering before). It confirmed the assignee select
showed a REAL pre-existing value rather than defaulting to "Unassigned"
(the specific wrong-payload failure that was flagged as the likeliest risk
before it was checked), the Review panel opened onto the branch's own
captured output, no retire control appeared anywhere, both bands and their
back-links were correct, and the Open actions filter/tag round-tripped a
real assignment end to end. **Still true: no browser has rendered the
delta view's layout, contrast, or whether five tiles plus a tabbed table is
too much page for one screen** — the shim proves wiring and data flow, not
appearance. Full account, per finding, is in `docs/drops/2026-08-14.md`.

**Migration timings, measured on the 220 MB / 540,192-run dev copy (NOT
production — production is ~4x this size), brought to v7 first (production's
current version) so the number is what the box will actually see:**
migration 8 alone 26.8 ms (O(1)); migration 9 alone (v8→v9) 0.883 s total,
of which 0.6 s is the `latest_runs` rebuild (12,008 rows — the one part of
either migration that scales with estate size); **combined v7→v9, 0.806 s**
— the number that matters for the actual upgrade. These numbers predate
this session's four fixes but are unaffected by them: none touches
`latest_runs`'s shape, only `assignments`/`current_assignments` (small
tables, O(1) ALTERs). Full detail and the measurement method (a read-only
`git archive` of `wp-18-timeline`'s `storage.py`, bringing a scratch copy to
exactly v7 without touching any branch) is in the drop note.

**SQLite is a permanent first-class backend** (user requirement, 2026-08-07):
`--db PATH` unchanged forever, zero-setup second instances, both backends
gated in CI. MariaDB is opt-in per instance via
`--db-config /etc/testboard/db.cnf --site-notes PATH`. The server never runs
DDL on MariaDB — schema comes from the migration tooling only.

The repo-root `testboard.db` is generated dev data (220 MB, 540,192 runs,
**still v5** — never opened with current code, only ever copied). Production
is ~900 MB / ~4.4M runs at **v7** on a network mount. Say which one any
number came from. **The dev machine also has MariaDB 12.3.2** (winget,
x86_64 under emulation, port 3307, datadir `.scratch/mariadb-data`, root
password in `.scratch/mariadb-test.cnf`): start with
`& "C:\Program Files\MariaDB 12.3\bin\mariadbd.exe" --datadir=<repo>\.scratch\mariadb-data --port=3307 --console`,
then `$env:TESTBOARD_TEST_DB_CNF=".scratch\mariadb-test.cnf"` activates the
dual-backend tests. Functional evidence only — never quote a perf number
from it.

## The immediate thing

**Two independent threads are both live right now — do not conflate them.**

**Thread A — accept and ship the combined WP-20+WP-21 drop
(`wp-21-streams`).** Suite-green on both backends, not deployed, not
reviewed. Before it ships:

1. Read `docs/drops/2026-08-14.md`'s acceptance list in full — it is the
   longest of any drop so far, because this is the first WP-21 surface a
   human has actually used, and the four fixes above are new ground:
   assign/review from a branch row, the origin tag/filter on Open actions,
   the test-page band.
2. Re-date the drop note and `whatsnew.html` if the ship day is not the
   14th (`DropDateTest` catches a mismatch between the two, not a date
   that is wrong in the same way in both places).
3. Feed a real branch or build's results in against a scratch copy and
   walk the acceptance checklist before it goes anywhere near production —
   this session's own driver scripts are a starting point, not a
   substitute; they proved data flow, not appearance.
4. Run the migration-8+9 probe against a copy of **production** (not just
   the dev copy measured so far) and record the number in the drop note
   before shipping.
5. Push `wp-21-streams` to `master` and follow the drop note's upgrade
   procedure (two migrations run in one restart).

`wp-14-in-run-progress` must renumber its migration to **10** before it can
merge, now that WP-20 has taken 8 and WP-21 has taken 9.

**Thread B — the MariaDB migration itself, independent of the above.** Steps 1
of the plan (deploy; migration 7; Timeline acceptance) happened 2026-08-07.
What remains: (2) §A server prep on the new account (target server confirmed
MariaDB 10.3.39 via client banner — re-confirm server-side with
`SELECT VERSION()`); (3) §C preflight, §E.1 **dry run on a prod copy**, then
the §E cutover with the freeze and feeder catch-up. Read
[`docs/drops/2026-08-07.md`](drops/2026-08-07.md) for the whole procedure
and the two small follow-ups (confirm the box is on `310f1c0`; push the
branch to `master`). **Note for whoever runs this:** by the time this
cutover happens, the schema may already be at v9 (if Thread A ships first)
— the migration tool and the exporter DDL both need to be from a checkout
that knows `streams`/`stream_id`/the assignment `stream_id` columns, the
same version-must-match rule that already applied for `script_hours` at v7.

**Before migration day:** the runbook was re-reviewed and corrected
2026-08-07 (see the status log's entries of that date). §C's preamble
matters most — UTF-8 locale, and the tool-must-match-database-version rule.

**Still open from earlier drops:** re-retire the tests the un-retire bug
released (search comments for "Automatically un-retired");
`tools/diagnose_db.py --compare-local` on the production server, never run.
Performance Phase 2 remains parked; MariaDB perf is unmeasured everywhere
and only the real box may produce numbers. The WP-21 legacy-UNIQUE
collision path (docs/STREAMS_PLAN.md §3.2 — a branch and mainline reporting
the identical test at the identical microsecond) is unit-tested on both
backends but has never been provoked against a real feed.

**Explicitly requested, deferred to WP-22:** a per-stream result switcher
("Every build") on the test-detail page — the user asked for it directly
during this session's review; it is recorded in `docs/STREAMS_PLAN.md` §4.1
so it is not dropped when that drop is planned.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1688 OK (skipped=1) on wp-21-streams
```

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here, except that this drop has now had one used
against it once, by a person, outside this environment — the fixes above
are what came back from that. Frontend changes are otherwise verified by
`tests/test_frontend_calls.py` (static analysis of the actual `.js` source)
and, for pieces that carry real risk, by driving the actual JS through the
node DOM-shim harness in `.scratch/` against a real running server. Neither
proves layout, colour, contrast, or whether a page is too much for one
screen — only a real browser does, and this drop has had exactly one look.

---

## The work waiting, in the order it wants doing

### 1. Accept and ship the combined WP-20+WP-21 drop

See Thread A above. This is now the single largest piece of unshipped,
unreviewed work in the repo — two work packages, one drop, two schema
migrations, a whole new comparison-and-triage surface, and the first real
human feedback this project has had on any of it.

### 2. Ship the 2026-08-07 drop's remaining MariaDB cutover steps

See Thread B above — independent of Thread A, and can proceed in parallel.

### 3. Merge the deferred work back — `wp-14-in-run-progress`

**Migration renumbers to 10 before merging** (registry §1, third
renumbering). Now further behind two whole work packages: expect conflicts
in `app.js`, `storage.py` (the backend seam, the upsert maintaining
`activity_hours`/`script_hours`, and now the stream partition), and the
status log.

### 4. WP-15 — progress pushes from a partial reader *(migration 10)*

Fully specified in `UPGRADE_PLAN.md` §WP-15. Note for the MariaDB era: its
migration entry is SQLite-only by definition; if the estate has cut over,
its schema change ALSO needs adding to the exporter DDL + a §D reload (or
hand-applied DDL via `testboard_migrate`) — decide when it lands.

### 5. WP-16 — site-specific info tab

Still noted, not specified.

### 6. WP-22 — cross-branch baselines, release builds

Deliberately deferred out of WP-21: `/api/compare?baseline=` refuses
anything but mainline in this drop with an explicit 400 naming WP-22. Now
also carries the explicit "Every build" dropdown request noted above — see
`docs/STREAMS_PLAN.md` §4 for the rest of what is already specified there.

## Needs a person, not a commit

1. **Acceptance-test and ship the combined WP-20+WP-21 drop** — the
   longest list of any drop so far; see `docs/drops/2026-08-14.md`.
2. Migration-8+9 probe on a prod copy (number into the drop note).
3. §A on the MariaDB server (root), §E.1 dry run, cutover decision.
4. Re-retire the tests the un-retire bug released.
5. `tools/diagnose_db.py --compare-local` on the production server.

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Dated section at the top of `static/whatsnew.html`, `data-drop-date`
   matching the heading (`DropDateTest`).
3. Operator note `docs/drops/YYYY-MM-DD.md` per `CLAUDE.md`; measure any
   migration on a prod copy first (§1.2).
4. Push the drop branch to `master` after acceptance.
5. On the box: stop, copy `testboard.db` + `-wal` + `-shm`, pull, start —
   when the feeder is not importing.
6. Check the drop per the operator note.
