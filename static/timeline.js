/* timeline.js — one environment's script running order, as rows on a
 * shared time axis.
 *
 * WHY THIS PAGE EXISTS: scripts share the system they test. When one
 * goes wrong and leaves static data modified, the failure surfaces in a
 * LATER script, and the test-centric views cannot say what "later"
 * means — they know results, not running order. This page is for the
 * archaeology: find the failure, then read upwards through what ran
 * before it.
 *
 * A ROW IS A SCRIPT EXECUTION, not a script: the server groups a
 * script's activity by timing gaps (the import contract has no batch
 * id), so a script that ran twice in the window appears twice, in its
 * real places, and a partial run is just a row whose count reads
 * "3 of 45 known tests". Both are the data, not special cases.
 *
 * THE BARS are drawn against the window's real span, so two things the
 * counts cannot show become visible: quiet gaps (the suite stalled
 * here) and overlaps (two scripts interleaved — the "illogical order"
 * this system is known for). The bar is decoration over the text; every
 * number it encodes is also written in the row.
 *
 * WORDING: blocks are labelled by their actual timestamps, never "last
 * night" — a suite can run twice a day or skip a weekend, and this
 * project has shipped that mistake three times (see WindowWordingTest).
 *
 * SECURITY: script and test names are data; they reach the DOM via
 * textContent/el() only, never innerHTML.
 */

"use strict";

import {
  clearError,
  clearNode,
  el,
  fetchJson,
  fillOutput,
  formatDuration,
  formatTime,
  renderUserWidget,
  resultChip,
  runApiPath,
  showError,
} from "./api.js";

const state = {
  environment: null,
  blocks: [],
  /* The selected window, as the SERVER'S OWN strings — block edges are
   * echoed back verbatim, so a picker choice cannot drift through a
   * client-side date round-trip. null means "newest block". */
  from: null,
  to: null,
  rows: [],
  seq: 0,
};

function timelineUrl() {
  const qs = new URLSearchParams();
  qs.append("environment", state.environment);
  if (state.from !== null && state.to !== null) {
    qs.append("from", state.from);
    qs.append("to", state.to);
  }
  return "/api/timeline?" + qs.toString();
}

function runsUrl(row) {
  return "/api/scripts/" + encodeURIComponent(state.environment)
    + "/" + encodeURIComponent(row.script)
    + "/runs?" + new URLSearchParams(
      { from: row.started, to: row.ended }).toString();
}

/** Keep the address bar shareable: environment always, window when
 * explicitly chosen. The default (newest block) is deliberately NOT
 * written — a link saved today should show the newest run tomorrow. */
function syncUrl() {
  const url = new URL(window.location.href);
  url.search = "";
  if (state.environment) {
    url.searchParams.set("environment", state.environment);
  }
  if (state.from !== null && state.to !== null) {
    url.searchParams.set("from", state.from);
    url.searchParams.set("to", state.to);
  }
  window.history.replaceState(null, "", url.toString());
}

/** "2026-07-25T02:14:07.123456" -> "02:14" (display only; date is in
 * the picker label beside it). */
function clockTime(iso) {
  return typeof iso === "string" && iso.length >= 16
    ? iso.slice(11, 16) : "—";
}

/** Milliseconds since epoch for bar geometry. The string is UTC with no
 * suffix; appending Z parses it as such. Display never uses this. */
function ms(iso) {
  return Date.parse(iso.slice(0, 23) + "Z");
}

async function load() {
  const seq = ++state.seq;
  clearError();
  try {
    const data = await fetchJson(timelineUrl());
    if (seq !== state.seq) {
      return;    // a later selection overtook this one
    }
    state.blocks = data.blocks;
    state.rows = data.rows;
    render(data);
  } catch (err) {
    if (seq === state.seq) {
      showError(err.message);
    }
  }
}

function blockLabel(block) {
  return formatTime(block.started).slice(0, 16) + " · "
    + block.runs.toLocaleString() + " runs"
    + (block.covered ? "" : " · partial");
}

