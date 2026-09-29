/* buildadmin.js — delete what a build holds for one environment (WP-34).
 *
 * For the upload that should not have happened: a build pushed against
 * the wrong environment, or a run of it that was broken from the start.
 * One environment at a time, and only ever from a build — mainline
 * cannot be deleted from here or from anywhere else in the UI.
 *
 * ZERO FOOTPRINT until asked for. The section is `hidden` in the shipped
 * markup; this module shows it only on a page scoped to a stream
 * (`?stream=<id>`), collapsed; and nothing is fetched until a person
 * opens it. A mainline page never sees it and never pays for it.
 *
 * THERE IS NO LOGIN. Anyone who can open the page can use this, so the
 * control is built to make a slip hard rather than to keep anyone out:
 *   - it states, from the server's own counts, exactly what will go;
 *   - the build's name has to be typed back, exactly;
 *   - a reason is required, and goes to the server log with the name of
 *     whoever did it;
 *   - the button stays disabled until all of that is true.
 * The server checks every one of those again. Nothing here is the
 * safeguard on its own.
 *
 * SECURITY: all dynamic text reaches the DOM via textContent/el(), never
 * innerHTML.
 */

"use strict";

import {
  clearNode,
  el,
  fetchJson,
  formatTime,
  postJson,
  requireUsername,
} from "./api.js";
import { getSelectedStreamId } from "./compare.js";
import { apiUrl, withStream } from "./urls.js";

/** Every scope level cleared: these endpoints are addressed by the
 * stream id in their own path, and a page's `environment=` filter
 * carried onto them would name a different environment from the one in
 * the path beside it. */
const NO_SCOPE = {
  product: null, stream: null, baseline: null, environment: null,
};

const state = {
  streamId: null,
  stream: null,        // the server's own identity for it
  environments: [],    // [{environment, tests, runs, last_run}]
  target: null,        // the row chosen for deletion, or null
  busy: false,
};

function byId(id) {
  return document.getElementById(id);
}

function environmentsUrl() {
  return apiUrl(
    "api/streams/" + state.streamId + "/environments", null, NO_SCOPE);
}

function deleteUrl(environment) {
  return apiUrl(
    "api/streams/" + state.streamId + "/environments/"
      + encodeURIComponent(environment) + "/delete",
    null, NO_SCOPE);
}

function plural(count, noun) {
  return count.toLocaleString() + " " + noun + (count === 1 ? "" : "s");
}

function buildLabel() {
  return state.stream.kind + " " + state.stream.name;
}

function setStatus(text, isError) {
  const status = byId("build-admin-status");
  status.textContent = text;
  status.className = isError ? "error-banner" : "muted";
  status.hidden = !text;
}

/** The delete button's one rule, in one place. */
function readyToDelete() {
  return state.target !== null && !state.busy
    && byId("build-admin-reason").value.trim() !== ""
    && byId("build-admin-confirm").value === state.stream.name;
}

function syncDeleteButton() {
  byId("build-admin-delete").disabled = !readyToDelete();
}

function closeForm() {
  state.target = null;
  byId("build-admin-form").hidden = true;
  byId("build-admin-reason").value = "";
  byId("build-admin-confirm").value = "";
  syncDeleteButton();
}

function openForm(row) {
  state.target = row;
  setStatus("", false);
  const last = state.environments.length === 1;
  byId("build-admin-target").textContent =
    "Delete " + buildLabel() + "’s results on " + row.environment
    + ": " + plural(row.tests, "test") + ", " + plural(row.runs, "run")
    + ", and their output. This cannot be undone.";
  byId("build-admin-consequence").textContent = (last
    ? "This is the only environment " + buildLabel() + " has results "
      + "on, so the build itself will disappear from the dashboard. "
    : "Its results on "
      + plural(state.environments.length - 1, "other environment")
      + " are not touched. ")
    + "Mainline is not touched. Comments and assignments on these "
    + "tests are kept. If a feeder is still sending these results, "
    + "they will come back the next time it pushes — stop that first.";
  byId("build-admin-confirm-label").textContent =
    "Type the build’s name (" + state.stream.name + ") to confirm";
  byId("build-admin-reason").value = "";
  byId("build-admin-confirm").value = "";
  byId("build-admin-form").hidden = false;
  syncDeleteButton();
}

