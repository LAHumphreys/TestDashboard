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
WP-15 on 8) move up behind it.

---

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

---

## 2. WP-20 — Products *(drop 1; one migration, no contract change)*

**Why.** Results from a second product are about to arrive. Without a
grouping, its environments pollute every estate view and the "one big number"
tiles become meaningless. Product is a read-time grouping of environments —
the cheapest possible slice, and it ships alone so the streams work never
blocks it.

**Already decided.** Environment → product is declared, one product per
environment. Deployments with one product (or none declared) must see **no
change whatsoever** — no switcher, no product column, no new words.

### 2.1 Schema (one migration; intended claim: next unshipped version)

```sql
CREATE TABLE environment_products (
    environment TEXT PRIMARY KEY,
    product     TEXT NOT NULL,        -- display name, shown as written
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL REFERENCES users(username)
)
```

No backfill. O(1). Same shape and lifecycle as `environment_expectations`.
An environment absent from the table belongs to the implicit product `""`.

### 2.2 API

- `PUT /api/environments/{env}/product` — body `{"product": "...", "username": "..."}`.
  Empty string clears the mapping. Mirrors the expectation endpoint.
- `/api/environments` — each row gains `"product"`.
- `/api/dashboard`, `/api/summary`, `/api/time`, `/api/timeline` — gain an
  optional `product=` query param, resolved server-side to
  `WHERE environment IN (...)` (environment count is small; the derived
  tables carry the cost, exactly as the existing environment filter does).
- `/api/summary` — response gains `"products": [{"product", "failing",
  "new_failures", "fixed", "unexpected_passes"}, ...]`, aggregated from
  `latest_runs` joined to the mapping — same table the tiles already read,
  same cost shape. Empty list when no products are declared: the frontend
  key for "do not show any of this."

### 2.3 Frontend

- Header gains the product switcher **only when ≥ 2 distinct products are
  declared** (the `products` list from `/api/summary` is the signal).
  Selection persists per browser (localStorage, like the What's-new unread
  dot) — a tester who owns one product lands scoped to it tomorrow.
