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
| WP-0 | Migration registry guard | **done** | `tests/test_migrations.py`, 19 tests |
| WP-11 | Vendor PyMySQL | **done** | `third_party/pymysql` 1.0.2, 13 tests |
| WP-4 | Deactivate users (migration 2) | **done** | migration 2, 869 tests |
| WP-5 | `duration_seconds` (migration 3) | **done** | migration 3, 882 tests |
| WP-1 | Extract `review.js` | **done** | pure move, 887 tests |
| WP-2 | Review on Open actions | **done** | 893 tests |
| WP-3 | Triage result emphasis | **done** | 893 tests |
| WP-7 | Sortable columns | **done** | `sorting.js`, 917 tests |
| WP-6 | Time analysis tab | **done** | `/api/time`, 908 tests |
| WP-8 | Last pass + flaky signal | **done** | `with_streak=1`, 936 tests |
| WP-9 | SQL portability groundwork | **done** | inventory + id pins, 947 tests |
| WP-10 | MariaDB export tool | **done** | `tools/export_for_mariadb.py`, 980 tests |
| — | Performance pass | **done** | migration 4, 952 tests |

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

### WP-0 — migration registry guard — **done**

`tests/test_migrations.py`, 19 tests. Migration 1 is pinned by a
whitespace- and comment-insensitive SHA-256 of its DDL
(`9b9dd4d0…`). Suite 808 → 827.

Two things worth knowing if this file is ever revisited:

- The first version of the fingerprint collapsed runs of whitespace but not
  spacing *around punctuation*, so re-indenting the DDL changed the digest.
  A freeze that trips on reformatting gets its constant updated as routine
  maintenance and stops meaning anything. Caught by
  `test_the_fingerprint_ignores_formatting_but_not_content`, which exists for
  exactly that reason. The digest changed when it was fixed.
- The planted regression was run for real, not just asserted at string level:
  a `planted_column TEXT` added to entry 1 in `storage.py` fails
  `test_migration_one_matches_what_was_deployed`. Verified, then reverted.

### WP-11 — vendor PyMySQL — **done**

`third_party/pymysql` 1.0.2 (last release supporting 3.6; 1.1.0 raised its floor
to 3.7). MIT, pure Python, no dependencies of its own, 18 files. Wired to
nothing — a test enforces that, so reverting stays a one-commit operation.
Suite 827 → 844.

Four things found on the way that are not obvious from the diff:

- **`cryptography` is an optional PyMySQL dependency** needed for
  `sha256_password` / `caching_sha2_password`. It is compiled, so vendoring it
  would destroy the "nothing to build on the server" property. MariaDB defaults
  to `mysql_native_password` and needs none of it — but the DB account must be
  created with that plugin. Added to the runbook §A.2 and its troubleshooting
  table, because the error message names a Python package and the fix is a SQL
  grant.
- **`paramstyle` is `pyformat`, not `format`.** The plan said `format`. Pinned
  by a test, because a stray `?` reaches MariaDB as a literal question mark
  rather than failing loudly.
- **The PEP 604 detector false-positived on vendored code.** Its
  module-level-assignment arm cannot distinguish `Number = int | float` from
  `CAPABILITIES = LONG_PASSWORD | LONG_FLAG | ...`, and PyMySQL's constants
  modules are full of the latter. That arm is now optional and off for vendored
  code; the gap it leaves (a vendored type alias using `|`) is caught by the
  ubi8/python-36 CI job, where it is a TypeError at import.
- **The "opens no socket at import" test could not be written as a
  monkeypatch.** Replacing `socket.socket` breaks `ssl.SSLSocket`, which
  subclasses it, so the test failed inside the standard library before reaching
  the driver. It uses `sys.addaudithook` instead, plus a companion test proving
  the hook actually fires on a real network call.

### WP-4 — deactivate users — **done**

Migration **2**: `users.deactivated_at`, `users.deactivated_by`. Presence of the
timestamp is the state, matching `test_retirements` — reversible, and no boolean
that can disagree with its own timestamp. Suite 844 → 869.

