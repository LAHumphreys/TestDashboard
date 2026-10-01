# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-10-01**. **WP-40 (muted failures) is built and
merged on `wp-40-acknowledged-failures`**; the drop of 2026-10-01 now carries
WP-39 **and** WP-40, and **a migration (11) runs on both backends**. Nothing
is pushed or deployed yet.

## Where things stand

- **Production is MariaDB, schema v10, serving `master` at `a6f59d2`** —
  the 2026-09-30 drop, PR #13. `--workers 16`.
- **Not reported back from the 2026-09-30 deploy** — ask before assuming
  either: whether the counters were left on; what "Waited, mean" reads
  during a run.
- **`wp-40-acknowledged-failures` is the branch that holds everything for the
  drop**: WP-39 (`2b8c2ff`), the MariaDB upgrade tool as a ledger (`2098aff`,
  `f3aaf0f`), the spec (`bc2aed9`), and WP-40 itself:
  `a8f3b30` migration 11 + storage, `c5c2b87` API, `6f7831d` storage tests +
  ledger step 10 to 11, `154a201` frontend, merged by `c85957e` and
  `0dfa121`; plus the docs commit on top (operator note, log entry, this
  file, CLAUDE.md's three numbers).
- **Measured on this branch:** SQLite **2640 OK (skipped 1)**, from 2550;
  MariaDB 12.3, the two MariaDB suites alone: **998 OK (skipped 70)** (the
  full-variant 3594 predates the 2026-10-01 amendment). Production is 10.3: CI's legs are the authority.
- **What WP-40 is, in one screen** (the log's "WP-40 spec", its addendum and
  "WP-40 built" are the contract): a person mutes a failing test on
  one stream with a reason for **1–168 whole hours** (the UI offers 12
  hours to 7 days from one shared dropdown; never indefinite; no `null`);
  the New / Still failing figures exclude live mutes and "+X muted" is shown
  beside them everywhere a failing count appears (no Muted tile; grey chart
  extensions; Watch "N (+X muted)"), while the **pass rate still counts muted
  failures**; muting assigns and posts the reason as a comment, and
  unassigning drops the mute; the Muted tab lists EVERY live mute, passing
  or failing, with a State column (`status.muted_total`); Open Actions has an
  Expiring list with Extend. **Amended after the first walkthrough (log,
  2026-10-01 "WP-40 amended ..."): the state was renamed "acknowledged" ->
  "muted" end to end, before anything shipped.** Migration 11 only creates
  `test_mutes` and `mute_history`. The MariaDB ledger step
  has `alters=()`, so the upgrade tool's dry run prints `SERVER: may keep
  running`. Six design observations are recorded (not changed) in the log.
  The spec spells the expiry column `until`; the code is `expires_at`.
- **The operator note is written**: `docs/drops/2026-10-01.md` (staging SQLite
  and production MariaDB procedures, rollback for each). The tester note is in
  `whatsnew.html`. The date is provisional in both.

## What remains before shipping

1. ~~The performance A/B against `master`~~ **Done** (log, "WP-40
   performance"): yesterday's gains intact; the one regression it found
   (the browse page's count carrying the mute join) is fixed in
   `dbb5523` and re-measured. Left for a later pass, found on `master`
   too: 65 `current_assignments` rows cost the browse page's count ~8 ms.
2. **Fast-forward `wp-40-acknowledged-failures` into `drop-2026-10-01`**
   (`drop-2026-10-01` holds only WP-39 and the deploy record; WP-40 was
   branched from it, so it is a fast-forward), then check the drop date in
   `whatsnew.html` and the operator note still agree.
3. **Push, open the PR, and wait for CI's two MariaDB 10.3 legs** — the only
   10.3 evidence. Expect the ubi8 leg to read skipped=5.
4. **Deploy per the operator note**: staging (SQLite) first, then production
   (MariaDB, runbook §G: dump, dry run and read the `SERVER:` line, upgrade,
   restart). The restart is not optional.
5. **Squash merges**: PR merges here are squashes, so after shipping, rebase
   or delete the working branches by name.

## Next session's plan

1. Finish the list above. After the deploy, the first-hour checks in the
   operator note, then see what the testers make of the Muted queue.
2. **Get the two deploy answers** (counters on? "Waited, mean" during a run?).
3. **Tidy the branches.** PR #12 (`docs-handover-2026-09-29`) is redundant —
   close it. `drop-2026-09-30` is merged in content but not an ancestor of
   `master`; delete it by name.
4. **Decide the Java client's fate** (PR #9), open since 2026-09-08.

## Where the code is

| | |
|---|---|
| `origin/master` | `a6f59d2` — **deployed** |
| **`wp-40-acknowledged-failures`** | **THE drop branch**: WP-39 + the ledger + WP-40, built and merged, docs on top. Not pushed |
| `drop-2026-10-01` | WP-39 + the deploy record only; receives the fast-forward above |
| `drop-2026-09-30` | shipped as PR #13 (squash). Delete when convenient |
| `docs-handover-2026-09-29` | PR #12 **open**, redundant — close it |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **12** (registry §1) — WP-40 took 11 |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25`, `worktree-agent-*` | merged or throwaway; prune when convenient. Sibling worktrees (`TestDashboard-*-wt`, `.claude/worktrees/*`) hold some of them — remove the worktree before the branch |

There is **no local `master` branch** in this checkout; work from
`origin/master`.

## Needs a person, not a commit

1. **Should there be a switch that turns the build delete off**, or anything
   in front of it beyond a typed name? The testers' use will say.
2. **Build comments — the workshop the user offered:** mainline lists show a
   test's newest comment of any origin, unlabelled; deleting a build clears
   the tag on its comments; nothing carries from one build to the next of the
   same branch. ("Has a comment" is not "muted" is now answered by
   WP-40.)
3. **Decide the Java client's fate** (PR #9): merge or park.
4. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls; first Tcl 8.5 site is
   still an experiment.
5. Carried from WP-31: a build whose last run is more than 36 hours old
   shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.
6. WP-40's six recorded observations (log): none needs a decision to ship;
   #2 and #3 (tab overlap; unassign from mainline dropping a build's
   mute) are the ones a tester is likeliest to trip over.

## Known slow, measured, not changed

Dev-scale SQLite, cold, in-process. Read the Metrics page first: if the
wait is for a worker, none of these is the problem. (Taken before WP-40; the
A/B in the log says what WP-40 did to them.)

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
  `master` and did before the drop**: its seed is dated August. Re-seed
  it before trusting a pass.
- `.scratch/net/drop-2026-09-30/` — the shipped drop's drivers, seeded
  databases and benchmark scripts; `README.txt` has the command lines.
  **To compare two code trees use `bench_ab.py`** — in-process,
  alternated — because HTTP timings here vary 2–3× between runs.
- A local MariaDB 12.3 lives in `.scratch/mariadb-data` (port 3307,
  option file `.scratch/mariadb-test.cnf`; `.scratch/mariadb-test-via.cnf`
  names a second sacrificial database for `TESTBOARD_TEST_DB_VIA_UPGRADE=1`
  runs): start `mariadbd.exe` with
  `--defaults-file=.scratch/mariadb-data/my.ini --port=3307`, then set
  `TESTBOARD_TEST_DB_CNF` (an absolute path). It is not 10.3; CI's two 10.3
  legs are the authority for production's stream.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # a6f59d2 on top means the 2026-10-01 drop has NOT shipped
gh pr list --state open              # expect #9 and, until closed, #12; plus the drop's PR once opened
python -m unittest discover          # expect 2640 OK (skipped 1) on wp-40-acknowledged-failures, SQLite
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
