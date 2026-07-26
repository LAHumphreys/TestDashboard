# testboard

A self-contained, open-source dashboard for overnight unit/regression test results.
It answers the questions triage engineers actually ask:

- **When did this test start failing?** (regression window: failing-since + last pass before it)
- **Is it flaky?** (transition-based flakiness score and classification)
- **Does it fail on a pattern?** (day-of-week failure profile — "always fails on Mondays")
- **Is it getting slower?** (min/median/max duration over the window)

testboard owns storage (SQLite), the HTTP API, analytics, and the web UI. Results are
pushed in by a small feeder script (also in this repo) from whatever test system you
run on site.

**Requires only Python 3.6+. No pip, no npm, no build step, no CDN, no internet access
at runtime.** Everything is Python standard library plus static HTML/JS/CSS served by
the same process.

![The testboard home screen: last-night KPIs, trend and environment charts, and the triage queues](docs/screenshot-dashboard.png)
*The home screen on a simulated 12,000-test estate — run the quick start below to get this locally.*

---

## Quick start

From a clean clone, one command gives you a browsable dashboard populated with 45 days
of simulated history (steady passers, a regression, a flaky test, a Monday-failer, a
known failure, a slowing test) plus real results from this repo's own test suite:

```
git clone <this repo>
cd TestDashboard
python tools/demo_bootstrap.py
```

Then open <http://127.0.0.1:8000>.

`demo_bootstrap.py` options: `--db` (default `testboard.db`), `--port` (default `8000`),
`--host`, `--days`, `--seed`, `--skip-self-tests`, `--no-serve`, and
`--scale-tests N` — seed N additional filler tests (mostly green, with a
realistic sprinkle of regressions, flaky tests, stale annotations and tests
gone silent) to preview the dashboard at production scale:

```
python tools/demo_bootstrap.py --scale-tests 12000
```

On RHEL/most Linux distributions the platform Python is invoked as `python3`:

```
python3 tools/demo_bootstrap.py
```

## Running the real server

