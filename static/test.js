/* test.js — the testboard test-detail page.
 *
 * Reads the (environment, script, test_name) triple from the query string,
 * loads /api/tests/{...} detail + analytics, users, paged history and
 * comments, and supports assigning, commenting and lazily fetching run
 * output. The day-of-week chart is hand-rolled inline SVG (no libraries).
 * All dynamic data reaches the DOM via textContent/createTextNode only.
 */

"use strict";

import {
  muteDetail,
  muteDurationSelect,
  clearError,
  clearNode,
  FLAKY_CADENCE_TITLE,
  FLAKY_OWN_MARK_TITLE,
  FLAKY_REASON_LABEL,
  FLAKY_REASON_PLACEHOLDER,
  FLAKY_UNMARK_TITLE,
  cadenceChanged,
  copyOutputButton,
  cadenceWords,
  displayResult,
  el,
  fetchJson,
  fillOutput,
  flakyCadenceSelect,
  flakyDefaultLink,
  flakyDefaultWords,
  flakyDetail,
  flakyMarkWords,
  flakySentence,
  flakySetBy,
  flakyTag,
  formatDuration,
  formatTime,
  getUsername,
  hasOwnMark,
  loadUsers,
  postJson,
  preselectUser,
  putJson,
  rememberUser,
  renderUserWidget,
  requireUsername,
  resultChip,
  runApiPath,
  showError,
  stabilityWord,
  testApiPath,
  userPickerSelect,
} from "./api.js";
import { formatNight, stackedColumnChart } from "./charts.js";
import {
  renderBranchBand,
  renderCompareStrip,
  streamLabel,
} from "./compare.js";
import {
  apiUrl,
  bulkFlakyUrl,
  bulkMutesUrl,
  flakyDefaultAdminUrl,
  unmarkFlakyUrl,
  unmuteUrl,
  pageUrl,
} from "./urls.js";

const HISTORY_LIMIT = 50;
const NEW_USER_SENTINEL = "\u0000new-user";
const SVG_NS = "http://www.w3.org/2000/svg";
const FAIL_COLOR = "#c62828";
/** What a mark does, on the header offer's "Mark as flaky" and on the
 * form's, as Mute's act button carries its own. */
const MARK_FLAKY_TITLE = "Accept that this test fails now and then: its "
  + "failures are tolerated until they reach a cadence you choose, on "
  + "mainline and every build, until the mark is removed.";

const query = new URLSearchParams(window.location.search);
const ident = {
  environment: query.get("environment"),
  script: query.get("script"),
  test_name: query.get("test_name"),
};

/**
 * `?stream=<id>` scopes the history table and analytics to one
 * stream's own runs of this triple (docs/STREAMS_PLAN.md section 3.6) --
 * null means the page is unscoped (mainline), which is every visit
 * before WP-21 and every visit that did not arrive from a
 * branch-scoped link.
 */
const rawStream = query.get("stream");
const streamId = rawStream ? parseInt(rawStream, 10) : null;

/** This page's own stream scope, normalised: `Number.isNaN` collapses
 * to null (mainline) the same way the pre-WP-24 local `withStream()`
 * helper did for a garbage `?stream=` value -- every apiUrl() call
 * below that carries this page's own scope uses this rather than the
 * raw `streamId` (which OTHER call sites in this file, e.g.
 * renderDetail's suite link, deliberately still use unnormalised --
 * see that call site's own comment). */
function streamApiScope() {
  return Number.isNaN(streamId) ? null : streamId;
}

const state = {
  currentAssignee: null,
  oldestLoadedStart: null,   // raw ISO start_time of the oldest loaded run
  streamIdentity: null,      // this page's own stream, when scoped (WP-21)
};

/** testApiPath for this page's triple, with an optional suffix. */
function myPath(suffix) {
  return testApiPath(ident.environment, ident.script, ident.test_name, suffix);
}

/* ---------------- header / detail ---------------- */

function renderDetail(detail) {
  document.title = detail.test_name + " — testboard";
  document.getElementById("test-title").textContent = detail.test_name;

  // The script is a link to the whole suite's execution history — from a
  // single test, "how did the rest of the suite do?" is one click away.
  const identity = document.getElementById("test-identity");
  clearNode(identity);
  identity.appendChild(document.createTextNode(detail.environment + " · "));
  // F3 superseded (docs/STREAMS_PLAN.md §5.2 "as built"): script.html
  // now honours `stream=` (script-page parity, FINAL ROUND), so this
  // link no longer needs the "(mainline)" honesty label F3 added when
  // it silently jumped back to mainline regardless of this page's own
  // scope -- it carries the SAME stream through instead, the scope-
  // self-sufficient pattern every other inbound link to a scoped page
  // already follows. streamId is null on mainline, so a mainline
  // visit is byte-for-byte unchanged. (Deliberately the raw `streamId`,
  // not streamApiScope()'s NaN-normalised form -- unchanged from
  // before WP-24, which never guarded this particular link either.)
  const suiteLink = document.createElement("a");
  suiteLink.href = pageUrl("script", {
    environment: detail.environment, script: detail.script,
  }, { stream: streamId, product: null, baseline: null });
  suiteLink.textContent = detail.script;
  suiteLink.title = "Execution history for this suite";
  identity.appendChild(suiteLink);

  renderSourceLink(detail.source_link);
  state.currentAssignee = detail.assignee;
  state.streamIdentity = detail.stream_identity;
  renderLatest(detail.latest, detail);
  renderMute(detail);
  renderFlaky(detail);
  renderAnalytics(detail.analytics);
}

/* ---------------- muted failures (WP-40) ---------------- */

/** This page's test, as the mute and flaky endpoints want it. */
function actTests() {
  const test = {
    environment: ident.environment, script: ident.script,
    test_name: ident.test_name,
  };
  if (streamApiScope() !== null) {
    test.stream_id = streamApiScope();
  }
  return [test];
}

/** The detail request: this page's test on this page's stream. */
function detailUrl() {
  return apiUrl(myPath(), {}, {
    stream: streamApiScope(), product: null, environment: null,
  });
}

/** Re-read the detail after an act on the test and redraw what an act
 * can change: the header's chip (a mute or a mark changes what the
 * result reads as), the mute and flaky pieces, and the assignee box
 * (muting assigns; unassigning drops the mute). One request, the one a
 * mute has always cost. */
