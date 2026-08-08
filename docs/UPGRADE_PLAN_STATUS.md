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

## Pre-migration review of the MariaDB runbook and tooling (2026-08-07)

The data migration is about to be run for real, on a new service account on
the web server, so the runbook, both tools and their guard tests got a full
review first. Suite: **1333 OK (skipped=1)**. The read-only audit was run
end-to-end against `.scratch/perf.db` (dev copy at v7, 540,192 runs): every
gate green, chosen sizes exactly the documented defaults (64/255/255).

**The two findings that change the plan:**

1. **§F is still not done.** `run_server.py` takes only a SQLite path;
   nothing in `testboard/` imports the driver. Whatever is loaded into
   MariaDB is a copy nothing reads yet. So the server-side work can be §A,
   the §C preflight and the §E.1 dry run — even a full load — but not a
   user-facing cutover: no freeze is needed, the feeder stays on, and the
   SQLite file remains the live database, untouched (the tool opens it
   `mode=ro` throughout).
2. **The tool must match the database version.** Production is at v6
   (master); this branch's exporter learned `script_hours` (v7). A newer
   tool against an older database is *stopped* by the `source_tables` gate,
   but an older tool against a newer database **silently skips the tables it
   has never heard of — and the verification, generated by the same tool,
   agrees with the omission**. Now documented in §C's preamble. Practically:
   run the migration tooling from the deployed master checkout against the
   v6 production file, or ship WP-18 first and use this branch against v7.
   Do not mix.

**Runbook corrections made (doc describing a design the code got right):**

- §B.6 claimed InnoDB would enforce the `REFERENCES` clauses. The generated
  DDL deliberately declares **no** FK constraints — reproducing SQLite's
  non-enforcement so §F inherits identical semantics. §B.6 rewritten; the
  audit's orphan advice string updated to match (orphans still block: they
  are latent corruption, and the migration is the cheap moment to find
  them); the appendix FK row reworded; two test docstrings carried the same
  stale rationale and were fixed.
- §B.3 claimed identity columns carry explicit collations. They *inherit*
  the database default — which is what the `database_collation` gate and the
  collation probe exist to protect. Rewritten to describe reality.
- Exit-code line fixed: `SystemExit` paths (missing file, no connection,
  unindexable sizes) exit 1, not 2; only `3` means "a check said no".

**Traps now documented before someone finds them at 2am:**

- Under `LANG=C`, Python 3.6 on RHEL 8 cannot print the tool's `§` and it
  dies at its first output line with `UnicodeEncodeError` — always *before*
  doing anything, so the fix is `export LANG=en_US.UTF-8` (§C preamble).
- PyMySQL does not treat `host = localhost` as "use the socket" the way the
  `mysql` client does; on a same-box install the cnf needs a `socket =` line
  or the grants must match the TCP identity (§A.9).