function renderBlockPicker() {
  const select = document.getElementById("timeline-block");
  clearNode(select);
  state.blocks.forEach((block, index) => {
    const option = el("option", "", blockLabel(block));
    option.value = String(index);
    select.appendChild(option);
  });
  if (!state.blocks.length) {
    const option = el("option", "", "no recent activity");
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const chosen = state.blocks.findIndex(
    (block) => block.started === state.from && block.ended === state.to);
  select.value = String(chosen === -1 ? 0 : chosen);
}

function render(data) {
  renderBlockPicker();

  const rowsHost = document.getElementById("timeline-rows");
  const axis = document.getElementById("timeline-axis");
  const empty = document.getElementById("timeline-empty");
  const failureNav = document.getElementById("failure-nav");
  clearNode(rowsHost);
  clearNode(axis);

  const meta = document.getElementById("timeline-meta");
  if (!state.rows.length) {
    meta.textContent = "";
    empty.textContent = data.window === null
      ? "No activity in the last " + data.days
        + " days for this environment."
      : "Nothing ran in this window.";
    empty.hidden = false;
    failureNav.hidden = true;
    return;
  }
  empty.hidden = true;

  const totalRuns = state.rows.reduce((sum, row) => sum + row.total, 0);
  const first = state.rows[0].started;
  const last = state.rows.reduce(
    (max, row) => (row.ended > max ? row.ended : max),
    state.rows[0].ended);
  meta.textContent = state.rows.length.toLocaleString()
    + (state.rows.length === 1 ? " script run · " : " script runs · ")
    + totalRuns.toLocaleString() + " tests · "
    + formatTime(first) + " → " + formatTime(last);

  /* The axis domain is the rows' real span, padded to whole hours so
   * the tick labels land on readable times. */
  const domainFrom = ms(first.slice(0, 13) + ":00:00.000");
  const lastMs = ms(last);
  const domainTo = lastMs + (3600000 - ((lastMs - domainFrom) % 3600000));
  const span = Math.max(domainTo - domainFrom, 1);

  /* Hour ticks, thinned so labels never crowd: aim for at most 12. */
  const hours = Math.round(span / 3600000);
  const step = Math.max(1, Math.ceil(hours / 12));
  for (let hour = 0; hour <= hours; hour += step) {
    const at = domainFrom + hour * 3600000;
    const tick = el("span", "tl-tick",
      clockTime(new Date(at).toISOString()));
    tick.style.left = ((at - domainFrom) / span * 100) + "%";
    axis.appendChild(tick);
  }

  const dayCount = new Set(
    state.rows.map((row) => row.started.slice(0, 10))).size;

  state.rows.forEach((row) => {
    rowsHost.appendChild(buildRow(row, domainFrom, span, dayCount > 1));
  });

  wireFailureNav(rowsHost);
}

/** Step between the rows that contain failures.
 *
 * A shortcut for the common read, and deliberately no more than that:
 * in this system a script whose tests all PASS can still be the one
 * that dirtied shared data, so the culprit is not guaranteed to be red.
 * These buttons save the scrolling; the running order stays the
 * evidence.
 */
function wireFailureNav(rowsHost) {
  const nav = document.getElementById("failure-nav");
  const prev = document.getElementById("prev-failure");
  const next = document.getElementById("next-failure");
  const pos = document.getElementById("failure-pos");

  const failing = [];
  state.rows.forEach((row, index) => {
    if (row.failed > 0) {
      failing.push(index);
    }
  });
  nav.hidden = failing.length === 0;
  if (!failing.length) {
    return;
  }

  /* Position within `failing`, -1 before the first jump. Local to this
   * render on purpose: new rows mean a new hunt. */
  let cursor = -1;

  const jumpTo = (position) => {
    if (cursor >= 0) {
      rowsHost.children[failing[cursor]].children[0]
        .classList.remove("tl-current");
    }
    cursor = position;
    const target = rowsHost.children[failing[cursor]];
    target.children[0].classList.add("tl-current");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    const toggle = target.querySelector(".tl-expand");
    if (toggle && toggle.getAttribute("aria-expanded") === "false") {
      toggle.click();
    }
    pos.textContent = (cursor + 1) + " of " + failing.length;
    prev.disabled = cursor <= 0;
    next.disabled = cursor >= failing.length - 1;
  };

  pos.textContent = failing.length === 1
    ? "1 row" : failing.length + " rows";
  prev.disabled = true;
  next.disabled = false;
  next.onclick = () => {
    if (cursor < failing.length - 1) {
      jumpTo(cursor + 1);
    }
  };
  prev.onclick = () => {
    if (cursor > 0) {
      jumpTo(cursor - 1);
    }
  };
}

function countText(row) {
  if (row.known_tests > row.total) {
    return row.total.toLocaleString() + " of "
      + row.known_tests.toLocaleString() + " known tests";
  }
  return row.total.toLocaleString()
    + (row.total === 1 ? " test" : " tests");
}

function buildRow(row, domainFrom, span, showDate) {
  const wrap = el("div", "tl-row-wrap");
  const line = el("div", "tl-row");

  const toggle = el("button", "tl-expand", "▸");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.title = "Show this run's tests, in running order";
  line.appendChild(toggle);

  /* The whole row is a click target — a 22px triangle is a test of
   * aim, not a control. The button stays: it is the keyboard-reachable,
   * screen-reader-announced control, and the row click just drives it.
   * Clicks on the script link (and on the button itself, which would
   * otherwise arrive twice via bubbling) are left alone. */
  line.classList.add("tl-clickable");
  line.addEventListener("click", (event) => {
    const interactive = event.target.closest
      ? event.target.closest("a, .tl-expand") : null;
    if (interactive) {
      return;
    }
    toggle.click();
  });

  /* On a window spanning more than one calendar day a bare clock time
   * is ambiguous, so the date comes back. */
  line.appendChild(el("span", "tl-time",
    showDate ? formatTime(row.started).slice(5, 16)
      : clockTime(row.started)));

  const params = new URLSearchParams();
  params.append("environment", state.environment);
  params.append("script", row.script);
  const link = el("a", "tl-script", row.script);
  link.href = "script.html?" + params.toString();
  link.title = "Execution history for this suite";
  line.appendChild(link);

  const track = el("div", "tl-track");
  const bar = el("div", "tl-bar" + (row.failed > 0 ? " has-fail" : ""));
  const left = (ms(row.started) - domainFrom) / span * 100;
  const width = (ms(row.ended) - ms(row.started)) / span * 100;
  bar.style.left = left + "%";
  bar.style.width = Math.max(width, 0.5) + "%";
  track.appendChild(bar);
  track.title = formatTime(row.started) + " → " + formatTime(row.ended);
  line.appendChild(track);

  const facts = el("span", "tl-facts");
  const count = el("span",
    "tl-count" + (row.known_tests > row.total ? " tl-partial" : ""),
    countText(row));
  if (row.known_tests > row.total) {
    count.title = "This run covered " + row.total + " of the "
      + row.known_tests + " tests this script has reported before — "
      + "a partial run, or it was cut short.";
  }
  facts.appendChild(count);
  if (row.failed > 0) {
    facts.appendChild(el("span", "chip result-fail",
      row.failed + " FAIL"));
  }
  if (row.results.UNEXPECTED_PASS > 0) {
    facts.appendChild(el("span", "chip result-unexpected-pass",
      row.results.UNEXPECTED_PASS + " UP"));
  }
  facts.appendChild(el("span", "tl-duration",
    formatDuration(row.duration_seconds)));
  line.appendChild(facts);

  wrap.appendChild(line);

  const detail = el("div", "tl-detail");
  detail.hidden = true;
  wrap.appendChild(detail);

  let loaded = false;
  toggle.addEventListener("click", async () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", open ? "false" : "true");
    toggle.textContent = open ? "▸" : "▾";
    detail.hidden = open;
    if (loaded || open) {
      return;
    }
    loaded = true;    // one fetch per row, however often it toggles
    detail.appendChild(el("p", "muted", "Loading…"));
    try {
      const data = await fetchJson(runsUrl(row));
      clearNode(detail);
      renderDetail(detail, data);
    } catch (err) {
      loaded = false;  // let a retry re-fetch after a failure
      clearNode(detail);
      detail.appendChild(el("p", "muted", "Could not load this run: "
        + err.message));
    }
  });

  return wrap;
}

