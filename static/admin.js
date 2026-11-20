/* admin.js — the Admin page: the board's rarely changed settings.
 *
 * Two sections, both open, Environments first (flaky-default design §3
 * U1): each environment's expected count, product and flaky default,
 * then the people who can be assigned work. They lived folded at the
 * foot of Open Actions, which is opened every morning to triage and
 * almost never to configure; here they are the page's whole job.
 *
 * The two reads start together in init() and each section draws on its
 * own answer, so neither waits for the other. A failed read is said in
 * its own section; a failed write goes to the page's banner and keeps
 * what was typed. Every write is recorded against the signed-in name,
 * and every user string reaches the DOM through textContent.
 */

"use strict";

import {
  FLAKY_DEFAULT_NOTE,
  cadenceWords,
  clearError,
  clearNode,
  el,
  fetchJson,
  flakyCadenceSelect,
  flakyDefaultChange,
  flakyDefaultEffect,
  flakyDefaultSetBy,
  formatTime,
  putJson,
  renderUserWidget,
  requireUsername,
  showError,
} from "./api.js";

/** The message every write shows when nobody is signed in. */
const NO_USERNAME = "Set a username first (the “Change” button, top right) "
  + "— this is recorded against your name.";

/** The one environment `?environment=` names: its row is outlined each
 * time the table is drawn, and scrolled to the first time (item 26's
 * "Change the default" links here). Never a filter. */
const targetEnvironment =
  new URL(window.location.href).searchParams.get("environment");
let scrolledToTarget = false;

/** Say a section's failed read in the section itself, and nowhere else. */
function sayFailed(id, what, err) {
  const line = document.getElementById(id);
  line.textContent = "Could not load " + what + ": " + err.message
    + ". Reload the page to try again.";
  line.hidden = false;
}

/* ================= people ================= */

/*
 * Deactivating a user. The roster asks for every user, active or not
 * (`include_inactive=1`): a genuinely different list from the assignable
 * one every picker reads through api.js's loadUsers(), fetched here once
 * per visit, and nowhere else.
 */
async function loadPeople() {
  const tbody = document.getElementById("people-body");
  try {
    const data = await fetchJson("api/users?include_inactive=1");
    clearNode(tbody);
    for (const person of data.users) {
      tbody.appendChild(buildPersonRow(person));
    }
    document.getElementById("people-empty").hidden = data.users.length !== 0;
    document.getElementById("people-failed").hidden = true;
  } catch (err) {
    sayFailed("people-failed", "the people", err);
  } finally {
    document.getElementById("people-loading").hidden = true;
  }
}

function buildPersonRow(person) {
  const tr = document.createElement("tr");
  if (!person.active) {
    tr.className = "is-inactive";
  }
  tr.appendChild(el("td", "", person.username));

  const status = el("td");
  status.appendChild(el("span", person.active ? "chip" : "chip chip-ghost",
    person.active ? "Active" : "Deactivated"));
  tr.appendChild(status);

  tr.appendChild(el("td", "muted",
    person.active ? "—" : formatTime(person.deactivated_at)
      + (person.deactivated_by ? " by " + person.deactivated_by : "")));

  const actionCell = el("td");
  const btn = el("button", "", person.active ? "Deactivate" : "Reactivate");
  btn.type = "button";
  btn.addEventListener("click", async () => {
    const me = requireUsername();
    if (!me) {
      showError(NO_USERNAME);
      return;
    }
    btn.disabled = true;
    clearError();
    try {
      await putJson("api/users/" + encodeURIComponent(person.username)
        + "/active", { active: !person.active, changed_by: me });
      await loadPeople();
    } catch (err) {
      btn.disabled = false;
      showError(err.message);
    }
  });
  actionCell.appendChild(btn);
  tr.appendChild(actionCell);
  return tr;
}

/* ================= environments ================= */

