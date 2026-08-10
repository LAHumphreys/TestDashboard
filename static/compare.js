/* compare.js — a stream vs. mainline, everywhere it is shown (WP-21,
 * docs/STREAMS_PLAN.md §3.5/§3.6).
 *
 * Two callers, one source of truth for the shape of a comparison:
 *
 *   - app.js: the dashboard's delta view. Scoping to a branch (`?stream=`)
 *     SWAPS the whole dashboard body for this — the status tiles, charts,
 *     triage queues and browse table are all mainline-only concepts and
 *     hide outright, per docs/STREAMS_PLAN.md §3.6. On a page with no
 *     `stream=` param this module is never called at all, which is what
 *     keeps mainline pages at ZERO visible change (the same rule
 *     products.js follows for a single-product deployment).
 *   - test.js: the test-detail compare strip, one triple's mainline result
 *     next to its branch result.
 *
 * "Mainline" is always drawn with ghostChip (outlined) and "this branch"
 * with resultChip (solid) — the same visual language api.js's
 * resultTransition uses for "was -> now", reused here for "elsewhere ->
 * here". A side with no result is NEVER a chip of any kind (text only,
 * "no result") — a colour standing in for absence is exactly the defect
 * ResultEmphasisTest exists to catch, extended to a comparison instead of
 * a transition.
 *
 * The selected stream lives ONLY in `?stream=<id>` — never localStorage.
 * Unlike the product switcher, a branch is something you are looking AT
 * right now, not a standing preference, and it follows the Watchlist's
 * "the URL is the whole configuration" rule (docs/STREAMS_PLAN.md §0.9):
 * a link to a branch-scoped dashboard has to reopen scoped to that branch
 * for anyone, with no browser state involved.
 */

"use strict";

import {
  assigneeSelect,
  clearNode,
  el,
  fetchJson,
  ghostChip,
  resultChip,
  showError,
} from "./api.js";
import { reopenIfOpen, toggleReview } from "./review.js";
import { mountSelectableTable } from "./selection.js";
import { apiUrl, pageUrl, withBaseline, withStream } from "./urls.js";

/** The five paginable comparison categories, in tab/tile display order. */
export const CATEGORY_ORDER = [
  "new_failures", "new_passes", "both_failing", "new_tests", "no_result",
];

/** Human labels, shared by the delta view's tiles/tabs and the
 * Watchlist's stream verdict cards — the same word never means two
 * different things on two surfaces. */
export const CATEGORY_LABELS = {
  new_failures: "New failures",
  new_passes: "New passes",
  both_failing: "Both failing",
  new_tests: "New tests",
  no_result: "No result",
};

const PAGE_LIMIT = 100;

/**
 * The stream this page is scoped to, from `?stream=<id>` — null for
 * mainline. The one place this is read from; every caller goes through
 * this function rather than parsing the query string itself.
 */
export function getSelectedStreamId() {
  const raw = new URLSearchParams(window.location.search).get("stream");
  if (!raw) {
    return null;
  }
  const id = parseInt(raw, 10);
  return Number.isNaN(id) ? null : id;
}

/**
 * The baseline this page was explicitly opened with, from `?baseline=<id>`
 * — null means "no explicit choice", which IS mainline: WP-25
 * (docs/ONE_KIND_PLAN.md §1.3) deleted the WP-22 client-side default-pick
 * (a build-scoped page no longer auto-selects its predecessor build) —
 * the default baseline is mainline, always; the Compare-to control is how
 * a predecessor is chosen now. Follows the same "the URL is the whole
 * configuration" rule getSelectedStreamId() does.
 */
export function getSelectedBaselineId() {
  const raw = new URLSearchParams(window.location.search).get("baseline");
  if (!raw) {
    return null;
  }
  const id = parseInt(raw, 10);
  return Number.isNaN(id) ? null : id;
}

/**
 * "mainline" or "build 2026.9.1" / "branch feat/x" — the one place this
 * wording is built (WP-22, docs/STREAMS_PLAN.md §4.1). Before this drop
 * every comparison's OTHER side was always mainline, so the word
 * "mainline" was hardcoded in several places; now the baseline can be
 * any stream, and every caller must read its label from the identity the
 * server actually returned rather than assume.
 */
export function streamLabel(meta) {
  return meta.kind === "mainline" ? "mainline" : meta.kind + " " + meta.name;
}

/** "...T12:34:56.123456" (naive UTC, no zone) -> a real Date, the same
 * "append Z" idiom app.js's nightsBetween() uses for the same reason:
 * without it, an unqualified ISO date-TIME string is parsed as local
 * time, not UTC. */
