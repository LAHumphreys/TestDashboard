/* app.js — the testboard home screen.
 *
 * A triage-first dashboard in four sections, all scoped by the single
 * environment filter in the toolbar:
 *
 *   1. "Latest results" — KPI tiles from /api/summary, counting each
 *      test's newest run since the derived recency cutoff.
 *   2. Charts        — nightly failing-run trend, failing-by-environment,
 *                      most-failing scripts (see charts.js).
 *   3. Triage        — tabbed work queues (new failures, still failing,
 *                      fixed, stale annotations, my actions) with a
 *                      one-click "Take" assign action.
 *   4. All tests     — the estate, ONE PAGE AT A TIME.
 *
 * The estate can hold tens of thousands of tests, so this page never
 * downloads it. Filtering, searching, sorting and paging of the test list
 * are all query parameters on /api/dashboard, and only the rows on screen
 * are ever in memory.
 *
 * Data, per refresh, all in parallel: /api/summary?parts=headline (the
 * tiles, charts, filters and every queue's COUNT — the fast part), one
 * /api/summary?parts=queue for the active triage tab's rows, and one
 * page of /api/dashboard. Each section paints as its data lands, so the
 * page is readable as soon as the headline answers; the other queue
 * tabs fetch their rows when first opened. While refreshing, previous
 * renders are held at reduced opacity (no skeletons, no layout jumps).
 * All data reaches the DOM via textContent only.
 */

"use strict";

import {
  RESULTS,
  clearError,
  clearNode,
  el,
  assigneeSelect,
  fetchJson,
  fillOutput,
  formatDuration,
  formatTime,
  getUsername,
  postJson,
  putJson,
  renderUserWidget,
  requireUsername,
  resultChip,
  resultTransition,
  resultClass,
  showError,
  testApiPath,
} from "./api.js";
import {
  barRows,
  formatNight,
  stackedColumnChart,
} from "./charts.js";
import {
  reopenIfOpen,
  toggleReview,
} from "./review.js";
import { attachSorting, sortRows } from "./sorting.js";
import { getSelectedProduct, renderSwitcher } from "./products.js";
import {
  fetchCompare,
  getSelectedBaselineId,
  getSelectedStreamId,
  initDeltaView,
  renderBranchBand,
  streamLabel,
} from "./compare.js";

/** Rows fetched per page of the All-tests table ("Show more" adds one). */
const CHUNK = 250;

/** Covered passes (docs/STREAMS_PLAN.md §5.2) a branch needs in the
 * 14-day lookback before its dashboard defaults to "Its own results"
 * rather than "Difference from mainline". Not hidden: the caption this
 * feeds (see selectBranchTab()) states the number and this threshold in
 * plain words, built from data, never silently assumed. */
const OWN_RESULTS_DEFAULT_PASSES = 2;

const state = {
  environment: "",        // "" = all environments
  // WP-23: non-null while viewing a branch stream's "Its own results"
  // tab (docs/STREAMS_PLAN.md §5.2) -- summaryUrl/queueUrl/browseUrl
  // append it as stream=, and a page that never opens a branch's own
  // tab (every mainline visit, and a build/delta-only branch visit)
  // leaves this null forever, which is what keeps those paths at zero
  // visible change.
  streamId: null,
  // F6 (docs/STREAMS_PLAN.md §5.2 "as built"): the SCOPED STREAM's own
  // product, stashed by initBranchDashboard() from data.stream.product
  // so renderBranchQuickLinks() can make its Time/Timeline links scope-
  // self-sufficient (the jump-fix principle, commit 4725bbc) rather
  // than relying on whatever this browser's switcher last stored.
  streamProduct: "",
  summary: null,          // last headline payload (no queue rows)
  // Queue rows by kind, fetched per tab on demand. Absent = not landed
  // yet for the current filters; renderQueueTable shows a loading line.
  queues: {},
  activeQueue: "new_failures",
  // The test list: only the rows currently on screen, plus the server's
  // exact total for the active filters.
  browseRows: [],
  browseTotal: 0,
  script: "",             // "" = all scripts
  sortKey: "environment",
  sortAsc: true,
  activeResults: new Set(),
  staleOnly: false,
  // F4 (docs/STREAMS_PLAN.md §5.2 "as built"): the browse filter row's
  // "Unassigned only" chip -- wired to /api/dashboard's existing
  // include_unassigned param (unassigned=1 on the wire), the same
  // filter Open Actions has always been able to apply server-side.
  unassignedOnly: false,
  qText: "",
  qTimer: null,
  requestSeq: 0,
  browseSeq: 0,
  showRetired: false,
  // Triage queue sort (client-side; see renderQueueTable for why that is
  // only valid while the queue is under its cap).
  queueSortKey: null,
  queueSortDesc: false,
};

const envSelect = document.getElementById("filter-environment");
const scriptSelect = document.getElementById("filter-script");
const qInput = document.getElementById("filter-q");
const tbody = document.getElementById("dashboard-body");

const SECTIONS = ["status-section", "charts-section", "triage-section",
  "browse-section"];

/* ================= data loading ================= */

/**
 * Add `product=<selected>` (WP-20) IF this browser has one selected.
 * The server resolves it to an environment allow-list, exactly as if
 * the environment filter had been set to every environment in it — see
 * docs/STREAMS_PLAN.md §2.2. Single-product deployments never set one,
 * so this is a no-op for them.
 */
function appendProduct(qs) {
  const product = getSelectedProduct();
  if (product) {
    qs.append("product", product);
  }
}

/**
 * Add `stream=<id>` (WP-23) IF this page is showing a branch's OWN
 * results tab (state.streamId set by initBranchDashboard()). A page
 * that never opens that tab never sets state.streamId, so this is a
 * no-op there -- the same "absent = zero visible change" shape
 * appendProduct() follows for a single-product deployment.
 */
function appendStream(qs) {
  if (state.streamId !== null) {
    qs.append("stream", String(state.streamId));
  }
}

/**
 * Stamp `stream_id` onto a page of dashboard/queue rows fetched while
 * showing a branch's own-results tab -- assigneeSelect()/toggleReview()
 * already read `entry.stream_id` generically (WP-21, api.js/review.js)
 * to record WHERE an assignment/comment was made from, an annotation
 * never a partition (docs/STREAMS_PLAN.md §0.4). A mainline page never
 * calls this with a stream set, so those rows are untouched -- zero
 * visible change there.
 */
function tagStream(rows) {
  if (state.streamId !== null) {
    for (const row of rows) {
      row.stream_id = state.streamId;
    }
  }
  return rows;
}

function summaryUrl() {
  const qs = new URLSearchParams();
  qs.append("parts", "headline");
  if (state.environment) {
    qs.append("environment", state.environment);
  }
  appendProduct(qs);
  appendStream(qs);
  // The "my actions" queue is filtered server-side: picking a user's
  // tests out of an already-capped queue would hide their own work.
  // The headline needs the assignee too — the "mine" tab count.
  const me = getUsername();
  if (me) {
    qs.append("assignee", me);
  }
  return "/api/summary?" + qs.toString();
}

/** URL for one triage queue's rows. */
function queueUrl(kind) {
  const qs = new URLSearchParams();
  qs.append("parts", "queue");
  qs.append("queue", kind);
  if (state.environment) {
    qs.append("environment", state.environment);
  }
  appendProduct(qs);
  appendStream(qs);
  const me = getUsername();
  if (me) {
    qs.append("assignee", me);
  }
  return "/api/summary?" + qs.toString();
}

