# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

**testboard is live in production and has been since 2026-07-26.** It is no longer
greenfield: ~25k lines, 1,333 tests, schema at migration 7, deployed and in daily use
by a small group of testers.

**Starting a session: read [`docs/SESSION_HANDOVER.md`](docs/SESSION_HANDOVER.md) first.**
It is one screen: what state the branches are in, what is parked where, and what the
next piece of work is. It is rewritten each session rather than appended to, so it is
current by construction.

The other documents, and what each is for:

| Document | What it is | How to treat it |
|---|---|---|
| `docs/SESSION_HANDOVER.md` | State of play, right now | **Rewrite** it when the state changes |
| `docs/UPGRADE_PLAN.md` | Work orders, WP-0 … WP-16, plus the migration version registry | Claim a migration version here before writing one |
| `docs/UPGRADE_PLAN_STATUS.md` | Running log: what was done, what was measured, what was decided and why | **Append only.** Never rewrite an entry |
| `docs/drops/YYYY-MM-DD.md` | **Operator note for one drop** — what changed, how to deploy it, how to roll it back, what was not verified | One per drop, written before it ships. See below |
| `docs/MARIADB_MIGRATION.md` | Runbook for the SQLite → MariaDB move | Fix it in the same commit if you find it wrong |
| `static/whatsnew.html` | What the testers see | Every user-visible change goes in it |
| `docs/BRIEF_dashboard.md`, `docs/BRIEF_feeder_copilot.md` | The original briefs | **Historical.** Useful for intent; the code and the log are the source of truth now, and both have moved on |

## Working practice

- **Changes ship as a dated drop.** Work is batched, verified together, and deployed as
  one group rather than trickled out. Each drop gets a dated section at the top of
  `static/whatsnew.html` (newest first) written for a *tester* — what changed, where to
  find it, what it means for them. Nothing user-visible ships without a line there, and
  nothing appears there that is not in the build; a note promising a feature the drop
  does not contain sends people hunting and then reporting its absence as a bug.
  Every release section carries `data-drop-date="YYYY-MM-DD"` matching its heading —
  three things read it and `tests/test_frontend_calls.py::DropDateTest` fails the build
  if it is missing or disagrees.
- **Every drop also gets an operator note**, `docs/drops/YYYY-MM-DD.md`, written for
  whoever deploys it rather than for whoever uses it — and written *before* it ships, so
  it is a plan and not a memoir. The two documents have different readers and neither
  substitutes for the other: `whatsnew.html` says "the Time page works again",
  the operator note says which flags are new, whether a migration runs, what the
  rollback is, and what to check in the first hour. It must contain:

  | Section | Why it is not optional |
  |---|---|
  | Suite count, schema version, whether a **migration runs** | Decides whether the rollback is `git checkout` or a database restore. Say it explicitly even when the answer is "none" |
  | What changed, fixes first | The operator is triaging, not reading a changelog |
  | The **exact commands**, including the stop/copy/pull/start order | "Restarting the server is not optional" is in this file and has still been forgotten twice |
  | How to **check it came up**, and how to **roll back** | A rollback improvised during an incident is a second incident |
  | New flags and any **decision the operator has to make** | A flag nobody was told about is a flag nobody uses |
  | **What was not verified** | The honest one, and the one worth the most. No browser has ever rendered this project's UI before a drop; say so, every time, rather than letting green tests imply otherwise |

  Keep it to one screen per section and lead with the number that changes the plan.
  A note that has to be read twice to find out whether a migration runs is not concise.
- **One package, one commit** (or a small ordered series), on a branch named
  `wp-<n>-<slug>`. Commit messages carry the *reasoning* and the *measurements* —
  they are the primary record and the log summarises them.
- **Measure, do not estimate.** Every performance or migration claim in this repo has a
  number behind it, and says which database it was taken on. The repo-root
  `testboard.db` is **generated dev data**, not a copy of production — production is
  roughly four times its size. Never say "production" about a number taken here.
- **Restarting the server is not optional after a Python change.** Static files are read
  from disk per request, so a stale process serves new HTML/JS against old handlers. That
  failure looks like a UI bug, not a deployment mistake, and it has cost time twice.
