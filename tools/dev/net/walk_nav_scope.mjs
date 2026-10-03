/* walk_nav_scope.mjs — class 1/2 (NIGHT_RUN_2026-08-09.md §2): header
 * nav scope carriage, tested directly against nav.js's exported
 * carryScopeIntoNav(), the same function every page's real markup
 * calls (nav.js line ~179). Doing it as a direct function call rather
 * than a full page boot is deliberate: the header <nav> is static
 * markup, not something app.js constructs, so the DOM-shim's id-only
 * install has nothing to hydrate there -- this hand-builds the same
 * shape real markup carries (a parent with <a href="index.html">-style
 * children) and calls the exact exported function shipped markup
 * calls, which is a more precise oracle than re-deriving the rule.
 *
 * Per nav.js's own STREAM_AWARE_HREFS comment: index.html, time.html,
 * timeline.html receive stream/product/environment; actions.html and
 * watch.html deliberately do NOT (own reasoning documented there,
 * re-asserted here so a future edit that quietly widens or narrows the
 * list is caught).
 *
 * Run: node tools/dev/net/walk_nav_scope.mjs   (server on 8931)
 */
import { installDom, Element } from "./domshim.mjs";

const BASE = "http://127.0.0.1:8931";
let failures = 0;
function check(condition, label) {
  if (condition) {
    console.log("  ok  " + label);
  } else {
    failures += 1;
    console.log("FAIL  " + label);
  }
}

function buildNav(hrefs) {
  const nav = new Element("nav");
  for (const href of hrefs) {
    const a = new Element("a");
    a.setAttribute("href", href);
    a.tagName = "A";
    nav.appendChild(a);
  }
  return nav;
}

// Fresh DOM/window so nav.js's module-eval-time code (which reads
// window.location) has something to read; carryScopeIntoNav itself is
// then called directly with hand-built args, not through init(). No
// #nav-whatsnew element -> init()'s early return, so its async fetch
// never fires and never touches global fetch (unset here).
installDom([], BASE + "/index.html");
const { carryScopeIntoNav } = await import("../../../static/nav.js");

console.log("== Build-scoped origin (product+stream+environment all set) ==");
{
  const nav = buildNav([
    "index.html", "time.html", "timeline.html", "actions.html",
    "watch.html", "whatsnew.html",
  ]);
  carryScopeIntoNav(
    nav, "?product=Atlas&stream=2&environment=atlas-lab-alpha");
  // carryScopeIntoNav rewrites via `new URL(href, location)`, so a
  // touched link gains a leading "/" (url.pathname); an untouched one
  // keeps the bare relative href exactly as shipped -- match either.
  const hrefOf = (path) =>
    nav.children.find((c) => {
      const h = c.getAttribute("href");
      return h === path || h.startsWith(path) || h.startsWith("/" + path);
    }).getAttribute("href");

  for (const page of ["index.html", "time.html", "timeline.html"]) {
    const href = hrefOf(page);
    check(href.indexOf("product=Atlas") !== -1,
      page + " nav link carries product=Atlas: " + href);
    check(href.indexOf("stream=2") !== -1,
      page + " nav link carries stream=2: " + href);
    check(href.indexOf("environment=atlas-lab-alpha") !== -1,
      page + " nav link carries environment=atlas-lab-alpha: " + href);
  }
  check(hrefOf("actions.html") === "actions.html",
    "actions.html nav link untouched (own reasoning, not stream-aware): "
    + hrefOf("actions.html"));
  check(hrefOf("watch.html") === "watch.html",
    "watch.html nav link untouched (own c= grammar): " + hrefOf("watch.html"));
  check(hrefOf("whatsnew.html") === "whatsnew.html",
    "whatsnew.html nav link untouched (never scoped): "
    + hrefOf("whatsnew.html"));
}

console.log("\n== Unscoped origin: zero visible change ==");
{
  const nav = buildNav(["index.html", "time.html", "timeline.html"]);
  const before = nav.children.map((c) => c.getAttribute("href"));
  carryScopeIntoNav(nav, "");
  const after = nav.children.map((c) => c.getAttribute("href"));
  check(JSON.stringify(before) === JSON.stringify(after),
    "no params in URL -> nav hrefs byte-identical: " + JSON.stringify(after));
}

console.log("\n== Product-only origin (no stream/environment) ==");
{
  const nav = buildNav(["index.html", "timeline.html"]);
  carryScopeIntoNav(nav, "?product=Beacon");
  const href = nav.children[0].getAttribute("href");
  check(href.indexOf("product=Beacon") !== -1,
    "product carried: " + href);
  check(href.indexOf("stream=") === -1,
    "no stray stream= when origin had none: " + href);
  check(href.indexOf("environment=") === -1,
    "no stray environment= when origin had none: " + href);
}

console.log(failures === 0
  ? "\nALL NAV SCOPE CHECKS PASSED"
  : "\n" + failures + " CHECK(S) FAILED");
process.exit(failures === 0 ? 0 : 1);
