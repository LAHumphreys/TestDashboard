# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-11-12, the owner's first look at WP-44 and the
rework of its triage tab.** The owner tried the build on a local server and
overturned the tab it had added to Home's Triage twice in one morning:
first to hold only the tests flaking now, then to be a queue like its
neighbours, with the run's output, the Review panel and a streak on every
row. Both are built, verified on both backends, measured, reviewed and
committed on the same branch. Three more items followed the same day: CI
caught the tab's keyed read scanning a whole stream on the old SQLite of
the 3.6.8 target (fixed, and green there), the owner could not find
"Mark as flaky" on a test's own page (it is in the header now), and the
cadence is worded as "after" throughout: the select opens on "fail
after…" and the sentence and the Streak cell beside it follow. The design
doc's §13 and §14 have the verdicts, how the first design came about, and
the plan that was run; its §12 strip has every item.

## Where things stand

- **Production is MariaDB, schema v11**, `--workers 16`, served over HTTPS
  behind a reverse proxy; the Python server speaks plain HTTP behind it.
  **It is the only deployment.** The last deploy the owner reported was the
  2026-10-01 drop (`49e596d`, PR #14), with only "Deployed" said of it.
- **The drops of 2026-10-12 (PR #19) and 2026-10-13 (PR #20) are merged**:
  `origin/master` is `6cba071`. **Whether either has been deployed is not
  reported: ask.**
- **Built, not merged, not deployed: WP-44, the drop of 2026-11-11**
  (provisional date), branch `wp-44-flaky-mark`, PR #21. Items 1 to 10 are
  the first build (to `0406e68`); items 11 to 15 are the rework of the
  triage tab (`b6f0162` backend, `6da4512` UI); item 16 is the keyed read
  on an old SQLite (`77f12d2`); item 17 the test page's header offer
  (`4b96426`); item 18 the cadence select's placeholder (`3d287b5`);
  item 19 the sentence and the Streak cell saying "after" as the select
  does (the branch's last commit); the documents between them.
  **Migration 12 runs** (create-only: `test_flaky_marks`,
  `flaky_mark_history`, one index); the upgrade tool's ledger has the step.
  **Production stays at 11 until the drop.** The server's Python changed, so
  the restart is required.
- **What WP-44 is:** a person marks a test as flaky with a cadence ("fail if
  the N most recent runs fail"). A mark changes what counts as a failure
  for that test; it is not a mute. Under its cadence a failing marked test
  is *flaking*: off the failing figures, out of New failures and Still
  failing, in its own colour, never a pass, and listed in Triage's
  **Flaking** tab with its streak against its cadence. The night it fails
  past its cadence it is a new failure again and sorts as one. A marked
  test that passes needs no triage and is in no tab. All tests has a
  FLAKING filter; Open Actions leaves out the flaking tests nobody owns.
  With no marks every read runs the statements it ran before.
- **The Flaking tab is a triage queue**, `GET
  /api/summary?parts=queue&queue=flaking`, drawn by the loop that draws
  every tab, with the Output column and the Review panel on every row.
  There is no `GET /api/flaky` and no page that lists every mark.
  `QueueTabReviewTest` fails the build if any triage tab's row lacks the
  Review button.
- **A mark is made from** the selection bar, the Review panel, or the
  header of the test's own page, where an unmarked test shows "Flaky" and a
  "Mark as flaky" button beside the Mute offer, whatever its result.
- **CI is green on `3d287b5`, all twelve jobs**: the six Python 3.6.8 jobs
  (the old SQLite) and both MariaDB 10.3 jobs, with the plan guards of the
  Flaking queue's rows and of Open Actions' page by keys. Item 6h (a
  dialect hook) is not needed. What followed that commit is item 19: two
  strings in `static/api.js`, the What's new line that quotes one, their
  guards, and documents; **CI on that last push had not finished when
  this was written.**
- **`_rows_by_key` is written differently on each backend, on purpose**:
  MariaDB uses a row-value `IN`; SQLite is driven from the keys with a
  `CROSS JOIN`, because SQLite 3.26 cannot use an index for the row-value
  form. The backend flag is `rows_by_key_from_keys`.
- **Not reported back from the 2026-10-01 deploy — ask, do not assume:**
  what the upgrade tool's dry run said (`SERVER: may keep running`
  expected); whether the start-up output named the orphaned build; how long
  the upgrade step and the restart took; whether the Metrics page shows
  schema 11; whether anyone has muted a test yet. Carried from 2026-09-30:
  were the counters left on; what "Waited, mean" reads during a run.
- **No ledger in `.claude/work/`.** Every item is committed and its ledger
  deleted. The run's heartbeat was session-only and is deleted.
- **The upgrade tool is a ledger** (WP-40): a SQLite migration above 7
  without its MariaDB step fails the suite (`LedgerTest`); runbook §G.5 is
  the procedure and §G.6 lists the steps, 11 → 12 included. Migration 13 is
  reserved by WP-15 (parked).
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
  kickoff; there is no other gate. WP-43, WP-44 and WP-44's rework were its
  runs; the rework was steered by the owner while it ran.
- The publication gate's hooks are installed in this clone; any other clone
  runs `python tools/dev/install_hooks.py` once. The term list is
  `.claude/private/terms.txt`, gitignored; it exists on this machine only.

## Next session's plan

1. **Read CI on PR #21 at the branch's head** (`gh pr checks 21`). It was
   green on `3d287b5`; the commit after it changes two strings in a static
   file, the What's new line that quotes one, their guards, and documents.
2. **The owner looks at the Flaking tab and a test's page in a browser**
   and decides the things under "Needs a person" items 1, 2 and 3. Anything
   overturned is a rework on the same branch before the merge.
3. **Ship it** when the owner says so (`/drop`): set the real date in the
   operator note and in What's new together; **rewrite PR #21's body**, which
   still describes the first build's tab and is the squash-commit message;
   squash-merge; then the housekeeping below; then record what the operator
   reports.
4. **Ask whether the drops of 2026-10-12 and 2026-10-13 were deployed**, and
   get the 2026-10-01 answers above into the log.
5. **The trend part's cost**, with two causes (many assignments from WP-43,
   many marks from WP-44): one owner decision, one covering index if
   chosen. A `backend-engineer` package with a migration registry row, a
   ledger step and an exporter table, measured on the push path.
6. **The queue prefix on a build** (found by the rework's measured pass,
   older than it): every build-scoped summary part reads its stream twice,
   and `declared_test_counts` and `activity_buckets` are not memoized: eight
   prefix statements after the headline on a build against three on
   mainline. One `backend-engineer` brief on the trigger paths.
7. **The same gap WP-41 fixed, in the other tools** (one `backend-engineer`
   brief): `prune_runs.py` and `diagnose_db.py` take only `--db` and default
   it to `testboard.db`; `add_site_note.py` defaults `--db` the same way;
   `drop_stream.py` has no MariaDB CLI test and prints a raw exception on a
   connect failure. Nothing on the trigger paths.
8. **Deferred from the WP-43 review, decision 10:** derive the assigned-open
   tile and the queue counts from the assigned-cells statement, net one
   statement fewer on every cold headline.
9. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
10. Then whatever the users report.

## Where the code is

| | |
|---|---|
| `origin/master` | `6cba071` — the drop of 2026-10-13 (PR #20) on top of the drop of 2026-10-12 (PR #19); deployment of neither reported |
| `wp-44-flaky-mark` | **WP-44, the drop of 2026-11-11**, PR #21: the design arc, items 1 to 10, then the rework (the plan commits, `fec3d34` the triage rule in `CLAUDE.md` and the UI agent's definition, `b6f0162` backend, `6da4512` UI, `77f12d2` the keyed read on an old SQLite, `4b96426` the test page's offer, `3d287b5` the cadence select's placeholder, the sentence and Streak cell that follow it, the documents). Squash-merge, delete |
| `design-flaky-mark` | the arc's frozen doc, local only, already the base of the branch above; **delete after the merge** |
| `wp44-11-wt`, `wp44-12-wt`, `wp44-14-base`, `wp44-14-wt`, `wp44-15-wt`, `wp44-16-wt`, `wp44-17-wt`, `wp44-18-wt`, `wp44-19-wt` and ten `worktree-agent-*` of 2026-11-12 | the rework's snapshot branches and agent worktrees under `.claude/worktrees/`; remove the worktrees, then the branches, after the merge. **The backend agent's worktree of item 16 holds an unmerged memory note** (`.claude/agent-memory/backend-engineer/old-sqlite-326-repro.md`, how to build SQLite 3.26 in the Linux subsystem): copy it out first if it is wanted |
| `wp44-*-wt` (items 1 to 9), `wp44-5-base`, `wp44-6-head`, `wp44-8-head`, `wp44-10-head`, `wp44-pre-reword`, older `worktree-agent-*` | the first build's snapshot and scratch branches, most with a worktree under `.claude/worktrees/` (about forty worktrees in all, several locked); remove the worktrees, then the branches, after the merge |
| `wp-43-charts-assigned-split`, `design-charts-assigned-split`, `wp43-backend-wt`, `wp43-seed-wt`, `wp43-ui-wt` | merged as PR #20; the three `wp43-*` worktrees and all five branches are still here: remove |
| `admin-2026-10-09-handover` | PR #17, a handover rewrite superseded four times; **close without merging** |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP (WP-15); its migration renumbers to **13** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-42-copy-output`, `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `drop-2026-07-30`, `main` | merged or dead; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03; keep it a mirror of
`origin/master`, never commit to it (it is behind: fast-forward it).

**Registered builds** (plans frozen out of `/design-review`, run by
`/overnight` or the small path; state lives here, never in a skill):

| Plan | Spec | Branch | Status |
|---|---|---|---|
| WP-43: the dashboard charts split assigned from unassigned | `docs/design/charts-assigned-split-r0.md` §8, status strip §9 (frozen 2026-10-13) | `wp-43-charts-assigned-split` | **merged as PR #20**; deployment not reported; one owner decision still open in its §7 (item 7 below) |
| WP-44: mark a test as flaky | `docs/design/flaky-mark-r0.md` §11, contract §1 (frozen 2026-11-11); the triage tab's contract is §14, which replaces §3 U8 and §13's | `wp-44-flaky-mark`, PR #21 | **built, every item done (1 to 10, then 11 to 19); not merged, not deployed.** The status strip is the doc's §12. Item 6h is not needed. CI green on `3d287b5`, 3.6.8 and MariaDB 10.3 included. Its item 3 claimed migration 12 and moved the parked WP-15 to 13 |

## Needs a person, not a commit

1. **WP-44, a Flaking row shows no time.** New failures has "Failed at" and
   Still failing "Failing since"; a Flaking row has its streak and nothing
   that says when the latest failure was. The row carries the time, so the
   column would cost no statement. Named to the owner, not built: add it or
   leave it.
2. **WP-44, given up with the old tab, named to the owner at the time:**
   "failed N of its last 20 runs" (decision 4 of the design, agreed for the
   list of every mark); and any page that lists every mark. A marked test
   that passes, or fails past its cadence, is known as marked by the "flaky"
   tag on its row and by its own page. Confirm, or ask for a "marked" filter
   on All tests.
3. **WP-44, every unmarked test's page now carries the offer in its
   header**, passing tests included: one quiet line ("Flaky", a button), no
   background. Shown always so that a flaky test that happens to be passing
   can be marked. Confirm, or ask for it only on failing tests.
4. **Two small things found on the way, each its own change.**
   `static/actions.js` holds a literal NUL character in its "unassigned"
   sentinel, as `test.js` did: respell it as `\u0000` and rework
   `CoverageTest.test_files_containing_a_nul_byte_are_still_read` to plant
   its own file (`ui-engineer`). And **the code's floor on SQLite is 3.15**
   (row values in two filter terms) and is stated nowhere: say so in the
   README, or decide it should be lower.
5. **WP-44, the headline's cost is over the bar the design set**, where
   marks exist and only cold: SQLite +6.2 ms (22% of base) at 50 marks and
   +11.4 (41%) at 1,000 against a bar of 18% and 34%; a local MariaDB 12.3
   +17.0 (18%) and +35.8 (38%) against 16% and 34%. Accepted in the build
   for the owner to confirm. Priced and not taken: a covering runs index,
   dropping the "Newly fixed" rule, dropping the cadence-2 read (the log's
   item 6 entry has each).
6. **WP-44, the per-day chart's split.** Two things. (a) The measured pass
   found its request on Home's first paint, which the design had not
   expected; with marks it cost the page 11% at 50 marks and 86% at 1,000.
   It is now sent after the headline has drawn, **for every load, marks or
   not**: Home's first batch is three requests, and the chart gains its
   split one request later than before. (b) The part itself with many
   marks: on the 90-night seed, 90 days at 1,000 marks, +909 to +1,047 ms
   against the 366 the design had recorded; 14 days at 50 marks, about
   +8.5 ms. Off first paint, so it delays the chart and nothing else.
7. **The trend part's cost is one decision with two causes** (WP-43: 1.5 s
   on SQLite and 2 to 3 s on MariaDB at 90 days with 1,000 assignments;
   WP-44: item 6b). Accept, a covering index on `runs` (for marks: 968 ms
   to 399, 23% more database, about 12% on a push), or a cap.
8. **WP-44, behaviour to confirm:** Open Actions' Failing mode leaves out
   the unassigned flaking tests (the design's intent; fewer rows than
   before where marks exist); the Flaking tab is hidden where nothing is
   flaking, and a carried sort applies to a tab only if the tab has that
   column (new for every tab); "All clear" reads "Set aside: N muted" even
   with no marks, and its "N flaking" opens the Flaking tab; with
   `retired=1` a retired flaking row reads FLAKING; a test is promoted to a
   new failure only by a run that started after its cadence was last set;
   `products` in `parts=names` is names only; Open Actions no longer warms
   Home's headline, so Home opened straight after it pays its own; What's
   new is dated 2026-11-11 and the drop sets the real date.
9. **WP-44, known and left:** if a Reload's headline fails while the
   chart's split is on screen, the chart keeps its old bars under the error
   banner until the next Reload; Open Actions' cold rows read runs for 46
   tests on the seed (none warm); above SQLite's parameter ceiling the
   filter with Passing chosen costs +42 to +55 ms; a cold open of the
   Flaking tab pays the queue prefix (2.8 ms on SQLite, 16 on a local
   MariaDB) that the headline has normally paid already; on a test's page,
   another act while the mark's form is open (assign, mute) redraws the
   slot and discards a half-typed reason, as the Mute offer does; one MariaDB
   reading (Home as a page with no marks, +9.6 ms in one order, -10.3 in
   the other, equal statements) is unexplained and wants a quiet machine.
10. **No browser has rendered WP-44**: the mauve, the Flaky group in the
   selection bar, the Flaking tab and its Review panel, the chart's fourth
   part. Nor WP-43's blue. The owner's look at a local server on
   2026-11-12 is what overturned the tab; what they saw after the rework is
   not recorded here.
11. **Found during WP-44, older than it, not touched:** every bulk endpoint
   resolves each entry's stream with two statements (about 110 ms per 100
   entries on MariaDB); Mute group can be armed again while its request is
   in flight; the test page's refresh after an act swallows its error; the
   DOM shim's `querySelectorAll` matches one class only and a button loses
   its label once a child is appended (walks read sortable headers by key).
12. **The performance harness's gaps** (`performance-engineer`, a change of
    its own): `ab.py` has no per-side request lists, no warm or
    after-the-headline mode, no MariaDB backend, no way to compare two
    trees that serve a read at different addresses, and no pre-marks base
    at N > 0; the WP-44 passes did these in scratch drivers.
