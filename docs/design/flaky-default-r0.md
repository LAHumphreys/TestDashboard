# A default flaky cadence for an environment — design, round 0

**Status: ROUND 1, dispositions in writing (2026-11-19). The owner's
verdicts are in "Round 1 review" at the foot of this file: twenty-two
decisions accepted as recommended, decision 3 reversed (the per-day chart
must apply a default), and §5's four questions answered (the fifth needs
no answer). §2 B.5, §3 U7 and
§3a's "The trend" are being rewritten for decision 3; until that lands,
§4 and those sections read as round 0 left them. Nothing is built.**
It builds on `flaky-mark-r0.md` (WP-44, frozen) and uses its terms: a
**mark**, the **cadence** N, **flaking**, **over its cadence**, a
**triple**. A **default** is new here: an environment's standing cadence
for every test in it that has no mark of its own.

How to read it. §0 is what is being built and §1 what was settled with the
owner before anything was written, in the owner's words. §2 (backend), §3
(UI) and §3a (the performance design pass) are the owning agents' sections.
§4 is the decisions, each as a recommended position with a verdict slot;
§5 the open questions. **The twenty-minute read is §0, §4 and §5**; §2,
§3 and §3a are where each decision is argued and are read as needed.
§3a was written after §2 and corrects its cost figures: where they
differ, §3a is right.

## §0 What we are building

An environment can carry a **default flaky cadence** of 2 to 10. Every test
in that environment that has no mark of its own is then judged as a marked
test is: it counts as failing only once its N most recent runs on a stream
have all failed. It is for the developers of a product whose suite runs on
more than one server into the same environment each night and who
investigate only the failures seen on every server; with a default of 2, a
failure on one server is flaking, not failing.