/** URL for one page of the test list under the current filters. */
function browseUrl(offset) {
  const qs = new URLSearchParams();
  if (state.environment) {
    qs.append("environment", state.environment);
  }
  appendProduct(qs);
  appendStream(qs);
  if (state.script) {
    qs.append("script", state.script);
  }
  for (const result of state.activeResults) {
    qs.append("result", result);
  }
  if (state.staleOnly) {
    qs.append("stale", "1");
  }
  if (state.showRetired) {
    qs.append("retired", "1");
  }
  if (state.unassignedOnly) {
    qs.append("unassigned", "1");
  }
  const query = state.qText.trim();
  if (query) {
    qs.append("q", query);
  }
  qs.append("sort", state.sortKey);
  qs.append("order", state.sortAsc ? "asc" : "desc");
  qs.append("limit", String(CHUNK));
  qs.append("offset", String(offset));
  return "/api/dashboard?" + qs.toString();
}

/**
 * Reload everything, painting each section as its data lands.
 *
 * Three requests go out together — the headline, the active queue's
 * rows, the first browse page — and each renders its own section on
 * arrival rather than waiting for the slowest. The tiles, charts and
 * tab counts are a fraction of the old monolithic payload, so the page
 * is readable at once even when a queue takes its time. A section that
 * fails shows the error without blanking the ones that succeeded.
 *
 * Every block checks the sequence number before touching state: a
 * filter change mid-flight abandons the whole generation.
 */
async function refreshAll() {
  const seq = ++state.requestSeq;
  state.browseSeq++;  // any in-flight page load is now stale
  clearError();
  setLoading(true);
  state.queues = {};  // the filters may have changed; refetch per tab

  const headlinePart = (async () => {
    try {
      const summary = await fetchJson(summaryUrl());
      if (seq !== state.requestSeq) {
        return;
      }
      state.summary = summary;
      renderHeadline();
      // First meaningful paint: the page is usable from here.
      document.getElementById("loading-state").hidden = true;
    } catch (err) {
      if (seq === state.requestSeq) {
        showError(err.message);
      }
    }
  })();
  const queuePart = loadQueue(state.activeQueue, seq);
  const browsePart = (async () => {
    try {
      const page = await fetchJson(browseUrl(0));
      if (seq !== state.requestSeq) {
        return;
      }
      state.browseRows = tagStream(page.tests);
      state.browseTotal = page.total;
      renderBrowse(state.browseRows, false);
    } catch (err) {
      if (seq === state.requestSeq) {
        showError(err.message);
      }
    }
  })();

  await Promise.all([headlinePart, queuePart, browsePart]);
  if (seq === state.requestSeq) {
    setLoading(false);
    document.getElementById("loading-state").hidden = true;
  }
}

/**
 * Fetch one queue's rows and render them if that tab is still active.
 *
 * `seq` ties the answer to the refresh generation that asked: a stale
 * answer is dropped, never rendered. The tab badge repaints too — the
 * queue's own total is fresher than the headline's.
 */
async function loadQueue(kind, seq) {
  try {
    const payload = await fetchJson(queueUrl(kind));
    if (seq !== state.requestSeq) {
      return;
    }
    tagStream(payload.queue.tests);
    state.queues[kind] = payload.queue;
    if (state.summary) {
      renderQueueTabs();
      if (state.activeQueue === kind) {
        renderQueueTable();
      }
    }
  } catch (err) {
    if (seq === state.requestSeq) {
      showError(err.message);
    }
  }
}

/**
 * Fetch the test list under the current filters.
 *
 * `append` continues after the rows already shown ("Show more");
 * otherwise this is a new filter/sort and paging restarts at the top.
 */
async function loadBrowse(append) {
  const seq = ++state.browseSeq;
  const offset = append ? state.browseRows.length : 0;
  try {
    const page = await fetchJson(browseUrl(offset));
    if (seq !== state.browseSeq) {
      return;  // a newer filter change already superseded this request
    }
    tagStream(page.tests);
    state.browseRows = append
      ? state.browseRows.concat(page.tests) : page.tests;
    state.browseTotal = page.total;
    renderBrowse(page.tests, append);
  } catch (err) {
    if (seq === state.browseSeq) {
      showError(err.message);
    }
  }
}

/*
 * At most one summary refresh in the air at a time.
 *
 * Every in-row action asks for fresh counts so the page stays honest,
 * and triaging a queue means assigning a dozen tests in a few seconds.
 * Before coalescing, each of those fired its own full summary — the
 * same expensive answer computed a dozen times, mostly thrown away.
 *
 * Coalescing keeps the behaviour and drops the duplicate work: a caller
 * arriving while a request is running waits for it instead of starting
 * another, and exactly one more request follows so the final counts
 * still reflect the final action. A burst of any size costs two
 * requests rather than one per action.
 *
 * A refresh is now the headline plus the ACTIVE queue's rows, fetched
 * together. The other queues' caches are dropped, not refetched: five
 * row payloads nobody is looking at, refetched on every "Take", is the
 * expense the parts split exists to avoid. Their tabs refetch on the
 * next click; their badge counts come fresh with the headline either
 * way.
 */
let summaryInFlight = null;
let summaryStale = false;

async function fetchSummary() {
  summaryStale = true;
  if (summaryInFlight !== null) {
    // Someone else is already asking. Their answer may predate our
    // action, but the loop below will issue one more once it lands.
    await summaryInFlight;
    return;
  }
  while (summaryStale) {
    summaryStale = false;
    const kind = state.activeQueue;
    summaryInFlight = Promise.all([
      fetchJson(summaryUrl()),
      fetchJson(queueUrl(kind)),
    ]);
    try {
      const results = await summaryInFlight;
      state.summary = results[0];
      state.queues = {};
      tagStream(results[1].queue.tests);
      state.queues[kind] = results[1].queue;
    } finally {
      summaryInFlight = null;
    }
  }
}

/** Reload the summary only (after a quick action like "Take"). */
async function refreshSummary() {
  try {
    await fetchSummary();
    renderStatus();
    renderEnvUpdated();
    renderCharts();
    renderQueues();
  } catch (err) {
    showError(err.message);
  }
}

function setLoading(loading) {
  for (const id of SECTIONS) {
    document.getElementById(id).classList.toggle("is-loading", loading);
  }
}

/** Render every section the headline payload feeds (all but browse). */
function renderHeadline() {
  for (const id of SECTIONS) {
    document.getElementById(id).hidden = false;
  }
  renderToolbar();
  renderStatus();
  renderEnvUpdated();
  renderCharts();
  renderQueues();
  populateScriptOptions();
  updateProductColumn();
  const switcherMount = document.getElementById("product-switcher");
  if (switcherMount) {
    renderSwitcher(switcherMount, state.summary.products || []);
  }
}

/* ================= toolbar ================= */

function renderToolbar() {
  const summary = state.summary;
  fillSelect(envSelect, summary.environments, "All environments",
    state.environment);
  const at = summary.generated_at;
  document.getElementById("refreshed-at").textContent =
    "Updated " + String(at).slice(11, 16) + " UTC";
}

/**
 * Show the Product column exactly when the page SPANS products (two or
 * more declared, and no product selected); when scoped to one, the
 * footer says so instead — a column of identical values is noise
 * (docs/STREAMS_PLAN.md §2.3, an established finding from the same
 * project). A deployment with fewer than two declared products shows
 * neither: zero visible change, the same rule the switcher follows.
 */