async function refreshActs() {
  try {
    const detail = await fetchJson(detailUrl());
    state.currentAssignee = detail.assignee;
    renderLatest(detail.latest, detail);
    renderMute(detail);
    renderFlaky(detail);
    await fillAssigneeSelect();
  } catch (err) {
    showError("Could not reload the test: " + err.message);
  }
}

/** POST a mute or flaky act on this test as the signed-in user, then
 * refresh. `recorded` names what is recorded ("mutes", "flaky marks")
 * in the message shown when nobody is signed in. */
async function postAct(url, body, buttons, recorded) {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— " + recorded + " are recorded against a name.");
    return;
  }
  body.username = me;
  buttons.forEach((btn) => { btn.disabled = true; });
  clearError();
  try {
    await postJson(url, body);
    await refreshActs();
  } catch (err) {
    showError(err.message);
    buttons.forEach((btn) => { btn.disabled = false; });
  }
}

function renderMute(detail) {
  const banner = document.getElementById("mute-banner");
  clearNode(banner);
  const mute = detail.mute;
  const failing = detail.latest && detail.latest.result === "FAIL";
  if (mute && mute.live) {
    banner.appendChild(el("span", "kf-label", "Muted"));
    banner.appendChild(el("span", "kf-reason",
      muteDetail(mute, true)));
    const extendFor = muteDurationSelect("review-duration-select", 168);
    const extend = el("button", "", "Extend");
    extend.type = "button";
    const unmute = el("button", "", "Unmute");
    unmute.type = "button";
    unmute.title = "Stop muting this test; a failure counts as failing "
      + "again. The test stays assigned.";
    extend.addEventListener("click", () => {
      const hours = Number(extendFor.value);
      if (!hours) {
        extendFor.focus();
        showError("Choose how long to extend the mute for.");
        return;
      }
      postAct(
        bulkMutesUrl(),
        { hours: hours, assignee: detail.assignee || getUsername(),
          tests: actTests() },
        [extend, unmute], "mutes");
    });
    unmute.addEventListener("click", () => postAct(
      unmuteUrl(), { tests: actTests() }, [extend, unmute], "mutes"));
    banner.appendChild(extendFor);
    banner.appendChild(extend);
    banner.appendChild(unmute);
    banner.hidden = false;
  } else if (failing) {
    banner.appendChild(el("span", "kf-label", "Mute"));
    const reason = document.createElement("input");
    reason.type = "text";
    reason.className = "mute-reason-input";
    reason.placeholder = "Why is this failure muted? (required)";
    reason.setAttribute("aria-label", "Reason for muting");
    const duration = muteDurationSelect("review-duration-select");
    const owner = userPickerSelect("review-owner-select");
    owner.title = "Who owns this failure while it is muted";
    preselectUser(owner, detail.assignee || getUsername());
    const go = el("button", "", "Mute");
    go.type = "button";
    go.title = "Stop counting this failure as failing for the time "
      + "chosen (up to 7 days). It is assigned to the owner chosen, "
      + "counted as muted, and your reason is posted as a comment.";
    go.addEventListener("click", () => {
      const text = reason.value.trim();
      const hours = Number(duration.value);
      if (!text) {
        reason.focus();
        showError("Say why this failure is muted — the reason is "
          + "kept with it.");
        return;
      }
      if (!hours) {
        duration.focus();
        showError("Choose how long to mute this failure for.");
        return;
      }
      postAct(bulkMutesUrl(), {
        reason: text, hours: hours, assignee: owner.value || getUsername(),
        tests: actTests(),
      }, [go], "mutes");
    });
    banner.appendChild(reason);
    banner.appendChild(duration);
    banner.appendChild(owner);
    banner.appendChild(go);
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
  renderMuteHistory(detail.mute_history || []);
}

function renderMuteHistory(history) {
  const section = document.getElementById("mute-history-section");
  const body = document.getElementById("mute-history-body");
  clearNode(body);
  section.hidden = history.length === 0;
  for (const item of history) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", item.action));
    tr.appendChild(el("td", "", item.actor));
    tr.appendChild(el("td", "", formatTime(item.acted_at)));
    tr.appendChild(el("td", "",
      item.expires_at ? formatTime(item.expires_at) : "—"));
    tr.appendChild(el("td", "wrap", item.reason || "—"));
    body.appendChild(tr);
  }
}

/* ---------------- flaky mark (WP-44) ---------------- */

/*
 * The mark and its history ride the detail (`flaky`, `flaky_history`),
 * so nothing here fetches: the banner, the Flakiness card's line and
 * the history are drawn from what the page already has, and an act
 * refreshes through refreshActs(), the one request a mute costs. Every
 * word comes from api.js's flaky helpers. `flaky` absent or null is an
 * unmarked test (and an older server); one with `source` "default" is a
 * test under its environment's default, which the detail carries in
 * every state (flaky-default design §2 B.3), so this page is the one
 * quiet place that says the default (decision 19).
 */
function renderFlaky(detail) {
  const flaky = detail.flaky || null;
  const muteLive = Boolean(detail.mute && detail.mute.live);
  renderFlakyBanner(flaky, muteLive);
  renderFlakyCard(flaky, muteLive);
  renderFlakyHistory(detail.flaky_history || []);
}

/** The slot under the mute banner. Marked: the mark and its cadence, who
 * made it, when and why, the sentence saying where the test stands on
 * this stream, and the controls to change the cadence or remove the
 * mark. Unmarked, whatever the latest result (a mark is a standing
 * property of the test, made while it passes as well as while it
 * fails): the offer, the act's subject as its label (as Mute's offer
 * is labelled) and the button that opens the form here, drawn quieter
 * than the mark: no tint, a neutral edge, a muted label. Under the
 * environment's default (design §3 U5) the offer says it too: the
 * default and who set it, the sentence for this stream, then "Mark as
 * flaky", which gives the test a mark of its own, and "Change the
 * default", a link to the environment's row on the Admin page. No Set
 * cadence and no Unmark: there is no mark. */
