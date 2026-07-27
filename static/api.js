/* api.js — shared helpers for the testboard frontend.
 *
 * Responsibilities: JSON fetch wrappers (with server error-message extraction),
 * API path building (every identity segment percent-encoded), username handling
 * backed by localStorage, the header user widget, and small DOM helpers.
 *
 * SECURITY: all dynamic data must reach the DOM via textContent/createTextNode.
 * Nothing in this file (or its callers) may interpolate data into innerHTML.
 */

"use strict";

/** localStorage key holding the self-identified username. */
const USERNAME_KEY = "testboard.username";

/** The four result values, in display order. Values match the server enum. */
export const RESULTS = ["PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS"];

/**
 * Fetch a URL and parse the JSON response.
 * On a non-2xx response, throws an Error whose message is the server's
 * {"error": "..."} message when present, else "HTTP <status>".
 * On a network failure, throws an Error with a friendly "server unreachable" hint.
 */
export async function fetchJson(url, opts) {
  let resp;
  try {
    resp = await fetch(url, opts);
  } catch (err) {
    throw new Error(
      "Cannot reach the testboard server (" + err.message + "). Is it running?");
  }
  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      data = null;
    }
  }
  if (!resp.ok) {
    const msg = data && typeof data.error === "string"
      ? data.error
      : "HTTP " + resp.status;
    throw new Error(msg);
  }
  return data;
}

/** POST a JSON body and parse the JSON response (see fetchJson for errors). */
export function postJson(url, body) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
}

