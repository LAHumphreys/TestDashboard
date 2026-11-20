# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-11-20, the default flaky cadence built overnight.**
`/overnight flaky-default-r0` ran the frozen plan unattended on the
owner's go: all seven items, 21 to 27, are committed on
`wp-44-flaky-mark`, pushed to PR #21, each through verify on both
backends, the measured pass where it was on a trigger path, fresh-eyes
review and, for the UI items, a walk. Nothing was parked. **Nothing is
merged or deployed. What is left is the owner's click-through on the play
server, then the drop.**

## Where things stand

- **Production is MariaDB, schema v11**, `--workers 16`, served over HTTPS
  behind a reverse proxy; the Python server speaks plain HTTP behind it.
  **It is the only deployment.** The owner said on 2026-11-19 that the drop
  of 2026-10-13 is live, so production runs `origin/master` (`6cba071`).
  Nothing else was reported of either deploy.
- **Built, not merged, not deployed: WP-44 and, on top of it, the default
  flaky cadence for an environment**, branch `wp-44-flaky-mark`, PR #21,
  head `0f2c755`. **Migrations 12 and 13 run**, both create-only;
  production stays at 11 until the drop. The designs are
  `docs/design/flaky-mark-r0.md` (strip in §12) and
  `docs/design/flaky-default-r0.md` (plan and strip in §6, every item
  ticked).
- **What the default is, as built.** An environment carries a cadence of 2
  to 10 with a required reason; every test in it with no mark of its own
  is judged as a marked test is, on every surface, the per-day chart
  included. It is set, changed and removed on a new **Admin** page, which
  also holds the two settings sections that were at the foot of Open
  Actions. A row under a default draws as a marked row does, with no pill,
  and offers "Mark as flaky" and a link to the Admin page's row. "Failing
  runs per day" waits for its late part and draws once, on every scope.
- **Its cost, measured cold on a quiet machine, SQLite ; a local MariaDB
  12.3, against the bars the owner accepted** (the commits of items 23 and
  24 have the full tables). Home's headline with a default of 2: +2.8 ;
  +5.8 ms at 50 failing tests in the environment (bar +7 to +10 ; +6) and
  +11.8 to +12.7 ; +17.3 to +18.1 at 500 (bar +29 to +31 ; +19 to +33).
  The per-day chart's late part, 14 days: +36 to +37 ; +71 to +75 at 50
  failing a night (bar +70 ; +80) and +135 to +139 ; +583 to +600 at 500
  (bar +250 ; +750). With no default anywhere: no statement added, by
  count, on every read measured, with and without marks; the push path
  runs the same statements; every Known slow row reads "same".
- **One regression was found and fixed inside the build**: Open Actions'
  rows on MariaDB at 50 failing read +12.3 ms on a 57 ms base (the count
  had left its covering index for a scan). It now reads by keys where a
  default is in scope: -6.3 to -6.9 ms at 50 and -8.4 to -9.5 at 500.
- **CI: green on `fc43eed`, all twelve jobs**, Python 3.6.8 with SQLite
  3.26 and both MariaDB 10.3 legs included; that commit has items 21, 22,
  23, 25, 26 and 27. **The runs on `5f0992a` (item 24) and `0f2c755` were
  in progress when this was written: check them first.** One CI failure
  happened and was fixed during the build (item 21's fixture doubled a
  run a second late where SQLite 3.45 rounds a time with a modifier).
- **No ledger in `.claude/work/`, no agent running, no heartbeat.**
- **A play server is up for the owner's click-through**: port 8944, from
  the main checkout at `0f2c755`, on
  `.scratch/play/play-2026-11-20.db` (below). It stops with the session
  that started it.
- **The drop is still to be dated.** What's new's section carries
  `data-drop-date="2026-11-11"` and the operator note is still
  `docs/drops/2026-11-11.md`; it says nothing yet of the default, the
  Admin page or migration 13.
