/* whatsnew.js — folds this site's own notes into the release notes.
 *
 * The dated sections in whatsnew.html are testboard's: they ship with the
 * build. This adds the notes THIS SITE recorded against the same dates —
 * "the reader that was filing runs under UNKNOWN is fixed" — because a
 * tester reading "what changed today" does not care which repository a
 * change came from, and having to look in two places means looking in
 * one.
 *
 * They are visibly marked as local rather than blended in. A tester who
 * cannot tell "testboard changed" from "our environment changed" cannot
 * tell who to go to about it.
 *
 * A note whose date has no section of its own gets one, at the top: a
 * site note written between drops is the common case, and it is precisely
 * the news that has not been announced any other way.
 *
 * SECURITY: notes are operator-supplied text and reach the DOM through
 * textContent only, never innerHTML.
 *
 * Failure is silent by design. If /api/site-notes is unreachable, absent
 * or empty, this page still renders everything the build shipped — the
 * release notes are the content, and these are an addition to them.
 */

"use strict";

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

/** "2026-07-30" -> "30 July 2026", matching the headings already there. */
function longDate(iso) {
  const year = iso.slice(0, 4);
  const month = parseInt(iso.slice(5, 7), 10);
  const day = parseInt(iso.slice(8, 10), 10);
  if (!month || !day) {
    return iso;
  }
  return day + " " + MONTHS[month - 1] + " " + year;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = text;
  }
  return node;
}

/** Group notes by their date, preserving the order within each. */
export function groupByDate(notes) {
  const groups = new Map();
  for (const note of notes) {
    if (!groups.has(note.date)) {
      groups.set(note.date, []);
    }
    groups.get(note.date).push(note);
  }
  return groups;
}

/** The block of local notes appended under one date. */
function notesBlock(notes) {
  const box = el("div", "site-notes");
  box.appendChild(el("h3", "site-notes-head", "Also today, from this site"));
  box.appendChild(el("p", "site-notes-intro muted",
    "Changes here rather than in testboard itself — logged by whoever made "
    + "them."));
  const list = el("ul", "site-notes-list");
  for (const note of notes) {
    const item = el("li");
    item.appendChild(el("span", "site-note-text", note.text));
    if (note.author) {
      item.appendChild(el("span", "site-note-author", " — " + note.author));
    }
    list.appendChild(item);
  }
  box.appendChild(list);
  return box;
}

/** A whole section, for notes on a date the build shipped no notes for. */
function standaloneSection(date, notes) {
  const section = el("section", "release release-site-only");
  section.setAttribute("data-drop-date", date);
  const head = el("div", "section-head");
  head.appendChild(el("h2", "eyebrow", longDate(date)));
  head.appendChild(el("span", "muted", "From this site"));
  section.appendChild(head);
  section.appendChild(notesBlock(notes));
  return section;
}

async function init() {
  const main = document.querySelector("main");
  if (!main) {
    return;
  }
  let notes = [];
  try {
    const response = await fetch("api/site-notes");
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    notes = Array.isArray(payload.notes) ? payload.notes : [];
  } catch (err) {
    return;                  // the build's own notes are already on screen
  }
  if (notes.length === 0) {
    return;
  }

  const sections = Array.from(document.querySelectorAll("section.release"));
  const byDate = new Map();
  for (const section of sections) {
    const date = section.getAttribute("data-drop-date");
    if (date) {
      byDate.set(date, section);
    }
  }

  const groups = groupByDate(notes);
  // Array.from, NOT Array.prototype.slice.call: a Map iterator has no
  // `length`, so slice returns an empty array from it and the loop below
  // silently does nothing. That shipped once and rendered no notes at all
  // while every other part of the feature looked correct.
  //
  // Newest first, so an invented section lands above the drop below it.
  const dates = Array.from(groups.keys()).sort().reverse();
  for (const date of dates) {
    const existing = byDate.get(date);
    if (existing) {
      existing.appendChild(notesBlock(groups.get(date)));
    } else {
      main.insertBefore(
        standaloneSection(date, groups.get(date)), main.firstChild);
    }
  }
}

init();