function updateProductColumn() {
  const products = (state.summary && state.summary.products) || [];
  const selected = getSelectedProduct();
  const spans = products.length >= 2 && !selected;
  const scoped = products.length >= 2 && Boolean(selected);
  document.getElementById("product-col-head").hidden = !spans;
  document.querySelectorAll("#dashboard-body .product-col")
    .forEach((cell) => { cell.hidden = !spans; });
  const note = document.getElementById("browse-product-note");
  note.hidden = !scoped;
  note.textContent = scoped
    ? "Scoped to " + selected + " — no product column needed." : "";
}

function fillSelect(select, values, allLabel, selected) {
  clearNode(select);
  const allOpt = el("option", "", allLabel);
  allOpt.value = "";
  select.appendChild(allOpt);
  for (const value of values) {
    const opt = el("option", "", value);
    opt.value = value;
    select.appendChild(opt);
  }
  select.value = values.indexOf(selected) !== -1 ? selected : "";
}

/**
 * Scope the whole page to `environment` ("" = all) and reload.
 *
 * The script filter belongs to the environment that was showing, so it
 * is dropped — unless the caller has just chosen a script deliberately
 * (`keepScript`), which is how clicking a script in the chart works.
 */
function setEnvironment(environment, keepScript) {
  state.environment = environment;
  if (!keepScript) {
    state.script = "";
  }
  const url = new URL(window.location.href);
  if (environment) {
    url.searchParams.set("environment", environment);
  } else {
    url.searchParams.delete("environment");
  }
  window.history.replaceState(null, "", url.toString());
  // F6: the quick links' Timeline target names the CURRENT environment
  // filter — keep them in step with it, own-results tab only.
  if (state.streamId !== null) {
    renderBranchQuickLinks(state.streamId);
  }
  refreshAll();
}

/* ================= "Latest results" tiles ================= */

function buildTile(spec) {
  const tile = el(spec.onClick ? "button" : "div",
    "tile" + (spec.hero ? " tile-hero" : "")
    + (spec.accent ? " " + spec.accent : ""));
  if (spec.onClick) {
    tile.type = "button";
    tile.addEventListener("click", spec.onClick);
  }
  tile.appendChild(el("span", "tile-label", spec.label));
  tile.appendChild(el("span", "tile-value", spec.value));
  if (spec.sub) {
    tile.appendChild(el("span", "tile-sub", spec.sub));
  }
  if (spec.delta) {
    tile.appendChild(
      el("span", "tile-delta " + spec.delta.cls, spec.delta.text));
  }
  return tile;
}

/*
 * "Last update" per environment.
 *
 * The environments run one after another and hours apart, so a single
 * estate-wide figure is only the newest of them — it looks healthy
 * while the one you are waiting on has not started. Each is named, with
 * its age in words, because the question is nearly always "has X run
 * yet" rather than "what time is it in UTC".
 */
function ageWords(iso, nowIso) {
  const minutes = Math.round(
    (Date.parse(nowIso + "Z") - Date.parse(iso + "Z")) / 60000);
  if (minutes < 0) return "just now";
  if (minutes < 90) return minutes + "m ago";
  const hours = Math.round(minutes / 60);
  if (hours < 36) return hours + "h ago";
  return Math.round(hours / 24) + "d ago";
}

function renderEnvUpdated() {
  const summary = state.summary;
  const container = document.getElementById("env-updated");
  clearNode(container);
  const updated = summary.environment_updated || {};
  // EVERY environment, including when one is selected. The pills are the
  // fastest way to switch between environments, and a list that collapsed
  // to the selected one would take that away at exactly the moment it is
  // wanted — you cannot click your way out of a filter you can no longer
  // see. The selected one is marked instead.
  const names = Object.keys(updated).sort();
  if (names.length === 0) {
    container.appendChild(el("span", "muted", "Nothing has reported yet."));
    return;
  }
  container.appendChild(el("span", "muted", "Last update"));
  for (const name of names) {
    const active = state.environment === name;
    const item = el("button",
      "env-updated-item" + (active ? " is-active" : ""));
    item.type = "button";
    item.appendChild(el("span", "env-updated-name", name));
    item.appendChild(el("span", "env-updated-when",
      ageWords(updated[name], summary.generated_at)));
    item.setAttribute("aria-pressed", active ? "true" : "false");
    item.title = name + " last reported at " + formatTime(updated[name])
      + " UTC · click to " + (active ? "show all environments"
        : "show only " + name);
    // Clicking the active pill clears the filter, so the same control
    // that scoped the page is the one that unscopes it.
    item.addEventListener("click", () => {
      setEnvironment(active ? "" : name);
    });
    container.appendChild(item);
  }
}

/*
 * What the tiles are counting, said in words.
 *
 * They used to be headed "Last night" and subtitled "36h", both fixed
 * strings. Neither survived WP-12: the window is now derived from when
 * the suite actually ran, so on a Tuesday morning after a Monday-morning
 * run it was three days wide while the page called it a night. The
 * label has to come from the same value the counting did.
 */
function windowPhrase(summary) {
  return "since " + formatTime(summary.stale_before);
}

function renderStatus() {
  const summary = state.summary;
  const status = summary.status;
  const container = document.getElementById("stat-tiles");
  clearNode(container);

  // Retired tests are context, not a work queue — they belong in the
  // header line rather than taking a tile away from something actionable.
  const meta = document.getElementById("status-meta");
  clearNode(meta);
  meta.appendChild(document.createTextNode(
    status.total_tests.toLocaleString() + " tests tracked"
    + (state.environment ? " in " + state.environment : "")
    + " · counting each test's latest run " + windowPhrase(summary)));
  if (status.retired > 0) {
    meta.appendChild(document.createTextNode(" · "));
    const link = el("button", "link-btn",
      status.retired.toLocaleString() + " retired");
    link.type = "button";
    link.title = "Tests approved as no longer in the suite";
    link.addEventListener("click", () => {
      state.showRetired = true;
      state.staleOnly = false;
      syncStaleToggle();
      syncRetiredToggle();
      refilterBrowse();
      scrollTo("browse-section");
    });
    meta.appendChild(link);
  }

  const ran = status.ran_recently;
  const failRecent = status.recent_results.FAIL;
  const diff = status.new_failures - status.fixed;
  let delta;
  if (ran === 0) {
    delta = null;
  } else if (diff > 0) {
    delta = { cls: "delta-bad",
      text: "▲ " + diff + " more failing than before" };
  } else if (diff < 0) {
    delta = { cls: "delta-good",
      text: "▼ " + (-diff) + " fewer failing than before" };
  } else {
    delta = { cls: "delta-flat", text: "no net change" };
  }
  let rate = "—";
  if (ran > 0) {
    const pct = ((ran - failRecent) / ran) * 100;
    rate = (pct === 100 ? "100" : pct.toFixed(1)) + "%";
  }

  container.appendChild(buildTile({
    hero: true,
    label: "Pass rate",
    value: rate,
    sub: ran > 0
      ? failRecent.toLocaleString() + " of "
        + ran.toLocaleString() + " runs failed"
      : "nothing has reported " + windowPhrase(summary),
    delta: delta,
  }));
  container.appendChild(buildTile({
    label: "Reported",
    value: ran.toLocaleString(),
    sub: "of " + status.total_tests.toLocaleString() + " tests",
  }));
  container.appendChild(buildTile({
    label: "New failures",
    value: status.new_failures.toLocaleString(),
    accent: status.new_failures > 0 ? "accent-fail" : "accent-zero",
    sub: "were passing before",
    onClick: () => openQueue("new_failures"),
  }));
  container.appendChild(buildTile({
    label: "Still failing",
    value: status.still_failing.toLocaleString(),
    accent: status.still_failing > 0 ? "accent-fail-soft" : "accent-zero",
    sub: "failed before too",
    onClick: () => openQueue("still_failing"),
  }));
  container.appendChild(buildTile({
    label: "Newly fixed",
    value: status.fixed.toLocaleString(),
    accent: status.fixed > 0 ? "accent-pass" : "accent-zero",
    sub: "worth verifying",
    onClick: () => openQueue("fixed"),
  }));
  container.appendChild(buildTile({
    label: "Stale annotations",
    value: status.results.UNEXPECTED_PASS.toLocaleString(),
    accent: status.results.UNEXPECTED_PASS > 0
      ? "accent-up" : "accent-zero",
    sub: "known failures that passed",
    onClick: () => openQueue("unexpected_passes"),
  }));
  container.appendChild(buildTile({
    label: "Not run",
    value: status.not_run.toLocaleString(),
    accent: status.not_run > 0 ? "accent-warn" : "accent-zero",
    sub: "no run " + windowPhrase(summary),
    onClick: () => openQueue("not_run"),
  }));
}

