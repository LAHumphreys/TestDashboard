/* sorting.js — table column sorting, shared by every table that has it.
 *
 * There are two kinds of sortable table here and they are NOT the same
 * problem:
 *
 *   - A table showing a COMPLETE result set can be sorted in the
 *     browser. Nothing is hidden, so reordering it tells the truth.
 *   - A table showing ONE PAGE of a larger result set must be sorted by
 *     the server. Sorting the fetched page and presenting it as sorted
 *     is a lie: "the slowest test" becomes "the slowest test among the
 *     hundred that happen to be loaded", which reads identically and is
 *     wrong.
 *
 * This module handles the first kind, and the header-click plumbing —
 * arrows, aria-sort, the direction toggle — for both, so a server-sorted
 * table and a client-sorted one look and behave the same to a user.
 */

"use strict";

/**
 * Wire up the sortable headers of one table.
 *
 * `table`      — the <table> (its <th> buttons carry data-key).
 * `onSort`     — called with (key, descending) whenever the sort changes.
 * `initial`    — {key, descending} to show as active at the start.
 *
 * Returns an object with `set(key, descending)` so a caller that sorts
 * server-side can keep the indicators in step with what it asked for.
 */
export function attachSorting(table, onSort, initial) {
  const state = {
    key: (initial && initial.key) || null,
    descending: Boolean(initial && initial.descending),
  };

  const buttons = table.querySelectorAll("thead .sort-btn");

  function paint() {
    for (const button of buttons) {
      const th = button.closest("th");
      const active = button.dataset.key === state.key;
      th.setAttribute(
        "aria-sort",
        active ? (state.descending ? "descending" : "ascending") : "none");
      const arrow = button.querySelector(".sort-arrow");
      if (arrow) {
        arrow.textContent = active ? (state.descending ? "▾" : "▴") : "";
      }
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.disabled) {
        return;
      }
      const key = button.dataset.key;
      // Same column toggles direction; a new column starts ascending,
      // which is what people expect from every other table they use.
      state.descending = key === state.key ? !state.descending : false;
      state.key = key;
      paint();
      onSort(state.key, state.descending);
    });
  }

  paint();
  return {
    set: (key, descending) => {
      state.key = key;
      state.descending = Boolean(descending);
      paint();
    },
    get: () => ({ key: state.key, descending: state.descending }),
    /**
     * Turn the controls off, with a reason.
     *
     * Used when a table is showing a truncated slice of a larger set:
     * reordering it would misrepresent the whole. Better a control that
     * says why it is unavailable than one that quietly lies.
     */
    disable: (reason) => {
      for (const button of buttons) {
        button.disabled = true;
        button.title = reason;
      }
      table.classList.add("sort-disabled");
    },
    enable: () => {
      for (const button of buttons) {
        button.disabled = false;
        button.removeAttribute("title");
      }
      table.classList.remove("sort-disabled");
    },
  };
}

/**
 * Sort an array of plain objects by `key`, in place-safe fashion.
 *
 * Strings compare with localeCompare (so "b" < "C"), everything else
 * numerically, and nulls always sort last regardless of direction —
 * "no value" is not a small value, and letting it lead a descending
 * list buries the rows somebody asked to see.
 */
export function sortRows(rows, key, descending) {
  const sorted = rows.slice();
  sorted.sort((left, right) => {
    const a = left[key];
    const b = right[key];
    if (a === b) {
      return 0;
    }
    if (a === null || a === undefined) {
      return 1;
    }
    if (b === null || b === undefined) {
      return -1;
    }
    let order;
    if (typeof a === "string" && typeof b === "string") {
      order = a.localeCompare(b);
    } else {
      order = a < b ? -1 : 1;
    }
    return descending ? -order : order;
  });
  return sorted;
}
