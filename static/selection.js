/* selection.js — multi-select tick boxes and the one shared bulk action
 * bar, for every table where a test can be assigned.
 *
 * WHY ONE MODULE: the same rationale as urls.js (docs/SCOPED_URLS_PLAN.md)
 * -- a checkbox column and a bulk-assign bar are easy to hand-roll per
 * page, and four hand-rolled copies is four sets of bugs (four different
 * "clears on re-render" behaviours, four ways to forget the disabled-
 * until-a-user-is-chosen gate). This module is the one owner;
 * tests/test_frontend_calls.py's SelectionColumnTest fails the build on a
 * hand-rolled `type="checkbox"` table column appearing anywhere else.
 *
 * THE MODEL. mountSelectableTable(table, options) is called ONCE per
 * selectable table (there can be more than one on a page at once -- the
 * dashboard's triage queue and its browse table both show checkboxes
 * simultaneously) and returns { headerCell, rowCell, reset }:
 *
 *   - headerCell() builds the leading "select all / deselect all" <th>,
 *     scoped to ITS OWN table (a click walks `table`'s own row
 *     checkboxes, never another table's).
 *   - rowCell(entry) builds one row's leading checkbox <td>. `entry` is
 *     the SAME shape assigneeSelect()/reviewEntry() already take --
 *     {environment, script, test_name, stream_id?} -- so a page that
 *     already stamps `stream_id` onto its row objects for the row-level
 *     assignee picker (app.js's tagStream() on a branch's own-results
 *     tab; compare.js's reviewEntry() on a build's delta table) carries
 *     the SAME origin into a multi-select action with no extra plumbing.
 *     A mainline surface's rows simply have no `stream_id`, so a
 *     selection made there carries none -- by construction, not by a
 *     special case.
 *   - reset() clears ONLY the selections this mount's rowCell() calls
 *     added (a per-mount namespace) -- called by the page at the top of
 *     its own FRESH (non-append) render, matching "selection is
 *     per-rendered-view by design" (the filter-mode bulk endpoint
 *     already covers "everything matching"). An APPEND ("Show more")
 *     must NOT call reset() -- the newly shown rows join the same view.
 *
 * The selection Set itself, and the one sticky bottom action bar, are
 * module-level singletons: every mounted table's checkboxes write into
 * the SAME Set and the SAME bar, so ticking two rows in the triage queue
 * and three in the browse table below it reads as "5 selected" and one
 * Assign click acts on all five together.
 *
 * SECURITY: all dynamic text reaches the DOM via textContent/el(), never
 * innerHTML.
 */

"use strict";

import {
  clearNode,
  el,
  muteDurationSelect,
  postJson,
  rememberUser,
  requireUsername,
  showError,
  userPickerSelect,
} from "./api.js";
import { entryKey } from "./review.js";
import { apiUrl, bulkMutesUrl, unmuteUrl } from "./urls.js";

/**
 * The bulk endpoint's URL, EVERY scope level explicitly cleared.
 *
 * apiUrl()'s whole point (WP-24, docs/SCOPED_URLS_PLAN.md) is that a
 * call with no `scope` argument CARRIES the current page's own
 * product/stream/baseline/environment -- exactly wrong here: this bar is
 * mounted on pages that may have `?environment=...`/`?stream=...` in
 * their own address bar (Open Actions, a branch's own-results tab), and
 * letting that leak onto this POST's query string would both be
 * meaningless to the endpoint AND, for `environment`, trip list mode's
 * own mutual-exclusion 400 (testboard.api._DASHBOARD_FILTER_QUERY_PARAMS)
 * -- a bug this project has a name for (docs/SCOPED_URLS_PLAN.md §1).
 * Naming all four levels null is what keeps this call through apiUrl()
 * AND carrying nothing.
 */
function bulkAssignmentsUrl() {
  return apiUrl(
    "api/assignments/bulk", null,
    { product: null, stream: null, baseline: null, environment: null },
  );
}

/** The bulk comment endpoint (WP-35), scope cleared for the same
 * reason: where each comment was posted from travels per test, inside
 * the body, exactly as it does for an assignment. */
function bulkCommentsUrl() {
  return apiUrl(
    "api/comments/bulk", null,
    { product: null, stream: null, baseline: null, environment: null },
  );
}

/** key -> {environment, script, test_name, stream_id, namespace}. */
const selected = new Map();

/** Functions to call after a successful bulk action, one per mount. */
const changeListeners = [];

let nextNamespace = 0;