/* ================= charts ================= */

const TREND_SERIES = [
  { key: "FAIL", label: "Failed", segClass: "seg-fail",
    swatchClass: "swatch-fail" },
  { key: "UNEXPECTED_PASS", label: "Unexpected pass",
    segClass: "seg-up", swatchClass: "swatch-up" },
];

function renderCharts() {
  const summary = state.summary;

  // 1. Nightly trend (stacked columns) + its table twin.
  stackedColumnChart(
    document.getElementById("trend-chart"),
    summary.trend.nights, TREND_SERIES);
  const twin = document.getElementById("trend-table-body");
  clearNode(twin);
  for (const night of summary.trend.nights) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", formatNight(night.date)));
    tr.appendChild(el("td", "num", night.FAIL.toLocaleString()));
    tr.appendChild(el("td", "num",
      night.UNEXPECTED_PASS.toLocaleString()));
    tr.appendChild(el("td", "num", night.total.toLocaleString()));
    twin.appendChild(tr);
  }

  // 2. Failing tests by environment (click to scope).
  const envItems = summary.by_environment
    .filter((entry) => entry.failed > 0)
    .sort((a, b) => b.failed - a.failed)
    .map((entry) => ({
      label: entry.environment,
      value: entry.failed,
      tooltipRows: [
        { swatchClass: "swatch-fail", label: "failing",
          value: entry.failed.toLocaleString() },
        { swatchClass: "", label: "new failures",
          value: entry.new_failures.toLocaleString() },
        { swatchClass: "", label: "tests",
          value: entry.total_tests.toLocaleString() },
      ],
      onClick: () => setEnvironment(entry.environment),
    }));
  barRows(document.getElementById("env-chart"), envItems, {});
  document.getElementById("env-chart-empty").hidden =
    envItems.length !== 0;

  // 3. Most-failing scripts (click to open in the test list).
  // Cap at 7 rows so the card stays in balance with its neighbours.
  const scriptItems = summary.top_failing_scripts.slice(0, 7).map((entry) => ({
    label: entry.script,
    sublabel: entry.environment,
    value: entry.failing,
    tooltipRows: [
      { swatchClass: "swatch-fail", label: "failing tests",
        value: entry.failing.toLocaleString() },
    ],
    onClick: () => openScriptInBrowse(entry),
  }));
  barRows(document.getElementById("scripts-chart"), scriptItems, {});
  document.getElementById("scripts-chart-empty").hidden =
    scriptItems.length !== 0;
}

function openScriptInBrowse(entry) {
  state.script = entry.script;
  state.activeResults = new Set(["FAIL"]);
  syncResultToggles();
  state.staleOnly = false;
  syncStaleToggle();
  if (entry.environment !== state.environment) {
    // Scope the whole page to the script's environment, keeping the
    // script just chosen; the reload picks up the filters set above.
    setEnvironment(entry.environment, true);
  } else {
    scriptSelect.value = entry.script;
    refilterBrowse();
  }
  scrollTo("browse-section");
}

/* ================= triage queues ================= */

const QUEUE_TABS = [
  { id: "new_failures", label: "New failures" },
  { id: "still_failing", label: "Still failing" },
  { id: "fixed", label: "Fixed" },
  { id: "unexpected_passes", label: "Stale annotations" },
  { id: "not_run", label: "Not run" },
  { id: "mine", label: "My actions" },
];

const QUEUE_EMPTY_TEXT = {
  new_failures: "No new failures — nothing broke that was passing before.",
  still_failing: "Nothing is stuck failing.",
  fixed: "No tests have gone from failing to passing.",
  unexpected_passes:
    "No stale annotations — every known failure still fails.",
  not_run: "Every test reported in — nothing has gone silent.",
  mine: "Nothing is assigned to you.",
};

function queueEntries(queueId) {
  // Every queue — "mine" included — arrives filtered, ordered and capped
  // from the server, with its exact total alongside. Rows are fetched
  // per tab; null means "not landed yet for the current filters".
  const queue = state.queues[queueId];
  return queue ? queue.tests : null;
}

function queueCount(queueId) {
  // The queue's own payload carries the freshest total; before it
  // lands, the headline's queue_totals covers the tab badge.
  const queue = state.queues[queueId];
  if (queue) {
    return queue.total;
  }
  const totals = state.summary && state.summary.queue_totals;
  return totals ? (totals[queueId] || 0) : 0;
}

function openQueue(queueId) {
  state.activeQueue = queueId;
  renderQueues();
  if (!state.queues[queueId]) {
    loadQueue(queueId, state.requestSeq);
  }
  scrollTo("triage-section");
}

function renderQueues() {
  renderQueueTabs();
  renderQueueTable();
}

/**
 * Refresh the counts after an in-row action, WITHOUT rebuilding the table.
 *
 * Assigning from a row must not yank the rows out from under the person
 * doing it — the dropdown they just used would be replaced mid-gesture
 * and any open review panel would close.
 */
async function refreshQueueCounts() {
  try {
    await fetchSummary();
    renderQueueTabs();
    renderStatus();
    renderEnvUpdated();
  } catch (err) {
    showError(err.message);
  }
}

function renderQueueTabs() {
  const tabs = document.getElementById("queue-tabs");
  clearNode(tabs);
  for (const tab of QUEUE_TABS) {
    const count = queueCount(tab.id);
    const btn = el("button", "tab");
    btn.type = "button";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected",
      tab.id === state.activeQueue ? "true" : "false");
    btn.appendChild(el("span", "", tab.label));
    btn.appendChild(el("span",
      "tab-count" + (tab.id === "new_failures" && count > 0
        ? " tab-count-hot" : ""),
      String(count)));
    btn.addEventListener("click", () => {
      state.activeQueue = tab.id;
      renderQueues();
      if (!state.queues[tab.id]) {
        loadQueue(tab.id, state.requestSeq);
      }
    });
    tabs.appendChild(btn);
  }

  const problems = queueCount("new_failures")
    + queueCount("still_failing")
    + queueCount("unexpected_passes");
  document.getElementById("all-clear").hidden = problems !== 0;
  document.getElementById("triage-meta").textContent =
    problems === 0 ? "" : problems.toLocaleString() + " open items";
}