/*
 * How many tests an environment is supposed to run — the denominator of
 * the coverage test that decides whether a night's activity counted as
 * a pass of the suite, and therefore when a quiet test is called
 * missing.
 *
 * The declaration on its own is a number in a box. What makes it usable
 * is the echo beside it: how many of the recent nights actually counted.
 * A number that is too high fails SILENTLY — nothing clears the bar, no
 * pass counts, and the staleness line drops back to a fixed 36-hour
 * window, which is the Monday-morning problem the derived one exists to
 * fix. "0 of 14 counted" is what that looks like from outside.
 *
 * The flaky default rides the same read (`flaky_default` and its newest
 * twenty changes, `flaky_default_history`). A server that does not send
 * the key has no defaults: no column, no sentence.
 */
async function loadEnvs() {
  const tbody = document.getElementById("envs-body");
  try {
    const data = await fetchJson("api/environments");
    const withDefaults = data.environments.some(
      (item) => "flaky_default" in item);
    document.getElementById("envs-flaky-head").hidden = !withDefaults;
    const note = document.getElementById("envs-flaky-note");
    note.textContent = withDefaults ? FLAKY_DEFAULT_NOTE : "";
    note.hidden = !withDefaults;
    clearNode(tbody);
    for (const item of data.environments) {
      const tr = buildEnvRow(item, withDefaults);
      tbody.appendChild(tr);
      if (item.environment === targetEnvironment) {
        tr.classList.add("env-row-target");
        if (!scrolledToTarget) {
          scrolledToTarget = true;
          tr.scrollIntoView({ block: "center" });
        }
      }
    }
    document.getElementById("envs-empty").hidden =
      data.environments.length !== 0;
    document.getElementById("envs-failed").hidden = true;
    document.getElementById("envs-cutoff").textContent =
      data.cutoff_from_passes
        ? "Tests are counted as quiet if they have not run since "
          + formatTime(data.cutoff) + ", which is when the suite was"
          + " last round."
        : "No recent night ran enough of any environment to count as a"
          + " full pass, so testboard has fallen back to a fixed "
          + data.recent_hours + "-hour window (" + formatTime(data.cutoff)
          + "). If the suite is running normally, an expected count below"
          + " is too high.";
    updateBulkAssignControl(data.environments);
  } catch (err) {
    sayFailed("envs-failed", "the environments", err);
  } finally {
    document.getElementById("envs-loading").hidden = true;
  }
}

/** The bulk "assign every unmapped environment to product X" control
 * (WP-23 addendum) — kept in sync with loadEnvs()'s own fetch rather
 * than issuing a second one; the unmapped list is data.environments
 * filtered by product === "", already in memory. Hidden entirely once
 * nothing is unmapped: zero visible change on an estate that has
 * already declared every environment, or never uses products at all. */
let unmappedEnvs = [];  // updated each loadEnvs(); read by bulkAssignUnmapped()

function updateBulkAssignControl(environments) {
  unmappedEnvs = environments.filter((item) => !item.product);
  document.getElementById("envs-bulk-assign").hidden =
    unmappedEnvs.length === 0;
  document.getElementById("envs-bulk-count").textContent =
    String(unmappedEnvs.length);
}

/**
 * One-time upgrade-day action (WP-23 addendum, docs/STREAMS_PLAN.md
 * §2): assign every CURRENTLY unmapped environment to one product, in
 * one click instead of N. Issues the SAME per-environment PUT
 * saveEnvProduct already uses below — no new API surface, and each
 * PUT is independently idempotent, so a network blip mid-batch just
 * leaves the remainder unmapped, safely retried by running the action
 * again.
 *
 * Confirm-before-do, the SAME idiom every other write here uses —
 * never a browser confirm() dialog, which nothing in this codebase
 * uses: the product name field is required and validated before
 * anything is sent, and the label states the exact count being
 * committed before the click happens.
 */
