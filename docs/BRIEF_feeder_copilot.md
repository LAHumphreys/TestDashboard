# Brief: Test Results Feeder Script (for an AI coding assistant)

## Context

We run an open-source dashboard ("testboard") on our network that displays overnight
unit/regression test results. The dashboard is a separate project; **your task is only
the feeder**: a Python script that reads your internal test result data and pushes it
into the dashboard's HTTP import API. Two modes are needed:

1. **Initial backfill** — import all available historical results.
2. **Daily update** — import the latest overnight results; safe to re-run (the API
   upserts, duplicates are impossible).

## Constraints

- **Python 3.6, standard library only.** No pip installs. Use `urllib.request` for
  HTTP, `json` for encoding. Do not use any Python 3.7+ features (no `dataclasses`,
  no `datetime.fromisoformat`, no `subprocess.run(capture_output=...)`).
- Fully type-annotated, clean, professional code with a `unittest` test suite
  (`python -m unittest discover`). Mock the HTTP layer in tests; do not hit a real
  server.
- The script must never crash the whole import because one record is bad: log and
  skip malformed records, and report counts at the end.

## What to collect per test run

Each *run* of a test must produce one record with:

- **Identity** (all three required):
  - `environment` — the environment that ran the test
  - `script` — the test script/suite the test belongs to
  - `test_name` — the individual test name
- `result` — exactly one of: `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS`
- `start_time`, `end_time` — when the run started/ended, **UTC**, ISO-8601 format
  `YYYY-MM-DDTHH:MM:SS.ffffff` (no timezone suffix). If source data is in local time,
  convert to UTC in the feeder.
- `output` — the full captured output of that run (string; empty string if none)
- `source_link` — a URL to the test's source code (your internal code browser)
- `known_failure_reason` — the recorded reason if the test is annotated as a known
  failure, else `null`

*(Adapt the "reading" side to wherever our results actually live — log files,
database, CI artifacts. That part is site-specific; structure it as a separate,
swappable reader module so the HTTP submission code is generic.)*

## The import API (fixed contract — do not change)

`POST http://<dashboard-host>:<port>/api/import` with `Content-Type: application/json`:

```json
{
  "runs": [
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
  ]
}
```

Response is JSON with counts of inserted/updated/rejected and per-record errors for
rejects. The server upserts on `(environment, script, test_name, start_time)`, so
**re-sending the same data is safe and is the intended recovery mechanism** — on any
doubt, just re-run.

## Behavioural requirements

- **Batching**: send runs in batches (configurable, default 500) — `output` fields can
  be large. Retry a failed batch up to 3 times with backoff; on final failure, write
  the batch to a local `.json` file for manual replay and continue with the next batch.
- **Daily mode**: track a high-water mark (last successfully imported `start_time`)
  in a small local state file (JSON); on each run, import everything at-or-after it
  minus a safety overlap (e.g. 1 day — upsert makes overlap free).
- **CLI**: `--url`, `--mode backfill|daily`, `--since <date>` (backfill start),
  `--batch-size`, `--state-file`, `--dry-run` (parse and validate, print counts, send
  nothing).
- **Validation before send**: identity fields non-empty, result in the allowed set,
  timestamps parseable and `end >= start`. Invalid records are logged with enough
  detail to find them at source.
- **Logging**: `logging` module, INFO summary per batch, WARNING per skipped record,
  final summary (read / valid / sent / upserted / rejected / skipped).
- Exit code 0 only if all valid records were accepted; non-zero otherwise (so the
  daily scheduled task flags failures).

## Deliverables

- `feeder.py` (CLI entry point), a reader module for your data source, a generic
  `submitter` module (validation + batching + HTTP), and `tests/` covering
  validation, batching, retry/replay-file behaviour, high-water-mark logic, and
  UTC conversion.
- Short README: how to run backfill once, how to schedule the daily run (Windows Task
  Scheduler example), where the state and replay files live.
