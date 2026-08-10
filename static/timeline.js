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
import { getSelectedProduct } from "./products.js";
import { renderBranchBand, renderStreamEnvironmentHint } from "./compare.js";
import { apiUrl, pageUrl } from "./urls.js";

/* How far back "Earlier runs…" reaches: the server's own cap, which
 * matches retention — so it means "any recorded run", not a teaser. */
const LONG_LOOKBACK_DAYS = 365;

const state = {
  environment: null,
  blocks: [],
  /* The selected window, as the SERVER'S OWN strings — block edges are
   * echoed back verbatim, so a picker choice cannot drift through a
   * client-side date round-trip. null means "newest block". */
  from: null,
  to: null,
  /* null = the server's default fortnight; LONG_LOOKBACK_DAYS once
   * somebody asks for earlier runs. */
  days: null,
  rows: [],
  seq: 0,
  // F7 (docs/STREAMS_PLAN.md §5.2 "as built"): a long-running branch's
  // OWN running order, read the same way mainline's is -- absent means
  // mainline, zero visible change. Fixed at load, same as the rest of
  // this page's scope (there is no in-page stream switcher here).
  streamId: null,
};

function timelineUrl() {
  const windowSet = state.from !== null && state.to !== null;
  return apiUrl("/api/timeline", {
    environment: state.environment,
    days: state.days,
    from: windowSet ? state.from : null,
    to: windowSet ? state.to : null,
  }, { stream: state.streamId, product: null, baseline: null });
}

/**
 * A link to this SAME page scoped to a DIFFERENT environment (WP-25,
 * docs/ONE_KIND_PLAN.md §2b.1) -- the stream-environment-hint's link
 * target. Deliberately drops `days`/`from`/`to`: a window chosen for the
 * environment being LEFT has no meaning for the new one, so the link
 * lands on its own newest block, the page's ordinary default.
 * `stream`/`product` carry through unchanged, product so the switcher
 * on the next load still offers the right environment list
 * (adoptProductFromUrl(), products.js) -- the same scope-self-
 * sufficient rule every other stream link in this app follows.
 */
function environmentSwitchUrl(environment) {
  return pageUrl("timeline", { environment: environment }, {
    stream: state.streamId, product: getSelectedProduct() || null,
    baseline: null,
  });
}

function runsUrl(row) {
  // environment is embedded in the PATH below, not the query string --
  // explicitly nulled so it is never also carried into the query by
  // apiUrl()'s default scope carriage (this page's own URL carries
  // `environment=` too, as the page's own identity).
  return apiUrl(
    "/api/scripts/" + encodeURIComponent(state.environment)
      + "/" + encodeURIComponent(row.script) + "/runs",
    { from: row.started, to: row.ended },
    { stream: state.streamId, product: null, baseline: null,
      environment: null });
}

/** Keep the address bar shareable: environment always, window when
 * explicitly chosen. The default (newest block) is deliberately NOT
 * written — a link saved today should show the newest run tomorrow. */
