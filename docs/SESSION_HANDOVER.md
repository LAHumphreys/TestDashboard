# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-10, overnight**, closing the tooling night.
All four phases of `NIGHT_RUN_2026-08-10.md` are **done, merged, and
green**. The night's output is one branch: **`tooling-2026-08-10`**.

## The one thing that matters this morning

**Deploy `tooling-2026-08-10` to production (MariaDB), including the
v7→v10 schema upgrade.** Everything needed now exists; last night it did
not. Read, in this order:

1. `docs/drops/2026-08-11.md` § **"Production deployment (MariaDB)"** —
   the exact command order, and the only section you must not skim.
2. `docs/MARIADB_MIGRATION.md` § **G** — the incremental-DDL runbook.

The shape of it: recreate `testboard_migrate` as root (§A.4; it was
dropped after the cutover, so **no DDL-capable credential exists** until
you do) → **pre-upgrade `mysqldump`, which IS the rollback** → run
`tools/upgrade_mariadb_schema.py --dry-run`, then live → verify → deploy
the code → **restart the server** → first-hour checks → delete the
migrate credential.

**DDL autocommits on MariaDB 10.3.** There is no transactional undo. The
dump is not a formality.

## Where the code is

| | |
|---|---|
| **`tooling-2026-08-10`** | **THE branch. Ship this.** `streams-upgrade` + WP-27 + WP-28 + WP-29 + the docs tidy. All six CI legs green |
| **prod** | MariaDB, schema **v7**, pre-streams code. Goes to v10 + this branch today |
| **staging** (old SQLite box) | streams drop, schema v10, deployed 2026-08-10 |
| `streams-upgrade` | the streams work + the two owed status-log writeups. Contained in the ship branch |
| `wp-27/28/29`, `docs-tidy-2026-08-10` | merged into the ship branch; keep until it lands, then prune |
| `origin/master` | `1e1ceae`. **Behind** — the ship branch is what merges into it |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |

**Suite on the ship branch: 2240 OK (skipped 1) SQLite-only; 3016 OK
(skipped 53) dual-backend.** Sanity net PASS both unprefixed (45.3s) and
`--url-prefix testboard` (48.0s). No expected-failure footnote any more —
see "the CRLF failures" below.

## What last night added

- **WP-27 — `tools/upgrade_mariadb_schema.py`.** In-place MariaDB v7→v10.
  Refuses an unexpected version in both directions, bidirectional
  consistency check, `--dry-run`, and a `verify` that diffs the result
  against a fresh v10 export. Plus the `delete_stream` dangling-id fix
  (`assignments`/`current_assignments`, the protection `comments` already
  had — SQLite's FK had been hiding the gap).
- **WP-28 — `--url-prefix`**, default `testboard`, for an nginx front
  door. **Bare paths always keep working**, which is what makes a
  default-on flag safe and what lets feeders bypass nginx entirely.
- **WP-29 — single-file feeders** (`clients/feeder.py`,
  `clients/feeder.tcl`) + `docs/FEEDER_TEMPLATE.md`, so a new product
  checks in ONE file instead of linking a checkout. The deployed feeder is
  untouched.
- **Docs tidy**: four plan docs deleted, `STREAMS_PLAN.md` 108→20 KB,
  `UPGRADE_PLAN.md` 64→32 KB (§1's registry kept verbatim).
- **CLAUDE.md's project state was wrong and is fixed** — it claimed seven
  migrations (there are ten) and that WP-15 owns version 8 (it owns 11).
  Anyone trusting it would have claimed a taken version.

## Findings worth carrying, not just outcomes

1. **The local MariaDB is 12.3.2, not 10.3.** Prod is 10.3.39. Local
   dual-backend runs prove nothing about prod's server; **CI's two
   `mariadb:10.3` legs are the only 10.3 evidence that exists.** Treat
   them as mandatory for anything touching MariaDB DDL.
2. **A new CI leg runs the whole suite against a database the upgrade
   tool built** (`python36-mariadb-upgraded`), because "the schema
   matches" and "the app serves on it" are different claims and only the
   second one is what today needs.
3. **`ALTER TABLE runs ADD COLUMN` takes the INSTANT path** — proven by
   an explicit `ALGORITHM=INSTANT` being accepted, not inferred from a
   fast clock. But only at 500k rows on 12.3.2; prod has ~4.4M. The tool
   prints the row count and warns if that step exceeds 5s.
4. **The CRLF failures are gone and were never real.** Two
   `ProductUrlAdoptionTest` cases asserted on `\n` against files read in
   binary (deliberately — `app.js` contains a real NUL). On a Windows
   checkout they failed on correct content in every fresh worktree while
   passing on Linux CI. `read()` now normalises line endings; the
   assertions are unchanged.
5. **Agents sharing one checkout corrupt each other.** Three collided in
   the main tree last night and one lost work to another's `git stash`.
   Every parallel implementer now gets its own `git worktree`, stated as
   the first instruction. Do this from the start.

## Needs a person, not a commit

1. **Today's deployment** (above). The one hard deadline.
2. **Only prod goes behind nginx**, and it is a follow-up whenever the box
   owners are ready — nothing in this drop assumes nginx exists. The
   tested `location` block is in the drop note; note it deliberately has
   **no trailing slash**, or a bare `/testboard` never reaches the backend.
3. Carried: re-retire the tests the un-retire bug released (search
   comments for "Automatically un-retired"); `tools/diagnose_db.py
   --compare-local` on prod; `max_allowed_packet` persistence with the
   daemon owners (still `SET GLOBAL` only); import output-size cap; the
   morning decision list's UI judgement calls (watch-card accents,
   composer placeholder, "Not run" tab dominance, Build-picker
   discoverability, Watch back-navigation).
4. **First 8.5 site is an experiment.** No Tcl 8.5 interpreter has ever
   executed `clients/feeder.tcl` — none exists here or in CI (8.6 only).
   A static gate rejects 8.6-only constructs; that is an argument, not a
   demonstration.

## First ten minutes of the next session

```bash
git checkout tooling-2026-08-10
git log --oneline -6
python -m unittest discover        # expect 2240 OK (skipped 1)
gh run list --branch tooling-2026-08-10 --limit 1   # expect success
```

The repo-root `testboard.db` is generated dev data (220 MB, v5) — only ever
copied, never opened with current code. There is still no browser here.
Two stale `git stash` entries exist in the main checkout from last night's
collision; they are superseded safety nets and can be dropped once today's
deployment is done.
