# Feeder Template — writing a site's single-file feeder for testboard

**Audience:** an AI coding assistant (and its human reviewer) implementing the
site-specific half of a testboard feeder. Follow this document exactly. Do not
invent alternative interfaces, file names, argument names, or JSON shapes —
the engine is already written and tested against the contracts below.

**This document is self-contained.** Everything needed — the invocation
model, the wire schema, the one function to implement, two complete worked
examples, and the acceptance checklist — is here. You do not need to open any
other file in the testboard repository, and the site repository you are
adding this to needs none of it either.

This document **supersedes** `docs/FEEDER_BRIEF.md`, which described an
older, checkout-based feeder (`run_feeder.py` + the `feeder/` package) built
for one product polling on a schedule. That feeder still exists and still
runs, untouched, for the product it was built for. This document is for
every **new** product: one file, pushed once per suite execution, checked
into that product's own repository.

---

## The one thing you are building

**One file.** Either:

- `clients/feeder.py` (Python 3.6+), or
- `clients/feeder_micro.py` (Python 3.6+ — the reduced engine, about a
  third of the code; see "Choosing between the two Python engines" below), or
- `clients/feeder.tcl` (vanilla Tcl 8.5+ — no tcllib, no `tls`, nothing
  beyond a bare `tclsh`)

Copy the file from the testboard repository into the site's own repository —
anywhere convenient, commonly the test framework's own tooling directory.
Every file has an **"IMPLEMENT THIS SECTION"** banner near the top and a
**"DO NOT EDIT BELOW THIS LINE"** banner below it. You edit only between
those two banners. Everything below the second banner is the engine:
argument parsing, wire-schema validation, batching, retry/backoff, replay
files, exit codes. It is already written, already tested, and upgrading it
later is a re-paste of everything below the line — never edit it, and never
ask a site to touch it.

**What you implement, concretely:**

- A `DASHBOARD_URL` constant — the dashboard's backend `host:port`
  (`feeder.py` and `feeder.tcl` only; `feeder_micro.py` has no such
  constant and takes a mandatory `--url` on every invocation instead).
- Whatever command-line flags your reader needs to find this run's results —
  the worked convention is a single `--results PATH`. In `feeder.py` this is
  a small `add_site_arguments(parser)` hook; in `feeder.tcl` it is a flat
  `EXTRA_FLAGS` list.
- One function, `read_records`, that turns the site's test results into
  plain dicts in the wire schema below (see "The one function to implement"
  further down).

That is the entire task. The engine already handles validation, batching,
retries, replay files and exit codes — do not reimplement any of it, and do
not add fields, flags, or files beyond what is described here.

### Choosing between the two Python engines

Both Python files carry the **same IMPLEMENT THIS contract** — the same
hook and reader symbols, called the same way — and a section written for
`feeder.py` drops into `feeder_micro.py` unchanged (the conformance suite
in the testboard repository transplants it on every push).
Write your reader once; it runs on both — either direction is a straight
paste (both files are ordinary annotated Python 3.6+; neither parses under
Python 2).

**Default to `feeder.py`.** Pick `feeder_micro.py` when the receiving
product's review gate balks at the full engine's size — that is the reason
it exists. What the micro engine gives up, and what stands in for it:

| Full engine (`feeder.py`) | Micro engine (`feeder_micro.py`) |
|---|---|
| A failed batch is saved to a **replay file**; the next invocation resends it automatically | **Nothing is written to disk, ever.** Exit 1 means "re-invoke me" — safe, because the server skips anything it already has. Recovery needs the framework to retry its cleanup step, or a person to re-run the command; a run that nobody re-invokes during an outage is a visible gap on the board |
| Full client-side validation mirroring the server's rules; `--dry-run` catches bad records locally | A shallow sanity check (required fields present, `result` recognised); the server's per-record rejections are reported in the log instead |
| Hard wall-clock ceiling: `--time-budget` + `--http-timeout`, ~115 s worst case | No budget flag; worst case is 3 × `--http-timeout` + two 2 s pauses per batch (~49 s by default, one batch is typical) |
| Exponential backoff between retries | A flat 2 s pause |
| A `DASHBOARD_URL` constant, with `--url` as an optional override | **No hardcoded URL**: `--url` is mandatory on every invocation |
| Ships the `--results` JSON-lines reader as its worked default section | **Ships stubs** — every site writes its own flags and reader (in practice they all do anyway); the unmodified file warns "not implemented" and sends nothing |

