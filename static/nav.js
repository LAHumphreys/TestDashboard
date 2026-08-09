/* nav.js — puts the latest drop's date on the "What's new" link, and
 * marks it when this browser has not seen that drop yet.
 *
 * WHY: release notes nobody notices are release notes nobody reads. A
 * tester has no reason to click "What's new" on the off-chance, so a drop
 * would land, the page would explain it, and the first anyone heard was a
 * bug report about a feature working as designed.
 *
 * WHERE THE DATE COMES FROM: `whatsnew.html` itself, which is the only
 * copy of it. Each release section carries `data-drop-date="YYYY-MM-DD"`,
 * this reads the newest one, and `tests/test_frontend_calls.py` asserts
 * that attribute agrees with the heading a human reads. The alternative —
 * a date written here as well — is one more thing to update per drop and
 * the first thing that would go stale, at which point the nav confidently
 * advertises the wrong day.
 *
 * The page is fetched as text and scanned for the attribute rather than
 * parsed: one attribute, one regex, no innerHTML anywhere near it. It is
 * a small file served with an ETag, so this is a 304 on every load after
 * the first.
 *
 * FAILING QUIETLY IS THE REQUIREMENT. This is decoration on someone
 * else's page. If the fetch fails, the file has no dates, or storage is
 * unavailable, the link stays exactly as the HTML shipped it.
 */

"use strict";

import { NAV_SCOPE_PAGES, pageUrl } from "./urls.js";

/** localStorage key holding the newest drop date this browser has read. */
const SEEN_KEY = "testboard.whatsnew.seen";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-07-30" -> "30 Jul" (display only). */
function shortDate(iso) {
  const month = parseInt(iso.slice(5, 7), 10);
  const day = parseInt(iso.slice(8, 10), 10);
  if (!month || !day) {
    return iso;
  }
  return day + " " + MONTHS[month - 1];
}

/** The newest data-drop-date in some HTML text, or null. */
export function newestDropDate(html) {
  const found = String(html).match(/data-drop-date="(\d{4}-\d{2}-\d{2})"/g);
  if (!found) {
    return null;
  }
  // Max rather than first: the file is newest-first by convention, and a
  // convention is not a guarantee. ISO dates compare lexically.
  return found
    .map((match) => match.slice(-11, -1))
    .reduce((newest, date) => (date > newest ? date : newest));
}

function readSeen() {
  try {
    return window.localStorage.getItem(SEEN_KEY);
  } catch (err) {
    return null;      // private mode, or storage disabled
  }
}

function writeSeen(date) {
  try {
    window.localStorage.setItem(SEEN_KEY, date);
  } catch (err) {
    /* Not being able to remember is not worth an error. */
  }
}

/**
 * Query params carried onto the NAV_SCOPE_PAGES nav links (urls.js —
 * the page-allowlist itself, and the reasoning for exactly those three
 * pages and no others, now lives there as data every caller of
 * pageUrl() shares; this is the SEPARATE question of which of the four
 * scope levels a nav-bar link carries), read from the CURRENT page's
 * own URL — never localStorage or any other standing preference, only
 * what is literally in THIS address bar right now, the same "the URL
 * is the whole configuration" rule docs/STREAMS_PLAN.md §0.9 already
 * applies to a Watch card link, extended here to the nav bar itself.
 * `environment` travels with `stream`/`product` because it is the same
 * scoping family, and timeline.html needs one to be useful at all.
 * `baseline` deliberately never travels — a baseline belongs to the
 * scope it was chosen in, the same reasoning the pickers' own
 * scope-reset follows (urls.js's hierarchy rule).
 */
const CARRIED_PARAMS = ["stream", "product", "environment"];

