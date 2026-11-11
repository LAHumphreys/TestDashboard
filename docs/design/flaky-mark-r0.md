# Mark a test as flaky — design review, round 0

**Status: ROUND 0 REVIEWED (2026-11-11); ROUND 1 OWED. Every one of §4's
twenty-three decisions has a verdict and §5's six questions are answered; the
review is captured verbatim in §8, which also lists what it changes in the
sections below and who owes the rewrite. Until round 1 lands, §2, §3 and §3a
read as written for round 0. Nothing is built.**
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
(verdict 17, added after this paragraph was agreed). That holds for a test
that already has an assignee too: the assignment is kept but dormant, and the
test returns to that person's list only if it goes over its cadence (§8,
which supersedes verdict 6). In lists a flaking row carries its own chip and
edge in the flaking colour, because flaking is a state in its own right
(§8, decision 8). A Flaky tab
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

## §2 Backend (§B, round 1, backend-engineer, base `652b0bb`)

Against §0, §1, §4's verdicts and §8. **Migration 12, create-only.** Nothing
on the push path; no existing table altered. §P's numbers are marked §P; the
rest were measured for this round (B.5). Minimise the cost, accept some.

### B.1 Definitions (round 1)

- **A failure is `FAIL` only** (decision 1): an `UNEXPECTED_PASS` or
  `FAILED_AS_EXPECTED` among the last N runs keeps the test flaking.
- **State on a stream**, judged on the triple's runs there, newest first, up
  to the latest; `streak` and `runs` are §1's, capped at N:

  | Latest | Previous | N | State | streak | runs | Reads `runs` |
  |---|---|---|---|---|---|---|
  | not FAIL | any | any | `passing` | 0 | null | no |
  | FAIL | none | any | `failing` (verdict 8) | 1 | 1 | no |
  | FAIL | not FAIL | any | `flaking` | 1 | null | no |
  | FAIL | FAIL | 2 | `failing` | 2 | 2 | no |
  | FAIL | FAIL | 3..10 | `flaking` if one of the newest N is not FAIL, else `failing` | leading FAILs | rows read | ≤ N rows |
  | no row there | | | `null`, absent there (§8) | | | no |

- **Counted once**: muted, else flaking, else assigned, else unassigned. A
  live mute wins everywhere, Open Actions included. Retired: in no count.
- **Dormant assignment (round 1, §8).** A marked test that is flaking, or
  passing with an assignee, and not live-muted, is on nobody's open figures
  on that stream: Open Actions, `status.assigned_open`,
  `queue_totals.assigned` and `.mine`, the assigned and My actions queues,
  WP-43's `assigned`. Over its cadence it is back. The assignment row is
  untouched; the mark never assigns. An `UNEXPECTED_PASS` is a stale
  annotation, not a failure the mark tolerates: it stays (B.8 1).
- **Newly fixed (round 1, decision 3).** A marked, unmuted, unretired test at
  not-FAIL after a FAIL is fixed only if that FAIL was over its cadence: the
  N runs ending at it (2 to N+1 of the newest N+1) all FAIL, or all it had,
  fewer than N. Otherwise it was tolerated: not in `status.fixed`,
  `queue_totals.fixed`, the Fixed queue or Home's `new_failures − fixed`.

### B.2 Data model and payloads (round 1)

```sql
CREATE TABLE test_flaky_marks (environment TEXT NOT NULL, script TEXT NOT NULL,
    test_name TEXT NOT NULL, cadence INTEGER NOT NULL,   -- 2..10, refused otherwise
    reason TEXT NOT NULL,                                -- a cadence change never replaces it
    marked_by TEXT NOT NULL REFERENCES users(username), marked_at TEXT NOT NULL,
    PRIMARY KEY (environment, script, test_name));       -- no stream: one mark per triple
CREATE TABLE flaky_mark_history (id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL, script TEXT NOT NULL, test_name TEXT NOT NULL,
    action TEXT NOT NULL, cadence INTEGER, reason TEXT,  -- 'mark' | 'cadence' | 'unmark'
    actor TEXT NOT NULL REFERENCES users(username), acted_at TEXT NOT NULL);
CREATE INDEX idx_flaky_mark_history_triple ON flaky_mark_history (environment, script, test_name, id);
```

**Migration 12**, claimed in `UPGRADE_PLAN.md` §1 in the build's commit (WP-15
to 13); MariaDB ledger step 11→12, `alters=()`, both tables in `ddl()` and
`_ENVIRONMENT_TABLES`; the server may stay up. **Rollback**: SQLite, the copy
taken first; MariaDB, drop both tables, `schema_version` back to 11.

