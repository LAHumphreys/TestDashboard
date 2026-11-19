# tools/dev/perf: the A/B instrument

Development-only. Nothing here is deployed or imported by the server.

## From a clean checkout to a number

```
python tools/dev/perf/seed.py --out <scratch>/perf.db            # ~75 s, ~95 MB
python tools/dev/perf/ab.py --base <base-sha> --head <head-sha> --db <scratch>/perf.db
```

- For a request that reads a window of `runs`, measure on the variant
  `perf-seed/1-h90` as well (below): `--history-nights 90`, about 8.5 min.
- For the flaky default's two bars, the fixtures of "The flaky default's
  fixtures" below (`--default-failing N`, `--flaky-default C`).
- `<scratch>` is a temp directory. `seed.py` refuses to write anywhere inside
  a checkout: the seed is scratch output. `ab.py` refuses only the repo-root
  `testboard.db` (dev data at about a quarter of production, which current
  code migrates on open); any other `--db` is read and copied, never written.
- Build the seed with the **base** tree (`--tree <base checkout>`, default:
  the checkout `seed.py` is in). Code refuses a database newer than itself;
  newer code migrates its own copy.
- `--head-tree <path>` instead of `--head` measures a checkout as it stands,
  uncommitted edits included (an implementer's worktree). `--base-tree`
  likewise.
- `--pages NAME=/api/...` adds rows; `{product}`, `{environment}` and
  `{build}` in a URL are filled from the seed. `--no-known` skips the Known
  slow rows.
- `--methods summary_rollup,dashboard_count` adds statements per call for
  those `Storage` methods, summed over every row's cold call.
- `--calls N` (default 24) alternated calls per side per row; `--json OUT`
  keeps the raw samples; `--keep` keeps the scratch directory.
- `--assign N` measures the `--pages` rows after the **assignments
  fixture** (below) is written through each tree's own `Storage`: 65 is
  the reference estate's figure, 1000 the stress. The Known slow rows are
  measured first, in their own states, whatever N is.
- `--marks N` measures the `--pages` rows and `--page-mode` pages after the
  **marks fixture** (below), written after the assignments fixture when
  both are given: 0, 50 and 1000 are the sizes the design's figures were
  taken at. N > 0 needs both trees at migration 12 (WP-44 item 3 or
  later); on an older tree it refuses before measuring anything, and
  `--marks 0` runs anywhere.
- `--page-mode open-actions home` times each page's **first paint** on
  `--workers` (default 2) long-lived worker threads (below).

A run of the six Known slow rows at 24 calls takes about 45 s.

### A measured pass with marks (WP-44's 6p and 8p)

```
python tools/dev/perf/ab.py --base <sha> --head <sha> --db <temp>/perf.db \
    --assign 65 --marks 50 --page-mode open-actions home \
    --pages "headline=/api/summary?parts=headline&assignee=amy" ...
```

Run it at `--marks 0`, `50` and `1000`, on perf-seed/1 and on `-h90`.
`--assign 65 --marks N` is the design-time fixture's shape (its figures
were taken on top of the 65 assignments and 10 mutes): read the build's
numbers against the design's at that composition.

## What the runner does

1. `git archive <sha> testboard` into a temp directory per side (no worktree
   is registered; the same SHA twice gives two separate copies).
2. Imports both packages into **one process** under separate module objects,
   swapping `sys.modules` between calls so a lazy import inside a function
   resolves to its own tree.
3. Copies `--db` once per side; `--db` itself is never opened for writing.
4. Per row: one discarded call each, then N calls per side **alternated**
   (A,B then B,A), each preceded by clearing every storage memo (the two
   invalidators a push calls, then `_cadence_runs_cache` by name when the
   tree has it, since no act's clear drops it, then any
   `_*_cache`/`_*_memo` dict). The SQLite page cache of the connection is
   kept, as a worker's is.
5. One more cold call per side under a statement trace, for the statement
   count.

Never over HTTP: on the development machine an unchanged request varies two
to three times between runs.

### The page mode (`--page-mode`)

