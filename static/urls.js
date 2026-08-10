/* urls.js — the one place every internally-scoped URL is built (WP-24,
 * docs/SCOPED_URLS_PLAN.md).
 *
 * WHY: the products/streams work (WP-20..23) made URLs carry scope --
 * `product=` (adopts-and-sticks, products.js), `stream=`/`baseline=` (a
 * branch or build, compare.js/streams.js), `environment=` (the dashboard's
 * own filter). Every page built such URLs BY HAND with `URLSearchParams`,
 * and the SAME bug -- a link or fetch silently dropping one of these --
 * shipped at least eight times, each found by a human, each fixed with a
 * one-off per-site guard test (the incident list is in
 * docs/SCOPED_URLS_PLAN.md §1). A per-site guard catches each
 * *regression*; none of them could catch the *next* hand-built URL. This
 * module is what removes the pattern rather than the latest instance of
 * it: every page-navigation href and every /api/... fetch's query string
 * is built here, and tests/test_frontend_calls.py's
 * ScopedUrlConstructionTest fails the build on a hand-built one appearing
 * anywhere else.
 *
 * THE SCOPE MODEL. Four levels, read from (or written to) the query
 * string only -- never localStorage; the standing "sticky product"
 * preference is products.js's own concern (getSelectedProduct()), a
 * layer above this module, not inside it:
 *
 *   - `product`   -- adopts-and-sticks (products.js). Empty string ""
 *                    is an EXPLICIT choice ("All products" -- clears the
 *                    sticky selection the moment it is adopted); ABSENT
 *                    (null) means "say nothing", never touching whatever
 *                    this browser already has.
 *   - `stream`    -- a branch or build. Absent means mainline, the
 *                    default on every page. Never written as "".
 *   - `baseline`  -- what a stream is compared against. Absent means
 *                    the server's own default (mainline). EXPLICIT "1"
 *                    is how a caller states "mainline" on purpose (the
 *                    Compare-to control, withBaseline("mainline")) --
 *                    the build page's absence-default is NOT mainline
 *                    (it is "no baseline chosen yet"), so on that one
 *                    page absence may never be relied on to encode a
 *                    choice. Mainline's own stream id is 1
 *                    (storage.MAINLINE_STREAM_ID, seeded by migration 9).
 *   - `environment` -- the dashboard's own filter, OR (on test.html/
 *                    script.html/timeline.html's run rows) a plain
 *                    IDENTITY param naming which environment a row
 *                    belongs to. The two meanings never collide: an
 *                    identity value is always passed via `params`
 *                    (below), which wins outright over anything the
 *                    scope machinery would otherwise carry.
 *
 * HIERARCHY: product contains stream and environment; stream contains
 * baseline (`product ⊃ {stream ⊃ baseline, environment}`). Naming an
 * outer level as an override RESETS every inner level it contains --
 * UNLESS that same call also names the inner level explicitly, which
 * always wins over the automatic reset (cardLink() in watch.js sets
 * `product` and `environment` together on purpose; that is not a bug).
 *
 * DEFAULT SCOPE CARRIAGE: pageUrl()/apiUrl(), called with no `scope`
 * argument (or a `scope` that leaves a level unmentioned), CARRY the
 * CURRENT page's own value for that level, read fresh from
 * `location.search` on every call -- this is the case that was forgotten
 * seven times: a link built without deliberately restating the current
 * scope silently reset to mainline/no-environment/no-product. Passing an
 * explicit `null` for a level clears it outright (never carries, never
 * defaults) -- the mechanism `params.environment` relies on to keep an
 * IDENTITY value from ever being shadowed by a carried FILTER value one
 * has nothing to do with the other.
 *
 * `params` (the second argument to pageUrl()/apiUrl()) is page-specific,
 * non-scope data -- environment-as-identity, script, test_name, at,
 * result, sort, offset, and so on -- appended in the exact order the
 * object's own keys are written, so a converted call site reproduces its
 * previous query string byte for byte. A key that also happens to be one
 * of the four scope names (this is only ever `environment`, in practice)
 * is written from `params` and the scope machinery's own contribution
 * for that same level is suppressed for this call -- there is exactly
 * one value in the query string for any given key, and `params` always
 * wins.
 */

"use strict";

/** The four scope levels, and the hierarchy relationships resolveScope()
 * enforces: product resets stream/baseline/environment; stream resets
 * baseline. Order here has no meaning beyond listing every key once. */
const SCOPE_KEYS = ["product", "stream", "baseline", "environment"];

/** `overrides` genuinely NAMES `key` (including an explicit null/""),
 * as opposed to simply not mentioning it -- own-property check, not a
 * truthiness check, because "explicit null clears a level" has to be
 * distinguishable from "not mentioned, so carry the current value". */