async function bulkAssignUnmapped() {
  const me = requireUsername();
  if (!me) {
    showError(NO_USERNAME);
    return;
  }
  const input = document.getElementById("envs-bulk-product");
  const product = input.value.trim();
  if (!product) {
    input.focus();
    showError(
      "Name the product to assign the unmapped environments to.");
    return;
  }
  if (unmappedEnvs.length === 0) {
    return;
  }
  const btn = document.getElementById("envs-bulk-btn");
  btn.disabled = true;
  clearError();
  try {
    for (const item of unmappedEnvs) {
      await putJson(
        "api/environments/" + encodeURIComponent(item.environment)
        + "/product",
        { product: product, username: me });
    }
    input.value = "";
  } catch (err) {
    // A failure partway through (env 3 of 5, say) still committed 1
    // and 2 server-side -- reloading in the finally block below,
    // rather than only here, is what keeps the table and the "Assign
    // N" count from lying about what actually happened.
    showError(err.message);
  } finally {
    // Always runs, success or partial failure, so the page never shows
    // a stale unmapped count against what the server actually holds.
    await loadEnvs();
    btn.disabled = false;
  }
}

/**
 * Which product an environment belongs to (WP-20). Same declare/echo
 * shape as the "Expected" column: an inline text box, blank means the
 * implicit product, a Clear button appears once one is set.
 */
function buildProductCell(item) {
  const cell = el("td");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "env-product";
  input.value = item.product || "";
  input.placeholder = "none";
  input.setAttribute(
    "aria-label", "Product for " + item.environment
    + " — blank means no product declared");
  cell.appendChild(input);

  const save = el("button", "link-btn", "Save");
  save.type = "button";
  save.addEventListener(
    "click", () => saveEnvProduct(item, input.value.trim(), save));
  cell.appendChild(document.createTextNode(" "));
  cell.appendChild(save);
  if (item.product) {
    const clear = el("button", "link-btn", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => saveEnvProduct(item, "", clear));
    cell.appendChild(document.createTextNode(" "));
    cell.appendChild(clear);
  }
  return cell;
}

async function saveEnvProduct(item, raw, button) {
  const me = requireUsername();
  if (!me) {
    showError(NO_USERNAME);
    return;
  }
  button.disabled = true;
  clearError();
  try {
    await putJson(
      "api/environments/" + encodeURIComponent(item.environment)
      + "/product",
      { product: raw, username: me });
    // Reload rather than patch the row: a product change can affect
    // every other environment's Product cell if the declared set
    // crosses the two-or-more threshold.
    await loadEnvs();
  } catch (err) {
    button.disabled = false;
    showError(err.message);
  }
}

function buildEnvRow(item, withDefaults) {
  const tr = document.createElement("tr");
  tr.appendChild(el("td", "", item.environment));
  tr.appendChild(buildProductCell(item));
  tr.appendChild(el("td", "muted", String(item.tests_seen)));

  const input = document.createElement("input");
  input.type = "number";
  input.min = "1";
  input.className = "env-expected";
  input.value = item.expected_tests === null ? "" : String(item.expected_tests);
  // Just the word. The inferred number is already in the Tests seen
  // column immediately to the left, and repeating it here did not fit:
  // a number input spends most of a narrow box on its spinner arrows.
  input.placeholder = "inferred";
  input.title = "Leave blank to use the inferred count ("
    + item.tests_seen.toLocaleString() + " tests seen)";
  input.setAttribute(
    "aria-label", "Expected tests for " + item.environment
    + " — blank means the inferred count, " + item.tests_seen);
  const expected = el("td");
  expected.appendChild(input);
  tr.appendChild(expected);

  // The echo. Colour is not the encoding — the count is.
  const counted = el("td", item.passes_covered === 0 && item.passes_total > 0
    ? "env-none-counted" : "");
  counted.textContent = item.passes_total === 0
    ? "none seen"
    : item.passes_covered + " of " + item.passes_total + " counted";
  tr.appendChild(counted);

  const latest = el("td", "muted");
  latest.textContent = item.latest_pass === null
    ? "—"
    : formatTime(item.latest_pass.started) + " · "
      + item.latest_pass.runs + " runs"
      + (item.latest_pass.covered ? "" : " (did not count)");
  tr.appendChild(latest);

  const actions = el("td");
  const save = el("button", "", "Save");
  save.type = "button";
  save.addEventListener(
    "click", () => saveEnv(item, input.value.trim(), save));
  actions.appendChild(save);
  if (item.expected_tests !== null) {
    const clear = el("button", "link-btn", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => saveEnv(item, "", clear));
    actions.appendChild(clear);
  }
  tr.appendChild(actions);
  // Who set the expected count, on its own two cells only: on the row
  // it showed beside the flaky default's "set by", perhaps another name.
  if (item.updated_by) {
    expected.title = actions.title = "Set by " + item.updated_by + " on "
      + formatTime(item.updated_at);
  }
  if (withDefaults) {
    tr.appendChild(buildFlakyDefaultCell(item, tr));
  }
  return tr;
}

