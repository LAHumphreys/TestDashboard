# Upgrade round 1 — status

Running log for [`UPGRADE_PLAN.md`](UPGRADE_PLAN.md). **Append, never rewrite.**

This file exists so work can be resumed cold, by someone (or something) with no
memory of the session that started it. If you are picking this up: **read
[`SESSION_HANDOVER.md`](SESSION_HANDOVER.md) first** — it is one screen of current
state, and it is rewritten rather than appended to, so it is not buried under
history the way anything in this file eventually is. Then read the plan, read the
state table below, run `git log --oneline`, run the suite, and take the first
package whose state is not `done`.

This file is the **log**: what was done, what was measured, and what was decided
and why. Append to it; never rewrite an entry. The handover is the **snapshot**.

**Ground rules that survive a restart** — the full set is §0 of the plan, but
these are the ones that get forgotten:

- Production is live. `MIGRATIONS[0]` is frozen; new entries only, versions
  claimed from the plan's §1 registry.
- Never run a migration, a tool, or the server against the repo-root
  `testboard.db`. Work on copies in a temp directory.
  **Corrected 2026-07-28:** it holds *generated* data on a dev machine, not a
  copy of production. Production is ~900 MB, roughly four times its size.
  Earlier entries here (WP-4, WP-5, the performance pass, WP-10) call it "a
  copy of the real database" and that wording is wrong — the measurements are
  real, the database they were taken on is not production. Say which one a
  number came from.
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
| WP-12 | Cutoff from the suite's rhythm | **done (core)** | `find_passes`, 980 tests |
| WP-13 | Declared environment expectations | **done** | `8c10de7`, `0ee7c1b` on `main` |
| — | MariaDB migration automation | **done** | `dae82c7` on `main` |
| WP-14 | In-run progress | **done, held back** | `1102463`, `b4b1030` on branch `wp-14-in-run-progress` |
| WP-15 | Progress pushes from a partial reader | `pending` | migration 6 claimed |
| WP-16 | Site-specific info tab | `noted` | content not specified yet |

**WP-14 is deliberately NOT on `main`, and this is the first split in this
log.** It is finished and green, but the progress bar it draws counts imported
runs, and the reader that feeds production cannot push runs mid-pass — WP-15 is
what makes it mean anything. Shipping it first would put a bar on the home
screen that sits flat all night and then jumps to 100%, which is worse than no
bar. It waits on the branch for WP-15.

So `main` is deployable on its own: WP-13's declared expectations, the whole of
round 1, and the MariaDB tooling, with nothing half-finished in it. `git log
wp-14-in-run-progress` is where the progress bar lives until then.

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

### WP-12 — derive "recently run" from the suite, not the wall clock — **done (core)**

`_SUMMARY_RECENT_HOURS = 36` was one wall-clock window answering a question it
cannot answer. Suite 980, green.

Two failure modes, both reported from real use:

- **Monday morning.** Last run Friday night, so at 36 hours every test in the
  estate looks abandoned.
- **Every morning, for whichever environment runs first.** Environments run
  **sequentially** — first reports in the small hours, last hours later — so on
  one shared clock the early ones are stale for the rest of the morning.

Consequences ranked: the review panel offered to **retire** thousands of healthy
tests (destructive, and gated on exactly this flag); the "not run" queue filled
with noise so real disappearances hide in it; the headline claimed nothing ran.

`analytics.find_passes` groups per-environment activity hours into passes.
`analytics.recent_cutoff` takes the start of the **previous covered pass** per
environment, then the oldest across environments.

Two properties are load-bearing and each comes from how the suite really runs:

- **Per environment**, because they run sequentially.
- **Coverage** — a block only counts as a pass if it ran ≥50% of that
  environment's tests. Without it, the ad-hoc re-runs triggered after a fix
  count as passes, dragging the line forward to this afternoon and flagging the
  whole estate. That would have been worse than the bug being fixed, and it was
  only caught because the user mentioned ad-hoc runs.

Safety rails: never stricter than the old window (so it can only flag *fewer*
tests), never older than 14 days (so a stalled feeder cannot slide the line back
for ever), and `latest_run_time` is now reported so a stalled feeder is visible
**as** a stalled feeder rather than as the estate quietly going stale.

Nothing knows what time the suite runs; every boundary comes from observed gaps.
The frontend no longer recomputes the cutoff — the server sends `stale_before`.

**Not yet done** (agreed next steps, see the conversation):

1. **Per-environment expectations, declared not inferred** — cadence and
   expected test count, stored and editable in the UI. Would replace the
   inferred coverage denominator and handle an environment whose rhythm the
   inference gets wrong.
2. **In-run progress** — one env takes 2.5 hours and there is no visibility
   while it runs. `find_passes` already computes runs-so-far per environment
   against an expected total, which is exactly a progress bar.

Both want the same new table, which is why they should be designed together.

### WP-13 — declared environment expectations — **done**

Migration **5**: `environment_expectations`. `GET /api/environments`,
`PUT /api/environments/{env}/expectation`, and an "Environment expectations"
fold-out beside "Manage people" on Open actions.

The first of WP-12's two named follow-ups. WP-12 decides whether a test has
gone quiet from whether the suite has been round since, and a night only
counts as a *pass* if it ran at least half the environment. That denominator
was inferred as `COUNT(*) FROM latest_runs` — **every test ever seen**, a
high-water mark that only grows.

**Why that had to become declarable: it fails silently, in the destructive
direction.** Too large a denominator means no block clears the coverage bar,
no pass counts, and `recent_cutoff` falls back to the 36-hour wall clock —
which is precisely the Monday-morning bug WP-12 exists to fix, review panel
offering to retire thousands of healthy tests included. Nothing anywhere says
so. A wrong number and a correct one look identical.

