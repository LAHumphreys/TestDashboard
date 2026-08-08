/* watch.js — the Watchlist: a shareable, URL-configured grid of verdict
 * cards (docs/STREAMS_PLAN.md §0.9/§2.4).
 *
 * THE URL IS THE WHOLE CONFIGURATION. Repeated `c=kind:name` params, order
 * preserved — there is no account and no server-side saved view, only
 * this browser's own "default" in localStorage (the same mechanism as
 * the What's-new unread dot). One request (/api/watch) answers every
 * card at once; each card carries its OWN freshness fields, because a
 * page that can mix a product and an environment with different windows
 * has no single truthful "as of" line to show.
 *
 * All data reaches the DOM via textContent/createElement, never innerHTML.
 */

"use strict";

import {
  clearError,
  clearNode,
  el,
  fetchJson,
  formatTime,
  showError,
} from "./api.js";

/** localStorage key holding this browser's default Watchlist query. */
const DEFAULT_KEY = "testboard.watch.default";

const state = {
  specs: [],   // [{kind, name}], in display/request order
};

/* ================= URL grammar =================
 *
 * "kind:name", split at the FIRST colon (docs/STREAMS_PLAN.md §2.4) — a
 * product or environment name may itself contain a colon, and the kind
 * letter never does.
 */

/** "e:lab-alpha" -> {kind: "e", name: "lab-alpha"}. */
export function splitSpec(spec) {
  const at = spec.indexOf(":");
  if (at === -1) {
    return { kind: spec, name: "" };
  }
  return { kind: spec.slice(0, at), name: spec.slice(at + 1) };
}

/** {kind, name} -> "e:lab-alpha", the inverse of splitSpec. */
export function joinSpec(entry) {
  return entry.kind + ":" + entry.name;
}

/** Parse a location.search string into [{kind, name}, ...], order kept. */
export function parseSpecs(search) {
  const params = new URLSearchParams(search);
  return params.getAll("c").map(splitSpec);
}

/** Build "watch.html?c=...&c=..." from a list of {kind, name} specs. */
export function buildUrl(specs) {
  const params = new URLSearchParams();
  for (const entry of specs) {
    params.append("c", joinSpec(entry));
  }
  const qs = params.toString();
  return qs ? "watch.html?" + qs : "watch.html";
}

/* ================= persistence ================= */

function readDefault() {
  try {
    return window.localStorage.getItem(DEFAULT_KEY);
  } catch (err) {
    return null;
  }
}

function writeDefault(query) {
  try {
    window.localStorage.setItem(DEFAULT_KEY, query);
  } catch (err) {
    /* Not being able to remember is not worth an error. */
  }
}

/* ================= rendering ================= */

/** Where a card's "Open in dashboard" link goes, or null (unknown kind). */
function cardLink(card) {
  const params = new URLSearchParams();
  if (card.kind === "product") {
    params.set("product", card.name);
  } else if (card.kind === "environment") {
    params.set("environment", card.name);
  } else {
    return null;
  }
  return "index.html?" + params.toString();
}

function buildStat(label, value) {
  const stat = el("div", "watch-stat");
  stat.appendChild(el("span", "watch-stat-value", String(value)));
  stat.appendChild(el("span", "watch-stat-label", label));
  return stat;
}

/**
 * One verdict card's freshness line, built from ITS OWN data — never a
 * page-wide "as of" line, and never the word "night" (a card's window
 * is derived from when ITS environments actually ran, which can be any
 * hour, more than once a day, or a fortnight ago).
 */
function freshnessLine(card) {
  const bits = [];
  if (card.last_reported) {
    bits.push("last reported " + formatTime(card.last_reported));
  }
  bits.push("counted as quiet after " + formatTime(card.stale_before));
  return bits.join(" — ");
}

function buildOkCard(card, index, total) {
  const div = el("div", "card watch-card");
  const head = el("div", "watch-card-head");
  head.appendChild(el("span", "watch-card-kind", card.kind));
  head.appendChild(el("span", "watch-card-name", card.name));
  div.appendChild(head);

  const verdict = el("div", "watch-card-verdict");
  verdict.appendChild(buildStat("Failing", card.failing));
  verdict.appendChild(buildStat("New failures", card.new_failures));
  verdict.appendChild(buildStat("Fixed", card.fixed));
  div.appendChild(verdict);

  div.appendChild(el("p", "watch-card-fresh muted", freshnessLine(card)));

  const link = cardLink(card);
  if (link) {
    const a = document.createElement("a");
    a.href = link;
    a.className = "watch-card-open";
    a.textContent = "Open in dashboard →";
    div.appendChild(a);
  }

  div.appendChild(buildCardControls(index, total));
  return div;
}

