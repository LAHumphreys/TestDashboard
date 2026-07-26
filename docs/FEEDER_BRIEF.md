# Feeder Reader Brief — writing the site-specific reader for testboard

**Audience:** an AI coding assistant (and its human reviewer) with access to the
internal test system whose results are being imported. Follow this brief exactly.
Do not invent alternative interfaces, file names, or JSON shapes — the dashboard
and feeder framework are already written and tested against the contracts below.

**This document is self-contained.** Everything needed to write the reader — the
interface, the output schema, every validation rule, a complete worked example,
and the command that checks your work — is in here. You do not need to open any
other file in the repository.

---

## How to use this with Copilot (read this first)

Attach **three** files and paste the prompt below:

1. **this brief** (`FEEDER_BRIEF.md`)
2. a **sample of your internal results data** (an export file, a query result, a
   screenshot of the schema — whatever shows the real field names and values)
3. a **second sample**, ideally one containing a failure and any known-failure
   annotation, so the result mapping can be worked out from real data

> **Prompt to paste:**
>
> Attached are (1) a brief describing exactly what to build, and (2, 3) samples
> of your internal test-results data.
>
> Write `internal_reader.py` following the brief exactly: same file name, same
> class and factory names, same output dict schema. Work out the mapping from
> your data (attachments 2 and 3) onto the brief's transport schema — especially
> the `result` values and the timestamp format, which must be converted to naive
> UTC.
>
> Python 3.6, standard library only, full type annotations, docstrings. Do not
> modify any other file. When you are done, tell me the exact command from the
> brief that verifies the reader, and list any field you had to guess.

Then run the verification command the brief gives you (`--check-reader`) and
paste the output back to Copilot if it fails. That loop — write, check, paste
the errors — is the whole workflow; each error message names the rule that was
broken and an affected test.

## Context: almost everything already exists

This repository ships the complete dashboard **and** the complete feeder framework:

- `run_feeder.py` — the finished CLI. Handles argument parsing, backfill/daily modes,
  the high-water-mark state file, batching, retries, replay files, logging, and exit
  codes. **Do not modify it.**
- `feeder/submitter.py` — validates records, batches them, POSTs to
  `/api/import`, retries with backoff, writes failed-batch replay files. **Do not
  modify it.**
- `feeder/reader.py` — the `Reader` base class and a ready-made JSON-lines reader.
  **Do not modify it.**

**The ONLY thing to write is one small reader module** that knows how to get run
results out of your internal test system and yield them as plain dicts. Nothing else.

## The file you will create: `internal_reader.py`

Create it in the **repo root** (next to `run_feeder.py`), named exactly
`internal_reader.py`. This matters: the repo's `.gitignore` excludes `internal_*.py`,
so internal hostnames, URLs, file paths, and parsing logic can never be committed to
the open-source repo by accident. **Never put proprietary details in any other
file**, and never commit this one.

## The interface to implement

The framework defines (in `feeder/reader.py`):

```python
class Reader(abc.ABC):
    @abc.abstractmethod
    def read(self, since: Optional[datetime.datetime]) -> Iterator[Dict[str, Any]]
        # Yields raw transport dicts (schema = RunRecord transport). May over-return;
        # the submitter/CLI filter by since. Must not raise on a bad record — skip and log.
```

Your module must provide a **factory function** that `run_feeder.py` loads via the
`--reader internal_reader:create_reader` flag:

```python
def load_reader(spec: str, sources: List[str]) -> Reader
    # spec "jsonl" -> JsonLinesReader(sources); else "module.path:factory" -> importlib,
    # factory(sources) -> Reader. Clear error on failure.
```

So `internal_reader.py` looks like this skeleton:

