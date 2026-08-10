/* script.js — the history of one suite (script).
 *
 * The dashboard is organised around individual tests, but people run and
 * reason about whole scripts: "did last night's regression suite pass,
 * and how did it compare with the night before?".
 *
 * Everything here is per EXECUTION rather than per calendar day. A suite
 * that runs twice in a day gets two bars and two rows — which the
 * daily trend on the home screen cannot show, because it buckets by
 * date. Executions are inferred from run timings server-side (see
 * GET /api/scripts/{env}/{script}/executions).
 *
 * All data reaches the DOM via textContent.
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
  resultChip,
  resultClass,
  showError,
} from "./api.js";
import { stackedColumnChart } from "./charts.js";
import { renderBranchBand } from "./compare.js";
import { apiUrl, pageUrl } from "./urls.js";

/** Tests listed per page in the lower table. */
const CHUNK = 100;

const state = {
  environment: "",
  script: "",
  days: 14,
  tests: [],
  total: 0,
  // Script-page parity (FINAL ROUND, docs/STREAMS_PLAN.md §5.2 "as
  // built"): this page had no stream support of its own before this --
  // absent means mainline, the same "zero visible change" rule every
  // other stream-aware page follows (WP-23's `?stream=` grammar).
  streamId: null,
};

/* The same status colours the rest of the dashboard uses. */
const EXECUTION_SERIES = [
  { key: "FAIL", label: "Failed", segClass: "seg-fail",
    swatchClass: "swatch-fail" },
  { key: "UNEXPECTED_PASS", label: "Unexpected pass",
    segClass: "seg-up", swatchClass: "swatch-up" },
];

/* ================= data ================= */

function scriptApiPath(suffix) {
  return "/api/scripts/" + encodeURIComponent(state.environment)
    + "/" + encodeURIComponent(state.script) + suffix;
}

async function loadExecutions() {
  clearError();
  try {
    // scriptApiPath already encodes environment/script into the PATH,
    // so product/baseline/environment are all explicitly nulled here
    // rather than left to pageUrl()'s default carriage -- this page's
    // own URL carries `environment=` too (as identity, per init()
    // below), and a duplicate query-string copy of it was never part
    // of this fetch's shape.
    const data = await fetchJson(apiUrl(
      scriptApiPath("/executions"), { days: state.days },
      { stream: state.streamId, product: null, baseline: null,
        environment: null }));
    renderExecutions(data);
  } catch (err) {
    showError(err.message);
  } finally {
    document.getElementById("loading-state").hidden = true;
  }
}

async function loadTests(append) {
  try {
    // Script-page parity: the "tests in this suite" table must show THIS
    // stream's own current results, not mainline's, when scoped -- the
    // same /api/dashboard?stream= every other list in the app reads.
    const page = await fetchJson(apiUrl("/api/dashboard", {
      environment: state.environment,
      script: state.script,
      retired: "1",
      sort: "test_name",
      limit: CHUNK,
      offset: append ? state.tests.length : 0,
    }, { stream: state.streamId, product: null, baseline: null }));
    state.tests = append ? state.tests.concat(page.tests) : page.tests;
    state.total = page.total;
    renderTests(page.tests, append);
  } catch (err) {
    showError(err.message);
  }
}

/* ================= executions ================= */

function renderExecutions(data) {
  // Script-page parity: the same guard time.js/timeline.js/test.js use
  // -- the band only ever draws when this page was actually asked to
  // scope to a stream AND the server named one back. A mainline load
  // (state.streamId === null) never touches renderBranchBand at all.
  if (state.streamId !== null && data.stream_identity) {
    renderBranchBand(data.stream_identity);
  }
  const executions = data.executions;
  document.getElementById("empty-state").hidden = executions.length !== 0;
  document.getElementById("executions-meta").textContent =
    executions.length === 0 ? ""
      : executions.length.toLocaleString() + " execution"
        + (executions.length === 1 ? "" : "s") + " in the last "
        + data.days + " days";

  // Oldest first for the chart, so time reads left to right.
  const bars = executions.slice().reverse().map((execution) => ({
    date: execution.started,
    label: shortStamp(execution.started),
    FAIL: execution.results.FAIL,
    UNEXPECTED_PASS: execution.results.UNEXPECTED_PASS,
    total: execution.total,
  }));
  stackedColumnChart(
    document.getElementById("executions-chart"), bars, EXECUTION_SERIES,
    {
      unit: "execution",
      labelFor: (item) => item.label,
      titleFor: (item) => item.label,
    });

  const note = document.getElementById("executions-note");
  const perDay = countDaysWithSeveral(executions);
  note.textContent = executions.length === 0 ? ""
    : "One bar per execution of the suite"
      + (perDay > 0
        ? " — " + perDay + " day" + (perDay === 1 ? "" : "s")
          + " in this window had more than one."
        : ".");

  const body = document.getElementById("executions-body");
  clearNode(body);
  for (const execution of executions) {
    body.appendChild(buildExecutionRow(execution));
  }
}