```text
rows (browse, queues, Open Actions), test detail, on the row's stream; comparison rows: none (decision 9)
  "flaky": null | {"cadence": 3, "marked_by", "marked_at", "reason", "state": "failing", "streak": 2, "runs": 2}
/api/summary status: "flaking", "flaking_new_failures", "flaking_still_failing", "flaky_total";
  "flaking" on by_environment[], top_failing_scripts[], products[], Watch's e:/p: cards
parts=assigned_trend nights[]: {"date", "FAIL_ASSIGNED", "FAIL_MUTED", "FAIL_FLAKING"}
GET /api/summary?parts=names[&product=][&stream=]                      (round 1, the build's first item)
  -> {"generated_at", "product", "stream", "stale_before", "environments", "assignees",
      "products": [{"product": "Atlas"}], "assignment_streams"}
GET /api/dashboard?open=1[&result=FAIL|UNEXPECTED_PASS]   rows, total and bulk leave out B.3(b)'s set
GET /api/flaky?stream=&environment=&product=&limit=100&offset=0 -> {"generated_at", "stream", "total",
  "retired", "marks": [{<triple>, "cadence", "marked_by", "marked_at", "reason", "retired", "result",
  "state", "muted", "assignee", "failed_runs": 7, "runs": 20}]}            order: D-B11
POST /api/flaky/bulk {"username", "reason"?, "cadence", "tests": [{<triple>, "stream_id"?}]}
  -> {"marked", "changed", "unchanged", "unknown"}       # unknown: no latest_runs row anywhere
POST /api/flaky/unmark {"username", "reason"?, "tests"} -> {"unmarked"}
test detail also: "flaky_history": [{"id", "action", "cadence", "reason", "actor", "acted_at"}]
```

A flaking row's own chip and edge (decision 8) need only `flaky.state` and
the row's `mute`: `result` stays `FAIL`; no Result filter value (§8).

### B.3 Where the cadence is evaluated (round 1)

**(a) At read time, bounded (decision 17; WP-8's streak bounds and a
`fail_streak` column were rejected there).** Python applies B.1 to these
statements, qmark-canonical, each with its driving side pinned:

```sql
-- Q1, one per stream: §P's cheaper shape, plus the dormant member
SELECT 'f', lr.<triple>, fm.cadence, fm.marked_at, lr.prev_result, lr.start_time,
       tm.expires_at, tr.retired_at, ca.assignee
FROM latest_runs AS lr CROSS JOIN test_flaky_marks AS fm ON <triple>     -- FAIL range first
LEFT JOIN test_mutes AS tm ON tm.stream_id = lr.stream_id AND <triple>
LEFT JOIN test_retirements AS tr ON <triple> LEFT JOIN current_assignments AS ca ON <triple>
WHERE lr.stream_id = ? AND lr.result = 'FAIL'
UNION ALL SELECT 'd', …   -- dormant: current_assignments CROSS JOIN marks CROSS JOIN latest_runs
                         -- (stream's key), tm, tr; assignee set, result not FAIL or UP
-- Qc, summary paths only: §P's count in SQL, decision 3's candidates in the same pass
SELECT CASE WHEN <fixed> THEN 'x' ELSE 'c' END, fm.environment, CASE WHEN <fixed> THEN fm.script END,
       CASE WHEN <fixed> THEN fm.test_name END, COUNT(*), MAX(fm.cadence), MAX(lr.start_time)
FROM test_flaky_marks AS fm CROSS JOIN latest_runs AS lr ON lr.stream_id = ? AND <triple>
LEFT JOIN test_retirements AS tr ON <triple>
WHERE tr.retired_at IS NULL GROUP BY 1, 2, 3, 4    -- <fixed>: prev_result = FAIL, result <> FAIL
-- Q2 (round 0's shape): newest N runs per test, ≤ its latest start, one UNION ALL per 100 tests
```

- **Plans, SQLite, measured**: Q1 `SEARCH lr USING INDEX
  idx_latest_runs_result`, then the marks' key; its `d` member `SCAN ca`;
  Qc `SCAN fm` and a temp B-tree; Q2 a range on the runs UNIQUE index.
  **Every statement on the marks has a plan guard** (§P: an inner join
  scanned the partition). MariaDB does not pin on `CROSS JOIN` (B.8 4).
- **Q2's floor** (`start_time >= streams.first_seen`) on **build streams
  only** (decision 18): migration 9 stamped mainline's `first_seen` with its
  own clock, hiding older runs on a migrated database (§P).
- Q2 also serves decision 3 at LIMIT N+1 for Qc's `x` rows and the tab at
  LIMIT 20, its page only (decision 4; never `recent_results`, §P: 52 ms).
  Mute liveness is judged in Python from `tm.expires_at` at `now`.

