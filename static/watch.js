/* watch.js — the Watchlist: a shareable, URL-configured grid of verdict
 * cards (docs/STREAMS_PLAN.md §0.9/§2.4).
 *
 * THE URL IS THE WHOLE CONFIGURATION. Repeated `c=kind:name` params, order
 * preserved — there is no account and no server-side saved view, only
 * this browser's own "default" in localStorage (the same mechanism as
 * the What's-new unread dot). One request (/api/watch) answers every
 * card at once; each card carries its OWN freshness fields, because a
 * page that can mix a product and an environment with different windows
 * has no single truthful "as of" line to show.
 *
 * All data reaches the DOM via textContent/createElement, never innerHTML.
 */

"use strict";

import {
  clearError,
  clearNode,
  el,
  fetchJson,
  formatTime,
  showError,
} from "./api.js";
import { CATEGORY_LABELS, CATEGORY_ORDER, ageText } from "./compare.js";
import { pageUrl } from "./urls.js";

/** localStorage key holding this browser's default Watchlist query. */
const DEFAULT_KEY = "testboard.watch.default";

const state = {
  specs: [],   // [{kind, name, expected}], in display/request order
};

/* ================= URL grammar =================
 *
 * "kind:name", split at the FIRST colon (docs/STREAMS_PLAN.md §2.4) — a
 * product or environment name may itself contain a colon, and the kind
 * letter never does.
 *
 * The name may carry an OPTIONAL declared-staleness suffix, "@<n>h" or
 * "@<n>d" (WP-23), split at the LAST "@" — but only when the text after
 * it matches EXPECTED_SUFFIX. A branch/build/product name is free text
 * and may itself contain "@", so an invalid or absent tail is part of
 * the name, not a suffix: mirrors testboard/api.py's
 * _parse_watch_spec()/_EXPECTED_SUFFIX exactly, so the same URL parses
 * the same way on both sides.
 */
const EXPECTED_SUFFIX = /^\d+[hd]$/;

/** Split "name" (or "name@1d") into {name, expected}. */
function splitExpectedSuffix(rest) {
  const atSign = rest.lastIndexOf("@");
  if (atSign === -1) {
    return { name: rest, expected: null };
  }
  const tail = rest.slice(atSign + 1);
  if (!EXPECTED_SUFFIX.test(tail)) {
    return { name: rest, expected: null };
  }
  return { name: rest.slice(0, atSign), expected: tail };
}

/** "e:lab-alpha@36h" -> {kind: "e", name: "lab-alpha", expected: "36h"}. */
export function splitSpec(spec) {
  const at = spec.indexOf(":");
  if (at === -1) {
    return { kind: spec, name: "", expected: null };
  }
  const kind = spec.slice(0, at);
  const suffix = splitExpectedSuffix(spec.slice(at + 1));
  return { kind: kind, name: suffix.name, expected: suffix.expected };
}

/** {kind, name, expected} -> "e:lab-alpha@36h", the inverse of splitSpec. */
export function joinSpec(entry) {
  const base = entry.kind + ":" + entry.name;
  return entry.expected ? base + "@" + entry.expected : base;
}

/** Parse a location.search string into [{kind, name}, ...], order kept. */
export function parseSpecs(search) {
  const params = new URLSearchParams(search);
  return params.getAll("c").map(splitSpec);
}

/** Build "watch.html?c=...&c=..." from a list of {kind, name} specs. */
export function buildUrl(specs) {
  const params = new URLSearchParams();
  for (const entry of specs) {
    params.append("c", joinSpec(entry));
  }
  const qs = params.toString();
  return qs ? "watch.html?" + qs : "watch.html";
}

/* ================= persistence ================= */

function readDefault() {
  try {
    return window.localStorage.getItem(DEFAULT_KEY);
  } catch (err) {
    return null;
  }
}

function writeDefault(query) {
  try {
    window.localStorage.setItem(DEFAULT_KEY, query);
  } catch (err) {
    /* Not being able to remember is not worth an error. */
  }
}

/* ================= rendering ================= */

/**
 * Where a card's "Open in dashboard" link goes, or null (unknown kind).
 *
 * SCOPE-SELF-SUFFICIENT (WP-23 bugfix, docs/STREAMS_PLAN.md §0.9): a
 * card whose scope differs from whatever product this browser last had
 * selected must still land on the right page, not on the OLD product
 * with an environment filter that then matches nothing under it. Every
 * link that names an environment or a stream also names ITS OWN
 * product — including the empty string for an environment nobody has
 * mapped, meaning "All products" — so index.html's own
 * adoptProductFromUrl() (products.js) has something to adopt. A card's
 * own `?product=` always wins over whatever was stored, the same "the
 * URL is the whole configuration" rule the Watchlist's own URL follows.
 */
