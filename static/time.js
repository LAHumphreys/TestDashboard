/* time.js — "where is the time going", as a drill-down.
 *
 * Environments → the scripts in one → the tests in one script. Each level
 * is a server-side GROUP BY over `latest_runs`, so the page never holds
 * more than the level on screen and the cost does not grow with how much
 * history exists.
 *
 * WHAT IT MEASURES, and why the page says so out loud: the most recent
 * run of each test, added up. Not a historical window. A test whose last
 * run was three weeks ago is excluded rather than counted, because
 * counting it would claim time was spent last night that was not — and
 * the number of exclusions is shown rather than swallowed.
 *
 * Form: a treemap — the profiler-style box graph. This replaced
 * horizontal bars, and the reasoning that chose bars is worth keeping
 * because it was not wrong: area is read less precisely than length, and
 * a treemap cannot label its small cells at all.
 *
 * What it buys instead is the question this page exists to answer. "Where
 * is the time going" is a question about PROPORTION — how much of the
 * night is this environment, is one script most of it — and a treemap
 * shows the whole and the parts in one shape, at every level of the
 * drill-down. A column of bars shows the ranking clearly and the share
 * only by arithmetic the reader has to do.
 *
 * The known weakness is answered rather than ignored: labels are drawn
 * only in boxes big enough to hold them, every box is a keyboard-focusable
 * control with its share in its accessible name, and the full data table
 * below the chart is sortable and lists every row. For a small slice the
 * table is not a fallback, it is the answer.
 *
 * Colour: ONE hue at five depths by share, and deliberately not the
 * pass/fail palette. This is magnitude, not status; borrowing the result
 * colours here would have people reading "red = bad" into a box that only
 * means "slow".
 */

"use strict";

import {
  clearError,
  clearNode,
  el,
  fetchJson,
  formatDuration,
  formatTime,
  renderUserWidget,
  showError,
} from "./api.js";
import { treemapBoxes } from "./charts.js";
import { attachSorting, sortRows } from "./sorting.js";
import { getSelectedProduct } from "./products.js";

const LEVELS = ["environment", "script", "test_name"];

const state = {
  environment: null,
  script: null,
  includeStale: false,
  items: [],
  sortKey: "total_seconds",
  sortDescending: true,
  seq: 0,
};

let sorter = null;

/** Which level we are looking at, derived from what is scoped. */
function level() {
  if (state.environment === null) {
    return LEVELS[0];
  }
  return state.script === null ? LEVELS[1] : LEVELS[2];
}

function url() {
  const qs = new URLSearchParams();
  qs.append("group_by", level());
  if (state.environment !== null) {
    qs.append("environment", state.environment);
  }
  if (state.script !== null) {
    qs.append("script", state.script);
  }
  if (state.includeStale) {
    qs.append("include_stale", "1");
  }
  // WP-20: scope the drill-down to a declared product's environments —
  // resolved server-side, same as every other product= filter
  // (docs/STREAMS_PLAN.md §2.2). Harmless to send alongside an explicit
  // `environment` once drilled in; the server combines both.
  const product = getSelectedProduct();
  if (product) {
    qs.append("product", product);
  }
  return "/api/time?" + qs.toString();
}

async function load() {
  const seq = ++state.seq;
  clearError();
  try {
    const data = await fetchJson(url());
    if (seq !== state.seq) {
      return;      // a later drill-down overtook this one
    }
    state.items = data.items.map((item) => ({
      key: item.key,
      total_seconds: item.total_seconds,
      test_count: item.test_count,
      mean: item.test_count
        ? item.total_seconds / item.test_count : 0,
    }));
    render(data);
  } catch (err) {
    if (seq === state.seq) {
      showError(err.message);
    }
  }
}

/** What the current level is a list OF, for labels and counts. */
function unitWord() {
  if (level() === "environment") {
    return "environments";
  }
  return level() === "script" ? "scripts" : "tests";
}

