# Session handover — state of play

**Rewrite this file when the state changes. It is not a log.**
The log is [`UPGRADE_PLAN_STATUS.md`](UPGRADE_PLAN_STATUS.md) and is append-only; this
is a snapshot, and a snapshot that has been appended to is just a worse log.

Last rewritten: **2026-08-07**, after building WP-19 (MariaDB backend),
folding it with WP-18 into the **2026-08-07 drop** — and after that drop
**went live the same day**: migration 7 completed on the production database
(30 s–2 min, operator-reported) and the Timeline is accepted.

---

## Where the code is

| | |
|---|---|
| **production** | **live on the 2026-08-07 drop** (schema v7). Confirm the box runs the FINAL drop commit `310f1c0` — What's new saying "7 August 2026" is the tell; older says 4 August |
| `origin/master` | `ea15ccc` — **stale**: push `wp-18-timeline` to `master` so the recorded state matches the deployed one |
| `wp-18-timeline` | the 2026-08-07 drop, deployed. **All four CI legs green** including the new `python36-mariadb` |
| `wp-14-in-run-progress` | parked WIP; **its migration is 8** — renumber before merging (registry §1) |

Suite: **1385 green** (skipped 1) locally; **1749 OK (skipped 16)** on the CI
MariaDB leg — 10.3, prod's stream. Schema moves to **migration 7** at deploy
(the WP-18 half), so **the rollback is the database copy**, not `git
checkout`. WP-19 consumed no version; 8 stays reserved for WP-15, 9 free.

**SQLite is a permanent first-class backend** (user requirement, 2026-08-07):
`--db PATH` unchanged forever, zero-setup second instances, both backends
gated in CI. MariaDB is opt-in per instance via
`--db-config /etc/testboard/db.cnf --site-notes PATH`. The server never runs
DDL on MariaDB — schema comes from the migration tooling only.

The repo-root `testboard.db` is generated dev data (218 MB, 540,192 runs,
still v5 — current code migrates it on open, COPY FIRST). Production is
~900 MB / ~4.4M runs at **v6** on a network mount. Say which one any number
came from. **The dev machine also has MariaDB 12.3.2** (winget, x86_64 under
emulation, port 3307, datadir `.scratch/mariadb-data`, root password in
`.scratch/mariadb-test.cnf`): start with
`& "C:\Program Files\MariaDB 12.3\bin\mariadbd.exe" --datadir=<repo>\.scratch\mariadb-data --port=3307 --console`,
then `$env:TESTBOARD_TEST_DB_CNF=".scratch\mariadb-test.cnf"` activates the
364 dual-backend tests. Functional evidence only — never quote a perf number
from it.

## The immediate thing

**The MariaDB migration itself — the drop is already live.** Steps 1 of the
plan (deploy; migration 7; Timeline acceptance) happened 2026-08-07. What
remains: (2) §A server prep on the new account (target server confirmed
MariaDB 10.3.39 via client banner — re-confirm server-side with
`SELECT VERSION()`); (3) §C preflight, §E.1 **dry run on a prod copy**, then
the §E cutover with the freeze and feeder catch-up. Read
[`docs/drops/2026-08-07.md`](drops/2026-08-07.md) for the whole procedure
and the two small follow-ups (confirm the box is on `310f1c0`; push the
branch to `master`).

**Before migration day:** the runbook was re-reviewed and corrected
2026-08-07 (see the status log's entries of that date). §C's preamble
matters most — UTF-8 locale, and the tool-must-match-database-version rule.
Production is now at **v7**, so migrate with this branch's checkout, whose
exporter knows `script_hours`. Never with an older one: an older tool
silently skips tables it has never heard of and its own verification agrees
with the omission.

**Still open from earlier drops:** re-retire the tests the un-retire bug
released (search comments for "Automatically un-retired");
`tools/diagnose_db.py --compare-local` on the production server, never run.
Performance Phase 2 remains parked; MariaDB perf is unmeasured everywhere
and only the real box may produce numbers.

## First ten minutes of a new session

```bash
git log --oneline -5                  # where am I
git status --short                    # should be clean
python -m unittest discover           # expect 1385 OK (skipped=1)
```

**If the UI looks wrong, check you restarted the server.** Static files are
read per request; the Python is whatever was imported at process start.

There is no browser here. Frontend changes are verified by the node DOM-shim
harness in `.scratch/` (see the 2026-08-04 handover text in git history for
its pieces); it cannot catch layout, colour or contrast.

---

## The work waiting, in the order it wants doing

### 1. Ship the 2026-08-07 drop, migrate, cut over

Acceptance list in the operator note. Deploy pushes `wp-18-timeline` to
`master`. The cutover itself is §E of the runbook, gated on the dry run.

### 2. Merge the deferred work back — `wp-14-in-run-progress`

**Migration renumbers to 8 before merging** (registry §1). Now further
behind: expect conflicts in `app.js`, `storage.py` (backend seam + the
upsert maintaining `script_hours`), and the status log.

### 3. WP-15 — progress pushes from a partial reader *(migration 8)*

Fully specified in `UPGRADE_PLAN.md` §WP-15. Note for the MariaDB era: its
migration entry is SQLite-only by definition; if the estate has cut over,
its schema change ALSO needs adding to the exporter DDL + a §D reload (or
hand-applied DDL via `testboard_migrate`) — decide when it lands.

### 4. WP-16 — site-specific info tab

Still noted, not specified.

## Needs a person, not a commit

1. **Acceptance-test and ship the 2026-08-07 drop.**
2. Migration-7 probe on a prod copy (number into the drop note).
3. §A on the server (root), §E.1 dry run, cutover decision.
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