- **The owner said a drop centred on performance should follow this one.**
  Candidates are under "Next session's plan".
- **`_rows_by_key` is written differently on each backend, on purpose**:
  MariaDB uses a row-value `IN`; SQLite is driven from the keys with a
  `CROSS JOIN`, because SQLite 3.26 cannot use an index for the row-value
  form. The backend flag is `rows_by_key_from_keys`.
- **Not reported back from any deploy since 2026-10-01 — ask, do not
  assume:** what the upgrade tool's dry run said; how long the upgrade
  step and the restart took; whether the Metrics page shows schema 11;
  whether anyone has muted a test yet; whether the counters were left on.
- **The upgrade tool is a ledger** (WP-40): a SQLite migration above 7
  without its MariaDB step fails the suite (`LedgerTest`); runbook §G.5 is
  the procedure and §G.6 lists the steps, 11 → 12 and 12 → 13 included.
  Migration 13 is this build's; the parked WP-15 is at 14 in the registry
  (`docs/UPGRADE_PLAN.md` §1).
- **Feeders are never stopped for an upgrade** (runbook §G.3).

## How work starts now

- Anything larger than one brief: `/design-review <what you want>`. One
  change: `/brief`. The main session never edits code (`CLAUDE.md`, "Who
  implements what"). Every implementer runs in its own worktree with an
  assigned sacrificial database (below); the performance pass runs only on
  the trigger paths (`storage.py`, `api.py`, the push path, first paint).
- **A triage surface is compared with its neighbours before it is built**
  (`CLAUDE.md`, "Triage is the morning touch point"): New failures and
  Still failing, column by column and act by act, every difference put to
  the owner by name. The review's walkthrough makes the same comparison.
- `/overnight` runs a frozen, sized plan unattended on the owner's go at
  kickoff; there is no other gate. WP-43, WP-44, WP-44's rework and the
  default flaky cadence were its runs.
- The publication gate's hooks are installed in this clone; any other clone
  runs `python tools/dev/install_hooks.py` once. The term list is
  `.claude/private/terms.txt`, gitignored; it exists on this machine only.

## Next session's plan

1. **The owner's click-through**, on the play server (port 8944; start it
   again if it has stopped, "Verification tooling" has the command). The
   play database's README, `.scratch/play/fd-play-README.txt`, says what
   is in it. A natural order, signed in as the fixture user amy: Home
   scoped to atlas-lab-alpha (the tiles, then the chart's last column and
   caption); the Flaking tab and the Review panel on a row under the
   default and on a marked row; New failures for a test that failed on
   both servers last night; "Change the default" through to the Admin
   page, its History, and the second environment at 3; atlas-lab-delta,
   which has no default, as the control; a build tab in alpha; a passing
   test's own page. What only a browser can show is listed under "Needs a
   person" item 1.
2. **The owner's open points from the build** ("Needs a person" items 2
   and 3): confirm, or ask for a change before the drop.
3. **Ship it** when the owner says so (`/drop`): the operator note renamed
   to the real date and extended (migrations 12 and 13: yes, both
   create-only, the server keeps serving; the Admin page; the default; the
   chart drawing once); What's new dated with it; the README's API section
   (the PUT, the two keys on `GET /api/environments`,
   `status.flaky_defaults`, `flaky.source`); **PR #21's body rewritten**,
   which is the squash-commit message and still describes WP-44's first
   build; a hand `EXPLAIN` on MariaDB 10.3 of the two new statements if a
   10.3 server can be reached (the per-day chart's read and Open Actions'
   keys statement: nothing in the build or CI shows their plans there);
   squash-merge; the housekeeping below; then record what the operator
   reports.