What a page **waits for**, which no single row shows: Open Actions
(`names`, `rows`) and Home (`headline`, `assigned_trend`, `queue` for New
failures, `browse`), the requests each page sends at its defaults
(`FIRST_PAINT` in `ab.py`, from `static/actions.js` and `static/app.js`).

- One pool of `--workers` threads, started once and kept for the run, as
  the server's fixed pool is; each keeps its own connection per copy, so
  its page cache is a worker's. Every worker opens its connection to both
  copies before anything is timed (one setup job per worker, held at a
  barrier so none takes two), then three warm-up loads per side.
- A request that raises in a worker fails the run, naming the request
  and carrying the exception's text; no page is reported with a hole in
  it. A non-200 answer is reported as the row's status, as a row's is.
- Per load: memos cleared **once** (a page's requests share memos, as in
  the server), every request put on the pool's queue at the same moment
  in the page's order, the load over when the last one answers. A and B
  alternate per load; `--calls` loads per side.
- The row is the page's time (its last answer); its band and verdict are a
  row's. Below it, per side, what the page waits for (the request with
  the latest median finish, and in how many loads it finished last) and
  every request's median finish. Statements: the page's requests run once
  in order on one thread, cold once (in the pool, which request fills a
  shared memo is a race).
- Two workers, the default, is fewer than Home's four requests, so two
  of them wait in the queue and that wait is part of the page's time,
  as it is for a page sharing the pool with other users. `--workers 4`
  gives each request its own worker and takes the queueing out.

## Reading the table

| Column | Meaning |
|---|---|
| A ms, B ms | median of the N cold calls |
| B-A ms | median of the N paired differences (each pair ran back to back) |
| 95% band | order-statistic interval for that median: the **noise band** of this row on this run |
| stmts A/B | SQL statements one cold call executes |
| verdict | `SLOWER`/`faster` only when the band is clear of zero **and** the delta exceeds max(1 ms, 5% of A); otherwise `same`. `MORE SQL` is appended whenever B executes more statements, whatever the milliseconds say |

An A/A with the marks fixture (2026-11-11, `--assign 65 --marks 50` and
`1000`, 24 calls, four rows and both pages, a busy machine: the headline
at 81 to 96 ms): every verdict `same`; the widest half-band 3.4 and 6.9 ms
on perf-seed/1, 9.0 and 11.9 ms on `-h90` (Open Actions' rows and page,
whose bands are lopsided there: [-5.4, +13.5], [-3.9, +19.9]). Home waited
for the headline in every load, Open Actions for its rows.

A row that reads `runs` (Open Actions' streaks, for one) costs more on
`-h90` than on perf-seed/1: the Known slow bar is perf-seed/1's, so quote
the seed with every number. An A/A on `-h90` (2026-10-13, 24 calls) put
Open Actions' rows at 154 ms against perf-seed/1's 97, and the widest
half-band at 18 ms (the Watch row).

The last line gives the widest half-band of the run. An A/A run (same SHA
both sides) is the noise floor: on this machine every verdict was `same`, the
largest |B-A| 5.4 ms, the widest half-band 14 ms (on the 250-300 ms rows).

## The Known slow rows

These are the requests behind the handover's "Known slow, measured, not
changed" table. `/perf-ab` measures them on every run, whether or not the
change claims to touch them.

| Row | Request | Why it is slow |
|---|---|---|
| watch 3 cards (incl. full build) | `/api/watch` with a product, an environment and the full-size build | `compare_counts_many` fetches every pair to classify in Python |
| compare full build, one category | `/api/compare?stream=<full build>&category=new_failures` | the pairs query runs twice (counts, then the page), pinned at two |
| open actions rows | `/api/dashboard?open=1&with_comment=1&with_streak=1`, 100 rows | never memoized; streaks per row |
| home headline | `/api/summary?parts=headline&assignee=amy` | the per-environment pass over `latest_runs` |
| browse page, no assignments / 65 assignments | `/api/dashboard`, 250 rows, before and after 65 of the largest environment's failures are assigned | the count costs more once assignments exist; the second row is measured after the runner assigns them through each tree's own `Storage` |

## The seed (recipe `perf-seed/1`)

- Product **Atlas**: five environments, 8,000 / 1,600 / 1,000 / 800 / 600
  tests (12,000), run one after another each night. Product **Beacon**: two
  environments, 800 tests, so a product scope and an unscoped request differ.
- Seven mainline nights ending 2026-09-28; behaviours drawn per test (stable,
  broken from a night on, fixed from a night on, flaky, expected failure).
- **nightly/full**: every Atlas test again on each of the last five nights,
  twelve hours after mainline, mostly agreeing with it. Three smaller builds
  (a branch over 60% of the largest environment for three nights, two
  releases over 70% of the second).
- About 170 comments on the last night's failures; **no assignments** (the
  runner adds those as a measured state).