async function saveEnv(item, raw, button) {
  const me = requireUsername();
  if (!me) {
    showError(NO_USERNAME);
    return;
  }
  let expected = null;
  if (raw !== "") {
    expected = Number(raw);
    if (!Number.isInteger(expected) || expected < 1) {
      showError(
        "Expected tests must be a whole number of at least 1, or blank"
        + " to go back to inferring it.");
      return;
    }
  }
  button.disabled = true;
  clearError();
  try {
    await putJson(
      "api/environments/" + encodeURIComponent(item.environment)
      + "/expectation",
      { expected_tests: expected, changed_by: me });
    // Reload rather than patch the row: changing the denominator changes
    // which nights counted for EVERY environment's echo and the cutoff
    // line above, so a row-level update would leave the rest lying.
    await loadEnvs();
  } catch (err) {
    button.disabled = false;
    showError(err.message);
  }
}

/* ================= the flaky default ================= */

/*
 * The environments table's last column (decision 18): the default in
 * force, or "none", with the act that changes it; who set it, when and
 * why; and the newest changes under History, drawn from the same read
 * with no request of their own (decision 8). Set, Change and Remove open
 * one editing row under the environment's, the table's width.
 */
function buildFlakyDefaultCell(item, tr) {
  const cell = el("td", "env-default-cell");
  const current = item.flaky_default || null;
  const history = item.flaky_default_history || [];
  const head = el("span");
  const act = (label, mode) => {
    const button = el("button", "link-btn", label);
    button.type = "button";
    button.addEventListener(
      "click", () => openFlakyDefaultEditor(item, tr, mode));
    return button;
  };
  if (current) {
    head.appendChild(el("strong", "", cadenceWords(current.cadence)));
    head.appendChild(document.createTextNode(" "));
    head.appendChild(act("Change", "change"));
    head.appendChild(document.createTextNode(" · "));
    head.appendChild(act("Remove", "remove"));
    cell.appendChild(head);
    cell.appendChild(el("span", "row-sub", flakyDefaultSetBy(current)));
  } else {
    head.appendChild(el("span", "muted", "none"));
    head.appendChild(document.createTextNode(" "));
    head.appendChild(act("Set", "set"));
    cell.appendChild(head);
    // The last act, when it was a removal: who took it away and why.
    if (history.length && history[0].action === "clear") {
      cell.appendChild(el("span", "row-sub", flakyDefaultChange(history[0])));
    }
  }
  if (history.length) {
    const details = el("details", "env-default-history");
    details.appendChild(el("summary", "", "History (" + history.length + ")"));
    const list = el("ul");
    for (const change of history) {
      list.appendChild(el("li", "", flakyDefaultChange(change)));
    }
    details.appendChild(list);
    cell.appendChild(details);
  }
  return cell;
}