function renderFlakyBanner(flaky, muteLive) {
  const banner = document.getElementById("flaky-banner");
  clearNode(banner);
  banner.classList.toggle("is-offer", !hasOwnMark(flaky));
  banner.hidden = false;
  if (!hasOwnMark(flaky)) {
    banner.appendChild(el("span", "kf-label", "Flaky"));
    if (flaky) {
      banner.appendChild(el("span", "", flakyDefaultWords(flaky.cadence)));
      banner.appendChild(el("span", "kf-reason", flakySetBy(flaky)));
      banner.appendChild(el("span", "flaky-sentence",
        flakySentence(flaky, muteLive)));
    }
    const open = el("button", "", "Mark as flaky");
    open.type = "button";
    open.title = flaky ? FLAKY_OWN_MARK_TITLE : MARK_FLAKY_TITLE;
    open.addEventListener("click", () => openFlakyForm(flaky, muteLive));
    banner.appendChild(open);
    if (flaky) {
      banner.appendChild(flakyDefaultLink(
        flakyDefaultAdminUrl(ident.environment)));
    }
    return;
  }
  banner.appendChild(el("span", "kf-label", flakyMarkWords(flaky.cadence)));
  banner.appendChild(el("span", "kf-reason", flakyDetail(flaky)));
  banner.appendChild(el("span", "flaky-sentence",
    flakySentence(flaky, muteLive)));
  const cadence = flakyCadenceSelect("review-cadence-select", flaky.cadence);
  const change = el("button", "", "Set cadence");
  change.type = "button";
  change.title = FLAKY_CADENCE_TITLE;
  const unmark = el("button", "", "Unmark");
  unmark.type = "button";
  unmark.title = FLAKY_UNMARK_TITLE;
  // Every button from the banner's state: nothing to send until the
  // cadence differs from the mark's own, and nothing at all while a
  // request is out. A success redraws the banner; anything else (a
  // failure, nobody signed in) lands back here, not on all-enabled.
  let inFlight = false;
  const derive = () => {
    change.disabled = inFlight || !cadenceChanged(cadence, flaky);
    unmark.disabled = inFlight;
  };
  const act = async (url, body) => {
    if (inFlight) {
      return;
    }
    inFlight = true;
    derive();
    await postAct(url, body, [], "flaky marks");
    inFlight = false;
    derive();
  };
  derive();
  cadence.addEventListener("change", derive);
  change.addEventListener("click", () => act(
    bulkFlakyUrl(), { cadence: Number(cadence.value), tests: actTests() }));
  unmark.addEventListener("click", () => act(
    unmarkFlakyUrl(), { tests: actTests() }));
  banner.appendChild(cadence);
  banner.appendChild(change);
  banner.appendChild(unmark);
}

/** The Flakiness card's mark: the mark in words, or, unmarked, a link
 * that opens the one form, in the header's slot, and takes the reader
 * there; the computed signal above it is what prompts some marks. Under
 * the environment's default, the default in words before the link. */
function renderFlakyCard(flaky, muteLive) {
  const box = document.getElementById("flaky-mark");
  clearNode(box);
  if (hasOwnMark(flaky)) {
    box.appendChild(el("span", "", flakyMarkWords(flaky.cadence)));
    return;
  }
  if (flaky) {
    box.appendChild(el("span", "", flakyDefaultWords(flaky.cadence)));
    box.appendChild(el("span", "muted", "·"));
  }
  const open = el("button", "linkish", "Mark as flaky");
  open.type = "button";
  open.title = "Opens the form at the top of the page";
  open.addEventListener("click", () => {
    openFlakyForm(flaky, muteLive);
    document.getElementById("flaky-banner").scrollIntoView(
      { block: "nearest" });
  });
  box.appendChild(open);
}

/** The form in the header's slot, behind the offer's button or the
 * card's link: a reason (required, and posted as a comment), a cadence,
 * and the act. The person marking owns the mark (decision 12), so there
 * is no owner to pick. Already open, it keeps what was typed and only
 * takes the focus. `flaky` and `muteLive` are what the slot drew before
 * it, null or the environment's default, which Cancel draws again. */
function openFlakyForm(flaky, muteLive) {
  const box = document.getElementById("flaky-banner");
  const drawn = box.querySelector(".flaky-reason-input");
  if (drawn) {
    drawn.focus();
    return;
  }
  clearNode(box);
  box.appendChild(el("span", "kf-label", "Flaky"));
  const reason = document.createElement("input");
  reason.type = "text";
  reason.className = "flaky-reason-input";
  reason.placeholder = FLAKY_REASON_PLACEHOLDER;
  reason.setAttribute("aria-label", FLAKY_REASON_LABEL);
  const cadence = flakyCadenceSelect("review-cadence-select");
  const mark = el("button", "", "Mark as flaky");
  mark.type = "button";
  mark.title = flaky ? FLAKY_OWN_MARK_TITLE : MARK_FLAKY_TITLE;
  const cancel = el("button", "", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => renderFlakyBanner(flaky, muteLive));
  mark.addEventListener("click", () => {
    const text = reason.value.trim();
    const chosen = Number(cadence.value);
    if (!text) {
      reason.focus();
      showError("Say why this test is flaky — the reason is kept with "
        + "the mark.");
      return;
    }
    if (!chosen) {
      cadence.focus();
      showError("Choose how many failures in a row count as failing.");
      return;
    }
    postAct(bulkFlakyUrl(), {
      reason: text, cadence: chosen, tests: actTests(),
    }, [mark, cancel], "flaky marks");
  });
  box.appendChild(reason);
  box.appendChild(cadence);
  box.appendChild(mark);
  box.appendChild(cancel);
  reason.focus();
}

/** Every act on the mark, newest first, as the mute history is drawn. */
function renderFlakyHistory(history) {
  const section = document.getElementById("flaky-history-section");
  const body = document.getElementById("flaky-history-body");
  clearNode(body);
  section.hidden = history.length === 0;
  for (const item of history) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", item.action));
    tr.appendChild(el("td", "", item.actor));
    tr.appendChild(el("td", "", formatTime(item.acted_at)));
    tr.appendChild(el("td", "",
      item.cadence ? cadenceWords(item.cadence) : "—"));
    tr.appendChild(el("td", "wrap", item.reason || "—"));
    body.appendChild(tr);
  }
}

/** Show the source link; only http(s)/file URLs become anchors. */
function renderSourceLink(link) {
  if (!link) {
    return;
  }
  const line = document.getElementById("source-line");
  line.hidden = false;
  if (/^(https?|file):/i.test(link)) {
    const a = document.getElementById("source-link");
    a.href = link;
    a.textContent = link;
    a.hidden = false;
  } else {
    const span = document.getElementById("source-text");
    span.textContent = link;
    span.hidden = false;
  }
}

