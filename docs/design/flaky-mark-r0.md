# Mark a test as flaky — design review, round 0

**Status: ROUND 0, FOR THE OWNER'S REVIEW (2026-11-11). §0 is agreed in chat;
the owner's seventeen verdicts so far are captured verbatim in §7; §4 holds
twenty-three decisions with a verdict slot each and §5 six open questions.
Nothing is built.**
Three sections were written by the owning agents against §0: §B (§2) by
`backend-engineer`, §UI (§3) by `ui-engineer`, and §P (§3a), the performance
design pass, by `performance-engineer` against the other two. §1 is the main
session's reconciliation of them. Reading bar: twenty minutes; §0, §1, §4 and
§5 are the review, and the sections are the reasoning behind them.

Two things to know before reading the sections. They were written against a
candidate contract, so where §2 or §3 says "§1" it means that candidate, and
§1 as it stands now already answers their pushback. And verdict 17 arrived
after they were written: what §B's question 2 and §3's U9 say of Open Actions
predates it (decision 23).

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
nothing on anyone's Open Actions. A flaking test is not an open issue: it
leaves Open Actions' "Needs action" list until it goes over its cadence
(verdict 17, added after this paragraph was agreed). Whether that also takes
a flaking test off the list of the person it is assigned to, where verdict 6
kept it with a "flaking" state marker, is open question 1. A Flaky tab
on Home, beside Muted, lists every mark with its owner, cadence, age and how
often the test has actually failed, so a stale mark is visible without anyone
being nagged. Any named user may remove a mark or change its cadence; every
act is recorded. The action is available on every product. It is for the
developers and managers of products that have accepted some tests as flaky, so
those tests stop crowding the failures that need triage.

Done means: mark, change and unmark from the selection bar and the test page;
the tiles, the three home charts (a fourth segment), Watch, the browse rows,
Open Actions and the Flaky tab all agree on what is flaking; mute behaves
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

## §1 The contract, reconciled

The two layers wrote in parallel against a candidate contract (this branch's
first commit carries it). This replaces it. Where §B, §UI and §P differed,
the line says whose position stands; "decision N" points at §4.

- **Identity.** A mark is per test in one environment: (environment, script,
  test name), the key an assignment uses. Fields: `cadence` (2 to 10),
  `marked_by` (the person making the mark; nobody else can be named, decision
  12), `marked_at`, `reason`.
- **The rule, per stream.** A failure is `FAIL` and nothing else (decision 1).
  The state is `passing`, `flaking` or `failing` by §B B.1's table, and `null`
  where the test never ran on the stream.
- **State on a row.** `flaky` is null or `{cadence, marked_by, marked_at,
  reason, state, streak, runs}`. `streak` is the consecutive failures ending
  at the latest run, capped at N. `runs` is how many runs the stream has of
  the test, capped at N, given when the state is `failing`: the one place the
  UI reads it, to say "every run here has failed (2 of 2)". Both were asked
  for by §UI (U12.1) and cost no read (§P). Carried by dashboard rows, queue
  rows, Open Actions' rows and the test detail, which also carries
  `flaky_history`. Comparison rows are decision 9.
- **Counted once.** Muted, else flaking, else assigned, else unassigned (§B
  and §UI agree). Flaking tests leave `new_failures`, `still_failing`, the
  per-environment `failed`, the per-script `failing`, the `assigned` figure
  the charts split on, and Watch's `unassigned_failing`. The pass rate's
  inputs do not change.
- **Counts.** `status.flaking`, `status.flaking_new_failures`,
  `status.flaking_still_failing` (one per tile, §UI U12.2), `status.flaky_total`
  (every mark in scope); `by_environment[].flaking`,
  `top_failing_scripts[].flaking`, `products[].flaking`, and `flaking` on
  Watch's environment and product cards. §UI notes nothing draws
  `products[].flaking`; it stays, because the product's failing figure has the
  flaking tests taken off it and the board never subtracts silently.
- **The trend.** `FAIL_FLAKING` per night, in `parts=assigned_trend` only,
  disjoint from `FAIL_MUTED` and `FAIL_ASSIGNED`. What it means for a past
  night is decision 2.
- **The Flaky tab** is its own `GET /api/flaky` (§B D-B8), not a queue kind as
  §UI assumed: a kind would add a statement to the full payload. Its badge is
  `status.flaky_total`, not `queue_totals.flaky`. Entries carry `failed_runs`
  and `runs` (§UI's `recent_failures` and `recent_runs`, under §B's names).
  It is paged by the server; how it is ordered is decision 16.
- **Acts.** `POST /api/flaky/bulk` with `{username, reason, cadence, tests}`
  returns `{marked, changed, unchanged, unknown}`; the reason is required for
  a fresh mark and optional for a cadence change. `POST /api/flaky/unmark`
  with `{username, tests}`.
- **Open Actions.** A flaking test is not on its "Needs action" list (verdict
  17). Neither section designed that exclusion; it is decision 23, for
  round 1.
- **An older payload** without any of these fields, and a scope with no marks,
  draw exactly today's page (§UI U7, U11).
