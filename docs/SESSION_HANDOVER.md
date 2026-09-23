# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-09-23**, after the first day branch builds were
loaded into the live dashboard and the one thing that came up immediately
was fixed as **WP-31** (branch `wp-31-own-results-always`).

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
- **WP-31 fixes that** and is the next drop: both tabs always exist on a
  stream-scoped load, the threshold picks only the default, the caption
  says why, and a Watch card's filtered click-through opens the own
  results tab. Static files only, no migration, no new flag. Operator
  note: `docs/drops/2026-09-23.md`. Tester note: `whatsnew.html`,
  section dated 2026-09-23 (provisional — re-date both if it ships later).

## Today's plan

1. **Push `wp-31-own-results-always`, open the PR, wait for all CI legs**
   (nothing touches Python or SQL, but the ubi8 3.6 leg and the MariaDB
   legs are the only evidence the suite is green anywhere but this box).
2. **Merge and deploy** per `docs/drops/2026-09-23.md` — stop, checkout,
   start. Restart is the whole deployment.
3. **Watch for the one thing the drop note flags:** a build whose last
   run is more than 36 hours old shows "Reported 0 of N" on its own tiles
   until it runs again (the unchanged fallback window, worded from data).
   "N tests tracked" and the browse table still show everything that ran.
   If testers report it, the answer is a design decision about a
   build-specific window, not a bug — bring it to the user, do not loosen
   the clamp.

## Where the code is

| | |
|---|---|
| **`wp-31-own-results-always`** | **THE branch. Ship this.** Two commits on `origin/master`: the fix + guards, then the notes and log |
| `origin/master` | `b9700b3` — the 2026-08-11 drop plus WP-30's Python client |
| `wp-30-java-feeder` | Java micro client + CI, one commit ahead of master, unmerged |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |
| `tooling-2026-08-10`, `streams-upgrade`, `wp-2x-*`, `docs-tidy-*` | merged into master via PR #7; prune when convenient |

**Suite on the ship branch: 2262 OK (skipped 1), SQLite-only.** Dual-
backend variants not run this session (no MariaDB option file here); the
change contains no Python.

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

1. **Merge + deploy WP-31** (above).
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
git checkout wp-31-own-results-always
git log --oneline -3
python -m unittest discover        # expect 2262 OK (skipped 1)
gh run list --branch wp-31-own-results-always --limit 1
```

The repo-root `testboard.db` is generated dev data — only ever copied,
never opened with current code. There is still no browser here.
`.scratch/net-wt` is pinned back at `b816151` after this session's live
runs; re-point it (`git -C .scratch/net-wt checkout --detach <branch>`)
to verify a branch, and restore the pin afterwards.