- 166,240 runs, 31,840 `latest_runs` rows. Deterministic: same arguments and
  tree, same rows (`fingerprint()`; `tests/test_dev_perf.py` builds it twice).

**What it is not.** Production keeps a year (about 4.4M runs); this has a
week. Every Known slow row reads the derived tables, which are at production
scale; a request that reads a window of `runs` is not, and a number for one
must say so. SQLite only: the MariaDB legs are for correctness, not timing.

## The variant with history (recipe `perf-seed/1-h90`)

For a request that reads a window of `runs` (a trend split, a timeline,
flakiness over 90 days):

```
python tools/dev/perf/seed.py --out <temp>/testboard-perf/perf-h90.db --history-nights 90
```

- perf-seed/1 exactly, plus 83 older nights of **mainline** for the
  largest environment (`atlas-lab-alpha`, 8,000 tests) written before it,
  so that environment has 90 nights and every other environment its seven.
  The history draws from its own random stream: the last seven nights are
  perf-seed/1's runs, row for row (`tests/test_dev_perf.py` checks it).
- Every seven history nights the behaviours are redrawn with the recipe's
  mix (broken and fixed tests turning inside the week, flaky, expected), so
  the 90 nights carry the week's flips and failure rate, not one static
  week repeated.
- **Mainline only.** The builds stay as perf-seed/1 has them: the full-size
  build on the last five nights, the three small ones. A nightly full build
  over 90 nights would put about 5.8M runs a year on one environment, more
  than the reference estate the harness is built to carries in a year
  across all of it (about 4.4M); mainline alone matches that scale. A
  statement whose cost grows with the streams a triple has run on is
  measured here at that density; a release-heavy product would pay more,
  by the share of its runs that are builds.
- Expect **about 8.5 minutes** and **about 390 MB** on the development
  machine (SQLite, `--tree` the base checkout): 830,240 runs, 31,840
  `latest_runs` rows.
  It lives outside every checkout, conventionally in
  `<temp>/testboard-perf/`; rebuild it when the base's schema moves.

### The assignments fixture

N current assignments in the largest environment, all to `amy`, of which
10 are live mutes on mainline (a week from the seed's clock). Order: the
environment's current mainline failures by (script, test), so the first 65
are exactly the Known slow row's `assign_65`; then tests that failed on
mainline before, most recent first (thousands on `-h90`, so 1,000 are all
tests that have failed in the window); then the rest. The 10 mutes are
spread evenly over the selection's current failures.

- `ab.py ... --assign 65|1000 --pages ...`: on each side's copy, the
  normal route. The seed file is never written.
- `seed.py --onto <a copy of the seed> --assignments 65|1000`: into a copy,
  once (it refuses a copy that already has assignments). For a database
  that has to carry the state outside the runner, such as the MariaDB copy
  the migration tool loads for a by-hand `EXPLAIN`.

### The marks fixture (WP-44)

N flaky marks on the largest environment (`atlas-lab-alpha`), written
through the tree's own `Storage.mark_flaky` (one call per cadence, by amy,
reason `perf fixture: accepted as flaky`), never by a raw INSERT: each mark
leaves its history row and its comment, as a person's would. Deterministic:
the same seed gives the same marks, cadences and rows (`fixture_digest()`).

- **When.** The marks, their history rows and their comments are dated an
  hour before the hour of the environment's first latest run on any stream
  (`marks_clock()`: 2026-09-27T23:00 on both seeds), so every stream's
  latest run follows every mark, as a night's runs follow a mark set in
  the day. The product promotes a test only on a run that started at or
  after its cadence was last set; marks dated at the seed's clock, after
  every run, leave the product's P at 0 whatever the census says, and the
  promoted paths unmeasured. The companions are written at the seed's
  clock.

