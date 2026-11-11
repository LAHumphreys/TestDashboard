# tools/dev/perf: the A/B instrument

Development-only. Nothing here is deployed or imported by the server.

## From a clean checkout to a number

```
python tools/dev/perf/seed.py --out <scratch>/perf.db            # ~75 s, ~95 MB
python tools/dev/perf/ab.py --base <base-sha> --head <head-sha> --db <scratch>/perf.db
```

- For a request that reads a window of `runs`, measure on the variant
  `perf-seed/1-h90` as well (below): `--history-nights 90`, about 8.5 min.
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
at the seed's clock, reason `perf fixture: accepted as flaky`), never by a
raw INSERT: each mark leaves its history row and its comment, as a person's
would. Deterministic: the same seed gives the same marks, cadences and rows
(`fixture_digest()`).

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
local server; put it back after). Tried 2026-11-11 on perf-seed/1 at 50
marks into a local 12.3: 4.7 s, 50 marks over nine cadences, 50 `mark`
history rows, 65 assignments, 10 mutes, 2 retirements, schema 12. Then
`EXPLAIN` and time there; the census above is the count to hold the
build's figures against.

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
`mark_flaky` (its history and comments); same seed, same digest; composed
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

Two builds of the same recipe agree in every row except one: the mainline
stream's last-seen time is the wall clock of the migration that created it.
`fingerprint()` does not read it; the runner's clock comes from `runs`.
