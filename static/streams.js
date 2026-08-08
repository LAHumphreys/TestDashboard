/* streams.js — the dashboard's Build picker (WP-21, docs/STREAMS_PLAN.md
 * §3.6).
 *
 * WHY: a branch or build's results are about to arrive beside mainline's,
 * and the picker is how a tester leaves mainline to look at one.
 *
 * Selection lives in the URL ONLY (`?stream=<id>`), never localStorage —
 * unlike the product switcher, a branch is something you are looking AT
 * right now, not a standing preference. It follows the Watchlist's "the
 * URL is the whole configuration" rule (docs/STREAMS_PLAN.md §0.9): a
 * link to a branch-scoped dashboard has to reopen scoped to that branch
 * for anyone, with no browser state involved. Picking an entry navigates
 * (a real `location.href` change, not a client-side swap) — compare.js's
 * initDeltaView(), which does the actual swap, reads the same `?stream=`
 * on the next load.
 *
 * Mounted only on index.html, where the toolbar this picker lives in is
 * — a page with no `#stream-picker` element is unaffected. Renders
 * nothing (a hidden container) when the current product has no declared
 * branch/build streams, so a deployment that has not adopted WP-21 yet —
 * or a product with only mainline traffic — sees zero visible change,
 * the same rule products.js follows for a single-product deployment.
 */

"use strict";

import { getSelectedProduct } from "./products.js";
import { clearNode, el, fetchJson } from "./api.js";

/**
 * Render the picker into `container` from the `/api/streams` list.
 * Hides `container` when there are no streams to choose from — the one
 * visible signal a mainline-only product ever sees from this file.
 */
export function renderPicker(container, streams, selectedId) {
  clearNode(container);
  if (!streams || streams.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const label = el("label", "stream-picker-label", "Build");
  const select = document.createElement("select");
  select.id = "stream-picker-select";
  select.setAttribute("aria-label", "Build");
  const mainlineOption = el("option", "", "Mainline nightlies");
  mainlineOption.value = "";
  select.appendChild(mainlineOption);
  for (const stream of streams) {
    const opt = el("option", "", stream.kind + ":" + stream.name);
    opt.value = String(stream.id);
    select.appendChild(opt);
  }
  select.value = selectedId ? String(selectedId) : "";
  select.addEventListener("change", () => {
    const url = new URL(window.location.href);
    if (select.value) {
      url.searchParams.set("stream", select.value);
    } else {
      url.searchParams.delete("stream");
    }
    window.location.href = url.toString();
  });
  label.appendChild(select);
  container.appendChild(label);
}

async function init() {
  const container = document.getElementById("stream-picker");
  if (!container) {
    return;   // this page carries no Build picker mount point
  }
  try {
    const product = getSelectedProduct();
    const data = await fetchJson(
      "/api/streams?product=" + encodeURIComponent(product));
    const rawStream = new URLSearchParams(window.location.search)
      .get("stream");
    const selectedId = rawStream ? parseInt(rawStream, 10) : null;
    renderPicker(container, data.streams || [], selectedId);
  } catch (err) {
    /* Decoration: a failed fetch leaves the page exactly as it shipped. */
  }
}

init();
