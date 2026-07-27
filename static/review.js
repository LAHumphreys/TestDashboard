/* review.js — the inline review panel, shared by every list of tests.
 *
 * Expands under a table row to show the latest run's output and offer the
 * three things somebody triaging actually does: assign it, say what they
 * found, and mark a test that has stopped reporting as gone. The point is
 * that none of it requires leaving the list — a queue is worked down, and
 * a round trip to a detail page per row is how a queue stops being worked
 * down.
 *
 * This started inside app.js, wired directly to the home screen's state
 * object. It lives here because the open-actions page needs the same
 * panel, and the alternative — a second copy — is a second set of bugs.
 *
 * The coupling is broken by INJECTION, not by duplication: the panel
 * cannot see any page's state, so everything page-specific arrives in
 * `options`. In particular it does not ask anyone whether a test is
 * stale; it is told the cutoff and works it out.
 *
 * SECURITY: all dynamic data reaches the DOM via textContent. Nothing
 * here may interpolate data into innerHTML.
 */

"use strict";

import {
  assigneeSelect,
  clearNode,
  el,
  fetchJson,
  fillOutput,
  postJson,
  putJson,
  requireUsername,
  showError,
  testApiPath,
} from "./api.js";

/*
 * Which panels are open, keyed by test identity.
 *
 * Joined with \0 rather than a space or a slash: environment, script and
 * test name are user-supplied and a separator that can occur inside one
 * of them makes two different tests collide on one key.
 */
const openPanels = new Set();

/** Key identifying a row's expanded state. */
export function entryKey(entry) {
  return [entry.environment, entry.script, entry.test_name].join("\0");
}

/**
 * Re-open this row's panel if it was open before the table was rebuilt.
 *
 * A list re-renders after an assignment, so without this the panel a
 * person was reading collapses the moment they act on it. The registry
 * stays private to this module: callers say "restore whatever was
 * open", not "here is my bookkeeping".
 */
export function reopenIfOpen(entry, row, button, options) {
  const key = entryKey(entry);
  if (!openPanels.has(key)) {
    return;
  }
  openPanels.delete(key);          // toggleReview will put it back
  toggleReview(entry, row, button, options);
}

/**
 * Open or close the review panel under `row`.
 *
 * `options`:
 *   staleBefore — ISO timestamp; a test whose latest run started before
 *                 this has stopped reporting, and is offered retirement.
 *                 Omit (or null) to never offer it.
 *   onChanged   — called after an assignment or a comment, with a
 *                 {kind, value} describing what happened. The caller
 *                 decides whether that means refreshing counts, patching
 *                 the row, or nothing. It is told WHAT changed so it can
 *                 update a row in place instead of refetching a page —
 *                 refetching closes every open panel, which on a queue
 *                 being worked down is the wrong behaviour.
 *   onRetired   — called after a successful retirement. Retirement
 *                 removes the test from every estate view, so a caller
 *                 usually has to reload rather than patch a row.
 */
export function toggleReview(entry, row, button, options) {
  const opts = options || {};
  const key = entryKey(entry);
  const existing = row.nextSibling;
  if (existing && existing.dataset && existing.dataset.reviewFor === key) {
    existing.remove();
    openPanels.delete(key);
    button.setAttribute("aria-expanded", "false");
    button.textContent = "Review";
    return;
  }
  openPanels.add(key);
  button.setAttribute("aria-expanded", "true");
  button.textContent = "Close";
  const panelRow = document.createElement("tr");
  panelRow.className = "review-row";
  panelRow.dataset.reviewFor = key;
  const cell = document.createElement("td");
  cell.colSpan = row.children.length;
  panelRow.appendChild(cell);
  row.parentNode.insertBefore(panelRow, row.nextSibling);
  buildReviewPanel(entry, cell, opts);
}

/** True when a test has not reported since `staleBefore`. */
function isStale(entry, staleBefore) {
  if (!staleBefore) {
    return false;
  }
  return new Date(entry.start_time + "Z") < new Date(staleBefore + "Z");
}