/** Column sets per queue: header + cell builder. */
/*
 * Queues where every row has the same current result.
 *
 * Repeating an identical chip down a whole page is noise, and noise is
 * what made these tables misread. So the result is stated ONCE, above
 * the table, and those queues get no result column at all. The two
 * queues that DO vary (new failures, fixed) show the change per row.
 */
const QUEUE_INVARIANT_RESULT = {
  still_failing: { result: "FAIL", text: "Every test here is failing now." },
  unexpected_passes: {
    result: "UNEXPECTED_PASS",
    text: "Every test here passed while annotated as a known failure.",
  },
};

function queueColumns(queueId) {
  const testCol = {
    header: "Test",
    sortKey: "test_name",
    cell: (entry) => {
      const cell = el("td", "wrap");
      const link = document.createElement("a");
      const params = new URLSearchParams();
      params.append("environment", entry.environment);
      params.append("script", entry.script);
      params.append("test_name", entry.test_name);
      // Scope-carriage (found by the F1-F7 sweep's follow-up link-matrix
      // audit): on a long-running branch's "Its own results" tab, these
      // rows are the branch's own -- the link must land back on that
      // SAME stream's test page, not mainline's. appendStream() is a
      // no-op (state.streamId === null) on every mainline page.
      appendStream(params);
      link.href = "test.html?" + params.toString();
      link.textContent = entry.test_name;
      cell.appendChild(link);
      cell.appendChild(el("span", "row-sub",
        entry.environment + " · " + entry.script));
      return cell;
    },
  };
  // Assigning happens IN the row: a dropdown that saves on change, plus
  // a one-click path to take it yourself. Neither needs a panel opened
  // or a page visited.
  const assigneeCol = {
    header: "Assignee",
    sortKey: "assignee",
    cell: (entry) => {
      const cell = el("td", "assignee-cell");
      cell.appendChild(assigneeSelect(entry, () => refreshQueueCounts()));
      if (!entry.assignee) {
        const btn = el("button", "take-btn", "Take");
        btn.type = "button";
        btn.title = "Assign this test to me";
        btn.addEventListener("click", () => takeTest(entry, btn));
        cell.appendChild(btn);
      }
      return cell;
    },
  };
  const when = (header, key) => ({
    header: header,
    sortKey: key,
    cell: (entry) => el("td", "",
      entry[key] ? formatTime(entry[key]) : "—"),
  });
  const failingSinceCol = {
    header: "Failing since",
    sortKey: "failing_since",
    cell: (entry) => {
      const cell = el("td");
      if (!entry.failing_since) {
        cell.textContent = "—";
        return cell;
      }
      cell.appendChild(document.createTextNode(
        formatNight(entry.failing_since)));
      const nights = nightsBetween(entry.failing_since,
        state.summary.generated_at);
      if (nights >= 1) {
        cell.appendChild(el("span", "row-sub",
          nights + (nights === 1 ? " night" : " nights")));
      }
      return cell;
    },
  };
  // What someone already found out. Without it, triage means opening
  // each test to discover it was looked at yesterday.
  const commentCol = {
    // No sortKey: ordering by comment time needs a field the
    // summary does not carry. A header that looked sortable and
    // quietly sorted by something else is worse than one that
    // does not sort at all.
    header: "Latest comment",
    cell: (entry) => {
      const cell = el("td", "wrap comment-cell");
      if (entry.latest_comment) {
        cell.appendChild(
          el("span", "comment-text", entry.latest_comment.text));
        cell.appendChild(el("span", "row-sub",
          entry.latest_comment.author + " · "
          + formatTime(entry.latest_comment.created_at)));
      } else {
        cell.appendChild(el("span", "muted", "—"));
      }
      return cell;
    },
  };

  const resultCol = {
    header: "Result",
    sortKey: "result",
    cell: (entry) => {
      const cell = el("td");
      cell.appendChild(resultChip(entry.result));
      return cell;
    },
  };

  // "was → now", for the two queues where the result CHANGED and the
  // change is the reason the row is here.
  //
  // These two used to show the previous result as a full solid chip
  // with the current one appearing only as a stripe on the row edge —
  // so a new failure (previously PASS) read as a pass, and a fixed test
  // (previously FAIL) read as a failure. Reported from real use, and
  // wrong in the misleading direction in both queues.
  const transitionCol = {
    header: "Result",
    sortKey: "result",
    cell: (entry) => resultTransition(
      el("td"), entry.prev_result, entry.result),
  };

  switch (queueId) {
    case "new_failures":
      return [testCol,
        when("Failed at", "start_time"),
        transitionCol,
        commentCol,
        assigneeCol];
    case "still_failing":
      return [testCol, failingSinceCol,
        when("Last pass", "last_pass_time"),
        commentCol,
        assigneeCol];
    case "fixed":
      // No "failing since" here: these tests are passing now, so the
      // summary reports no streak for them.
      return [testCol,
        when("Passed at", "start_time"),
        transitionCol,
        commentCol,
        { header: "Assignee",
          cell: (entry) => el("td", "", entry.assignee || "—") }];
    case "unexpected_passes":
      return [testCol,
        { header: "Known-failure reason",
          cell: (entry) => el("td", "wrap reason-cell",
            entry.known_failure_reason || "—") },
        commentCol,
        assigneeCol];
    case "not_run":
      return [testCol,
        when("Last seen", "start_time"),
        { header: "Last result",
          cell: (entry) => {
            const cell = el("td");
            cell.appendChild(resultChip(entry.result));
            return cell;
          } },
        commentCol,
        assigneeCol];
    default:  // mine
      return [testCol, resultCol, failingSinceCol, commentCol,
        assigneeCol];
  }
}

/* ---- inline review ----
 *
 * The panel itself lives in review.js: the open-actions page needs
 * the same one, and a second copy would be a second set of bugs.
 * What stays here is only what is specific to this page — the
 * staleness cutoff, which comes from the summary, and what a change
 * made inside the panel should refresh.
 */

/** Options handed to the shared review panel from this page. */
function reviewOptions() {
  // The panel is told the cutoff rather than asking anyone whether a
  // test is stale, so it stays free of this page's state.
  // The SERVER decides what counts as stale — it is derived from when
  // the suite actually ran, not from a fixed number of hours, and this
  // value gates the offer to retire a test. Recomputing it here from
  // recent_hours would re-introduce the bug where every test looks
  // abandoned on a Monday.
  return {
    staleBefore: state.summary ? state.summary.stale_before : null,
    onChanged: () => refreshQueueCounts(),
    onRetired: () => refreshSummary(),
  };
}

