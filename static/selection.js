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
  postJson,
  rememberUser,
  requireUsername,
  showError,
  userPickerSelect,
} from "./api.js";
import { entryKey } from "./review.js";
import { apiUrl } from "./urls.js";

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
    "/api/assignments/bulk", null,
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
  noteInputEl.placeholder = "note (optional)";
  noteInputEl.setAttribute(
    "aria-label", "Optional comment to post on every selected test");
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