- **Which tests.** The design-time order (`docs/design/flaky-mark-r0.md`
  §2 B.5's script): failing on mainline now first, then by mainline
  failures over the whole history, most first, then by (script, test).
  Three **fillers** are moved to the front so every state of the rule is
  marked from N = 3: the first test passing after a pass, the first
  passing after a tolerated failure, the first real fix. At 50 they take
  the places of the design's last three plainly failing tests (failing,
  not promoted), which follow at 51 to 53; on both seeds the 1,000 marks
  are the design's 1,000.
- **Cadences.** The design's cycle by a test's place in that order, 3, 3,
  2, 5, 3, 4, 10, 3, except that its 10-slot takes 10, 6, 7, 8 and 9 in
  turn, so every cadence from 2 to 10 is marked (the rotation changes none
  of F, X or P on either seed, checked).
- **Companions**, written at **every** N, 0 included, so 0, 50 and 1,000
  differ only in the marks: the first flaking test of the first 50 that
  the reference assignments fixture (65) mutes is muted (and so assigned,
  a mute being owned); the next flaking one it assigns is assigned; the
  first two plainly failing ones are retired. Because they are the
  reference fixture's own, `--assign 65 --marks N` adds no assignment or
  mute to it. Below 50 a companion may not be among the marks.
- **The census** (`mark_census()`, printed by `ab.py` and `seed.py
  --onto`) is the harness's own reading of the rule over the copy's
  tables, independent of the product's: what a measured pass holds the
  build's counts against. It reproduces the design script's F, X and P
  exactly on the design's own marks.

What it holds, mainline (`--assign 65 --marks N`, the design's
composition; the marks fixture alone in brackets where it differs, as
`seed.py --onto <copy> --marks N` writes it). At 0 the tables are empty
and only the companions are written.

**"Alone" in `ab.py` needs `--no-known`.** With the Known slow rows on
(the default), the `browse page, 65 assignments` row's state writes 65
assignments into both copies before the fixture state, so the `--pages`
rows of `ab.py --marks N` without `--assign` are measured on the 65
assignments and the marks, not the marks alone. Only `--no-known`
measures the marks fixture alone in `ab.py`.

| perf-seed/1 | 0 | 50 | 1,000 |
|---|---|---|---|
| marks, `flaky_total` (2 retired) | 0 | 50, 48 | 1,000, 998 |
| passing (candidates, real fixes) | | 3 (2, 1) | 899 (27, 4) |
| flaking: F, X | | 13: 11, 7 (12, 11) | 62: 56, 22 (61, 60) |
| failing; P | | 34; 8 | 39; 9 (10) |
| marked and muted, assigned | | 5, 29 (1, 2) | 10, 65 (1, 2) |

| perf-seed/1-h90 | 0 | 50 | 1,000 |
|---|---|---|---|
| marks, `flaky_total` (2 retired) | 0 | 50, 48 | 1,000, 998 |
| passing (candidates, real fixes) | | 3 (2, 1) | 899 (8, 3) |
| flaking: F, X | | 13: 11, 6 (12, 11) | 63: 57, 21 (62, 61) |
| failing; P | | 34; 7 (8) | 38; 7 (8) |
| marked and muted, assigned | | 6, 30 (1, 2) | 10, 65 (1, 2) |

Failing with fewer than N runs, every one a FAIL, is a build's state:
`nightly/full` (five nights) has 3 at either size, the branch (three nights)
9 to 11. Mainline has none (seven nights or more, and no cadence-10 test
failing all seven among the marks).

**Against the design-time fixture** (§2 B.5: 50 marks F 11, X 7, P 8; 1,000
F 56, X 22, P 9 on perf-seed/1; -h90 F 11, X 6, P 7 and F 57, X 21, P 7):
F, X and P are the same at both sizes on both seeds. It differs in four
ways, each on purpose. `flaky_total` counts every mark in scope, the
"Newly fixed" candidates included (the design's script left them out: 973
of 1,000; the design asks for this). At 50 the three fillers replace three
plainly failing marks, so the 50 hold a passing test and two candidates,
where the design's held none. Two plainly failing marked tests are retired
(in no count, so `flaky_total` is 48 and 998). Cadences 6 to 9 appear.
And each mark leaves a history row and a comment, as the product writes
them.

