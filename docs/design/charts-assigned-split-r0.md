# The dashboard's charts split assigned from unassigned — design review, round 0

**Status: FROZEN 2026-10-13. Round 0 reviewed in chat the same day, every decision agreed (decision 10 deferred); the master plan is §8 and the build is WP-43, registered in `docs/SESSION_HANDOVER.md`. Nothing is built yet.**
Three sections were written by the owning agents against the §0 agreed in
chat: §B by `backend-engineer`, §UI by `ui-engineer`, §P the performance
design pass by `performance-engineer`. §1 is the main session's
reconciliation of the two layers; §4 and §5 collect every decision and open
question in one place. The Claude Doc is the viewer; this file is the record.
Reading bar: twenty minutes. The agents' section labels are kept: §B is
§2, §UI is §3, §P is §3a; their open questions are cited as "§B question
N" and "§3 question N". Three terms used throughout: "Take" is the
one-click assign-to-me action on the triage queues; "Watch" is the
watchlist page; the "mute epoch" is the memo key that changes whenever a
mute is created, lifted or expires.

## §0 What we are building

On the home dashboard, all three charts distinguish failing tests that are
assigned to someone from those that are nobody's yet, as a separate colour in
the stack. The two current-state bar charts, failing by environment and most
failures by script, split each bar into three segments: failing and
unassigned in the existing red, failing and assigned to someone in a second
colour, and muted in the existing grey (WP-40). A mute also assigns, so the
assigned segment excludes muted tests and the three segments sum to the bar's
failing total. The per-day trend stack, failing runs per day, splits its
Failed column the same way, by whether the run's test is assigned now: the
owner has accepted that a day's history recolours as people take tests, and
the chart says so in a caption. Legend and tooltip name every segment. The
payload adds the assigned count per environment entry and per script entry,
and per day the assigned and muted counts, scoped exactly as the figures
already are, so a branch's own-results
tab gets the split too. Done means a developer glancing at the board sees at
once how much of the red is nobody's yet, on SQLite and MariaDB alike, with
the summary endpoint's cold cost measured against the base and reported.

