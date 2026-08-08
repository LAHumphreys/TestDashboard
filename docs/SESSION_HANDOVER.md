# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-08**, after WP-22 (release builds + compare-any-two,
`docs/STREAMS_PLAN.md` §4, **no migration**) was built on `wp-22-builds`, cut
from `wp-21-streams`'s tip. **Built and green on both backends, not yet
accepted, not yet deployed** — the 2026-08-07 drop is still the one running
in production. WP-20+21+22 now ship as ONE combined drop; there is nothing
left in the streams/products design (`docs/STREAMS_PLAN.md` §§0–4) unbuilt
except WP-23 (long-running branch streams — deliberately last, see below).

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7). Confirm the box runs the FINAL drop commit `310f1c0` — What's new saying "7 August 2026" is the tell. Nothing below has been deployed |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` so the recorded state matches the deployed one |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. All four CI legs green |
| `wp-20-products` | WP-20 (products + Watchlist), migration 8. **Superseded as a ship candidate by `wp-22-builds` below**, which contains it |
| `wp-21-streams` | WP-21 (branches/builds beside mainline), migration 9. **Superseded as a ship candidate by `wp-22-builds` below**, which contains it |
| `wp-22-builds` | **WP-22 (release builds + compare-any-two), no migration — the current ship candidate, contains WP-20+21+22 in full.** `/api/compare?baseline=` now accepts any same-product stream, not only mainline; the Build picker gains a Builds group; the build-scoped dashboard gains a "Compare to" box defaulting to the predecessor build; the test page gains the "Every build" table + stream switcher explicitly requested after WP-21's first human use; the Watchlist's `s:` card works for builds. Suite green on BOTH backends. Operator note is `docs/drops/2026-08-14.md` — **date provisional**, re-date before pushing if it ships on a different day |
| `wp-14-in-run-progress` | parked WIP; its migration is now **10** (WP-20 took 8, WP-21 took 9 — the registry note in `UPGRADE_PLAN.md` §1 tracks this renumbering) — renumber before merging |

