# A default flaky cadence for an environment — design

**Status: FROZEN after round 1 (2026-11-19). No decision is open. The
owner's verdicts are in §4's slots and, word for word, in "Round 1
review" at the foot of this file: twenty-two of round 0's decisions
accepted as recommended, decision 3 reversed (the per-day chart applies a
default), and four more taken on the chart (24 to 27). §2 B.5, §3 U7a and
§3a's "The trend, round 1" are the round's rewrites. §6 is the plan the
build runs from. Nothing is built.**
It builds on `flaky-mark-r0.md` (WP-44, frozen) and uses its terms: a
**mark**, the **cadence** N, **flaking**, **over its cadence**, a
**triple**. A **default** is new here: an environment's standing cadence
for every test in it that has no mark of its own.

How to read it. §0 is what is being built and §1 what was settled with the
owner before anything was written, in the owner's words. §2 (backend), §3
(UI) and §3a (the performance design pass) are the owning agents' sections.
§4 is the decisions, each as a recommended position with a verdict slot;
§5 the questions, answered in round 1. **The twenty-minute read is §0, §4
and §5**; §2,
§3 and §3a are where each decision is argued and are read as needed.
§3a was written after §2 and corrects its cost figures: where they
differ, §3a is right. §6 is the build's plan, and "Round 1 review" at the
foot is the owner's answers, word for word. Every ESCALATE in §2 and §3a
was taken to the owner and is settled by a decision in §4; a build parks
only on a new excess over §3a's bars.

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
outside the per-day chart's late part (B.5), nothing counts or reads an
environment's passing tests. **Migration 13,
create-only; nothing on the push path.** The price is Q2 (the newest N+1
runs of each FAIL-after-FAIL test and each fix candidate) on Home's
headline: about +18 ms SQLite at 500 failing, an estimated +40 on MariaDB
(B.6). **ESCALATE** that figure; B.7 is the alternative that removes it.
(Settled in round 1: §4, decisions 1 and 2.)

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
  uncounted, WP-44's decisions 3 and 25). A mark's "cadence last set" is, for a
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
| Per-day trend | every run of each marked test in the window | + one statement per defaulted E in scope, driven from the window's FAIL rows and judged in the engine, no Q2; memo per (stream, E) (B.5, §3a round 1) |

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
  (name kept). No `source` reads as a mark. `flaky_defaults > 0` also
  promises `FAIL_FLAKING` applies the default (B.5).
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
  per push, not once a minute; cold unchanged. The trend's default
  figures: one entry per (stream, E) (B.5).

| Edge | Rule |
|---|---|
| Mark and default on one test | the mark, whole; unmark returns it to the default |
| Set, changed, removed | state re-judged at once; never New (`cadence_at`); removed: FAIL-after-pass is New, FAIL/FAIL Still, as with no mark; and the chart's nights, by today's default |
| Newly fixed / tolerated | WP-44's decision 3, candidates from Qc's new member |
| Fewer than N runs | B.1's table: FAIL with no prior is failing, New as today |
| Live mute / retired | mute wins; retired in no count |
| Build stream | covered; judged on its own runs; Q2 floored at its first run |
| Renamed environment | keyed by name: the new name has no default; the old row stays until the old environment is dropped |
| Moved between products | stays with the environment; product scope is an environment list, already in every key |
| Environment deleted | default and history go with it (`_ENVIRONMENT_TABLES`, as marks) |
| No failing test | nothing moves; `flaky_defaults` counts it, so the toggle shows |
| Unknown environment | 404, as the expected count |

### B.5 The per-day trend, round 1: judged by the default

Decision 3 is reversed: the chart applies a default to every night it draws, late, holding
nothing else up. The read's shape and cost are §3a's, and where they differ, §3a's stand.

**The rule.** On stream S, a FAIL run of test T in environment E counts in its UTC day's
`FAIL_FLAKING` when T has no live mute now and `analytics.flaking_that_night(c, runs)` flags it.
`runs` is T's runs on S, oldest first, with the N−1 before the window. `c` is T's mark's cadence,
else E's default **as it is today** (as WP-44 judges past nights by today's marks, so decision 9
re-judges the chart at once), else T is not read. Rejected: each night's own default from
`flaky_default_history`, which would disagree with the tiles the morning a default is set. Per
stream, builds included (`floor`); a mark wins whole; a run counts once (muted, else flaking,
else assigned). It counts failed runs per day: §3's "the day's tolerated failures" is exact.

Example: default 2, servers A then B nightly. T passed on night 0, failed on A only on nights 1
to 3 and on both on night 4: `P · F P · F P · F P · F F` (checked against the pure rule). Nights 1
to 3 read FAIL 1, FAIL_FLAKING 1; night 4 reads 2 and 1 (A's F follows a P; B's is the second in
a row). B first on night 3 (`… P F · F F`): 1/1, then 2/0, §0's approximation as the tiles
judge it. A mark of 3 makes night 4 2/2; a live mute now moves T's FAILs to `FAIL_MUTED`.