13. **WP-43, left as built:** the trend's fallback is all-or-nothing; a row
    with every count zero still draws a zero-width red fill and a 2 px gap;
    the full unsliced payload does not carry the trend keys.
14. **Should there be a switch that turns the build delete off**, or
    anything in front of it beyond a typed name? The users' habits will say.
15. **Build comments — the workshop the owner offered:** mainline lists show
    a test's newest comment of any origin, unlabelled; deleting a build
    clears the tag on its comments; nothing carries from one build to the
    next of the same branch.
16. Carried from 2026-08-11, still open: re-retire the tests the un-retire
    bug released (search comments for "Automatically un-retired");
    `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
    persistence with the daemon owners; import output-size cap; the
    morning decision list's UI judgement calls; first Tcl 8.5 site is
    still an experiment.
17. Carried from WP-31: a build whose last run is more than 36 hours old
    shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
18. **The sanity net's seed is dated August** and fails five clock-dependent
    checks; re-dating it is a brief for `backend-engineer` when the net is
    next needed for a drop.
19. **The `ui-engineer` agent type is still not registered** in a session;
    every UI item since WP-42 ran as a general-purpose Opus agent told to
    read the definition as its instructions. Check the agent list at the
    start of the next session.
20. **Agents in an isolated worktree cannot write the ledger** in the main
    checkout's `.claude/work/`; in the rework each kept its notes in the
    session's scratch directory and the main session copied the decisions
    across. Either the briefs say so from the start or the ledgers move.
21. **The agents' memory notes from WP-44 are not merged** (backend, UI,
    performance): the owner decides what is kept.
## Known slow, measured, not changed

**Production-scale SQLite (perf-seed/1, `tools/dev/perf/`), cold,
in-process, 32 calls, 2026-10-03, on `a58df48` (shipped code identical to
`49e596d`).** WP-43's and WP-44's measured passes (2026-10-13, 2026-11-11
and 2026-11-12, perf-seed/1 and the 90-night variant) read every row "same"
against this table with no marks, so it stands; it is updated only from a
run on the merged commit. Read the Metrics page first: if the wait is for a
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

**New with WP-43 and WP-44, not Known slow rows:**
`/api/summary?parts=assigned_trend`, off every first paint as of WP-44 and
memoized until the next push or act: 23 ms at 14 days and 101 ms at 90 days
with 65 assignments on the 90-night seed; 317 ms and 1,553 ms at 1,000
assignments; with marks, about +8.5 ms at 50 and up to +1,047 ms at 90 days
with 1,000 (items 6 and 7 above). **With WP-44 Open Actions as a page is 25
to 48% faster on SQLite** (it fetches the names part, not the full
summary) and 10% faster to level on MariaDB; its rows row above is
unchanged with no marks. Where marks exist the headline costs 18 to 41%
more cold (item 5 above). **The Flaking tab's rows** (`queue=flaking`,
measured 2026-11-12 on a day the machine read about five times faster than
the table above, so compare only within this sentence): on the click after
the headline 0.7 ms at 50 marks and 2.0 at 1,000 on SQLite, 2.3 and 5.4 on
a local MariaDB 12.3; cold 5.9 and 8.1, 23.0 and 27.7; Still failing on the
same tree and day, 6.4 after the headline and 11.5 cold on SQLite.

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- **`tclsh` must be on PATH for the suite's counts to compare.** Nine Tcl
  feeder tests exist only where it is; without it nothing is reported as
  skipped, the count is simply nine lower. On this machine the Bash tool
  has it and PowerShell does not: append the Git installation's
  `clangarm64\bin` to PATH for the run. The counts below are with it.
- **No interpreter on this machine carries an old SQLite** (3.50 on
  Windows, 3.45 in the Linux subsystem), so a plan that only an old SQLite
  gets wrong shows first in CI's 3.6.8 jobs (3.26). Item 16's implementer
  reproduced one locally in about ten minutes: SQLite 3.26 built from the
  public amalgamation in a scratch directory inside the subsystem and
  loaded for one python3 process, nothing installed on the host. A plan
  guard's failure message now names the SQLite version it ran on.
- `tools/dev/net/run_net.py` — the six-class sanity net; boots the repo it
  lives in on a copy of the database on a free port, needs `node` on PATH
  for the DOM walks (optional; the API checks run without it). **It fails
  five checks** on every tree since its August seed went stale (item 18
  above).
- `tools/dev/perf/` — the performance engineer's harness: `seed.py` builds
  the production-scale seed in ~75 s, and with `--history-nights 90` the
  90-night variant `perf-seed/1-h90` in about 8.5 minutes (393 MB; the
  README names where it is kept); `ab.py --assign N --marks N` applies the
  assignments fixture (65 or 1,000, ten mutes) and the marks fixture (0, 50
  or 1,000, dated before the runs so that promoted tests exist) to the
  measured rows, and `--page-mode` measures a page's first batch on two
  workers; `seed.py --onto COPY --assignments N --marks N` writes the
  fixtures into a copy for a by-hand MariaDB load or a play server;
  `README.md` is the recipe. `/perf-ab` runs it; its numbers are the table
  above. **Never compare trees over HTTP here** — timings vary 2–3× between
  runs. Name the seed beside every number.
- **`node` on PATH** is also what runs the DOM-shim guards in
  `tests/test_frontend_calls.py` (the real modules loaded in a subprocess);
  without it those classes skip, more skipped than the counts below.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307): start
  `mariadbd.exe` with `--defaults-file=.scratch/mariadb-data/my.ini
  --port=3307`. Four option files, each naming a sacrificial database the
  suite drops and recreates; `/brief` assigns them and `/verify` keeps the
  last (absolute paths in `TESTBOARD_TEST_DB_CNF`):
  `.scratch/mariadb-test.cnf` (agent A), `.scratch/mariadb-test-via.cnf`
  (agent B, also `TESTBOARD_TEST_DB_VIA_UPGRADE=1` runs),
  `.scratch/mariadb-test-c.cnf` (agent C; a by-hand load with the migration
  tool needs `max_allowed_packet` raised to 64 MB for the load and the
  database created `utf8mb4_nopad_bin`), `.scratch/mariadb-test-main.cnf`
  (the main session's `/verify`). It is not 10.3; CI's two 10.3 legs are
  the authority for production's stream.
- A play server for trying the UI: `python run_server.py --db <COPY>
  --port 8944 --host 127.0.0.1 --workers 4`, on a copy of a seed with the
  fixtures written in. **The seeds' runs end in September and the server
  reads the wall clock**, so an unshifted copy looks abandoned (the chart
  empty, every test "Not run", every mute expired): shift every stored time
  forward by whole days in the copy first. Never the repo-root
  `testboard.db` itself. **Restart it after any Python change.** One is
  left running on this machine from the main checkout, on the 50-mark
  copy.
- **Agents clean up only a directory they created**, by its full path,
  never by pattern in a shared one: the cleanup contract of every brief
  says so. The session's scratch directory is shared between agents: files
  there are prefixed with the item's slug and nobody overwrites another's.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -4 origin/master   # 6cba071 on top = the drop of 2026-10-13; 49e596d below is what production last reported running
gh pr list --state open              # expect #9, #17 (close it) and #21 (WP-44)
gh pr checks 21                      # the two MariaDB 10.3 legs decide the rows-by-key plan guard
python -m unittest discover          # on wp-44-flaky-mark, tclsh and node on PATH: expect 3067 OK (skipped 1), SQLite; 4242 OK (skipped 91) with MariaDB
python tools/dev/install_hooks.py    # once per clone: the publication gate's hooks
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
