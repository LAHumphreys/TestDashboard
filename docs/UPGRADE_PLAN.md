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
- **Do not push and do not open PRs.** There is no remote configured, and
  publishing is a decision the user has not yet made.
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
| 5+ | *unallocated* | Claim by editing this table in the same commit | — |

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

### WP-0 — Migration registry guard *(blocks every schema package)*

**Why.** The schema is locked and three packages want to change it. Without a
guard, the first accidental edit to entry 1 is discovered by a production
database that will not start.

**Already decided.** See §1.1 for the four required assertions.

**Changes.** `tests/test_migrations.py` (new). No production code.

**Tests.** The file *is* the test. It must carry planted-regression cases
proving each detector can fail: an edited entry 1, a duplicate version, a gap in
the sequence.

**Risks.** Hashing raw DDL strings makes the test fail on whitespace-only edits.
Normalise (collapse runs of whitespace, strip comments) before hashing, and say
so in the docstring.

**Done when.** `python -m unittest tests.test_migrations` passes, and each
planted regression fails the assertion it targets.

---

### WP-1 — Extract the review panel into a shared module *(blocks WP-2, WP-8)*

**Why.** Items 2 and 7 both want triage's Review expander on the Open actions
page. It currently lives in `static/app.js` (`toggleReview`, ~line 772) and is
coupled to `state.openReviews`, `state.summary` and `isStale` — all home-screen
state. It cannot be reused as it stands.

**Already decided.**

- New module `static/review.js`. It exports one function:
  `attachReview(row, entry, button, options)` and owns its own open-panel
  registry keyed by `environment\0script\0test_name`.
- The coupling is broken by **injection, not by duplication**. `options` carries
  what the panel cannot derive: `{ onChanged, staleBefore, canRetire }`.
  `staleBefore` is an ISO string or null; the panel decides staleness itself
  instead of calling back into `app.js`.
- `app.js` keeps its current behaviour exactly. This package changes no
  behaviour at all — it is a pure move.

**Changes.** `static/review.js` (new); `static/app.js` (delete the moved code,
import it); `static/index.html` needs no change (module graph is followed from
`app.js`).

**Tests.** `tests/test_frontend_calls.py` — this package **will** trip it,
because the scan asserts which files define what. Widen it: add `review.js` to
the expected file set, and add an assertion that the review panel is defined in
exactly one file (the same shape as the `/api/users` rule, and for the same
reason — a second copy is a second set of bugs). Assert `app.js` no longer
defines `function toggleReview`.

**Risks.** The panel fetches run output on open. Two panels open at once on
different pages must not share a cache keyed by test only — key by `run_id`.
There is no JS test runner, so behaviour is verified by running the server and
clicking. Do that; do not skip it.

**Done when.** Home screen review panel behaves identically (open, assign,
comment, retire, close), `review.js` is the only definition, suite green.

---

### WP-2 — Review expander on Open actions **[item 2]**

**Why.** Open actions is where people work. Today it can reassign in-row but
cannot show output or take a comment, so every actual triage means leaving the
page.

**Depends on** WP-1.

**Already decided.**

- Same visual treatment as triage: a `Review` button in a trailing action cell,
  expanding a `review-row` beneath. Reuse `.review-*` CSS unchanged.
- Retirement **is** offered here (Open actions is a triage surface now — see
  WP-8), gated the same way it is on the home screen.
- After an action that changes the row, refresh the row, not the page. Full
  `refresh(false)` re-fetches a 100-row page and closes every open panel; that
  is the wrong behaviour when someone is working down a list.

**Changes.** `static/actions.js` (import and wire `attachReview`, add the action
column), `static/actions.html` (`<th>` for the new column).

**Tests.** Extend `tests/test_frontend_calls.py`: assert `actions.js` imports
the shared review module and does not define its own. Manual click-through.

**Risks.** `actions.js` currently calls `refresh(false)` from
`assigneeSelect`'s `onSaved`. With a panel open that is a jarring collapse.
Change it to update the row in place and leave the panel open.

**Done when.** A failure can be read, assigned, commented and retired from Open
actions without navigating away.

---

### WP-3 — Fix the result emphasis in triage **[item 4]**