So the endpoint does not just store the declaration, it **echoes it against
what actually happened**: per environment, how many of the recent nights
counted (`3 of 14`), and globally whether the cutoff came from a pass at all.
A declaration you cannot check against reality is a form nobody knows how to
fill in.

Decided and worth not re-litigating:

- **Declared overrides inferred, per environment; an environment with no row
  behaves exactly as before.** Additive, so it cannot regress an environment
  nobody has configured.
- **The clamps in `recent_cutoff` do not move.** A declaration feeds the
  coverage denominator only. `min(fallback)` and `max(floor)` are what keep a
  wrong declaration a slightly-off cutoff instead of a destructive one, and
  there is now a test asserting the cutoff stays inside the wall-clock window
  at declared values of 1, 4 and 900.
- **No cadence column**, despite the plan naming "cadence and expected test
  count". Nothing consumes it: every boundary in `find_passes` comes from
  observed gaps, deliberately. A declared schedule would either sit unread or
  displace the observation, which is the design it would be displacing.
- **Retired tests came out of the inferred denominator** in the same change.
  They are excluded from every other estate view, and a pass that does not run
  a retired test has missed nothing. This is the same defect being fixed, so
  it is fixed here rather than left as the next surprise.
- **404 on an environment that has never reported.** Declaring one affects
  nothing and a typo would leave a row with no visible purpose. The listing
  still unions declarations in, so a *renamed* environment's stale row remains
  visible and can be cleared.

**One call path, deliberately** (`api._pass_view`). If the admin page worked
out its passes separately from the cutoff, a declaration could change what the
page shows and not what the estate is judged by — worse than not having the
feature, because it would look like it worked. A test asserts that declaring
changes `/api/summary`'s `stale_before`, not just the new endpoint.

**Found by running it against real-shaped data, not by testing it.** With
`win-sim` declared at 9,000 against 1,680 tests, its own row read `0 of 12
counted` — but the **global** `cutoff_from_passes` stayed `true`, because the
other two environments still contributed a covered pass. So the global flag
alone would have said everything was fine while one environment was silently
uncounted. That is why the per-environment count is the primary signal and the
global flag is only the headline.

`analytics.recent_cutoff` now returns a `Cutoff` NamedTuple rather than a bare
datetime. `from_passes` cannot be derived from the timestamp by a caller —
"the fallback won because nothing counted" and "the fallback won because every
pass is more recent than it" produce the same value and mean opposite things.

`analytics.complete_passes` drops the oldest block when it starts within one
gap of the 14-day floor. Its run count is whatever fell inside the window, so
its coverage verdict is an artefact of the edge; harmless for the cutoff (it
only makes it more lenient), but on a page it is a permanently red row that
means nothing. Display only.

**Migration timing.** 8 ms, on the **dev database** (218 MB, 540,192 runs,
12,008 tests of *generated* data — not a copy of production, whatever the
earlier entries in this file say; see the corrected ground rule at the top),
brought to version 4 first so the number is entry 5 alone. Production is
roughly four times that and was **not** measured from here — it does not need to be for this entry: `CREATE TABLE` writes one
page and rewrites no existing row, so the number cannot grow with the
database. A migration that touched existing rows would need the real thing, as
entry 3's backfill did.

**WP-12 had no unit tests for `find_passes` or `recent_cutoff`** — it shipped
with API-level coverage only. Since this package changes what feeds both, they
have them now: pass grouping, the ad-hoc re-run case that coverage exists for,
per-environment separation, and each clamp separately.

`tools/export_for_mariadb.py` needed the new table in its load order, DDL and
verification queries — caught by its own guard test, which is what that test is
for. Exporting into a schema missing a table drops it silently.

Frontend verified by driving the real module against a live server on the dev
database (a minimal DOM in node, not a browser): the section renders, Save with
an unreachable 9,000 flips the row to `0 of 12 counted` and grows a Clear
button, Clear restores inference, and a typed `0` is refused with a readable
message. **Not click-verified in a browser** — that is still worth doing.

Suite 980 → 1037.

A parallel session was writing `tools/migrate_to_mariadb.py` in this tree at
the same time. Its files are untracked and are **not** in this commit; the
1032 above is the suite with them excluded.

**WP-14 (in-run progress) is next** and is specified in the plan. It needs no
new query: `find_passes` already computes runs-so-far per environment against
an expected total, which is exactly a progress bar. The one design point is
that "still running" must use a shorter idle threshold than the 6-hour pass
boundary, or a finished 2.5-hour environment shows as in progress for another
six.
### WP-14 — in-run progress — **done, on `wp-14-in-run-progress`**

Built, green, and deliberately **not on `main`**. Full entry lives with the
code on that branch; whoever merges it should replace this pointer with it.

The short version of why it is held back: the bar counts imported runs, and the
reader feeding production cannot push runs mid-pass — it has identities and
results but no per-test timings until the end. So on production today the bar
would sit flat all night and then jump to 100%, which is worse than no bar.
WP-15 below is what makes it mean something, and the two should land together.

---

## Where things stand

### Deploying `main` — measured 2026-07-28

`origin/master` is **17 commits behind** `main`, so this is not a small
upgrade: it carries everything from the worker-pool fix (`964e0b4`) onwards —
all of round 1, WP-12's derived staleness cutoff, WP-13, and the MariaDB
tooling. The running instance is at `a41cfe0`.

**The database migrates on first start, from version 1 to version 5.**
Measured on a copy of the dev database (218 MB, 540,192 runs, 12,008 tests) at
version 1, through the real `Storage` startup path: **237 ms** for the whole
chain, ending at version 5 with every row intact and migration 3's duration
backfill complete.