Identical in both: the invocation model, `--environment`/`--build`
stamping, the `--build` acknowledgement (an old server is a loud exit 1 in
both, never a silent misfile into mainline), batching (500 records / 8 MB),
the exit-code meanings of 0 and 2, and the wire contract. Where the rest of
this document says "replay file" or "time budget", read it as full-engine
behaviour; the micro engine's own header comment states its versions of
those promises. (A full-engine section transplanted into the micro engine
keeps its `DASHBOARD_URL` constant as harmless dead weight — the micro
engine never reads it.)

---

## The invocation model — read this before writing any code

This is **not** the old feeder. The old one polls a schedule, keeps a
high-water mark, and re-reads a scanning window. This one does none of that.

**The test framework invokes the feeder once, from its own CLEANUP phase,
immediately after one suite execution finishes.** It is a short-lived push,
not a background process. Every invocation is independent: there is no
state file, no daily mode, no "since last time" — `read_records` is handed
only the arguments for *this* invocation and produces records for *this*
suite execution alone.

A framework's cleanup step calls it like this (Python shown; the Tcl
invocation is identical apart from the interpreter):

```bash
python3 /path/to/feeder.py --environment linux-nightly \
    --results /var/ci/run-8842/results.jsonl
```

or, for a release/RC build running beside mainline:

```bash
python3 /path/to/feeder.py --environment linux-nightly --build 2026.9.1-rc2 \
    --results /var/ci/run-8842/results.jsonl
```

### The argument contract

| Flag | Required? | Meaning |
|---|---|---|
| `--environment NAME` | **required** | Which environment this suite execution ran on. Stamped onto every record by the engine — `read_records` never sets it. |
| `--build NAME` | optional | This run belongs to a non-mainline **stream** (a release/RC build), not mainline. Pass the framework's own branch/release parameter here — **there is no separate `--build`/`--branch` distinction**; a framework's "branch" parameter is passed AS the build value. Also stamped onto every record by the engine. |
| `--dry-run` | optional | Validate and print what would be sent; POST nothing. See "Acceptance checklist" below. |
| `--url URL` | optional | Override `DASHBOARD_URL` for one invocation — mainly for testing against a scratch server. |
| `--results PATH` (or whatever your reader needs) | site-defined | Locates this run's results. `--results` is the worked convention below and is repeatable. |

`read_records` never sees `--environment` or `--build` — the engine applies
them to every record *after* `read_records` hands it over, overriding
anything the reader happens to set. This is deliberate: it means
`read_records` cannot get the stamp wrong, and a reader ported from another
site's template does not need touching for this part at all.

### Exit codes — tell your framework owner exactly this

| Code | Meaning | Should it fail the build? |
|---|---|---|
| **0** | Every valid record was accepted (or, under `--dry-run`, validated cleanly). | No. |
| **1** | The server was unreachable, refused a batch, or an old server did not acknowledge `--build`. The run's results are **safe** — written to a replay file next to the feeder file — and the *next* invocation resends them automatically, before its own batch. | Usually **not on its own** — this is "deferred", not "lost". A framework that wants zero tolerance for transient outages may still choose to fail on it; that is a judgement call for the framework owner, not this document. |
| **2** | Usage or validation error: bad arguments, unreadable results, no dashboard URL configured, or `read_records` crashed outright. **Nothing was sent.** | Yes — this means the invocation itself is broken, not the suite. |

A handful of bad individual *records* inside an otherwise successful batch
is **not** a failure: they are logged, skipped, and counted, and the
invocation still exits 0. Only a batch-level failure (unreachable server,
refused request, missing `--build` acknowledgement) is exit 1.

