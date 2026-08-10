# Scoped URLs — design and work order (WP-24)

One work package: **every internal URL the frontend builds — navigation
links and API fetches alike — goes through one scope-aware builder
module**, and a guard test makes hand-rolling a scoped URL anywhere else
a build failure. Written 2026-08-09 for an overnight run in a fresh
session; this document is self-contained on purpose.

Read first if you are the fresh session: `docs/SESSION_HANDOVER.md`
(state of play), this document (the whole spec), and the memory notes
(`sonnet-implementers-fable-reviews`, `production-estate-shape`). The
working model: a Sonnet subagent implements from this spec on a branch
cut from `wp-23-longrunning`'s tip (`wp-24-scoped-urls`); the
coordinator reviews every diff before pushing, checks all four CI legs,
and never touches `master`, `wp-18-timeline`, `wp-14-in-run-progress`,
or the repo-root `testboard.db`.

---

## 1. Why — the incident list is the argument

The streams/products work (WP-20..23, `docs/STREAMS_PLAN.md`) made URLs
carry scope: `product=` (adopts-and-sticks), `stream=`, `baseline=`,
`environment=`. Every page builds such URLs by hand with
`URLSearchParams`, and **the same bug shipped at least seven times**,
each found by a human, each fixed with a per-site guard test:

| # | Site | Defect |
|---|---|---|
| 1 | delta rows (`compare.js`) | test link dropped `stream=` — branch test pages unreachable by click |
| 2 | Open Actions rows (`actions.js`) | origin link dropped `stream=`/`product=` |
| 3 | Timeline run rows (`timeline.js`) | dropped `stream=` |
| 4 | header nav (`nav.js`) | dropped ALL scope crossing pages |
| 5 | queue + browse rows (`app.js`) | dropped `stream=` on the own-results tab |
| 6 | Compare-to combo (`compare.js`) | mainline encoded as PARAM ABSENCE on the one page whose absence-default is NOT mainline — explicit choice indistinguishable from no choice |
| 7 | Build picker / product switcher | STALE params carried across scope changes (baseline across streams; stream/baseline/environment across products) |
| 8 | Open Actions summary fetch (`actions.js`) | API fetch missing `product=` while the same page's rows fetch carried it — the server-side scoping fix helps only callers that send the param |

Per-site guards catch each *regression*; none can catch the *next*
hand-built URL. That is what this package removes.

## 2. Decided (do not re-litigate without the user)

1. **One module owns scoped URL construction** for both navigation
   hrefs and API fetch query strings. (User, 2026-08-09: "spec it".)
