# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-10-13, the overnight build of WP-43 and the drop of
2026-10-13** (`/design-review` in the morning, `/overnight` through the day,
`/drop`, then `/handover`). The first unattended run: three items briefed,
built by fresh agents in their own worktrees, verified on both backends in
the main checkout, measured on the trigger paths, read by fresh eyes and
committed through the gate, with two findings sent back per item and one
design decision taken on the owner's behalf and recorded.

## Where things stand

- **Production is MariaDB, schema v11**, `--workers 16`, served over HTTPS
  behind a reverse proxy; the Python server speaks plain HTTP behind it.
  **It is the only deployment.** The last deploy the owner reported was the
  2026-10-01 drop (`49e596d`, PR #14), with only "Deployed" said of it.
- **The drop of 2026-10-12 merged as PR #19** (`e839953` on `origin/master`:
  WP-41, the environment-drop tool on MariaDB; WP-42, the Copy output
  button). **Whether it has been deployed is not reported: ask.**
- **In flight: the drop of 2026-10-13**, branch `wp-43-charts-assigned-split`
  on top of `e839953`: WP-43, the three home-dashboard charts split assigned
  from unassigned failing tests. **No migration; schema stays at 11; no new
  flag; the server's Python changed, so the restart is required.** Operator
  note `docs/drops/2026-10-13.md`; **PR #20** is open from that branch, its
  body the squash message. Rollback is a checkout and a restart.
- **One decision waits for the owner, nothing in the build depends on it:**
  the trend chart's split is its own summary part, off every first paint
  and memoized, costing about 23 ms at the default 14 days and 101 ms at
  90 days with the reference estate's 65 assignments, but 1.5 s on SQLite
  and 2 to 3 s on MariaDB at 90 days with 1,000. A covering index on
  `runs` would make it about 25 ms flat at the cost of migration 12 and a
  write per pushed run. The design doc's §7 sets out accept, index or cap.
- **Not reported back from the 2026-10-01 deploy — ask, do not assume:**
  what the upgrade tool's dry run said (`SERVER: may keep running`
  expected); whether the start-up output named the orphaned build; how long
  the upgrade step and the restart took; whether the Metrics page shows
  schema 11; whether anyone has muted a test yet. Carried from 2026-09-30:
  were the counters left on; what "Waited, mean" reads during a run.
- **No ledger in `.claude/work/`.** Every WP-43 item is committed and its
  ledger deleted. The heartbeat cron of the overnight run is deleted.
- **The upgrade tool is a ledger** (WP-40): a SQLite migration above 7
  without its MariaDB step fails the suite (`LedgerTest`); runbook §G.5 is
  the procedure. Migration 12 is reserved by WP-15 (parked); the covering
  index above, if chosen, would need its own number after it.
- **Feeders are never stopped for an upgrade** (runbook §G.3).

## How work starts now

