/* walk_picker_transitions.mjs — class 2 (NIGHT_RUN_2026-08-09.md §2):
 * drive the product switcher, the Build (stream) picker, and the
 * Compare-to control through real change events and assert the
 * resulting URL has no stale narrower params, and that an explicit
 * mainline choice on Compare-to is never ambiguous with absence
 * (SCOPED_URLS_PLAN.md §1 bug #6/#7 -- exactly what this walk targets).
 *
 * Run: node tools/dev/net/walk_picker_transitions.mjs   (server on $NET_PORT, default 8931)
 */
import { installDom } from "./domshim.mjs";
import { readFileSync } from "node:fs";

const BASE = "http://127.0.0.1:" + (process.env.NET_PORT || "8931");
// WP-28: see walk_index_scope.mjs's own comment on this same constant --
// NET_URL_PREFIX (run_net.py's --url-prefix) is the prefix this walk
// loads pages THROUGH; PAGE_BASE is what every relative href/fetch this
// walk makes or resolves is measured against, matching what a real
// browser under nginx would use (the document's own URL), not always
// the bare origin.
const PREFIX = (process.env.NET_URL_PREFIX || "").replace(/^\/|\/$/g, "");
const PAGE_BASE = BASE + (PREFIX ? "/" + PREFIX : "") + "/";
let failures = 0;
function check(condition, label) {
  if (condition) {
    console.log("  ok  " + label);
  } else {
    failures += 1;
    console.log("FAIL  " + label);
  }
}
const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const realFetch = globalThis.fetch;
globalThis.fetch = (url, opts) => {
  const raw = String(url);
  if (PREFIX && !raw.startsWith("http")) {
    // WP-28: see walk_index_scope.mjs's own comment on this same check --
    // complements currentUrl()'s href assertion below by covering the
    // OTHER thing a relative literal produces: a fetch call. A stray
    // root-absolute "/api/..." literal would still resolve and answer
    // correctly (the server always accepts bare paths), so only
    // asserting on the RESOLVED url (as currentUrl() does for hrefs)
    // would miss it -- this asserts on what app.js actually WROTE.
    check(!raw.startsWith("/"),
      "fetch(" + JSON.stringify(raw) + ") is prefix-relative, not "
      + "root-absolute");
  }
  return realFetch(raw.startsWith("http") ? raw : new URL(raw, PAGE_BASE).href,
    opts);
};
async function jget(path) { return (await (await fetch(path)).json()); }

// installDom() first (products.js's top-level adoptProductFromUrl() reads
// `window` at import-evaluation time), THEN import -- and as a dynamic
// import specifically, since a static `import` at file-top would be
// hoisted above the installDom() call regardless of source order.
// Imported ONCE, called directly per scenario below (rather than relying
// on app.js's transitive re-import of these modules to re-run their own
// top-level init()): Node's ESM cache keys on the resolved specifier, and
// app.js's own `import "./products.js"` / `import "./streams.js"` carry
// no cache-busting query, so across repeated `loadPage()` calls within
// ONE node process they resolve to the SAME cached module instance --
// its top-level `init()` already ran once (against the FIRST loadPage's
// DOM) and never runs again against a later call's fresh document. A
// real browser reloads the whole JS context per navigation and never
// hits this; it is a harness limitation, not an app behaviour, so the
// fix is to call the exported render functions directly instead of
// trusting the cached side effect.
installDom([], PAGE_BASE + "index.html");
const { renderSwitcher } = await import("../../../static/products.js");
const { renderPicker } = await import("../../../static/streams.js");

const atlasStreams = (await jget("api/streams?product=Atlas")).streams;
const byName = (name) => atlasStreams.find((s) => s.name === name);
const rcOld = byName("2026.9.0");
const rcNew = byName("2026.9.1");

const indexHtml = readFileSync(
  new URL("../../../static/index.html", import.meta.url), "utf-8");
