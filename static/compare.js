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
  assigneeSelect,
  clearNode,
  el,
  fetchJson,
  ghostChip,
  resultChip,
  showError,
} from "./api.js";
import { reopenIfOpen, toggleReview } from "./review.js";

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

/**
 * The baseline this page was explicitly opened with, from `?baseline=<id>`
 * — null means "no explicit choice", which is NOT necessarily mainline:
 * a build-scoped page with no explicit baseline still defaults to its
 * predecessor build client-side (see :func:`pickDefaultBuildBaseline`),
 * the same "the URL is the whole configuration" rule getSelectedStreamId()
 * follows (WP-22, docs/STREAMS_PLAN.md §4.1).
 */
export function getSelectedBaselineId() {
  const raw = new URLSearchParams(window.location.search).get("baseline");
  if (!raw) {
    return null;
  }
  const id = parseInt(raw, 10);
  return Number.isNaN(id) ? null : id;
}

/**
 * "mainline" or "build 2026.9.1" / "branch feat/x" — the one place this
 * wording is built (WP-22, docs/STREAMS_PLAN.md §4.1). Before this drop
 * every comparison's OTHER side was always mainline, so the word
 * "mainline" was hardcoded in several places; now the baseline can be
 * any stream, and every caller must read its label from the identity the
 * server actually returned rather than assume.
 */