let barEl = null;
let countEl = null;
let userSelectEl = null;
let noteInputEl = null;
let assignBtnEl = null;
let unassignBtnEl = null;
let commentBtnEl = null;
let muteDurationEl = null;
let muteBtnEl = null;
let unmuteBtnEl = null;

function selectionEntries() {
  return Array.from(selected.values());
}

/** Build the tests[] payload POST /api/assignments/bulk expects. */
function testsPayload() {
  return selectionEntries().map((entry) => {
    const out = {
      environment: entry.environment, script: entry.script,
      test_name: entry.test_name,
    };
    if (entry.stream_id !== null && entry.stream_id !== undefined) {
      out.stream_id = entry.stream_id;
    }
    return out;
  });
}

/** Uncheck every rendered checkbox (row and header) across every mounted
 * table -- called after the shared Set is emptied, so the DOM catches
 * up with state that already changed. */
function syncCheckboxesToSelection() {
  document.querySelectorAll("input.row-select-checkbox")
    .forEach((box) => {
      const key = box.dataset.selectKey;
      box.checked = Boolean(key) && selected.has(key);
    });
  document.querySelectorAll("input.select-all-checkbox").forEach((box) => {
    updateHeaderCheckboxState(box);
  });
}

function updateHeaderCheckboxState(headerBox) {
  const table = headerBox.closest("table");
  if (!table) {
    return;
  }
  const boxes = Array.from(
    table.querySelectorAll("tbody input.row-select-checkbox"));
  const checkedCount = boxes.filter((box) => box.checked).length;
  headerBox.checked = boxes.length > 0 && checkedCount === boxes.length;
  headerBox.indeterminate =
    checkedCount > 0 && checkedCount < boxes.length;
}

function clearSelection() {
  selected.clear();
  syncCheckboxesToSelection();
  renderBar();
}

function notifyChanged() {
  for (const fn of changeListeners) {
    try {
      fn();
    } catch (err) {
      // One page's own refresh failing must not stop the others, or a
      // single broken listener would make every OTHER mounted table on
      // the page look like the bulk action itself failed.
      showError(err.message);
    }
  }
}

function updateAssignButtonState() {
  if (!assignBtnEl) {
    return;
  }
  assignBtnEl.disabled = !userSelectEl.value || selected.size === 0;
  updateCommentButtonState();
}

/** "Comment only" needs something to say and somewhere to say it. */
function updateCommentButtonState() {
  if (!commentBtnEl) {
    return;
  }
  commentBtnEl.disabled =
    noteInputEl.value.trim() === "" || selected.size === 0;
  updateMuteButtonState();
}

/** Mute needs all three: an owner (the "Assign to" box), a reason (the
 * note box -- also posted as the comment) and a duration. Gated here
 * so the button cannot be clicked into a server-side 400. Called from
 * the comment updater, which the picker's and the note's own listeners
 * both reach, and from the duration select's own change. */
function updateMuteButtonState() {
  if (!muteBtnEl) {
    return;
  }
  muteBtnEl.disabled = selected.size === 0
    || !userSelectEl.value
    || noteInputEl.value.trim() === ""
    || !muteDurationEl.value;
}

/**
 * Post the note on every selected test and change nothing else (WP-35)
 * -- for the thirty failures that share one cause. Each comment is
 * recorded as posted from wherever its row was selected (testsPayload()
 * carries that per test), so a selection made on a build's page reads
 * back on that build's own list.
 */
async function doComment() {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— comments are recorded against a name.");
    return;
  }
  const note = noteInputEl.value.trim();
  if (!note || selected.size === 0) {
    return;
  }
  commentBtnEl.disabled = true;
  try {
    await postJson(
      bulkCommentsUrl(),
      { username: me, text: note, tests: testsPayload() });
    noteInputEl.value = "";
    clearSelection();
    notifyChanged();
  } catch (err) {
    showError(err.message);
  } finally {
    updateCommentButtonState();
  }
}

/**
 * Mute every selected failure (WP-40): not counted as failing for the
 * chosen time (12 hours to 7 days), owned by someone. The owner is the
 * "Assign to" box, the reason is the note box -- the same two fields
 * Assign uses, so there is one place to type each -- and the duration
 * is the select beside the button. All three are required (the button
 * stays disabled until they are set), and the reason is also posted as
 * a comment on each test, exactly as Assign-with-a-note does.
 */