function renderQueueTable() {
  const queueId = state.activeQueue;
  const allEntries = queueEntries(queueId);
  const table = document.getElementById("queue-table");
  const headRow = document.getElementById("queue-head-row");
  const body = document.getElementById("queue-body");
  const emptyNote = document.getElementById("queue-empty");
  const capNote = document.getElementById("queue-cap-note");
  const resultNote = document.getElementById("queue-result-note");

  clearNode(headRow);
  clearNode(body);
  clearNode(resultNote);

  const invariant = QUEUE_INVARIANT_RESULT[queueId];
  resultNote.hidden = !invariant;
  if (invariant) {
    resultNote.appendChild(resultChip(invariant.result));
    resultNote.appendChild(el("span", "", invariant.text));
  }

  if (queueId === "mine" && !getUsername()) {
    table.hidden = true;
    emptyNote.textContent =
      "Set a username (top right) to see tests assigned to you.";
    emptyNote.hidden = false;
    capNote.hidden = true;
    resultNote.hidden = true;
    return;
  }
  if (allEntries === null) {
    // This tab's rows have not landed yet. One quiet line rather than
    // a skeleton — refreshes keep the previous table dimmed, so this
    // shows only the first time a queue is opened.
    table.hidden = true;
    emptyNote.textContent = "Loading queue…";
    emptyNote.hidden = false;
    capNote.hidden = true;
    resultNote.hidden = true;
    return;
  }
  if (allEntries.length === 0) {
    table.hidden = true;
    emptyNote.textContent = QUEUE_EMPTY_TEXT[queueId];
    emptyNote.hidden = false;
    capNote.hidden = true;
    resultNote.hidden = true;
    return;
  }

  const columns = queueColumns(queueId);
  // These queues are CAPPED slices of a larger set, so sorting them in
  // the browser is only honest while nothing has been cut off. Past the
  // cap, "the oldest failure" would silently mean "the oldest among the
  // 500 that happen to have been sent" — the same lie that keeps Open
  // actions on server-side sorting. The controls are switched off with
  // a reason rather than quietly reordering a truncated list.
  const capped = queueCount(queueId) > allEntries.length;
  const entries = capped
    ? allEntries
    : sortRows(allEntries, state.queueSortKey || "", state.queueSortDesc);

  for (const column of columns) {
    const th = el("th");
    if (column.sortKey) {
      th.setAttribute("aria-sort", "none");
      const button = el("button", "sort-btn", column.header);
      button.type = "button";
      button.dataset.key = column.sortKey;
      button.appendChild(el("span", "sort-arrow"));
      th.appendChild(button);
    } else {
      th.textContent = column.header;
    }
    headRow.appendChild(th);
  }
  // An explicit, labelled action column: the review panel has to be
  // obvious to someone who has never used the page before.
  headRow.appendChild(el("th", "", "Output"));

  for (const entry of entries) {
    const tr = document.createElement("tr");
    const marker = resultClass(entry.result);
    if (marker) {
      tr.className = marker;
    }
    for (const column of columns) {
      tr.appendChild(column.cell(entry));
    }
    const actionCell = el("td", "review-cell");
    const reviewBtn = el("button", "review-btn", "Review");
    reviewBtn.type = "button";
    reviewBtn.setAttribute("aria-expanded", "false");
    reviewBtn.title = "Show this run's output, and assign it";
    reviewBtn.addEventListener(
      "click", () => toggleReview(entry, tr, reviewBtn, reviewOptions()));
    actionCell.appendChild(reviewBtn);
    tr.appendChild(actionCell);
    body.appendChild(tr);

    // Keep panels open across the re-render that follows an action.
    reopenIfOpen(entry, tr, reviewBtn, reviewOptions());
  }
  const sorter = attachSorting(table, (key, descending) => {
    state.queueSortKey = key;
    state.queueSortDesc = descending;
    renderQueueTable();
  }, { key: state.queueSortKey, descending: state.queueSortDesc });
  if (capped) {
    sorter.disable(
      "This queue is showing the first " + allEntries.length.toLocaleString()
      + " of " + queueCount(queueId).toLocaleString()
      + ". Sorting them here would order that slice, not the whole "
      + "queue. Narrow the filters to sort.");
  }

  table.hidden = false;
  emptyNote.hidden = true;

  const total = queueCount(queueId);
  if (total > entries.length) {
    capNote.textContent = "Showing the first "
      + entries.length.toLocaleString() + " of "
      + total.toLocaleString()
      + " — filter by environment to narrow the queue.";
    capNote.hidden = false;
  } else {
    capNote.hidden = true;
  }
}

async function takeTest(entry, btn) {
  const username = requireUsername();
  if (!username) {
    return;
  }
  btn.disabled = true;
  try {
    await putJson(
      testApiPath(entry.environment, entry.script, entry.test_name,
        "/assignee"),
      { username: username, assigned_by: username });
    await refreshSummary();
  } catch (err) {
    btn.disabled = false;
    showError(err.message);
  }
}

/* ================= all tests (browse) ================= */

function populateScriptOptions() {
  const scripts = state.summary.scripts;
  // Defensive: if the selected script is not in scope any more, the
  // dropdown would show "All scripts" while the list stayed filtered to
  // something invisible. Drop the filter and reload instead.
  if (state.script && scripts.indexOf(state.script) === -1) {
    state.script = "";
    refilterBrowse();
  }
  fillSelect(scriptSelect, scripts, "All scripts", state.script);
}

/** A link to one suite's execution history. */
function scriptLink(environment, script, text) {
  const params = new URLSearchParams();
  params.append("environment", environment);
  params.append("script", script);
  const link = document.createElement("a");
  link.href = "script.html?" + params.toString();
  link.textContent = text || script;
  link.title = "Execution history for this suite";
  return link;
}

/** Whole nights between two ISO timestamps (date difference in days). */
function nightsBetween(fromIso, toIso) {
  const from = new Date(fromIso.slice(0, 10) + "T00:00:00Z");
  const to = new Date(toIso.slice(0, 10) + "T00:00:00Z");
  return Math.round((to - from) / 86400000);
}

/** Apply a change to the test-list filters and reload it from the top. */
function refilterBrowse() {
  loadBrowse(false);
}

/**
 * Render the test list. `rows` are the rows just fetched; when `append`
 * they are added below what is already there, otherwise they replace it.
 */
function renderBrowse(rows, append) {
  if (!append) {
    clearNode(tbody);
  }
  for (const row of rows) {
    tbody.appendChild(buildRow(row));
  }
  updateSortIndicators();
  // Newly appended rows (e.g. "Show more") start with their Product cell
  // hidden — bring them into line with whatever the headline already
  // decided, rather than waiting for the next full refresh.
  updateProductColumn();

  const shown = state.browseRows.length;
  const total = state.browseTotal;
  document.getElementById("empty-state").hidden = total !== 0;
  document.getElementById("browse-meta").textContent = state.summary
    ? state.summary.status.total_tests.toLocaleString() + " tests"
    : "";

  const count = document.getElementById("browse-count");
  const moreBtn = document.getElementById("show-more");
  if (total > shown) {
    count.textContent = "Showing " + shown.toLocaleString()
      + " of " + total.toLocaleString() + " matching tests";
    moreBtn.textContent = "Show "
      + Math.min(CHUNK, total - shown).toLocaleString() + " more";
    moreBtn.hidden = false;
  } else {
    count.textContent = total === 0 ? ""
      : total.toLocaleString() + " matching tests";
    moreBtn.hidden = true;
  }
}

