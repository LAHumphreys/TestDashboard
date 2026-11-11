# Mark a test as flaky — design review, round 0

**Status: ROUND 0 BEING WRITTEN (2026-11-11). §0 is agreed in chat; the owner's
verdicts so far are captured verbatim in §7. Nothing is built.**
The sections are written by the owning agents against §0 and the candidate
contract in §1: §B by `backend-engineer`, §UI by `ui-engineer`, §P the
performance design pass by `performance-engineer`. §4 and §5 collect every
decision and open question in one place. Reading bar: twenty minutes.

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
nothing on anyone's Open Actions, and a test that is assigned as well as
marked stays on its assignee's list with a "flaking" state marker. A Flaky tab
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

## §1 The candidate contract

Written by the main session for the two layers to write against in parallel.
It is a starting position: either section may push back on any line, and the
reconciled contract replaces this one when the sections are assembled.

- **Identity.** A mark is per test in one environment: (environment, script,
  test name), the same key an assignment uses. At most one current mark per
  test. Fields: `cadence` (integer, 2 to 10), `marked_by`, `marked_at`,
  `reason`.
- **The rule, per stream.** A marked test is **flaking** on a stream when its
  latest result there is a failure and at least one of its N most recent runs
  on that stream is not a failure. It is **over its cadence** when its latest
  min(N, runs it has) runs on that stream are all failures. Which results
  count as "a failure" for this rule is §B's to define; the existing
  flakiness signal already has a definition and the two should not disagree.
- **State on a row.** Every row that carries `mute` today also carries
  `flaky`: null, or `{cadence, marked_by, marked_at, reason, state}` with
  `state` one of `"passing"`, `"flaking"`, `"failing"` for the row's stream.
  A live mute takes precedence in what is drawn and counted; the `flaky`
  object is still present so the tag can be shown.
- **Counts.** Wherever a payload carries `muted` beside a failing figure it
  gains `flaking` beside it, defined the same way: failing, unretired, marked,
  under the cadence, no live mute. Those tests leave `new_failures` and
  `still_failing` and the per-environment and per-script failing figures, as
  muted ones do; the pass-rate figures are unchanged. Candidate keys:
  `status.flaking`, `status.flaky_total` (every mark), `by_environment[].flaking`,
  `top_failing_scripts[].flaking`, `products[].flaking`, the Watch cards'
  `flaking`.
- **The trend.** The per-day chart gains a flaking series beside
  `FAIL_ASSIGNED` and `FAIL_MUTED`, served by the same off-first-paint part.
  What "flaking" means for a past day (as the test stands now, like the other
  two, or as it stood that night) is an open decision for §B and §P.
- **The Flaky tab.** A `flaky` queue beside `muted`, listing every mark
  whatever the test's result, fetched only when the tab is opened. Each entry:
  the test's key, `cadence`, `marked_by`, `marked_at`, `reason`, the state and
  latest result on the request's stream, and a measure of how often the test
  has actually failed (shape for §B to propose and cost).
- **Acts.** `POST /api/flaky/bulk` with `{username, reason, cadence, tests}`
  marks unmarked tests and changes the cadence of marked ones (the owner does
  not change); `POST /api/flaky/unmark` with `{username, tests}`. The reason
  is required for a fresh mark and is posted as a comment on each test, as a
  mute's is. Every act is a history row; the test detail carries the current
  mark and its history.
- **Open Actions.** Its rows carry the same `flaky` object so an assigned,
  flaking test can show its state marker. Nothing is added to or removed from
  the list by a mark.
- **The cost bar.** With no marks, every existing read costs what it costs
  today. With marks, the cadence is evaluated only for marked tests whose
  latest result is a failure, and reads at most 10 runs each.

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
  lab-alpha·auth                 dev1 · 41 days     row       20 runs
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

## §3a Performance design pass (§P)

*To be written by `performance-engineer` against §B.*

## §4 Decisions

*Assembled by the main session from the sections.*

## §5 Open questions

*Assembled by the main session from the sections.*

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