function renderDetail(detail, data) {
  if (data.truncated) {
    detail.appendChild(el("p", "muted cap-note",
      "Showing the first " + data.runs.length.toLocaleString()
      + " runs of this window — it holds more."));
  }
  const wrapper = el("div", "table-wrap");
  const table = el("table", "data-table small");
  const head = el("thead");
  const headRow = el("tr");
  ["", "Started", "Test", "Result", "Duration"].forEach((label, index) => {
    headRow.appendChild(el("th", index === 4 ? "num" : "", label));
  });
  head.appendChild(headRow);
  table.appendChild(head);

  const body = el("tbody");
  let firstFailMarked = false;
  data.runs.forEach((run) => {
    const tr = el("tr", "tl-run-row");

    const runToggle = el("button", "tl-expand", "▸");
    runToggle.type = "button";
    runToggle.setAttribute("aria-expanded", "false");
    runToggle.title = "Show this run's captured output";
    const toggleCell = el("td", "tl-run-toggle");
    toggleCell.appendChild(runToggle);
    tr.appendChild(toggleCell);

    tr.appendChild(el("td", "", formatTime(run.start_time).slice(11)));

    const cell = el("td", "wrap");
    const params = new URLSearchParams();
    params.append("environment", data.environment);
    params.append("script", data.script);
    params.append("test_name", run.test_name);
    const link = el("a", "", run.test_name);
    link.href = "test.html?" + params.toString();
    cell.appendChild(link);
    tr.appendChild(cell);

    const resultCell = el("td");
    resultCell.appendChild(resultChip(run.result));
    if (run.result === "FAIL" && !firstFailMarked) {
      firstFailMarked = true;
      resultCell.appendChild(el("span", "row-sub",
        "first failure in this run"));
    }
    tr.appendChild(resultCell);

    tr.appendChild(el("td", "num",
      formatDuration(run.duration_seconds)));
    body.appendChild(tr);

    /* The output row is built now and hidden, so opening it is a flip
     * plus (once) a fetch — no row insertion, no reflow surprises. The
     * output itself is fetched on first open only: it is the one big
     * payload in the system, and this table can hold a hundred rows. */
    const outputRow = el("tr", "tl-output-row");
    outputRow.hidden = true;
    const outputCell = el("td", "");
    outputCell.setAttribute("colspan", "5");
    const pre = el("pre", "tl-output");
    outputCell.appendChild(pre);
    outputRow.appendChild(outputCell);
    body.appendChild(outputRow);

    let outputLoaded = false;
    runToggle.addEventListener("click", async () => {
      const open = runToggle.getAttribute("aria-expanded") === "true";
      runToggle.setAttribute("aria-expanded", open ? "false" : "true");
      runToggle.textContent = open ? "▸" : "▾";
      outputRow.hidden = open;
      if (outputLoaded || open) {
        return;
      }
      outputLoaded = true;   // one fetch per run, however often it toggles
      pre.textContent = "Loading output…";
      try {
        const full = await fetchJson(runApiPath(run.run_id));
        const truncated = fillOutput(pre, full.output);
        if (truncated) {
          outputCell.appendChild(el("p", "muted cap-note",
            truncated + " Open the test page for all of it."));
        }
      } catch (err) {
        outputLoaded = false;  // let a retry re-fetch after a failure
        pre.textContent = "Could not load the output: " + err.message;
      }
    });

    /* Same convenience as the script rows: the row is the target, the
     * button is the control. The test-name link keeps its own job. */
    tr.addEventListener("click", (event) => {
      const interactive = event.target.closest
        ? event.target.closest("a, .tl-expand") : null;
      if (interactive) {
        return;
      }
      runToggle.click();
    });
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  detail.appendChild(wrapper);
}

async function loadEnvironments() {
  const data = await fetchJson("/api/environments");
  const items = data.environments;
  const select = document.getElementById("timeline-environment");
  clearNode(select);
  items.forEach((item) => {
    const option = el("option", "", item.environment);
    option.value = item.environment;
    select.appendChild(option);
  });
  if (!items.length) {
    return null;
  }
  if (state.environment && items.some(
      (item) => item.environment === state.environment)) {
    select.value = state.environment;
    return state.environment;
  }
  /* Default to the environment that reported most recently: the one
   * whose night somebody is most likely digging through. */
  let best = items[0];
  for (const item of items) {
    const a = item.latest_pass ? item.latest_pass.ended : "";
    const b = best.latest_pass ? best.latest_pass.ended : "";
    if (a > b) {
      best = item;
    }
  }
  select.value = best.environment;
  return best.environment;
}

async function init() {
  renderUserWidget(document.getElementById("user-widget"), null);

  const params = new URL(window.location.href).searchParams;
  state.environment = params.get("environment");
  if (params.get("from") && params.get("to")) {
    state.from = params.get("from");
    state.to = params.get("to");
  }

  document.getElementById("timeline-environment")
    .addEventListener("change", (event) => {
      state.environment = event.target.value;
      state.from = null;    // a window belongs to one environment
      state.to = null;
      syncUrl();
      load();
    });

  document.getElementById("timeline-block")
    .addEventListener("change", (event) => {
      const block = state.blocks[Number(event.target.value)];
      if (!block) {
        return;
      }
      const newest = state.blocks[0];
      const isNewest = block.started === newest.started
        && block.ended === newest.ended;
      state.from = isNewest ? null : block.started;
      state.to = isNewest ? null : block.ended;
      syncUrl();
      load();
    });

  try {
    const chosen = await loadEnvironments();
    if (chosen === null) {
      document.getElementById("timeline-empty").textContent =
        "No environments have reported any runs yet.";
      document.getElementById("timeline-empty").hidden = false;
      return;
    }
    state.environment = chosen;
  } catch (err) {
    showError(err.message);
    return;
  }
  syncUrl();
  load();
}

init();