- "All products" view: tiles carry the per-product split rows (top 4 +
  "+N more", per the mockup); the scope line labels each product's own
  window — **never one wall-clock phrase across products**
  (`WindowWordingTest` applies; each product's `stale_before` is its own).
- Triage and browse tables show a **Product** column only when the page
  spans products; when scoped, the table footer says
  "Scoped to <product> — no product column needed." A column of identical
  values is noise (established finding — keep the footer wording so the
  absence reads as deliberate).
- Environment management UI (where expectations are edited) gains the
  product cell.
- Time/Timeline pages: product scoping only via the existing environment
  filter semantics (`product=` resolves to environments); no new UI beyond
  the switcher scoping them.

**Upgrade-day bulk assignment (WP-23 addendum, decided with the user).**
Every pre-upgrade environment starts life unmapped (implicit product `""`)
until someone declares it — fine for a handful of environments, tedious for
an estate that already has dozens by the time products ship. The Open
Actions environment-management view (§2.3's product cell, above) gains a
small control, shown only while at least one environment is unmapped: a
product-name field plus an "Assign N unmapped environments" button that
issues the SAME `PUT /api/environments/{env}/product` the per-row Save
button already uses, once per currently-unmapped environment — **no new API
surface**, since the environment count is small and every PUT is
independently idempotent (a partial failure mid-batch just leaves the
remainder unmapped, safely retried by running the action again). Hidden
entirely once nothing is unmapped: zero visible change on an estate that
has already declared everything, or that never uses products at all.

**Rejected alternative: a standing default product for new environments.**
Considered and rejected. A standing default (e.g. "any environment with no
mapping belongs to product X") would be silent and permanent in exactly the
way this feature must not be: a NEW product's CI can begin pushing
branch/build results under a freshly-seen environment before a human gets
around to declaring its mapping, and a stream's `product` is fixed at the
moment the stream is first seen (§3.1/README "What upgrading means for
clients") — there is no admin action that moves an existing stream to a
different product afterwards. A standing default would silently and
irreversibly mislabel that product's environments the moment its first
result arrived, with no error and no obvious symptom until someone asked
why a stream showed up under the wrong product months later. The bulk
action above is deliberately the opposite shape: explicit, one-time, run by
a human who names the product being assigned, and it only ever touches
environments that are unmapped **at the moment it is clicked** — it is a
convenience for a backlog of already-known environments, never a rule
applied to environments not yet seen.

### 2.4 The Watchlist page (`static/watch.html` + `/api/watch`)

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

### 2.5 Feeder

Untouched. Product is not on the wire.

### 2.6 Tests

- Storage: mapping CRUD; summary per-product aggregation; unmapped
  environment lands in product `""`.
- API: `product=` filtering on each endpoint; unknown product = empty result,
  not 404 (a product exists by having environments). `/api/watch`: request
  order preserved; error cards for unknown scopes (`ok: false`, page still
  200); the 50-card cap refuses clearly; every card carries its own
  freshness field.
- Frontend (DOM-shim): switcher absent when < 2 products; product column
  present exactly when unscoped; localStorage persistence. Watch page: URL
  round-trip (parse → render → regenerate identical URL); error card
  rendering; bare-URL default loading; card links carry the right scope
  into `index.html`.
- Migration: standard §1.1 assertions; dual-backend variants.

### 2.7 Risks / not in this drop

- Risk: none structural — the migration is O(1) and the feature is read-time.
- Not in this drop: products on streams (WP-21 consumes the same mapping),
  per-product expectations (already per-environment, which is finer), any
  notion of product on the wire, stream cards on the Watchlist (`s:` is
  specced above so the grammar never changes, but it 400s with "streams
  arrive in a later drop" until WP-21).

**Done when:** a second product's environments can be declared, the switcher
appears, "All products" reads honestly, a manager can compose a
product+environment Watchlist and hand the URL to a colleague, and a
single-product deployment shows zero visible change except the new nav
entry. Suite green both backends; whatsnew + operator note per house rules
(migration runs ⇒ rollback is the database copy).

---

## 3. WP-21 — Streams and branch builds *(drop 2; one migration + contract extension)*

**Why.** "Did my branch break anything relative to mainline?" is the whole
question. Today branch CI results either pollute mainline or go nowhere.
Streams give branch runs a home that mainline never sees, and a delta view
that lists only differences.

**Already decided.** §0 items 2, 3, 5, 6. Plus, from review discussion:
compare against mainline's **current** latest results at read time, showing
the baseline timestamp — never snapshot/pin baselines (that would mean
snapshotting `latest_runs`; refused).

### 3.1 Schema (one migration; intended claim: next unshipped version at branch time)

```sql
CREATE TABLE streams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product    TEXT NOT NULL,          -- '' when env unmapped / mainline
    kind       TEXT NOT NULL,          -- 'mainline' | 'branch' | 'build'
    name       TEXT NOT NULL,          -- '' for mainline
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE (product, kind, name)
);
INSERT INTO streams (id, product, kind, name, first_seen, last_seen)
    VALUES (1, '', 'mainline', '', <now>, <now>);
ALTER TABLE runs ADD COLUMN stream_id INTEGER NOT NULL DEFAULT 1
    REFERENCES streams(id);            -- O(1); existing rows = mainline
-- latest_runs is DERIVED: rebuild it with the stream in the key.
-- (~12k rows; the WP-5 precedent. CREATE new, INSERT..SELECT with
--  stream_id 1, DROP, RENAME. Measure on a prod copy; state the number.)
--   PK (stream_id, environment, script, test_name)
--   plus index (environment, script, test_name) for "this test on every
--   stream" (WP-22 reads it; cheap to create now while the table is small).
ALTER TABLE comments ADD COLUMN stream_id INTEGER;   -- NULL = mainline-era
```

`activity_hours` / `script_hours`: **unchanged, and the writer must skip
maintaining them for `stream_id != 1`.** This is load-bearing: it is what
keeps branch runs out of staleness, the trend, and the Timeline without
touching those code paths at all.

### 3.2 The frozen v1 UNIQUE on `runs` — decision

Entry 1 declares `UNIQUE (environment, script, test_name, start_time)` and is
frozen; dropping a table-level UNIQUE in SQLite means rebuilding `runs`
(4.4M rows, network mount) — not permitted in a startup migration, and not
worth an offline stop-the-world tool for what it protects against.

**Decision: keep it, and treat it as a benign over-constraint.** The upsert
SELECT changes to key on `(stream_id, environment, script, test_name,
start_time)` — SELECT-then-UPDATE-or-INSERT as today, ids stable, `INSERT OR
REPLACE` still forbidden. If the SELECT misses but a row exists under the
legacy key on a **different** stream (a branch run microsecond-identical to a
mainline run: probability ~zero, but the constraint makes it impossible to
store), the record is **rejected** with an error naming both streams — a
visible per-record error in the import response, never a silent wrong-stream
update. Document it in the README contract section. The MariaDB schema comes
from the migration tooling and may carry the correct
`UNIQUE (stream_id, …)` from day one; the rejection behaviour must be
identical on both backends (tested).

### 3.3 Import contract (README section updated in the same commit)

Two new optional fields per record:

- `branch`: string, non-empty after stripping — this run belongs to branch
  stream `(product-of-environment, 'branch', name)`.
- `build`: same, kind `'build'`.
- Both present ⇒ that record is rejected (`"branch/build: mutually
  exclusive"`); the batch continues, per the one-bad-record rule.
- Absent ⇒ mainline. Unknown-key tolerance is unchanged for other keys.

Stream find-or-create happens inside the import transaction;
`streams.last_seen` is bumped there too. Product is resolved from the
record's environment via `environment_products` **at creation time** and
is then fixed — declare mappings before pointing branch CI at a
multi-product estate (README note). Re-uploading a name that exists is just
newer runs on that stream (this is also how build rebuilds work — WP-22).

**Response acknowledgment — the old-server trap.** Today's servers ignore
unknown keys, so a feeder sending `branch:` at a pre-WP-21 server would
silently import branch runs **into mainline**. Therefore the import response
gains `"streams_seen": ["branch:feat/x", ...]` (empty list for pure-mainline
batches), and the feeder — when invoked with `--branch`/`--build` — **must
abort with a clear error if the response lacks the acknowledgment**. This is
the same class of protection as the version-refusal rule: a mismatch refuses
loudly instead of corrupting quietly.

### 3.4 Behavioural rules in storage

- `_maintain_latest` writes the `(stream_id, …)` partition of `latest_runs`;
  estate queries pin `stream_id = 1`.
- **Un-retirement is mainline-only.** Today any run reporting a retired test
  clears the retirement; a *branch* run must NOT (the branch may predate the
  retirement). Widen the guard test that pins retirement-survives-reimport,
  and say so in the commit message.
- Byte-identical skip (`output_fingerprint`) works per run row — already
  stream-correct once the upsert key includes stream.

### 3.5 API

- Endpoints that read run data gain optional `stream=` (an id) — `dashboard`,
  `tests/{...}` detail/history, `runs/{id}` unchanged (id is global).
  Default: mainline. The summary/time/timeline endpoints stay
  mainline-only in this drop.
- `GET /api/streams?product=…` — the picker's data: id, kind, name, product,
  first_seen, last_seen, and a cheap latest verdict (failing count from its
  `latest_runs` partition). Streams with old `last_seen` are flagged stale
  by the *caller-visible* age, not by a hidden constant (report the
  timestamp; let the UI phrase it from data).
- `GET /api/compare?stream=A&baseline=B` — B defaults to mainline. Returns
  the five counts (`new_failures`, `new_passes`, `both_failing`,
  `new_tests`, `no_result`) plus one requested category as a paginated list.
  Implementation: joins of two `latest_runs` partitions on the triple —
  indexed, ~12k rows a side, no `runs` scan, pagination in SQL. Response
  carries both sides' identity and freshness: stream last_seen, baseline
  last_seen — the UI's honesty line ("baseline 11 days old — rerun to
  trust") is built from these, never from a constant.
- Comment POST accepts optional `stream_id`; comment GET returns it.

### 3.6 Frontend

- Toolbar gains the **Build picker** (default "Mainline nightlies"), listing
  the scoped product's branches; entries carry owner-free facts only (we
  have no owner concept — the mockup's `owner` field is **dropped**; the
  assignee system already covers "whose work is this" at test level).
- Scoping to a branch shows the **branch band** (sticky, visually loud,
  "Back to mainline") and swaps the dashboard body for the **delta view**:
  five tiles + tabbed paginated tables (`Mainline` / `This branch` result
  columns, ghost vs solid), "N tests agree and are not listed" line, run
  strip (solid latest / outlined superseded / striped running is dropped —
  we cannot know "running"; only recorded runs exist), coverage card
  ("N of M tests have a result", derived), baseline card with both
  timestamps.
- Test detail: `stream=` param scopes the history table and analytics
  (computed from that stream's runs of the triple — the existing endpoints
  filtered by stream); a compare strip (mainline chip → branch chip) when
  scoped; comments always shown in full with their "posted from" tag; the
  **same branch band as the dashboard** (shared `renderBranchBand`, not a
  second implementation) — found missing in first human use of the branch
  dashboard: a reader who clicked through to a test's own page from the
  delta table lost every indication they were scoped to a branch at all.
  "Back to mainline" is the CURRENT URL with only `stream` removed, never a
  fixed target — the dashboard's link strips to `index.html`, the test
  page's preserves `environment`/`script`/`test_name`, from one shared
  function.
- **Triage still works from a branch** (§0.4): the delta table's rows carry
  the same assignee select and inline Review expander (output, "View in
  timeline", comment) as every other list in the app — assigning from a
  branch row assigns the SAME test everyone else sees, never a
  per-stream partition of ownership. Retirement is refused on a branch row
  by construction (the shared review panel is simply never told a
  `staleBefore`, so its own gate never opens) — retirement stays a mainline
  decision (§3.4). The compare row payload carries the branch's own
  `stream_run_id`/`stream_start_time` (null exactly when there is no run on
  that side to review) and the triple's current, UNPARTITIONED `assignee` —
  no new query shape, both already live on `latest_runs`/`current_assignments`.
- **Assignment origin is an annotation, not a partition** (mirrors
  comments.stream_id exactly): `assignments`/`current_assignments` both
  gained a nullable `stream_id` — WHERE the assignment was MADE from, never
  which test it targets. `PUT .../assignee` accepts it optionally; the
  frontend sends the page's own scope when set, omits it on every mainline
  page (unchanged behaviour for every client that predates this). Folded
  into migration 9 itself (still unshipped when found) rather than spent on
  a migration 10 — see the entry's own comment in `storage.py`.
- **Open Actions shows the origin**: a row whose current assignment carries
  a non-mainline `stream_id` gets a small "branch feat/x" tag (names
  resolved via the same batch `streams` map pattern the comments endpoint
  established — `/api/dashboard`'s response carries one for whatever
  distinct streams are on the returned page), and a binary
  branch-vs-mainline filter chip pair (`origin=branch`/`origin=mainline` on
  `/api/dashboard`, server-side — the SAME rule the existing owner filter
  already follows, for the same reason: a client-side filter over one paged
  fetch turns "every branch-originated item" into "the ones that happen to
  be on this page"). The filter is entirely absent — not merely
  empty-valued — on any estate where no current assignment carries a
  stream (`/api/summary`'s `assignment_streams`, empty list is the signal),
  the same "zero visible change" rule every other WP-21/WP-20 addition
  follows.
- All new DOM via `textContent`/`createElement` — the mockup's
  innerHTML/template idiom does not port, per the frontend security rule.
- Mainline pages: **zero visible change when no non-mainline stream
  exists** — same rule as products.
- **Watchlist gains the `s:` card** (branch flavour). A branch card is a
  verdict card: the compare-vs-mainline headline ("2 new failures", "no
  new failures"), both freshness timestamps (its last run, the baseline's),
  and the stale warning derived from them. Click-through opens the
  branch-scoped dashboard. `/api/watch` resolves `s:` cards through the
  same storage reads as `/api/compare` counts — still O(derived). A
  manager's morning URL can now mix products, environments and the
  branches they are responsible for; this is the drop where the
  20-emails problem is substantially dead.

### 3.7 Feeder

- `--branch NAME` / `--build NAME` (mutually exclusive), stamped on every
  record of the invocation; local validation mirrors the server's.
- Requires the `streams_seen` acknowledgment (§3.3) or aborts before
  declaring success — exit code non-zero, replay files as usual.
- **State files are per stream**: the daily high-water-mark state must not
  be shared between a mainline invocation and a branch invocation (a branch
  catchup would otherwise fast-forward mainline's hwm or vice versa). Key
  the state file name on the stream, document it.

### 3.8 Tools

- `tools/drop_stream.py --db … --product P --kind branch --name N`
  (`--dry-run` first) — the `drop_environment` analogue for typo'd or dead
  streams: deletes the stream row, its runs, their outputs, its
  `latest_runs` partition. It cannot be undone; it never accepts stream 1.

### 3.9 Tests

- Storage: stream find-or-create in-transaction; upsert keyed with stream;
  legacy-UNIQUE collision ⇒ per-record rejection naming both streams (both
  backends); derived-table skip for non-mainline (activity_hours row count
  unchanged by a branch import — **guard test**, this is decision §0.5);
  un-retire is mainline-only (widened guard); compare counts and pagination.
- API: contract extension validation; `streams_seen` ack; compare endpoint
  correctness incl. `no_result`/`new_tests` directions.
- Feeder: flag stamping; ack-or-abort against a stubbed old server; state
  file separation.
- Frontend: band presence when scoped; NO RESULT never rendered as a result
  chip variant of pass/fail; delta table column headers name both sides.
- Migration: §1.1 assertions; latest_runs rebuild equivalence
  (fresh vs incremental identical); measured time on a prod copy in the
  migration comment and operator note.

### 3.10 Risks / not in this drop

- The latest_runs rebuild is the only migration step above O(1) — measure it
  (prod copy) before the drop; expect small (~12k rows) but say the number.
- Retention: branch runs accumulate inside `runs` with the same 1-year
  horizon as everything else; `drop_stream` is the pressure valve. Revisit
  only if usage shows a problem (measure first).
- Not in this drop: builds UI beyond accepting the field (WP-22), compare
  against non-mainline baselines (WP-22), per-stream triage/trend (WP-23),
  lifecycle states of any kind, Timeline/Time for streams.

**Done when:** a branch CI job can `--branch` its results, mainline is
provably untouched (guard tests), the delta view answers "what did my branch
change" in one screen, and an old feeder against the new server — and the new
feeder against an old server — both behave loudly-correctly.

---

## 4. WP-22 — Release builds and compare-any-two *(drop 3; no migration expected)*

**Why.** RCs and releases are the same mechanism as branches (a stream with
`kind='build'`) but a different reading: built when cut, not nightly; judged
against the build before it or against mainline; re-cut (rebuilt) under the
same name.

**Already decided.** §0 items 2, 6, 7. Rebuild = re-import under the same
`build` name; latest run wins; superseded runs render as ghosts.

### 4.1 Changes

- `GET /api/compare` loses the "baseline must be mainline" restriction:
  `baseline=` accepts any stream id in the same product. The SQL is already
  symmetric; this drop is mostly UI + tests. Comparing across products is
  refused with a clear error (nothing joins — the environments differ).
- Build picker gains the **Builds** group, newest first by `last_seen`,
  searchable (substring on the name as written — never parsed).
- Build-scoped dashboard: same delta machinery as WP-21 with a
  "Compare to <picker>" control (datalist combo; plain HTML) defaulting to
  the previous build by time where one exists, else mainline. The
  "built <ts> · nothing has run since" framing comes from `last_seen`.
- Test detail gains the **"Every build"** disclosure: this triple's latest
  result on each stream of its product, newest first — reads the
  `(environment, script, test_name)` index added in WP-21; row count is the
  stream count, bounded and small. NO RESULT rows say NO RESULT.
  **User has explicitly asked for this** (found missing during first human
  use of WP-21's branch dashboard, 2026-08-08 — a per-stream result
  switcher/dropdown on the test page was the specific request) — do not
  drop it from scope when this drop is planned.
- **Watchlist `s:` cards work for build streams** (same card machinery;
  the verdict line is "failing in <name>" plus its vs-previous-build delta
  when a predecessor exists). A release manager's URL is a row of RC cards
  beside mainline.
- Rebuild strip: run count per stream = its runs of the *suite window*;
  simple count of distinct import bursts is NOT derivable reliably — show
  run history (latest solid, older ghosted) and the timestamps; do not
  invent a "rebuild 3 of 3" ordinal the data cannot support. (The mockup's
  ordinal is dropped for honesty.)
- Optional, if wanted at build time: a declared RC/Released label —
  **deferred by default**; if requested, it is a
  `streams.state` declared via a small PUT + tool, retirement-pattern, its
  own migration, and it moves to WP-23's migration to stay one-per-drop.

### 4.2 Tests

Compare symmetric both directions; cross-product refusal; every-build table
correctness incl. NO RESULT; picker grouping/order; ghost rendering of
superseded runs.

### 4.3 Risks / not in this drop

- Stream count growth makes the picker long — group + search + stale-fold
  handles it; no pagination needed at realistic counts (measure if in doubt).
- Not in this drop: lifecycle declarations, per-stream triage.

**Done when:** an RC can be uploaded as `--build 2026.9.1`, re-cut under the
same name, judged against its predecessor and against mainline, and a test's
page answers "which builds fail this?" in one disclosure.

### 4.4 As built (2026-08-08, `wp-22-builds`)

Matches the plan above with these decisions made during implementation,
recorded here rather than left implicit:

- **Cross-product refusal rule, precisely:** refused whenever `baseline`
  is non-mainline AND its `product` differs from `stream`'s — checked
  regardless of which of the two happens to be mainline. An earlier pass
  checked this only when NEITHER side was mainline, which let
  `stream=<mainline id>&baseline=<a real product's stream>` through to
  silently compare against mainline's own product (`''`) instead of the
  baseline's real one; found in review and closed the same day (see
  `docs/UPGRADE_PLAN_STATUS.md`'s WP-22 review entry). No shipped
  frontend constructs that direction (`getSelectedStreamId()` is null
  for mainline, never the literal id), but the endpoint is a documented
  contract regardless.
- **"Searchable" (§4.1) IS a datalist combo for the Build picker too**,
  the same input+`<datalist>` pattern the "Compare to" control uses —
  reversing an earlier pass's decision to keep it a native `<select>`
  with optgroups. That decision rested on a premise that turned out
  false on review: a `<select>`'s own type-ahead only PREFIX-matches,
  so a release manager typing `rc2` against `2026.9.1-rc2` would find
  nothing, and "substring on the name as written" (this section's own
  words) was not actually true. `StreamPickerTest`'s assertions survive
  the rewrite unchanged — none of them exercise `<select>`-specific
  mechanics.
- **The per-triple endpoint is `GET /api/tests/{env}/{script}/{test}/streams`**,
  an extension of the test-detail SHAPE (a sibling sub-resource, not a
  field folded into the existing detail payload) — chosen because it is
  fetched independently and lazily (the "Every build" disclosure is
  collapsed by default) and because the SAME payload also drives the
  stream switcher, which needs to render before the disclosure is ever
  opened.
- **"Superseded runs render as ghosts"** (§4, opening paragraph) is
  **not implemented** — verified, not merely left alone: the history
  table draws every row solid. Newer-wins-as-latest and the older run
  remaining visible in history both hold; there is no solid/outline
  visual distinction between them. Out of scope for this drop per its
  own "nothing new to build if... already renders older runs" escape
  valve — flagged here so it is not silently assumed to exist.
- **The "optional declared RC/Released label" paragraph stays void**, per
  §0's closing decision — not built, not reconsidered.

---

## 5. WP-23 — Long-running branch streams *(drop 4; one migration)*

**Why.** A months-long feature branch with its own nightly CI is a second
mainline in all but name: it needs its *own* triage (new failures **on the
branch**), its own trend, and its own staleness — plus the delta view it
already has. Without this, a long branch's delta vs mainline drowns in drift.

**Deliberately last:** it multiplies the pass-detection machinery per stream
and should be justified by real usage of WP-21 first.

### 5.1 Schema (one migration)

`activity_hours` and `script_hours` gain `stream_id` in their PK — both are
derived tables; rebuild like latest_runs in WP-21. Sizes: activity_hours
≈ envs × hours × results (~175k rows/year at 5 envs) — rebuild affordable
but **measured on a prod copy first**, number in the operator note. The
writer then maintains them for every stream (the WP-21 skip is deleted —
its guard test is *widened* to assert partition isolation instead of
absence, and the commit message says so).

### 5.2 Changes

- `find_passes` / `recent_cutoff` / the summary tiles / the trend take a
  stream parameter; all existing clamps (36-hour fallback floor, 14-day
  ceiling) apply per stream unchanged — **do not remove them**.
- Branch-scoped dashboard for streams with enough cadence gains the two-tab
  header from the mockup: "Its own results" (the full mainline-style
  dashboard, scoped) / "Difference from <baseline>" (WP-21's delta view).
  "Enough cadence" is decided by the reader from data on screen (runs per
  day visible), not by a hidden threshold — both tabs always exist for a
  branch; the default tab may prefer "own results" when the stream has ≥
  some visible number of covered passes, and that heuristic must be stated
  in the UI caption, not buried.
- Drift framing: "behind by N commits" is **not knowable** (no VCS
  integration) — the mockup's `behind` line is dropped. What is knowable
  and shown: baseline freshness, this stream's window, and the
  both-failing count ("of N failing here, M fail on mainline too").
- Timeline/Time pages accept `stream=` (they read the now-stream-scoped
  hour tables).

### 5.3 Tests

Partition isolation guards (branch import changes only its partition —
mainline rows byte-identical before/after); per-stream pass detection incl.
clamps; migration rebuild equivalence + measured timings.

**Done when:** a long-running branch reads like its own small testboard, its
delta view separates "drift" from "mine," and mainline's numbers are provably
identical with and without the branch importing nightly.

### 5.4 As built (2026-08-09, `wp-23-longrunning`)

Matches the plan above with these decisions made during implementation,
recorded here rather than left implicit:

- **Migration 10 claims the version** (registry: WP-15's parked
  reservation moves to 11, the fifth such swap — see `UPGRADE_PLAN.md`
  §1's running note). `activity_hours`/`script_hours` rebuilt with
  `stream_id` in their PRIMARY KEY, existing rows copied with a literal
  `stream_id = 1` (both tables had been mainline-only since migrations
  6/7, so every row on file already IS mainline's) — the migration-9
  `latest_runs` precedent exactly, not a re-aggregate over `runs`.
  MEASURED on a copy of the dev database (220 MB, 540,192 runs, 12,008
  tests): **0.038–0.041s** for entry 10 alone (brought to v9 first),
  **~0.17–0.18s** for entries 8+9+10 combined from v7 (production's
  current version) — both consistently reproduced across repeated runs
  this session. Differs noticeably from the 2026-08-14 note's earlier
  v7→v9 number (0.806s, migration 9 alone 0.883s); the two sessions ran
  on the same machine at different times, and no attempt was made to
  reconcile the difference beyond noting it — see CLAUDE.md's "measure,
  do not estimate" rule: this entry reports what was actually measured
  this session, not a reconciled or averaged figure.
- **`find_passes`/`recent_cutoff` needed NO signature change.** Both are
  pure functions over whatever activity buckets/test counts they are
  handed; scoping to one stream is the same mechanism `_pass_view`'s
  own docstring already documented for WP-20's `product=` filter —
  restrict the *inputs* (`Storage.activity_buckets`/
  `test_counts_by_environment`, both gained a `stream_id` parameter,
  default mainline). The two clamps inside `recent_cutoff` (36h
  fallback floor, 14-day ceiling) are therefore unchanged code and
  apply per stream automatically — pinned by a test that a branch too
  sparse to have a single covered pass falls back to the exact same
  36-hour window mainline would use in the same spot.
- **A pre-existing WP-21-era class of bug, found by the sweep this
  package's own guard-test discipline demanded**: several reads of
  `latest_runs`/`activity_hours` had no stream filter at all —
  `Storage.test_counts_by_environment`, `script_test_counts`,
  `daily_result_counts` (plus its trend-memo cache key),
  `prune_runs_before`'s `prev_result` recomputation. Each was correct
  *before* migration 10 only because the tables in question held
  nothing but mainline's rows; once every stream is maintained
  (this drop), an unfiltered read silently mixes a branch's numbers
  into mainline's own coverage denominator, trend, or `prev_result` the
  moment a branch reports into the SAME environment mainline uses —
  exactly the scenario `docs/STREAMS_PLAN.md` §5's "mainline
  behaviour byte-identical" requirement is about. All closed in the
  migration-10 commit, each with a guard test that imports a branch
  into a shared environment and asserts the unscoped response/table is
  untouched.
- **The "own results" tab is real second dashboard, not a filtered
  view of mainline's**: `/api/summary`, `/api/time` and `/api/timeline`
  all gained an optional `stream=` (default mainline, so every existing
  caller is unaffected) that scopes status, trend, every triage queue,
  `queue_totals`, `top_failing_scripts`, duration rollups and the
  Timeline's blocks/rows to the requested stream's own partition.
  Catalog fields (`products`, `environments`, `scripts`, `assignees`,
  `assignment_streams`) stay estate-wide regardless — they answer "what
  exists", not "what this stream's results are".
- **Default-tab heuristic, stated exactly**: `/api/summary` gained
  `covered_passes` (the count of COVERED passes `_pass_view` already
  computes for the requested stream, in its own 14-day lookback). The
  branch dashboard's two-tab header (`static/app.js`,
  `initBranchDashboard`) defaults to "Its own results" when
  `covered_passes >= 2`, else "Difference from mainline" — both the
  count and the threshold appear literally in the caption's own
  sentence ("this branch has completed N passes in the last 14 days (2
  or more shows its own dashboard first)"), the `WindowWordingTest`
  discipline applied to a new kind of default rather than a recency
  window. **Both tabs always exist** for a `kind='branch'` stream;
  release builds (`kind='build'`) and anything else keep the exact
  WP-21/22 delta-only behaviour with no tab header at all — §5.2 frames
  the two-tab header as a branch concept, and an RC is not a second
  mainline with its own nightly cadence.
- **Drift framing landed as one line**, not a redesign of the delta
  view: baseline freshness and "this stream's window" already existed
  (WP-21's `delta-baseline` line); what was missing was "of N failing
  here, M fail on `<baseline>` too" (N = `new_failures + both_failing`,
  both guaranteed FAIL on the stream by `CompareCounts`' own
  definition; M = `both_failing`, the subset also failing on the
  baseline). "Behind by N commits" stays void, confirmed again: not
  knowable without VCS integration, not built.
- **The Watchlist `s:` card decision: vs-mainline verdict only, not
  widened.** §5.2 explicitly permits this ("if this makes the card
  crowded, prefer the vs-mainline verdict and note the decision") and
  it is the decision taken, for two reasons recorded in
  `static/watch.js`'s own comment: the card is already five stats, a
  headline and two freshness lines; and more concretely, `/api/watch`
  is architecturally O(cards) in Python but O(1) in QUERIES
  (`Storage.compare_counts_many` batches every requested stream's
  comparison in one query, pinned flat by a dedicated test) — a
  per-branch "own new failures" number needs its own per-stream
  pass-detection cutoff, which this endpoint has no batched
  multi-stream form of, and adding it would make N branch cards cost N
  times the pass-detection work. Click through to the branch's own
  dashboard tab instead.
- **Assignment origin extends to the own-results tab.** WP-21 already
  tagged an assignment made from a branch's delta table with
  `stream_id` (an annotation, never a partition — §0.4). This drop
  extends the same tagging to rows fetched while on a branch's "Its
  own results" tab (`tagStream()` in `static/app.js`), so Open Actions'
  existing branch-origin tag/filter (WP-21) is complete for both
  branch-scoped surfaces, not only the delta table.
- **Live-verified against a real server**, the same method WP-21/22
  established: a scratch database seeded with two products, a
  short-lived one-off branch (1 covered pass) and a long-running branch
  (8 nightly covered passes over 8 nights, its own standing regression
  plus one failure that also hits mainline from night 7), driven
  through the node DOM-shim harness with real `click()` dispatches on
  the two tab buttons — band text, tab visibility, caption wording
  (including the exact count/threshold), the default selection for
  both branches, the branch's own FAIL count differing correctly from
  mainline's, tab-switching both directions, the drift line's exact
  wording, and a genuine zero-stream-param mainline load touching none
  of the new elements at all. See `docs/drops/2026-08-14.md` for the
  full account.
- **F7 fix (usability sweep, found after first human use of the
  branch dashboard): §5.2's "Timeline/Time pages accept `stream=`"
  was true of the SERVER, not of the PAGES.** `/api/time` and
  `/api/timeline` gained `stream=` when this section was written, but
  `time.js`/`timeline.js` never read the param from their own URL nor
  forwarded it — a long-running branch's own time breakdown and
  running order were unreachable from the UI at all, only from `curl`.
  Fixed: both pages read `?stream=` at load, forward it to every
  request they make (not only the top-level page load — Timeline's
  row-expansion fetch and its test-search suggestions fetch hit
  DIFFERENT endpoints and would otherwise have silently read
  MAINLINE's data while the page itself read as branch-scoped), and
  render the shared branch band (`compare.js:renderBranchBand`) the
  same way `index.html`/`test.html` already do — both pages gained the
  `#branch-band` mount point to match. `/api/time` and `/api/timeline`
  both gained a `stream_identity` field (mirroring test detail's own
  `stream_identity`) so the pages have a kind/name to render the band
  from without an extra fetch. A deeper gap found in the same pass:
  `/api/scripts/{env}/{script}/runs` (the Timeline's row-expansion read
  of the raw `runs` table) had stayed hardcoded to mainline since WP-21
  even after migration 10 made the TOP-level blocks/rows stream-aware —
  expanding a row on a branch-scoped Timeline would have silently shown
  mainline's runs from the same time window instead of the branch's
  own. Fixed the same way, `stream=` accepted (default mainline, zero
  visible change when absent). Neither page has an in-page stream
  switcher of its own — the scope is fixed at load from the URL, the
  same way a Watch card's link or a shared deep link sets it.
- **F1/F2/F6 fixes (same usability sweep): three more links that
  silently dropped a branch's own scope, all fixed with the SAME
  pattern this section's F7 entry and §2.4's Watch-card fix already
  established — append `stream=`/`product=` from data already on hand,
  never a new query.**
  - **Open Actions.** A row whose CURRENT assignment carries a non-
    mainline origin (`assignment_stream_id`) linked to `test.html`
    WITHOUT `stream=` — "why is this broken in the RC" landed on
    mainline's view of the same test, exactly the ambiguity the
    origin tag beside the link already warns about. Fixed: that link
    now also carries `stream=<origin>` and `product=<the stream's own
    product>`, resolved from the SAME batched `state.streams` map the
    origin tag already reads (no per-row lookup). A mainline-
    originated row's link is unchanged. **Known limitation, by design
    of the row itself, not this fix:** the row's own `result`/`run_id`
    are always MAINLINE's (`/api/dashboard` lists stream 1 only), so
    the target link can point at a stream where the triple has no run
    at all — confirmed live: `GET /api/tests/.../{test}?stream=<origin
    that never ran it>` returns 404 (`"unknown test: no runs recorded
    for …"`), and `test.js` turns that into a plain "Could not load
    test: …" error banner rather than a blank page or silently-wrong
    data. Not fixed further: the coordinator's ask was literally this
    link, the failure mode is legible rather than misleading, and
    there is no query the frontend can afford per row to predict it
    (the whole estate-scale discipline this file keeps citing).
  - **`review.js`'s shared panel.** "View in timeline" (and the same
    bug found right beside it while fixing it — "Open full test
    page") dropped the stream: opened from a branch delta row — the
    case the bug report actually named — they deep-linked into
    MAINLINE's timeline/test page, where that run does not exist at
    all. `entry.stream_id` is stamped onto exactly the rows that need
    it two ways: `app.js`'s `tagStream()` on the branch "own results"
    tab's own rows, and `compare.js`'s `reviewEntry()` on every DELTA
    row (this is what actually answers the bug report — undefined
    everywhere else, including every Open Actions row, where the
    similarly-named `assignment_stream_id` is a DIFFERENT concept);
    appending it needed F7 landed first, since `timeline.html` could
    not read `stream=` before that.
  - **The branch dashboard's "Its own results" tab** gains two quick
    links into that SAME branch's own Time and Timeline pages
    (`renderBranchQuickLinks()` in `app.js`), shown only on that tab
    (hidden on "Difference from …" and on every mainline load) and
    kept in step with the dashboard's own environment filter. Needed
    F7 for the same reason. Both links also carry `product=` —
    `state.streamProduct`, stashed by `initBranchDashboard()` from the
    same `fetchCompare` payload `renderBranchBand` already reads —
    without it these are exactly the bug commit `4725bbc` fixed:
    Time/Timeline both load `products.js`, which adopts `?product=`
    into localStorage, so a browser holding a DIFFERENT product than
    the branch's own would render Timeline's environment picker
    filtered to the wrong product (caught in review before shipping —
    the first live check used a scratch estate with only the implicit
    `""` product, where wrong-product and right-product looked
    identical).
  - **Verified live**, same DOM-shim method, in two passes (the second
    to re-check the review finding above against a NON-empty product,
    since the first pass could not have told a wrong product from a
    missing one): a row assigned from a branch, opened on Open
    Actions, whose link carried the exact `stream=`/`product=`
    expected; the branch dashboard's own-results tab showing both
    quick links with the branch's actual `stream=2&product=Beacon`;
    and a review panel opened from BOTH the own-results tab and a
    branch delta row (the "New tests" category, since this scratch
    estate's branch tests never ran on mainline at all), whose "Open
    full test page" and "View in timeline" links both carried the
    branch's stream id in every case.
- **F4 (same usability sweep): the Watch card's unassigned-failing
  stat becomes a way in, not only a number.**
  - **`app.js`.** The browse filter row's state (`result=`, `unassigned=`,
    and — "for symmetry", trivial once the other two existed — `stale=`)
    is now read from the page's own URL at load, BEFORE
    `buildResultToggles()`/the sync calls paint the controls, so a deep
    link lands with its toggles already showing the state it set. New
    plain **"Unassigned only"** toggle chip, same `aria-pressed` pattern
    as `stale-toggle`/`retired-toggle`, wired to `/api/dashboard`'s
    existing `unassigned=1` (`include_unassigned`) filter — no new
    endpoint, no new query shape. Fixed the coordinator's own wording in
    passing: the server's param is `result=` (singular, repeatable — see
    `_parse_results_param`), never `results=`; the usability-batch
    message used the plural, which the server would silently ignore.
  - **`watch.js`.** `buildStat()` gained an optional third argument that
    turns the stat's value into a link — every other call site is
    unaffected. The "Unassigned failing" stat on EVERY card kind
    (product, environment, and stream) now links to `index.html`
    scoped to the card (its `cardLink()` params plus
    `result=FAIL&unassigned=1`, the toggle chip's own URL contract
    above) — clicking the number lands on exactly those rows, filtered,
    rather than only naming a count. Zero visible change when the stat
    itself is absent (`unassigned_failing` is 0) — the exact rule the
    stat's own existence already followed.

    A stream card's link was originally special-cased to OMIT
    `result=`/`unassigned=`, reasoning that "the delta view already
    shows every row's assignee inline" — **advisor-caught as wrong for
    a long-running branch**: a branch with `OWN_RESULTS_DEFAULT_PASSES`
    (2) or more covered passes defaults to "Its own results"
    (`app.js`'s `initBranchDashboard`), not the delta view at all, and
    that tab is the SAME browse table/filter row these params are
    built for (`activateOwnResultsTab()` calls `wireMainlineControls()`,
    the one place that reads them from the URL). Appending them to
    every card kind unconditionally is safe: on a build or a SPARSE
    branch (which still default to the diff tab), `compare.js`'s
    `initDeltaView` never reads `result=`/`unassigned=` from the URL at
    all, so they are simply inert there, not a wrong filter — the
    delta table's own inline assignees are still what a reader sees in
    that case, just without a redundant filtered detour.
  - **Perf**, per the coordinator's explicit ask: no new endpoint, no
    new query shape on either side — the toggle reuses `/api/dashboard`'s
    existing `unassigned=`/`assignee=` filters (already flat, already
    paginated), and the stat link is a client-side URL construction
    from data the card already has.
  - **Verified live**, same DOM-shim method: a page loaded
    `?environment=linux-sim&result=FAIL&unassigned=1` rendered with the
    "Unassigned only" chip and the FAIL toggle both already pressed, and
    exactly the one matching row on screen; a Watch page with a product,
    environment and stream card each showing "Unassigned failing" — all
    three stat links carried `result=FAIL&unassigned=1` beside their
    own scope (product/environment/stream). A SEPARATE live check, run
    after the advisor caught the stream-card special case above, seeded
    a long-running branch (4 covered passes, defaulting to "Its own
    results") with an unassigned failure and confirmed clicking its
    Watch card's stat lands there with the filter ALREADY applied —
    the exact case the original special-cased design would have missed.
- **F5 (same usability sweep): a build's delta view names BOTH
  canonical baselines, not just the one currently selected.**
  - **`compare.js`.** New `renderBuildVerdict(streamId, data,
    productStreams)`, fired FIRE-AND-FORGET (not awaited) from
    `initDeltaView()` right before `delta-section` becomes visible —
    its own fetch(es) never sit on the critical path between the main
    comparison landing and first paint. Renders "vs `<previous
    build>`: N new failures · M fixed — vs mainline: K new failures"
    under the section header (`#delta-verdict`, new mount next to
    `#delta-build-framing`), hidden outright for a branch (no
    "previous build" concept) or a product's first build (nothing to
    name as the predecessor). Whichever of the two canonical baselines
    (previous build, mainline) the page was actually opened against is
    already loaded — `data.counts` — so the common case costs exactly
    ONE extra counts-only `/api/compare` call for the other one; an
    explicit `?baseline=` naming a THIRD build costs two (both legs
    need fetching). A failed extra fetch hides the line rather than
    raising an error banner over a delta view that otherwise loaded
    fine — enrichment only. Guards against a stale render
    (`deltaState.streamId !== streamId`) the same way a page-wide
    `requestSeq` would, though in practice `initDeltaView` only ever
    calls this once per build page load (changing baselines via the
    "Compare to" control is a full navigation, not an in-place
    re-render).
  - **MEASURED COST (the coordinator's explicit ask — the "~15ms"
    figure in the usability-batch message was an assumption, not a
    measurement, and turned out to be off by roughly 10x at estate
    scale):** a synthetic ~12,000-test, 3-environment product (the
    same scale CLAUDE.md's "~12,000 tests a night" names), two builds
    each carrying the full set. Storage-layer `compare_counts`, 30
    samples after warmup:
    - Against a copy of the repo-root dev db (migrated up from a
      pre-streams schema, so it also carries that file's accumulated
      layout from months of history): **median 141–158 ms**.
    - Against a database built fresh at the identical scale (rules out
      the migrated file's layout as the explanation): **median
      115–125 ms** comparing against a REAL (non-empty) baseline
      partition — confirmed twice, since a first attempt at this
      measurement silently compared against an EMPTY partition (a
      seeding bug: two streams sharing one `start_time` collide on
      `upsert_runs`'s legacy-key check and the second is rejected,
      caught by row-count exactly 0 where 12,000 was expected) and
      returned a bogus ~45 ms.
    - End-to-end (HTTP + JSON) on the fresh db: **median ~125 ms** for
      the counts-only call, **~347–404 ms** for a full page (counts
      plus one category's rows) — for scale, the number CLAUDE.md's
      historical "15ms end-to-end" note was measuring on a smaller
      dataset than the full nightly estate.
    - Live end-to-end via the DOM-shim harness: delta-section's own
      render completed at +742ms from module load, the verdict line
      filled in at +870ms — a +128ms delay, matching the standalone
      measurement, confirmed to land AFTER first paint every time.
    All figures **dev-tier hardware, dev-scale data** — never
    production numbers. The design (fire-and-forget, no await on the
    critical path) was correct regardless of the true cost, but the
    real number is materially higher than the assumption behind the
    request, worth a look before a much larger product makes this
    verdict line the slowest thing on a build page.
  - **Verified live**, same DOM-shim method: a build's delta view
    (default baseline: its predecessor) filled in "vs build 1.0.0: 300
    new failures · 257 fixed — vs mainline: 342 new failures" —
    exact-template match — strictly after the delta section's own
    render completed, never before.
- **F3 (same usability sweep, smallest): the suite link on a
  stream-scoped test page stops silently switching context.**
  `script.html` has no stream support of its own — that is a
  decision-list item, out of scope here — so the suite link on
  `test.html` has always opened MAINLINE's execution history, even
  when the page itself is stream-scoped. `test.js`'s `renderDetail()`
  now says so honestly rather than fixing (or hiding) the mismatch:
  when `detail.stream_identity` is non-null, the link's `title`
  becomes "Execution history for this suite (mainline)" and a plain
  " (mainline)" text node is appended right beside the link itself —
  never hover-only, the same "never the only signal" rule this
  project applies to colour/state everywhere else. A mainline visit
  (`stream_identity` is always `null` there) is unchanged in both the
  title and the visible text. Verified live via the DOM-shim harness:
  a stream-scoped test page rendered both the annotated title and the
  visible note; a mainline visit rendered neither.

  **Superseded by script-page parity, PART B of this same later
  round** (below) — once `script.html` honours `stream=`, the link
  simply carries it through instead of needing an honesty label. Noted
  here now, ahead of that entry, so a reader mid-PART-A does not
  wonder why F3's hack is still live: it is, in this commit, and stops
  being true in the next one.
- **Link-matrix audit follow-up, PART A (FINAL ROUND): three more
  scope-carriage gaps into `test.html`**, found by a full audit of
  every link in the app against every surface that became reachable
  under stream scope across the F1–F7 sweep. All three are the
  identical pattern already established (append `stream=` from data
  already on hand, a no-op on mainline):
  - **`app.js`'s triage queue rows** (`queueColumns()`'s test column) —
    on a long-running branch's "Its own results" tab, a queue row is
    the branch's own, and its link must land back on that same stream's
    test page.
  - **`app.js`'s browse table rows** (`buildRow()`) — the same gap, the
    dashboard's own test list.
  - **`timeline.js`'s expanded run rows** — a stream-scoped Timeline's
    run rows (revealed by expanding a block) still linked to mainline's
    test page; their `test.html` links now carry `stream=` too. (The
    same audit found the block row's OWN `script.html` link still
    unscoped as well — that is PART B, below, since a `stream=` on
    that link is only useful once `script.html` actually reads it.)

  Verified live via the DOM-shim harness against a scratch server: a
  long-running branch (4 covered passes, defaulting to "Its own
  results") — its browse table's, and its "Still failing" queue's,
  test links both carried `stream=2`; a stream-scoped Timeline's
  expanded run row's test link carried it too.
- **Link-matrix audit follow-up, PART B (FINAL ROUND): script-page
  parity — the last mainline-only tool.** `script.html` (suite
  execution history) now accepts `?stream=`, the same as every other
  page. This closes the user's explicit requirement: "detailed
  analysis on a product's branch using all the same tools as
  mainline."
  - **`testboard/api.py`.** Both script endpoints now accept `stream=`
    (default mainline): `GET /api/scripts/{env}/{script}/runs` already
    did (F7); `GET /api/scripts/{env}/{script}/executions` gained it
    here, resolved the same way and echoed back as `stream` plus a
    `stream_identity` object (`None` on mainline) so the page can
    render its own branch band without a second fetch — the identical
    pattern `/api/time`/`/api/timeline` already established.
    `storage.script_runs()` **already carried a `stream_id` predicate
    in its SQL for every caller** (F7) — this change only changes
    which VALUE gets bound to it for this one endpoint, not the query
    shape at all. `storage.script_exists()` deliberately stays
    UNSCOPED, matching `/runs`'s own precedent: a script's identity is
    not partitioned by stream, only its runs are.
  - **`testboard/analytics.group_executions()` needed NO change**,
    confirmed by reading it: it is a pure function over whatever
    `Sequence[StoredRun]` it is handed, with no awareness of
    environment, script, or stream at all — the scoping happens
    entirely in what `storage.script_runs()` feeds it, one layer down.
  - **EXPLAIN QUERY PLAN verdict: byte-identical, not degraded.**
    `script_runs()`'s SQL text is unchanged by this drop (the
    `stream_id = ?` predicate was already there since F7); SQLite does
    not plan differently for different bound VALUES of the same
    literal predicate, confirmed by running the plan for both
    `stream_id=1` (mainline) and `stream_id=3` (a build) against the
    same real script — identical output both times:
    `SEARCH runs USING INDEX sqlite_autoindex_runs_1 (environment=? AND
    script=?)` followed by `USE TEMP B-TREE FOR ORDER BY`. Worth
    recording plainly since it is NOT what "bounded window" might
    suggest: the UNIQUE index's third column is `test_name`, before
    `start_time`, so a fixed `(environment, script)` prefix does not
    give SQLite an ordered-by-time seek — `start_time`/`stream_id` are
    both applied as row-level filters over that whole prefix, and the
    final `ORDER BY start_time` needs a temp b-tree regardless of
    scope. This is a PRE-EXISTING characteristic of `script_runs()`
    (unchanged since before F7), not something this drop introduces or
    worsens.
  - **Measured, dev-tier hardware, ~12k-test/540k-run dev-scale data**
    (a copy of the repo-root dev db — never called production, per
    house rules): `/api/scripts/{env}/{script}/executions` against a
    busy real script (1,350 mainline runs), 40 samples after warmup —
    **unscoped (the pre-existing behaviour): median 38.35 ms, p95
    40.55 ms**; **explicit `stream=1` (same value, the new code path):
    median 38.16 ms, p95 39.37 ms** — statistically indistinguishable,
    confirming the extra `_resolve_stream_id()` call costs nothing
    measurable on the hot (absent-param) mainline path, since it
    returns immediately without a query when `stream=` is not in the
    request. A build stream with far fewer runs for the same script
    answered in **median 5.19 ms** — faster, as expected, never slower.
  - **`static/script.js`.** Reads `?stream=` into `state.streamId` at
    init, forwards it to BOTH requests the page makes (`/executions`
    AND the "tests in this suite" table's `/api/dashboard` call — a
    DIFFERENT endpoint, found by the same discipline F7's timeline.js
    fix used: every outbound request needs the param, not only the
    first one), renders the shared branch band
    (`compare.js:renderBranchBand`) guarded the same way
    time.js/timeline.js/test.js already are, and its own test.html
    links now carry the stream through.
  - **`static/script.html`** gains the `#branch-band` mount, identical
    markup to every other stream-aware page.
  - **Every inbound link to `script.html` now carries the stream**:
    `static/app.js`'s `scriptLink()` (the app's one script.html
    link-builder), `static/timeline.js`'s block-row script link (found
    unscoped in the PART A audit pass, fixed here since it was only
    useful once `script.html` could read the param), and
    `static/test.js`'s suite link.
  - **F3 SUPERSEDED.** `test.js`'s suite link no longer needs the
    "(mainline)" honesty label — the title reverts to the plain
    constant it was before F3, and the link carries `stream=` through
    instead, the same scope-self-sufficient pattern every other
    inbound link to a scoped page follows. A mainline visit is
    byte-for-byte unchanged either way.
  - **Verified live**, same DOM-shim method, two passes: (1) a
    stream-scoped `script.html` rendered its executions, its branch
    band naming the branch, and its "tests in this suite" table's link
    carrying `stream=2`; the same page unscoped showed no band text and
    (as seeded) no executions for that mainline environment/script
    pair. (2) All three inbound links — `app.js`'s browse table's
    script column, `timeline.js`'s block row, `test.js`'s suite link —
    confirmed to carry `stream=2` into `script.html`, and `test.js`'s
    link confirmed to show the plain title with no visible "(mainline)"
    note any more.

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
