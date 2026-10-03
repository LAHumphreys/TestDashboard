# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-10-03, process round 1** (a line-level update of
the 2026-10-01 rewrite; the next full rewrite goes through `/handover`). The drop of 2026-10-01
(WP-39 empty builds pruned; WP-40 muted failures, migration 11) was
squash-merged as PR #14 and **deployed to production the same day**. The
user reported only "Deployed"; nothing else from the deploy is recorded yet.

## Where things stand

- **Production is MariaDB, schema v11, serving `master` at `49e596d`** —
  the 2026-10-01 drop, PR #14. `--workers 16`. **It is the only
  deployment**: the SQLite staging box was decommissioned on 2026-10-01,
  so every drop from now on meets production cold.
- **Not reported back from the deploy — ask, do not assume:** what the
  upgrade tool's dry run said (`SERVER: may keep running` expected); whether
  the start-up output named the orphaned build (`empty builds: removed 1
  (build:…)`); how long the upgrade step and the restart took; whether the
  Metrics page shows schema 11; whether anyone has muted a test yet.
  Carried from 2026-09-30, still unanswered: were the counters left on; what
  "Waited, mean" reads during a run.
- **In flight: `process-round-1`** (docs and `.claude/` only): `PROCESS.md`,
  the `CLAUDE.md` split, two agent definitions, the brief and ledger
  templates, four skills. Read `PROCESS.md` before the next piece of code
  work; every change now goes through `/brief`. PR #12 is closed
  (redundant). The shipped branches (`drop-2026-10-01`,
  `wp-40-acknowledged-failures`, `drop-2026-09-30`,
  `docs-handover-2026-09-29`) are deleted locally and on origin.
- **The upgrade tool is a ledger** (WP-40): a SQLite migration above 7
  without its MariaDB step fails the suite (`LedgerTest`); runbook §G.5 is
  the procedure. Migration 12 is reserved by WP-15 (parked).
- **Feeders are never stopped for an upgrade** (runbook §G.3); the tool's
  dry run says whether the SERVER must stop.

## Next session's plan

1. **Get the deploy answers above** into the log; rewrite this paragraph.
   Then process round 2 (`PROCESS.md` §7): the publication-gate scanner and
   hooks, `tools/dev/`, `/perf-ab`, `/drop`, and two supervised packages.
2. **Watch the first days of muting.** The log's WP-40 entries list six
   recorded edges; the two a user is likeliest to trip over: the Assigned
   and Muted tabs overlap (a mute assigns), and unassigning from mainline
   drops a build's mute of the same test. The "New failures" delta line
   does not add muted failures back. Change nothing until someone asks.
3. **The master-side perf finding** from the WP-40 A/B: 65
   `current_assignments` rows cost the browse page's count ~8 ms, on
   `master` too. A candidate for the next performance pass, with the
   "Known slow" table below.
4. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
5. Then whatever the users report.

## Where the code is

| | |
|---|---|
| `origin/master` | `49e596d` — **deployed** |
| `process-round-1` | the process files (docs and `.claude/` only); PR open, review and squash |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **12** before merging (registry §1) and needs a MariaDB ledger step |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25` | merged; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

A local `master` exists since 2026-10-03 (created by the PR #15 merge);
keep it a mirror of `origin/master`, never commit to it.

## Needs a person, not a commit

1. **Should there be a switch that turns the build delete off**, or anything
   in front of it beyond a typed name? The users' habits will say.
2. **Build comments — the workshop the user offered:** mainline lists show a
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
6. WP-40's recorded edges (log, "WP-40 built" and "amended"): none needed a
   decision to ship; see plan item 2.

## Known slow, measured, not changed

Dev-scale SQLite, cold, in-process. Read the Metrics page first: if the
wait is for a worker, none of these is the problem. (Taken before WP-40; the
"WP-40 performance" entry in the log says what WP-40 did to them: nothing,
within noise, after `dbb5523`.)

| | |
|---|---|
| Watch, three cards incl. one full-size build | 84 ms — `compare_counts_many` fetched 20,652 rows to classify in Python |
| A comparison page for a full-size build | 58 ms — the pairs query twice, pinned at two by WP-23 |
| Open Actions' rows | 45 ms, never memoized |
| Home headline | 36 ms, of which 24 is the one pass that is left |
| Browse page's count with 65 assignments | +8 ms over none — on `master` too (WP-40 A/B) |

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `.scratch/net/run_net.py` — the six-class sanity net (~18 s, port 8931).
  Boots the pinned worktree `.scratch/net-wt` (`b816151`); re-point it to
  verify a branch and restore the pin afterwards. **It fails 5 checks on
  `master` and did before the drop**: its seed is dated August. Re-seed
  it before trusting a pass.
- `.scratch/net/drop-2026-09-30/` — the 2026-09-30 drop's drivers, seeded
  databases and benchmark scripts, plus the WP-40 A/B scripts and raw
  numbers (`wp40_ab.txt`, `wp40_ab.py`, `wp40_bench.py`, `wp40_dash.py`);
  `README.txt` has the command lines. **To compare two code trees use the
  in-process, alternated method** — HTTP timings here vary 2–3× between runs.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307,
  option file `.scratch/mariadb-test.cnf`; `.scratch/mariadb-test-via.cnf`
  names a second sacrificial database for `TESTBOARD_TEST_DB_VIA_UPGRADE=1`
  runs or a second agent): start `mariadbd.exe` with
  `--defaults-file=.scratch/mariadb-data/my.ini --port=3307`, then set
  `TESTBOARD_TEST_DB_CNF` (an absolute path). It is not 10.3; CI's two 10.3
  legs are the authority for production's stream. It may still be running
  from the 2026-10-01 session.
- A play server for trying the UI: `python run_server.py --db <COPY of
  testboard.db> --port 8947 --host 127.0.0.1 --workers 4`. Never the
  repo-root file itself.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # 49e596d on top = the 2026-10-01 drop; it is deployed
gh pr list --state open              # expect #9 only (plus this admin PR until merged)
python -m unittest discover          # expect 2640 OK (skipped 1), SQLite
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