/** The latest run's line. Its chip is what the result reads as now
 * (WP-44): FLAKING for a failure under its flaky mark's cadence with no
 * live mute, with the "flaky" pill for a marked test that does not read
 * FLAKING. Run history and the compare strip stay raw: a run is what
 * happened, flaking is a judgement on the test now (decision 9). */
function renderLatest(latest, detail) {
  const box = document.getElementById("latest-run");
  clearNode(box);
  if (!latest) {
    return;
  }
  const flaky = detail.flaky || null;
  const muteLive = Boolean(detail.mute && detail.mute.live);
  box.appendChild(el("span", "muted", "Latest run:"));
  box.appendChild(resultChip(
    displayResult(latest.result, flaky ? flaky.state : null, muteLive)));
  const tag = flakyTag(
    { result: latest.result, flaky: flaky, mute: detail.mute });
  if (tag) {
    box.appendChild(tag);
  }
  box.appendChild(el("span", "", formatTime(latest.start_time)
    + " (" + formatDuration(latest.duration_seconds) + ")"));

  // The same door the dashboard's review panel has always had, added
  // here by the 2026-08-09 persona walk: the delver journey reads
  // "test page → history → timeline of that night", and this page had
  // NO route to the Timeline at all — the scoped "View in timeline →"
  // link existed only on the review panel, one step earlier, so a
  // reader who followed "Open full test page" lost it. Same wording,
  // same params as review.js's link: `at` is the run's start time, so
  // the Timeline opens the run that CONTAINS this result. `stream` is
  // named explicitly (naming `product` suppresses default carriage for
  // the levels it contains — the four-site fix round's lesson).
  const inTimeline = document.createElement("a");
  inTimeline.className = "latest-run-timeline";
  inTimeline.href = pageUrl("timeline", {
    environment: ident.environment, script: ident.script,
    test: ident.test_name, at: latest.start_time,
  }, { stream: streamApiScope(), product: null, baseline: null });
  inTimeline.textContent = "View in timeline →";
  inTimeline.title = "This run in its night's running order — what ran "
    + "before it is listed above it";
  box.appendChild(inTimeline);

  // The known-failure reason is the annotation that says "we already
  // know about this" — it decides whether a failure needs looking at, so
  // it gets its own banner rather than being a grey aside or a column
  // buried in the history table.
  const banner = document.getElementById("known-failure");
  clearNode(banner);
  if (latest.known_failure_reason) {
    banner.appendChild(el("span", "kf-label", "Known failure"));
    banner.appendChild(
      el("span", "kf-reason", latest.known_failure_reason));
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

/* ---------------- analytics ---------------- */

function renderAnalytics(analytics) {
  document.getElementById("analytics-section").hidden = false;
  const win = analytics.window;
  document.getElementById("window-info").textContent =
    "Window: last " + win.max_days + " days / up to " + win.max_runs
    + " runs — " + win.run_count + " runs between "
    + formatTime(win.from) + " and " + formatTime(win.to) + " (UTC)";

  renderFailingSince(analytics.failing_since, analytics.last_pass_before_failure);
  renderFlakiness(analytics.flakiness, win.run_count);
  renderDuration(analytics.duration_seconds);
  renderDowChart(analytics.day_of_week);
  renderDowTable(analytics.day_of_week);
  renderByDay(analytics.by_day);
}

/* The four results, bottom of the stack first: passes make the base, so
   failures sit on top where the eye lands. */
const BY_DAY_SERIES = [
  { key: "PASS", label: "Passed", segClass: "seg-pass",
    swatchClass: "swatch-pass" },
  { key: "FAILED_AS_EXPECTED", label: "Expected fail",
    segClass: "seg-fae", swatchClass: "swatch-fae" },
  { key: "UNEXPECTED_PASS", label: "Unexpected pass",
    segClass: "seg-up", swatchClass: "swatch-up" },
  { key: "FAIL", label: "Failed", segClass: "seg-fail",
    swatchClass: "swatch-fail" },
];

/** This test's results, one column per day of the analytics window. */
function renderByDay(days) {
  const container = document.getElementById("by-day-chart");
  const tbody = document.getElementById("by-day-table-body");
  clearNode(tbody);
  clearNode(container);
  if (days === undefined || days === null) {
    // The field is missing entirely: this page is newer than the server
    // it is talking to. Saying "no runs" here would be a lie, and would
    // send someone looking for a data problem that does not exist.
    container.appendChild(el("p", "empty-state",
      "This page is newer than the server it is talking to — restart "
      + "the testboard server to pick up the current code."));
    return;
  }
  if (days.length === 0) {
    container.appendChild(el("p", "empty-state", "No runs in the window."));
    return;
  }
  stackedColumnChart(container, days, BY_DAY_SERIES, {
    unit: "day",
    titleFor: (day) => formatNight(day.date),
    labelFor: (day, index) => {
      const dayOfMonth = parseInt(day.date.slice(8, 10), 10);
      return index === 0 || dayOfMonth === 1
        ? formatNight(day.date) : String(dayOfMonth);
    },
  });
  for (const day of days) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", formatNight(day.date)));
    tr.appendChild(el("td", "num", day.PASS));
    tr.appendChild(el("td", "num", day.FAIL));
    tr.appendChild(el("td", "num", day.FAILED_AS_EXPECTED));
    tr.appendChild(el("td", "num", day.UNEXPECTED_PASS));
    tbody.appendChild(tr);
  }
}

/** A "run #N at <time>" link that fetches and shows that run's output. */
function runLink(runRef) {
  const btn = el("button", "linkish",
    formatTime(runRef.start_time) + " (run #" + runRef.run_id + ")");
  btn.type = "button";
  btn.addEventListener("click", () => showRunOutput(runRef.run_id));
  return btn;
}

function renderFailingSince(failingSince, lastPass) {
  const box = document.getElementById("failing-since");
  clearNode(box);
  if (!failingSince) {
    box.appendChild(el("div", "muted", "Not currently failing."));
    return;
  }
  const line = el("div", "");
  line.appendChild(document.createTextNode("Failing since "));
  line.appendChild(runLink(failingSince));
  box.appendChild(line);
  const passLine = el("div", "muted");
  if (lastPass) {
    passLine.appendChild(document.createTextNode("Last pass: "));
    passLine.appendChild(runLink(lastPass));
  } else {
    passLine.textContent = "No pass within the window.";
  }
  box.appendChild(passLine);
}

function renderFlakiness(flakiness, runCount) {
  const box = document.getElementById("flakiness");
  clearNode(box);
  box.appendChild(el("div", "big", stabilityWord(flakiness.classification)));
  box.appendChild(el("div", "muted",
    "score " + flakiness.score + " — " + flakiness.transitions
    + " transitions in " + runCount + " runs"));
}

function renderDuration(duration) {
  const box = document.getElementById("duration-stats");
  clearNode(box);
  if (!duration) {
    box.appendChild(el("div", "muted", "No runs in window."));
    return;
  }
  box.appendChild(el("div", "big", formatDuration(duration.median) + " median"));
  box.appendChild(el("div", "muted",
    "min " + formatDuration(duration.min)
    + " · max " + formatDuration(duration.max)));
}

/* ---------------- day-of-week SVG chart (hand-rolled) ---------------- */

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const name of Object.keys(attrs)) {
    node.setAttribute(name, String(attrs[name]));
  }
  return node;
}

