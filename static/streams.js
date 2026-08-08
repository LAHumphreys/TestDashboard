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

/** Newest first by `last_seen` — WP-22's ordering rule for the Builds
 * group (docs/STREAMS_PLAN.md §4.1). ISO strings, so lexical compare is
 * chronological. */
function byNewest(a, b) {
  return a.last_seen < b.last_seen ? 1 : -1;
}

/**
 * Render the picker into `container` from the `/api/streams` list.
 * Hides `container` when there are no streams to choose from — the one
 * visible signal a mainline-only product ever sees from this file.
 *
 * WP-22 (docs/STREAMS_PLAN.md §4.1): builds get their own `<optgroup>`,
 * newest first by `last_seen` — the version-string-as-written rule
 * (§0.7) means it is the only ordering that means anything, since the
 * name itself is never parsed. Branches keep their own group, in the
 * order the API returned (unchanged from WP-21). A single-kind product
 * (only branches, or only builds — the common case before a release
 * process exists) renders one group, not an empty one beside it.
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

  const branches = streams.filter((s) => s.kind === "branch");
  const builds = streams.filter((s) => s.kind === "build").sort(byNewest);
  const groups = [
    ["Branches", branches],
    ["Builds", builds],
  ];
  for (const entry of groups) {
    const groupName = entry[0];
    const groupStreams = entry[1];
    if (groupStreams.length === 0) {
      continue;
    }
    const optgroup = document.createElement("optgroup");
    optgroup.label = groupName;
    for (const stream of groupStreams) {
      const opt = el("option", "", stream.kind + ":" + stream.name);
      opt.value = String(stream.id);
      optgroup.appendChild(opt);
    }
    select.appendChild(optgroup);
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
