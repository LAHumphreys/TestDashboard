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
 * rule nav.js follows for the What's-new marker. It fetches its own tiny
 * slice of /api/summary (parts=headline) independently of whatever else
 * the page fetches, exactly as nav.js independently fetches whatsnew.html.
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
 * Render the switcher into `container` from the `products` list
 * `/api/summary` returns. Hides `container` (and clears a stale
 * selection) when fewer than two products are declared — the one
 * visible signal a single-product deployment ever sees from this file.
 */
export function renderSwitcher(container, products) {
  clearNode(container);
  if (!products || products.length < 2) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const names = products.map((entry) => entry.product);
  let selected = getSelectedProduct();
  if (selected && names.indexOf(selected) === -1) {
    // A product this browser remembered has since been renamed away.
    selected = "";
    setSelectedProduct("");
  }

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
    window.location.reload();
  });
  label.appendChild(select);
  container.appendChild(label);
}

async function init() {
  const container = document.getElementById("product-switcher");
  if (!container) {
    return;   // this page carries no switcher mount point
  }
  try {
    const data = await fetchJson("/api/summary?parts=headline");
    renderSwitcher(container, data.products || []);
  } catch (err) {
    /* Decoration: a failed fetch leaves the page exactly as it shipped. */
  }
}

init();