- **Guard tests encode production findings.** If your change makes one fail, widen its
  scope — never weaken its assertion — and say which you did in the commit message.

## Hard Constraints (apply to all code)

1. **Python 3.6 exactly.** No 3.7+ features. Specifically forbidden: `dataclasses` (use `typing.NamedTuple` class syntax), `http.server.ThreadingHTTPServer` (hand-compose one on `http.server.HTTPServer`; note it must be a fixed worker POOL, not `socketserver.ThreadingMixIn` — see below), `datetime.fromisoformat` (write one `strptime`-based ISO-8601 parser, unit-test it, use it everywhere), `subprocess.run(capture_output=...)`, `typing.Protocol`/`Literal`, walrus operator, positional-only params, `breakpoint()`, `contextlib.nullcontext`. f-strings and `async`/`await` are fine.
2. **Standard library only.** No pip installs, no build step, nothing to set up on the server — that property is the point, not the letter of the rule. `unittest` (not pytest), `sqlite3`, `http.server`, `json`, `urllib.request`.
   **One narrow exemption:** pure-Python third-party source may be *vendored* under `third_party/`, where the stdlib has no equivalent and the package has no dependencies of its own. Vendored code is *present*, not *installed*, so the deployment property is preserved. It is exempt from this project's style rules (annotations, `typing.*` conventions) — holding upstream to them would mean either editing it, making it un-updatable, or accumulating excuses, making the gate meaningless — but **not** from the 3.6 parse gate, which is correctness. `tests/test_python36_compat.py::VendoredCodeTest` enforces the split. Currently: PyMySQL 1.0.2 — used by `tools/migrate_to_mariadb.py` today and by the MariaDB storage backend when §F of the runbook lands; `tests/test_vendored_driver.py` allowlists who may import it and still holds `testboard/` and `feeder/` to "not yet". Do not add anything else without the same justification.
   **Never shell out to a tool that would have to be installed.** Constraint 2 is about the *deployment host*, not about `pip` specifically: an RPM, a system binary, or a CLI on `PATH` is a dependency in exactly the same way, and it fails the same way — at deployment, on someone else's machine, at the worst moment. Vendored code exists precisely so this is never necessary. Concretely: the MariaDB work talks to the server through `third_party/pymysql`, **never** through the `mysql` client via `subprocess`. If a guard test appears to forbid using a vendored package for a legitimate purpose, amend the guard with its reasoning updated — do not route around it by adding a host dependency. `subprocess` is for things that are unambiguously part of the interpreter's own environment (`sys.executable`), not for substituting a library.
3. **No npm, no build step, no CDN.** Frontend is static HTML + vanilla ES6 JS + CSS served by the same process; assume browsers have no internet access.
4. **Full type annotations** on every function/method (3.6-compatible). No `Any` except at JSON boundaries, converted to typed structures immediately. Use `typing.List`/`Dict`/`Optional` — **never** PEP 585 builtin generics (`list[str]`) or PEP 604 unions (`int | None`), which are a runtime `TypeError` on 3.6 while looking perfectly fine on a modern interpreter.
5. **No global mutable state**; the storage object is injected into handlers.

`tests/test_python36_compat.py` enforces constraints 1 and 4 statically on every test run, so a 3.6 violation fails the suite on whatever interpreter you are holding rather than waiting for the RHEL 8 CI job. It parses every file with `ast.parse(..., feature_version=(3, 6))`, rejects builtin generics *anywhere* (including `cast(list[str], x)` and module-level aliases), whitelists `typing` names against 3.6.0, forces every annotation to evaluate (3.14 defers them under PEP 649, so a green suite is otherwise no evidence), and keeps `run_server.py`/`run_feeder.py` free of inline annotations and f-strings so they still parse under Python 2. It carries planted-regression tests to prove the detectors can actually fail.

## Commands

- Run all tests: `python -m unittest discover`
- Run one test module: `python -m unittest tests.test_storage`
- Run one test: `python -m unittest tests.test_storage.TestClass.test_method`
- Run the server: `python run_server.py --port <p> --db <path> --host <h>`
  (add `--workers N` / `--cache-mb MB` to tune the pool; defaults are fine)