- **The cost bar, as §P leaves it.** With no marks, no read gains a statement;
  the one exception is a primary-key join on the rows statements (decision
  19). With marks, the headline reads only the marked tests that are failing
  on the stream, and counts the marks in SQL (§P's cheaper shape, replacing
  §B's Q1); only tests that have failed twice running under a cadence of 3 to
  10 read the runs table, at most N rows each. §B's lower bound on that read
  applies to build streams only: §P found it wrong for mainline on a migrated
  database, which is production's case (decision 18).

## §2 Backend (§B, round 0, backend-engineer, base `e70c076`)

Against §0, the candidate contract in §1 and the verdicts in §7. **Migration
12, create-only.** Nothing on the push path; no existing table altered.

### B.1 Definitions

- **A failure is `FAIL` only**, `_compute_flakiness`' and `stability_of`'s
  definition: an `UNEXPECTED_PASS` or `FAILED_AS_EXPECTED` in the window makes
  the test flaking. The signal describes, the mark decides; one definition of
  a failure, so they cannot disagree on one (B.7).
- **State on a stream.** "The N most recent runs" are the triple's runs with
  that `stream_id`, newest first, up to the latest (a same-day re-run is a
  run; no stream's runs count for another). `latest_runs`' per-stream
  `result`/`prev_result` decide every case but one:

  | Latest | Previous | N | State | Reads `runs` |
  |---|---|---|---|---|
  | not FAIL | any | any | `passing` | no |
  | FAIL | none (one run) | any | `failing` (verdict 8) | no |
  | FAIL | not FAIL | any | `flaking` | no |
  | FAIL | FAIL | 2 | `failing` | no |
  | FAIL | FAIL | 3..10 | `flaking` if one of the newest N is not FAIL; else `failing`, fewer than N runs included (verdict 8) | at most N rows |
  | no row on the stream | | | `null` (never ran there) | no |

- **Counted once**: muted (verdict 7), else flaking, else assigned, else
  unassigned. Flaking leaves `new_failures`/`still_failing` (by
  `prev_result`, as mutes), `failed`, `failing` **and WP-43's `assigned`**,
  so the four bar segments sum to the unretired FAIL tests. The pass rate's
  inputs are unchanged. **Retired** tests are in no count; the mark stays.

### B.2 Data model and payloads

```sql
CREATE TABLE test_flaky_marks (     -- current mark per triple; no stream (verdict 1)
    environment TEXT NOT NULL, script TEXT NOT NULL, test_name TEXT NOT NULL,
    cadence INTEGER NOT NULL,       -- 2..10, refused otherwise by API and storage
    reason TEXT NOT NULL,           -- the marker's; a cadence change never replaces it
    marked_by TEXT NOT NULL REFERENCES users(username), marked_at TEXT NOT NULL,
    PRIMARY KEY (environment, script, test_name));
CREATE TABLE flaky_mark_history (   -- one row per act
    id INTEGER PRIMARY KEY AUTOINCREMENT, environment TEXT NOT NULL,
    script TEXT NOT NULL, test_name TEXT NOT NULL,
    action TEXT NOT NULL, cadence INTEGER, reason TEXT,   -- 'mark' | 'cadence' | 'unmark'
    actor TEXT NOT NULL REFERENCES users(username), acted_at TEXT NOT NULL);
CREATE INDEX idx_flaky_mark_history_triple ON flaky_mark_history (environment, script, test_name, id);
-- e.g. ('atlas-lab-alpha', 'net/ipv6_suite', 'test_dad_timeout', 3, 'races the DAD timer', 'dev1', '2026-11-12T09:14:03.000000')
--      history (17, <triple>, 'mark', 3, <reason>, 'dev1', …), (18, …, 'cadence', 5, NULL, 'dev2', …), (19, …, 'unmark', …)
```

**Migration 12**, claimed in `UPGRADE_PLAN.md` §1 in the build's commit;
WP-15's reservation moves to 13 (the seventh time; WP-40's precedent).
MariaDB: ledger step 11→12, `alters=()`, sizes as `step_10_to_11`, both tables
in the exporter's `ddl()`; the server may stay up. Both join
`_ENVIRONMENT_TABLES`. **Rollback**: SQLite, restore the copy taken first;
MariaDB, drop the two tables, `schema_version` back to 11.

```text
every row (dashboard, queues, Open Actions) and test detail, for the row's stream:
  "flaky": null | {"cadence": 3, "marked_by": "dev1", "marked_at": "…", "reason": "…", "state": "flaking"}
/api/summary status: "flaking": 11, "flaking_new_failures": 9, "flaking_still_failing": 2, "flaky_total": 50
  by_environment[], top_failing_scripts[], products[], Watch's e:/p: cards: "flaking": 11
parts=assigned_trend nights[]: {"date", "FAIL_ASSIGNED": 52, "FAIL_MUTED": 10, "FAIL_FLAKING": 9}
GET /api/flaky?stream=&environment=&product=&limit=100&offset=0 -> {"generated_at", "stream", "total": 50,
  "marks": [{<triple>, "cadence", "marked_by", "marked_at", "reason", "retired": false, "result": "FAIL",
             "state": "flaking", "muted": false, "assignee": null, "failed_runs": 7, "runs": 20}]}
POST /api/flaky/bulk {"username", "reason"?, "cadence", "tests": [{<triple>, "stream_id"?}]}
  -> {"marked": 3, "changed": 1, "unchanged": 0, "unknown": 0}    # unknown: no latest_runs row anywhere
POST /api/flaky/unmark {"username", "reason"?, "tests"} -> {"unmarked": 2}
test detail also: "flaky_history": [{"id", "action", "cadence", "reason", "actor", "acted_at"}]
```

### B.3 Where the cadence is evaluated (qmark-canonical, both backends)

- **(a) Read time, bounded: recommended.** Q1 reads the stream's marks;
  Python applies B.1's table; only its "3..10" row reads `runs` (Q2, one
  statement per 100 such tests).

  ```sql
  -- Q1: driven from the marks, primary-key seeks only
  SELECT fm.<triple>, fm.cadence, fm.reason, fm.marked_by, fm.marked_at,
         lr.result, lr.prev_result, lr.start_time, ca.assignee IS NOT NULL,
         CASE WHEN tr.retired_at IS NULL THEN 0 ELSE 1 END,
         CASE WHEN <_MUTE_LIVE> THEN 1 ELSE 0 END   -- and the tm join: only while mute_epoch() is set
  FROM test_flaky_marks AS fm
  LEFT JOIN latest_runs AS lr ON lr.stream_id = ? AND <triple>
  LEFT JOIN current_assignments AS ca ON <triple> LEFT JOIN test_retirements AS tr ON <triple>
  [LEFT JOIN test_mutes AS tm ON tm.stream_id = lr.stream_id AND <triple>]
  -- Q2: the newest N runs of each test, floored at the stream's first run
  SELECT k, result FROM (SELECT ? AS k, result FROM runs WHERE <triple = ?> AND stream_id = ?
      AND start_time <= ? AND start_time >= ?   -- latest start; streams.first_seen
      ORDER BY start_time DESC LIMIT ?) AS t0   -- N
  UNION ALL SELECT k, result FROM (…) AS t1 …
  ```

  SQLite: Q1 `SCAN fm` then primary keys; Q2 the runs UNIQUE index, a range.
  **The floor is load-bearing**: that index has no `stream_id`, so a build
  with fewer than N runs would walk the test's whole history (B.5);
  `first_seen` is widened from run starts at import, so the floor is exact.