function buildErrorCard(card, index, total) {
  const div = el("div", "card watch-card watch-card-error");
  const head = el("div", "watch-card-head");
  head.appendChild(el("span", "watch-card-kind", card.kind));
  head.appendChild(el("span", "watch-card-name", card.name));
  div.appendChild(head);
  div.appendChild(el("p", "watch-card-error-text", card.error));
  div.appendChild(buildCardControls(index, total));
  return div;
}

/** Remove / up / down — drag-free reorder, keyboard-reachable. */
function buildCardControls(index, total) {
  const row = el("div", "watch-card-controls");
  const up = el("button", "link-btn", "↑ Move up");
  up.type = "button";
  up.disabled = index === 0;
  up.addEventListener("click", () => moveCard(index, -1));
  row.appendChild(up);

  const down = el("button", "link-btn", "↓ Move down");
  down.type = "button";
  down.disabled = index === total - 1;
  down.addEventListener("click", () => moveCard(index, 1));
  row.appendChild(down);

  const remove = el("button", "link-btn watch-card-remove", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => removeCard(index));
  row.appendChild(remove);
  return row;
}

/* ================= data ================= */

function currentQuery() {
  const params = new URLSearchParams();
  for (const entry of state.specs) {
    params.append("c", joinSpec(entry));
  }
  return params;
}

function syncLinkInput() {
  const url = buildUrl(state.specs);
  document.getElementById("watch-link").value =
    new URL(url, window.location.href).toString();
}

async function refresh() {
  clearError();
  syncLinkInput();
  const grid = document.getElementById("watch-grid");
  const empty = document.getElementById("empty-state");
  if (state.specs.length === 0) {
    clearNode(grid);
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  try {
    const data = await fetchJson("/api/watch?" + currentQuery().toString());
    clearNode(grid);
    const total = data.cards.length;
    data.cards.forEach((card, index) => {
      grid.appendChild(
        card.ok
          ? buildOkCard(card, index, total)
          : buildErrorCard(card, index, total));
    });
  } catch (err) {
    showError(err.message);
  }
}

/* ================= editing ================= */

function moveCard(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.specs.length) {
    return;
  }
  const [entry] = state.specs.splice(index, 1);
  state.specs.splice(target, 0, entry);
  refresh();
}

function removeCard(index) {
  state.specs.splice(index, 1);
  refresh();
}

function addCard() {
  const kind = document.getElementById("add-kind").value;
  const name = document.getElementById("add-name").value;
  if (!name) {
    return;
  }
  state.specs.push({ kind: kind, name: name });
  refresh();
}

/* ================= the add-card picker ================= */

/**
 * Populate the name dropdown from the declared products and known
 * environments. Decoration on top of the page's real job (showing the
 * cards already in the URL) — a failed fetch here must not block that,
 * so it fails quietly, same rule as nav.js.
 */
async function populatePicker() {
  const kindSelect = document.getElementById("add-kind");
  const nameSelect = document.getElementById("add-name");
  let names = { p: [], e: [] };
  try {
    const [summary, environments] = await Promise.all([
      fetchJson("/api/summary?parts=headline"),
      fetchJson("/api/environments"),
    ]);
    names = {
      p: summary.products.map((entry) => entry.product),
      e: environments.environments.map((entry) => entry.environment),
    };
  } catch (err) {
    return;
  }
  const fill = () => {
    clearNode(nameSelect);
    for (const name of names[kindSelect.value] || []) {
      const opt = el("option", "", name);
      opt.value = name;
      nameSelect.appendChild(opt);
    }
  };
  kindSelect.addEventListener("change", fill);
  fill();
}

/* ================= init ================= */

function init() {
  const search = new URL(window.location.href).search;
  const saved = readDefault();
  state.specs = search
    ? parseSpecs(search)
    : (saved ? parseSpecs("?" + saved) : []);

  document.getElementById("add-card-btn")
    .addEventListener("click", addCard);

  document.getElementById("save-default-btn").addEventListener("click", () => {
    writeDefault(currentQuery().toString());
    document.getElementById("save-status").textContent =
      "Saved as this browser's default.";
  });

  populatePicker();
  refresh();
}

init();
