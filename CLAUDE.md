# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

This is a greenfield project. The only content so far is two specification briefs in `docs/`:

- `docs/BRIEF_dashboard.md` — the main project: "testboard", a self-contained web dashboard for overnight test results (storage, HTTP API, analytics, web UI).
- `docs/BRIEF_feeder_copilot.md` — a separate feeder script that pushes results into the dashboard's `/api/import` endpoint.

**Read the relevant brief in full before implementing anything.** The briefs are the source of truth for the domain model, API contract, project layout, and quality bar; this file only summarizes the non-negotiables.

## Hard Constraints (apply to all code)

1. **Python 3.6 exactly.** No 3.7+ features. Specifically forbidden: `dataclasses` (use `typing.NamedTuple` class syntax), `http.server.ThreadingHTTPServer` (hand-compose one on `http.server.HTTPServer`; note it must be a fixed worker POOL, not `socketserver.ThreadingMixIn` — see below), `datetime.fromisoformat` (write one `strptime`-based ISO-8601 parser, unit-test it, use it everywhere), `subprocess.run(capture_output=...)`, `typing.Protocol`/`Literal`, walrus operator, positional-only params, `breakpoint()`, `contextlib.nullcontext`. f-strings and `async`/`await` are fine.
2. **Standard library only.** No pip installs, no build step, nothing to set up on the server — that property is the point, not the letter of the rule. `unittest` (not pytest), `sqlite3`, `http.server`, `json`, `urllib.request`.
   **One narrow exemption:** pure-Python third-party source may be *vendored* under `third_party/`, where the stdlib has no equivalent and the package has no dependencies of its own. Vendored code is *present*, not *installed*, so the deployment property is preserved. It is exempt from this project's style rules (annotations, `typing.*` conventions) — holding upstream to them would mean either editing it, making it un-updatable, or accumulating excuses, making the gate meaningless — but **not** from the 3.6 parse gate, which is correctness. `tests/test_python36_compat.py::VendoredCodeTest` enforces the split. Currently: PyMySQL 1.0.2 (for the MariaDB backend). Do not add anything else without the same justification.
3. **No npm, no build step, no CDN.** Frontend is static HTML + vanilla ES6 JS + CSS served by the same process; assume browsers have no internet access.
4. **Full type annotations** on every function/method (3.6-compatible). No `Any` except at JSON boundaries, converted to typed structures immediately. Use `typing.List`/`Dict`/`Optional` — **never** PEP 585 builtin generics (`list[str]`) or PEP 604 unions (`int | None`), which are a runtime `TypeError` on 3.6 while looking perfectly fine on a modern interpreter.
5. **No global mutable state**; the storage object is injected into handlers.

`tests/test_python36_compat.py` enforces constraints 1 and 4 statically on every test run, so a 3.6 violation fails the suite on whatever interpreter you are holding rather than waiting for the RHEL 8 CI job. It parses every file with `ast.parse(..., feature_version=(3, 6))`, rejects builtin generics *anywhere* (including `cast(list[str], x)` and module-level aliases), whitelists `typing` names against 3.6.0, forces every annotation to evaluate (3.14 defers them under PEP 649, so a green suite is otherwise no evidence), and keeps `run_server.py`/`run_feeder.py` free of inline annotations and f-strings so they still parse under Python 2. It carries planted-regression tests to prove the detectors can actually fail.

## Commands

- Run all tests: `python -m unittest discover`
- Run one test module: `python -m unittest tests.test_storage`
- Run one test: `python -m unittest tests.test_storage.TestClass.test_method`
- Run the server (once implemented): `python run_server.py --port <p> --db <path> --host <h>`

## Architecture (dashboard, per brief)

Planned layout: `testboard/` package with `model.py` (NamedTuples, `Result` enum, ISO time parse/format), `storage.py` (all SQL, sqlite migrations), `analytics.py` (pure functions), `api.py` (framework-free routing/handlers), `server.py` (http.server glue + static files); `static/` for the UI; `tests/` for unittest suites.

