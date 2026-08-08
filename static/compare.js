/* compare.js — a stream vs. mainline, everywhere it is shown (WP-21,
 * docs/STREAMS_PLAN.md §3.5/§3.6).
 *
 * Two callers, one source of truth for the shape of a comparison:
 *
 *   - app.js: the dashboard's delta view. Scoping to a branch (`?stream=`)
 *     SWAPS the whole dashboard body for this — the status tiles, charts,
 *     triage queues and browse table are all mainline-only concepts and
 *     hide outright, per docs/STREAMS_PLAN.md §3.6. On a page with no
 *     `stream=` param this module is never called at all, which is what
 *     keeps mainline pages at ZERO visible change (the same rule
 *     products.js follows for a single-product deployment).
 *   - test.js: the test-detail compare strip, one triple's mainline result
 *     next to its branch result.
 *
 * "Mainline" is always drawn with ghostChip (outlined) and "this branch"
 * with resultChip (solid) — the same visual language api.js's
 * resultTransition uses for "was -> now", reused here for "elsewhere ->
 * here". A side with no result is NEVER a chip of any kind (text only,
 * "no result") — a colour standing in for absence is exactly the defect
 * ResultEmphasisTest exists to catch, extended to a comparison instead of
 * a transition.
 *
 * The selected stream lives ONLY in `?stream=<id>` — never localStorage.
 * Unlike the product switcher, a branch is something you are looking AT
 * right now, not a standing preference, and it follows the Watchlist's
 * "the URL is the whole configuration" rule (docs/STREAMS_PLAN.md §0.9):
 * a link to a branch-scoped dashboard has to reopen scoped to that branch
 * for anyone, with no browser state involved.
 */

"use strict";

import {
  clearNode,
  el,
  fetchJson,
  ghostChip,
  resultChip,
  showError,
} from "./api.js";

/** The five paginable comparison categories, in tab/tile display order. */
export const CATEGORY_ORDER = [
  "new_failures", "new_passes", "both_failing", "new_tests", "no_result",
];

/** Human labels, shared by the delta view's tiles/tabs and the
 * Watchlist's stream verdict cards — the same word never means two
 * different things on two surfaces. */
export const CATEGORY_LABELS = {
  new_failures: "New failures",
  new_passes: "New passes",
  both_failing: "Both failing",
  new_tests: "New tests",
  no_result: "No result",
};

const PAGE_LIMIT = 100;

/**
 * The stream this page is scoped to, from `?stream=<id>` — null for
 * mainline. The one place this is read from; every caller goes through
 * this function rather than parsing the query string itself.
 */
export function getSelectedStreamId() {
  const raw = new URLSearchParams(window.location.search).get("stream");
  if (!raw) {
    return null;
  }
  const id = parseInt(raw, 10);
  return Number.isNaN(id) ? null : id;
}

/** "...T12:34:56.123456" (naive UTC, no zone) -> a real Date, the same
 * "append Z" idiom app.js's nightsBetween() uses for the same reason:
 * without it, an unqualified ISO date-TIME string is parsed as local
 * time, not UTC. */
function parseUtc(iso) {
  return new Date(iso + "Z");
}

/**
 * "3 days ago" / "1 hour ago" / "just now", from a real timestamp and
 * the caller's own `nowMs` — always derived from the two actual values
 * involved, never from a fixed window constant. This is the same
 * discipline WindowWordingTest pins for every other page's recency
 * wording (docs/STREAMS_PLAN.md §3.5: "let the UI phrase it from data").
 */