Production is ~900 MB, roughly four times that, and **the number should not be
scaled by four.** Nothing in migrations 2–5 touches `runs`: entry 2 is two
`ADD COLUMN`s, entry 3 backfills `latest_runs`, entry 4 indexes `latest_runs`,
entry 5 is a `CREATE TABLE`. All of them are proportional to the number of
TESTS — about 12,000 in both databases — and production's extra size is
`run_outputs` blobs, which are never read. Expect well under a second.

Nothing is reversible by the code: a database at version 5 is refused by
older code, which is deliberate. **Take a copy of the SQLite file before
starting the new build.** That is the rollback.

Smoke-tested against that migrated copy through a real server: every page and
every endpoint answers 200, the summary carries no `progress` key (WP-14 is
not in this build), and the environment-expectations section renders, saves and
clears against real data.

To publish: the local branch is `main` and the remote branch is `master`, so
it is `git push origin main:master` — a fast-forward, since `main` contains
all of `origin/master`. Not done here; publishing is the user's call.


`main` is deployable on its own and has nothing half-finished in it: all of
round 1, WP-12's derived staleness cutoff, WP-13's declared environment
expectations (migration 5), and the MariaDB migration tooling.

Held on branches, on purpose:

- `wp-14-in-run-progress` — the progress bar, waiting for WP-15.

Still open, and needing a person rather than a commit:

1. `tools/diagnose_db.py --compare-local` on the production server — still never
   run since the worker-pool fix.
2. `/api/summary` at ~197 ms — needs a design decision, not an index.
3. The MariaDB migration itself: §A needs whoever holds root, then the dry run
   against a copy of production. `tools/migrate_to_mariadb.py` runs everything
   after that, but nothing in it that talks to MariaDB has been executed
   anywhere — there is no server here or in CI.
4. **Nothing on `main` has been clicked through in a real browser.** WP-13 was
   driven headlessly against a live server, which catches wrong field names and
   DOM errors but not layout, focus or keyboard behaviour.

### WP-15 — progress pushes from a partial reader — **specified, not built**

Raised by the user on 2026-07-28, immediately after WP-14: the in-house reader
**cannot produce a full run record mid-pass**. During the night it has test
identities and results but no per-test timings; it does know when the
environment's run started, and the final push upserts everything in full.

That makes WP-14 as built useless against the reader that actually exists — a
flat bar all night, then a jump to 100%, which is the blindness it was written
to remove. Worth knowing before anyone reads the WP-14 entry above and assumes
it works in production.

**It cannot go through `/api/import`, and not only because that contract is
fixed.** Run identity is `(environment, script, test_name, start_time)`, so a
record with no start time has no identity. Synthesising one means the final
push — carrying the real per-test time — writes a SECOND row rather than
updating the first: one duplicate per test per night, in the table whose whole
design assumes one row per test per start.

So partial records never become `runs` rows. Full specification is WP-15 in the
plan; migration **6** is claimed there. The shape:

- `run_progress`, keyed by the test triple, one row per in-flight test.
- `POST /api/progress`, carrying `pass_started` and completed tests only —
  confirmed with the user, so there is no "started but not finished" state and
  the `Result` enum does not grow.
- A real import **deletes the test's progress row in the same transaction**,
  reusing the `_unretire_on_new_run` precedent, so a test is in exactly one
  place and the counts cannot double.
- A provisional result is **not** "the latest result": it stays out of
  `latest_runs`, the queues, the staleness cutoff and WP-13's coverage
  denominator, all of which keep reading completed, timed runs. That line is
  what keeps the package additive.

`pass_started` is incidentally the first real batch identifier this system has
had — WP-12 and WP-13 infer pass boundaries from gaps precisely because the
import contract has none. The inference stays: the final push still carries no
identifier, and history still needs it.

**Not started.** It shares `storage.py` and the migration registry with the
MariaDB work in flight, so the version is claimed and the code is sequenced
after it.

---

## Drop of 2026-07-30 — production fixes, treemap, operator tooling

**Not a work package.** A small group of fixes requested directly, plus two
things found while looking for them. Suite **1137 → 1268 green** (skipped 1).
**No migration**: schema stays at 5, so this is the first drop whose rollback is
a `git checkout` and a restart. Operator note: `docs/drops/2026-07-30.md`.

### The Time page was broken in production the whole time it has existed

`static/time.js` called `formatTime()` and never imported it. It parses, it
loads, and the two call sites are both on branches that only run when some test
has stopped reporting — which the generated dev database never has and
production always does. So it worked here and threw
`formatTime is not defined` there.

Reported as "I assume a python 3.6.8 related error?". It is not: ES modules have
no implicit global scope, so a missing import is a `ReferenceError` at the moment
the line runs.

The interesting part is that nothing could have caught it. There is no
JavaScript test runner and there is not going to be. So
`test_frontend_calls.py::SharedImportTest` now asserts the narrow form of the
property: for every name exported by `api.js`/`charts.js`/`sorting.js`, if a page
CALLS it, that page imports it. Regex-level, and verified against the pre-fix
file — it fails with exactly `['formatTime']`. The general form ("every free
identifier is bound") needs a JavaScript parser and was not attempted.

Two more bugs on the same page, found while in there:

- Its tooltips were passing `tooltipRows` as **arrays** where `showTooltip`
  reads `{label, value}` objects, as every other caller passes. So the Time
  page's tooltips rendered blank values. Fixed by the treemap work below.
- The page never said how much it excluded. It does now.

### Worker starvation from idle keep-alive connections — the "stuck page"

Reported as "what looks like threading issues (I have seen at least one case of a
developer being stuck with the page not loading)". Root-caused and reproduced.

A worker serves a whole **connection**, `protocol_version = "HTTP/1.1"`, and
Python's default handler `timeout` is `None`. So a worker that had answered a
request sat in `readline()` on an idle socket **forever** — until the browser
chose to close it. Browsers open up to six connections per origin and hold them
long after they are done with them, so **two tabs could hold every worker in the
default eight-worker pool with nothing in flight at all.** The server is idle
and the page does not load.

