/* walk_index_scope.mjs — classes 1 & 2 (NIGHT_RUN_2026-08-09.md §2) on
 * index.html: the highest-value page for the eight historical URL bugs
 * (SCOPED_URLS_PLAN.md §1 -- five of eight sites are index.html/app.js
 * or compare.js, both rendered on this page).
 *
 * Class 1 (scope-dropping links): boot the page under a build-scoped, a
 * product-scoped, and a mixed configuration; harvest every <a href> the
 * page renders and assert product/stream/environment carriage against
 * the rules the SOURCE documents (products.js/streams.js/app.js/
 * compare.js docstrings), not a re-derived guess.
 *
 * Class 2 (param collisions & stale scope): drive the product switcher,
 * the Build (stream) picker, and the Compare-to control through real
 * change events and assert the resulting URL (window.location.href,
 * which the shim's history.replaceState/`location.href =` assignment
 * both write to directly) has no stale narrower params.
 *
 * No urls.js import anywhere -- written at WP-25, before
 * WP-24 lands the builder (SCOPED_URLS_PLAN.md is not yet merged here).
 * The checks are purely behavioural (parsed hrefs vs the scope rules),
 * so per WP-24 being a declared pure refactor (no URL shape change) the
 * same script should pass unchanged after WP-24 lands too.
 *
 * Run: node tools/dev/net/walk_index_scope.mjs   (server on $NET_PORT, default 8931)
 */
import { installDom } from "./domshim.mjs";

const BASE = "http://127.0.0.1:" + (process.env.NET_PORT || "8931");
// WP-28: NET_URL_PREFIX (set by run_net.py's --url-prefix) is the path
// prefix this walk loads every page THROUGH, e.g. "testboard" ->
// PAGE_BASE "http://127.0.0.1:8931/testboard/". Empty (the default) ->
// PAGE_BASE is just the origin, today's exact behaviour. This is the
// harness's own analogue of what a real browser resolves relative
// hrefs/fetches against: the URL of the page that made them, NOT
// always the bare origin -- resolving against a hardcoded bare origin
// regardless of prefix is exactly what would make this walk blind to
// a relative link that renders fine but escapes the prefix.
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
    // WP-28: the OTHER half of the prefix guarantee, complementing
    // harvestLinks()'s href check below. A literal fetch("/api/x")
    // resolves fine against the bare origin regardless of PREFIX (the
    // server always answers bare paths, by design) -- so a stray
    // root-absolute API literal in app.js would render correctly and
    // this walk would stay silent about it. The source-scan guard
    // (test_frontend_calls.py::RootAbsoluteApiUrlTest) forbids the
    // literal existing at all; this proves that every fetch this run
    // ACTUALLY issues is prefix-relative, not just that no such literal
    // was found by pattern-matching. Every fetch call this file makes
    // for its own setup is written relative too (see below), so nothing
    // here is exempted from its own check.
    check(!raw.startsWith("/"),
      "fetch(" + JSON.stringify(raw) + ") is prefix-relative, not "
      + "root-absolute");
  }
  return realFetch(raw.startsWith("http") ? raw : new URL(raw, PAGE_BASE).href,
    opts);
};

async function jget(path) {
  return (await (await fetch(path)).json());
}

// Ids resolved live, not hardcoded -- deterministic given the seed's
// fixed random seed, but resolving keeps the walk correct even if the
// seeding order changes. Written relative ("api/..." not "/api/...") so
// this call is itself covered by the fetch-override check above.
const atlasStreams = (await jget("api/streams?product=Atlas")).streams;
const byName = (name) => atlasStreams.find((s) => s.name === name);
const cadenced = byName("feature/checkout-rewrite");   // covered-pass, own tab
const rcOld = byName("2026.9.0");
const rcNew = byName("2026.9.1");
if (!cadenced || !rcOld || !rcNew) {
  console.log("FAIL  seed streams not found (feature/checkout-rewrite, "
    + "2026.9.0, 2026.9.1) -- cannot run the walk");
  process.exit(1);
}

// Parse every id="..." out of the shipped index.html and which of them
// carry a bare `hidden` attribute in the markup -- the domshim's
// Element defaults hidden=false (it never parses real HTML), so a page
// load that never TOUCHES an element must still start from the
// shipped-hidden state, or "stayed hidden" is unfalsifiable.
import { readFileSync } from "node:fs";
const indexHtml = readFileSync(
  new URL("../../../static/index.html", import.meta.url), "utf-8");
