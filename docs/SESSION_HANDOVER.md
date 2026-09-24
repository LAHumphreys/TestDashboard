# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-09-24**. Two one-day drops in a row from real use:
**WP-31** (a build's own dashboard always reachable; merged #10,
2026-09-23) and **WP-32** (Refresh + Follow on the Timeline, branch
`wp-32-timeline-follow`, today's drop).

## Where things stand

- **Production is MariaDB, schema v10, serving the streams drop** (the
  2026-08-11 drop, `tooling-2026-08-10` → `master` via PR #7). WP-30's
  micro feeder client (PR #8) is on `master` too; its Java sibling is on
  `wp-30-java-feeder`, one commit ahead of `master`, not yet merged.
- **Branch builds are now being fed to the dashboard for real** — the
  streams work has met real data. First finding, same day: a fresh branch
  build was **compare-only**. Its "Its own results" tab (WP-23) was hidden
  by WP-25's covered-pass gate until the build had run twice in a
  fortnight, so nothing on the page said how much had actually run.
- **WP-31 fixed that** and is merged (#10, 2026-09-23). Whether it has
  been DEPLOYED is not recorded here — check the box's What's new date.
- **The site now pushes results DURING a run** (a full run takes ~7h), so
  the Timeline fell behind the run it showed. **WP-32** adds Refresh and
  Follow to the Timeline: front-end only, no migration, no flag. Operator
  note: `docs/drops/2026-09-24.md`. Tester note: `whatsnew.html`, section
  dated 2026-09-24 (provisional — re-date both if it ships later).

## Today's plan

1. **Merge and deploy `wp-32-timeline-follow`** per
   `docs/drops/2026-09-24.md` — stop, checkout, start. If WP-31 was never
   deployed, this deploys both; the procedure is the same.
2. **Watch a real run with Follow on.** The ten-second cadence (60 s in
   the first draft; dropped after measuring — see the drop note's "Load,
   stated") has only ever been invoked by hand in the shim; the first seven-hour run with a
   tab following it is the first time the timer has actually ticked.
   Scroll restoration is also unexercised (no viewport in the shim).
3. **Carried from WP-31:** a build whose last run is more than 36 hours
   old shows "Reported 0 of N" on its own tiles until it runs again (the
   unchanged fallback window, worded from data). If testers report it,
   it is a design decision about a build-specific window, not a bug —
   bring it to the user, do not loosen the clamp.

## Where the code is

| | |
|---|---|
| **`wp-32-timeline-follow`** | **THE branch. Ship this.** Two commits on `origin/master`: the feature + guards, then the notes and log |
| `origin/master` | `158fbcd` — WP-31 merged (#10) on top of the 2026-08-11 drop and WP-30's Python client |
| `wp-31-own-results-always` | merged; prune |
| `wp-30-java-feeder` | Java micro client + CI, one commit ahead of master, unmerged |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |
| `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*` | merged into master via PR #7; prune when convenient |

**Suite on the ship branch: 2270 OK (skipped 1), SQLite-only.** Dual-
backend variants not run this session (no MariaDB option file here); the
change contains no Python.

## Live evidence for WP-32 (DOM shim, not a browser)

`.scratch/net/wp32_drive_timeline.mjs` (scratch, gitignored), importing
the WORKING TREE's `static/timeline.js` (not the pinned worktree) against
a server booted from the repo root on a copy of the shifted seed: 43
checks PASS — see the status log entry for the sequence. The ten-second
poll is captured by wrapping `setTimeout` and invoked by hand; runs are
imported into the newest block between checks (start = last row's end +
5 min, so they join it).

## Live evidence for WP-31 (DOM shim, not a browser)

`.scratch/net/wp31_drive_branch_tabs.mjs` (scratch, gitignored) against a
server booted from the fix commit on a seeded copy of the dev estate.
Two runs: the net seed as-is (its nights are dated August, so under
today's date every build has 0 covered passes — every build is "sparse")
and a re-seed with the nights shifted 39 days into the last fortnight (the
cadenced build then meets the threshold). Both PASS on every check:
sparse build shows both tabs, opens on the difference with the stated
caption, reaches its own results in one click with its own count of 3;
filtered URL opens on own results with the toggle pressed; cadenced
build opens on its own results; mainline load unchanged. The legacy
WP-23 driver (`legacy_drive_branch_tabs.mjs`) is stale against today's
`app.js` (hand-written id list) — use the WP-31 one or the `walk_*`
scripts, which parse ids from the shipped markup.

## Needs a person, not a commit

1. **Merge + deploy WP-32** (above); confirm WP-31 reached the box.
2. **Decide the Java client's fate** (`wp-30-java-feeder`): merge or
   park.
3. Carried from 2026-08-11, still open: re-retire the tests the un-retire
   bug released (search comments for "Automatically un-retired");
   `tools/diagnose_db.py --compare-local` on prod; `max_allowed_packet`
   persistence with the daemon owners; import output-size cap; the
   morning decision list's UI judgement calls (watch-card accents,
   composer placeholder, "Not run" tab dominance, Build-picker
   discoverability, Watch back-navigation); first Tcl 8.5 site is still
   an experiment (no 8.5 interpreter has ever run `clients/feeder.tcl`).
4. **Old-build tiles** (the "Reported 0 of N" reading above) if it comes
   up — a design decision, see the drop note's "What was NOT verified".

## First ten minutes of the next session

```bash
git checkout wp-32-timeline-follow
git log --oneline -3
python -m unittest discover        # expect 2270 OK (skipped 1)
gh run list --branch wp-32-timeline-follow --limit 1
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code. There is still no browser here.
`.scratch/net-wt` is pinned back at `b816151` after this session's live
runs; re-point it (`git -C .scratch/net-wt checkout --detach <branch>`)
to verify a branch, and restore the pin afterwards.