Reproduced at `workers=2`: two idle connections, and a third request that never
arrived. That reproduction is now
`test_server_pool.py::KeepAliveTest::test_idle_connections_do_not_starve_the_pool`.

The fix is four interacting parts, and it took three attempts:

1. **Two timeouts, not one.** `_KEEPALIVE_IDLE_SECONDS` (5s, Apache's default)
   bounds waiting for the next request; `_ACTIVE_SECONDS` (60s) bounds a request
   in progress. One short timeout for both aborts a slow import body; one long
   one for both is the bug. `test_a_request_arriving_slowly_is_still_served`
   pauses 6s mid-request and pins the split.
2. **An interruptible wait.** The first attempt used one blocking read under the
   idle timeout: correct, and a queued request still waited out the whole 5s
   (**measured: 5.00s**). Now the wait polls every 250ms and gives the
   connection up as soon as another is queued — **0.24s**.
3. **`peek`, not `select`.** Selecting on the socket misses a pipelined request
   already sitting in `rfile`'s buffer, and would then close the connection with
   it unanswered. `rfile.peek(1)` under a short socket timeout answers for both,
   consumes nothing, and leaves the stream untouched on timeout.
4. **Adaptive keep-alive.** `_write_response` sets `Connection: close` when
   the pool has queued work, so a connection about to be reclaimed is
   announced before the client reuses it.

   **This and the poll in (2) are ONE mechanism, and separating them was a
   real mistake made in this session — caught only because the user asked
   whether `--perf-log` was the cause of a page feeling laggier.** The perf
   log was not (measured below at +15 ms on `/api/summary`, nothing
   elsewhere). Chasing it found that the eager close fires constantly during
   page loads — 2 of 8 responses in a first crude probe — which looked like
   pure waste, so it was removed on the reasoning that the poll alone fixes
   the starvation. It was pushed as a review candidate. It was **worse**.

   Reclaiming a worker means closing a connection the client still believes
   is open. The announcement is what makes that safe. With it gone the poll
   reclaimed connections silently and clients sent their next request into a
   dead socket. Measured with `http.client` — a strict client that, unlike a
   browser, does not retry — against a 210 MB copy of the dev data, one full
   dashboard load per simulated user:

   | Concurrent users | Announced (deployed): closes / failed | Silent (the candidate): closes / failed |
   |---|---|---|
   | 2 | 16 / **0** | 0 / **6** |
   | 4 | 37 / **0** | 0 / **16** |
   | 6 | 60 / **0** | 0 / **27** |

   About 40% of requests on reused connections died. A browser retries an
   idempotent GET and would mostly hide it; a POST — a comment, an
   assignment — is not retried and fails in front of the user. Reverted.

   The earlier "one client saw its socket aborted mid-body" was **also
   wrong**, and worth recording because it nearly became a production
   diagnosis: that probe ignored `Connection: close` and kept writing to a
   socket the server had properly announced it was closing. A correct client
   sees no truncation and no corruption — verified byte-for-byte against the
   files on disk, 0 truncated and 0 corrupted at every concurrency level.
   (A first pass reported 2 corrupted responses; that was a fresh git
   checkout having CRLF where the working tree has LF, not the server.)

   The cost of announcing is one TCP handshake per closed connection, a
   fraction of a millisecond on a LAN. `--workers` is the knob that reduces
   how often it fires: on the deployed code, raising it from 8 to 24 took
   4-user page loads from 37 closes to 8, with no code change.

   Guarded by `test_a_reclaimed_connection_is_always_announced_first`, which
   drives 12 keep-alive clients against 4 workers with a non-retrying client
   and asserts nothing fails. Verified by re-planting the regression.

   **The invariant, for whoever touches this next: the server must never
   close a connection without having told the client in a response header.**

### Where the time goes, as a treemap — and the constraint that decided it

Requested: "a box graph similar to you get when profiling ... you click a box you
get a new rectangle drilling down". The drill-down already existed; only the mark
type changed. `time.js`'s header comment argued *against* a treemap, and that
reasoning was not wrong — area is read less precisely than length, and small
cells cannot be labelled. The comment now records why the decision changed
rather than contradicting the file it heads.

`treemapLayout` is squarified (Bruls, Huizing & van Wijk), pure arithmetic inside
a fixed viewBox, so it needs no measured element and was verified without a
browser: areas proportional to values within 0.5%, full coverage, no overlap, no
escapes, no slivers, and degenerate inputs (empty, all-zero, zero-width,
negative) return cleanly.

**Three iterations, and the third was decided by the user mid-session:**
"make sure the treemap stays vaguely representative. Something that's 1% of total
runtime shouldn't be 25% of the screen."

- **Attempt 1 — draw everything.** Honest, unreadable. Measured on the dev
  database: 251 scripts in one environment, median box 30x32 units, and **not one
  of them large enough to hold its name.**
- **Attempt 2 — top 24, rescaled to fill the rectangle.** Every box labelled and
  the arithmetic *false*: the top 24 are 11% of the time, so rescaling inflated
  every box on screen about ninefold. Exactly what the user then ruled out.
- **Attempt 3 — proportional to the total, tail folded into one box.** Areas are
  never rescaled. Items below ~220 square units combine into a single
  "N smaller scripts" box carrying their true summed value, outside the shade
  scale so it does not read as the biggest script. On that data it is 89% of the
  rectangle, which is the finding: *no single script is the problem, it is spread
  across all of them* — the thing attempt 2 hid.

Verified against the rendered SVG rather than the layout function, so the gap
inset and the aggregate box are both included: **worst drift 0.5 percentage
points**, all of it the 2px gaps. The gap now scales with the smallest box,
because a fixed inset takes a larger fraction of a small box than a large one
and that is a distortion of the encoding.

The page states what the chart is not saying: how many items were combined, and
how many boxes are too small to carry a name. Shade is relative to the largest
box on screen, not an absolute share — an absolute scale collapses to one colour
at both ends of the range this page shows.

### Smaller items

**"Last update" pills are filter buttons** (`setEnvironment`, which already
existed). All environments stay visible when one is selected, deliberately: a
list that collapsed to the selection would remove the control you need to get
out of it.

**The What's new link carries the latest drop date** with an unread dot, from
`data-drop-date` on each release section in `whatsnew.html` — the only copy of
that date. `DropDateTest` asserts the attribute agrees with the heading a human
reads, because a copied section keeping the previous date parses perfectly and
misinforms everyone.

**Site-specific What's new notes** (`testboard/site_notes.py`,
`tools/add_site_note.py`, `GET /api/site-notes`). A JSON file outside the
repository, so a deployment cannot overwrite it; **no migration and no table** —
these are one site's commentary on testboard's data, not testboard's data, and
version 6 belongs to WP-15. Read per request, so a note is live without a
restart. Which is also why `--edit` and `--remove` exist, added on the user's
prompt ("make sure the operator can correct / remove a note if they make a
mistake"): a note is in front of every tester the moment it is written, so
retraction cannot mean hand-editing JSON underneath a running server. Ids are
stable across loads, never reused after a removal, and assigned deterministically
even for a hand-written file that has none.

**`tools/drop_environment.py`** for the `UNKNOWN` rows that appeared in
production. Not retirement — that keeps history. One transaction over every
table keyed by environment plus `run_outputs` via `runs.id`, ordered so no
derived row is ever left referencing a deleted run.
`EnvironmentDeleteTest::test_the_table_list_covers_the_whole_schema` asks the
live schema rather than trusting the hand-written tuple, so a later migration
adding an environment-keyed table fails the suite instead of quietly leaving its
rows behind.

**Performance logging** (`testboard/perf.py`, `tools/perf_report.py`,
`--perf-log`). Off unless asked for; capped and rolled over so leaving it on is
safe; a full disk degrades to "no records", never to a 500.

The unit is a **storage operation, not a SQL statement**, and that is not
convenience: `sqlite3`'s `execute()` steps a statement once, so for a SELECT most
of the cost lands in the following `fetchall()`. Timing statements would
systematically under-report precisely the slow reads worth finding. The
consequence to be honest about is that a multi-statement method is one number.

The field that earns it is the **queue wait**, attributed to the first request on
a connection only — repeating it per request on a keep-alive connection would
turn one 3-second stall into ten. Measured on a copy of the dev database, 20
concurrent `/api/summary` against 2 workers: mean **184ms served, 615ms median
queued, 1.23s worst**, and the report says "that is contention, not query time".
`activity_buckets` dominated the storage section at 139ms mean / 2.92s total,
consistent with the decomposition already recorded for `/api/summary`.

A documentation error caught by its own test: the report first described p99 as
"1 in 100 was at least this slow", which is wrong — with 99 samples at 1ms and
one at 1000ms, p99 *is* 1ms. Percentiles are textbook nearest-rank now, and the
column is described as the boundary it is, with `max` for the worst.

### What `--perf-log` actually costs — measured, because it was suspected

Asked directly: "Does that perf logging impose an overhead? I've enabled it and
the page feels just a little laggier?" Worth answering with an A/B rather than a
reassurance, and the answer turned out to be "no, but you were right that
something was".

Same code, same database (a 210 MB copy of the dev data), same request pattern,
alternated ON/OFF/ON/OFF across two rounds so machine drift shows as spread
rather than as a result. Twelve sequential requests per endpoint after a
twelve-request warm-up, plus a sixteen-way concurrent burst.

| | perf ON | perf OFF | overhead |
|---|---|---|---|
| `/api/summary` median | 728 ms | 713 ms | **+15 ms (+2.1%)** |
| `/api/dashboard` median | 33.5 ms | 34.3 ms | −0.8 ms (−2.4%) |
| `/api/time` median | 581 ms | 588 ms | −7.0 ms (−1.2%) |
| 16 concurrent, wall | 3876 ms | 3883 ms | −6 ms (−0.2%) |

Round-to-round spread was tight (ON 724/732 ms, OFF 715/711 ms), so the 15 ms on
`/api/summary` is probably real and everything else is noise.

**Why `/api/summary` and nothing else:** it is the only endpoint that writes a
lot of records. One call logs **~110** — 93 of them `failure_streak_bounds`,
which is called once per queue entry. So ~0.14 ms per record, covering
`time.time()` twice, a lock, `json.dumps`, and a line-buffered write syscall.
`/api/dashboard` issues a handful of storage calls and shows nothing at all.

2% on the heaviest endpoint is not something a person can feel, which is what
sent the search elsewhere and found the adaptive-keep-alive fault above. Left as
it is: the cost is real but small, and the alternative (buffering instead of
line-buffering) would trade it for losing the tail of the log in exactly the
situation the log exists for — a process that has stalled and is still holding
the file open.

Worth knowing for production: the log is a write per record on the same
filesystem as the database. On the dev machine that is an SSD and invisible. If
the production database lives on a network mount, put the log somewhere local.

### Verification, and its limits

Frontend verified by driving the real ES modules against a live server under a
minimal DOM shim in node. It earned its keep twice: it found the `formatTime`
bug, and it found `Array.prototype.slice.call(map.keys())` in the new
`whatsnew.js` — which returns `[]`, because a Map iterator has no `length`, so
no site note would ever have rendered while every other part of the feature
looked correct.

**No browser has rendered any of this**, and the shim cannot catch layout,
colour or contrast. Every number above is from the dev database (218 MB
generated, 540,192 runs) on a developer machine; production is ~900 MB and
roughly four times that. The keep-alive figures are from the reproduction, not
from production traffic. `tools/diagnose_db.py --compare-local` has still never
been run on the production server.

## Drop of 2026-07-31 (WP-17) — the summary at production scale, and the 10-minute re-push

First production perf log (day one, user-run report): `/api/summary` mean 6 s,
`activity_buckets` 3.5 s of it, worst response 60 s. Analysed locally, fixed on
`wp-17-summary-perf`. Suite 1288 (from 1268). Migration 6 ships.

### The finding: one query was O(total history)

The staleness-cutoff bucket query — `GROUP BY environment, hour` over a
fortnight of `runs` — was planned by SQLite as a **full covering scan of the
runs UNIQUE index**, because the `(start_time, result)` index does not carry
`environment` and the planner preferred scanning everything to 168k row
lookups. Cost proportional to the whole year of history, not the window; paid
by `/api/summary`, `/api/time`, `/api/dashboard?stale=1` and
`/api/environments`, uncached; growing nightly. Reproduced on the dev copy at
607 ms (70% of the 871 ms summary mean; prod 3.5 s of 6 s — same shape ×
history size × network mount).

What did NOT work, measured before choosing: `ANALYZE` (planner switched to a
skip-scan on the same wrong index, 207 ms); an unforced covering
`(start_time, environment)` index (planner ignored it — bound-parameter range
selectivity is unknowable at prepare time). `INDEXED BY` worked (115 ms,
window-proportional) but is SQLite-only syntax, leaves the query O(window),
and the index adds ~15–20% to the file. Rejected in favour of the project's
own pattern:

**`activity_hours` (migration 6)** — third derived table, environment × UTC
hour × result → count, maintained in the import transaction (+1 on insert,
−1/+1 on a result flip; rows deleted at zero so it stays byte-equal to the
GROUP BY it replaces), backfilled from full history in one aggregate pass
(2.4 s cold on the dev copy), rebuilt by `prune_runs_before`, covered by
`delete_environment` via `_ENVIRONMENT_TABLES`, exported by the MariaDB tool.
`ActivityHoursTest` pins live-vs-rebuild equality both directions, with a
planted-drift test proving the comparison can fail, and a planted-skew test
proving the readers read the table. The trend query reads it too (the result
dimension exists for exactly that), so the memo layer is now belt-and-braces.

Measured, dev copy: buckets 607 → 2.3 ms; `/api/summary` 751 → ~190 ms;
`/api/time` 630 → 40 ms; headline part ~100–170 ms at 14 KB.

### The user's disclosure that reframed the analysis

The site feeder re-pushes its whole recent window (~10k records) **every 10
minutes**, unchanged or not, taking 1 min+. Measured cost of one unchanged
record before: ~2.3 KB WAL (runs UPDATE + run_outputs INSERT OR REPLACE of an
identical blob + latest_runs touch) — ~23 MB per push, ~3.3 GB/day through the
production mount, page-cache eviction for every reader, ~20 trend-memo
invalidations per push. And a production bug: `_unretire_on_new_run` fired on
ANY upsert, so **every retirement was undone within 10 minutes** of being
made. Nobody had connected the "Automatically un-retired" comments to the
push schedule.

**The skip:** a record whose metadata all matches and whose
`runs.output_fingerprint` (SHA-1 of output text, new column, NULL for
pre-migration rows) matches writes nothing — no UPDATE, no blob REPLACE, no
`_maintain_latest`, no un-retire, no memo invalidation. NULL never matches, so
the active window stamps itself in one push cycle and the first post-upgrade
push behaves like today. Measured: 2,000 unchanged records 0.46 s / 4.6 MB WAL
→ **0.04 s / 0 bytes**. Wire compatibility decided deliberately: response
`updated` still counts unchanged records (deployed feeder sums and logs it);
additive `unchanged` field refines it. `test_reimport_identical_batch...`
re-pinned from `updated=3` to `unchanged=3` — a deliberate semantic change,
this line is the record of it. Un-retire on a CHANGED old record is pinned as
still happening (`test_a_changed_reimport_still_unretires`); only the
unchanged case stopped.

### The split (user chose to pull it into this drop)

`/api/summary?parts=headline` (everything but queue rows, plus
`queue_totals`), `parts=queue&queue=<kind>`; bare call unchanged-plus-totals,
pinned to the parts by `SummaryPartsTest` (slice-vs-whole, not hand-written
values). Home page fetches headline + active tab + browse page in parallel,
paints each on arrival, other tabs on first click ("Loading queue…" line —
the no-skeleton rule respected; refreshes still dim-and-hold). Action
refreshes fetch headline + active queue only, still coalesced.
`SummaryPartsFetchTest` pins the fetch shape the way the coalescer is pinned.
Verified end-to-end under the node DOM shim against a live server: staged
requests, badge paint, on-demand fetch, cached re-click.

### Housekeeping with reasons

- **Registry swap:** WP-17 took version 6; WP-15 (parked WIP branch) moved to
  7 — versions must ship contiguously, so an unshipped claim cannot hold 6
  while 7 ships. The WIP branch must renumber before merging. Registry,
  CLAUDE.md, and the operator note all say so.
- `TestMigrationFiveOnAnExistingDatabase` now asserts `MIGRATIONS[-1][0]`
  rather than the literal 5 — the pin was about migration 5 arriving on a v4
  file, not about 5 being last.
- `test_the_trend_cache_is_invalidated` was passing vacuously: it passed `7`
  (a days count the signature does not have) as the ENVIRONMENT filter, so
  both sides of the assertion were always empty. Fixed and given a
  populated-before precondition.
- MariaDB exporter: `activity_hours` in `TABLE_ORDER` + DDL + a verify query;
  `runs.output_fingerprint` in the DDL; the null-vs-empty test's `row[-1]`
  made explicit (`row[8]`) — "last column" silently became a different
  question when the column list grew. The guard tests caught all of this,
  which is what they are for.

### Verification, and its limits

Same honest line as the last drop: **no browser, no production numbers.**
Everything measured here is the 218 MB dev copy on a developer machine; the
plan's Phase 2 (streaks, queue payloads, worker count) is parked until one
night of production perf log after this drop re-ranks the remaining terms.

## CI repair, 2026-07-31 — the 3.8 and 3.6 legs had been red since 07-27

Every leg except 3.14 was red from the moment the full local history reached
GitHub. All four causes were in **test code**, none in shipped code, and every
one was invisible on a modern interpreter — which is the entire reason those
legs exist. Fixed in two commits on `wp-17-summary-perf` (`17b8b4b`,
`64a3468`); all three legs are now green on the full 1288, including the
authoritative ubi8/python-3.6.8 container.

What each failure taught, briefly (details in the commit messages):

1. `test_storage.py` used `Tuple` in an annotation without importing it —
   latent since WP-8, silent under PEP 649's lazy annotations, an ImportError
   on 3.6/3.8 that dropped all 209 of the module's tests from discovery.
   **Guard widened:** the compat gate's annotation-evaluation sweep now covers
   `tests/` too, and was proven able to catch exactly this by reverting the
   fix and watching it fail on 3.14.
2. `ast.parse(feature_version=(3,6))` is only *enforced* from 3.9 (PEG
   parser); 3.8 accepts the walrus anyway. The planted regression caught it —
   the grammar-gate tests now skip below 3.9 instead of pretending.
3. Two copies of the storage.py literal scan matched only `ast.Constant`;
   the 3.6 parser emits `ast.Str`, so on the deployment interpreter the scan
   found nothing and every count "passed" over an empty list. The
   scan-finds-something tripwires fired as designed. One copy now handles
   both node types; the other delegates to it.
4. Two runtime-library differences: 3.6's sqlite3 requires registered
   callbacks to be hashable (`set_trace_callback(list.append)` is a
   TypeError there and fine on 3.8+), and SQLite 3.36 changed query-plan
   wording (`SEARCH TABLE runs` → `SEARCH runs`), which one diagnose_db
   assertion had pinned to the modern spelling.

The meta-lesson, worth the ink: **the gates that failed were doing their
job.** Each red leg was a true positive, and the one gap — nothing forced
test annotations to evaluate on a lazy interpreter — is now closed. The ubi8
leg runs the suite with `skipped=5` (the four version-gated grammar tests
plus the standing skip); that number is expected, not a regression.

## Drop of 2026-08-04 (WP-18) — the Timeline: script running order per environment

**The problem, as the user stated it.** Scripts are not run in a
dependency-safe order, and one that goes wrong tends to leave static data
modified — the failure then surfaces in a LATER script's tests. Walking back
from the failure to the culprit needs the night's script running order, and
every existing view is test-centric: results, not order.

**What was built.** A fifth page, Timeline: one environment, one block of
activity, one row per script *execution* (the 60-minute-gap inference from
`group_executions`, applied at script grain), rows in running order on a
shared time axis. Partial runs read short against the script's known test
count ("41 of 45 known tests"); a script that ran twice is two rows; a row
expands in place to its tests in start order, first FAIL marked. Ad-hoc
blocks appear in the picker labelled "partial" — a twenty-test re-run after
a fix is often exactly the state-poisoning suspect.

**Storage: `script_hours`, migration 7, the fourth derived table.** The page
needed script × time, no table had it, and the standing rule ("nothing may
scan a window of `runs` at request time") is why it is a derived table
rather than a request-time GROUP BY. Design notes that will matter later:

- Shape mirrors `activity_hours` plus two exact-timestamp columns
  (`first_start`, `last_end` per bucket) — hour bucketing keeps the table
  small (21,988 rows where `runs` holds 540,192 on the dev copy, ~4%),
  the span columns give sub-hour ordering.
- PK leads `(environment, hour)`: a window read is one index range,
  pinned by a query-plan test.
- MIN/MAX cannot be decremented, so the two shrink paths (re-import
  changing a stored result or end time) recompute their buckets from
  `runs` exactly, inside the same transaction. The fingerprint skip makes
  those paths rare by construction; a bucket both grown and recomputed in
  one batch is recomputed only (the recompute already sees the batch's
  inserts — double-counting was designed out, and there is a test that
  plants exactly that batch).
- `ScriptHoursTest` holds the table byte-equal to its GROUP BY in both
  directions, through live maintenance, environment deletion and pruning,
  with a planted-skew test proving the comparison can fail.

**Renumbering, second verse.** WP-15's parked claim moved again (7 → 8):
versions ship contiguously, so the ship-first package takes the lowest
unshipped number. The registry now states the pattern explicitly — a parked
claim is a reservation, not a number.

**MEASURED, all on the dev copy (218 MB, 540,192 runs) — production is ~4×
this on a network mount and the migration MUST be re-measured there before
the drop ships (§1.2):**

- Migration 7 backfill: **3.2 s** (21,988 rows). The v5 dev copy's full
  open-and-migrate (6 then 7, both rebuilds) was 5.95 s.
- New endpoints: `/api/timeline` **36.8 ms** median for a full night of
  251 script rows (comparable to `/api/time` at 37 ms); row expansion
  **1.5 ms**.
- No regression on existing endpoints (median, before → after):
  `/api/summary` 177.6 → 180.9 ms, `parts=headline` 104.4 → 104.7 ms,
  `/api/dashboard` 30.3 → 31.9 ms, `/api/time` 36.5 → 37.4 ms,
  `/api/environments` 14.4 → 14.3 ms. All within run-to-run noise.
- Import (2000 records, 40 scripts, temp db, median of 5): fresh
  293 → 295 ms; **byte-identical no-op push 71 → 73 ms — the skip still
  writes nothing**; the pathological all-2000-records-flip-result batch
  304 → 347 ms (+14%), which is the recompute path priced at its worst
  and a batch shape the fingerprint skip exists to prevent.

**Frontend verified the established way** (no browser here): the real
`timeline.js` driven under a node DOM shim against a live server on the
migrated dev copy — 18 checks: initial paint, row/block counts, running
order, bar geometry, expansion fetch-once, links, jump-to-first-failure
visibility, block switching, URL round-trip, environment switching. Layout,
colour and contrast remain unverified by any human eye, as ever.

**Guards widened, not weakened:** `WindowWordingTest` now scans
`timeline.js` (both the `recent_hours` ban and "last night"); the MariaDB
exporter learned `script_hours` because its parity test failed the build
the moment the table existed — exactly as designed. The runbook's port
notes (§F) gained the recompute-path paragraph.

## WP-18 refinements re-measured, 2026-08-04 (later the same day)

After a round of UX refinements from first use (whole-row click targets,
per-test output expansion, the failing-test stepper with search/deep-link
re-basing, "Find test", "View in timeline" from every output view, the
365-day "Earlier runs" lookback, vi-ish keys), the endpoint analysis was
repeated to confirm nothing degraded. Same method: origin/master worktree
vs the branch, interleaved runs, dev copy (218 MB, 540,192 runs — NOT
production).

**No regression.** The decisive check was a CROSS test — new code serving
a fresh copy of the baseline's own database file, interleaved with old
code — because the first comparison confounded code with file layout (the
v7 file carries script_hours pages). Interleaved medians, same file:
`/api/summary` old 61.7/59.4/62.6 ms vs new 60.9/58.0/58.9 ms;
`parts=headline` ranges overlap entirely (33–48 old, 33–43 new), with the
single worst rep belonging to the OLD build. This machine's own
run-to-run band (p95 78–133 ms on an identical build) is wider than any
before/after delta observed.

**The refinements' own paths, measured** (current build, dev copy):
default `/api/timeline` 11.6 ms median (unchanged); row expansion 1.8 ms;
the on-demand year view (`days=365`, 45 blocks) 27.6 ms median / 43 ms
p95 — read from the bucket tables only, fetched only when "Earlier runs"
or an old deep link asks; the search box's debounced suggestion query
(`/api/dashboard?q&limit=20`) 9.5 ms median.

**Import path: not re-measured, by argument rather than omission** —
`git diff` since the measured commit shows storage.py untouched (the only
backend change is api.py's day-cap constant), so the first pass's numbers
stand: fresh unchanged, no-op re-push still write-free, worst-case
all-flip batch +14%.

Production caveat as always: these are dev-copy constants; the mount sets
the real ones, and the migration probe on a prod copy remains the gate.

## The keep-alive flake WAS a bug — two of them (2026-08-04, evening)

`KeepAliveTest::test_a_reclaimed_connection_is_always_announced_first`
failed on a DOCS-ONLY commit's CI run. Looped locally: ~1 failure in 20
standalone, ~1 in 14 inside the pool suite. The guard was not flaky; the
server was, at low probability, doing exactly what the guard forbids —
closing a connection the client believed open, losing the request in it.

**Bug 1, the big one: a timed-out read poisons the reader, permanently.**
`socket.makefile` readers set `SocketIO._timeout_occurred` the first time
any read times out; every later read raises `OSError("cannot read from
timed out object")` — including reads that would have found a request
waiting. `_wait_for_request` polled by peeking WITH the poll timeout, so
the first quiet quarter-second broke the reader. Consequences, both
confirmed by standalone repro:

- The 5-second keep-alive idle window was FICTION. Every connection died
  at the first 0.25s poll tick, via the OSError branch. Browsers mostly
  papered over it (idempotent GET retry), which is why nobody saw it.
- A request arriving after one quiet tick was UNSERVABLE — the reader
  refused to see it, the loop closed the connection, and a non-retried
  POST died. That is the loss the flake was reporting.

Fixed by never letting `rfile` experience a timed-out read: the waiting
moved to `select.select` on the socket, and every peek is non-blocking
(raw reads return None harmlessly in that mode; the poison flag is never
set). EOF is distinguished by peeking after select reports readable.

**Bug 2: reclaim raced an arriving request.** The loop checked contention
BEFORE peeking, so a request landing in the same tick the pool became
contended was closed unread. And since a response that announces
`Connection: close` never returns to the wait loop at all, EVERY reclaim
close is unannounced by construction — so the reclaim now (a) peeks
first, serving anything already arrived (the response announces the
close, because the pool is contended), and (b) gives a genuinely quiet
connection ONE grace tick before closing, converting "close under the
client's feet" into "serve the request that was already coming" for
everything but the truly idle. Worst-case reclaim latency ~0.5s, same
order as the ~0.2s previously measured and recorded.

**MEASURED (this machine):** before: 1 failure in 20 standalone runs of
the guard, 1 in 14 pool-suite runs. After: 0 in 30 standalone + 0 in 16
suite runs + full suite green. The residual race — bytes hitting the
socket in the microseconds between a final empty peek and the close — is
inherent to TCP and beyond a server's power to remove.

`YieldPeekTest` (4 tests) pins all of it deterministically: a buffered
request beats the reclaim, an idle contended connection still yields
fast, the 5-second idle window actually lasts 5 seconds, and a request
arriving after two quiet ticks is served. The KeepAliveTest guard keeps
its zero-loss assertion untouched.

Ships in the 2026-08-04 drop (it is a `server.py` change — the restart
the operator note already mandates covers it).