async function buildReviewPanel(entry, container, opts) {
  clearNode(container);
  const panel = el("div", "review-panel");
  container.appendChild(panel);

  const head = el("div", "review-head");
  const params = new URLSearchParams();
  params.append("environment", entry.environment);
  params.append("script", entry.script);
  params.append("test_name", entry.test_name);
  const full = document.createElement("a");
  full.href = "test.html?" + params.toString();
  full.textContent = "Open full test page →";
  head.appendChild(el("span", "review-title", "Latest run output"));
  head.appendChild(full);
  panel.appendChild(head);

  // Actions FIRST. The output block is tall, so anything below it is
  // off-screen for a real failure — which is how "I can't comment from
  // triage" happens even though the box was there all along.
  panel.appendChild(buildReviewActions(entry, opts));

  const pre = el("pre", "review-output", "Loading output…");
  panel.appendChild(pre);

  try {
    const run = await fetchJson("/api/runs/" + entry.run_id);
    const truncated = fillOutput(pre, run.output);
    if (truncated) {
      pre.parentNode.insertBefore(
        el("p", "review-note",
          truncated + " Open the full test page for all of it."),
        pre);
    }
  } catch (err) {
    pre.textContent = "Could not load the output: " + err.message;
  }
}

function buildReviewActions(entry, opts) {
  const actions = el("div", "review-actions");
  const changed = opts.onChanged || (() => {});

  /* --- assign to anyone --- */
  const assignGroup = el("div", "review-group");
  assignGroup.appendChild(el("label", "review-label", "Assign to"));
  assignGroup.appendChild(assigneeSelect(
    entry, (name) => changed({ kind: "assigned", value: name })));
  actions.appendChild(assignGroup);

  /* --- comment --- */
  const commentGroup = el("div", "review-group review-group-wide");
  commentGroup.appendChild(el("label", "review-label", "Add a comment"));
  const input = document.createElement("input");
  input.type = "text";
  input.className = "review-input";
  input.placeholder = "What did you find?";
  const post = el("button", "", "Post");
  post.type = "button";
  const submit = async () => {
    const me = requireUsername();
    if (!me) {
      showError(
        "Set a username first (the “Change” button, top right) "
        + "— comments are recorded against a name.");
      return;
    }
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    post.disabled = true;
    try {
      await postJson(
        testApiPath(entry.environment, entry.script, entry.test_name,
          "/comments"),
        { username: me, text: text });
      input.value = "";
      post.textContent = "Posted";
      window.setTimeout(() => { post.textContent = "Post"; }, 1500);
      changed({ kind: "commented", value: { author: me, text: text } });
    } catch (err) {
      showError(err.message);
    } finally {
      post.disabled = false;
    }
  };
  post.addEventListener("click", submit);
  // Enter posts. Typing a sentence and pressing Enter is what people
  // do; without this the keypress does nothing and the comment is lost
  // the moment the panel closes.
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });
  commentGroup.appendChild(input);
  commentGroup.appendChild(post);
  actions.appendChild(commentGroup);

  /* --- retire: only offered where it makes sense --- */
  // Offered for any test that has stopped reporting, wherever it is
  // being looked at.
  if (isStale(entry, opts.staleBefore) && !entry.retired_at) {
    const retireGroup = el("div", "review-group review-group-wide");
    retireGroup.appendChild(el("label", "review-label",
      "This test has stopped reporting"));
    const why = document.createElement("input");
    why.type = "text";
    why.className = "review-input";
    why.placeholder = "Why is it gone? (required — e.g. deleted in 4.2)";
    const retire = el("button", "danger-btn",
      "Mark as no longer in the suite");
    retire.type = "button";
    retire.addEventListener("click", async () => {
      const me = requireUsername();
      if (!me) {
        // Silently doing nothing here read as a broken button.
        showError(
          "Set a username first (the “Change” button, top "
          + "right) — retirements record who approved them.");
        return;
      }
      if (!why.value.trim()) {
        why.focus();
        showError("Say why the test is gone — the note is kept with it.");
        return;
      }
      retire.disabled = true;
      try {
        await putJson(
          testApiPath(entry.environment, entry.script, entry.test_name,
            "/retired"),
          { retired: true, username: me, comment: why.value.trim() });
        if (opts.onRetired) {
          await opts.onRetired();
        } else {
          changed();
        }
      } catch (err) {
        showError(err.message);
        retire.disabled = false;
      }
    });
    retireGroup.appendChild(why);
    retireGroup.appendChild(retire);
    actions.appendChild(retireGroup);
  }
  return actions;
}