**Migration timing, measured not estimated.** Applied to a copy of the real
database (218 MB, 540,192 runs, 12,008 tests): **31 ms**, including opening the
connection. `ALTER TABLE ADD COLUMN` does not rewrite rows in SQLite, so this
does not grow with the database — production being larger changes nothing.
Verified afterwards: row counts unchanged, existing users read as active.

Exercised against that copy through the running server, not only through tests.
The 409 fired on real data — `priya` genuinely owned two tests — then reassigning
them let the deactivation through, the picker list dropped to two names, and an
attempt to assign to the deactivated account was refused.

Decisions worth not re-litigating:

- **Deactivating an owner is a hard 409, not a warning.** Work assigned to a
  name no picker offers is an invisible queue; nothing would ever surface it.
- **Retired tests do not count as open work.** Retirement deliberately leaves
  the assignment in place, so counting them would block deactivation forever
  over work that no longer exists.
- **Clearing an assignment is never blocked** — it is the way out of the
  situation, not another instance of it.
- **`assigneeSelect` injects `entry.assignee` even when absent from the fetched
  list, and that is deliberate.** With active-only listing, a test still owned
  by a deactivated account would otherwise render with an empty dropdown and
  look unassigned. There is now a comment there saying so.

Two things found by running it rather than by testing it:

- The `<details>` panel loaded its list on the `toggle` event only. `toggle`
  fires on a *change*, so a panel that is already open on arrival — markup, or
  browser-restored state — showed an empty table forever. Now it also loads if
  it is open at init.
- `tests/test_frontend_calls.py` fired, correctly: `actions.js` now names
  `/api/users` twice more. **Widened, not weakened** — mutations (`putJson`) and
  the admin roster (`include_inactive=1`, a different result set, fetched lazily
  and once) are exempt; a second fetch of the *assignable* list, which is what
  caused the original 250-request stampede, is still banned. A new test keeps
  the exemption narrow.

### WP-5 — `latest_runs.duration_seconds` — **done**

Migration **3**: the column, maintained in `_maintain_latest`, backfilled over
`latest_runs` only. `DASHBOARD_SORTS["duration"]` repointed; `julianday()` gone.
Suite 869 → 882.

**Migrations 2+3 against a copy of the real database** (218 MB, 540,192 runs,
12,008 tests): **466 ms** total. Backfill verified against the source of truth —
0 rows left at the placeholder default, 0 mismatches in a 2,000-row sample
recomputed from `runs`.

**Migrations can now contain Python steps**, written as `"python: <name>"` and
dispatched through `storage.apply_migration_statement`. The backfill has to use
`model.duration_seconds` — the same function the API serialises with, or a stored
duration and a displayed one could disagree — and doing it in SQL would have
meant `julianday()`, which is the thing being removed. The list stays all
strings so the entry-1 freeze can still hash it and a human can still read it,
and the tests build databases through the same dispatch function so they cannot
diverge from the real runner.

**A measurement that contradicted the plan, recorded because it did.** The plan
justified this partly as a speed win for the duration sort. Measured: **1.1x**,
not the "stops evaluating an expression over the whole filtered set" the first
version of the comment claimed. The `julianday` call was never the bottleneck.

- Same query without `ORDER BY`: **3.9 ms**. With it: **155 ms**. The sort is
  ~97% of the cost.
- An index does **not** help and is not even used. Every `DASHBOARD_SORTS` entry
  orders its first column in the requested direction and the primary-key
  tiebreak ASC, so a descending sort needs a mixed-direction ORDER BY, which no
  all-ASC index can serve. Measured with a composite index in place: 0.98x, plan
  unchanged (`USE TEMP B-TREE FOR ORDER BY`).

So the real justifications for this column are portability (julianday gone) and
WP-6 (a GROUP BY over 12k rows instead of millions). **Per-direction indexes for
every sort key is a separate piece of work — logged for the performance pass.**

### WP-1 — extract the review panel — **done**

`static/review.js`. Suite 882 → 887.

**Kept strictly a pure move.** Three improvements got written into the new module
while moving it — Enter-to-post a comment, refreshing after a comment, and an
error instead of a silent return when retiring with no username set — and all
three were reverted. A behaviour change hiding inside a 200-line move is
invisible, and the point of a pure move is that its diff can be checked as a
no-op. They belong in WP-2, where they are the subject rather than a passenger.