- **(b) WP-8's `failure_streak_bounds*`: rejected.** Its first seek walks the
  whole FAIL streak, not N runs, and returns dates, not a count.
- **(c) A `fail_streak` column on `latest_runs`, capped at 10: rejected.** It
  saves only Q2 (1.7–1.9 ms at 1,000 marks) for an ALTER of the hottest
  derived table, a backfill of up to 10 runs per row (estimate ~1 s on the
  seed's 31,840 rows at Q2's ~35 µs a test) and a change to `_maintain_latest`
  on the push path: every estate pays what only one with marks saves.
- **Dialect.** Derived tables with `ORDER BY … LIMIT` and aliases are standard
  on both; Q1 drives from the outer side of LEFT JOINs. **MariaDB not timed
  nor syntax-checked** (the local server was down): CI's 10.3 legs gate it.

### B.4 Memos and caches

- **No marks, no statement.** "Any mark exists" is folded into `mute_epoch`'s
  statement (`SELECT MIN(expires_at), EXISTS (SELECT 1 FROM test_flaky_marks)
  FROM test_mutes WHERE …`) and its per-stream memo. Every summary path reads
  the epoch already; a mark write drops every memo, so the bit is exact.
- **`flaky_states`**: Q1+Q2 per stream, stored per environment, key
  `("flaky_states", stream, environment, mute_epoch)`, tagged `(stream,
  environment)` (`muted_cells`' pattern): counts, bars, queues, Watch, tab.
- **Q2's answers** in `_streak_cache`, key `("cadence", stream, <triple>,
  latest start, N)`, already narrowed per stream; the browse rows share it.
  The trend split: the summary memo beside `assigned_trend`'s.
- **Drops.** A push into (stream, environment): that entry, as today. A mark,
  cadence change or unmark: `_invalidate_summary_cache()`, as `mute_tests`.
  An expiry moves the epoch in the key. **A mark invalidates nothing a mute
  does not already; the push path gains nothing.**
- **Queues and bars stay exact without SQL for the cadence**: `queue_counts`
  subtracts flaking cells as `_subtract_muted` does; the two failing queues
  and `top_failing_scripts` over-fetch by the flaking count in scope and drop
  them in Python (exact: at most F entries shrink).

### B.5 Cost, measured

Copies of perf-seed/1 and -h90 (built today at `e70c076`) with the
assignments fixture (65, 10 live mutes) and the tables made by hand; marks in
the largest environment (8,000 tests), current failures first, cadences
cycling 3,3,2,5,3,4,10,3. SQLite, in-process, memos cleared per call,
alternated, median ms; B = base handler + Q1, Q2 and fold, hand-composed.

| Read (seed, calls) | 0 marks | 50 marks | 1,000 marks |
|---|---|---|---|
| `mute_epoch` + EXISTS fold (both, 400) | +0.0002 | +0.0003 | +0.0004 |
| Headline, cold handler (perf-seed/1, 32) | 17.0, +0.19 (B adds nothing: noise) | 17.8, +2.71 | 18.4, +6.51 |
| Headline, cold handler (-h90, 24) | 18.7, +0.04 | 18.7, +2.22 | 19.8, +6.28 |
| Q1 / Q2 alone (tests needing Q2) | not run | 0.14 / 0.53–0.55 (41–42) | 2.4–2.6 / 1.7–1.9 (48–49) |
| Q2 on two builds (-h90), floor off → on | not run | 3.6–4.9 → 0.4–0.6 | 4.9–5.9 → 0.5–1.8 |
| Bars: `top_failing_scripts`, LIMIT 10 → 10+F | 0.16, same | +0.014 (F 13) | +0.056 (F 56) |
| Bars: `assigned_cells` (untouched) | 0.68–0.81 | 0.68 | 0.69 |
| Browse count (untouched) | 2.49–2.52 | 2.50–2.51 | 2.53–2.61 |
| Browse rows (250), + PK LEFT JOIN on marks | 0.58, +0.09 | +0.11 | +0.13–0.18 |

States: 50 marks, 37 failing and 13 flaking; 1,000, 899 passing, 62–63
flaking. **No marks: nothing added but the rows' join (B.9).** With marks the
headline pays +1 statement, +1 per 100 tests needing Q2, and a fold: it grows
with marks, not the estate. The cold headline here is ~18 ms, not the Known
slow 90 ms (the machine swings): only deltas compare.

**Trend part** (off first paint), FAIL_FLAKING alone, on top of today's split
(-h90, 65 assignments: 4.7–4.8 ms at 14 days, 22.6–23.9 at 90). WP-43's shape:
every run of every marked test in the window is a table fetch (14,000 at 1,000
marks × 14 days); the plan starts `SCAN fm` (`CROSS JOIN`).

| Window, seed | 50 marks: marked now / that night | 1,000 marks: marked now / that night |
|---|---|---|
| 7 days, perf-seed/1 | 2.7 / 3.0 | 16.5 / 26.8 (+Q1 2.4) |
| 14 days, -h90 | 3.8 / 4.6 | 44.7 / 64.2 (+Q1 2.6) |
| 90 days, -h90 | 17.6 / 24.2 | 248 / 366 (+Q1 2.7) |

### B.6 Decisions, as positions for the owner

- **D-B1 Trend "as it stood that night"**, by today's mark: a past FAIL run
  is flaking when one of the N runs ending at it was not a failure, so a
  spell over the cadence is red there as in the bars; 20–60 % dearer than
  "marked now" (every FAIL of a marked test), which greys runs the bars call
  failing. Disjoint from muted and assigned (both "now", WP-43);
  `FAIL_ASSIGNED` reduced in Python. Only `parts=assigned_trend` carries it.
- **D-B2 The tab's measure: "failed X of its last 20 runs"** on the stream,
  via `stability_of` (WP-8's window and sentence), read for the SQL-paged
  page (100) only, Q2's shape at LIMIT 20: estimate ~7 ms a page, never on
  first paint. Alternative: failures since `marked_at` (grows with age).
- **D-B3 The unsliced `/api/summary` carries the counts** so its queues agree
  with the headline's: +2.2–6.5 ms with marks (0 without) on Open Actions'
  first paint, which reads only catalogues from it. Not the trend series.
- **D-B4 Lifecycle.** Retired: kept, uncounted, flagged. Environment dropped:
  marks and history go with it. Build deleted: marks untouched (no stream).
- **D-B5 The reason's comment is tagged with the stream the act came from**
  (`tests[].stream_id`, default mainline); the mark has none. A cadence
  change's optional reason goes to a comment and history, never the mark.
- **D-B6 A deactivated user cannot mark** (assignment's 400); unknown users
  are created implicitly. Change and unmark: anyone.
- **D-B7 Comparison and Watch's build cards unchanged** (no mute input there
  either); environment and product cards subtract flaking, also from
  `unassigned_failing`.
- **D-B8 The tab is `GET /api/flaky`, not a `QUEUE_KINDS` entry**: a kind
  would add a statement to the full payload. Badge: `status.flaky_total`.

### B.7 Tests the build writes

- `test_storage.py`: `FlakyMarkWriteTest` (acts, 2..10, reason, owner kept,
  history, comment tag, `unknown`); `FlakyCadenceTest` (every row of B.1's
  table, verdict 8, no stream leaking, floor on = off);
  `FlakyAgreesWithAnalyticsTest` (one definition of a failure, UP and FAE).
- `test_analytics.py`: `summarize_rollup`/`summarize_by_product(flaking=…)`:
  pass rate untouched, mute wins, retired out, the four segments sum.
- **`NoMarksAddsNothingTest`, the guard**: empty table, cold statement counts
  of headline, full payload, browse, Watch and `assigned_trend` pinned at
  base; only the epoch fold and the rows' join name the table. The count-join
  guard's scope widened: no `COUNT(*)` names it.
- Cache guards (`TestQueueCounts` style): headline cold = base + 1 +
  ⌈needing Q2 / 100⌉, warm = base; `TargetedInvalidationTest` extended; plan
  guards (Q1 `SCAN fm`, Q2 a range, the trend never `idx_runs_start_time_result`).
- `test_migrations.py` (12); `LedgerTest` (11→12, `alters=()`, exporter);
  `test_api.py` (shapes, 400s, paging); every storage class also on MariaDB.

### B.8 Open questions

1. D-B1: "that night" (20–60 % dearer) or "marked now" for the trend?
2. A flaking **unassigned** test stays on Open Actions' default list (§1),
   against the request's "shouldn't clutter up the open action screen".
   Removing it excludes a computed set from a paged SQL list (cost
   unmeasured): now, or in the clean-up follow-up?
3. D-B3: accept +2–6 ms with marks on Open Actions' first paint, or give that
   page a catalogue-only request in the follow-up (it would save far more)?
4. Headline: accept +2.2–2.7 ms at 50 marks, +6.3–6.5 at 1,000, or a cap?
5. A mark whose test never ran on a build is absent from that build's tab
   and counts (`state` null). Confirm.

### B.9 Pushback on §0 and the contract

- **The rows' join breaks the letter of the no-marks bar**: a primary-key
  LEFT JOIN on the rows statement only, never a count, like `_MUTE_JOIN`,
  +0.09 ms on 250 rows with no marks. The strict alternative, a memoized
  marks lookup, is +1 statement on every cold page, a round trip on MariaDB.
  Recommended: the join; the owner's call.
- **"Reads at most 10 runs each" holds only with the floor** (B.3): 3.6–5.9
  ms against 0.4–1.8 ms on -h90 without it, worse with a year of runs.
- §1 also needs `state: null`, `flaking_new_failures`/`flaking_still_failing`
  for the tiles, and the tab as its own endpoint (D-B8). The rest stands.

## §3 UI (§UI, round 0, ui-engineer, base `e70c076`)

Against §0, §1, §7. Flaky tests leave the red in a colour that reads "known
and tolerated"; a product with no marks sees nothing new. Mute (WP-40) is
the pattern: the set-aside figure rides beside the number it left, a pill
qualifies the row, the chip still states the result. Wireframes are
illustrative; no browser has rendered any of this. Key: `█` unassigned
(red), `▓` assigned (blue), `░` muted (grey), `▒` flaking, `·` empty.

### U1 The colour for "flaking"

**Recommended: `--c-flaking: #a66993`** (dusty mauve, OKLCH L .60 C .096
h 339): red's quieter cousin, a failure that is tolerated. Checked by the
WP-43 U1 method (the dataviz validator's OKLab ΔE×100 and Machado CVD
functions, light surface) against every token. Worst pairs: normal **15.9**
(red; the muted grey at .55 ≈ `#a6a6a2`), CVD **8.7** (assigned blue,
protan). Purple 19.0/17.9, green 24.1/12.4, amber 27.1/26.7. All clear the
normal floor (15) and the CVD target (8); contrast 4.15:1 on white.
**Second: teal `#0c9180`**: clears every chart neighbour (CVD 9.9 vs grey)
but is **10.4 from the pass green**, under the floor, and green sits beside
it in every row; teal also reads "fine". A lighter red is rejected as in
WP-43. The board has one light theme (no dark rules in `style.css`), so
there is no dark value to validate; on `#1a1a19` the mauve holds 4.20:1.
Never colour alone: the word "flaking" or "flaky" is always beside the mark.