function svgText(x, y, text, attrs) {
  const node = svgEl("text", Object.assign({
    x: x,
    y: y,
    "font-size": 10,
    fill: "#6b6b66",
    "text-anchor": "middle",
    "font-family": "inherit",
  }, attrs || {}));
  node.appendChild(document.createTextNode(text));
  return node;
}

/** Rounded-top bar path: square at the baseline, 4px-radius data end. */
function barPath(x, width, baseline, height) {
  const r = Math.min(4, height, width / 2);
  const top = baseline - height;
  return "M" + x + " " + baseline
    + " V" + (top + r)
    + " Q" + x + " " + top + " " + (x + r) + " " + top
    + " H" + (x + width - r)
    + " Q" + (x + width) + " " + top + " " + (x + width) + " " + (top + r)
    + " V" + baseline + " Z";
}

function dayTooltip(day) {
  if (day.runs === 0) {
    return day.day + ": no runs";
  }
  let text = day.day + ": " + (day.failure_rate * 100).toFixed(0)
    + "% failure rate — " + day.failures + " of " + day.runs + " runs failed";
  if (day.unexpected_passes > 0) {
    text += ", " + day.unexpected_passes + " unexpected pass(es)";
  }
  return text;
}

/**
 * Draw the 7-day failure-rate mini bar chart (Mon..Sun) into #dow-chart.
 * Single series, one hue; runs counts labeled under the day names; native
 * SVG <title> tooltips; a data table twin lives next to the chart.
 */
function renderDowChart(days) {
  const container = document.getElementById("dow-chart");
  clearNode(container);

  const width = 340;
  const height = 152;
  const marginLeft = 36;
  const marginRight = 6;
  const baseline = 110;
  const plotTop = 10;
  const plotHeight = baseline - plotTop;
  const band = (width - marginLeft - marginRight) / 7;
  const barWidth = 20;

  const svg = svgEl("svg", {
    viewBox: "0 0 " + width + " " + height,
    role: "img",
  });
  svg.setAttribute("aria-label",
    "Failure rate by day of week. " + days.map(dayTooltip).join(". "));

  // Recessive hairline gridlines at 0%, 50%, 100% with axis tick labels.
  for (const rate of [0, 0.5, 1]) {
    const y = baseline - rate * plotHeight;
    svg.appendChild(svgEl("line", {
      x1: marginLeft, y1: y, x2: width - marginRight, y2: y,
      stroke: rate === 0 ? "#d8d8d3" : "#ececea", "stroke-width": 1,
    }));
    svg.appendChild(svgText(marginLeft - 5, y + 3,
      (rate * 100) + "%", { "text-anchor": "end", "font-size": 9 }));
  }

  days.forEach((day, i) => {
    const bandX = marginLeft + i * band;
    const barX = bandX + (band - barWidth) / 2;
    const group = svgEl("g", {});

    const title = document.createElementNS(SVG_NS, "title");
    title.appendChild(document.createTextNode(dayTooltip(day)));
    group.appendChild(title);

    // Generous invisible hit/hover target for the native tooltip.
    group.appendChild(svgEl("rect", {
      x: bandX, y: plotTop - 6, width: band, height: height - plotTop,
      fill: "transparent",
    }));

    if (day.runs > 0 && day.failure_rate > 0) {
      const barHeight = Math.max(2, day.failure_rate * plotHeight);
      group.appendChild(svgEl("path", {
        d: barPath(barX, barWidth, baseline, barHeight),
        fill: FAIL_COLOR,
      }));
    }

    const centerX = bandX + band / 2;
    group.appendChild(svgText(centerX, baseline + 14, day.day, {}));
    group.appendChild(svgText(centerX, baseline + 27, String(day.runs),
      { "font-size": 9, fill: "#9a9a94" }));
    svg.appendChild(group);
  });

  container.appendChild(svg);
}

/** The chart's table-view twin (values readable without color or hover). */
function renderDowTable(days) {
  const tbody = document.getElementById("dow-table-body");
  clearNode(tbody);
  for (const day of days) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", day.day));
    tr.appendChild(el("td", "num", day.runs));
    tr.appendChild(el("td", "num", day.failures));
    tr.appendChild(el("td", "num", (day.failure_rate * 100).toFixed(1) + "%"));
    tr.appendChild(el("td", "num", day.unexpected_passes));
    tbody.appendChild(tr);
  }
}

/* ---------------- run output (shared viewer + history rows) ---------------- */

/** Fetch a run (with output) into the standalone "Run output" section. */
async function showRunOutput(runId) {
  clearError();
  try {
    const run = await fetchJson(runApiPath(runId));
    const section = document.getElementById("run-output-section");
    const meta = document.getElementById("run-output-meta");
    meta.textContent =
      "Run #" + run.run_id + " — " + run.result + " — "
      + formatTime(run.start_time)
      + " (" + formatDuration(run.duration_seconds) + ") ";
    // The same timeline door the history rows carry, anchored to THIS
    // run's start time. Unscoped (deliberately, unchanged from before
    // WP-24): this link never carried this page's own stream even when
    // scoped, so `stream` is explicitly excluded rather than left to
    // pageUrl()'s default carriage.
    const inTimeline = document.createElement("a");
    inTimeline.href = pageUrl("timeline", {
      environment: ident.environment, script: ident.script,
      test: ident.test_name, at: run.start_time,
    }, { stream: null, product: null, baseline: null });
    inTimeline.textContent = "View in timeline →";
    inTimeline.title = "This run in its night's running order — what "
      + "ran before it is listed above it";
    meta.appendChild(inTimeline);
    const copy = copyOutputButton(() => run.output);
    copy.disabled = !run.output;
    meta.appendChild(document.createTextNode(" "));
    meta.appendChild(copy);
    document.getElementById("run-output").textContent =
      run.output === "" ? "(no output)" : run.output;
    section.hidden = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showError("Could not load run output: " + err.message);
  }
}