A test flaking under a default shows exactly as a marked test's failure
does: FLAKING in the flaking colour, counted in "(+N flaking)" beside the
failing figures, off New failures and Still failing, and listed in the
Flaking tab with its streak ("1 in a row, failing after 2") and the Review
panel. Past the cadence it is a new failure, as a marked test is. A test
marked by hand keeps its own cadence and reason: the mark wins over the
default. There is no way to hold one test strict under a default. The
default is set by any named user in the per-environment settings, beside
the expected count, with a required reason; every change is recorded with
who and when; removing it returns the environment to failing on the first
failure. Those settings move with this work: the two disclosures at the
foot of Open Actions (Manage people, Environment expectations) are lifted
onto a new Admin page with its own entry in the site navigation, and the
default is set there (§1, the owner's amendment). It covers mainline and every build of the environment and is
judged per stream, as a mark is.

The rule is "the N most recent runs", the rule a mark uses, and not "the
runs of one night". Between two servers' pushes a test can therefore read
as failing on one failure from each night, and then show under Fixed once
the second push passes. That is accepted, and stated on the Admin page and in
What's new.

Done means: Manage people and Environment expectations are on the Admin
page and gone from Open Actions; the default can be set, changed and
removed there;
every surface that shows flaking agrees about a test flaking by default; an
environment with no default, on an estate with no marks, runs the
statements it runs today, by count and by plan (four of them carry one
more primary-key `MIN`, measured in microseconds: §3a); the cost is measured cold on SQLite and MariaDB
with 50 to 500 failing tests in the environment that carries the default;
and it ships in the drop that carries WP-44.

Layers: backend (where the default and its history are kept, so a
migration with its MariaDB ledger step; the evaluation; the counts; the
endpoint) and UI (the new Admin page and the setting on it; how a row
flaking by
default explains itself). On the performance trigger paths: yes
(`storage.py`, `api.py`, Home's first paint), so round 0 carries §3a and
the build carries a measured pass.

**Not in this arc:** a strict mark (a test that fails on its first failure
under a default); pairing runs by night; recording which server pushed a
run.

## §1 Settled before round 0 (2026-11-19, in chat, captured verbatim)

The request: "I'd ideally like to set a default flake policy for a product.
E.g I have one product (two envs - happy to mark per env, that fits better
in data model I think) that pushes from two servers eveyr night. The team
only investigate failures on both. So I'd like to set a default flake
policy of 2. Thus it'll only trigger a fail if both fail"

Seven questions were put with options; the owner's choice is quoted as the
option's label.

1. How the two servers push: **"Same env, every test twice"** (both
   servers run every test and push into the same environment each night;
   if one misses a night the pair spans two nights).
2. How a test that fails on one server only is shown the next morning:
   **"Exactly as a marked test"** (FLAKING, counted, and listed in the
   Flaking tab with "1 in a row, failing after 2" and the Review panel).
3. What ships when: "OK we'll do a single drop in practice. But let's do
   your proposed build order. We can delay the drop. Get request 1 built,
   I'll verify and then I'll sign off the whole work with a test click
   through. Then we'll start a design-review for 2"
4. A hand mark under a default, and strictness: **"Hand mark wins, no
   strict"**.
5. Who sets it and where: **"Any user, on Open Actions"** (beside the
   expected count, with a required reason, every change recorded).
6. The rule reads the two most recent runs, not one night's pair:
   **"Accept it, name it"**.
7. How many tests fail on at least one server, on a typical night, in the
   larger environment: **"Around 50 to 500"**.

On §0 as first written (the default set in Open Actions' per-environment
section): "Agreed, go ahead with round 0"

An amendment while round 0 was being written, after the owner had read the
UI section's summary: "I think as part of this work we should lift the
admin section from the bottom of open action to a new admin tab". §0 above
is amended to match. Read as: both disclosures at the foot of Open Actions
move to a new page reached from the site navigation; what else the page
holds, its name and its place in the navigation are decisions in §4.

## §1a The provisional contract the two sections are written against

A starting point so that §2 and §3 name the same things. §2 may change any
of it with its reason; §3 follows §2.

- An environment's default: `flaky_default`, null or `{cadence, set_by,
  set_at, reason}`, carried wherever the per-environment settings of Open
  Actions are read today.
- Set, change or clear: `PUT /api/environments/{environment}/flaky-default`
  with `{username, cadence, reason}`; `cadence: null` clears it.
- A row's `flaky` object (dashboard rows, queue rows, Open Actions' rows,
  the test detail) gains `source`: `"mark"` or `"default"`. For
  `"default"`, `cadence`, `marked_by`, `marked_at` and `reason` are the
  default's own.
- Everything else of WP-44's contract (`flaky-mark-r0.md` §1 and §14)
  stands, read with "marked" meaning "marked, or under a default".

As round 0 left it (§2 B.3, answering §3 U12): the settings read is
`GET /api/environments`, fetched by the Admin page; each environment
also carries `flaky_default_history`, the newest twenty changes, in the
mark history's fields (`id`, `action`, `cadence`, `reason`, `actor`,
`acted_at`); a list row carries a default's `flaky` only where its
result is `FAIL`, the test detail in every state; `status.flaky_total`
still counts marks, `status.flaky_defaults` is new, and the names
part's `flaky_marks` is true where a mark or a default exists; a
`flaky` object with no `source` is a mark.

## §2 Backend (round 0, backend-engineer, base `6217c3e`)

A default is read as a mark on every unmarked test of its environment, and
WP-44's machinery judges it, with three changes of shape: the defaulted
FAIL rows come from the FAIL range, not from the marks; "flaking by default"
at a FAIL after a non-FAIL is a predicate over `lr`, never a bound list; and
nothing counts or reads an environment's passing tests. **Migration 13,
create-only; nothing on the push path.** The price is Q2 (the newest N+1
runs of each FAIL-after-FAIL test and each fix candidate) on Home's
headline: about +18 ms SQLite at 500 failing, an estimated +40 on MariaDB
(B.6). **ESCALATE** that figure; B.7 is the alternative that removes it.

> **Assembly note (main session).** §3a measured this section's shape
> and its numbers replace B.6's "this design, estimate" row: as written
> the headline costs +37 to +42 ms on SQLite and +50 to +58 on a local
> MariaDB at 500 failing, not +18 and +40. §3a's cheaper shape is the
> recommended design (§4, decisions 1 and 5); it also corrects B.1's F
> term, which as written drops a test failing on its first run. B.7's
> "about +2 ms" at 500 is likewise an estimate that §3a measured at +11
> to +14 on SQLite. B.9's decision 7 now applies only to a default of 3
> or more, the one case that still makes that read. The section is
> otherwise as its author wrote it.

### B.1 The rule, and where the default is read

- **A test's cadence** = its mark's, else its environment's default, else
  none. The mark wins whole (cadence, reason, owner, `source: "mark"`);
  unmarking under a default returns the test to the default.
- **WP-44's B.1 unchanged** (FAIL only, per stream, mute wins, retired
  uncounted, decisions 3 and 25). A mark's "cadence last set" is, for a
  default, its `cadence_at`: no run before it is a promotion.
- **The defaults map**, one statement, memoized estate-wide, read only when
  the bit says a default exists. The bit is one more primary-key `MIN` on
  the four statements that carry the mark bit today (mute epoch,
  environment products, a queue count, the names part).

| Read | Today (marks) | With a default in environment E |
|---|---|---|
| Q1 (rows, lists, headline) | FAIL rows CROSS JOIN marks | + a `UNION ALL` member: E's FAIL range (`idx_latest_runs_result`), LEFT JOIN marks, mute, retirement, assignee. Same statement |
| Qc (summary paths) | marks counted, fix candidates | + a member: E's rows with `prev_result = FAIL AND result <> FAIL` (E's primary-key range, filtered in the engine). Same statement. Defaulted tests are not counted |
| Q2 | marked FAIL/FAIL, candidates | + E's FAIL/FAIL rows (state at N ≥ 3; promotion at 2 unless before `cadence_at`) and E's candidates. LIMIT N+1, 100 a statement |
| F term (browse FLAKING/FAIL, Open Actions' X, queues) | bound row-value list | **predicate** for a FAIL after a non-FAIL in E: `lr.environment IN (<E…>) AND lr.prev_result <> ?`, less a bound list of E's marked or live-muted FAIL tests; the list form only for E's FAIL/FAIL flaking at N ≥ 3. Reads `lr` only: no join on any count |
| Rows' `flaky` | mark join, pair, Q2 page-local | default from the map, in Python, **FAIL rows only** (§3 P2) |
| Test detail | `test_acts`' `UNION ALL` | + a member: E's default by key. Same statement; state from the analytics window |
| Per-day trend | every run of each marked test | **unchanged: marks only** (B.5 P4) |

### B.2 Data model (migration 13; WP-15 to 14 in `UPGRADE_PLAN.md` §1)

```sql
CREATE TABLE environment_flaky_defaults (environment TEXT NOT NULL PRIMARY KEY,
    cadence INTEGER NOT NULL, reason TEXT NOT NULL,          -- cadence 2..10
    set_by TEXT NOT NULL REFERENCES users(username), set_at TEXT NOT NULL,
    cadence_at TEXT NOT NULL);           -- when the cadence last moved
CREATE TABLE flaky_default_history (id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL, action TEXT NOT NULL,  -- set|cadence|reason|clear
    cadence INTEGER, reason TEXT NOT NULL,
    actor TEXT NOT NULL REFERENCES users(username), acted_at TEXT NOT NULL);
CREATE INDEX idx_flaky_default_history_env ON flaky_default_history (environment, id);
```

- **Not a column** on `environment_expectations`/`environment_products`:
  their rows live only while their declaration does, a column is an
  `ALTER`, and the history needs a table anyway.
- **Ledger step 12 → 13**, `alters=()`, two probes; both tables in `ddl()`
  and `_ENVIRONMENT_TABLES`. **Create-only: the server keeps serving.**
  Rollback: SQLite's copy; MariaDB, the G.2 dump or drop both and set 12.
- `set_by`/`set_at`: the last act's. `cadence_at` moves on `set` and
  `cadence` only, so a reason edit makes no promotion night.

### B.3 Payloads (§1a confirmed, with these changes)

```text
GET /api/environments  (fetched by the Admin page, §3 U1; +1 statement)
  environments[]: {..., "flaky_default": null | {"cadence": 2,
     "set_by": "amy", "set_at": "2026-11-16T09:12:00.000000",
     "reason": "two servers; investigate failures on both"},
   "flaky_default_history": [{"id": 7, "action": "clear", "cadence": null,
     "reason": "trial over", "actor": "ben", "acted_at": "…"}, …]}  newest first, ≤ 20
PUT /api/environments/atlas-lab-alpha/flaky-default
  {"username": "amy", "cadence": 2, "reason": "two servers; …"}   cadence null clears
  200 {"environment": "atlas-lab-alpha", "action": "set"|"cadence"|"reason"|
       "clear"|"unchanged", "flaky_default": {…} | null}
  404 unknown environment: atlas-lab-zulu
  400 cadence: must be a whole number from 2 to 10 or null, got 11
  400 reason: required, to set, change or remove a default
  400 username: carol is not a known user; only a known, active user can
      set, change or remove a flaky default        (deactivated: as marks)
queue row (Flaking tab), unmarked test in atlas-lab-alpha:
  {"result": "FAIL", "prev_result": "PASS", …, "flaky": {"source": "default",
   "cadence": 2, "marked_by": "amy", "marked_at": "…", "reason": "two servers; …",
   "state": "flaking", "streak": 1, "runs": null}}
test detail, same test, state passing:
  "flaky": {"source": "default", "cadence": 2, "marked_by": "amy", …,
            "state": "passing", "streak": 0, "runs": null},
  "flaky_history": []            (the test's own mark acts only)
```

- **§3 P1, accepted**: history rides `GET /api/environments`, one
  statement for current and history, newest 20 per environment, in the mark
  history's fields (`id`, `actor`, `acted_at`, not `by`/`at`): one renderer.
- **P2, accepted**: a default's `flaky` on list rows only where the result
  is FAIL (unfiltered browse then builds a few, not 250); on the detail in
  every state, `state: null` where the stream has no run.
- **P3, accepted**: `flaky_total` unchanged (marks); new
  `status.flaky_defaults` = environments in scope with a default and a run
  on the stream, no statement. `flaky_marks` becomes "a mark or a default"
  (name kept). No `source` reads as a mark.
- **The repeated reason stays on the row**: responses are gzipped, and a
  response-level map would change what every row consumer reads.
- A default act posts no comment (it would be thousands); it drops every memo.

### B.4 Counts, memos, edges

- **Counts**: `flaky_view`'s `counted`, as for marks. `status.flaking` is
  the Flaking badge, the tab's total and the FLAKING filter's count; every
  tile, environment, script, product and Watch `flaking` figure, P and the
  tolerated passes follow. Toggle and chart key: `flaky_total +
  flaky_defaults > 0`.
- **Memos**: `flaky_states` stays per (stream, environment); the candidates
  ride Qc's per-stream entry; the map is one estate-wide entry. A push into
  E drops E's pairs; a default act drops all; Q2's cache key carries N.
  **The push path gains nothing.** Recommended: Q2's answers kept until
  E's next push, not 60 s (the key carries the latest start): paid once
  per push, not once a minute; cold unchanged.

| Edge | Rule |
|---|---|
| Mark and default on one test | the mark, whole; unmark returns it to the default |
| Set, changed, removed | state re-judged at once; never New (`cadence_at`); removed: FAIL-after-pass is New, FAIL/FAIL Still, as with no mark |
| Newly fixed / tolerated | decision 3, candidates from Qc's new member |
| Fewer than N runs | B.1's table: FAIL with no prior is failing, New as today |
| Live mute / retired | mute wins; retired in no count |
| Build stream | covered; judged on its own runs; Q2 floored at its first run |
| Renamed environment | keyed by name: the new name has no default; the old row stays until the old environment is dropped |
| Moved between products | stays with the environment; product scope is an environment list, already in every key |
| Environment deleted | default and history go with it (`_ENVIRONMENT_TABLES`, as marks) |
| No failing test | nothing moves; `flaky_defaults` counts it, so the toggle shows |
| Unknown environment | 404, as the expected count |

### B.5 The per-day trend (§3 P4): marks only in this arc

Judging past nights by today's default reads every run of every test in E
over the window, a window scan of `runs` (+298 to +317 ms at 14 days,
B.6). **Recommended**: the flaking part counts marks only; a default's
tolerated failures draw as failures and the caption says so where
`flaky_defaults > 0`. A streak per run in `activity_hours` would fix it, as
a package of its own.

### B.6 Cost (SQLite, scratch copies of perf-seed/1 at migration 12)

atlas-lab-alpha (8,000 tests) rewritten to **50 and 500 failing** on
mainline (half after a pass, half after a failure, 1 in 7 of those
promoted at N = 2; 15 and 150 fix candidates). **Measured cold,
in-process, alternated (12 and 16 calls), today's code**, against the same
copy with every alpha test marked at 2: the default emulated as 8,000
marks, an upper bound (it also counts the marks and reads the window):

| B−A ms, 50 ; 500 | headline (base 34) | queue New | Still | Flaking | browse none | FAIL | FLAKING | Open Actions rows | trend 14 d |
|---|---|---|---|---|---|---|---|---|---|
| emulated (upper bound) | +17.3 ; +53.7 | +4.1 ; +24.6 | +0.2 ; +22.7 | +4.8 ; +33.3 | +3.8 ; +4.1 | +1.0 ; +20.1 | +1.8 ; +15.7 | +1.3 ; +17.1 | +298 ; +317 |
| **this design, estimate** | **+2 ; +18** | +0.5 ; +9 | +0.5 ; +9 | +0.5 ; +9 | ≤ +0.5 | +0.5 ; +1.5 | +0.5 ; +1.5 | +0.5 ; +1.5 | 0 |

Statements measured alone (median of 15, page cache warm), 50 ; 500:
Q1's new member 0.08 ; 0.93; Qc's new member 0.78 ; 0.90 (base alpha rollup
5.6); Qc over 8,000 marks 8.8 ; 8.9 (avoided); **Q2 at LIMIT 3, 40 ; 400
tests, 0.73 ; 14.2** (LIMIT 4: 16.2). F term at F = 250, count / rows: list
5.90 / 6.82, **predicate 0.41 / 1.28**, none 0.14 / 1.24 (same 320 rows).
Queues' +9 is Q2 over the 250 FAIL/FAIL rows, cold; after Home's headline
they read none. **MariaDB not timed**: ×2.3 from §3c's ratios gives the
headline about **+5 at 50 and +40 at 500** over its 44 to 46 (estimate).

**No-default guarantee.** No mark and no default: the statements of today,
the four bit carriers each with one more primary-key MIN. Marks and no
default: the statements of `6217c3e`, same additions. Both pinned (B.8).

### B.7 The alternative to Q2: a streak written with the run

`latest_runs` gains `prev_streak` (FAILs ending at the previous run, cap 11)
and `prev_depth` (runs up to it, cap 12): state, promotion and fix for any
N ≤ 10 with no Q2, marks included. Push: two columns more in the SELECT and
UPDATE `_maintain_latest` already runs; its backfill path reads ≤ 12 runs
where it reads 1. Headline at 500 about +2 ms SQLite (estimate). Costs:
migration 13 **alters `latest_runs`** (stop the server) with a backfill,
and WP-44's verified evaluation is rewritten before its drop. Not
recommended unless the owner refuses B.6's figure.

### B.8 Tests the build writes

- `test_storage.py`: `FlakyDefaultWriteTest` (acts, history, reason, user,
  2..10, memos); **`DefaultEqualsMarksTest`** (a default equals every
  unmarked test marked at it: F, X, P, counts, queues, row states; the
  emulation as oracle); `MarkWinsTest`; `DefaultPromotionTest` (set,
  change, remove, reason edit: no New); **`DefaultPredicateTermTest`**
  (predicate = list = keys, rows and `total`, every filter mix and sort, a
  marked and a muted test in E); `DefaultEdgesTest`; `TrendMarksOnlyTest`.
- **`NoMarksAddsNothingTest` widened** to "no default"; a twin pins marks
  without a default at `6217c3e`'s statements. Cache guards cold: base + 1
  (the map) + ⌈Q2/100⌉, warm base; count-join guard over the predicate;
  `TargetedInvalidationTest` (a push into E drops E's pairs only).
- `test_api.py`: the PUT's answers and errors, `EnvironmentsListTest`,
  `flaky_defaults`, the names bit. `test_migrations.py`, `LedgerTest`
  12 → 13, `EnvironmentDeleteTest`; MariaDB variants at 500 FAIL rows.

### B.9 Decisions (recommended), open questions, pushback

1. **Read-time, by WP-44's machinery** (above). Rejected: B.7 now; a
   default as 8,000 virtual marks (+54 ms SQLite at 500, measured).
2. **Predicate for a FAIL after a non-FAIL in E**; lists only for the rest.
   Rejected: bound lists (+11 ms per browse at F = 250, measured).
3. **Two new tables, migration 13, create-only.** Rejected: a column.
4. **Reason required on every act, removal included** (§3 U10 2).
5. **`flaky_total` unchanged, `flaky_defaults` added, `flaky_marks` widened**.
6. **Trend: marks only** in this arc. Rejected: the window read (+300 ms).
7. **Q2 answers held until the environment's next push.** Rejected: 60 s.

Open: (1) for §3a, MariaDB 10.3 timings of Q2 at 400 tests, both new
members and the predicate term, and whether the Flaking queue at 200 to
500 rows meets its neighbours' cost; (2) for the owner, ESCALATE: the
headline at about +40 ms on MariaDB at 500 failing (estimate), or B.7.

Pushback: §0's "every surface that shows flaking agrees" cannot hold for
the per-day chart at an acceptable cost (B.5). At 50 failing a default
costs about what 50 marks do; the price is at the top of the range.

## §3 UI (round 0, ui-engineer, base `6217c3e`)

The two disclosures leave Open Actions for a new Admin page (U1), where the
default is set (U2). A row flaking by default draws as a marked one does;
only its acts differ (U5). U12 asks §2 for three things. No browser has rendered it.

### U1 The Admin page

New `static/admin.html` and `admin.js`. Both sections open, Environments
first (it changes more often): they were folded only to spare a triage
visit their fetches, and that reason stays behind on Open Actions.

```
testboard  Dashboard  Open actions  Time  Timeline  Watch  Metrics  Admin  What's new   amy [Change]
Admin
Settings for the whole board, rarely changed. Every change is recorded against your name.

ENVIRONMENTS                                                          #envs-section
  How many tests each environment is supposed to run … (today's text, then U2's)
  Assign 3 unmapped environment(s) to [product name] [Assign]         (hidden when none)
  Environment  Product  Tests seen  Expected  Recent passes  Latest pass      Flaky default
  …one row per environment, every product: U2

PEOPLE                                                                #people-section
  Deactivating someone removes them from every assignee dropdown. …  (today's note)
  Person  Status       Since
  dana    Deactivated  2026-10-02 09:00 by ben    [Reactivate]
```

- **Fetches**: `init()` starts `api/environments` and
  `api/users?include_inactive=1` together; each section draws on its own
  answer. With nav.js's What's new read, that is the whole first paint.
- **States, per section**: "Loading…"; today's empty lines; a failed read is
  said in its section only; a failed write uses the page's banner. An older
  server has both endpoints; only U2's column needs the new key.
- **No product filter or switcher**: a few dozen rows, five in the largest
  product, each with its editable Product cell, which a filter would hide on
  change. Like Metrics, not in `NAV_SCOPE_PAGES`; no scope carried.
- **Navigation**: the source is each page's `<nav class="site-nav">` markup
  (nine pages, now ten); `nav.js` only dates What's new and carries scope.
  "Admin" after Metrics, before What's new; `aria-current` on its own page.
- **Anchors**: `#envs-section`, `#people-section`; "Change the default" is
  `admin.html?environment=<env>#envs-section`, scrolled to and outlining that
  row once drawn, never a filter. Nothing links to the old anchors today.
- **Moves out** of `actions.js`: `loadPeople`, `buildPersonRow`, `loadEnvs`,
  `updateBulkAssignControl`, `bulkAssignUnmapped`, `buildProductCell`,
  `saveEnvProduct`, `buildEnvRow`, `saveEnv` and their `init()` wiring; of
  `actions.html`, both sections. Deactivate's "Reload the page for the
  assignee dropdowns to catch up" goes: Admin has no dropdown.
- **Open Actions keeps** filters, owners, list, bulk assign, expiring mutes;
  no pointer to Admin (decision 11).

### U2 The setting, on the Admin page

`admin.js`'s `buildFlakyDefaultCell(item)` and `openFlakyDefaultEditor(…)`.
The paragraph gains `FLAKY_DEFAULT_NOTE`: "A flaky default makes every test in
an environment that has no mark of its own count as failing only once its last
N runs have failed. It reads the most recent runs, not one night's."

```
Environment      … Expected  Recent passes   Latest pass        Flaky default
atlas-lab-alpha  … inferred  7 of 7 counted  11-18 00:00 · …  [Save]   none  Set
atlas-lab-beta   … 4000      6 of 7 counted  11-18 00:00 · …  [Save] Clear
                                                              2 in a row  Change · Remove
                                                              set by amy · 3 days ago · two
                                                              servers; investigate both
                                                              History (2) ▸
(after a removal)                                             none  Set
                                                              removed by ben · 9 days ago · trial over
Set or Change opens a row under the environment's, the table's width:
  Flaky default for atlas-lab-alpha  [fail after…▾] [Why this default? (required)] [Save] [Cancel]
  Every test here with no mark of its own counts as failing once its last 2 runs have failed.
Remove:
  Remove the flaky default for atlas-lab-beta  [Why remove it? (required)] [Remove] [Cancel]
  Every test here with no mark of its own then counts as failing on its first failure.
```

- `flakyCadenceSelect("env-cadence-select", current)`, WP-44's builder;
  Change opens on the current cadence, reason empty (the new one is what rows
  show); the sentence follows the choice (`cadenceSentence(n)`).
- Save disabled until a cadence and a reason, Remove until a reason, as the
  Review panel's `derive()`. Saving disables the row's buttons; an error goes
  to the page's banner and keeps what was typed; success redraws (`loadEnvs()`).
  History opens with no fetch (P1). The signed-in user is named. No
  `flaky_default` key (an older server): no column.

### U3 Wording: the helpers in `api.js`

```
flakySource(flaky)      "default" | "mark"; no `source` reads "mark" (a WP-44 server)
hasOwnMark(flaky)       what Set cadence, Unmark and the bar's `marked` read
flakySentence(f, mute)  widened by the forms below
flakyDetail(f)          "environment default · set by amy · 3 days ago · two servers; …"
flakyDefaultWords(c)    "Environment default, 2 in a row" (twin of flakyMarkWords)
flakyDefaultChange(h)   "removed by ben · 9 days ago · trial over"
flakyTag(entry)         null for a default, in every state

flaking    Flaking under the environment's default: failed its last run;
           counts as failing after 2 in a row
runs = N   Failing: its last 2 runs all failed; the environment's default is 2 in a row
runs < N   Failing: its only run here failed; the environment's default is 2 in a row
passing    The environment's default: counts as failing once its last 2 runs have failed
live mute  Under the environment's default; the mute comes first until it runs out
no run     Environment default, 2 in a row; no run here yet
```

### U4 A row flaking by default, surface by surface

```
state (default, no mark)  chip            edge   tag    the words, where
flaking, no live mute     FLAKING         mauve  none   Review panel; Streak cell's title
over the cadence          FAIL            red    none   Review panel
the night it goes over    FLAKING → FAIL  red    none   Review panel (New failures)
passing                   PASS            green  none   none: the row has no `flaky` (P2)
live mute                 its result      as now muted  the mute's
```

Row builders are unchanged. **No tag on a passing test** (every row would
carry one): the quiet place is the test's own page (U5), in every state.

### U5 The acts

```
Review panel, Flaky group:
Flaky  Flaking under the environment's default: failed its last run; counts as failing after 2 in a row
       environment default · set by amy · 3 days ago · two servers; investigate both
       [Why is this test flaky? (required)] [fail after…▾] [Mark as flaky]   Change the default
Test page header (the quiet offer style, every state) and card:
Flaky  Environment default, 2 in a row   set by amy · 3 days ago · two servers; …
       Flaking under the environment's default: …   [Mark as flaky]   Change the default
FLAKINESS   Environment default, 2 in a row · Mark as flaky
```

- **Mark as flaky** stays: "Give this test a mark of its own: its cadence and
  your reason replace the environment's default for it, until the mark is
  removed." No Set cadence or Unmark without `hasOwnMark`; Unmark's titles
  gain "…or as the environment's default says". **Change the default** goes
  to the Admin page's row (U1's anchors).
- **Selection bar**: `marked` is `hasOwnMark(entry.flaky)`; Mark as flaky gives
  each row under a default its own mark. The bar never touches a default.

### U6 The Flaking tab against its neighbours

| | New failures | Still failing | Flaking (a mark or a default) |
|---|---|---|---|
| Test; comment; Assignee, Take; Output, Review; the bar | yes | yes | yes |
| When | Failed at | Failing since, Last pass | **Streak** (D1, item 15's) |
| Review's Flaky group | the test's | the test's | **a default: Mark and a link only** (D2, decision 6) |

**200 such rows**, default 2: badge 200, all drawn (cap 500), each Streak "1 in
a row, failing after 2", after marks nearer their cadence, then by environment,
script, name. Whether that wants anything is Q1; nothing here pre-empts it.

### U7 Tiles, "All clear", the charts, Watch, the approximation

Counting only, but the FLAKING toggle, the charts' flaking series, the New
failures tile's "were passing or flaking before" and Open Actions' footer
hide where the scope has no mark (P3). The trend caption reads "by today's
marks and defaults" if §2 judges past nights so (P4). The approximation is
said in U2's paragraph and What's new only.

### U8 Performance impact

| Page | First-paint requests | Payload |
|---|---|---|
| Home, build tab, browse, script page | unchanged | `source` per `flaky` (+18 B); a default's `flaky` on FAIL rows only (P2); one integer (P3) |
| Test page | unchanged | `flaky` under a default in every state |
| Open Actions | unchanged | loses two lazy reads (no other user of either) and ~350 lines of `actions.js` |
| Admin (new) | 2, in parallel | `api/environments` with default and history; the roster |

Flag: a default's reason repeats on each row, ~20 KB on 200 Flaking rows.

### U9 Guards (`tests/test_frontend_calls.py`)

- **Moved**: `UserListTest`'s roster guard (to `admin.js`'s `loadPeople()`,
  reasoning rewritten); `BulkAssignUnmappedTest` (to `admin.*`, in `#envs-section`).
- **Widened**: `MetricsPageTest._PAGES`, `DropDateTest`'s nav marker and
  `ModulePreloadTest` take `admin.html`; `FlakyWordingTest`;
  `FlakingFilterTest`, `FlakingTrendTest`, `OpenActionsFlakingDomTest` (P3).
- **New**: `AdminPageTest` (one `admin.html` link per nav, after Metrics,
  current on its own page, not scoped; both reads with no `await` between;
  each section's states; `actions.*` hold neither section nor read, planted);
  `FlakyDefaultSettingTest` (no column without the key; three cells; Save and
  Remove gated, planted); `FlakyDefaultDomTest` (U4's states; Mark and the
  link only; `marked` false; no `source`, WP-44's DOM).

### U10 Decisions (recommended)

1. A last column, edited in a row beneath, with `flakyCadenceSelect`. Rejected: a section of its own; a number box.
2. A reason to set, change and remove; the signed-in user named. Rejected: none on removal, as Unmark; a picker.
3. Every change in the cell's History. Rejected: the last setting only.
4. No "flaky" pill for a default. Rejected: a pill on every failing row there.
5. "The environment's default" in sentences. Rejected: its name in each.
6. Under a default, Mark (row or bar) and a link. Rejected: changing the default from a row; hiding Mark.
7. The quiet place is the test's page; the approximation on Admin and in What's new. Rejected: Home, rows.
8. The Flaking tab unchanged. Rejected: designing for 200 rows before Q1.
9. The page is "Admin", after Metrics and before What's new. Rejected: "Settings"; next to Open actions, a daily page.
10. Both sections move, open, Environments first; nothing else joins: a build's delete stays on Home (it acts on the build in scope), Metrics on its page, expiring mutes on Open Actions. Rejected: gathering every admin-like act now.
11. Open Actions keeps no link. Rejected: a pointer at its foot, which outlives its reason.
12. No product filter on Admin. Rejected: the product switcher.

### U11 Open questions

1. 200 default rows in Flaking: as drawn, a count line ("180 under an environment's default · 20 marked"), or defaults after marks? Recommended: as drawn.
2. A sub-line under Streak naming the default? Recommended: no.
3. Removal needs a reason (decision 2): agreed?
4. Older What's new sections say the two sections are on Open Actions: left as history, or annotated? Recommended: left; the new line says where they went.

### U12 Pushback on §1a

- **P1** For removals and the record §0 promises: `flaky_default_history:
  [{action, cadence, by, at, reason}]`, newest first, on `GET /api/environments`.
  (§2 accepted it with the mark history's names, `actor` and `acted_at`: §1a.)
- **P2** A default's `flaky` on list rows only where the result is FAIL; on the
  test detail in every state, `null` included.
- **P3** `flaky_total` and `flaky_marks` mean a mark: add `status.flaky_defaults`
  and make `flaky_marks` true where a default exists. No `source` is a mark.
- **P4** The trend under today's default, or not, is §2's; U7 follows.

### What's new (draft, for the drop)

- **Admin**: Manage people and Environment expectations have moved from the
  foot of Open Actions to their own page, Admin, in the navigation bar.
- **A flaky default for an environment**, set on Admin with a reason ("fail
  after 2 in a row", 2 to 10): a test there with no mark of its own counts as
  failing only once its last N runs have failed; until then it reads FLAKING,
  in the Flaking tab, as a marked test does. Its own mark wins. It reads the
  most recent runs, not one night's: one failure on each of two nights
  counts, and the test may then show under Fixed.

## §3a Performance design pass (round 0, performance-engineer, base `6217c3e`)

Mine unless marked §2 or estimate. Copies of perf-seed/1 at migration 12 with 65 assignments
and 10 mutes. atlas-lab-alpha's mainline was planted at 50, 200 and 500 failing with §2's
`plant.py` mix: half FAIL after a pass, half FAIL after a FAIL (1 in 7 promoted at 2), and
0.3 × that many fix candidates; a default row was added at cadence 2. The same copies were
loaded into the local MariaDB **12.3, not production's 10.3**. Each read was a scratch
subclass of `6217c3e`'s `Storage`, measured in-process and cold (memos cleared before every
call). Five arms ran rotated per call on one database: A and A2 (base), **D** (§2 as
written), **C** (the cheaper shape below) and **S** (B.7's columns, added and backfilled on
the copy). SQLite ran 24 calls, MariaDB 24 to 32. Cells give B−A to B−A2 in ms. The A/A
noise on the headline was −0.2 to −3.2 ms on SQLite (the index-0 arm offset) and up to −9 ms
on MariaDB (IQR ±15 at 500); elsewhere it was within ±0.7. D, C and S returned
**byte-identical bodies** at every count on both backends, and they equal §2's 8,000-mark
emulation (500: new 66, still 254, fixed 42, flaking 250).

| Cold B−A, ms, SQLite ; MariaDB (base) | 50 | 200 | 500 |
|---|---|---|---|
| **Headline as written** (31–35 ; 52–71), statements 37 → 41/42/44 | +9 to +12 ; +9 to +10 | +20 to +24 ; +25 to +28 | **+37 to +42 ; +50 to +58** |
| Headline, C (run 3 on the members), 37 → 40 | +7 to +10 ; +6 | +16 to +20 ; +13 to +14 | +29 to +31 ; +19 to +33 |
| Headline, S (B.7), 37 → 40 | +5 to +7 ; +6 | +7 to +10 ; +10 to +11 | +11 to +14 ; +11 to +18 |
| New failures queue, Home's first paint (5–15 ; 23–31): D / C / S | +4.5 / +4 / +1 ; +5.5 / +4 / +3 | +11 / +8 / +2 ; +15 / +6.5 / +4 | +22 / +15 / +4 ; +34 / +12 to +16 / +7 to +10 |
| Flaking queue (4 ; 16–18): D / C / no promotion read (L) | +4.7 / +4 / +1.2 ; +6.7 / +4.7 / – | +14 / +11 / – ; +20 / +12 / – | +28 / +21 / +11 ; +44 / +24 / +19 |
| Browse, no filter, first paint (10 ; 16), 3 → 4 statements | +0.3 to +1.0 ; – | +0.5 to +1.0 ; – | +0.3 to +0.7 ; +0.5 to +1.0 |
| Browse FAIL (2.5–5 ; 8), F bound as a list | +0.5 ; – | – | +14 to +15 ; +11 to +12 |
| Browse FLAKING (today no statement, 0.05 ; 0.4) | +1.1 ; – | – | +9.4 to +9.9 ; +23.4 to +23.8 |
| Open Actions' rows, default mode, first paint (25–29 ; 95–108) | −1 to +0.1 ; – | – | +6 to +9 ; −3 to −5 (band ±10) |
| Open Actions, Failing mode (20 ; 79–85) | – | – | +13 ; +26 to +32 (IQR ±12) |

- **The estimates do not hold; ESCALATE.** §2's estimates were +2 and +18 on SQLite, and
  +5 and +40 on MariaDB. Measured as written, the headline costs +9 to +12 and +37 to +42 on
  SQLite, and +9 to +10 and +50 to +58 on MariaDB. The cause is §2's `parts.py`, which times
  statements alone, repeated and warm. Inside a cold call, Q2 for 400 tests is four statements
  of 6.1 to 6.6 ms each on SQLite (25 ms, against 14.2 alone) and of 9.6 to 10.2 ms on MariaDB
  (40 ms). The rest is fixed whatever the count: E's candidates member (1.8 to 3.1 ms SQLite,
  4.4 MariaDB), Q1's member (3.0 and 6.0 ms at 500), the map and the fold. Q2 carries it.
- **The Flaking queue as written reads Q2 for every FAIL-after-FAIL test.** At cadence 2 that
  read only decides promotion, which the queue never shows. It costs +28 / +44 ms at 500.

**The cheaper read-time shape (C + L), measured.**
1. **Run 3 rides the existing members at cadence 2.** Q1's E member and Qc's E member each
   gain `CASE WHEN lr.prev_result = 'FAIL' THEN (SELECT r.result FROM runs AS r WHERE <triple>
   AND r.stream_id = lr.stream_id AND r.start_time < lr.start_time ORDER BY r.start_time DESC
   LIMIT 1 OFFSET 1) END`. Q1 already reads `flaky_mark_history` the same way, a scalar
   subquery. The holders are pre-filled with `(result, prev_result, run 3)`, so Q2 reads
   nothing for a defaulted test at 2. Q2 stays only for a default of 3 to 10, which costs D's
   figures (no cheaper read exists there).
   - Plans: `sqlite_autoindex_runs_1 (… AND start_time<?)` on SQLite 3.50; a `ref` on
     `uq_runs_identity` at key_len 2302 on 12.3.
   - Cost alone: 11.1 ms SQLite, 8.6 MariaDB for 250 lookups.
   - It saves 9 to 11 ms (SQLite) and 17 to 35 ms (MariaDB) at 500, and 44 → 40 statements.
2. **Rows paths read no promotion (L).** The Flaking queue, browse and Open Actions' rows
   decide a default's FAIL after FAIL at 2 by its pair: `_flaky_decide`'s `settled` rule,
   widened for a caller that reads no P. The run-3 read is then made only by a count path:
   either the member above, filled by the headline, or one statement over E's FAIL range.
   Measured on the Flaking queue at 500: +11 SQLite and +19 MariaDB, against +28 and +44. At
   50: +1.0 to +1.4.
3. **§2's predicate, with one correction.** A first-run FAIL has no `prev_result`, so
   `lr.prev_result <> ?` is NULL. The exclude term then drops that FAIL row, which is failing
   and not flaking. It needs `lr.prev_result IS NOT NULL AND`.
   - Alone at F = 250 (SQLite ; MariaDB, count + rows), FAIL chosen: none 1.7 ; 4.1, list
     12.4 ; 8.6, predicate 2.1 ; 6.1. FLAKING chosen: list 2.1 ; 14.1, predicate 2.5 ; 6.2.
   - On both engines the predicate keeps the `idx_latest_runs_result (stream_id=? AND
     result=?)` range. The excluded marked and muted keys are a 10-row `LIST SUBQUERY`, a
     filter and never a driver. That is not item 16's trap, where 3.26 drove the read from a
     row-value IN.
   - MariaDB's list form for FLAKING switched to a PRIMARY range over the 250 keys. The
     predicate removes that choice.
   - With the predicate, browse FAIL at 500 is about +4 / +9 and FLAKING about +9.5 / +16
     (estimate: the measured list rows less the measured forms).
   - Gate: the build's plan guard requires the range text. Plans by hand on 10.3 and SQLite
     3.26 in `/perf-ab`. My plans are from 3.50 and 12.3 only.
4. **Measured no.** Carrying the candidates' keys on E's rollup (the existing pass) costs
   +2.3 / +3.3 ms against the separate member's 1.8 / 4.4. With run 3 it is worse (+13.5 /
   +9.6). Keep §2's member.

**The no-default guarantees (§0's "Done means"): true in count and plan; not true in text.**
- Adding a third MIN to the mute-epoch statement measured +0.0007 ms on SQLite and +0.023 ms
  on MariaDB, with no added statement or round trip. The other three carriers are already
  MIN/UNION ALL riders of the same kind.
- No form is cheaper: holding the bit in a memo is a statement on every cold read.
- With a default, the map is +1 statement cold (browse 3 → 4) and Q1's and Qc's members are
  extra work inside existing statements.
- `NoMarksAddsNothingTest` should pin the statement count and plan with no default, and the
  rider as the only change to the text. Widen it; do not weaken it.

**B.7, honestly.**
- **Headline saving over C at 500:** about 17 ms SQLite and 5 to 15 ms MariaDB. At 50: about 2
  ms and none.
- **Push path, nightly in-order run:** no added statement (two more columns on the SELECT and
  UPDATE that `_maintain_latest` already runs). Not timed.
- **Push path, out-of-order or first-sighting run:** the seek reads up to 12 runs instead of 1,
  measured +10 µs a run on SQLite (13 → 23) and +30 µs on MariaDB (170 → 201). That is +0.08 s
  / +0.24 s for an 8,000-run night where one server's runs all arrive out of order, which two
  servers pushing into one environment can cause every night.
- **Upgrade:** two ADD COLUMNs took 37 ms on SQLite and 65 to 104 ms on 12.3 at 31,840 rows
  (the instant path; 10.3.2+ should take it too, which `/perf-ab` would have to show). The
  backfill took 1.7 s on SQLite and 20 s on MariaDB (16 s of that is row-by-row UPDATEs, which
  can be batched). It grows with `latest_runs` rows × 13 runs, not with history. The server
  stops for it, because a push would race it.
- **Risk:** the rules need not change. S only replaced Q2's source with a window built from the
  columns, and its bodies were identical. But it adds a second, derived copy of every test's
  streak, which re-imports, deletes and the drop tool must keep equal to `runs`. That is new
  push-path logic just before WP-44 ships.
- **Recommendation:** not in this drop. Offer it in a performance-centred drop if production's
  nightly counts sit near 500.

**The trend: "marks only" is right.** The chart counts runs per night as they stood. A latest
bar built from the headline would count tests as they stand now, and miss a tolerated run
whose test then passed (a candidate). No bounded read-time route exists. The bounded route is
a per-run flag written at push into `activity_hours`: its own package, on the push path.

**The bar for the build** (C + L + the predicate, cold, against `6217c3e`, SQLite ; MariaDB):

| Read | 50 failing | 500 failing |
|---|---|---|
| Home's headline | +7 to +10 ; +6 | +29 to +31 ; +19 to +33 |
| New failures queue (first paint) | +3 to +4 ; +3 to +5 | +15 to +16 ; +12 to +16 |
| Flaking queue | +1.0 to +1.4 ; about +4 (estimate) | +11 ; +19 |
| Browse, no filter (first paint) | at most +1 ; at most +1 | at most +1 ; at most +1 |
| Browse FAIL / FLAKING | +0.5 / +1.1 ; not measured | about +4 / +9.5 ; about +9 / +16 (estimate) |
| Open Actions' rows (first paint) | level ; not measured | +6 to +9 ; level (±10) |
| No default, with or without marks | 0 statements; the rider's µs | the same |

`/perf-ab` measures:
- the Known slow rows with no default (statement counts equal);
- each row above, plus Watch with an E card (`flaky_view` serves it; not measured here), the
  test detail, a build's headline in E (the default covers builds; Q2 carries the floor) and
  Open Actions' Failing mode;
- at 500 failing with a default of 3 as well (§4, decision 4; added at assembly);
- at 0, 50, 200 and 500 failing, on SQLite and on MariaDB by hand with `EXPLAIN`, including
  10.3 for the run-3 subquery and the predicate;
- statement counts for `mute_epoch`, the map, `_flaky_failing`, `_flaky_marked`,
  `_cadence_runs_iso`, `queue_counts` and `status_queue`.

**Harness note (not built).**
- `seed.py --onto <copy> --default-failing N [--default-cadence C]`: §2's `plant.py` mix,
  newest three runs and `latest_runs` rewritten together, and the default written through the
  tree's own API so its history is real. It also needs what `plant.py` lacks: a first-run FAIL
  in E, and one hand-marked, one muted and one assigned planted failure in E. It leaves
  `activity_hours` stale, so the fixture is invalid for the trend: say so in the census.
- `ab.py --default-failing N` and a three-arm A/A2/B mode, because the index-0 arm offset
  showed again on this run.
- §2's `measure.py` is sound in method but has no A/A arm and compares two files.
  `parts.py`'s warm timings are what under-read Q2.

**Verdict: proceed with the named cheaper shape, and ESCALATE the headline.**
- The cheaper shape is C (run 3 on the members at cadence 2), L (no promotion read on rows
  paths) and §2's predicate with the `IS NOT NULL` fix. B.6's table is replaced by these
  measured rows.
- The owner chooses between:
  - Accepting C on Home's cold headline: +19 to +33 ms on MariaDB at 500 failing, +13 to +14 at
    200, +6 at 50. WP-44 accepted +6 to +7 at 50 marks and +13 to +15 at 1,000.
  - B.7 in a later performance drop: +11 to +18 at 500.

## §4 Decisions

Each is the recommended position, with where it is argued and a verdict
slot. The first four are the ones that cost something or change §0; the
rest are how the sections chose to build what §0 already says.

Five terms. **Home's headline** is the one summary request Home's tiles
are drawn from on its first paint. A failure is **tolerated** while its
test is flaking: it is not counted as failing. A **fix candidate** is a
test that was failing and now passes, which has to be judged as fixed
or as a tolerated failure ending. **The bar** is the cost the build's
measured pass is held to: §3a's last table. **The parked
in-run-progress work** is WP-15, an unmerged package that had reserved
migration 13.

**The four that need the owner.**

1. **The price on Home's headline is accepted, in §3a's cheaper shape.**
   Cold, against `6217c3e`, with a default of 2 (SQLite ; a local MariaDB
   12.3): +7 to +10 ; +6 ms at 50 failing tests in the environment, +16 to
   +20 ; +13 to +14 at 200, +29 to +31 ; +19 to +33 at 500, on bases of 31
   to 35 and 52 to 71. For scale, WP-44 accepted +6 to +7 at 50 marks and
   +13 to +15 at 1,000 on MariaDB. With no default anywhere, no
   statement is added and no plan changes (§0's "Done means").
   The alternative is decision 2. (§3a's table and bar) — verdict:
2. **A streak kept on each test's latest-run row at push time is not built
   in this drop**, and is offered for the performance-centred drop after
   it. It would bring the headline at 500 to +11 to +14 ; +11 to +18 ms.
   Its costs: the server stops for the upgrade (a backfill measured at 20 s
   on the seed's MariaDB copy), every out-of-order push reads up to twelve
   runs where it reads one, and a second, derived copy of every streak has
   to be kept equal to the runs by pushes, re-imports and deletes, written
   just before WP-44 ships. (§2 B.7, §3a "B.7, honestly") — verdict:
3. **The per-day chart counts hand-marked tests only.** A failure tolerated
   under a default draws there as a failure, and the chart's caption says
   so where a default exists. Judging past nights by a default reads every
   run of the environment over the window (+298 to +317 ms at 14 days),
   and a bar built from the headline would count tests, not runs. §0's
   "every surface that shows flaking agrees" is read as every surface but
   this chart. The route that would fix it is a per-run flag written at
   push time, a package of its own. (§2 B.5, §3a "The trend") — verdict:
4. **A default may be 2 to 10, as §0 says, and the bar above is for 2.**
   The cheaper shape works because at 2 a test's state needs the two
   results its latest-run row already holds, plus one earlier run that
   a statement already running can fetch beside them. A default of 3 or more reads
   each failing test's recent runs as §2 first wrote it: +37 to +42 ; +50
   to +58 ms at 500. The build measures a default of 3 and reports it; the
   alternative is to offer only "2 in a row" as a default in this drop.
   (§3a, cheaper shape 1) — verdict:

**How it is built.**

5. Judged when a page is read, by WP-44's machinery: the environment's
   failing tests and its fix candidates ride statements that already run;
   the third run rides them at a default of 2; lists read no promotion;
   "flaking by default" is a condition on the latest-run rows, with §3a's
   correction so that a test failing on its first run stays a failure.
   The push path gains nothing. For a default of 3 or more, the one
   case that still reads each failing test's recent runs, that read is
   kept until the environment's next push rather than for a minute.
   (§2 B.1 and B.9 7, §3a) — verdict:
6. Two new tables, migration 13, create-only: the server keeps serving
   through the upgrade. The parked in-run-progress work moves to 14. Not a
   column on an existing table. (§2 B.2) — verdict:
7. A reason is required to set, change and remove a default; the signed-in
   user is the one named. (§2 B.9 4, §3 U10 2) — verdict:
8. Every change is recorded; the newest twenty per environment are shown
   in the cell's History, with no request of their own. (§2 B.3, §3 U2) —
   verdict:
9. Setting, changing or removing a default re-judges every test at once.
   Setting or changing one never makes a New failure; editing only the
   reason moves nothing. Removing one returns its flaking tests to what they would be
   with no default: a failure after a pass is a New failure that morning.
   (§2 B.4) — verdict:
10. A hand mark wins whole (cadence, reason, owner); unmarking a test
    under a default returns it to the default. (§2 B.1) — verdict:
11. The Flaking tab's badge and the FLAKING filter count a test flaking by
    default as they count a marked one. `flaky_total` still counts marks;
    a new `status.flaky_defaults` counts environments with a default, and
    the FLAKING filter and the charts' flaking series show where either
    exists. (§2 B.3 P3, B.4) — verdict:
12. A list row carries a default's details only where the test is failing;
    the test's own page carries them in every state. Rows repeat the
    default's reason (about 20 KB on 200 rows before compression). (§2
    B.3) — verdict:
13. A default belongs to an environment by name: a renamed environment
    starts with none, and a deleted one takes its default and history
    with it, as marks do. (§2 B.4) — verdict:

**What the developer sees.**

14. The page is "Admin", after Metrics and before What's new in the
    navigation. Rejected: "Settings"; beside Open actions. (§3 U1) —
    verdict:
15. Both sections move, open rather than folded, Environments first;
    nothing else joins them now (a build's delete stays on Home, expiring
    mutes stay on Open Actions). (§3 U10 10) — verdict:
16. Open Actions keeps no link to Admin; What's new says where the
    sections went. (§3 U10 11) — verdict:
17. No product filter on Admin: a few dozen rows. (§3 U10 12) — verdict:
18. The setting is the environments table's last column, edited in a row
    beneath it, with the select a mark uses ("fail after…"). (§3 U2) —
    verdict:
19. No "flaky" pill for a default, on a passing or a failing row: the
    FLAKING chip alone. The one quiet place that says an environment has a
    default is a test's own page. (§3 U4, U10 4 and 7) — verdict:
20. Sentences say "the environment's default", not the environment's
    name. (§3 U3) — verdict:
21. Under a default a row offers "Mark as flaky" (which gives the test a
    mark of its own) and a link, "Change the default", to the Admin page's
    row. Set cadence and Unmark are not offered: there is no mark. This is
    the one new difference between the Flaking tab and its neighbours.
    (§3 U5, U6) — verdict:
22. The Flaking tab is otherwise unchanged (§5, question 1). (§3 U6) —
    verdict:
23. The approximation (the N most recent runs, not one night's) is said in
    the Admin page's paragraph and in What's new, nowhere else. (§3 U2,
    U7) — verdict:

## §5 Open questions

1. **A morning with 200 rows flaking by default in the Flaking tab.** As
   drawn (recommended), a count line above the rows ("180 under an
   environment's default · 20 marked"), or defaults sorted after marks?
2. **A sub-line under Streak naming the default?** Recommended: no; the
   Review panel says it.
3. **Older What's new sections** say the two sections are on Open
   Actions. Left as history (recommended), or annotated?
4. **Where in "50 to 500" a typical night sits.** It decides how soon
   decision 2's alternative is worth its upgrade; nothing in this drop
   depends on it.
5. **Not measured in round 0, and named in §3a's bar for the build's
   measured pass:** Watch with a card for the environment, the test's
   page, a build's headline, Open Actions' Failing mode on MariaDB at 50
   and 200; and the plans on MariaDB 10.3 and SQLite 3.26, which are
   CI's and the measured pass's to show.

## Round 1 review (2026-11-19, in chat, captured verbatim)

The owner went through §4 and §5 in chat, one decision at a time, each put
with its recommended position and the alternatives as options. The owner's
choice is quoted as the option's label; anything the owner typed is quoted
whole.

**§4, the four that need the owner.**

1. The headline's price: **"Accept (Recommended)"**.
2. The streak kept at push time: **"Later drop (Recommended)"** (not in
   this drop; a candidate for the performance-centred drop after it).
3. The per-day chart, offered as "Marks only (Recommended)", "Chart must
   agree" or "Discuss". The owner typed: "Chart must agree. It can render
   late (+300ms) but it must not hold up the rest of the page. Drop can
   mvoe out […]". Asked then what the chart area
   shows while the figures that apply the default are loading: **"Wait,
   draw once (Recommended)"** (the usual loading state, then one drawing
   with the right figures; nothing is shown that then changes).
4. The values a default may take: **"2 to 10 (Recommended)"**.

**§4, how it is built.** 5: **"Accept (Recommended)"**. 6: **"Accept
(Recommended)"**. 7 (a reason to set, change and remove): **"Accept
(Recommended)"**. 8 (the newest twenty changes shown): **"Accept
(Recommended)"**. 9 (a removal can make New failures): **"Accept
(Recommended)"**. 10: **"Accept (Recommended)"**. 11: **"Accept
(Recommended)"**. 12 (the reason repeated on each failing row): **"Accept
(Recommended)"**. 13: **"Accept (Recommended)"**.

**§4, what the developer sees.** 14: **"Admin, after Metrics
(Recommended)"**. 15: **"Accept (Recommended)"**. 16: **"No link
(Recommended)"**. 17: **"No filter (Recommended)"**. 18: **"Accept
(Recommended)"**. 19: **"No pill (Recommended)"**. 20: **"Accept
(Recommended)"**. 21 ("Mark as flaky" and a "Change the default" link):
**"Accept (Recommended)"**. 22: **"Accept (Recommended)"**. 23: **"Accept
(Recommended)"**.

**§5.** 1 (200 rows flaking by default in the Flaking tab): **"As drawn
(Recommended)"**. 2 (a sub-line under Streak): **"No (Recommended)"**. 3
(older What's new sections): **"Leave as history (Recommended)"**. 4
(where in "50 to 500" a typical night sits): **"Nearer 50"**. 5 was not
put: it lists what the build's measured pass has to cover.

**Read as.** All are decisions; none is a style instruction, and no
question came back. Twenty-two decisions stand as round 0 recommended
them. Decision 3 is reversed, and it changes the design:

- §0's "every surface that shows flaking agrees about a test flaking by
  default" stands with no exception. The per-day chart applies an
  environment's default to the nights it draws.
- The cost the owner accepted is the read round 0 measured and rejected:
  about +300 ms at a 14-day window, taken on SQLite. It has not been
  measured on MariaDB, nor at a longer window; round 1 measures both
  before the figure is called accepted there.
- That read is not on any page's first paint and holds nothing else up.
  Where the chart's scope has a default, the chart shows its loading
  state and draws once.
- The drop that carries WP-44 moves out for it.

Round 1 rewrites §2 B.5, §3 U7 and §3a's "The trend" to that, and §4's
decision 3 with them.
