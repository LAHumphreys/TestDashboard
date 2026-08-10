# Products & streams — design and work orders (WP-20 … WP-23)

Design for two new dimensions: **products** (test results from more than one
product in one testboard) and **streams** (branch builds and release builds
beside the mainline nightlies). Written 2026-08-08, from the claude.ai/design
exploration (`Products & branch builds v2`) reviewed against this codebase.

Read `UPGRADE_PLAN.md` §1 (migration registry) before starting any package
here. The registry is the coordination point; the version numbers named below
are **intentions, not claims** — a claim is an edit to that table in the same
commit as the migration, and the established pattern applies: whatever ships
next takes the lowest unshipped version, and parked reservations (currently
WP-15 on 11) move up behind it.

---

**Trimmed 2026-08-10 (docs tidy).** WP-20 … WP-23 all shipped (products,
streams, release builds/compare-any-two, long-running branch streams — see
`UPGRADE_PLAN_STATUS.md` and `docs/drops/2026-08-11.md` for the as-built
record and measurements). The detailed schema/API/frontend/test specs for
each drop, and their as-built addenda, are cut below — the shipped code and
that log are now the record, not this plan. Two things from the cut sections
still bind and are kept verbatim after §1. One correction to keep in mind
while reading §0 below: item 2's `branch`/`build` two-kind model was
collapsed to a single non-mainline kind, `build`, before the two-kind form
ever shipped anywhere (WP-25, night of 2026-08-09) — the current wire
contract is in `README.md`'s "Streams" section, not here.

## 0. Decisions already made (do not re-litigate without the user)

1. **An environment belongs to exactly one product.** Product is a declared
   grouping of environments — not a new component of test identity, not a
   field on the wire. (User, 2026-08-08.)
2. **Streams are implied by the upload, not registered.** An import record
   may carry `branch: "<name>"` or `build: "<name>"` (mutually exclusive,
   both optional). Absent both = mainline — which is what every deployed
   feeder already sends, so back-compat is free. There is no stream registry
   an operator maintains; the writer maintains an *observed* streams table as
   a side effect of import, exactly as environments already work.
   (User, 2026-08-08.)
3. **Branches of different products are distinct streams.** Stream identity
   is `(product, kind, name)`. Product is resolved from the record's
   environment at import time. (User, 2026-08-08.)
4. **Test identity stays the triple** `(environment, script, test_name)`.
   Comments, assignments and retirements stay attached to the triple —
   stream-agnostic — so work done from a branch view is visible from
   mainline and vice versa. (From the design exploration; matches the
   existing model.)
5. **Branch/build runs never feed the mainline trend, triage queues, or
   staleness.** Mainline's numbers are computed from mainline runs only,
   always. (Design exploration; non-negotiable per the estate rules.)
6. **A stream with no result for a test says nothing about it.** "No result"
   is never rendered as a pass, a fail, or an implied anything. The UI says
   NO RESULT and, where it matters, "removed or not run — the dashboard
   cannot tell which, so it claims neither."
7. **Version strings are opaque.** Build names are shown as written and never
   parsed; ordering everywhere is by run/import time (lexical on ISO
   timestamps, as usual).
8. **Ship in four drops.** Products first (cheapest, no contract change),
   then streams + short-lived branches (the core value), then release
   builds + compare-any-two, then long-running branch streams (own triage).
   Each is a dated drop with its own operator note.
9. **The Watchlist: one configurable morning view, shared by URL.**
   (User, 2026-08-08.) Part of the problem being solved is visibility — a
   manager responsible for some set of products / environments / branches
   today checks many emails; they must instead be able to compose ONE page
   showing the sets they care about, share it as a plain URL, and drill
   from any card into the existing detailed views. **The URL is the entire
   configuration** — no accounts, no server-side saved views (usernames
   are self-declared here; anything server-side would be everyone's to
   edit, and a URL is shareable by construction). The page ships in WP-20
   with product and environment cards and gains branch/build cards in
   WP-21/22; the card grammar is designed for that from day one.

**Item 4 reconfirmed (User, 2026-08-09), during the perf round's Open
Actions addendum.** The question was raised directly: should an
assignment be scoped per-stream instead of per-triple, now that a row's
displayed result can visibly disagree between mainline and its
assignment's origin stream (§5.4's ADDENDUM 3)? Decision: **no** — one
owner per triple, unchanged. The fix for the disagreement is
TRUTHFUL DISPLAY (show both sides), not a second axis of ownership;
splitting assignment by stream would mean the same failing test could
have a different owner on mainline and on every branch that touches
it, which is a different, larger feature nobody has asked for and
which item 4's own reasoning ("visible from mainline and vice versa")
was written to prevent.

Terminology: **"stream" is the internal name** (tables, code, API params).
The UI calls the picker **"Build"** with "Mainline nightlies" as the default
entry — testers pick builds and branches; they never need the word stream.

---

## 1. The data model in one screen