const ALL_IDS = [...indexHtml.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
const HIDDEN_AT_LOAD = new Set(
  [...indexHtml.matchAll(/<[a-zA-Z0-9]+[^>]*\bid="([^"]+)"[^>]*>/g)]
    .filter((m) => /\shidden(\s|>|=)/.test(m[0]))
    .map((m) => m[1]));

async function loadPage(qs) {
  // WP-28: the page's own URL carries the prefix, exactly as a real
  // browser's address bar would after nginx served it through
  // /PREFIX/ -- see walk_index_scope.mjs's loadPage() for why this
  // matters (urls.js's window.location.href-based rewrites).
  const ids = installDom(ALL_IDS, PAGE_BASE + "index.html" + qs);
  for (const id of HIDDEN_AT_LOAD) ids[id].hidden = true;
  await import("../../../static/app.js?q=" + encodeURIComponent(qs)
    + "&t=" + Date.now());
  await settle(700);
  return ids;
}

function findById(root, id) {
  return root.find((n) => n.id === id)[0] || null;
}

function currentUrl() {
  // WP-24's urls.js (post-refactor) sometimes assigns window.location.href
  // a root-relative string ("/index.html?...", or under WP-28's prefix,
  // "/PREFIX/index.html?...") rather than always a full absolute one --
  // a real browser resolves that fine when you assign to location.href;
  // this shim does not, so a base is required here. Root-relative
  // strings resolve against PAGE_BASE's ORIGIN regardless of its own
  // path (same as a browser), so this is correct whether or not a
  // prefix is configured -- PAGE_BASE rather than the bare BASE only
  // matters if something ever assigns a page-RELATIVE (no leading "/")
  // href, which nothing here does today.
  const resolved = new URL(globalThis.window.location.href, PAGE_BASE);
  // WP-28's real acceptance test for this walk: after a picker
  // rewrite, the page's OWN url must still be under the prefix it
  // started under -- a rewrite that read window.location.href before
  // it carried the prefix (or dropped it while rebuilding the path)
  // would silently navigate the tab back to the bare root.
  check(resolved.href.startsWith(PAGE_BASE),
    "post-rewrite URL stays under the prefix (" + PAGE_BASE + "): "
    + resolved.href);
  return resolved;
}

/** Force-render the product switcher and Build picker fresh into THIS
 * page's containers (see the import-time comment above for why this is
 * necessary instead of trusting app.js's own wiring here). */
async function renderPickersFresh(ids, product, selectedStreamId) {
  const summary = await jget("api/summary?parts=headline");
  renderSwitcher(ids["product-switcher"], summary.products || []);
  const streamsResp = await jget(
    "api/streams?product=" + encodeURIComponent(product || ""));
  renderPicker(ids["stream-picker"], streamsResp.streams || [],
    selectedStreamId);
}

console.log("== Product switcher: Atlas (with stream+baseline+environment) "
  + "-> All products ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&baseline=" + rcOld.id + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  await renderPickersFresh(ids, "Atlas", rcNew.id);
  const select = findById(ids["product-switcher"], "product-switcher-select");
  check(select !== null, "product switcher select rendered (>=2 products)");
  if (select) {
    select.value = "";
    await select.dispatch("change");
    const url = currentUrl();
    // Post-WP-24 (coordinator, known intentional change): "All products"
    // now writes an EXPLICIT empty product= rather than deleting the
    // param (urls.js's documented encoding: product empty string => All
    // products). Accept either shape so this walk is meaningful on both
    // the pre- and post-WP-24 tip; what must NEVER happen either way is
    // product carrying a stale non-empty value.
    check(!url.searchParams.has("product") || url.searchParams.get("product") === "",
      "product param cleared or explicitly empty: " + url.search);
    check(!url.searchParams.has("stream"),
      "stale stream param NOT carried past a product change: " + url.search);
    check(!url.searchParams.has("baseline"),
      "stale baseline param NOT carried past a product change: " + url.search);
    check(!url.searchParams.has("environment"),
      "stale environment param NOT carried past a product change: "
      + url.search);
  }
}

console.log("\n== Product switcher: Atlas -> Beacon (cross-product) ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  await renderPickersFresh(ids, "Atlas", rcNew.id);
  const select = findById(ids["product-switcher"], "product-switcher-select");
  check(select !== null, "product switcher select rendered");
  if (select) {
    select.value = "Beacon";
    await select.dispatch("change");
    const url = currentUrl();
    check(url.searchParams.get("product") === "Beacon",
      "product switched to Beacon: " + url.search);
    check(!url.searchParams.has("stream"),
      "Atlas's stream id not carried into Beacon's scope: " + url.search);
    check(!url.searchParams.has("environment"),
      "Atlas's environment not carried into Beacon's scope: " + url.search);
  }
}

console.log("\n== Build picker: RC (stream+baseline set) -> Mainline ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&baseline=" + rcOld.id + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  await renderPickersFresh(ids, "Atlas", rcNew.id);
  const input = findById(ids["stream-picker"], "stream-picker-input");
  check(input !== null, "stream picker input rendered");
  if (input) {
    input.value = "Mainline nightlies";
    await input.dispatch("change");
    const url = currentUrl();
    check(!url.searchParams.has("stream"),
      "stream cleared going back to mainline: " + url.search);
    check(!url.searchParams.has("baseline"),
      "stale baseline NOT carried back to mainline: " + url.search);
    check(url.searchParams.get("environment") === "atlas-lab-bravo",
      "environment (orthogonal to stream) survives the change: "
      + url.search);
    check(url.searchParams.get("product") === "Atlas",
      "product (outer scope) survives the change: " + url.search);
  }
}

console.log("\n== Build picker: RC-old -> RC-new (same product, new stream) ==");
{
  const qs = "?product=Atlas&stream=" + rcOld.id
    + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  await renderPickersFresh(ids, "Atlas", rcOld.id);
  const input = findById(ids["stream-picker"], "stream-picker-input");
  check(input !== null, "stream picker input rendered");
  if (input) {
    const label = rcNew.kind + ":" + rcNew.name;
    input.value = label;
    await input.dispatch("change");
    const url = currentUrl();
    check(url.searchParams.get("stream") === String(rcNew.id),
      "stream switched to the new RC id: " + url.search);
    check(!url.searchParams.has("baseline"),
      "baseline reset on a stream change (a new scope re-derives its "
      + "own default): " + url.search);
    check(url.searchParams.get("environment") === "atlas-lab-bravo",
      "environment survives a same-product stream change: " + url.search);
  }
}

console.log("\n== Compare-to: explicit mainline choice encodes baseline=1, "
  + "not absence (bug #6) ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  const input = ids["compare-to-input"];
  check(input.hidden === false, "Compare-to control shown for a non-mainline "
    + "stream (un-gated by WP-25 for every stream, not just 'build')");
  input.value = "Mainline nightlies";
  await input.dispatch("change");
  const url = currentUrl();
  check(url.searchParams.get("baseline") === "1",
    "explicit mainline choice is baseline=1 (EXPLICIT, distinguishable "
    + "from absence): " + url.search);
}

console.log("\n== Compare-to: choosing another build sets baseline=<id> ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&environment=atlas-lab-bravo";
  const ids = await loadPage(qs);
  const input = ids["compare-to-input"];
  const label = rcOld.kind + ":" + rcOld.name;
  input.value = label;
  await input.dispatch("change");
  const url = currentUrl();
  check(url.searchParams.get("baseline") === String(rcOld.id),
    "baseline set to the chosen build's id: " + url.search);
}

console.log(failures === 0
  ? "\nALL PICKER TRANSITION CHECKS PASSED"
  : "\n" + failures + " CHECK(S) FAILED");
process.exit(failures === 0 ? 0 : 1);
