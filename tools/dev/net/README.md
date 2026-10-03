# The sanity net

A development tool, not part of the app and never deployed. It boots this
checkout's server against a copy of the generated dev estate, seeds a
demonstration estate of products and builds on top of it, and checks the
running board from the outside in two ways:

| Check | How | What it catches |
|---|---|---|
| Class 5, seed coverage | `api_checks.py` over HTTP | UI the seed never exercises: an empty compare category, a card no data reaches |
| Class 6, contract honesty | `api_checks.py` | A response that disagrees with what the API documents (an old nine-field client's import, error shapes) |
| Class 3, unscoped catalogs | `api_checks.py` | A product- or build-scoped list that leaks rows from outside its scope |
| Class 4, dishonest empties | `api_checks.py` | An empty answer where the data says there should be rows, or the reverse |
| Extra | `api_checks.py` | A retired value accepted again; the NUL sentinel in `static/actions.js` |
| Classes 1 and 2, links and scope | `walk_*.mjs` in `node`, through `domshim.mjs` | A link that drops the product/build/environment it should carry; a picker change that leaves a stale narrower parameter in the URL |

The walks load the real ES modules from `static/` into a minimal DOM
(`domshim.mjs`): no layout, no CSS, no rendering. They prove wiring, data
flow and DOM shape; they cannot see colour or overlap, and they are no
substitute for a browser.

## Running it

```
python tools/dev/net/run_net.py                      # copy testboard.db, seed, check
python tools/dev/net/run_net.py --url-prefix testboard   # the same, pages under /testboard/
python tools/dev/net/run_net.py --base-db PATH       # another generated estate
python tools/dev/net/run_net.py --db seeded.db       # reuse a seeded copy, skip seeding
```

Run it twice, with and without `--url-prefix`: the prefix run is the one
that catches a relative link that resolves outside the prefix.

The server listens on `127.0.0.1:8931`. The database is a **copy** in a
temp directory; the source file is never opened (opening the repo-root
`testboard.db` with current code would migrate it). The copy and the
server log are removed on exit unless `--keep` is given. A run takes
about a minute, most of it seeding.

## Seeding

`run_net.py` seeds automatically, in this order, from `seeds/`:

1. `seed_wp23_v3_net.py`: products Atlas and Beacon over the generated
   estate's environments, three mainline nights each, a long-running
   build, a short build and two release builds (over HTTP).
2. `seed_corvus_net.py`: product Corvus, fed in the nine-field record
   shape of a pre-streams client (over HTTP).
3. `addendum3_net.py DB`: the few shapes nothing else produces (a test
   only on a build, a test failing on both sides, an assignment whose
   origin disagrees with mainline), written directly through `Storage`
   while the server stays up.

The base estate is what `tools/generate_demo_data.py` produces. Its
nights are dated August 2026, so checks that depend on the wall clock
(staleness against a declared expectation, a build's cadence) drift as
the date moves on; a failure there is the seed's age before it is a bug.

## node is optional

The walks need `node` on `PATH`: optional dev tooling, nothing to
install from npm, never needed by the server, the tests or a deployment.
Without it `run_net.py` prints a `SKIP` line, still runs every API
check, and reports the walks as skipped rather than failed.

Exit status: 0 when every check that ran passed, 1 on any failure, 2 when
the base database is missing.
