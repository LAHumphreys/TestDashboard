# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-11-11, the design arc and the unattended build of
WP-44, mark a test as flaky** (`/design-review` through two measured
rounds, frozen and sized; `/overnight` for the build; then `/handover`).
Nine items built by fresh agents in their own worktrees, verified on both
backends in the main checkout, measured on the trigger paths, read by fresh
eyes and committed through the gate. The second half of the build ran
unattended: the decisions the build took to finish are listed below, each
one for the owner to confirm or overturn.

## Where things stand

- **Production is MariaDB, schema v11**, `--workers 16`, served over HTTPS
  behind a reverse proxy; the Python server speaks plain HTTP behind it.
  **It is the only deployment.** The last deploy the owner reported was the
  2026-10-01 drop (`49e596d`, PR #14), with only "Deployed" said of it.
- **The drops of 2026-10-12 (PR #19) and 2026-10-13 (PR #20) are merged**:
  `origin/master` is `6cba071`. **Whether either has been deployed is not
  reported: ask.**
- **Built, not merged, not deployed: WP-44, the drop of 2026-11-11**
  (provisional date), branch `wp-44-flaky-mark`, cut from the design branch
  at `57d817b`. Items 1 to 9 are committed (`e10a0f9` is item 9); item 10 is
  the docs commit that carries this file, the README's "Flaky marks" section
  and the operator note `docs/drops/2026-11-11.md`. The branch is pushed and
  a PR opened from it after that commit; its number is in `gh pr list`.
  **Migration 12 runs** (create-only: `test_flaky_marks`,
  `flaky_mark_history`, one index); the upgrade tool's ledger has the step.
  **Production stays at 11 until the drop.** The server's Python changed, so
  the restart is required.
- **What WP-44 is:** a person marks a test as flaky with a cadence ("fail if
  the N most recent runs fail"). Under its cadence a failing marked test is
  *flaking*: set aside from the failing figures and from triage, in its own
  colour, never a pass. The night it fails past its cadence it is a new
  failure again. A Flaky tab lists the marks, All tests has a FLAKING
  filter, Open Actions leaves out the flaking tests nobody owns. With no
  marks every read runs the statements it ran before.
- **CI has not yet run the branch when this was written.** The two MariaDB
  10.3 legs decide whether the plan's conditional item 6h (a dialect hook)
  exists: it is built only if a plan guard fails there.
- **Not reported back from the 2026-10-01 deploy — ask, do not assume:**
  what the upgrade tool's dry run said (`SERVER: may keep running`
  expected); whether the start-up output named the orphaned build; how long
  the upgrade step and the restart took; whether the Metrics page shows
  schema 11; whether anyone has muted a test yet. Carried from 2026-09-30:
  were the counters left on; what "Waited, mean" reads during a run.
- **No ledger in `.claude/work/`.** Every WP-44 item is committed and its
  ledger deleted. The run's heartbeat was session-only.
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
- `/overnight` runs a frozen, sized plan unattended on the owner's go at
  kickoff; there is no other gate. WP-43 and WP-44 were its two runs.
- The publication gate's hooks are installed in this clone; any other clone
  runs `python tools/dev/install_hooks.py` once. The term list is
  `.claude/private/terms.txt`, gitignored; it exists on this machine only.

## Next session's plan

1. **The owner tries WP-44 on a local server and decides on the drop**: the
   decisions under "Needs a person" items 1 to 3 first. Anything overturned
   is an issue and a rework on the same branch before the merge.
2. **Read CI on the WP-44 PR.** Green on both 10.3 legs: item 6h does not
   exist; strike it from the plan's status strip. A plan guard red there:
   brief item 6h to `backend-engineer` from the design's §11.
3. **Ship it** when the owner says so: set the real date in the operator
   note and in What's new together, squash-merge with the PR text, then the
   housekeeping below, then record what the operator reports.
4. **Ask whether the drops of 2026-10-12 and 2026-10-13 were deployed**, and
   get the 2026-10-01 answers above into the log.
5. **The trend part's cost**, now with two causes (many assignments from
   WP-43, many marks from WP-44): one owner decision, one covering index if
   chosen. A `backend-engineer` package with a migration registry row, a
   ledger step and an exporter table, measured on the push path.
6. **The same gap WP-41 fixed, in the other tools** (one `backend-engineer`
   brief): `prune_runs.py` and `diagnose_db.py` take only `--db` and default
   it to `testboard.db`; `add_site_note.py` defaults `--db` the same way;
   `drop_stream.py` has no MariaDB CLI test and prints a raw exception on a
   connect failure. Nothing on the trigger paths.
7. **Deferred from the WP-43 review, decision 10:** derive the assigned-open
   tile and the queue counts from the assigned-cells statement, net one
   statement fewer on every cold headline.
8. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
9. Then whatever the users report.

## Where the code is

| | |
|---|---|
| `origin/master` | `6cba071` — the drop of 2026-10-13 (PR #20) on top of the drop of 2026-10-12 (PR #19); deployment of neither reported |
| `wp-44-flaky-mark` | **WP-44, the drop of 2026-11-11**: the design arc's ten commits (to `57d817b`), the build's start, items 1 to 9, the docs commit. PR open once pushed. Squash-merge, delete |
| `design-flaky-mark` | the arc's frozen doc, local only, already the base of the branch above; **delete after the merge** |
| `wp44-*-wt`, `wp44-5-base`, `wp44-6-head`, `wp44-8-head`, `wp44-10-head`, `wp44-pre-reword`, `worktree-agent-*` | the build's snapshot and scratch branches, most with a worktree under `.claude/worktrees/` (about two dozen, several locked); remove the worktrees, then the branches, after the merge |
| `wp-43-charts-assigned-split`, `design-charts-assigned-split`, `wp43-backend-wt`, `wp43-seed-wt`, `wp43-ui-wt` | merged as PR #20; the three `wp43-*` worktrees and all five branches are still here: remove |
| `admin-2026-10-09-handover` | PR #17, a handover rewrite superseded three times; **close without merging** |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP (WP-15); its migration renumbers to **13** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-42-copy-output`, `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `drop-2026-07-30`, `main` | merged or dead; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03; keep it a mirror of
`origin/master`, never commit to it (it is behind: fast-forward it).

**Registered builds** (plans frozen out of `/design-review`, run by
`/overnight` or the small path; state lives here, never in a skill):

| Plan | Spec | Branch | Status |
|---|---|---|---|
| WP-43: the dashboard charts split assigned from unassigned | `docs/design/charts-assigned-split-r0.md` §8, status strip §9 (frozen 2026-10-13) | `wp-43-charts-assigned-split` | **merged as PR #20**; deployment not reported; one owner decision still open in its §7 (item 3 below) |
| WP-44: mark a test as flaky | `docs/design/flaky-mark-r0.md` §11, contract §1 (frozen 2026-11-11) | `wp-44-flaky-mark`, cut from `design-flaky-mark` at `57d817b` | **built, items 1 to 9 done, item 10 the docs commit; PR open, not merged, not deployed.** The status strip is the doc's §12. Conditional item 6h waits on CI's 10.3 legs. Its item 3 claimed migration 12 and moved the parked WP-15 to 13 |

## Needs a person, not a commit

1. **WP-44, the headline's cost is over the bar the design set**, where
   marks exist and only cold: SQLite +6.2 ms (22% of base) at 50 marks and
   +11.4 (41%) at 1,000 against a bar of 18% and 34%; a local MariaDB 12.3
   +17.0 (18%) and +35.8 (38%) against 16% and 34%. Accepted in the build
   for the owner to confirm. Priced and not taken: a covering runs index,
   dropping the "Newly fixed" rule, dropping the cadence-2 read (the log's
   item 6 entry has each).
2. **WP-44, the per-day chart's split.** Two things. (a) The measured pass
   found its request on Home's first paint, which the design had not
   expected; with marks it cost the page 11% at 50 marks and 86% at 1,000.
   It is now sent after the headline has drawn, **for every load, marks or
   not**: Home's first batch is three requests, and the chart gains its
   split one request later than before. (b) The part itself with many
   marks: on the 90-night seed, 90 days at 1,000 marks, +909 to +1,047 ms
   against the 366 the design had recorded; 14 days at 50 marks, about
   +8.5 ms. Off first paint, so it delays the chart and nothing else.
3. **The trend part's cost is now one decision with two causes** (WP-43:
   1.5 s on SQLite and 2 to 3 s on MariaDB at 90 days with 1,000
   assignments; WP-44: item 2b). Accept, a covering index on `runs` (for
   marks: 968 ms to 399, 23% more database, about 12% on a push), or a cap.
4. **WP-44, behaviour to confirm:** Open Actions' Failing mode leaves out
   the unassigned flaking tests (the design's intent; fewer rows than
   before where marks exist); the Flaky tab's badge counts the marks that
   are not retired while the list shows the retired ones last, tagged;
   "All clear" reads "Set aside: N muted" even with no marks; with
   `retired=1` a retired flaking row reads FLAKING; a test is promoted to a
   new failure only by a run that started after its cadence was last set;
   `products` in `parts=names` is names only; Open Actions no longer warms
   Home's headline, so Home opened straight after it pays its own; What's
   new is dated 2026-11-11 and the drop sets the real date.
5. **WP-44, known and left:** if a Reload's headline fails while the
   chart's split is on screen, the chart keeps its old bars under the error
   banner until the next Reload; Open Actions' cold rows read runs for 46
   tests on the seed (none warm); above SQLite's parameter ceiling the
   filter with Passing chosen costs +42 to +55 ms; one MariaDB reading
   (Home as a page with no marks, +9.6 ms in one order, -10.3 in the other,
   equal statements) is unexplained and wants a quiet machine.
6. **No browser has rendered WP-44**: the mauve, the Flaky group in the
   selection bar, the tab, the chart's fourth part. Nor WP-43's blue.
7. **Found during WP-44, older than it, not touched:** every bulk endpoint
   resolves each entry's stream with two statements (about 110 ms per 100
   entries on MariaDB); Mute group can be armed again while its request is
   in flight; the test page's refresh after an act swallows its error.