### Replay files — the only thing this feeder writes to disk

A failed batch is saved to a uniquely-named replay file next to wherever
`--replay-dir` points (default: the current directory). **Every invocation
first resends any pending replay files, then sends its own batch** — so a
transient outage self-heals on the next suite execution without anyone
doing anything. Replay file names are per-invocation (process id + a
millisecond timestamp), so two suite executions on different environments,
running cleanup on the same host at the same moment, never collide.

This is the **only** persistence the feeder has. There is no state file, no
log file it manages, nothing else written to disk.

### The time bound

Every HTTP call carries a timeout, and the whole invocation is bounded by a
wall-clock budget checked before every attempt. **Cleanup must never hang.**
With the shipped defaults the worst case is documented in the file's own
header comment (`--time-budget` + `--http-timeout`, roughly 115 seconds) —
comfortably inside the couple of minutes a cleanup step can spare. Do not
change these defaults in the IMPLEMENT-THIS section; they are engine
constants, not site configuration.

---

## Transport: direct to the backend port, always

**Feeders POST straight to the dashboard's backend port, plain HTTP.**
Never through nginx, never with a URL prefix, even on a site where the
dashboard is normally reached through one. `DASHBOARD_URL` is the bare
`host:port` a human would use to curl the API directly — nothing about
paths changes. This keeps the vanilla-Tcl engine viable (no `tls` package
assumed anywhere) and means a later nginx rollout in front of the dashboard
changes nothing about any deployed feeder. See the testboard README's
"Feeding in your own results" section for the same rule stated from the
server side.

---

## The wire schema — exactly what `/api/import` accepts

`read_records` yields plain dicts. Each one becomes one JSON object in the
`runs` array of a `POST /api/import` request. This is the **complete**
schema; anything not listed here is either optional-with-a-default or
rejected.

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

| Field | Required? | Rule |
|---|---|---|
| `environment` | set by the engine | Do not set this in `read_records` — see "the invocation model" above. |
| `script` | **required** | Non-empty string after stripping whitespace. The path/module of the containing script or suite. |
| `test_name` | **required** | Non-empty string after stripping whitespace. |
| `result` | **required** | Exactly one of `"PASS"`, `"FAIL"`, `"FAILED_AS_EXPECTED"`, `"UNEXPECTED_PASS"`. Map your framework's outcome values onto these four explicitly — never pass an internal value straight through. |
| `start_time`, `end_time` | **required** | `YYYY-MM-DDTHH:MM:SS.ffffff` — naive **UTC**, no timezone suffix (`Z` or `+00:00` is rejected). Fractional seconds are 1–6 digits or omitted. `end_time >= start_time` is required. |
| `output` | **required key** | A string; `""` is fine, but the key must be present. |
| `source_link` | optional | Defaults to `""`. |
| `known_failure_reason` | optional | String or `null`, defaults to `null`. Send it whenever the framework records *why* a failure is expected — a ticket id, a one-line reason. The dashboard shows it as a banner on the test page. |
| `build` | set by the engine | Do not set this in `read_records`; see "the invocation model" above. |

**Never send a `branch` key.** An earlier design considered a second
non-mainline "kind" called `branch`; it never shipped anywhere and the
server rejects any record carrying that key outright, whatever its value —
loudly, per-record, without aborting the rest of the batch. If your
framework calls its parameter "branch", pass its value as `--build`; there
is nothing else to do.

**All times UTC.** If your test system records local time — it probably
does — convert to naive UTC *inside* `read_records`, before formatting the
string. A reader that silently ships local time imports cleanly and puts
every run in the wrong hour, which is the kind of bug nothing downstream can
catch for you.

---

## The one function to implement

### Python (`clients/feeder.py`)

```python
def read_records(args: argparse.Namespace) -> Iterator[Dict[str, Any]]:
    """Yield one raw transport dict per test run for THIS invocation."""
```

