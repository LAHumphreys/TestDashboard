/* metrics.js — the Metrics page (WP-37).
 *
 * What the server has been doing since it started — which requests, how
 * long, how much of that was waiting for a worker, which storage
 * methods the time went into — and how big the database is, without
 * anybody logging on to the server to find out.
 *
 * ONE request, made when the page opens or Refresh is pressed. There is
 * no timer in this file and there must not be one: the counters are
 * free to keep and nearly free to read, but a page that polled would be
 * traffic of its own in the very figures it shows, and the database
 * sizes behind it are real queries.
 *
 * Every figure and every word that depends on one comes from the
 * response. Nothing here knows how many workers there are, what engine
 * is underneath, or what the histogram's edges are.
 *
 * SECURITY: a request's target carries test names and search text typed
 * by users. All dynamic text reaches the DOM via textContent/el(), never
 * innerHTML.
 */

"use strict";

import {
  clearError,
  clearNode,
  el,
  fetchJson,
  formatTime,
  postJson,
  showError,
} from "./api.js";
import { apiUrl } from "./urls.js";

/** This page is about the server, not about a product or a build. */
const NO_SCOPE = {
  product: null, stream: null, baseline: null, environment: null,
};

function byId(id) {
  return document.getElementById(id);
}

function whole(number) {
  return Number(number).toLocaleString();
}

/** "0.4 ms", "12 ms", "3.2 s" — three figures at most, never more
 * precision than a person can use. */
function duration(ms) {
  if (ms === null || ms === undefined) {
    return "—";
  }
  if (ms < 10) {
    return ms.toFixed(1) + " ms";
  }
  if (ms < 1000) {
    return Math.round(ms) + " ms";
  }
  if (ms < 60000) {
    return (ms / 1000).toFixed(1) + " s";
  }
  return (ms / 60000).toFixed(1) + " min";
}

function size(bytes) {
  if (bytes === null || bytes === undefined) {
    return "—";
  }
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return (unit === 0 ? String(value) : value.toFixed(value < 10 ? 2 : 1))
    + " " + units[unit];
}

function span(seconds) {
  if (seconds < 90) {
    return Math.round(seconds) + " seconds";
  }
  if (seconds < 5400) {
    return Math.round(seconds / 60) + " minutes";
  }
  if (seconds < 172800) {
    return (seconds / 3600).toFixed(1) + " hours";
  }
  return (seconds / 86400).toFixed(1) + " days";
}

function row(cells) {
  const tr = document.createElement("tr");
  for (const cell of cells) {
    const td = el("td", cell.wrap ? "wrap" : "", cell.text);
    if (cell.title) {
      td.title = cell.title;
    }
    tr.appendChild(td);
  }
  return tr;
}

function emptyRow(body, columns, text) {
  const tr = document.createElement("tr");
  const td = el("td", "muted", text);
  td.colSpan = columns;
  tr.appendChild(td);
  body.appendChild(tr);
}

/** The histogram's own answer: the bucket the 95th request fell in. */
function within(entry, edges) {
  if (entry.p95_ms !== null && entry.p95_ms !== undefined) {
    return duration(entry.p95_ms);
  }
  return entry.count
    ? "over " + duration(edges[edges.length - 1]) : "—";
}

function renderActivity(activity) {
  const since = byId("metrics-since");
  const idle = byId("metrics-not-collecting");
  const requests = byId("metrics-requests-body");
  const slowest = byId("metrics-slowest-body");
  const storage = byId("metrics-storage-body");
  clearNode(requests);
  clearNode(slowest);
  clearNode(storage);
  byId("metrics-reset").disabled = !activity.collecting;
  if (!activity.collecting) {
    since.textContent = "";
    byId("metrics-requests-meta").textContent = "";
    idle.textContent = "This server was started with --no-metrics, so "
      + "requests and storage calls are not being counted. The "
      + "database figures below are read on request and are not "
      + "affected.";
    idle.hidden = false;
    emptyRow(requests, 9, "Not collected.");
    emptyRow(slowest, 7, "Not collected.");
    emptyRow(storage, 5, "Not collected.");
    return;
  }
  idle.hidden = true;
  since.textContent = "Counting since " + formatTime(activity.since)
    + " UTC — " + span(activity.seconds) + ".";
  byId("metrics-requests-meta").textContent =
    whole(activity.totals.requests) + " requests, "
    + whole(activity.totals.errors) + " answered with a server error";

  for (const entry of activity.requests) {
    requests.appendChild(row([
      { text: entry.route, wrap: true },
      { text: whole(entry.count)
        + (entry.errors ? " (" + whole(entry.errors) + " failed)" : "") },
      { text: duration(entry.mean_ms) },
      { text: within(entry, activity.bucket_edges_ms) },
      { text: duration(entry.max_ms) },
      { text: duration(entry.queue_mean_ms) },
      { text: String(entry.storage_calls_mean) },
      { text: duration(entry.storage_mean_ms) },
      { text: duration(entry.total_ms) },
    ]));
  }
  if (!activity.requests.length) {
    emptyRow(requests, 9, "Nothing has been requested yet.");
  }

  for (const entry of activity.slowest) {
    slowest.appendChild(row([
      { text: formatTime(entry.at) },
      { text: entry.target, wrap: true, title: entry.route },
      { text: entry.status === null ? "—" : String(entry.status) },
      { text: duration(entry.ms) },
      { text: duration(entry.queue_ms) },
      { text: String(entry.storage_calls) },
      { text: duration(entry.storage_ms) },
    ]));
  }
  if (!activity.slowest.length) {
    emptyRow(slowest, 7, "Nothing has been requested yet.");
  }

  for (const entry of activity.storage) {
    storage.appendChild(row([
      { text: entry.method },
      { text: whole(entry.calls) },
      { text: duration(entry.mean_ms) },
      { text: duration(entry.max_ms) },
      { text: duration(entry.total_ms) },
    ]));
  }
  if (!activity.storage.length) {
    emptyRow(storage, 5, "No storage method has been called yet.");
  }
}

