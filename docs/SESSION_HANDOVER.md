# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-10-01, early**. Two branches in flight: the drop
of 2026-10-01 (WP-39, ready to ship alone) and **WP-40, candidate
environments**, whose first commit — paying off the MariaDB upgrade
tool's debt before building migration 11 on it — is done.

## Where things stand

- **Production is MariaDB, schema v10, serving `master` at `a6f59d2`** —
  the 2026-09-30 drop, PR #13, squash-merged 06:15 UTC. `--workers 16`.
  The dodgy environment was deleted and left an empty build in the Build
  picker: that is WP-39.
- **Not reported back from the 2026-09-30 deploy** — ask before assuming
  either: whether the counters were left on; what "Waited, mean" reads
  during a run.
- **`drop-2026-10-01` holds WP-39** (empty builds pruned: the environment
  delete settles the builds it touches, and the server sweeps at start).
  Python changed, no migration. Operator note `docs/drops/2026-10-01.md`;
  tester note in `whatsnew.html`. **Ship it alone**; WP-40 is its own drop.
- **`wp-40-acknowledged-failures` holds WP-40 commits 1–3** (branched from
  `drop-2026-10-01`; rebase onto `master` after that ships, since PR
  merges here are squashes): `tools/upgrade_mariadb_schema.py` is now a
  ledger — one `Step` per migration above 7, everything derived from it,
  `LedgerTest` (server-free) failing the suite when a SQLite migration
  has no MariaDB step. Runbook §G is a procedure; §G.5 is how to add a
  step. **Feeders are never stopped for an upgrade** (a dozen servers;
  every feeder's contract defers a push that meets a stopped server);
  the dry run says whether the SERVER must stop, from the ledger's
  declaration of which tables a step rewrites. Migration 11 will only
  create, so it runs under the live server.
- **The candidate-environments proposal is DROPPED** (user, 2026-10-01).
  WP-40 is now **acknowledged failures**: a person acknowledges a
  failing test on a stream, with a reason, for 1–7 days on mainline or
  until the build goes; failing counts exclude acknowledged tests and
  show the acknowledged count beside them. **The spec is written** — the
  2026-10-01 "WP-40 spec" entry in the log is the contract; every
  design question was put to the user and answered there. Migration 11
  is claimed in the registry (WP-15 moves to 12). **No spec for the package itself is written yet** — the
  proposal and its review are in the conversation of 2026-09-30 evening
  and summarised below.

## WP-40 in one screen (the log's spec entry is the contract)

- **Unit:** one test, one environment, one stream (`stream_id` + the
  triple). **Predicate:** failing now AND a live acknowledgment
  (`until IS NULL OR until > now`, `now` the request's, never memoized).
  A pass in between changes nothing.
- **Duration:** 1–7 days on mainline (default 7; `>7` and `null` refused
  in storage AND the API); on a build the same or "until the build goes".
- **Extension:** acknowledging an acknowledged test extends it; every
  act is a history row; the extension count shows on the test. Open
  Actions gets an **Expiring** queue with one-click extend.
- **Reason:** required on a fresh acknowledgment, optional on an extension.
- **Headline rule, everywhere:** `failing` excludes acknowledged;
  `acknowledged` is shown beside it — home tiles, Actions, Watch,
  product cards, build tiles. Never subtracted silently.
- **Reads:** the rollup stays one pass; acknowledged counts are a tiny
  second query bounded by the number of acknowledgments, subtracted at
  request time. List categories gain `NOT EXISTS (live ack)`; a new
  `acknowledged` category; rows carry the acknowledgment on the page
  only. Nothing on the push path.
- **Schema (migration 11, creates only):** `test_acknowledgments`
  (current, PK stream+triple, `extensions` counter) and
  `acknowledgment_history`. Both in `_ENVIRONMENT_TABLES` and in the
  stream delete/prune. MariaDB step in the ledger with empty `alters`;
  both tables in the exporter's `ddl()`.
- **API:** `POST /api/acknowledgments/bulk` (same `tests` list as the
  other bulk endpoints; `days`), `POST /api/acknowledgments/clear`,
  `GET /api/acknowledgments?expiring_within_hours=`.
- **One drop.** Migration runs on both backends; operator note says so.

## Next session's plan

1. **Ship `drop-2026-10-01`** per its operator note; the start-up line
   `empty builds: removed 1 (build:…)` is the check.
2. **Build WP-40** from the spec, in this order, one commit each:
   migration 11 (both halves — SQLite entry, ledger step, exporter
   tables; `LedgerTest` says when they agree) + storage + the two table
   lists; the API (bulk, clear, list) + the predicate in summary and
   the dashboard list + Actions' Expiring queue; the UI (home tile and
   queue, selection bar, test page, build pages, Watch/Actions counts);
   `whatsnew.html`; the operator note with a database step (runbook §G,
   creates-only so the server may keep running).
3. **Get the two deploy answers above.**
4. **Tidy the branches.** PR #12 (`docs-handover-2026-09-29`) is redundant
   — close it. `drop-2026-09-30` is merged in content but not an ancestor
   of `master`; delete it by name.
5. **Decide the Java client's fate** (PR #9), open since 2026-09-08.

## Where the code is

| | |
|---|---|
| `origin/master` | `a6f59d2` — **deployed** |
| `drop-2026-10-01` | WP-39 + the deploy record. **Ready to ship**, alone |
| **`wp-40-acknowledged-failures`** | **THE working branch** (renamed from `wp-40-candidate-environments` at the pivot). Three commits: the ledger, the feeder correction, the spec. The build itself next |
| `drop-2026-09-30` | shipped as PR #13 (squash). Delete when convenient |
| `docs-handover-2026-09-29` | PR #12 **open**, redundant — close it |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, green, untouched since 2026-09-08 |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **12** once WP-40 takes 11 (registry §1) |
| `wp-32-timeline-follow`, `wp-31-own-results-always`, `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25` | merged; prune when convenient. Six sibling worktrees (`TestDashboard-*-wt`) hold some of them — remove the worktree before the branch |

There is **no local `master` branch** in this checkout; work from
`origin/master`.

## Needs a person, not a commit

1. **WP-40 has no open judgement calls** — all eight were put to the
   user on 2026-10-01 and are in the spec entry. Anything the build
   turns up goes in the log, not here.
2. **Should there be a switch that turns the delete off**, or anything in
   front of it beyond a typed name? Now that it is deployed, the
   testers' first use will say.
3. **Build comments — the workshop the user offered:** mainline lists
   show a test's newest comment of any origin, unlabelled; deleting a
   build clears the tag on its comments; nothing carries from one build
   to the next of the same branch; "has a comment" is not "acknowledged".
4. **Decide the Java client's fate** (PR #9): merge or park.
5. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls; first Tcl 8.5 site is
   still an experiment.
6. Carried from WP-31: a build whose last run is more than 36 hours old
   shows "Reported 0 of N" on its own tiles. **Do not loosen the clamp**.

## Known slow, measured, not changed

Dev-scale SQLite, cold, in-process. Read the Metrics page first: if the
wait is for a worker, none of these is the problem.

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
  `TESTBOARD_TEST_DB_CNF`. It is not 10.3; CI's two 10.3 legs are the
  authority for production's stream.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # a6f59d2 on top means drop-2026-10-01 has NOT shipped
gh pr list --state open              # expect #9 and, until closed, #12
python -m unittest discover          # expect 2550 OK (skipped 1) on wp-40-acknowledged-failures, SQLite
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