function buildRow(row) {
  const tr = document.createElement("tr");
  const marker = resultClass(row.result);
  if (marker) {
    tr.className = marker;
  }
  tr.appendChild(el("td", "", row.environment));

  // Hidden by default; updateProductColumn() shows it once the headline
  // (products.length, the current scope) has landed — which may arrive
  // before or after this row does.
  const productCell = el("td", "product-col", row.product);
  productCell.hidden = true;
  tr.appendChild(productCell);

  // The script is a link to that suite's execution history — the way to
  // answer "how did the whole suite do last night?".
  const scriptCell = document.createElement("td");
  scriptCell.appendChild(scriptLink(row.environment, row.script));
  tr.appendChild(scriptCell);

  const testCell = document.createElement("td");
  testCell.className = "wrap";
  const link = document.createElement("a");
  const params = new URLSearchParams();
  params.append("environment", row.environment);
  params.append("script", row.script);
  params.append("test_name", row.test_name);
  // Scope-carriage (F1-F7 sweep follow-up): the browse table on a
  // branch's "Its own results" tab shows that branch's own rows -- the
  // link must land back on that SAME stream's test page. No-op on
  // mainline.
  appendStream(params);
  link.href = "test.html?" + params.toString();
  link.textContent = row.test_name;
  testCell.appendChild(link);
  tr.appendChild(testCell);

  const resultCell = document.createElement("td");
  const chip = resultChip(row.result);
  if (row.known_failure_reason) {
    chip.title = "Known failure: " + row.known_failure_reason;
  }
  resultCell.appendChild(chip);
  tr.appendChild(resultCell);

  tr.appendChild(el("td", "", formatTime(row.start_time)));
  tr.appendChild(el("td", "num", formatDuration(row.duration_seconds)));

  const ownerCell = el("td", "assignee-cell");
  ownerCell.appendChild(assigneeSelect(row, null));
  tr.appendChild(ownerCell);

  // Every test gets the review panel, not just failing ones: a passing
  // test is exactly where you want to record "this only passes because
  // the fixture is stubbed" while you still remember it.
  const actionCell = el("td", "review-cell");
  const reviewBtn = el("button", "review-btn", "Review");
  reviewBtn.type = "button";
  reviewBtn.setAttribute("aria-expanded", "false");
  reviewBtn.title = "Show this run's output, assign it, or comment";
  reviewBtn.addEventListener(
    "click", () => toggleReview(row, tr, reviewBtn, reviewOptions()));
  actionCell.appendChild(reviewBtn);
  tr.appendChild(actionCell);
  return tr;
}

function updateSortIndicators() {
  const buttons =
    document.querySelectorAll("#dashboard-table thead .sort-btn");
  for (const btn of buttons) {
    const th = btn.closest("th");
    const arrow = btn.querySelector(".sort-arrow");
    if (btn.dataset.key === state.sortKey) {
      arrow.textContent = state.sortAsc ? " ▲" : " ▼";
      th.setAttribute("aria-sort",
        state.sortAsc ? "ascending" : "descending");
    } else {
      arrow.textContent = "";
      th.setAttribute("aria-sort", "none");
    }
  }
}

/* ================= filter controls ================= */

function buildResultToggles() {
  const container = document.getElementById("result-toggles");
  for (const result of RESULTS) {
    const btn = el("button", "toggle " + resultClass(result), result);
    btn.type = "button";
    btn.dataset.result = result;
    btn.setAttribute("aria-pressed", "false");
    btn.addEventListener("click", () => {
      if (state.activeResults.has(result)) {
        state.activeResults.delete(result);
      } else {
        state.activeResults.add(result);
      }
      syncResultToggles();
      refilterBrowse();
    });
    container.appendChild(btn);
  }
}

function syncResultToggles() {
  const buttons = document.querySelectorAll("#result-toggles .toggle");
  for (const btn of buttons) {
    btn.setAttribute("aria-pressed",
      state.activeResults.has(btn.dataset.result) ? "true" : "false");
  }
}

function syncStaleToggle() {
  document.getElementById("stale-toggle")
    .setAttribute("aria-pressed", state.staleOnly ? "true" : "false");
}

function syncRetiredToggle() {
  document.getElementById("retired-toggle")
    .setAttribute("aria-pressed", state.showRetired ? "true" : "false");
}

function syncUnassignedToggle() {
  document.getElementById("unassigned-toggle")
    .setAttribute("aria-pressed", state.unassignedOnly ? "true" : "false");
}