**Every chart that shows flaking.** The per-day trend (`renderTrend`, Home and a build tab) now
**applies the default**. The by-environment, top-scripts and product bars (`by_environment`,
`top_failing_scripts(flaking=)`, `_products_summary`) and Watch's e:/p: cards (`_handle_watch`)
have applied it since round 0 through `flaky_view` (B.4). The rest (Watch's s: cards, Time,
Timeline, the script page, a test's run history, Compare) shows no flaking: nothing to apply.

**Where the read lives:** `parts=assigned_trend` (`api._assigned_trend_nights`), not a part of its
own. It is sent after the headline is drawn, and only the trend card waits for it. Split and
default draw together, once, so a second part would only add a request. The headline is
unchanged: `trend.nights` (`daily_result_counts`) counts every FAIL, and `status.flaky_defaults`
(B.3 P3, no statement) names a default in scope. The full payload has no split (WP-43). No
first-paint request gains a statement. §3's two round 1 asks (no marks-only fallback; an older server reads as no default) are
accepted. (1) Where the headline's
`flaky_defaults > 0`, the same scope's `FAIL_FLAKING` includes the default's tolerated runs; no
marks-only fallback exists, so no field names one. An act made between the two requests leaves
the part the newer. (2) An older server sends no key and marks-only figures. Read as 0, that
draws today's chart; nothing here contradicts it.

**The read** is §3a's, round 1. The marks statement is unchanged and runs only where a mark
exists (the "any mark" bit is split from "any default"). Each defaulted E in scope (from the
map) gets one statement, driven from the window's FAIL rows and judged in the engine by each
row's N−1 previous runs on the stream; no Q2. It grows with the failing runs in the window, not
with E's tests. The shape first written here (every window run of E's tests read into Python,
then Q2) was measured at +1,010 ; +2,224 ms at 50 failing a night and is withdrawn, with its
`script_hours` narrowing (§3a, "Measured no").

**Payload.** `&environment=atlas-lab-alpha`, default 2: `{"generated_at": …,
"environment": "atlas-lab-alpha", "product": null, "stream": 1, "trend": {"days": 14, "from":
"2026-11-06", "to": "2026-11-19", "nights": [… {"date": "2026-11-18", "FAIL_ASSIGNED": 9,
"FAIL_MUTED": 2, "FAIL_FLAKING": 231} …]}}`. Beta, no default: marks only. `&product=Atlas`
(alpha defaulted, beta not): alpha's marks and default, plus beta's marks. Same keys throughout.

**No default, nothing added.** With none anywhere, the statements are today's by count and plan;
the only change of text is the bit on the mute epoch (B.1, §3a). With one only outside the
scope: +1 cold (the map), and no runs are read. **Memo:** per (S, E),
keyed as §3a gives it, stored with `_store_summary(…, S, E)`. It is
dropped by a push into (S, E), by every act (default, mark, assignment, mute, retirement), by a
mute expiry and by the TTL; a push elsewhere leaves it. §3a decides against a hold until E's next push: the usual expiry stands. Nothing is added on the push path.

**Tests.** `DefaultTrendTest` replaces `TrendMarksOnlyTest`: `FlakyTrendTest`'s oracle with
c = mark, else default, over the example in both orders, mark wins, mute wins then expires, runs
before the window, a build's floor, a default set after the nights, removal, assigned counted
once. `DefaultEqualsMarksTest` gains the trend. `DefaultTrendStatementsTest`: cold = today's +
the map + 1 per defaulted E, nothing warm (§3a: no Q2). `TargetedInvalidationTest`: a push into F
leaves E's entry served and right. Widened: `FlakyPlanTest`'s trend guard to the new member
(SQLite; MariaDB excluded by name, 10.3 by hand), and `NoMarksAddsNothingTest` to "no default"
at 14 and 90 days, with a marks twin at `6217c3e`. `AssignedTrendDefaultTest`: three scopes,
ask (1), headline and full payload unchanged. MariaDB variants come through each base class.

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
| **this design, estimate** | **+2 ; +18** | +0.5 ; +9 | +0.5 ; +9 | +0.5 ; +9 | ≤ +0.5 | +0.5 ; +1.5 | +0.5 ; +1.5 | +0.5 ; +1.5 | §3a, round 1 (+62 to +76 ms at 50 failing a night, 14 days) |

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
  marked and a muted test in E); `DefaultEdgesTest`; `DefaultTrendTest`,
  `DefaultTrendStatementsTest`, the widened plan guard and
  `NoMarksAddsNothingTest` (B.5).
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
6. **Trend: judged by today's default**, in `parts=assigned_trend`, after
   the headline. Rejected: marks only (owner, round 1); a part of its own.
7. **Q2 answers held until the environment's next push.** Rejected: 60 s.

Open: (1) for §3a, MariaDB 10.3 timings of Q2 at 400 tests, both new
members and the predicate term, and whether the Flaking queue at 200 to
500 rows meets its neighbours' cost; (2) for the owner, ESCALATE: the
headline at about +40 ms on MariaDB at 500 failing (estimate), or B.7.
(Both closed: §3a measured (1); the owner settled (2) in §4's decisions 1
and 2.)

Pushback: withdrawn, because the owner reversed decision 3 in round 1 and
the chart now applies the default (B.5).

## §3 UI (round 0, ui-engineer, base `6217c3e`)