export function streamLabel(meta) {
  return meta.kind === "mainline" ? "mainline" : meta.kind + " " + meta.name;
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

/**
 * GET /api/compare — counts alone (no category) or one paginated page.
 *
 * *baselineId*, added WP-22 (docs/STREAMS_PLAN.md §4.1), is omitted
 * (server default: mainline) when null/undefined — every call site from
 * before this drop keeps working unchanged.
 */
export async function fetchCompare(streamId, category, offset, baselineId) {
  const qs = new URLSearchParams();
  qs.append("stream", String(streamId));
  if (baselineId !== null && baselineId !== undefined) {
    qs.append("baseline", String(baselineId));
  }
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

/** Review-panel options for a delta row (WP-21, docs/STREAMS_PLAN.md
 * §0.4/§3.6's "triage still works from a branch").
 *
 * `staleBefore` is deliberately OMITTED (not null — genuinely absent):
 * review.js's own retire gate is `isStale(entry, opts.staleBefore) &&
 * !entry.retired_at`, and isStale() returns false outright when
 * staleBefore is falsy, before it even looks at entry.retired_at.
 * Retirement is a MAINLINE decision (§3.4) — a branch's own staleness
 * says nothing about whether the test is still in the suite — so this
 * is how the shared panel is told "never offer it here" without
 * review.js having to know whose page it is on (it explicitly cannot;
 * see its module docstring).
 */
function deltaReviewOptions() {
  return {};
}

/**
 * One delta-table row's entry for the shared review panel/assignee
 * select: enough of the TestSummaryRow/TestStatusRow shape those expect
 * to work unmodified, sourced from a CompareRow instead of a dashboard
 * row. `stream_id` (WP-21) is this page's own scope, so an assignment
 * made from here is annotated with where it came from.
 */
function reviewEntry(row, streamId) {
  return {
    environment: row.environment,
    script: row.script,
    test_name: row.test_name,
    run_id: row.stream_run_id,
    start_time: row.stream_start_time,
    assignee: row.assignee,
    stream_id: streamId,
  };
}

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

  // Triage from a branch (docs/STREAMS_PLAN.md §0.4/§3.6): the SAME
  // assignee select the dashboard's own queue rows use, so a failure
  // found on a branch can be taken/assigned exactly like a mainline one
  // — assignment is never partitioned by stream, only annotated with
  // where it was made (see reviewEntry()).
  const entry = reviewEntry(row, streamId);
  const assigneeTd = el("td", "assignee-cell");
  assigneeTd.appendChild(assigneeSelect(entry, () => {}));
  tr.appendChild(assigneeTd);

  // The Review expander needs a run to show — a no_result row (present
  // on mainline, absent on this branch) has none, and shows no button
  // at all rather than one that opens onto nothing.
  const outputTd = el("td", "review-cell");
  if (row.stream_run_id !== null) {
    const reviewBtn = el("button", "review-btn", "Review");
    reviewBtn.type = "button";
    reviewBtn.setAttribute("aria-expanded", "false");
    reviewBtn.title = "Show this run's output, and assign it";
    reviewBtn.addEventListener("click", () => toggleReview(
      entry, tr, reviewBtn, deltaReviewOptions()));
    outputTd.appendChild(reviewBtn);
    // Keep the panel open across the re-render a category switch or
    // "Show more" triggers — the same rule app.js's queue table follows.
    reopenIfOpen(entry, tr, reviewBtn, deltaReviewOptions());
  }
  tr.appendChild(outputTd);

  return tr;
}

/* ================= orchestration (the dashboard delta view) ================= */

const deltaState = {
  streamId: null,
  // null = mainline (the server's own default) OR "no explicit choice
  // yet" during the initial build-predecessor lookup — see
  // initDeltaView(). Once set, always the ACTUAL baseline id in use.
  baselineId: null,
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
    deltaState.streamId, deltaState.category, deltaState.offset,
    deltaState.baselineId);
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

/**
 * All wording here is built from *streamMeta*/*baselineMeta*'s own
 * kind/name — never the literal word "mainline" or "branch" (WP-22,
 * docs/STREAMS_PLAN.md §4.1: a build's baseline is routinely another
 * build, not mainline, since it defaults to its predecessor). The
 * heading and the two column headers are set here too so the whole
 * section reads consistently, not just the prose lines.
 */
function renderBaselineCard(streamMeta, baselineMeta, counts, nowMs) {
  const baseline = streamLabel(baselineMeta);
  const streamNoun = streamMeta.kind === "mainline" ? "stream" : streamMeta.kind;

  document.getElementById("delta-heading").textContent =
    "Compare to " + baseline;
  document.getElementById("delta-col-baseline").textContent =
    baselineMeta.kind === "mainline" ? "Mainline" : baseline;
  document.getElementById("delta-col-stream").textContent =
    "This " + streamNoun;

  document.getElementById("delta-agree").textContent =
    counts.agree + " test" + (counts.agree === 1 ? "" : "s")
    + " agree and " + (counts.agree === 1 ? "is" : "are") + " not listed.";

  const total = totalCompared(counts);
  const covered = total - counts.no_result;
  document.getElementById("delta-coverage").textContent =
    covered + " of " + total + " tests have a result on this " + streamNoun
    + ".";

  document.getElementById("delta-baseline").textContent =
    "This " + streamNoun + " last ran " + ageText(streamMeta.last_seen, nowMs)
    + " — " + baseline + " last ran " + ageText(baselineMeta.last_seen, nowMs)
    + ".";

  // A stale baseline is a fact about the two timestamps just shown, not
  // a hidden threshold — 14 days is a display judgement about when the
  // WARNING earns its place on screen, not a value anything is filtered
  // or counted by (the CLAUDE.md rule that applies to is server-side
  // cutoffs like stale_before).
  const warning = document.getElementById("delta-stale-warning");
  const baselineAgeMs = nowMs - parseUtc(baselineMeta.last_seen).getTime();
  if (baselineAgeMs > 14 * 24 * 60 * 60 * 1000) {
    const label = baseline.charAt(0).toUpperCase() + baseline.slice(1);
    warning.textContent =
      label + " itself last ran " + ageText(baselineMeta.last_seen, nowMs)
      + " ago — this comparison may be stale.";
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }
}

/**
 * The build-only framing line (WP-22, docs/STREAMS_PLAN.md §4.1): "built
 * <when> · nothing has run since" when a build was imported once and
 * never rebuilt (first_seen === last_seen), or "built <when> · last ran
 * <when>" once it has been re-imported (a rebuild). Worded from
 * first_seen/last_seen themselves, never a constant — the same
 * WindowWordingTest discipline every other recency line in this project
 * follows. Hidden for anything that is not kind 'build' (branches and
 * mainline have no "built once" framing that means anything).
 */
function renderBuildFraming(streamMeta, nowMs) {
  const line = document.getElementById("delta-build-framing");
  if (streamMeta.kind !== "build") {
    line.hidden = true;
    return;
  }
  const built = "Built " + ageText(streamMeta.first_seen, nowMs);
  line.textContent = streamMeta.first_seen === streamMeta.last_seen
    ? built + " — nothing has run since."
    : built + " — last ran " + ageText(streamMeta.last_seen, nowMs) + ".";
  line.hidden = false;
}

/**
 * The sticky "you are scoped to a branch" band — shared by the
 * dashboard's delta view and the test-detail page (WP-21
 * docs/STREAMS_PLAN.md §3.6: found in first human use, a reader deep in
 * a test's history/analytics/compare strip had no indication they were
 * scoped at all). Every page that mounts `#branch-band` renders it the
 * same way, so "Back to mainline" means the same promise everywhere:
 * this URL with ONLY `stream` removed — never a fixed page like
 * `index.html`, which would silently drop test.html's
 * environment/script/test_name and land on the wrong page entirely.
 *
 * Guards its own mount the way products.js's host-managed call sites do
 * (ProductSwitcherHostManagedTest) — a page with no `#branch-band`
 * simply does not render one, rather than throwing mid-render and
 * taking the rest of that page's first paint down with it.
 *
 * *baselineMeta* (WP-22, docs/STREAMS_PLAN.md §4.1) is optional: the
 * test-detail compare strip always compares against mainline and has no
 * baseline object to pass, so omitting it keeps that call site's wording
 * exactly what it was before this drop. The dashboard's delta view
 * always passes the ACTUAL baseline — which, for a build, is routinely
 * a predecessor build rather than mainline.
 */
export function renderBranchBand(streamMeta, baselineMeta) {
  const container = document.getElementById("branch-band");
  if (!container) {
    return;
  }
  const textEl = document.getElementById("branch-band-text");
  const backLink = document.getElementById("branch-band-back");
  const baseline = baselineMeta ? streamLabel(baselineMeta) : "mainline";
  textEl.textContent = "Viewing " + streamMeta.kind + " " + streamMeta.name
    + " — compared against " + baseline + ".";
  const url = new URL(window.location.href);
  url.searchParams.delete("stream");
  backLink.href = url.pathname + url.search;
  container.hidden = false;
}

/** Sections that only mean something on the mainline dashboard. Hidden
 * outright while scoped to a branch — docs/STREAMS_PLAN.md §3.6 calls
 * this a swap, not an addition. */
const MAINLINE_SECTIONS = [
  "status-section", "charts-section", "triage-section", "browse-section",
];

/* ================= build-scoped "Compare to" control (WP-22) ================= */

/** GET /api/streams?product= — every same-product stream (branches AND
 * builds), the raw data both the default-baseline pick and the "Compare
 * to" datalist need. A failed fetch degrades to "mainline only" rather
 * than breaking the delta view that already loaded. */
async function fetchProductStreams(product) {
  try {
    const data = await fetchJson(
      "/api/streams?product=" + encodeURIComponent(product));
    return data.streams || [];
  } catch (err) {
    return [];
  }
}

/**
 * The WP-22 default: the nearest earlier same-product BUILD by
 * `last_seen` (id as tiebreak), or null (meaning mainline) when none
 * exists (docs/STREAMS_PLAN.md §4.1: "the previous build by last_seen
 * where one exists, else mainline"). Mirrors
 * Storage.previous_builds' ordering rule exactly — the backend needs its
 * own copy for the O(1) Watchlist card path, this is the frontend's for
 * a page that already has the full stream list in hand and gains
 * nothing from a second round trip to ask the server the same question.
 */
export function pickDefaultBuildBaseline(streamMeta, streams) {
  let best = null;
  for (const candidate of streams) {
    if (candidate.kind !== "build" || candidate.id === streamMeta.id) {
      continue;
    }
    if (candidate.last_seen >= streamMeta.last_seen) {
      continue;   // ISO strings: lexical compare is chronological.
    }
    if (best === null || candidate.last_seen > best.last_seen
        || (candidate.last_seen === best.last_seen
            && candidate.id > best.id)) {
      best = candidate;
    }
  }
  return best;
}

/**
 * The "Compare to" datalist combo (WP-22, docs/STREAMS_PLAN.md §4.1):
 * shown only when the scoped stream is kind 'build' — a branch has no
 * predecessor concept, so this stays hidden (and *streams* is never
 * fetched) for every branch-scoped page, keeping that case's footprint
 * at zero. A plain `<input list=…>`, not a `<select>`: the text typed
 * or picked IS the label ("build:1.0"), matched back to an id through a
 * small map built fresh on every render; an unrecognised value (a typo)
 * is a no-op, not a broken navigation.
 */
function renderCompareToControl(streamMeta, baselineMeta, streams) {
  const field = document.getElementById("compare-to-field");
  if (!field) {
    return;
  }
  if (streamMeta.kind !== "build") {
    field.hidden = true;
    return;
  }
  const input = document.getElementById("compare-to-input");
  const datalist = document.getElementById("compare-to-options");
  clearNode(datalist);
  const labelToId = {};   // display text -> id (null for mainline)
  const mainlineLabel = "Mainline nightlies";
  labelToId[mainlineLabel] = null;
  const mainlineOpt = document.createElement("option");
  mainlineOpt.value = mainlineLabel;
  datalist.appendChild(mainlineOpt);

  const others = streams
    .filter((s) => s.id !== streamMeta.id)
    .sort((a, b) => (a.last_seen < b.last_seen ? 1 : -1));
  for (const other of others) {
    const label = other.kind + ":" + other.name;
    labelToId[label] = other.id;
    const opt = document.createElement("option");
    opt.value = label;
    datalist.appendChild(opt);
  }

  input.value = baselineMeta.kind === "mainline"
    ? mainlineLabel : baselineMeta.kind + ":" + baselineMeta.name;
  // Idempotent assignment, same reasoning as the show-more/reload
  // buttons below: this control can be re-rendered by a reload.
  input.onchange = () => {
    const chosen = labelToId[input.value];
    if (chosen === undefined) {
      return;   // not a recognised option -- leave the view as it is
    }
    const url = new URL(window.location.href);
    if (chosen === null) {
      url.searchParams.delete("baseline");
    } else {
      url.searchParams.set("baseline", String(chosen));
    }
    window.location.href = url.toString();
  };
  field.hidden = false;
}

/**
 * Swap the dashboard body for the branch-vs-mainline delta view.
 *
 * Called from app.js's init() ONLY when getSelectedStreamId() is
 * non-null, and nothing else in app.js runs afterwards for that page
 * load — the mainline code path (summary fetch, queues, the browse
 * table) never executes, which is what keeps this feature's entire
 * footprint on a mainline page at zero.
 *
 * Baseline resolution (WP-22, docs/STREAMS_PLAN.md §4.1): an explicit
 * `?baseline=` wins outright. Otherwise, for a BUILD-kind stream only,
 * the product's stream list is fetched once and the previous build (if
 * any) becomes the baseline for this load — a second /api/compare
 * fetch, paid only by build-scoped pages, never by branch-scoped ones
 * (which keep the original single-fetch cost this function has had
 * since WP-21).
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

  let productStreams = [];
  try {
    const explicitBaselineId = getSelectedBaselineId();
    let data = await fetchCompare(streamId, null, 0, explicitBaselineId);
    if (explicitBaselineId === null && data.stream.kind === "build") {
      productStreams = await fetchProductStreams(data.stream.product);
      const predecessor = pickDefaultBuildBaseline(
        data.stream, productStreams);
      if (predecessor !== null) {
        data = await fetchCompare(streamId, null, 0, predecessor.id);
      }
    } else if (data.stream.kind === "build") {
      productStreams = await fetchProductStreams(data.stream.product);
    }
    deltaState.baselineId =
      data.baseline.kind === "mainline" ? null : data.baseline.id;
    renderBranchBand(data.stream, data.baseline);
    renderBuildFraming(data.stream, Date.now());
    renderCompareToControl(data.stream, data.baseline, productStreams);
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