- Catch a stall for later reading: `--perf-log PATH`, then
  `python tools/perf_report.py PATH`. Off unless asked for, capped and rolled over, so
  it is safe to leave on — and an intermittent fault cannot be caught by logging
  started after it. The queue-wait column is what separates "slow query" from "no free
  worker"; they are opposite diagnoses.
- Delete a bogus environment: `python tools/drop_environment.py --db <path> -e NAME`
  (`--dry-run` first; it cannot be undone)
- Add a site-specific What's new note: `python tools/add_site_note.py --db <path>
  --text "..."` (`--list` for ids, `--edit`/`--remove` to correct one — a note is live
  the moment it is written)
- **Never against the repo-root `testboard.db`** for anything that migrates or writes —
  copy it to a temp directory first. Opening it with current code migrates it.

## Architecture (dashboard)

Layout as built: `testboard/` package with `model.py` (NamedTuples, `Result` enum, ISO time parse/format), `storage.py` (all SQL, sqlite migrations), `analytics.py` (pure functions), `api.py` (framework-free routing/handlers), `server.py` (http.server glue + static files); `static/` for the UI; `tests/` for unittest suites.

Key design decisions. The first few came from the brief; the rest were bought
with production incidents and are recorded in `docs/UPGRADE_PLAN_STATUS.md`:

- **Handlers are plain testable functions** taking (parsed request, storage) and returning `(status, headers, body)`; the `BaseHTTPRequestHandler` subclass is a thin shell. Unit-test handlers directly; a few end-to-end tests boot a real server on an ephemeral port.
- **Test identity** is the triple `(environment, script, test_name)`; a **run** is keyed
  by that triple plus `start_time`, with a UNIQUE constraint. Import is an idempotent
  upsert on that key, done as **SELECT-then-UPDATE-or-INSERT** — deliberately *not*
  `INSERT OR REPLACE`, which deletes and re-inserts and would churn `runs.id` on every
  nightly re-import, while `run_outputs.run_id` and `latest_runs.run_id` both reference
  it. `ON CONFLICT DO UPDATE` is unavailable (3.6's bundled sqlite predates it).
  `tests/test_sql_portability.py` pins both the id stability and the fact that the only
  two `INSERT OR REPLACE` sites are on tables where the difference is unobservable.
- **Timestamps** are ISO-8601 UTC strings (`YYYY-MM-DDTHH:MM:SS.ffffff`, no timezone suffix) everywhere — storage, transport, and comparisons (lexical ordering works).
- **`result`** is an `enum.Enum`: `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS`. Analytics treat `FAILED_AS_EXPECTED` as non-failure and `UNEXPECTED_PASS` as noteworthy-but-not-failure.
- **`output` can be large**: it lives in its own table (`run_outputs`), zlib-compressed, and is read by exactly one endpoint (`GET /api/runs/{id}`). Never join it into a list query — keeping it out of `runs` is what keeps metadata reads dense.
- **The server serves from a fixed worker pool, never a thread per request.** Storage keeps connections in `threading.local()`, so a thread per request means a connection per request means an empty SQLite page cache on every request — measured: 20 requests, 20 connections, and no `cache_size` setting can help a cache that is discarded before it is used twice. The pool size *is* the connection count and is what a `--cache-mb` budget is divided by; `tests/test_server_pool.py` fails if the mixin comes back.
- SQLite: WAL mode + busy timeout at connect (threaded server), versioned migration
  table (`schema_version`). **`MIGRATIONS` holds seven entries and entry 1 describes a
  database that exists in production — never edit it.** Every schema change is a new
  appended entry whose version is claimed from the registry in `docs/UPGRADE_PLAN.md`
  §1 *in the same commit*; version 8 is claimed by WP-15 (renumbered from 6, then 7,
  as WP-17 and WP-18 each shipped first — the parked WIP branch must renumber before
  merging).
  `tests/test_migrations.py` freezes entry 1 by hash and asserts the fresh-install and
  incremental paths produce identical schemas. A migration may contain a Python step
  (`"python: <name>"`). A database whose version exceeds the code's is refused, not
  used — so a rollback needs a copy of the file taken beforehand.
- **Scale is the design constraint**: ~12,000 tests a night, kept for a year (~4.4M runs). No endpoint may be proportional to the size of the estate — *or of its history*. Three derived tables are maintained inside the writing transaction: `latest_runs` (one row per test, carrying its latest and previous result), `current_assignments`, and `activity_hours` (run counts per environment × UTC hour × result — what the staleness cutoff and the trend read; migration 6). Estate-wide reads go through them, list endpoints are paginated in SQL, and only the returned page joins `runs`. Nothing may scan a window of `runs` at request time — the bucket query that did was 3.5s mean on production and grew every night. `ORDER BY` cannot be parameterized — sort keys come from the `DASHBOARD_SORTS` whitelist.
- **A byte-identical re-import writes nothing.** The site feeder re-pushes its whole recent window every 10 minutes whether anything ran or not; `runs.output_fingerprint` is how an unchanged record is recognised without reading the stored blob. The skip is also what lets a retirement survive the next push — before it, ANY re-import un-retired the test. On the wire, the import response's `updated` still includes unchanged records (deployed feeders sum it); `unchanged` refines it.
- **A run belongs to a test, not to a batch** — the import contract has no session/batch id. A *suite execution* is therefore inferred from run timings by `analytics.group_executions` (new execution when a run starts more than 60 min after the latest end seen). A suite can run more than once a day, so anything bucketed by calendar day (the home trend) must not be described as "per night".
- **"Recently run" is derived from the suite, not from the wall clock.** Environments run
  SEQUENTIALLY and hours apart, and the suite does not run every night, so a fixed window
  was wrong every Monday and wrong every morning for whichever environment ran first —
  and it gated the offer to RETIRE a test, so it offered to retire thousands of healthy
  ones. `analytics.find_passes` groups per-environment activity into passes (a block only
  counts if it ran ≥50% of that environment's tests, or ad-hoc re-runs after a fix would
  count); `analytics.recent_cutoff` takes the start of the *previous* covered pass, then
  the oldest across environments. Two clamps are load-bearing: never stricter than the
  36-hour fallback, never older than 14 days. **Do not remove them** — everything feeding
  this is derived or declared, and they are what bound how wrong it can get.
- **Never label a window from a constant.** `_SUMMARY_RECENT_HOURS` (36) is only the
  fallback; the window actually used is `stale_before`, which the API reports. Wording
  built from the constant has been wrong three separate times — "Last night" over a
  78-hour window, "silent for 36h+", "nothing in the last 36 hours" over a fortnight.
  `tests/test_frontend_calls.py::WindowWordingTest` fails the build if it comes back.
- **The coverage denominator is declarable** (`environment_expectations`, migration 5).
  Inferred from `latest_runs` it is a high-water mark, and too large a denominator means
  no pass counts, which silently drops the cutoff back to the wall clock. `/api/environments`
  echoes how many recent passes actually counted so a wrong number is visible.
- **Retirement** (`test_retirements`) is human-entered state marking a test as no longer in the suite: excluded from every estate view, counted only as `status.retired`, history untouched, and cleared automatically if the test reports a run again.
- Analytics are **pure functions over lists of runs** (failing-since/regression window, flakiness score from result transitions, day-of-week profile, duration trend) over a window of last 90 days or last 200 runs, whichever is smaller.
- The `/api/import` transport schema is a **fixed contract shared with the feeder** — keep it stable and document it in the README.
- Frontend security: user-supplied strings (comments, test output) go into the DOM via `textContent`, never `innerHTML`. Static file serving must reject path traversal (`..`) — and be tested for it.

## Feeder (separate deliverable, per its brief)

`feeder.py` CLI + swappable site-specific reader module + generic submitter module (validation, batching default 500, retry ×3 with backoff, failed-batch replay files, high-water-mark state file for daily mode). Never let one bad record abort an import — log, skip, count. Exit code 0 only if all valid records were accepted.
