# Upgrade round 1 — status

Running log for [`UPGRADE_PLAN.md`](UPGRADE_PLAN.md). **Append, never rewrite.**

This file exists so work can be resumed cold, by someone (or something) with no
memory of the session that started it. If you are picking this up: read the
plan, read this file, run `git log --oneline`, run the suite, then take the
first package below whose state is not `done`.

**Ground rules that survive a restart** — the full set is §0 of the plan, but
these are the ones that get forgotten:

- Production is live. `MIGRATIONS[0]` is frozen; new entries only, versions
  claimed from the plan's §1 registry.
- Never run a migration, a tool, or the server against the repo-root
  `testboard.db` — it holds a copy of real data. Work on copies in a temp
  directory.
- Guard tests (`test_frontend_calls.py`, `test_server_pool.py`,
  `test_python36_compat.py`, `test_migrations.py`) encode production findings.
  Widen them; never weaken them.
- Full suite green before every commit. One package, one commit. Do not push.

---

## State

| # | Package | State | Commit |
|---|---|---|---|
| — | Plan + MariaDB runbook | **done** | *(this commit)* |
| WP-0 | Migration registry guard | pending | |
| WP-11 | Vendor PyMySQL | pending | |
| WP-4 | Deactivate users (migration 2) | pending | |
| WP-5 | `duration_seconds` (migration 3) | pending | |
| WP-1 | Extract `review.js` | pending | |
| WP-2 | Review on Open actions | pending | |
| WP-3 | Triage result emphasis | pending | |
| WP-7 | Sortable columns | pending | |
| WP-6 | Time analysis tab | pending | |
| WP-8 | Last pass + flaky signal | pending | |
| WP-9 | SQL portability groundwork | pending | |
| WP-10 | MariaDB export tool | pending | |
| — | Performance pass | pending | |

States: `pending` → `in progress` → `done`, or `blocked` / `deferred` with a
reason in the log below.

---

## Log

### 2026-07-27 — planning

Wrote `UPGRADE_PLAN.md` (12 packages) and `MARIADB_MIGRATION.md` (runbook).

Decision taken by the user: **vendor the MySQL driver** so nothing is installed
on site. Plan §4 decision 1 closed; WP-11 added.

Baseline at the start of implementation: **808 tests, OK (skipped=1)**, working
tree otherwise clean at `964e0b4`.

Still outstanding and **not** a code task: `tools/diagnose_db.py --compare-local`
has not been run on production since the worker-pool fix. Every timing predating
that fix describes a server that discarded its page cache every request.