- Anything larger than one brief: `/design-review <what you want>`. One
  change: `/brief`. The main session never edits code (`CLAUDE.md`, "Who
  implements what"). Every implementer runs in its own worktree with an
  assigned sacrificial database (below); the performance pass runs only on
  the trigger paths (`storage.py`, `api.py`, the push path, first paint).
- `/overnight` runs a frozen, sized plan unattended on the owner's go at
  kickoff; there is no other gate. WP-43 was its first run, end to end.
- The publication gate's hooks are installed in this clone; any other clone
  runs `python tools/dev/install_hooks.py` once. The term list is
  `.claude/private/terms.txt`, gitignored; it exists on this machine only.

## Next session's plan

1. **Ship the drop of 2026-10-13**: when CI is green and the owner says so,
   squash-merge with the PR text, delete the branch and the design branch
   (its commits are the build branch's base), remove the three `wp43-*`
   worktrees, then record what the operator reports and stamp the note.
   Then ask whether the drop of 2026-10-12 was deployed, and record it.
2. **The owner's decision on the trend part's cost** (above). If the index:
   a `backend-engineer` package with migration registry row, ledger step
   and exporter table, measured on the push path before review.
3. **Get the deploy answers above** into the log; rewrite that paragraph.
4. **The same gap WP-41 fixed, in the other tools** (one `backend-engineer`
   brief): `prune_runs.py` and `diagnose_db.py` take only `--db` and default
   it to `testboard.db`; `add_site_note.py` defaults `--db` the same way;
   `drop_stream.py` has no MariaDB CLI test and prints a raw exception on a
   connect failure. Nothing on the trigger paths.
5. **Deferred from the WP-43 review, decision 10:** derive the assigned-open
   tile and the queue counts from the new assigned-cells statement, net one
   statement fewer on every cold headline. A candidate backend package;
   the measured tie-break favours it on MariaDB (one round trip fewer).
6. **Watch the first days of muting** (WP-40 edges in the log) and now of
   the split: the first question is likeliest to be why a past day's
   columns changed colour; the caption answers it.
7. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
8. Then whatever the users report.

## Where the code is

| | |
|---|---|
| `origin/master` | `e839953` — the drop of 2026-10-12 (PR #19) on top of WP-41 and the process PR; deployment not reported |
| `wp-43-charts-assigned-split` | **the drop of 2026-10-13**, PR #20 open; the design arc's two commits, the status strip, items 1, 0 and 2 of WP-43, the drop's docs. Squash-merge, delete |
| `design-charts-assigned-split` | the arc's frozen doc, two commits, already the base of the branch above; **delete after the merge** |
| `admin-2026-10-09-handover` | PR #17, a handover rewrite superseded twice; **close without merging** |
| `wp43-backend-wt`, `wp43-seed-wt`, `wp43-ui-wt` | the agents' snapshot branches, each with a worktree under `.claude/worktrees/`; remove the worktrees, then the branches, after the merge |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **12** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `drop-2026-07-30`, `main` | merged or dead; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03; keep it a mirror of
`origin/master`, never commit to it. Two stale agent worktrees under
`.claude/worktrees/` (`agent-a10cdde96c56b4346`, `agent-aef4d881612312e8c`)
predate WP-43; remove when they unlock.

**Registered builds** (plans frozen out of `/design-review`, run by
`/overnight` or the small path; state lives here, never in a skill):

| Plan | Spec | Branch | Status |
|---|---|---|---|
| WP-43: the dashboard charts split assigned from unassigned | `docs/design/charts-assigned-split-r0.md` §8, status strip §9 (frozen 2026-10-13) | `wp-43-charts-assigned-split` | **built, every item done; drop PR #20 open**; one owner decision recorded in §7 |

## Needs a person, not a commit

1. **The trend part's cost at 90 days with many assignments** (Where things
   stand): accept as built, a covering index on `runs` as its own package,
   or a cap. The run did not choose.
2. **WP-43, left as built:** the trend's fallback is all-or-nothing (a
   night the part does not carry draws the whole card as two series); a
   hung part keeps only the trend card dimmed for its 10 s timeout; a row
   with every count zero still draws a zero-width red fill and a 2 px gap,
   kept so callers that pass no segments get byte-identical DOM; the full
   unsliced payload does not carry the trend keys (§7, reversible). No
   browser has rendered the blue, the three-segment bars or the legends.
3. **Should there be a switch that turns the build delete off**, or anything
   in front of it beyond a typed name? The users' habits will say.
4. **Build comments — the workshop the owner offered:** mainline lists show a
   test's newest comment of any origin, unlabelled; deleting a build clears
   the tag on its comments; nothing carries from one build to the next of the
   same branch.
5. **Decide the Java client's fate** (PR #9): merge or park.
6. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls; first Tcl 8.5 site is
   still an experiment.
7. Carried from WP-31: a build whose last run is more than 36 hours old
   shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
8. **The sanity net's seed is dated August** and fails five clock-dependent
   checks; re-dating it is a brief for `backend-engineer` when the net is
   next needed for a drop.
9. **WP-42, left as built:** the test page's standalone "Run output"
   section builds its Copy button only after a successful fetch. Nobody has
   seen the four-item Review head row in a browser.
10. **The `ui-engineer` agent type is still not registered** in this
    session; the WP-42 and WP-43 UI items ran as a general-purpose Opus
    agent carrying the definition as its instructions. Check the agent list
    at the start of the next session.

## Known slow, measured, not changed

**Production-scale SQLite (perf-seed/1, `tools/dev/perf/`), cold,
in-process, 32 calls, 2026-10-03, on `a58df48` (shipped code identical to
`49e596d`).** WP-43's two measured passes (2026-10-13, perf-seed/1 and the
90-night variant) read every row "same" against this table, so it stands;
it is updated only from a run on the merged commit. Read the Metrics page
first: if the wait is for a worker, none of these is the problem. The A/A
noise band is ±10 ms on the two slowest rows, under ±1 ms on the browse
rows, and the machine's speed swings about 2× between runs, so only paired
deltas within one run compare.

