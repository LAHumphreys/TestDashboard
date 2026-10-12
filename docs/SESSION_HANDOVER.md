# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-10-12, the drop of 2026-10-12** (`/drop`, then
`/handover`). The first two features went through the process end to end
(`PROCESS.md`): WP-41 and WP-42, each briefed, built in a worktree,
verified on both backends, reviewed by fresh eyes and gated. This rewrite
supersedes the one in PR #17 (2026-10-09), which never merged.

## Where things stand

- **Production is MariaDB, schema v11, serving the 2026-10-01 drop
  (`49e596d`, PR #14)**, `--workers 16`. **It is the only deployment**: the
  SQLite staging box was decommissioned on 2026-10-01, so every drop meets
  production cold. Deployed the same day; the owner reported only "Deployed".
- **Production is served over HTTPS behind a reverse proxy** (owner, 2026-10-12);
  the Python server itself still speaks plain HTTP behind it. Browser APIs
  gated on a secure context (the clipboard, for one) work there.
- **`origin/master` is three commits past production** (`600ff55` the deploy
  record, `18005ee` the process PR, `022ce41` WP-41). None changes the
  server's Python; the drop below carries them without a migration or a flag.
- **In flight: the drop of 2026-10-12**, branch `wp-42-copy-output` on top
  of `022ce41`: WP-41 (`tools/drop_environment.py` takes `--db-config` and
  `--site-notes` for MariaDB; `--db` lost its default) and WP-42 (a "Copy
  output" button in all four run-output viewers, copying the full output
  the viewer fetched). **No migration; schema stays at 11.** Operator note
  `docs/drops/2026-10-12.md`; **PR #19** is open from that branch, its body
  the squash message. Rollback is a checkout and a restart.
- **Not reported back from the 2026-10-01 deploy — ask, do not assume:**
  what the upgrade tool's dry run said (`SERVER: may keep running`
  expected); whether the start-up output named the orphaned build
  (`empty builds: removed 1 (build:…)`); how long the upgrade step and the
  restart took; whether the Metrics page shows schema 11; whether anyone has
  muted a test yet. Carried from 2026-09-30: were the counters left on; what
  "Waited, mean" reads during a run.
- **No ledger in `.claude/work/`, no registered build.** Both WP-41 and
  WP-42 are committed; their ledgers were deleted at commit.
- **The upgrade tool is a ledger** (WP-40): a SQLite migration above 7
  without its MariaDB step fails the suite (`LedgerTest`); runbook §G.5 is
  the procedure. Migration 12 is reserved by WP-15 (parked).
- **Feeders are never stopped for an upgrade** (runbook §G.3); the tool's
  dry run says whether the SERVER must stop.

## How work starts now