```python
#!/usr/bin/env python3
"""Site-specific testboard reader for <internal system>. DO NOT COMMIT."""
import datetime
from typing import Any, Dict, Iterator, List, Optional

from feeder.reader import Reader


class InternalReader(Reader):
    """Reads overnight run results from <internal system>."""

    def __init__(self, sources: List[str]) -> None:
        self._sources = sources  # whatever --source values mean for your system

    def read(self, since: Optional[datetime.datetime]) -> Iterator[Dict[str, Any]]:
        """Yield one transport dict per test run."""
        # ... fetch/parse internal results, yield dicts (schema below) ...
        # It is FINE to yield runs older than `since` — the framework filters.
        # NEVER raise because one record is bad: log a warning and continue.
        raise NotImplementedError


def create_reader(sources: List[str]) -> Reader:
    """Factory used by: run_feeder.py --reader internal_reader:create_reader"""
    return InternalReader(sources)
```

Notes on `read()`:

- `since` is an optimisation hint (a naive UTC datetime, or `None`): if the internal
  system can query "results newer than X" cheaply, use it. If not, yield everything —
  the framework drops old records itself, correctness does not depend on you.
- The generator must be robust: one unparseable record must be **skipped with a
  logged warning**, never allowed to raise and kill the whole import.

## The dict shape to yield (RunRecord transport schema — verbatim contract)

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

Validation rules the framework (and the server) will enforce on every dict:

- `environment`, `script`, `test_name` — required strings, non-empty after stripping
  whitespace. Together they identify the test.
- `result` — required; exactly one of the strings `"PASS"`, `"FAIL"`,
  `"FAILED_AS_EXPECTED"`, `"UNEXPECTED_PASS"`. Map the internal system's outcome
  values onto these four. Anything else is rejected.
- `start_time`, `end_time` — required strings, format
  `YYYY-MM-DDTHH:MM:SS.ffffff` (fractional part 1–6 digits, or omitted entirely).
  **No timezone suffix** — `"...07Z"` or `"...07+00:00"` is rejected.
  `end_time >= start_time` is required.
- `output` — the key is required and the value must be a string; `""` is allowed.
- `source_link` — optional string (weblink to the test source); defaults to `""`.
- `known_failure_reason` — optional; a string or `null`; defaults to `null`.
  Never required, and a run without one imports normally. If the internal system
  records WHY a failure is expected (a ticket id, a defect reference, a
  sentence), send it: the dashboard shows it as a banner on the test page, and
  it is what tells a reviewer whether the annotation is still justified. Send it
  on `FAILED_AS_EXPECTED` runs above all.
- Extra keys are ignored, so you may pass through internal fields harmlessly.

## A complete worked example

This is a full, working reader for an imagined internal format — a CSV export
with local timestamps and site-specific outcome codes. **Copy this structure**
and change the parsing and the mappings to match your real data.

Given input rows like:

```
run_id,suite,case,outcome,started,duration_ms,logfile,defect
88213,user_lifecycle,partial_update_retry,FAILED,2026-07-25 03:14:07,1878,/logs/88213.log,JIRA-4821
88214,user_lifecycle,cancel_retry,OK,2026-07-25 03:14:09,942,/logs/88214.log,
```

