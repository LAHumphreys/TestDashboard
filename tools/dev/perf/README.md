# tools/dev/perf: the A/B instrument

Development-only. Nothing here is deployed or imported by the server.

## From a clean checkout to a number

```
python tools/dev/perf/seed.py --out <scratch>/perf.db            # ~75 s, ~95 MB
python tools/dev/perf/ab.py --base <base-sha> --head <head-sha> --db <scratch>/perf.db
```

- `<scratch>` is a temp directory. Both scripts refuse a path inside a
  checkout: the seed is scratch output, and the repo-root `testboard.db` is
  dev data at about a quarter of production that current code migrates on
  open.
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

## Tests

`tests/test_dev_perf.py`: a slow path planted in a copy of the package shows
up as `SLOWER` and an untouched row does not; a cold call executes more SQL
than a warm one; the seed is deterministic and has its shape; both scripts
refuse the repo-root database.