**(b) Open Actions without the set-aside tests (round 1, decision 23).**
- **One definition.** `open=1` ("needs action") leaves out X: the stream's
  marked, unretired, not live-muted tests that are flaking, or passing (not
  UP) with an assignee. It lives in `_dashboard_filters`, so the rows, the
  `total` and `POST /api/assignments/bulk` agree (WP-26). The page sends
  `open=1` in all three modes, plus `result=FAIL` or `UNEXPECTED_PASS` for
  the narrow two (the UI's contract). Browse sends no `open`: unchanged.
- **X comes from Q1's memo** (B.4), bound as parameters: paging and the count
  stay exact SQL, and the count gains no join (its WHERE reads `lr`, `ca`):

  ```sql
  AND NOT ((lr.result = ? OR ca.assignee IS NOT NULL)      -- every member of X is one
           AND lr.test_name IN (?, …)                       -- prefilter
           AND ((lr.environment = ? AND lr.script = ? AND lr.test_name IN (?, …)) OR …))
  ```

  The gate names `ca`, so the term runs after `ca`'s probe and the open
  composite, not on every row of the partition. Ungated, the count took
  +2.9 ms at 50 marks and +13.5 ms at 1,000; gated, +0.02 and +0.09 (B.5).
- **Ceiling.** Parameters: 1 + names + 2 × groups + |X|. The MariaDB driver
  interpolates; SQLite before 3.32 allows 999, about 300 excluded tests in
  scope. Above the backend's cap the read takes the matched keys in order
  (every `DASHBOARD_SORTS` key is on `lr` or `ca`), drops X and slices in
  Python, then fetches the page by key: exact, bounded by the open items.
- **The figures agree.** `status.assigned_open`, `queue_totals.assigned`
  and `mine` lose X's assigned flaking members in scope (B.4; they count
  FAIL and UP only, so never held the passing ones); the assigned and My
  actions queues drop them. The owner strip is names, no figure: a person
  with only dormant assignments keeps a chip and the list says 0 open, which
  is true. Dormancy is judged on the stream the figure reads (B.8 2).

### B.4 Memos, caches and the no-marks bit (round 1)

- **No marks: no statement and no join, on any read.** The "any mark" bit
  rides a statement each request already runs: on summary paths, the mute
  epoch's memo entry, `mute_epoch()`'s return value unchanged (§P: five
  callers test it); on `/api/dashboard`, the environment-products statement,
  moved before the rows (`… UNION ALL SELECT NULL, MIN(environment) FROM
  test_flaky_marks`: one row, the min-optimisation on both engines); on a
  queue part whose kind reads no epoch today (fixed, unexpected passes, not
  run, assigned, mine), the same `MIN` as a scalar column on its count, run
  before its rows. Bit clear: no Q1, Qc, predicate or join; `"flaky": null`.
- **`flaky_states`**: Q1 (and Qc on summary paths) and Q2 per stream, stored
  per environment, key `("flaky_states", stream, environment)`, so tagged,
  no epoch; Q2's answers in `_streak_cache` (stream, triple, latest start,
  N). §P: about seven entries per stream join the 128-entry memo. A push
  drops its pair; a mark act or assignment drops everything, as
  `mute_tests`. **The push path gains nothing.** The names part: no memo.
- **Counts exact without SQL for the cadence**, as `_subtract_muted`: the
  flaking cells leave new, still, assigned and mine, the tolerated fixes
  leave fixed; capped queues and `top_failing_scripts` over-fetch by the
  count in scope and drop (§P: exact, +0.056 ms at F = 56).

### B.5 Cost, measured (round 1)

Copies of perf-seed/1 and -h90, the 65-assignment fixture through the base
tree's `Storage`, the tables by hand, round 0's marks (largest environment,
current failures first, cadences 3,3,2,5,3,4,10,3). SQLite, in-process, memos
cleared before every call, alternated. Base `652b0bb`'s handler; head the same
with the proposed SQL substituted at execute time, the fold, X and rows by
hand. Median ms; B−A the median paired difference [95 % band]; statements per
cold call. **MariaDB not timed.**

| Open Actions' rows, Known slow URL | 0 marks | 50 marks | 1,000 marks |
|---|---|---|---|
| perf-seed/1, 48 calls | 20.5, +0.05 [−0.25, +0.40]; 295/295 | 21.0, +3.44 [+2.89, +3.77]; 295/297 | 20.4, +5.74 [+5.44, +6.67]; 295/291 |
| -h90, 24 calls | 34.4, −0.25 [−1.04, +0.49]; 295/295 | 35.3, +2.17 [+1.75, +2.76]; 295/297 | 35.8, +0.01 [−1.20, +0.84]; 295/291 |
| `total` | 177 | 166 | 121 (-h90: 120) |