### U2 Wording: one set of helpers in `api.js`, from the data

```
setAsideSuffix(muted, flaking)   "+3 muted · +4 flaking" | "+4 flaking" | ""
failingWithSetAside(n, muted, flaking)        "12 (+3 muted, +4 flaking)"
failingSplit(failing, assigned, muted, flaking)
        .words "8 unassigned, 6 assigned, 4 muted, 3 flaking" (zeros left out)
cadenceWords(3)     "3 in a row"
cadenceSentence(3)  "counts as failing once its last 3 runs have all failed"
flakyState(flaky, mute) -> {marker: "flaking" | "flaky" | null, sentence}
FLAKY_CADENCES 2..10, flakyCadenceSelect(className, value)
```

They replace `mutedSuffix` and `failingWithMuted`, printing today's bytes
when nothing is flaking. Sentences ("here" is the row's stream; verdict 8 is
the third):

```
flaking         Flaking: failed its last 2 runs; counts as failing at 3 in a row
over, runs ≥ N  Failing: its last 3 runs all failed (marked flaky, 3 in a row)
over, runs < N  Failing: every run here has failed (2 of 2); marked flaky,
                3 in a row        ("its only run here failed" for one run)
passing         Marked flaky: counts as failing once its last 3 runs have
                all failed
live mute       Marked flaky; the mute comes first until it runs out
```

