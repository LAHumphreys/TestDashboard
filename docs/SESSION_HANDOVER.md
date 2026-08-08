# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-08**, after building WP-21 (branches/builds beside
mainline — the whole of `docs/STREAMS_PLAN.md` §3, migration 9) on
`wp-21-streams`, cut from `wp-20-products`. **Built and green on both
backends, not yet accepted, not yet deployed** — the 2026-08-07 drop is
still the one running in production; nothing in this paragraph has shipped.

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7). Confirm the box runs the FINAL drop commit `310f1c0` — What's new saying "7 August 2026" is the tell. Neither WP-20 nor WP-21 below has been deployed |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` so the recorded state matches the deployed one |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. All four CI legs green |
| `wp-20-products` | WP-20 (products + Watchlist), migration 8. Built and suite-green on both backends. **Superseded as a ship candidate by `wp-21-streams` below**, which contains it — ship the combined drop, not this branch alone, unless WP-21 is deliberately deferred |
| `wp-21-streams` | **NEW, this session.** WP-21 (branches/builds beside mainline), migration 9, built on top of WP-20. Suite green on BOTH backends. Operator note is `docs/drops/2026-08-14.md`, rewritten this session to cover migrations 8 AND 9 as one drop — **date provisional**, re-date before pushing if it ships on a different day. Acceptance list is at the bottom of that note. **Unusually for this project, the frontend has partial real-server verification** (see below) — but still no browser |
| `wp-14-in-run-progress` | parked WIP; its migration is now **10** (WP-20 took 8, WP-21 took 9 — the registry note in `UPGRADE_PLAN.md` §1 tracks this renumbering) — renumber before merging |

Suite on `wp-21-streams`: **1644 green** (skipped 1) SQLite-only. The
dual-backend suite (`TESTBOARD_TEST_DB_CNF` against this dev machine's local
`mariadbd`, port 3307, `.scratch/mariadb-test.cnf`) was re-run after every
backend change this session, most recently after the two fixes described
below — check `docs/drops/2026-08-14.md` for the final count, captured there
rather than duplicated here since the note is what a deploy decision reads.
**CI's own `python36-mariadb` leg (mariadb:10.3, prod's actual stream) has
not been observed against this branch** — the local server is a newer
MariaDB, functional evidence only, never a perf number.