Deviations from the plan, both deliberate:

- The exported function is `toggleReview`, not `attachReview`. It toggles.
- `reviewCell()` was drafted and dropped: the two call sites have different
  button tooltips, so using it would have been a behaviour change (see above).

The coupling is broken by injection. `review.js` is told a `staleBefore`
timestamp and works out staleness itself, rather than calling back into the home
screen's `isStale`. A test asserts the module never names `state.`,
`refreshSummary` or `refreshQueueCounts`.

Verified in a browser, not only by tests: a temporary probe page imported the
module, called `toggleReview` against a real failing test from the production
copy, and the panel opened, fetched the run output and rendered its actions. The
home screen was then screenshotted intact.

Two guard tests fired and were **widened, not weakened**:

- `test_files_containing_a_nul_byte_are_still_read` asserted on `app.js`
  specifically. The `\0` composite-key separator moved to `review.js` with
  `entryKey`, so the test failed for the move rather than for the property. It
  now asks the question it cares about — at least one scanned file contains a
  NUL and was read anyway — and fails if none does, so it cannot go vacuous.
- The new "panel reads no page state" check matched the word `state.` in
  English prose ("the row's expanded state."). It now strips comments first,
  with its own test proving the stripper removes prose and keeps code.

### WP-3 — triage result emphasis — **done**

Suite 887 → 893. Screenshots before/after taken against the production copy.

The reported bug was real and it was a visual-encoding defect, not a preference.
In `new_failures` and `fixed` the *previous* result was a full solid chip in its
own column while the *current* result appeared only as a 3px stripe on the row
edge. The loudest thing in the row was the wrong value — and wrong in the
misleading direction in both queues, since a new failure's previous run is
usually PASS and a fixed test's previous run is FAIL.

Both queues now show one `Result` column reading `PASS → FAIL`: the superseded
value as an outlined ghost chip, the current one solid, in time order so the eye
finishes on what is true now. Both keep their text labels, so nothing is carried
by colour alone.

**Scoped to the two queues that actually had the bug**, per the plan's
correction. `still_failing` is FAIL on every row and `unexpected_passes` is
UNEXPECTED_PASS on every row; a per-row chip there is a column of identical
values, which is more of the noise this item is about. Those state it once above
the table (`QUEUE_INVARIANT_RESULT`) and carry no result column. `not_run`
already had one and was left alone.

### WP-2 — review expander on Open actions — **done**

`actions.js` imports the shared panel. Suite unchanged at 893 (the behaviour is
covered by the WP-1 guards plus the browser check).

The three improvements deliberately held back from WP-1's pure move land here,
where they are the subject: Enter posts a comment, the panel reports what
changed, and retiring with no username set says so instead of silently doing
nothing.

**Rows are patched in place; the page is not refetched.** The old `onSaved`
handler called `refresh(false)`, which refetches 100 rows and rebuilds the
table — closing every open panel. On a queue somebody is working down, that
throws away what they were reading the moment they act on it. `onChanged` now
carries `{kind, value}`, so the assignee cell and the comment cell are rewritten
from data already in hand and no request is made at all. Retirement still
reloads, because it removes the test from every estate view.

One ordering detail worth keeping: `reopenIfOpen` runs after the row is appended
to the table, not inside `buildRow`. It inserts a sibling row, which needs a
parent to insert into.

### WP-6 — where the time goes — **done**

`GET /api/time` + `static/time.html` / `time.js`. Nav added to all four existing
pages. Suite 893 → 908.

Scoped to the newest run of each test, as planned: a `GROUP BY` over ~12k rows,
and a test asserts the query plan never touches `runs`. `group_by` is whitelisted
(`_DURATION_GROUPS`) because a GROUP BY column cannot be a bound parameter —
the same rule as `DASHBOARD_SORTS`, and tested with `"; DROP TABLE runs"`.

Form: horizontal bars (reusing `barRows`), breadcrumb drill-down, data table
alongside. One hue, deliberately **not** the result palette — this is magnitude,
and borrowing the pass/fail colours would have people reading "red = bad" into a
bar that only means "slow".

