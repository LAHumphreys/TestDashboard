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
environment → `index.html` with the environment filter set.

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