| | |
|---|---|
| Watch, three cards incl. one full-size build | 312 ms — `compare_counts_many` fetches every pair to classify in Python |
| A comparison page for a full-size build | 268 ms — the pairs query twice, pinned at two by WP-23 |
| Open Actions' rows | 97 ms, never memoized; 154 ms on the 90-night seed because its streaks read the runs history |
| Home headline | 90 ms, mostly the one per-environment pass that is left; WP-43 adds one bounded statement, about 0.3 ms |
| Browse page's count, no assignments | 22 ms |
| Browse page's count with 65 assignments | 28 ms — the assignment rows (WP-40 A/B found it on `master` too) |

**New with WP-43, not a Known slow row:** `/api/summary?parts=assigned_trend`,
off every first paint and memoized until the next push or assignment
change: 23 ms at 14 days and 101 ms at 90 days with 65 assignments on the
90-night seed; 317 ms and 1,553 ms at 1,000 assignments (the owner's open
decision above).

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `tools/dev/net/run_net.py` — the six-class sanity net; boots the repo it
  lives in on a copy of the database on a free port, needs `node` on PATH
  for the DOM walks (optional; the API checks run without it). **It fails
  five checks** on every tree since its August seed went stale (item 8
  above).
- `tools/dev/perf/` — the performance engineer's harness: `seed.py` builds
  the production-scale seed in ~75 s, and with `--history-nights 90` the
  90-night variant `perf-seed/1-h90` in about 8.5 minutes (393 MB; the
  README names where it is kept); `ab.py --assign N` applies an
  assignments fixture (65 or 1,000, ten mutes) to the measured rows, and
  `seed.py --onto COPY --assignments N` writes it into a copy for a by-hand
  MariaDB load; `ab.py` is the cold, in-process, alternated A/B of two
  trees; `README.md` is the recipe. `/perf-ab` runs it; its numbers are the
  table above. **Never compare trees over HTTP here** — timings vary 2–3×
  between runs. Name the seed beside every number.
- **`node` on PATH** is also what runs the DOM-shim guards in
  `tests/test_frontend_calls.py` (WP-43's `AssignedSplitDomTest` loads the
  real modules in a subprocess); without it that class skips, one more
  skipped class than the counts below.
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
- A play server for trying the UI: `python run_server.py --db <COPY of
  testboard.db> --port 8947 --host 127.0.0.1 --workers 4`. Never the
  repo-root file itself.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -4 origin/master   # e839953 on top = the drop of 2026-10-12; 49e596d below is what production last reported running
gh pr list --state open              # expect #9, #17 (close it) and #20, the drop of 2026-10-13
python -m unittest discover          # expect 2751 OK (skipped 1), SQLite, on the drop branch; 3769 OK (skipped 79) with MariaDB (node on PATH; one more skipped class without it)
python tools/dev/install_hooks.py    # once per clone: the publication gate's hooks
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
