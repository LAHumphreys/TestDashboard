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
  muteDetail,
  muteDurationSelect,
  assigneeSelect,
  clearNode,
  FLAKY_CADENCE_TITLE,
  FLAKY_REASON_LABEL,
  FLAKY_REASON_PLACEHOLDER,
  FLAKY_UNMARK_TITLE,
  cadenceChanged,
  copyOutputButton,
  el,
  fetchJson,
  fillOutput,
  flakyCadenceSelect,
  flakyDetail,
  flakySentence,
  getUsername,
  runStrip,
  postJson,
  preselectUser,
  putJson,
  requireUsername,
  showError,
  testApiPath,
  userPickerSelect,
} from "./api.js";
import {
  bulkFlakyUrl,
  bulkMutesUrl,
  unmarkFlakyUrl,
  unmuteUrl,
  pageUrl,
} from "./urls.js";

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
 *   onMuted     — called after a mute or a flaky mark changes (WP-40,
 *                 WP-44). Both move a test between queues and counts,
 *                 so a caller reloads; without it, onChanged is called.
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
  // F2 (docs/STREAMS_PLAN.md §3.6/§0.9): a row read from a branch's own
  // dashboard tab (app.js's tagStream() stamps entry.stream_id on
  // exactly those rows — undefined/absent everywhere else, including
  // every Open Actions row, since that is a DIFFERENT concept there,
  // assignment_stream_id) must open BOTH links on that stream, not
  // mainline — "View in timeline" from a branch delta row used to
  // deep-link to the mainline timeline, where that run does not exist
  // at all (needs F7: timeline.js could not read `stream=` before it).
  // A TRUTHY check, not `!== null`: undefined (the common case) must
  // also skip the stream scope. This panel cannot know whose page it
  // is on (see the module docstring), so BOTH links pass an entirely
  // EXPLICIT scope — never pageUrl()'s default carriage, which reads
  // whatever page happens to be hosting the panel.
  const streamScope = entry.stream_id || null;
  const full = document.createElement("a");
  full.href = pageUrl("test", {
    environment: entry.environment, script: entry.script,
    test_name: entry.test_name,
  }, { stream: streamScope, product: null, baseline: null });
  full.textContent = "Open full test page →";
  // The poisoned-data workflow's door: this run, in its night's running
  // order, with everything that ran before it listed above. `at` is the
  // run's start time, so the Timeline opens the run that CONTAINS this
  // result rather than whatever ran most recently.
  const inTimeline = document.createElement("a");
  inTimeline.href = pageUrl("timeline", {
    environment: entry.environment, script: entry.script,
    test: entry.test_name, at: entry.start_time,
  }, { stream: streamScope, product: null, baseline: null });
  inTimeline.textContent = "View in timeline →";
  inTimeline.title = "This run in its night's running order — what ran "
    + "before it is listed above it";
  let output = null;
  const copy = copyOutputButton(() => output);
  head.appendChild(el("span", "review-title", "Latest run output"));
  head.appendChild(inTimeline);
  head.appendChild(full);
  head.appendChild(copy);
  panel.appendChild(head);

  // Actions FIRST. The output block is tall, so anything below it is
  // off-screen for a real failure — which is how "I can't comment from
  // triage" happens even though the box was there all along.
  panel.appendChild(buildReviewActions(entry, opts));

  const pre = el("pre", "review-output", "Loading output…");
  panel.appendChild(pre);

  try {
    const run = await fetchJson("api/runs/" + entry.run_id);
    const truncated = fillOutput(pre, run.output);
    output = run.output;
    copy.disabled = !output;
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

/** This one test, in the shape the mute and flaky endpoints take. */
function actTests(entry) {
  const test = {
    environment: entry.environment, script: entry.script,
    test_name: entry.test_name,
  };
  if (entry.stream_id) {
    test.stream_id = entry.stream_id;
  }
  return [test];
}

/** After a mute or a flaky act: the caller's reload (see toggleReview's
 * `onMuted`), else its onChanged, told which `kind` of act it was. The
 * comparison page's panel has only onChanged, and reads `change.kind`. */
function afterStandingChange(opts, kind) {
  if (opts.onMuted) {
    opts.onMuted();
  } else if (opts.onChanged) {
    opts.onChanged({ kind: kind, value: null });
  }
}

/** The username to record an act against, or null after saying why. */
function actor(recorded) {
  const me = requireUsername();
  if (!me) {
    showError(
      "Set a username first (the “Change” button, top right) "
      + "— " + recorded + " are recorded against a name.");
  }
  return me;
}

/**
 * The Mute group (WP-40), for a FAILING test or one with a live mute:
 * muting is a statement about a failure. Already muted -> who, until
 * when, why, with a duration (7 days preselected), "Extend" and
 * "Unmute"; otherwise a reason, a duration and an owner. Durations come
 * from api.js's muteDurationSelect(), in hours. Everything arrives via
 * entry/opts.
 */
function buildMuteGroup(entry, opts) {
  const group = el("div", "review-group review-group-wide");
  group.appendChild(el("label", "review-label", "Mute"));
  const done = () => afterStandingChange(opts, "muted");
  const needUser = () => actor("mutes");
  const mute = entry.mute;
  if (mute && mute.live) {
    group.appendChild(el("span", "mute-line",
      "Muted " + muteDetail(mute, true)));
    const extendFor = muteDurationSelect("review-duration-select", 168);
    const extend = el("button", "", "Extend");
    extend.type = "button";
    const unmute = el("button", "", "Unmute");
    unmute.type = "button";
    unmute.title = "Stop muting this failure; it counts as "
      + "failing again. It stays assigned.";
    extend.addEventListener("click", async () => {
      const me = needUser();
      if (!me) {
        return;
      }
      const hours = Number(extendFor.value);
      if (!hours) {
        extendFor.focus();
        showError("Choose how long to extend the mute for.");
        return;
      }
      extend.disabled = true;
      unmute.disabled = true;
      try {
        await postJson(bulkMutesUrl(), {
          username: me, hours: hours, assignee: entry.assignee || me,
          tests: actTests(entry),
        });
        done();
      } catch (err) {
        showError(err.message);
        extend.disabled = false;
        unmute.disabled = false;
      }
    });
    unmute.addEventListener("click", async () => {
      const me = needUser();
      if (!me) {
        return;
      }
      extend.disabled = true;
      unmute.disabled = true;
      try {
        await postJson(unmuteUrl(), {
          username: me, tests: actTests(entry),
        });
        done();
      } catch (err) {
        showError(err.message);
        extend.disabled = false;
        unmute.disabled = false;
      }
    });
    group.appendChild(extendFor);
    group.appendChild(extend);
    group.appendChild(unmute);
    return group;
  }

  const reason = document.createElement("input");
  reason.type = "text";
  reason.className = "review-input";
  reason.placeholder = "Why is this failure muted? (required)";
  const duration = muteDurationSelect("review-duration-select");
  const owner = userPickerSelect("review-owner-select");
  owner.title = "Who owns this failure while it is muted";
  preselectUser(owner, entry.assignee || getUsername());
  const go = el("button", "", "Mute");
  go.type = "button";
  go.title = "Stop counting this failure as failing for the time chosen "
    + "(up to 7 days). It is assigned to the owner chosen, listed as "
    + "muted, and your reason is posted as a comment.";
  go.addEventListener("click", async () => {
    const me = needUser();
    if (!me) {
      return;
    }
    const text = reason.value.trim();
    if (!text) {
      reason.focus();
      showError("Say why this failure is muted — the reason is "
        + "kept with it.");
      return;
    }
    const hours = Number(duration.value);
    if (!hours) {
      duration.focus();
      showError("Choose how long to mute this failure for.");
      return;
    }
    go.disabled = true;
    try {
      await postJson(bulkMutesUrl(), {
        username: me, reason: text, hours: hours,
        assignee: owner.value || me, tests: actTests(entry),
      });
      done();
    } catch (err) {
      showError(err.message);
      go.disabled = false;
    }
  });
  group.appendChild(reason);
  group.appendChild(duration);
  group.appendChild(owner);
  group.appendChild(go);
  return group;
}

/**
 * The Flaky group (WP-44), for any test: a mark is a standing
 * statement about the test, not about one failure. Marked -> the
 * sentence saying where it stands, who marked it, when and why, the
 * cadence select on its own cadence with "Set cadence", and "Unmark";
 * otherwise a reason, a cadence and "Mark as flaky". The person acting
 * owns the mark (decision 12), so no owner is picked here. Reads
 * `entry.flaky` defensively: absent or null is an unmarked test.
 */
function buildFlakyGroup(entry, opts) {
  const group = el("div", "review-group review-group-wide");
  group.appendChild(el("label", "review-label", "Flaky"));
  const flaky = entry.flaky || null;
  // `derive` sets every button from the group's state, `inFlight`
  // included, so nothing (a cadence change, a failed request) re-arms a
  // button while a request is out or one that has nothing to send.
  let inFlight = false;
  let derive = () => {};
  const send = async (url, body) => {
    if (inFlight) {
      return;
    }
    const me = actor("flaky marks");
    if (!me) {
      return;
    }
    body.username = me;
    body.tests = actTests(entry);
    inFlight = true;
    derive();
    try {
      await postJson(url, body);
      afterStandingChange(opts, "flaky");
    } catch (err) {
      showError(err.message);
    } finally {
      inFlight = false;
      derive();
    }
  };

  if (flaky) {
    const muteLive = Boolean(entry.mute && entry.mute.live);
    // The sentence, with who, when and why under it (a block sub-line).
    const line = el("span", "mute-line");
    line.appendChild(document.createTextNode(flakySentence(flaky, muteLive)));
    line.appendChild(el("span", "row-sub", flakyDetail(flaky)));
    group.appendChild(line);
    const cadence = flakyCadenceSelect("review-cadence-select", flaky.cadence);
    const change = el("button", "", "Set cadence");
    change.type = "button";
    change.title = FLAKY_CADENCE_TITLE;
    const unmark = el("button", "", "Unmark");
    unmark.type = "button";
    unmark.title = FLAKY_UNMARK_TITLE;
    // Nothing to send until the cadence differs from the mark's own.
    derive = () => {
      change.disabled = inFlight || !cadenceChanged(cadence, flaky);
      unmark.disabled = inFlight;
    };
    derive();
    cadence.addEventListener("change", derive);
    change.addEventListener("click", () => send(
      bulkFlakyUrl(), { cadence: Number(cadence.value) }));
    unmark.addEventListener("click", () => send(unmarkFlakyUrl(), {}));
    group.appendChild(cadence);
    group.appendChild(change);
    group.appendChild(unmark);
    return group;
  }

  const reason = document.createElement("input");
  reason.type = "text";
  reason.className = "review-input";
  reason.placeholder = FLAKY_REASON_PLACEHOLDER;
  reason.setAttribute("aria-label", FLAKY_REASON_LABEL);
  const cadence = flakyCadenceSelect("review-cadence-select");
  const mark = el("button", "", "Mark as flaky");
  mark.type = "button";
  mark.title = "Tolerate this test's failures until they reach the "
    + "cadence chosen, on mainline and every build, until the mark is "
    + "removed. You own the mark, and your reason is posted as a comment.";
  mark.addEventListener("click", () => {
    const text = reason.value.trim();
    if (!text) {
      reason.focus();
      showError("Say why this test is flaky — the reason is kept with "
        + "the mark.");
      return;
    }
    const chosen = Number(cadence.value);
    if (!chosen) {
      cadence.focus();
      showError("Choose how many failures in a row count as failing.");
      return;
    }
    send(bulkFlakyUrl(), { reason: text, cadence: chosen });
  });
  derive = () => { mark.disabled = inFlight; };
  group.appendChild(reason);
  group.appendChild(cadence);
  group.appendChild(mark);
  return group;
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

  /* --- what it has been doing lately --- */
  // The strip lives in the panel rather than the row: rows are already
  // dense, and item 4 exists precisely because one got visually noisy.
  if (opts.stability && opts.stability.runs) {
    const historyGroup = el("div", "review-group review-group-wide");
    historyGroup.appendChild(el("label", "review-label",
      "Last " + opts.stability.runs + " runs (oldest first)"));
    historyGroup.appendChild(runStrip(opts.stability));
    actions.appendChild(historyGroup);
  }

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
      // WP-35: say where this comment was posted from, the way the
      // assignee picker beside it always has. entry.stream_id is set on
      // exactly the rows that were read from a build's own page (see
      // the link scope above); until this, a comment typed here on a
      // build's row was recorded as posted from nowhere -- only the
      // test page's own comment box tagged it.
      const body = { username: me, text: text };
      if (entry.stream_id) {
        body.stream_id = entry.stream_id;
      }
      const posted = await postJson(
        testApiPath(entry.environment, entry.script, entry.test_name,
          "/comments"),
        body);
      input.value = "";
      post.textContent = "Posted";
      window.setTimeout(() => { post.textContent = "Post"; }, 1500);
      changed({
        kind: "commented",
        value: {
          author: me, text: text,
          created_at: posted && posted.comment
            ? posted.comment.created_at : null,
        },
      });
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

  /* --- mute: a failing test, or any test whose mute is still live
     (the Muted tab lists passing ones too, and they need Extend and
     Unmute) --- */
  if (entry.result === "FAIL" || (entry.mute && entry.mute.live)) {
    actions.appendChild(buildMuteGroup(entry, opts));
  }

  /* --- flaky: any test, marked or not (WP-44) --- */
  actions.appendChild(buildFlakyGroup(entry, opts));

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