function cardLink(card) {
  // Every branch composes through pageUrl() (WP-24) — this is the
  // "non-c=" part of the exemption watch.js's own module docstring and
  // docs/SCOPED_URLS_PLAN.md §4 both call out: the card's OWN scope is
  // always passed as an EXPLICIT override, never pageUrl()'s default
  // carriage (which would read whatever this browser's own address bar
  // happens to hold, defeating the whole point of a card that names
  // its own scope). `baseline`/`environment`/`stream` all end up null
  // for a plain product card, `stream`/`baseline` null for an
  // environment card, and `baseline`/`environment` null for a stream
  // card — the SAME cascade the hierarchy rule already produces from
  // naming `product` alone; each branch below only needs to add
  // whatever ELSE the original single-field append also stated.
  if (card.kind === "product") {
    return pageUrl("index", {}, { product: card.name });
  }
  if (card.kind === "environment") {
    return pageUrl("index", { environment: card.name },
      { product: card.product || "" });
  }
  if (card.kind === "stream") {
    // A stream is identified by ID, not name (two products can each
    // have a "feat/x" branch) — the dashboard's delta view reads the
    // same ?stream=<id> the Build picker writes.
    return pageUrl("index", {},
      { stream: card.id, product: card.product || "" });
  }
  return null;
}

/**
 * *href*, when given, turns the stat's value into a link rather than
 * plain text (F4, docs/STREAMS_PLAN.md §5.2 "as built") -- every OTHER
 * call site omits it and gets the exact unlinked stat this page has
 * always rendered.
 */
function buildStat(label, value) {
  // The optional third (href) argument died with its one caller when
  // the 2026-08-10 redesign moved the unassigned-failing count — and
  // its F4 link — into the hero (buildHero above): the supporting
  // stats below the hero are plain numbers again.
  const stat = el("div", "watch-stat");
  stat.appendChild(el("span", "watch-stat-value", String(value)));
  stat.appendChild(el("span", "watch-stat-label", label));
  return stat;
}

/**
 * Where the "Unassigned failing" stat itself links to (F4) -- an
 * enrichment on top of cardLink(): scopes the dashboard's browse table
 * straight to the failing, unassigned rows (?result=FAIL&unassigned=1
 * -- the "Unassigned only" toggle chip's own URL contract, read by
 * app.js's wireMainlineControls()).
 *
 * Applied to EVERY card kind, including stream -- advisor-caught: the
 * original design special-cased stream cards to omit these params,
 * reasoning that "the delta view already shows assignees inline". That
 * holds for a build or a sparse (<2 covered passes) branch, both of
 * which land on the DIFF tab -- but a long-running branch with 2+
 * covered passes (OWN_RESULTS_DEFAULT_PASSES, app.js) defaults to
 * "Its own results" instead, which is the SAME browse table/filter
 * row this function's params are built for (activateOwnResultsTab()
 * calls wireMainlineControls(), the one place that reads them from the
 * URL). Appending them unconditionally is safe either way: the diff
 * tab's rendering path (compare.js's initDeltaView) never reads
 * result=/unassigned= from the URL at all, so on a build or sparse
 * branch they are simply inert extra params, not a wrong filter.
 */
function unassignedStatLink(card) {
  const base = cardLink(card);
  if (!base) {
    return base;
  }
  const at = base.indexOf("?");
  const params = new URLSearchParams(at === -1 ? "" : base.slice(at + 1));
  params.set("result", "FAIL");
  params.set("unassigned", "1");
  return (at === -1 ? base : base.slice(0, at)) + "?" + params.toString();
}

/**
 * One verdict card's freshness line, built from ITS OWN data — never a
 * page-wide "as of" line, and never the word "night" (a card's window
 * is derived from when ITS environments actually ran, which can be any
 * hour, more than once a day, or a fortnight ago).
 */
