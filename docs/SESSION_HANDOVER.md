# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-10, evening**, closing the planning session for
the tooling night run. **The fresh session's one job: execute
[`NIGHT_RUN_2026-08-10.md`](NIGHT_RUN_2026-08-10.md)** — status GO, every
decision closed with the user, nothing left to ask overnight.

## The world changed today — read this before anything else

- **Production serves MariaDB.** The SQLite→MariaDB cutover is complete
  and successful; everyone has moved onto it, and word is spreading among
  committers of the first onboarded product. Prod is at **schema v7**
  (pre-streams code). Memory note `mariadb-host-migration-state` holds
  the cutover facts; the append-only status log does NOT yet (writeup
  still owed).
- **The old SQLite box is now a STAGING instance**, and the big streams
  drop (migrations 8–10, tip `4d03da3`) deployed there successfully on
  2026-08-10 — so v7→v10 is proven on production-size **SQLite** data.
- **Tomorrow (2026-08-11) the streams drop goes to prod (MariaDB)** —
  and the repo currently has NO way to do that: the app never runs DDL
  on MariaDB (version mismatch refuses startup both directions),
  `tools/migrate_to_mariadb.py` only does full loads from SQLite, no
  DDL-capable account exists (`testboard_migrate` dropped post-cutover),
  and the drop note's rollback section is SQLite-only wording. Tonight's
  **Phase 2 is what closes all of that** — it is the hard-deadline
  phase; run it first.

## Where the code is

| | |
|---|---|
| **prod** | MariaDB, schema **v7**, pre-streams code. Gets v7→v10 + the streams drop TOMORROW via tonight's Phase 2 tool |
| **staging** (old SQLite box) | the streams drop, tip `4d03da3`, schema v10, deployed 2026-08-10 |
| **`streams-upgrade`** | THE branch — deployed-to-staging tip `4d03da3` + tonight's plan/handover commit + a merge of current master. Cut every night branch from its tip |
| `origin/master` | `1e1ceae` — **current**: PR #6 merged the streams work this morning; master fully contains the deployed drop |
| `wp-24-scoped-urls` / `wp-25-one-kind` / `wp-20…23` | superseded, all contained in `streams-upgrade` |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |

Suite at `4d03da3`: **2094 OK (skipped 1)** SQLite-only (drop note's last
measured figure); dual-backend variants exist when `TESTBOARD_TEST_DB_CNF`
points at the local MariaDB (port 3307, `.scratch/mariadb-test.cnf`).

## Tonight (the night-run doc is authoritative; this is the shape)

Four phases + consolidation, in execution order **2 → 4 → 3 → 1**:
WP-27 MariaDB in-place upgrade tool (+ the decided `delete_stream`
dangling-id fix) → WP-29 single-file feeders (Python 3.6 + vanilla Tcl
8.5, cleanup-invoked push model, conformance suite, `FEEDER_TEMPLATE.md`,
Tcl CI leg) → WP-28 `--url-prefix` (default `testboard`, bare paths
always accepted; nginx will NOT strip; prod-only) → docs tidy
(disposition table approved verbatim, runs last — `FEEDER_BRIEF.md`
harvests into Phase 4's template). No migration version claimed tonight;
nothing user-visible; no whatsnew line. Ship branch:
`tooling-2026-08-10`.

## Needs a person, not a commit (carried + new)

1. **Tomorrow's prod deployment** (the operator, with tonight's runbook
   section): recreate `testboard_migrate` (root, §A.4), pre-drop
   mysqldump, dry-run then live upgrade, deploy, restart, first-hour
   checks.
2. **Status-log writeups owed**: the cutover night (numbers live only in
   the memory note) and the 2026-08-10 staging deployment.
3. Carried from before: re-retire the tests the un-retire bug released
   (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgment calls (watch-card accents,
   composer placeholder, "Not run" tab dominance, Build-picker
   discoverability, Watch back-navigation).

## First ten minutes of the fresh session

```bash
git log --oneline -4                  # expect: master merge, plan/handover commit, then PR #6's merge — on streams-upgrade
git status --short                    # clean
python -m unittest discover           # expect 2094 OK (skipped 1)
python .scratch\net\run_net.py        # expect PASS, ~18s
```

Then read `NIGHT_RUN_2026-08-10.md` end-to-end and arm its §1 operating
pattern (TaskList per phase, hourly fallback wakeup, Sonnet implements /
coordinator reviews). Guardrails verbatim from the plan: never touch
`master`, `wp-18-timeline`, `wp-14-in-run-progress`, or the repo-root
`testboard.db`; the local MariaDB is load-bearing for Phase 2 — if it
will not start, Phase 2 stops at "written, unverified" and says so.

The repo-root `testboard.db` is generated dev data (220 MB, v5 — only
ever copied, never opened with current code). There is still no browser
here; nothing tonight is user-visible, so the usual "no browser rendered
this" caveat applies only to Phase 3's prefix walks — the DOM-shim net
covers link resolution, not layout.