function renderMemo(memo) {
  const asked = memo.hits + memo.misses;
  const line = byId("metrics-memo");
  if (asked === 0) {
    line.textContent = "Nothing has asked the memo for anything yet.";
    return;
  }
  line.textContent = whole(memo.hits) + " of " + whole(asked)
    + " answers came from the memo ("
    + Math.round(100 * memo.hits / asked) + "%); " + whole(memo.misses)
    + " had to be computed. It has been cleared " + whole(memo.clears)
    + (memo.clears === 1 ? " time" : " times")
    + " by a write — an import, an assignment, a comment, a "
    + "retirement — and each clearing puts the next load of every "
    + "page back to its full cost. It holds " + whole(memo.entries)
    + (memo.entries === 1 ? " answer" : " answers") + " now.";
}

function renderDatabase(database) {
  byId("metrics-database-meta").textContent =
    "read " + formatTime(database.as_of) + " UTC; kept for "
    + database.kept_seconds + " seconds";
  byId("metrics-database-summary").textContent =
    database.engine + " " + database.version + ", schema version "
    + database.schema_version + ", " + size(database.bytes)
    + " on disk, " + database.connections
    + (database.connections === 1 ? " connection" : " connections")
    + ". Runs on record from " + formatTime(database.oldest_run)
    + " to " + formatTime(database.newest_run) + " UTC.";

  const parts = byId("metrics-parts-body");
  clearNode(parts);
  for (const part of database.parts) {
    parts.appendChild(row([
      { text: part.label, wrap: true }, { text: size(part.bytes) },
    ]));
  }

  const names = Object.keys(database.rows).sort(
    (a, b) => database.rows[b] - database.rows[a]);
  const sized = Object.keys(database.tables).length > 0;
  byId("metrics-rows-size-head").hidden = !sized;
  const rows = byId("metrics-rows-body");
  clearNode(rows);
  for (const name of names) {
    const cells = [
      { text: name }, { text: whole(database.rows[name]) },
    ];
    if (sized) {
      const table = database.tables[name];
      cells.push({ text: table ? size(table.bytes) : "—" });
    }
    rows.appendChild(row(cells));
  }
  for (const name of database.rows_not_counted) {
    const cells = [{ text: name }, { text: "not counted" }];
    if (sized) {
      const table = database.tables[name];
      cells.push({ text: table ? size(table.bytes) : "—" });
    }
    rows.appendChild(row(cells));
  }
  byId("metrics-rows-note").textContent =
    "“runs” is the sum of the hourly activity table, which is "
    + "kept equal to it as runs are written — counting the runs "
    + "themselves means reading all of them. "
    + database.rows_not_counted.join(", ")
    + (database.rows_not_counted.length === 1 ? " is" : " are")
    + " not counted for the same reason."
    + (sized ? " Sizes are the database server’s own figures." : "");

  const streams = byId("metrics-streams-body");
  clearNode(streams);
  for (const stream of database.streams) {
    streams.appendChild(row([
      { text: stream.kind === "mainline"
        ? "mainline" : stream.kind + " " + stream.name, wrap: true },
      { text: stream.product || "—" },
      { text: whole(stream.tests) },
      { text: whole(stream.environments) },
    ]));
  }
}

async function load() {
  clearError();
  let data;
  try {
    data = await fetchJson(apiUrl("api/metrics", null, NO_SCOPE));
  } catch (err) {
    showError(err.message);
    return;
  }
  renderActivity(data.activity);
  renderMemo(data.memo);
  renderDatabase(data.database);
}

async function reset() {
  try {
    await postJson(apiUrl("api/metrics/reset", null, NO_SCOPE), {});
  } catch (err) {
    showError(err.message);
    return;
  }
  await load();
}

function init() {
  byId("reload-btn").addEventListener("click", load);
  byId("metrics-reset").addEventListener("click", reset);
  load();
}

init();