function freshnessLine(card) {
  const bits = [];
  if (card.last_reported) {
    bits.push("last reported " + formatTime(card.last_reported));
  }
  // A product spans environments reporting hours apart, so it gets no
  // single timestamp (see the API's product-card comment). What it gets
  // instead is the LAGGARD, by name: the environment furthest behind is
  // the one the manager is actually waiting on, and naming it means an
  // old-but-quiet environment can never hide behind a fresh one.
  if (card.laggard) {
    bits.push(card.laggard.last_reported
      ? "slowest environment: " + card.laggard.environment
        + ", last reported " + formatTime(card.laggard.last_reported)
      : "environment " + card.laggard.environment
        + " has never reported");
  }
  bits.push("counted as quiet after " + formatTime(card.stale_before));
  return bits.join(" — ");
}

/** The ISO timestamp a card's OWN declared-staleness judgment is made
 * against (WP-23, docs/STREAMS_PLAN.md §2.4) — matches _handle_watch's
 * choice server-side: environment -> last_reported; product -> its
 * laggard's; stream -> last_seen. */
function cardFreshnessIso(card) {
  if (card.kind === "stream") {
    return card.last_seen;
  }
  if (card.kind === "product") {
    return card.laggard ? card.laggard.last_reported : null;
  }
  return card.last_reported;
}

/**
 * The declared-staleness wording line — present only when the card's
 * spec carried an "@" suffix (``card.expected`` is omitted entirely
 * otherwise, per the API contract). Both halves are real data:
 * ``card.expected`` is the URL's own declaration, echoed back by the
 * server, and the age comes from the card's own freshness timestamp
 * via the same ageText() the branch card's freshness line already
 * uses — never a hidden constant standing in for either half.
 */
function stalenessText(card, nowMs) {
  if (!card.expected) {
    return null;
  }
  return "expected within " + card.expected + " — last run "
    + ageText(cardFreshnessIso(card), nowMs);
}

/**
 * Accent precedence (WP-23, docs/STREAMS_PLAN.md §2.4): a card can be
 * both stale AND carry unassigned failures at once. The BORDER always
 * shows the unassigned-failure accent when there is one — an owner gap
 * is the more actionable of the two facts — while the staleness TEXT
 * LINE (stalenessText above) is added independently of which accent
 * wins the border, so neither fact is ever silently dropped.
 */
function applyWatchAccent(div, card) {
  if (card.unassigned_failing) {
    div.classList.add("watch-card-accent-fail");
  } else if (card.stale) {
    div.classList.add("watch-card-accent-stale");
  }
}

/**
 * The morning-scan hero (user redesign, 2026-08-10): the TWO numbers a
 * manager scanning the board actually reads — how much is unowned, and
 * how fresh the data is — promoted to the top of every ok card, big.
 * Everything else (the category counts, the freshness detail line, the
 * declared-staleness line) stays below as supporting detail.
 *
 * The unassigned count now shows even at ZERO — on a scan board,
 * "0 unassigned failures" IS the good news being looked for. This
 * deliberately supersedes §2.4's zero-adds-no-stat rule, which was
 * about not changing pre-feature cards when the stat was introduced;
 * the user's redesign makes the count the card's headline. The ACCENT
 * border keeps its nonzero gate (applyWatchAccent, unchanged), and the
 * F4 click-through link only renders when there is something to land
 * on — a zero is a plain muted number, never a dead link.
 *
 * The freshness value is the SAME per-kind timestamp the card's
 * declared-staleness judgment already uses (cardFreshnessIso:
 * environment → last report, product → its laggard, stream →
 * last_seen), phrased by ageText() from the two real values —
 * WindowWordingTest's discipline — with the exact time in the title.
 * A product card names its laggard in the label, so the big number is
 * never read as "every environment is this fresh".
 */
function buildHero(card, nowMs) {
  const hero = el("div", "watch-card-hero");

  const unassigned = el("div", "watch-hero-stat");
  const count = card.unassigned_failing || 0;
  if (count > 0) {
    const a = document.createElement("a");
    a.href = unassignedStatLink(card);
    a.className = "watch-hero-value watch-hero-alarm";
    a.textContent = String(count);
    unassigned.appendChild(a);
  } else {
    unassigned.appendChild(
      el("span", "watch-hero-value watch-hero-ok", "0"));
  }
  unassigned.appendChild(el("span", "watch-hero-label",
    count === 1 ? "unassigned failure" : "unassigned failures"));
  hero.appendChild(unassigned);

  const freshIso = cardFreshnessIso(card);
  const fresh = el("div", "watch-hero-stat");
  const value = el("span", "watch-hero-value",
    ageText(freshIso, nowMs));
  if (freshIso) {
    value.title = formatTime(freshIso) + " (UTC)";
  }
  fresh.appendChild(value);
  fresh.appendChild(el("span", "watch-hero-label",
    card.kind === "product" && card.laggard
      ? "last result (slowest: " + card.laggard.environment + ")"
      : "last result"));
  hero.appendChild(fresh);

  return hero;
}