async function doMute() {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— mutes are recorded against a name.");
    return;
  }
  const owner = userSelectEl.value;
  const reason = noteInputEl.value.trim();
  const hours = Number(muteDurationEl.value);
  if (!owner || !reason || !hours || selected.size === 0) {
    return;
  }
  muteBtnEl.disabled = true;
  try {
    await postJson(bulkMutesUrl(), {
      username: me, reason: reason, hours: hours, assignee: owner,
      tests: testsPayload(),
    });
    rememberUser(owner);
    noteInputEl.value = "";
    muteDurationEl.value = "";
    clearSelection();
    notifyChanged();
  } catch (err) {
    showError(err.message);
  } finally {
    updateAssignButtonState();
  }
}

async function doAssign() {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— assignments record who made them.");
    return;
  }
  const username = userSelectEl.value;
  if (!username || selected.size === 0) {
    return;
  }
  const note = noteInputEl.value.trim();
  const body = {
    username: username, assigned_by: me, tests: testsPayload(),
  };
  if (note) {
    body.comment = note;
  }
  assignBtnEl.disabled = true;
  unassignBtnEl.disabled = true;
  try {
    await postJson(bulkAssignmentsUrl(), body);
    rememberUser(username);
    noteInputEl.value = "";
    clearSelection();
    notifyChanged();
  } catch (err) {
    showError(err.message);
  } finally {
    updateAssignButtonState();
    unassignBtnEl.disabled = selected.size === 0;
  }
}

async function doUnassign() {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— this is recorded against your name.");
    return;
  }
  if (selected.size === 0) {
    return;
  }
  assignBtnEl.disabled = true;
  unassignBtnEl.disabled = true;
  try {
    await postJson(
      bulkAssignmentsUrl(),
      { username: null, assigned_by: me, tests: testsPayload() });
    clearSelection();
    notifyChanged();
  } catch (err) {
    showError(err.message);
  } finally {
    updateAssignButtonState();
    unassignBtnEl.disabled = selected.size === 0;
  }
}

/**
 * Unmute every selected test (WP-40). Needs only a selection: the
 * server removes the mutes that exist and leaves the rest alone, so a
 * mixed selection is fine, and nothing else about the tests changes
 * (they stay assigned). Mirrors doUnassign.
 */
async function doUnmute() {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— this is recorded against your name.");
    return;
  }
  if (selected.size === 0) {
    return;
  }
  unmuteBtnEl.disabled = true;
  try {
    await postJson(unmuteUrl(), { username: me, tests: testsPayload() });
    clearSelection();
    notifyChanged();
  } catch (err) {
    showError(err.message);
  } finally {
    unmuteBtnEl.disabled = selected.size === 0;
  }
}

function ensureBar() {
  if (barEl) {
    return;
  }
  barEl = el("div", "selection-bar");
  barEl.hidden = true;

  countEl = el("span", "selection-count");
  barEl.appendChild(countEl);
  barEl.appendChild(document.createTextNode(" selected · Assign to "));

  userSelectEl = userPickerSelect("selection-user-select");
  userSelectEl.addEventListener("change", updateAssignButtonState);
  barEl.appendChild(userSelectEl);

  noteInputEl = document.createElement("input");
  noteInputEl.type = "text";
  noteInputEl.className = "selection-note-input";
  noteInputEl.placeholder = "note";
  noteInputEl.setAttribute(
    "aria-label", "Comment to post on every selected test — optional "
      + "when assigning");
  noteInputEl.addEventListener("input", updateCommentButtonState);
  barEl.appendChild(noteInputEl);

  assignBtnEl = el("button", "selection-assign-btn", "Assign");
  assignBtnEl.type = "button";
  assignBtnEl.disabled = true;
  assignBtnEl.addEventListener("click", doAssign);
  barEl.appendChild(assignBtnEl);

  barEl.appendChild(el("span", "selection-sep", "·"));

  unassignBtnEl = el("button", "selection-unassign-btn", "Unassign");
  unassignBtnEl.type = "button";
  unassignBtnEl.addEventListener("click", doUnassign);
  barEl.appendChild(unassignBtnEl);

  barEl.appendChild(el("span", "selection-sep", "·"));

  commentBtnEl = el("button", "selection-comment-btn", "Comment only");
  commentBtnEl.type = "button";
  commentBtnEl.disabled = true;
  commentBtnEl.title = "Post the note on every selected test, without "
    + "changing who any of them is assigned to";
  commentBtnEl.addEventListener("click", doComment);
  barEl.appendChild(commentBtnEl);

  barEl.appendChild(el("span", "selection-sep", "·"));

  muteDurationEl = muteDurationSelect("selection-duration-select");
  muteDurationEl.addEventListener("change", updateMuteButtonState);
  barEl.appendChild(document.createTextNode("Mute "));
  barEl.appendChild(muteDurationEl);

  muteBtnEl = el("button", "selection-mute-btn", "Mute");
  muteBtnEl.type = "button";
  muteBtnEl.disabled = true;
  muteBtnEl.title = "Stop counting the selected failures as failing for "
    + "the time chosen. Needs an owner (“Assign to”), a reason "
    + "(the note box) and a duration. The reason is also posted as a "
    + "comment; the tests are assigned to the owner.";
  muteBtnEl.addEventListener("click", doMute);
  barEl.appendChild(muteBtnEl);

  barEl.appendChild(el("span", "selection-sep", "·"));

  unmuteBtnEl = el("button", "selection-unmute-btn", "Unmute");
  unmuteBtnEl.type = "button";
  unmuteBtnEl.disabled = true;
  unmuteBtnEl.title = "Stop muting the selected tests; their failures "
    + "count as failing again. They stay assigned. Rows that were not "
    + "muted are left alone.";
  unmuteBtnEl.addEventListener("click", doUnmute);
  barEl.appendChild(unmuteBtnEl);

  barEl.appendChild(el("span", "selection-sep", "·"));

  const clearBtn = el("button", "selection-clear-btn", "Clear selection");
  clearBtn.type = "button";
  clearBtn.addEventListener("click", clearSelection);
  barEl.appendChild(clearBtn);

  document.body.appendChild(barEl);
}