/**
 * The editing row under an environment's: `mode` is "set" or "change"
 * (a cadence and a reason; Change starts on the cadence in force with
 * the reason empty, as the new reason is what rows will show) or
 * "remove" (a reason only). Save waits for a cadence and a reason,
 * Remove for a reason, and nothing is pressed twice while a request is
 * out. A success redraws the table; a failure goes to the banner and
 * keeps what was typed. The signed-in user is the one recorded: there
 * is no one to pick (decision 7).
 */
function openFlakyDefaultEditor(item, tr, mode) {
  // One editor open at a time: a save redraws the table, which would
  // drop whatever was typed in another row's editor without a word.
  for (const open of tr.parentNode.querySelectorAll(".env-default-editor")) {
    open.remove();
  }
  const removing = mode === "remove";
  const row = el("tr", "env-default-editor");
  const td = el("td");
  td.colSpan = tr.children.length;
  const controls = el("div", "env-default-controls");
  controls.appendChild(el("span", "env-default-label",
    (removing ? "Remove the flaky default for " : "Flaky default for ")
    + item.environment));
  let cadence = null;
  if (!removing) {
    cadence = flakyCadenceSelect("env-cadence-select",
      mode === "change" ? item.flaky_default.cadence : undefined);
    controls.appendChild(cadence);
  }
  const reason = document.createElement("input");
  reason.type = "text";
  reason.className = "env-default-reason";
  reason.placeholder = removing
    ? "Why remove it? (required)" : "Why this default? (required)";
  reason.setAttribute("aria-label", removing
    ? "Reason for removing the flaky default"
    : "Reason for the flaky default");
  controls.appendChild(reason);
  const send = el("button", "", removing ? "Remove" : "Save");
  send.type = "button";
  const cancel = el("button", "", "Cancel");
  cancel.type = "button";
  controls.appendChild(send);
  controls.appendChild(cancel);
  td.appendChild(controls);
  const effect = el("p", "muted env-default-effect");
  td.appendChild(effect);
  row.appendChild(td);

  let inFlight = false;
  const derive = () => {
    const chosen = removing ? null : Number(cadence.value);
    send.disabled = inFlight || !reason.value.trim()
      || (!removing && !chosen);
    cancel.disabled = inFlight;
    effect.textContent = removing || chosen
      ? flakyDefaultEffect(chosen) : "";
    effect.hidden = !effect.textContent;
  };
  derive();
  reason.addEventListener("input", derive);
  if (cadence) {
    cadence.addEventListener("change", derive);
  }
  cancel.addEventListener("click", () => row.remove());
  send.addEventListener("click", async () => {
    const text = reason.value.trim();
    if (inFlight || !text || (!removing && !Number(cadence.value))) {
      return;
    }
    const me = requireUsername();
    if (!me) {
      showError(NO_USERNAME);
      return;
    }
    inFlight = true;
    derive();
    clearError();
    try {
      await putJson(
        "api/environments/" + encodeURIComponent(item.environment)
        + "/flaky-default",
        { username: me, cadence: removing ? null : Number(cadence.value),
          reason: text });
    } catch (err) {
      showError(err.message);
      inFlight = false;
      derive();
      return;
    }
    // The redraw removes this row; should the read fail, the row stays,
    // armed again, under the section's own message.
    await loadEnvs();
    inFlight = false;
    derive();
  });
  tr.parentNode.insertBefore(row, tr.nextSibling);
  // The first thing to choose: a cadence on Set; on Change the cadence
  // in force is already chosen, and Remove has only a reason.
  (mode === "set" ? cadence : reason).focus();
}

/* ================= init ================= */

function init() {
  renderUserWidget(document.getElementById("user-widget"));
  document.getElementById("envs-bulk-btn")
    .addEventListener("click", bulkAssignUnmapped);
  // Both reads at once, Environments first; each section draws on its
  // own answer and neither waits for the other.
  loadEnvs();
  loadPeople();
}

init();