function buildOkCard(card, index, total) {
  const nowMs = Date.now();
  const div = el("div", "card watch-card");
  applyWatchAccent(div, card);
  const head = el("div", "watch-card-head");
  head.appendChild(el("span", "watch-card-kind", card.kind));
  head.appendChild(el("span", "watch-card-name", card.name));
  div.appendChild(head);

  div.appendChild(buildHero(card, nowMs));

  const verdict = el("div", "watch-card-verdict");
  verdict.appendChild(buildStat("Failing", card.failing));
  verdict.appendChild(buildStat("New failures", card.new_failures));
  verdict.appendChild(buildStat("Fixed", card.fixed));
  div.appendChild(verdict);

  div.appendChild(el("p", "watch-card-fresh muted", freshnessLine(card)));

  const stale = stalenessText(card, nowMs);
  if (stale) {
    div.appendChild(el("p", "watch-card-stale", stale));
  }

  div.appendChild(buildCardFooter(card, index, total));
  return div;
}

/** "mainline" or "build 2026.9.0" — the card's own copy of the same
 * label rule compare.js's streamLabel() applies dashboard-side; kept
 * local rather than imported so this file's only dependency on
 * compare.js stays the category constants it already had. */
function baselineLabel(card) {
  return card.baseline_kind === "mainline"
    ? "mainline" : card.baseline_kind + " " + card.baseline_name;
}

/**
 * A stream verdict card (WP-21/WP-22, docs/STREAMS_PLAN.md §3.6/§4.1):
 * "N failing in <name>", the compare-vs-baseline headline for one
 * branch/build, both sides' freshness, click-through to the scoped
 * dashboard. Resolved on the server through the same storage reads
 * /api/compare uses (Storage.compare_counts_many), so this card costs
 * no more per-card work than an environment or product card does.
 *
 * A BUILD's baseline is its predecessor build when one exists, not
 * mainline (server-resolved, docs/STREAMS_PLAN.md §4.1) — `card.
 * baseline_kind`/`baseline_name` name whatever it actually was, so
 * nothing here assumes "mainline" the way the WP-21 version of this
 * card did.
 *
 * WP-23 DECISION (docs/STREAMS_PLAN.md §5.2's own escape hatch —
 * "keep the card honest and small; if this makes the card crowded,
 * prefer the vs-mainline verdict and note the decision"): this card
 * stays the vs-mainline verdict ONLY, even for a long-running branch
 * with its own triage numbers now available via the two-tab dashboard.
 * Two reasons, not one: the card is already five stats plus a
 * headline plus two freshness lines — a sixth number earns its keep
 * less than the branch's own dashboard does, where it has room to be
 * explained; and more concretely, `/api/watch` is architecturally
 * O(cards) IN PYTHON, never O(cards) in QUERIES (`Storage.
 * compare_counts_many` batches every requested stream's comparison in
 * ONE query, pinned flat by
 * tests/test_api.py::TestWatchStreamCards::
 * test_query_count_does_not_grow_with_s_card_count) — a per-branch
 * "own new failures" number needs its own pass-detection cutoff
 * (Storage.activity_buckets + analytics.find_passes, PER STREAM,
 * since each branch's cadence is its own), which this endpoint has no
 * batched multi-stream form of. Adding it here would mean N branch
 * cards costing N times the passes/cutoff work — the exact shape
 * CLAUDE.md's "no endpoint may be proportional to card count" rule
 * exists to keep out of the Watchlist. Click through to the branch's
 * own dashboard for its own numbers instead — a real second click,
 * but an honest one over a query-count regression on a page whose
 * whole point is O(1) per card.
 */