- `ab.py ... --marks 0|50|1000 [--assign 65] --pages ...`: on each side's
  copy, through that tree's `Storage`; the two sides must agree, in what
  was written and in the census, or the run stops.
- `seed.py --onto <a copy> [--assignments 65] --marks 50 --tree
  <checkout at migration 12 or later>`: into a copy, once (it refuses a
  copy that already has marks), and prints the census.

**By hand on MariaDB** (6p, 8p): the fixture goes into the SQLite copy,
and the migration tool carries those rows over, so MariaDB holds the
fixture exactly and nothing is written to it by hand. Into a sacrificial
database only:

```
cp <temp>/perf.db <temp>/mdb.db
python tools/dev/perf/seed.py --onto <temp>/mdb.db --assignments 65 --marks 50 --tree <checkout>
python tools/migrate_to_mariadb.py export --db <temp>/mdb.db --out <temp>/export
python tools/migrate_to_mariadb.py load --config <sacrificial.cnf> --max-blob-bytes 181 --force --out <temp>/export
```

The load needs `max_allowed_packet` raised for its duration (64 MB on the
local server; put it back after), and the database recreated as
`utf8mb4_nopad_bin`: `mariadb_load.py` (below, "Into MariaDB") does both,
the export and load, and puts the packet back. Tried 2026-11-11 on perf-seed/1 at 50
marks into a local 12.3: 4.7 s, 50 marks over nine cadences, 50 `mark`
history rows, 65 assignments, 10 mutes, 2 retirements, schema 12. Then
`EXPLAIN` and time there; the census above is the count to hold the
build's figures against.

## The flaky default's fixtures (WP-44 items 21 to 24)

The two bars of `docs/design/flaky-default-r0.md` §3a were measured on
two different fixtures, and the measured passes of items 23 and 24 are
held to them, so each is a recipe here. Both start from a seed at the
base's schema with the reference assignments fixture (65 assigned, 10
muted), then rewrite the largest environment's (`atlas-lab-alpha`,
8,000 tests) mainline to N tests failing:

