# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-09-30, late evening**. The drop of 2026-09-30
(WP-33 … WP-38) was merged and deployed to production the same day it was
cut; the user reports it "worked beautifully". **The next drop,
`drop-2026-10-01`, is being built tonight from the first day's use**: its
point 1 (WP-39) is done; more points may follow from the user.

## Where things stand

- **Production is MariaDB, schema v10, serving `master` at `a6f59d2`** —
  the 2026-09-30 drop, PR #13, squash-merged 06:15 UTC. No migration ran.
- **Production can now measure itself.** `metrics.html` is live there.
  Every timing this project has ever quoted was SQLite on a development
  machine; the first production number will come from that page.
- **The site pushes results DURING a run, and runs twice a night** —
  mainline, then one build, ~13,000 tests each. A push now drops only the
  `(stream, environment)` memos it wrote (WP-38), so pages are served from
  memos for most of the day; what remains is a wait for a free worker,
  which no memo shortens.
- **Production runs `--workers 16`** (raised from 8 the day of the deploy).
  **The dodgy environment has been deleted** — and left the build that
  only ran on it as an empty entry in the Build picker, which is WP-39.
- **Not reported back from the deploy** — ask before assuming either:
  whether the counters were left on; what "Waited, mean" reads during a
  run.
- **`drop-2026-10-01` holds WP-39** (empty builds pruned: the environment
  delete settles the builds it touches, and the server sweeps at start).
  Python changed, no migration. Operator note written:
  `docs/drops/2026-10-01.md`; tester note in `whatsnew.html`.

## Next session's plan

1. **Finish `drop-2026-10-01`**: any further points the user adds, then
   merge and deploy per its operator note. The start-up line
   `empty builds: removed 1 (build:…)` is the check that WP-39 did what
   the drop was for.
2. **Get the two answers above.** With 16 workers, "Waited, mean" during
   a run says whether the pool was the bottleneck; if it is near zero,
   the next pass is the query cost table below.
3. **Tidy the branches.** PR #12 (`docs-handover-2026-09-29`) is redundant
   — its commit rode inside the drop — close it. `drop-2026-09-30` is
   merged in content but, being squash-merged, is not an ancestor of
   `master`; delete it by name, not from `--merged`.
4. **Decide the Java client's fate** (PR #9), open since 2026-09-08.
5. Then whatever the testers report from the first days of the delete, the
   build comments and the environment filter.

## Where the code is

| | |
|---|---|
| `origin/master` | `a6f59d2` — **deployed** |
| **`drop-2026-10-01`** | **THE branch.** The deploy record (docs) and WP-39; tonight's further points go here |
| `drop-2026-09-30` | shipped as PR #13 (squash). Delete when convenient |
| `docs-handover-2026-09-29` | PR #12 **open**, redundant — close it |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08; one commit ahead of `master`, now three behind |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25` | merged; prune when convenient. Six sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

There is **no local `master` branch** in this checkout; work from
`origin/master`.

## Needs a person, not a commit

Questions the drop raised and deliberately did not answer. The status log
entries for 2026-09-29→30 and 2026-09-30 have the numbers behind each.

1. ~~`--workers 16` on production?~~ **Done, 2026-09-30.** Whether it
   was needed is what "Waited, mean" now says.
2. **Should there be a switch that turns the delete off**, or anything in
   front of it beyond a typed name? And should a delete block re-import —
   which needs a table, so migration 11? Now that it is deployed, the
   testers' first use will say whether either matters.
3. **Build comments — the workshop the user offered:**
   - mainline lists show a test's newest comment of any origin, unlabelled;
   - deleting a build clears the tag on its comments;
   - nothing carries from one build to the next of the same branch;
   - "has a comment" is not "acknowledged" — no way to hide explained rows.
4. **Decide the Java client's fate** (PR #9): merge or park.
5. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls; first Tcl 8.5 site is
   still an experiment.
6. Carried from WP-31: a build whose last run is more than 36 hours old
   shows "Reported 0 of N" on its own tiles. A design decision about a
   build-specific window — **do not loosen the clamp**.

## Known slow, measured, not changed

Dev-scale SQLite, cold, in-process. Candidates for the next pass, in the
order the numbers suggest — but read the Metrics page first: if the wait
is for a worker, none of these is the problem.

| | |
|---|---|
| Watch, three cards incl. one full-size build | 84 ms — `compare_counts_many` fetched 20,652 rows to classify in Python |
| A comparison page for a full-size build | 58 ms — the pairs query twice, pinned at two by WP-23 |
| Open Actions' rows | 45 ms, never memoized |
| Home headline | 36 ms, of which 24 is the one pass that is left |

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `.scratch/net/run_net.py` — the six-class sanity net (~18 s, port 8931).
  Boots the pinned worktree `.scratch/net-wt` (`b816151`); re-point it to
  verify a branch and restore the pin afterwards. **It fails 5 checks on
  `master` and did before the drop**: its seed is dated August, so no
  build in it has run in the last fortnight. Re-seed it before trusting a
  pass.
- `.scratch/net/drop-2026-09-30/` — the shipped drop's drivers (212 checks,
  13 scenarios), its two seeded databases, and the benchmark scripts. Its
  `README.txt` has the command lines. They import the WORKING TREE's
  `static/`, one node process per scenario. **To compare two code trees use
  `bench_ab.py`** — in-process, alternated — because HTTP timings on this
  machine vary 2–3× between runs minutes apart.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307,
  option file `.scratch/mariadb-test.cnf`): start `mariadbd.exe` with
  `--defaults-file=.scratch/mariadb-data/my.ini --port=3307`, then set
  `TESTBOARD_TEST_DB_CNF`. It is not 10.3; CI's two 10.3 legs are the
  authority for production's stream.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # a6f59d2 on top means drop-2026-10-01 has NOT shipped
gh pr list --state open              # expect #9 and, until closed, #12
python -m unittest discover          # expect 2540 OK (skipped 1) on drop-2026-10-01, SQLite
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