function names(overrides, key) {
  return Boolean(overrides)
    && Object.prototype.hasOwnProperty.call(overrides, key);
}

/**
 * Read the current page's scope from `location.search` -- the one place
 * this parsing happens. Each field is the raw string value when the
 * param is present (including ""), or null when it is absent entirely --
 * `product`'s null/"" distinction is what lets pageUrl()'s default
 * carriage reproduce an already-explicit-empty `?product=` (a real "All
 * products" choice) while leaving an ordinary absent param alone.
 */
export function currentScope() {
  const params = new URLSearchParams(window.location.search);
  const read = (key) => (params.has(key) ? params.get(key) : null);
  return {
    product: read("product"),
    stream: read("stream"),
    baseline: read("baseline"),
    environment: read("environment"),
  };
}

/**
 * `currentScope()` merged with `overrides` per the hierarchy rule.
 * Every level not named in `overrides` carries its current value
 * unchanged. A level named in `overrides` (including as null) takes
 * that value outright, and -- for product/stream, which contain other
 * levels -- resets the levels it contains to null UNLESS this same
 * `overrides` object also names them, in which case the explicit
 * sibling value wins over the automatic reset.
 */
function resolveScope(overrides) {
  const current = currentScope();
  const result = {
    product: current.product, stream: current.stream,
    baseline: current.baseline, environment: current.environment,
  };

  if (names(overrides, "product")) {
    result.product = overrides.product;
    if (!names(overrides, "stream")) {
      result.stream = null;
    }
    if (!names(overrides, "baseline")) {
      result.baseline = null;
    }
    if (!names(overrides, "environment")) {
      result.environment = null;
    }
  }
  if (names(overrides, "stream")) {
    result.stream = overrides.stream;
    if (!names(overrides, "baseline")) {
      result.baseline = null;
    }
  }
  if (names(overrides, "baseline")) {
    result.baseline = overrides.baseline;
  }
  if (names(overrides, "environment")) {
    result.environment = overrides.environment;
  }
  return result;
}

/* ---------------- the four levels' own encodings ---------------- */

/** product: "" is EXPLICIT (write `product=`); null/undefined is absent
 * (write nothing at all) -- the one level whose empty string carries
 * meaning of its own (see the module docstring). */
function appendProductParam(qs, value) {
  if (value === null || value === undefined) {
    return;
  }
  qs.append("product", value);
}

/** stream/baseline/environment: absent OR "" both mean "write nothing" --
 * none of the three has a meaningful empty-string state. */
function appendPlainScopeParam(qs, name, value) {
  if (value === null || value === undefined || value === "") {
    return;
  }
  qs.append(name, String(value));
}

function appendScope(qs, resolved, skip) {
  if (skip.indexOf("product") === -1) {
    appendProductParam(qs, resolved.product);
  }
  if (skip.indexOf("stream") === -1) {
    appendPlainScopeParam(qs, "stream", resolved.stream);
  }
  if (skip.indexOf("baseline") === -1) {
    appendPlainScopeParam(qs, "baseline", resolved.baseline);
  }
  if (skip.indexOf("environment") === -1) {
    appendPlainScopeParam(qs, "environment", resolved.environment);
  }
}

/* ---------------- page-specific (non-scope) params ---------------- */

/**
 * Append `params`' own keys, in the order the object was written --
 * preserved by every engine this project runs on (ES2015+ guarantees
 * insertion order for string keys), which is what lets a converted call
 * site reproduce its previous query string byte for byte just by
 * writing its object literal in the same order the old `.append()`
 * calls ran in. A value that is an array or a Set (state.activeResults'
 * shape, e.g.) appends once per entry, the same as the hand-rolled
 * `for (const x of set) qs.append(key, x)` loops it replaces. null,
 * undefined and "" are all "omit this key", matching every hand-rolled
 * `if (x) { qs.append(...) }` guard this module replaces.
 */
function appendParams(qs, params) {
  if (!params) {
    return;
  }
  for (const key of Object.keys(params)) {
    const value = params[key];
    if (value === null || value === undefined || value === "") {
      continue;
    }
    if (Array.isArray(value) || value instanceof Set) {
      for (const entry of value) {
        qs.append(key, String(entry));
      }
      continue;
    }
    qs.append(key, String(value));
  }
}

/** The finished "?a=b&c=d" (or "" when nothing was ever written). */
function buildQuery(params, scope) {
  const qs = new URLSearchParams();
  appendParams(qs, params);
  const skip = params ? Object.keys(params) : [];
  appendScope(qs, resolveScope(scope), skip);
  const query = qs.toString();
  return query ? "?" + query : "";
}