2. **The scope model and its two hard rules live in that module only:**
   - *Hierarchy resets:* product ⊃ {stream ⊃ baseline, environment}.
     Setting an outer scope deletes the inner params (commit `aeea626`'s
     rule, now centralized).
   - *Encodings:* `stream` absent ⇒ mainline (every page's default);
     `baseline` is EXPLICIT `1` for mainline (commit `89012d4` — the
     build page's absence-default is the predecessor build, so absence
     may never encode a choice); `product` empty string ⇒ All products
     (clears the sticky selection, per the adoption rule).
3. **Pure refactor.** No URL shape changes, no behavior changes; the
   existing link-carriage/adoption/reset guard tests are the oracle and
   must pass byte-for-byte unmodified except where they assert
   implementation shape (a test that greps for a hand-built pattern may
   be updated to grep for the builder call — assertion INTENT unchanged,
   say so per test in the commit).
4. **The Watch page's `c=` card grammar is exempt** — it is its own
   documented URL language (`STREAMS_PLAN.md` §2.4) with its own
   round-trip tests. The exemption is stated in the guard test with
   this reasoning, not silently.
5. Ship as part of the same pending drop (`docs/drops/2026-08-11.md`);
   no migration, no API change, no wire change.

## 3. The module — `static/urls.js`

ES6 module, stdlib-DOM only, same idiom as `api.js`. Surface (names
final unless implementation finds a genuine conflict — deviations
reported, not silent):

```js
// Read the current page's scope from location.search (one place).
// -> {product, stream, baseline, environment} (nulls where absent).
export function currentScope()

// An internal page link carrying scope. `page`: "index" | "test" |
// "time" | "timeline" | "script" | "actions" | "watch" (bare — watch
// composes its own c= params on top). `params`: page-specific params
// (environment/script/test_name/at/results/unassigned/...).
// `scope`: what to carry — DEFAULT: carry the current page's full
// scope (the common case that was forgotten seven times). Pass
// overrides to change one level; setting an outer level RESETS inner
// levels per the hierarchy rule. Explicit null clears a level.
export function pageUrl(page, params, scope)

// An /api/... URL with the same scope semantics. Replaces every
// hand-rolled appendProduct/summaryUrl-style helper.
export function apiUrl(path, params, scope)

// The scope-mutation helpers the pickers/switchers use: return the
// CURRENT page's URL with one scope level changed and inner levels
// reset. These are what streams.js/products.js/compare.js call.
export function withStream(streamIdOrNull)
export function withBaseline(baselineIdOrMainline)  // mainline -> "1"
export function withProduct(productOrEmpty)
```

Notes for the implementer:
- `nav.js`'s `carryScopeIntoNav()` becomes a thin `pageUrl` consumer;
  its page-allowlist (which pages receive scope) moves into `urls.js`
  as data, with the existing reasoning comment moved along with it.
- The clear-on-focus combos, the band back-links, the review panel, the
  every-build rows, the Watch `cardLink()` (which composes `pageUrl`
  under its own card grammar), the F6 quick links — every enumerated
  site in §1 plus the full construction-site list below converts.
- Known construction sites at spec time (verify by grep, the list may
  have grown): `app.js` 939/1279/1364/1615/1618 area, `compare.js`
  delta rows + combo, `review.js` 142/157 area, `script.js` 201,
  `timeline.js` 493/622, `streams.js` picker, `products.js` switcher,
  `nav.js`, `watch.js` cardLink/composer links, `actions.js` listUrl/
  summaryUrl + row links, `test.js` switcher/suite/every-build links.
- API-fetch conversion covers the scoped fetches (`product=`/`stream=`/
  `baseline=` carriers). A fetch with no scope semantics (e.g.
  `/api/users`) may stay plain — the guard distinguishes by shape (see
  §4), not by opinion.

## 4. Enforcement — the guard that ends the family

`tests/test_frontend_calls.py` gains a `ScopedUrlConstructionTest`:

1. **No page-URL literals outside `urls.js`**: any `".html?"` string
   concatenation or `location.href = ` assignment built from
   `URLSearchParams` in a `static/*.js` file other than `urls.js`
   fails, naming the file/line. Documented exemptions, each with its
   reason in the test: `watch.js` (own grammar, but it must still call
   `pageUrl` for the non-`c=` part — assert that separately).
2. **No scoped API params by hand**: the literals `"product"`,
   `"stream"`, `"baseline"` as first argument to `.append(`/`.set(` on
   a `URLSearchParams` outside `urls.js` fail. (`environment` is also a
   *data* param on some pages — the test allowlists the specific
   data-param sites it grandfathers, each with a comment, so new ones
   still fail.)
3. The existing per-site guards from §1 stay — they now test behavior
   through the builder.
4. A planted-regression test: a source string with a hand-built scoped
   URL must be caught by the detector (the compat-gate pattern —
   detectors must be shown able to fail).

## 5. Verification

- Full suite green at every commit (baseline at spec time: **1978 OK,
  skipped=1**; dual-backend 2650-ish — run it if the local MariaDB
  starts, report counts either way).
- DOM-shim end-to-end walks re-run on a live scratch server (never the
  shared one if one is running): the build-scoped nav walk, the Watch
  card jump walk, the delta drill-down walk, scope-reset on both
  pickers, mainline baseline selection. These exist from prior rounds —
  re-running them IS the refactor's acceptance.
- Endpoint re-timing storm afterwards (summary/watch warm+cold,
  compare, dashboard): this is a frontend refactor and must be a
  perf no-op; confirm, don't assume.
- No browser will have rendered it (say so in the drop note, as every
  round does).

## 6. Done when

Every construction site routes through `urls.js`; the enforcement test
fails on a planted hand-built URL and passes on the tree; all §1 guard
tests green unmodified in intent; suite green all four CI legs; docs
current (`STREAMS_PLAN.md` cross-reference, `whatsnew.html` needs NO
entry — this is invisible to testers by design — and the drop note's
"what changed" gains one line for the operator).

## 7. Risks

- **Biggest:** silent behavior drift during conversion (a site that
  carried scope accidentally gaining it, or vice versa). Mitigation:
  convert site-by-site in reviewable commits, each naming the site and
  asserting "shape unchanged" against its guard test.
- `actions.js` contains a literal NUL byte in its `UNASSIGNED`
  sentinel. It is PRE-EXISTING and DELIBERATELY untouched (two prior
  agents disagree on its intent; a human ruling is on the decision
  list). Byte-check it survives every edit to that file:
  `python -c "print(b'\\x00' in open('static/actions.js','rb').read())"`
  must stay `True`.
- The Watch exemption is the likeliest place for the next bug to hide —
  hence the separate assertion that `cardLink()` composes through
  `pageUrl` for everything except the `c=` grammar itself.