Either use `yield` (a generator) or `return` an iterator/list. Never raise
because one record is bad — catch the specific parsing exception you
expect, log a warning via the module's `logging` (never `print`), and
`continue`. If your framework's whole results source is unreadable, log the
problem and `return` without yielding anything — that becomes an ordinary
"nothing to send" invocation, not a crash. If `read_records` itself raises
an uncaught exception, the engine reports it as a fatal usage error (exit
2) and sends nothing at all, even records it had already produced — a
reader that can crash mid-stream is a bug in the reader.

Site-specific arguments come from `add_site_arguments(parser)`, called once
before parsing:

```python
def add_site_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--results", action="append", default=None,
                         metavar="PATH", help="a results file to read")
```

### Tcl (`clients/feeder.tcl`)

Tcl has no generators without 8.6 coroutines (forbidden — this engine
targets vanilla 8.5), so the shape is a **callback** instead of a return
value:

```tcl
proc read_records {opts emit} {
    ;# Call "$emit $recordDict" once per record. Never raise an error
    ;# because one record is bad - log with tb::Log WARNING and skip it.
}
```

`opts` is the parsed-arguments dict; your own flags are reachable as
`[tb::DGetList [dict get $opts site_args] --results]`. Site-specific flags
are declared as a flat Tcl list:

```tcl
set ::EXTRA_FLAGS {--results}
```

Every flag named here is **repeatable** and collects a list of string
values — the same shape as Python's `action="append"`.

---

## Worked example 1: a results-file reader (the shipped default)

Both `clients/feeder.py` and `clients/feeder.tcl` ship with this example
already implemented as their default `read_records` — a site whose test
framework can emit one JSON object per line, already close to the wire
schema, needs to write *nothing at all* beyond pointing `--results` at that
file. Given input like:

```
{"script": "regression/user_lifecycle.py", "test_name": "test_partial_update_retry", "result": "FAIL", "start_time": "2026-07-25T02:14:07.123456", "end_time": "2026-07-25T02:14:09.001000", "output": "boom\n"}
```

the Python shape is:

```python
def read_records(args):
    if not args.results:
        logging.getLogger("testboard_feeder").warning(
            "no --results given; nothing to read")
        return
    for path in args.results:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logging.getLogger("testboard_feeder").warning(
                "cannot open %s (%s); skipping it", path, exc)
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError as exc:
                    logging.getLogger("testboard_feeder").warning(
                        "%s:%d: skipping malformed JSON line (%s)",
                        path, line_number, exc)
                    continue
                yield obj
```

If your framework's native output is already close to this shape, teaching
it to emit one JSON object per line (matching the wire schema field names)
is usually less work than writing a custom reader at all.

## Worked example 2: scraping a plain-text test log

Most frameworks do not emit JSON. This example parses a simple text log —
one line per test, local time, site-specific outcome codes — and converts
to UTC explicitly. **Copy this structure**; change the parsing and mapping
to match real data.

Given input lines like:

```
2026-07-25 03:14:07 [FAILED] user_lifecycle.partial_update_retry (1878ms) JIRA-4821
2026-07-25 03:14:09 [OK] user_lifecycle.cancel_retry (942ms)
```