function render(data) {
  renderBreadcrumb();

  const chart = document.getElementById("time-chart");
  const empty = document.getElementById("time-empty");
  const excluded = document.getElementById("time-excluded");

  document.getElementById("time-total").textContent =
    formatDuration(data.total_seconds) + " across "
    + data.test_count.toLocaleString()
    + (data.test_count === 1 ? " test" : " tests");

  document.getElementById("time-meta").textContent =
    data.items.length.toLocaleString() + " " + unitWord();

  const staleToggle = document.getElementById("stale-toggle");
  staleToggle.setAttribute(
    "aria-pressed", state.includeStale ? "true" : "false");

  if (state.includeStale) {
    excluded.textContent =
      "Including every test's most recent run, however long ago it was. "
      + "Some of this time was not spent recently.";
    excluded.hidden = false;
  } else if (data.excluded_tests) {
    excluded.textContent = "Excludes "
      + data.excluded_tests.toLocaleString()
      + (data.excluded_tests === 1 ? " test that has" : " tests that have")
      + " not reported since " + formatTime(data.stale_before)
      + " — their last duration is on file, but counting it would"
      + " claim time that was not spent.";
    excluded.hidden = false;
  } else {
    excluded.hidden = true;
  }

  if (state.items.length === 0) {
    clearNode(chart);
    // An empty page here almost always means "the suite has not run
    // lately", not "there is nothing to measure". Say which, and point
    // at the way to see it anyway — an all-or-nothing recency cutoff
    // otherwise blanks the page after any long weekend.
    empty.textContent = data.excluded_tests
      ? "Nothing has reported since " + formatTime(data.stale_before)
        + ". Turn on “Include tests that have not run recently” to see "
        + "the breakdown from their last run."
      : "Nothing to show here.";
    empty.hidden = false;
    renderTable();
    return;
  }
  empty.hidden = true;

  const canDrill = level() !== "test_name";
  const capped = treemapBoxes(chart, state.items.map((item) => ({
    label: item.key,
    sublabel: item.test_count.toLocaleString()
      + (item.test_count === 1 ? " test" : " tests")
      + " · " + formatDuration(item.mean) + " each",
    value: item.total_seconds,
    valueText: formatDuration(item.total_seconds),
    onClick: canDrill ? () => drillInto(item.key) : null,
    // The tooltip carries Share explicitly. On a treemap the share IS
    // the encoding, so a reader estimating it from the area is exactly
    // the reading that needs a number to check itself against.
    tooltipRows: [
      { label: "total", value: formatDuration(item.total_seconds) },
      { label: "of all time shown", value: data.total_seconds
        ? Math.round(item.total_seconds / data.total_seconds * 100) + "%"
        : "—" },
      { label: "tests", value: item.test_count.toLocaleString() },
      { label: "mean each", value: formatDuration(item.mean) },
    ],
  })), {
    unitLabel: unitWord(),
    measureLabel: "the run time shown",
    formatValue: formatDuration,
  });

  // What the chart is not saying, said. The rectangle IS all of the time
  // above — every box, the combined one included, is the size its share
  // deserves — but two things still need admitting: which items got
  // merged, and which boxes are too small to carry a name. Left unsaid,
  // an unlabelled box invites "that big one must be the slow one" about
  // a box that is nothing of the kind.
  const capNote = document.getElementById("time-capped");
  const notes = [];
  if (capped.hiddenCount) {
    notes.push("The smallest " + capped.hiddenCount.toLocaleString() + " "
      + unitWord() + " are combined into one box — "
      + formatDuration(capped.hiddenValue) + " between them, "
      + (capped.total
        ? Math.round((capped.hiddenValue / capped.total) * 100) : 0)
      + "% of the time above.");
  }
  if (capped.unlabelled) {
    notes.push(capped.unlabelled.toLocaleString() + " of "
      + capped.drawn.toLocaleString()
      + " boxes are too small to print a name in; hover one to see it.");
  }
  if (notes.length) {
    capNote.textContent = notes.join(" ")
      + " The data table below names every one.";
    capNote.hidden = false;
  } else {
    capNote.hidden = true;
  }

  renderTable();
}

function drillInto(key) {
  if (level() === "environment") {
    state.environment = key;
  } else {
    state.script = key;
  }
  load();
}

function renderBreadcrumb() {
  const nav = document.getElementById("breadcrumb");
  clearNode(nav);

  const crumbs = [{ label: "All environments", scope: {} }];
  if (state.environment !== null) {
    crumbs.push({
      label: state.environment,
      scope: { environment: state.environment },
    });
  }
  if (state.script !== null) {
    crumbs.push({
      label: state.script,
      scope: { environment: state.environment, script: state.script },
    });
  }

  crumbs.forEach((crumb, index) => {
    const last = index === crumbs.length - 1;
    if (last) {
      nav.appendChild(el("span", "crumb crumb-current", crumb.label));
      return;
    }
    const button = el("button", "crumb", crumb.label);
    button.type = "button";
    button.addEventListener("click", () => {
      state.environment = crumb.scope.environment !== undefined
        ? crumb.scope.environment : null;
      state.script = crumb.scope.script !== undefined
        ? crumb.scope.script : null;
      load();
    });
    nav.appendChild(button);
    nav.appendChild(el("span", "crumb-sep", "›"));
  });
}

function renderTable() {
  const body = document.getElementById("time-table-body");
  clearNode(body);
  // The table holds the WHOLE level, not a page of it, so sorting it in
  // the browser reorders everything there is. That is the condition
  // under which client-side sorting is honest.
  const rows = sortRows(state.items, state.sortKey, state.sortDescending);
  for (const item of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "wrap", item.key));
    tr.appendChild(el("td", "num", formatDuration(item.total_seconds)));
    tr.appendChild(el("td", "num", item.test_count.toLocaleString()));
    tr.appendChild(el("td", "num", formatDuration(item.mean)));
    body.appendChild(tr);
  }
}

function init() {
  renderUserWidget(document.getElementById("user-widget"), null);

  const params = new URL(window.location.href).searchParams;
  state.environment = params.get("environment");
  state.script = params.get("script");

  document.getElementById("stale-toggle")
    .addEventListener("click", () => {
      state.includeStale = !state.includeStale;
      load();
    });

  sorter = attachSorting(
    document.getElementById("time-table"),
    (key, descending) => {
      state.sortKey = key;
      state.sortDescending = descending;
      renderTable();
    },
    { key: state.sortKey, descending: state.sortDescending });

  load();
}

init();