/** PUT a JSON body and parse the JSON response (see fetchJson for errors). */
export function putJson(url, body) {
  return fetchJson(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
}

/**
 * Build "/api/tests/{environment}/{script}/{test_name}" + optional suffix.
 * EVERY identity segment goes through encodeURIComponent so names containing
 * "/", spaces, brackets etc. survive as single path segments (%2F and friends).
 */
export function testApiPath(environment, script, testName, suffix) {
  const base = "/api/tests/"
    + encodeURIComponent(environment) + "/"
    + encodeURIComponent(script) + "/"
    + encodeURIComponent(testName);
  return suffix ? base + suffix : base;
}

/** Build "/api/runs/{runId}". */
export function runApiPath(runId) {
  return "/api/runs/" + encodeURIComponent(String(runId));
}

/* ---------------- username handling ---------------- */

/** Return the stored username, or null when unset (or storage unavailable). */
export function getUsername() {
  try {
    return window.localStorage.getItem(USERNAME_KEY);
  } catch (err) {
    return null;
  }
}

/** Store (trimmed, non-empty) or clear (empty/null) the username. */
export function setUsername(name) {
  const trimmed = name ? name.trim() : "";
  try {
    if (trimmed) {
      window.localStorage.setItem(USERNAME_KEY, trimmed);
    } else {
      window.localStorage.removeItem(USERNAME_KEY);
    }
  } catch (err) {
    /* storage unavailable: username simply won't persist */
  }
  refreshUserWidget();
  if (userChangeHandler) {
    userChangeHandler(trimmed);
  }
}

/**
 * Return the current username, prompting for one if unset.
 * Returns null when the user cancels or enters only whitespace.
 */
export function requireUsername() {
  const existing = getUsername();
  if (existing) {
    return existing;
  }
  const entered = window.prompt(
    "Enter a username (stored in this browser; used for comments and assignments):");
  if (!entered || !entered.trim()) {
    return null;
  }
  const name = entered.trim();
  setUsername(name);
  return name;
}

/* The one container registered via renderUserWidget; re-rendered on changes. */
let userWidgetContainer = null;
/* Optional callback run after the username changes (see renderUserWidget). */
let userChangeHandler = null;

/**
 * Render the header user widget (current user + "Change" button) into
 * `container` and keep it in sync with later username changes.
 *
 * `onChange` (optional) is called after the username changes — pages
 * whose content depends on who is signed in (the home screen asks the
 * server for "my actions") use it to reload.
 */
export function renderUserWidget(container, onChange) {
  userWidgetContainer = container;
  userChangeHandler = onChange || null;
  refreshUserWidget();
}

function refreshUserWidget() {
  if (!userWidgetContainer) {
    return;
  }
  clearNode(userWidgetContainer);
  const name = getUsername();
  if (name) {
    userWidgetContainer.appendChild(document.createTextNode("Signed in as "));
    userWidgetContainer.appendChild(el("strong", "", name));
  } else {
    userWidgetContainer.appendChild(document.createTextNode("No username set"));
  }
  const btn = el("button", "", "Change");
  btn.type = "button";
  btn.addEventListener("click", () => {
    const entered = window.prompt(
      "Username (leave empty to clear):", getUsername() || "");
    if (entered === null) {
      return; // cancelled
    }
    setUsername(entered);
  });
  userWidgetContainer.appendChild(btn);
}

/* ---------------- assignment ---------------- */

/*
 * The in-flight request for the user list, or null before the first one.
 *
 * The PROMISE is cached, not the array it resolves to, and that is the
 * whole point. Caching the result looks equivalent and is not: the
 * assignment happens after an `await`, so every caller that runs before
 * the first response arrives sees an empty cache and starts its own
 * request. `assigneeSelect()` below is a per-row cell builder and the
 * row loop is synchronous, so a 250-row page issued 250 concurrent
 * GET /api/users for one identical list.
 *
 * Caching the promise collapses them: the second caller and the
 * two-hundred-and-fiftieth get the same pending promise as the first.
 *
 * This is the ONLY place the frontend fetches /api/users. Adding a
 * second one brings the stampede back, so tests/test_frontend_calls.py
 * fails the build if one appears.
 */
let usersPromise = null;

/* Names assigned during this page's life, not in the fetched list. */
const addedUsers = [];

/** Every username the server knows, for the assignee pickers. */
export function loadUsers() {
  if (usersPromise === null) {
    usersPromise = fetchJson("/api/users")
      .then((data) => data.users.map((user) => user.username))
      // A missing user list must not break assigning: the dropdown still
      // offers you, the current assignee, and anyone added since.
      .catch(() => []);
  }
  return usersPromise.then((names) => names.concat(addedUsers));
}

/**
 * Add a username to the cached list, after assigning to a new person.
 *
 * Kept separately from the fetched names rather than pushed into them:
 * the fetched array lives inside a promise that may not have resolved
 * yet, and mutating it later would be a race. Concatenating on read
 * costs nothing at these sizes and cannot lose a name.
 */
export function rememberUser(name) {
  if (name && addedUsers.indexOf(name) === -1) {
    addedUsers.push(name);
  }
}

/**
 * An assignee dropdown that saves as soon as it changes.
 *
 * Assigning is the single most common action in a triage queue, so it
 * lives in the row itself — no panel to open, no page to visit.
 * `onSaved(name)` runs after a successful save.
 */
export function assigneeSelect(entry, onSaved) {
  const select = document.createElement("select");
  select.className = "assignee-select";
  select.title = "Assign this test";

  const rebuild = (users) => {
    clearNode(select);
    const me = getUsername();
    const none = el("option", "", "— unassigned —");
    none.value = "";
    select.appendChild(none);

    // The current assignee is added even when the fetched list omits
    // them, and that is DELIBERATE — do not "fix" it.
    //
    // /api/users returns active users only, so a test still owned by a
    // deactivated account would otherwise render with an empty
    // dropdown, silently looking unassigned. Injecting the name keeps
    // the row honest about who holds it, and reassigning away is the
    // one action that has to keep working.
    //
    // The same line covers you before the server has heard of you.
    const names = users.slice();
    for (const extra of [me, entry.assignee]) {
      if (extra && names.indexOf(extra) === -1) {
        names.push(extra);
      }
    }
    names.sort((a, b) => a.localeCompare(b));
    for (const name of names) {
      const opt = el("option", "", name === me ? name + " (me)" : name);
      opt.value = name;
      select.appendChild(opt);
    }
    select.value = entry.assignee || "";
    if (!entry.assignee) {
      select.classList.add("is-unassigned");
    }
  };

  rebuild([]);
  loadUsers().then(rebuild);

  select.addEventListener("change", async () => {
    const target = select.value || null;
    // Assigning to somebody requires knowing who YOU are, for the audit
    // trail. Say so plainly instead of failing silently.
    const me = requireUsername();
    if (!me) {
      select.value = entry.assignee || "";
      showError(
        "Set a username first (the “Change” button, top right) "
        + "— assignments record who made them.");
      return;
    }
    select.disabled = true;
    try {
      await putJson(
        testApiPath(entry.environment, entry.script, entry.test_name,
          "/assignee"),
        { username: target, assigned_by: me });
      entry.assignee = target;
      rememberUser(target);
      select.classList.toggle("is-unassigned", !target);
      if (onSaved) {
        onSaved(target);
      }
    } catch (err) {
      select.value = entry.assignee || "";
      showError(err.message);
    } finally {
      select.disabled = false;
    }
  });
  return select;
}

/* ---------------- run output ---------------- */

/**
 * Largest slice of a run's output rendered inline.
 *
 * A failing test can dump megabytes; putting that in the DOM stalls the
 * page. The tail is what matters — that is where the failure is — so
 * long output is truncated from the front.
 */
export const MAX_INLINE_OUTPUT = 20000;

/**
 * Put a run's output into `pre`, truncating if it is very long.
 *
 * Returns a note to show beside it when the output was truncated, or
 * null. Shared by the triage review panel and the run-history rows so
 * both behave the same way on a huge output.
 */
export function fillOutput(pre, text) {
  pre.classList.remove("muted");
  if (!text) {
    pre.textContent = "(this run captured no output)";
    pre.classList.add("muted");
    return null;
  }
  if (text.length > MAX_INLINE_OUTPUT) {
    pre.textContent = text.slice(-MAX_INLINE_OUTPUT);
    return "Showing the last "
      + Math.round(MAX_INLINE_OUTPUT / 1000) + " KB of "
      + Math.round(text.length / 1000).toLocaleString() + " KB.";
  }
  pre.textContent = text;
  return null;
}

/* ---------------- DOM helpers ---------------- */

/** Create an element with optional class and (textContent-only) text. */
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null && text !== "") {
    node.textContent = String(text);
  }
  return node;
}