4. **The drop centred on performance** that the owner wants after this
   one. Candidates so far: a failure streak or a per-run flag kept at push
   time (the default's decisions 2 and 24: it would make the headline's
   and the chart's cost independent of how many tests fail, at the price
   of a backfill that stops the server); the list reads at a default of 3
   or more, which read each failing test's recent runs where the third run
   the members already fetch would do (+15 to +44 ms at 500 failing,
   never priced by the design); setting marked tests aside before the
   chart's judgement (47 ms at 1,000 marks on SQLite, not measured on
   MariaDB); the trend part's cost (item 9 under "Needs a person"); the
   queue prefix on a build; deriving the assigned-open tile and the queue
   counts from the assigned-cells statement.
5. **The same gap WP-41 fixed, in the other tools** (one `backend-engineer`
   brief): `prune_runs.py` and `diagnose_db.py` take only `--db` and default
   it to `testboard.db`; `add_site_note.py` defaults `--db` the same way;
   `drop_stream.py` has no MariaDB CLI test and prints a raw exception on a
   connect failure. Nothing on the trigger paths.
6. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
7. Then whatever the users report.

## Where the code is

| | |
|---|---|
| `origin/master` | `6cba071` — the drop of 2026-10-13 (PR #20) on top of the drop of 2026-10-12 (PR #19); live in production, by the owner's word on 2026-11-19 |
| `wp-44-flaky-mark` | **The next drop**, PR #21, head `0f2c755`: WP-44's design arc and items 1 to 20, the default's design, then the build: `50df8bb` (21), `8ba7953` (25), `941d1a0` (21's CI fix), `3a0a158` (22, migration 13), `3335911` (27), `7ee508a` (the owner on item 22), `5707775` (23), `fc43eed` (26), `5f0992a` (24), `0f2c755` (a log entry). Squash-merge, delete |
| `fd-21-perf-wt`, `fd-22-backend-wt`, `fd-23-backend-wt`, `fd-23-perf-wt`, `fd-24-backend-wt`, `fd-24-perf-wt`, `fd-25-ui-wt`, `fd-26-ui-wt`, `fd-27-ui-wt`, `fd-int-wt`, `fd-final-wt`, `fd-23-staged` | the build's snapshot and integration branches, each but the last with a worktree under `.claude/worktrees/fd-*`; their work is in the commits above. Remove the worktrees, then the branches, after the merge. **Agent memory notes from this build sit in those worktrees** (`.claude/agent-memory/` in each, gitignored): copy out what is wanted first |
| `design-flaky-mark` | the WP-44 arc's frozen doc, local only, already the base of the build branch; **delete after the merge** |
| `wp44-11-wt` … `wp44-19-wt`, `wp44-14-base`, `wp44-20-backend-wt`, and the `worktree-agent-*` of 2026-11-12 and 2026-11-19 | WP-44's rework and item 20: snapshot branches and agent worktrees under `.claude/worktrees/`; remove after the merge. **The backend agent's worktree of item 16 holds an unmerged memory note** (how to build SQLite 3.26 in the Linux subsystem): copy it out first if it is wanted |
| `wp44-*-wt` (items 1 to 9), `wp44-5-base`, `wp44-6-head`, `wp44-8-head`, `wp44-10-head`, `wp44-pre-reword`, older `worktree-agent-*` | the first build's snapshot and scratch branches, most with a worktree (fifty-seven worktrees in all, several locked); remove the worktrees, then the branches, after the merge |
| `wp-43-charts-assigned-split`, `design-charts-assigned-split`, `wp43-backend-wt`, `wp43-seed-wt`, `wp43-ui-wt` | merged as PR #20; the three `wp43-*` worktrees and all five branches are still here: remove |
| `admin-2026-10-09-handover` | PR #17, a handover rewrite superseded many times; **close without merging** |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP (WP-15); its migration renumbers to **14** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-42-copy-output`, `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `drop-2026-07-30`, `main` | merged or dead; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03; keep it a mirror of
`origin/master`, never commit to it (it is behind: fast-forward it).

**Registered builds** (plans frozen out of `/design-review`, run by
`/overnight` or the small path; state lives here, never in a skill):

| Plan | Spec | Branch | Status |
|---|---|---|---|
| WP-43: the dashboard charts split assigned from unassigned | `docs/design/charts-assigned-split-r0.md` §8, status strip §9 (frozen 2026-10-13) | `wp-43-charts-assigned-split` | **merged as PR #20**; deployment not reported; one owner decision still open in its §7 (item 9 below) |
| WP-44: mark a test as flaky | `docs/design/flaky-mark-r0.md` §11, contract §1 (frozen 2026-11-11); the triage tab's contract is §14 | `wp-44-flaky-mark`, PR #21 | **built, every item done (1 to 20); not merged, not deployed.** The status strip is the doc's §12. Its item 3 claimed migration 12 |
| A default flaky cadence for an environment, with the Admin page (ships in WP-44's drop) | `docs/design/flaky-default-r0.md` §6, status strip in §6 (frozen 2026-11-19 after round 1) | `wp-44-flaky-mark`, PR #21 | **built, every item done (21 to 27), 2026-11-20; not merged, not deployed.** Item 22 claimed migration 13. Both of §3a's bars are met. Waits on the owner's click-through, then the drop |

## Needs a person, not a commit

1. **No browser has rendered the default, the Admin page or the chart
   drawing once**; every walk was the DOM shim's against a live server.
   For the owner's click-through: whether the Admin table's Flaky default
   cell holds its width with a long reason and its History open, and the
   editing row at narrow widths; where focus visibly lands when an editor
   opens; whether "Loading…" is legible under the chart card's dim and
   the card's height holds between the line and the chart; whether the
   amber "Not split" line is noticed; the spacing of "Change the default"
   after the Mark button.
2. **Decided during the build where the design was silent, put to the
   owner.** Confirmed on 2026-11-19: a `PUT` with no `cadence` key is a
   400; a default's reason is at most 500 characters. Not yet answered:
   **unmarking a test under a default can make that night's run a New
   failure** (history pass, fail, fail; default 2; its own mark at 5: it
   is flaking, and unmarked it is what the default alone makes it), kept
   as built and pinned by test, the alternative costing a lookup per
   failing row on the headline; after a reason-only edit the Admin cell
   reads "set by" whoever reworded it (the History shows who set the
   cadence).
3. **What a default does that the owner should see before the drop.** On
   a two-server environment the tiles count the tests flaking this
   morning and the chart's last column the failed runs tolerated last
   night (22 and 52 on the play database): a test that failed on the
   first server and passed on the second is passing now and in no tab. At
   a default of 2 every such row's Streak cell reads "1 in a row, failing
   after 2", so a Flaking row showing no time (item 5) is felt more.
   Newly fixed drops the morning a default is set. The sentence that
   explains "Mark as flaky" on a row already reading FLAKING is the
   button's title only. **The selection bar's Unmark stays enabled over a
   selection with no marked row**, older than this build: under a default
   it can be clicked on rows reading FLAKING with nothing to remove.
4. **Known gaps left by the build.** `mark_flaky` and `unmark_flaky` have
   the two-writers gap on MariaDB that item 22 closed for defaults (two
   simultaneous fresh marks of one test give a 500); the owner accepted
   it as left for this drop. A held answer for a defaulted test at a
   cadence of 3 or more is not dropped when another process writes the
   database, where a minute's expiry used to bound it. The plan guard for
   Open Actions' keys form catches a return to the bound predicate on the
   MariaDB leg only and pins the form, not a production-size plan. Not
   walked on a live server: a test under a default with no run on a
   stream, and one with a single run.
5. **Mutes and quiet tests, and a Flaking row's time: found on
   2026-11-19's walkthrough, older than item 20, put to the owner and not
   answered.** The Review panel and a test's page offer Mute only for a
   failing test; the mute's wording speaks only of failures; the Muted tab
   shows a quiet muted row as "PASS→PASS". With them: whether New
   failures' and Still failing's empty lines should name muted tests as
   Not run's now does; a build's `stale=1` browse uses mainline's cutoff;
   the Time page's count of excluded quiet tests still includes muted
   ones. And **a Flaking row shows no time**: New failures has "Failed at"
   and Still failing "Failing since"; the row carries the time, so the
   column would cost no statement. Given up with the old tab: "failed N of
   its last 20 runs", and any page that lists every mark.
6. **Two small things, each its own change.** `static/actions.js` holds a
   literal NUL character in its "unassigned" sentinel (three items of this
   build had to edit that file byte-safely): respell it as `\u0000` and
   rework `CoverageTest.test_files_containing_a_nul_byte_are_still_read`
   to plant its own file (`ui-engineer`). And **the code's floor on SQLite
   is 3.15** and is stated nowhere: say so in the README, or decide it
   should be lower.
7. **WP-44, the headline's cost where marks exist** is over the bar its
   design set, cold: SQLite +6.2 ms at 50 marks and +11.4 at 1,000; a
   local MariaDB 12.3 +17.0 and +35.8. Accepted by the owner on
   2026-11-19, for now.
8. **WP-44, the per-day chart's split** is sent after the headline has
   drawn, for every load; with many marks, at 90 days and 1,000 marks, the
   part reads +909 to +1,047 ms. Off first paint.
9. **The trend part's cost is one decision with two causes** (WP-43: 1.5 s
   on SQLite and 2 to 3 s on MariaDB at 90 days with 1,000 assignments;
   WP-44's marks). Accept, a covering index on `runs`, or a cap.
10. **WP-44, behaviour to confirm:** Open Actions' Failing mode leaves out
    the unassigned flaking tests; the Flaking tab is hidden where nothing
    is flaking; "All clear" reads "Set aside: N muted" even with no marks;
    with `retired=1` a retired flaking row reads FLAKING; a test is
    promoted to a new failure only by a run that started after its cadence
    was last set; Open Actions no longer warms Home's headline.
11. **WP-44, known and left:** above SQLite's parameter ceiling the filter
    with Passing chosen costs +42 to +55 ms; a cold open of the Flaking
    tab pays the queue prefix that the headline has normally paid; on a
    test's page, another act while the mark's form is open discards a
    half-typed reason; one MariaDB reading (Home as a page with no marks)
    is unexplained and wants a quiet machine.
12. **Found during WP-44, older than it, not touched:** every bulk endpoint
    resolves each entry's stream with two statements; Mute group can be
    armed again while its request is in flight; the test page's refresh
    after an act swallows its error; the DOM shim's `querySelectorAll`
    matches one class only and drops sortable header labels.
13. **The performance harness's gaps** (`performance-engineer`, a change of
    its own): `ab.py` has no per-side request lists, no warm or
    after-the-headline mode, no MariaDB backend and no three-arm mode; the
    passes of this build did these in scratch drivers, kept under
    `.scratch/overnight/flaky-default/fd-23-perf/` and `fd-24-perf/`.
14. **WP-43, left as built:** the trend's fallback is all-or-nothing; a row
    with every count zero still draws a zero-width red fill and a 2 px gap;
    the full unsliced payload does not carry the trend keys.
15. **Should there be a switch that turns the build delete off**, or
    anything in front of it beyond a typed name?
16. **Build comments — the workshop the owner offered:** mainline lists show
    a test's newest comment of any origin, unlabelled; deleting a build
    clears the tag on its comments; nothing carries from one build to the
    next of the same branch.
17. Carried from 2026-08-11, still open: re-retire the tests the un-retire
    bug released; `tools/diagnose_db.py --compare-local` on prod;
    `max_allowed_packet` persistence with the daemon owners; import
    output-size cap; first Tcl 8.5 site is still an experiment.
18. Carried from WP-31: a build whose last run is more than 36 hours old
    shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
19. **The sanity net's seed is dated August** and fails five clock-dependent
    checks; re-dating it is a brief for `backend-engineer`.
20. **The `ui-engineer` agent type is still not registered** in a session
    (checked 2026-11-19); the three UI items of this build ran as a
    general-purpose Opus agent told to read the definition.
21. **Process, for the owner to decide.** An agent spawned with worktree
    isolation is cut from the default branch, so the main session made
    every worktree itself and named its path in the spawn. To keep the
    backend's three dependent items moving, each was started on a snapshot
    of the one before while that one was still under review; it cost two
    merges and saved the wait. And `/overnight` says an agent that died is
    started fresh from its ledger, never resumed: on 2026-11-20 four agents
    stopped mid-work by an interruption outside the run were instead
    resumed from where they stopped, each told to bring its ledger current
    first, because their ledgers had no hand-off and three were at the
    finish line. If that is the rule wanted, the skill should say so.
22. **The agents' memory notes from WP-44 and from this build are not
    merged** (backend, UI, performance): the owner decides what is kept.

## Known slow, measured, not changed

**Production-scale SQLite (perf-seed/1, `tools/dev/perf/`), cold,
in-process, 32 calls, 2026-10-03, on `a58df48` (shipped code identical to
`49e596d`).** Every measured pass since (WP-43, WP-44, and this build's on
2026-11-19 and 2026-11-20) read every row "same" against this table with
no marks and no default, so it stands; it is updated only from a run on
the merged commit. Read the Metrics page first: if the wait is for a
worker, none of these is the problem. The A/A noise band is ±10 ms on the
two slowest rows, under ±1 ms on the browse rows, and the machine's speed
swings 2× to 5× between runs, so only paired deltas within one run compare.

| | |
|---|---|
| Watch, three cards incl. one full-size build | 312 ms — `compare_counts_many` fetches every pair to classify in Python |
| A comparison page for a full-size build | 268 ms — the pairs query twice, pinned at two by WP-23 |
| Open Actions' rows | 97 ms, never memoized; 154 ms on the 90-night seed because its streaks read the runs history |
| Home headline | 90 ms, mostly the one per-environment pass that is left; WP-43 adds one bounded statement, about 0.3 ms |
| Browse page's count, no assignments | 22 ms |
| Browse page's count with 65 assignments | 28 ms — the assignment rows (WP-40 A/B found it on `master` too) |

**New with WP-43, WP-44 and the default, not Known slow rows:**
`/api/summary?parts=assigned_trend`, off every first paint and memoized
until the next push or act: 23 ms at 14 days and 101 ms at 90 days with 65
assignments on the 90-night seed; with marks, about +8.5 ms at 50 and up
to +1,047 ms at 90 days with 1,000; **with an environment's default in
scope, the figures under "Where things stand"**, growing with the failed
runs in the window and not with the environment's tests; a build tab in
that environment, +22 to +27 ; +39 to +69 ms across a default of 2 and 3.
**Where a default exists** the headline, the queues and the filtered
browse cost what item 23's commit tabulates (a default of 3 or more costs
more than 2: the headline +30.2 ; +45.8 ms at 500 failing, the list reads
+15 to +44). Where marks exist the headline costs 18 to 41% more cold
(item 7 above). With WP-44 Open Actions as a page is 25 to 48% faster on
SQLite and 10% faster to level on MariaDB.

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- **`tclsh` must be on PATH for the suite's counts to compare.** Nine Tcl
  feeder tests exist only where it is; without it nothing is reported as
  skipped, the count is simply nine lower. On this machine the Bash tool
  has it and PowerShell does not. The counts below are with it.