Suite on `wp-22-builds`: **1739 green** (skipped 1) SQLite-only; **2307
green** (skipped 18) with `TESTBOARD_TEST_DB_CNF` set against this dev
machine's local `mariadbd` (port 3307, `.scratch/mariadb-test.cnf`) — both
on the FINAL tree, re-run after every change this session. **CI's own
`python36-mariadb` leg (mariadb:10.3, prod's actual stream) has not been
observed against this branch** — the local server is a newer MariaDB,
functional evidence only, never a perf number.

**WP-22 got the same real-server-plus-DOM-shim treatment WP-21's first
human use established**, this session: a scratch server seeded with two
products, one feature branch, and TWO builds of the same product (older +
newer, an overlapping-but-changed test set) so previous-build defaulting
was actually exercised — not merely asserted by a unit test. **It caught
one real defect**: a JSDoc comment in `compare.js` contained a literal
`*/` mid-sentence ("built from *streamMeta*/*baselineMeta*'s own..."),
which closed the block comment early and turned the rest of it into a
syntax error. `node --check` on the same file did NOT catch this —
only the DOM shim's real dynamic `import()`, which parses the actual
module graph the way a browser would, did. This would have broken
`index.html` AND `test.html` outright (both import `compare.js`) had it
shipped; no unit test in this project (all static-analysis, no JS
runtime) could have found it. Fixed same-session, before the rest of the
verification pass ran. Full account is in `docs/drops/2026-08-14.md`'s
"What was NOT verified" section (which also says, plainly, what the pass
still does not prove: layout, contrast, real screen widths).

**SQLite is a permanent first-class backend** (user requirement, 2026-08-07):
`--db PATH` unchanged forever, zero-setup second instances, both backends
gated in CI. MariaDB is opt-in per instance via
`--db-config /etc/testboard/db.cnf --site-notes PATH`. The server never runs
DDL on MariaDB — schema comes from the migration tooling only.

The repo-root `testboard.db` is generated dev data (220 MB, 540,192 runs,
**still v5** — never opened with current code, only ever copied). Production
is ~900 MB / ~4.4M runs at **v7** on a network mount. Say which one any
number came from. **The dev machine also has MariaDB 12.3.2** (winget,
x86_64 under emulation, port 3307, datadir `.scratch/mariadb-data`, root
password in `.scratch/mariadb-test.cnf`): start with
`& "C:\Program Files\MariaDB 12.3\bin\mariadbd.exe" --datadir=<repo>\.scratch\mariadb-data --port=3307 --console`,
then `$env:TESTBOARD_TEST_DB_CNF=".scratch\mariadb-test.cnf"` activates the
dual-backend tests. Functional evidence only — never quote a perf number
from it.

## The immediate thing

**Two independent threads are both live right now — do not conflate them.**

**Thread A — accept and ship the combined WP-20+WP-21+WP-22 drop
(`wp-22-builds`).** Suite-green on both backends, not deployed, not
reviewed. Before it ships:

1. Read `docs/drops/2026-08-14.md`'s acceptance list in full — WP-22 added
   its own items (the Builds group, the "Compare to" default, the
   build-framing line, the "Every build" table + stream switcher, the
   build-kind Watchlist card's wording) on top of WP-21's already-long
   list.
2. Re-date the drop note and `whatsnew.html` if the ship day is not the
   14th (`DropDateTest` catches a mismatch between the two, not a date
   that is wrong in the same way in both places).
3. Feed real branch AND build results in against a scratch copy —
   including a SECOND build under a different name, so the rebuild and
   previous-build-defaulting paths both get exercised by a human — and
   walk the acceptance checklist before it goes anywhere near production.
4. Run the migration-8+9 probe against a copy of **production** (not just
   the dev copy measured so far) and record the number in the drop note
   before shipping. (WP-22 itself claims no migration, so this number is
   unchanged from the WP-20/21 measurement.)
5. Push `wp-22-builds` to `master` and follow the drop note's upgrade
   procedure (two migrations run in one restart, same as before — WP-22
   adds none).

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
that knows `streams`/`stream_id`/the assignment `stream_id` columns, the
same version-must-match rule that already applied for `script_hours` at v7.

**Before migration day:** the runbook was re-reviewed and corrected
2026-08-07 (see the status log's entries of that date). §C's preamble
matters most — UTF-8 locale, and the tool-must-match-database-version rule.

**Still open from earlier drops:** re-retire the tests the un-retire bug
released (search comments for "Automatically un-retired");
`tools/diagnose_db.py --compare-local` on the production server, never run.
Performance Phase 2 remains parked; MariaDB perf is unmeasured everywhere
and only the real box may produce numbers. The WP-21 legacy-UNIQUE
collision path (docs/STREAMS_PLAN.md §3.2 — a branch and mainline reporting
the identical test at the identical microsecond) is unit-tested on both
backends but has never been provoked against a real feed.

**WP-23 (long-running branch streams, its own migration) is next after this
ships**, and deliberately last — it should be justified by real WP-21/22
usage first, per `docs/STREAMS_PLAN.md` §5's own reasoning.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1739 OK (skipped=1) on wp-22-builds
```

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here. Frontend changes are verified by
`tests/test_frontend_calls.py` (static analysis of the actual `.js` source)
and, for pieces that carry real risk, by driving the actual JS through the
node DOM-shim harness in `.scratch/` against a real running server via a
real dynamic `import()` — the WP-22 pass this session is the second time
this method has been used, and it found a real bug the first static check
missed (see above). Neither proves layout, colour, contrast, or whether a
page is too much for one screen — only a real browser does, and no drop so
far has had one.

---

## The work waiting, in the order it wants doing

### 1. Accept and ship the combined WP-20+WP-21+WP-22 drop

See Thread A above. This is now the single largest piece of unshipped,
unreviewed work in the repo — three work packages, one drop, two schema
migrations, a whole new comparison-and-triage surface, and compare-any-two
on top of it.

### 2. Ship the 2026-08-07 drop's remaining MariaDB cutover steps

See Thread B above — independent of Thread A, and can proceed in parallel.

### 3. Merge the deferred work back — `wp-14-in-run-progress`

**Migration renumbers to 10 before merging** (registry §1, third
renumbering). Now further behind three whole work packages: expect
conflicts in `app.js`, `storage.py` (the backend seam, the upsert
maintaining `activity_hours`/`script_hours`, and now the stream
partition), and the status log.

### 4. WP-15 — progress pushes from a partial reader *(migration 10)*

Fully specified in `UPGRADE_PLAN.md` §WP-15. Note for the MariaDB era: its
migration entry is SQLite-only by definition; if the estate has cut over,
its schema change ALSO needs adding to the exporter DDL + a §D reload (or
hand-applied DDL via `testboard_migrate`) — decide when it lands.

### 5. WP-16 — site-specific info tab

Still noted, not specified.

### 6. WP-23 — long-running branch streams *(own migration, deliberately last)*

`docs/STREAMS_PLAN.md` §5. Gives a long-running branch its own triage/trend/
staleness instead of only a delta view. Should wait for real usage data from
WP-21/22 first — the plan's own reasoning for putting it last.

## Needs a person, not a commit

1. **Acceptance-test and ship the combined WP-20+WP-21+WP-22 drop** — the
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
