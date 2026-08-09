/* products.js — the header product switcher (WP-20, docs/STREAMS_PLAN.md §2.3).
 *
 * WHY: results from a second product are about to arrive, and without a
 * grouping its environments pollute every estate view. The switcher lets
 * a tester who owns one product land scoped to it — but a deployment with
 * one product (or none declared) must see ZERO visible change, so this
 * renders nothing at all unless at least two products are declared.
 *
 * Mounted only on the pages that carry an `#product-switcher` element in
 * their header (index.html, actions.html, time.html, timeline.html,
 * watch.html) — a page that does not include this script, or that has no
 * mount point, is unaffected, the same "decoration that fails quietly"
 * rule nav.js follows for the What's-new marker. Pages that fetch no
 * `/api/summary` of their own (time.html, timeline.html, watch.html) get
 * it fetched here, independently, exactly as nav.js independently fetches
 * whatsnew.html. Pages that already fetch `/api/summary` for their own
 * headline data (index.html, actions.html) mark their mount point
 * `data-host-managed` and call `renderSwitcher` themselves once that
 * fetch lands — a second, redundant `/api/summary` request on the two
 * heaviest-traffic pages in the app is not an acceptable price for one
 * `<select>`, and this module has no way to know the host already has
 * the data short of being told.
 *
 * Selection persists per browser in localStorage (the same mechanism as
 * the What's-new unread state and the self-declared username) — there is
 * no account, so "my product" is remembered locally, not on the server.
 * Changing it reloads the page: every page already derives its own
 * filtered state at load time, so a reload is the simplest correct way to
 * apply a new scope rather than layering a second live-update mechanism
 * onto pages that each already have one.
 */

"use strict";

import { clearNode, el, fetchJson } from "./api.js";
import { withProduct } from "./urls.js";

/** localStorage key holding the selected product ("" = All products). */
const PRODUCT_KEY = "testboard.product";

/** The selected product, or "" for "All products" (storage unavailable
 * counts as "All products" too — never a hidden filter nobody chose). */
export function getSelectedProduct() {
  try {
    return window.localStorage.getItem(PRODUCT_KEY) || "";
  } catch (err) {
    return "";
  }
}

/** Store (or, for "", clear) the selected product. */
export function setSelectedProduct(product) {
  try {
    if (product) {
      window.localStorage.setItem(PRODUCT_KEY, product);
    } else {
      window.localStorage.removeItem(PRODUCT_KEY);
    }
  } catch (err) {
    /* Not being able to remember is not worth an error. */
  }
}

/**
 * "The URL wins, and winning makes it stick" (WP-23 bugfix,
 * docs/STREAMS_PLAN.md §0.9/§2.4) — the same principle the Watchlist's
 * own URL already follows: a link built from one product's scope (a
 * Watch card, a shared deep link) must reopen that SAME scope for
 * anyone, not whatever THIS browser's switcher last remembered. Found
 * live: an environment card link set `?environment=` correctly but the
 * switcher still read the OLD stored product from localStorage, so the
 * page rendered scoped to the wrong product — and an environment param
 * from another product under that scope resolves to an empty allow-list,
 * i.e. a silently blank page.
 *
 * A `product` query param — present at all, including empty — is
 * ADOPTED: it becomes both the rendered scope (every caller of
 * getSelectedProduct() sees it) AND the new stored selection, exactly
 * as if the user had picked it from the switcher themselves. No
 * `product` param at all is today's behaviour unchanged: read whatever
 * is already stored. An empty `product=` means "All products" and
 * clears the stored selection the same way manually picking "All
 * products" from the switcher already does.
 *
 * Runs unconditionally at MODULE EVALUATION time (a plain top-level
 * call, not inside the async init() below) so it completes before ANY
 * importing page's own init() can call getSelectedProduct() to build
 * its first request — ES modules evaluate an import's top-level code
 * before resuming the importing module's own, so this ordering holds
 * regardless of which page imports this file or in what sequence.
 */
function adoptProductFromUrl() {
  const params = new URL(window.location.href).searchParams;
  if (!params.has("product")) {
    return;   // no param at all: today's behaviour, read what is stored
  }
  setSelectedProduct(params.get("product") || "");
}

adoptProductFromUrl();

/**
 * Render the switcher into `container` from the `products` list
 * `/api/summary` returns. Hides `container` (and clears a stale
 * selection) when fewer than two products are declared — the one
 * visible signal a single-product deployment ever sees from this file.
 *
 * The stale/bogus-selection clamp below runs BEFORE the `< 2` early
 * return, not after: a `?product=` adopted from a stale or hand-typed
 * URL (adoptProductFromUrl() above, unconditional at module-eval time)
 * must not stick forever on a single-product or no-products install,
 * which has no switcher UI to ever clear it again otherwise.
 */
export function renderSwitcher(container, products) {
  clearNode(container);
  const names = (products || []).map((entry) => entry.product);
  let selected = getSelectedProduct();
  if (selected && names.indexOf(selected) === -1) {
    // A product this browser remembered (or just adopted from a URL)
    // does not exist — renamed away, or never real to begin with.
    selected = "";
    setSelectedProduct("");
  }
  if (!products || products.length < 2) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const label = el("label", "product-switcher-label", "Product");
  const select = document.createElement("select");
  select.id = "product-switcher-select";
  select.setAttribute("aria-label", "Product");
  const allOption = el("option", "", "All products");
  allOption.value = "";
  select.appendChild(allOption);
  for (const name of names) {
    const opt = el("option", "", name);
    opt.value = name;
    select.appendChild(opt);
  }
  select.value = selected;
  select.addEventListener("change", () => {
    setSelectedProduct(select.value);
    // NOT window.location.reload(): a page reached via a Watch card's
    // scope-self-sufficient link (or any other ?product= URL) would
    // reload the SAME query string, and adoptProductFromUrl() would
    // immediately overwrite this pick right back to the URL's product
    // on the very next load -- the switcher would silently snap back
    // to whatever the URL said, discarding the choice just made. "The
    // URL wins" only holds together if changing the switcher also
    // rewrites the URL, not just localStorage.
    //
    // withProduct() (WP-24, urls.js) resets stream/baseline/environment
    // per the scope hierarchy — a stream (and its baseline) and an
    // environment filter all belong to the product they were chosen
    // under. Carrying them across a product switch is either
    // contradictory (another product's stream id, an out-of-product
    // environment whose allow-list match is guaranteed empty) or at
    // best never-chosen — the same stale-scope family as the Build
    // picker's baseline carry-over.
    window.location.href = withProduct(select.value);
  });
  label.appendChild(select);
  container.appendChild(label);
}

async function init() {
  const container = document.getElementById("product-switcher");
  if (!container) {
    return;   // this page carries no switcher mount point
  }
  if (container.hasAttribute("data-host-managed")) {
    return;   // the host page already fetches /api/summary itself and
              // calls renderSwitcher directly — fetching here too would
              // be a second request for data the page already has
  }
  try {
    const data = await fetchJson("/api/summary?parts=headline");
    renderSwitcher(container, data.products || []);
  } catch (err) {
    /* Decoration: a failed fetch leaves the page exactly as it shipped. */
  }
}

init();
