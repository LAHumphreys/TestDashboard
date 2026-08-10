# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-10, early morning**, at the end of the overnight
run `docs/NIGHT_RUN_2026-08-09.md` executed. All six phases completed.
**The streams upgrade is consolidated on ONE ship branch,
`streams-upgrade`, pushed, all four CI legs green — waiting on the
morning's manual testing and the user's merge/no-merge call.** Nothing
was merged to `master` overnight, by instruction.

> **The morning path:** (1) read the morning summary message in the
> overnight session; (2) work through
> [`MORNING_TESTING.md`](MORNING_TESTING.md) — a 15-minute per-persona
> click checklist against the seeded server on **port 8791**; (3) fix
> anything found (the re-verify loop is one command,
> `python .scratch\net\run_net.py`); (4) your call: push the pending
> `wp-18-timeline` → `master` first (keeps master's history honest),
> then merge `streams-upgrade` → `master`; (5) re-date the drop note +
> `whatsnew.html` to the actual ship day (`DropDateTest` holds them in
> lockstep but cannot catch a date wrong the same way in both); (6)
> deploy per [`drops/2026-08-14.md`](drops/2026-08-14.md).

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7, commit `310f1c0`). Nothing below is deployed |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` before the streams merge |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. All four CI legs green |
| **`streams-upgrade`** | **THE ship candidate** — tip `2adc1d2`, identical to `wp-24-scoped-urls`'s tip. Contains WP-20+21+22+23 (from `wp-23-longrunning`) + WP-25 (one stream kind, `wp-25-one-kind`) + WP-24 (scoped URLs) + the overnight fixes (origin-filter rename, four-site stream carriage, three persona-walk fixes). Chain is linear; nothing ships without all of it |
| `wp-25-one-kind` / `wp-24-scoped-urls` | the overnight work branches, both pushed, both contained in full by `streams-upgrade` — superseded as ship candidates |
| `wp-20-products`…`wp-23-longrunning` | earlier stages of the same chain, superseded |
| `wp-14-in-run-progress` | parked WIP; its migration renumbers to **11** before merging (registry §1) |

Suite on `streams-upgrade`: **2022 OK (skipped=1)** SQLite-only;
**2705 OK (skipped=44)** dual-backend against the local MariaDB (port
3307, `.scratch/mariadb-test.cnf` — functional evidence only, never a
perf number). The 44 skips are all deliberate and per-test-reasoned —
36 are query-count guards that need `sqlite3.set_trace_callback`, which
the MariaDB backend has no equivalent for (so O(N)-query protections
are SQLite-enforced only — known asymmetry, consistent with "MariaDB
perf numbers come from the real box"). CI's four legs (3.6.8 ubi8,
3.6.8+mariadb:10.3, 3.8, 3.14) green on every overnight push.

**The drop:** migrations **8, 9, 10** run in sequence on one restart
(WP-22/24/25 add none; WP-25 amended entry 9's comment in place, DDL
byte-identical). Rollback **needs the database copy** — a v10 file is
refused by v7 code. Combined v7→v10 measured ~0.17–0.18s on the 220MB
dev copy; **never measured on a production copy** — item 2 of "needs a
person" below.

## What the overnight run did (detail: the status log's 2026-08-09→10 entry, and the drop note's "Overnight round" section)

- **WP-25** — one stream kind (`build`); `branch:` on the wire is a loud
  per-record rejection; mainline-default baseline everywhere; data-gated
  two-tab; Time/Timeline stream-scoped empty states name where the data
  is; What's New rewritten for scannability. One review catch: the F5
  verdict line was restored (data-gated) after being over-deleted.
- **WP-24** — `static/urls.js` owns every scoped URL;
  `ScopedUrlConstructionTest` ends the hand-built-URL bug family (eight
  historical incidents). Invisible to testers; pure refactor, measured
  perf no-op.
- **The sanity net** — `.scratch/net/run_net.py` (~18s, unattended):
  six gotcha classes, API + DOM-shim walks. Caught two real defects the
  full unit suite could not see (the origin-filter dead spelling, and a
  four-site `stream=` drop in WP-24's conversion — 517 links, one root
  cause). Fully green at the ship tip. Runner scripts listed in the
  morning summary.
- **Persona walks** (manager/delver/RC-owner, from cold) — three fixes:
  test-page assign now records stream origin (was silently
  mainline-origin), three visible dead-kind strings renamed (+ a sweep
  guard), and the test page gained the review panel's scoped "View in
  timeline →" link. Judgment calls went to the decision list, not code.

## Decided in morning testing, 2026-08-10 — post-drop work, not in this drop

Open Actions gains **bulk unassign** and **bulk assign-with-comment**
(user decision, 2026-08-10, while exploring abandoned-build cleanup:
assignments from a dead build persist until cleared, and today that is
per-row). Related finding to fold into the same work: `delete_stream`
nulls `comments.stream_id` but NOT `current_assignments.stream_id`, so
a dropped stream's assignments keep their origin-filter grouping while
losing their tag. Assignments stay estate-level one-owner-per-triple
(§0.4) — bulk operations act on the current filter, never introduce
per-stream ownership. Not specced yet; task #8 in the session task
list carries the design notes.

## The morning decision list (needs the user, not a commit)

New from the persona walks: watch-card accents don't rank cards when
all are failing (real-browser question); composer name-select
pre-selects the first environment instead of a placeholder; the
"Not run 12,009" tab dominates a naive worst-queue read; the empty
"Every build" section shows on pure-mainline test pages; the Build
picker is invisible on a bare multi-product dashboard (route is
switcher-first, no on-screen hint); no explicit "back" from a Watch
drill-down (nav + saved default is the mechanism). Carried: ghosting
deferral; the `actions.js` NUL sentinel ruling; staleness client-clock
wording; compare O(partition) numbers from a real box; summary
residual cold cost (~25ms measured tonight, dev-labelled); callerless
`previous_builds`/`compare_counts_many(baselines=)` — keep or delete.

## Thread B — the MariaDB cutover (independent, unchanged tonight)

Steps as before: §A server prep, §C preflight, §E.1 dry run on a prod
copy, §E cutover with freeze + feeder catch-up — see
[`drops/2026-08-07.md`](drops/2026-08-07.md). **If Thread A ships
first, the schema is v10** — the migration tool and exporter DDL must
come from a checkout that knows the WP-23 columns AND WP-25's one-kind
world, same version-must-match rule as always.

## Needs a person, not a commit

1. **The morning manual pass → merge → deploy** (see "the morning path"
   above).
2. Migration-8+9+10 probe on a **production** copy — the dev number has
   disagreed with itself across sessions; a production number has never
   been taken.
3. The morning decision list above.
4. §A on the MariaDB server (root), §E.1 dry run, cutover decision.
5. Re-retire the tests the un-retire bug released (search comments for
   "Automatically un-retired"); `tools/diagnose_db.py --compare-local`
   on the production server — both still open from earlier drops.

## First ten minutes of a new session

```bash
git log --oneline -5                  # expect 2adc1d2 on streams-upgrade / wp-24-scoped-urls
git status --short                    # should be clean
python -m unittest discover           # expect 2022 OK (skipped=1)
python .scratch\net\run_net.py        # expect PASS, ~18s (the whole overnight gotcha net)
```

**If the UI looks wrong, check you restarted the server.** Static files
are read per request; the Python is whatever was imported at process
start.

There is still no browser here. The overnight run verified everything a
DOM-shim and a unit suite can see — 528 rendered links' scope carriage,
every empty state's wording, the whole import contract — and none of
what they cannot: layout, colour, contrast, whether a page is too much
for one screen. The morning checklist is organised around exactly that
gap.

The repo-root `testboard.db` is generated dev data (220 MB, v5 — only
ever copied, never opened with current code). The seeded manual-test
server runs from a scratchpad COPY on **port 8791** with a fresh perf
log; `.scratch/net/` and `.scratch/seeds/` hold the harness and seed
scripts (gitignored tooling, listed in the morning summary).
