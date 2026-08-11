# testboard — upgrade plan (post-launch, round 1)

**Status:** live in production since 2026-07-26. First deployment succeeded;
the schema is now **locked**.

This document is the work order for the next round of changes. It is written
to be executed by agents working unattended, so every package states what is
already decided, what must not change, and how "done" is proved. Where a
decision is genuinely the user's, it is marked **OPEN** and a default is given
so that work is never blocked waiting for an answer.

The MariaDB move (item 1) has its own document:
[`MARIADB_MIGRATION.md`](MARIADB_MIGRATION.md). Only its *code* half appears
here, as WP-9.

---

## 0. Standing rules

These apply to every package. They are not negotiable and they are not
summarised versions of something looser elsewhere.

### 0.1 The existing constraints still hold

Python 3.6 exactly; standard library only; no npm, no build step, no CDN; full
3.6-style annotations (`typing.List`, never `list[str]`); no global mutable
state. `tests/test_python36_compat.py` enforces this statically —
if it fails, the code is wrong, not the test.

### 0.2 The schema is locked

`MIGRATIONS[0]` (version 1) describes a database that exists in production.
**Never edit it.** Every schema change is a new entry, appended, with a version
number taken from the registry in §1. A migration runs inside one transaction
against a ~900 MB database — see §1.2 before writing one.

### 0.3 Guard tests encode production findings

`tests/test_frontend_calls.py`, `tests/test_server_pool.py` and
`tests/test_python36_compat.py` exist because each caught a real, silent,
expensive bug that was invisible in code review. Two of them were written after
the bug reached production.

> **If your change makes a guard test fail, widen its scope — never weaken its
> assertion.** Say in the commit message which you did and why.

Concretely: if you add a second file that fetches `/api/users`, the fix is to
import the shared loader, not to relax the "exactly one fetch site" assertion.
If you add a new page, add it to the scan rather than excluding it.

### 0.4 Scale is still the design constraint

~12,000 tests, ~4.4M runs at a year's retention. **No endpoint may be
proportional to the size of the estate.** Estate-wide reads go through
`latest_runs` / `current_assignments`; list endpoints page in SQL; only the
returned page joins `runs`. A new endpoint that does a `GROUP BY` over `runs`
is a defect even if it is fast on the dev database (540k rows, local SSD).

New rule, arising from this round: **any new list query must have a test that
asserts its cost does not grow with the estate** — either by asserting the query
plan uses an index, or by asserting the number of queries issued is O(1) in the
page size. Not just that it returns the right rows.

### 0.5 Commit discipline

- One package, one branch, one commit (or a small ordered series). Branch name
  `wp-<n>-<slug>`.
- Full suite green before commit: `python -m unittest discover`.
- **Do not push and do not open PRs.** *(Stale as of 2026-07-26 — `origin`
  is configured, CI runs four legs on push, and PRs are the norm; see
  `docs/SESSION_HANDOVER.md` and the current session's own plan for the
  actual push/review gate. Left here rather than silently rewritten —
  docs tidy, 2026-08-10.)*
- Never commit anything matching `internal_*.py`, and never put details of the
  proprietary source format into any file. This repository is intended to
  become public.

### 0.6 When a package can't be finished