/* ---------------- history ---------------- */

/** Load one page of history; `before` is the oldest loaded ISO start_time. */
async function loadHistory(before) {
  const data = await fetchJson(apiUrl(myPath("/history"), {
    limit: HISTORY_LIMIT, before: before,
  }, { stream: streamApiScope(), product: null, environment: null }));
  const runs = data.runs;
  const tbody = document.getElementById("history-body");
  for (const run of runs) {
    appendHistoryRow(tbody, run);
  }
  if (runs.length > 0) {
    state.oldestLoadedStart = runs[runs.length - 1].start_time;
  }
  document.getElementById("history-empty").hidden =
    tbody.children.length !== 0;
  document.getElementById("load-more").hidden = runs.length < HISTORY_LIMIT;
}

function appendHistoryRow(tbody, run) {
  const tr = document.createElement("tr");

  const resultCell = document.createElement("td");
  resultCell.appendChild(resultChip(run.result));
  tr.appendChild(resultCell);
  tr.appendChild(el("td", "", formatTime(run.start_time)));
  tr.appendChild(el("td", "num", formatDuration(run.duration_seconds)));
  tr.appendChild(el("td", "wrap",
    run.known_failure_reason ? run.known_failure_reason : "—"));

  // The way to read a run's output has to say what it does. A bare
  // disclosure triangle is not something a first-time user goes looking
  // for, so this is a labelled button — the same one the triage queues
  // use, in a column with a heading.
  const toggleCell = el("td", "review-cell");
  const toggle = el("button", "review-btn", "View output");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label",
    "View output of the run at " + formatTime(run.start_time));
  toggleCell.appendChild(toggle);
  tr.appendChild(toggleCell);

  // Hidden detail row holding the lazily fetched output.
  const detailTr = document.createElement("tr");
  detailTr.hidden = true;
  detailTr.className = "review-row";
  const detailTd = document.createElement("td");
  detailTd.colSpan = 5;
  const panel = el("div", "review-panel");
  // Each history row is one specific run, so its timeline link carries
  // THAT run's start time — landing in the night this result belongs
  // to, not whatever ran most recently. Same door the triage panels
  // have; the poisoned-data hunt usually starts from a page like this.
  const head = el("div", "review-head");
  // Unscoped (deliberately, unchanged from before WP-24) -- see the
  // identical note on showRunOutput()'s copy of this same link above.
  const inTimeline = document.createElement("a");
  inTimeline.href = pageUrl("timeline", {
    environment: ident.environment, script: ident.script,
    test: ident.test_name, at: run.start_time,
  }, { stream: null, product: null, baseline: null });
  inTimeline.textContent = "View in timeline →";
  inTimeline.title = "This run in its night's running order — what ran "
    + "before it is listed above it";
  head.appendChild(el("span", "review-title",
    "Run at " + formatTime(run.start_time)));
  head.appendChild(inTimeline);
  // The full text, for the Copy button: `pre` may hold only the tail.
  let output = null;
  const copy = copyOutputButton(() => output);
  head.appendChild(copy);
  panel.appendChild(head);
  const note = el("p", "review-note", "");
  note.hidden = true;
  const pre = el("pre", "review-output", "");
  panel.appendChild(note);
  panel.appendChild(pre);
  detailTd.appendChild(panel);
  detailTr.appendChild(detailTd);

  let loaded = false;
  toggle.addEventListener("click", async () => {
    if (detailTr.hidden) {
      detailTr.hidden = false;
      toggle.textContent = "Hide output";
      toggle.setAttribute("aria-expanded", "true");
      if (!loaded) {
        pre.textContent = "Loading…";
        try {
          const full = await fetchJson(runApiPath(run.run_id));
          const truncated = fillOutput(pre, full.output);
          output = full.output;
          copy.disabled = !output;
          note.textContent = truncated || "";
          note.hidden = !truncated;
          loaded = true;
        } catch (err) {
          pre.textContent = "Could not load output: " + err.message;
        }
      }
    } else {
      detailTr.hidden = true;
      toggle.textContent = "View output";
      toggle.setAttribute("aria-expanded", "false");
    }
  });

  tbody.appendChild(tr);
  tbody.appendChild(detailTr);
}

/* ---------------- assignee ---------------- */

/**
 * (Re)fill the assignee select, keeping the current value.
 *
 * The names come from the shared cache in api.js rather than a fetch of
 * its own. One page must make one request for this list however many
 * times it needs it, and a second fetch site is how that stops being
 * true - see the note on loadUsers() there.
 */
async function fillAssigneeSelect() {
  const names = await loadUsers();
  const select = document.getElementById("assignee-select");
  clearNode(select);
  const unassigned = el("option", "", "Unassigned");
  unassigned.value = "";
  select.appendChild(unassigned);
  for (const username of names) {
    const opt = el("option", "", username);
    opt.value = username;
    select.appendChild(opt);
  }
  const newOpt = el("option", "", "New user…");
  newOpt.value = NEW_USER_SENTINEL;
  select.appendChild(newOpt);
  select.value = state.currentAssignee ? state.currentAssignee : "";
  select.disabled = false;
}