function buildStreamCard(card, index, total) {
  const nowMs = Date.now();
  const div = el("div", "card watch-card");
  applyWatchAccent(div, card);
  const head = el("div", "watch-card-head");
  head.appendChild(el("span", "watch-card-kind", card.stream_kind));
  head.appendChild(el("span", "watch-card-name", card.name));
  div.appendChild(head);
  div.appendChild(el("p", "row-sub", card.product));

  // The same morning-scan hero every ok card gets (buildHero above) —
  // cardFreshnessIso() picks last_seen for a stream card.
  div.appendChild(buildHero(card, nowMs));

  const failing = card.both_failing + card.new_failures;
  div.appendChild(el("p", "watch-card-headline",
    failing + (failing === 1 ? " test failing in " : " tests failing in ")
    + card.name + "."));

  const verdict = el("div", "watch-card-verdict");
  for (const key of CATEGORY_ORDER) {
    verdict.appendChild(buildStat(CATEGORY_LABELS[key], card[key]));
  }
  div.appendChild(verdict);

  const fresh = el("p", "watch-card-fresh muted",
    "this " + card.stream_kind + " " + ageText(card.last_seen, nowMs)
    + " — " + baselineLabel(card) + " "
    + ageText(card.baseline_last_seen, nowMs));
  div.appendChild(fresh);

  const stale = stalenessText(card, nowMs);
  if (stale) {
    div.appendChild(el("p", "watch-card-stale", stale));
  }

  div.appendChild(buildCardFooter(card, index, total));
  return div;
}

function buildErrorCard(card, index, total) {
  const div = el("div", "card watch-card watch-card-error");
  const head = el("div", "watch-card-head");
  head.appendChild(el("span", "watch-card-kind", card.kind));
  head.appendChild(el("span", "watch-card-name", card.name));
  div.appendChild(head);
  div.appendChild(el("p", "watch-card-error-text", card.error));
  const footer = el("div", "watch-card-footer");
  footer.appendChild(buildCardControls(index, total));
  div.appendChild(footer);
  return div;
}

/** Remove / up / down — drag-free reorder, keyboard-reachable. Hidden
 * until hovered or focused (CSS opacity/focus-within on the parent
 * `.watch-card`); stays in normal flow throughout so revealing them
 * never reflows the card and Tab order is unaffected. */
function buildCardControls(index, total) {
  const row = el("div", "watch-card-controls");
  const up = el("button", "watch-card-control-btn", "↑ Move up");
  up.type = "button";
  up.disabled = index === 0;
  up.addEventListener("click", () => moveCard(index, -1));
  row.appendChild(up);

  const down = el("button", "watch-card-control-btn", "↓ Move down");
  down.type = "button";
  down.disabled = index === total - 1;
  down.addEventListener("click", () => moveCard(index, 1));
  row.appendChild(down);

  const remove = el(
    "button", "watch-card-control-btn watch-card-remove", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => removeCard(index));
  row.appendChild(remove);
  return row;
}

/**
 * The card's bottom row: the "Open in dashboard" link on the left
 * (when the card has one — an error card never does, cardLink() is
 * simply not called for it) and the hover-reveal controls on the
 * right, as ONE hairline-separated footer rather than two stacked
 * elements — otherwise the controls fading in/out would shove the
 * link up and down a line each time.
 */
function buildCardFooter(card, index, total) {
  const footer = el("div", "watch-card-footer");
  const link = cardLink(card);
  if (link) {
    const a = document.createElement("a");
    a.href = link;
    a.className = "watch-card-open";
    a.textContent = "Open in dashboard →";
    footer.appendChild(a);
  }
  footer.appendChild(buildCardControls(index, total));
  return footer;
}

/* ================= data ================= */

function currentQuery() {
  const params = new URLSearchParams();
  for (const entry of state.specs) {
    params.append("c", joinSpec(entry));
  }
  return params;
}

function syncLinkInput() {
  const url = buildUrl(state.specs);
  document.getElementById("watch-link").value =
    new URL(url, window.location.href).toString();
}

async function refresh() {
  clearError();
  syncLinkInput();
  const grid = document.getElementById("watch-grid");
  const empty = document.getElementById("empty-state");
  if (state.specs.length === 0) {
    clearNode(grid);
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  try {
    const data = await fetchJson("/api/watch?" + currentQuery().toString());
    clearNode(grid);
    const total = data.cards.length;
    data.cards.forEach((card, index) => {
      let built;
      if (!card.ok) {
        built = buildErrorCard(card, index, total);
      } else if (card.kind === "stream") {
        built = buildStreamCard(card, index, total);
      } else {
        built = buildOkCard(card, index, total);
      }
      grid.appendChild(built);
    });
  } catch (err) {
    showError(err.message);
  }
}

/* ================= editing ================= */

function moveCard(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.specs.length) {
    return;
  }
  const [entry] = state.specs.splice(index, 1);
  state.specs.splice(target, 0, entry);
  refresh();
}

function removeCard(index) {
  state.specs.splice(index, 1);
  refresh();
}