function syncUrl() {
  const windowSet = state.from !== null && state.to !== null;
  // pageUrl() rebuilds the whole query string from `state`, the same
  // shape timelineUrl() already sends the server -- so the address bar
  // matches the fetch it caused. A relative "timeline.html?..." resolves
  // against the current document exactly like the pathname it replaces.
  window.history.replaceState(null, "", pageUrl("timeline", {
    environment: state.environment,
    days: state.days,
    from: windowSet ? state.from : null,
    to: windowSet ? state.to : null,
  }, { stream: state.streamId, product: null, baseline: null }));
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
  // Every recorded run is reachable, not just the recent fortnight —
  // but the long view is fetched only when somebody asks for it.
  if (state.days !== LONG_LOOKBACK_DAYS) {
    const more = el("option", "", "Earlier runs… (up to a year)");
    more.value = "__earlier__";
    select.appendChild(more);
  }
  if (!state.blocks.length && state.days === LONG_LOOKBACK_DAYS) {
    const option = el("option", "", "no recorded activity");
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const chosen = state.blocks.findIndex(
    (block) => block.started === state.from && block.ended === state.to);
  select.value = state.blocks.length
    ? String(chosen === -1 ? 0 : chosen) : "__earlier__";
}

function render(data) {
  // F7: only when this page was actually asked to scope to a stream
  // AND the server named one back -- a mainline load (streamId ===
  // null) never touches renderBranchBand at all, same guard test.js's
  // and time.js's own call sites use.
  if (state.streamId !== null && data.stream_identity) {
    renderBranchBand(data.stream_identity);
  }
  renderBlockPicker();

  const rowsHost = document.getElementById("timeline-rows");
  const axis = document.getElementById("timeline-axis");
  const empty = document.getElementById("timeline-empty");
  const failureNav = document.getElementById("failure-nav");
  clearNode(rowsHost);
  clearNode(axis);
  markCurrent(null);       // the marked row is being rebuilt

  const meta = document.getElementById("timeline-meta");
  if (!state.rows.length) {
    meta.textContent = "";
    if (data.stream_environments) {
      // WP-25 (docs/ONE_KIND_PLAN.md §2b.1): scoped to a stream, and
      // THIS environment is empty for it -- say where the stream's data
      // actually is, rather than a bare "nothing ran" that reads as a
      // data problem when the data is simply on another environment.
      renderStreamEnvironmentHint(
        empty, data.stream_environments, environmentSwitchUrl);
    } else {
      empty.textContent = data.window === null
        ? "No activity in the last " + data.days
          + " days for this environment."
        : "Nothing ran in this window.";
    }
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

  rowControllers = state.rows.map((row) => {
    const controller = buildRow(row, domainFrom, span, dayCount > 1);
    rowsHost.appendChild(controller.wrap);
    return controller;
  });

  wireFailureNav();
  consumePendingLocate(data);
}

/* A deep link ("View in timeline" on the triage panels) names a test
 * and the start time of the run being looked at. Consumed after a
 * render: first switch to the block CONTAINING that time — the run the
 * panel showed, not whatever ran most recently — then place the test. */
let pendingLocate = null;

function consumePendingLocate(data) {
  if (!pendingLocate) {
    return;
  }
  const pending = pendingLocate;
  if (pending.at) {
    // A block "ending" at 04:00 ran tests until 04:59 — block edges
    // are hour starts, so the final hour matches by prefix.
    const containing = state.blocks.find((block) =>
      block.started <= pending.at
      && (pending.at <= block.ended
          || pending.at.slice(0, 13) === block.ended.slice(0, 13)));
    if (!containing && state.days !== LONG_LOOKBACK_DAYS
        && state.blocks.length
        && pending.at < state.blocks[state.blocks.length - 1].started) {
      // The run predates the default fortnight (a history-row link can
      // point anywhere in retention): widen once and look again.
      state.days = LONG_LOOKBACK_DAYS;
      syncUrl();
      load();          // still pending; next render decides
      return;
    }
    const shownFrom = data.window === null ? null : data.window.from;
    if (containing && containing.started !== shownFrom) {
      state.from = containing.started;
      state.to = containing.ended;
      syncUrl();
      load();          // still pending; next render lands the test
      return;
    }
  }
  pendingLocate = null;
  const box = document.getElementById("test-search");
  box.value = pending.test_name;
  placeTest([pending.script], pending.test_name, "");
}

/* One controller per rendered script row, rebuilt with them: the
 * failure stepper and the test search drive rows open through these
 * rather than clicking DOM they would have to go looking for. */
let rowControllers = [];

/* The row the failure stepper or a search last landed on. One marker,
 * shared, so the two ways of moving can never leave two highlights. */
let currentMark = null;

/* Set per render by wireFailureNav; lets a search or deep-link landing
 * re-base the stepper's position. null when there are no failures. */
let failureNavSync = null;

/* Also set per render: g / G park the cursor before the first failure
 * or after the last, so the next n / p sweeps from that edge. */
let failureNavReset = null;

function markCurrent(tr) {
  if (currentMark) {
    currentMark.classList.remove("tl-current");
  }
  currentMark = tr;
  if (tr) {
    tr.classList.add("tl-current");
  }
}

/** Step between FAILING TESTS, across the whole window, in run order.
 *
 * Each jump opens the script row it lands in, opens the test's
 * captured output, and scrolls there — the archaeology loop (read a
 * failure, hop to the next) without any trip back up the page; the
 * nav is a fixed pill at the corner of the viewport, and `n`/`p` do
 * the same from the keyboard.
 *
 * A shortcut, deliberately no more: a script whose tests all PASS can
 * still be the one that dirtied shared data, so the culprit is not
 * guaranteed to be red. The buttons save the scrolling; the running
 * order stays the evidence.
 *
 * The total comes from the rows' own failure counts, so it is known
 * before any expansion is fetched; which TEST is the k-th failure is
 * only knowable from a row's runs, so a jump may await that fetch.
 */
function wireFailureNav() {
  const nav = document.getElementById("failure-nav");
  const prev = document.getElementById("prev-failure");
  const next = document.getElementById("next-failure");
  const pos = document.getElementById("failure-pos");

  /* prefix[r] = failing tests before row r; total = the lot. */
  const prefix = [];
  let total = 0;
  state.rows.forEach((row) => {
    prefix.push(total);
    total += row.failed;
  });
  nav.hidden = total === 0;
  failureNavSync = null;
  failureNavReset = null;
  if (!total) {
    return;
  }

  /* Position in the flat failing-test sequence, -1 before the first
   * jump; local to this render on purpose — new rows, new hunt. */
  let cursor = -1;
  let busy = false;

  const jumpTo = async (position) => {
    if (busy) {
      return;      // a jump is already fetching; keep clicks sane
    }
    busy = true;
    try {
      // The last row whose prefix does not pass `position` — ties
      // resolve to the later row, which is the one with the failures.
      let index = state.rows.length - 1;
      while (index > 0 && prefix[index] > position) {
        index -= 1;
      }
      const nth = position - prefix[index];
      const tests = await rowControllers[index].openTests();
      const fails = tests.filter((test) => test.result === "FAIL");
      if (!fails.length) {
        return;    // row data drifted under us; leave the cursor be
      }
      const target = fails[Math.min(nth, fails.length - 1)];
      markCurrent(target.tr);
      await target.showOutput();
      target.tr.scrollIntoView({ behavior: "smooth", block: "center" });
      cursor = position;
      pos.textContent = (cursor + 1) + " of " + total;
      prev.disabled = cursor <= 0;
      next.disabled = cursor >= total - 1;
    } finally {
      busy = false;
    }
  };

  pos.textContent = total === 1
    ? "1 failing test" : total + " failing tests";
  prev.disabled = true;
  next.disabled = false;

  /* The cursor can sit BETWEEN failures: landing on a non-failing test
   * via search or a deep link sets it to k - 0.5 (k failures lie
   * before that test), so Next means "first failure after where I am"
   * and Prev "last one before" — the stepper continues from wherever
   * the hunt actually is, never from the top. */
  next.onclick = () => {
    const target = Math.floor(cursor) + 1;
    if (target <= total - 1) {
      jumpTo(target);
    }
  };
  prev.onclick = () => {
    const target = Math.ceil(cursor) - 1;
    if (target >= 0) {
      jumpTo(target);
    }
  };

  /* g / G: park the cursor at an edge so the sweep restarts there.
   * The counter goes back to the total form — no position is claimed
   * that the stepper is not actually on. */
  failureNavReset = (edge) => {
    cursor = edge === "top" ? -1 : total - 0.5;
    pos.textContent = total === 1
      ? "1 failing test" : total + " failing tests";
    prev.disabled = Math.ceil(cursor) - 1 < 0;
    next.disabled = Math.floor(cursor) + 1 > total - 1;
  };

  /* How a search or deep-link landing re-bases the stepper. Args: the
   * landed row's index, how many of its failures lie at-or-before the
   * landed test, and whether the landed test IS one of them. */
  failureNavSync = (rowIndex, failsThroughHit, landedOnFailure) => {
    const through = prefix[rowIndex] + failsThroughHit;
    cursor = landedOnFailure ? through - 1 : through - 0.5;
    pos.textContent = landedOnFailure
      ? through + " of " + total
      : (total === 1 ? "1 failing test" : total + " failing tests");
    prev.disabled = Math.ceil(cursor) - 1 < 0;
    next.disabled = Math.floor(cursor) + 1 > total - 1;
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

  // Script-page parity (FINAL ROUND): a stream-scoped Timeline's block
  // row must land on that SAME stream's suite history, not mainline's
  // -- script.html now honours stream= (previously a dead param).
  // pageUrl()'s default scope carriage is a no-op on mainline.
  const link = el("a", "tl-script", row.script);
  link.href = pageUrl("script", {
    environment: state.environment, script: row.script,
  }, { stream: state.streamId, product: null, baseline: null });
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

  /* One fetch per row however often it toggles — and the same promise
   * whether the opener was a click, the failure stepper or a search
   * jump, so none of them can double-load or race each other. */
  let detailPromise = null;
  const loadDetail = () => {
    if (detailPromise === null) {
      detailPromise = (async () => {
        detail.appendChild(el("p", "muted", "Loading…"));
        try {
          const data = await fetchJson(runsUrl(row));
          clearNode(detail);
          return renderDetail(detail, data);
        } catch (err) {
          detailPromise = null;   // let a retry re-fetch after a failure
          clearNode(detail);
          detail.appendChild(el("p", "muted", "Could not load this run: "
            + err.message));
          return [];
        }
      })();
    }
    return detailPromise;
  };

  const setOpen = (open) => {
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.textContent = open ? "▾" : "▸";
    detail.hidden = !open;
  };

  toggle.addEventListener("click", () => {
    const wasOpen = toggle.getAttribute("aria-expanded") === "true";
    setOpen(!wasOpen);
    if (!wasOpen) {
      loadDetail();
    }
  });

  return {
    wrap: wrap,
    row: row,
    /** Open the row and resolve to its test controllers. */
    openTests: () => {
      setOpen(true);
      return loadDetail();
    },
  };
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
  const controllers = [];
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
    // Scope-carriage (F1-F7 sweep follow-up): a run row expanded on a
    // branch's Timeline must link to that SAME stream's test page --
    // the same state.streamId this file's other outbound requests
    // already carry (runsUrl() above). pageUrl()'s default scope
    // carriage is a no-op on mainline.
    const link = el("a", "", run.test_name);
    link.href = pageUrl("test", {
      environment: data.environment, script: data.script,
      test_name: run.test_name,
    }, { stream: state.streamId, product: null, baseline: null });
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

    /* One fetch per run, shared between the click path and the
     * stepper's jump — same shape as the row detail above. */
    let outputPromise = null;
    const loadOutput = () => {
      if (outputPromise === null) {
        outputPromise = (async () => {
          pre.textContent = "Loading output…";
          try {
            const full = await fetchJson(runApiPath(run.run_id));
            const truncated = fillOutput(pre, full.output);
            if (truncated) {
              outputCell.appendChild(el("p", "muted cap-note",
                truncated + " Open the test page for all of it."));
            }
          } catch (err) {
            outputPromise = null;  // let a retry re-fetch after a failure
            pre.textContent = "Could not load the output: " + err.message;
          }
        })();
      }
      return outputPromise;
    };

    const setOutputOpen = (open) => {
      runToggle.setAttribute("aria-expanded", open ? "true" : "false");
      runToggle.textContent = open ? "▾" : "▸";
      outputRow.hidden = !open;
    };

    runToggle.addEventListener("click", () => {
      const wasOpen = runToggle.getAttribute("aria-expanded") === "true";
      setOutputOpen(!wasOpen);
      if (!wasOpen) {
        loadOutput();
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

    controllers.push({
      test_name: run.test_name,
      result: run.result,
      tr: tr,
      showOutput: () => {
        setOutputOpen(true);
        return loadOutput();
      },
    });
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  detail.appendChild(wrapper);
  return controllers;
}

/* ---------------- find a test in the window ----------------
 *
 * The victim-first entry to the whole workflow: "THIS test inherited
 * bad data — what ran before it?". Type its name, land on its row in
 * the running order; everything above it is the suspect list.
 *
 * Names come from the dashboard's existing server-side search over
 * latest_runs (one paged query, nothing new to maintain); placing the
 * hit uses the rows already on this page.
 */

let searchTimer = null;
let searchSeq = 0;
let searchMatches = [];   // last suggestions: [{script, test_name}]

/* Vim-style inline completion in the search box: Ctrl-j / Ctrl-k cycle
 * the field's VALUE through the current suggestions, the way insert-
 * mode complete replaces text. (Ctrl-n/Ctrl-p would be truer to vim,
 * but browsers reserve Ctrl-N at a level preventDefault cannot reach.)
 * Typing resets the cycle; programmatic value changes do not re-fetch
 * suggestions, so the stem's list survives the walk through it. */
let suggestionNames = [];
let completionIndex = -1;

async function fetchSearchMatches(query) {
  const data = await fetchJson(apiUrl("/api/dashboard", {
    environment: state.environment, q: query, limit: "20",
  }, { stream: state.streamId, product: null, baseline: null }));
  return data.tests.map((test) => ({
    script: test.script, test_name: test.test_name,
  }));
}

function refreshSuggestions(query) {
  if (searchTimer !== null) {
    clearTimeout(searchTimer);
  }
  if (query.length < 2) {
    return;
  }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const matches = await fetchSearchMatches(query);
      if (seq !== searchSeq) {
        return;
      }
      searchMatches = matches;
      const list = document.getElementById("test-search-list");
      clearNode(list);
      const seen = [];
      for (const match of matches) {
        if (seen.indexOf(match.test_name) !== -1) {
          continue;
        }
        seen.push(match.test_name);
        const option = el("option", "");
        option.value = match.test_name;
        option.setAttribute("label", match.script);
        list.appendChild(option);
      }
      suggestionNames = seen;
      completionIndex = -1;
    } catch (err) {
      /* Suggestions are decoration; the Enter path reports errors. */
    }
  }, 250);
}

async function locateTest(name) {
  const note = document.getElementById("search-note");
  note.textContent = "";
  let candidates = searchMatches.filter(
    (match) => match.test_name === name);
  if (!candidates.length) {
    try {
      searchMatches = await fetchSearchMatches(name);
    } catch (err) {
      showError(err.message);
      return;
    }
    candidates = searchMatches.filter(
      (match) => match.test_name === name);
    if (!candidates.length && searchMatches.length) {
      candidates = [searchMatches[0]];    // nearest match, said below
    }
  }
  if (!candidates.length) {
    note.textContent = "No test matching “" + name
      + "” in this environment.";
    return;
  }
  const targetName = candidates[0].test_name;
  await placeTest(
    candidates.map((match) => match.script), targetName,
    targetName === name ? "" : "Nearest match: " + targetName + ". ");
}

/** Open the row holding *targetName* (first of *scripts* that has it),
 * open that test's OUTPUT, mark it, scroll there, and re-base the
 * failure stepper on the landing spot. The shared tail of the search
 * box and the "View in timeline" deep link from the triage panels. */
async function placeTest(scripts, targetName, prefixNote) {
  const note = document.getElementById("search-note");
  let sawScript = false;
  for (let index = 0; index < rowControllers.length; index++) {
    const controller = rowControllers[index];
    if (scripts.indexOf(controller.row.script) === -1) {
      continue;
    }
    sawScript = true;
    const tests = await controller.openTests();
    const at = tests.findIndex((test) => test.test_name === targetName);
    if (at === -1) {
      continue;
    }
    const hit = tests[at];
    markCurrent(hit.tr);
    hit.showOutput();
    hit.tr.scrollIntoView({ behavior: "smooth", block: "center" });
    if (failureNavSync) {
      const failsThroughHit = tests.slice(0, at + 1).filter(
        (test) => test.result === "FAIL").length;
      failureNavSync(index, failsThroughHit, hit.result === "FAIL");
    }
    note.textContent = (prefixNote || "")
      + "Everything above its row ran before it.";
    return;
  }
  note.textContent = sawScript
    ? targetName + " did not run in the selected run of "
      + scripts.join(", ") + " — try another run in the picker."
    : targetName + " belongs to " + scripts.join(", ")
      + ", which has no row in the selected run — try another run in "
      + "the picker.";
}

/* ---------------- hotkeys ----------------
 *
 * Vi-flavoured and strictly optional — every one of these has a mouse
 * path, and none fire while a field has focus. `?` shows the list.
 */

function handleHotkey(event) {
  const target = event.target || {};
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
    if (event.key === "Escape" && target.blur) {
      target.blur();      // vi: leave insert mode
    }
    if (target.id === "test-search" && event.ctrlKey
        && (event.key === "j" || event.key === "k")
        && suggestionNames.length) {
      const stepBy = event.key === "j" ? 1 : -1;
      completionIndex = (completionIndex + stepBy
        + suggestionNames.length) % suggestionNames.length;
      target.value = suggestionNames[completionIndex];
      if (event.preventDefault) {
        event.preventDefault();
      }
    }
    return;
  }
  if (event.ctrlKey || event.altKey || event.metaKey) {
    return;
  }
  const step = (id) => {
    const nav = document.getElementById("failure-nav");
    const button = document.getElementById(id);
    if (nav && !nav.hidden && button && !button.disabled) {
      button.click();
    }
  };
  switch (event.key) {
    case "/": {
      const box = document.getElementById("test-search");
      if (box && box.focus) {
        box.focus();
        if (event.preventDefault) {
          event.preventDefault();   // don't type the slash
        }
      }
      break;
    }
    case "n":
      step("next-failure");
      break;
    case "N":
    case "p":
      step("prev-failure");
      break;
    case "g":
      if (window.scrollTo) {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      if (failureNavReset) {
        failureNavReset("top");    // next n sweeps from the first
        markCurrent(null);
      }
      break;
    case "G":
      if (window.scrollTo) {
        window.scrollTo({
          top: document.body ? document.body.scrollHeight : 0,
          behavior: "smooth",
        });
      }
      if (failureNavReset) {
        failureNavReset("bottom"); // next p sweeps from the last
        markCurrent(null);
      }
      break;
    case "?": {
      const help = document.getElementById("hotkey-help");
      if (help) {
        help.hidden = !help.hidden;
      }
      break;
    }
  }
}

async function loadEnvironments() {
  const data = await fetchJson("/api/environments");
  // WP-20: the picker itself is the "existing environment filter
  // semantics" product= is defined to work through here (this page is
  // inherently single-environment, so there is no separate product=
  // request param to add — see /api/timeline's docstring). A selection
  // that would leave nothing gets ignored rather than emptying the
  // picker on a stale choice.
  const product = getSelectedProduct();
  const scoped = product
    ? data.environments.filter((item) => item.product === product)
    : data.environments;
  const items = scoped.length ? scoped : data.environments;
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
  if (params.get("days")) {
    state.days = Number(params.get("days")) || null;
  }
  const rawStream = params.get("stream");
  state.streamId = rawStream ? parseInt(rawStream, 10) : null;
  if (params.get("test") && params.get("script")) {
    pendingLocate = {
      script: params.get("script"),
      test_name: params.get("test"),
      at: params.get("at"),
    };
  }

  document.getElementById("timeline-environment")
    .addEventListener("change", (event) => {
      state.environment = event.target.value;
      state.from = null;    // a window belongs to one environment
      state.to = null;
      searchMatches = [];   // suggestions belonged to the old one
      syncUrl();
      load();
    });

  const searchBox = document.getElementById("test-search");
  searchBox.addEventListener("input", (event) => {
    completionIndex = -1;      // typing restarts the completion cycle
    refreshSuggestions(event.target.value.trim());
  });
  searchBox.addEventListener("change", (event) => {
    const value = event.target.value.trim();
    if (value) {
      // Enter both jumps AND leaves the box — vi's exit from insert
      // mode — so n / p step failures immediately instead of typing.
      if (event.target.blur) {
        event.target.blur();
      }
      locateTest(value);
    }
  });

  if (window.addEventListener) {
    window.addEventListener("keydown", handleHotkey);
  }

  document.getElementById("timeline-block")
    .addEventListener("change", (event) => {
      if (event.target.value === "__earlier__") {
        state.days = LONG_LOOKBACK_DAYS;   // keep the selected window
        syncUrl();
        load();
        return;
      }
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