**Why.** Reported from real use: *"the stylised 'Previous run' dominates the
subtle row-edge current pass/fail — it's misleading, making failures look like
passes."* This is a correctness bug in the visual encoding, not a preference.

Cause: in the `new_failures` and `fixed` queues the *previous* result is
rendered as a full solid `resultChip` in its own column, while the *current*
result appears only as a 3px `inset` box-shadow on the row edge
(`style.css:271-274`). The loudest thing in the row is the wrong value. In the
`fixed` queue the previous result is `FAIL` — so a fixed test reads as a
failure, and in `new_failures` the previous result is usually `PASS`, so a new
failure reads as a pass. Exactly backwards, both times.

**Already decided.**

- **The bug is in exactly two queues** — `new_failures` ("Previous run") and
  `fixed` ("Previously"). They are the only ones that render a solid chip for a
  value that is not the current result. Fix those two.
- **Do not add a constant column to the others.** `still_failing` is `FAIL` on
  every row and `unexpected_passes` is `UNEXPECTED_PASS` on every row; a
  per-row chip repeating the same word down the page is more of the visual
  noise this item is about, not less. `not_run` already has a result column and
  needs no change. Where the result is invariant for the queue, **state it once
  as a badge beside the queue heading** ("all failing"), not once per row.
- The rule to hold is therefore: *no queue may communicate a **varying** result
  by row edge alone.* In `new_failures` and `fixed` the current result varies
  (a `fixed` test may be `PASS`, `FAILED_AS_EXPECTED` or `UNEXPECTED_PASS`), so
  those two get a solid `resultChip` column, positioned **before** the
  previous/history column.
- The previous result is demoted to a **secondary, outlined** chip
  (`.chip-ghost`: transparent background, 1px border in the result colour, text
  in normal ink) and read as a transition: `PASS → FAIL`, with the arrow as a
  real character in its own muted span.
- Never colour alone: both chips keep their text labels. This also keeps the
  existing colour-blind story intact.
- Rename the columns to say what they are: `Previous run` → `Was`, `Previously`
  → `Was`. The transition reads left-to-right in one cell if you prefer that to
  two columns — either is acceptable, both must show the current result more
  prominently than the previous one.

**Changes.** `static/app.js` (`columnsFor`, ~line 690-750), `static/style.css`
(add `.chip-ghost`, strengthen `tr.result-*` edge from 3px to 4px).

**Tests.** No automated assertion is possible on visual weight. Add a note to
the CSS explaining *why* `.chip-ghost` exists so it is not "simplified" back
into a solid chip. Verify by screenshot against the live server, on all six
queues, and attach the before/after to the commit message.

**Risks.** The `fixed` queue's whole point is "this was failing and now
passes" — do not lose the previous result, only its dominance.

**Done when.** In every triage queue, the current result is the most prominent
element in the row, and no queue lacks an explicit result chip.

---

### WP-4 — Deactivate users **[item 3]** *(migration 2)*

**Why.** One person already has two usernames. The dropdown offers both, and
will keep offering both forever.

**Already decided.**

- Deactivation is **soft and reversible**: two nullable columns on `users`
  (`deactivated_at TEXT`, `deactivated_by TEXT`), presence of `deactivated_at`
  being the state. History is never touched — comments and assignment records
  keep the name they were made under, exactly as `test_retirements` leaves run
  history alone.
- `GET /api/users` returns **active users only** by default;
  `?include_inactive=1` returns all, each with its `deactivated_at`. The
  dropdown consumes the default and therefore gets shorter with no frontend
  change.
- `PUT /api/users/{username}/active` with `{"active": false, "changed_by": ...}`.
  Reactivation is the same call with `true`.
- **A user holding current assignments cannot be deactivated.** The request
  fails 409 with the count and the list of tests (capped, with a total). The
  UI's next step is "reassign these to…". Rationale: silently deactivating an
  owner leaves work assigned to a name nobody can select — an invisible queue.
  This is the single most likely way to lose track of open work, so it is a
  hard block rather than a warning.