- At 0 marks the head carries the fold and `"flaky": null` (+1.5 KB per 100
  rows); a 32-call perf-seed/1 run read +0.69, +3.85, +7.35. **Where it
  goes** (perf-seed/1, 50 / 1,000): Q1+Q2+fold 2.1 / 3.4 ms (-h90 2.5 / 4.4,
  with Qc's count: an upper bound); the rows walk further for their 100,
  +0.6 / +2.7 (inherent); the gated predicate +0.02 / +0.09; the join +0.14.
  On -h90 the rows replacing 57 excluded failures carry cheaper streaks.
- **Fixture**: 50 marks: 29–30 assigned, 13 flaking, X 11 (4–5 assigned);
  1,000: all 65 assignments marked, 62–63 flaking, X 56–57 (34–36
  assigned).
- **Against the bar.** No marks: same, nothing added (met). With marks the
  row reads SLOWER by the runner's rule: +16 % at 50 and +28 % at 1,000 on
  perf-seed/1 (Known slow 97 ms; base here 20.5), +6 % at 50 on -h90, same
  at 1,000. The page still waits less (below; B.9).

| Open Actions' catalogue request | full payload | `parts=names` | B−A |
|---|---|---|---|
| unscoped, perf-seed/1, 32 calls | 38.7 ms, 161,512 B, 47 stmts | 3.0 ms, 374 B, 14 | −36.2 [−39.3, −34.9] |
| `product=Atlas`, perf-seed/1, 32 | 38.0, 154,356 B, 46 | 3.1, 346 B, 15 | −35.0 [−35.8, −34.3] |
| unscoped, -h90, 24 | 38.8, 161,571 B, 47 | 3.1, 374 B, 14 | −35.2 [−38.1, −34.1] |

The five fields equal the full payload's on every row (asserted). The page
waits for the slower of its two parallel requests: today the full payload
(38.7 ms here), after it the rows (20.5, up to 26.6 at 1,000 marks): 12 to
18 ms less, with or without marks.

**Carried.** Headline (§P, SQLite): +2.2–2.7 ms at 50 marks, +3.3–3.7 at
1,000. Decision 3 adds (statements, mine): Qc over the count alone +0.0 at
50, +0.5 (perf-seed/1) and +1.5 ms (-h90) at 1,000; Q2 at N+1 0.41 ms for 27
candidates (23 tolerated), 0.11 for 8. Composed, about +4.2 and +5.3 ms at
1,000; §P measures the handler. Tab (§P, page of 100): 5.7, 11.4 ms. Trend
(round 0, -h90): 4.6 ms at 14 days, 50 marks; 366 at 90 days, 1,000 (21).

### B.6 Decisions, as settled (round 1)

- As decided: **D-B1** trend "that night" (2, corner with WP-43's, 21),
  **D-B2** last-20 by Q2 (4), **D-B4 to D-B8** lifecycle (11), comment tag
  (10), deactivated user (12), comparison and Watch's builds (9), the tab.
- **D-B3 (round 1)** The unsliced `/api/summary` keeps the counts (its
  queues agree with the headline's) and after D-B10 is on **no page's first
  paint**. Left: `feeder/status.py` (`--status`, connection test), the curl
  hint `tools/diagnose_db.py` prints, drop notes' smoke checks, contract
  tests, Open Actions' fallback on an older server. Home, a build's tab and
  the other pages read `parts=` slices or `/api/products` already.
- **D-B9 (round 1)** Open Actions: B.3(b). **D-B10 (round 1)** `parts=names`
  is a part, not an endpoint: the handler's own scope resolution (`product=`,
  a scoped stream's product, per-product `stale_before`) makes the five
  values the full payload's by construction; an older server's 400 is the
  UI's cue to fall back. `products`: names only.
- **D-B11 (round 1, decision 16 confirmed)**: failing, flaking, passing,
  newest mark first, then the triple, no measure needed. The first two groups
  come from the memo (bounded by the stream's marked failures); passing is
  one SQL page (marks `CROSS JOIN` latest_runs, `ORDER BY fm.marked_at DESC,
  <triple> LIMIT ? OFFSET ?`, offset less the first two); retired follow,
  flagged. `total` counts every row and `retired` those; the badge
  (`status.flaky_total`) counts the unretired.

### B.7 Tests the build writes (round 1)

- `test_storage.py`: `FlakyMarkWriteTest`; `FlakyCadenceTest` (B.1 with
  `streak`, `runs`, the floor on builds only); `FlakyAgreesWithAnalyticsTest`;
  **`OpenActionsSetAsideTest`** (each mode; `total` equals the rows paged;
  bulk acts on the same set; dormancy ends over the cadence; mute wins; UP
  stays; a planted cap of 1 answers as the bound form);
  **`DormantFiguresTest`** (every B.3(b) figure agrees); **`FixedRuleTest`**
  (both ways, cadence 2, fewer than N runs); **`FlakyTabOrderTest`**.
  `test_api.py`: shapes, 400s, paging, **`NamesPartTest`**.
- **`NoMarksAddsNothingTest`, widened**: empty table, the cold counts of the
  headline, full payload, each queue kind, browse, Open Actions, Watch and
  `assigned_trend` equal base, and only the three bit carriers name the
  marks. The count-join guard's scope widened: no `COUNT(*)` joins them.
- Cache guards, with marks: Open Actions cold = base + 1 + ⌈Q2 tests / 100⌉,
  headline base + 2 + ⌈…⌉, `parts=names` pinned, warm = base; plan guards
  on Q1, Qc, Q2, the gated predicate, the trend; `TargetedInvalidationTest`,
  `test_migrations.py`, `LedgerTest` (11→12); MariaDB variants (decision 22).

### B.8 Open questions (round 1)

1. **A marked test at `UNEXPECTED_PASS`** stays on Open Actions as a stale
   annotation (B.1). Confirm, or set it aside as passing.
2. **An assignment made from a build** is dormant by the list's stream
   (mainline on Open Actions), not its origin build's: that would cost one
   more Q1 per such build.
3. **The test page's mark history**: one key-range statement on every test
   page, marked or not, or fetched when its disclosure opens (§UI).
4. **For §P**: MariaDB 10.3 plans of Q1, Qc, the gated predicate and the
   fold; whether `CROSS JOIN` needs `STRAIGHT_JOIN` there.

### B.9 Pushback (round 1)

- **Decision 19, refined, not reopened**: the rows' join is added only when
  the bit says a mark exists, so no marks costs nothing on any read.
- **Decision 23 costs Open Actions' own row** +3.4 to +5.7 ms on perf-seed/1
  with marks, SLOWER by the runner's rule; the page waits 12 to 18 ms less
  only because of D-B10. The owner's acceptance of the row's number is asked.

## §3 UI (§UI, round 1, ui-engineer, base `652b0bb`)

Against §0, §1 as reconciled, §4 and §8. Flaking is a state of its own: a
flaking row reads FLAKING, chip and edge in the flaking colour, no pill
(decision 8). Flaking tests leave Open Actions (decision 23), which drops
its marker and fetches less (decision 20). No marks: today's page. No
browser has rendered any of this. Key: `█` unassigned (red), `▓` assigned
(blue), `░` muted (grey), `▒` flaking, `·` empty.

### U1 The colour for "flaking" (round 1)

Charts keep **`--c-flaking: #a66993`** (decision 7). It cannot carry text:
white on it is 4.15:1, and a chip's 12 px semibold text needs 4.5:1. Chips
and row edges take **`--c-flaking-strong: #8f638b`**, a step darker in the
same hue (OKLCH L .56 C .08 h 330 against L .60 C .096 h 339; ΔE×100 4.6,
one colour to the eye): **white on it 4.85:1** (red's chip 5.62, green's
5.13); the 3 px edge clears 3:1. By U1's method (OKLab ΔE×100, Machado
CVD) against the chips it sits beside: normal, UNEXPECTED_PASS 16.4, red
16.6, green 21.8, amber 30.3 (floor 15); CVD minimum 11.2 (green, deutan;
target 8). The script reproduces round 0's `#a66993` figures (15.9, 8.7).
The amber chip's own ink is the precedent for a second shade.

### U2 Wording: one set of helpers in `api.js`, from the data (round 1)

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
```

`displayResult` is the one place FLAKING is made (`state` is `flaking`,
no live mute); `resultClass` maps it to `result-flaking`. It never
touches `entry.result`, so filters, sorts and the wire stay FAIL. The
set-aside pair replaces `mutedSuffix` and `failingWithMuted`, printing
today's bytes when nothing is flaking. Titles
read `streak` and `runs` (§1); the two `runs` rows are over the cadence:

```
flaking    Flaking: failed its last 2 runs; counts as failing at 3 in a row
           ("failed its last run" when streak is 1)
runs = N   Failing: its last 3 runs all failed (marked flaky, 3 in a row)
runs < N   Failing: every run here has failed (2 of 2); marked flaky, 3 in a row
           ("its only run here failed" for one run)
passing    Marked flaky: counts as failing once its last 3 runs have failed
live mute  Marked flaky; the mute comes first until it runs out
```

### U3 The selection bar

Unchanged (decision 13; round 0's wireframe stands): `Flaky [at…▾] Mark
as flaky · Unmark` after Mute, about 400 px wider (estimate), sharing the
note box as the reason; the owner is the person marking (decision 12).
**Set cadence** replaces Mark when every ticked row is marked.

### U4 The Review panel (round 1)

No pill: the row shows the chip; the panel gives its sentence in text.

```
Flaky  [why is this test flaky? (required)    ] [at…▾] [Mark as flaky]
Flaky  Flaking: failed its last run; counts as failing at 3 in a row
       dev1 · 12 days ago · race in teardown  [3 in a row▾] Set cadence Unmark
```

### U5 The test page (round 1)

The mark rides the detail (`flaky`, `flaky_history`). The header's latest
chip is the test's state here, so it goes through `displayResult`; the
run history keeps each run's FAIL (a run is what happened; flaking is a
judgement on the test now). The compare strip stays raw (decision 9).

```
login_retry                  [FLAKING] 2026-11-10 02:14 · 41.2 s
Marked flaky · 3 in a row    dev1 · 12 days ago · race in teardown
  Flaking: failed its last run; counts as failing at 3 in a row
  [3 in a row▾] [Set cadence] [Unmark]
FLAKINESS   unmarked: [Mark as flaky]   marked: Marked flaky, 3 in a row
Flaky mark history ▸   Action | By | When (UTC) | Cadence | Reason
```

With `state` null (no run on this stream; rows never carry it) the banner
reads "Marked flaky, 3 in a row; no run here yet", so it can be unmarked.

### U6 Home tiles and "All clear" (round 1)

```
New failures 12                               Still failing 31
were passing before · +3 muted · +4 flaking   failed before too · +2 flaking

✓ All clear. No new failures, nothing still failing, no stale annotations.
  Set aside: 3 muted · 4 flaking
```

**"All clear" names what is set aside** (§8) from `setAsideWords`: muted
is `muted_new_failures + muted_still_failing`, flaking `status.flaking`.
"Set aside: 3 muted", "Set aside: 4 flaking", or both as drawn; both zero
or an older server, no line. Each part is a link button to its tab, as
"retired" is. The counts come with the headline that gates the banner.

**Fixed and the delta (decision 3)**: no UI change. A pass after a
tolerated failure is not in `status.fixed`: neither the tile nor "▼ N
fewer failing than before" moves. A pass after a spell over the cadence
is a real fix, "worth verifying" stands, and the row reads FAIL (ghost)
→ PASS (flaky). One asymmetry remains (question 1).

### U7 The three home charts (round 1)

Round 0's design and wireframe stand: a fourth segment `▒` after the grey,
in the chart token, its key, tooltip row, column and caption words only
where `status.flaky_total > 0` (decision 5). New: `FAIL_FLAKING` is each
night as it stood then, under today's marks (decision 2), and the caption
says "flaking that night, by today's marks".

### U8 The Flaky tab, beside Muted (round 1)

```
New failures 3 · Still failing 11 · Muted 2 · Flaky 7 · Fixed 4 · …
Failing first, then flaking, then passing; newest mark first.
☐ Test             State    Mark                 Cadence     Failed
☐ dad_timeout      FAIL     races the DAD timer  2 in a row  9 of its
  lab-alpha·ipv6            dev2 · 3 days ago                last 20 runs
☐ login_retry      FLAKING  race in teardown     3 in a row  6 of its
  lab-alpha·auth            dev1 · 41 days ago               last 20 runs
☐ cache_warm       PASS     slow mirror          5 in a row  0 of its
  lab-alpha·cache           dev2 · 90 days ago               last 20 runs
```

- **Data**: `GET /api/flaky` (§1) on the first click only, never in
  `refreshAll` or `fetchSummary`; 100 a page, "Show more" appends, in the
  server's order (decision 16). No column sorts (`SortingTest`'s rule).
- **Badge**: `status.flaky_total`, "…" until the headline, never hot; the
  tab exists only above zero. A `state: null` mark is absent (§1).
- **Cells**: `failedOfRuns(failed_runs, runs)`: "0 of its last 20 runs"
  shows a stale mark. Chip and edge from `displayResult(result, state,
  muted)`, a muted pill when `muted`, no "flaky" tag (every row is
  marked). "retired" under a retired test. Assignee with Take follows; a
  flaking or passing row's assignee reads "dormant", titled "On nobody's
  Open Actions until it fails past its cadence".
- **States**: "Loading…"; empty (an unmark raced it) "No test is marked
  flaky here."; errors as other tabs. An act refetches page one and the
  counts, as Mute does.

### U9 Rows everywhere else (round 1)

```
state                    chip      row edge   tag
under cadence, no mute   FLAKING   mauve      none
over its cadence         FAIL      red        flaky   (grey pill)
marked and passing       PASS      green      flaky
live mute, any state     result    result     muted flaky
lab-alpha  auth/login  login_retry  FLAKING        2026-11-10 02:14
lab-alpha  ipv6/dad    dad_timeout  FAIL (flaky)   2026-11-10 02:15
lab-alpha  cache/warm  cache_warm   PASS (flaky)   2026-11-10 02:16
```

Each row builder gives one `displayResult` value to both `resultClass`
(the edge) and `resultChip`: a class and a word, no element, and chip and
edge cannot disagree. FLAKING's title is `flakySentence`. Covered: browse,
the script page's tests (`/api/dashboard` rows), every Home queue, a
build's own tab, Not run's "Last result"; New failures and Still failing
hold no flaking test (§1). Raw results stay in run history, the execution
breakdown, the compare strip, the comparison page and Watch's build cards:
**no comparison pill** (decision 9). Watch's environment and product cards
read `Failing 12 (+3 muted, +4 flaking)`. Browse's FAIL toggle still
matches FLAKING rows (FAIL on the wire; a flaking value was not taken).

**Open Actions.** No flaking test in any mode, assigned or not; a dormant
assignment comes back when the test goes over its cadence. So there is
no flaking marker. A marked row there is over its cadence: FAIL (flaky).
One quiet footer line, only where the scope has a mark:

```
☐ dad_timeout  atlas  FAIL (flaky)  dev2  races the DAD…  2026-11-10 02:15
42 tests · Flaking tests are not listed until they fail past their cadence.
```

**The names-only request.** `refresh()` swaps `summaryUrl()` for one
product-scoped request returning `environments`, `assignees`, `products`
(`[{product}]`, `/api/products`' shape), `assignment_streams` and
`stale_before` under today's names, so the reading code is unchanged;
plus `flaky_marks` (U12). Nothing else on the page reads the summary (the
switcher reads names and a length; expiring, users and environments fetch
their own). The "fetched once" gate moves from `state.assignees.length`
to a loaded flag: with no assignees it refetches on every refresh today.

### U10 Performance impact (round 1)

| Page | First-paint data requests | Payload |
|---|---|---|
| Home (and a build's tab) | 4 → 4; Flaky +1 on click only | integers |
| Browse, script page | 1 → 1 | +13 B a row, 3.2 KB per 250 |
| Watch | unchanged | `flaking` per env/product card |
| Test page | unchanged, detail first | the mark and history |
| Open Actions | 2 → 2, + expiring (not awaited) | summary → 5 lists |

Home's four: headline, active queue, `assigned_trend`, browse. Open
Actions sheds the queues it never read (about 19 of 36 ms, §P), so the
flaking counts reach no first paint. Render: a class and a word per
flaking row, a pill per marked row, no per-row listener; no marks, today's
DOM. The build measures requests, payload and sequencing per page.

### U11 Guards (`tests/test_frontend_calls.py`) (round 1)

- **Widen `ResultEmphasisTest`, every assertion kept**: each row builder
  passes one `displayResult(` value to `resultClass(` and `resultChip(`
  (planted: a builder on `resultClass(row.result)`); `ghostChip` never
  takes it, so FLAKING is never outlined; `.chip.result-flaking` and
  `tr.result-flaking` use the strong token. Also widened:
  `MutedBesideFailingTest`, `AssignedSplitTest`, `SortingTest`.
- **New**: `FlakyWordingTest` (planted; "FLAKING", "in a row", "Set
  aside" only in `api.js`), `FlakyMarkControlsTest`, `FlakyTabTest` (click
  only; first paint stays four), `AllClearSetAsideTest`,
  `OpenActionsCatalogueTest` (`actions.js` never names `api/summary`).
- **DOM shim**: U9's four states; no new fields, or `flaky_total: 0`,
  draw the WP-43 DOM; Still failing never draws FLAKING.

### U12 Pushback on §0 and §1 (round 1)

Round 0's eight points are settled (1 to 4 and 8 by §1, 5 by D-B8, 6 by
decision 9, 7 by decision 3). Nothing is pushed back. Two asks of §1:

1. **`flaky_marks`**, a boolean on the names-only response for U9's line
   (the "any mark" bit beside the mute epoch, scoped by product). If §B
   prices it above zero, the line goes and What's new carries the rule.
2. **`runs` means two things**: N-capped on a row's `flaky`, the window on
   a tab entry. Buildable as is; a distinct tab name would be clearer.

### Open questions (round 1)

Round 0's are answered (§8). Still open:

1. **A promotion is not counted.** A test going over its cadence lands in
   Still failing (its previous run failed), so "▲ more failing" never
   counts it, while its fix counts "▼ fewer". Count promotions in the
   delta (one integer), or accept?
2. **Home's "My actions"**: does decision 23 apply (no flaking test,
   assignments dormant)? The UI assumes so; it is one person's list.

### What's new (draft, for the drop) (round 1)

- **Mark a test as flaky** from the selection bar or beside Flakiness on
  its page, with a reason and how many failures in a row count as failing
  (2 to 10). It stays until removed, on mainline and every build.
- **Flaking is its own state.** Under its cadence a failing test reads
  FLAKING in its own colour, beside the figures ("+4 flaking"), and still
  counts against the pass rate. Past the cadence it is red again.
- **Flaking tests stay off Open Actions** until they fail past their
  cadence, and "All clear" names what is set aside.
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

## §4 Decisions

Each is written as the recommended position, with the alternative after
"Or:". The owner's verdict goes in the slot: agree, change to X, or reject.
The section named holds the reasoning. The first eleven change what a
developer sees; the rest are how it is built.

| # | Decision (recommended position) | Section | Verdict |
|---|---|---|---|
| 1 | For the cadence a failure is a `FAIL` and nothing else, the definition the existing flakiness signal uses. An unexpected pass or an expected failure among the last N runs keeps the test flaking | §B B.1 | Agreed |
| 2 | The per-day chart judges each past night **as it stood that night**, using today's mark: a spell over the cadence stays red there, as it was in the bars. Or: "marked now", every past failure of a marked test drawn as flaking, which colours as tolerated some runs the bars called failing; "that night" is 20 to 60 % dearer than it | §B D-B1, §P | Agreed |
| 3 | A marked test that passes after a **tolerated** failure is not "Newly fixed" and does not move the "fewer failing than before" line; one that passes after being **over its cadence** is, because that was a real failure and its fix is news. §P measured the extra read at 0.1 to 0.4 ms. Or: a marked test is never in Fixed at all, which is simpler and hides a real fix. Neither section recommended one; this is the main session's position | §UI U12.7, §P | Agreed |
| 4 | The Flaky tab's measure is "failed X of its last 20 runs" on the stream. Or: failures since the mark was made, which grows with the mark's age and says less about now | §B D-B2 | Agreed |
| 5 | The tab, the legend key, the tooltip row and the table column appear **only where the scope has a mark**; a product with no marks sees exactly today's page. Or: always shown, as the Muted tab is | §UI D-U4 | Agreed |
| 6 | The word "flaky" on screen always means the mark. The computed signal on the test page and Open Actions changes from "flaky — flips about 1 run in 3" to "flips about 1 run in 3" (and "flips often" in the card). Or: keep both meanings | §UI D-U6 | Agreed |
| 7 | The colour for flaking is a new token, a dusty mauve (`#a66993`), checked against every existing chart colour for normal and colour-blind vision. Or: a teal, which sits too close to the pass green | §UI U1 | Agreed |
| 8 | A row keeps its result's colour on the chip and the row edge; flaking is a pill beside it, as muted is. Or: a mauve row edge | §UI D-U5 | Changed: a flaking row has its own chip and edge, no pill (§8) |
| 9 | The comparison page and Watch's build cards are **unchanged in this arc**: they take no account of mutes today either, and the comparison's pairs query is the slowest thing on the board. Its counts include flaking tests. Or: the flaky pill on the comparison's rows, which §P allows on the returned page only | §B D-B7, §UI U12.6, §P | Agreed |
| 10 | The reason's comment is tagged with the stream the user was looking at when they marked the test, though the mark itself covers every stream. Or: no tag | §B D-B5 | Agreed |
| 11 | When a marked test is retired the mark is kept, uncounted and flagged on the tab; when an environment is dropped its marks go with it; deleting a build touches no mark | §B D-B4 | Agreed |
| 12 | The mark's owner is the person making it. The selection bar's Assign-to box is not read, because a mark is not an assignment. A deactivated user cannot mark | §UI D-U2, §B D-B6 | Agreed |
| 13 | The selection bar gains one Flaky group after Mute: a cadence select, "Mark as flaky" and "Unmark", sharing the note box. The button reads "Set cadence" when every ticked test is already marked. The bar is about 400 px wider and wraps to three rows below about 900 px (an estimate from the markup) | §UI U3, D-U3 | Agreed |
| 14 | On the test page the button sits in the Flakiness card and opens its form there; the current mark shows as a banner under the mute banner, with its history below | §UI U5, D-U7 | Agreed |
| 15 | Migration 12 creates two tables (the marks, and one row per act) and alters nothing; the parked WP-15 moves to 13. The server may stay up for the MariaDB step | §B B.2 | Agreed |
| 16 | The Flaky tab is paged by the server in an order it knows without the measure: failing first, then flaking, then passing, newest mark first. §UI's "most failures first" would need the measure for every mark, an estimated 110 to 130 ms at 1,000 marks. Or: accept that growth, off the first paint. The order is the main session's reconciliation, for §B to confirm in round 1 | §P, §UI U8 | Agreed |
| 17 | The cadence is judged when a page is read, from the latest and previous results the board already keeps, reading run history only for a test that has failed twice running under a cadence of 3 to 10. Nothing is added to the push path. Or: a failure count kept as results arrive, which saves under 2 ms and costs every estate a change to the hottest table | §B B.3 | Agreed |
| 18 | The headline reads only the marked tests that are failing, so its cost grows with failures and not with marks: about +2.5 ms at 50 marks and +3.5 ms at 1,000 on SQLite, nothing at none. MariaDB pays more: the run-history read alone was about 5 ms for the fixture's 42 tests. That read is bounded below by a stream's first run on builds only | §P | Agreed |
| 19 | Rows learn of their mark through a primary-key join on the rows statement: +0.09 ms per 250 rows on SQLite and +0.2 ms on MariaDB with no marks. This is the one place a board with no marks pays anything. Or: a separate lookup, a statement and a round trip on every cold page | §B B.9, §P | Agreed |
| 20 | The unsliced summary, which Open Actions fetches on its first paint, carries the counts so its queues agree with the headline's: nothing with no marks, +2 to 6 ms with marks on SQLite, an estimated +6 to 10 ms on MariaDB. §P recommends giving Open Actions a catalogue-only request as its own package, saving about 19 ms, more than this adds | §B D-B3, §P | Agreed, and the names-only request is built in this arc, first (§8) |
| 21 | The per-day chart's cost at 90 days with 1,000 marks (366 ms on SQLite, off the first paint) joins the trend decision already open from the assigned split: accept, a covering index, or a cap. It is not decided here | §P | Agreed: decided with the open trend decision |
| 22 | MariaDB is gated by CI's 10.3 legs running the cadence tests with at least 101 marked tests, so a full statement and a second one both execute, plus a by-hand `EXPLAIN` on a production-scale copy during the measured pass | §P | Agreed |
| 23 | A flaking test is off Open Actions' "Needs action" list and returns when it goes over its cadence. No section designed this: the list is paged in SQL and "flaking" is computed, so how they are excluded and what it costs is unmeasured. Round 1 has §B design it and §P cost it; that page's rows are a Known slow row | §7 verdict 17, §B B.8 question 2 | Decided (verdict 17); design in round 1 |

## §5 Open questions

**All six are answered in §8.** They stand here as they were put.

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
