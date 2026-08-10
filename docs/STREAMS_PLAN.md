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
record and measurements). Most of the detailed schema/API/frontend/test
specs for each drop, and their as-built addenda, are cut below — the
shipped code and that log are now the record, not this plan. Three things
from the cut sections still bind and are kept (two summarised, one — §2.4,
the Watch page's `c=` URL grammar, cited by name from a live guard test —
kept in full) after §1. One correction to keep in mind
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

### 2.4 The Watch page's `c=` URL grammar — kept in full, not a decision summary

**Restored 2026-08-10 (docs tidy)**, after the first trim cut it along with
the rest of §2: `tests/test_frontend_calls.py`'s `ScopedUrlConstructionTest`
names this section, by number, as the documented reason `watch.js` is
exempt from the shared `pageUrl()` builder ("its OWN `c=` grammar
(docs/STREAMS_PLAN.md §2.4), which has nothing to do with the
product/stream/baseline/environment scope model \[urls.js\] owns"), and
`static/watch.js`'s own comments cite it by number more than a dozen times
for the grammar, the staleness-suffix parsing, and the accent-precedence
rule. Losing the section would leave that citation pointing at nothing —
a bigger problem than the extra length, so unlike the rest of §2 this
subsection is reproduced whole rather than summarised.

The morning view (decision §0.9). One page, a grid of **cards**, each card
one scope, each card a link into the existing detailed view of that scope.

**URL grammar — the whole configuration.** Repeated `c=` params, order
preserved; each value is a one-letter kind, a colon, then the name
(URL-encoded; split at the FIRST colon only):

```
watch.html?c=p:Atlas&c=e:lab-alpha&c=e:dp-cert
   p:<product name>      product card
   e:<environment name>  environment card
   s:<stream id>         stream card (WP-21+; ids are stable and avoid
                         quoting product/kind/name triples in URLs)
```

**Declared staleness — an optional `@<n>h`/`@<n>d` suffix (WP-23, as
built).** Different scopes have different cadences (a daily branch, a
weekly build), and the URL is the whole configuration, so the expected
cadence is part of the card spec, not a page-wide setting:

```
watch.html?c=e:win-sim@36h&c=p:Atlas@7d&c=s:2@1d
```

Parsing splits at the **LAST** `@` in the name — never the first — and
only when the text after it matches `^\d+[hd]$`; names are free text and
may themselves contain `@` (`p:release@2026` has no valid tail, so the
whole thing is the name; `p:release@2026@1d` splits at the second `@`,
name `release@2026`, expectation `1d`). No suffix means no staleness
judgment at all — today's behaviour, byte for byte (`_parse_watch_spec`/
`_EXPECTED_SUFFIX` in `testboard/api.py`, mirrored in `static/watch.js`'s
`splitExpectedSuffix`/`EXPECTED_SUFFIX`, same regex on both sides).

A card whose spec carries `@` gets two extra response fields — `expected`
(the suffix, echoed) and `stale` (bool) — compared against the card's OWN
freshness timestamp: environment → `last_reported`; product → its
laggard's (the OLDEST-reporting environment — "everything reported" is
the bar, not "something did"); stream → `last_seen` (always present, so a
stream card is never stale-by-absence the way an environment/product card
can be). The composer offers a cadence choice (none / 1d / 7d / custom
hours) that round-trips through this grammar exactly.

**Unassigned-failure highlight (WP-23, as built).** Every ok card also
carries `unassigned_failing`: the count of tests in the card's own scope
whose latest result is FAIL and which have no current assignee
(assignments are triple-scoped and stream-agnostic — for an `s:` card the
question is "failing on THIS stream and the TEST has no assignee";
`e:`/`p:` cards are always mainline). Computed from exactly two aggregate
queries per request regardless of card count — `Storage.
unassigned_failing_by_environment()` (one row per environment, mainline)
and `Storage.unassigned_failing_by_stream()` (batched across every
requested stream id) — never a per-card query, preserving the flat-cost
property `test_query_count_does_not_grow_with_card_count` pins (7 → 8
queries for the FIRST card once this landed; still flat from 1 card to
the 50-card cap).

Frontend: `unassigned_failing > 0` gets the `watch-card-accent-fail`
border (reusing `--c-fail`) plus an explicit "Unassigned failing" stat —
colour is never the only carrier. `stale: true` gets a distinct
`watch-card-accent-stale` border, the SAME non-result amber `.tl-partial`
uses for a coverage warning (`#8a6d00`) — never `--c-fail`/`--c-fae`,
since staleness is a timing fact, not a failure. **Accent precedence when
a card is both:** the unassigned-failure accent wins the border (an owner
gap is the more actionable fact); the staleness TEXT LINE
("expected within 1d — last run 3 days ago", both halves real data —
never a hidden constant) renders independently of which accent wins.
`unassigned_failing === 0` and no `@` suffix ⇒ zero visual change, the
card looks exactly as it did before this feature existed.

A bare `watch.html` loads the browser's saved default (localStorage, same
mechanism as the What's-new unread state). "Save as my default" and
"Copy link" (a visible read-only input holding the URL — no clipboard-API
dependency) are the only two persistence affordances. Editing is on the
page: add-a-card picker, remove, drag-free reorder (up/down buttons —
keyboard-reachable beats drag).

**Cards say verdicts, dated.** A product card: failing / new failures /
fixed, its own window timestamp. An environment card: the same scoped to
the environment, plus its last-reported time (the env-pill fact). Every
card labels its freshness from its own data — `WindowWordingTest` applies
per card; there is no page-wide "as of" line because there is no single
truthful one. Card click-through: product → `index.html` scoped to it,
environment → `index.html` with the environment filter set, stream →
`index.html?stream=<id>`. **Every link is scope-self-sufficient (WP-23
bugfix, "as built"):** an environment or stream card's link ALSO carries
`?product=<its own product>` — including the empty string for an
environment nobody has mapped — because a link that only set the
environment/stream filter left the PRODUCT scope to whatever this
browser's switcher last had stored, which silently rendered under the
wrong product (an environment filter under the wrong product resolves to
an empty allow-list — a blank page, not an error). `index.html` (and
every other page the switcher appears on) ADOPTS a present `?product=`
param as both the rendered scope and the new stored selection —
"the URL wins, and winning makes it stick" (§0.9's principle, extended
from the Watchlist's own URL to a card's link) — so a shared Watch-card
link reopens the exact same scope for anyone, not whatever product they
happened to have selected last.

**A missing scope is an explicit error card, not a gap.** A shared URL
outlives renames and deletions; a card for an environment that no longer
reports says so on the card ("nothing under this name — removed or
renamed?"). Silently missing data is worse than an unexpected row
(established rule).

**`GET /api/watch?c=…&c=…`** answers the whole page in one request:
an array of card objects in request order, each `{spec, kind, ok,
headline numbers, freshness timestamps}` or `{spec, ok: false, error}`.
Every number comes from the derived tables (`latest_runs` aggregates,
`environment_expectations`, and in later drops `streams.last_seen` and the
compare counts) — a card is O(derived), never O(runs). Cards per request
capped at 50 (413-style refusal with a clear message; a URL that long is a
mistake, and the cap is stated in the README).

**Nav.** The page joins the header nav ("Watch"). Single-product,
no-streams deployments still benefit (environment cards), so unlike the
switcher it is NOT hidden — but it renders a short how-to empty state when
opened bare with no default saved.

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