- **`assigneeSelect` already injects `entry.assignee` into the option list even
  when the fetched list omits it** (`static/api.js:249-254`). That is exactly
  what makes rows owned by a deactivated user still render correctly. **This is
  deliberate — do not "fix" it.** Add a comment there saying so.

**OPEN (user's call, default given).** *Merging* two usernames — moving one
person's comments and assignments onto their other name — is **not** in this
package. Default: deactivate the dupe and reassign its open work by hand; there
are 3 users and 8 comments today, so the manual path is cheap. If merge is
wanted later it is a separate package with its own migration.

**Changes.** `testboard/storage.py` (migration 2; `set_user_active`,
`list_users(include_inactive=False)`, `assignments_held_by`), `testboard/api.py`
(list filter, new route), `static/api.js` (unchanged — verify), a management
surface: **default is a small section on Open actions**, not a new page.

**Tests.** `tests/test_storage.py`: round-trip deactivate/reactivate; migration
2 applied to a v1 database with existing users; deactivated user excluded from
`list_users()` and present with `include_inactive`. `tests/test_api.py`: 409
with assignments held, 200 without, unknown user 404, reactivation.

**Risks.** Case sensitivity. `users.username` is a SQLite `TEXT` primary key and
therefore case-**sensitive**: `Luke` and `luke` are two users today. Under a
default MariaDB collation they would collide — see the runbook, §B.3. Do not
"normalise" usernames as part of this package; that is a data migration with
its own consequences.

**Done when.** A deactivated user disappears from every assignee dropdown, keeps
their history, cannot be deactivated while holding open work, and can be
brought back.

---

### WP-5 — `latest_runs.duration_seconds` *(migration 3)* *(blocks WP-6)*

**Why.** One column, three unrelated wins:

1. It makes item 5's drill-down a `GROUP BY` over ~12k rows instead of an
   aggregate over 4.4M.
2. It removes the `duration` sort's `julianday()` call
   (`testboard/storage.py:432`) — currently a full expression evaluation over
   the whole filtered set on every sorted page.
3. `julianday()` is the **only** use of a SQLite date function in the codebase,
   and a portability blocker for WP-9. Removing it is free progress on the
   MariaDB move that can be verified tonight, without a MariaDB.

**Already decided.**

- `duration_seconds REAL NOT NULL DEFAULT 0` on `latest_runs`.
- Maintained in `_maintain_latest`, inside the same import transaction that
  writes the rest of the row. Not a trigger.
- Backfill in the migration, over `latest_runs` only (~12k rows, one pass,
  measured). **Not** over `runs`.
- `DASHBOARD_SORTS["duration"]` becomes `("lr.duration_seconds",) + _PK_ORDER`.
- Add an index only if measurement says the sort needs one. Measure first;
  record the number.

**Tests.** `tests/test_storage.py`: the column is maintained on insert, on
re-import of the same run, and when a newer run supersedes an older one; the
duration sort orders identically before and after the change (build a fixture,
sort both ways, compare). Migration 3 against a v2 database with existing
`latest_runs` rows backfills every row.

**Risks.** `duration_seconds` derived from `end_time - start_time` must use the
same `model.duration_seconds` the API already uses, or the sort and the
displayed value will disagree. Import it; do not recompute in SQL.

**Done when.** No `julianday` remains in the codebase, the duration sort still
sorts correctly, and every `latest_runs` row carries its duration.

---

### WP-6 — "Where is the time going" tab **[item 5]**

**Why.** Nobody currently has any way to see which environments, scripts and
tests consume the suite's runtime.

**Depends on** WP-5.

**Already decided — read this before designing anything.**

- **Scope is the latest run per test.** The drill-down aggregates
  `latest_runs.duration_seconds`, so it answers *"where did last night's time
  go"*. This is cheap (12k rows), it is honest, and the page must say so in a
  caption. A historical windowed version needs a real aggregate table and is
  explicitly **out of scope tonight** — do not start it.
- **Exclude stale tests, not just retired ones.** A test whose latest run is
  three weeks old still has a `duration_seconds`, and counting it makes the
  page claim time was spent last night that was not. Apply the same recency
  cutoff every other estate view uses (`_SUMMARY_RECENT_HOURS`, 36h) and report
  what was excluded — "excludes 214 tests that have not run in 36 hours" —
  rather than dropping them silently. This is the only way the numbers can be
  *wrong* as opposed to merely narrow, so it is not optional and the caption
  does not cover it.
- **Form: horizontal bar rows, with breadcrumb drill-down.** Not a treemap, not
  a sunburst. `static/charts.js` already exports `barRows()`, which labels every
  row, is readable at a glance, and degrades to a table. Treemaps hide small
  values, cannot label reliably, and encode magnitude in an area, which people
  read badly. Reuse `barRows` rather than writing a new chart.
- Levels: environments → scripts in that environment → tests in that script.
  Breadcrumb above the chart, every level clickable back. The current level's
  total is stated as a number, in words ("4h 12m across 3 environments").
- Every level ships a `<details>` data table beside it, matching the existing
  chart cards. Sortable by name and by duration (see WP-7).
- One new endpoint: `GET /api/time?group_by=environment|script|test` plus
  optional `environment` / `script` scoping. Returns
  `{"level": ..., "items": [{"key", "total_seconds", "test_count"}], "total_seconds"}`.
  Server-side aggregation only.
- Colour: this is **magnitude, not identity** — one hue, light→dark sequential,
  not the categorical result palette. Do not reuse the pass/fail colours here;
  they mean something else and reusing them will be read as status.

**Changes.** `testboard/storage.py` (`duration_rollup`), `testboard/api.py`
(`/api/time`), `static/time.html` + `static/time.js` (new), the `site-nav`
block in all four existing HTML pages, `static/style.css`.

**Tests.** `tests/test_storage.py`: rollup correctness including a test with
zero duration and an environment with no tests. `tests/test_api.py`: each
`group_by`, scoping, bad `group_by` → 400, retired tests excluded (they are not
in the suite; excluding them is consistent with every other estate view), stale
tests excluded **and counted in the response** so the page can say how many.
`tests/test_frontend_calls.py`: add `time.js` to the scan; assert it does not
fetch per row.

**Risks.** The nav is duplicated across **four** HTML files (`index.html`,
`actions.html`, `script.html`, `test.html`); a new tab must be added to all of
them or people will reach the page once and never find it again. Grep for
`site-nav`.

---

### WP-7 — Sortable table columns **[items 6 and 9 — the same item]**

**Why.** Only the "All tests" table sorts today.

**Already decided — the split matters, and getting it wrong produces a lying UI.**

| Table | Backed by | Sort where | Notes |
|---|---|---|---|
| All tests (home) | `/api/dashboard` | server ✅ *(already done)* | reference implementation |
| Open actions | `/api/dashboard` | **server** | paged — see below |
| Triage queues (home) | `/api/summary` | **client** | capped list — see below |
| Time analysis (WP-6) | `/api/time` | client | full result set, small |
| Script executions | `/api/scripts/.../executions` | client | full result set |

- **Open actions is paged (100 rows at a time) with an exact server-side
  total.** Sorting the fetched page in the browser sorts 100 of 1,340 rows and
  presents the result as if it were the whole set. That is a lying UI. It sorts
  server-side, via `sort=`/`order=`, refetching from offset 0.
- **Triage queues are capped at 500 entries** (`_SUMMARY_QUEUE_CAP`) with the
  exact total alongside. Client-side sorting is correct here **only when the
  cap has not been hit.** Once it has, sorting the loaded 500 by `failing_since`
  gives you the oldest failure *among an arbitrary 500*, displayed as though it
  were the oldest overall — the same lying UI rejected for Open actions one row
  above, and the cap note does not redeem it: the note says "500 of 1,340"
  while the sort implies "the top 500 by this key".

  **Hard requirement:** when `total > _SUMMARY_QUEUE_CAP` for the queue, either
  disable the sort controls with a visible reason, or make that queue sort
  server-side. Choosing "disable" is acceptable and is the default. What is not
  acceptable is sorting a truncated list and saying nothing.

  Nobody currently knows whether any production queue exceeds 500 — which is
  precisely why this must be a runtime condition and not an assumption that it
  never happens.
- **A new server-side sort key must be added to `DASHBOARD_SORTS`.** `ORDER BY`
  takes no parameters, so the whitelist is the security boundary — this is not
  optional. Every entry must end with the full primary key so paging cannot
  repeat or skip a row.
- Reuse the existing `.sort-btn` / `.sort-arrow` markup and `aria-sort`
  handling from `index.html`; extract the click-and-arrow logic into a shared
  helper rather than copying it a fourth time.

**OPEN (default given).** Sorting Open actions by *latest comment time* needs a
new whitelist entry and a join. Default: **not in this package.** Ship the
columns that map to existing keys (environment, script, test, result, last run,
assignee) and note the omission in the UI rather than shipping a sort that
silently does something else.

**Changes.** `static/sorting.js` (new shared helper), `static/actions.js`,
`static/actions.html`, `static/app.js`, `testboard/storage.py` only if a new
key is added.

**Tests.** `tests/test_api.py`: every `DASHBOARD_SORTS` key sorts both
directions and pages without repeats — assert by fetching two consecutive pages
and checking the union has no duplicate identity triples. That test is the one
that catches a missing primary-key tiebreak.

---

### WP-8 — Last pass, and "did it break or is it flaky" **[items 7 and 8]**

**Why.** Item 7: triage from Open actions needs the same evidence triage has —
above all *last pass date*. Item 8: a last-pass date alone cannot distinguish
"this broke on the 14th and has failed every night since" from "this fails one
night in three". Those need different responses, and today they look identical.

**Depends on** WP-2 (the surface it renders into).

**Already decided.**

- Open actions gains `Failing since` and `Last pass` columns, from the same
  `FailureStreak` the triage queues already use
  (`storage.failure_streak_bounds`).
- **Cost is the whole problem here.** `failure_streak_bounds` is three index
  seeks per row. `/api/summary` pays it only for the entries it shows, and only
  for FAIL rows. `/api/dashboard` must do the same: streaks are computed **for
  the returned page only**, behind an opt-in `with_streak=1`, never for the
  count query. A test must assert that the number of streak lookups equals the
  number of FAIL rows on the page and does not vary with `total`.
- **The flakiness signal is a stability ratio over the recent window**, not a
  new score: over the last N runs of the test (N = 20, bounded), the number of
  result *transitions*. Zero transitions after the break = "broken". Several =
  "flaky". `analytics._compute_flakiness` already computes transitions on a run
  window — reuse its definition so two places cannot disagree about what flaky
  means.
- **Presentation: a run-strip, not a number.** Twenty small cells, oldest to
  newest, one per run, coloured by result, with a text label beside it
  ("fails ~1 night in 3" / "failing every night since 14 Jul"). A ratio alone
  does not answer the question people are actually asking, which is *what does
  the pattern look like*. Colour alone is not sufficient — the sentence is the
  primary encoding and the strip supports it.
- New storage method `recent_results(triples, limit_per_test)` fetching the last
  N results for a **page** of tests in **one** query, not one query per row.
  Cap the input at the page size. This is the method most likely to be written
  as an N+1 by accident; the test for it must assert the query count.

**OPEN (default given).** Whether the run-strip appears in the row or only in
the expanded review panel. Default: **the sentence in the row, the strip in the
panel.** Rows are already dense and item 4 exists precisely because the row got
visually noisy.

**Changes.** `testboard/storage.py`, `testboard/api.py`, `static/actions.js`,
`static/app.js`, `static/review.js`, `static/style.css`.

**Tests.** Storage: one query for a page of triples; correct ordering; a test
with fewer than N runs. API: `with_streak` off by default; the page-only
guarantee. Analytics: the transition count agrees with
`_compute_flakiness` on the same window (assert against the existing function,
not against a hand-written expectation).

**Risks.** This is the package most likely to reintroduce a per-row fetch —
the exact bug `tests/test_frontend_calls.py` exists to catch. Read that file
before starting.

---

### WP-9 — MariaDB portability groundwork **[item 1, code half]**

**Why.** The migration runbook is `MARIADB_MIGRATION.md`. This package is the
part that can be **written and verified tonight, without a MariaDB server** —
because there isn't one in the dev environment or in CI, and an agent cannot
verify a driver port it cannot run.

**Depends on** WP-5 (which removes the `julianday` call).

**Already decided.**

- **No driver work tonight.** Vendoring a MySQL driver is an open decision for
  the user (runbook §F). Do not add one, do not add a dependency, do not write
  a backend that cannot be executed.
- What *is* in scope is making the SQLite-specific surface small, explicit and
  measured:
  1. **Inventory.** A test that enumerates every SQLite-specific construct in
     `storage.py` and asserts the list matches a committed expectation. Today
     that is 3 × `INSERT OR REPLACE`, 3 × `AUTOINCREMENT`, 1 × `julianday`
     (removed by WP-5), 8 × `PRAGMA`, 2 × `substr`, 9 × `sqlite3.` — across 59
     execute sites. When someone adds a tenth, the test tells them, and the
     runbook's translation table stays true.
  2. **Funnel the upsert.** All three `INSERT OR REPLACE` sites go through one
     private method with a docstring stating the semantics that matter:
     OR REPLACE **deletes and re-inserts**, so `runs.id` changes on re-import,
     whereas MariaDB's `ON DUPLICATE KEY UPDATE` updates in place. These are
     different behaviours, not different syntax — see the runbook, §B.5.
  3. **Pin the id-stability behaviour with a test now**, on SQLite, so the
     MariaDB port has something to be measured against: re-import an identical
     batch and assert what happens to `runs.id` and to the `run_outputs` /
     `latest_runs` rows that reference it. Whatever it does today is the
     contract; write it down.
  4. **Placeholders.** Note (do not yet change) that PyMySQL uses `%s`, not `?`.
     A future dialect seam handles this; a search-and-replace tonight would be
     unverifiable churn.

**Tests.** `tests/test_sql_portability.py` (new) — the inventory, with a planted
regression proving it fails when a new construct is added.

**Done when.** The runbook's translation table can be regenerated from the test's
expectation list, and re-import id behaviour is pinned.

---

### WP-10 — The MariaDB export tool **[item 1, transport half]** *(optional tonight)*

**Why.** The data migration needs no Python driver at all: export to files, and
the `mysql` client loads them. That makes this the *other* half of item 1 that
is fully verifiable on a machine with no MariaDB — the export half is pure
SQLite reading and text writing.

**Already decided.** Full specification is in
[`MARIADB_MIGRATION.md` §D.1](MARIADB_MIGRATION.md) — outputs, escaping, hex
encoding of blobs, foreign-key load order, index-after-load, and the required
behaviours (refuse to overwrite, stream rather than buffer, per-table timing,
non-zero exit on error). Build to that spec; if you find it wrong, fix the
runbook in the same commit.

**Changes.** `tools/export_for_mariadb.py` (new),
`tests/test_export_mariadb.py` (new).

**Tests.** Round-trip structurally without a database: build a small SQLite
fixture containing a tab, a newline, a backslash, a NULL, a non-ASCII test name
and a binary blob; export; parse the TSV back with the same escaping rules and
assert every value survives. Assert the load order satisfies the foreign keys.
Assert `verify_source.txt` contains every check listed in the runbook's §E.4
table — that assertion is what stops the two documents drifting.

**Risks.** The load half **cannot** be verified here. Say so in the tool's
docstring rather than implying it is proven. The hex round-trip on blobs is the
most likely thing to be subtly wrong and the least likely to be noticed.

**Done when.** `--out` produces a directory that matches the runbook's file
table, and the escaping round-trip test passes on adversarial input.

---

### WP-11 — Vendor the database driver **[item 1, dependency half]**

**Why.** Decided by the user on 2026-07-27: the driver is vendored so that
**nothing is installed on site**. This is what makes a MariaDB backend possible
at all under the deployment constraints — see the runbook §F.1 for why there is
no stdlib alternative.

**Already decided.**

- **PyMySQL**, pure Python, MIT-licensed, compatible with this repo's MIT
  `LICENSE`. Vendored under `third_party/pymysql/`, imported as
  `third_party.pymysql`. It is *present*, not *installed*: no pip, no wheel, no
  compiler, no environment to set up — the same property the feeder has.
- **The vendored tree is third-party code and is not held to this project's
  style rules.** `tests/test_python36_compat.py` scans project source; it must
  **exclude** `third_party/` from the annotation and typing-name checks — those
  encode *our* conventions, not correctness — while still asserting that every
  vendored file **parses under `feature_version=(3, 6)`**. That last part is the
  one that matters: it is the actual compatibility question, and it is the
  reason to vendor a specific version rather than "whatever is current".
- Record provenance in `third_party/README.md`: package, exact version, source
  URL, retrieval date, licence, and the sentence *"do not edit these files; to
  update, replace the directory wholesale and re-run the suite"*.
- Ship `third_party/pymysql/LICENSE` unmodified. Add a "Third-party code"
  section to the repo `README.md`.
- **Nothing imports it yet.** No storage code changes in this package. It is a
  dependency landing on its own so that, if it turns out to be wrong, reverting
  is one commit that touches nothing else.

**Changes.** `third_party/` (new), `tests/test_python36_compat.py` (exclusion +
the parse assertion), `tests/test_vendored_driver.py` (new), `CLAUDE.md`
(constraint wording), `README.md`.

**Tests.** `tests/test_vendored_driver.py`: the package imports; it exposes the
DB-API 2.0 surface actually needed (`connect`, `paramstyle`, the exception
hierarchy); its `LICENSE` file is present and says MIT; **it makes no network
call and starts no thread at import time**; every `.py` under `third_party/`
parses at `feature_version=(3, 6)`.

Note for the eventual port: PyMySQL's `paramstyle` is **`pyformat`**, not
`format` — it accepts `%(name)s` and positional `%s`. `sqlite3` is `qmark`
(`?`). Every parameterised statement changes, and a stray `?` reaches MariaDB as
a literal question mark rather than failing loudly, so the value is pinned by a
test rather than assumed.

**Risks.** Amending a stated project constraint is a real change, not a
footnote. `CLAUDE.md` must say what the rule now is and why: *"standard library
only — no pip installs, no build step, nothing to set up on the server. Vendored
pure-Python third-party code under `third_party/` is permitted where the stdlib
has no equivalent, because it preserves that property; it is exempt from this
project's style rules but not from the 3.6 parse gate."* Anything vaguer will be
read as permission to add dependencies.

---

## 3. Execution order

Three lanes. WP-0 lands before anything that touches `MIGRATIONS`.

```
Lane A (schema/backend)    WP-0 → WP-4 → WP-5 → WP-9
Lane B (frontend)          WP-1 → WP-2 → WP-3 → WP-7
Lane C (features)                         WP-6, WP-8   (start after WP-5 + WP-2)
Lane D (independent)       WP-11 → WP-10  (touches only new files — never blocked)
```

WP-10 and WP-11 share no file with any other package (WP-11 touches
`test_python36_compat.py`, which nothing else in this round does), so they can
run at any time by whoever is free.

### File ownership — avoid these collisions

| File | Wanted by | Rule |
|---|---|---|
| `static/app.js` | WP-1, WP-3, WP-7, WP-8 | **Lane B only.** WP-8's app.js edits land last, after WP-7. |
| `testboard/storage.py` | WP-4, WP-5, WP-8, WP-9 | **Lane A order is mandatory** — migrations must not interleave. |
| `static/style.css` | WP-3, WP-6, WP-8 | Append-only; add new blocks at the end with a comment header. |
| `MIGRATIONS` | WP-4, WP-5 | Sequential. Never concurrent. |
| `tests/test_frontend_calls.py` | WP-1, WP-2, WP-6 | Widen, never weaken (§0.3). |

If a lane finishes early, take the next package in its own lane rather than
reaching into another's files.

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
- **Publishing the repository.** Still no remote configured; commit authorship
  is a real name and personal address, which the user may want to change before
  anything becomes public.
- **Re-measuring the storage verdict.** `tools/diagnose_db.py --compare-local`
  has still not been run on the production server *since* the worker-pool fix.
  Every timing taken before that fix was taken against a server that discarded
  its page cache on every request, so those numbers describe a system that no
  longer exists. This matters to the MariaDB decision and is a one-command job —
  see the runbook's opening section.
