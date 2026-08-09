# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-09**, after WP-23 (long-running branch streams,
`docs/STREAMS_PLAN.md` §5, **migration 10**) was built on `wp-23-longrunning`,
cut from `wp-22-builds`'s tip. **Built and green on both backends, not yet
accepted, not yet deployed** — the 2026-08-07 drop is still the one running
in production. WP-20+21+22+23 now ship as ONE combined drop; there is
nothing left in the products/streams design (`docs/STREAMS_PLAN.md`) that
was planned and is unbuilt.

> **Tonight's session runs TWO packages, IN THIS ORDER:**
> 1. **[`ONE_KIND_PLAN.md`](ONE_KIND_PLAN.md) (WP-25, user-commissioned
>    2026-08-09 late-day):** collapse the branch/build distinction to
>    one non-mainline kind (`build`) — deletion before first contact,
>    nothing kind-shaped has shipped. Branch `wp-25-one-kind` off
>    `wp-23-longrunning`'s tip.
> 2. **[`SCOPED_URLS_PLAN.md`](SCOPED_URLS_PLAN.md) (WP-24):** one
>    scope-aware URL builder ending the hand-built-URL bug family
>    (incident table in the doc). Branch `wp-24-scoped-urls` off
>    **WP-25's reviewed tip** — WP-25 deletes kind-gated URL sites
>    WP-24 would otherwise preserve, which is why the order matters.
>
> Both docs are self-contained. Suite baseline: **1978 OK (skipped=1)**
> — later than the counts elsewhere in this file, which predate the
> day's usability/perf rounds (see `UPGRADE_PLAN_STATUS.md`).

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7). Confirm the box runs the FINAL drop commit `310f1c0` — What's new saying "7 August 2026" is the tell. Nothing below has been deployed |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` so the recorded state matches the deployed one |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. All four CI legs green |
| `wp-20-products` / `wp-21-streams` | **superseded as ship candidates** by `wp-23-longrunning` below, which contains both in full |
| `wp-22-builds` | WP-20+21+22 combined, migration 8+9, no migration of its own. **Superseded as a ship candidate by `wp-23-longrunning` below**, which contains it |
| `wp-23-longrunning` | **WP-23 (long-running branch streams), migration 10 — the current ship candidate, contains WP-20+21+22+23 in full.** A long-running branch stream now gets a two-tab dashboard ("Its own results" / "Difference from mainline"); `activity_hours`/`script_hours` are maintained for every stream, not only mainline's; `/api/summary`/`/api/time`/`/api/timeline` all accept `stream=`; the delta view states drift ("of N failing here, M fail on mainline too"); the Watchlist `s:` card is deliberately unchanged (documented decision, not a gap). Suite green on both backends. Operator note is `docs/drops/2026-08-14.md` — **date provisional**, re-date before pushing if it ships on a different day |
| `wp-14-in-run-progress` | parked WIP; its migration is now **11** (WP-20 took 8, WP-21 took 9, WP-23 took 10 — the registry note in `UPGRADE_PLAN.md` §1 tracks this renumbering, now five swaps deep) — renumber before merging |