The numbers need two integers §1 lacks (U12.1). Screen readers: bar rows
and trend bands say `.words`; the cadence select is labelled "Count as
failing once this many runs in a row have failed"; pills carry their word.

### U3 The selection bar

A wrapping flex row, today ≈ 1,310 px of controls (estimate). Flaky follows:

```
5 selected · Assign to [dev1▾] [note      ] Assign · Unassign · Comment only
Mute [for…▾] Mute · Unmute · Flaky [at…▾] Mark as flaky · Unmark · Clear
```

≈ +400 px: one row at 1,920 px, two down to ≈ 900, three below (today
below ≈ 660); the walkthrough measures it. **Reason**: the shared note box,
posted as the comment. **Owner**: the person marking; the Assign-to box is
not read, because a mark is not an assignment. **Cadence**: `at…`, then 2
to 10 "in a row". **Gate**: selection, cadence, and a note unless every
ticked row is marked, when the button reads **Set cadence** (the selection
keeps `marked` from `rowCell(entry)`). **Unmark** needs a selection only.
Nothing loads; errors go to `showError` and re-enable, as Mute's do.

### U4 The Review panel

A Flaky group after Mute, under Mute's gate widened to any marked test (the
Flaky tab's passing rows need Set cadence and Unmark). No request:

```
Flaky  [why is this test flaky? (required)    ] [at…▾] [Mark as flaky]
Flaky  [flaking] Flaking: failed its last run; counts as failing at 3 in a row
       dev1 · 12 days ago · race in teardown  [3 in a row▾] Set cadence Unmark
```