/**
 * Rewrite `nav`'s NAV_SCOPE_PAGES children's `href` to carry whichever
 * of CARRIED_PARAMS are present in `currentSearch` — the bug this
 * fixes: navigating Dashboard -> Timeline (or any of the three) from a
 * scoped page silently landed on mainline, the bare
 * `href="timeline.html"` in every page's markup never having heard of
 * `?stream=`. `nav` is the element whose CHILDREN are the `<a>` tags
 * (real markup: `<nav class="site-nav"><a href="index.html">…</a>…
 * </nav>`) — sibling traversal from `#nav-whatsnew`'s own parent,
 * chosen over `document.querySelectorAll(".site-nav a")` only because
 * it needs no selector the id-only DOM-shim harness would have to grow
 * support for; both walk the identical real markup in a real browser.
 *
 * ZERO CHANGE when unscoped: if the current URL carries none of the
 * three params, nothing is touched at all — not even re-set to its own
 * existing value — so a byte-diff of the DOM before/after is empty.
 *
 * Builds every new href through pageUrl() (WP-24) rather than hand-
 * editing a URL's searchParams — but with an EXPLICIT scope object
 * built from `currentSearch` (never pageUrl()'s own default carriage,
 * which reads the real `window.location.search`): `currentSearch` is a
 * plain string argument precisely so this function stays testable
 * without a real `window.location`, and silently reading around it
 * would make that argument a lie the moment the two disagreed.
 */
export function carryScopeIntoNav(nav, currentSearch) {
  const params = new URLSearchParams(currentSearch);
  const carry = CARRIED_PARAMS.filter((name) => params.has(name));
  if (carry.length === 0 || !nav || !nav.children) {
    return;
  }
  const scope = {
    product: params.has("product") ? params.get("product") : null,
    stream: params.has("stream") ? params.get("stream") : null,
    baseline: null,
    environment: params.has("environment") ? params.get("environment") : null,
  };
  for (const child of nav.children) {
    if (!child.getAttribute || child.tagName !== "A") {
      continue;
    }
    const href = child.getAttribute("href");
    if (NAV_SCOPE_PAGES.indexOf(href) === -1) {
      continue;
    }
    const page = href.slice(0, -".html".length);
    child.setAttribute("href", pageUrl(page, {}, scope));
  }
}

/**
 * Annotate `link` for a drop dated `date`.
 *
 * Exported for the same reason the date parser is: it is the part with
 * the branching in it, and it can be checked without a browser.
 */
export function decorateLink(link, date, seen, onThisPage) {
  const unread = !onThisPage && (seen === null || seen < date);

  const stamp = document.createElement("span");
  stamp.className = "nav-drop-date";
  stamp.textContent = shortDate(date);
  link.appendChild(stamp);

  if (unread) {
    const dot = document.createElement("span");
    dot.className = "nav-unread-dot";
    // Decorative: the meaning is in the link's accessible name below, and
    // a screen reader announcing a bullet adds nothing to it.
    dot.setAttribute("aria-hidden", "true");
    link.appendChild(dot);
    link.classList.add("has-update");
    link.setAttribute("aria-label",
      "What's new — updated " + shortDate(date) + ", not read yet");
    link.title = "Updated " + shortDate(date) + " — you have not read this one";
  } else {
    link.setAttribute("aria-label", "What's new — updated " + shortDate(date));
    link.title = "Last updated " + shortDate(date);
  }
  return unread;
}

async function init() {
  const link = document.getElementById("nav-whatsnew");
  if (!link) {
    return;
  }
  // Independent of the What's new decoration below (and everything it
  // can fail on) -- #nav-whatsnew's parent IS the nav bar itself, real
  // markup: <nav class="site-nav"><a href="index.html">…</a> …
  // <a id="nav-whatsnew" href="whatsnew.html">…</a></nav>.
  carryScopeIntoNav(link.parentNode, window.location.search);
  // aria-current is already on the link of the page you are looking at,
  // so it is the honest answer to "am I reading this right now" without
  // matching on filenames or worrying about how the URL was written.
  const onThisPage = link.getAttribute("aria-current") === "page";
  let date = null;
  try {
    const response = await fetch("whatsnew.html", { cache: "no-cache" });
    if (!response.ok) {
      return;
    }
    date = newestDropDate(await response.text());
  } catch (err) {
    return;
  }
  if (date === null) {
    return;
  }
  decorateLink(link, date, readSeen(), onThisPage);
  if (onThisPage) {
    // Reading the page IS the acknowledgement.
    writeSeen(date);
  }
}

init();