function renderEnvironments() {
  const body = byId("build-admin-rows");
  clearNode(body);
  for (const row of state.environments) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "", row.environment));
    tr.appendChild(el("td", "", row.tests.toLocaleString()));
    tr.appendChild(el("td", "", row.runs.toLocaleString()));
    tr.appendChild(el("td", "", formatTime(row.last_run) + " UTC"));
    const action = el("td", "");
    const pick = el("button", "", "Delete…");
    pick.type = "button";
    pick.addEventListener("click", () => openForm(row));
    action.appendChild(pick);
    tr.appendChild(action);
    body.appendChild(tr);
  }
  byId("build-admin-table").hidden = state.environments.length === 0;
}

async function load() {
  closeForm();
  const intro = byId("build-admin-intro");
  intro.textContent = "Loading what this build holds…";
  let data;
  try {
    data = await fetchJson(environmentsUrl());
  } catch (err) {
    intro.textContent = "";
    setStatus(err.message, true);
    return;
  }
  state.stream = data.stream;
  state.environments = data.deletable ? data.environments : [];
  renderEnvironments();
  if (!data.deletable) {
    intro.textContent = "Mainline results cannot be deleted from here.";
  } else if (state.environments.length === 0) {
    intro.textContent = "This build holds no results.";
  } else {
    intro.textContent = "What " + buildLabel() + " holds, by "
      + "environment. Deleting removes one environment’s results "
      + "from this build only.";
  }
}

async function submit() {
  if (!readyToDelete()) {
    return;
  }
  const username = requireUsername();
  if (!username) {
    setStatus("A username is required — a deletion is recorded "
      + "against a name.", true);
    return;
  }
  const target = state.target;
  state.busy = true;
  syncDeleteButton();
  let result;
  try {
    result = await postJson(deleteUrl(target.environment), {
      username: username,
      reason: byId("build-admin-reason").value.trim(),
      confirm: byId("build-admin-confirm").value,
    });
  } catch (err) {
    state.busy = false;
    syncDeleteButton();
    setStatus(err.message, true);
    return;
  }
  state.busy = false;
  const done = "Deleted " + plural(result.deleted.runs, "run") + " of "
    + plural(result.deleted.latest_runs, "test") + " from " + buildLabel()
    + " on " + result.environment + ".";
  if (result.stream_deleted) {
    // Nothing on this page describes anything that still exists.
    closeForm();
    state.environments = [];
    renderEnvironments();
    byId("build-admin-intro").textContent = "";
    setStatus(done + " That was its only environment, so the build is "
      + "gone.", false);
    const back = byId("build-admin-back");
    back.href = withStream(null);
    back.hidden = false;
    return;
  }
  await load();
  setStatus(done, false);
  // The page above still shows what was just deleted. Refresh belongs
  // to whichever tab is showing, and does the right thing for it.
  const reload = byId("reload-btn");
  if (reload) {
    reload.click();
  }
}

function init() {
  const section = byId("build-admin");
  if (!section) {
    return;
  }
  const streamId = getSelectedStreamId();
  if (streamId === null) {
    return;   // mainline: the section stays hidden, and nothing is fetched
  }
  state.streamId = streamId;
  section.hidden = false;
  section.addEventListener("toggle", () => {
    if (section.open) {
      load();
    }
  });
  byId("build-admin-reason").addEventListener("input", syncDeleteButton);
  byId("build-admin-confirm").addEventListener("input", syncDeleteButton);
  byId("build-admin-cancel").addEventListener("click", closeForm);
  byId("build-admin-delete").addEventListener("click", submit);
}

init();