### U5 The test page

The mark rides the detail request (`detail.flaky`, `detail.flaky_history`).

```
FLAKY  Flaking: failed its last run; counts as failing at 3 in a row
       dev1 · 12 days ago · race in teardown  [3 in a row▾] Set cadence Unmark
FLAKINESS          unmarked: [Mark as flaky]    marked: Marked flaky, 3 in a row
flips often        score 0.31 — 9 transitions in 29 runs
  on click: [why? (required)            ] [at…▾] [Mark as flaky]
Flaky mark history ▸   Action | By | When (UTC) | Cadence | Reason
```

The banner shows only when marked, under the mute banner (mute comes
first). The button sits in the Flakiness card (verdict 15), whatever the
classification, and opens the form there: a failing test's header gains no
second form beside Mute's. Loading: with the analytics. Error: `showError`,
form kept. Older server (no `flaky` key): no button, banner or history.

### U6 Home tiles

```
New failures 12                               Still failing 31
were passing before · +3 muted · +4 flaking   failed before too · +2 flaking
```

`setAsideSuffix(status.muted_new_failures, status.flaking_new_failures)`
per tile; no Flaky tile; the pass rate does not move (§0). Loading and
error are the headline's.

### U7 The three home charts

Fourth segment after the grey (verdict 13); end label `failing`, sort and
click targets unchanged:

```
Failing tests by environment   ■ Unassigned ■ Assigned ■ Muted ■ Flaking
atlas-lab-alpha  ████████▓▓▓▓▓▓░░░░▒▒▒···  14   8 + 6, +4 muted, +3 flaking
atlas-lab-beta   ▒▒▒▒▒···················   0   all flaking: mauve only
Failing runs per day: bottom first █ ▓ ░ ▒, Unexpected pass on top.
```