8. **The performance harness's gaps** (`performance-engineer`, a change of
   its own): `ab.py` has no per-side request lists, no warm mode, no
   MariaDB backend and no pre-marks base at N > 0; the WP-44 passes did
   these in scratch scripts.
9. **WP-43, left as built:** the trend's fallback is all-or-nothing; a row
   with every count zero still draws a zero-width red fill and a 2 px gap;
   the full unsliced payload does not carry the trend keys.
10. **Should there be a switch that turns the build delete off**, or
    anything in front of it beyond a typed name? The users' habits will say.
11. **Build comments — the workshop the owner offered:** mainline lists show
    a test's newest comment of any origin, unlabelled; deleting a build
    clears the tag on its comments; nothing carries from one build to the
    next of the same branch.
12. Carried from 2026-08-11, still open: re-retire the tests the un-retire
    bug released (search comments for "Automatically un-retired");
    `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
    persistence with the daemon owners; import output-size cap; the
    morning decision list's UI judgement calls; first Tcl 8.5 site is
    still an experiment.
13. Carried from WP-31: a build whose last run is more than 36 hours old
    shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
14. **The sanity net's seed is dated August** and fails five clock-dependent
    checks; re-dating it is a brief for `backend-engineer` when the net is
    next needed for a drop.
15. **The `ui-engineer` agent type is still not registered** in a session;
    the WP-42, WP-43 and WP-44 UI items ran as a general-purpose Opus agent
    carrying the definition as its instructions. Check the agent list at
    the start of the next session.
16. **The three agents' memory notes from WP-44 are not merged**
    (backend, UI, performance): the owner decides what is kept.

## Known slow, measured, not changed

**Production-scale SQLite (perf-seed/1, `tools/dev/perf/`), cold,
in-process, 32 calls, 2026-10-03, on `a58df48` (shipped code identical to
`49e596d`).** WP-43's and WP-44's measured passes (2026-10-13 and
2026-11-11, perf-seed/1 and the 90-night variant) read every row "same"
against this table with no marks, so it stands; it is updated only from a
run on the merged commit. Read the Metrics page first: if the wait is for a
worker, none of these is the problem. The A/A noise band is ±10 ms on the
two slowest rows, under ±1 ms on the browse rows, and the machine's speed
swings 2× to 3× between runs, so only paired deltas within one run compare.

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
with 1,000 (items 2 and 3 above). **With WP-44 Open Actions as a page is 25
to 48% faster on SQLite** (it fetches the names part, not the full
summary) and 10% faster to level on MariaDB; its rows row above is
unchanged with no marks. Where marks exist the headline costs 18 to 41%
more cold (item 1 above).

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `tools/dev/net/run_net.py` — the six-class sanity net; boots the repo it
  lives in on a copy of the database on a free port, needs `node` on PATH
  for the DOM walks (optional; the API checks run without it). **It fails
  five checks** on every tree since its August seed went stale (item 14
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
  `testboard.db` itself.
- **Agents clean up only a directory they created**, by its full path,
  never by pattern in a shared one: the cleanup contract of every brief
  says so.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -4 origin/master   # 6cba071 on top = the drop of 2026-10-13; 49e596d below is what production last reported running
gh pr list --state open              # expect #9, #17 (close it) and the WP-44 PR from wp-44-flaky-mark
gh pr checks <the WP-44 PR>          # the two MariaDB 10.3 legs decide whether item 6h exists
python -m unittest discover          # on wp-44-flaky-mark: expect 3021 OK (skipped 1), SQLite; 4175 OK (skipped 90) with MariaDB (node on PATH; more skipped without it)
python tools/dev/install_hooks.py    # once per clone: the publication gate's hooks
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