```python
import argparse
import datetime
import logging
import re
from typing import Any, Dict, Iterator

logger = logging.getLogger("testboard_feeder")

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<outcome>\w+)\] (?P<suite>[\w.]+)\.(?P<case>\w+) "
    r"\((?P<ms>\d+)ms\)(?: (?P<ticket>\S+))?$"
)

# Local lab time -> UTC. Do this explicitly; never ship local time.
_UTC_OFFSET = datetime.timedelta(hours=1)  # BST -> UTC

_RESULTS = {"OK": "PASS", "FAILED": "FAIL", "KNOWN_FAIL": "FAILED_AS_EXPECTED"}


def read_records(args: argparse.Namespace) -> Iterator[Dict[str, Any]]:
    if not args.results:
        logger.warning("no --results given; nothing to read")
        return
    for path in args.results:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("cannot open %s (%s); skipping it", path, exc)
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                match = _LINE_RE.match(line.rstrip("\n"))
                if match is None:
                    if line.strip():
                        logger.warning(
                            "%s:%d: unrecognised line, skipping: %r",
                            path, line_number, line.rstrip("\n"))
                    continue
                result = _RESULTS.get(match.group("outcome"))
                if result is None:
                    logger.warning(
                        "%s:%d: unknown outcome %r; skipping",
                        path, line_number, match.group("outcome"))
                    continue
                start_local = datetime.datetime.strptime(
                    match.group("ts"), "%Y-%m-%d %H:%M:%S")
                start = start_local - _UTC_OFFSET
                end = start + datetime.timedelta(
                    milliseconds=int(match.group("ms")))
                yield {
                    "script": match.group("suite") + ".py",
                    "test_name": "test_" + match.group("case"),
                    "result": result,
                    "start_time": start.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "end_time": end.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "output": "",
                    "known_failure_reason": match.group("ticket"),
                }
```

Note what this example does that yours must also do: it **maps** outcome
codes explicitly rather than passing them through, it **converts local time
to UTC before formatting**, it never raises on an unrecognised line, and it
names the file and line number in every warning so a bad line can actually
be found.

---

## Acceptance checklist

Work through this before calling the reader done. Every item is checkable
without a network connection except the last two.

- [ ] `python3 feeder.py --environment test --results <a real results
      capture> --dry-run` (or the Tcl equivalent) exits **0** and prints
      `skipped=0` — every record from a real capture validates.
- [ ] The printed "would be sent as" records are right in *content*, not
      merely valid: the right script path, the right test names, real
      output text, correctly-converted UTC timestamps.
- [ ] Deliberately corrupt one line/record in a copy of the results capture
      (bad outcome code, malformed timestamp) and confirm `--dry-run` logs
      one `skipping invalid record` warning naming it and continues — never
      an unhandled crash.
- [ ] Counts match a known sample: if the capture has N tests and you know
      how many should map to each result, the `--dry-run` summary
      (`read=… valid=… skipped=…`) agrees.
- [ ] `--dry-run` against the **real dashboard URL** (still sends nothing)
      completes without error, confirming the URL and network path are
      right.
- [ ] A real invocation (no `--dry-run`) against a scratch or staging
      dashboard round-trips: the run appears on the dashboard with the
      right result, timestamps and output.

---

## What a site may NOT do

- **No extra dependencies.** Python: standard library only. Tcl: nothing
  beyond `package require http` (bundled with every stock Tcl). No `pip
  install`, no tcllib, no `tls`, no shelling out to a tool that would need
  installing.
- **Never edit below the "DO NOT EDIT BELOW THIS LINE" banner.** Everything
  below it is the engine. If it needs a fix, that fix belongs in the
  testboard repository and ships as a new version of the whole file to
  re-paste — never a local patch that a future re-paste would silently
  discard.
- **Never poll.** No cron job, no scheduled task, no daemon mode. The
  feeder is invoked once per suite execution, from the framework's own
  cleanup phase. If a site wants a scheduled *backfill* of history instead,
  that is a different tool (`run_feeder.py`, the checkout-based feeder) —
  not this one.
- **Never route through nginx or a URL prefix.** See "Transport" above.
- **Never add a new field to the wire schema**, and never rely on one being
  silently accepted — an unrecognised field is either ignored (harmless,
  forward-compatible) or, in the one deliberate exception (`branch`),
  loudly rejected. If a genuinely new field is needed, that is a change to
  the testboard server and this document, not something a site reader can
  introduce unilaterally.

---

## Getting Copilot to write the reader

Attach this document and two samples of the site's own test-results data
(ideally one containing a failure and any known-failure annotation, so the
result mapping and `known_failure_reason` handling can be worked out from
real data), then ask for exactly one function — `read_records` (Python) or
`proc read_records {opts emit}` (Tcl) — plus whatever `add_site_arguments`/
`EXTRA_FLAGS` the results location needs. Ask it to work through the
acceptance checklist above and report which fields it had to guess.