Suite on `wp-23-longrunning`: **1750 green** (skipped 1) SQLite-only; **2329
green** (skipped 18) with `TESTBOARD_TEST_DB_CNF` set against this dev
machine's local `mariadbd` (port 3307, `.scratch/mariadb-test.cnf`) — both
on the FINAL tree, re-run after every change this session. **CI's own
`python36-mariadb` leg (mariadb:10.3, prod's actual stream) has not been
observed against this branch** — the local server is a newer MariaDB,
functional evidence only, never a perf number.

**Migration 10 measured this session** (dev copy, 220 MB / 540,192 runs /
12,008 tests, brought to v7 first): entry 10 alone **0.038–0.041s**;
combined v7→v10 **~0.17–0.18s**, both reproduced across repeated runs.
This differs from the v7→v9 figure recorded in the WP-21 session
(0.806s) — measured on the same machine, at a different time, no attempt
made to reconcile the two beyond noting it (see `docs/drops/2026-08-14.md`
and `UPGRADE_PLAN_STATUS.md`'s WP-23 entry). Neither rebuild
(migration 9's `latest_runs`, migration 10's `activity_hours`/
`script_hours`) scales with `runs` — both are bounded by test/test-hour
counts, so production's 4.4M rows should not multiply the pause the way a
`runs`-table backfill would. **Not measured on a production-sized copy.**

**WP-23 got the same real-server-plus-DOM-shim treatment** WP-21/22
established, this session (the third time this method has been used): a
scratch server seeded with two products, a short-lived one-off branch (1
covered pass) and a long-running branch (8 nightly covered passes over 8
nights, a standing regression plus one failure that also hits mainline
from night 7), driven by the node DOM-shim harness with real `click()`
events on the two-tab header's buttons. All checks passed — band text,
tab visibility/default-selection for both branches, the caption's exact
wording (states the real covered-pass count and the "2 or more"
threshold), the branch's own FAIL count differing from mainline's, both
tab-switch directions, the drift line's exact wording, and a genuine
mainline load touching none of the new elements. One setup wrinkle worth
knowing if this method is reused: the shim's `Element` defaults
`hidden=false` (it does not parse the real HTML), so a mainline check
needs its `hidden` state seeded to match what the shipped markup ships
with, or elements that JS never touches (because it never needs to) read
as visible when they are not. Full account in
`docs/UPGRADE_PLAN_STATUS.md`'s WP-23 entry and
`docs/drops/2026-08-14.md`'s "What was NOT verified" section.

**SQLite is a permanent first-class backend** (user requirement, 2026-08-07):
`--db PATH` unchanged forever, zero-setup second instances, both backends
gated in CI. MariaDB is opt-in per instance via
`--db-config /etc/testboard/db.cnf --site-notes PATH`. The server never runs
DDL on MariaDB — schema comes from the migration tooling only.

The repo-root `testboard.db` is generated dev data (220 MB, 540,192 runs,
**v5** as of this session — never opened with current code for anything
that migrates or writes, only ever copied). Production is ~900 MB /
~4.4M runs at **v7** on a network mount. Say which one any number came
from. **The dev machine also has MariaDB 12.3.2** (winget, x86_64 under
emulation, port 3307, datadir `.scratch/mariadb-data`, root password in
`.scratch/mariadb-test.cnf`): start with
`& "C:\Program Files\MariaDB 12.3\bin\mariadbd.exe" --datadir=<repo>\.scratch\mariadb-data --port=3307 --console`,
then `$env:TESTBOARD_TEST_DB_CNF=".scratch\mariadb-test.cnf"` activates the
dual-backend tests. Functional evidence only — never quote a perf number
from it.

## The immediate thing

**Two independent threads are both live right now — do not conflate them.**

**Thread A — accept and ship the combined WP-20+21+22+23 drop
(`wp-23-longrunning`).** Suite-green on both backends, not deployed, not
reviewed. Before it ships:

1. Read `docs/drops/2026-08-14.md`'s acceptance list in full — WP-23 added
   its own items (the two-tab header's default selection on real cadence,
   the drift line at a real screen size, Time/Timeline with `stream=`, the
   Watchlist `s:` card staying unchanged) on top of WP-20/21/22's already-
   long list.
2. Re-date the drop note and `whatsnew.html` if the ship day is not the
   14th (`DropDateTest` catches a mismatch between the two, not a date
   that is wrong in the same way in both places).
3. Feed real branch results in against a scratch copy over SEVERAL real
   nights (not one burst) so a branch actually accumulates 2+ covered
   passes and the two-tab header's default has something real to react
   to — then walk the acceptance checklist before it goes anywhere near
   production.
4. Run the migration-8+9+10 probe against a copy of **production** (not
   just the dev copy measured so far) and record the number in the drop
   note before shipping — the dev-copy number has now been measured
   TWICE, in two different sessions, with two different results (see
   above); a production number has never been taken at all.
5. Push `wp-23-longrunning` to `master` and follow the drop note's
   upgrade procedure (three migrations run in one restart).

`wp-14-in-run-progress` must renumber its migration to **11** before it can
merge, now that WP-20 has taken 8, WP-21 has taken 9, and WP-23 has taken 10.

**Thread B — the MariaDB migration itself, independent of the above.** Steps 1
of the plan (deploy; migration 7; Timeline acceptance) happened 2026-08-07.
What remains: (2) §A server prep on the new account (target server confirmed
MariaDB 10.3.39 via client banner — re-confirm server-side with
`SELECT VERSION()`); (3) §C preflight, §E.1 **dry run on a prod copy**, then
the §E cutover with the freeze and feeder catch-up. Read
[`docs/drops/2026-08-07.md`](drops/2026-08-07.md) for the whole procedure
and the two small follow-ups (confirm the box is on `310f1c0`; push the
branch to `master`). **Note for whoever runs this:** by the time this
cutover happens, the schema may already be at v10 (if Thread A ships first)
— the migration tool and the exporter DDL both need to be from a checkout
that knows `streams`/`stream_id`/the assignment `stream_id` columns AND the
`activity_hours`/`script_hours` `stream_id` columns (WP-23), the same
version-must-match rule that already applied for `script_hours` at v7.

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

**The products/streams design (`docs/STREAMS_PLAN.md`) is now fully built**
(§§0–5, drops 1–4). Nothing further is planned there unless real usage of
WP-23 surfaces a gap.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1750 OK (skipped=1) on wp-23-longrunning
```

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here. Frontend changes are verified by
`tests/test_frontend_calls.py` (static analysis of the actual `.js` source)
and, for pieces that carry real risk, by driving the actual JS through the
node DOM-shim harness in `.scratch/` against a real running server via a
real dynamic `import()` — WP-23's pass this session is the THIRD time this
method has been used, after WP-21's first human use and WP-22's pass, and
each time it has found something a purely static check would have missed.
Neither proves layout, colour, contrast, or whether a page is too much for
one screen — only a real browser does, and no drop so far has had one.

---

## The work waiting, in the order it wants doing

### 1. Accept and ship the combined WP-20+21+22+23 drop

See Thread A above. This is now the single largest piece of unshipped,
unreviewed work in the repo — four work packages, one drop, three schema
migrations, a whole new comparison-and-triage surface, compare-any-two, and
a second full dashboard scoped to a branch.

### 2. Ship the 2026-08-07 drop's remaining MariaDB cutover steps

See Thread B above — independent of Thread A, and can proceed in parallel.

### 3. Merge the deferred work back — `wp-14-in-run-progress`

**Migration renumbers to 11 before merging** (registry §1, fifth
renumbering). Now further behind four whole work packages: expect
conflicts in `app.js`, `storage.py` (the backend seam, the upsert
maintaining `activity_hours`/`script_hours` per stream now, `_pass_view`),
and the status log.

### 4. WP-15 — progress pushes from a partial reader *(migration 11)*

Fully specified in `UPGRADE_PLAN.md` §WP-15. Note for the MariaDB era: its
migration entry is SQLite-only by definition; if the estate has cut over,
its schema change ALSO needs adding to the exporter DDL + a §D reload (or
hand-applied DDL via `testboard_migrate`) — decide when it lands.

### 5. WP-16 — site-specific info tab

Still noted, not specified.

## Needs a person, not a commit

1. **Acceptance-test and ship the combined WP-20+21+22+23 drop** — the
   longest list of any drop so far; see `docs/drops/2026-08-14.md`.
2. Migration-8+9+10 probe on a prod copy (number into the drop note) —
   the dev-copy number has now disagreed with itself across two sessions;
   a production number matters more than usual here.
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