function scrollTo(sectionId) {
  document.getElementById(sectionId)
    .scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ================= init ================= */

/** Sections that only mean something scoped to a real dashboard body
 * (mainline, or a branch's "own results" tab) — the same set
 * compare.js's MAINLINE_SECTIONS names, kept here too because app.js
 * is what shows them again when switching back from the diff tab. */
const DASHBOARD_SECTIONS = SECTIONS;

let mainlineControlsWired = false;

/**
 * Wire every one-time event listener the dashboard body needs
 * (environment/script filters, search, toggles, sort headers, refresh,
 * show-more). Idempotent — called once whether the page starts on a
 * genuine mainline load or on a branch's "Its own results" tab, and
 * NEVER again after that: switching between "Its own results" and
 * "Difference from …" (see selectBranchTab()) must not pile up a
 * second copy of every listener.
 */
function wireMainlineControls() {
  if (mainlineControlsWired) {
    return;
  }
  mainlineControlsWired = true;

  const url = new URL(window.location.href);
  state.environment = url.searchParams.get("environment") || "";

  // F4 (docs/STREAMS_PLAN.md §5.2 "as built"): URL-driven filter state,
  // so a deep link (a Watch card's "N unassigned failing" stat, or any
  // other) can land pre-filtered rather than only naming a number.
  // Read BEFORE buildResultToggles()/the sync calls below paint the
  // controls, so their initial aria-pressed state already matches —
  // reading it after would need a second render pass.
  for (const raw of url.searchParams.getAll("result")) {
    if (RESULTS.indexOf(raw) !== -1) {
      state.activeResults.add(raw);
    }
  }
  if (url.searchParams.get("unassigned") === "1") {
    state.unassignedOnly = true;
  }
  if (url.searchParams.get("stale") === "1") {
    state.staleOnly = true;
  }

  buildResultToggles();
  syncResultToggles();
  syncStaleToggle();
  syncUnassignedToggle();

  envSelect.addEventListener("change",
    () => setEnvironment(envSelect.value));
  scriptSelect.addEventListener("change", () => {
    state.script = scriptSelect.value;
    refilterBrowse();
  });
  qInput.addEventListener("input", () => {
    if (state.qTimer) {
      window.clearTimeout(state.qTimer);
    }
    // Debounced: one query per pause in typing, not one per keystroke.
    state.qTimer = window.setTimeout(() => {
      state.qText = qInput.value;
      refilterBrowse();
    }, 250);
  });
  document.getElementById("stale-toggle")
    .addEventListener("click", () => {
      state.staleOnly = !state.staleOnly;
      syncStaleToggle();
      refilterBrowse();
    });
  document.getElementById("retired-toggle")
    .addEventListener("click", () => {
      state.showRetired = !state.showRetired;
      syncRetiredToggle();
      refilterBrowse();
    });
  document.getElementById("unassigned-toggle")
    .addEventListener("click", () => {
      state.unassignedOnly = !state.unassignedOnly;
      syncUnassignedToggle();
      refilterBrowse();
    });
  document.getElementById("reload-btn")
    .addEventListener("click", () => refreshAll());
  document.getElementById("show-more").addEventListener("click", () => {
    loadBrowse(true);
  });

  for (const btn of
    document.querySelectorAll("#dashboard-table thead .sort-btn")) {
    btn.addEventListener("click", () => {
      const key = btn.dataset.key;
      if (state.sortKey === key) {
        state.sortAsc = !state.sortAsc;
      } else {
        state.sortKey = key;
        state.sortAsc = true;
      }
      // Sorting is a server-side ORDER BY over the whole matching set,
      // not a reshuffle of the page on screen.
      refilterBrowse();
    });
  }
}

/**
 * F6 (docs/STREAMS_PLAN.md §5.2 "as built"): quick links from a
 * branch's "Its own results" tab into that SAME branch's own Time and
 * Timeline pages — needs F7 (those pages could not read `stream=`
 * before it). `environment=` is included only when the dashboard's own
 * environment filter is currently set to one; Timeline still works
 * without it (it picks a sensible default itself), Time never needed
 * one. Hidden outright when leaving the tab (mainline, or "Difference
 * from …") — this is an "own results" concept only.
 *
 * Both links also carry `product=` (state.streamProduct, stashed by
 * initBranchDashboard from data.stream.product — empty string included
 * when the estate has no products). Without it these links are exactly
 * the bug commit 4725bbc fixed: Time/Timeline load products.js, which
 * adopts `?product=` into localStorage, but absent the param they keep
 * whatever this browser last had — Timeline's environment picker is
 * filtered by that stored product, so a wrong-product browser opening
 * this link can find a picker that does not even list the branch's own
 * environment.
 */
function renderBranchQuickLinks(streamId) {
  const mount = document.getElementById("branch-quick-links");
  if (!mount) {
    return;
  }
  clearNode(mount);
  if (streamId === null) {
    mount.hidden = true;
    return;
  }
  const timeParams = new URLSearchParams();
  timeParams.append("stream", String(streamId));
  timeParams.append("product", state.streamProduct || "");
  const timelineParams = new URLSearchParams();
  if (state.environment) {
    timelineParams.append("environment", state.environment);
  }
  timelineParams.append("stream", String(streamId));
  timelineParams.append("product", state.streamProduct || "");

  const timeLink = document.createElement("a");
  timeLink.href = "time.html?" + timeParams.toString();
  timeLink.textContent = "This branch's Time →";
  const timelineLink = document.createElement("a");
  timelineLink.href = "timeline.html?" + timelineParams.toString();
  timelineLink.textContent = "This branch's Timeline →";

  mount.appendChild(timeLink);
  mount.appendChild(document.createTextNode("  ·  "));
  mount.appendChild(timelineLink);
  mount.hidden = false;
}

/**
 * Show the dashboard body (status/charts/triage/browse), scoped to
 * *streamId* if the "Its own results" tab is active — WP-23,
 * docs/STREAMS_PLAN.md §5.2. Hides the delta section, wires the
 * mainline controls exactly once, and reloads.
 */
function activateOwnResultsTab() {
  document.getElementById("delta-section").hidden = true;
  const envField = document.getElementById("env-filter-field");
  if (envField) {
    envField.hidden = false;
  }
  wireMainlineControls();
  renderBranchQuickLinks(state.streamId);
  document.getElementById("loading-state").hidden = false;
  refreshAll();
}

/** Swap to the delta ("Difference from …") view — hides the dashboard
 * body outright, the same swap compare.js's own initDeltaView() has
 * always done for a branch-scoped page. */
function activateDiffTab(streamId) {
  for (const id of DASHBOARD_SECTIONS) {
    document.getElementById(id).hidden = true;
  }
  renderBranchQuickLinks(null);   // F6: "own results" concept only
  initDeltaView(streamId);
}

/**
 * The two-tab header for a long-running branch stream (WP-23,
 * docs/STREAMS_PLAN.md §5.2): "Its own results" (this same dashboard,
 * scoped to the branch's own stream_id) and "Difference from
 * <baseline>" (the WP-21/22 delta view, unchanged). BOTH tabs always
 * exist for a branch stream — this is not gated on cadence, only the
 * DEFAULT selection is (see the caption below).
 *
 * Release builds (kind 'build') and anything else non-branch keep the
 * exact WP-21/22 behaviour — delta view only, no tab header — since
 * §5.2 frames this as a BRANCH concept: an RC is not a second mainline
 * with its own nightly cadence, and giving it a trend/staleness of its
 * own would mostly be empty.
 */
function selectBranchTab(which, streamId) {
  const ownBtn = document.getElementById("branch-tab-own");
  const diffBtn = document.getElementById("branch-tab-diff");
  ownBtn.setAttribute("aria-selected", which === "own" ? "true" : "false");
  diffBtn.setAttribute("aria-selected", which === "diff" ? "true" : "false");
  if (which === "own") {
    state.streamId = streamId;
    activateOwnResultsTab();
  } else {
    state.streamId = null;
    activateDiffTab(streamId);
  }
}

async function initBranchDashboard(streamId) {
  let data;
  try {
    data = await fetchCompare(streamId, null, 0, getSelectedBaselineId());
  } catch (err) {
    showError(err.message);
    return;
  }
  renderBranchBand(data.stream, data.baseline);
  // F6: stashed once here, before either tab renders, so
  // renderBranchQuickLinks() can make its links scope-self-sufficient.
  state.streamProduct = data.stream.product || "";

  const tabs = document.getElementById("branch-tabs");
  const caption = document.getElementById("branch-tab-caption");
  if (data.stream.kind !== "branch" || !tabs) {
    // Builds, and any deployment predating this drop's markup: the
    // unchanged WP-21/22 delta-only behaviour.
    if (tabs) {
      tabs.hidden = true;
    }
    if (caption) {
      caption.hidden = true;
    }
    activateDiffTab(streamId);
    return;
  }

  const ownBtn = document.getElementById("branch-tab-own");
  const diffBtn = document.getElementById("branch-tab-diff");
  diffBtn.textContent = "Difference from " + streamLabel(data.baseline);
  ownBtn.onclick = () => selectBranchTab("own", streamId);
  diffBtn.onclick = () => selectBranchTab("diff", streamId);
  tabs.hidden = false;

  // The default tab: "Its own results" once the branch shows a
  // regular-enough cadence, "Difference from …" otherwise. Stated in
  // the caption FROM DATA — the covered-pass count and the threshold
  // are both literally in the sentence, never a silent constant
  // (docs/STREAMS_PLAN.md §5.2's own wording: "must be stated in the
  // UI caption, not buried" — the same discipline WindowWordingTest
  // holds every other recency line to).
  let coveredPasses = 0;
  try {
    const headline = await fetchJson(
      "/api/summary?parts=headline&stream=" + streamId);
    coveredPasses = headline.covered_passes;
  } catch (err) {
    // The tab still works either way; the caption just falls back to
    // the safe (diff) default below rather than guessing.
  }
  const preferOwn = coveredPasses >= OWN_RESULTS_DEFAULT_PASSES;
  if (caption) {
    const passWord = coveredPasses === 1 ? "pass" : "passes";
    caption.hidden = false;
    caption.textContent = preferOwn
      ? ("Showing its own results by default — this branch has "
         + "completed " + coveredPasses + " " + passWord + " in the "
         + "last 14 days (" + OWN_RESULTS_DEFAULT_PASSES + " or more "
         + "shows its own dashboard first).")
      : ("Showing the difference from mainline by default — this "
         + "branch has completed " + coveredPasses + " " + passWord
         + " in the last 14 days (needs " + OWN_RESULTS_DEFAULT_PASSES
         + " or more to show its own dashboard first).");
  }
  selectBranchTab(preferOwn ? "own" : "diff", streamId);
}

function init() {
  // "My actions" is scoped server-side to the signed-in user, so a
  // username change has to go back to the server for it. Rendered
  // before the stream check below: the header is not a mainline-only
  // concept, so a branch-scoped page keeps it too.
  renderUserWidget(document.getElementById("user-widget"),
    () => { if (state.summary) { refreshSummary(); } });

  // WP-21/23 (docs/STREAMS_PLAN.md §3.6/§5.2): a branch-scoped page
  // load never reaches the plain mainline path below this check — no
  // unscoped /api/summary fetch, no queues, no browse table. That is
  // what keeps every mainline page (this branch never taken) at zero
  // visible change.
  const streamId = getSelectedStreamId();
  if (streamId !== null) {
    initBranchDashboard(streamId);
    return;
  }

  wireMainlineControls();
  refreshAll();
}

init();