function parseUtc(iso) {
  return new Date(iso + "Z");
}

/**
 * "3 days ago" / "1 hour ago" / "just now", from a real timestamp and
 * the caller's own `nowMs` — always derived from the two actual values
 * involved, never from a fixed window constant. This is the same
 * discipline WindowWordingTest pins for every other page's recency
 * wording (docs/STREAMS_PLAN.md §3.5: "let the UI phrase it from data").
 */
export function ageText(iso, nowMs) {
  if (!iso) {
    return "never";
  }
  const ms = nowMs - parseUtc(iso).getTime();
  if (ms < 90 * 1000) {
    return "just now";
  }
  const minutes = Math.floor(ms / 60000);
  if (minutes < 60) {
    return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return hours + (hours === 1 ? " hour ago" : " hours ago");
  }
  const days = Math.floor(hours / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

/**
 * GET /api/compare — counts alone (no category) or one paginated page.
 *
 * *baselineId*, added WP-22 (docs/STREAMS_PLAN.md §4.1), is omitted
 * (server default: mainline) when null/undefined — every call site from
 * before this drop keeps working unchanged.
 */
export async function fetchCompare(streamId, category, offset, baselineId) {
  const params = {};
  if (category) {
    params.category = category;
    params.limit = PAGE_LIMIT;
    params.offset = offset || 0;
  }
  return fetchJson(apiUrl("api/compare", params, {
    stream: streamId,
    baseline: baselineId === undefined ? null : baselineId,
  }));
}

/** Every test compared, including the ones that agree — counts.agree is
 * a real field precisely so this total does not have to be re-derived
 * from a category fetch nobody asked for. */
export function totalCompared(counts) {
  let total = counts.agree;
  for (const key of CATEGORY_ORDER) {
    total += counts[key];
  }
  return total;
}

/* ================= result cells ================= */

function noResultCell() {
  return el("span", "chip-none muted", "no result");
}

/** Mainline's side of a comparison row/strip — outlined, never solid. */
export function mainlineCell(result) {
  return result ? ghostChip(result) : noResultCell();
}

/** This stream's side of a comparison row/strip — solid, the current
 * fact, drawn last so it is the loudest thing and the thing the eye
 * finishes on (the same ordering resultTransition uses). */
export function streamCell(result) {
  return result ? resultChip(result) : noResultCell();
}

/* ================= tiles ================= */

function buildTile(label, value) {
  const tile = el("div", "tile");
  tile.appendChild(el("span", "tile-value", String(value)));
  tile.appendChild(el("span", "tile-label", label));
  return tile;
}

/** The five headline tiles, in CATEGORY_ORDER. */
export function renderTiles(container, counts) {
  clearNode(container);
  for (const key of CATEGORY_ORDER) {
    container.appendChild(buildTile(CATEGORY_LABELS[key], counts[key]));
  }
}

/* ================= the paginated category table ================= */

/** Review-panel options for a delta row (WP-21, docs/STREAMS_PLAN.md
 * §0.4/§3.6's "triage still works from a branch").
 *
 * `staleBefore` is deliberately OMITTED (not null — genuinely absent):
 * review.js's own retire gate is `isStale(entry, opts.staleBefore) &&
 * !entry.retired_at`, and isStale() returns false outright when
 * staleBefore is falsy, before it even looks at entry.retired_at.
 * Retirement is a MAINLINE decision (§3.4) — a branch's own staleness
 * says nothing about whether the test is still in the suite — so this
 * is how the shared panel is told "never offer it here" without
 * review.js having to know whose page it is on (it explicitly cannot;
 * see its module docstring).
 */
function deltaReviewOptions() {
  return {};
}

/**
 * One delta-table row's entry for the shared review panel/assignee
 * select: enough of the TestSummaryRow/TestStatusRow shape those expect
 * to work unmodified, sourced from a CompareRow instead of a dashboard
 * row. `stream_id` (WP-21) is this page's own scope, so an assignment
 * made from here is annotated with where it came from.
 */
function reviewEntry(row, streamId) {
  return {
    environment: row.environment,
    script: row.script,
    test_name: row.test_name,
    run_id: row.stream_run_id,
    start_time: row.stream_start_time,
    assignee: row.assignee,
    stream_id: streamId,
  };
}

function buildDeltaRow(row) {
  const tr = document.createElement("tr");
  // Built below (entry) before the selection checkbox so both share the
  // SAME object -- entry.stream_id is this page's own scope, carried as
  // this row's selection origin exactly as it is the assignee picker's.
  const streamId = getSelectedStreamId();
  const entry = reviewEntry(row, streamId);
  tr.appendChild(deltaSelectionMount.rowCell(entry));

  const testCell = el("td", "wrap");
  const link = document.createElement("a");
  // Carry the page's stream scope into the link: the whole point of
  // clicking a delta row is reading THIS branch's history/output of the
  // test, and test.html only shows that when ?stream= arrives with it.
  // Without this line the stream-scoped test page is unreachable by
  // clicking — found by the first human to use the branch dashboard.
  // `stream` is named explicitly (this page's own, the same value
  // getSelectedStreamId() reads) rather than left to pageUrl()'s default
  // carriage: naming `product` below resets the levels it contains,
  // stream included, unless this same overrides object also names them
  // (coordinator fix round -- this site computed streamId but never
  // passed it, silently dropping `stream=`). `product` and `baseline`
  // are still explicitly nulled because this link never carried either
  // (test.html has no product concept, and a baseline belongs to the
  // scope it was chosen in, not to a linked-to test page).
  link.href = pageUrl("test", {
    environment: row.environment, script: row.script, test_name: row.test_name,
  }, { product: null, baseline: null, stream: streamId });
  link.textContent = row.test_name;
  testCell.appendChild(link);
  testCell.appendChild(el("span", "row-sub",
    row.environment + " · " + row.script));
  tr.appendChild(testCell);

  const mainlineTd = document.createElement("td");
  mainlineTd.appendChild(mainlineCell(row.baseline_result));
  tr.appendChild(mainlineTd);

  const streamTd = document.createElement("td");
  streamTd.appendChild(streamCell(row.stream_result));
  tr.appendChild(streamTd);

  // Triage from a branch (docs/STREAMS_PLAN.md §0.4/§3.6): the SAME
  // assignee select the dashboard's own queue rows use, so a failure
  // found on a branch can be taken/assigned exactly like a mainline one
  // — assignment is never partitioned by stream, only annotated with
  // where it was made (see reviewEntry(), built above with the
  // selection checkbox).
  const assigneeTd = el("td", "assignee-cell");
  assigneeTd.appendChild(assigneeSelect(entry, () => {}));
  tr.appendChild(assigneeTd);

  // The Review expander needs a run to show — a no_result row (present
  // on mainline, absent on this branch) has none, and shows no button
  // at all rather than one that opens onto nothing.
  const outputTd = el("td", "review-cell");
  if (row.stream_run_id !== null) {
    const reviewBtn = el("button", "review-btn", "Review");
    reviewBtn.type = "button";
    reviewBtn.setAttribute("aria-expanded", "false");
    reviewBtn.title = "Show this run's output, and assign it";
    reviewBtn.addEventListener("click", () => toggleReview(
      entry, tr, reviewBtn, deltaReviewOptions()));
    outputTd.appendChild(reviewBtn);
    // Keep the panel open across the re-render a category switch or
    // "Show more" triggers — the same rule app.js's queue table follows.
    reopenIfOpen(entry, tr, reviewBtn, deltaReviewOptions());
  }
  tr.appendChild(outputTd);

  return tr;
}

/* ================= orchestration (the dashboard delta view) ================= */

// Lazily mounted (2026-08-10, multi-select), not at module load: this
// module is also imported by actions.js (for renderCompareStrip), and
// actions.html has no #delta-table at all -- mounting only once
// initDeltaView() actually runs keeps that page untouched by this
// feature. Each selected row carries THIS stream's own id as origin
// (reviewEntry() already builds that shape for the assignee picker
// beside it) -- see selection.js's own module docstring for why that
// needs no extra plumbing here.
let deltaSelectionMount = null;

function ensureDeltaSelectionMounted() {
  if (deltaSelectionMount) {
    return;
  }
  deltaSelectionMount = mountSelectableTable(
    document.getElementById("delta-table"),
    { onChanged: () => loadCategory(true) });
  const headRow = document.querySelector("#delta-table thead tr");
  headRow.insertBefore(
    deltaSelectionMount.headerCell(), headRow.firstChild);
}

const deltaState = {
  streamId: null,
  // null = mainline (the server's own default) OR "no explicit choice
  // yet" during the initial build-predecessor lookup — see
  // initDeltaView(). Once set, always the ACTUAL baseline id in use.
  baselineId: null,
  category: CATEGORY_ORDER[0],
  offset: 0,
  total: 0,
};

async function loadCategory(reset) {
  const body = document.getElementById("delta-body");
  const empty = document.getElementById("delta-empty");
  const moreBtn = document.getElementById("delta-show-more");
  if (reset) {
    deltaState.offset = 0;
    clearNode(body);
    // A fresh render (category switch, initial load, or the reload
    // button) is a NEW view; "Show more" (reset=false) joins the SAME
    // view and must not clear it -- see selection.js's own docstring.
    ensureDeltaSelectionMounted();
    deltaSelectionMount.reset();
  }
  const page = await fetchCompare(
    deltaState.streamId, deltaState.category, deltaState.offset,
    deltaState.baselineId);
  deltaState.total = page.total;
  for (const row of page.tests) {
    body.appendChild(buildDeltaRow(row));
  }
  deltaState.offset += page.tests.length;
  empty.hidden = body.children.length !== 0;
  if (empty.hidden === false) {
    empty.textContent = "No tests in "
      + CATEGORY_LABELS[deltaState.category].toLowerCase() + ".";
  }
  moreBtn.hidden = deltaState.offset >= deltaState.total;
}

function renderTabs() {
  const tabs = document.getElementById("delta-tabs");
  clearNode(tabs);
  for (const key of CATEGORY_ORDER) {
    const btn = el("button", "tab", CATEGORY_LABELS[key]);
    btn.type = "button";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected",
      key === deltaState.category ? "true" : "false");
    btn.addEventListener("click", () => {
      if (deltaState.category === key) {
        return;
      }
      deltaState.category = key;
      renderTabs();
      loadCategory(true).catch((err) => showError(err.message));
    });
    tabs.appendChild(btn);
  }
}

/**
 * All wording here is built from streamMeta's / baselineMeta's own
 * kind/name — never the literal word "mainline" hardcoded for the OTHER
 * side (WP-22, docs/STREAMS_PLAN.md §4.1: a build's baseline can be any
 * other stream once the Compare-to control names one explicitly). The
 * heading and the two column headers are set here too so the whole
 * section reads consistently, not just the prose lines.
 */
function renderBaselineCard(streamMeta, baselineMeta, counts, nowMs) {
  const baseline = streamLabel(baselineMeta);
  const streamNoun = streamMeta.kind === "mainline" ? "stream" : streamMeta.kind;

  document.getElementById("delta-heading").textContent =
    "Compare to " + baseline;
  document.getElementById("delta-col-baseline").textContent =
    baselineMeta.kind === "mainline" ? "Mainline" : baseline;
  document.getElementById("delta-col-stream").textContent =
    "This " + streamNoun;

  document.getElementById("delta-agree").textContent =
    counts.agree + " test" + (counts.agree === 1 ? "" : "s")
    + " agree and " + (counts.agree === 1 ? "is" : "are") + " not listed.";

  const total = totalCompared(counts);
  const covered = total - counts.no_result;
  document.getElementById("delta-coverage").textContent =
    covered + " of " + total + " tests have a result on this " + streamNoun
    + ".";

  document.getElementById("delta-baseline").textContent =
    "This " + streamNoun + " last ran " + ageText(streamMeta.last_seen, nowMs)
    + " — " + baseline + " last ran " + ageText(baselineMeta.last_seen, nowMs)
    + ".";

  // WP-23 (docs/STREAMS_PLAN.md §5.2): drift framing built from data —
  // "of N failing here, M fail on <baseline> too" — never a "behind by
  // N commits" line (explicitly not knowable, not built: no VCS
  // integration). N is new_failures+both_failing (both categories are
  // guaranteed FAIL on THIS stream by CompareCounts' own definition);
  // M is both_failing, the subset that is ALSO failing on the baseline
  // — the honest way to separate "drift" (new_failures, unique to this
  // stream) from "inherited" (both_failing, not this stream's doing).
  const drift = document.getElementById("delta-drift");
  if (drift) {
    const failingHere = counts.new_failures + counts.both_failing;
    if (failingHere === 0) {
      drift.textContent = "Nothing is failing on this " + streamNoun + ".";
    } else {
      const failWord = counts.both_failing === 1 ? "fails" : "fail";
      drift.textContent = "Of " + failingHere + " test"
        + (failingHere === 1 ? "" : "s") + " failing here, "
        + counts.both_failing + " also " + failWord + " on " + baseline
        + " too.";
    }
    drift.hidden = false;
  }

  // A stale baseline is a fact about the two timestamps just shown, not
  // a hidden threshold — 14 days is a display judgement about when the
  // WARNING earns its place on screen, not a value anything is filtered
  // or counted by (the CLAUDE.md rule that applies to is server-side
  // cutoffs like stale_before).
  const warning = document.getElementById("delta-stale-warning");
  const baselineAgeMs = nowMs - parseUtc(baselineMeta.last_seen).getTime();
  if (baselineAgeMs > 14 * 24 * 60 * 60 * 1000) {
    const label = baseline.charAt(0).toUpperCase() + baseline.slice(1);
    warning.textContent =
      label + " itself last ran " + ageText(baselineMeta.last_seen, nowMs)
      + " ago — this comparison may be stale.";
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }
}

/**
 * The build-only framing line (WP-22, docs/STREAMS_PLAN.md §4.1): "built
 * <when> · nothing has run since" when a build was imported once and
 * never rebuilt (first_seen === last_seen), or "built <when> · last ran
 * <when>" once it has been re-imported (a rebuild). Worded from
 * first_seen/last_seen themselves, never a constant — the same
 * WindowWordingTest discipline every other recency line in this project
 * follows. Hidden for mainline (which has no "built once" framing that
 * means anything).
 */
function renderBuildFraming(streamMeta, nowMs) {
  const line = document.getElementById("delta-build-framing");
  if (streamMeta.kind === "mainline") {
    line.hidden = true;
    return;
  }
  const built = "Built " + ageText(streamMeta.first_seen, nowMs);
  line.textContent = streamMeta.first_seen === streamMeta.last_seen
    ? built + " — nothing has run since."
    : built + " — last ran " + ageText(streamMeta.last_seen, nowMs) + ".";
  line.hidden = false;
}

/** "1 new failure"/"2 new failures" — the one pluralisation rule this
 * file's counts share, factored out once F5 needed it a second time. */
function countWord(n, noun) {
  return n + " " + noun + (n === 1 ? "" : "s");
}

/**
 * The nearest earlier same-product BUILD by `last_seen` (id as
 * tiebreak) — used ONLY to label the verdict line below, never to
 * choose the actual comparison baseline (that choice is always
 * mainline unless the Compare-to control names something else, per
 * WP-25, docs/ONE_KIND_PLAN.md §1.3). `candidate.kind !== "build"`
 * excludes mainline — the only other kind that exists — from ever
 * being named as a "predecessor build"; it is not a reintroduced
 * kind-gate, just the one remaining way to say "not mainline" now
 * that there is nothing else to check against. Mirrors
 * Storage.previous_builds' own ordering rule exactly: the backend
 * needs its own copy for the O(1) Watchlist card path (still used
 * there, WP-25 §1.4 aside — see that method's docstring); this is the
 * frontend's for a page that already has the full stream list in hand
 * and gains nothing from a second round trip to ask the server the
 * same question.
 */
function findPredecessorBuild(streamMeta, streams) {
  let best = null;
  for (const candidate of streams) {
    if (candidate.kind !== "build" || candidate.id === streamMeta.id) {
      continue;
    }
    // Same tie-break as Storage.previous_builds: a candidate qualifies
    // when it is strictly earlier, or exactly as recent with a
    // smaller id (never "==", which excluding with `>=` would
    // silently disagree with the backend's `<` on the id side for two
    // builds sharing a last_seen).
    if (candidate.last_seen > streamMeta.last_seen
        || (candidate.last_seen === streamMeta.last_seen
            && candidate.id >= streamMeta.id)) {
      continue;   // ISO strings: lexical compare is chronological.
    }
    if (best === null || candidate.last_seen > best.last_seen
        || (candidate.last_seen === best.last_seen
            && candidate.id > best.id)) {
      best = candidate;
    }
  }
  return best;
}

/**
 * F5 (docs/STREAMS_PLAN.md §5.2 "as built"; restored after a WP-25
 * over-deletion, docs/ONE_KIND_PLAN.md fix round): a delta view only
 * ever answers "is this RC good?" against ONE baseline at a time —
 * this line names the OTHER canonical baseline too, so a reader
 * comparing against the previous build still sees mainline's own
 * verdict (or vice versa) without a second navigation. Hidden outright
 * for mainline scope (no delta view at all), or a build with no
 * predecessor to name (the first build of a product) — the predecessor
 * is looked up unconditionally, regardless of which side the page was
 * actually opened against, because BOTH names are always shown.
 *
 * LAZY: called fire-and-forget from initDeltaView, AFTER first paint —
 * this function's own fetch(es) must never be awaited on the critical
 * path (measured cost is in the F5 commit message). One of the two
 * legs is usually already on hand (whichever baseline this page was
 * actually opened with — mainline by WP-25's default, or whatever the
 * Compare-to control was explicitly set to), so the common case costs
 * exactly ONE extra counts-only /api/compare call; only an explicit
 * ?baseline= naming a THIRD build (neither the predecessor nor
 * mainline) costs two.
 *
 * Guards against a stale render the same way a page-wide requestSeq
 * would: initDeltaView only ever calls this once per non-mainline page
 * load (changing the "Compare to" baseline is a full navigation, not
 * an in-place re-render — so deltaState.streamId cannot legitimately
 * change out from under this call), but the check costs nothing and
 * makes that invariant load-bearing rather than assumed.
 */
async function renderBuildVerdict(streamId, data, productStreams) {
  const line = document.getElementById("delta-verdict");
  if (!line) {
    return;
  }
  if (data.stream.kind === "mainline") {
    line.hidden = true;
    return;
  }
  const predecessor = findPredecessorBuild(data.stream, productStreams);
  if (predecessor === null) {
    line.hidden = true;
    return;
  }
  const currentBaselineId =
    data.baseline.kind === "mainline" ? null : data.baseline.id;
  let predecessorCounts =
    currentBaselineId === predecessor.id ? data.counts : null;
  let mainlineCounts = currentBaselineId === null ? data.counts : null;
  try {
    if (predecessorCounts === null) {
      const page = await fetchCompare(streamId, null, 0, predecessor.id);
      predecessorCounts = page.counts;
    }
    if (mainlineCounts === null) {
      const page = await fetchCompare(streamId, null, 0, null);
      mainlineCounts = page.counts;
    }
  } catch (err) {
    // Enrichment only — a failed extra fetch must not show an error
    // banner over a delta view that otherwise loaded fine.
    line.hidden = true;
    return;
  }
  if (deltaState.streamId !== streamId) {
    return;   // the page moved on while this was in flight
  }
  line.textContent =
    "vs " + streamLabel(predecessor) + ": "
    + countWord(predecessorCounts.new_failures, "new failure")
    + " · " + predecessorCounts.new_passes + " fixed"
    + " — vs mainline: "
    + countWord(mainlineCounts.new_failures, "new failure");
  line.hidden = false;
}

/**
 * The sticky "you are scoped to a branch" band — shared by the
 * dashboard's delta view and the test-detail page (WP-21
 * docs/STREAMS_PLAN.md §3.6: found in first human use, a reader deep in
 * a test's history/analytics/compare strip had no indication they were
 * scoped at all). Every page that mounts `#branch-band` renders it the
 * same way, so "Back to mainline" means the same promise everywhere:
 * this URL with ONLY `stream` removed — never a fixed page like
 * `index.html`, which would silently drop test.html's
 * environment/script/test_name and land on the wrong page entirely.
 *
 * Guards its own mount the way products.js's host-managed call sites do
 * (ProductSwitcherHostManagedTest) — a page with no `#branch-band`
 * simply does not render one, rather than throwing mid-render and
 * taking the rest of that page's first paint down with it.
 *
 * *baselineMeta* (WP-22, docs/STREAMS_PLAN.md §4.1) is optional: the
 * test-detail compare strip always compares against mainline and has no
 * baseline object to pass, so omitting it keeps that call site's wording
 * exactly what it was before this drop. The dashboard's delta view
 * always passes the ACTUAL baseline — mainline by default (WP-25,
 * docs/ONE_KIND_PLAN.md §1.3: the client-side predecessor-build default
 * is gone), or whatever the Compare-to control was explicitly set to.
 */
export function renderBranchBand(streamMeta, baselineMeta) {
  const container = document.getElementById("branch-band");
  if (!container) {
    return;
  }
  const textEl = document.getElementById("branch-band-text");
  const backLink = document.getElementById("branch-band-back");
  const baseline = baselineMeta ? streamLabel(baselineMeta) : "mainline";
  textEl.textContent = "Viewing " + streamMeta.kind + " " + streamMeta.name
    + " — compared against " + baseline + ".";
  // withStream(null) (WP-24, urls.js): this URL with ONLY `stream`
  // removed (and, per the scope hierarchy, the `baseline` it contains)
  // — never a fixed page like index.html, which would silently drop
  // test.html's environment/script/test_name and land on the wrong
  // page entirely.
  backLink.href = withStream(null);
  container.hidden = false;
}

/**
 * Fill *container* (cleared first) with the stream-scoped empty-state
 * hint the Time and Timeline pages share (WP-25, docs/ONE_KIND_PLAN.md
 * §2b.1, user-reported 2026-08-09): a build that ran on one environment
 * showed a bare empty page on every OTHER environment -- the data was
 * honest, the page was not. *environments* is the payload's own
 * `stream_environments` list (Storage.environments_for_stream, one
 * grouped query over the stream's own latest_runs partition -- present
 * only when the caller was ALREADY empty and scoped away from mainline,
 * so a page with data never fetches or renders this). *linkFor* builds
 * each environment's href -- the CALLER's job, since Time and Timeline
 * build slightly different query strings around the shared
 * `environment` switch (Time carries `group_by`/`script`; Timeline
 * carries `days`/`from`/`to`); each link changes ONLY the environment
 * param, per the scope-self-sufficient rule every other stream link in
 * this app follows.
 */
export function renderStreamEnvironmentHint(container, environments, linkFor) {
  clearNode(container);
  if (!environments.length) {
    container.appendChild(document.createTextNode(
      "This stream has no runs on any environment."));
    return;
  }
  container.appendChild(document.createTextNode(
    "This stream has no runs on this environment. It does have runs "
    + "on: "));
  environments.forEach((environment, index) => {
    if (index > 0) {
      container.appendChild(document.createTextNode(", "));
    }
    const link = document.createElement("a");
    link.href = linkFor(environment);
    link.textContent = environment;
    container.appendChild(link);
  });
  container.appendChild(document.createTextNode("."));
}

/** Sections that only mean something on the mainline dashboard. Hidden
 * outright while scoped to a branch — docs/STREAMS_PLAN.md §3.6 calls
 * this a swap, not an addition. */
const MAINLINE_SECTIONS = [
  "status-section", "charts-section", "triage-section", "browse-section",
];

/* ================= "Compare to" control (WP-22, un-gated by WP-25) ================= */

/** GET /api/streams?product= — every same-product stream, the raw data
 * the "Compare to" datalist needs. A failed fetch degrades to "mainline
 * only" rather than breaking the delta view that already loaded. */
async function fetchProductStreams(product) {
  try {
    const data = await fetchJson(
      apiUrl("api/streams", {}, { product: product }));
    return data.streams || [];
  } catch (err) {
    return [];
  }
}

/**
 * The "Compare to" datalist combo (WP-22, docs/STREAMS_PLAN.md §4.1;
 * un-gated by WP-25, docs/ONE_KIND_PLAN.md §1.3): shown for EVERY
 * non-mainline stream — since the default baseline is always mainline
 * now (no client-side predecessor pick), this control is how a
 * predecessor gets chosen at all, for any stream, not only a 'build'
 * one. A plain `<input list=…>`, not a `<select>`: the text typed or
 * picked IS the label ("build:1.0"), matched back to an id through a
 * small map built fresh on every render; an unrecognised value (a typo)
 * is a no-op, not a broken navigation.
 */
function renderCompareToControl(streamMeta, baselineMeta, streams) {
  const field = document.getElementById("compare-to-field");
  if (!field) {
    return;
  }
  if (streamMeta.kind === "mainline") {
    field.hidden = true;
    return;
  }
  const input = document.getElementById("compare-to-input");
  const datalist = document.getElementById("compare-to-options");
  clearNode(datalist);
  const labelToId = {};   // display text -> id (null for mainline)
  const mainlineLabel = "Mainline nightlies";
  labelToId[mainlineLabel] = null;
  const mainlineOpt = document.createElement("option");
  mainlineOpt.value = mainlineLabel;
  datalist.appendChild(mainlineOpt);

  const others = streams
    .filter((s) => s.id !== streamMeta.id)
    .sort((a, b) => (a.last_seen < b.last_seen ? 1 : -1));
  for (const other of others) {
    const label = other.kind + ":" + other.name;
    labelToId[label] = other.id;
    const opt = document.createElement("option");
    opt.value = label;
    datalist.appendChild(opt);
  }

  input.value = baselineMeta.kind === "mainline"
    ? mainlineLabel : baselineMeta.kind + ":" + baselineMeta.name;
  // Clear-on-focus / restore-on-blur, same defect and same cure as the
  // Build picker (see streams.js renderPicker): a datalist filters by
  // the box's current text, so a pre-filled box offers no suggestions
  // until hand-cleared. Idempotent handler assignment throughout, same
  // reasoning as the show-more/reload buttons below: this control can
  // be re-rendered by a reload.
  input.onfocus = () => {
    input.dataset.restore = input.value;
    input.value = "";
  };
  input.onblur = () => {
    if (input.value.trim() === "" && input.dataset.restore) {
      input.value = input.dataset.restore;
    }
  };
  input.onchange = () => {
    const chosen = labelToId[input.value];
    if (chosen === undefined) {
      return;   // not a recognised option -- leave the view as it is
    }
    // EXPLICIT mainline, as an EXPLICIT param, even though "no
    // baseline" already means mainline server-side (WP-25,
    // docs/ONE_KIND_PLAN.md §1.3 deleted the client-side predecessor-
    // build default that used to apply here).
    // KEPT deliberately per that same decision: it costs nothing and
    // makes the choice explicit rather than merely absent -- this is
    // also the exact encoding that fixed the original "choosing
    // mainline snaps back to the predecessor" bug the first RC
    // reviewer hit, before the predecessor default existed to snap
    // back to at all. Mainline's stream id is 1 by migration-9
    // invariant (storage.MAINLINE_STREAM_ID; the row is seeded by the
    // migration itself), the same invariant the s: Watch-card grammar
    // already leans on server-side. withBaseline("mainline") (WP-24,
    // urls.js) is the one path allowed to write that explicit "1".
    window.location.href = chosen === null
      ? withBaseline("mainline") : withBaseline(chosen);
  };
  field.hidden = false;
}

/**
 * Swap the dashboard body for the branch-vs-mainline delta view.
 *
 * Called from app.js's init() ONLY when getSelectedStreamId() is
 * non-null, and nothing else in app.js runs afterwards for that page
 * load — the mainline code path (summary fetch, queues, the browse
 * table) never executes, which is what keeps this feature's entire
 * footprint on a mainline page at zero.
 *
 * Baseline resolution (WP-25, docs/ONE_KIND_PLAN.md §1.3, superseding
 * WP-22's client-side predecessor-build default): an explicit
 * `?baseline=` wins outright; otherwise the server's own default
 * (mainline) stands — no second /api/compare fetch to guess at a
 * "better" default. The product's stream list is still fetched once,
 * for any non-mainline stream, because the Compare-to control (which
 * IS how a predecessor gets chosen now) needs it.
 */
export async function initDeltaView(streamId) {
  deltaState.streamId = streamId;
  deltaState.category = CATEGORY_ORDER[0];

  const loading = document.getElementById("loading-state");
  loading.hidden = false;
  loading.textContent = "Loading comparison…";
  for (const id of MAINLINE_SECTIONS) {
    document.getElementById(id).hidden = true;
  }
  const envField = document.getElementById("env-filter-field");
  if (envField) {
    envField.hidden = true;
  }

  let productStreams = [];
  try {
    const data = await fetchCompare(streamId, null, 0, getSelectedBaselineId());
    if (data.stream.kind !== "mainline") {
      productStreams = await fetchProductStreams(data.stream.product);
    }
    deltaState.baselineId =
      data.baseline.kind === "mainline" ? null : data.baseline.id;
    renderBranchBand(data.stream, data.baseline);
    renderBuildFraming(data.stream, Date.now());
    renderCompareToControl(data.stream, data.baseline, productStreams);
    renderTiles(document.getElementById("delta-tiles"), data.counts);
    renderBaselineCard(data.stream, data.baseline, data.counts, Date.now());
    renderTabs();
    // F5: fire-and-forget, deliberately NOT awaited — its own fetch(es)
    // must never hold up the section becoming visible on the next line.
    // The .catch() here is a pure safety net (the function's own
    // try/catch already turns a failed fetch into "stay hidden", never
    // a throw) against any future change making that no longer true.
    renderBuildVerdict(streamId, data, productStreams).catch(() => {});
    document.getElementById("delta-section").hidden = false;
    loading.hidden = true;
    await loadCategory(true);
  } catch (err) {
    loading.hidden = true;
    showError(err.message);
    return;
  }

  // Idempotent assignment (not addEventListener): initDeltaView can run
  // again from this same click, and a growing pile of listeners would
  // fire the refresh N times on the Nth click.
  document.getElementById("delta-show-more").onclick =
    () => loadCategory(false).catch((err) => showError(err.message));
  document.getElementById("reload-btn").onclick =
    () => initDeltaView(streamId);
}

/* ================= the test-detail compare strip ================= */

/**
 * "mainline: PASS   this branch: FAIL" beside a test's own detail —
 * built from TWO detail fetches test.js already makes (one unscoped,
 * one `stream=`), not a new endpoint: a single test's result on two
 * streams is not what /api/compare (an ESTATE-wide comparison) answers.
 */
export function renderCompareStrip(container, streamMeta, baselineResult, streamResult) {
  clearNode(container);
  const wrap = el("span", "chip-transition");
  wrap.appendChild(el("span", "row-sub", "mainline"));
  wrap.appendChild(mainlineCell(baselineResult));
  wrap.appendChild(el("span", "row-sub",
    streamMeta.kind + ":" + streamMeta.name));
  wrap.appendChild(streamCell(streamResult));
  container.appendChild(wrap);
  container.hidden = false;
}