/**
 * The pages a scope-carrying NAV BAR link may safely target — the ones
 * that actually READ scope params from their own URL AND are meant to
 * be reached from the header with the current scope attached. This is
 * NOT "every page pageUrl() can build a link to", and NOT "every page
 * that reads `stream=`": test.html and script.html carry `stream=` too
 * (their own call sites use pageUrl()'s default scope carriage
 * directly), they are simply never nav-bar targets. NOT `watch.html`
 * (its own URL grammar, `c=`, is untouched — a Watch card carries its
 * own scope, appending a global one would be a lie on top of a
 * different lie); NOT `actions.html` (WP-23 decision: assignments stay
 * one-owner-per-test and estate-level, never scoped to a stream —
 * docs/STREAMS_PLAN.md's decisions section); NOT `whatsnew.html`
 * (never scoped, ever). A page that ignores a param it receives is
 * harmless; sending one to a page it would MISLEAD is not. Moved here
 * (WP-24) from nav.js, which owns the SEPARATE question of which scope
 * levels a nav-bar link carries (nav.js's own CARRIED_PARAMS).
 */
export const NAV_SCOPE_PAGES = ["index.html", "time.html", "timeline.html"];

/* ---------------- the public surface ---------------- */

/**
 * An internal page link carrying scope. `page` is the bare page name
 * ("index" | "test" | "time" | "timeline" | "script" | "actions" |
 * "watch" -- watch composes its own `c=` params on top of this, see
 * watch.js's own module docstring for that exemption). `params` is
 * page-specific data (environment-as-identity, script, test_name, at,
 * result, ...); `scope` overrides one or more of the four scope levels
 * -- omit entirely to carry the CURRENT page's full scope unchanged,
 * the default that was forgotten seven times before this module existed.
 */
export function pageUrl(page, params, scope) {
  return page + ".html" + buildQuery(params, scope);
}

/**
 * An `/api/...` URL with the same scope semantics as pageUrl() --
 * replaces every hand-rolled per-page `summaryUrl()`/`listUrl()`/
 * `appendStream()`-style helper. `path` is the fixed part, e.g.
 * "/api/summary" or "/api/dashboard", or something built by
 * api.js's testApiPath()/runApiPath() for an identity-scoped endpoint.
 */
export function apiUrl(path, params, scope) {
  return path + buildQuery(params, scope);
}

/**
 * The CURRENT page's URL (its pathname, unchanged) with one scope level
 * overridden and the levels it contains reset -- what the pickers and
 * switchers navigate to. Every OTHER param this page's own URL already
 * carries (a page-specific filter this module knows nothing about, e.g.
 * timeline.html's `days`/`from`/`to`) survives untouched: only the four
 * scope keys are ever rewritten here.
 */
function currentUrlWithScope(overrides) {
  const url = new URL(window.location.href);
  for (const key of SCOPE_KEYS) {
    url.searchParams.delete(key);
  }
  const resolved = resolveScope(overrides);
  appendScope(url.searchParams, resolved, []);
  const query = url.searchParams.toString();
  return url.pathname + (query ? "?" + query : "");
}

/** The current page, scoped to a different stream (null = mainline).
 * Resets `baseline` (a stream's own comparison choice belongs to THAT
 * stream) per the hierarchy rule; leaves `product`/`environment` alone,
 * matching streams.js's Build picker, the one caller of this today. */
export function withStream(streamIdOrNull) {
  return currentUrlWithScope({
    stream: streamIdOrNull === null || streamIdOrNull === undefined
      ? null : String(streamIdOrNull),
  });
}

/** The current page, scoped to a different baseline. `"mainline"` is
 * the one caller-facing spelling for "compare against mainline,
 * explicitly" -- the build page's absence-default is NOT mainline (it
 * is "nothing chosen"), so this is the only path that may ever write
 * the explicit `baseline=1` encoding (mainline's own stream id, see the
 * module docstring); every other id is passed through as given. */
export function withBaseline(baselineIdOrMainline) {
  const value = baselineIdOrMainline === "mainline"
    ? "1" : String(baselineIdOrMainline);
  return currentUrlWithScope({ baseline: value });
}

/** The current page, scoped to a different product (""  = All products).
 * Resets `stream`/`baseline`/`environment` per the hierarchy rule --
 * every one of them belongs to the product they were chosen under, and
 * carrying them across a product switch is either contradictory (a
 * stream id from another product) or simply never chosen. */
export function withProduct(productOrEmpty) {
  return currentUrlWithScope({ product: productOrEmpty || "" });
}