/** How many calendar days in the window hold more than one execution. */
function countDaysWithSeveral(executions) {
  const perDay = new Map();
  for (const execution of executions) {
    const day = execution.started.slice(0, 10);
    perDay.set(day, (perDay.get(day) || 0) + 1);
  }
  let several = 0;
  for (const count of perDay.values()) {
    if (count > 1) {
      several += 1;
    }
  }
  return several;
}

function shortStamp(iso) {
  return iso.slice(5, 10) + " " + iso.slice(11, 16);
}

function buildExecutionRow(execution) {
  const tr = document.createElement("tr");
  if (execution.failed > 0) {
    tr.className = "result-fail";
  }
  tr.appendChild(el("td", "", formatTime(execution.started)));
  tr.appendChild(el("td", "num", execution.total.toLocaleString()));
  tr.appendChild(el("td", "num", execution.failed.toLocaleString()));

  const rate = execution.total === 0 ? 0
    : ((execution.total - execution.failed) / execution.total) * 100;
  tr.appendChild(el("td", "num",
    (rate === 100 ? "100" : rate.toFixed(1)) + "%"));
  tr.appendChild(el("td", "num",
    formatDuration(execution.duration_seconds)));

  const breakdown = el("td", "wrap");
  for (const [name, count] of Object.entries(execution.results)) {
    if (count > 0) {
      const chip = resultChip(name);
      chip.title = count + " " + name;
      breakdown.appendChild(chip);
      breakdown.appendChild(el("span", "chip-count", String(count)));
    }
  }
  tr.appendChild(breakdown);
  return tr;
}

/* ================= tests in the suite ================= */

function renderTests(rows, append) {
  const body = document.getElementById("tests-body");
  if (!append) {
    clearNode(body);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    const marker = resultClass(row.result);
    if (marker) {
      tr.className = marker;
    }

    const cell = el("td", "wrap");
    const link = document.createElement("a");
    // Scope-carriage: this page's own scope, so the test opened from
    // it lands on that SAME stream, not mainline's.
    link.href = pageUrl("test", {
      environment: row.environment, script: row.script,
      test_name: row.test_name,
    }, { stream: state.streamId, product: null, baseline: null });
    link.textContent = row.test_name;
    cell.appendChild(link);
    if (row.retired_at) {
      cell.appendChild(el("span", "row-sub",
        "retired by " + row.retired_by));
    }
    tr.appendChild(cell);

    const resultCell = el("td");
    resultCell.appendChild(resultChip(row.result));
    tr.appendChild(resultCell);
    tr.appendChild(el("td", "", formatTime(row.start_time)));
    tr.appendChild(el("td", "", row.assignee || "—"));
    body.appendChild(tr);
  }

  const shown = state.tests.length;
  document.getElementById("tests-meta").textContent =
    state.total.toLocaleString() + " tests";
  const count = document.getElementById("tests-count");
  const moreBtn = document.getElementById("show-more");
  if (state.total > shown) {
    count.textContent = "Showing " + shown.toLocaleString() + " of "
      + state.total.toLocaleString();
    moreBtn.hidden = false;
    moreBtn.textContent = "Show "
      + Math.min(CHUNK, state.total - shown).toLocaleString() + " more";
  } else {
    moreBtn.hidden = true;
    count.textContent = "";
  }
}

/* ================= init ================= */

function init() {
  renderUserWidget(document.getElementById("user-widget"));

  const url = new URL(window.location.href);
  state.environment = url.searchParams.get("environment") || "";
  state.script = url.searchParams.get("script") || "";
  const rawStream = url.searchParams.get("stream");
  state.streamId = rawStream ? parseInt(rawStream, 10) : null;
  if (!state.environment || !state.script) {
    showError("This page needs an environment and a script in the URL.");
    document.getElementById("loading-state").hidden = true;
    return;
  }

  document.getElementById("script-title").textContent = state.script;
  document.getElementById("script-identity").textContent =
    state.environment;
  document.title = "testboard — " + state.script;

  const daysSelect = document.getElementById("filter-days");
  daysSelect.addEventListener("change", () => {
    state.days = Number(daysSelect.value);
    loadExecutions();
  });
  document.getElementById("reload-btn").addEventListener("click", () => {
    loadExecutions();
    loadTests(false);
  });
  document.getElementById("show-more").addEventListener("click", () => {
    loadTests(true);
  });

  loadExecutions();
  loadTests(false);
}

init();