async function onAssigneeChange() {
  const select = document.getElementById("assignee-select");
  const statusSpan = document.getElementById("assignee-status");
  const previous = state.currentAssignee ? state.currentAssignee : "";
  let target = select.value;

  if (target === NEW_USER_SENTINEL) {
    const entered = window.prompt("New user's name:");
    if (!entered || !entered.trim()) {
      select.value = previous;
      return;
    }
    target = entered.trim();
    try {
      await postJson("api/users", { username: target });
      rememberUser(target);
    } catch (err) {
      showError("Could not create user: " + err.message);
      select.value = previous;
      return;
    }
  }

  const me = requireUsername();
  if (!me) {
    select.value = previous;
    return;
  }

  clearError();
  try {
    const body = {
      username: target === "" ? null : target,
      assigned_by: me,
    };
    // Tag the assignment with the stream this page is scoped to, if
    // any — WHERE it was made from, an annotation, never a partition
    // of who owns the test (docs/STREAMS_PLAN.md §3.4) — the same rule
    // onCommentSubmit() below already follows and api.js's
    // assigneeSelect() has followed since WP-21. Found by the
    // 2026-08-09 persona walk: assigning from a build-scoped test page
    // landed as mainline-origin, so the assignment never showed an
    // origin tag in Open Actions while the SAME action through the
    // dashboard's review panel did.
    if (streamId !== null && !Number.isNaN(streamId)) {
      body.stream_id = streamId;
    }
    const resp = await putJson(myPath("/assignee"), body);
    state.currentAssignee = resp.assignee;
    // Unassigning drops the mute server-side; reread it.
    refreshActs();
    // The response already names the assignee, so refetching the whole
    // user list to discover a name we were just handed is a wasted
    // round trip. Remember it locally and refill from the cache.
    rememberUser(resp.assignee);
    await fillAssigneeSelect();
    statusSpan.textContent = "Saved.";
    window.setTimeout(() => { statusSpan.textContent = ""; }, 2000);
  } catch (err) {
    showError("Could not update assignee: " + err.message);
    select.value = previous;
  }
}

/* ---------------- comments ---------------- */

/**
 * `streams` is the batch-resolved `{stream_id: Stream}` map the
 * comments endpoint returns alongside the list (WP-21) -- a
 * per-comment lookup would cost one query per comment on a thread
 * with mixed history, so the caller resolves everything ONCE and
 * this function only reads it. A comment with `stream_id: null`, or
 * one whose id is not in the map (posted before WP-21, or with no
 * declared context), gets no tag at all -- never a fabricated
 * "mainline".
 */
function commentNode(comment, streams) {
  const box = el("div", "comment");
  const head = el("p", "comment-head");
  head.appendChild(el("strong", "", comment.author));
  head.appendChild(document.createTextNode(
    " — " + formatTime(comment.created_at) + " (UTC)"));
  const stream = (comment.stream_id === null
    || comment.stream_id === undefined)
    ? null : streams[String(comment.stream_id)];
  if (stream) {
    head.appendChild(el("span", "comment-stream-tag muted",
      " — posted from " + streamLabel(stream)));
  }
  box.appendChild(head);
  box.appendChild(el("div", "comment-body", comment.text));
  return box;
}

async function loadComments() {
  const data = await fetchJson(myPath("/comments"));
  const list = document.getElementById("comments-list");
  clearNode(list);
  const streams = data.streams || {};
  for (const comment of data.comments) {   // oldest first from the server
    list.appendChild(commentNode(comment, streams));
  }
  document.getElementById("comments-empty").hidden =
    data.comments.length !== 0;
}

async function onCommentSubmit(event) {
  event.preventDefault();
  const textarea = document.getElementById("comment-text");
  const text = textarea.value.trim();
  if (!text) {
    textarea.focus();
    return;
  }
  const me = requireUsername();
  if (!me) {
    // Cancelling the username prompt used to do nothing at all, which
    // looks exactly like a broken button.
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— comments are recorded against a name.");
    return;
  }
  clearError();
  try {
    const body = { username: me, text: text };
    // Tag the comment with the stream this page is scoped to, if any —
    // "posted from" is where the comment was written FROM, not a filter
    // on where it shows (it always shows, on every scope, per
    // docs/STREAMS_PLAN.md §3.6).
    if (streamId !== null && !Number.isNaN(streamId)) {
      body.stream_id = streamId;
    }
    const resp = await postJson(myPath("/comments"), body);
    const streams = state.streamIdentity
      ? { [String(streamId)]: state.streamIdentity } : {};
    document.getElementById("comments-list")
      .appendChild(commentNode(resp.comment, streams));
    document.getElementById("comments-empty").hidden = true;
    textarea.value = "";
  } catch (err) {
    showError("Could not post comment: " + err.message);
  }
}

/* ---------------- compare strip (WP-21) ---------------- */

/**
 * Only when this page was opened `?stream=<id>`: mainline's own result
 * for the SAME triple, fetched separately and shown beside the branch's
 * (already in `detail.latest`). Not a new endpoint — a single test's
 * result on two named streams is not what /api/compare answers (that is
 * an ESTATE-wide comparison); two ordinary detail fetches say it in the
 * same shape the rest of this page already uses.
 *
 * A test that exists on the branch but has never run on mainline 404s
 * the second fetch — read as "no result", not an error, the same
 * "absence is a fact, not a failure" rule the delta table's no-result
 * cells follow.
 */
async function loadCompareStrip(detail) {
  const strip = document.getElementById("compare-strip");
  if (streamId === null || !detail.stream_identity) {
    strip.hidden = true;
    return;
  }
  // The loud "you are scoped" indicator, shared with the dashboard's
  // delta view — a reader deep in this page's history/analytics has to
  // be able to tell at a glance, which is exactly what was missing when
  // the compare strip alone shipped (WP-21, first human use).
  renderBranchBand(detail.stream_identity);
  let mainlineResult = null;
  try {
    const mainlineDetail = await fetchJson(myPath());
    mainlineResult = mainlineDetail.latest ? mainlineDetail.latest.result : null;
  } catch (err) {
    mainlineResult = null;
  }
  const streamResult = detail.latest ? detail.latest.result : null;
  renderCompareStrip(strip, detail.stream_identity, mainlineResult, streamResult);
}

/* ---------------- stream switcher / "Every build" (WP-22) ---------------- */

/** This page, scoped to *targetStreamId* (null = mainline) — the same
 * environment/script/test_name, per docs/STREAMS_PLAN.md §4.1: switching
 * a stream never changes WHICH test is being looked at. */
function testPageUrl(targetStreamId) {
  return pageUrl("test", {
    environment: ident.environment, script: ident.script,
    test_name: ident.test_name,
  }, { stream: targetStreamId, product: null, baseline: null });
}

/** "Mainline nightlies" / "build:1.0" / "branch:feat/x" — an <option>
 * cannot carry a chip, so this label plus the result word IS the
 * carrier (docs/STREAMS_PLAN.md §4.1's explicit note: never colour
 * alone, and a select option cannot even offer colour). */
function switcherLabel(stream) {
  return stream.kind === "mainline"
    ? "Mainline nightlies" : stream.kind + ":" + stream.name;
}