**A design flaw found by running it, not by testing it.** The stale-test
exclusion is right in principle — counting a test that last ran three weeks ago
claims time that was not spent — but as an all-or-nothing cutoff it rendered
"0.0s across 0 tests" on the production copy, whose data is older than the 36h
window. That is not a test-data artefact: any long weekend, CI outage, or
Monday-afternoon visit produces the same dead page. Fixed with an explicit
`include_stale` opt-in: the honest filter stays the default, the empty state
explains *why* it is empty and points at the toggle, and turning it on says
plainly that some of the time shown was not spent recently.

`formatDuration` grew hours. Totalling a suite produced "520m 22s", which is
correct and which nobody reads as "most of a working day".

`static/sorting.js` landed here rather than in WP-7 because this page needed it
first. WP-7 adopts it elsewhere.

### WP-7 — sortable columns (items 6 and 9, the same item) — **done**

Suite 908 → 917. `static/sorting.js` is the one implementation; a test enforces
that.

The split the plan insisted on, unchanged:

| Table | Sorted | Why |
|---|---|---|
| All tests | server (already) | paged |
| Open actions | **server** | paged — 100 of 148 |
| Triage queues | **client, only under the cap** | capped slice of a larger set |
| Time | client | holds the whole level |

Open actions re-sorting returns to offset 0: a re-ordered list has a different
first page, and keeping the offset would show an arbitrary slice of it.

**Triage sorting disables itself when the queue is truncated.** Below
`_SUMMARY_QUEUE_CAP` the browser holds the whole queue and reordering it is
honest; past it, "the oldest failure" would mean "the oldest among the 500 that
happen to have been sent". The controls grey out with that explanation rather
than silently reordering part of a queue.

`Latest comment` is deliberately not sortable anywhere. It needs a whitelist
entry and a join that does not exist; a header that looked sortable and quietly
ordered by something else would be worse than one that does not sort.

The load-bearing test is `TestSortingIsStable`: every key, both directions,
three pages, asserting 40 rows come back with no repeats. That is what catches a
missing primary-key tiebreak — a sort on `result` (four distinct values estate-
wide) otherwise lets SQLite order ties differently between pages, so a row shows
up twice and another never, with no error anywhere.

### WP-8 — last pass, and broken vs flaky — **done**

`GET /api/dashboard?with_streak=1` adds `failing_since`, `last_pass_time` and a
`stability` block; Open actions gains a **Last pass** column carrying the date
plus a plain sentence, and the review panel shows a 20-run strip. Suite 917 →
936.

Item 8 is the reason item 7 is not enough on its own: a last-pass date cannot
separate "broke on the 14th and has failed every night since" from "fails about
one night in three", and those need different responses — bisect a regression,
or stabilise a test.

`analytics.stability_of` classifies a bare result sequence and **shares its
definition of a transition with `_compute_flakiness`**, the detail page's
version. A test asserts the two agree across five patterns; two definitions of
"flaky" that can disagree would be worse than one imperfect one.

**Cost is bounded by the page, and measured.** Against the production copy
(12,008 tests, 540k runs): plain page 10 ms, with streaks 69 ms at 148 rows —
+14 ms at 10 rows, +63 ms at 100. Flat in estate size.

`Storage.recent_results` batches identities 100 at a time, so 250 tests is
**three** queries rather than 250 — asserted directly by tracing the connection.
Batching is not stylistic: SQLite caps a statement at 999 bound parameters and
each identity triple costs three. Duplicate triples are collapsed first.

The stability window is 30 days / 20 runs, deliberately shorter than the detail
page's 90: this answers "what is it doing lately", and a quarter of history
would bury a test that broke this week.

Placement follows the plan's default — sentence in the row, strip in the panel.
Rows are already dense, and WP-3 exists because one got visually noisy.

### WP-9 — SQL portability groundwork — **done**

`tests/test_sql_portability.py`, 11 tests. Suite 936 → 947.

An inventory of the SQLite-specific surface, counted against a committed
expectation, so a tenth construct is a test failure rather than a 3am surprise —
and so the runbook's §B translation table cannot silently go stale.

**It corrected the runbook.** §B.5 warned that `INSERT OR REPLACE` →
`ON DUPLICATE KEY UPDATE` is a behaviour change that would churn `runs.id`.
Checking rather than assuming:

