# Project Brief: Test Results Dashboard ("testboard")

## Purpose

A self-contained, open-sourceable web dashboard showing the current state of overnight
unit/regression test runs, with drill-down into per-test history to answer questions like:

- When did this test start failing?
- Is it flaky (intermittent pass/fail)?
- Does it fail on a pattern (e.g. always Mondays)?

Data is pushed in by an external feeder script (written separately, see the companion
feeder brief). This project owns storage, the HTTP API, analytics, and the web UI.

## Hard Constraints — read carefully

1. **Python 3.6 only.** Target CPython 3.6 exactly. Do not use any feature introduced
   in 3.7+. Known traps to avoid:
   - `dataclasses` (3.7) — use `typing.NamedTuple` with class syntax instead.
   - `http.server.ThreadingHTTPServer` (3.7) — compose it yourself:
     `class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer)`.
   - `datetime.fromisoformat` (3.7) — write and unit-test a small ISO-8601 parser
     (`strptime` based) in one place; use it everywhere.
   - `subprocess.run(capture_output=...)` (3.7).
   - `typing.Protocol`, `typing.Literal`, positional-only params, walrus operator.
   - `breakpoint()`, `contextlib.nullcontext`.
   - f-strings, variable annotations, `async`/`await` are fine (all 3.6).
2. **Standard library only.** No pip installs. No pytest — use `unittest`. Storage is
   `sqlite3`. HTTP server is `http.server`. JSON is `json`.
3. **No npm / no build step / no CDN.** The frontend is static files (HTML + vanilla
   JS + CSS) served by the same process. Assume the browser has **no internet access**:
   no external fonts, no CDN scripts, no source maps fetched remotely. ES6 vanilla JS
   is acceptable (evergreen internal browsers).
4. **No authentication.** Users self-identify by typing/selecting a username, stored in
   `localStorage` client-side and sent with mutating requests. Users are created
   implicitly on first use and thereafter appear in dropdowns.
5. All Python code must be **fully type-annotated** (3.6-compatible annotations) and
   covered by a **comprehensive unit test suite** runnable with
   `python -m unittest discover`.

## Domain Model

A **test** is uniquely identified by the triple:

- `environment` — the environment that ran the test (e.g. `linux-prod-sim`, `win-uat`)
- `script` — the test script/suite the test belongs to
- `test_name` — the individual test name

A **run** is one execution of a test and carries:

| Field                  | Type            | Notes                                            |
|------------------------|-----------------|--------------------------------------------------|
| environment            | str             | part of identity                                 |
| script                 | str             | part of identity                                 |
| test_name              | str             | part of identity                                 |
| result                 | enum            | `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS` |
| start_time             | UTC datetime    | ISO-8601 in transport                            |
| end_time               | UTC datetime    | duration is derived (`end - start`), not stored  |
| output                 | str             | full captured output for the run                 |
| source_link            | str (URL)       | weblink to the test's source code                |
| known_failure_reason   | str or null     | why a failure is "known"/expected, if applicable |

Use an `enum.Enum` for `result`. `FAILED_AS_EXPECTED` and `UNEXPECTED_PASS` exist
because tests can carry a known-failure annotation: the interesting states for triage
are `FAIL` (new breakage) and `UNEXPECTED_PASS` (a known failure that now passes —
the annotation is probably stale).

**Idempotency:** a run is uniquely keyed by `(environment, script, test_name,
start_time)`. Re-importing the same run must upsert, not duplicate. Enforce with a
UNIQUE constraint and `INSERT OR REPLACE` (or explicit upsert logic — 3.6's bundled
sqlite predates `ON CONFLICT DO UPDATE`, so do not rely on it).

Additional entities:

- **User**: just a unique username string + created_at. Created implicitly the first
  time a username is used for a comment/assignment, or explicitly via the API.
- **Comment**: attached to a *test* (the triple, not a single run): author (user),
  UTC timestamp, free text.