/**
 * The composer's cadence choice (WP-23): none / 1d / 7d / a custom
 * hour count. Returns the "@" suffix value ("1d", "36h", ...) or null
 * for "no expectation" — the exact grammar :func:`splitSpec` parses
 * back out, so what the picker builds and what a hand-typed URL means
 * are the same thing.
 */
function readCadence() {
  const cadence = document.getElementById("add-cadence").value;
  if (cadence === "custom") {
    const hours = parseInt(
      document.getElementById("add-cadence-hours").value, 10);
    return hours > 0 ? hours + "h" : null;
  }
  return cadence || null;
}

function addCard() {
  const kind = document.getElementById("add-kind").value;
  const name = document.getElementById("add-name").value;
  if (!name) {
    return;
  }
  state.specs.push({ kind: kind, name: name, expected: readCadence() });
  refresh();
}

/* ================= the add-card picker ================= */

/**
 * Populate the name dropdown from the declared products, known
 * environments, and every product's streams. Decoration on top of the
 * page's real job (showing the cards already in the URL) — a failed
 * fetch here must not block that, so it fails quietly, same rule as
 * nav.js.
 *
 * Entries are `{value, label}` uniformly (not bare strings): an
 * environment/product card is named by its NAME, but a stream card is
 * named by its ID (docs/STREAMS_PLAN.md §3.6 — two products can each
 * have their own "feat/x" branch, so only the id is unambiguous), and
 * /api/streams is per-product, unlike the flat /api/environments list
 * beside it — one request per known product, plus "" for the implicit
 * product, all in parallel.
 */
async function populatePicker() {
  const kindSelect = document.getElementById("add-kind");
  const nameSelect = document.getElementById("add-name");
  let entries = { p: [], e: [], s: [] };
  try {
    const [summary, environments] = await Promise.all([
      fetchJson("/api/summary?parts=headline"),
      fetchJson("/api/environments"),
    ]);
    const productNames = summary.products.map((entry) => entry.product);
    entries.p = productNames.map((name) => ({ value: name, label: name }));
    entries.e = environments.environments.map((entry) => ({
      value: entry.environment, label: entry.environment,
    }));
    const streamLists = await Promise.all(
      [""].concat(productNames).map((product) =>
        fetchJson("/api/streams?product=" + encodeURIComponent(product))
          .catch(() => ({ streams: [] }))));
    for (const page of streamLists) {
      for (const stream of page.streams) {
        entries.s.push({
          value: String(stream.id),
          label: (stream.product || "(no product)") + " · "
            + stream.kind + ":" + stream.name,
        });
      }
    }
  } catch (err) {
    return;
  }
  const fill = () => {
    clearNode(nameSelect);
    for (const entry of entries[kindSelect.value] || []) {
      const opt = el("option", "", entry.label);
      opt.value = entry.value;
      nameSelect.appendChild(opt);
    }
  };
  kindSelect.addEventListener("change", fill);
  fill();
}

/* ================= init ================= */

function init() {
  const search = new URL(window.location.href).search;
  // BUGFIX (ADDENDUM to the perf round): this used to branch on
  // whether `search` was non-empty AT ALL, not on whether it actually
  // carried a `c=` card -- so ANY other param arriving alongside it
  // (a stray ?product=, adopted by products.js's adoptProductFromUrl()
  // from a stale link, or set by the switcher that used to live on
  // this page) took the `search` branch, found zero `c=` values, and
  // silently discarded the saved default entirely: a shareable
  // Watchlist with cards saved as "my default" would render EMPTY the
  // moment a `?product=` param showed up next to it. Checking for `c`
  // specifically is what "the URL is the whole configuration" (this
  // file's own module docstring) actually requires -- a param this
  // page does not speak is not part of that configuration.
  const hasCards = new URLSearchParams(search).has("c");
  const saved = readDefault();
  state.specs = hasCards
    ? parseSpecs(search)
    : (saved ? parseSpecs("?" + saved) : []);

  document.getElementById("add-card-btn")
    .addEventListener("click", addCard);

  const cadenceSelect = document.getElementById("add-cadence");
  const hoursLabel = document.getElementById("add-cadence-hours-label");
  cadenceSelect.addEventListener("change", () => {
    hoursLabel.hidden = cadenceSelect.value !== "custom";
  });

  document.getElementById("save-default-btn").addEventListener("click", () => {
    writeDefault(currentQuery().toString());
    document.getElementById("save-status").textContent =
      "Saved as this browser's default.";
  });

  populatePicker();
  refresh();
}

init();