Key design decisions mandated by the brief:

- **Handlers are plain testable functions** taking (parsed request, storage) and returning `(status, headers, body)`; the `BaseHTTPRequestHandler` subclass is a thin shell. Unit-test handlers directly; a few end-to-end tests boot a real server on an ephemeral port.
- **Test identity** is the triple `(environment, script, test_name)`; a **run** is keyed by that triple plus `start_time`. Import is an idempotent upsert on that key — use a UNIQUE constraint with `INSERT OR REPLACE` (3.6's sqlite predates `ON CONFLICT DO UPDATE`).
- **Timestamps** are ISO-8601 UTC strings (`YYYY-MM-DDTHH:MM:SS.ffffff`, no timezone suffix) everywhere — storage, transport, and comparisons (lexical ordering works).
- **`result`** is an `enum.Enum`: `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS`. Analytics treat `FAILED_AS_EXPECTED` as non-failure and `UNEXPECTED_PASS` as noteworthy-but-not-failure.
- **`output` can be large**: it lives in its own table (`run_outputs`), zlib-compressed, and is read by exactly one endpoint (`GET /api/runs/{id}`). Never join it into a list query — keeping it out of `runs` is what keeps metadata reads dense.
- **The server serves from a fixed worker pool, never a thread per request.** Storage keeps connections in `threading.local()`, so a thread per request means a connection per request means an empty SQLite page cache on every request — measured: 20 requests, 20 connections, and no `cache_size` setting can help a cache that is discarded before it is used twice. The pool size *is* the connection count and is what a `--cache-mb` budget is divided by; `tests/test_server_pool.py` fails if the mixin comes back.
- SQLite: WAL mode + busy timeout at connect (threaded server), versioned migration table (`schema_version`). `MIGRATIONS` currently holds ONE entry that creates the schema outright — nothing is deployed, so there is no history to preserve. The first schema change after a deployment exists adds entry 2 and never edits entry 1. A database whose version is newer than the code is refused, not used.
- **Scale is the design constraint**: ~12,000 tests a night, kept for a year (~4.4M runs). No endpoint may be proportional to the size of the estate. `latest_runs` (one row per test, carrying its latest and previous result) and `current_assignments` are derived tables maintained inside the writing transaction; estate-wide reads go through them, list endpoints are paginated in SQL, and only the returned page joins `runs`. `ORDER BY` cannot be parameterized — sort keys come from the `DASHBOARD_SORTS` whitelist.
- **A run belongs to a test, not to a batch** — the import contract has no session/batch id. A *suite execution* is therefore inferred from run timings by `analytics.group_executions` (new execution when a run starts more than 60 min after the latest end seen). A suite can run more than once a day, so anything bucketed by calendar day (the home trend) must not be described as "per night".
- **Retirement** (`test_retirements`) is human-entered state marking a test as no longer in the suite: excluded from every estate view, counted only as `status.retired`, history untouched, and cleared automatically if the test reports a run again.
- Analytics are **pure functions over lists of runs** (failing-since/regression window, flakiness score from result transitions, day-of-week profile, duration trend) over a window of last 90 days or last 200 runs, whichever is smaller.
- The `/api/import` transport schema is a **fixed contract shared with the feeder** — keep it stable and document it in the README.
- Frontend security: user-supplied strings (comments, test output) go into the DOM via `textContent`, never `innerHTML`. Static file serving must reject path traversal (`..`) — and be tested for it.

## Feeder (separate deliverable, per its brief)

`feeder.py` CLI + swappable site-specific reader module + generic submitter module (validation, batching default 500, retry ×3 with backoff, failed-batch replay files, high-water-mark state file for daily mode). Never let one bad record abort an import — log, skip, count. Exit code 0 only if all valid records were accepted.
