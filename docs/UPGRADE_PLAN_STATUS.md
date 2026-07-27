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
| WP-7 | Sortable columns | pending | |
| WP-6 | Time analysis tab | **done** | `/api/time`, 908 tests |
| WP-8 | Last pass + flaky signal | pending | |
| WP-9 | SQL portability groundwork | pending | |
| WP-10 | MariaDB export tool | pending | |
| — | Performance pass | pending | |

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