```python
#!/usr/bin/env python3
"""Site-specific testboard reader for <internal system>. DO NOT COMMIT."""

import csv
import datetime
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

from feeder.reader import Reader

logger = logging.getLogger(__name__)

# Our outcome codes -> testboard results. Anything not listed is skipped
# with a warning rather than guessed at.
_RESULTS = {
    "OK": "PASS",
    "PASSED": "PASS",
    "FAILED": "FAIL",
    "KNOWN_FAIL": "FAILED_AS_EXPECTED",
    "UNEXPECTED_OK": "UNEXPECTED_PASS",
}

# The lab reports local time; the dashboard stores naive UTC.
_UTC_OFFSET = datetime.timedelta(hours=1)   # BST -> UTC


class InternalReader(Reader):
    """Reads overnight run results exported as CSV by <internal system>."""

    def __init__(self, sources: List[str]) -> None:
        """Remember the export paths passed via --source."""
        self._sources = sources

    def read(
        self, since: Optional[datetime.datetime]
    ) -> Iterator[Dict[str, Any]]:
        """Yield one transport dict per test run.

        Over-returning is fine: the framework drops anything older than
        `since`. One bad row must never raise — log it and continue.
        """
        for path in self._sources:
            if not os.path.isfile(path):
                logger.warning("source %s does not exist; skipping", path)
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, row in enumerate(csv.DictReader(handle), 2):
                    record = self._to_record(row, path, line_number)
                    if record is not None:
                        yield record

    def _to_record(
        self, row: Dict[str, str], path: str, line_number: int
    ) -> Optional[Dict[str, Any]]:
        """Convert one export row, or None if it cannot be converted."""
        try:
            result = _RESULTS.get((row.get("outcome") or "").strip().upper())
            if result is None:
                logger.warning(
                    "%s:%d: unknown outcome %r; skipping",
                    path, line_number, row.get("outcome"),
                )
                return None
            start_local = datetime.datetime.strptime(
                row["started"].strip(), "%Y-%m-%d %H:%M:%S"
            )
            start = start_local - _UTC_OFFSET          # -> naive UTC
            end = start + datetime.timedelta(
                milliseconds=int(row["duration_ms"])
            )
            defect = (row.get("defect") or "").strip()
            return {
                "environment": "linux-prod-sim",
                "script": "{0}.py".format(row["suite"].strip()),
                "test_name": "test_{0}".format(row["case"].strip()),
                "result": result,
                "start_time": start.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "end_time": end.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "output": self._read_log(row.get("logfile")),
                "source_link": "https://git.example.com/tests/{0}.py".format(
                    row["suite"].strip()
                ),
                "known_failure_reason": defect if defect else None,
            }
        except (KeyError, ValueError) as exc:
            logger.warning(
                "%s:%d: cannot convert row (%s); skipping",
                path, line_number, exc,
            )
            return None

    def _read_log(self, log_path: Optional[str]) -> str:
        """Return the captured output for a run; '' when unavailable."""
        if not log_path:
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


def create_reader(sources: List[str]) -> Reader:
    """Factory used by: run_feeder.py --reader internal_reader:create_reader"""
    return InternalReader(sources)
```

Note what this example does that yours must also do: it **maps** outcome codes
explicitly rather than passing them through, it **converts local time to UTC**,
it **never raises** on a bad row, and it returns `None`/`""` rather than
inventing values.

### UTC conversion is YOUR job

The dashboard stores and compares naive **UTC** timestamps. If the internal system
reports local time (it probably does), the reader must convert to UTC **before**
formatting the string. Do the conversion explicitly and write a unit test for it —
a reader that silently ships local times will make "when did this start failing?"
answers wrong by hours. Format with:

```python
dt.strftime("%Y-%m-%dT%H:%M:%S.%f")   # on a naive datetime already in UTC
```

## Running imports (already implemented — these commands work as-is)

Both modes are **safe to re-run at any time**: the server upserts on
`(environment, script, test_name, start_time)`, so re-importing the same runs
updates rather than duplicates.

**Backfill** (one-off, load history; `--since` optionally bounds how far back):

```
python run_feeder.py --url http://HOST:8000 --mode backfill --reader internal_reader:create_reader
```

### Backfilling a year of history

This is the first real import, and it is big: roughly 12,000 tests × 365 nights
= 4.4 million runs. Measured end to end, the feeder sustains **~2,000 records a
second**, so expect the full backfill to take **about 35–40 minutes**. What that
looks like, and what to do when it goes wrong:

- It **streams** — the reader is a generator and batches are released after
  they are sent, so memory stays flat whether you import one night or a year.
- Every 30 seconds it logs a progress line (`progress: 73084 records read
  (69430 valid, 3654 skipped) in 30s, 2436 records/s`). A long import is never
  silent, so it is never mistaken for a hung one.
