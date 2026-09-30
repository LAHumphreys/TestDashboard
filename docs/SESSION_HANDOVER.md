# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-09-30**. A five-package drop, built overnight from
requests made the evening before, is on `drop-2026-09-30` waiting to be
merged and deployed. Nothing in it has been deployed.

## Where things stand

- **Production is MariaDB, schema v10, serving `master` at `91d5cd3`** —
  the 2026-09-24 drop (WP-32). The user states GitHub `master` is what is
  deployed. Follow works on real runs ("working beautifully", 2026-09-29).
- **The site pushes results DURING a run, and runs twice a night** —
  mainline, then one build, ~13,000 tests each. Both facts are now the
  design load: every changing import clears every memo, so for the hours
  a run lasts, a page's COLD cost is its cost.
- **`drop-2026-09-30` is built, tested and not deployed.** WP-33 … WP-37.
  No migration. Python changed in every package.

## Today's plan

1. **Read `docs/drops/2026-09-30.md`** — in particular "The decision you
   have to make" and the three things there is no switch for. The delete
   has no login in front of it and no flag to turn it off.
2. **Merge and deploy** per that note: stop, checkout, start. If it ships
   on a later day, re-date the note, the `whatsnew.html` heading and its
   `data-drop-date` together.
3. **Open the Metrics page on production during a run.** It is the first
   measurement this project will have of production itself. Look at
   "Waited, mean" (should be near zero), at the home headline's mean, and
   at how fast the memo's "cleared N times" climbs.
4. **Delete the dodgy build's environment** — after stopping whatever is
   still sending it, or it will come back at the next push.

## What the drop is

| | | |
|---|---|---|
| WP-33 | Environment filter on a build's "Difference from" tab | `bc73f1e` |
| WP-34 | Delete what a build holds for ONE environment, from its page | `e7e5a86` |
| WP-35 | A comment made from a build's page is a comment on that build | `04f2537` |
| WP-36 | Performance pass: the summary reads its partition once | `1131b56` |
| WP-37 | The Metrics page | `3aadcf9` (unfinished) + `9b97ce4` (completes it) |
| WP-38 | A push drops only the memos of what it wrote; the home page paints its frame first | see `git log` |

**Suite on the ship branch: 2526 OK (skipped 1), SQLite.** Dual-backend,
local MariaDB 12.3: 3419 OK (skipped 66).

## Where the code is

| | |
|---|---|
| **`drop-2026-09-30`** | **THE branch. Ship this.** Cut from `docs-handover-2026-09-29`, so it contains that branch's one commit too |
| `origin/master` | `91d5cd3` — deployed |
| `docs-handover-2026-09-29` | PR #12, docs only, green. Merging the drop makes it redundant: close it, or merge it first — either order works, the content is identical |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08; one commit ahead of `master`, two behind |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25` | merged; prune when convenient. Six sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

There is **no local `master` branch** in this checkout; work from
`origin/master`.

## Needs a person, not a commit

Questions the drop raised and deliberately did not answer. The status log
entry for 2026-09-29→30 has the numbers behind each.

1. ~~Should a changing import clear only its own stream's memos?~~
   **Done, WP-38**, one level further: per `(stream, environment)`.
   The question that remains is the pool: **`--workers 16` on
   production?** — decide from the Metrics page's "Waited, mean".
2. **Should there be a switch that turns the delete off**, or anything in
   front of it beyond a typed name? And should a delete block re-import —
   which needs a table, so migration 11?
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
order the numbers suggest:

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
  verify a branch and restore the pin afterwards.
- `.scratch/net/drop-2026-09-30/` — this drop's drivers (212 checks, 13
  scenarios: `wp33_`, `wp34_`, `wp35_`, `wp37_drive.mjs`), its two seeded
  databases, and the benchmark scripts. Its `README.txt` has the command
  lines. They import the WORKING TREE's `static/`, one node process per
  scenario (module state is a singleton). **To compare two code trees use
  `bench_ab.py`** — in-process, alternated — because HTTP timings on this
  machine vary 2–3× between runs minutes apart.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307,
  option file `.scratch/mariadb-test.cnf`): start `mariadbd.exe` with
  `--defaults-file=.scratch/mariadb-data/my.ini --port=3307`, then set
  `TESTBOARD_TEST_DB_CNF`. It is not 10.3.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # 91d5cd3 on top means the drop has NOT merged
gh pr list --state open
python -m unittest discover          # expect 2501 OK (skipped 1) on the drop branch
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
