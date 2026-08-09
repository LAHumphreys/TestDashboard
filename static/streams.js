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
 * build streams, so a deployment that has not adopted WP-21 yet —
 * or a product with only mainline traffic — sees zero visible change,
 * the same rule products.js follows for a single-product deployment.
 */

"use strict";

import { getSelectedProduct } from "./products.js";
import { clearNode, el, fetchJson } from "./api.js";

/** Newest first by `last_seen` — WP-22's ordering rule for builds
 * (docs/STREAMS_PLAN.md §4.1). ISO strings, so lexical compare is
 * chronological. */
function byNewest(a, b) {
  return a.last_seen < b.last_seen ? 1 : -1;
}

/**
 * Render the picker into `container` from the `/api/streams` list.
 * Hides `container` when there are no streams to choose from — the one
 * visible signal a mainline-only product ever sees from this file.
 *
 * WP-22 (docs/STREAMS_PLAN.md §4.1): a plain `<input list=…>` +
 * `<datalist>` combo, not a `<select>` — "searchable (substring on the
 * name as written)" needs SUBSTRING matching (a release manager typing
 * "rc2" against "2026.9.1-rc2"), and a native `<select>`'s own type-ahead
 * only matches by PREFIX, which would find nothing for that exact case.
 * The same pattern compare.js's "Compare to" control uses: the typed or
 * picked TEXT is the label, matched back to an id via a map built fresh
 * on every render; an unrecognised value (a typo, or a stale suggestion
 * from before a page refresh) is a no-op, never a broken navigation.
 * ONE flat group, newest first by `last_seen` (WP-25, docs/ONE_KIND_PLAN.md
 * §1.4: the branch/build split died with the `branch` kind — there was
 * never more than one group of streams to browse, only one group's
 * worth of code pretending there might be two) — the version-string-as-
 * written rule (§0.7) means recency is the only ordering that means
 * anything, since the name itself is never parsed.
 */
export function renderPicker(container, streams, selectedId) {
  clearNode(container);
  if (!streams || streams.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const label = el("label", "stream-picker-label", "Build");
  const input = document.createElement("input");
  input.type = "text";
  input.id = "stream-picker-input";
  input.setAttribute("list", "stream-picker-options");
  input.setAttribute("aria-label", "Build");

  const datalist = document.createElement("datalist");
  datalist.id = "stream-picker-options";

  const labelToId = {};   // display text -> id ("" for mainline)
  const mainlineLabel = "Mainline nightlies";
  labelToId[mainlineLabel] = "";
  const mainlineOpt = document.createElement("option");
  mainlineOpt.value = mainlineLabel;
  datalist.appendChild(mainlineOpt);

  let selectedLabel = mainlineLabel;
  for (const stream of streams.slice().sort(byNewest)) {
    const text = stream.kind + ":" + stream.name;
    labelToId[text] = String(stream.id);
    const opt = document.createElement("option");
    opt.value = text;
    datalist.appendChild(opt);
    if (selectedId && stream.id === selectedId) {
      selectedLabel = text;
    }
  }

  input.value = selectedLabel;
  // A datalist filters its suggestions by the input's CURRENT text, so
  // a box already holding "Mainline nightlies" offers nothing until the
  // user hand-deletes it — reported as "switching off mainline is
  // annoying" by the first person to use it (2026-08-09). So: empty the
  // box on focus (every option becomes visible, typing starts a fresh
  // substring search), and put the old value back on blur if nothing
  // was chosen — an aborted click must never leave the control blank,
  // because the box doubles as the ONLY statement of the current scope.
  input.addEventListener("focus", () => {
    input.dataset.restore = input.value;
    input.value = "";
  });
  input.addEventListener("blur", () => {
    if (input.value.trim() === "" && input.dataset.restore) {
      input.value = input.dataset.restore;
    }
  });
  input.addEventListener("change", () => {
    const target = labelToId[input.value];
    if (target === undefined) {
      return;   // not a recognised option -- leave the page as it is
    }
    delete input.dataset.restore;   // a real choice; nothing to restore
    const url = new URL(window.location.href);
    if (target) {
      url.searchParams.set("stream", target);
    } else {
      url.searchParams.delete("stream");
    }
    // A baseline belongs to the scope it was chosen in: carrying an
    // RC's explicit baseline into a branch (a legal but never-chosen
    // comparison) or into mainline (a dangling param) is the stale-
    // scope cousin of the choosing-mainline sentinel bug. Changing
    // stream resets it; the new scope re-derives its own default.
    url.searchParams.delete("baseline");
    window.location.href = url.toString();
  });

  label.appendChild(input);
  container.appendChild(label);
  container.appendChild(datalist);
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
