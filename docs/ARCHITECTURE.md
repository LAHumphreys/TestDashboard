# Architecture — the decisions behind the layout

Moved verbatim from `CLAUDE.md` on 2026-10-03 (process round 1) so that the
agent which acts on them reads them on spawn, and every other reader is not
charged for them on every turn. **The text below is unchanged**; each bullet
was bought with a production incident recorded in `docs/UPGRADE_PLAN_STATUS.md`.
Add to it in the same commit as the change that earns a new bullet; never
shorten one — a shorter rule is how the incident happens again.

## Architecture (dashboard)

Layout as built: `testboard/` package with `model.py` (NamedTuples, `Result` enum, ISO time parse/format), `storage.py` (all SQL, sqlite migrations, the `_SqliteBackend` half of the backend seam), `mariadb.py` (the MariaDB backend — the ONE serving-path module that touches the vendored driver), `dbconfig.py` (mysql option-file parsing, shared with the migration tool), `analytics.py` (pure functions), `api.py` (framework-free routing/handlers), `server.py` (http.server glue + static files), `perf.py` (the optional on-disk timing log), `metrics.py` (in-memory counters for the Metrics page); `static/` for the UI; `tests/` for unittest suites. The SQL in `storage.py` is qmark-canonical permanently; the MariaDB wrapper translates at execute time, and the app NEVER runs DDL on MariaDB (schema comes from the migration tooling; `schema_version` equality is verified, both mismatch directions refuse).

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
  table (`schema_version`). **`MIGRATIONS` holds eleven entries and entry 1 describes a
  database that exists in production — never edit it.** Every schema change is a new
  appended entry whose version is claimed from the registry in `docs/UPGRADE_PLAN.md`
  §1 *in the same commit*; version 11 is WP-40's, and 12 is claimed by WP-15
  (renumbered six times now, as WP-17, WP-18, WP-20, WP-21, WP-23 and WP-40 each
  shipped first — the parked WIP branch must renumber before merging). **The app never runs DDL on MariaDB**: there, the
  schema is moved by `tools/upgrade_mariadb_schema.py` (WP-27; a ledger since
  WP-40 — every migration above 7 needs a step there and a table in the exporter's
  `ddl()`, in the same commit; `LedgerTest` fails the suite otherwise, no server
  needed; runbook §G.5) and the backend only verifies the recorded version
  matches, refusing in both directions.
  `tests/test_migrations.py` freezes entry 1 by hash and asserts the fresh-install and
  incremental paths produce identical schemas. A migration may contain a Python step
  (`"python: <name>"`). A database whose version exceeds the code's is refused, not
  used — so a rollback needs a copy of the file taken beforehand.
- **Scale is the design constraint**: ~12,000 tests a night, kept for a year (~4.4M runs). No endpoint may be proportional to the size of the estate — *or of its history*. Three derived tables are maintained inside the writing transaction: `latest_runs` (one row per test, carrying its latest and previous result), `current_assignments`, and `activity_hours` (run counts per environment × UTC hour × result — what the staleness cutoff and the trend read; migration 6). Estate-wide reads go through them, list endpoints are paginated in SQL, and only the returned page joins `runs`. Nothing may scan a window of `runs` at request time — the bucket query that did was 3.5s mean on production and grew every night. `ORDER BY` cannot be parameterized — sort keys come from the `DASHBOARD_SORTS` whitelist.
- **The cold cost is the cost.** Results are pushed DURING a run, throughout the day —
  mainline's through the morning, builds' into the afternoon — and a push drops the
  memos of what it wrote. So measure with the memos cleared before every call, and
  never quote a warm number as what a page costs. A summary reads a stream's partition
  of `latest_runs` in one pass PER ENVIRONMENT (`Storage._environment_rollup`,
  assembled by `_partition_rollup`) and every scope of it is a filter of those cells;
  a new figure for the headline is a new column on that pass or a sum over its cells,
  not another pass. Compare two code trees in-process and alternated: over HTTP on the
  development machine an unchanged request varies 2-3x between runs.
- **A memo entry names the stream, and the environment, it was computed from**
  (`_store_summary(key, value, stream_id, environment)`), and an import drops only the
  `(stream, environment)` pairs it wrote (WP-38). An entry computed from more than one
  stream may not be memoized there at all; a wrong tag is a stale page. Assignments,
  comments, retirements and deletes still drop everything — they are human-rate. Never
  add cost to the push path to pay for this: the pairs come from the cells the import
  already maintains. `tests/test_storage.py::TargetedInvalidationTest` pairs every
  "still served" with a "still right".
- **Nothing a request does may take a lock to be counted.** `testboard/metrics.py`
  tallies per thread and merges to read; `tests/test_metrics.py::NoLockOnTheRequestPathTest`
  replaces the lock with one that counts. A counter every worker queues for is a new
  way for requests to wait on each other, introduced by the thing meant to find those.
  The Metrics page's database figures are read when the page asks and never scan
  `runs` — its count is `SUM(activity_hours.count)`.
- **A comment belongs to a test and records the stream it was posted FROM.** Every
  way of commenting from a build's page must send that stream — the Review panel and
  the bulk-assign note did not until WP-35. A build's "Difference from" list shows the
  newest comment posted from that build and no other.
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