/** Remove all children of a node. */
export function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

/** CSS marker class for a result value ("" for unknown values). */
export function resultClass(result) {
  switch (result) {
    case "PASS": return "result-pass";
    case "FAIL": return "result-fail";
    case "FAILED_AS_EXPECTED": return "result-failed-as-expected";
    case "UNEXPECTED_PASS": return "result-unexpected-pass";
    default: return "";
  }
}

/** Build a result chip span (colored, text always present — never color-alone). */
export function resultChip(result) {
  return el("span", ("chip " + resultClass(result)).trim(), result);
}

/**
 * A chip for a result that is NO LONGER true — outlined, not filled.
 *
 * Solid means "this is the case now"; outlined means "this was the case
 * before". Without the distinction a triage row showed the previous
 * result as a full saturated chip while the current one was a 3px
 * stripe on the row edge, so a new failure (previous run: PASS) read as
 * a pass and a fixed test (previous run: FAIL) read as a failure. The
 * loudest thing in the row was the wrong value, in the wrong direction,
 * both times.
 *
 * The text label is kept, so this is never colour-alone.
 */
export function ghostChip(result) {
  return el("span", ("chip chip-ghost " + resultClass(result)).trim(), result);
}

/**
 * "was → now" as one cell's worth of nodes, appended to `parent`.
 *
 * Reads left to right in time order. The arrow is muted because it is
 * punctuation, not data.
 */
export function resultTransition(parent, previous, current) {
  const wrap = el("span", "chip-transition");
  if (previous) {
    wrap.appendChild(ghostChip(previous));
    wrap.appendChild(el("span", "transition-arrow", "→"));
  }
  wrap.appendChild(resultChip(current));
  if (!previous) {
    wrap.appendChild(el("span", "row-sub", "first run"));
  }
  parent.appendChild(wrap);
  return parent;
}

/** "2026-07-25T02:14:07.123456" -> "2026-07-25 02:14:07" (display only). */
export function formatTime(iso) {
  if (typeof iso !== "string" || iso.length < 19) {
    return String(iso === null || iso === undefined ? "—" : iso);
  }
  return iso.slice(0, 19).replace("T", " ");
}

/**
 * Human-friendly duration from seconds (e.g. "1.9s", "2m 05s", "8h 40m").
 *
 * Hours matter now that whole suites are being totalled: "520m 22s" is
 * technically correct and nobody reads it as "most of a working day".
 * Seconds are dropped once there are hours — at that scale they are
 * noise, and the extra characters push the value out of its column.
 */
export function formatDuration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) {
    return "—";
  }
  if (seconds < 60) {
    return seconds.toFixed(1) + "s";
  }
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return minutes + "m " + String(rest).padStart(2, "0") + "s";
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours + "h " + String(minutes).padStart(2, "0") + "m";
}

/* ---------------- stability: broken, or just flaky? ---------------- */

/**
 * One sentence saying what a test has been DOING lately.
 *
 * A last-pass date on its own cannot separate "this broke on the 14th
 * and has failed every night since" from "this fails about one night in
 * three". Those need completely different responses — one is a
 * regression to bisect, the other is a test to stabilise — and until
 * now they looked identical in every list.
 */
export function stabilitySentence(stability) {
  if (!stability || !stability.runs) {
    return "no recent runs";
  }
  if (stability.classification === "flaky") {
    const period = Math.max(
      2, Math.round(stability.runs / Math.max(1, stability.transitions)));
    return "flaky — flips about 1 run in " + period;
  }
  if (stability.classification === "stable-fail") {
    return stability.transitions === 0
      ? "failed every one of the last " + stability.runs + " runs"
      : "failing steadily since it broke";
  }
  return "passing steadily";
}

/**
 * A strip of the last N results, oldest to newest.
 *
 * Supports the sentence rather than replacing it: the SHAPE of a
 * failure pattern is what people actually want to see, but colour alone
 * carries nothing for a reader who cannot separate the hues — so the
 * sentence is the primary encoding and every cell keeps a title naming
 * its result.
 */
export function runStrip(stability) {
  const strip = el("div", "run-strip");
  const results = (stability && stability.recent_results) || [];
  if (!results.length) {
    strip.appendChild(el("span", "muted", "no recent runs"));
    return strip;
  }
  results.forEach((result, index) => {
    const cell = el("span", "run-cell " + resultClass(result));
    cell.title = result + " (" + (results.length - index) + " runs ago)";
    strip.appendChild(cell);
  });
  return strip;
}

/* ---------------- error banner ---------------- */

/** Show `message` in the page's #error-banner element. */
export function showError(message) {
  const banner = document.getElementById("error-banner");
  if (!banner) {
    return;
  }
  banner.textContent = String(message);
  banner.hidden = false;
}

/** Hide the page's #error-banner element. */
export function clearError() {
  const banner = document.getElementById("error-banner");
  if (banner) {
    banner.hidden = true;
    banner.textContent = "";
  }
}