function renderBar() {
  ensureBar();
  const count = selected.size;
  barEl.hidden = count === 0;
  // The bar is FIXED to the viewport bottom (the failure-nav pill's own
  // idiom, style.css), so it sits over whatever the page would
  // otherwise show there. Reserve room for it only while it is
  // actually visible -- no permanent gap on every page that never
  // shows a selection.
  document.body.classList.toggle("has-selection-bar", count > 0);
  if (count === 0) {
    return;
  }
  countEl.textContent = count.toLocaleString();
  updateAssignButtonState();
  unassignBtnEl.disabled = false;
  unmuteBtnEl.disabled = false;
}

/**
 * Mount a checkbox column onto `table`. See the module docstring for
 * the returned object's shape. `options.onChanged` (optional) is called
 * after a bulk action this mount's own rows may have taken part in
 * succeeds -- the SAME notification every OTHER mount on the page also
 * gets (a bulk action can span more than one mounted table's rows), so
 * a page refreshes whichever of its own views could have changed.
 */
export function mountSelectableTable(table, options) {
  const opts = options || {};
  const namespace = nextNamespace++;
  ensureBar();
  if (opts.onChanged) {
    changeListeners.push(opts.onChanged);
  }

  function headerCell() {
    const th = el("th", "select-col");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "select-all-checkbox";
    box.title = "Select all rows shown";
    box.addEventListener("change", () => {
      // Snapshot the intent BEFORE the loop: each row's own "change"
      // handler below calls updateHeaderCheckboxState(box) -- the SAME
      // node this closure holds -- which recomputes and overwrites
      // box.checked/indeterminate from partial progress. Comparing
      // against the live box.checked on every iteration meant only the
      // FIRST row ever actually toggled; every later one read a header
      // state the first row's own side effect had already changed.
      const target = box.checked;
      const rowBoxes = table.querySelectorAll(
        "tbody input.row-select-checkbox");
      rowBoxes.forEach((rowBox) => {
        if (rowBox.checked !== target) {
          rowBox.checked = target;
          rowBox.dispatchEvent(new Event("change"));
        }
      });
    });
    th.appendChild(box);
    return th;
  }

  function rowCell(entry) {
    const td = el("td", "select-col");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "row-select-checkbox";
    const key = entryKey(entry);
    box.dataset.selectKey = key;
    box.checked = selected.has(key);
    box.addEventListener("change", () => {
      if (box.checked) {
        selected.set(key, {
          environment: entry.environment,
          script: entry.script,
          test_name: entry.test_name,
          stream_id: entry.stream_id !== undefined
            ? entry.stream_id : null,
          namespace: namespace,
        });
      } else {
        selected.delete(key);
      }
      const headerBox = table.querySelector(
        "thead input.select-all-checkbox");
      if (headerBox) {
        updateHeaderCheckboxState(headerBox);
      }
      renderBar();
    });
    td.appendChild(box);
    return td;
  }

  function reset() {
    let changed = false;
    for (const [key, value] of selected) {
      if (value.namespace === namespace) {
        selected.delete(key);
        changed = true;
      }
    }
    if (changed) {
      renderBar();
    }
  }

  return { headerCell: headerCell, rowCell: rowCell, reset: reset };
}