The two disclosures leave Open Actions for a new Admin page (U1), where the
default is set (U2). A row flaking by default draws as a marked one does;
only its acts differ (U5). U12 asks §2 for four things. No browser has rendered it.

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
  no pointer to Admin (U10 11).

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
hide where the scope has no mark and no default (P3). The trend: U7a. The approximation is
said in U2's paragraph and What's new only.

### U7a The per-day chart, round 1: it waits for the default and draws once

**Today** (`static/app.js`, `6217c3e`). (1) `refreshAll()` notes `hadSplit`, empties `state.trendSplit`,
dims `#trend-card` (`is-loading`: opacity 0.55, no pointer events), and sends headline, active queue
and browse page. (2) The headline lands: `renderHeadline()` draws all but the trend; where no split
was on screen (first load, or after a failed split) `renderTrend()` draws the headline's totals,
Failed all red, under the dim; then `loadTrendSplit(seq)` asks for `parts=assigned_trend`.
(3) `fetchAssignedTrend()` gives the part, or `null` on an error, a 400 or `ASSIGNED_TREND_TIMEOUT_MS`
(10 s); the newest answer of a current generation is drawn and the dim lifted. `splitNights()` merges
by date (a night missing: totals); `trendFlaking()` adds Flaking where `flakyScope()` (`flaky_total >
0`) and every night has `FAIL_FLAKING`. (4) A failed headline lifts the dim; a Take (`fetchSummary()`)
keeps the chart undimmed and recolours it when its part lands. **So a first load draws, then
corrects** (all red, then red, blue, grey, mauve); a reload with a split on screen draws once.

**After** (U10 13: everywhere). (1) As today, but a first load shows "Loading…" (`.empty-state`, at
the chart's height) in the chart's place; `hadSplit` and the headline's `renderTrend()` go: the card
draws only in `loadTrendSplit()`. (2) **Nothing above waits**: toolbar, tiles and "All clear", both
bar cards, tab badges, the active queue's rows and the first browse page draw on their own answers,
as today; only then is the part asked for, same URL. The first batch stays three, the 10 s timeout
stays. (3) It lands: one drawing, split, with Flaking where `flakyScope()`, now
`flaky_total > 0 || flaky_defaults > 0` (decision 11), its figures counting the default's tolerated
runs (+62 to +76 ms later at 50 failing a night on both backends, +238 ; +716 to +804 at 500, 14 days: §3a). (4) A reload or new scope keeps the chart dimmed until its
part lands; a Take recolours, as today. With no default in scope the steps are the same and Flaking
follows marks; the headline's `status.flaky_defaults` settles, before the part answers, the series
and caption. Alternative: wait only where a mark or default is in scope, so a scope with neither
keeps its early all-red drawing (today's, at the headline's arrival). Not recommended: that drawing
is what the owner declined, and it keeps two paths.

```
first load            reload / new scope       drawn
Failing runs per day  Failing runs per day     Failing runs per day
  Loading…            [chart on screen, dim]   ■Unassigned ■Assigned ■Muted ■Flaking ■Unexpected pass
▸ Data table (empty)  ▸ Data table             [a column per day] Assigned and muted … Flaking: …
failed / timed out: the headline's totals, undimmed (Failed, Unexpected pass), and in the caption's
  place: "Not split: the assigned, muted and flaking runs could not be loaded, so Failed counts
  every failed run. Reload to try again."  ("flaking" only where flakyScope())
older server (no `flaky_defaults`, read as 0; or no `FAIL_FLAKING`): today's chart, today's rules
```
U10 14: that line everywhere, as muted runs drawn red already disagree with the tiles (alternative:
only where a mark or default is in scope). Flaking is drawn from the part alone, never marks-only.

**Captions**: `trendFlakingCaption(marks, defaults)` in `api.js`, from `flaky_total` and
`flaky_defaults`, replaces `FLAKING_TREND_CAPTION` after the split caption. Marks only: " Flaking: the
day's tolerated failures, by today's marks." Defaults only: "… by today's environment default." (1),
"… environment defaults." (2+). Both: "… by today's marks and environment default(s)." Neither: no
Flaking, no sentence. Round 0's "the tests flaking that night" goes: the part counts runs, per day.

**Other charts.** The bar cards draw the headline's `flaking`, judged under the default by §2:
`flakyScope()`'s widening only. Watch's "(+N flaking)" is its payload's count. Test page per-day,
script page executions: raw results, no Flaking even for a mark. Time, Timeline: none. Nothing else.

**Guards.** `AssignedTrendFetchTest.test_the_first_paint_does_not_wait_for_it`: the stricter pin,
`renderTrend()` only in `loadTrendSplit()`, planted. `TrendSplitAfterHeadlineDomTest`: three first for
marks zero, flaky, default; its reload test's first load reads the loading line, dimmed. New
`TrendDrawsOnceDomTest`: one drawing per load and reload per fixture; a failed and a rejected part
give totals, undimmed, with the line. `FlakingTrendTest`: defaults only; the four captions.

**What's new**: "**Failing runs per day counts the default too**: failures a default tolerated draw
as flaking, as everywhere else. The chart now draws once, a moment after the rest of the page, with
every part in place; if its parts cannot be loaded it draws the totals and says so."

### U8 Performance impact

| Page | First-paint requests | Payload |
|---|---|---|
| Home, build tab, browse, script page | unchanged | `source` per `flaky` (+18 B); a default's `flaky` on FAIL rows only (P2); one integer (P3); the trend draws once its part lands, +62 to +76 ms later at 50 failing a night where a default is in scope (§3a, round 1); first-paint requests unchanged |
| Test page | unchanged | `flaky` under a default in every state |
| Open Actions | unchanged | loses two lazy reads (no other user of either) and ~350 lines of `actions.js` |
| Admin (new) | 2, in parallel | `api/environments` with default and history; the roster |

Flag: a default's reason repeats on each row, ~20 KB on 200 Flaking rows.

### U9 Guards (`tests/test_frontend_calls.py`)

- **Moved**: `UserListTest`'s roster guard (to `admin.js`'s `loadPeople()`,
  reasoning rewritten); `BulkAssignUnmappedTest` (to `admin.*`, in `#envs-section`).