- **No interpreter on this machine's host carries an old SQLite** (3.50 on
  Windows; the Linux subsystem's python3 has 3.45, which is what reproduced
  the one CI failure of this build), so a plan that only SQLite 3.26 gets
  wrong shows first in CI's 3.6.8 jobs. There is no MariaDB 10.3 here
  either: CI's two 10.3 legs check answers, not plans.
- `tools/dev/net/run_net.py` — the six-class sanity net; **it fails five
  checks** on every tree since its August seed went stale (item 19 above).
- `tools/dev/perf/` — the performance engineer's harness. `seed.py` builds
  the production-scale seed (and `--history-nights 90` the 90-night
  variant); `ab.py` runs the cold A/B. **New with this build** (README,
  "The flaky default's fixtures"): `seed.py --onto COPY --default-failing
  N --failing-mix latest|nightly [--two-servers]` builds an environment
  with N tests failing now, or N a night on two servers over 90 nights;
  every rewrite is checked against `runs` and `--census` prints the counts
  a pass checks first; `--flaky-default C` sets a default through the
  tree's own endpoint; `mariadb_load.py` loads a fixture into a sacrificial
  database. The four fixtures the passes measured on are built, at schema
  12, in `.scratch/overnight/flaky-default/fd-21/` (2.0 GB).
  **Never compare trees over HTTP here** — timings vary 2–3× between runs.
  Name the seed beside every number.