- **latest mix** (the headline's bar, §3a "The bar for the build"): on
  perf-seed/1, only the newest three mainline runs of the planted tests
  change. N fail now, half after a pass; half after a FAIL, run 3 a PASS
  for one in seven (promoted at cadence 2); 0.3 N fix candidates, one in
  five a real fix; every other test that failed in its newest two
  passes twice. This is the mix §3a describes ("half FAIL after a pass,
  half FAIL after a FAIL (1 in 7 promoted at 2), and 0.3 × that many fix
  candidates").
- **nightly mix** (the per-day chart's bar, §3a "The trend, round 1"):
  on perf-seed/1-h90, every one of the 90 mainline nights. 30% of N fail
  on both servers in five-night spells; 70% fail on one server, drawn
  each night (one in four) from a pool four times their number.
  `--two-servers` runs every mainline run of the environment again 3 h
  later, with its own result and a copy of the first run's output (an
  import stores an output for every run, and a test's run history reads
  it). The draw is an MD5 of the test's place and the night, so the same
  seed gives the same rows, and the fourteen-night counts §3a quotes.

After the rewrite `latest_runs` is re-pointed for the environment, and
`activity_hours` and `script_hours` are rebuilt by the tree's own
rebuild functions (a prune's). A rewrite of `runs` that left
`latest_runs` or the hourly tables describing the old runs would make
every read of them invalid, so `check_derived()` then reads every
derived row against `runs`, independently of the product's code (each
rollup equal to its GROUP BY; one `latest_runs` row per stream and
test, pointing at the newest run, its time and result, with the run
before's result; an output for every run), and the build fails if
anything disagrees. `tests/test_dev_perf.py` plants each kind of
staleness and sees it found.

### The recipe, from nothing

Everything is built in a temp directory: `seed.py` refuses any path
inside a checkout, `.scratch/` included. Move the finished files where
the pass will read them; `ab.py` and `seed.py --census` read from
anywhere but the repo-root `testboard.db`.

```
T=<temp>/testboard-perf/fd
python tools/dev/perf/seed.py --out $T/perf.db --tree <base>                       # 25 s to 75 s
python tools/dev/perf/seed.py --out $T/perf-h90.db --history-nights 90 --tree <base> # 3 to 9 min
for N in 50 500; do
  cp $T/perf.db $T/latest-$N.db
  python tools/dev/perf/seed.py --onto $T/latest-$N.db --assignments 65 \
      --default-failing $N --failing-mix latest --tree <base>                       # 3 s
  cp $T/perf-h90.db $T/nightly-$N.db
  python tools/dev/perf/seed.py --onto $T/nightly-$N.db --assignments 65 \
      --default-failing $N --two-servers --tree <base>                              # 70 s
done
python tools/dev/perf/seed.py --census $T/nightly-50.db --cadence 2                # 21 s
```

`<base>` is a checkout at migration 12 or later (the fixtures above were
built 2026-11-19 at 5e80e20, migration 12). Sizes: latest 95 MB,
nightly 720 MB. `--onto` prints the census and the check; `--census`
prints them again for a built file (read-only) and exits 1 if a derived
table disagrees. **A pass runs `--census` on its fixture first and
compares it with the tables below.**

### The default: the one step after item 22

The fixtures hold no default: they are at migration 12, and migration 13
and its endpoint came with item 22. The default is set through the tree's own
`PUT /api/environments/{environment}/flaky-default` (the design's §1a
contract), in-process, as amy, dated an hour before the environment's
first run (`default_clock()`: no run precedes `cadence_at`, so every
latest run can promote, and the chart judges every night it reads):

- **In a measured pass**: `ab.py ... --flaky-default 2` (or `3`). Each
  side's copy gets it through that side's tree, after `--assign` and
  `--marks`; both must hold it and the same rows, or the run stops; the
  default's census is printed. Both trees must have the endpoint, so
  the **base is item 22's commit** (it stores the default and judges
  nothing; its own pass shows every first-paint statement count equal
  to the build before it), not 6217c3e, which the bars name. A tree
  without the endpoint is refused before anything is measured.
- **Into a file** (for MariaDB, or a server to click through):
  `seed.py --onto <copy in temp> --flaky-default 2 --tree <item 22 or
  later>`. Opening the copy with that tree migrates it to 13.

How the passes use them:

- **Item 23** (§3a's first bar): `latest-50.db` and `latest-500.db`,
  `--flaky-default 2`, and `latest-500.db` at `--flaky-default 3`
  (decision 4). The Known slow rows in a run of their own on perf-seed/1
  with no default (statement counts equal); the bar's rows with
  `--no-known`, since the Known slow `assign65` state adds 65 more
  assignments before the fixture state (as "Alone in `ab.py`" under the
  marks fixture says). Promotion is measured here only: the nightly fixtures have
  no promoted test on mainline (P 0 below).
- **Item 24** (§3a's per-day chart bar): `nightly-50.db` and `nightly-500.db`,
  `--flaky-default 2` and `3`, the row
  `trend=/api/summary?parts=assigned_trend` (14 days) and with `&days=90`;
  MariaDB 12.3 and 10.3 by hand (below).

### The census each fixture should show

Failing census (`--census`, alpha's mainline; derived tables valid in all four):

| Fixture | nights, runs a test a night | runs (FAIL, tests) | newest 14 nights: FAIL runs, tests | latest: FAIL (after a FAIL), candidates | estate runs = outputs |
|---|---|---|---|---|---|
| latest-50 | 7, 1 | 56,000 (467, 256) | 467, 256 | 50 (25), 15 | 166,240 |
| latest-500 | 7, 1 | 56,000 (1,492, 823) | 1,492, 823 | 500 (250), 150 | 166,240 |
| nightly-50 | 90, 2 | 1,440,000 (6,457, 432) | 1,038, 204 | 42 (18), 29 | 1,550,240 |
| nightly-500 | 90, 2 | 1,440,000 (59,293, 3,310) | 9,286, 1,790 | 328 (143), 200 | 1,550,240 |

§3a ("The trend, round 1") quotes the nightly rows' 14 nights: 1,038
FAIL runs over 204 tests at 50, 9,286 over 1,790 at 500.

The default's census on alpha's mainline (`--cadence C`, or the default
held; muted 10 and assigned 65 in all; no marks):

| Fixture, cadence | passing (candidates, real fixes) | flaking (F, X) | failing | P |
|---|---|---|---|---|
| latest-50, 2 | 7,950 (15, 3) | 25 (25, 25) | 25 | 4 |
| latest-50, 3 | 7,950 (15, 0) | 29 (29, 29) | 21 | 20 |
| latest-500, 2 | 7,500 (150, 30) | 250 (250, 247) | 250 | 36 |
| latest-500, 3 | 7,500 (150, 1) | 286 (286, 283) | 214 | 210 |
| nightly-50, 2 or 3 | 7,958 (29, 2 ; 0) | 24 (24, 24) | 18 | 0 |
| nightly-500, 2 or 3 | 7,672 (200, 16 ; 0) | 185 (184, 184) | 143 | 0 |

- The census is alpha's alone. The product's counts cover every
  environment: §3a gives new 66, still 254, fixed 42, flaking 250 at
  500 and cadence 2, of which alpha's are P 36, failing 250 less P,
  fixes 30, flaking 250.
- The default covers alpha's builds too, which no mix touches: on every
  fixture `nightly/full` at cadence 2 is flaking 70, failing 58, P 14,
  and `feature/checkout-rewrite` flaking 34, failing 40, P 7 (at 3: 84,
  44, 7 and 41, 33, 0).
- The trend's per-night figures are not in the census; item 24's oracle
  (`flaking_that_night` over every run) is its tests'.

### The bars (copied from §3a, cold, against 6217c3e, SQLite ; MariaDB 12.3)

The headline's bar ("The bar for the build"; fixture: latest mix,
default 2):

| Read | 50 failing | 500 failing |
|---|---|---|
| Home's headline | +7 to +10 ; +6 | +29 to +31 ; +19 to +33 |
| New failures queue (first paint) | +3 to +4 ; +3 to +5 | +15 to +16 ; +12 to +16 |
| Flaking queue | +1.0 to +1.4 ; about +4 (estimate) | +11 ; +19 |
| Browse, no filter (first paint) | at most +1 ; at most +1 | at most +1 ; at most +1 |
| Browse FAIL / FLAKING | +0.5 / +1.1 ; not measured | about +4 / +9.5 ; about +9 / +16 (estimate) |
| Open Actions' rows (first paint) | level ; not measured | +6 to +9 ; level (±10) |
| No default, with or without marks | 0 statements; the rider's µs | the same |

The per-day chart's bar ("The bar for the build's measured pass", under
"The trend, round 1"; fixture: nightly mix, two servers, default 2
unless named):

| Read | 50 failing | 500 failing |
|---|---|---|
| `assigned_trend`, 14 d, E / product / unscoped | ≤ +70 ; ≤ +80 | ≤ +250 ; ≤ +750 (accepted by the owner: decision 24) |
| the same, cadence 3 | ≤ +70 ; ≤ +85 | ≤ +280 ; ≤ +820 |
| the same, 90 d (no page sends it) | ≤ +300 ; ≤ +1,900 | ≤ +1,400 ; ≤ +8,900 |
| Every first-paint request | statements and text equal to the build without the read | the same |
| Default only outside the scope ; none anywhere | +1 statement cold (the map), 0 after the headline ; 0 | the same |

§3a also lists what `/perf-ab` measures beside these rows (Watch with an
E card, the test detail, a build's headline in E, Open Actions' Failing
mode, 0 and 200 failing, the statement counts per method) and the plan
guards; that list is the pass's, not repeated here.

### Into MariaDB

```
cp <fixture>.db <temp>/mdb.db       # optional: --flaky-default into the copy first (item 22's tree)
python tools/dev/perf/mariadb_load.py --config <sacrificial.cnf> --db <temp>/mdb.db \
    --recreate --tree <checkout at the fixture's schema>
```

`mariadb_load.py` drops the database the option file names and creates
it again as utf8mb4 / `utf8mb4_nopad_bin` (the migration tool's
collation gate), raises the server's `max_allowed_packet` to 64 MB for
the load (its preflight stops below that) and puts the old value back
whatever happens, runs the tree's migration tool (`export`, `load`,
`verify`) with this interpreter, and `ANALYZE`s `runs` and `latest_runs`.
It reaches the server only through the migration tool's own connection,
never through a client installed on the host, and prints what the
database holds (schema, runs and outputs, alpha's latest FAIL, the
assignments, mutes and defaults) to hold against the census. The fixture
is read, never written; `--recreate` is required. Tried 2026-11-19 on
nightly-50 into a local 12.3: 46 s, every `verify` check agreed, schema
12, 1,550,240 runs and outputs, alpha's latest 42 FAIL (18 after a
FAIL), 65 assignments, 10 mutes, the packet back at 16 MB. A local 12.3
is not production's 10.3: plans there are indicative, and item 24's
10.3 figures need a 10.3 server.

## Tests

`tests/test_dev_perf.py`: a slow path planted in a copy of the package shows
up as `SLOWER` and an untouched row does not; a cold call executes more SQL
than a warm one; the seed is deterministic and has its shape; both scripts
refuse the repo-root database; the `-hN` variant (tiny) goes back N nights in
the largest environment only, is deterministic, and ends in the plain seed's
runs; the fixture writes N assignments and 10 mutes, current failures first,
through either route. The marks fixture (on a seed at scale 0.3 over seven
nights, which holds every state): `mark_state` is §2 B.1's table row by row;
every cadence 2 to 10; the census at 0, 40 and 120 pinned; every state
marked; one marked test muted, one assigned, two retired; written through
`mark_flaky` (its history and comments); every mark dated before the
latest runs, and the product's P on mainline equal to the census's (on a
tree that promotes); same seed, same digest; composed
with the reference assignments fixture it adds no assignment or mute; the
runner applies it and a tree without marks refuses N > 0 but runs N = 0.
A retired real fix leaves the candidate and fix counts; a tree writing the
same marks with another reason is refused (the digest, not only the
census); `run()` refuses a fixture with no row or page to apply it to.
The page mode: its shape (each load's time is its last request's finish);
a slow path planted in Browse makes Home `SLOWER` and Browse what it waits
for; a handler's exception is a 500, reported as the page's status; one
escaping the handler ends the run naming the request with its text. The
pool: one setup job on each worker; an exception fails the load with its
text and leaves the pool usable; a setup that raises does not leave the
others waiting. The cold sweep clears `_cadence_runs_cache` by name, refuses one
it cannot clear and does not mind its absence.
The flaky default's fixtures (seventeen nights at scale 0.03): the
nightly mix's draw gives §3a's fourteen nights at 8,000 tests
(1,038 and 9,286 FAIL runs); with two servers every test runs twice a
night, 3 h apart, same duration, the pair's results the mix's, the
first run's output copied, and nothing else in the estate moves; with
one server nothing is added; the latest mix writes the plan's newest
three runs and its census follows from it (one in seven promoted, one
in five candidates a real fix); a rewrite without the rebuild, a stale
rollup count, a wrong `prev_result` and a missing output are each found
by `check_derived`; same seed, same fixture; the refusals (two servers
with the latest mix, twice, with `--marks`, too many, an unknown mix, a
default outside 2 to 10, the repo root); the CLI and `--census` (exit 1
on a stale table). The default: `put_flaky_default` sends the §1a
request at its clock and refuses a non-2xx answer; it is dated before
every run; a marked test is left to its mark; the census reads the
default held. The runner, on planted trees with a stand-in endpoint:
it sets the default on both sides and prints its census, refuses a
tree without the endpoint, a side writing other rows, a cadence
outside 2 to 10 and a run with no row to apply it to.
`mariadb_load.py`, without a server: what it refuses before touching
anything, the largest output measured from the fixture, no mention of
the driver and no program started but this interpreter.

Two builds of the same recipe agree in every row except one: the mainline
stream's last-seen time is the wall clock of the migration that created it.
`fingerprint()` does not read it; the runner's clock comes from `runs`.