- **Assignment**: each test has at most one current assignee (nullable). Keep an
  assignment history table (test, assignee, assigned_by, timestamp) so reassignment
  is auditable; "current assignee" is the latest row.

## Storage

Single SQLite file (path configurable via CLI arg, default `testboard.db`).

- Enable WAL mode and a busy timeout at connection time — the server is threaded and
  imports can be large.
- Schema created/migrated on startup via a simple versioned migration table
  (`schema_version`), even if v1 is the only version — this is an open-source project
  and the schema will evolve.
- Index `(environment, script, test_name, start_time)` for history queries, and an
  index supporting "latest run per test" for the dashboard query.
- Store timestamps as ISO-8601 UTC strings (`YYYY-MM-DDTHH:MM:SS.ffffff`); comparisons
  then work lexically. All server logic treats times as UTC.
- `output` can be large; never fetch it in list queries — only in the single-run
  detail endpoint.

## HTTP API

JSON over HTTP, served by `http.server` with a threaded server class. Structure the
code so request routing/handlers are plain testable functions/classes that take a
parsed request and a storage object, and return `(status, headers, body)` — the
`BaseHTTPRequestHandler` subclass is a thin shell. Unit-test handlers directly; add a
small number of end-to-end tests that boot the real server on an ephemeral port and
exercise it via `http.client`.

Endpoints (prefix `/api`):

- `POST /api/import` — bulk upsert of runs. Body: `{"runs": [RunRecord, ...]}` using
  the transport schema below. Response: counts of inserted/updated/rejected with
  per-record error messages for rejects (do not fail the whole batch on one bad
  record). This is the contract the feeder script targets — keep it stable and
  document it in the README.
- `GET /api/dashboard` — latest run per test, minus `output`. Query params:
  `environment`, `script`, `result` (repeatable), free-text `q` against test name.
- `GET /api/tests/{environment}/{script}/{test_name}` — test detail: current state,
  current assignee, analytics summary (below).
- `GET /api/tests/{environment}/{script}/{test_name}/history?limit=&before=` — runs,
  newest first, without `output`.
- `GET /api/runs/{run_id}` — single run including full `output`.
- `GET /api/tests/{...}/comments` / `POST` — post body `{"username": ..., "text": ...}`.
- `PUT /api/tests/{...}/assignee` — body `{"username": assignee-or-null, "assigned_by": ...}`.
- `GET /api/users` / `POST /api/users` — list / explicit create (also created
  implicitly by comment/assign).
- Path segments are URL-encoded; decode carefully (test names may contain `/`, spaces,
  brackets — consider requiring encoded segments and unit-test the edge cases).

Validation: reject unknown `result` values, missing identity fields, unparseable
timestamps, `end_time < start_time`. Return 400 with a JSON error body; 404 for
unknown tests/runs; 405 for wrong methods. Never return HTML from `/api/*`.

### Transport schema for a RunRecord (shared with the feeder)

```json
{
  "environment": "linux-prod-sim",
  "script": "regression/user_lifecycle.py",
  "test_name": "test_partial_update_retry",
  "result": "FAIL",
  "start_time": "2026-07-25T02:14:07.123456",
  "end_time": "2026-07-25T02:14:09.001000",
  "output": "…full captured output…",
  "source_link": "https://git.example.com/tests/user_lifecycle.py#L120",
  "known_failure_reason": null
}
```

All times UTC, ISO-8601, no timezone suffix. `known_failure_reason` null unless the
test is annotated as a known failure. `output` may be empty but must be present.

## Analytics (server-side, unit-tested pure functions)

Computed over a configurable window (default: last 90 days or last 200 runs,
whichever is smaller) and returned in the test-detail endpoint:

- **First failing run** of the current failure streak ("failing since"), and the last
  passing run before it — the regression window.