Layers: backend (`testboard/storage.py`, `testboard/api.py`) and UI
(`static/app.js`, `static/charts.js`, `static/style.css`, `static/index.html`,
`static/api.js`, `tests/test_frontend_calls.py`). On the performance trigger
paths: yes (storage.py, api.py, the home page's first paint), so the build
carries a `/perf-ab`. No migration; schema stays at 11. The owner's verdicts
so far, from chat: all three charts; segment order unassigned, assigned,
muted; an arc rather than two bare briefs.

## §1 The contract, reconciled

The two sections were written in parallel against a candidate contract.
They agree on the field names and on two new trend keys. They differed on
where the trend's split is served; §P settles it. The contract the build
runs from:

- `by_environment[i].assigned` and `top_failing_scripts[i].assigned`: of the
  entry's `failed` / `failing` (FAIL, unretired, no live mute), those with a
  current assignee. Unassigned is the difference; `muted` is unchanged.
  Always present, integers.
- `trend.nights[i].FAIL_ASSIGNED` and `FAIL_MUTED`: of the day's `FAIL`
  runs, those whose test has an assignee now and no live mute now, and those
  under a live mute now. Disjoint, so the trend's three segments carry the
  bars' definitions (the UI's pushback, accepted; §B's D-B3 says the same).
- The trend's two keys are served by `parts=assigned_trend` only (§B D-B2),
  because they are the one figure that must read `runs`. Neither the
  headline nor the full, unsliced payload carries them: the measured pass
  found the Open Actions page fetches the full payload on its first paint
  (§7, decided during the build).
- **The UI sends that part in the same parallel batch as the headline**, the
  home page's three requests becoming four, and paints the trend card when
  both have landed; nothing else on the page waits (§P's condition on D-B2).
  §UI's U7 was written before this and says "no request is added"; round 1
  revises U7 and covers the branch's own-results tab, which fetches its own
  headline and must fetch its own split or go without.
- An older payload without the new fields draws exactly today's charts
  (§UI U7), never everything red.
- The page merges the part into the headline's `trend.nights` by date; the
  part is requested with the same `days`, `environment`, `product` and
  `stream` as the headline, and a night the part does not carry draws
  today's two series.
- If the part fails, times out or an older server answers 400, the trend
  card draws today's two series from the headline alone; it never blocks
  the card or the page. While the part is in flight the card keeps its
  previous render at reduced opacity, as every refresh does.
- The summary refresh after a Take fetches the headline and the part
  together, so red turns blue in all three charts without a reload.

## §2 Backend (§B, round 0, backend-engineer, base `c9f1533`)

Against §0's candidate contract. **No migration**, nothing at write time, nothing on the push path.

### B.1 Definitions, reconciled

- **`by_environment[i].failed` (unchanged)**: latest result FAIL on the
  request's stream, unretired, no live mute on that stream (WP-40 §C); not
  recency-gated. **`assigned` (new)**: the subset with a current assignee
  (`current_assignments.assignee IS NOT NULL`, triple-scoped). Unassigned =
  `failed - assigned`; segments sum to today's `failed + muted`.
- **`top_failing_scripts[i].assigned` (new)**: same rule per (environment, script) ⊆ `failing`.
- **Watch's `unassigned_failing_by_environment` (untouched)** counts FAIL,
  unretired, unassigned, *muted or not*: it equals `failed - assigned` only by
  the write-path invariant "live mute implies assigned" (`mute_tests` assigns,
  unassign calls `_drop_mutes_on_unassign`). Ours is exact without it.
- **Different populations**: `status.assigned_open` (FAIL or UP, muted included) and
  `queue_totals.assigned` (overlaps Muted); neither is `Σ by_environment.assigned`.
- **`trend.nights[i].FAIL_ASSIGNED` (new)**: of the day's `FAIL` runs (the
  stream's `activity_hours`, retired tests included), those whose triple has an
  assignee **now** and no live mute on the stream **now**; **`FAIL_MUTED`**
  (D-B3) those under a live mute now; unassigned is the rest. No retirement
  filter, because `FAIL` has none. **In plain words**: a past day's Failed
  column is split by who owns each test *today*; assigning a test recolours
  every red run it had in the window, unassigning or an expiry recolours them
  back, and a day's `FAIL` never changes.
- **D-B5. `UNEXPECTED_PASS` stays whole**: the split answers "how much of the
  red is nobody's", an unexpected pass is not red, and the bars split failures
  only. Verdict: agreed (§7).

### B.2 Data model (illustrative numbers in the seed's shape)

```json
"by_environment": [{"environment": "atlas-lab-alpha", "total_tests": 8000,
  "failed": 161, "new_failures": 40, "unexpected_passes": 4, "not_run": 12,
  "muted": 10, "assigned": 55}],
"top_failing_scripts": [{"environment": "atlas-lab-alpha",
  "script": "net/ipv6_suite", "failing": 9, "muted": 2, "assigned": 4}],
"trend": {"days": 14, "from": "2026-09-29", "to": "2026-10-12", "nights": [
  {"date": "2026-10-12", "PASS": 11650, "FAIL": 171, "FAILED_AS_EXPECTED": 120,
   "UNEXPECTED_PASS": 4, "total": 11945, "FAIL_ASSIGNED": 55, "FAIL_MUTED": 10}]}
```

That bar: 106 unassigned, 55 assigned, 10 muted. New keys are integers, always present (D-B2 places the trend's).

### B.3 Queries (qmark-canonical, SQLite and MariaDB 10.3 alike)

**Q1 `Storage.assigned_cells(now, environment, environments, stream_id)`, one
new statement for both bar charts (FAIL only: decision 10 is deferred, §7):**

```sql
SELECT lr.environment, lr.script, lr.result,
       CASE WHEN (tm.expires_at IS NOT NULL AND tm.expires_at > ?) THEN 1 ELSE 0 END AS muted, COUNT(*)
FROM current_assignments AS ca
JOIN latest_runs AS lr ON lr.stream_id = ? AND lr.environment = ca.environment
     AND lr.script = ca.script AND lr.test_name = ca.test_name
LEFT JOIN test_retirements AS tr ON <triple>
LEFT JOIN test_mutes AS tm ON tm.stream_id = lr.stream_id AND <triple>  -- only while mute_epoch() is set
WHERE ca.assignee IS NOT NULL AND lr.result = ? AND tr.retired_at IS NULL
GROUP BY lr.environment, lr.script, lr.result, muted
```

Whole stream, scope filtered in Python (as `muted_cells`), so every scope
shares one memo. `by_environment.assigned` = Σ (FAIL, muted 0) cells per
environment, via `summarize_rollup(assigned=...)` beside `muted=`; the script
figure is the same Σ per (environment, script), merged after the scripts memo.
No live mute, no `tm` join. SQLite plan on the seed: covering
`idx_latest_runs_result (stream_id=?, result=?)` plus PK seeks; bounded by
assignments or the stream's FAIL+UP rows. Same shape as `queue_counts`'
assignment statement, already on MariaDB.

**Q2 `Storage.daily_assigned_failures(since, now, environment, environments, stream_id)`,
the trend split.** It must read `runs`: `activity_hours` has no test identity.

```sql
SELECT SUBSTR(r.start_time, 1, 10) AS day,
       CASE WHEN (tm.expires_at IS NOT NULL AND tm.expires_at > ?) THEN 1 ELSE 0 END AS muted, COUNT(*)
FROM current_assignments AS ca
CROSS JOIN runs AS r ON r.environment = ca.environment AND r.script = ca.script
     AND r.test_name = ca.test_name AND r.start_time >= ?
LEFT JOIN test_mutes AS tm ON tm.stream_id = ? AND <triple on ca>   -- only while an epoch is set
WHERE ca.assignee IS NOT NULL AND r.stream_id = ? AND r.result = ? [AND <scope on ca.environment>]
GROUP BY day, muted
```

**The `CROSS JOIN` is load-bearing.** As a plain `JOIN`, SQLite scanned the
whole window of `runs` by `idx_runs_start_time_result`, the shape ARCHITECTURE
forbids (29 ms on the seed). `CROSS JOIN` forces `SCAN ca`, then the runs UNIQUE
index (triple, `start_time>?`): bounded by assignments × window × streams; same
rows (asserted). **Dialect concern**: MariaDB reads `CROSS JOIN … ON` as an
inner join and may reorder it; it should still drive from 65 rows, unverified
(§B question 4). `SUBSTR`, `CASE`, `GROUP BY` alias, ISO text vs bound `now`: both already.

### B.4 Memos and caches

- **Q1**: summary memo per environment, key `("assigned_cells", stream, environment,
  mute_epoch)`, tagged `(stream, environment)`: the `muted_cells` pattern, cutoff-independent.
  Right the moment someone assigns: `set_assignee`, `bulk_set_assignee*`, `mute_tests`,
  `unmute_tests` already drop every summary memo. A push drops only its environment's entry.
- **`top_failing_scripts`' memo is untouched** (key, statement): its hit rate is unchanged,
  as assignment writes already drop it and nothing new does.
- **Q2 is kept out of `_trend_cache`**, which assignment writes do not drop
  (a new assignment would show up to 60 s late). It goes in the summary memo,
  key `("assigned_trend", stream, since, environment, envs_key, mute_epoch)`,
  tagged with the stream (and environment when scoped): dropped by assignments,
  mutes, pushes and the TTL. Writes that change `FAIL` drop both caches, so
  `FAIL_ASSIGNED + FAIL_MUTED <= FAIL` holds. Rejected: an epoch in
  `_trend_cache`'s key (a new invalidation path); no memo (Q2 every warm load).

### B.5 Cost

**Measured**, per statement, in-process, 48 alternated calls, median, on a scratch copy of the
production-scale seed (perf-seed/1, SQLite, 7 nights); 65 assignments in the largest
environment, 10 muted. Statement timings: the build's gate is still the endpoint `/perf-ab`.

| Statement | ms, 65 assigned | ms, 1000 assigned |
|---|---|---|
| Q1 `assigned_cells` (new) | 0.25 | 0.38 |
| `assigned_open_count` (existing, unmemoized; D-B4 retires it) | 0.07 | 0.11 |
| rejected: `ca` column on the largest environment's pass | 8.59 → 11.54 | 8.72 → 12.83 |
| rejected: `ca` column on `top_failing_scripts` (no env totals) | 0.21 → 0.26 | 0.21 → 0.28 |
| Q2 trend split, `CROSS JOIN` (new) | 5.00 | 18.42 |
| Q2 as a plain `JOIN` (runs-window plan) | 29.19 | 29.56 |
| `daily_result_counts` (existing) | 0.21 | 0.21 |

- **Q1 on first paint**: about +0.2 ms cold against the 90 ms headline; with
  D-B4, one statement fewer cold and warm. Scales with assignments, not the estate.
- **Q2**: 906 runs (mainline and builds, each a table fetch) for 65 triples.
  **Estimate**: production's 14-day window doubles that, ~10 ms at 65 and ~35 ms
  at 1000 assignments; `days=90` scales with it. +6–11 % on the cold headline,
  hence D-B2. MariaDB not timed; correctness and plan guarded (B.7).

### B.6 Decisions, as positions for the owner

- **D-B1. One cells read (Q1) serves both bar charts**; no join on the
  per-environment pass or the scripts query. Verdict: agreed (§7).
- **D-B2. The trend split is `parts=assigned_trend`**, `{"trend": {"days",
  "from", "to", "nights": [{"date", "FAIL_ASSIGNED", "FAIL_MUTED"}]}}`, same
  scope parameters; the full payload does NOT carry the keys (amended at
  item 1p, §7: the Open Actions page fetches it on first paint). First
  paint pays nothing; in the headline, about 5–10 ms
  cold (estimate). Verdict: agreed, fetched in parallel (§7).
- **D-B3. Add `FAIL_MUTED`**: free (the `tm` join is needed anyway to keep muted
  runs out of `FAIL_ASSIGNED`), and the trend gets the bars' segments. Verdict: agreed (§7).
- **D-B4. Derive `status.assigned_open` and `queue_counts`' `assigned`/`mine`
  from Q1's cells** (FAIL+UP, muted included; `mine` by assignee). The headline
  counts that population twice today; net −1 statement, values pinned.
  Verdict: deferred to its own package (§7); Q1 above is already narrowed.

### B.7 Tests the build writes

- `test_storage.py::AssignedCellsTest`: each B.1 rule; `failed - assigned` equals
  Watch's figure on mainline; `assigned <= failed`, `<= failing`. (The sums
  against the assigned-open count belonged to D-B4, deferred.)
- `AssignedTrendTest`: `FAIL_ASSIGNED + FAIL_MUTED <= FAIL` daily; an assignment recolours the
  next call; an expiry does without a write; retired-assigned counts.
- `TargetedInvalidationTest` extended: a push drops only its environment's cells.
- Cache guards (`TestQueueCounts` style): headline cold statements pinned (base +1), warm adds none; `assigned_trend` one cold, zero warm; no live mute, no `test_mutes`.
  Plan guard: Q2 opens `SCAN ca`, never `idx_runs_start_time_result`; MariaDB: `ca` first in `EXPLAIN`.
- `test_api.py`: keys on every entry, `parts=assigned_trend` shape and 400, `stream=` scoping;
  `test_analytics.py`: `summarize_rollup(assigned=...)`.

### B.8 Open questions

1. D-B2: a second request for the trend split, or an estimated 5–10 ms on the
   headline's cold first paint?
2. D-B4 touches two figures nobody asked about, to make the change net cheaper:
   this arc, or its own package?
3. Q2 grows with assignments × window × streams (18 ms on the seed at 1000).
   Accept, or skip the split above N assignments?
4. Q2's join order on MariaDB: the 10.3 EXPLAIN guard, or a first dialect hook
   (`STRAIGHT_JOIN`) in `mariadb.py`? I recommend the guard; the hook only if it fails.
5. No split on the product switcher's failing counts: confirm out of scope.

### B.9 Pushback on §0 and the contract

- `FAIL_ASSIGNED` in the headline's `trend.nights`: D-B2; the one figure that reads `runs`.
- "Own-results tab for free": yes, but an assignment is the triple's and a mute the stream's,
  so a test assigned from mainline is assigned on every build's tab (STREAMS_PLAN §0.4).

## §3 UI (§UI): the three charts, split three ways (ui-engineer, round 0)

Written against §0 and the candidate contract at `c9f1533`. The glance asks
one question of each chart, without a hover: *how much of the red is nobody's
yet?* The red stays the alarm, where the eye starts (left of a bar, bottom of
a column); what someone has taken steps back into a calmer colour beside it;
muted stays WP-40's grey. Titles, end labels, sort and click targets do not
move. The wireframes are illustrative; the build is checked by DOM-shim guards
and a walkthrough against a play server. Key: `█` unassigned (red), `▓` assigned
(new colour), `░` muted (grey), `·` empty track.

### U1 The colour for "assigned"

**Recommended: a new token `--c-assigned: #3b6cc0`** (mid blue) with
`.seg-assigned`, `.swatch-assigned`, `.tt-key.swatch-assigned`,
`.bar-assigned`. Run through the dataviz palette validator (OKLab ΔE×100,
light surface) against every existing token: the worst pair is the
unexpected-pass purple, **18.1 normal, 8.3 deutan**; red, the muted grey
(`--muted` at .55 ≈ `#a6a6a2`), amber and green are all further. Every pair
clears the CVD target (8) and the normal floor (15).

**Rejected: a lighter red** ("still red, but owned"). `#e57373` is 14.6 from
the grey for normal vision and 6.4 for deutans; `#ef9a9a` 1.4 for protans: a
tint light enough to part from `--c-fail` lands on the grey's lightness.
Oranges hit the red (`#e64a19` 9.2) or read as amber. Blue reads as "in
hand" and is no result colour. Neighbour: `--accent-link #1758a8` (ΔE 7.4),
the focus ring and primary button, never a data mark.

Without colour: fixed order (unassigned first), the 2px surface gap between
segments, a legend (U4), a tooltip row per segment (U5), a table column each
(U2), and an `aria-label` with the counts in words on every row and band.

**Verdict:** agreed (§7)

### U2 "Failing runs per day" (stacked columns + data table)

The Failed part of each column becomes three series, bottom first, with
Unexpected pass on top: `FAIL_UNASSIGNED` (derived), `FAIL_ASSIGNED`,
`FAIL_MUTED`, `UNEXPECTED_PASS`. `stackedColumnChart` needs **no change**;
series keys are data. `renderCharts` adds, in one pass over ≤ 45 nights,
`FAIL_UNASSIGNED = max(0, FAIL - FAIL_ASSIGNED - FAIL_MUTED)`.

```
■ Unassigned  ■ Assigned  ■ Muted  ■ Unexpected pass
   mixed   all-assigned  all-muted  all-zero
    ▒▒         ▒▒           ▒▒                 ▒ = unexpected pass
    ░░         ▓▓           ░░
    ▓▓         ▓▓           ░░
    ██         ▓▓           ░░     (nothing drawn; band still hoverable)
```

Tooltip, built from the series as today: `Oct 9` / `4 Unassigned` /
`3 Assigned` / `1 Muted` / `2 Unexpected pass` / `118 runs`; the band's aria
says the same. The semantics, in a new caption under the plot: *"Assigned
and muted are as they stand now: a day's columns recolour as tests are
taken."*

Data table: `Day | Failed | Unassigned | Assigned | Muted | Unexpected pass |
Runs`. Failed stays (the sum people know); the three after it are its parts.

Found in passing: home passes no `options`, so the SVG's aria says "per
night" of a per-day chart; the build passes `{unit: "day"}`.

**Verdict:** agreed (§7; `FAIL_MUTED` is decision 9)

### U3 "Failing tests by environment" and "Most-failing scripts" (bars)

Unassigned, assigned, muted, left to right; the scale is the longest total,
as WP-40's is. **End label unchanged**: `failed`/`failing` (unassigned +
assigned), muted outside it, as "N (+M muted)" is everywhere.

```
Failing tests by environment         ■ Unassigned ■ Assigned ■ Muted
atlas-lab-alpha  ████████▓▓▓▓▓▓░░░░···  14   mixed: 8 + 6, +4 muted
atlas-lab-beta   ▓▓▓▓▓▓▓▓░░··········    8   all assigned, +2 muted
atlas-lab-gamma  ████················    4   all unassigned
atlas-rel-2.4    ░░░░░···············    0   all muted: grey only
(no rows)        "No failing tests. 🎉"      all zero: empty state as today
Most-failing scripts: the same rows, with the environment as sublabel.
```

Row `aria-label`: "atlas-lab-alpha: 14 failing, 8 unassigned, 6 assigned, 4
muted". No red means nothing unowned. One click target per row, as today.

**Verdict:** agreed (§7)

### U4 Legend for the bars

**Recommended: one legend per bar card, above the bars**, the trend's
`.chart-legend` markup, via `barRows(…, {legend: [{label, swatchClass}]})`.
Not tooltip-only: the glance must read without a hover, and keyboard and
touch users rarely hover. Not one shared row for the grid: the trend's legend
has four keys, so a shared row would repeat or contradict it. Drawn whenever
the payload carries the split, zeros included: no height change on a Take.

**Verdict:** agreed (§7)

### U5 Wording: one helper, never assembly

`failingWithMuted(count, muted)` → "12 (+3 muted)" is the bar tooltips' one
row today. One new helper in `api.js` feeds both bar charts' tooltip rows
and aria-label:

```
failingSplit(failing, assigned, muted) -> {unassigned, assigned, muted, words}
  words: "8 unassigned, 6 assigned, 4 muted"  (zero parts omitted;
         "none failing" when all are 0)
```

Tooltip rows: `■ 8 unassigned` / `■ 6 assigned` / `■ 4 muted`, then today's
`new failures` and `tests`. A zero part keeps its tooltip row: "0 unassigned"
is the good news. `failingWithMuted` stays for tiles, Watch cards and every
place with no assigned count. Words are the board's own (the "Unassigned
only" chip, Watch's "N unassigned failing"): not "owned", not "triaged".

### U6 `barRows` grows `segments`

`item.segments` (optional, `[{value, className}]`, left to right);
`item.value` stays the end label and aria number; `item.extra` stays and is
normalised to `[{value, fillClass}, {extra, "bar-muted"}]` on the loop's
first line, so one path draws. Scale: the longest segment sum. Each non-zero
segment gets the 1.5% floor; all but the last non-zero get `bar-fill-open`;
`bar-track-split` only with two or more drawn. **An item with neither key
draws exactly today's DOM** (one `bar-fill` of `fillClass`, 0% included).
Zero segments after the first are skipped (today's all-muted row draws a
0-width red fill and a stray 2px gap). `treemapBoxes` ignores `segments`.
The suite page uses only `stackedColumnChart`, with its own series and
`unit`: unaffected, no edit.

### U7 States

- **Loading:** as written in round 0 this said no request is added; §1
  supersedes it. The bar charts render from `parts=headline` as now with
  the two `assigned` fields riding it; the trend card paints when the
  headline and the parallel `parts=assigned_trend` have both landed.
  Nothing new is awaited before the tiles and bars paint.
- **Empty:** as today (`env-chart-empty`, …); the legend stays.
- **Older server, newer page:** a missing `assigned` must not draw all as
  unassigned red (claiming nobody owns anything). `typeof entry.assigned !==
  "number"` → today's value + extra, today's tooltip, no legend; a night
  without `FAIL_ASSIGNED` → today's two series and four-column table.
- **Newer server, cached page:** the old JS ignores the new integers. A branch's own-results tab requests the part with its own stream id
  beside its own headline. After "Take", `refreshSummary()` fetches both
  and re-renders: red turns blue without a reload.

### U8 Performance impact

One request more per load, `parts=assigned_trend`, sent in the same
parallel batch as the headline (§1); nothing new before the tiles and
bars paint. Payload: one integer per environment and script row in the
headline; two per day in the new part. DOM at production
scale (5 environments, 7 scripts, ≤ 45 days): ≤ 12 divs, ≤ 90 SVG shapes, 6
spans, one pass over ≤ 45 nights. The summary's cold cost is the backend's.

### U9 Guards (`tests/test_frontend_calls.py`)

- **Widen** `MutedBesideFailingTest.test_the_charts_draw_a_grey_extension`:
  it pins the spelling `extra: entry.muted || 0` twice; it widens to "both
  bar callers draw a muted segment". That both draw muted stays asserted.
- **New `AssignedSplitTest`:** both bar callers use `failingSplit`; order
  unassigned, assigned, muted in both and in the trend's series; the "as
  they stand now" caption ships in `index.html`; the table's three headers
  in order; no "unassigned" text assembled in `app.js` outside the helper;
  home's trend call passes `unit: "day"`; each new CSS class has a user.
- **DOM-shim:** `barRows` with neither key and with `extra` yields the class
  lists and widths of `c9f1533`; `renderCharts` on a summary without the new
  fields draws two series, no `.bar-assigned`, no bar legend.

### Open questions

1. **Add `FAIL_MUTED` to the trend: recommended.** Without it the trend has
   two failure segments to the bars' three, and `FAIL_ASSIGNED` must include
   muted runs (a mute assigns). Contract ask: `FAIL_ASSIGNED` *excludes* runs
   whose test is muted now and `FAIL_MUTED` counts them: disjoint keys, the
   bars' definition carried over.
2. **Muted "now" in the trend**, like assigned: a day also recolours when a
   test is muted; the same caption covers it. Confirm.
3. **End label:** keep "failing", or show the unassigned count? Recommended:
   keep; the red length answers it and the tiles say "failing".
4. **Per-segment click** (red → the "Unassigned only" list): left out; three
   targets in a 12px bar are not click targets. Wanted later, separately?
5. **Sort:** by failing as today, or by unassigned? Recommended: as today;
   resorting on every Take moves rows under the cursor.

## §3a Performance design pass (§P, round 0, performance-engineer, base `c9f1533`)

Not measured: statement figures are backend B.5's, perf-seed/1 SQLite, cold, in-process; the rest is labelled estimate. No MariaDB here.

- **Cost.** Q1: +1 statement on `parts=headline` (D-B4: −1 net), 0.25 ms at 65 assignments, 0.38 at 1000. It grows with assignments and the stream's FAIL+UP rows, not with history; that is far below the headline's −0.4…+3 ms A/A band and the ±10 ms the brief quotes. Q2 is the only new read of `runs`. B.5 measured 5 ms for 65 triples and 18 ms at 1000 over 7 nights. `UNIQUE(environment, script, test_name, start_time)` has no `stream_id` and no `result`, so every build's run of an assigned triple is a table fetch and then filtered out. It grows with assignments × window × streams. Estimates: ~10 ms over 14 days, ~35 ms at 1000 assignments, about 6× that at `days=90`.
- **Where it lands.** Q1: cold first paint (headline), cannot move it beyond noise. Q2 in the headline: +6–11 % on the 90 ms Known slow row (estimate), which is measurably outside the headline's own band, and grows with history window and release streams: that shape would be an `ESCALATE`.
- **Memos.** Q1 keyed per environment on `mute_epoch` (the `muted_cells` pattern): set_assignee/bulk_*/mute/unmute already drop every summary memo (storage.py 8511, 8746, 8855), so an assignment invalidates nothing it did not before; `top_failing_scripts`' and `_trend_cache`'s hit rates are unchanged. Q2 in the summary memo, not `_trend_cache`: right; it costs one cold Q2 after each push or Take, nothing warm.
- **D-B2 (owner's question 1): the separate `parts=assigned_trend` is cheaper for the glance, on one condition.** The UI must send it in the same parallel batch as the headline (app.js:20's three requests become four), never chained after the headline, and paint the trend card when both have landed. Then it finishes inside the headline's 90 ms (estimate), and the bars' and tiles' first paint pays nothing. The price is one memoized request, about 10 ms of a worker per cold load. Chained after the headline, it would add a full round trip to the trend card: then the headline is cheaper. §3 assumed no new request and must take this on in round 1, including the branch tab's headline fetch (app.js:1867), or that tab goes without the trend split.
- **D-B4 (question 2): perf-neutral within noise** (it saves ~0.1–0.3 ms). On production MariaDB each statement is a network round trip, so −1 against +1 is the tie-break, and it favours D-B4. It is a scope call, not a cost one. If it is deferred, Q1 should drop `UNEXPECTED_PASS` from its `IN` and `assignee` from its `GROUP BY`, which only D-B4 needs.
- **§B question 3:** with D-B2, accept the growth with no N cutoff: it stays off the first paint. The measured pass must time `days=90` and 1000 assignments with a widened seed, because perf-seed/1 holds 7 nights.
- **CROSS JOIN on MariaDB 10.3 (question 4).** MariaDB reads `CROSS JOIN … ON` as an inner join and may reorder it. With 65 `ca` rows against a 14-day `idx_runs_start_time_result` range, the cost model should start from `ca` (estimate). An EXPLAIN guard is the wrong gate. CI's `mariadb:10.3` leg runs on few-row tables, where starting from `runs` can be the honest cheapest plan, so the guard is flaky one way and proves nothing about production the other way. Every MariaDB EXPLAIN assertion in tests/test_mariadb_backend.py:42–112 is skipped for exactly this reason. Gate: keep the SQLite plan guard, and take MariaDB's EXPLAIN by hand on a production-scale MariaDB copy in `/perf-ab` (the WP-23 practice). Add the `STRAIGHT_JOIN` hook only if that EXPLAIN starts from `runs`. With D-B2 a wrong plan costs ~29 ms or more, off the first paint, not on it.
- **Cheaper shape.** None for Q2 within the semantics. "Assigned now" recolours history, so nothing can be kept at write time, and a covering `runs` index would add cost to the push path and the schema's widest index. Q1 is already the cheapest form: B.5 measured the rejected `ca` column on the per-environment pass at +3 ms.
- **Verdict:** proceed with D-B1, D-B2 (fetched in parallel), D-B3. D-B4 at the owner's scope call. Q2 inside the headline would be an `ESCALATE`.

## §4 Decisions

Each is written as the recommended position. The owner's verdict goes in
the slot: agree, change to X, or reject. The section named holds the
reasoning.

| # | Decision (recommended position) | Section | Verdict (§7) |
|---|---|---|---|
| 1 | The colour for "assigned" is a new token, a mid blue, validated against every existing chart colour for normal and colour-blind vision; a lighter red was rejected because it lands on the muted grey's lightness | §UI U1 | Agreed |
| 2 | The trend chart stacks four series, bottom first: unassigned (derived in the page), assigned, muted, unexpected pass; a caption says the split is "as they stand now"; the data table gains Unassigned, Assigned, Muted columns after Failed | §UI U2 | Agreed |
| 3 | The two bar charts split left to right in the same order; end label, sort and click target stay as today | §UI U3 | Agreed |
| 4 | Each bar card gets its own legend, in the trend's legend markup, drawn whenever the payload carries the split so a card's height never changes on a Take | §UI U4 | Agreed |
| 5 | One new wording helper builds the three-way tooltip rows and the row's screen-reader label; the existing "N (+M muted)" helper stays where it is used | §UI U5 | Agreed |
| 6 | `barRows` gains an optional segments list; today's `extra` is normalised into it; a caller passing neither draws exactly today's DOM | §UI U6 | Agreed |
| 7 | One new statement, driven from the assignments table into the latest runs, feeds both bar charts; no join is added to the per-environment pass (measured +3 ms) or the scripts query | §B D-B1 | Agreed |
| 8 | The trend's split is its own `parts=assigned_trend`, fetched by the page in parallel with the headline and drawn when both land; inside the headline it would cost an estimated 5 to 10 ms on the cold first paint and grow with the window and the release streams | §B D-B2, §P | Agreed |
| 9 | `FAIL_MUTED` is added; `FAIL_ASSIGNED` excludes muted runs; the two are disjoint | §B D-B3, §UI Q1 | Agreed |
| 10 | **Defer** deriving `status.assigned_open` and the queue counts from the new statement to its own package. §B proposes it (net one statement fewer); §P calls it cost-neutral and a scope call. The main session recommends keeping this arc to the charts, so the new statement drops the unexpected-pass result and the per-assignee grouping that only that derivation needs | §B D-B4, §P | Deferred |
| 11 | Unexpected pass stays one whole series: it is not red | §B D-B5 | Agreed |
| 12 | The MariaDB join-order risk is gated by the SQLite plan guard plus a by-hand EXPLAIN on a production-scale MariaDB copy during `/perf-ab`; a `STRAIGHT_JOIN` dialect hook only if that plan starts from the runs table. §B proposed an EXPLAIN guard on the CI leg; §P shows that leg's few-row tables make it the wrong gate | §P, §B B.8 Q4 | Agreed |
| 13 | The trend statement's growth with assignments × window × streams is accepted with no cutoff, because it is off the first paint; the measured pass times 90 days and 1,000 assignments on a widened seed | §P, §B B.8 Q3 | Agreed |

## §5 Open questions

1. The trend's muted segment is also "as it stands now": a mute today
   recolours the test's past red runs grey, and an expiry recolours them
   back. The same caption covers it. Confirm. (§UI Q2)
2. The bars' end label stays "failing", the sum of unassigned and assigned,
   with muted outside it as everywhere else. The alternative is to print the
   unassigned count. Recommended: keep. (§UI Q3)
3. Clicking the red part of a bar to open the "Unassigned only" list is left
   out: three click targets in a 12 px bar are not targets. Wanted later as
   its own package? (§UI Q4)
4. Sort stays by failing, as today; sorting by unassigned would move rows
   under the cursor on every Take. Recommended: keep. (§UI Q5)
5. The product switcher's failing counts are not split. Confirm out of
   scope. (§B B.8 Q5)
6. An assignment belongs to the test and a mute to the stream, so a test
   assigned from mainline shows as assigned on every build's own-results
   tab. That is the existing rule (`docs/STREAMS_PLAN.md` §0.4) seen in a
   new place. Acceptable? (§B B.9)

## §6 Carried into the build, not decisions

- The home page passes no unit to the trend chart, so its screen-reader
  label says "per night" of a per-day chart; the build passes the unit.
  (§UI U2)
- The stacked chart draws its legend even for one series while its comment
  says two or more; harmless, every caller has two. (§UI, found in passing)
- The measured pass needs a seed with more than seven nights to time the
  90-day window (§P; `docs/SESSION_HANDOVER.md`, "Needs a person", item 7).
- The MariaDB EXPLAIN is taken by hand on a production-scale copy during
  `/perf-ab`, as WP-23 did, and the number goes in the log. (§P)

## §7 Round 0 review (2026-10-13, in chat, captured verbatim)

The owner was taken through every decision and question in chat, each
offered as the recommended position with its alternatives. The answers,
word for word as recorded:

| # | Verdict | Classified as |
|---|---|---|
| 1 | Agree (Recommended) | decision |
| 2 | Agree (Recommended) | decision |
| 3 | Agree (Recommended) | decision |
| 4 | Agree (Recommended) | decision |
| 5 | Agree (Recommended) | decision |
| 6 | Agree (Recommended) | decision |
| 7 | Agree (Recommended) | decision |
| 8 | Agree (Recommended) | decision |
| 9 | Agree (Recommended) | decision |
| 10 | Defer (Recommended) | decision: scope |
| 11 | Agree (Recommended) | decision |
| 12 | Agree (Recommended) | decision |
| 13 | Agree (Recommended) | decision |

| Question | Answer | Classified as |
|---|---|---|
| 1 muted "as it stands now" in the trend | Confirm (Recommended) | decision |
| 2 bars' end label | Keep "failing" (Recommended) | decision |
| 3 clicking the red part of a bar | Leave out, no follow-up (Recommended) | decision |
| 4 sort | Keep today's sort (Recommended) | decision |
| 5 product switcher | Confirm out of scope (Recommended) | decision |
| 6 a mainline assignment shows on every build's tab | Acceptable (Recommended) | decision |

Earlier in the same chat, before round 0 was written: all three charts;
segment order unassigned, assigned, muted; an arc rather than two bare
briefs.

### Dispositions

Every verdict is the position as written, so no section is rewritten. Two
consequences the doc already names become instructions to the build:

- **Decision 10 deferred:** the new statement (§2 Q1) selects the FAIL
  result only and does not group by assignee. `status.assigned_open` and
  the queue counts are untouched. The derivation is noted for the handover
  as a candidate package, not scheduled.
- **Decision 8 agreed:** §3 U7's Loading bullet is superseded by §1: the
  page sends `parts=assigned_trend` in the same parallel batch as the
  headline and paints the trend card when both have landed; the branch's
  own-results tab fetches its own split beside its own headline.

No pushback on the owner's behalf was needed: nothing agreed conflicts
with a constraint.

### Decided during the build, by the main session (2026-10-13, item 1p)

**The full, unsliced `/api/summary` payload does not carry `FAIL_ASSIGNED`
and `FAIL_MUTED`; `parts=assigned_trend` is their only carrier.** §1 and
§2 D-B2 said the full payload would carry them too. The measured pass found
that the Open Actions page fetches the unsliced payload on its first paint,
so the trend statement landed on a first paint after all: +18.9 ms
[+14.4, +20.7] and +13.6 ms [+6.3, +17.7] on two cold, alternated runs of
the production-scale SQLite seed with 65 assignments, growing with
assignments × window × streams. Decisions 8 and 13 both rest on the split
being off every first paint, so the cheaper shape (the pre-WP-43 payload
shape for every unsliced caller; the part as the only carrier, which has no
other consumer) was taken as the design without parking the run. **The
owner may reverse it**: putting the keys back is one branch in the handler
and one test, and would cost the Open Actions page the number above.

Also from the measured pass: the MariaDB plan for the trend statement
starts from `current_assignments` on the local server (12.3, indicative
only), so item 1h does not exist; `parts=assigned_trend` costs two cold
statements (the mute-epoch read plus the trend statement), not one, and
its guard pins two.

## §8 Master plan (sizing pass; the doc is frozen from here)

One package, **WP-43**, one branch `wp-43-charts-assigned-split`, three
items in order on the small path. Each item is one brief; the layer, the
trigger status and the gate are stated so the performance pass is planned
in rather than discovered.

| # | Item | Layer | Trigger paths | Gated on |
|---|---|---|---|---|
| 0 | **Widen the performance seed** so a 90-day window and 1,000 assignments can be timed: at least 90 nights of `runs` for the largest environment and an assignments fixture at both sizes, as a seed variant beside `perf-seed/1`, with `tools/dev/perf/README.md` updated | performance-engineer | n/a (tooling) | nothing; may run in parallel with item 1 |
| 1 | **Backend:** `assigned_cells` (Q1, FAIL only, no assignee grouping) memoized per environment on the mute epoch; `by_environment[i].assigned` and `top_failing_scripts[i].assigned` from it via `summarize_rollup(assigned=...)`; `daily_assigned_failures` (Q2) with the `CROSS JOIN`, served as `parts=assigned_trend` and inside the full payload's `trend.nights` as `FAIL_ASSIGNED` and `FAIL_MUTED`, memoized in the summary memo, never the trend cache; the tests in §2 less the deferred D-B4 ones (no sum against the assigned-open count; the headline's cold statement count pinned at base +1), including the SQLite plan guard | backend-engineer | **yes**: storage.py, api.py, first paint | nothing |
| 1p | **Measured pass** (`/perf-ab`) of item 1 against the base on the production-scale seed: the headline part, the new part at 14 and 90 days and at 65 and 1,000 assignments, the Known slow table; plus the MariaDB EXPLAIN of Q2 taken by hand on a production-scale MariaDB copy, recorded in the log. The copy is the local MariaDB of the verification tooling loaded from the widened seed by the migration tool; it is a newer server than production's 10.3, so the plan is indicative and CI's 10.3 legs stay the authority for correctness | performance-engineer | the gate for item 1 | items 0 and 1 |
| 1h | **Only if 1p's EXPLAIN starts from `runs`:** a `STRAIGHT_JOIN` dialect hook in `mariadb.py`, the first dialect-specific SQL on the serving path, with its own guard test; otherwise this item does not exist | backend-engineer | **yes**: storage.py, mariadb.py | item 1p's EXPLAIN |
| 2 | **UI:** the three charts per §3 with the contract in §1: the token and classes, four trend series with the caption and the seven-column table, three-segment bars with a legend per card, `failingSplit` in `api.js`, `barRows` segments with today's DOM pinned for legacy callers, the fourth parallel request and the branch tab's own, the older-payload fallback, the day unit on the home trend call, the guards in §3 U9 and a fetch-sequencing guard for the new request | ui-engineer | **first paint** (one parallel request; the sequencing guard is the gate, no A/B) | item 1's commit (built on top of it, on the same branch) |
| 3 | **The drop:** What's new line for developers ("the charts show which failures are assigned"), operator note (no migration, no flag, a restart), `/verify`, `/review`, `/drop` | main session | n/a | items 1p and 2 |

Item 2 may be briefed as soon as item 1's commit exists, before 1p
finishes; a 1p finding goes back to item 1 and item 2 rebases. The build
runs the small path item by item, not `/overnight`.

## §9 Build status strip (WP-43, `wp-43-charts-assigned-split`)

One line per item of §8, kept current by the run; the restart point after
any interruption is the first line not `done` or `parked`.

| Item | Status | Commit | Note |
|---|---|---|---|
| 0 seed | in flight (spawned 2026-10-13) | | ledger `.claude/work/wp43-0-seed.md` |
| 1 backend | done | see §9 after item 0 | one finding from 1p (the full payload) and three from fresh eyes, fixed before the commit |
| 1p measured pass | first half done (perf-seed/1, 65 assignments); second half (90 days, 1,000 assignments on the widened seed) pending item 0 | | Known slow table holds; MariaDB plan starts from `current_assignments` |
| 1h dialect hook | not needed | | 1p's EXPLAIN starts from `current_assignments` (local MariaDB 12.3, indicative) |
| 2 UI | not started | | |
| 3 drop | not started | | |