- One unreadable file, or one malformed line, is **skipped with its name and
  line number** — a truncated export in the middle of the directory does not
  stop the other 364 nights.
- The first 5 records of each distinct problem are logged in full; after that
  the reason is counted rather than repeated. A systematic mistake affecting
  every record produces a handful of examples and one total, not millions of
  log lines.
- **If the server goes away mid-import**, the feeder stops after 3 consecutive
  failed batches instead of grinding through the rest writing one replay file
  per batch. Fix the server and re-run the same command — importing is
  idempotent, so re-running is always safe and is the intended recovery.
- If a batch is rejected as too large (HTTP 413 — a few tests dumping megabytes
  of output can do it), the message tells you the batch size to use instead.

Re-running after a partial import is cheap and correct: already-imported runs
are updated in place rather than duplicated. If you want to skip what already
landed, pass `--since` with a date.

**Daily** (incremental; run after the overnight tests finish):

```
python run_feeder.py --url http://HOST:8000 --mode daily --reader internal_reader:create_reader
```

Daily mode remembers the newest accepted `start_time` in `feeder_state.json` (the
high-water mark) and next time imports everything newer than that minus
`--overlap-days` (default 1). The overlap plus server-side upserts means nothing is
missed and nothing duplicates. A successful backfill primes the state file, so the
rollout sequence is: backfill once, then schedule daily.

Other useful flags: `--source` (repeatable; a file, a glob, **or a directory** —
a year of history usually arrives as a directory of per-night files, and the
directory is walked recursively for `.jsonl`/`.json`/`.ndjson`. Passed straight
to your factory, so for a custom reader it means whatever you want),
`--since ISO` (backfill lower bound), `--batch-size` (default 500),
`--state-file` (default `feeder_state.json`), `--replay-dir` (default `.`),
`--dry-run`, `--check-reader`, `--allow-empty`, `--max-consecutive-failures`
(default 3), `--verbose`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | every valid record was accepted |
| `1` | something needs attention — see below |
| `2` | fatal: bad arguments, or the reader could not be loaded |

Exit `1` covers: the server rejected records; a batch failed; **nothing was read
at all**; or **10% or more of the records were invalid**. The last two exist
because a scheduled task only reports its exit code — an import that silently
stopped feeding, or a reader that mis-maps a field for a tenth of the estate,
must not look like success. A handful of bad records among thousands stays exit
`0` (the count is in the summary). If an empty import is genuinely expected,
pass `--allow-empty`.

## Debugging workflow: `--check-reader` first, then `--dry-run`

While developing the reader, **never point a first attempt at the real server**.

**Step 1 — check the reader on its own.** This needs no server, no `--url`, and
no network. It loads the reader, reads every record, validates each one exactly
as the server would, and tells you what is wrong:

```
python run_feeder.py --check-reader --reader internal_reader:create_reader --source ... --verbose
```

It prints what the reader actually produced — environments, script count, result
distribution, the span of `start_time` — then either `reader OK: every record
validates` (exit 0) or the grouped list of problems (exit 1). Iterate here until
it is clean; it is much faster than a round trip through the server.

It also warns about things that are *valid but almost certainly wrong*, the most
important being **timestamps in the future**, which is what a reader emitting
local time instead of UTC looks like. Take those warnings seriously: they
describe failures that import cleanly and silently put every run in the wrong
hour.

**Step 2 — dry-run the full pipeline**, which adds the `--since` filtering and
batching without sending anything:

```
python run_feeder.py --url http://HOST:8000 --mode backfill --reader internal_reader:create_reader --dry-run --verbose
```

Then read the output:

1. Every skipped record produces a WARNING log line stating the reason, the record's
   identity (environment / script / test_name), and a repr of the offending value.
2. The final summary line prints all counts: `read`, `valid`, `skipped`, `sent`,
   `inserted`, `updated`, `rejected`, `failed_batches`.