- `runs` **never** uses `INSERT OR REPLACE`. The import path does
  SELECT-then-UPDATE-or-INSERT precisely so ids survive, and a test now pins
  that — including that `run_outputs` and `latest_runs` follow the repair.
- The only two uses are `run_outputs` and `test_retirements`. Neither has a
  generated id and nothing references either, so the two upsert forms are
  indistinguishable from outside.

Not the blocker it looked like. The test still fails if a third appears on a
table where it *would* matter.

Placeholders are pinned too (`?` count > 50, `%s` count 0), because a leftover
`?` reaches MariaDB as a literal question mark rather than failing loudly.

### Performance pass — **done**

Profiled every endpoint warm against the production copy (12,008 tests, 540,192
runs). Suite 947 → 952.

| Endpoint | Before |
|---|---|
| `/api/dashboard?sort=duration&order=desc` | 204 ms |
| `/api/dashboard?sort=start_time&order=desc` | 183 ms |
| `/api/summary` | 197 ms |
| `/api/dashboard` (default order) | 35 ms |
| `/api/time` | 29 ms |

The sorted pages were the outlier and are now **2.6–8.1 ms** (migration 4).

**Two wrong diagnoses on the way, both caught by measuring the real query.**

1. I first concluded the ORDER BY was mixed-direction (`col DESC, pk ASC`) and
   built `DESC`-first indexes. The real ORDER BY descends on *every* column
   together, so those indexes matched nothing and changed no plan.
2. My benchmark query omitted the `LEFT JOIN test_retirements` the real query
   carries. The hand-written lookalike happily used an index that the actual
   query could not.

The fix is **two plain ascending composite indexes** covering all four cases —
ascending pages read forwards, descending pages read backwards. `_plan()` in the
test now captures the SQL `dashboard()` actually issues via a trace callback,
because a lookalike query would keep passing after the real one changed.

**The trade is measured, not asserted.** Every index is maintained per upserted
test and a nightly import touches all 12,008: **4.42s → 10.16s (+130%, +5.7s)**.
A four-index version cost +215% for no further benefit. +5.7s of unattended
batch time for ~170 ms → ~3 ms on a page people load repeatedly — and far more
than 170 ms on the production mount, where the temp B-tree's page reads are
round trips.

**Not done, and worth knowing.** `/api/summary` is still ~197 ms, of which
`summary_rollup` is 42 ms (a GROUP BY over all 12k rows — inherent) and most of
the rest is the per-entry streak lookups for the `still_failing` queue. That is
the next thing to look at, and it needs a design decision rather than an index:
either cache the rollup or denormalise the streak.

### WP-10 — MariaDB export tool — **done**

`tools/export_for_mariadb.py` + 28 tests. Suite 952 → 980.

Needs **no MySQL driver**: reads SQLite read-only, writes text, and the `mysql`
client loads it. That is what keeps the data migration inside the "nothing
installed on the server" constraint, and it is why this half could be written
and tested with no MariaDB anywhere near the machine.

Run against the production copy: **552,200 runs exported in 13.4s**, 148 MB out
of a 223 MB database. Parsed back and compared to the source: **0 mismatches
across all 552,200 rows**, and 0 across 5,000 sampled blobs.

Covered: escaping (tabs, newlines, backslashes, non-ASCII, empty-vs-NULL),
hex round-trip for blobs, foreign-key load order, indexes created after the
load, and that the verification queries run on SQLite and use nothing
engine-specific. The DDL test asserts it carries migrations 2 and 3, because
exporting into the launch-day schema would drop the new columns silently.

**The load half is not verified and the docstring says so.** No MariaDB here or
in CI. The runbook's dry run (§E.1) is what checks it.

---

## Round complete

All nine requested items are implemented, plus the MariaDB groundwork. **980
tests, green.** Every package is committed separately with its reasoning.

Still open, and needing a person rather than a commit:

1. `tools/diagnose_db.py --compare-local` on the production server — still never
   run since the worker-pool fix.
2. `/api/summary` at ~197 ms (see the performance-pass entry) — needs a design
   decision, not an index.
3. The MariaDB migration itself: run §C's audit on production, get §A done by
   whoever holds root, then the dry run.
4. Nothing has been pushed; no remote is configured.