export function ageText(iso, nowMs) {
  if (!iso) {
    return "never";
  }
  const ms = nowMs - parseUtc(iso).getTime();
  if (ms < 90 * 1000) {
    return "just now";
  }
  const minutes = Math.floor(ms / 60000);
  if (minutes < 60) {
    return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return hours + (hours === 1 ? " hour ago" : " hours ago");
  }
  const days = Math.floor(hours / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

/** GET /api/compare — counts alone (no category) or one paginated page. */
export async function fetchCompare(streamId, category, offset) {
  const qs = new URLSearchParams();
  qs.append("stream", String(streamId));
  if (category) {
    qs.append("category", category);
    qs.append("limit", String(PAGE_LIMIT));
    qs.append("offset", String(offset || 0));
  }
  return fetchJson("/api/compare?" + qs.toString());
}

/** Every test compared, including the ones that agree — counts.agree is
 * a real field precisely so this total does not have to be re-derived
 * from a category fetch nobody asked for. */
export function totalCompared(counts) {
  let total = counts.agree;
  for (const key of CATEGORY_ORDER) {
    total += counts[key];
  }
  return total;
}

/* ================= result cells ================= */

function noResultCell() {
  return el("span", "chip-none muted", "no result");
}

/** Mainline's side of a comparison row/strip — outlined, never solid. */
export function mainlineCell(result) {
  return result ? ghostChip(result) : noResultCell();
}

/** This stream's side of a comparison row/strip — solid, the current
 * fact, drawn last so it is the loudest thing and the thing the eye
 * finishes on (the same ordering resultTransition uses). */
export function streamCell(result) {
  return result ? resultChip(result) : noResultCell();
}

/* ================= tiles ================= */

function buildTile(label, value) {
  const tile = el("div", "tile");
  tile.appendChild(el("span", "tile-value", String(value)));
  tile.appendChild(el("span", "tile-label", label));
  return tile;
}

/** The five headline tiles, in CATEGORY_ORDER. */
export function renderTiles(container, counts) {
  clearNode(container);
  for (const key of CATEGORY_ORDER) {
    container.appendChild(buildTile(CATEGORY_LABELS[key], counts[key]));
  }
}

/* ================= the paginated category table ================= */

function buildDeltaRow(row) {
  const tr = document.createElement("tr");
  const testCell = el("td", "wrap");
  const link = document.createElement("a");
  const params = new URLSearchParams();
  params.append("environment", row.environment);
  params.append("script", row.script);
  params.append("test_name", row.test_name);
  // Carry the page's stream scope into the link: the whole point of
  // clicking a delta row is reading THIS branch's history/output of the
  // test, and test.html only shows that when ?stream= arrives with it.
  // Without this line the stream-scoped test page is unreachable by
  // clicking — found by the first human to use the branch dashboard.
  const streamId = getSelectedStreamId();
  if (streamId !== null) {
    params.append("stream", String(streamId));
  }
  link.href = "test.html?" + params.toString();
  link.textContent = row.test_name;
  testCell.appendChild(link);
  testCell.appendChild(el("span", "row-sub",
    row.environment + " · " + row.script));
  tr.appendChild(testCell);

  const mainlineTd = document.createElement("td");
  mainlineTd.appendChild(mainlineCell(row.baseline_result));
  tr.appendChild(mainlineTd);

  const streamTd = document.createElement("td");
  streamTd.appendChild(streamCell(row.stream_result));
  tr.appendChild(streamTd);

  return tr;
}

/* ================= orchestration (the dashboard delta view) ================= */

const deltaState = {
  streamId: null,
  category: CATEGORY_ORDER[0],
  offset: 0,
  total: 0,
};

async function loadCategory(reset) {
  const body = document.getElementById("delta-body");
  const empty = document.getElementById("delta-empty");
  const moreBtn = document.getElementById("delta-show-more");
  if (reset) {
    deltaState.offset = 0;
    clearNode(body);
  }
  const page = await fetchCompare(
    deltaState.streamId, deltaState.category, deltaState.offset);
  deltaState.total = page.total;
  for (const row of page.tests) {
    body.appendChild(buildDeltaRow(row));
  }
  deltaState.offset += page.tests.length;
  empty.hidden = body.children.length !== 0;
  if (empty.hidden === false) {
    empty.textContent = "No tests in "
      + CATEGORY_LABELS[deltaState.category].toLowerCase() + ".";
  }
  moreBtn.hidden = deltaState.offset >= deltaState.total;
}

function renderTabs() {
  const tabs = document.getElementById("delta-tabs");
  clearNode(tabs);
  for (const key of CATEGORY_ORDER) {
    const btn = el("button", "tab", CATEGORY_LABELS[key]);
    btn.type = "button";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected",
      key === deltaState.category ? "true" : "false");
    btn.addEventListener("click", () => {
      if (deltaState.category === key) {
        return;
      }
      deltaState.category = key;
      renderTabs();
      loadCategory(true).catch((err) => showError(err.message));
    });
    tabs.appendChild(btn);
  }
}

function renderBaselineCard(streamMeta, baselineMeta, counts, nowMs) {
  document.getElementById("delta-agree").textContent =
    counts.agree + " test" + (counts.agree === 1 ? "" : "s")
    + " agree and " + (counts.agree === 1 ? "is" : "are") + " not listed.";

  const total = totalCompared(counts);
  const covered = total - counts.no_result;
  document.getElementById("delta-coverage").textContent =
    covered + " of " + total + " tests have a result on this branch.";

  document.getElementById("delta-baseline").textContent =
    "This branch last ran " + ageText(streamMeta.last_seen, nowMs)
    + " — mainline last ran " + ageText(baselineMeta.last_seen, nowMs)
    + ".";

  // A stale baseline is a fact about the two timestamps just shown, not
  // a hidden threshold — 14 days is a display judgement about when the
  // WARNING earns its place on screen, not a value anything is filtered
  // or counted by (the CLAUDE.md rule that applies to is server-side
  // cutoffs like stale_before).
  const warning = document.getElementById("delta-stale-warning");
  const baselineAgeMs = nowMs - parseUtc(baselineMeta.last_seen).getTime();
  if (baselineAgeMs > 14 * 24 * 60 * 60 * 1000) {
    warning.textContent =
      "Mainline itself last ran " + ageText(baselineMeta.last_seen, nowMs)
      + " ago — this comparison may be stale.";
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }
}

function renderBranchBand(streamMeta) {
  document.getElementById("branch-band-text").textContent =
    "Viewing " + streamMeta.kind + " " + streamMeta.name
    + " — compared against mainline.";
  document.getElementById("branch-band").hidden = false;
}

/** Sections that only mean something on the mainline dashboard. Hidden
 * outright while scoped to a branch — docs/STREAMS_PLAN.md §3.6 calls
 * this a swap, not an addition. */
const MAINLINE_SECTIONS = [
  "status-section", "charts-section", "triage-section", "browse-section",
];

/**
 * Swap the dashboard body for the branch-vs-mainline delta view.
 *
 * Called from app.js's init() ONLY when getSelectedStreamId() is
 * non-null, and nothing else in app.js runs afterwards for that page
 * load — the mainline code path (summary fetch, queues, the browse
 * table) never executes, which is what keeps this feature's entire
 * footprint on a mainline page at zero.
 */
export async function initDeltaView(streamId) {
  deltaState.streamId = streamId;
  deltaState.category = CATEGORY_ORDER[0];

  const loading = document.getElementById("loading-state");
  loading.hidden = false;
  loading.textContent = "Loading comparison…";
  for (const id of MAINLINE_SECTIONS) {
    document.getElementById(id).hidden = true;
  }
  const envField = document.getElementById("env-filter-field");
  if (envField) {
    envField.hidden = true;
  }

  try {
    const data = await fetchCompare(streamId, null, 0);
    renderBranchBand(data.stream);
    renderTiles(document.getElementById("delta-tiles"), data.counts);
    renderBaselineCard(data.stream, data.baseline, data.counts, Date.now());
    renderTabs();
    document.getElementById("delta-section").hidden = false;
    loading.hidden = true;
    await loadCategory(true);
  } catch (err) {
    loading.hidden = true;
    showError(err.message);
    return;
  }

  // Idempotent assignment (not addEventListener): initDeltaView can run
  // again from this same click, and a growing pile of listeners would
  // fire the refresh N times on the Nth click.
  document.getElementById("delta-show-more").onclick =
    () => loadCategory(false).catch((err) => showError(err.message));
  document.getElementById("reload-btn").onclick =
    () => initDeltaView(streamId);
}

/* ================= the test-detail compare strip ================= */

/**
 * "mainline: PASS   this branch: FAIL" beside a test's own detail —
 * built from TWO detail fetches test.js already makes (one unscoped,
 * one `stream=`), not a new endpoint: a single test's result on two
 * streams is not what /api/compare (an ESTATE-wide comparison) answers.
 */
export function renderCompareStrip(container, streamMeta, baselineResult, streamResult) {
  clearNode(container);
  const wrap = el("span", "chip-transition");
  wrap.appendChild(el("span", "row-sub", "mainline"));
  wrap.appendChild(mainlineCell(baselineResult));
  wrap.appendChild(el("span", "row-sub",
    streamMeta.kind + ":" + streamMeta.name));
  wrap.appendChild(streamCell(streamResult));
  container.appendChild(wrap);
  container.hidden = false;
}