3. Below it, skip/reject reasons are grouped by message with a count and an example
   record identity for each group, e.g.:

   ```
   23 x [result: unknown value] first: linux-prod-sim / regression/foo.py / test_bar
   ```

   That line means: 23 records had an unrecognised `result` string, and here is one
   concrete test to look up in the internal system. Fix the reader's result mapping,
   re-run with `--dry-run`, repeat until `skipped` is 0 and `valid == read`.

Only then drop `--dry-run` and do the real backfill. If the server rejects anything
(`rejected > 0`), the same grouped summary tells you which validation rule failed and
for which tests. If a batch fails outright (server down mid-import), the exact batch
body is saved as `testboard_failed_batch_NNNN.json` for later replay — nothing is
lost.

Connection problems print an actionable message (e.g. "Cannot reach the dashboard at
<url> (connection refused). Is the server running on that host?") — check the URL and
that the server is up before touching the reader.

## Scheduling (Windows Task Scheduler)

Once the dry-run is clean and the backfill has been done, schedule the daily import
after the overnight runs finish (adjust time and paths):

```bat
schtasks /Create /TN "testboard-daily-feed" /SC DAILY /ST 06:30 ^
  /TR "cmd /c cd /d C:\path\to\TestDashboard && python run_feeder.py --url http://HOST:8000 --mode daily --reader internal_reader:create_reader >> feeder.log 2>&1"
```

Check `feeder.log` and the task's Last Result (should be `0`) after the first
scheduled run.

## Testing the reader

First: `python run_feeder.py --check-reader --reader internal_reader:create_reader`
(see above) is the fast loop, and it must exit 0 before you go near the server.

Then write a `unittest` module for the reader (name it `internal_test_reader.py`
or similar under `internal_*.py` so it is also gitignored). Guidance:

- Save one or two small, **sanitised** fixture files/payloads of internal results
  next to the tests (also named `internal_*` so they stay out of git) and assert that
  `read()` yields dicts that pass validation — the authoritative check is:

  ```python
  from testboard.model import parse_run_record
  for rec in reader.read(None):
      parse_run_record(rec)   # raises ValidationError with a clear message if wrong
  ```

- Test the local-time → UTC conversion with a known input/output pair.
- Test that a malformed record is skipped (yields continue past it) rather than
  raising.
- Run with: `python -m unittest internal_test_reader` from the repo root.

## Definition of done

The reader is finished when all of these are true:

- [ ] The file is `internal_reader.py`, in the repo root, with a
      `create_reader(sources)` factory returning a `Reader` subclass.
- [ ] `python run_feeder.py --check-reader --reader internal_reader:create_reader --source <your data>`
      exits **0** and prints `reader OK: every record validates`.
- [ ] That command reports **no warnings**, in particular none about timestamps
      in the future (which means local time is being emitted as UTC).
- [ ] The reported environments, script count and result distribution match what
      you expect from the source system — a reader that maps every outcome to
      `PASS` validates perfectly and is still wrong.
- [ ] A deliberately corrupted input row is skipped with a warning, not raised.
- [ ] `--dry-run` against the real server URL shows `skipped=0`.
- [ ] No proprietary hostname, path, or field name appears in any file other
      than `internal_*.py`.

## Constraints (non-negotiable)

- **Python 3.6.** The production host runs CPython 3.6.8. No 3.7+ features: no
  `dataclasses`, no `datetime.fromisoformat` (use `strptime`), no walrus `:=`, no
  `typing.Protocol`/`Literal`. f-strings are fine inside the reader module.
- **Standard library only.** No `pip install` anything. HTTP via `urllib.request`
  (the framework already handles submission for you), parsing via `json` / `csv` /
  `re` / `xml.etree` as needed.
- **Full type annotations** on every function and method, 3.6-style: `List[str]`,
  `Optional[X]` from `typing` — never `list[str]` or `X | None`.
- Docstrings on the module and every public function/class.
- `unittest` only for tests.