/**
 * The dropdown near the top of the page (docs/STREAMS_PLAN.md §4.1,
 * explicitly requested during first human use of WP-21): mainline plus
 * every stream of this product that has ANY result for this triple —
 * exactly `results`, the per-triple endpoint's own list, never widened
 * with NO RESULT entries (a switcher exists to jump to a result, and an
 * entry with nothing to show is not a useful destination — that is the
 * "Every build" table's job instead, see loadEveryBuild()). Hidden
 * entirely when there is nothing to switch TO.
 */
function renderStreamSwitcher(results) {
  const field = document.getElementById("stream-switcher-field");
  if (results.length < 2) {
    field.hidden = true;
    return;
  }
  const select = document.getElementById("stream-switcher-select");
  clearNode(select);
  for (const row of results) {
    const opt = el("option", "",
      switcherLabel(row.stream) + " — " + row.result);
    opt.value = row.stream.kind === "mainline" ? "" : String(row.stream.id);
    select.appendChild(opt);
  }
  // The CURRENT scope may be absent from this list: a stream reached by
  // some other route (e.g. a delta-table link) that has no result for
  // THIS triple never appears here by construction (results-only,
  // above), so the select simply falls back to its first option rather
  // than claiming a match that is not there.
  select.value = streamId === null ? "" : String(streamId);
  select.onchange = () => {
    const chosen = select.value ? parseInt(select.value, 10) : null;
    window.location.href = testPageUrl(chosen);
  };
  field.hidden = false;
}

function buildEveryBuildRow(entry, isHere) {
  const tr = document.createElement("tr");
  const streamTd = document.createElement("td");
  const link = document.createElement("a");
  link.href = testPageUrl(
    entry.stream.kind === "mainline" ? null : entry.stream.id);
  link.textContent = switcherLabel(entry.stream);
  streamTd.appendChild(link);
  if (isHere) {
    streamTd.appendChild(el("span", "row-sub", " — you are here"));
  }
  tr.appendChild(streamTd);

  const resultTd = document.createElement("td");
  resultTd.appendChild(entry.result
    ? resultChip(entry.result) : el("span", "chip-none muted", "no result"));
  tr.appendChild(resultTd);

  tr.appendChild(el(
    "td", "", entry.start_time ? formatTime(entry.start_time) : "—"));
  return tr;
}

/**
 * The "Every build" disclosure (docs/STREAMS_PLAN.md §4.1): this
 * triple's latest result on EVERY stream of its product, newest first,
 * NO RESULT rows for streams that never ran it. `GET .../streams`
 * (results-only, per §0.6 never fabricated) is unioned here — by
 * STREAM ID, against the FULL product stream list from
 * `GET /api/streams?product=` — to render the absent ones as NO RESULT;
 * the per-triple list itself is kept intact rather than rebuilt from
 * the product list, so a stream whose product mapping changed after it
 * ran this test is never silently dropped (it is still in `results`
 * even if `/api/streams?product=<the CURRENT mapping>` no longer names
 * it).
 */
async function loadEveryBuild() {
  const section = document.getElementById("every-build-section");
  let data;
  try {
    data = await fetchJson(myPath("/streams"));
  } catch (err) {
    section.hidden = true;
    return;
  }
  renderStreamSwitcher(data.results);

  let productStreams = [];
  if (data.product) {
    try {
      const list = await fetchJson(
        "api/streams?product=" + encodeURIComponent(data.product));
      productStreams = list.streams || [];
    } catch (err) {
      productStreams = [];
    }
  }

  const withResult = data.results.slice();   // already newest first
  const resultKeys = new Set(withResult.map((row) =>
    row.stream.kind === "mainline" ? "mainline" : String(row.stream.id)));
  const withoutResult = [];
  if (!resultKeys.has("mainline")) {
    withoutResult.push({
      stream: { kind: "mainline", name: "", id: null, last_seen: "" },
      result: null, start_time: null,
    });
  }
  for (const stream of productStreams) {
    if (!resultKeys.has(String(stream.id))) {
      withoutResult.push({ stream: stream, result: null, start_time: null });
    }
  }
  withoutResult.sort((a, b) =>
    (a.stream.last_seen < b.stream.last_seen) ? 1 : -1);

  const body = document.getElementById("every-build-body");
  clearNode(body);
  const rows = withResult.concat(withoutResult);
  for (const entry of rows) {
    const isHere = entry.stream.kind === "mainline"
      ? streamId === null : entry.stream.id === streamId;
    body.appendChild(buildEveryBuildRow(entry, isHere));
  }
  // One row (mainline, the page's own result, nothing else exists for
  // this product) is not a disclosure worth opening — zero visible
  // change on a page/product with no other streams, the same rule every
  // other WP-21/WP-22 addition follows.
  section.hidden = rows.length < 2;
}

/* ---------------- init ---------------- */

async function init() {
  renderUserWidget(document.getElementById("user-widget"));

  if (!ident.environment || !ident.script || !ident.test_name) {
    showError("Missing environment/script/test_name in the URL. "
      + "Open this page from a dashboard row.");
    return;
  }

  document.getElementById("test-title").textContent = ident.test_name;
  document.getElementById("test-identity").textContent =
    ident.environment + " · " + ident.script;

  let detail;
  try {
    detail = await fetchJson(detailUrl());
  } catch (err) {
    showError("Could not load test: " + err.message);
    return;
  }
  renderDetail(detail);
  loadCompareStrip(detail);

  document.getElementById("history-section").hidden = false;
  document.getElementById("comments-section").hidden = false;
  document.getElementById("assignee-select")
    .addEventListener("change", onAssigneeChange);
  document.getElementById("load-more")
    .addEventListener("click", () => {
      loadHistory(state.oldestLoadedStart)
        .catch((err) => showError("Could not load history: " + err.message));
    });
  document.getElementById("comment-form")
    .addEventListener("submit", onCommentSubmit);

  // Independent loads: one failure should not blank the others.
  fillAssigneeSelect().catch((err) =>
    showError("Could not load users: " + err.message));
  loadHistory(null).catch((err) =>
    showError("Could not load history: " + err.message));
  loadComments().catch((err) =>
    showError("Could not load comments: " + err.message));
  // Decoration, like streams.js's picker: a failed fetch leaves the
  // switcher/disclosure exactly as the page shipped (both hidden),
  // never a visible error banner over a page that otherwise loaded.
  loadEveryBuild().catch(() => {});
}

init();
