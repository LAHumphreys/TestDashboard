# Mark a test as flaky — design, frozen

**Status: FROZEN 2026-11-11. Three rounds were reviewed the same day; every
decision in §4 has a verdict and no question is open. The master plan is
§11 and the build is WP-44, registered in `docs/SESSION_HANDOVER.md`.
Nothing is built.**

How to read it. §0 (what is being built), §1 (the contract the two layers
build against), §4 (the decisions) and §11 (the plan) are the frozen design
and are the twenty-minute read. §2 and §3 are the owning agents' sections as
round 2 left them: `backend-engineer`'s and `ui-engineer`'s statements,
wireframes, tests and reasoning. §3a, §3b and §3c are the performance
engineer's three passes, each an addendum to the one before, so a number in
an earlier pass may be replaced in a later one; §1's cost bar quotes the
last. §5 and §6 are the questions as they were put and what the build
carries. §7 to §10 are the owner's words, verbatim, in order. Where an
earlier review and a later one disagree, the later one is right, and §1
says how it came out.

Terms the sections use: a **triple** is (environment, script, test name);
**Q1** is the statement that reads the marks and **Q2** the bounded read of a
test's recent runs; **the floor** is Q2's lower bound at a stream's first
run; the **mute epoch** is the memo key that changes whenever a mute is
created, lifted or expires; **Take** is the one-click assign-to-me action.

Four terms used throughout. A **mark** is the standing record that a test is
accepted as flaky. The **cadence** is the mark's number N: the test counts as
failing only once its N most recent runs have all failed. A marked test whose
latest failure is under the cadence is **flaking**: its failure is tolerated.
A marked test whose failures have reached the cadence is **over its cadence**
and is an ordinary failure.

Three sets the sections name. **F** is a stream's marked tests that are
flaking under no live mute: exactly the rows that read FLAKING. **X** is F's
members with no assignee, the only tests Open Actions leaves out. **P** is
the tests promoted tonight: over their cadence on this run and not on the
one before. The **keys form** is a read that returns the matched keys in
order and lets Python drop a set before paging, used where a bound list
would be too long.

## §0 What we are building

A developer can **mark a test as flaky**: a standing, owned acceptance that
this test fails now and then, with a cadence of the form "count it as failing
only once its N most recent runs have all failed", N from 2 to 10. The mark is
made by a named user with a required reason, covers the test in one
environment across mainline and every release build (as an assignment does),
and stands until someone removes it; it never expires. Each stream judges the
cadence on its own runs, and a stream with fewer than N runs of the test is
over the cadence when every run it has is a failure. While a marked test is
under its cadence it is *flaking*: the failure leaves New failures and Still
failing, is drawn in its own subdued colour and reported beside the failing
figures ("12 (+4 flaking)"), and still counts against the pass rate, because
it is not a pass. When the test passes it is an ordinary pass carrying a small
"flaky" tag. Once it is over its cadence it is an ordinary red failure,
nobody's unless it is separately assigned, and the mark stays, so it is
tolerated again after its next pass. A live mute wins over the mark: a muted
test shows as muted until the mute runs out. The mark's owner is a record of
who accepted the flakiness and is not an assignment: marking a test puts
nothing on anyone's Open Actions. An unassigned test that is flaking is not
listed on Open Actions; an assigned test is listed whatever its state and
reads FLAKING while it flakes, and everything else on that page is as it is
today (§9, which replaced a wider reading of verdict 17). In lists a flaking
row carries its own chip and edge in the flaking colour, because flaking is
a state in its own right (§8, decision 8). The browse page's Result filter
has a FLAKING value, and its FAIL value leaves flaking rows out (§9). On the
night a marked test goes over its cadence it is a New failure, and Still
failing after that (§9). A Flaky tab
on Home, beside Muted, lists every mark with its owner, cadence, age and how
often the test has actually failed, so a stale mark is visible without anyone
being nagged. Any named user may remove a mark or change its cadence; every
act is recorded. The action is available on every product. It is for the
developers and managers of products that have accepted some tests as flaky, so
those tests stop crowding the failures that need triage.

Done means: mark, change and unmark from the selection bar and the test page;
the tiles, the three home charts (a fourth segment), Watch, the browse rows,
the browse filter, Open Actions and the Flaky tab all agree on what is
flaking; mute behaves
exactly as before; and the headline, browse and chart reads measure the same
as today on an estate with no marks, on SQLite and MariaDB alike.