**This session went beyond the usual static-analysis-plus-curl frontend
check.** A scratch server was booted against a copy of the dev database, fed
a real branch import, and the actual frontend JavaScript was driven against
it through the project's existing node DOM-shim harness (`.scratch/`,
gitignored — extended this session with `location.search`/`.pathname`,
previously absent) across four scenarios: a bare mainline dashboard load
(zero fetches to `/api/compare`, confirming the zero-visible-change
property empirically, not just by source scan), a branch-scoped dashboard
load (tiles/tabs/agree-and-coverage lines/table all populated correctly
from real data), a branch-scoped test-detail page (the compare strip, and a
posted-from-tagged comment), and a Watchlist `s:` card. **This caught two
real defects that no unit test could have** (every unit test that touches a
stream also declares a product first, which is precisely the case that was
broken): `test.js` never actually rendered a comment's "posted from" tag
even though the backend already returned the data for it — fixed, wired,
and now guard-tested; and the Watchlist's `s:` card was **silently
all-zero** for any stream whose product is `""` (the common case for a site
that has never declared any product — WP-20's default shape), because
`_handle_watch` resolved environment scope through a dict that is *always*
empty for `""` — fixed in `api.py`, still O(1), pinned by
`TestWatchStreamCardImplicitProduct`. Full detail is in the drop note.
**Still true: no browser has rendered any of this** — the shim proves
wiring and data flow, not layout, contrast, or whether the delta view is
too much page for one screen.

**Migration timings, measured on the 220 MB / 540,192-run dev copy (NOT
production — production is ~4x this size), brought to v7 first (production's
current version) so the number is what the box will actually see:**
migration 8 alone 26.8 ms (O(1), unchanged from the original WP-20
measurement); migration 9 alone (v8→v9) 0.883 s total, of which 0.6 s is the
`latest_runs` rebuild (12,008 rows — the one part of either migration that
scales with estate size, not a fixed cost); **combined v7→v9, 0.806 s** —
this is the number that matters for the actual upgrade. Full detail and the
measurement method (a read-only `git archive` of `wp-18-timeline`'s
`storage.py` used to bring a scratch copy to exactly v7 without touching any
branch) is in the drop note.

**SQLite is a permanent first-class backend** (user requirement, 2026-08-07):
`--db PATH` unchanged forever, zero-setup second instances, both backends
gated in CI. MariaDB is opt-in per instance via
`--db-config /etc/testboard/db.cnf --site-notes PATH`. The server never runs
DDL on MariaDB — schema comes from the migration tooling only.

The repo-root `testboard.db` is generated dev data (220 MB, 540,192 runs,
**still v5** — confirmed again this session via a read-only `schema_version`
query, never opened with current code). Production is ~900 MB / ~4.4M runs
at **v7** on a network mount (confirmed against `CLAUDE.md`). Say which one
any number came from. **The dev machine also has MariaDB 12.3.2** (winget,
x86_64 under emulation, port 3307, datadir `.scratch/mariadb-data`, root
password in `.scratch/mariadb-test.cnf`): start with
`& "C:\Program Files\MariaDB 12.3\bin\mariadbd.exe" --datadir=<repo>\.scratch\mariadb-data --port=3307 --console`,
then `$env:TESTBOARD_TEST_DB_CNF=".scratch\mariadb-test.cnf"` activates the
dual-backend tests. Functional evidence only — never quote a perf number
from it.

## The immediate thing

**Two independent threads are both live right now — do not conflate them.**

**Thread A — accept and ship the combined WP-20+WP-21 drop
(`wp-21-streams`).** Built this session, suite-green on both backends, not
deployed, not reviewed. Before it ships:

1. Read `docs/drops/2026-08-14.md`'s acceptance list in full — it is long
   because WP-21's frontend has never been seen in a browser, and the list
   says exactly what a human needs to look at (the Build picker, the delta
   view's five tiles/tabs/agree-and-coverage lines, the compare strip, the
   Watchlist `s:` card) with a REAL branch or build's results, not just
   dev/demo data.
2. Re-date the drop note and `whatsnew.html` if the ship day is not the
   14th (`DropDateTest` catches a mismatch between the two, not a date
   that is wrong in the same way in both places).
3. Feed a real branch or build's results in against a scratch copy and
   walk the acceptance checklist before it goes anywhere near production —
   this session's own driver scripts (described above) are a starting
   point, not a substitute; they proved data flow, not appearance.
4. Run the migration-8+9 probe against a copy of **production** (not just
   the dev copy this session measured) and record the number in the drop
   note before shipping — migration 9's `latest_runs` rebuild is the one
   part of this drop whose cost scales with estate size.
5. Push `wp-21-streams` to `master` and follow the drop note's upgrade
   procedure (two migrations run in one restart).

`wp-14-in-run-progress` must renumber its migration to **10** before it can
merge, now that WP-20 has taken 8 and WP-21 has taken 9.

**Thread B — the MariaDB migration itself, independent of the above.** Steps 1
of the plan (deploy; migration 7; Timeline acceptance) happened 2026-08-07.
What remains: (2) §A server prep on the new account (target server confirmed
MariaDB 10.3.39 via client banner — re-confirm server-side with
`SELECT VERSION()`); (3) §C preflight, §E.1 **dry run on a prod copy**, then
the §E cutover with the freeze and feeder catch-up. Read
[`docs/drops/2026-08-07.md`](drops/2026-08-07.md) for the whole procedure
and the two small follow-ups (confirm the box is on `310f1c0`; push the
branch to `master`). **Note for whoever runs this:** by the time this
cutover happens, the schema may already be at v9 (if Thread A ships first)
— the migration tool and the exporter DDL both need to be from a checkout
that knows `streams`/`stream_id`, the same version-must-match rule that
already applied for `script_hours` at v7.

**Before migration day:** the runbook was re-reviewed and corrected
2026-08-07 (see the status log's entries of that date). §C's preamble
matters most — UTF-8 locale, and the tool-must-match-database-version rule.

**Still open from earlier drops:** re-retire the tests the un-retire bug
released (search comments for "Automatically un-retired");
`tools/diagnose_db.py --compare-local` on the production server, never run.
Performance Phase 2 remains parked; MariaDB perf is unmeasured everywhere
and only the real box may produce numbers. **New from this session:** the
WP-21 legacy-UNIQUE collision path (docs/STREAMS_PLAN.md §3.2 — a branch
and mainline reporting the identical test at the identical microsecond) is
unit-tested on both backends but has never been provoked against a real
feed.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1644 OK (skipped=1) on wp-21-streams
```

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here. Frontend changes are verified by
`tests/test_frontend_calls.py` (static analysis of the actual `.js` source)
and, for the pieces that carry real risk, by driving the actual JS through
the node DOM-shim harness in `.scratch/` against a real running server —
see this session's additions above for what that caught. Neither proves
layout, colour, contrast, or whether a page with five tiles, a tabbed
table, and a sticky band is too much for one screen.

---

## The work waiting, in the order it wants doing

### 1. Accept and ship the combined WP-20+WP-21 drop

See Thread A above. This is now the single largest piece of unshipped,
unreviewed work in the repo — two work packages, one drop, two schema
migrations, a whole new comparison surface.

### 2. Ship the 2026-08-07 drop's remaining MariaDB cutover steps

See Thread B above — independent of Thread A, and can proceed in parallel.

### 3. Merge the deferred work back — `wp-14-in-run-progress`

**Migration renumbers to 10 before merging** (registry §1, third
renumbering). Now further behind two whole work packages: expect conflicts
in `app.js`, `storage.py` (the backend seam, the upsert maintaining
`activity_hours`/`script_hours`, and now the stream partition), and the
status log.

### 4. WP-15 — progress pushes from a partial reader *(migration 10)*

Fully specified in `UPGRADE_PLAN.md` §WP-15. Note for the MariaDB era: its
migration entry is SQLite-only by definition; if the estate has cut over,
its schema change ALSO needs adding to the exporter DDL + a §D reload (or
hand-applied DDL via `testboard_migrate`) — decide when it lands.

### 5. WP-16 — site-specific info tab

Still noted, not specified.

### 6. WP-22 — cross-branch baselines

Deliberately deferred out of WP-21: `/api/compare?baseline=` refuses
anything but mainline in this drop with an explicit 400 naming WP-22. Not
yet specified beyond that reservation.

## Needs a person, not a commit

1. **Acceptance-test and ship the combined WP-20+WP-21 drop** — the
   longest list of any drop so far; see `docs/drops/2026-08-14.md`.
2. Migration-8+9 probe on a prod copy (number into the drop note).
3. §A on the MariaDB server (root), §E.1 dry run, cutover decision.
4. Re-retire the tests the un-retire bug released.
5. `tools/diagnose_db.py --compare-local` on the production server.

## Shipping a drop

1. Branch, build, `python -m unittest discover` green.
2. Dated section at the top of `static/whatsnew.html`, `data-drop-date`
   matching the heading (`DropDateTest`).
3. Operator note `docs/drops/YYYY-MM-DD.md` per `CLAUDE.md`; measure any
   migration on a prod copy first (§1.2).
4. Push the drop branch to `master` after acceptance.
5. On the box: stop, copy `testboard.db` + `-wal` + `-shm`, pull, start —
   when the feeder is not importing.
6. Check the drop per the operator note.