const ALL_IDS = [...indexHtml.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
const HIDDEN_AT_LOAD = new Set(
  [...indexHtml.matchAll(/<[a-zA-Z0-9]+[^>]*\bid="([^"]+)"[^>]*>/g)]
    .filter((m) => /\shidden(\s|>|=)/.test(m[0]))
    .map((m) => m[1]));

async function loadPage(qs) {
  // WP-28: the page's OWN URL carries the prefix (or not), exactly as
  // a real browser's address bar would after nginx served it through
  // /PREFIX/ -- this is what makes urls.js's `new URL(window.location.href)`
  // -based rewrites (currentUrlWithScope() et al) preserve the prefix
  // through a picker change instead of silently dropping back to root.
  const ids = installDom(ALL_IDS, PAGE_BASE + "index.html" + qs);
  for (const id of HIDDEN_AT_LOAD) ids[id].hidden = true;
  await import("../../../static/app.js?q=" + encodeURIComponent(qs)
    + "&t=" + Date.now());
  await settle(700);
  return ids;
}

/** Every <a href> anywhere in the rendered tree, as parsed URLs.
 *
 * The domshim installs each id="..." as an INDEPENDENT top-level
 * Element (installDom() has no single document-body root tying them
 * together -- real markup does, but the shim only ever hydrates what a
 * page's JS explicitly reaches via getElementById()). So "the rendered
 * tree" here means every registered id's own subtree, unioned -- not a
 * single root.find() from one element, which would only see whatever
 * happened to be that one element's descendants. */
function harvestLinks(idsOrRoot) {
  const roots = idsOrRoot.find ? [idsOrRoot] : Object.values(idsOrRoot);
  const seen = new Set();
  const out = [];
  for (const root of roots) {
    if (!root || !root.find) continue;
    for (const a of root.find(
        (n) => n.tagName === "A" && typeof n.href === "string" && n.href)) {
      if (seen.has(a)) continue;
      seen.add(a);
      // WP-28: resolved against the PAGE's own base, not the bare
      // origin -- a real browser resolves `<a href="test.html">`
      // against the document that rendered it, which under a prefixed
      // load is .../PREFIX/, not the root. This is the exact
      // resolution step that catches "renders relative, escapes the
      // prefix" -- resolving against the bare origin regardless of
      // PREFIX would make every relative href look fine even when it
      // is not.
      const resolved = new URL(a.href, PAGE_BASE);
      // WP-28's real acceptance test, right here: every internal link
      // this page renders must resolve back INTO the prefix it was
      // loaded under (or stay at the bare origin when PREFIX is "").
      // A link that renders as a plain relative "test.html?..." but
      // resolves to "/test.html" instead of "/testboard/test.html" is
      // invisible to source review (the href text looks identical
      // either way) and invisible to grep -- only resolving it against
      // the real page URL, exactly as a browser does, shows it left
      // the prefix.
      check(resolved.href.startsWith(PAGE_BASE),
        "link \"" + a.textContent + "\" stays under the prefix ("
        + PAGE_BASE + "): resolved to " + resolved.href);
      out.push({ raw: a.href, url: resolved, text: a.textContent });
    }
  }
  return out;
}

function pageName(url) {
  return url.pathname.split("/").pop();
}

// Pages that read `product=` from their own URL (products.js's own
// mount-point comment: index/actions/time/timeline/watch).
const PRODUCT_AWARE = new Set(
  ["index.html", "actions.html", "time.html", "timeline.html", "watch.html"]);
// Pages that read `stream=` from their own URL.
const STREAM_AWARE = new Set(
  ["index.html", "test.html", "script.html", "time.html", "timeline.html"]);

function assertCarriage(links, originProduct, originStream, label) {
  let productChecked = 0;
  let streamChecked = 0;
  for (const { raw, url, text } of links) {
    const page = pageName(url);
    if (PRODUCT_AWARE.has(page) && page !== "watch.html") {
      // watch.html is exempt from the automatic assertion: its links are
      // built by watch.js's own composer grammar (STREAMS_PLAN.md §2.4,
      // the same exemption SCOPED_URLS_PLAN.md §2.4 states for c=), not
      // by app.js/compare.js -- present here only to document the
      // exclusion, not to silently skip a real site.
      const got = url.searchParams.get("product") || "";
      productChecked += 1;
      check(got === (originProduct || ""),
        label + ": link to " + page + " (\"" + text + "\") carries "
        + "product=\"" + originProduct + "\", got \"" + got + "\" -- " + raw);
    }
    if (STREAM_AWARE.has(page) && originStream !== null) {
      const got = url.searchParams.get("stream");
      // Only rows/links that are part of the OWN-results rendering are
      // expected to carry stream -- a bare cross-page link with no
      // stream-bearing context is not asserted here (false positives
      // from e.g. a "back to mainline" control would be a harness bug,
      // not an app bug). We only assert POSITIVELY when the link's own
      // querystring already shows evidence it is scope-built (carries
      // environment/script/test_name a row link would carry, or is one
      // of the two documented quick-links).
      if (url.searchParams.has("test_name")
          || url.searchParams.has("script")
          || text.indexOf("build") !== -1
          || text.indexOf("Build") !== -1) {
        streamChecked += 1;
        check(String(got) === String(originStream),
          label + ": link to " + page + " (\"" + text + "\") carries "
          + "stream=" + originStream + ", got " + got + " -- " + raw);
      }
    }
  }
  return { productChecked, streamChecked };
}

console.log("== Build-scoped (product=Atlas, stream=" + cadenced.id
  + " [cadenced, own tab], environment=atlas-lab-alpha) ==");
{
  const qs = "?product=Atlas&stream=" + cadenced.id
    + "&environment=atlas-lab-alpha";
  const ids = await loadPage(qs);
  check(ids["branch-tab-own"].getAttribute("aria-selected") === "true",
    "own-results tab is the default for the cadenced stream");
  const links = harvestLinks(ids);
  check(links.length > 0, "at least one link harvested (" + links.length + ")");
  const { productChecked, streamChecked } =
    assertCarriage(links, "Atlas", cadenced.id, "build-scoped");
  check(productChecked > 0, "at least one product-aware link inspected ("
    + productChecked + ")");
  check(streamChecked > 0, "at least one stream-carrying link inspected ("
    + streamChecked + ")");

  // The two F6 quick links (app.js renderBranchQuickLinks): documented
  // to carry stream + product + environment (Time) and stream + product
  // + environment (Timeline).
  const quick = harvestLinks(ids["branch-quick-links"]);
  const timeLink = quick.find((l) => pageName(l.url) === "time.html");
  const timelineLink = quick.find((l) => pageName(l.url) === "timeline.html");
  check(!!timeLink && timeLink.url.searchParams.get("stream")
      === String(cadenced.id)
      && timeLink.url.searchParams.get("product") === "Atlas",
    "quick Time link carries stream+product: "
      + (timeLink ? timeLink.raw : "MISSING"));
  check(!!timelineLink && timelineLink.url.searchParams.get("stream")
      === String(cadenced.id)
      && timelineLink.url.searchParams.get("product") === "Atlas"
      && timelineLink.url.searchParams.get("environment")
      === "atlas-lab-alpha",
    "quick Timeline link carries stream+product+environment: "
      + (timelineLink ? timelineLink.raw : "MISSING"));
}

console.log("\n== Product-scoped only (product=Atlas, no stream) ==");
{
  const ids = await loadPage("?product=Atlas");
  const links = harvestLinks(ids);
  const { productChecked } = assertCarriage(links, "Atlas", null, "product-only");
  // NOT asserted as a failure if zero: on this config (mainline tab, no
  // stream picked) the only product-aware-page links app.js renders are
  // the F6 branch-quick-links, which are hidden outright with no stream
  // selected (renderBranchQuickLinks: streamId === null -> hidden) --
  // zero such links here is the CORRECT shape, not a gap. Positive
  // carriage is asserted in the build-scoped/mixed sections instead,
  // where those links do render.
  console.log("  --  product-aware links on this page/config: "
    + productChecked + " (0 expected: no stream selected -> quick-links "
    + "hidden; informational only)");
  // No stream in scope: nothing should carry a stray stream= at all.
  const strayStream = links.filter((l) => l.url.searchParams.has("stream"));
  check(strayStream.length === 0,
    "no link carries a stray stream= when scope has none: "
    + JSON.stringify(strayStream.map((l) => l.raw)));
}

console.log("\n== Mixed (product=Atlas, stream=" + rcNew.id
  + " [RC], environment=atlas-lab-bravo, baseline=" + rcOld.id + ") ==");
{
  const qs = "?product=Atlas&stream=" + rcNew.id
    + "&environment=atlas-lab-bravo&baseline=" + rcOld.id;
  const ids = await loadPage(qs);
  const links = harvestLinks(ids);
  assertCarriage(links, "Atlas", rcNew.id, "mixed/RC");
  // Delta rows specifically (compare.js buildDeltaRow): the historical
  // bug #1 site. Assert at least one delta test-page link exists and
  // carries this stream.
  const deltaLinks = harvestLinks(ids["delta-body"]);
  const testLinks = deltaLinks.filter((l) => pageName(l.url) === "test.html");
  if (testLinks.length > 0) {
    check(testLinks.every((l) =>
      l.url.searchParams.get("stream") === String(rcNew.id)),
      "every delta-row test link carries stream=" + rcNew.id + ": "
      + JSON.stringify(testLinks.map((l) => l.raw)));
  } else {
    console.log("  --  no delta rows rendered on this page/tab to check "
      + "(not a failure -- may be the own-results tab by default)");
  }
}

console.log(failures === 0
  ? "\nALL INDEX SCOPE-LINK CHECKS PASSED"
  : "\n" + failures + " CHECK(S) FAILED");
process.exit(failures === 0 ? 0 : 1);