```
python run_server.py [--host 127.0.0.1] [--port 8000] [--db testboard.db] [--static <repo>/static]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to serve your team. |
| `--port` | `8000` | TCP port. |
| `--db` | `testboard.db` | SQLite database file. Created and migrated automatically on startup. |
| `--static` | `static/` next to `run_server.py` | Directory of frontend files. |

The server prints its URL on startup and shuts down cleanly on Ctrl+C. The database
schema is versioned (`schema_version` table) and migrated automatically, so upgrading
testboard is just replacing the code and restarting.

---

## HTTP API reference

All endpoints live under `/api` and speak JSON (`application/json; charset=utf-8`).
Errors are always JSON of the shape `{"error": "message"}` — never HTML:

- `400` — validation failure (message names the offending field/value)
- `404` — unknown test/run/route
- `405` — wrong method (response carries an `Allow` header listing valid methods)

**URL encoding:** the test identity is the triple `(environment, script, test_name)`
and each appears as its own path segment. Segments must be percent-encoded by the
client; the server decodes each segment individually, so test names containing `/`
must be sent as `%2F`. `+` is **not** decoded to a space — use `%20`.

**Timestamps:** all times are UTC, ISO-8601, format `YYYY-MM-DDTHH:MM:SS.ffffff`,
with **no timezone suffix** (no `Z`, no `+00:00`). Values with a timezone suffix are
rejected.

### POST /api/import — bulk upsert of runs (the feeder contract)

Request body:

```json
{"runs": [RunRecord, ...]}
```

#### Transport schema for a RunRecord (shared with the feeder)

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

Validation rules per record:

- `environment`, `script`, `test_name`: required strings, non-empty after stripping.
- `result`: required; one of `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS`.
- `start_time`, `end_time`: required, parseable ISO-8601 as above; `end_time` must be
  `>= start_time`.
- `output`: required key, must be a string (may be `""`).
- `source_link`: optional string, defaults to `""`.
- `known_failure_reason`: optional, string or null, defaults to null.
- Unknown extra keys are ignored (forward compatibility).

**Idempotency:** a run is uniquely keyed by
`(environment, script, test_name, start_time)`. Re-importing the same run updates it
in place — re-running an import is always safe and never duplicates data.

Response — `200` even when some records are rejected; one bad record never aborts the
batch (valid records are still upserted):

```json
{
  "inserted": 40,
  "updated": 2,
  "rejected": 1,
  "errors": [
    {
      "index": 17,
      "error": "result: unknown value 'BROKE'",
      "environment": "linux-prod-sim",
      "script": "regression/foo.py",
      "test_name": "test_bar",
      "start_time": "2026-07-25T02:14:07.123456"
    }
  ]
}
```

Each error object carries the record's identity fields when they could be extracted
from the raw dict (null when absent), so you can grep your source data for the exact
offending run. `400` is returned only when the envelope itself is malformed (invalid
JSON, or `runs` missing / not a list).

### GET /api/dashboard — latest run per test (paginated)

**This endpoint returns one page, never the whole estate.** With 12,000 tests a
night, serializing every row was a 4.6 MB response and several hundred
milliseconds of work; filtering, searching, sorting and paging are therefore all
done in SQL, and the response carries the exact total for the filters so a
caller knows what its page is a slice of.

Query parameters (all optional):

| Parameter | Meaning |
|---|---|
| `environment` | exact match |
| `script` | exact match |
| `result` | repeatable; invalid value → 400 |
| `q` | case-insensitive (ASCII) substring match on test name; `%`, `_` and `\` are literal |
| `stale` | `1`/`true` — only tests whose latest run is older than the 36 h recency window ("not run") |
| `retired` | `1`/`true` — include tests retired as no longer in the suite (hidden by default) |
| `assignee` | repeatable — only tests owned by these people |
| `unassigned` | `1`/`true` — include tests nobody owns (ORed with `assignee`) |
| `with_comment` | `1`/`true` — add `latest_comment` to each row (one index seek per returned row, so it is opt-in) |
| `sort` | one of `environment` (default), `script`, `test_name`, `result`, `start_time`, `duration`, `assignee`; anything else → 400 |
| `order` | `asc` (default) or `desc`; anything else → 400 |
| `limit` | 1..1000, default 250 |
| `offset` | ≥ 0, default 0 |

Response:

```json
{
  "tests": [
    {
      "environment": "...", "script": "...", "test_name": "...",
      "run_id": 7, "result": "FAIL",
      "start_time": "...", "end_time": "...", "duration_seconds": 1.234,
      "known_failure_reason": null, "source_link": "...", "assignee": null,
      "retired_at": null, "retired_by": null
    }
  ],
  "total": 12376, "limit": 250, "offset": 0
}
```

With `with_comment=1` each row also carries
`"latest_comment": {"author": "...", "created_at": "...", "text": "..."}`
(absent when the test has no comments).

`total` counts every test matching the filters, not the rows returned. Every
ordering ends with the full test identity, so paging with `limit`/`offset` can
neither repeat nor skip a row.

`output` is never included in list responses (it can be large — see
[Scale](#scale-and-retention)).

### GET /api/summary — the home-screen estate rollup

Everything the triage home screen needs in one request. Query parameters (all
optional): `environment` (exact match; scopes every number below), `days`
(integer 1..90, default 14; the trend window — invalid values → 400), `assignee`
(adds the `mine` queue for that user).

None of this is proportional to the size of the estate: the headline counts come
from a single `GROUP BY` (a few dozen rows however many tests exist), and each
queue is its own indexed query.

```json
{
  "generated_at": "2026-07-26T06:30:00.000000",
  "environment": null,
  "environments": ["linux-prod-sim", "linux-uat-sim"],
  "scripts": ["regression/user_lifecycle.py", "smoke/login.py"],
  "assignees": ["alice", "luke"],
  "recent_hours": 36,
  "queue_cap": 500,
  "status": {
    "total_tests": 12000, "ran_recently": 11840, "not_run": 160, "retired": 34,
    "results":        {"PASS": 11400, "FAIL": 420, "FAILED_AS_EXPECTED": 130, "UNEXPECTED_PASS": 50},
    "recent_results": {"PASS": 11280, "FAIL": 410, "FAILED_AS_EXPECTED": 110, "UNEXPECTED_PASS": 40},
    "new_failures": 35, "still_failing": 385, "fixed": 28, "assigned_open": 210
  },
  "trend": {"days": 14, "from": "2026-07-13", "to": "2026-07-26",
            "nights": [{"date": "2026-07-13", "PASS": 11500, "FAIL": 300,
                        "FAILED_AS_EXPECTED": 120, "UNEXPECTED_PASS": 30, "total": 11950}]},
  "by_environment": [{"environment": "linux-prod-sim", "total_tests": 8000, "failed": 300,
                      "new_failures": 25, "unexpected_passes": 35, "not_run": 90}],
  "top_failing_scripts": [{"environment": "linux-prod-sim",
                           "script": "regression/user_lifecycle.py", "failing": 40}],
  "queues": {
    "new_failures":      {"total": 35,  "tests": [QueueEntry, ...]},
    "still_failing":     {"total": 385, "tests": [...]},
    "unexpected_passes": {"total": 50,  "tests": [...]},
    "fixed":             {"total": 28,  "tests": [...]},
    "not_run":           {"total": 160, "tests": [...]},
    "assigned":          {"total": 210, "tests": [...]},
    "mine":              {"total": 4,   "tests": [...]}
  }
}
```

Definitions (matching the analytics semantics above):

- A test **ran recently** when its latest run started within `recent_hours`
  (36) of now — wide enough that a test misses a whole nightly batch before it
  counts as `not_run`, without flapping on the batch's own start hour.
- `results` counts every test by its latest run (the current state of the
  estate); `recent_results` counts only recently-run tests (the "last night"
  view).
- **New failure**: latest FAIL, previous run not FAIL (or first ever run).
  **Still failing**: latest and previous both FAIL. **Fixed**: previous FAIL,
  latest not. **Not run**: has not reported inside the recency window — this is
  the queue where a disappeared test is [retired](#put-apitestsenvscripttestretired--approve-a-disappeared-test).
  **Assigned** (open actions): has an assignee and the latest result is FAIL or
  UNEXPECTED_PASS. A test may appear in several queues.
- **Retired tests are in none of them**, and are excluded from every count
  except `status.retired`.
- `mine` is `assigned` narrowed to the `assignee` parameter, filtered in SQL —
  not a client-side filter of `assigned`, which would hide a user's own tests
  behind other people's once the estate has more open items than the cap.
  Without an `assignee` parameter it is `{"total": 0, "tests": []}`.
- `trend.nights` is zero-filled: every UTC calendar day in the window appears
  even when nothing ran. Its cost tracks the *width of the window*, not the
  size of the history behind it.
- `scripts` lists the distinct scripts in scope — it feeds the test-list filter,
  which can no longer derive them from a downloaded estate.
- Each queue reports its exact `total` but carries at most `queue_cap` (500)
  entries, selected by test identity; narrow with `environment` to see past it.
  A QueueEntry is a dashboard Row (minus `end_time`/`source_link`) plus
  `prev_result`, `failing_since` (start of the current FAIL streak) and
  `last_pass_time` (most recent PASS before that streak). The streak fields are
  populated only for the queues that report them — `still_failing` and `mine` —
  and are null elsewhere, including for every entry whose latest run is not a
  FAIL. Within the slice it returns, `still_failing` is ordered
  oldest-regression-first.

### GET /api/scripts/{environment}/{script}/executions — suite history

The estate is keyed on individual tests, but people run and reason about whole
scripts. This returns the recent **executions** of one suite, newest first.

Query parameters: `days` (1..90, default 14).

```json
{
  "environment": "linux-prod-sim", "script": "regression/user_lifecycle.py",
  "days": 14, "gap_minutes": 60, "run_count": 450, "truncated": false,
  "executions": [
    {"started": "2026-07-26T14:30:00.000000",
     "ended": "2026-07-26T14:47:56.000000",
     "duration_seconds": 1076.0, "total": 30, "failed": 4,
     "results": {"PASS": 26, "FAIL": 4,
                 "FAILED_AS_EXPECTED": 0, "UNEXPECTED_PASS": 0}}
  ]
}
```

**Executions are inferred from timing**, because a run record carries no batch
identifier — the import contract is a flat list of runs. A new execution starts
when a run begins more than `gap_minutes` (60) after the latest END seen so far.
Measuring from the last *end* rather than the last *start* means one slow test
cannot split its own execution in two.

This is what the home screen's daily trend cannot show: a suite that runs twice
in a day is one bar there and two executions here.

### GET /api/tests/{environment}/{script}/{test_name} — test detail

`404` if the triple has no runs. Response:

```json
{
  "environment": "...", "script": "...", "test_name": "...",
  "source_link": "...",
  "assignee": "alice",
  "latest": {"run_id": 7, "result": "FAIL", "start_time": "...", "end_time": "...",
             "duration_seconds": 1.234, "known_failure_reason": null, "source_link": "..."},
  "analytics": { ...see Analytics below... }
}
```

### GET /api/tests/{environment}/{script}/{test_name}/history

Query: `limit` (integer 1..500, default 50), `before` (ISO timestamp; returns runs
with `start_time < before` — pass the oldest loaded `start_time` to page). Bad values
→ 400; unknown triple → 404. Response: `{"runs": [RunOut, ...]}` newest first, where
RunOut is the same shape as `latest` above (no `output`).

### GET /api/runs/{run_id}

The only endpoint that returns `output`. Non-integer or unknown id → 404. Response:
RunOut plus `{"environment", "script", "test_name", "output"}`.

### Known-failure reason

`known_failure_reason` is optional on every run and is never required — a run
without one imports exactly as before. When a run does carry one it is
**surfaced as a banner on the test page**, directly under the latest result, and
in the `Known failure reason` column of the run history. Whitespace-only values
are normalised to `null`.

It is the annotation that decides whether a failure needs looking at, so it is
worth sending: typically alongside `FAILED_AS_EXPECTED`, with a ticket id or a
sentence.

### Comments

Comments are per TEST (the triple), not per run, and can be added to any
test whatever its latest result — recording *why* a test only passes is as
useful as recording why it fails. The full thread is on the test page; the
newest comment is shown in the triage queues and the open-actions view.

- `GET /api/tests/{env}/{script}/{test}/comments` — 404 unknown triple. Response:
  `{"comments": [{"id": 1, "author": "alice", "created_at": "...", "text": "..."}]}`
  oldest first.
- `POST` same path — body `{"username": "...", "text": "..."}`. `username` non-empty,
  max 100 chars (stripped); `text` non-empty, max 10000 chars. The user is created
  implicitly if unknown. Returns `201` with `{"comment": {...}}`.

### PUT /api/tests/{env}/{script}/{test}/assignee

Body `{"username": "bob-or-null", "assigned_by": "alice"}`. The `"username"` key is
**required**; pass `null` to clear the assignment. `assigned_by` must be a non-empty
string. Both users are created implicitly if unknown. Assignment history is kept for
audit; the current assignee is the most recent entry. Returns `200`
`{"assignee": "bob"}` (or null).

### PUT /api/tests/{env}/{script}/{test}/retired — approve a disappeared test

A test that stops being reported shows up as "not run" forever. Retiring it is a
human approving that absence, so it takes a username **and** a reason:

```json
{"retired": true, "username": "luke", "comment": "Deleted in release 4.2."}
```

400 if `comment` is missing or blank, or `retired` is not a boolean.
Response: `{"retired": true, "retired_by": "luke", "comment": {...}}`.

- The reason is appended to the test's normal comment thread, so the next person
  to look finds it where they already look.
- A retired test disappears from the **estate views** — the headline counts, the
  triage queues, the default test list — and is counted in `status.retired`.
- Its **history is untouched**: the detail page, run history and comments all
  work exactly as before. Retirement means "not in the suite any more", not
  "never happened".
- If the test reports a run again it is **un-retired automatically**, with a
  comment recording why. Silently missing data is worse than an unexpected row.
- Send `{"retired": false, ...}` to put it back by hand.

### Users

- `GET /api/users` — `{"users": [{"username": "...", "created_at": "..."}]}` sorted by
  username.
- `POST /api/users` — body `{"username": "..."}` (non-empty, max 100, stripped).
  New user → `201`, existing → `200`; both return
  `{"user": {"username": "...", "created_at": "..."}, "created": <bool>}` where
  `created` is `true` only when the user was just created.

There is **no authentication** (see Non-goals): users self-identify by username,
stored client-side in `localStorage`.

---

## Analytics definitions

Returned by the test-detail endpoint, computed server-side by pure, unit-tested
functions. Exact semantics:

- **Failure** means `result == FAIL` only. `FAILED_AS_EXPECTED` counts as non-failure
  (the failure is annotated and expected). `UNEXPECTED_PASS` is also non-failure but
  is tracked separately — it usually means a known-failure annotation is stale.
- **Window**: runs with `start_time >= now - max_days`, then the newest `max_runs` of
  those. Defaults: `max_days=90`, `max_runs=200` (i.e. last 90 days or last 200 runs,
  whichever is smaller).
- **Failure streak / regression window**: `failing_since` is the oldest run of the
  consecutive streak of `FAIL` runs ending at the newest run (null if the latest run
  in the window is not `FAIL`). `last_pass_before_failure` is the most recent run
  older than that streak with `result == PASS` (null if none in the window). Together
  they bracket the commit range that introduced a regression.
- **Flakiness**: each run's state is "failing" if `FAIL`, else "passing".
  `transitions` = number of adjacent (in time order) state changes in the window.
  `score = transitions / run_count` (0.0 when fewer than 2 runs). Classification:
  - `no-data` — empty window
  - `flaky` — score >= threshold (default **0.2**)
  - `stable-fail` — not flaky and newest run is `FAIL`
  - `stable-pass` — otherwise
- **By day** (`by_day`): per calendar day within the window, the count of each
  result. Zero-filled between the first and last day with a run so gaps show,
  but not padded to the full 90 days. This is the "results over time" chart on
  the test page.
- **Day-of-week profile**: always 7 entries, Monday first (`Mon`..`Sun`); per day:
  `runs`, `failures` (`FAIL` only), `failure_rate = failures / runs` (0.0 when no
  runs), and `unexpected_passes`.
- **Duration**: min / median / max of `(end_time - start_time)` in seconds over all
  runs in the window (`statistics.median` — the median makes hangs/timeouts stand out
  against a stable baseline); null when the window is empty.

JSON shape (score and failure_rate rounded to 4 decimal places, durations to 3):

```json
{
  "window": {"max_days": 90, "max_runs": 200, "run_count": 37,
             "from": "2026-04-27T00:00:00.000000", "to": "2026-07-26T00:00:00.000000"},
  "failing_since": {"run_id": 7, "result": "FAIL", "start_time": "..."},
  "last_pass_before_failure": null,
  "flakiness": {"transitions": 4, "score": 0.1081, "classification": "flaky"},
  "day_of_week": [{"day": "Mon", "runs": 5, "failures": 2, "failure_rate": 0.4, "unexpected_passes": 0}],
  "duration_seconds": {"min": 1.2, "median": 3.4, "max": 9.9}
}
```

---

## Feeding in your own results

The feeder framework ships in this repo (`feeder/` + `run_feeder.py`). The only
site-specific piece is a small **reader** module that yields run dicts in the
transport schema above — see **[docs/FEEDER_BRIEF.md](docs/FEEDER_BRIEF.md)** for a
complete brief on writing one (aimed at an AI assistant, usable by anyone).

Out of the box, a JSON-lines reader is included (`--reader jsonl`): one transport
JSON object per line, blank lines skipped, malformed lines logged and skipped.

### One-off backfill (import everything, or everything since a date)

```
python run_feeder.py --url http://127.0.0.1:8000 --mode backfill \
    --reader jsonl --source results/*.jsonl --since 2026-01-01T00:00:00
```

### Daily incremental import

```
python run_feeder.py --url http://127.0.0.1:8000 --mode daily \
    --reader jsonl --source results/*.jsonl --state-file feeder_state.json
```

Daily mode keeps a **high-water mark** (max accepted `start_time`) in the state file
(default `feeder_state.json`) and on the next run imports everything newer than
`hwm - overlap_days` (default `--overlap-days 1`). The overlap plus server-side
upserting means re-imports are always safe and gaps from clock skew are covered. A
successful backfill also primes the state file, so `backfill` then `daily` is the
standard sequence. With no state file yet, daily mode imports everything.

All flags: `--url` (required), `--mode backfill|daily` (required), `--since ISO`
(backfill lower bound), `--reader jsonl|module:factory` (default `jsonl`), `--source`
(repeatable; file paths and/or globs), `--batch-size` (default 500), `--state-file`
(default `feeder_state.json`), `--replay-dir` (default `.`), `--overlap-days`
(default 1), `--dry-run`, `--verbose`.

### Reliability behaviour

- Records are validated locally first; invalid records are logged (with the record's
  identity and offending value) and skipped — one bad record never aborts an import.
- Valid records are POSTed in batches (default 500). Server errors (HTTP 5xx) and
  connection failures are retried up to 3 times with exponential backoff; HTTP 400
  (malformed envelope) is not retried.
- A batch that ultimately fails is written to a **replay file**
  `testboard_failed_batch_NNNN.json` in `--replay-dir`, containing the exact
  `{"runs": [...]}` body — you can re-send it later with `curl` or a jsonl import
  once the server is back.
- `--dry-run` validates and counts everything but sends nothing — always do this
  first when developing a reader.
- The final log line summarizes read / valid / skipped / sent / inserted / updated /
  rejected / failed_batches counts, plus a breakdown of skip/reject reasons with an
  example record identity for each — enough to find and fix bad source data without
  guesswork.

**Exit codes:** `0` — all valid records accepted (no rejects, no failed batches);
`1` — some records rejected or batches failed; `2` — fatal error (bad arguments,
reader failed to load).

### Scheduling the daily import

RHEL 8 cron (crontab -e), run daily at 06:30 after the overnight runs finish:

```cron
30 6 * * * cd /opt/testboard && /usr/bin/python3 run_feeder.py --url http://dashboard-host:8000 --mode daily --reader internal_reader:create_reader >> /var/log/testboard-feeder.log 2>&1
```

Windows Task Scheduler (`schtasks`, from an elevated prompt):

```bat
schtasks /Create /TN "testboard-daily-feed" /SC DAILY /ST 06:30 ^
  /TR "cmd /c cd /d C:\opt\testboard && python run_feeder.py --url http://dashboard-host:8000 --mode daily --reader internal_reader:create_reader >> C:\opt\testboard\feeder.log 2>&1"
```

Non-zero exit codes surface in cron mail / Task Scheduler history, so a silently
broken feed is visible.

---

## Deployment

### RHEL 8 (the reference target)

RHEL 8's platform Python is **3.6.8**, invoked as `python3` — testboard targets it
exactly, with zero dependencies to install:

```
sudo dnf install python3        # if not already present
sudo git clone <this repo> /opt/testboard
cd /opt/testboard && python3 -m unittest discover   # sanity check
python3 run_server.py --host 0.0.0.0 --port 8000 --db /var/lib/testboard/testboard.db
```

Note: on RHEL 8 plain `python` may be Python 2.7 or missing entirely — always use
`python3`. testboard's entry scripts detect this and print exactly what to type
instead of dying with a bare SyntaxError.

Systemd unit (`/etc/systemd/system/testboard.service`):

```ini
[Unit]
Description=testboard test-results dashboard
After=network.target

[Service]
Type=simple
User=testboard
WorkingDirectory=/opt/testboard
ExecStart=/usr/bin/python3 /opt/testboard/run_server.py --host 127.0.0.1 --port 8000 --db /var/lib/testboard/testboard.db
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now testboard
```

**Schema changes.** Migrations run automatically on startup inside one
transaction — if one fails the database is left untouched and the server exits
with the reason. A database written by a *newer* testboard than the running code
is refused rather than used, so a half-rolled-back deployment cannot quietly
corrupt it.

**Reverse proxy:** testboard serves plain HTTP with no authentication or TLS. If it
must be reachable beyond a trusted network, put it behind a reverse proxy (nginx,
Apache httpd) that terminates HTTPS and applies whatever access control your site
needs; bind testboard to `127.0.0.1` and proxy to it.

### Anywhere else

There is nothing RHEL-specific in the code: testboard runs anywhere Python 3.6+
exists — newer Linux, macOS, Windows (development happens on Windows). The CI matrix
exercises 3.8 and 3.14 alongside the authoritative 3.6.8 container job.

---

## Scale and retention

The design target is **~12,000 tests a night kept for at least a year** — about
4.4 million runs. Two things follow from that, and both are built in rather than
assumed.

### Nothing scales with the size of the estate

Every endpoint is either constant-work or proportional to the page it returns:

- `latest_runs` holds one row per test — its newest run, that run's result, and
  the previous run's result — maintained inside the same transaction as the
  import. The home screen's headline numbers are a single `GROUP BY` over that
  table (a few dozen rows, whatever the estate size); the triage queues are
  indexed lookups capped at 500 entries with exact totals alongside.
- `/api/dashboard` is paginated, filtered, searched and sorted **in SQL**. The
  count query touches only `latest_runs`; only the returned page joins `runs`.
- `current_assignments` holds the current owner per test, so "who owns this"
  never re-derives itself from the append-only `assignments` log.
- The nightly trend is an index range scan whose cost tracks the *width of the
  window* (14 days by default), not the history behind it.

Measured on a generated year — **12,000 tests × 365 nights = 4.38 million
runs, a 2.3 GB database** — with realistic harness output:

| Request | Time |
|---|---|
| `GET /api/dashboard` — a 250-row page (105 KB) | **3 ms** |
| …open actions, with each test's latest comment | 1 ms |
| …substring search across the estate | 3 ms |
| …the last page (`offset=11750`) | 15 ms |
| `GET /api/scripts/…/executions` — a suite's history | 1 ms |
| …over 90 days | 5 ms |
| `GET /api/tests/…` — detail + 90-day analytics | 3 ms |
| `GET /api/runs/{id}` — one run with full output | 0.1 ms |
| `GET /api/summary` — the whole home screen | **22 ms** |
| …with a 90-day trend window | 22 ms |

The summary figures are the steady state. The **first** request after each
nightly import pays for the trend scan itself — 89 ms for the default 14-day
window, 493 ms for 90 days — and is then memoized until the next import (see
`Storage.daily_result_counts`). Everything else in the summary is a few
milliseconds regardless of history: the counts come from a `GROUP BY` over one
row per test, not from the 4.38M runs behind them, and the per-row extras
(failure streaks, latest comments) are bounded by the 500-entry queue cap —
measured at 0.4 ms for a queue filled to that cap.

Writing is not the bottleneck either: importing a year takes **about 10
minutes** (~7,000 runs/s in 500-record batches), and a nightly 12,000-test
import onto a full year of history takes **~2.5 seconds**.

To see this at your own scale, seed a large estate and time it:

```
python3 tools/demo_bootstrap.py --db scale.db --scale-tests 12000 --days 60 \
    --skip-self-tests --no-serve
python3 run_server.py --db scale.db
```

### Where the bytes go, and how to bound them

Test **output** would otherwise be the overwhelming majority of the database,
and it is read by exactly one endpoint. So it does not live in `runs`:

- It sits in its own table, `run_outputs`, keyed by run id. Keeping it out of
  `runs` keeps the metadata rows dense, which is what makes index-then-lookup
  queries (run history, failure streaks, the detail page) cheap regardless of
  how much a failing test dumps.
- It is stored **zlib-deflated** (level 6). Measured over the generated year,
  that is a **14× reduction: 12.2 GB of output becomes 0.85 GB**, and it is
  what takes a year from ~14 GB down to 2.3 GB on disk. It costs ~35 µs per run
  to write (about 0.4 s on a 12,000-test night) and ~11 µs to read back — and
  it makes imports *faster*, not slower, because the I/O saved dwarfs the CPU
  spent.

SQLite itself is not the limit here (its file-size ceiling is measured in
terabytes); backups, file copies and OS cache pressure are. So keep a retention
policy:

```
# after the nightly import, from cron:
python3 tools/prune_runs.py --db /var/lib/testboard/testboard.db --keep-days 365

# quarterly, in a maintenance window (exclusive lock, rewrites the file):
python3 tools/prune_runs.py --db /var/lib/testboard/testboard.db \
    --keep-days 365 --vacuum
```

`prune_runs.py` deletes runs older than the window **except each test's newest
run**, which is what the dashboard shows — a test that stopped running must
appear under "Not run", not vanish. `prev_result` is re-derived afterwards so
the triage queues cannot be left describing a run that no longer exists. Use
`--dry-run` to see the count first. Without `--vacuum` the freed pages stay in
the file and are reused by later imports, which for a steady nightly load is
usually what you want.

---

## When something goes wrong

The system is meant to tell you what to do without anyone reading the source.
Every failure below was tested by causing it.

**The server won't start.** It prints the cause and the fix, then exits `2`:
a database path whose directory doesn't exist, isn't writable, is a directory,
holds a file that isn't a SQLite database, is corrupt, is locked by another
process, or is on a full disk — each gets its own message and its own next step.
A port already in use, or a missing `static/` directory, likewise.

**An import failed.** The feeder's exit code is the summary:

| Code | Meaning | What to do |
|---|---|---|
| `0` | everything valid was accepted | nothing |
| `1` | needs attention | read the grouped reasons at the end of the log |
| `2` | fatal — bad arguments or the reader wouldn't load | the message names the fix |

Exit `1` includes three cases that would otherwise pass silently: the server
rejected records, **nothing was read at all** (a `--source` that stopped
matching, or a reader returning nothing), and **≥10% of records were invalid**
(a reader mis-mapping a field). A scheduled task only reports its exit code, so
those cannot be warnings.

**The import log is the diagnosis.** Invalid records are grouped by reason with
a count and one affected test — `6000 x [end_time: must be >= start_time] first:
linux-sim / integration/suite_000.py / test_case_00017` — so a systematic
problem reads as one line, not six thousand. Only the first five examples of
each distinct problem are logged in full; the rest are counted.

**A long import looks stuck.** It isn't: every 30 seconds it logs
`progress: N records read (…), N records/s`.

**The server died mid-backfill.** The feeder stops after 3 consecutive failed
batches rather than writing a replay file for every remaining batch, and says
so. Fix the server and re-run the same command — imports are idempotent, so
re-running is always the right recovery. Individual failed batches are saved as
`testboard_failed_batch_NNNN.json` with the exact `curl` command to re-send
them.

**The disk is filling up.** See [Scale and retention](#scale-and-retention):
`tools/prune_runs.py` with `--dry-run` first, then `--vacuum` to return the
space.

**A reader is being written or changed.** `python3 run_feeder.py --check-reader
--reader mymodule:create_reader --source …` validates every record with no
server and no network, and reports what the reader actually produced. See
[`docs/FEEDER_BRIEF.md`](docs/FEEDER_BRIEF.md).

---

## Keeping proprietary data out

This repo is public-friendly by construction:

- The **only** site-specific code is your feeder reader. Name it `internal_reader.py`
  (or anything matching `internal_*.py`) in the repo root — that pattern is in
  `.gitignore`, so proprietary hostnames, URLs, parsing logic and credentials can
  never be committed by accident.
- Databases (`testboard.db*`, `*.sqlite*`), feeder state (`feeder_state.json`),
  failed-batch replay files (`testboard_failed_batch_*.json`) and demo data
  (`demo.jsonl`) are gitignored too — real test output never lands in git.
- Everything else in the repo is generic and contains only simulated example data.

---

## Why Python 3.6?

The production host is RHEL 8, whose platform Python is CPython **3.6.8** — and the
deployment constraint is "no installs beyond the OS". So the code targets 3.6
exactly: no `dataclasses`, no `datetime.fromisoformat`, no
`http.server.ThreadingHTTPServer`, no walrus operator, `typing.NamedTuple` instead of
dataclasses, a hand-rolled (and unit-tested) ISO-8601 parser, and a threading HTTP
server composed from `socketserver.ThreadingMixIn` + `http.server.HTTPServer`.

At the same time, the suite runs clean on modern Pythons (CI covers up to 3.14), so
development on a current interpreter is painless. The dedicated CI job running inside
the `ubi8/python-36` container is the authoritative compatibility gate.

## Development

```
python -m unittest discover               # full suite
python -m unittest tests.test_storage     # one module
python -m unittest tests.test_storage.TestClass.test_method
```

Layout:

```
run_server.py           # server entry point
run_feeder.py           # feeder CLI (backfill / daily)
testboard/              # model, storage (all SQL), analytics (pure), api, server
feeder/                 # reader interface + jsonl reader, submitter, state file
tools/                  # demo data generator, self-test collector, demo_bootstrap,
                        #   prune_runs (retention)
static/                 # vanilla ES6 frontend, no build step:
                        #   index.html   dashboard + triage
                        #   actions.html open actions by owner
                        #   script.html  one suite's execution history
                        #   test.html    one test's detail
tests/                  # unittest suites (unit + e2e on an ephemeral port)
docs/                   # briefs, incl. FEEDER_BRIEF.md for site readers
```

Ground rules for contributions: Python 3.6-compatible, standard library only, every
function fully type-annotated (3.6-style `typing`), `unittest` only, parameterized
SQL only, no global mutable state, user data into the DOM via `textContent` only.

One more, because the estate is large: **no endpoint may do work proportional to
the number of tests.** List endpoints page in SQL, aggregates come from
`latest_runs`, and anything per-row is bounded by the page or the queue cap.
`ORDER BY` cannot be parameterized, so sortable columns come from the
`DASHBOARD_SORTS` whitelist in `testboard/storage.py` — `tests/test_api.py`
asserts the table headers in `static/index.html` never drift from it.

## Non-goals (v1) / future work

- Authentication/authorisation and HTTPS — run behind a reverse proxy if needed.
- Editing/deleting comments, or deleting individual runs. (Bulk retention is
  covered — see [Scale and retention](#scale-and-retention).)
- Real-time updates/websockets — plain page refresh is fine.
- Charting libraries — every chart (nightly trend, per-environment bars,
  day-of-week profile) is hand-rolled SVG/HTML.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Luke Humphreys.