`barRows` is unchanged; `failingBar` adds a `bar-flaking` segment; a row
stays while `failed`, `muted` or `flaking` is above zero. The trend takes
`FAIL_FLAKING` from `parts=assigned_trend`. **Only where the scope has a
mark** (`status.flaky_total > 0`): the legend key, the tooltip row ("0
flaking" kept), the table column, the caption's "and flaking". No marks, or
an older server: exactly the WP-43 charts.

### U8 The Flaky tab, beside Muted

```
New failures 3 · Still failing 11 · Muted 2 · Flaky 7 · Fixed 4 · …
☐ Test           State           Mark                Cadence   Failed
☐ login_retry    FAIL [flaking]  race in teardown    3 in a    6 of last
  lab-alpha·auth                 dev1 · 41 days      row       20 runs
☐ cache_warm     PASS [flaky]    slow mirror · dev2   5 in a row 0 of last 20
```

Then Assignee with Take (over its cadence a test is nobody's). Order:
failing, flaking, passing, most failures; Cadence, Failed, Age sort in the
browser (marks are human-rate). "0 of last 20 runs" shows a stale mark
without nagging. Badge `queue_totals.flaky`, "…" until the headline.
**Fetched on first click only.** "Loading…"; empty "No test is marked
flaky."; error as other tabs. Only where a mark exists; older server: none.

### U9 Rows everywhere else

```
FAIL [flaking]        under its cadence, no live mute (mauve dot and border)
FAIL [flaky]          over its cadence: an ordinary red failure (grey pill)
PASS [flaky]          marked and passing (grey pill)
FAIL [muted] [flaky]  a live mute wins: no "flaking"
```

One `flakyTag(entry)` beside `mutedTag(entry)`: the browse table, every home
queue (My actions too), a build's own-results tab, and Open Actions (the
marker and nothing else, verdict 6). Chip and row edge keep the result's
colour, as a muted row's do (`ResultEmphasisTest`); the title is the U2
sentence. **Comparison**: the build's cell gains the pill if its rows carry
`flaky` (U12.6); counts unchanged. **Watch**: `Failing 12 (+3 muted, +4
flaking)`; the hero and accent stay calm when the red is all flaking.

### U10 Performance impact

First-paint data requests: **Home** (and a build's tab) 4 → 4, +1 only when
Flaky is opened; **Watch, test page, Open Actions, comparison** unchanged.
Payload: integers on the headline and trend part; `"flaky":null` (13 bytes)
per row, ≈ 1.3 KB per 100-row browse page; the mark and history on the
detail. Render: one div per bar (≤ 12), ≤ 45 SVG rects, one legend key; per
row one property check, a pill only on a marked row, no listener. No marks:
today's DOM. Open Actions reads nothing new from its unsliced summary, so
`flaking` need not join it there. Query cost is §B's and §P's.

### U11 Guards (`tests/test_frontend_calls.py`)

- **Widen** `MutedBesideFailingTest` (tiles pass `flaking_*`; its pinned
  `buildStat("Failing", …)` gains `card.flaking`) and `AssignedSplitTest`
  (order unassigned, assigned, muted, flaking everywhere; new classes used).
- **New**: `FlakyMarkControlsTest` (one `flakyCadenceSelect(` in bar, panel,
  test page; the gate; Mark posts `cadence`, `reason`, never `assignee`);
  `FlakyWordingTest` with a planted regression ("flaking", "in a row",
  "counts as failing" only in `api.js`); `FlakyTabTest` (after Muted; never
  in `refreshAll`/`fetchSummary`; first-paint count stays four).
- **DOM shim**: no new fields, and `flaky_total: 0`, draw the WP-43 DOM;
  mute plus mark draws `muted` and `flaky`, never `flaking`; two failed runs
  at cadence 3 say "every run here has failed (2 of 2)".

### U12 Pushback on §0 and §1

1. **`flaky.streak` and `flaky.runs`** (latest consecutive failures; runs on
   the stream; each capped at N): without them verdict 8 can only say "over
   its cadence". §B reads those runs anyway.
2. **Tiles need the split**: `status.flaking_new_failures` and
   `flaking_still_failing`, as `muted_*`.
3. **Disjoint parts**: `assigned` and `FAIL_ASSIGNED` exclude flaking tests,
   or an assigned flaking test counts twice: muted, then flaking, then the rest.
4. **Watch `unassigned_failing` excludes flaking**: it drives hero and accent.
5. **`queue_totals.flaky`** for the badge; Flaky entries add
   `recent_failures` and `recent_runs`, integers, not a rate or a sentence.
6. **Compare rows** carry no `mute`, so §1 misses them: add `flaky`.
7. **Fixed and the delta line**: a marked test passing after a tolerated
   failure lands in "Newly fixed: worth verifying" and pushes "▼ N fewer
   failing than before" night after night. Ask §B for the symmetric rule.
8. `products[].flaking` has no consumer (`products[].muted` is not drawn).

### Decisions (recommended; alternative in brackets) and open questions

D-U1 mauve [teal]. D-U2 the owner is who marks [the Assign-to box, as Mute:
reads as an assignment]. D-U3 a Flaky group after Mute, "Set cadence" when
all ticked are marked. D-U4 key, tooltip row, column, caption words and tab
only where a mark exists [always, as Muted]. D-U5 chip and row edge keep the
result colour [a mauve edge]. D-U6 the computed signal stops saying "flaky"
("flips about 1 run in 3", "flips often"), so "flaky" on screen always means
the mark [both meanings]. D-U7 button in the card, mark in the header.

1. D-U2: is the owner always the person marking, or can another be named?
2. D-U4: hide the tab, legend key and tooltip row where no mark exists?
3. U12.7: should a flaky test's return to passing leave Fixed and the delta?
4. "All clear" shows while tests flake (and while muted, today): name them
   on the banner when non-zero ("4 flaking, 3 muted set aside")?
5. Comparison New failures includes flaking tests: keep, or "+4 flaking"?
6. Trend "as it stands now" or "as it stood that night" is §B's and §P's;
   the UI needs disjoint keys and words the caption either way.

### What's new (draft, for the drop)

- **Mark a test as flaky**, from the selection bar or beside Flakiness on
  its page, with a reason and how many failures in a row count as failing
  (2 to 10). It stays until removed, on mainline and every build.
- **Flaky failures stop crowding the red.** Under its cadence a failure is
  *flaking*, in its own colour, beside the failing figures ("+4 flaking");
  it still counts against the pass rate. Past the cadence it is red again.
- **A Flaky tab beside Muted** lists every mark: owner, cadence, age, and
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

## §4 Decisions

Each is written as the recommended position, with the alternative after
"Or:". The owner's verdict goes in the slot: agree, change to X, or reject.
The section named holds the reasoning. The first eleven change what a
developer sees; the rest are how it is built.

| # | Decision (recommended position) | Section | Verdict |
|---|---|---|---|
| 1 | For the cadence a failure is a `FAIL` and nothing else, the definition the existing flakiness signal uses. An unexpected pass or an expected failure among the last N runs keeps the test flaking | §B B.1 | |
| 2 | The per-day chart judges each past night **as it stood that night**, using today's mark: a spell over the cadence stays red there, as it was in the bars. Or: "marked now", every past failure of a marked test drawn as flaking, which colours as tolerated some runs the bars called failing; "that night" is 20 to 60 % dearer than it | §B D-B1, §P | |
| 3 | A marked test that passes after a **tolerated** failure is not "Newly fixed" and does not move the "fewer failing than before" line; one that passes after being **over its cadence** is, because that was a real failure and its fix is news. §P measured the extra read at 0.1 to 0.4 ms. Or: a marked test is never in Fixed at all, which is simpler and hides a real fix. Neither section recommended one; this is the main session's position | §UI U12.7, §P | |
| 4 | The Flaky tab's measure is "failed X of its last 20 runs" on the stream. Or: failures since the mark was made, which grows with the mark's age and says less about now | §B D-B2 | |
| 5 | The tab, the legend key, the tooltip row and the table column appear **only where the scope has a mark**; a product with no marks sees exactly today's page. Or: always shown, as the Muted tab is | §UI D-U4 | |
| 6 | The word "flaky" on screen always means the mark. The computed signal on the test page and Open Actions changes from "flaky — flips about 1 run in 3" to "flips about 1 run in 3" (and "flips often" in the card). Or: keep both meanings | §UI D-U6 | |
| 7 | The colour for flaking is a new token, a dusty mauve (`#a66993`), checked against every existing chart colour for normal and colour-blind vision. Or: a teal, which sits too close to the pass green | §UI U1 | |
| 8 | A row keeps its result's colour on the chip and the row edge; flaking is a pill beside it, as muted is. Or: a mauve row edge | §UI D-U5 | |
| 9 | The comparison page and Watch's build cards are **unchanged in this arc**: they take no account of mutes today either, and the comparison's pairs query is the slowest thing on the board. Its counts include flaking tests. Or: the flaky pill on the comparison's rows, which §P allows on the returned page only | §B D-B7, §UI U12.6, §P | |
| 10 | The reason's comment is tagged with the stream the user was looking at when they marked the test, though the mark itself covers every stream. Or: no tag | §B D-B5 | |
| 11 | When a marked test is retired the mark is kept, uncounted and flagged on the tab; when an environment is dropped its marks go with it; deleting a build touches no mark | §B D-B4 | |
| 12 | The mark's owner is the person making it. The selection bar's Assign-to box is not read, because a mark is not an assignment. A deactivated user cannot mark | §UI D-U2, §B D-B6 | |
| 13 | The selection bar gains one Flaky group after Mute: a cadence select, "Mark as flaky" and "Unmark", sharing the note box. The button reads "Set cadence" when every ticked test is already marked. The bar is about 400 px wider and wraps to three rows below about 900 px (an estimate from the markup) | §UI U3, D-U3 | |
| 14 | On the test page the button sits in the Flakiness card and opens its form there; the current mark shows as a banner under the mute banner, with its history below | §UI U5, D-U7 | |
| 15 | Migration 12 creates two tables (the marks, and one row per act) and alters nothing; the parked WP-15 moves to 13. The server may stay up for the MariaDB step | §B B.2 | |
| 16 | The Flaky tab is paged by the server in an order it knows without the measure: failing first, then flaking, then passing, newest mark first. §UI's "most failures first" would need the measure for every mark, an estimated 110 to 130 ms at 1,000 marks. Or: accept that growth, off the first paint. The order is the main session's reconciliation, for §B to confirm in round 1 | §P, §UI U8 | |
| 17 | The cadence is judged when a page is read, from the latest and previous results the board already keeps, reading run history only for a test that has failed twice running under a cadence of 3 to 10. Nothing is added to the push path. Or: a failure count kept as results arrive, which saves under 2 ms and costs every estate a change to the hottest table | §B B.3 | |
| 18 | The headline reads only the marked tests that are failing, so its cost grows with failures and not with marks: about +2.5 ms at 50 marks and +3.5 ms at 1,000 on SQLite, nothing at none. MariaDB pays more: the run-history read alone was about 5 ms for the fixture's 42 tests. That read is bounded below by a stream's first run on builds only | §P | |
| 19 | Rows learn of their mark through a primary-key join on the rows statement: +0.09 ms per 250 rows on SQLite and +0.2 ms on MariaDB with no marks. This is the one place a board with no marks pays anything. Or: a separate lookup, a statement and a round trip on every cold page | §B B.9, §P | |
| 20 | The unsliced summary, which Open Actions fetches on its first paint, carries the counts so its queues agree with the headline's: nothing with no marks, +2 to 6 ms with marks on SQLite, an estimated +6 to 10 ms on MariaDB. §P recommends giving Open Actions a catalogue-only request as its own package, saving about 19 ms, more than this adds | §B D-B3, §P | |
| 21 | The per-day chart's cost at 90 days with 1,000 marks (366 ms on SQLite, off the first paint) joins the trend decision already open from the assigned split: accept, a covering index, or a cap. It is not decided here | §P | |
| 22 | MariaDB is gated by CI's 10.3 legs running the cadence tests with at least 101 marked tests, so a full statement and a second one both execute, plus a by-hand `EXPLAIN` on a production-scale copy during the measured pass | §P | |
| 23 | A flaking test is off Open Actions' "Needs action" list and returns when it goes over its cadence. No section designed this: the list is paged in SQL and "flaking" is computed, so how they are excluded and what it costs is unmeasured. Round 1 has §B design it and §P cost it; that page's rows are a Known slow row | §7 verdict 17, §B B.8 question 2 | Decided (verdict 17); design in round 1 |

## §5 Open questions

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

- Every statement on the marks table pins its driving side and has a plan
  guard: an inner join let SQLite scan the latest-runs table, 1.4 ms against
  0.024 ms. (§P)
- The "any mark exists" bit rides the mute epoch's memo entry beside the
  epoch; the epoch's own return value does not change, because five callers
  test it. (§P)
- The Flaky tab's measure uses the bounded run-history read at 20 runs, not
  the existing recent-results read, which took 52 ms for the same page. (§P)
- The measured pass needs a marks fixture in the performance harness (0, 50
  and 1,000 marks, with one marked test also muted and one also assigned), on
  both seeds; §P lists the rows it times. (§P)
- The summary memo holds 128 entries and clears completely when full; the new
  per-environment entries add about seven per stream read to the 46 a cold
  home load stores today. Watched in the measured pass. (§P)
- MariaDB was checked by §P on a local 12.3, not production's 10.3: the
  statements parse and plan as on SQLite, and the run-history read costs three
  to four times SQLite's per test. (§P)
- "All clear", the "fewer failing than before" line and "Newly fixed · worth
  verifying" are the Home wordings that change meaning with decision 3 and
  question 2. (§UI report)
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