- Anything larger than one brief: `/design-review <what you want>`. One
  change: `/brief`. The main session never edits code (`CLAUDE.md`, "Who
  implements what"). Every implementer runs in its own worktree with an
  assigned sacrificial database (below); the performance pass runs only on
  the trigger paths (`storage.py`, `api.py`, the push path, first paint).
- `/overnight` runs a frozen, sized plan unattended on the owner's go at
  kickoff; there is no other gate.
- The publication gate's hooks are installed in this clone; any other clone
  runs `python tools/dev/install_hooks.py` once. The term list is
  `.claude/private/terms.txt`, gitignored; it exists on this machine only.

## Next session's plan

1. **Ship the drop of 2026-10-12**: when CI is green and the owner says so,
   squash-merge with the PR text, delete the branch, then record what the
   operator reports (the restart, the Copy button seen in a real browser
   for the first time, the tool's `--help`) and stamp the operator note.
2. **Get the deploy answers above** into the log; rewrite that paragraph.
   Nothing from the 2026-10-01 deploy has been reported yet.
3. **The same gap WP-41 fixed, in the other tools** (one `backend-engineer`
   brief): `prune_runs.py` and `diagnose_db.py` take only `--db` and default
   it to `testboard.db`; `add_site_note.py` defaults `--db` the same way;
   `drop_stream.py` has no MariaDB CLI test and prints a raw exception on a
   connect failure. Nothing on the trigger paths.
4. **Watch the first days of muting.** The log's WP-40 entries list six
   recorded edges; the two a user is likeliest to trip over: the Assigned
   and Muted tabs overlap (a mute assigns), and unassigning from mainline
   drops a build's mute of the same test. The "New failures" delta line
   does not add muted failures back. Change nothing until someone asks.
5. **The master-side perf finding** from the WP-40 A/B: 65
   `current_assignments` rows cost the browse page's count about 6 ms at
   production scale (28 against 22, below). A candidate backend item.
6. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
7. Then whatever the users report. The first one is likeliest to be about
   how the Review panel's head row looks with four items in it (WP-42).

## Where the code is

| | |
|---|---|
| `origin/master` | `022ce41` — WP-41 (PR #18) on top of the process PR and the deployed `49e596d`; the server's Python is still `49e596d`'s |
| `wp-42-copy-output` | **the drop of 2026-10-12**, PR #19 open; WP-42 plus the drop's docs. Squash-merge, delete |
| `admin-2026-10-09-handover` | PR #17, a handover rewrite superseded by this one; **close without merging** |
| `wp-41-drop-env-mariadb` | merged as PR #18; the remote branch can go |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **12** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `drop-2026-07-30`, `main` | merged or dead; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03; keep it a mirror of
`origin/master`, never commit to it. One stale agent worktree
(`.claude/worktrees/agent-aef4d881612312e8c`) is locked by a stray process
from 2026-10-01; harmless, remove when it unlocks.

**Registered builds** (plans frozen out of `/design-review`, run by
`/overnight` or the small path; state lives here, never in a skill):

| Plan | Spec | Branch | Status |
|---|---|---|---|
| none registered | | | |

## Needs a person, not a commit

1. **Should there be a switch that turns the build delete off**, or anything
   in front of it beyond a typed name? The users' habits will say.
2. **Build comments — the workshop the owner offered:** mainline lists show a
   test's newest comment of any origin, unlabelled; deleting a build clears
   the tag on its comments; nothing carries from one build to the next of the
   same branch. ("Has a comment" is not "muted" is now answered by WP-40.)
3. **Decide the Java client's fate** (PR #9): merge or park.
4. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls; first Tcl 8.5 site is
   still an experiment.
5. Carried from WP-31: a build whose last run is more than 36 hours old
   shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
6. **The sanity net's seed is dated August** and fails five clock-dependent
   checks; re-dating it is a brief for `backend-engineer` when the net is
   next needed for a drop.
7. **The performance seed holds a week of history, not a year**, so a
   request that reads a window of `runs` is under-measured; a year-scale
   seed is a brief for `performance-engineer` before the timeline or
   flakiness pages are touched.
8. **WP-42, left as built:** the test page's standalone "Run output"
   section builds its Copy button only after a successful fetch, so a
   failed fetch leaves the previous run's button beside that run's output.
   Nobody has seen the four-item Review head row in a browser.
9. **The `ui-engineer` agent type was not registered** in a session started
   with `/remote-control` while its two siblings were; the WP-42 build ran
   as a general-purpose Opus agent carrying the definition as its
   instructions. Check the agent list at the start of the next session.

## Known slow, measured, not changed

**Production-scale SQLite (perf-seed/1, `tools/dev/perf/`), cold,
in-process, 32 calls, 2026-10-03, on `a58df48` (shipped code identical to
`49e596d`).** These replace the dev-scale figures taken before WP-40, which
read a quarter to a third of these; the explanations are unchanged. Read the
Metrics page first: if the wait is for a worker, none of these is the
problem. The A/A noise band is ±10 ms on the two slowest rows, under ±1 ms
on the browse rows.

| | |
|---|---|
| Watch, three cards incl. one full-size build | 312 ms — `compare_counts_many` fetches every pair to classify in Python |
| A comparison page for a full-size build | 268 ms — the pairs query twice, pinned at two by WP-23 |
| Open Actions' rows | 97 ms, never memoized |
| Home headline | 90 ms, mostly the one per-environment pass that is left |
| Browse page's count, no assignments | 22 ms |
| Browse page's count with 65 assignments | 28 ms — the assignment rows (WP-40 A/B found it on `master` too) |

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `tools/dev/net/run_net.py` — the six-class sanity net; boots the repo it
  lives in on a copy of the database on a free port, needs `node` on PATH
  for the DOM walks (optional; the API checks run without it). **It fails
  five checks** on every tree since its August seed went stale (item 6
  above).
- `tools/dev/perf/` — the performance engineer's harness: `seed.py` builds
  the production-scale seed in ~75 s; `ab.py` is the cold, in-process,
  alternated A/B of two trees; `README.md` is the recipe. `/perf-ab` runs
  it; its numbers are the table above. **Never compare trees over HTTP
  here** — timings vary 2–3× between runs.
- `.scratch/net/drop-2026-09-30/` — the 2026-09-30 drop's live drivers and
  raw numbers; superseded for measuring by `tools/dev/perf/`.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307): start
  `mariadbd.exe` with `--defaults-file=.scratch/mariadb-data/my.ini
  --port=3307`. Four option files, each naming a sacrificial database the
  suite drops and recreates; `/brief` assigns them and `/verify` keeps the
  last (absolute paths in `TESTBOARD_TEST_DB_CNF`):
  `.scratch/mariadb-test.cnf` (agent A), `.scratch/mariadb-test-via.cnf`
  (agent B, also `TESTBOARD_TEST_DB_VIA_UPGRADE=1` runs),
  `.scratch/mariadb-test-c.cnf` (agent C), `.scratch/mariadb-test-main.cnf`
  (the main session's `/verify`). It is not 10.3; CI's two 10.3 legs are the
  authority for production's stream.
- A play server for trying the UI: `python run_server.py --db <COPY of
  testboard.db> --port 8947 --host 127.0.0.1 --workers 4`. Never the
  repo-root file itself.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -4 origin/master   # 022ce41 on top = WP-41; 49e596d below is what production runs until the drop lands
gh pr list --state open              # expect #9, #17 (close it) and #19, the drop of 2026-10-12
python -m unittest discover          # expect 2687 OK (skipped 1), SQLite, on the drop branch; 3674 OK (skipped 71) with MariaDB
python tools/dev/install_hooks.py    # once per clone: the publication gate's hooks
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