- **Widened**: `MetricsPageTest._PAGES`, `DropDateTest`'s nav marker and
  `ModulePreloadTest` take `admin.html`; `FlakyWordingTest`;
  `FlakingFilterTest`, `FlakingTrendTest`, `OpenActionsFlakingDomTest` (P3),
  `AssignedTrendFetchTest`, `TrendSplitAfterHeadlineDomTest`;
  `FlakyWordingTest` takes `trendFlakingCaption`.
- **New**: `TrendDrawsOnceDomTest`; `AdminPageTest` (one `admin.html` link per nav, after Metrics,
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
13. The trend draws once, everywhere. Rejected: only where a mark or default is in scope. (accepted by the owner, round 1: §4 decision 25)
14. A failed part: totals and a "Not split" line, everywhere. Rejected: silent. (accepted by the owner, round 1: §4 decision 26)

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
- **P4** Settled (decision 3 reversed): the part's `FAIL_FLAKING` applies the default; U7a.

### What's new (draft, for the drop)

- **Admin**: Manage people and Environment expectations have moved from the
  foot of Open Actions to their own page, Admin, in the navigation bar.
- **A flaky default for an environment**, set on Admin with a reason ("fail
  after 2 in a row", 2 to 10): a test there with no mark of its own counts as
  failing only once its last N runs have failed; until then it reads FLAKING,
  in the Flaking tab, as a marked test does. Its own mark wins. It reads the
  most recent runs, not one night's: one failure on each of two nights
  counts, and the test may then show under Fixed.
- **Failing runs per day counts the default too**: failures a default
  tolerated draw as flaking, as everywhere else. The chart now draws once, a
  moment after the rest of the page, with every part in place; if its parts
  cannot be loaded it draws the totals and says so.

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

**The trend.** Round 0 judged "marks only" right here; the owner reversed it (§4, decision 3).
"The trend, round 1", at the end of this section, replaces this paragraph.

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

**Verdict: proceed with the named cheaper shape, and ESCALATE the headline.** (Settled in
round 1: §4, decision 1.)
- The cheaper shape is C (run 3 on the members at cadence 2), L (no promotion read on rows
  paths) and §2's predicate with the `IS NOT NULL` fix. B.6's table is replaced by these
  measured rows.
- The owner chooses between:
  - Accepting C on Home's cold headline: +19 to +33 ms on MariaDB at 500 failing, +13 to +14 at
    200, +6 at 50. WP-44 accepted +6 to +7 at 50 marks and +13 to +15 at 1,000.
  - B.7 in a later performance drop: +11 to +18 at 500.

**The trend, round 1: judged by the default, in the trend's late part.**

*How it was measured.* Round 0's +300 ms was taken on perf-seed/1, which has seven nights, so its "14 days" was seven nights of one run per test. Round 1 used copies of perf-seed/1-h90 at migration 12, with 65 assignments (10 muted). Over all 90 nights, atlas-lab-alpha's mainline (8,000 tests) was rewritten to 50 or to 500 tests failing a night. 30% of those failed on both servers in five-night spells; 70% failed on one server, drawn from a pool four times that size. Every run was then doubled 3 h later, for §1's two servers. In 14 nights at 50, that is 1,038 FAIL runs over 204 tests; at 500, 9,286 over 1,790. The windows are 14 days, which is the only one any page asks for (Home and a build tab send no `days`), and 90, the API's ceiling, which nothing sends. Cadences 2 and 3 were measured on SQLite 3.50 and on a local MariaDB 12.3 (128 MB buffer pool), not production's 10.3. Method: scratch subclasses of `6217c3e` run through the real handler, cold, in-process, with arms A/A2/B rotated per call, 16 calls (6 to 8 at 90 days on MariaDB). The A/A noise at 14 days was ±1 ms on SQLite and ±1 ms on MariaDB at 50 (−7 to −11 at 500); at 90 days it was ±4 to ±7 on SQLite and ±100 to ±160 on MariaDB. Every shape returned the same nights as the oracle (`flaking_that_night` over every run) at cadences 2, 3 and 5, on mainline and on a build.

| `parts=assigned_trend`, cold B−A, ms (SQLite ; MariaDB 12.3) | 50 failing a night | 500 failing a night |
|---|---|---|
| §2 B.5 as written (every run of E's tests into Python), 14 d | +1,010 ; +2,224 (runs read by full scan) | +1,045 ; not run |
| same, 90 d | +5,940 ; not run | +5,986 ; not run |
| **Recommended shape (below), 14 d, cadence 2** | **+62 to +64 ; +67 to +70** | **+238 ; +716 to +735** |
| same, 14 d, cadence 3 | +66 ; +76 | +269 ; +804 |
| same, a build tab in E (stream 2), 14 d | +67 ; +67 | +94 ; +76 |
| same, 90 d, cadence 2 ; cadence 3 | +285 to +289 ; +290 SQLite, +1,800 ; +1,930 MariaDB | +1,377 ; +1,586 SQLite, +8,849 MariaDB (c2) |
| Scope with no default, default elsewhere (bravo) | +0.04 ; +0.55 | the same |
| No default anywhere | 0 statements, identical text | the same |

Scoped to E, to its product, or unscoped: these cost the same (Atlas 14 d +64 ; +66), because the read is bounded by the defaulted environments in scope.

*The cheapest read-time shape.* One statement per defaulted E in scope, with the result aggregated in the engine:
`SELECT day, assigned, COUNT(*) FROM (SELECT f.id FROM runs AS f WHERE f.start_time >= ? AND f.result = 'FAIL') AS k CROSS JOIN runs AS r ON r.id = k.id LEFT JOIN marks / mutes on S / assignments WHERE r.environment = E AND r.stream_id = S AND no mark AND no live mute AND (COALESCE((SELECT p.result FROM runs AS p WHERE <triple, stream> AND p.start_time < r.start_time [AND >= floor] ORDER BY p.start_time DESC LIMIT 1 OFFSET k), 'FAIL') <> 'FAIL' OR …) GROUP BY day, assigned`.
- **The OR has N−1 terms** (k = 0 … N−2). A FAIL run is tolerated when any of its N−1 previous runs on the stream did not fail; with fewer runs than that, all FAIL, it is failing. That is `flaky_state`'s rule.
- **What it reads.** The window of the estate's `(start_time, result)` index, covering: the row id is in the entry on both engines. It reads E's FAIL rows by primary key, and for each of those, up to N−1 backward steps of the runs UNIQUE index.
- **What it does not do.** No Python per run, at most 2 × days rows back, and no Q2 (the backward steps reach before the window).
- **What it costs.** Roughly, and fitted at 500 failing a night: about 60 ms for the range, plus about 23 µs per FAIL run on SQLite and about 73 µs on MariaDB; the table's measured rows are the figures to hold to. It grows with failing runs in the window, not with E's passing tests.
- **Plans.** SQLite 3.50: `f COVERING INDEX idx_runs_start_time_result (start_time>?)`, then `r INTEGER PRIMARY KEY`, then `CORRELATED SCALAR SUBQUERY` on `sqlite_autoindex_runs_1 (environment=? AND script=? AND test_name=? AND start_time<?)`. MariaDB 12.3: `f range idx_runs_start_time_result, Using index`, then `r eq_ref PRIMARY`, then `DEPENDENT SUBQUERY p ref uq_runs_identity`. Both are the same at 14 and at 90 days.

*Measured no, all against the same oracle:*
- **Window frames** (`SUM … OVER ROWS N−1 PRECEDING`) over E's runs: +1,139 SQLite. Over only the failing tests' runs: +629 SQLite at 500, and on MariaDB a full scan of runs.
- **E's FAIL rows through the runs UNIQUE prefix:** SQLite reads all of E's history, +640.
- **Hiding the environment from that index with a CASE:** SQLite takes the time range, but MariaDB takes a full scan, and an `EXISTS` judgement becomes a semi-join; killed after 200 s at 90 days.
- **A subquery nested in the judging subquery:** MariaDB +12.5 s at 14 days, cadence 3.
- **§2's `script_hours` narrowing (E's scripts with a FAIL in the window):** 3,938 of 8,000 tests at 50 and 7,926 at 500. It at most halves §2's +1,010 and does nothing at 500. The key-driven visit of E's runs, with nothing returned, costs 390 ms on SQLite, so no shape driven from E's tests can come under it.

*Where it lands, and what it holds up.*
- **Only in `parts=assigned_trend`**, which the page sends after the headline is drawn.
- **First paint gains nothing.** Statement counts and SQL text are identical with and without the read, cold, at 500. Each first-paint request measured: headline 40, Atlas headline 39, queue New 16, Still 22, Flaking 15, Mine 17, names 15, full summary (Open Actions) 53, browse 4. These are round 0's accepted figures, against 37/36/14/20/12/16/14/50/3 at `6217c3e`.
- **The part's own statements.** Cold with a default in scope it goes from 2 to 5: the mute epoch, the split, the marks statement, the map and this read. The marks statement runs only because the "any mark" bit now also means "any default". Split the bit so it runs only where a mark exists; a scope whose default is elsewhere then pays the map alone, which is memoized by the headline.
- **No lock is held while it reads.** `_summary_lock` guards only the dict, and there is no single-flight. It takes one worker of 8 and one connection, and the work is in the engine: SQLite releases the GIL in `step`, and on MariaDB the work is in the server.
- **A cold headline next to it, measured with the trend read in a loop on another thread:** SQLite 37 → 41 ms at 50 and 54 → 61 at 500; MariaDB 55–60 → 65 at 50. Next to §2's read the same headline goes to 44 / 68 / 82. Idle blocks before and after match.
- **MariaDB's buffer pool.** A read of runs this large evicts pages on a 128 MB pool: the next cold base call read 119 ms against 7 after §2's shape. The recommended shape reads only the covering range and the FAIL rows.

*Memo.* One entry per (S, E), keyed `(since, cadence, cadence_at, mute epoch)`, tagged `(S, E)`, under the usual 60 s TTL and the usual drops: a push into (S, E), any act, a mute expiry. **No hold until the next push.** At the owner's typical night the read is 62 to 76 ms, and a hold past acts would need a new invalidation path outside the summary memo. Stated cost: every Take re-reads it, about 0.73 s of MariaDB time at 500, off the critical path.

*The bar for the build's measured pass* (cold, against `6217c3e`, with a default of 2 in alpha, SQLite ; MariaDB):

| Read | 50 failing | 500 failing |
|---|---|---|
| `assigned_trend`, 14 d, E / product / unscoped | ≤ +70 ; ≤ +80 | ≤ +250 ; ≤ +750 (accepted by the owner: decision 24) |
| the same, cadence 3 | ≤ +70 ; ≤ +85 | ≤ +280 ; ≤ +820 |
| the same, 90 d (no page sends it) | ≤ +300 ; ≤ +1,900 | ≤ +1,400 ; ≤ +8,900 |
| Every first-paint request | statements and text equal to the build without the read | the same |
| Default only outside the scope ; none anywhere | +1 statement cold (the map), 0 after the headline ; 0 | the same |

- **Fixture:** `seed.py --history-nights 90`, then `--default-failing N --two-servers` (to add: round 1's `build.py` mix, with the activity tables rebuilt).
- **Plan guards:** the guard asserts the covering range first and the PK join on SQLite. MariaDB **10.3** gets a hand `EXPLAIN` and a timing, with the 12.3 figures above as the reference.

**Verdict: ESCALATE at 500.** At 50, the owner's typical night, the chart draws 62 to 76 ms later on both backends, well inside the accepted +300. At 500 on MariaDB it draws +716 to +804 ms later, 2.4 to 2.7 times the accepted figure. It still holds nothing else up and stays far inside the page's 10 s give-up. No read-time shape found comes near 300 there, because the cost is the backward steps per FAIL run. A per-run flag written at push (decision 2's family) is the only route found to a 14-day figure independent of failures at 500. At 90 days the read reaches 9.9 s total on MariaDB; no page asks for that window.

*The part as it is today, with no mark and no default in scope* (the wait §4's decision 25
adds before such a scope's chart appears, with one round trip): unscoped with 65 assignments,
14 to 16 ms on SQLite and 7 to 8 on MariaDB; an environment with no assignments, 0.08 ; 0.5.

**Accepted by the owner in round 1** (§4, decision 24): the chart is built in this shape, at
these figures.

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
   The alternative is decision 2. (§3a's table and bar) — verdict: **accepted** (round 1).
2. **A streak kept on each test's latest-run row at push time is not built
   in this drop**, and is offered for the performance-centred drop after
   it. It would bring the headline at 500 to +11 to +14 ; +11 to +18 ms.
   Its costs: the server stops for the upgrade (a backfill measured at 20 s
   on the seed's MariaDB copy), every out-of-order push reads up to twelve
   runs where it reads one, and a second, derived copy of every streak has
   to be kept equal to the runs by pushes, re-imports and deletes, written
   just before WP-44 ships. (§2 B.7, §3a "B.7, honestly") — verdict: **accepted** (round 1).
3. **The per-day chart applies the default, as every other surface does.**
   Round 0 recommended that it count hand-marked tests only; the owner
   reversed that ("Chart must agree"). The chart's late part judges each
   night it draws by today's default, the chart waits for that part and
   draws once, and nothing else on the page waits for it. §0's "every
   surface that shows flaking agrees" stands with no exception. Its cost
   is decision 24. (§2 B.5, §3 U7a, §3a "The trend, round 1") — reversed
   by the owner in round 1; as now written, **accepted**.
4. **A default may be 2 to 10, as §0 says, and the bar above is for 2.**
   The cheaper shape works because at 2 a test's state needs the two
   results its latest-run row already holds, plus one earlier run that
   a statement already running can fetch beside them. A default of 3 or more reads
   each failing test's recent runs as §2 first wrote it: +37 to +42 ; +50
   to +58 ms at 500. The build measures a default of 3 and reports it; the
   alternative is to offer only "2 in a row" as a default in this drop.
   (§3a, cheaper shape 1) — verdict: **accepted** (round 1).

**How it is built.**

5. Judged when a page is read, by WP-44's machinery: the environment's
   failing tests and its fix candidates ride statements that already run;
   the third run rides them at a default of 2; lists read no promotion;
   "flaking by default" is a condition on the latest-run rows, with §3a's
   correction so that a test failing on its first run stays a failure.
   The push path gains nothing. For a default of 3 or more, the one
   case that still reads each failing test's recent runs, that read is
   kept until the environment's next push rather than for a minute.
   (§2 B.1 and B.9 7, §3a) — verdict: **accepted** (round 1).
6. Two new tables, migration 13, create-only: the server keeps serving
   through the upgrade. The parked in-run-progress work moves to 14. Not a
   column on an existing table. (§2 B.2) — verdict: **accepted** (round 1).
7. A reason is required to set, change and remove a default; the signed-in
   user is the one named. (§2 B.9 4, §3 U10 2) — verdict: **accepted** (round 1).
8. Every change is recorded; the newest twenty per environment are shown
   in the cell's History, with no request of their own. (§2 B.3, §3 U2) —
   verdict: **accepted** (round 1).
9. Setting, changing or removing a default re-judges every test, and
   every night the per-day chart draws, at once.
   Setting or changing one never makes a New failure; editing only the
   reason moves nothing. Removing one returns its flaking tests to what they would be
   with no default: a failure after a pass is a New failure that morning.
   (§2 B.4) — verdict: **accepted** (round 1).
10. A hand mark wins whole (cadence, reason, owner); unmarking a test
    under a default returns it to the default. (§2 B.1) — verdict: **accepted** (round 1).
11. The Flaking tab's badge and the FLAKING filter count a test flaking by
    default as they count a marked one. `flaky_total` still counts marks;
    a new `status.flaky_defaults` counts environments with a default, and
    the FLAKING filter and the charts' flaking series show where either
    exists. (§2 B.3 P3, B.4) — verdict: **accepted** (round 1).
12. A list row carries a default's details only where the test is failing;
    the test's own page carries them in every state. Rows repeat the
    default's reason (about 20 KB on 200 rows before compression). (§2
    B.3) — verdict: **accepted** (round 1).
13. A default belongs to an environment by name: a renamed environment
    starts with none, and a deleted one takes its default and history
    with it, as marks do. (§2 B.4) — verdict: **accepted** (round 1).

**What the developer sees.**

14. The page is "Admin", after Metrics and before What's new in the
    navigation. Rejected: "Settings"; beside Open actions. (§3 U1) —
    verdict: **accepted** (round 1).
15. Both sections move, open rather than folded, Environments first;
    nothing else joins them now (a build's delete stays on Home, expiring
    mutes stay on Open Actions). (§3 U10 10) — verdict: **accepted** (round 1).
16. Open Actions keeps no link to Admin; What's new says where the
    sections went. (§3 U10 11) — verdict: **accepted** (round 1).
17. No product filter on Admin: a few dozen rows. (§3 U10 12) — verdict: **accepted** (round 1).
18. The setting is the environments table's last column, edited in a row
    beneath it, with the select a mark uses ("fail after…"). (§3 U2) —
    verdict: **accepted** (round 1).
19. No "flaky" pill for a default, on a passing or a failing row: the
    FLAKING chip alone. The one quiet place that says an environment has a
    default is a test's own page. (§3 U4, U10 4 and 7) — verdict: **accepted** (round 1).
20. Sentences say "the environment's default", not the environment's
    name. (§3 U3) — verdict: **accepted** (round 1).
21. Under a default a row offers "Mark as flaky" (which gives the test a
    mark of its own) and a link, "Change the default", to the Admin page's
    row. Set cadence and Unmark are not offered: there is no mark. This is
    the one new difference between the Flaking tab and its neighbours.
    (§3 U5, U6) — verdict: **accepted** (round 1).
22. The Flaking tab is otherwise unchanged (§5, question 1). (§3 U6) —
    verdict: **accepted** (round 1).
23. The approximation (the N most recent runs, not one night's) is said in
    the Admin page's paragraph and in What's new, nowhere else. (§3 U2,
    U7) — verdict: **accepted** (round 1).

**Round 1: the per-day chart.** Put to the owner after the round's
measurements; each answer is in "Round 1 review".

24. **The chart's cost is accepted as measured.** Cold, on the chart's late
    part only, at a 14-day window with a default of 2 (SQLite ; a local
    MariaDB 12.3): +62 to +64 ; +67 to +70 ms at 50 failing tests a
    night, the owner's typical night, and +238 ; +716 to +735 at 500. A
    default of 3 costs +66 ; +76 and +269 ; +804. No first-paint request
    gains a statement. The figure first accepted, about +300, was round
    0's and is exceeded at 500 on MariaDB; the owner accepted the
    measured figures. A per-run flag written at push time, the one route
    found to a cost that does not grow with failures, stays a candidate
    for the performance-centred drop (decision 2). (§3a "The trend,
    round 1") — verdict: **accepted** (round 1).
25. **The chart waits for its late part and draws once, on every scope.**
    A first load shows the loading line at the chart's height; the early
    drawing from the headline's totals goes. A scope with no mark and no
    default sees its chart 7 to 16 ms and one round trip later than
    today (the part as it is today: §3a "The trend, round 1"). Rejected: waiting only where a mark or a default is in scope.
    (§3 U7a, U10 13) — verdict: **accepted** (round 1).
26. **A late part that fails or times out draws the headline's totals and
    says so**, on every scope: "Not split: the assigned, muted and flaking
    runs could not be loaded, so Failed counts every failed run. Reload to
    try again." (It names flaking only where a mark or a default is in
    scope.) Flaking is drawn from the part alone, never from marks only. Rejected: saying so only where a mark or a default is in scope;
    no chart at all. (§3 U7a, U10 14) — verdict: **accepted** (round 1).
27. **A past night is judged by today's default**, as WP-44 judges past
    nights by today's marks; the caption says so. Rejected: each night's
    own default, from the history. (§2 B.5) — verdict: **accepted**
    (round 1).

## §5 Open questions

Answered in round 1 ("Round 1 review"): 1, as drawn; 2, no; 3, left as
history; 4, nearer 50; 5 needed no answer and is in §3a's bars.

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

## §6 The build (sizing pass, 2026-11-19)

Seven items, in order, on branch `wp-44-flaky-mark`, numbered on from
WP-44's twenty. Each is one brief, verified on both backends, reviewed and
committed by itself. "Trigger" says whether the item is on the performance
trigger paths, and so what its measured pass is held to.

| Item | Layer | What | Trigger | Gated on |
|---|---|---|---|---|
| 21 | performance | The fixture both bars need, in `tools/dev/perf/`: 90 nights of history, an environment with N tests failing a night, two servers (§3a, both bars) | no: a dev tool | nothing |
| 22 | backend | The default kept and recorded: migration 13 with its MariaDB ledger step and the registry line (the parked in-run-progress work moves to 14), the two tables, `PUT /api/environments/{environment}/flaky-default`, `flaky_default` and `flaky_default_history` on `GET /api/environments`; the tests §2 B.8 names for this part (§2 B.2, B.3, B.8; decisions 6 to 8, 13) | yes (`storage.py`, `api.py`): no read that a page makes on first paint changes, shown by statement counts | nothing |
| 23 | backend | The default judged: every read that judges a mark judges a default, in §3a's cheaper shape with its correction; `flaky.source`; the counts and `status.flaky_defaults`; what a change re-judges; the tests §2 B.8 names for this part (§2 B.1, B.3, B.4, B.8; decisions 1, 4, 5, 9 to 12) | yes: measured against §3a's first bar, a default of 3 at 500 failing included | 21, 22 |
| 24 | backend | The per-day chart's late part applies the default (§2 B.5, §3a "The trend, round 1"; decisions 3, 24, 27) | yes: measured against §3a's round 1 bar; MariaDB 10.3 by hand | 21, 23 |
| 25 | UI | The Admin page: the two sections moved off Open Actions, the navigation on every page, the Flaky default column with its editor and history; the guards §3 U9 names for this part; What's new: where the sections went, and that a default is set there (§3 U1, U2, U8, U9; decisions 7, 8, 14 to 18, 23) | a new page, and Open Actions loses two lazy fetches: walked, with the request counts of both pages stated | 22's contract to start; 22 built for the walk |
| 26 | UI | A row flaking by default: the wording helpers and sentences, the acts ("Mark as flaky", "Change the default"), no pill, the test's own page, the FLAKING filter and the bar charts' flaking series, with `flakyScope()` widened to a default (this item alone edits it); the guards §3 U9 names for this part; What's new: what a default does to a row (§3 U3 to U7, U8, U9; decisions 11, 19 to 22) | Home's first paint: no request added, pinned by test; walked | 23 |
| 27 | UI | The per-day chart waits and draws once: the loading line, the four captions, the "Not split" line; it calls `flakyScope()` and does not change it; U7a's guards; What's new: U7a's line (§3 U7a; decisions 25 to 27) | Home's trend card: first-paint requests unchanged, pinned by test; walked | nothing to start; 24 built for the walk |

Order of work: 21 and 22 start together, and 25 starts on 22's contract;
23 follows 22, and 24 follows 23; 26 follows 23 and 27 follows 24. The
backend items and the UI items run side by side, each in its own worktree.
"Walked" means read surface by surface by the reviewer on a local server;
the chart's "late part" is the one request Home sends after its headline
is drawn (`parts=assigned_trend`).
The owner's choice (2026-11-19): the plan runs unattended through
`/overnight`, aiming at a drop dated 2026-11-20.

Not items, and the main session's at the drop: the operator note
(migration 13: yes, create-only, the server keeps serving), the README's
API section, the pull request's body, the handover.

Status strip, ticked by the run as each item is committed; a parked item
says why:

- [x] 21 the fixture for both bars
- [ ] 22 the default kept and recorded (migration 13)
- [ ] 23 the default judged
- [ ] 24 the per-day chart's late part
- [x] 25 the Admin page
- [ ] 26 a row flaking by default
- [ ] 27 the per-day chart waits and draws once

Done when §0's "Done means" holds with the chart included, both of §3a's
bars are met or each excess has been taken to the owner, and the owner has
clicked through a play server that has an environment with a default.

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

**Second sitting, the same day, after the round's sections were back.**
Four more were put the same way. The chart's cost as measured (+62 to
+76 ms at about 50 failing tests a night on both backends; about +240 on
SQLite and +716 to +804 on MariaDB at 500; only the chart waits):
**"Accept (Recommended)"**. Where the chart waits for its split and
draws once: **"Everywhere (Recommended)"**. What a split that fails or
times out shows: **"Totals, saying so (Recommended)"**. Which default
judges a past night: **"Today's default (Recommended)"**. These are §4's
decisions 24 to 27. Read as: four decisions, all as recommended; the
second widens the first sitting's "where the chart's scope has a
default" to every scope. No decision is open.