- **`node` on PATH** is also what runs the DOM-shim guards in
  `tests/test_frontend_calls.py`; without it those classes skip.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307): start
  `mariadbd.exe` with `--defaults-file=.scratch/mariadb-data/my.ini
  --port=3307`. Four option files, each naming a sacrificial database the
  suite drops and recreates; `/brief` assigns them and `/verify` keeps the
  last (absolute paths in `TESTBOARD_TEST_DB_CNF`):
  `.scratch/mariadb-test.cnf` (agent A), `.scratch/mariadb-test-via.cnf`
  (agent B, also `TESTBOARD_TEST_DB_VIA_UPGRADE=1` runs),
  `.scratch/mariadb-test-c.cnf` (agent C; it holds the 500-a-night fixture
  from the last measured pass), `.scratch/mariadb-test-main.cnf` (the main
  session's `/verify`). It is not 10.3. With three suites sharing it a
  MariaDB leg takes thirteen minutes where it takes seven alone.
- **The play server for the owner's click-through:** `python run_server.py
  --db .scratch/play/play-2026-11-20.db --port 8944 --host 127.0.0.1
  --workers 4`, from the main checkout. The database is the two-server
  fixture shifted so that its newest night is 2026-11-20 (mainline by 53
  days, the builds by 52), with a default of 2 on atlas-lab-alpha, a
  default of 3 on atlas-lab-bravo, twelve marks, and atlas-lab-delta with
  no default as the control; `.scratch/play/fd-play-README.txt` says how
  it was built, which five runs were changed by hand and how to rebuild.
  **It reads the wall clock**: after 2026-11-21 its tests start to read
  "Not run"; rebuild it with a later date. Never the repo-root
  `testboard.db` itself. **Restart the server after any Python change.**
  The server on 8944 and the MariaDB on 3307 were started by the session
  that wrote this and stop with it: check both and start them again.
- **Agents clean up only a directory they created**, by its full path,
  never by pattern in a shared one. The session's scratch directory is
  shared between agents: files there are prefixed with the item's slug.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -4 origin/master   # 6cba071 on top = the drop of 2026-10-13, which production runs
git log --oneline -3                 # on wp-44-flaky-mark: this handover on top of 0f2c755
gh pr list --state open              # expect #9, #17 (close it) and #21 (the next drop)
gh pr checks 21                      # green on fc43eed; the runs for item 24's commit were in progress
python -m unittest discover          # tclsh and node on PATH: expect 3304 OK (skipped 1), SQLite; 4608 OK (skipped 93) with MariaDB
python tools/dev/install_hooks.py    # once per clone: the publication gate's hooks
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