Layers: backend (a new table for the marks and one for their history, so a
migration with its MariaDB ledger step; the cadence evaluation; the counts;
the endpoints) and UI (the action, the colour, the figures, the tab, the
state markers). On the performance trigger paths: yes (`storage.py`, `api.py`,
the home page's first paint), so round 0 carries §P and the build carries a
`/perf-ab`.

**Not in this arc, by the owner's direction:** a general clean-up of Open
Actions (an easy way to deal with passing tests and clear them in bulk) is a
follow-up design. An assigned test that is flaking is seen as a special case
of that, which is why it stays on Open Actions here with only a state marker.

## §1 The contract, as frozen

The main session's reconciliation of §2 and §3 after round 2. It is what
the two layers build against; "decision N" points at §4.

- **Identity.** A mark is per test in one environment: (environment, script,
  test name), the key an assignment uses. Fields: `cadence` (2 to 10),
  `marked_by` (the person making the mark; nobody else can be named, decision
  12), `marked_at`, `reason`.
- **The rule, per stream.** A failure is `FAIL` and nothing else (decision 1).
  The state is `passing`, `flaking` or `failing` by §2 B.1's table, and `null`
  where the test never ran on the stream. A live mute wins over the mark.
- **State on a row.** `flaky` is null or `{cadence, marked_by, marked_at,
  reason, state, streak, runs}`. `streak` is the consecutive failures ending
  at the latest run, capped at N. `runs` is how many runs the stream has of
  the test, capped at N, given when the state is `failing`: the one place the
  UI reads it, to say "every run here has failed (2 of 2)". Carried by
  dashboard rows, queue rows, Open Actions' rows and the test detail, which
  also carries `flaky_history`, so opening the history fetches nothing.
  Comparison rows carry nothing (decision 9).
- **What reads FLAKING.** A row whose result is `FAIL`, whose state is
  `flaking` and which has no live mute. The wire result stays `FAIL`; one
  helper in `static/api.js` makes the word, the chip and the row edge.
- **Counted once.** Muted, else flaking, else assigned, else unassigned.
  Flaking tests leave `new_failures`, `still_failing`, the per-environment
  `failed`, the per-script `failing`, the `assigned` figure the charts split
  on, and Watch's `unassigned_failing`. The pass rate's inputs do not change.
- **Going over the cadence** (decision 25). A marked test is in
  `new_failures` and the New failures queue on the night it goes over its
  cadence, which is exactly when run N+1 exists and is not a `FAIL`; after
  that it is Still failing. With N+1 runs or fewer, all failures, it was over
  already; a marked test failing on its first run on a stream is New, as any
  test is today. A cadence change brings no run, so it never makes a test
  new. Its
  row needs no new field: a New failures row with `prev_result` `FAIL` and
  state `failing` reads "FLAKING → FAIL (flaky)".
- **Newly fixed** (decision 3). A marked test passing after a failure is
  fixed only if it had been over its cadence; a pass after a tolerated
  failure is in no Fixed figure, queue or delta.
- **Counts.** `status.flaking`, `status.flaking_new_failures`,
  `status.flaking_still_failing` (one per tile), `status.flaky_total` (every
  mark in scope); `by_environment[].flaking`,
  `top_failing_scripts[].flaking`, `products[].flaking`, and `flaking` on
  Watch's environment and product cards. Nothing draws `products[].flaking`;
  it stays, because the product's failing figure has the flaking tests taken
  off it and the board never subtracts silently.
- **The trend.** `FAIL_FLAKING` per night, in `parts=assigned_trend` only,
  disjoint from `FAIL_MUTED` and `FAIL_ASSIGNED`, each night as it stood
  then under today's marks (decision 2).
- **The Flaky tab** is its own `GET /api/flaky`, fetched on the tab's first
  click. Its badge is `status.flaky_total`. Entries carry the mark, `result`,
  `state`, `muted`, `retired`, `assignee` (the current assignee, as
  anywhere) and `recent_failures` of `recent_runs`, the last 20 runs on the
  stream. Paged by the server, 100 a page: failing first, then flaking, then
  passing, newest mark first, retired last (decision 16). A mark on a test
  with no run on the stream is absent from the tab and the badge (§8). A
  retired test's mark is listed, flagged, and is in no count (decision 11):
  the response gives `total` and, apart from it, `retired`.
- **Acts.** `POST /api/flaky/bulk` with `{username, reason, cadence, tests}`
  returns `{marked, changed, unchanged, unknown}`; the reason is required for
  a fresh mark and optional for a cadence change. `POST /api/flaky/unmark`
  with `{username, tests}` returns `{unmarked}`.
- **Open Actions** (decision 23, as §9 narrowed it). An unassigned test that
  is flaking is not listed; everything else on the page is as it is today.
  An assigned test is listed whatever its state and reads FLAKING while it
  flakes; a marked test at an unexpected pass stays. The rows, their `total`
  and bulk assign agree on the set. Every open-assignment figure and queue
  is unchanged: `status.assigned_open`, `queue_totals.assigned` and `.mine`,
  the assigned and My actions queues. A footer line says what is left out,
  only where a mark exists.
- **Open Actions' names-only request** (decision 20), the build's first
  item. `GET /api/summary?parts=names` returns `environments`, `assignees`,
  `products`, `assignment_streams`, `stale_before` and `flaky_marks` (is
  there any mark) under today's names; the page stops fetching the full
  summary.
- **The browse Result filter** (decision 24). `result=FLAKING` matches the
  rows that read FLAKING; `result=FAIL` matches every other `FAIL` row, a
  muted flaking one included; both together are exactly today's `FAIL`.
  `result` stays repeatable and sorting stays on the wire value. The toggle
  is on the browse page only and hidden where the scope has no mark. Because
  `result=FAIL` alone now leaves flaking rows out once a mark exists, Open
  Actions' Failing mode sends both values, and Watch's "failing, unassigned"
  link and the scripts chart's click now list what their figures count.
- **An older payload** without any of these fields, and a scope with no
  marks, draw exactly today's page.
- **The cost bar, as §3c leaves it.** With no marks, no read gains a
  statement or a join, and the push path gains nothing with or without
  them. With marks, cold, against today, on perf-seed/1; each cell is SQLite
  then MariaDB (a local 12.3, not production's 10.3), in ms:

  | Read | 50 marks | 1,000 marks |
  |---|---|---|
  | Home's headline (MariaDB base 44 to 46) | +3.4 ; +7.2 | +6.5 ; +15.2 |
  | Browse, no filter, on Home's first paint | +0.3 ; +0.7 | +0.4 ; +0.5 |
  | Browse with Failing chosen | +3.0 ; +6.7 | +3.2 ; +7.4 |
  | Browse with Flaking chosen | +0.6 ; +4.2 | +1.8 ; +7.6 |
  | Open Actions' rows (MariaDB base 83 to 85) | +1.0 ; +8.8 | +1.2 ; +10.0 |
  | Open Actions' page, with the names-only request | faster ; −5.4 | faster ; −1.0 |

  The owner accepted the headline's figures (§10: about 6 and 13 ms on
  MariaDB once the runs statement's members are parenthesised, an estimate)
  and Open Actions' rows (§9). The headline reads only the marked tests that
  are failing, so it grows with a stream's failures and barely with its
  marks. Once Home has been loaded the filters and Open Actions' rows are
  within 0.4 ms of today or faster, bar Open Actions' rows on MariaDB at 50
  marks (+2.7) and either filter with Passing also chosen (+0.5 to +2.8).
  With no marks Open Actions' page is 27 ms faster than today
  on SQLite and 14.5 ms faster on MariaDB. The per-day chart's part, off
  every first paint, runs from 4.6 ms (14 days, 50 marks) to 366 ms (90
  days, 1,000 marks) on SQLite and is decided with the open trend decision
  (decision 21). A figure worse than these in the build's measured passes
  returns to the owner before it ships.

## §2 Backend (§B, round 2, backend-engineer, base `028eb33`)

Against §0, §4, §8 and §9, and §1 as round 1 left it; the design as
frozen. **Migration 12, create-only**; nothing on the push path. Numbers
marked §P or §3b are the performance pass's, the rest this round's (B.5).
Some cost is accepted.

### B.1 Definitions (round 2)

- **A failure is `FAIL` only** (decision 1): an `UNEXPECTED_PASS` or
  `FAILED_AS_EXPECTED` among the last N runs keeps the test flaking.
- **State on a stream**, from its runs there, newest first; `streak`,
  `runs` are §1's, capped at N; **New**: a New failure tonight (§9):

  | Latest | Prev | N | State | streak | runs | New | Reads `runs` |
  |---|---|---|---|---|---|---|---|
  | not FAIL | any | any | `passing` | 0 | null | no | no |
  | FAIL | none | any | `failing` (verdict 8) | 1 | 1 | yes, as today | no |
  | FAIL | not FAIL | any | `flaking` | 1 | null | no | no |
  | FAIL | FAIL | 2 | `failing` | 2 | 2 | run 3 exists, not FAIL | summary paths, ≤ 3 |
  | FAIL | FAIL | 3..10 | `flaking` if one of the newest N is not FAIL, else `failing` | leading FAILs | rows read | failing, and run N+1 exists, not FAIL | ≤ N+1 |
  | no row | | | `null`, absent there (§8) | | | | no |

- **Went over on this run (round 2)**: over now and not over at the
  previous run, which is exactly "run N+1 exists and is not FAIL". With
  N+1 runs or fewer, all FAIL, it was over already (verdict 8). The next
  night it is Still failing. A cadence change brings no run: never New.
- **Counted once**: muted, else flaking, else assigned, else unassigned
  (the charts' split); a live mute wins everywhere. Retired: in no count.
- **F and X (round 2, §9).** **F**: the stream's marked tests that are
  `flaking` under no live mute, exactly the rows that read FLAKING (§3).
  **X**: F's members with no assignee, not retired, the only tests Open
  Actions leaves out. Nothing is dormant: an assigned flaking test is
  listed, reading FLAKING; a marked `UNEXPECTED_PASS` stays (§9). The
  open-assignment figures and queues are **as today**
  (`status.assigned_open`, `queue_totals.assigned`, `.mine`, the assigned
  and My actions queues).
- **Newly fixed** (decision 3): a marked, unmuted, unretired test passing
  after a FAIL is fixed only if runs 2 to N+1 of its newest N+1 all FAIL,
  or all it had; else tolerated, in no Fixed figure, queue or delta.

### B.2 Data model and payloads (round 2)

```sql
CREATE TABLE test_flaky_marks (environment TEXT NOT NULL,
    script TEXT NOT NULL, test_name TEXT NOT NULL,
    cadence INTEGER NOT NULL, reason TEXT NOT NULL,       -- cadence 2..10
    marked_by TEXT NOT NULL REFERENCES users(username), marked_at TEXT NOT NULL,
    PRIMARY KEY (environment, script, test_name));        -- no stream
CREATE TABLE flaky_mark_history (id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL, script TEXT NOT NULL, test_name TEXT NOT NULL,
    action TEXT NOT NULL, cadence INTEGER, reason TEXT,  -- mark|cadence|unmark
    actor TEXT NOT NULL REFERENCES users(username), acted_at TEXT NOT NULL);
CREATE INDEX idx_flaky_mark_history_triple
    ON flaky_mark_history (environment, script, test_name, id);
```

**Migration 12** (claimed in `UPGRADE_PLAN.md` §1 in the build's commit;
WP-15 to 13): ledger step 11→12, `alters=()`, both tables in `ddl()` and
`_ENVIRONMENT_TABLES`. **Rollback**: SQLite's copy; MariaDB, drop, back to 11.

```text
rows (browse, queues, Open Actions), test detail; comparison rows: none
  "flaky": null | {cadence, marked_by, marked_at, reason, state, streak, runs}
  a New failures row with prev_result FAIL and state "failing" is a
  promotion (§3: FLAKING -> FAIL (flaky)); queue rows carry both today
/api/summary: §1's counts, unchanged (status.flaking, …, FAIL_FLAKING)
GET /api/summary?parts=names -> round 1's fields plus flaky_marks
GET /api/dashboard?...&result=FLAKING: B.3(c); ?open=1 leaves out X: B.3(b)
GET /api/flaky?stream=&environment=&product=&limit=100&offset=0 -> {
  generated_at, stream, total, retired, marks: [{<triple>, cadence,
  marked_by, marked_at, reason, retired, result, state, muted, assignee,
  recent_failures, recent_runs}]}                        order: D-B11
POST /api/flaky/bulk {username, reason?, cadence, tests} -> {marked,
  changed, unchanged, unknown};  POST /api/flaky/unmark -> {unmarked}
test detail: "flaky_history": [{id, action, cadence, reason, actor,
  acted_at}], in the detail (§3b)
```

**The Flaky tab (round 2).** `assignee` is the current assignee, as
anywhere; nothing reads "dormant". The window figures are renamed
`recent_failures`, `recent_runs` (§3 U12): `runs` is only a row's.

### B.3 Where the cadence is evaluated (round 2)

**(a) At read time, bounded (decision 17).** Python applies B.1 to these,
qmark-canonical, each with its driving side pinned and a plan guard:

```sql
-- Q1, per stream, the marked FAIL rows (round 1's dormant member gone)
SELECT lr.<triple>, fm.cadence, lr.prev_result, lr.start_time,
  tm.expires_at, tr.retired_at, ca.assignee FROM latest_runs AS lr
CROSS JOIN test_flaky_marks AS fm ON <triple> LEFT JOIN test_mutes AS tm ON
  tm.stream_id = lr.stream_id AND <triple> LEFT JOIN test_retirements AS tr
  ON <triple> LEFT JOIN current_assignments AS ca ON <triple>
WHERE lr.stream_id = ? AND lr.result = 'FAIL'
-- Qc (summary paths, round 1's shape): flaky_total, decision 3's candidates
-- Q2: newest N+1 runs per test, <= latest start, UNION ALL per 100 tests;
--   the floor on build streams only (decision 18)
```

- **One Q2 for every list**, at LIMIT N+1: marked FAIL/FAIL rows of
  cadence 3 to 10; on summary paths also cadence 2 (promotion) and Qc's
  candidates. Plans (SQLite): Q1 the `idx_latest_runs_result` range, then
  the marks' key; Q2 a runs UNIQUE range. Mute liveness: Python, `now`.

**(b) Open Actions: X only (round 2, §9).** `open=1` leaves out X in
`_dashboard_filters`: rows, `total` and bulk assign agree (WP-26). The
page sends `open=1` in its three modes; its Failing mode sends
`result=FAIL&result=FLAKING` (today's FAIL, (c)).
- **SQLite: the keys form only** (§3b). One statement returns the matched
  keys in sort order (no `runs` or mute join); Python drops X; `total` is
  what is left; the page comes by key, `(lr.<triple>) IN (VALUES (?, ?,
  ?), …)`, ≤ 250. It replaces rows and count; bounded by the open items.
- **MariaDB: bound, gated**, rows and count: `AND NOT (lr.result = ? AND
  ca.assignee IS NULL AND (lr.<triple>) IN ((?, ?, ?), …))`. **The gate
  changes** from `(result = FAIL OR assigned)`, prefilter and groups to
  `result = FAIL AND ca.assignee IS NULL` and a row-value list (§3b (d)):
  true of every member of X, and naming `ca` keeps it after `ca`'s probe.
  The count already joins `ca`: no join is added.

**(c) The browse Result filter gains Flaking (round 2, §9).** The wire
follows the chip (§3): `result=FLAKING` matches F; `result=FAIL` every
other FAIL row, a muted flaking one included; both together are today's
FAIL. `result` stays repeatable; sorting stays on the wire value. The API
maps FLAKING to raw `FAIL` plus a mode: FAIL alone *excludes F*, FLAKING
alone *keeps only F among FAIL rows*, both or neither has none. Non-FAIL
rows are never touched, so every other filter composes by AND.
- **The term binds F on both engines**, scoped in Python to the request's
  environments and script: exclude `AND NOT (lr.result = ? AND
  (lr.<triple>) IN (<F>))`, keep `AND (lr.result <> ? OR (lr.<triple>) IN
  (<F>))`; `<F>` is `VALUES (?, ?, ?), …` on SQLite, `((?, ?, ?), …)` on
  MariaDB. Rows and count alike; the count's WHERE reads `lr` only: **no
  join**. Paging, sort and `total` stay exact SQL.
- **Not the keys form** (B.9), except **above the ceiling** (SQLite
  before 3.32, over 300 of F in scope: an outage), where it answers exact.
- **No marks**: FLAKING maps to nothing, so alone it is an empty result
  list, answered today without a statement; FAIL is today's SQL.
  Watch's `result=FAIL&unassigned=1` link and the scripts chart's click
  now list what their figures count (both leave flaking out, §1).

**(d) The promotion, counted (round 2, §9).** P: the promoted tests in
scope, unmuted, unretired. `new_failures` loses the flaking cells with
prev not FAIL and gains |P|; `still_failing` loses the other flaking
cells and |P|; Home's delta follows. When P is not empty the New failures
queue's rows bind it from the memo, `OR (lr.<triple>) IN (<P>)` (§3c's
condition, replacing this round's wider read). Only above the parameter
ceiling do they widen to `prev not FAIL OR fm.cadence IS NOT NULL`
(decision 19's join), over-fetch by the marked FAIL/FAIL rows in scope
and keep P. Still failing drops F and P, over-fetching; engine order.

### B.4 Memos, caches and the no-marks bit (round 2)

- **No marks: no statement and no join, on any read.** The bit rides
  statements already run (round 1): the mute epoch's entry (its return
  value unchanged); `/api/dashboard`'s environment-products statement,
  moved first (`UNION ALL SELECT NULL, MIN(environment) FROM
  test_flaky_marks`); a `MIN` on a queue kind's count; and `flaky_marks`,
  a `UNION ALL` row on the names part's products statement (§3b).
- **One entry per (stream, environment)**, `flaky_states`: Q1's rows and
  the states from Q2, **filled by whichever page reads first**. **Rows
  paths never run Qc**, its own entry, summary paths only; a summary path
  reads what a rows-path fill lacks (cadence 2, fix candidates) in its one
  Q2. Dropped by a push to its pair and by a mark, mute, assignment or
  retirement act (Q1 reads `ca`, `tm`, `tr`).
- **Q2's answers survive every act** (§3b (c)): their own dict,
  `_cadence_runs_cache`, key (stream, triple, latest start, N), which no
  act's full clear touches; deletes drop it, and a push its stream's, for
  correctness: a re-pushed or late run changes the window under one key
  (§3c, correcting this round). It carries `_streak_cache`'s 60 s limit
  and a size cap that clears in full (§3c). The name keeps it in the
  harness's cold sweep.
- **Unfiltered browse never runs Q1**: state from the row's own result,
  `prev_result` and cadence; Q2 only for the page's marked FAIL/FAIL rows
  of cadence 3 to 10, unless memoized.
- **Counts exact without SQL** (B.3(d), as `_subtract_muted`); capped
  queues over-fetch and drop. **The push path gains nothing.**

### B.5 Cost, measured (round 2)

Copies of perf-seed/1 and -h90, 65 assignments, ten mutes, round 1's
marks and cadences. SQLite, in-process, memos cleared before every call,
alternated: 32 calls (perf-seed/1), 24 (-h90), 16 (the forms). Base
`028eb33`; head its handler with the proposed SQL substituted. Median
paired B−A [95 % band], ms; statements per cold call base/head; warm =
memo filled, as after a Home glance. A fast machine (browse count 2.5 ms,
Known slow 22): compare deltas. **MariaDB not timed.** perf-seed/1 (-h90):
50 marks, F 11, X 7 (6), P 8 (7); 1,000, F 56 (57), X 22 (21), P 9 (7).

| Open Actions' rows, perf-seed/1 ; -h90 | 0 | 50 | 1,000 |
|---|---|---|---|
| cold | +0.16 [−0.15, +0.28] 295/295 ; +0.39 [−0.39, +0.76] | +1.23 [+0.71, +1.77] 295/297 ; +1.25 [+0.85, +1.67] | +1.43 [+1.09, +1.86] 295/294 ; −0.10 [−0.89, +0.50] |
| warm | | −1.69 [−1.97, −0.71] ; −1.75 [−2.88, −1.13] | −1.40 [−1.84, −0.74] ; −3.49 [−3.75, −2.95] |
| `total` (base 177) | 177 | 170 ; 171 | 155 ; 156 |

Round 1's +3.4 and +5.7 (perf-seed/1) become +1.2 and +1.4; warm beats
base because the keys form replaces the count and the walk.

| Browse `limit=250`, perf-seed/1 ; -h90 | 0 | 50 | 1,000 |
|---|---|---|---|
| no filter (base 4.9 ms) | +0.06 3/3 ; +0.05 3/3 | +0.24 [+0.19, +0.28] 3/4 ; +0.26 3/3 | +0.44 [+0.40, +0.46] 3/4 ; +0.30 3/3 |
| its count alone (same SQL) | +0.03 ; +0.01 | +0.01 ; +0.01 | +0.01 ; +0.01 |
| Failing cold (base 1.8 ms) | +0.04 3/3 ; +0.04 | +3.27 3/5 ; +3.48 | +3.60 3/5 ; +3.83 |
| Failing warm | | +0.31 ; +0.32 | +0.22 ; +0.21 |
| Flaking cold | −1.72 3/1 ; −1.73 | +0.73 3/5 ; +0.75 | +2.12 3/5 ; +2.34 |
| Flaking warm | | −1.44 ; −1.45 | −0.86 ; −0.85 |
| count alone, Failing / Flaking term | | +0.09 / +0.00 | +0.26 / +0.04 |

Filtered bands within ±0.2 ms; FAIL 171 splits 160 + 11 and 115 + 56,
asserted against the keys form. **No filter meets the bar**: no marks,
same; with marks the join and a page-local Q2 (+1 statement, no memo),
+0.2 to +0.5 ms, under round 1's whole-stream Q1 and Q2. **Failing and
Flaking**, off every first paint: +2 to +4 ms cold against the Known slow
count, nearly all the memo's fill; +0.3 or faster warm. With Passing:
bound +4.1 to +5.7 cold, +0.5 to +1.1 warm; keys +12.1 to +12.6 warm.

| Headline, perf-seed/1 ; -h90 | 0 | 50 | 1,000 |
|---|---|---|---|
| round 2 read vs base | +0.05 [−0.55, +0.37] 37/37 ; −0.03 | +3.69 [+3.15, +4.07] 37/40 ; +4.12 [+3.31, +4.68] | +7.55 [+7.22, +7.78] 37/40 ; +7.24 [+6.69, +7.86] |
| the promotion's share | | +0.17 [−0.34, +0.43] ; +0.03 [−0.50, +0.64] | +0.65 [+0.18, +1.21] ; +0.44 [−0.28, +1.06] |

The share: round 2's one Q2 against round 1's (state at LIMIT N, fixes
at N+1), same handler. §3b's +6.3 to +6.9 at 1,000 becomes +7.2 to +7.6.
Queue over-fetch not timed (§3a: +0.056 ms at F 56).

**The whole page, corrected.** Round 1's "Open Actions waits 12 to 18 ms
less" holds **on SQLite** (§3b: 27 and 20 less). **On MariaDB** (§3b's
numbers, 12.3) the page is −11.7 ms with no marks, level at about 50
(+2.6) and slower beyond (+11.0 at 1,000); the rows alone +9.2 and +19.1
cold, about +11 cold and +4 warm at 1,000 with (a) to (d) (estimate).

### B.6 Decisions, as settled (round 2)

- As decided: **D-B1** trend "that night", **D-B2** last-20 by Q2, **D-B3**
  the unsliced summary keeps the counts, **D-B4 to D-B8** lifecycle, tag,
  deactivated user, comparison and Watch's builds, the tab, **D-B10**
  `parts=names`, **D-B11** the tab's order (failing, flaking, passing,
  newest mark first, retired last). **Round 2: D-B9** X only (B.3(b)),
  **D-B12** `result=FLAKING` (B.3(c)), **D-B13** the promotion (B.3(d)).

### B.7 Tests the build writes (round 2)

- `test_storage.py`: `FlakyMarkWriteTest`; `FlakyCadenceTest` (B.1, the
  floor on builds only); `FlakyAgreesWithAnalyticsTest`;
  **`OpenActionsSetAsideTest`** (only X leaves; assigned flaking and UP
  listed; mute wins; `total` = rows paged; bulk on the same set; Failing
  mode with FAIL+FLAKING = today); **`OpenAssignmentFiguresUnchangedTest`**,
  replacing `DormantFiguresTest` (`status.assigned_open`,
  `queue_totals.assigned`, `.mine`, the assigned and My actions queues
  byte-identical with and without marks, bar the rows' `flaky`);
  **`BrowseFlakingFilterTest`** (FAIL and FLAKING partition today's FAIL,
  rows and `total`; muted flaking under FAIL; paging across a boundary,
  every sort; with Passing; a planted ceiling answers alike; bulk
  agrees); **`PromotionTest`** (New that night, Still after; cadence 2;
  N+1 runs or fewer; no New from a cadence change; the queue row's
  `prev_result` and `flaky`; Home's delta); `FixedRuleTest`;
  `FlakyTabOrderTest`. `test_api.py`: shapes, 400s, `NamesPartTest`.
- **`NoMarksAddsNothingTest`, widened**: empty table, the cold statements
  of the headline, full payload, queues, browse (none, FAIL, FAIL+FLAKING),
  Open Actions, Watch, `assigned_trend` equal base; FLAKING alone runs
  none; no `COUNT(*)` joins the marks (the count-join guard widened).
- Cache guards, cold, same rows: Open Actions and filtered browse base + 1
  + ⌈Q2/100⌉; headline base + 2 + ⌈Q2/100⌉; unfiltered browse base +
  ⌈page's Q2/100⌉; warm = base; `parts=names` pinned.
  **`TargetedInvalidationTest`**: acts leave `_cadence_runs_cache` filled;
  a push drops its stream's, a delete all. Plan guards on Q1, Qc, Q2, both
  terms; `test_migrations.py`; `LedgerTest` (11→12); MariaDB variants with
  ≥ 101 marks (decision 22).

### B.8 Open questions (round 2)

1. **For §P, MariaDB 10.3**: plans and timings of both row-value terms,
   Q1 and Q2 at LIMIT N+1; `STRAIGHT_JOIN` needed? (12.3: no, §3b).

### B.9 Pushback (round 2)

- **§3b's "keys form only on SQLite" holds for Open Actions, not browse**,
  measured (B.5): with Passing also chosen browse matches the whole
  partition, +12.1 to +12.6 ms warm by keys against +0.5 to +1.1 bound;
  alone the two are within 0.3 ms. Keys stays as the above-ceiling form.
- **Open Actions' Failing mode must send FAIL+FLAKING** (§3): FAIL alone
  would drop an assigned flaking test from that mode, which §9 rules out.

## §3 UI (§UI, round 2, ui-engineer, base `028eb33`)

Against §0, §4, §8 and §9, and §1 as round 1 left it; the design
freezes here. A flaking row reads FLAKING, chip and edge in the strong
mauve, no pill (decision 8). Open Actions follows §9's rule and nothing
more. Browse's Result filter gains FLAKING. No marks: today's page. No
browser has rendered any of this. `▒` is the flaking chart segment.

### U1 The colour for "flaking"

Two steps of one mauve (agreed, §9). Charts keep **`--c-flaking:
#a66993`** (decision 7); white on it is 4.15:1, too low for chip text.
Chips, row edges and the pressed FLAKING toggle take
**`--c-flaking-strong: #8f638b`** (ΔE×100 4.6 from the chart token, one
colour to the eye): white on it 4.85:1, the 3 px edge clears 3:1. Against
its neighbours (OKLab ΔE×100): UNEXPECTED_PASS 16.4, red 16.6, green 21.8,
amber 30.3 (floor 15); CVD minimum 11.2 (target 8).

### U2 Wording: one set of helpers in `api.js`, from the data (round 2)

```
displayResult(result, flakyState, muted)    "FLAKING" | result unchanged
flakyTag(entry)       the "flaky" pill, or null (unmarked, or FLAKING)
flakySentence(flaky, muteLive)              the title, below
setAsideSuffix(muted, flaking)  "+3 muted · +4 flaking" | "+4 flaking" | ""
failingWithSetAside(n, muted, flaking)        "12 (+3 muted, +4 flaking)"
failingSplit(failing, assigned, muted, flaking)  .words, zeros left out
setAsideWords(muted, flaking)   "3 muted · 4 flaking" | "4 flaking" | ""
failedOfRuns(6, 20)             "6 of its last 20 runs"
cadenceWords(3) "3 in a row"; cadenceSentence(3); flakyCadenceSelect(…)
newFailuresSub(flakyTotal)  "were passing or flaking before" if any mark
previousShown(entry)  "FLAKING" on a promotion row, else prev_result
failToggleTitle()     "Failing, flaking tests left out"
BROWSE_RESULTS        PASS FAIL FLAKING FAILED_AS_EXPECTED UNEXPECTED_PASS
```

`displayResult` is the one place FLAKING is made (`state` flaking, no live
mute) and `resultClass` maps it to `result-flaking`; it never touches
`entry.result`, so sorts and the wire stay FAIL. The set-aside pair
replaces `mutedSuffix` and `failingWithMuted`, printing today's bytes when
nothing is flaking. Every row over its cadence, the promotion night
included (U6), takes one form of title:

```
flaking    Flaking: failed its last 2 runs; counts as failing at 3 in a row
           ("failed its last run" when streak is 1)
runs = N   Failing: its last 3 runs all failed; marked flaky, 3 in a row
runs < N   Failing: every run here has failed (2 of 2);
           marked flaky, 3 in a row ("its only run here failed" for one)
passing    Marked flaky: counts as failing once its last 3 runs have failed
live mute  Marked flaky; the mute comes first until it runs out
```

### U3 The selection bar

Unchanged (decision 13): `Flaky [at…▾] Mark as flaky · Unmark` after
Mute, about 400 px wider (estimate), the note box as the reason, the
person marking as owner (decision 12). **Set cadence** replaces Mark when
every ticked row is marked.

### U4 The Review panel

```
Flaky  [why is this test flaky? (required)    ] [at…▾] [Mark as flaky]
Flaky  Flaking: failed its last run; counts as failing at 3 in a row
       dev1 · 12 days ago · race in teardown  [3 in a row▾] Set cadence
       Unmark
```

### U5 The test page

The mark and its history ride the detail (`flaky`, `flaky_history`; no
fetch when the history opens, §3b). The header's chip goes through
`displayResult`; run history and the compare strip stay raw (a run is
what happened; flaking is a judgement on the test now; decision 9). With
`state` null the banner reads "Marked flaky, 3 in a row; no run here yet".

```
login_retry                  [FLAKING] 2026-11-10 02:14 · 41.2 s
Marked flaky · 3 in a row    dev1 · 12 days ago · race in teardown
  Flaking: failed its last run; counts as failing at 3 in a row
  [3 in a row▾] [Set cadence] [Unmark]
FLAKINESS   unmarked: [Mark as flaky]   marked: Marked flaky, 3 in a row
Flaky mark history ▸   Action | By | When (UTC) | Cadence | Reason
```

### U6 Home tiles, "All clear", the promotion night (round 2)

```
New failures 12                        Still failing 31
were passing or flaking before         failed before too · +2 flaking
· +3 muted · +4 flaking

✓ All clear. No new failures, nothing still failing, no stale annotations.
  Set aside: 3 muted · 4 flaking
```

**Going over the cadence is a New failure** (§9). That night the test is
in `status.new_failures`, so "▲ N more failing than before" moves with no
UI change; the next night it is Still failing. Its change cell reads
`FLAKING → FAIL (flaky)`: `previousShown` gives FLAKING for a New
failures row with `prev_result` FAIL and `flaky.state` failing. Its title
is U2's `runs = N` line: the queue says when, the title says why.
Decision 3 is now symmetric: a real failure counts "▲" when it starts and
"▼" when fixed (`FAIL → PASS (flaky)`); a tolerated one moves neither.
**The subline**: "were passing before" is untrue of a promotion, so
where `status.flaky_total > 0` it reads "were passing or flaking before";
otherwise today's bytes. No new field.

**Tiles stay buttons that open their queues**: "+4 flaking" cannot hold a
link inside a `<button>`, so it stays text, as "+3 muted" does.

**"All clear" names what is set aside** (§8): muted
`muted_new_failures + muted_still_failing`, flaking `status.flaking`;
both zero, no line. "3 muted" opens the Muted tab; **"4 flaking" opens
browse on FLAKING alone** (stale cleared, scrolled to, as "retired"
does): the tests flaking now, where the Flaky tab lists every mark.

### U7 The three home charts

Round 0's design stands: a fourth segment `▒` after the grey, in the chart
token, with key, tooltip row, column and caption words only where
`status.flaky_total > 0` (decision 5). `FAIL_FLAKING` is each night as it
stood then, under today's marks (decision 2), captioned "flaking that
night, by today's marks"; so the promotion night is red, as in New
failures.

### U8 The Flaky tab, beside Muted (round 2)

```
New failures 3 · Still failing 11 · Muted 2 · Flaky 7 · Fixed 4 · …
Failing first, then flaking, then passing; newest mark first.
1 flaking now · List them in All tests
☐ Test         State    Mark                    Cadence  Failed   Assignee
☐ dns_timeout  FAIL     races the DNS · dev2    2 in a…  9 of 20  [Take]
☐ login_retry  FLAKING  race in teardown · dev1 3 in a…  6 of 20  dev1
☐ cache_warm   PASS     slow mirror · dev2      5 in a…  0 of 20  [Take]
```

- **Data**: `GET /api/flaky` on the first click only, never in
  `refreshAll` or `fetchSummary`; 100 a page, "Show more" appends, in the
  server's order (decision 16); no column sorts.
- **Badge**: `status.flaky_total`, "…" until the headline; the tab exists
  only above zero. A `state: null` mark is absent.
- **Cells**: environment·script under the test and the mark's age under
  its owner (cut from the drawing); `cadenceWords`; `failedOfRuns` ("0 of
  its last 20 runs" shows a stale mark); chip and edge by `displayResult`;
  a muted pill; "retired". **The assignee cell is every queue's**, name or
  Take, no qualifier: an assignment means the same in any state (§9).
- **Link line** (new): "N flaking now · List them in All tests" from
  `status.flaking`, above zero only; it does what "All clear"'s does.
- **States**: "Loading…"; empty "No test is marked flaky here."; errors as
  other tabs. An act refetches page one and the counts, as Mute does.

### U9 Rows everywhere else (round 2)

```
state                    chip              row edge   tag
under cadence, no mute   FLAKING           mauve      none
over its cadence         FAIL              red        flaky (grey pill)
marked and passing       PASS              green      flaky
marked, unexpected pass  UNEXPECTED_PASS   its own    flaky
live mute, any state     result            result     muted flaky
```

Each row builder gives one `displayResult` value to `resultClass` (the
edge) and `resultChip`, so they cannot disagree. Covered: browse, the
script page, every Home queue, a build's own tab, Not run's "Last result".
Still failing holds no flaking test; New failures holds the promotion
only (U6). Raw results stay in run history, the execution breakdown, the
compare strip, the comparison page and Watch's build cards (decision 9).
Watch's cards read `Failing 12 (+3 muted, +4 flaking)`.

**Open Actions (round 2).** One change from today: an unassigned flaking
test is not listed, in any mode or owner filter; all else is as today. An
assigned test is listed whatever its state, reading FLAKING while
flaking; a marked test at an unexpected pass stays, tagged "flaky"; an
unassigned one over its cadence is FAIL (flaky). Home's **My actions**
follows the same rule. The page's Failing mode sends `result=FAIL` and
`result=FLAKING` together (§2 B.3(b)): FAIL alone now leaves flaking
rows out, an assigned one included. The footer line shows only if
`flaky_marks`:

```
☐ login_retry  atlas  FLAKING                  dev1  2026-11-10 02:14
☐ dns_timeout  atlas  FAIL (flaky)             —     2026-11-10 02:15
☐ cache_warm   atlas  UNEXPECTED_PASS (flaky)  dev2  2026-11-10 02:16
42 open actions
Unassigned tests that are flaking are left out until they fail past their
cadence.
```

**The names-only request.** `refresh()` swaps `summaryUrl()` for one
product-scoped request returning `environments`, `assignees`, `products`,
`assignment_streams`, `stale_before` and `flaky_marks` under today's
names. The "fetched once" gate moves from `state.assignees.length` to a
loaded flag (with no assignees it refetches on every refresh today).

**The browse Result filter (round 2).** Browse only.

```
Script [all ▾]  [PASS] [FAIL] [FLAKING] [FAILED_AS_EXPECTED]
[UNEXPECTED_PASS]  [Not run recently] [Include retired] [Unassigned only]
[Search test name…]                                  4 matching tests
```

- **The value follows the chip.** FLAKING matches rows reading FLAKING
  (FAIL on the wire, flaking, no live mute); FAIL matches every other FAIL
  row, muted ones included. The two partition the failing tests; both
  pressed is today's FAIL. Sorting stays on the wire value.
- **Order and look**: after FAIL, the two halves side by side; the wire
  word as label, like its neighbours; pressed in the strong token. While
  shown, FAIL's title is "Failing, flaking tests left out".
- **Hidden**: until the headline lands with `status.flaky_total > 0`, or
  while pressed, so a pressed filter is never invisible.
- **The URL**: `result=FLAKING`, repeatable beside today's values, read at
  load by today's loop (over `BROWSE_RESULTS`); on a build it opens "Its
  own results" as any `result=` does. Toggles do not write the address
  bar today and this one does not start to.
- **Count line and empty state**: today's ("4 matching tests").
- **Links in**: "All clear" and the Flaky tab set FLAKING alone. Watch's
  `result=FAIL&unassigned=1` is unchanged and now lands on exactly its
  figure, which leaves flaking out (§1). The scripts chart's click sets
  FAIL, plus FLAKING when that bar has a flaking segment.

### U10 Performance impact (round 2)

| Page | First-paint data requests | Payload |
|---|---|---|
| Home (and a build's tab) | 4 → 4; Flaky +1 on click only | integers |
| Browse, script page | 1 → 1; FLAKING is a value on it | +13 B a row |
| Watch | unchanged | `flaking` per env/product card |
| Test page | unchanged, detail first | the mark and history |
| Open Actions | 2 → 2, + expiring (not awaited) | summary → names |

Home's four: headline, active queue, `assigned_trend`, browse. The toggle
appears with the headline, already in flight; nothing waits on it. Open
Actions excludes a smaller set than §3b measured, an upper bound; the
browse filter's cost on rows and count (a Known slow row) is §2's and
§P's. Render: a class and a word per flaking row, a pill per marked row,
one hidden button, no per-row listener; no marks, today's DOM.

### U11 Guards (`tests/test_frontend_calls.py`) (round 2)

- **Widen `ResultEmphasisTest`, every assertion kept**: each row builder
  passes one `displayResult(` value to `resultClass(` and `resultChip(`;
  `ghostChip` takes a raw result or `previousShown(`, never
  `displayResult(` (both planted); `.result-flaking` chips, rows and
  pressed toggles use the strong token, and the pressed-toggle ink rule's
  `:not()` list names it, or that rule wins on specificity. Also widened:
  `MutedBesideFailingTest`, `AssignedSplitTest`, `SortingTest`.
- **New**: `FlakyWordingTest` (planted; the flaking words only in
  `api.js`), `FlakyMarkControlsTest`, `FlakyTabTest` (click only),
  `AllClearSetAsideTest`, `OpenActionsCatalogueTest` (`actions.js` never
  names `api/summary`; its Failing mode sends FAIL and FLAKING),
  `FlakingFilterTest` (after FAIL; hidden until
  `flaky_total > 0` or pressed; read at load; on the existing request),
  `PromotionTransitionTest` (prev FAIL, state failing: `FLAKING → FAIL`).
- **DOM shim**: U9's five states; an assigned flaking row on Open Actions
  reads FLAKING; no new fields, or `flaky_total: 0`, draw the WP-43 DOM.

### U12 Pushback and asks

Nothing is pushed back; `flaky_marks` is granted (§3b). Round 1's ask is
granted too: a tab entry's window is `recent_failures` of `recent_runs`
(§2 B.2), so `runs` is only a row's. §2 B.3(c) splits FLAKING from FAIL
exactly as U9's chip does (a muted flaking row is FAIL).

### Open questions

None. Round 1's question 1 is closed by §9 (U6); question 2 by §9's rule
(U9: My actions keeps its flaking tests).

### What's new (draft, for the drop) (round 2)

- **Mark a test as flaky** from the selection bar or its page, with a
  reason and how many failures in a row count as failing (2 to 10), on
  mainline and every build, until removed.
- **Flaking is its own state**: FLAKING in its own colour, beside the
  figures ("+4 flaking"), still against the pass rate. The night it fails
  past its cadence it is a new failure, red again.
- **Open Actions leaves out unassigned flaking tests**; an assigned one
  stays, reading FLAKING. "All clear" names what is set aside.
- **All tests gains a FLAKING filter**; FAIL now leaves flaking tests out.
- **A Flaky tab beside Muted** lists every mark: owner, cadence, age and
  how often the test has actually failed.

## §3a Performance design pass (§P, round 0, performance-engineer, base `8d1e28a`)

Numbers marked §B are B.5's. The rest are mine. They were taken on copies of the two seeds §B rebuilt (perf-seed/1 and -h90), with the 65-assignment fixture, 10 mutes and §B's marks fixture: the largest environment, current failures first, cadences 3,3,2,5,3,4,10,3. SQLite runs were in-process with memos cleared, as medians of 96 to 128 calls, alternated wherever a delta is quoted. MariaDB runs were by hand on the perf-seed/1 copy loaded into a local MariaDB 12.3 (not production's 10.3), as medians of 40 calls through the vendored driver. This machine ran fast today: on perf-seed/1 the cold headline took 17.5 ms, the unsliced `/api/summary` 36 ms and Open Actions' rows 20 ms, about a fifth of the Known slow figures, so only deltas compare. Estimates are labelled as estimates.

- **The headline: proceed with a cheaper Q1.** §B's +2.2 to 2.7 ms at 50 marks stands. At 1,000 marks, Q1 hands Python every mark and the headline discards the 899 that are passing. The cheaper Q1 does two things:
  - It is driven from the stream's FAIL rows: the covering range of `idx_latest_runs_result`, then the marks' primary key. That is 171 rows on mainline here, at any mark count.
  - It counts `flaky_total` in SQL from the marks, by `CROSS JOIN`. That count takes 0.35 ms at 1,000 marks.

  Measured Q1+Q2+fold, §B's shape against the cheaper one:

  | Database, seed | 50 marks | 1,000 marks |
  |---|---|---|
  | SQLite, perf-seed/1 | the same (±0.3 ms) | 5.8 → 3.3 ms |
  | SQLite, -h90 | the same | 6.6 → 3.7 ms |
  | MariaDB, perf-seed/1 | the same | Q1 9.9 ms → 1.4 ms, plus the count at 2.3 ms |

  MariaDB pays the most because the pure-Python driver decodes every row it is handed. With this shape the cost grows with the stream's failures, not with the number of marks, which is §1's own cost bar. The count can also carry the per-environment "marked and fixed" figure that U12.7 needs. It can be one statement (`UNION ALL`) or two.
- **Plan trap (mine).** An inner `JOIN` on `test_flaky_marks` lets SQLite drive from `latest_runs`, a scan of the stream's partition. Written that way the count took 1.4 ms with 50 marks; written with `CROSS JOIN` it took 0.024 ms. Every statement on the marks needs its driving side pinned (`LEFT JOIN` or `CROSS JOIN` from the marks, or the FAIL range), and a plan guard on each.
- **Against the bar.** The headline's A/A band is −0.4 to +3 ms (WP-43 §3a). 50 marks sits inside it. 1,000 marks in §B's shape (+6.5 ms) is outside it and would read SLOWER at the runner's 5 % threshold. With the cheaper Q1 it is +3.3 to 3.7 ms on SQLite. The growth is bounded by marks, never by the estate, so no cap is needed.
- **Open Actions' first paint (D-B3): proceed with the cheaper Q1; recommend a catalogue-only request.** The unsliced payload (36 ms) is Open Actions' slower request (its rows take 20 ms), so whatever the counts add lands on that page's first paint in full:
  - +2 to 6 ms on SQLite (§B).
  - On MariaDB, an estimated +6 ms at 50 marks and +17 ms at 1,000 in §B's shape, or about +10 ms at 1,000 with the cheaper Q1. These are built from my statement timings: Q1 0.8 and 9.9 ms, Q2 5.1 and 6.7 ms.

  The payload cannot drop the counts while its queues must agree with the headline's. The bigger saving is §B question 3's catalogue-only request: the queues Open Actions never reads already cost about 36 − 17.5 ≈ 19 ms of that payload, more than this feature adds at any mark count. Recommended as its own small package, scheduled by the owner.
- **The rows' join (B.9): proceed with the join.**
  - With no marks: +0.09 ms per 250 rows on SQLite (§B) and +0.2 ms on MariaDB (2.11 → 2.32 ms), inside the browse rows' ±1 ms band.
  - With 1,000 marks: +0.7 ms on MariaDB.
  - The strict alternative, a separate statement, is a round trip even on an empty table (0.19 ms), and 3.1 ms at 1,000 marks if it reads the marks whole.

  Condition: the join goes on a rows statement only. Never on a `COUNT(*)`, and never on the comparison's pairs query (the 268 ms Known slow row): compare rows (U12.6) take it on the returned page only.
- **The floor: sound for builds, unsound for mainline on a migrated database, which is production's case.** For builds, `first_seen` is widened at import and re-derived after a delete. With the floor, Q2 on -h90 went 4.9 → 1.7 ms on nightly/full and 6.2 → 0.4 ms on the branch, matching §B. Mainline is different:
  - Migration 9, and its MariaDB ledger step, stamped mainline's `first_seen` with the clock at the time the migration ran.
  - An import only ever moves `first_seen` earlier, and every later import is newer, so it never moves.
  - So every mainline run older than that migration lies under the floor. A marked test with fewer than N mainline runs since then would wrongly read "failing".
  - The seeds hide this: they are built fresh, so their `first_seen` is the first run.

  Mainline gains nothing from the floor anyway (0.69 against 0.68 ms, 1.95 against 1.91 ms on -h90). **Condition: apply the floor on build streams only.** On a stream that runs a test rarely, Q2 reads every run of the test, on any stream, since its Nth-newest run on that stream, not just N runs. The floor bounds this by the stream's age.
- **Q2 on MariaDB (mine, 12.3).** It parses, and plans as it does on SQLite: each derived table is a `range` on `uq_runs_identity` with an index condition.
  - A full chunk of 100 is 34 KB on the wire, far under `max_allowed_packet`, and takes 12.6 to 13 ms.
  - That is about 0.11 to 0.13 ms a test, three to four times SQLite's 0.035 ms. Q2 is the dearer half of the headline on production: 5.1 ms for the fixture's 42 tests at 50 marks.
  - Parenthesised union members are 30 % cheaper, but SQLite rejects them. An `OR` over the triples reads every run since the floor, which is a year on mainline.
  - The fixture is the worst case: it marks tests that fail every night. A marked test that fails 1 run in 3 needs Q2 about 1 time in 9 (estimate).

  **Gate:** CI's 10.3 legs run `FlakyCadenceTest` on MariaDB with at least 101 tests, so that both a full chunk and a second statement are executed. On top of that, a by-hand `EXPLAIN` on a production-scale MariaDB copy during `/perf-ab` (WP-43 decision 12). An `EXPLAIN` assertion on CI's few-row tables proves nothing.
- **The trend (D-B1): proceed with "as it stood that night".** At the default 14 days with 50 marks it costs 4.6 ms against 3.8 ms for "marked now" (§B), on top of the split's 23 ms. It is off the first paint, and only the trend card waits for it.
  - On MariaDB both statements start from the marks. At 1,000 marks on perf-seed/1 the "that night" read took 26 ms against 19 ms for "marked now" (statement only, mine).
  - "That night" ships every run through the driver, so its premium grows on MariaDB (estimate).
  - There is no cheaper shape under either meaning: both read every run of each marked test in the window. The covering index in the open trend decision would serve this read, Q2 and the tab.

  **ESCALATE only the 90-day × 1,000-mark corner** (248 and 366 ms, §B), as part of that existing decision (accept, index or cap), not as a new one.
- **The Flaky tab (D-B2): proceed, on condition it uses Q2's shape.** Q2's shape at LIMIT 20 for a page of 100 marks: 5.7 ms on perf-seed/1 and 11.4 ms on -h90 (SQLite), 12.9 ms on MariaDB (perf-seed/1). §B estimated about 7 ms. `recent_results`, which today's stability figure reads, took 52 ms for the same page on -h90, so it must not be reused here.

  **To reconcile:** §UI U8 orders by state and then by "most failures", sorting in the browser. That needs the measure for every mark, not one page: about 110 to 130 ms at 1,000 marks (estimate). Either the server pages in SQL by an order it knows without the measure, or that growth is accepted, off the first paint.
- **The over-fetch (B.4) is exact.** The failing queues are capped lists with no offset. Measured +0.056 ms at F = 56 (§B).
- **What the UI asks for (U12).**
  - `streak` and `runs` cost nothing: B.1 decides them from `result` and `prev_result`, except in Q2's row, where Q2's up-to-N results give both.
  - "A marked test is never in Fixed" costs one per-environment count and the same over-fetch.
  - Telling a tolerated failure from a real fix is Q2 again, at LIMIT N+1, for marked tests that pass after a failure. At 1,000 marks that was 27 tests in 0.38 ms on perf-seed/1 and 8 tests in 0.12 ms on -h90, one statement either way. About 0.11 ms a test on MariaDB (estimate).
  - Either Fixed rule is cost-neutral at realistic counts, so that choice is semantic.
  - `"flaky": null` adds 13 bytes a row, 3.2 KB on a 250-row page. `"mute": null` is the precedent.
- **Memos: proceed, on one condition.**
  - A mark write drops every memo, as a mute does, at a human rate. The push path gains nothing: Q2's memo key carries the stream at index 1, so a push's targeted drop matches it.
  - The summary memo's 60-second TTL means a glance pays the cold cost anyway.
  - **Condition:** the fold must leave `mute_epoch()`'s return value unchanged. Five callers test it for `None` or put it in memo keys (for example, `muted_cells` returns `[]` on `None`). The marks bit goes in the same memo entry, beside it. The fold costs +0.02 to 0.1 ms on MariaDB.
  - `flaky_states` adds one entry per environment of every stream read (7 on mainline here) to a 128-entry memo that clears completely when full. A cold home load stores 46 entries today.
- **The measured pass, planned now.**
  - **Fixture**, added to `tools/dev/perf` (`ab.py --marks N`, `seed.py --onto --marks N`): 0, 50 and 1,000 marks in §B's order and cadences, plus 10 marks in a second environment. It sits on the 65-assignment fixture, with at least one marked test also muted and one also assigned. Both seeds.
  - **Rows timed:** the six Known slow rows (with 0 marks every one reads "same" with equal statement counts, the rows' join being the only change); the headline; the unsliced payload; Open Actions' rows; browse rows and count; Watch with an environment card; a build's headline (Q2 with the floor); `parts=assigned_trend` at 14 and 90 days on -h90; one `/api/flaky` page; the test detail.
  - **Statement counts** for `summary_rollup`, `queue_counts`, `status_queue`, `mute_epoch` and the new marks reader.
  - **MariaDB:** syntax on CI's 10.3 legs; plans and timings by hand on a production-scale copy (the migration tool loads perf-seed/1 in 4 s).
- **Verdict.**
  - Headline: proceed with the cheaper Q1.
  - D-B3: proceed on the same condition; the catalogue-only request is recommended as its own package.
  - Rows' join: proceed.
  - Q2: proceed, with the floor on build streams only and the 101-test MariaDB case.
  - D-B1 "that night": proceed; ESCALATE only the 90-day × 1,000-mark corner, as part of the open trend decision.
  - D-B2: proceed with Q2's shape; §UI U8's sort to reconcile.
  - U12: cost-neutral.
  - Memos: proceed, leaving `mute_epoch()`'s return value unchanged.

## §3b Performance pass, round 1 (§P, performance-engineer, base `8cee580`)

An addendum to §3a; the code is unchanged from §B's base `652b0bb`. Numbers marked §B are B.5's; the rest are mine unless marked as estimates. SQLite numbers come from copies of perf-seed/1 and -h90, with the 65-assignment fixture and §B's marks and cadences. They were taken in-process, cold (memos cleared before every call) and alternated, over 24 to 48 calls. Paired bands were ±0.3 to 1 ms. This machine ran SQLite at about a fifth of the Known slow figures (rows 20 to 24 ms against 97), so compare deltas only. The MariaDB numbers are from the perf-seed/1 copy loaded into a local **12.3, not production's 10.3**, over 30 to 40 calls through the vendored driver. Bands there were ±3 to 5 ms, and a 0-mark A/B read +0.5 [−2.0, +4.8].

- **Open Actions' rows, §B rechecked.** On SQLite I reproduce §B: +3.0 to 3.4 ms at 50 marks, +5.8 to 6.4 at 1,000. §B's head also ran Q1's flaky_total member (`'c'`), which the rows never read. Without it the cost is +3.1 and +5.6 ms. On -h90 it is +2.5 and −0.3, because the replacement rows carry cheaper streaks. **MariaDB (12.3), from a base of 80 to 83 ms:** +9.2 [+6.9, +12.2] cold at 50 marks and +19.1 [+16.3, +22.0] at 1,000; 0 marks reads the same. At 1,000 marks the cold extra breaks down as Q1 `f` 1.6 ms, `d` 0.4, `c` 3.4 and Q2 5.2. The rest is the walk and the predicate.
- **Is "the rows walk further" inherent?** Yes, and it has a ceiling. The rows statement walks the stream's partition in sort order until it has 100 open rows. Excluding X moves the 100th match deeper. Its worst case is a walk of the whole partition: 4.8 → 6.9 ms on SQLite, 15.3 → 22.7 ms on MariaDB. Base already pays that on its own second page or on a quiet night. With the predicate alone (Q1 and Q2 taken from a memo) the cost is +0.6 and +2.2 ms on SQLite at 50 and 1,000 marks. The same shape on a smaller open set does not cost more.
- **Cheaper shapes for the rows**, each measured unless marked:
  - (a) **No `'c'` member on the rows path**: −0.8 ms on SQLite and −3.4 ms on MariaDB at 1,000 marks. B.4 already says so; B.5 measured with it in.
  - (b) **One memo entry for Q1+Q2 per stream, filled by whichever page reads first, with Qc kept apart.** After a Home glance (60 s TTL, no push or act since), the rows cost +0.7 and +2.4 ms on SQLite and +3.0 and +8.4 on MariaDB, against +3.1 and +5.6, +9.2 and +19.1 cold.
  - (c) **Q2's entries survive assignment, mute and mark acts.** Their key already pins the latest start and N, so they cannot go stale; only deletes need to drop them. A cold rerun after an act then pays Q1 only: about 2 ms on MariaDB instead of about 7 (estimate, from the statement times).
  - (d) **On MariaDB, bind X as a row-value list**, `(lr.environment, lr.script, lr.test_name) IN ((?, ?, ?), …)`, still gated. At 1,000 marks the rows statement takes 22.9 ms against 26.1 for the groups, and the count 11.5 against 13.0: −4.7 ms. On SQLite the two forms are equal within 0.2 ms.
  - The page needs all of Q1 `f`+`d` and Q2. X's flaking members need Q2, and the page's own "over its cadence" rows need streak and runs, so nothing smaller is exact. **With (a) and (d), the MariaDB cost at 1,000 marks is about +11 ms cold and +4 ms warm (estimate).**
- **What a developer meets.** Open Actions fetches the catalogue only on its first `refresh()` (`static/actions.js` 203–206). Every later refresh fetches only the rows, so it pays the rows' delta with nothing taken off. That covers filters, sort, bulk assign, mute and retire. A per-row assignment patches the row in place and sends no request, but it drops the memos on the server, so the next refresh is cold.
- **The page as a whole: §B's claim holds on SQLite and fails on MariaDB.** I measured it as the page makes it: two requests on two long-lived worker threads in one process, sharing the GIL, cold.
  - **SQLite:** today's page waits 52.4 ms. After the change it waits 24.9 ms with no marks and 33.0 at 1,000: 27 and 20 ms less, better than §B's 12 to 18.
  - **MariaDB:** the rows are the slower request already (82 ms alone, against 72 for the full payload), and the page waits 95 to 100 ms. After the change the gain is −11.7 ms with no marks, +2.6 at 50 marks and +11.0 at 1,000 (§B's shape, `'c'` in).
  - So on production's backend, `parts=names` pays for the marks only up to about 50. With (a) and (d), 1,000 marks would read about +3 ms (estimate).
- **`parts=names`.** SQLite: 2.4 ms cold, 14 statements (15 product-scoped), matching §B. MariaDB: 18.9 to 19.3 ms against the full payload's 70.5, a saving of 51 ms.
  - It is not the floor. Nine of the statements build `stale_before` through the pass view: 13.1 of MariaDB's 19 ms, memoized, so warm after any summary read. Only the review panel's retire gate reads `stale_before` (`actions.js` 545).
  - Trimming it would not move the first paint, which waits for the rows on both backends. Leave it.
  - **`flaky_marks`** (U12.1) costs 0.005 to 0.015 ms on SQLite and 0.056 ms on MariaDB as its own statement. As a `UNION ALL` row on the products statement it costs no extra round trip. Grant it.
- **The parameter ceiling.** The authoritative target, Python 3.6.8 on RHEL 8, links SQLite 3.26, which allows 999 parameters. MariaDB has no parameter ceiling, because the driver interpolates the values.
  - §B's form needs 213 parameters at X 56, 705 at X 356 (a synthetic outage: 300 marked tests flaking at once) and 1,345 at X 856. At 1,345, with a 999 limit planted, SQLite answers "too many SQL variables". In this seed's shape the limit falls at X ≈ 500 to 600; §B's ~300 is the worst case of one test per script.
  - Not at the fixture's X ≤ 57: X is bounded by the stream's marked failures plus the dormant assignments. Reachable when an outage fails hundreds of marked tests in one night.
  - **§B's fallback, measured** (keys in order, X dropped in Python, then the page fetched by key): the same page and `total` every time. On SQLite it is **cheaper** than the bound predicate at every X: −1.4 ms [−3.0, −0.5] at X 56, −3.6 at 356, −5.0 at 856. On MariaDB it brings nothing (+4.5 [+1.2, +10.4] at 50 marks, −2.0 [−8.8, +4.3] at 1,000).
- **Decision 3 at the headline handler.** On SQLite, adding the "Newly fixed" candidates costs +3.1 ms [+2.3, +3.9] on perf-seed/1 (27 candidates) and +2.1 [+1.2, +3.4] on -h90 (8), at 1,000 marks. That is not §B's +0.5 to 1.5: statement timings miss the cold page cache.
  - The headline then reads +6.3 to +6.9 ms at 1,000 marks, against decision 18's agreed +3.5. At 50 marks it reads +2.8 to +3.0, because the fixture has no candidates there.
  - One Q2 statement for both lists saves 0.7 ms [0.3, 1.0].
  - On MariaDB, Qc with the candidates takes 3.43 ms against 1.97 without, and Q2 at N+1 takes 3.18 ms for 27: about +4.7 ms (statements).
- **MariaDB plans (12.3).** `CROSS JOIN` is not pinned: the optimizer reorders. Its choices were the cheap ones each time, so no `STRAIGHT_JOIN` hook is needed on 12.3; 10.3 is still to check.
  - Q1 `f` drives from the marks at 50 (0.65 ms) and from `idx_latest_runs_result` at 1,000 (1.58 ms). `STRAIGHT_JOIN` measured 0.89 and 1.30 ms.
  - `d` drives from the marks at 50, and from `idx_current_assignments_assignee` at 1,000.
  - Qc scans the marks with a temporary table and a filesort. It is inherent, since it counts every mark: 0.37 and 3.43 ms.
  - The predicate leaves both plans unchanged: the covering range for the count, a primary-key walk for the rows.
  - The fold's `MIN` is "Select tables optimized away": 0.096 ms against 0.091.
- **The three bit carriers and the join: sound.** No marks costs nothing, measured on both backends. The bit is estate-wide, though: one mark anywhere turns on Q1, the fold and the join for every product. That costs about 0.1 ms on SQLite and 0.7 on MariaDB in a product with no marks (estimate, from Q1 `f`'s statement times).
- **The test page's mark history**: in the detail. One key-range statement takes about 0.06 ms on MariaDB (estimate, from the `MIN` round trip). Fetching it when the section opens would cost a request on every open.
- **The measured pass, replanned.** The harness gains `--marks N` and a two-worker page mode. That mode is mine; it is this pass's `p_page.py`.
  - **Item 1, `parts=names` alone:** names against the full payload, and the two-worker page, on both seeds and MariaDB, with its 14/15 statements pinned.
  - **The flaky build:** the six Known slow rows at 0 marks. Open Actions' rows at 0, 50 and 1,000 marks, cold and warm (a headline call first), on both seeds and MariaDB. The page at 1,000 marks on MariaDB. The headline with decision 3. The outage case at X ≥ 300 on SQLite with 999 planted. MariaDB plans by hand on a production-scale copy, on 10.3 if one can be had.
- **Verdicts.**
  - **Rows: ESCALATE, recommending accept with (a) to (d).** The owner chooses between this cost on Open Actions' first paint and every refresh, and reversing decision 23, which costs nothing.
  - **Whole page:** proceed with `parts=names` first. §B's 12 to 18 ms claim holds on SQLite only and must be rewritten.
  - **`parts=names`:** proceed as designed. Grant `flaky_marks`.
  - **Ceiling:** proceed. Use the keys form as SQLite's only form, which removes the ceiling and is cheaper there. Use (d) on MariaDB.
  - **Decision 3:** proceed with one Q2. The owner should know it measures 2 to 3 ms on SQLite and about 4.7 on MariaDB at 1,000 marks, not 0.1 to 0.4.
  - **MariaDB plans:** proceed. No hook on 12.3; the 10.3 check stays.
  - **Bit carriers and join:** proceed. **History:** in the detail.

## §3c Performance pass, round 2 (§P, performance-engineer, base `ee75e46`)

Mine unless marked §B or estimate. Seed copies with §B's fixture; cold,
in-process, alternated; paired medians B−A, ms. SQLite 24 calls (-h90 16),
A/A ±0.3, B.5 reproduced within 0.5. MariaDB: perf-seed/1 in a local
**12.3, not 10.3**, 40 calls; 0-mark bands ±2.5 rows, ±0.5 headline.

| perf-seed/1 (MariaDB base ms) | MariaDB, 50 marks | MariaDB, 1,000 | SQLite, 50 ; 1,000 |
|---|---|---|---|
| Open Actions' rows, cold / warm (83 to 85) | +8.8 [+6.6, +11.3] / +2.7 | +10.0 [+8.6, +11.9] / −0.1 | +1.0 ; +1.2 / −1.3 ; −1.5 |
| Open Actions' page, two workers (91 to 92; 0 marks −14.5) | −5.4 [−6.8, −3.1] | −1.0 [−2.7, +2.3] | |
| Headline (44 to 46), 37 → 40 statements | +7.2 [+6.2, +7.9] | +15.2 [+14.3, +15.6] | +3.4 ; +6.5 (-h90 +3.3 ; +6.1) |
| … of which the promotion and cadence 2 | +0.6 [−0.1, +1.2] | +1.4 [+0.3, +2.1] | +0.5 ; +0.5 |
| Browse, no filter (10.9 to 12.2; 0 marks +0.13, the fold's row) | +0.69 | +0.48 | +0.27 ; +0.44, count same SQL |
| Failing, cold / warm (3.2 to 3.8) | +6.7 / +0.4 | +7.4 / +0.3 | +3.0 ; +3.2 / +0.3 ; +0.2 |
| Flaking, cold / warm | +4.2 / −1.9 | +7.6 / +0.3 | +0.6 ; +1.8 / −1.5 ; −0.9 |
| Either with Passing, cold / warm (11.6 to 13.1) | +7.2 to +7.5 / +1.0 to +1.2 | +8.3 to +10.3 / +0.9 to +2.8 | +3.8 ; +4.6 / +0.5 ; +1.0 |
| New failures queue rows: widened (B.3(d)) ; P bound | +0.73 ; +0.12 | +1.30 ; +0.29 | +0.24 ; +0.26 / +0.06 ; +0.07 |

- **Keys form on browse.** With Passing it reads ~12,700 keys: +11.1 to
  +12.3 warm against +0.5 to +1.0 bound (§B +12.1 to +12.6). Above the
  ceiling +14.5 to +16.7 cold (§B), else as bound: only on SQLite before
  3.32 with over ~330 of F in scope, an outage. MariaDB has no ceiling.
- **Headline.** At 1,000: Q1 1.4, Qc 3.3, one Q2 of 83 tests 9.5, 27 of
  them fix candidates. Q2's members parenthesised on MariaDB (a backend
  spelling): −1.5 / −2.0, same rows. Splitting Qc: +0.4, no. §B's harness
  leaves the candidates out of `flaky_total` (973 of 1,000).
- **`_cadence_runs_cache`.** No act stales it. The push's drop is
  correctness, not hygiene: a re-pushed or late older run changes the
  window under one key. Size: a marked FAIL/FAIL test or candidate per
  stream read (83 on mainline at 1,000).
- **Measured pass.** `ab.py --marks 0/50/1000`, both seeds: Known slow at 0,
  these rows, counts. MariaDB by hand: the same, `EXPLAIN` of all; on 10.3
  the keep term, Q1's side, the parentheses. SQLite, 999 planted, F ≥ 340.

**Verdicts.**
- Keys form on browse: proceed with §2's split; no cheaper shape is worth it.
- Browse unfiltered: proceed, at the bar. Failing, Flaking: proceed, off
  first paint; exclude keeps the FAIL range, keep is a key range over F.
- Open Actions: proceed, Q2 parenthesised on MariaDB; replaces §3b's
  estimate (+11, +4) and its page figures (+2.6, +11.0).
- Queues: proceed with P bound on New failures, `OR (lr.<triple>) IN
  (<P>)` from the memo; §2's widening above the ceiling only.
- `_cadence_runs_cache`: proceed if a push drops its stream's entries,
  with `_streak_cache`'s 60 s TTL and a cap that clears in full.
- Plans: proceed; no `STRAIGHT_JOIN` on 12.3 (Q1 from the marks at 50, the
  FAIL range at 1,000; Q2 a `uq_runs_identity` range); 10.3 is the build's.
- **Headline: ESCALATE.** Accept about +6 ms at 50 marks and +13 at 1,000
  on Home's cold headline on MariaDB (measured +7.2, +15.2, less the
  condition: estimates), or reverse decision 3's rule, which saves about 4
  to 5 at 1,000 and nothing at 50 (estimate).

## §4 Decisions

Each is written as the recommended position, with the alternative after
"Or:". The owner's verdict goes in the slot: agree, change to X, or reject.
The section named holds the reasoning. The first eleven change what a
developer sees; the rest are how it is built. A decision reads as it was
put, so a number in it may be a round 0 figure; its verdict carries what
later rounds changed. Decisions 24 and 25 were added by the round 1 review.
The Section column says where the reasoning stood when the decision was
put: an id such as D-U5, U12.7 or a B.8 question is round 0's, and round 2
folded those into §2 B.6 and §3 U1 to U12.

| # | Decision (recommended position) | Section | Verdict |
|---|---|---|---|
| 1 | For the cadence a failure is a `FAIL` and nothing else, the definition the existing flakiness signal uses. An unexpected pass or an expected failure among the last N runs keeps the test flaking | §B B.1 | Agreed |
| 2 | The per-day chart judges each past night **as it stood that night**, using today's mark: a spell over the cadence stays red there, as it was in the bars. Or: "marked now", every past failure of a marked test drawn as flaking, which colours as tolerated some runs the bars called failing; "that night" is 20 to 60 % dearer than it | §B D-B1, §P | Agreed |
| 3 | A marked test that passes after a **tolerated** failure is not "Newly fixed" and does not move the "fewer failing than before" line; one that passes after being **over its cadence** is, because that was a real failure and its fix is news. §P measured the extra read at 0.1 to 0.4 ms. Or: a marked test is never in Fixed at all, which is simpler and hides a real fix. Neither section recommended one; this is the main session's position | §UI U12.7, §P | Agreed, and kept at its measured cost, 2 to 3 ms of the headline at 1,000 marks on SQLite (§9, §10) |
| 4 | The Flaky tab's measure is "failed X of its last 20 runs" on the stream. Or: failures since the mark was made, which grows with the mark's age and says less about now | §B D-B2 | Agreed |
| 5 | The tab, the legend key, the tooltip row and the table column appear **only where the scope has a mark**; a product with no marks sees exactly today's page. Or: always shown, as the Muted tab is | §UI D-U4 | Agreed |
| 6 | The word "flaky" on screen always means the mark. The computed signal on the test page and Open Actions changes from "flaky — flips about 1 run in 3" to "flips about 1 run in 3" (and "flips often" in the card). Or: keep both meanings | §UI D-U6 | Agreed |
| 7 | The colour for flaking is a new token, a dusty mauve (`#a66993`), checked against every existing chart colour for normal and colour-blind vision. Or: a teal, which sits too close to the pass green | §UI U1 | Agreed; chips, row edges and the pressed filter take a darker step, `#8f638b`, the charts keep this one (§9) |
| 8 | A row keeps its result's colour on the chip and the row edge; flaking is a pill beside it, as muted is. Or: a mauve row edge | §UI D-U5 | Changed: a flaking row has its own chip and edge, no pill (§8) |
| 9 | The comparison page and Watch's build cards are **unchanged in this arc**: they take no account of mutes today either, and the comparison's pairs query is the slowest thing on the board. Its counts include flaking tests. Or: the flaky pill on the comparison's rows, which §P allows on the returned page only | §B D-B7, §UI U12.6, §P | Agreed |
| 10 | The reason's comment is tagged with the stream the user was looking at when they marked the test, though the mark itself covers every stream. Or: no tag | §B D-B5 | Agreed |
| 11 | When a marked test is retired the mark is kept, uncounted and flagged on the tab; when an environment is dropped its marks go with it; deleting a build touches no mark | §B D-B4 | Agreed |
| 12 | The mark's owner is the person making it. The selection bar's Assign-to box is not read, because a mark is not an assignment. A deactivated user cannot mark | §UI D-U2, §B D-B6 | Agreed |
| 13 | The selection bar gains one Flaky group after Mute: a cadence select, "Mark as flaky" and "Unmark", sharing the note box. The button reads "Set cadence" when every ticked test is already marked. The bar is about 400 px wider and wraps to three rows below about 900 px (an estimate from the markup) | §UI U3, D-U3 | Agreed |
| 14 | On the test page the button sits in the Flakiness card and opens its form there; the current mark shows as a banner under the mute banner, with its history below | §UI U5, D-U7 | Agreed |
| 15 | Migration 12 creates two tables (the marks, and one row per act) and alters nothing; the parked WP-15 moves to 13. The server may stay up for the MariaDB step | §B B.2 | Agreed |
| 16 | The Flaky tab is paged by the server in an order it knows without the measure: failing first, then flaking, then passing, newest mark first. §UI's "most failures first" would need the measure for every mark, an estimated 110 to 130 ms at 1,000 marks. Or: accept that growth, off the first paint. The order is the main session's reconciliation, for §B to confirm in round 1 | §P, §UI U8 | Agreed |
| 17 | The cadence is judged when a page is read, from the latest and previous results the board already keeps, reading run history only for a test that has failed twice running under a cadence of 3 to 10. Nothing is added to the push path. Or: a failure count kept as results arrive, which saves under 2 ms and costs every estate a change to the hottest table | §B B.3 | Agreed; as designed in round 2 run history is also read for a cadence of 2 and for the "Newly fixed" candidates, on summary paths only (§2 B.3(a)) |
| 18 | The headline reads only the marked tests that are failing, so its cost grows with failures and not with marks: about +2.5 ms at 50 marks and +3.5 ms at 1,000 on SQLite, nothing at none. MariaDB pays more: the run-history read alone was about 5 ms for the fixture's 42 tests. That read is bounded below by a stream's first run on builds only | §P | Agreed; as measured in round 2 the headline gains 3.4 ms at 50 marks and 6.5 ms at 1,000 on SQLite, 7.2 and 15.2 ms on MariaDB, accepted (§10) |
| 19 | Rows learn of their mark through a primary-key join on the rows statement: +0.09 ms per 250 rows on SQLite and +0.2 ms on MariaDB with no marks. This is the one place a board with no marks pays anything. Or: a separate lookup, a statement and a round trip on every cold page | §B B.9, §P | Agreed; since round 1 the join is added only where a mark exists, so a board with no marks pays nothing |
| 20 | The unsliced summary, which Open Actions fetches on its first paint, carries the counts so its queues agree with the headline's: nothing with no marks, +2 to 6 ms with marks on SQLite, an estimated +6 to 10 ms on MariaDB. §P recommends giving Open Actions a catalogue-only request as its own package, saving about 19 ms, more than this adds | §B D-B3, §P | Agreed, and the names-only request is built in this arc, first (§8) |
| 21 | The per-day chart's cost at 90 days with 1,000 marks (366 ms on SQLite, off the first paint) joins the trend decision already open from the assigned split: accept, a covering index, or a cap. It is not decided here | §P | Agreed: decided with the open trend decision |
| 22 | MariaDB is gated by CI's 10.3 legs running the cadence tests with at least 101 marked tests, so a full statement and a second one both execute, plus a by-hand `EXPLAIN` on a production-scale copy during the measured pass | §P | Agreed |
| 23 | A flaking test is off Open Actions' "Needs action" list and returns when it goes over its cadence. No section designed this: the list is paged in SQL and "flaking" is computed, so how they are excluded and what it costs is unmeasured. Round 1 has §B design it and §P cost it; that page's rows are a Known slow row | §7 verdict 17, §B B.8 question 2 | Decided, then narrowed (§9): only an unassigned flaking test is left out, and nothing else on the page changes. Designed in §2 B.3(b), measured in §3c |
| 24 | The browse page's Result filter has a FLAKING value, and FAIL leaves flaking rows out, so the two partition the failing tests. Browse only; Open Actions keeps its modes. Or: no filter value, declined in round 0, which the owner called a mistake | §2 B.3(c), §3 U9 | Agreed (§9) |
| 25 | On the night a marked test goes over its cadence it counts as a New failure and moves the "more failing than before" line; after that it is Still failing. Or: it appears under Still failing without ever having been new | §2 B.3(d), §3 U6 | Agreed (§9) |

## §5 Open questions

**All six are answered in §8**, and the answer to question 1 was replaced in
§9. They stand here as they were put.

1. **Does verdict 17 replace verdict 6?** Verdict 17 says a flaking test is
   "not an open issue", in reply to the report that an unassigned flaking
   test would stay on Open Actions. Verdict 6 kept an *assigned* flaking test
   on its assignee's list, with a state marker, until the Open Actions
   clean-up. Read plainly, verdict 17 covers both: a flaking test is on
   nobody's Open Actions, the assignment is kept, and the test comes back
   when it goes over its cadence. That is the main session's reading and
   recommendation; the alternative keeps verdict 6 for assigned tests. Either
   way the "flaking" state marker stays on the browse and queue rows.
2. **"All clear" while tests are flaking.** The Home banner shows "All clear"
   when nothing is failing, and does so today while failures are muted. Should
   it name what is set aside when that is non-zero ("4 flaking, 3 muted set
   aside")? (§UI question 4)
3. **A build the test never ran on.** A marked test with no run on a build is
   absent from that build's Flaky tab and counts. Confirm. (§B B.8 question 5)
4. **Open Actions' catalogue-only request** (decision 20): as its own small
   package soon, or folded into the Open Actions clean-up design?
5. **The headline's cost with marks** (decision 18) is accepted with no cap on
   the number of marks, because it grows with the stream's failures and barely
   with the marks. Confirm.
6. **Nothing tracked states "flaky tests are not tolerated" outright**, so
   there is no tenet to reword. The build rewords the places that imply it:
   the stability sentence and its comment in `static/api.js`, the Flakiness
   card on the test page, and the docstrings in `testboard/analytics.py`. Is
   there anywhere else the owner wants the new position stated, for example a
   sentence in the README?

## §6 Carried into the build, not decisions

Conditions the performance passes set and facts the briefs must carry. Each
names the pass or section it comes from.

- Every statement on the marks table pins its driving side and has a plan
  guard: an inner join let SQLite scan the latest-runs table, 1.4 ms against
  0.024 ms. The plan guards run in the MariaDB variants too, so CI's 10.3
  legs are where production's plans are checked; every MariaDB number in
  this doc is from a local 12.3. (§3a, §3c)
- The "any mark exists" bit rides statements already run, and the mute
  epoch's own return value does not change, because five callers test it.
  The bit is estate-wide: once any product has a mark, a product with none
  pays the rows' join, an estimated 0.1 ms on SQLite and 0.7 ms on MariaDB.
  (§3a, §3b)
- The New failures queue binds the promoted tests from the memo; it widens
  its read only above the parameter ceiling. (§3c)
- The cached runs answers are dropped by a push to their stream, as a matter
  of correctness: a re-pushed or late run changes the window under the same
  key. They carry the streak cache's 60 s time limit and a size cap that
  clears in full. (§3c)
- On MariaDB the runs statement's union members are written in parentheses:
  1.5 to 2 ms less, same rows. (§3c)
- SQLite before 3.32 allows 999 bound parameters, about 330 tests. Open
  Actions uses the keys form on SQLite always; the browse filter and the New
  failures queue use it only above the ceiling, which takes an outage to
  reach and costs 14 to 17 ms cold there. A test plants the ceiling. (§3b,
  §3c)
- The Flaky tab's measure uses the bounded run-history read at 20 runs, not
  the existing recent-results read, which took 52 ms for the same page.
  (§3a)
- The measured passes need a marks fixture in the performance harness (0, 50
  and 1,000 marks, with one marked test also muted and one also assigned),
  on both seeds. Its count of marks must include the "Newly fixed"
  candidates, which the design-time script left out. (§3a, §3c)
- The summary memo holds 128 entries and clears completely when full; the
  new per-environment entries add about seven per stream read to the 46 a
  cold home load stores today. Watched in the measured pass. (§3a)
- Once a mark exists, `result=FAIL` alone leaves flaking rows out for every
  caller of the rows request. An Open Actions page left open across the
  upgrade misses assigned flaking tests in its Failing mode until it is
  reloaded; the operator note says so. (§2 B.9)
- The browse list includes tests that have not run recently, so the "4
  flaking" link may open a list a row or two longer than the figure, as the
  FAIL figure's does today. (§3)
- The Home wordings that change meaning: "All clear" names what is set
  aside; the New failures tile reads "were passing or flaking before" where
  a mark exists; "Newly fixed" leaves out a pass after a tolerated failure.
  (§3 U6)
- No browser has rendered any surface in §3; the selection bar's width is an
  estimate. The operator note will say so, as every drop's does.

## §7 The owner's verdicts so far (2026-11-11, in chat, captured verbatim)

**The request, as written:**

> One of the original key tennants of the board was that flakey tests were not
> to be tollerated. We have now widdened the net of our products we have
> onboarded to include products that have accepted certain tests as flakey. I
> want to add a new first-class "Mark as flakey" action that can be applied to
> a test. My idea is that a named user can mark a test as flakey with a
> cadence (e.g "Fail if the 3 most recent runs fail"). Flakey tests will not
> clutter up know failures, and will have their own muted colour (just like
> "muted" tests are grey). They don't count as "passes" - but they don't
> clutter things up. A mute is owned by a user, by they shouldn't clutter up
> the "open action" screen

The main session read the last sentence as "a flaky mark is owned by a user";
the owner did not correct it.

**Questions put in chat, each with a recommended position, and the owner's
answer.** Where the answer is the option's label, the label is quoted; where
the owner wrote their own, it is quoted in full.

| # | Question | Answer |
|---|---|---|
| 1 | Where does the mark apply? | "All streams, one env (Recommended)": one mark covers the test in that environment on mainline and on every release build, like an assignment; the cadence is judged within each stream's own runs |
| 2 | Does a flaky mark run out? | "Stands until removed (Recommended)": a Flaky list shows every mark with its owner, age and how often it has actually failed, so stale marks are visible without nagging anyone |
| 3 | How does a marked test count in the pass rate? | "Like mute (Recommended)": a pass when it passes, with a small flaky marker; a tolerated failure leaves New / Still failing and shows as "+N flaky", but still counts against the pass rate |
| 4 | What happens when it goes over its cadence? | "Fails, nobody's": it counts as a normal red, unassigned failure like any other; the mark's owner is only a record of who accepted the flakiness; the mark stays. (The recommended position, putting it on the mark owner's Open Actions, was not taken) |
| 5 | What shapes of cadence? | "N in a row, 2 to 10 (Recommended)" |
| 6 | A test is assigned and marked; while flaking, does it show on the assignee's Open Actions? | "It stays in open actiosn for now - we will need a state marker ("flaking"?). We need to do a follow-up design to clean up open-actions in general. We need an easy way to deal with "passing" tests and bulk clear them. I view your scenario as a special case of that" |
| 7 | How do mute and the mark combine? | "Mute wins": both may exist, and a live mute takes precedence; the failure shows grey as muted until the mute runs out, then as flaky or failing. (The recommended position, independent, was not taken) |
| 8 | A release build has fewer runs than the cadence needs. What is it? | "Failing if all runs fail": with fewer than N runs, the test is over its cadence when every run the build has is a failure. (The recommended position, tolerated until N runs, was not taken) |
| 9 | Every product, or switched on per product? | "Every product (Recommended)" |
| 10 | Is a reason required? | "Required, posted as comment (Recommended)" |
| 11 | Who can remove a mark or change its cadence? | "Anyone, recorded (Recommended)": changing the cadence does not change the owner |
| 12 | Where does the Flaky list live? | "A Home tab beside Muted (Recommended)": loaded only when opened |
| 13 | How do flaking failures appear on the three home charts? | "Own fourth segment (Recommended)": after the grey, with a legend entry, on all three charts |
| 14 | What are the states called on screen? | "Flaky mark, 'flaking' state (Recommended)": the action is "Mark as flaky" and the tab is "Flaky"; a tolerated failure carries the state marker "flaking"; a marked test that passed shows a small "flaky" tag; one over its cadence is just failing, with the tag |
| 15 | Should the test page's existing flakiness signal offer the action? | "Button beside the signal (Recommended)" |
| 16 | Start round 0? | "Yes, start round 0" |
| 17 | (Unprompted, while round 0 was being assembled, after the main session reported that an unassigned flaking test would still appear on Open Actions' default list and that this would come as an open question) | "flaking (not yet promoted to failure): not an open issue. The whole point is to declutter these from triage" |

## §8 Round 0 review (2026-11-11, captured verbatim)

The owner reviewed round 0 the same day, first in the Claude Doc (a verdict
dropdown per decision) and then in chat, where the main session put what was
left as questions with a recommended position each. Captured before anything
is rewritten. Each item is classed as a decision (D), an answer to an open
question (Q) or an instruction on how to work (S).

**An instruction, given with the request to be asked what was left (S):**

> I am keane to minimize perfomance impact whilst accepting its added
> functionality so some loss is inevitable

**In the Claude Doc.** Decisions 1, 2, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14 and
15: "Agree" (D). Decision 8: "Change (see note)", and the note in full (D):

> Flaking is a first class failure state

Decisions 3 and 16 to 22 and the six questions were left blank there and
answered in chat, below. No comment threads were left on the doc.

**In chat.** Where the answer is an option's label, the label is quoted; where
the owner wrote their own, it is quoted in full.

| Item | Question as put | Answer | Class |
|---|---|---|---|
| Decision 8 | Given the note, how should a flaking row look and behave in lists? | "Own chip and edge": the row's result chip reads FLAKING in mauve and the row edge is mauve, in place of the red FAIL chip; no separate pill; a marked test over its cadence is a red FAIL with a small "flaky" tag. (Offered and not taken: the same plus a Result filter value; a red chip with a mauve edge) | D |
| Decision 3 | Does a marked test that passes after a tolerated failure show under "Newly fixed"? | "Only after a real failure (Recommended)" | D |
| Question 1 | Does "not an open issue" replace the earlier answer that an assigned flaking test stays on its assignee's Open Actions? | "Off everyone's list. It shoudln't be assigned. There is an audit of who marked the test as flakey - it doesn't assign it" | D |
| (follow-up) | What if a test already has an assignee when it is marked flaky? | "Assignment kept, dormant (Recommended)": it puts nothing on anyone's Open Actions while the test is flaking or passing, and the test returns to that person's list if it goes over its cadence | D |
| Question 2 | What does "All clear" say when the only failures are flaking or muted? | "All clear, naming what is set aside (Recommended)" | Q |
| Decision 20, question 4 | The flaking counts land on Open Actions' first paint through the full summary; a names-only request would save about 19 ms. What do we do? | "Slim request in this build (Recommended)": the first item of the build gives Open Actions a names-only request | D |
| Decision 16 | How is the Flaky tab ordered? | "By state, newest mark first (Recommended)" | D |
| Decision 21 | The per-day chart's cost at 90 days with 1,000 marks? | "Accept, decide with WP-43's (Recommended)" | D |
| Decisions 17, 18, 19, 22, question 5 | The five build choices where the recommended position is the cheapest shape measured: agree? | "Agree all five (Recommended)" | D |
| Question 3 | Is a mark shown on a build the test never ran on? | "Absent there (Recommended)" | Q |
| Question 6 | Is the new position stated anywhere besides the reworded places? | "A README section (Recommended)" | Q |

**What this changes in the doc as written, owed in round 1:**

- §3 U9 and U8 (rows): a flaking row carries its own chip and edge, not a
  pill beside a red FAIL chip. The guard that pins a row's chip to its result
  is widened, not weakened. (`ui-engineer`)
- §3 U9 (Open Actions) and §1: no flaking test is on Open Actions, so that
  page needs no "flaking" marker; verdict 6 is superseded. How the paged list
  excludes a computed set, with an existing assignment left dormant, and what
  it costs on a Known slow row, is undesigned and unmeasured.
  (`backend-engineer`, then `performance-engineer`)
- §2 D-B3 and §3a: Open Actions gets a names-only request as the build's
  first item, so the flaking counts land on no page's first paint. Its shape
  is undesigned. (`backend-engineer`, `ui-engineer`, `performance-engineer`)
- §2 B.3 and B.5: the performance pass's cheaper marks statement and the
  lower bound on build streams only replace what §B wrote. (`backend-engineer`)
- §2: the rule for "Newly fixed" (decision 3) and the Flaky tab's order
  (decision 16) are written into the backend design. (`backend-engineer`)
- §3 U6: the "All clear" banner names what is set aside. (`ui-engineer`)

## §9 Round 1 review (2026-11-11, in chat, captured verbatim)

Round 1's sections (§2, §3) and the round 1 performance pass (§3b) were put
to the owner as questions, each with a recommended position. Captured before
anything is rewritten. Classes as in §8: a decision (D), an answer to an open
question (Q), an instruction on how to work (S).

**Before round 1 began (S):** asked whether to measure the undesigned pieces
before freezing, or freeze and measure in the build:

> 1: yes agree. Let's get it measured now and then freeze.

**After it:**

| Question as put | Answer | Class |
|---|---|---|
| Keeping flaking tests off Open Actions makes its rows request slower whenever marks exist: on MariaDB +9 ms at 50 marks and +19 ms at 1,000 as first designed, an estimated +11 ms cold and +4 ms warm at 1,000 with the performance pass's four reductions; +3 to +6 ms on SQLite. Accept? | "Accept, with the reductions (Recommended)" | D |
| The "Newly fixed only after a real failure" rule measures +2 to 3 ms on SQLite and about +5 ms on MariaDB at 1,000 marks, not the 0.1 to 0.4 ms first quoted. Keep the rule? | "Keep the rule (Recommended)" | D |
| Should the night a marked test goes over its cadence be shown as new? | "Counts as a New failure (Recommended)": it appears under New failures and moves the "more failing" line that night, and is Still failing after | D |
| Should the browse page's "Failing" filter include flaking rows, a separate Flaking value having been declined? | "That was my mistake - there should have been a falking filter. " | D |
| With a Flaking value, what does "Failing" show? | "Failing excludes flaking (Recommended)" | D |
| Where does the Flaking filter value appear? | "Browse page only (Recommended)" | D |
| How is the Flaking filter costed? | "Short measured pass, then freeze (Recommended)" | S |
| Does a marked test at an unexpected pass stay on Open Actions? | "Stays listed (Recommended)" | Q |
| A test assigned from a build, flaking on mainline and over its cadence on the build: is the assignment dormant on Open Actions? | "Once a test is assigned it shows up in open actions. Full stop. Flaking doesn't stop that. Flaking stops an unassigned 'failing' test from polluting open actions if its still within flakeyness threshold. The design needs to rip out anything that is goinjg ruther than that when it comes to the action list" | D |
| Chips and row edges in a darker step of the mauve (`#8f638b`), the charts keeping `#a66993`? | "Two steps of mauve (Recommended)" | D |

**The Open Actions rule, as it now stands.** The last-but-one answer
replaces the main session's reading of verdict 17 and the "assignment kept,
dormant" answer in §8, which went further than the owner meant. The rule is
one sentence: **an unassigned test that is flaking is not listed on Open
Actions; everything else on that page is as it is today.** An assigned test
is listed whatever its state, as verdict 6 first said, and reads FLAKING
when it is. Nothing is dormant. The figures that count open assignments are
unchanged.

**What this changes in the doc as written, owed in round 2:**

- §2 B.1 and B.3(b): the dormant assignment is removed. The set left out of
  Open Actions is the unassigned flaking tests only; Q1 loses its dormant
  member; the open-assignment figures and queues are untouched. Round 1's
  measured cost is an upper bound on the smaller set. (`backend-engineer`)
- §2, new: a Flaking value in the browse page's Result filter, and "Failing"
  leaving flaking tests out, on the rows and their count. Undesigned and
  unmeasured; the browse count is a Known slow row. (`backend-engineer`,
  then `performance-engineer`)
- §2, new: a marked test counts as a New failure on the night it goes over
  its cadence. The extra run read is estimated, not measured.
  (`backend-engineer`, then `performance-engineer`)
- §2: the performance pass's round 1 conditions folded in: the rows skip the
  count member; one memo for the marks and runs reads per stream, shared with
  Home; cached runs reads survive assign, mute and mark acts; on MariaDB the
  set is bound as a row-value list; on SQLite the keys form is the only form,
  which removes the parameter ceiling; one runs statement for both of
  decision 3's lists; the mark's history comes with the test detail; the
  names-only response carries the "any mark" flag. (`backend-engineer`)
- §3 U8 and U9: Open Actions lists an assigned flaking test, reading
  FLAKING; "dormant" goes from the Flaky tab; the footer line says only
  unassigned flaking tests are left out; the browse Result filter gains
  Flaking; a test going over its cadence reads as a New failure.
  (`ui-engineer`)

## §10 Round 2 review (2026-11-11, in chat, captured verbatim)

Round 2's sections (§2, §3) left no open question; the round 2 performance
pass (§3c) escalated one item, put to the owner as a question with a
recommended position. Captured before anything is rewritten.

| Question as put | Answer | Class |
|---|---|---|
| The design makes Home's headline slower on MariaDB when a product has marks. Today the headline takes about 45 ms cold. With marks it gains about 6 ms at 50 marks and about 13 ms at 1,000 marks; with no marks it is unchanged; measured on a local MariaDB 12.3, not production's 10.3. Accept that cost? (Offered and not taken: drop the "Newly fixed" rule, saving an estimated 4 to 5 ms at 1,000 marks and nothing at 50) | "Accept the cost (Recommended)": keep the design as it is; if the build measures worse than this on MariaDB, it comes back to the owner before it ships | D |

**Settled by the main session in round 2, not put to the owner,** because
each follows from a decision already given and only fixes how the two
layers meet:

- The Flaking filter is a new value of the request's existing `result`
  parameter and follows the chip, so a muted flaking row counts under FAIL
  (verdict 7, "Mute wins"). §2 first proposed a separate parameter that left
  `result=FAIL` meaning what it does today; the one-parameter form was taken
  so a filtered view is one linkable value. Its cost: once a mark exists,
  `result=FAIL` alone leaves flaking rows out for every caller of the
  request, so Open Actions' Failing mode sends both values.
- The Flaky tab entry's window fields are `recent_failures` and
  `recent_runs`.
- The performance pass's round 2 conditions are taken as written (§3c): the
  New failures queue binds the promoted tests; the cached runs answers are
  dropped by a push to their stream and carry a time limit and a size cap;
  the runs statement's members are parenthesised on MariaDB.

**What this changes in the doc as written:** nothing in §2, §3 or §3c. §0,
§1, §4 and §6 are brought into line with rounds 1 and 2 at the freeze.

## §11 Master plan (sizing pass; the doc is frozen from here)

One package, **WP-44**, one branch `wp-44-flaky-mark`, an ordered series of
commits, one per item. Each item is one brief; its layer, its trigger
status and its gate are stated so the performance passes are planned in
rather than discovered. The contract every brief builds against is §1; the
section named in each item holds the statements, wireframes and tests.

| # | Item | Layer | Trigger paths | Gated on |
|---|---|---|---|---|
| 1 | **The names-only request, backend:** `GET /api/summary?parts=names` returning `environments`, `assignees`, `products`, `assignment_streams` and `stale_before` under today's names, with `NamesPartTest` and its cold statement count pinned (§2 B.2, B.7). `flaky_marks` joins the response in item 6 | backend-engineer | **yes**: api.py, Open Actions' first paint | nothing |
| 2 | **The names-only request, UI:** Open Actions fetches the names part in place of the full summary; the "fetched once" gate becomes a loaded flag; `OpenActionsCatalogueTest`'s first half, the page never naming the full summary (§3 U9, U11) | ui-engineer | **first paint** (the request count stays at two; the guard is the gate) | item 1's commit |
| 2p | **Measured pass** of items 1 and 2: Open Actions' page against the base on both seeds, SQLite and the local MariaDB; the Known slow table. Expected: 27 ms less on SQLite, 14.5 ms less on MariaDB (§3b, §3c) | performance-engineer | the gate for items 1 and 2 | items 1 and 2 |
| 3 | **The mark, backend:** migration 12 with its ledger step, the registry row in the same commit (WP-15 moves to 13); the two tables in `ddl()`, the environment tables and the migration tool; `POST /api/flaky/bulk` and `/unmark` with history, the reason posted as a comment tagged with the stream, and the lifecycle of decision 11; B.1's state as a pure function with the bounded runs read; `flaky` and `flaky_history` on the test detail; the "any mark" bit and its carriers, bar the names part's, which is item 6's (§2 B.1, B.2, B.4; `FlakyMarkWriteTest`, `FlakyCadenceTest`, the migration and ledger tests) | backend-engineer | **yes**: storage.py, api.py; the test page gains the mark's read | item 1's commit (the backend items run in series) |
| 3p | **Measured pass** of item 3: the Known slow table and the test page against the base with the tables present and empty, both seeds. The bar: the same | performance-engineer | the gate for item 3 | item 3 |
| 4 | **The marks fixture in the performance harness:** `ab.py --marks 0/50/1000` on both seeds, one marked test also muted and one also assigned, the count of marks including the "Newly fixed" candidates; `tools/dev/perf/README.md` updated (§6) | performance-engineer | n/a (tooling) | item 3's commit (it writes the new tables) |
| 5 | **Controls, UI:** the two colour tokens; the wording helpers in `api.js`; the selection bar's Flaky group; the Review panel; the test page's banner, card and history; the computed signal reworded (decision 6) (§3 U1 to U5; `FlakyWordingTest`, `FlakyMarkControlsTest`) | ui-engineer | no | item 3's commit |
| 6 | **State and counts, backend:** the per-stream memo of marks and states, filled by whichever page reads first, and the runs cache with §6's conditions; `flaky` on browse, queue and Open Actions rows, the join only where a mark exists; every count in §1, and `flaky_marks` on the names part; "Newly fixed" by decision 3; the promotion in `new_failures`, the delta and the queues, bound not widened; Watch's cards; `analytics.py`'s docstrings reworded (§2 B.3(a), (d), B.4; `FlakyAgreesWithAnalyticsTest`, `PromotionTest`, `FixedRuleTest`, `TargetedInvalidationTest`, `NoMarksAddsNothingTest` and the cache and plan guards for these reads) | backend-engineer | **yes**: storage.py, api.py, Home's first paint | the commits of items 1 and 3 |
| 6p | **Measured pass** of item 6 with the fixture: the headline, the unfiltered browse rows and count, the queues, Watch and the Known slow table at 0, 50 and 1,000 marks on both seeds; MariaDB by hand on the local server with `EXPLAIN` of every marks statement. The bars are §1's; a headline worse than the owner accepted returns to the owner (§10) | performance-engineer | the gate for item 6 | items 4 and 6 |
| 6h | **Only if a plan guard fails on CI's 10.3 legs:** a `STRAIGHT_JOIN` dialect hook in `mariadb.py` for the statement that needs it, with its own guard; otherwise this item does not exist | backend-engineer | **yes**: storage.py, mariadb.py | item 6's CI run |
| 7 | **State on Home and rows, UI:** one displayed result per row for chip and edge; the tags; the tiles' set-aside suffixes and sublines; "All clear" naming what is set aside; the promotion row; the fourth segment on the two bar charts; Watch's cards (§3 U6, U7, U9; `ResultEmphasisTest` and its siblings widened, `AllClearSetAsideTest`, `PromotionTransitionTest`, the DOM shim's five states) | ui-engineer | **first paint** (no request added; the sequencing guards are the gate) | item 6's commit |
| 8 | **Lists and filters, backend:** Open Actions leaves out unassigned flaking tests, by the keys form on SQLite and the gated row-value term on MariaDB, rows, total and bulk assign agreeing; `result=FLAKING` and the narrowed `result=FAIL` on the rows request and its count; `GET /api/flaky`; `FAIL_FLAKING` in the trend part (§2 B.3(b), (c), B.2; `OpenActionsSetAsideTest`, `OpenAssignmentFiguresUnchangedTest`, `BrowseFlakingFilterTest` with the planted ceiling, `FlakyTabOrderTest`; `NoMarksAddsNothingTest` and the cache and plan guards widened to these reads and both filter terms) | backend-engineer | **yes**: storage.py, api.py | item 6's commit |
| 8p | **Measured pass** of item 8 with the fixture: Open Actions' rows and page, the browse rows and count with Failing, with Flaking and with Passing beside each, the Flaky tab's page, the trend part at 14 and 90 days, the Known slow table; MariaDB by hand as in 6p. The bars are §1's | performance-engineer | the gate for item 8 | item 8 |
| 9 | **Lists and filters, UI:** the Flaky tab; the browse page's FLAKING toggle and the links into it, the scripts chart's click included; Open Actions' footer line and its Failing mode sending both values; the per-day chart's fourth series; the What's new section (§3 U7, U8, U9; `FlakyTabTest`, `FlakingFilterTest`, `OpenActionsCatalogueTest`'s second half) | ui-engineer | **first paint** (no request added; the tab loads on its first click; guards are the gate) | the commits of items 7 and 8; ships in the same drop as item 8, never after it |
| 10 | **The drop:** a "Flaky marks" section in the README; the operator note (migration 12, create-only, the server may stay up for the MariaDB step, a restart; an Open Actions page left open must be reloaded; no browser has rendered it); `/verify`, `/review`, `/drop` | main session | n/a | every item above |

**Order and parallelism.** Each layer's items run in series on the one
branch: the backend's 1, 3, 6, 8 and the UI's 2, 5, 7, 9. A UI item and a
backend item may run side by side when neither is gated on the other, as
items 5 and 6 are. Item 7 may be briefed when item 6's commit exists,
before 6p finishes; a 6p finding goes back to item 6 and item 7 rebases.
The same holds for items 8, 8p and 9. Every backend item carries the
`test_api.py` shape and 400 tests of the endpoints it adds.

**An early drop.** Items 1, 2 and 2p stand alone: they change nothing a
developer can see except Open Actions loading sooner, and may ship as their
own drop if the owner wants the saving early. If they do, item 2 carries
its own What's new line.

**How it runs.** Fifteen items is an `/overnight` plan; item by item on the
small path is the supervised alternative. The owner chooses at kickoff.

**What the measured passes hold the build to** is §1's cost bar: nothing
with no marks on any read; the figures in its table with marks; MariaDB
taken by hand on the local 12.3 and recorded in the log, with CI's 10.3
legs the authority for correctness and for the plans.

## §12 Build status strip (WP-44, `wp-44-flaky-mark`)

The owner's go, 2026-11-11, verbatim: "Go for the build - all as one drop".
So there is no early drop of items 1, 2 and 2p; the What's new section is
item 9's alone. The run is unattended (`/overnight`), from a suite green on
both backends at `57d817b`.

One line per item of §11, kept current by the run; the restart point after
any interruption is the first line not `done` or `parked`.

| Item | Status | Commit | Note |
|---|---|---|---|
| 1 names-only request, backend | done | the commit carrying this line | `products` carries names only (the design's shape; the counted list is the headline's pass); fresh eyes found nothing to send back; 2p is its measured pass |
| 2 names-only request, UI | done | the commit carrying this line | one fresh-eyes finding fixed before the commit (a load-more hid a catalogue that had failed to load); no browser has rendered it |
| 2p measured pass | done | | the page 38 ms less on perf-seed/1 and 78 ms less on the 90-night seed (SQLite), 9.5 to 36.7 ms less on a local MariaDB 12.3; Known slow rows the same. For the owner: Open Actions no longer warms Home's headline memo, so Home opened straight after it pays its own cold headline |
| 3 the mark, backend | done | the commit carrying this line | fresh eyes found no defect; a blank reason on a cadence change and a helper with no caller fixed before the commit; the three carriers of the "any mark" bit left to item 6 |
| 3p measured pass | done | | Known slow rows the same with the tables empty; the detail of a marked test +0.07 to +0.27 ms on SQLite, +0.41 to +0.84 on a local MariaDB 12.3; the push path runs the same statements. Carried into item 6: unmark and cadence change send a statement per test on MariaDB. Recorded, outside the package: every bulk endpoint resolves each entry's stream with two statements |
| 4 marks fixture | done | the commit carrying this line | with 65 assignments it reproduces the design-time fixture's F, X and P at 50 and 1,000 marks on both seeds; seven fresh-eyes hardening points fixed before the commit; the page mode is in the harness |
| 5 controls, UI | done | the commit carrying this line | four fresh-eyes findings fixed before the commit; the bar's Set cadence is reachable only once rows carry the mark (item 6); no browser has rendered it |
| 6 state and counts, backend | done | the commit carrying this line | **over the cost bar and committed on the main session's call, for the owner to confirm** (see 6p); fresh eyes found NamedTuple field defaults above the 3.6.0 floor, removed and now detected by the gate; a test is promoted only on a run that started after its cadence was set |
| 6p measured pass | done | | Home headline: SQLite +6.2 ms (22% of base) at 50 marks and +11.4 (41%) at 1,000; local MariaDB 12.3 +17.0 (18%) and +35.8 (38%); the bar was 18% / 34% and 16% / 34%. No marks: no change, Known slow rows the same. Priced and not taken: a covering runs index, dropping decision 3, dropping the cadence-2 read. The marks fixture was dating its marks after the runs (no promoted tests); fixed in its own commit. Carried into item 8: the assigned queue's own runs read, browse's per-row Python |
| 6h dialect hook | only if a plan guard fails on CI's 10.3 legs | | |
| 7 state on Home and rows, UI | done | the commit carrying this line | two fresh-eyes points fixed before the commit; "All clear" names the muted failures even on a board with no marks (the one region that changes there); "N flaking" is text until item 9 links it; no browser has rendered it |
| 8 lists and filters, backend | done | the commit carrying this line | a cold filtered read first ran the stream's environment list (6 to 10 ms): replaced after the measured pass by the environments that hold a mark; the trend's plan guard runs on SQLite only, its MariaDB plan read by hand |
| 8p measured pass | done | | two passes. Found `parts=assigned_trend` on Home's first paint (+11% at 50 marks, +86% at 1,000 on SQLite): item 9 sends it after the headline, and Home is then the base's. Every list and filter row inside the bar on the final tree. **For the owner:** the trend part at 90 days with 1,000 marks is +909 to +1,047 ms against the design's 366; a covering runs index is priced, not taken |
| 9 lists and filters, UI | done | the commit carrying this line | Open Actions' Failing mode sends `open=1` with FAIL and FLAKING (fewer rows with marks; the same rows with none), for the owner to confirm; fresh eyes found a page appended twice after an assignment on a paged tab, fixed; no browser has rendered it |
| 10 the drop | not started | | |
