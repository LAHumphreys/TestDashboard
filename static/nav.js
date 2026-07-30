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
