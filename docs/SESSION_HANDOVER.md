# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-09-29**. WP-31 and WP-32 are both in production;
nothing is waiting to ship. The next drop has **not been specified yet** —
it is to be specced today, built overnight, deployed 2026-09-30.

## Where things stand

- **Production is MariaDB, schema v10, serving `master` at `91d5cd3`** —
  the 2026-09-24 drop (WP-32, PR #11) on top of the 2026-09-23 drop
  (WP-31, PR #10). The user states GitHub `master` is what is deployed.
- **Follow works on real runs.** The user's report, 2026-09-29: "Follow
  is working beautifully". That is the first time the ten-second timer
  has been seen ticking by itself. Scroll restoration, the failure
  stepper restarting, and the six-hour block split were not itemised in
  that report — no complaint, no separate confirmation.
- **The site pushes results DURING a run** (a full run takes ~7h), and
  branch builds are being fed for real. Both are now the normal case,
  not the new one.
- **No drop is in flight.** No branch carries unshipped dashboard code
  except the two listed as parked/open below.

## The next piece of work

**A small drop, spec to come from the user.** Until the spec exists there
is nothing to build; do not infer one from the candidates below.

- It will be **WP-33** (no WP-33 exists anywhere in the repo yet), on a
  branch `wp-33-<slug>` cut from `origin/master`.
- Deploying 2026-09-30 means the tester section in `whatsnew.html`
  (`data-drop-date="2026-09-30"`) and the operator note
  `docs/drops/2026-09-30.md` are dated **2026-09-30**, and the operator
  note is written before it ships.
- If it needs a schema change, version **11 is claimed by WP-15**
  (registry, `UPGRADE_PLAN.md` §1) — a new claim renumbers WP-15 again,
  in the same commit. A migration also turns the rollback from
  `git checkout` into a database restore, and on MariaDB the schema moves
  by `tools/upgrade_mariadb_schema.py`, never by the app. Say which in
  the operator note.

**Candidates already on record** (for the user to pick from, or ignore):

1. **`/api/timeline` environment check** — `environment not in
   storage.known_environments()` scans the `latest_runs` primary key
   (6–12 ms of a 27 ms request, dev-scale copy); the neighbouring
   handlers use `storage.environment_exists()`, three seeks. Follow now
   calls this every ten seconds per open tab. Python, so a restart.
2. **Old-build tiles** — a build whose last run is more than 36 hours
   old shows "Reported 0 of N" on its own tiles until it runs again.
   A design decision about a build-specific window, not a bug — bring it
   to the user, **do not loosen the clamp**.
3. The 2026-08-11 morning list's UI judgement calls (below).

## Where the code is

| | |
|---|---|
| `origin/master` | `91d5cd3` — **deployed.** Cut the next branch from here |
| `docs-handover-2026-09-29` | this rewrite + one status-log entry; docs only |
| `wp-32-timeline-follow`, `wp-31-own-results-always` | merged (squash); prune |
| `wp-30-java-feeder` | Java micro client + CI, PR #9 **open**, all checks green, untouched since 2026-09-08; one commit ahead of `master`, two behind |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |
| `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*`, `wp-17`…`wp-25` | merged; prune when convenient. Six sibling worktrees (`TestDashboard-*-wt`) still hold some of them checked out — remove the worktree before the branch |

There is **no local `master` branch** in this checkout; work from
`origin/master`.

**Suite on `master`'s tree: 2270 OK (skipped 1)**, 144 s, SQLite-only,
run 2026-09-29. Dual-backend variants not run here (no MariaDB option
file on this machine); CI's MariaDB legs are green on `91d5cd3`.

## Needs a person, not a commit

1. **Spec the next drop** (above).
2. **Decide the Java client's fate** (PR #9): merge or park.
3. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls (watch-card accents,
   composer placeholder, "Not run" tab dominance, Build-picker
   discoverability, Watch back-navigation); first Tcl 8.5 site is still
   an experiment (no 8.5 interpreter has ever run `clients/feeder.tcl`).
4. **Prune merged branches and worktrees** — deletion, so it waits to be
   asked for.

## Verification tooling (DOM shim, not a browser)

There is still no browser here; every drop's operator note says so.

- `.scratch/net/run_net.py` — the six-class sanity net (~18 s, port 8931).
- `.scratch/net/wp32_drive_timeline.mjs` — 43 checks on the Timeline's
  Refresh/Follow; imports the WORKING TREE's `static/timeline.js`
  against a server booted from the repo root on a copy of the shifted
  seed. The poll is captured by wrapping `setTimeout` and invoked by hand.
- `.scratch/net/wp31_drive_branch_tabs.mjs` — the branch-build tabs.
  The legacy WP-23 driver (`legacy_drive_branch_tabs.mjs`) is stale
  against today's `app.js`; use the WP-31 one or the `walk_*` scripts,
  which parse ids from the shipped markup.
- `.scratch/net-wt` is pinned at `b816151`; re-point it
  (`git -C .scratch/net-wt checkout --detach <branch>`) to verify a
  branch, and restore the pin afterwards.

All of `.scratch/` is gitignored — it exists on this machine only.

## First ten minutes of the next session

```bash
git fetch origin --prune
git log --oneline -3 origin/master   # expect 91d5cd3 on top, unless the drop has merged
git switch -c wp-33-<slug> origin/master
python -m unittest discover          # expect 2270 OK (skipped 1) before any change
gh run list --branch master --limit 1
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code.