Finish everything that isn't blocked, commit that, and write what remains and
why into `docs/UPGRADE_PLAN_STATUS.md` (create it; append, don't rewrite).
Do not half-land a schema change: a migration either ships with its backfill,
its tests and its readers, or it does not ship.

---

## 1. Migration version registry

Versions are **pre-assigned here** so that two agents working in parallel cannot
both write "entry 2" and produce a `MIGRATIONS` list that is silently broken
after a merge.

| Version | Package | Change | Backfill? |
|---|---|---|---|
| 1 | *(deployed)* | Initial schema — **frozen** | — |
| 2 | WP-4 | `users.deactivated_at`, `users.deactivated_by` | No |
| 3 | WP-5 | `latest_runs.duration_seconds` | Yes — see §1.2 |
| 4 | Perf pass | Sort indexes on `latest_runs` | No |
| 5 | WP-13 | `environment_expectations` table | No |
| 6 | WP-17 | `activity_hours` table, `runs.output_fingerprint` | Yes — see §1.2 |
| 7 | WP-18 | `script_hours` table *(took 7 from WP-15 — see below)* | Yes — see §1.2 |
| 8 | WP-20 | `environment_products` table *(took 8 from WP-15 — see below)* | No |
| 9 | WP-21 | `streams` table, `runs.stream_id`, `comments.stream_id`, `assignments.stream_id`, `current_assignments.stream_id`, `latest_runs` rebuilt with `stream_id` *(took 9 from WP-15 — see below; the two `assignments`/`current_assignments` columns were folded in after this entry first landed but before this branch shipped anywhere; then WP-25 (docs/ONE_KIND_PLAN.md) amended it AGAIN IN PLACE, same precedent, to narrow `streams.kind` from {mainline, branch, build} to {mainline, build} — the `branch` kind died before it ever shipped anywhere, so this is deletion, not migration. `kind` was never CHECK-constrained, so the DDL is unchanged; only the comment and the application-level validation moved. See the entry's own comment in `storage.py`)* | Yes — see §1.2 (`latest_runs` rebuild; ~12k rows) |
| 10 | WP-23 | `activity_hours`/`script_hours` rebuilt with `stream_id` in their PRIMARY KEY *(took 10 from WP-15 — see below)* | Yes — see §1.2 (both tables rebuilt; a straight copy, not a `runs` re-aggregate — ~1k + ~22k rows on the dev copy) |
| 11 | WP-15 | `run_progress` table *(renumbered from 6, then 7, then 8, then 9, then 10 — see below)* | No |
| 12+ | *unallocated* | Claim by editing this table in the same commit | — |

**Why 6 and 7 swapped (2026-07-30).** Versions must ship contiguously —
`tests/test_migrations.py` enforces it, and a database at version 7 with no 6
behind it could never take 6 retroactively. WP-15's claim on 6 lived only on
the parked `wp-14-in-run-progress` branch, while WP-17 had to ship first, so
WP-17 took 6 and WP-15 moved to 7.

**And why 7 and 8 swapped too (2026-08-04).** The same situation a second
time: WP-18 (Timeline) shipped while `wp-14-in-run-progress` was still
parked, so the ship-first package took the next contiguous number and the
parked claim moved back one. This is now the established pattern — a parked
claim is a RESERVATION, not a number: whatever ships next takes the lowest
unshipped version, and the reservation follows it up. WP-19 (the MariaDB
backend, 2026-08-07) deliberately consumed **no** version: the SQLite schema
is untouched, and the MariaDB schema is only ever created by the migration
tooling, never by the app. (This note originally said the WIP branch must
renumber its migration entry to 8 before merging — **superseded by the next
note**, which moved that target to 9. Left here rather than deleted so the
history of the swap reads in order.)

**And why 8 and 9 swapped too (2026-08-08).** The same situation a third
time: WP-20 (products, drop 1 of `docs/STREAMS_PLAN.md`) shipped while
`wp-14-in-run-progress` was still parked, so the ship-first package took the
next contiguous number (8) and the parked claim moved back one (9). (This
note originally said the WIP branch must renumber its migration entry to 9
before merging — **superseded by the next note**, which moved that target
to 10. Left here rather than deleted so the history of the swap reads in
order.)

**And why 9 and 10 swapped too (2026-08-08).** The same situation a fourth
time, same day: WP-21 (streams, drop 2 of `docs/STREAMS_PLAN.md`) shipped
while `wp-14-in-run-progress` was still parked, so the ship-first package
took the next contiguous number (9) and the parked claim moved back one
(10). (This note originally said the WIP branch must renumber its migration
entry to 10 before merging — **superseded by the next note**, which moved
that target to 11. Left here rather than deleted so the history of the swap
reads in order.)

**And why 10 and 11 swapped too (2026-08-08).** The same situation a fifth
time, same day: WP-23 (long-running branch streams, drop 4 of
`docs/STREAMS_PLAN.md`) shipped while `wp-14-in-run-progress` was still
parked, so the ship-first package took the next contiguous number (10) and
the parked claim moved back one (11). This is the CURRENT instruction:
**when the WIP branch comes back it must renumber its migration entry to
11 before merging.**

**Claiming a version means editing this table in the same commit as the
migration.** An entry here with no migration is fine; a migration with no entry
here is a merge conflict waiting to happen.

### 1.1 Required test (WP-0)

`tests/test_migrations.py` must assert:

1. Versions are unique, contiguous from 1, and ascending.
2. `MIGRATIONS[0]` is byte-identical to the deployed version-1 DDL. Pin it with
   a hash of the normalised statement list, committed as a literal in the test.
   The failure message must say *"entry 1 describes a database that exists in
   production; add a new entry instead"*.
3. Applying every migration to an empty database, and applying migration N to a
   database built at version N-1, produce the same schema. Compare normalised
   `sqlite_master` dumps. This is what stops the two paths drifting.
4. A database whose `schema_version` exceeds `MIGRATIONS[-1][0]` is refused
   (already implemented — pin the behaviour).

### 1.2 Migrations run against a real database

Production is ~900 MB and ~4.4M runs on a network mount. A migration holds one
transaction.

- `ALTER TABLE ... ADD COLUMN` with no default is O(1) in SQLite — safe.
- **A backfill that rewrites every row of `runs` is not safe** and is not
  permitted in a startup migration.
- WP-5's backfill touches `latest_runs` (~12k rows), not `runs`. That is the
  reason it is affordable. Keep it that way.
- Any migration must print progress and its elapsed time to the log. The
  operator needs to know whether a silent five-minute startup is working or
  hung.
- Measure on a copy of production before shipping. State the measured time in
  the migration's comment. Do not estimate.

---

## 2. Work packages

Each package: **why**, **already decided**, **changes**, **tests**, **risks**,
**done when**. User-facing item numbers from the original request are in
brackets.

---

**Collapsed 2026-08-10 (docs tidy).** WP-0 through WP-13 (below) all shipped
and are unpacked in full in `UPGRADE_PLAN_STATUS.md`'s `## State` table and
their own dated drop entries — that is now the record, not this plan. This
table exists as a map, not a spec: what each package changed, one line, with
a status-log pointer.

| WP | Package | What it did | Status-log pointer |
|---|---|---|---|
| WP-0 | Migration registry guard | `tests/test_migrations.py` enforcing §1's four required assertions | "WP-0 \| Migration registry guard \| done \| `tests/test_migrations.py`, 19 tests" |
| WP-1 | Extract the review panel | Pure move of the triage Review expander into shared `static/review.js` | "WP-1 \| Extract `review.js` \| done \| pure move, 887 tests" |
| WP-2 | Review expander on Open actions | Same review panel reused on the Open actions page **[item 2]** | "WP-2 \| Review on Open actions \| done \| 893 tests" |
| WP-3 | Fix the result emphasis in triage | Current result made the visually dominant chip, not the previous one **[item 4]** | "WP-3 \| Triage result emphasis \| done \| 893 tests" |
| WP-4 | Deactivate users *(migration 2)* | Soft/reversible deactivation, `deactivated_at`/`deactivated_by`, blocked while holding assignments **[item 3]** | "WP-4 \| Deactivate users (migration 2) \| done \| migration 2, 869 tests" |
| WP-5 | `latest_runs.duration_seconds` *(migration 3)* | Column maintained at import time; removed the codebase's only `julianday()` call | "WP-5 \| `duration_seconds` (migration 3) \| done \| migration 3, 882 tests" |
| WP-6 | "Where is the time going" tab **[item 5]** | `/api/time`, `static/time.html`/`time.js`, bar-row drill-down by environment/script/test | "WP-6 \| Time analysis tab \| done \| `/api/time`, 908 tests" |
| WP-7 | Sortable table columns **[items 6, 9]** | Server-side sort on Open actions/`DASHBOARD_SORTS`, client-side elsewhere, shared `sorting.js` | "WP-7 \| Sortable columns \| done \| `sorting.js`, 917 tests" |
| WP-8 | Last pass + "broke or flaky" **[items 7, 8]** | `Failing since`/`Last pass` columns, run-strip flakiness signal, `with_streak=1` | "WP-8 \| Last pass + flaky signal \| done \| `with_streak=1`, 936 tests" |
| WP-9 | MariaDB portability groundwork **[item 1, code half]** | SQLite-specific-construct inventory test, funnelled upsert, id-stability pin | "WP-9 \| SQL portability groundwork \| done \| inventory + id pins, 947 tests" |
| WP-10 | The MariaDB export tool **[item 1, transport half]** | `tools/export_for_mariadb.py`, escaping round-trip tests | "WP-10 \| MariaDB export tool \| done \| `tools/export_for_mariadb.py`, 980 tests" |
| WP-11 | Vendor the database driver **[item 1, dependency half]** | PyMySQL 1.0.2 vendored under `third_party/pymysql`, parse-gate + license tests | "WP-11 \| Vendor PyMySQL \| done \| `third_party/pymysql` 1.0.2, 13 tests" |
| WP-13 | Declared environment expectations *(migration 5)* | `environment_expectations` table, `/api/environments`, overrides the inferred coverage denominator (WP-12, the prerequisite "recently run" fix this depends on, shipped even earlier and predates this document's WP list) | "WP-13 \| Declared environment expectations \| done \| `8c10de7`, `0ee7c1b` on `main`" |

File-ownership/lane-sequencing detail for running WP-0…WP-13 concurrently
(now moot — all shipped) is cut; if a future round needs the same kind of
parallel-package scheduling, write a fresh one rather than reusing this
one's file list.

---

### WP-14 — in-run progress **(depends on WP-13)**

**Why.** One environment takes 2.5 hours and there is no visibility while it
runs. `find_passes` already computes runs-so-far per environment against an
expected total — which is exactly a progress bar, and the numbers are already
being fetched.

**Already decided.**

- **No new query.** It reads the passes WP-13 already computes from
  `activity_buckets` (hour buckets, 14-day floor). §0.4's rule is satisfied by
  reusing a bounded query rather than justifying a new one.
- **"Running" is a shorter idle threshold than a pass boundary.** A pass ends
  after `_PASS_GAP_HOURS` (6h) of quiet, so treating "the latest block has not
  closed" as "still running" would show a finished 2.5-hour environment as in
  progress for another six. Judge it on recent activity instead, and say
  "finished at HH:MM" once it is done.
- Surfaced on the dashboard home, where people look in the morning, not on the
  admin page.

**Risks.** An environment with no declared expectation has an inferred
denominator that is a high-water mark, so its bar can sit at 80% forever. Show
the number alongside the bar, and let a declaration be the fix — that is what
WP-13 is for.

---

### WP-15 — accept progress pushes from a partial reader *(migration 11 as of 2026-08-08 — renumbered five times, see §1; this heading originally said 8, then 9, then 10 as each renumbering landed — §1 is the current source of truth)*

**Why.** WP-14 counts *imported runs*, and the in-house reader cannot produce a
full run record mid-pass: during the night it has test identities and results
but **no per-test timings**. It does know when the environment's run started,
and the final push upserts everything with full detail.

So as built, WP-14 shows a flat bar all night and then a jump to 100% — the
exact blindness it was written to remove. This is the package that makes it
work against the reader that actually exists.

**Already decided.**

- **`/api/import` does not change.** It is a fixed contract shared with the
  feeder (README), and it *cannot* carry these records anyway: run identity is
  `(environment, script, test_name, start_time)`, so a record with no start
  time has no identity. Synthesising one — the environment's start, say —
  means the final push, carrying the real per-test time, writes a **second
  row** rather than updating the first. One duplicate per test per night, in
  the table whose whole design assumes one row per test per start.
- **Partial records therefore never become `runs` rows.** New table
  `run_progress`, keyed `(environment, script, test_name)` — one row per
  in-flight test, because a test is only in one pass at a time:
  `result`, `pass_started`, `reported_at`.
- **New endpoint `POST /api/progress`:**
  `{"environment", "pass_started", "tests": [{"script", "test_name",
  "result"}]}`. Idempotent upsert, so snapshots and deltas both work and the
  reader may simply resend the whole list. Same per-record tolerance as
  `/api/import` — one bad record never aborts the batch; log, skip, count —
  and the same response shape, so the feeder's error handling is unchanged.
- **A progress push names only tests that have COMPLETED** (confirmed with the
  user, 2026-07-28). There is no "started but not finished" state, so `result`
  is a plain `Result` and the enum does not grow. Tests not yet named are
  simply not yet reached.
- **Reconciliation reuses the retirement precedent.** `_unretire_on_new_run`
  already clears a retirement inside the import transaction when a test reports
  again. Same shape: importing a real run for a test **deletes its progress
  row in the same transaction**. A test is then in exactly one of the two
  places, so `runs_so_far = provisional + real` cannot double-count and the
  normal path needs no cleanup job.
- **Abandoned passes drain themselves.** A push carrying a newer
  `pass_started` for that environment drops that environment's older rows; a
  push older than the newest recorded is ignored (out-of-order); anything past
  the `_PASS_LOOKBACK_DAYS` floor is ignored. A killed run clears on the next
  night rather than lingering for ever.
- **A provisional result is NOT "the latest result".** It has no timings and
  may be superseded, so it stays out of `latest_runs` and out of every estate
  view: triage queues, failing-since, flakiness, the staleness cutoff, and
  WP-13's coverage denominator all keep reading completed, timed runs only.
  This is the line that keeps the package additive; crossing it would put
  untimed, provisional data into the tables every analytic trusts.
- **`find_passes` keeps reading `runs`.** Coverage decides the staleness
  cutoff, which gates the offer to retire a test. It stays on completed data.
- Two consequences in `latest_progress`, which are the actual code:
  1. It must **synthesise an entry from `run_progress` alone.** An environment
     whose pass is entirely provisional has nothing in `activity_buckets`, so
     today it would produce no row at all — the bar would be missing exactly
     when it is wanted. `pass_started` supplies the start, the row count the
     progress, `reported_at` the freshness.
  2. **`running` must consider `reported_at`**, not only the hour buckets over
     `runs`. During a provisional pass `reported_at` is the only fresh signal
     there is.
- Retired tests are excluded from the numerator, as WP-13 excluded them from
  the denominator. Otherwise a bar can exceed its own total.

**OPEN (default given).** Show provisional failures as a count beside the bar
("14 failing so far"). Default: **yes** — it is the thing worth knowing at 3am
and it costs one more column in the same query. **Not** in the triage queues,
which need timings and stability history.

**Note for later, not this package.** `pass_started` is the first real batch
identifier this system has had; WP-12 and WP-13 infer pass boundaries from gaps
precisely because "the import contract has no session/batch id". Do **not** rip
the inference out: the final push still carries no identifier and history still
needs it. But for a live pass the start is now known rather than guessed.

**Changes.** `testboard/storage.py` (migration 6; `upsert_progress`,
`progress_counts`, the delete inside `_maintain_latest`),
`testboard/analytics.py` (`latest_progress` takes provisional counts),
`testboard/api.py` (`POST /api/progress`), `static/app.js` (the failing-so-far
count), README (document the new endpoint beside the import contract).

**Tests.** Storage: upsert idempotency; a real import clears the progress row
in the same transaction; a newer `pass_started` drops older rows; an older one
is ignored; retired tests excluded. Per §0.4 the progress read needs a cost
test — it is a `GROUP BY environment` over at most one row per test, the same
shape as `test_counts_by_environment`, and must never touch `runs`. API:
validation, per-record tolerance matching `/api/import`, unknown result → that
record rejected and the rest accepted. Analytics: an environment with only
provisional data still produces a progress row; `running` driven by
`reported_at`. **And the one that matters most:** a provisional row changes
nothing in `/api/summary`'s queues, `stale_before`, or the WP-13 coverage
verdict.

**Risks.** The temptation will be to let provisional results feed the queues,
because a failure known at 3am is worth seeing. Resist it in this package: those
rows carry no timings, and `failure_streak_bounds`, the flakiness window and the
duration sort all assume they exist. If it is wanted later it is its own
package, with its own argument about what a run without an end time means.

**Done when.** A reader that can push only identities and results mid-pass
produces a live progress bar, the final full push supersedes every provisional
row it covers, and nothing else in the dashboard can tell the difference.

---

### WP-16 — site-specific info tab *(noted, not specified)*

Raised by the user on 2026-07-28 as a note for later, with no content
defined yet. Recorded here rather than lost in a conversation; **do not
start building it from this paragraph.**

**What is known.** A tab, alongside Dashboard / Open actions / Time /
What's new, carrying information specific to this site — the things a
newcomer or an out-of-hours person has to ask someone for today.

**What is not known, and has to be answered before any code.** What goes
on it, and whether it is static text or reads from the database. Those
are different pieces of work: static content is `whatsnew.html` again
(a page, a nav entry, no API), whereas anything derived — who owns which
environment, where the logs live, which contacts cover what — wants a
table and an editing surface, which is WP-13's shape and considerably
more.

**The one structural thing to remember.** The nav is duplicated across
**six** static HTML files now (`index`, `actions`, `script`, `test`,
`time`, `whatsnew`). A new tab must be added to all six or people reach
it once and never find it again. Grep for `site-nav`.

**Default if it is never specified further:** a static page, following
`whatsnew.html` exactly, with content the user supplies. That is an
hour's work and can be replaced by a data-driven version later without
anything having to be undone.

---

### WP-17 — summary performance: `activity_hours`, the no-op re-import, and the parts split *(migration 6)*

Shipped 2026-07-31; the full analysis and measurements are in
`UPGRADE_PLAN_STATUS.md` (2026-07-30 entry) and the drop's operator note.
Summarised here because three of its decisions constrain later work:

- **`activity_hours` is the third derived table** (environment × UTC hour ×
  result → run count), maintained inside the import transaction. It exists
  because the staleness cutoff's bucket query was answered with a full scan
  of the runs UNIQUE index — the only read whose cost grew with total
  history, measured at 3.5s mean on production and 70% of `/api/summary`.
  Reads that need a window of run activity go through it; nothing new may
  scan a window of `runs` at request time.
  `tests/test_storage.py::ActivityHoursTest` holds it byte-equal to the
  GROUP BY it replaced.
- **A byte-identical re-import writes nothing.** The site feeder re-pushes
  its whole recent window every 10 minutes; before the skip that was ~23 MB
  of WAL per push for zero information — and it silently un-retired tests,
  which made retirement impossible to keep. `runs.output_fingerprint`
  (SHA-1 of the output text, NULL = pre-migration row, self-heals in one
  push) is what makes "identical" cheap to decide. The import response
  gained an additive `unchanged` field; **on the wire `updated` still
  includes unchanged records**, so deployed feeders' arithmetic holds.
- **`/api/summary` serves parts.** `parts=headline` (everything but queue
  rows, plus `queue_totals`), `parts=queue&queue=<kind>` (one queue's
  rows); no parameter = the full pre-split payload, which
  `tests/test_api.py::SummaryPartsTest` pins the parts to. The home page
  fetches headline + active tab only; other tabs on click.

### WP-18 — Timeline: script running order per environment *(migration 7)*

Shipped 2026-08-04; measurements in `UPGRADE_PLAN_STATUS.md` (2026-08-04
entry) and the drop's operator note. The problem: scripts share the system
they test and are not run in a dependency-safe order, so a script that
leaves static data dirty surfaces as a LATER script's test failing. Tracking
the culprit needs the night's script running order, which no test-centric
view can show.

What was decided, because it constrains later work:

- **`script_hours` is the fourth derived table** (environment × UTC hour ×
  script × result → count, plus exact `first_start`/`last_end` per bucket),
  maintained inside the import transaction. The span columns give sub-hour
  ordering without touching `runs` at request time; the PK leads
  `(environment, hour)` so a window read is a pure index range. A MIN/MAX
  cannot be decremented, so the rare shrink paths (a re-import changing a
  stored result or end time — the fingerprint skip makes these exceptional)
  recompute their buckets from `runs` exactly.
  `tests/test_storage.py::ScriptHoursTest` holds the table byte-equal to
  its GROUP BY, including the span columns.
- **A Timeline row is a script *execution*, not a script** — grouped by the
  same 60-minute gap rule as `analytics.group_executions`, so re-runs are
  separate rows and a partial run is a row whose count reads short against
  the script's `latest_runs` test count ("41 of 45 known tests" — "known",
  not "expected": it is a high-water mark).
- **Endpoints**: `GET /api/timeline?environment&days[&from&to]` (blocks from
  the same `find_passes` machinery the environments page uses — ad-hoc
  blocks included, labelled by `covered`; rows for the selected window) and
  `GET /api/scripts/{env}/{script}/runs?from&to` (one execution's runs, the
  row expansion). Window edges are inclusive at hour resolution,
  deliberately: block edges are bucket starts, and trimming against the
  exact edge would drop whatever ran in the block's final hour.
- The block picker and every caption are worded from actual timestamps,
  never "last night" — `WindowWordingTest` now scans `timeline.js` too.

### WP-19 — the MariaDB backend (§F of the runbook) *(no migration)*

Built 2026-08-07, folded into the same drop as WP-18; measurements and the
build story in `UPGRADE_PLAN_STATUS.md` (2026-08-07 entries). Consumes **no
migration version** — the SQLite schema is untouched and the MariaDB schema
only ever comes from the migration tooling.

What was decided, because it constrains later work:

- **One `Storage`, two backend objects** (`_SqliteBackend` in `storage.py`,
  `MariaDBBackend` in `testboard/mariadb.py`, reached only via
  `Storage.mariadb()`); the SQL stays qmark-canonical **permanently** and
  the MariaDB wrapper translates at execute time. The whole dialect surface
  is three rewrites + two composed fragments; each is pinned by a unit test
  and enumerated in the runbook's §F.2.
- **SQLite is a permanent first-class backend**, the zero-setup default —
  never to be described as legacy. `run_server.py --db` is unchanged;
  `--db-config` (mysql option file, §A.10) selects MariaDB and *requires*
  `--site-notes`; wrong flag combinations are refused with reasons.
- **The app never runs DDL on MariaDB** — `schema_version` equality is
  verified and both mismatch directions refuse.
- **CI runs both backends on every push**: the `python36-mariadb` job (ubi8
  python-36 + `mariadb:10.3`, prod's stream) activates generated
  dual-backend suites via `TESTBOARD_TEST_DB_CNF`; the test schema is built
  from the exporter's own DDL. SQLite-only instruments (PRAGMA, trace
  callbacks, one perf pin) are skipped there with reasons recorded in
  `tests/test_mariadb_backend.py`.
- The vendored-driver guard evolved as its docstring instructed:
  `NotYetWiredTest` → `DriverImportAllowlistTest`; the driver's
  serving-path blast radius is exactly `testboard/mariadb.py`.

**Collapsed 2026-08-10 (docs tidy).** This section scheduled concurrent work
across lanes (A: schema/backend, B: frontend, C: features, D: independent)
for WP-0…WP-13, plus a file-ownership collision table for the same set —
all of it moot now that every package in it has shipped. One live fact it
carried has moved: WP-15 still shares `storage.py` and the migration
registry with any other in-flight schema work, and its claimed version is
**11**, not the 8 this section used to say — see §1, which is kept current.
If a future round runs several packages concurrently again, write a fresh
lane plan rather than reviving this one's WP-0…WP-13 file list.

---

## 4. Open decisions for the user

Work is not blocked on any of these — each has a default that will be built if
no answer arrives.

1. ~~**Vendored MySQL driver (runbook §F).**~~ **DECIDED 2026-07-27: vendor it.**
   PyMySQL goes into the tree so that nothing is installed on site. See WP-11.
2. **Username merge (WP-4).** Deactivation hides the duplicate account but its
   comments and assignments stay under the old name. Default: manual reassign,
   no merge feature.
3. **Sort Open actions by comment time (WP-7).** Default: omitted this round.
4. **Run-strip placement (WP-8).** Default: sentence in the row, strip in the
   panel.
5. **Time analysis window (WP-6).** Default: latest run per test only. A
   historical window is a bigger piece of work and needs its own aggregate
   table.

---

## 5. What is deliberately not in this round

- **Any change to the `/api/import` transport contract.** It is a fixed contract
  shared with the feeder and is documented in the README. Nothing here needs it.
- **Historical time aggregation** (see WP-6).
- **A username merge tool** (see WP-4).
- **The MariaDB driver port itself** (see WP-9 and the runbook).
- **Publishing the repository as open source.** *(The "no remote configured"
  premise is stale — see the §0.5 note above — but whether the repo goes
  fully public, and whether commit authorship needs changing first, is
  still the user's call and still undecided as of this docs tidy,
  2026-08-10.)*
- **Re-measuring the storage verdict.** `tools/diagnose_db.py --compare-local`
  has still not been run on the production server *since* the worker-pool fix.
  Every timing taken before that fix was taken against a server that discarded
  its page cache on every request, so those numbers describe a system that no
  longer exists. This matters to the MariaDB decision and is a one-command job —
  see the runbook's opening section.