```
products      : declared. environment → product mapping table
                (environment_products; like environment_expectations).
streams       : observed. One row per (product, kind, name);
                kind ∈ {mainline, branch, build}. Row 1 is THE mainline
                stream (product '', name '') and is created by migration.
                first_seen / last_seen maintained in the import txn.
runs          : gains stream_id (DEFAULT 1 = mainline; O(1) ALTER).
                Upsert key becomes (stream_id, env, script, test, start_time)
                — see §3.2 for how this coexists with the frozen v1 UNIQUE.
latest_runs   : REBUILT (it is derived; ~12k rows) with
                PK (stream_id, environment, script, test_name).
                Estate views read stream_id = 1. A branch's latest results
                are the same table, different partition — the delta view is
                a join of two partitions.
activity_hours,
script_hours  : mainline-only until WP-23. The writer skips them for
                non-mainline runs; branch staleness in drops 2–3 derives
                from streams.last_seen, which is honest and cheap.
comments      : gain a nullable stream_id ("posted from") — annotation
                only; the comment lives on the triple.
everything
else          : unchanged. run_outputs, users, assignments,
                current_assignments, test_retirements, expectations.
```

What deliberately does **not** exist: a product column on `runs` (derived via
environment, always), per-stream retirement, per-stream assignment, a stream
lifecycle machine (deferred; see WP-22/23 — only *declared* or *derived*
states, never guessed).

Note (as-built): `kind` narrowed from `{mainline, branch, build}` to
`{mainline, build}` before this ever shipped (WP-25) — see the note at the
top of this document. Everything else in this data model shipped as drawn.

---

## Two decisions from the cut WP sections that still bind

**§2.3's rejected alternative: a standing default product for new
environments.** Considered and rejected. A standing default (e.g. "any
environment with no mapping belongs to product X") would be silent and
permanent in exactly the way this feature must not be: a NEW product's CI
can begin pushing branch/build results under a freshly-seen environment
before a human gets around to declaring its mapping, and a stream's
`product` is fixed at the moment the stream is first seen (README's "What
upgrading means for clients") — there is no admin action that moves an
existing stream to a different product afterwards. A standing default would
silently and irreversibly mislabel that product's environments the moment
its first result arrived, with no error and no obvious symptom until someone
asked why a stream showed up under the wrong product months later. The
bulk-assign action WP-20 shipped instead is deliberately the opposite shape:
explicit, one-time, run by a human who names the product being assigned, and
it only ever touches environments that are unmapped **at the moment it is
clicked** — a convenience for a backlog of already-known environments, never
a rule applied to environments not yet seen.

**§3.2's decision: the frozen v1 UNIQUE on `runs` is kept, and treated as a
benign over-constraint.** Entry 1 declares `UNIQUE (environment, script,
test_name, start_time)` and is frozen; dropping a table-level UNIQUE in
SQLite means rebuilding `runs` (4.4M rows, network mount) — not permitted in
a startup migration, and not worth an offline stop-the-world tool for what
it protects against. The upsert SELECT changed to key on `(stream_id,
environment, script, test_name, start_time)` — SELECT-then-UPDATE-or-INSERT
as always, ids stable, `INSERT OR REPLACE` still forbidden. If the SELECT
misses but a row exists under the legacy key on a **different** stream (a
branch run microsecond-identical to a mainline run: probability ~zero, but
the constraint makes it impossible to store), the record is **rejected**
with an error naming both streams — a visible per-record error in the
import response, never a silent wrong-stream update. Documented in the
README contract section. The MariaDB schema comes from the migration
tooling and carries the correct `UNIQUE (stream_id, …)` from day one; the
rejection behaviour is identical on both backends (tested).

---

## 6. Cross-cutting notes

- **Migration coordination.** Three of the four drops carry one migration
  each. At each branch cut, claim the lowest unshipped version in
  `UPGRADE_PLAN.md` §1 *in the same commit* and move parked reservations up
  (WP-15's is floating; the pattern is documented there).
- **MariaDB era.** Every schema change here must also land in the migration
  tooling's exporter DDL (`tools/migrate_to_mariadb.py`) and
  `testboard_migrate` — the server never runs DDL on MariaDB, and an old
  exporter silently omits tables it has never heard of (runbook rule).
  Dual-backend test variants are mandatory for every storage change above.
- **No new dependencies.** Everything above is stdlib + the existing
  vendored driver. Nothing shells out.
- **Docs per drop:** whatsnew section (tester-facing, only what shipped),
  operator note (suite count, schema version, migration yes/no + measured
  time, exact commands, rollback = database copy whenever a migration ran,
  what was not verified — no browser has rendered any of this before a
  drop; say so every time).
- **Wording rules carry over verbatim:** never label a window from a
  constant; text always carries meaning beside colour; solid = now,
  outlined = before; product/stream columns appear only when the page spans
  them.
- **Formerly-open questions — decided with the user, 2026-08-08:**
  1. **No declared lifecycle states, in any drop.** Stale-by-age only: the
     picker folds streams with old `last_seen` under a stale group;
     `drop_stream` deletes dead ones. Same model environments have always
     used. (WP-22's "optional declared RC/Released label" paragraph is
     void — do not build it.)
  2. **No retention policy yet — measure first.** `drop_stream` is the
     manual pressure valve; revisit with real growth numbers once WP-21
     has production history. No automatic pruning.
  3. `/api/summary` never aggregates across streams. Confirmed.
  4. Pushed digest (email/webhook) confirmed out of scope for all four
     drops; the Watchlist converts the problem to pull first. Revisit only
     if pull proves insufficient — it would be the project's first
     outbound network dependency.
  5. Watchlist URLs keep human-readable names; renames surface as error
     cards. Confirmed.
