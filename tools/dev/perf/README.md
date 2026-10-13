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

A run of the six Known slow rows at 24 calls takes about 45 s.

## What the runner does

1. `git archive <sha> testboard` into a temp directory per side (no worktree
   is registered; the same SHA twice gives two separate copies).
2. Imports both packages into **one process** under separate module objects,
   swapping `sys.modules` between calls so a lazy import inside a function
   resolves to its own tree.
3. Copies `--db` once per side; `--db` itself is never opened for writing.
4. Per row: one discarded call each, then N calls per side **alternated**
   (A,B then B,A), each preceded by clearing every storage memo (the two
   invalidators a push calls, plus any `_*_cache`/`_*_memo` attribute). The
   SQLite page cache of the connection is kept, as a worker's is.
5. One more cold call per side under a statement trace, for the statement
   count.

Never over HTTP: on the development machine an unchanged request varies two
to three times between runs.

## Reading the table

| Column | Meaning |
|---|---|
| A ms, B ms | median of the N cold calls |
| B-A ms | median of the N paired differences (each pair ran back to back) |
| 95% band | order-statistic interval for that median: the **noise band** of this row on this run |
| stmts A/B | SQL statements one cold call executes |
| verdict | `SLOWER`/`faster` only when the band is clear of zero **and** the delta exceeds max(1 ms, 5% of A); otherwise `same`. `MORE SQL` is appended whenever B executes more statements, whatever the milliseconds say |

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

## Tests

`tests/test_dev_perf.py`: a slow path planted in a copy of the package shows
up as `SLOWER` and an untouched row does not; a cold call executes more SQL
than a warm one; the seed is deterministic and has its shape; both scripts
refuse the repo-root database; the `-hN` variant (tiny) goes back N nights in
the largest environment only, is deterministic, and ends in the plain seed's
runs; the fixture writes N assignments and 10 mutes, current failures first,
through either route.

Two builds of the same recipe agree in every row except one: the mainline
stream's last-seen time is the wall clock of the migration that created it.
`fingerprint()` does not read it; the runner's clock comes from `runs`.