- `IDENTIFIED VIA ... USING PASSWORD('...')` needs MariaDB 10.4+; the 10.3
  stream (RHEL 8's default) wants the hash (§A.4).
- The §E.1 scratch database needs §A.3's collation and §A.4's grants
  repeated on it, plus its own option file with `database =
  testboard_dryrun` (§E.1).

**Cross-checks that came back clean:** exporter `TABLE_ORDER`, DDL columns,
nullability and secondary indexes verified column-for-column against
`MIGRATIONS` 1–7 — no drift. `LONGBLOB` output, `VARCHAR(40) ascii_bin`
fingerprint, `VARCHAR(13)` hour, `VARCHAR(26)` stamps all match the data.
The drift guards hold: a new *table* cannot be silently skipped
(`test_every_exported_table_is_in_the_order` is what forced this branch's
exporter update), and a new *column* missing from the DDL fails the load
loudly (LOAD DATA names a column the target lacks) rather than silently.

CLAUDE.md's stale facts were also brought up to this branch's state
(1,333 tests, seven migration entries, WP-15's claim now 8).

**Still not verified, and cannot be from here:** everything §E.1 exists
for. There is no MariaDB in this environment or CI; the fake-server tests
prove the tool's decisions, not the SQL. The dry run on the real hardware
is the other half of the evidence, and it also produces the only honest
downtime estimate.

## WP-19 — the dashboard learns MariaDB (2026-08-07)

§F of the runbook, built in one day as six commits, each leaving the suite
green, folded into the 2026-08-04 drop and the whole re-dated 2026-08-07.
No migration version consumed. The user's requirement, set before the first
line: **SQLite stays a permanent first-class backend** — quick zero-setup
start-up for a second instance — and MariaDB is opt-in per instance.

**The measurements:**

- Suite locally: 1333 → **1385 OK** (skipped=1) across the six commits; with
  the MariaDB variants active, **1749 OK**.
- CI: all four legs green on the first push of the new job —
  `python36-mariadb` (ubi8 python-36 + `mariadb:10.3` service, prod's
  stream) ran **1749 tests, OK (skipped=16)**: the 5 baseline ubi8 skips
  plus exactly the 11 reasoned MariaDB exclusions. The port is proven on
  prod's interpreter against prod's database version.
- Local dev server (MariaDB 12.3.2 via winget, x86_64 under emulation,
  functional evidence ONLY): the 364 generated dual-backend tests pass in
  ~40 s. No performance number was taken from it and none ever will be.

**Shape (details in the runbook §F.2 and the WP-19 plan section):** one
Storage, two backend objects; qmark-canonical SQL forever with execute-time
translation (three rewrites, two composed fragments, every one pinned);
FOUND_ROWS/binary_prefix/strict-assertion/lock-timeout at connect;
ping-on-borrow reconnect, never inside a transaction, never retrying a
statement; no DDL on MariaDB ever — schema_version equality or refusal.

**What the first real server contact caught** (each now pinned by a test):
derived tables under EXCEPT need aliases; PyMySQL fetchall returns a tuple
where sqlite3 returns a list (cursor proxy keeps sqlite3's convention); a
BOM from a Windows editor silently emptied an option file (utf-8-sig);
seven test helpers had sqlite3.connect plumbing where store._conn() was the
backend-agnostic intent; eleven tests are ABOUT SQLite and skip on MariaDB
with reasons (tests/test_mariadb_backend.py::EXCLUDED_TESTS).

**Guard evolution, per the guard's own instruction:** NotYetWiredTest said
to delete it the day storage imported the driver; it became
DriverImportAllowlistTest instead — the rule narrowed rather than died, and
the driver's serving-path blast radius is exactly testboard/mariadb.py.

**Corrections made while documenting:** the runbook said 8 PRAGMAs (5), 59
execute sites (135 + 1 executemany), and — twice, including in the
2026-08-07 morning revision — that testboard does not set
`PRAGMA foreign_keys=ON`. storage.py:~1323 sets it on every connection;
§B.6 now records the correction rather than papering over it, and the no-FK
MariaDB schema stays right for the reasons stated there.

**Not verified, and where it is written down:** the drop note's
NOT-verified list (docs/drops/2026-08-07.md) — no MariaDB has served this
app outside CI's container and the local emulated install; the RHEL 8
host's 10.3.39, socket auth, SELinux and the unit file wait for the §E.1
dry run; MariaDB performance is unmeasured everywhere.

## The 2026-08-07 drop went live the same day it was cut (2026-08-07, later)

Deployed to production by the operator during the afternoon. **Migration 7
completed on the live database** — ~4.4M runs on the network mount — in the
**30 s–2 minutes** bracket (operator-reported, not precisely timed; recorded
as the bracket rather than dressed up as a number). That is well above a
linear scaling of the dev copy's 3.2 s, which is the network mount's round
trips: future backfills of this shape should budget minutes. **The Timeline
is accepted** — the first human eyes on it liked it. SQLite serving is
confirmed unchanged in production, which was WP-19's first requirement.

Consequences recorded in the drop note and handover: production is at
schema v7, so the MariaDB migration must run from THIS branch's checkout
(its exporter knows script_hours; an older tool would silently omit the
table and verify the omission as agreement). Still open: confirm the box
runs the final drop commit 310f1c0 (What's new saying "7 August 2026" is
the tell), push the branch to master, then §A → §C → §E.1 dry run → cutover.

## Runbook simplified for the install we actually have (2026-08-07, evening)

Operator decision: one box (dashboard and MariaDB on the same host, socket
connections) and ONE database account — the app/migrate split judged
overkill for a single-operator install. What that trades away is recorded
in §A.4 where the decision lives: the serving app now holds DDL rights it
never uses; accepted because the backend refuses DDL by design, everything
is parameterized, and the split is restorable with two GRANTs and a file
edit. One account also means ONE credentials file — /etc/testboard/db.cnf
(root:testboard 0640, with the socket= line) now serves the migration tool,
the dashboard and the mysql-client fallback alike; the personal
~/.testboard-migrate.cnf lifecycle is gone.

Kept deliberately: §A's section NUMBERING (A.1 became the one-box statement
plus the localhost trap, A.6 a retirement stub) so every cross-reference in
tool messages, tests and this log stays true; and the §A.1 socket-vs-TCP
explanation, which is the same-box trap par excellence. The two-host,
two-account revision lives in git history.

Ripples: three tool advice strings de-two-account-ed (grant probe,
target-not-empty, --config help; the pinned grant-probe assertion moved
from "testboard_app" to "§A.4"); dbconfig's permissions warning narrowed to
WORLD-readable only — the canonical file is group-readable BY DESIGN and a
warning that fires on the documented-correct state is one nobody reads.

Suite after: 1385 OK (skipped=1).

## Two DB accounts restored; the runbook survives a fresh-eyes review (2026-08-07, late)

The single-account simplification lasted a few hours: the operator had read
"two accounts" as two LINUX users. There is one Linux user and there are
two MariaDB accounts; §A.4 is back to the app/migrate split (both grants
@'localhost' — the same-box, socket-only simplification stays) and now
counts the accounts out loud in the preamble so the words cannot collide
again. Tool advice strings and the pinned grant-probe assertion restored
with it.

Then a fresh-context agent read the whole runbook as its actual audience
will — a 20-year Linux dev, not a DBA, following it exactly once — and
found what familiarity had hidden. Two blockers, both real:

- **§E never said to switch the dashboard to MariaDB.** The flip existed
  only as a comment in §A.10's unit file. An operator could execute
  §C-§E perfectly, verify against a still-SQLite dashboard, restart the
  feeder into the wrong backend, and end the night having migrated
  nothing. §E.4 now carries the service switch as an explicit step, and
  §E.5 refuses to restart the feeder before it.
- **The app credential was never exercised before cutover night** — a typo
  in /etc/testboard/db.cnf surfaced only at the E.4 restart. §A.11 now
  authenticates it as part of hand-over (the preflight failing its grants
  check with "cannot create tables" doubles as proof the data-only account
  is data-only).

Plus eleven CONFUSING and nine NIT fixes, all applied: who runs §A.9 (the
~ lands in /root otherwise), a separate dry-run export directory (the
exporter refuses non-empty, and finding out mid-freeze is the wrong time),
the C.2/C.3/C.4 numbering map, which audit figure feeds --max-blob-bytes,
the mysql-prompt note before the first bare SQL block, the sha256 fallback
covering both accounts, buffer-pool and unit-file placeholders marked as
placeholders, /var/lib/mysql disk budgeted, a workable verify diff recipe,
$HOME over tilde-after-equals, the placeholder-password trap, prompt-marker
paste warning, and the E.2 high-water-mark note now has a purpose (E.5
confirms the catch-up passed it).

Suite: 1385 OK (skipped=1) — the account-restore touched three tool advice
strings and one pinned assertion; the review fixes are doc-only.

## WP-21 — branches and builds beside mainline (2026-08-08)

Built on `wp-21-streams`, cut from `wp-20-products`: the whole of
`docs/STREAMS_PLAN.md` §3, migration 9. Streams (`(product, kind, name)`,
mainline the un-droppable id 1) resolved lazily inside the import
transaction; `runs.stream_id`/`comments.stream_id` added, `latest_runs`
rebuilt with `stream_id` leading its key (SQLite cannot widen a PRIMARY KEY
in place — CREATE new / INSERT..SELECT / DROP / RENAME, five indexes
recreated); the legacy `(environment, script, test_name, start_time)`
UNIQUE on `runs` predates streams and cannot hold two streams' runs at the
identical instant, so that collision is a per-record REJECTION, never a
silent overwrite or misfile. Mainline provably unaffected:
`activity_hours`/`script_hours` and un-retirement both mainline-only by
construction, every estate-wide read hardcoded to mainline with no override
parameter. `streams_seen` in the import response — its ABSENCE (not `[]`)
is how a new feeder detects a server that has never heard of streams and
aborts loudly rather than filing everything into mainline silently.
Frontend: the Build picker, the branch band, the delta view (five tiles,
tabbed paginated tables, an agree/coverage line pair, both sides'
freshness), the test-detail compare strip, comment "posted from" tags, and
a Watchlist `s:` verdict card — all through a shared `compare.js`, all
zero-visible-change on a mainline page (a single guarded, early-returning
branch in `app.js`'s `init()`; nothing below it runs when unscoped).
`tools/drop_stream.py` is the `drop_environment.py` analogue, refusing
mainline unconditionally. Migration 9 measured on the 220 MB dev copy
(NOT production): 0.883 s alone (v8→v9), 0.6 s of it the `latest_runs`
rebuild (12,008 rows); combined with WP-20's migration 8, v7→v9 (the actual
production upgrade path) measured at 0.806 s.

Two real defects were caught only by driving the actual frontend JS
against a live server through the project's node DOM-shim harness (not by
unit tests, which all declare a product before touching a stream — exactly
the case that was broken): a comment's "posted from" tag was never wired
into `test.js` despite the backend already returning the data; and the
Watchlist's `s:` card was silently all-zero for any stream whose product
is `""` (the common case on a site that has never declared a product) —
`_handle_watch` resolved environment scope through a dict that is ALWAYS
empty for `""`, fixed to special-case it the way
`Storage.environments_for_product("")` already did on the single-stream
path.

Suite: 1645 OK (skipped=1) SQLite-only; 2152 OK (skipped=16) dual-backend
(this dev machine's local MariaDB, `.scratch/mariadb-test.cnf`) — both
re-run after every backend change and after the two fixes above.

## First human use of the branch dashboard finds four gaps (2026-08-08, later)

Found on the same day the branch dashboard was first opened in a real
browser (still the only page of this project ever to receive that):
clicking a delta-table row landed on the MAINLINE test page (`eb05c7a`,
one line — `buildDeltaRow()` built its link without the page's own stream
id, fixed and guard-tested). Then, working with the user, four more:

1. **The branch band was dashboard-only.** A reader who followed a delta
   row to a test's own page lost every indication they were scoped to a
   branch — the compare strip alone was not loud enough. `renderBranchBand`
   is now exported and shared between `index.html` and `test.html`, and
   "Back to mainline" generalised to "the current URL with only `stream`
   removed" (preserves `environment`/`script`/`test_name` on the test page,
   where a fixed `index.html` target would have landed on the wrong page).
2. **Triage from a branch didn't actually work.** The delta table shipped
   chips-only. Added the same assignee select and inline Review expander
   (output, "View in timeline", a comment box) every other list in the app
   already has — assigning from a branch row assigns the SAME
   (environment, script, test_name), never a stream-scoped copy of
   ownership. Retirement is refused by construction (the shared panel is
   simply never given a staleness cutoff on a branch row, so its own gate
   never opens) — retirement stays mainline-only per §3.4.
   `/api/compare`'s paginated rows gained `stream_run_id`/
   `stream_start_time` (the branch's own run, null exactly when there is
   nothing to review) and the triple's current, unpartitioned `assignee` —
   both already live on `latest_runs`/`current_assignments`, no new query
   shape.
3. **Assignment origin folded into migration 9**, still unshipped when
   found, rather than spent on a migration 10: `assignments`/
   `current_assignments` both gained a nullable `stream_id`, the exact
   shape `comments.stream_id` already established — an annotation of WHERE
   an assignment was made, never a partition of WHO it targets. `PUT
   .../assignee` accepts it optionally; every existing caller that never
   sends it is unaffected.
4. **Open actions shows the origin**: a "branch feat/x" tag (batch-resolved
   per page, the comments-endpoint pattern) and a server-side
   `origin=branch`/`origin=mainline` filter next to the existing owner
   chips — the same "server-side, not a client reshuffle" rule the
   existing owner filter already follows, and the same "absent, not just
   empty, when nothing needs it" rule every WP-20/WP-21 addition follows
   (`/api/summary`'s `assignment_streams`, empty list is the signal).

Verified beyond the source-level guard tests by driving all four against a
live scratch server through the DOM-shim harness — extended further this
pass (`insertBefore`/`nextSibling`/`remove()`, `document
.createDocumentFragment()`, a `find()` guard against text-node leaves;
`actions.js` had never been driven through row-rendering before, only
checked for its product-switcher mount). Confirmed the assignee select
showed a REAL pre-existing value rather than defaulting to "Unassigned"
(the specific wrong-payload failure the coordinator's own review flagged
as the likeliest bug), the Review panel opened onto the branch's own
captured output, no retire control appeared anywhere, both branch bands
and their back-links were correct, and the Open actions filter/tag
round-tripped a real assignment including the server-side re-fetch on a
filter click.

`docs/STREAMS_PLAN.md` §3.6 updated to record all four as part of WP-21's
actual shipped scope (found in first use, not deferred); §4 gained an
explicit note that the user has asked for the test-page per-stream
("Every build") dropdown, so WP-22 planning does not drop it.

Suite: 1688 OK (skipped=1) SQLite-only. Dual-backend re-run on the final
tree; see `docs/drops/2026-08-14.md` for the count captured there rather
than duplicated here.

## WP-22 — release builds + compare-any-two (2026-08-08, same day, `wp-22-builds`)

Cut from `wp-21-streams`'s tip. `docs/STREAMS_PLAN.md` §4. **No migration**
— every piece reads WP-21's `streams` table and the
`(environment, script, test_name)` index migration 9 already created.

**Backend.** `/api/compare?baseline=` loses the "must be mainline"
restriction: any stream of the SAME product as `stream=` is now accepted
(mainline stays the one universal exception on either side, since its
`product` is `''` by construction). A cross-product pairing 400s naming
both products — the environments filter both sides of the SQL join share
is resolved from `stream='s` own product alone, so an unchecked mismatch
would not have errored, it would have silently compared against the wrong
(empty) environments on the baseline side. `Storage.compare_counts_many`
gained an optional per-stream `baselines` argument so a build-kind
Watchlist card can default to its predecessor build without paying one
query per card — every distinct baseline id folds into the SAME
`IN`-clause query the method already ran, keeping
`test_query_count_does_not_grow_with_s_card_count` green for a real reason.
`Storage.previous_builds` resolves "the nearest earlier same-product build
by `last_seen`" in ONE query bounded to the distinct products among the
requested builds. New `Storage.stream_results_for_triple` +
`GET /api/tests/{env}/{script}/{test}/streams` (plus a small
`Storage.product_for_environment` single-row lookup so the frontend can
resolve which product's FULL stream list to union against): a triple's
latest result on every stream that has one, newest first — deliberately
NOT product-filtered at the query level, since the triple's `environment`
is already the discriminator and filtering again by the CURRENT
`environment_products` mapping would silently drop a row after a remap.
Widened the WP-21 guard test that pinned "non-mainline baseline is
refused" into same-product-allowed / cross-product-refused /
mainline-always-exempt cases — that restriction was WP-21's own stated
scope boundary, not a production finding, and lifting it was WP-22's job.

**Frontend.** Every place that built its wording from the literal word
"mainline" — the delta view's heading, column headers, freshness lines,
the branch band, the Watchlist's stream card — now reads it from the
baseline's own identity (new `compare.js` export `streamLabel()`), because
a build's baseline is routinely a PREDECESSOR BUILD once this ships, not
mainline. The build-scoped dashboard gained a "Compare to" box (a plain
datalist combo, visible only for a `kind='build'` stream — a branch
dashboard's cost and appearance stay exactly what WP-21 shipped) defaulting
to the previous build by `last_seen` where one exists, else mainline, plus
a build-only framing line ("Built … — nothing has run since." / "…last
ran …"). The Build picker gained a Builds `<optgroup>`, newest first by
`last_seen` (branches keep their own group, unchanged order) — "searchable"
was judged satisfied by native `<select>` type-ahead at realistic stream
counts rather than converting the picker to a second datalist combo, to
avoid churn against `StreamPickerTest` for a nice-to-have. The test page
gained the "Every build" disclosure (this triple's latest result on every
stream of its product, newest first, unioned by stream id against the full
`/api/streams` list so absent streams render NO RESULT rather than being
silently dropped or omitted) and the stream switcher near the top of the
page — the SPECIFIC thing the user asked for after WP-21's first human use,
recorded in `docs/STREAMS_PLAN.md` §4.1 so it would not be dropped when
this drop was planned. Watchlist `s:` cards gained a "N tests failing in
`<name>`" headline (§4.1's literal wording) ahead of the existing five-tile
stat grid.

**Item 7 (superseded-run ghosting) was verification, not new code**: a
build rebuild (second import under the same `build` name) was checked end
to end — same stream, no duplicate, newer run wins as `latest`, older run
still shows in history. Correction to the plan's own wording, found by
checking rather than assuming: "superseded runs render as ghosts" is not
literally true — the history table draws every row solid, with no
ghost/outline distinction. Said plainly in the operator note rather than
left for someone to discover later.

**Live verification, this session** (the second time this project has
driven real frontend JS against a real running server, after WP-21's first
human use): a scratch server seeded with two products, one feature branch,
and TWO builds of the same product (older + newer, an
overlapping-but-changed test set) so previous-build defaulting was actually
exercised. Confirmed via the node DOM-shim harness (`.scratch/`,
gitignored): the Builds group and its ordering; the newer build's delta
view defaulting to the older build (band text, heading, column header, the
Compare-to box's preset value, and the tile counts all checked against the
real numbers); the older build (no predecessor) falling back to mainline;
a branch-scoped dashboard showing ZERO of the WP-22 additions; the
Watchlist `s:` card's wording naming the real predecessor, never falling
back to "mainline"; the test page's "Every build" table (4 rows, newest
first, current scope marked "you are here") and stream switcher; and the
cross-product refusal, live, both directions via `curl`.

**This caught one real defect before it shipped**: a JSDoc comment in
`compare.js` read "built from `*streamMeta*/*baselineMeta*'s` own
kind/name" — the literal `*/` mid-sentence closed the block comment early,
turning the rest of it into code and producing a syntax error. `node
--check` on the same file, run earlier in the same session, did NOT catch
it (it does not fully parse the module the way a real ES module loader
does); only the DOM shim's actual dynamic `import()` — which loads the
real module graph, the way `<script type="module">` does in a browser —
failed on it. This would have broken `index.html` AND `test.html` outright
(both import `compare.js`), and no unit test in this project (all
static-analysis, no JS runtime) could have caught it. Fixed same-session,
before the rest of the verification pass ran; the checks above are
POST-fix. This is now the concrete case for why this project's frontend
verification method insists on a real dynamic import over a syntax-only
pass, beside the two defects WP-21's first human use already found.

Suite: 1736 OK (skipped=1) SQLite-only; 2303 OK (skipped=18) dual-backend
(this dev machine's local MariaDB, `.scratch/mariadb-test.cnf` — two new
query-count tests needed the same `EXCLUDED_TESTS` treatment every other
`sqlite3.set_trace_callback`-based test already gets). CI's own
`python36-mariadb` leg has not been observed against this branch.
`docs/drops/2026-08-14.md` rewritten to cover WP-20+21+22 as one coherent
combined drop, per house rule.

## WP-22 — three fixes from a second-pass review (2026-08-08, same day)

A review pass of the WP-22 work above found three real issues before
they shipped, none caught by the first DOM-shim pass:

1. **`pickDefaultBuildBaseline` (frontend) disagreed with
   `Storage.previous_builds` (backend) on a same-`last_seen` tie**,
   despite the JS docstring claiming an exact mirror — the frontend
   excluded a same-timestamp candidate outright where the backend's
   `ORDER BY last_seen, id` includes the smaller-id one as the
   predecessor. For two builds sharing a `last_seen` (reachable with
   fixture data or a CI that stamps a whole batch identically), the
   dashboard's default and the Watchlist card's default could name two
   different predecessors for the same build. Fixed the JS exclusion to
   match the SQL's `<` / `= and <` rule exactly; a synthetic-data
   DOM-shim check (pure function logic, no server needed) pins both
   directions.
2. **`/api/compare`'s cross-product refusal was one-sided**: it only
   fired when NEITHER side was mainline, so `stream=<mainline id>&
   baseline=<a real product's stream>` passed through and silently
   compared against mainline's own product (`''`, matching nothing the
   baseline actually ran) — the exact "wrong environments, no error"
   failure the guard exists to prevent, reachable from the direction
   nobody checked. No shipped frontend constructs this
   (`getSelectedStreamId()` is null for mainline, never the literal id),
   but the endpoint is documented as symmetric in the README regardless.
   Simplified the guard to drop the one-sided clause — "baseline is
   non-mainline and its product differs from stream's" is symmetric by
   construction and costs no new query. New regression test drives the
   previously-broken direction.
3. **Neither new control's `change` handler had ever actually been
   fired** in the first DOM-shim pass — every check read rendered DOM
   state (option text, preset values) but none dispatched the event
   that navigates. This is the EXACT class of bug WP-21's first human
   use found (`eb05c7a`: a control that rendered correctly and linked
   to the wrong place). Added real `change` dispatches to the Build
   picker, the "Compare to" control, and the test page's stream
   switcher, asserting on the resulting `window.location.href` — all
   three confirmed to navigate correctly, not merely render correctly.

Also converted the Build picker from a `<select>` to the same
input+datalist combo pattern the "Compare to" control uses: a native
`<select>`'s type-ahead only prefix-matches, so §4.1's own "searchable
(substring on the name as written)" was not actually satisfied — a
release manager typing `rc2` against `2026.9.1-rc2` would have found
nothing. This reverses the §4.4 "as built" note's earlier judgement call
(kept native `<select>`) after a review pass showed the cited reason
(test churn against `StreamPickerTest`) did not hold: none of that
class's five assertions touch `<select>` mechanics.

Suite: 1739 OK (skipped=1) SQLite-only; 2307 OK (skipped=18) dual-backend
— both re-run after every fix in this entry.

## WP-23 — long-running branch streams (2026-08-09, `wp-23-longrunning`)

Drop 4 of `docs/STREAMS_PLAN.md`, built on `wp-22-builds`'s tip. A
months-long feature branch with its own nightly CI is a second mainline
in all but name; this gives it its own trend/staleness/triage instead of
only the WP-21/22 delta view. Full account of decisions made during
implementation is `docs/STREAMS_PLAN.md` §5.4 ("as built") — this entry
is the chronological record, that one is the reference.

**Migration 10** claims the version WP-15's parked reservation was
sitting on (moves to 11 — fifth such swap, see `UPGRADE_PLAN.md` §1).
`activity_hours`/`script_hours` rebuilt with `stream_id` in their PRIMARY
KEY, the migration-9 `latest_runs` precedent exactly (existing rows
copied with a literal `stream_id = 1`, not re-aggregated from `runs` —
both tables had been mainline-only since migrations 6/7). MEASURED on a
copy of the dev database (220 MB, 540,192 runs, 12,008 tests) THIS
session: entry 10 alone **0.038–0.041s** (from v9); entries 8+9+10
combined from v7 (production's current version) **~0.17–0.18s** — both
numbers reproduced across repeated runs. This differs from the
2026-08-14 note's earlier v7→v9 figure (0.806s) recorded in the WP-21
session; no attempt was made to reconcile the two beyond noting the
difference here, per CLAUDE.md's "measure, do not estimate" — what is
reported is what was actually measured this session, on this machine, at
this time.

**The writer's WP-21 skip is deleted.** `activity_hours`/`script_hours`
are now maintained for every stream inside the import transaction, keyed
by its own `stream_id` — `_apply_activity_deltas`/
`_apply_script_hour_changes`'s dict keys gained a leading `stream_id`,
and the two `if stream_id == MAINLINE_STREAM_ID` guards in
`upsert_runs` are gone. The guard test this touches
(`DerivedTablePartitionIsolationTest`, "branch import leaves the tables
unchanged") is WIDENED, not weakened, per CLAUDE.md's rule — its old
assertion is now false BY DESIGN (a branch gains its own rows; that is
the entire point of this drop), so it now asserts PARTITION ISOLATION
instead, checked in both directions (branch-after-mainline,
mainline-after-branch) plus sibling branches against each other.
`ActivityHoursTest`/`ScriptHoursTest`'s own invariant comparisons were
separately widened to include `stream_id` in their GROUP BY, so a
stream_id bug would fail those too, not only the dedicated isolation
class.

**A sweep for WP-21-era cross-stream leaks**, prompted by an advisor
review before writing production code (write the failing guard first,
watch it fail, then sweep every unfiltered `FROM latest_runs`/
`FROM activity_hours`/`FROM script_hours`): `test_counts_by_environment`,
`script_test_counts`, `daily_result_counts` (plus its trend-memo cache
key), and `prune_runs_before`'s `prev_result` recomputation all read
without a stream filter. Each was correct before this drop only because
the tables held nothing but mainline's rows; once every stream is
maintained, an unfiltered read silently mixes a branch's numbers into
mainline's own coverage denominator, trend, or `prev_result` the moment
a branch reports into the SAME environment mainline uses. All four
closed in the migration-10 commit, plus `summary_rollup`/
`assigned_open_count`/`status_queue`/`status_queue_count`/
`duration_rollup`/`latest_run_time`/`latest_run_time_by_environment`/
`top_failing_scripts` gained a `stream_id` parameter (default mainline,
so no existing caller's behaviour changes) as the mechanism the "own
results" tab reads.

**Per-stream pass detection needed no change to `analytics.find_passes`/
`recent_cutoff`** — both are pure functions over whatever buckets/test
counts they are handed; scoping to a stream is the exact mechanism
`_pass_view`'s own docstring already documented for WP-20's `product=`
filter (restrict the inputs). The two clamps (36h fallback floor,
14-day ceiling) are therefore unchanged code, applying per stream
automatically — pinned by a test that a branch too sparse to have one
covered pass falls back to the same 36h window mainline would use in
the same spot. `/api/summary`, `/api/time`, `/api/timeline` all gained
an optional `stream=` (default mainline); guard tests import a branch
into a shared environment and assert the UNSCOPED response is
byte-identical before/after, closing the exact scenario the sweep
above found.

**The branch dashboard's two-tab header** (`static/app.js`): `init()` no
longer calls `initDeltaView()` directly — it calls
`initBranchDashboard(streamId)`, which shows a `#branch-tabs` header for
`kind='branch'` streams only (builds keep the unchanged WP-21/22
delta-only view). "Its own results" re-enters the mainline dashboard
body scoped via a new `appendStream()` helper (mirrors `appendProduct`,
threaded into `summaryUrl`/`queueUrl`/`browseUrl`); one-time listener
wiring was factored out of `init()` into `wireMainlineControls()`,
guarded by a module flag, so switching tabs repeatedly never stacks
duplicate listeners. Default tab: `/api/summary` gained `covered_passes`
(the count `_pass_view` already computes); `>=2` shows "Its own results"
first, and the caption states the actual count AND the threshold in its
own sentence, never a hidden constant. The frontend guard test
`DeltaViewTest::test_a_mainline_page_load_never_reaches_the_delta_view`
was WIDENED (not weakened, per CLAUDE.md) to check the new two-hop call
chain (`init()` → `initBranchDashboard()` → `activateDiffTab()` →
`initDeltaView()`) instead of the old direct call.

**Drift framing**: one new line in the delta view — "of N failing here,
M fail on `<baseline>` too" (N = `new_failures + both_failing`, both
guaranteed FAIL on the stream by `CompareCounts`' own definition; M =
`both_failing`). "Behind by N commits" stays void — not knowable, not
built, confirmed again rather than silently reconsidered.

**The Watchlist `s:` card decision**: kept as the vs-mainline verdict
only, per §5.2's own escape hatch, for two reasons recorded in
`static/watch.js`'s comment — the card is already full, and
`/api/watch` is architecturally O(cards) in Python but O(1) in queries
(`compare_counts_many` batches every requested stream's comparison in
one query, pinned flat by a dedicated test); a per-branch own-results
number needs its own per-stream pass-detection cutoff with no batched
multi-stream form here, and adding it would make N branch cards cost N
times that work.

**Live verification, this session** (third time this project has driven
real frontend JS against a real running server): a scratch database
(`.scratch/wp23verify.db`, gitignored) seeded with two products, a
short-lived one-off branch (1 covered pass) and a long-running branch (8
nightly covered passes over 8 nights: a standing regression on the
branch alone, plus one failure that also hits mainline from night 7),
served by `run_server.py`, driven by the node DOM-shim harness
(`.scratch/drive_branch_tabs.mjs`, gitignored) with real `click()`
dispatches on both tab buttons. Confirmed: band text, tab visibility,
caption wording (the exact covered-pass count and the "2 or more"
threshold, both literally in the sentence), the default tab selection
for BOTH branches (own for the regular one, diff for the sparse one),
the branch's own FAIL count (2) differing correctly from mainline's
whole-estate count, both tab-switch directions toggling the right
sections, the drift line's exact wording ("Of 2 tests failing here, 1
also fails on mainline too" — matching `/api/compare`'s own
`new_failures=1, both_failing=1` for that stream), and a genuine
zero-`stream=`-param mainline load touching none of the new elements —
seeded with the shim's `hidden` state matching the real shipped markup
first (the shim builds bare `<div>`s with `hidden=false` by default; it
does not parse index.html), a setup detail worth recording since it
produced four false failures before being caught.

**Not run this session**: CI's own `python36-mariadb` leg (mariadb:10.3,
production's stream) — only the dual-backend suite against this dev
machine's local MariaDB (12.3.2, functional evidence only, never a perf
number). Production-scale migration timing (dev copy only, ~4x smaller
than production per CLAUDE.md). No human has looked at the two-tab
header, the drift line, or the caption's wording in a real browser —
the DOM-shim harness proves wiring and DOM shape, not legibility,
layout, or whether two tabs plus a caption plus the existing toolbar is
too much for one screen.

Suite: 1750 OK (skipped=1) SQLite-only; 2329 OK (skipped=18) dual-backend
(this dev machine's local MariaDB) — both on the final tree, after every
commit in this entry.