- **Flakiness score**: number of result transitions (pass↔fail) divided by runs in
  window; expose the raw transition count too. Classify: stable-pass / stable-fail /
  flaky (threshold configurable, pick a sensible default and document it).
- **Day-of-week profile**: per-weekday run count and failure rate, so "always fails
  on Mondays" is visible. Treat `FAILED_AS_EXPECTED` as non-failure and
  `UNEXPECTED_PASS` as noteworthy-but-not-failure for these stats; document this
  choice in the README.
- **Duration trend**: min/median/max duration in window (median helps spot
  timeouts/hangs).

Keep analytics as pure functions over lists of runs — trivially unit-testable with
synthetic histories (steady fail, alternating, Monday-only failure, etc.).

## Web UI

Static files in `static/`, served at `/`. Vanilla JS (ES6 modules are fine), no
framework, no build. Keep JS modest and readable; it is not unit-tested (Python is),
so keep logic server-side where possible.

Pages/views:

1. **Dashboard** (`/`): table of tests with latest result, colour-coded
   (PASS green, FAIL red, FAILED_AS_EXPECTED amber, UNEXPECTED_PASS blue/violet),
   last-run time, duration, assignee. Filters for environment, script, result;
   text search. Summary counts across the top. Sort by clicking columns.
2. **Test detail**: identity + source-code link; current assignee with dropdown of
   known users plus an inline "new user…" option (prompts for a name, creates via
   API, assigns); analytics summary (failing since, flakiness classification,
   day-of-week bar mini-chart — render with plain DOM/SVG, no chart library);
   history list, newest first, each row expandable/linking to full run output;
   comments thread with add-comment box.
3. **Username handling**: first mutating action prompts for username, persisted in
   `localStorage`, shown in the header with a "change" affordance. Sent in request
   bodies as specified above.

Escape all user-supplied strings when inserting into the DOM (`textContent`, never
`innerHTML` with raw data) — comments and test output are untrusted.

## Project Layout

```
testboard/
├── README.md                  # usage, API contract, analytics definitions, screenshots
├── LICENSE                    # MIT
├── run_server.py              # entry point: args for --port --db --host
├── testboard/
│   ├── __init__.py
│   ├── model.py               # NamedTuples, Result enum, ISO time parse/format
│   ├── storage.py             # sqlite layer, migrations, all SQL lives here
│   ├── analytics.py           # pure functions
│   ├── api.py                 # routing + handlers (framework-free, testable)
│   └── server.py              # http.server glue, static file serving
├── static/
│   ├── index.html
│   ├── test.html
│   ├── app.js / test.js / api.js / style.css
└── tests/
    ├── test_model.py
    ├── test_storage.py        # against sqlite :memory: / tmp file
    ├── test_analytics.py
    ├── test_api.py            # handlers called directly with fake requests
    └── test_e2e.py            # real server on ephemeral port via http.client
```

## Quality Bar

- Every function/method type-annotated; no `Any` except where genuinely unavoidable
  (JSON boundaries), and convert to typed structures immediately at the boundary.
- Docstrings on public functions; module docstrings stating responsibility.
- No global mutable state; storage object injected into handlers.
- `python -m unittest discover` passes; aim for meaningful coverage of storage,
  analytics, API validation, URL decoding, idempotent import, and the implicit
  user-creation paths. Include at least one large-batch import test (e.g. 5k runs)
  to catch accidental O(n²) behaviour.
- Serving static files must not allow path traversal (`..`) — test it.
- README documents: quick start, the `/api/import` contract (verbatim schema above),
  analytics definitions, and the Python 3.6 constraint with rationale.

## Non-Goals (v1)

- Authentication/authorisation, HTTPS (run behind a reverse proxy if needed).
- Editing/deleting comments; deleting runs; retention/pruning (note as future work).
- Real-time updates/websockets — plain page refresh is fine.
- Charting libraries — hand-rolled SVG only where needed.
