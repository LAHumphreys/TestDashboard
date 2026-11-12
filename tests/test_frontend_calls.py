"""The frontend must not hammer the server.

There is no JavaScript test runner here and there is not going to be:
"no npm, no build step" is a project constraint, and a browser is not
available on the deployment target. So the properties that matter are
asserted against the source, the way ``test_python36_compat.py`` asserts
Python-version properties against the source.

Only one class of property is worth testing this way: the kind that is
invisible in review, silent in production, and expensive. The one that
prompted this file is exactly that. ``loadUsers()`` cached the array it
resolved to rather than the promise, and the assignment happened after
an ``await``:

    let knownUsers = null;
    async function loadUsers() {
      if (knownUsers === null) {              // still null for everyone
        knownUsers = await fetchJson(...);    // ...until this resolves
      }
    }

``assigneeSelect()`` is a per-row cell builder and the row loop is
synchronous, so every row on a 250-row page passed that guard before the
first response arrived. One page issued 250 concurrent requests for one
identical list. Nothing in the UI looked wrong; it only showed up in a
production network log.

The assertion with teeth is that there is exactly ONE place in the
frontend that fetches the user list. Every reintroduction of this bug
begins with a second one.

Python 3.6 compatible; standard library only.
"""

import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from typing import Any, Dict, List, Tuple

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def read(name: str) -> str:
    """Return a frontend file's text.

    Read as bytes and decoded rather than opened as text: ``app.js``
    contains a legitimate ``join("\\0")`` separator, which makes tools
    that sniff for NUL treat the file as binary. A test that silently
    matched nothing would pass forever.

    Line endings are then normalised to ``\\n``, which reading in binary
    would otherwise leave alone. Every assertion in this module is about
    the STRUCTURE of the JavaScript — that a call sits at top level, that
    a guard clause returns — and several spell that structure with an
    explicit ``\\n``. On a Windows checkout with ``core.autocrlf=true``
    those files arrive CRLF, so two such tests failed on content that was
    entirely correct, in every fresh worktree, while passing on Linux CI.
    Git stores these blobs as LF, so normalising here compares against
    what the repository actually contains rather than against whatever
    the checkout happened to write to disk. The assertions are unchanged.
    """
    with io.open(os.path.join(STATIC_DIR, name), "rb") as handle:
        text = handle.read().decode("utf-8")
    return text.replace("\r\n", "\n")


def scripts() -> Dict[str, str]:
    """Every frontend .js file, by name."""
    return {
        name: read(name)
        for name in sorted(os.listdir(STATIC_DIR))
        if name.endswith(".js")
    }


def fetch_sites(source: str, endpoint: str) -> List[int]:
    """Line numbers where ``source`` names ``endpoint`` in a call."""
    pattern = re.compile(r'["\']' + re.escape(endpoint))
    return [
        number for number, line in enumerate(source.split("\n"), 1)
        if pattern.search(line)
    ]


class CoverageTest(unittest.TestCase):
    """The scan has to actually be looking at the frontend."""

    def test_the_frontend_files_are_found_and_read(self) -> None:
        found = scripts()
        self.assertGreaterEqual(len(found), 5, sorted(found))
        for name in ("api.js", "app.js", "test.js"):
            self.assertIn(name, found)
            self.assertGreater(len(found[name]), 1000, name)

    def test_files_containing_a_nul_byte_are_still_read(self) -> None:
        """A composite key is joined with \\0; that is not corruption.

        The separator has to be a character that cannot occur inside an
        environment, script or test name, or two different tests collide
        on one key. Tools that sniff for NUL call the file binary and
        read nothing from it, and a scan that silently matched nothing
        would pass every test in this module forever.

        It used to live in app.js and moved to review.js with
        ``entryKey``. Asserting on a specific filename made this test
        fail for the move rather than for the property, so it now asks
        the question it actually cares about: at least one scanned file
        contains a NUL, and it was read anyway.
        """
        with_nul = sorted(
            name for name, source in scripts().items() if "\x00" in source
        )
        self.assertTrue(
            with_nul,
            "no scanned file contains a NUL; if the composite key "
            "separator changed, this guard is no longer proving that "
            "such a file can be read")
        for name in with_nul:
            self.assertGreater(len(scripts()[name]), 1000, name)


#: Files under static/ still spelling a NUL as the byte itself rather
#: than as the escape "\u0000" (the same string at run time). Each is
#: named so that this list can only shrink: respelling one fails
#: NoNulByteTest until its name is removed here. (actions.js is outside
#: WP-44 item 17's scope; CoverageTest's NUL read relies on it.)
_NUL_BYTE_FILES = ("actions.js",)


def _nul_byte_files(directory: str) -> List[str]:
    """The files under *directory*, recursively and by path relative to
    it, that contain a NUL byte."""
    found = []  # type: List[str]
    for parent, _dirs, names in os.walk(directory):
        for name in names:
            path = os.path.join(parent, name)
            with io.open(path, "rb") as handle:
                if b"\x00" in handle.read():
                    found.append(
                        os.path.relpath(path, directory).replace(os.sep, "/"))
    return sorted(found)


class NoNulByteTest(unittest.TestCase):
    """WP-44 item 17: test.js held a literal NUL inside a string, which
    made search tools call it binary and the editing tools refuse it. A
    NUL in a string is spelled as its escape; no file under static/
    holds the byte, bar the ones named in _NUL_BYTE_FILES."""

    def test_no_static_file_holds_a_nul_byte(self) -> None:
        self.assertEqual(_nul_byte_files(STATIC_DIR),
                         sorted(_NUL_BYTE_FILES))

    def test_the_sentinel_is_spelled_as_its_escape(self) -> None:
        self.assertIn('const NEW_USER_SENTINEL = "\\u0000new-user";',
                      read("test.js"))

    def test_the_detector_can_fail(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            os.mkdir(os.path.join(scratch, "sub"))
            for name, data in (("clean.js", b'const a = "\\u0000x";\n'),
                               ("sub/key.js", b'const a = "\x00x";\n')):
                with io.open(os.path.join(scratch, name), "wb") as handle:
                    handle.write(data)
            self.assertEqual(_nul_byte_files(scratch), ["sub/key.js"])


class UrlsModuleTest(unittest.TestCase):
    """urls.js (WP-24, docs/SCOPED_URLS_PLAN.md): the one module that
    builds every scoped URL, both navigation hrefs and /api/... fetch
    query strings. Same "assert against the source" method every other
    class in this file uses (there is no JS runtime here — see the
    module docstring) — these are urls.js's OWN unit tests, standing in
    for the ones a real JS test runner would carry, since "no npm, no
    build step" rules one out (CLAUDE.md constraint 3)."""

    def test_the_public_surface_is_exactly_the_spec_names(self) -> None:
        code = read("urls.js")
        for name in ("currentScope", "pageUrl", "apiUrl", "withStream",
                     "withBaseline", "withProduct"):
            self.assertIn(
                "export function " + name + "(", code,
                name + " is missing from urls.js's public surface")

    def test_current_scope_reads_only_location_search(self) -> None:
        body = _function_body(read("urls.js"), "export function currentScope(")
        self.assertIn("window.location.search", body)
        self.assertNotIn("localStorage", body)

    def test_current_scope_distinguishes_absent_from_explicit_empty(
        self
    ) -> None:
        """product's "" ("All products", explicit) vs null (absent,
        say nothing) is the encoding the adoption rule depends on —
        currentScope() must echo an explicit `?product=` back as ""
        rather than folding it into null the way every other level
        does (none of the other three has a meaningful empty state)."""
        body = _function_body(read("urls.js"), "export function currentScope(")
        self.assertIn("params.has(key) ? params.get(key) : null", body)

    def test_product_resets_stream_baseline_and_environment(self) -> None:
        """The hierarchy rule: product contains stream and environment,
        so naming it as an override resets both — and stream contains
        baseline, so product's reset reaches it too."""
        body = _function_body(read("urls.js"), "function resolveScope(")
        product_at = body.index('if (names(overrides, "product")) {')
        stream_at = body.index('if (names(overrides, "stream")) {')
        block = body[product_at:stream_at]
        for reset in ('result.stream = null', 'result.baseline = null',
                      'result.environment = null'):
            self.assertIn(reset, block, "product override must reset " + reset)

    def test_stream_resets_baseline_only(self) -> None:
        """Stream contains baseline, not environment — changing the
        Build picker must never drop the dashboard's own environment
        filter (streams.js never touched it, and this pins that)."""
        body = _function_body(read("urls.js"), "function resolveScope(")
        stream_at = body.index('if (names(overrides, "stream")) {')
        baseline_at = body.index('if (names(overrides, "baseline")) {')
        block = body[stream_at:baseline_at]
        self.assertIn("result.baseline = null", block)
        self.assertNotIn("result.environment", block)

    def test_an_explicit_sibling_value_beats_the_automatic_reset(
        self
    ) -> None:
        """cardLink() (watch.js) and environmentSwitchUrl() (time.js/
        timeline.js) name product/stream together with a sibling level
        on purpose — resolveScope() must let the explicit value win,
        never silently reset what the SAME call just set."""
        body = _function_body(read("urls.js"), "function resolveScope(")
        self.assertIn('if (!names(overrides, "stream")) {', body)
        self.assertIn('if (!names(overrides, "environment")) {', body)

    def test_naming_product_without_stream_drops_stream_even_when_present(
        self
    ) -> None:
        """THE TRAP a coordinator fix round found live across four call
        sites (app.js's queue rows, app.js's browse rows, app.js's
        scriptLink(), compare.js's delta rows): each passed
        `{ product: null, baseline: null }` to pageUrl() INTENDING
        "suppress product and baseline, let stream carry by pageUrl()'s
        DEFAULT carriage" -- but the hierarchy rule keys off whether
        `product` is NAMED, not what its value is, so naming it (even as
        null) resets `stream` to null right alongside it, unless that
        SAME overrides object also names `stream`. All four sites
        silently dropped `stream=` on every stream-scoped page as a
        result (historical bug classes #1 and #5, reintroduced; found
        live 2026-08-08).

        There is no JS runtime in this suite (see the module docstring
        above) to literally call pageUrl() and inspect the query string,
        so this pins the same two composed steps a real call takes:
        resolveScope({product: null, baseline: null}) resets `stream` to
        null (test_product_resets_stream_baseline_and_environment above
        pins that reset in isolation, in a call that never names
        `stream`) -- and appendPlainScopeParam(), the function that
        turns a resolved scope into query-string bytes, treats a null
        value exactly like an absent one and writes nothing
        (test_stream_baseline_environment_never_write_empty above pins
        the "" branch of the same guard; null takes the same branch).
        Composed: a resolved stream of null can never surface as
        `?stream=...`, regardless of what the CURRENT page's own stream
        actually was -- the "let stream carry by default" the four
        broken sites relied on was never reachable once `product` was
        named without `stream` alongside it."""
        resolve_body = _function_body(read("urls.js"), "function resolveScope(")
        append_body = _function_body(
            read("urls.js"), "function appendPlainScopeParam(")
        product_at = resolve_body.index('if (names(overrides, "product")) {')
        stream_if_at = resolve_body.index(
            'if (names(overrides, "stream")) {', product_at)
        product_block = resolve_body[product_at:stream_if_at]
        # The reset is INSIDE the product block, guarded by "unless this
        # SAME overrides object also names stream" -- and it precedes
        # the separate `stream` block below, so a bare
        # {product: null, baseline: null} call never reaches a branch
        # that could put `stream` back.
        self.assertIn("result.stream = null", product_block)
        self.assertIn('if (!names(overrides, "stream")) {', product_block)
        # And once resolved to null, appendPlainScopeParam refuses to
        # write it -- the second half of the trap.
        self.assertIn(
            'value === null || value === undefined || value === ""',
            append_body)

    def test_product_empty_string_is_written_explicitly(self) -> None:
        body = _function_body(
            read("urls.js"), "function appendProductParam(")
        self.assertIn("value === null || value === undefined", body)
        self.assertNotIn('value === ""', body)

    def test_stream_baseline_environment_never_write_empty(self) -> None:
        body = _function_body(
            read("urls.js"), "function appendPlainScopeParam(")
        self.assertIn('value === ""', body)

    def test_page_url_appends_dot_html(self) -> None:
        body = _function_body(read("urls.js"), "export function pageUrl(")
        self.assertIn('page + ".html"', body)

    def test_with_baseline_mainline_is_the_only_explicit_one_encoding(
        self
    ) -> None:
        body = _function_body(read("urls.js"), "export function withBaseline(")
        self.assertIn('baselineIdOrMainline === "mainline"', body)
        self.assertIn('"1"', body)

    def test_with_product_resets_the_levels_it_contains(self) -> None:
        body = _function_body(read("urls.js"), "export function withProduct(")
        self.assertIn("currentUrlWithScope({ product:", body)

    def test_params_key_order_is_preserved_not_alphabetised(self) -> None:
        """A converted call site relies on this to reproduce its old
        query string byte for byte: Object.keys() order, not a sort."""
        body = _function_body(read("urls.js"), "function appendParams(")
        self.assertIn("Object.keys(params)", body)
        self.assertNotIn(".sort(", body)

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", read("urls.js"))


class UserListTest(unittest.TestCase):
    """One page, one request for the user list."""

    def test_only_one_place_fetches_the_picker_user_list(self) -> None:
        """The assertion that stops the stampede coming back.

        A second fetch site is not merely a duplicated request: it is a
        second cache, which by construction cannot dedupe against the
        first.

        WIDENED (not weakened) when user deactivation landed. Two kinds
        of call now name this endpoint and neither is a second picker
        cache:

        - mutations (POST to create, PUT .../active to deactivate),
        - the administrative roster, which asks for
          ``include_inactive=1`` and is a genuinely different result set
          — every user rather than the assignable ones — fetched once,
          lazily, when a fold-out section is opened.

        What is still banned is the thing that caused the outage: a
        second place fetching the *assignable* list, which is what every
        dropdown is built from. That list has exactly one source.
        """
        offenders = {}  # type: Dict[str, List[int]]
        for name, source in scripts().items():
            lines = []  # type: List[int]
            for number in fetch_sites(source, "api/users"):
                line = source.split("\n")[number - 1]
                if "postJson" in line or "putJson" in line:
                    continue          # a mutation, not a listing
                if "include_inactive" in line:
                    continue          # the admin roster, not the picker
                lines.append(number)
            if lines:
                offenders[name] = lines
        self.assertEqual(
            offenders, {"api.js": offenders.get("api.js", [])},
            "the assignable user list must be fetched in exactly one "
            "place (api.js loadUsers); found " + repr(offenders))
        self.assertEqual(
            len(offenders.get("api.js", [])), 1,
            "api.js must fetch the assignable /api/users exactly once")

    def test_the_admin_roster_is_fetched_at_most_once_and_lazily(
        self
    ) -> None:
        """The exemption above must stay narrow.

        One place, asking explicitly for the full roster. If a second
        appears, or if it stops being explicit, the exemption has
        started covering something it was not written for.
        """
        sites = []  # type: List[str]
        for name, source in scripts().items():
            for number in fetch_sites(source, "api/users"):
                line = source.split("\n")[number - 1]
                if "include_inactive" in line:
                    sites.append("%s:%d" % (name, number))
        self.assertLessEqual(
            len(sites), 1,
            "more than one place fetches the full user roster: "
            + repr(sites))
        if sites:
            body = _function_body(read("actions.js"), "async function loadPeople()")
            self.assertIn(
                "include_inactive", body,
                "the roster fetch must live in loadPeople(), which runs "
                "only when the Manage people section is opened")

    def test_the_promise_is_cached_not_the_result(self) -> None:
        """Caching the resolved value is the bug; it looks identical."""
        source = read("api.js")
        self.assertIn("usersPromise", source)
        self.assertNotIn(
            "knownUsers", source,
            "knownUsers was the array-caching version that stampeded")

    def test_the_cached_loader_is_not_async(self) -> None:
        """An `async` wrapper would re-introduce the await-before-assign
        window that caused this, so the shape is pinned deliberately."""
        source = read("api.js")
        self.assertIn("export function loadUsers()", source)
        self.assertNotIn("export async function loadUsers()", source)

    def test_a_newly_assigned_name_survives_into_later_dropdowns(
        self
    ) -> None:
        """rememberUser must not silently no-op against a promise.

        The failure it guards against: assign to somebody new, then find
        their name missing from the next dropdown on the page.
        """
        source = read("api.js")
        self.assertIn("export function rememberUser", source)
        self.assertIn("addedUsers", source)
        self.assertIn("concat(addedUsers)", source)

    def test_every_consumer_imports_the_shared_loader(self) -> None:
        for name in ("app.js", "test.js"):
            source = read(name)
            if "loadUsers" not in source:
                continue
            self.assertNotIn(
                "function loadUsers", source,
                name + " defines its own loadUsers; it must import the "
                "shared one from api.js")


class ReviewPanelTest(unittest.TestCase):
    """The review panel is defined once and imported everywhere.

    Same shape as the user-list rule above, and for the same reason. The
    panel fetches a run's output, posts comments, sets assignees and
    retires tests; a second copy is a second set of behaviours that will
    diverge, and the divergence shows up as "it works on the dashboard
    but not on open actions".
    """

    #: Functions that make up the panel. Any of them appearing outside
    #: review.js means a copy has been made.
    _PANEL_FUNCTIONS = (
        "function toggleReview",
        "function buildReviewPanel",
        "function buildReviewActions",
    )

    def test_the_panel_is_defined_in_exactly_one_file(self) -> None:
        offenders = {}  # type: Dict[str, List[str]]
        for name, source in scripts().items():
            if name == "review.js":
                continue
            found = [fn for fn in self._PANEL_FUNCTIONS if fn in source]
            if found:
                offenders[name] = found
        self.assertEqual(
            offenders, {},
            "the review panel must be defined only in review.js and "
            "imported elsewhere; found " + repr(offenders))

    def test_review_js_actually_defines_it(self) -> None:
        """Otherwise the rule above is satisfied by it existing nowhere."""
        source = read("review.js")
        for function in self._PANEL_FUNCTIONS:
            self.assertIn(function, source)
        self.assertIn("export function toggleReview", source)

    def test_the_panel_does_not_read_any_page_state(self) -> None:
        """It is shared, so it cannot know whose page it is on.

        The extraction exists because the panel was wired to the home
        screen's `state` object. Everything page-specific arrives in
        `options` — including the staleness cutoff, which is why the
        panel is TOLD a timestamp rather than asking whether a test is
        stale.
        """
        code = _strip_comments(read("review.js"))
        for forbidden in ("state.", "refreshSummary", "refreshQueueCounts"):
            self.assertNotIn(
                forbidden, code,
                "review.js must not reach into a page's state; pass it "
                "in through options instead (found " + forbidden + ")")

    def test_both_review_links_carry_the_entrys_stream_when_set(
        self
    ) -> None:
        """F2: "View in timeline" (and the same bug found right beside
        it, "Open full test page") must not deep-link a branch row's
        run into MAINLINE's timeline/test page, where it does not
        exist. entry.stream_id is stamped two ways: app.js's
        tagStream() on rows read from a branch's own dashboard tab, and
        compare.js's reviewEntry() on every delta-table row (the case
        the bug report actually named — see
        DeltaViewTest.test_review_entry_carries_the_streams_id_for_f2).
        It is undefined everywhere else (a mainline row, or any Open
        Actions row, where the similarly-named assignment_stream_id is
        a DIFFERENT concept), so this must be a truthy check, not a
        `!== null` one.

        WP-24: both links are now pageUrl() calls; the truthy
        entry.stream_id check became one `entry.stream_id || null`
        (streamScope), passed to BOTH calls as an EXPLICIT `stream`
        override -- this panel cannot know whose page it is on (see the
        module docstring), so it must never lean on pageUrl()'s default
        scope carriage the way an ordinary page's own row links do."""
        body = _strip_comments(
            _function_body(read("review.js"),
                           "async function buildReviewPanel("))
        self.assertIn("const streamScope = entry.stream_id || null;", body)
        self.assertEqual(body.count("stream: streamScope"), 2, body)
        self.assertIn('full.href = pageUrl("test"', body)
        self.assertIn('inTimeline.href = pageUrl("timeline"', body)
        # Never a `!== null` check: undefined (the common case, every
        # row that never touched a branch's own dashboard tab) must
        # also resolve streamScope to null, which pageUrl() omits.
        self.assertNotIn("entry.stream_id !== null", body)

    def test_the_comment_stripper_does_not_hide_real_code(self) -> None:
        """The scan above runs on stripped source, so prove the stripper
        removes prose and keeps code — otherwise it could hide the very
        thing it is looking for."""
        stripped = _strip_comments(
            "/* a row's expanded state. */\n"
            "const a = state.summary;  // trailing state.\n"
            "// state.thing\n"
        )
        self.assertIn("state.summary", stripped)
        self.assertNotIn("expanded state.", stripped)
        self.assertNotIn("state.thing", stripped)

    def test_consumers_import_it_rather_than_redefining(self) -> None:
        for name in ("app.js", "actions.js"):
            source = read(name)
            if "toggleReview" not in source:
                continue
            self.assertIn(
                'from "./review.js"', source,
                name + " uses the review panel but does not import it")


class ResultEmphasisTest(unittest.TestCase):
    """A superseded result must never outshout the current one.

    Reported from real use after launch: triage rows were misleading.
    The previous result was drawn as a full solid chip in its own
    column, while the CURRENT result appeared only as a 3px stripe on
    the row edge — so the loudest thing in the row was the wrong value,
    and wrong in the misleading direction in both queues that did it. A
    new failure's previous run is usually PASS, so a failure read as a
    pass; a fixed test's previous run is FAIL, so a fix read as a
    failure.

    This is a defect in the visual encoding, not a preference, which is
    why it is pinned rather than left to taste.
    """

    def test_a_superseded_result_uses_the_ghost_chip(self) -> None:
        source = read("api.js")
        self.assertIn("export function ghostChip", source)
        self.assertIn("export function resultTransition", source)
        body = _function_body(source, "export function ghostChip")
        self.assertIn("chip-ghost", body)

    def test_the_transition_puts_the_current_result_last(self) -> None:
        """Left to right in time order: was → now.

        The current result is the solid chip and it comes last, so it
        is both the loudest thing in the cell and the thing the eye
        finishes on.
        """
        body = _function_body(read("api.js"), "export function resultTransition")
        ghost = body.index("ghostChip(previous)")
        solid = body.index("resultChip(current)")
        self.assertLess(
            ghost, solid,
            "the previous result must be rendered before the current one")

    def test_the_ghost_chip_keeps_its_text_label(self) -> None:
        """Never colour alone — the outline is an addition, not a
        replacement for the words."""
        body = _function_body(read("api.js"), "export function ghostChip")
        self.assertIn("result", body)
        self.assertIn("el(", body)

    def test_neither_misleading_queue_still_uses_a_solid_prev_chip(
        self
    ) -> None:
        """The specific regression: resultChip(entry.prev_result)."""
        code = _strip_comments(read("app.js"))
        self.assertNotIn(
            "resultChip(entry.prev_result)", code,
            "a previous result must use ghostChip/resultTransition, not "
            "the solid chip that made failures look like passes")

    def test_the_ghost_chip_is_styled_as_an_outline(self) -> None:
        css = read_text("style.css")
        self.assertIn(".chip.chip-ghost", css)
        block = css[css.index(".chip.chip-ghost"):][:400]
        self.assertIn("background: transparent", block)

    def test_queues_with_one_invariant_result_state_it_once(self) -> None:
        """The other half of the fix.

        `still_failing` is FAIL on every row and `unexpected_passes` is
        UNEXPECTED_PASS on every row. A per-row chip there is a column
        of identical values — more of the noise this item is about.
        """
        code = _strip_comments(read("app.js"))
        self.assertIn("QUEUE_INVARIANT_RESULT", code)
        for queue in ("still_failing", "unexpected_passes"):
            self.assertIn(queue, code)

    #: Every list row builder of WP-44 design §3 U9: (file, signature).
    _ROW_BUILDERS = (("app.js", "function buildRow("),
                     ("app.js", "function renderQueueTable("),
                     ("script.js", "function renderTests("),
                     ("actions.js", "function buildRow("))

    def test_every_row_builder_shows_one_displayed_result(self) -> None:
        """WP-44 item 7 (design §3 U9, decision 8 as changed): a flaking
        row has its own chip and edge. Each row builder takes ONE value
        from rowResult() (displayResult() of the row's result, mark and
        mute) and gives it to resultClass(), the edge, and resultChip(),
        the chip, so the two cannot disagree; neither reads the raw
        result any more."""
        api = _strip_comments(read("api.js"))
        self.assertRegex(
            _function_body(api, "export function rowResult(entry)"),
            r"return displayResult\(entry\.result, flaky \? flaky\.state "
            r": null,\s*Boolean\(entry\.mute && entry\.mute\.live\)\);")
        for name, signature in self._ROW_BUILDERS:
            with self.subTest(builder=name + " " + signature):
                body = _strip_comments(_function_body(read(name), signature))
                self.assertEqual(body.count("const shown = rowResult("), 1)
                self.assertIn("const marker = resultClass(shown);", body)
                for raw in ("resultClass(row.result)",
                            "resultClass(entry.result)",
                            "resultChip(row.result)",
                            "resultChip(entry.result)"):
                    self.assertNotIn(raw, body)
                if signature == "function renderQueueTable(":
                    self.assertIn("column.cell(entry, shown)", body)
                else:
                    self.assertIn("resultChip(shown)", body)
        # The queue's cells draw the value they are handed.
        columns = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertEqual(columns.count("resultChip(shown)"), 2)
        self.assertNotIn("entry.result", columns)

    def test_the_flaky_pill_stands_beside_the_chip(self) -> None:
        """Wherever the "muted" pill goes the "flaky" one follows it; a
        queue that states its one result above the table puts the pill
        after the test's name instead."""
        for name, signature in (("app.js", "function buildRow("),
                                ("actions.js", "function buildRow(")):
            body = _strip_comments(_function_body(read(name), signature))
            self.assertIn("for (const tag of [mutedTag(row), flakyTag(row)])",
                          body, name)
        self.assertIn("flakyTag(row)", _strip_comments(_function_body(
            read("script.js"), "function renderTests(")))
        columns = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertIn("const tag = invariant ? flakyTag(entry) : null;",
                      columns)
        # The Result, the two was -> now and Not run's Last result cells.
        self.assertEqual(columns.count("withFlakyTag("), 4)

    def test_runs_stay_raw(self) -> None:
        """Decision 9: a run is what happened. Run history, the execution
        breakdown, the compare strip, the comparison page, the timeline
        and Watch's build cards never read the displayed result."""
        displayed = re.compile(r"\b(?:displayResult|rowResult)\s*\(")
        for name in ("compare.js", "timeline.js", "time.js"):
            self.assertIsNone(displayed.search(_strip_comments(read(name))),
                              name)
        test = _strip_comments(read("test.js"))
        self.assertIn("resultCell.appendChild(resultChip(run.result));", test)
        execution = _strip_comments(_function_body(
            read("script.js"), "function buildExecutionRow("))
        self.assertIn("resultChip(name)", execution)
        self.assertIsNone(displayed.search(execution))
        actions = _strip_comments(_function_body(
            read("actions.js"), "function buildRow("))
        self.assertIn("renderCompareStrip(resultCell, originStream, "
                      "row.result,", actions)

    def test_every_flaking_rule_takes_its_own_step(self) -> None:
        """Chips, row edges, the outlined chip and (item 9) a pressed
        toggle take the strong mauve; the chart parts take the chart's.
        A pressed FLAKING toggle, once it exists, must also be named in
        the pressed-toggle ink rule's :not() list, or that rule wins on
        specificity and paints it grey."""
        css = read_text("style.css")
        ink_prefix = '.toggle[aria-pressed="true"]:not('
        # Widened for the FLAKING toggle: the ink rule names .result-flaking only to
        # leave it out, so it is checked on its own below, not as a
        # flaking rule.
        flaking = [line for line in css.split("\n")
                   if "result-flaking" in line and "{" in line
                   and not line.startswith(ink_prefix)]
        self.assertGreaterEqual(len(flaking), 3)
        for rule in flaking:
            self.assertIn("var(--c-flaking-strong)", rule)
            self.assertNotIn("var(--c-flaking)", rule)
        if ".toggle.result-flaking" in css:
            ink = [line for line in css.split("\n")
                   if line.startswith(ink_prefix)]
            self.assertTrue(ink and ":not(.result-flaking)" in ink[0])
        for selector in (".bar-flaking {", ".tt-key.swatch-flaking {",
                         ".swatch-flaking, .legend-swatch.swatch-flaking {"):
            rule = css[css.index(selector):]
            rule = rule[:rule.index("}")]
            self.assertIn("var(--c-flaking)", rule, selector)


class SharedControlStylingTest(unittest.TestCase):
    """The Build picker looked "out of place" — browser-default, not
    styled at all — because WP-22 swapped it from ``<select>`` to
    ``<input type="text" list=…>`` (a combo box) and the shared base
    control rule near the top of style.css only ever named
    ``select, input[type="search"], textarea``. The regression shape:
    a control's TYPE changed and the stylesheet silently did not follow
    — pinned here so the same class of gap (a new/changed input type
    landing outside the shared rule) is caught by the suite, not by
    someone noticing the page "looks off" in a real browser.
    """

    def test_the_shared_base_rule_covers_text_inputs(self) -> None:
        css = read_text("style.css")
        self.assertIn('input[type="text"]', css)
        rule_at = css.index('input[type="text"]')
        # It must be in the SAME rule as select/input[type="search"],
        # not a copy-pasted duplicate elsewhere — the whole point is
        # ONE shared rule every control type stays under.
        line = css[css.rfind("\n", 0, rule_at) + 1:css.index("{", rule_at)]
        self.assertIn("select", line)
        self.assertIn('input[type="search"]', line)

    def test_the_build_and_compare_to_combos_are_wide_enough_to_read(
        self
    ) -> None:
        css = read_text("style.css")
        self.assertIn("#stream-picker-input", css)
        self.assertIn("#compare-to-input", css)

    def test_the_dead_select_rule_is_gone(self) -> None:
        """The picker has not been a <select> since WP-22; a rule
        still targeting one is either dead or, worse, a sign the two
        drifted apart again."""
        css = read_text("style.css")
        self.assertNotIn(".stream-picker-label select", css)


def read_text(name: str) -> str:
    """Read any file from the static directory as text.

    Normalises line endings for the same reason :func:`read` does — a
    CRLF checkout must not change what an assertion about file content
    sees.
    """
    with io.open(os.path.join(STATIC_DIR, name), "rb") as handle:
        return handle.read().decode("utf-8").replace("\r\n", "\n")


class SortingTest(unittest.TestCase):
    """A paged table must not be sorted in the browser.

    The two cases look identical in the UI and one of them lies. A table
    holding a COMPLETE result set can be reordered locally — nothing is
    hidden. A table holding ONE PAGE cannot: sorting the hundred rows in
    hand and labelling the column "sorted" turns "the oldest failure"
    into "the oldest among those that happen to be loaded", which is
    wrong and looks right.
    """

    def test_open_actions_sorts_on_the_server(self) -> None:
        """WP-24: `qs.append("sort"/"order", ...)` became `sort:`/
        `order:` keys in the params object passed to apiUrl() — same
        intent, both still travel on every list request."""
        code = _strip_comments(read("actions.js"))
        self.assertIn("sort: state.sortKey", code)
        self.assertIn("order: state.sortDescending", code)
        self.assertNotIn(
            "sortRows(", code,
            "actions.js pages its results, so it must not sort them in "
            "the browser — that reorders the page, not the queue")

    def test_re_sorting_a_paged_table_returns_to_the_first_page(
        self
    ) -> None:
        """Keeping the offset across a sort change shows an arbitrary
        slice of the newly-ordered list."""
        body = _function_body(read("actions.js"), "function init()")
        self.assertIn("attachSorting", body)
        self.assertIn("refresh(false)", body)

    def test_the_triage_queues_stop_sorting_when_truncated(self) -> None:
        """The queues are capped slices, so client-side sorting is only
        honest below the cap. Past it the control is disabled with a
        reason rather than quietly reordering part of the queue."""
        code = _strip_comments(read("app.js"))
        self.assertIn("const capped = queueCount(queueId)", code)
        self.assertIn("sorter.disable(", code)
        self.assertIn("sortRows(allEntries", code)

    def test_the_time_page_may_sort_locally(self) -> None:
        """It holds the whole level, so reordering shows everything."""
        code = _strip_comments(read("time.js"))
        self.assertIn("sortRows(state.items", code)

    def test_sorting_stays_on_the_wire_result(self) -> None:
        """WP-44 item 7: a row that reads FLAKING is FAIL on the wire, and
        every sort keeps the wire value, so a flaking row sorts with the
        failures. The result columns sort by ``result``; nothing that
        sorts reads the displayed result."""
        displayed = re.compile(r"\b(?:displayResult|rowResult)\s*\(")
        self.assertIsNone(displayed.search(_strip_comments(read("sorting.js"))))
        columns = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertEqual(columns.count('sortKey: "result"'), 3)
        self.assertNotIn("sortKey: \"shown\"", columns)
        self.assertIn('data-key="result">Result', read("index.html"))

    def test_sorting_is_implemented_once(self) -> None:
        offenders = [
            name for name, source in scripts().items()
            if name != "sorting.js"
            and "function attachSorting" in _strip_comments(source)
        ]
        self.assertEqual(
            offenders, [],
            "sorting belongs in sorting.js; found copies in "
            + repr(offenders))


class PerRowFetchTest(unittest.TestCase):
    """Nothing that runs per row may reach the network.

    A row builder is called once per row, so a fetch inside one is a
    request per row by construction. This is the shape of the bug rather
    than the specific instance of it.
    """

    #: Functions called once per rendered row.
    _PER_ROW = ("assigneeSelect", "resultChip", "commentNode")

    def test_no_row_builder_fetches_directly(self) -> None:
        source = read("api.js")
        body = _function_body(source, "export function assigneeSelect")
        self.assertIsNotNone(body, "assigneeSelect not found")
        for call in ("fetchJson(", "fetch("):
            self.assertNotIn(
                call, body,
                "assigneeSelect runs once per row; a direct " + call +
                " in it is one request per row")

    def test_the_row_builder_still_gets_its_users(self) -> None:
        """Not fetching is only correct if it uses the shared cache."""
        body = _function_body(read("api.js"), "export function assigneeSelect")
        self.assertIn("loadUsers()", body)


class SummaryRefreshTest(unittest.TestCase):
    """The heaviest endpoint is not asked once per user action.

    ``/api/summary`` computes the rollups, the trend, every queue and the
    top failing scripts. Every in-row action refreshed it so the counts
    stay honest, which is right — but triaging a queue means assigning a
    dozen tests in a few seconds, and each one fired its own full
    summary. Coalescing keeps the behaviour and drops the duplicate work.
    """

    def test_only_the_initial_load_and_the_coalescer_fetch_it(self) -> None:
        """Two sites: the parallel first paint, and fetchSummary().

        A third is somebody adding another ad-hoc refresh, which is how
        the per-action pile-up comes back.
        """
        source = read("app.js")
        sites = [
            number for number, line in enumerate(source.split("\n"), 1)
            if "fetchJson(summaryUrl())" in line
        ]
        self.assertEqual(
            len(sites), 2,
            "app.js should fetch the summary in exactly two places (the "
            "initial parallel load and fetchSummary); found lines "
            + repr(sites))

    def test_the_coalescer_exists_and_is_what_callers_use(self) -> None:
        source = read("app.js")
        self.assertIn("summaryInFlight", source)
        self.assertIn("async function fetchSummary()", source)
        for caller in ("refreshSummary", "refreshQueueCounts"):
            body = _function_body(source, "async function " + caller + "()")
            self.assertIn("await fetchSummary()", body, caller)

    def test_a_burst_still_ends_with_a_fresh_summary(self) -> None:
        """Coalescing must not mean the last action goes unreflected.

        The loop is what guarantees it: a request arriving mid-flight
        sets the stale flag, and the running caller issues one more
        before it finishes.
        """
        body = _function_body(read("app.js"), "async function fetchSummary()")
        self.assertIn("while (summaryStale)", body)
        self.assertIn("summaryStale = true", body)


def _strip_comments(source: str) -> str:
    """Remove ``//`` and ``/* */`` comments from JavaScript source.

    Naive: it does not understand strings or regex literals, so a
    ``"//"`` inside a string literal would be treated as a comment. That
    is acceptable here because the callers ask "does this identifier
    appear in the CODE", and the failure mode is a false pass on a
    contrived string — not a false failure. It exists because scanning
    raw source for ``state.`` matches English prose ("the row's expanded
    state.") as readily as a property access.
    """
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"//[^\n]*", " ", without_block)


def _function_body(source: str, signature: str) -> str:
    """Return the text of a function, brace-matched from its signature."""
    start = source.find(signature)
    if start == -1:
        return ""
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


class PlantedRegressionTest(unittest.TestCase):
    """Prove the detectors can fail, not merely that they pass today."""

    def test_a_second_fetch_site_would_be_caught(self) -> None:
        planted = 'const data = await fetchJson("/api/users");'
        self.assertEqual(len(fetch_sites(planted, "/api/users")), 1)

    def test_the_original_buggy_shape_would_be_caught(self) -> None:
        """The exact code that shipped, against the exact assertions."""
        buggy = (
            "let knownUsers = null;\n"
            "export async function loadUsers() {\n"
            "  if (knownUsers === null) {\n"
            '    const data = await fetchJson("/api/users");\n'
            "  }\n"
            "}\n"
        )
        self.assertIn("knownUsers", buggy)
        self.assertIn("export async function loadUsers()", buggy)

    def test_a_fetch_inside_a_row_builder_would_be_caught(self) -> None:
        planted = (
            "export function assigneeSelect(entry) {\n"
            '  const users = fetchJson("/api/users");\n'
            "  return users;\n"
            "}\n"
        )
        body = _function_body(planted, "export function assigneeSelect")
        self.assertIn("fetchJson(", body)


class WindowWordingTest(unittest.TestCase):
    """No page may name a window it is not the one being used.

    The recency line stopped being a fixed 36 hours when it started
    being derived from when the suite actually ran. The LABELS did not
    follow, so on a Tuesday morning after a Monday-morning run the home
    screen read:

        heading   "Last night"
        tile      "Ran last night — 100 of 100"
        not-run   "silent for 36h+"

    while the window it had actually counted was 78 hours wide, and
    nothing had run last night at all. Every one of those was a fixed
    string describing a value the server computes.

    The rule this pins: user-facing wording about the recency window is
    built from ``stale_before`` — the value the counting used — never
    from ``recent_hours``, which is only the wall-clock fallback.
    """

    #: Pages that show counts bounded by the recency cutoff, or that
    #: label a time window at all (the Timeline is nothing but one).
    #: watch.js joined this list under WP-20: every card labels its own
    #: freshness from its own `stale_before`/`last_reported`, and a card
    #: mixing a product with several environments has no single
    #: truthful window to describe from a constant.
    _PAGES = ("app.js", "time.js", "timeline.js", "watch.js")

    def test_no_page_labels_the_window_from_recent_hours(self) -> None:
        offenders = {}  # type: Dict[str, List[int]]
        for name in self._PAGES:
            code = _strip_comments(read(name))
            lines = [
                number
                for number, line in enumerate(code.splitlines(), 1)
                if "recent_hours" in line
            ]
            if lines:
                offenders[name] = lines
        self.assertEqual(
            offenders, {},
            "recent_hours is the wall-clock FALLBACK, not the window "
            "that was counted. Word it from stale_before instead; "
            "found " + repr(offenders))

    def test_the_home_screen_says_which_window_it_counted(self) -> None:
        """Removing the wrong number is only half of it — the right one
        has to be shown, or the tiles say nothing about their scope."""
        code = _strip_comments(read("app.js"))
        self.assertIn("function windowPhrase(", code)
        self.assertIn("summary.stale_before", code)

    def test_nothing_still_calls_the_window_a_night(self) -> None:
        """A suite can run at any hour, more than once a day, or not for
        a long weekend. "Last night" is only ever right by luck."""
        for name in ("app.js", "time.js", "actions.js", "timeline.js",
                     "watch.js"):
            self.assertNotIn(
                "last night", _strip_comments(read(name)).lower(), name)
        self.assertNotIn(
            "last night", read_text("index.html").lower(),
            "index.html still labels the tiles as a night")

    def test_the_time_page_is_told_its_own_cutoff(self) -> None:
        """It filters on the derived cutoff too, so its caption has the
        same obligation as the home screen's."""
        code = _strip_comments(read("time.js"))
        self.assertIn("data.stale_before", code)


class PlantedWindowRegressionTest(unittest.TestCase):
    """Prove the detector above can fail."""

    def test_a_fixed_hours_label_would_be_caught(self) -> None:
        planted = 'sub: "silent for " + summary.recent_hours + "h+",'
        self.assertIn("recent_hours", _strip_comments(planted))

    def test_a_night_label_would_be_caught(self) -> None:
        planted = 'label: "Pass rate last night",'
        self.assertIn("last night", _strip_comments(planted).lower())


#: The frontend's own shared modules. A name one of these exports is a
#: name every other file has to IMPORT before using — ES modules have no
#: implicit global scope, so a missing import is a ReferenceError at the
#: moment the line runs, not at load. products.js joined under WP-20:
#: watch.js (and, in a later drop, other pages) calls its exports.
_SHARED_MODULES = (
    "api.js", "charts.js", "sorting.js", "review.js", "products.js",
    "compare.js", "selection.js",
)


def _exported_names(source: str) -> List[str]:
    """Names a module exports as functions or consts."""
    return re.findall(
        r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)",
        _strip_comments(source), flags=re.M)


def _imported_names(source: str) -> List[str]:
    """Names a module imports, from every ``import { ... } from`` block."""
    names = []  # type: List[str]
    for block in re.findall(
            r"import\s*\{([^}]*)\}\s*from", _strip_comments(source), re.S):
        for piece in block.split(","):
            name = piece.strip().split(" as ")[-1].strip()
            if name:
                names.append(name)
    return names


def _defines(source: str, name: str) -> bool:
    """True when *source* declares *name* itself rather than importing it."""
    return re.search(
        r"^(?:export\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+"
        + re.escape(name) + r"\b",
        source, flags=re.M) is not None


class SharedImportTest(unittest.TestCase):
    """Every shared helper a page CALLS has to be in its import block.

    ``time.js`` shipped calling ``formatTime()`` without importing it.
    Nothing caught it: it parses, it loads, and the two call sites are
    both on branches that only run when some test has stopped reporting —
    which the generated dev database never has and production always
    does. So it worked here and threw "formatTime is not defined" there,
    on the one page that needed it.

    This is the narrow form of the check, deliberately. "Every free
    identifier is bound" needs a JavaScript parser, which this project
    does not have and is not going to grow. "Every name a shared module
    exports, if a page calls it, is imported by that page" needs a
    regex, and catches exactly the class of bug that shipped.
    """

    def test_every_shared_helper_used_is_imported(self) -> None:
        exports = {}  # type: Dict[str, str]
        for module in _SHARED_MODULES:
            for name in _exported_names(read(module)):
                exports[name] = module

        self.assertIn("formatTime", exports, sorted(exports))
        self.assertGreater(len(exports), 20, sorted(exports))

        missing = []  # type: List[str]
        for filename, source in sorted(scripts().items()):
            if filename in _SHARED_MODULES:
                continue        # a shared module may define its own
            code = _strip_comments(source)
            imported = set(_imported_names(source))
            for name, home in sorted(exports.items()):
                if not re.search(r"\b" + re.escape(name) + r"\s*\(", code):
                    continue    # not called here
                if name in imported or _defines(code, name):
                    continue
                missing.append(
                    "{0} calls {1}() from {2} without importing it".format(
                        filename, name, home))
        self.assertEqual(missing, [], "\n".join(missing))


class PlantedSharedImportRegressionTest(unittest.TestCase):
    """Prove the import check can fail — on the code that actually shipped."""

    def test_the_missing_formatTime_import_would_be_caught(self) -> None:
        planted = (
            'import { formatDuration } from "./api.js";\n'
            'excluded.textContent = "since " + formatTime(data.stale_before);\n'
        )
        code = _strip_comments(planted)
        self.assertNotIn("formatTime", _imported_names(planted))
        self.assertRegex(code, r"\bformatTime\s*\(")
        self.assertFalse(_defines(code, "formatTime"))

    def test_a_locally_defined_helper_is_not_flagged(self) -> None:
        planted = (
            "function formatTime(iso) { return iso; }\n"
            "const label = formatTime(x);\n"
        )
        self.assertTrue(_defines(_strip_comments(planted), "formatTime"))

    def test_a_renamed_import_counts_as_imported(self) -> None:
        planted = 'import { formatTime as fmt } from "./api.js";\n'
        self.assertIn("fmt", _imported_names(planted))


#: Month names as the release headings write them.
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


class DropDateTest(unittest.TestCase):
    """Every release section must carry a machine-readable date.

    Three things read ``data-drop-date`` off ``whatsnew.html``: the date
    shown on the "What's new" link, the unread marker beside it, and the
    place this site's own notes are attached. All three fail silently on a
    section that lacks it — the nav advertises an older drop than the one
    that just shipped, and a site note filed against today's date grows a
    duplicate section instead of joining the release notes.

    The attribute can also be RIGHT while being wrong: a copied section
    keeping the previous drop's date parses perfectly and misinforms
    everyone. So this checks it against the heading a human reads, which
    is the copy nobody forgets to update.
    """

    def sections(self) -> List[Tuple[str, str]]:
        """(data-drop-date, heading text) for each release section.

        Comments are stripped first, and not defensively: the note at the
        top of the file explains the convention by quoting the markup,
        so a scan of the raw text finds a ``<section class="release">``
        and a ``data-drop-date="YYYY-MM-DD"`` inside the prose and reports
        the documentation as a broken section.
        """
        html = re.sub(r"<!--.*?-->", " ", read("whatsnew.html"), flags=re.S)
        found = []
        for block in html.split('<section class="release"')[1:]:
            date = re.search(r'data-drop-date="([^"]*)"', block)
            heading = re.search(r'<h2 class="eyebrow">([^<]*)</h2>', block)
            found.append((
                date.group(1) if date else "",
                heading.group(1).strip() if heading else "",
            ))
        return found

    def test_the_scan_finds_the_release_sections(self) -> None:
        sections = self.sections()
        self.assertGreaterEqual(len(sections), 2, sections)

    def test_every_section_has_a_drop_date(self) -> None:
        missing = [head for date, head in self.sections() if not date]
        self.assertEqual(
            missing, [],
            "release section(s) with no data-drop-date; the nav date, the "
            "unread marker and site notes all key off it")

    def test_every_drop_date_is_iso(self) -> None:
        for date, head in self.sections():
            self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$", head)

    def test_the_drop_date_matches_the_heading(self) -> None:
        """A copied section that kept the old date parses fine and lies."""
        for date, heading in self.sections():
            year, month, day = date.split("-")
            expected = "{0} {1} {2}".format(
                int(day), _MONTHS[int(month) - 1], year)
            self.assertEqual(
                heading, expected,
                "section dated {0} is headed {1!r}; one of them is "
                "wrong".format(date, heading))

    def test_the_dates_descend(self) -> None:
        """Newest first: the file's only ordering rule."""
        dates = [date for date, _ in self.sections()]
        self.assertEqual(dates, sorted(dates, reverse=True), dates)

    def test_every_page_can_show_the_nav_marker(self) -> None:
        """A page that omits nav.js silently shows a stale-looking link."""
        for name in sorted(os.listdir(STATIC_DIR)):
            if not name.endswith(".html"):
                continue
            html = read(name)
            self.assertIn('id="nav-whatsnew"', html,
                          name + " has no identified What's new link")
            self.assertIn('src="nav.js"', html,
                          name + " does not load nav.js")


class PlantedDropDateRegressionTest(unittest.TestCase):
    """Prove the date checks can fail."""

    def test_a_copied_section_keeping_the_old_date_is_caught(self) -> None:
        date, heading = "2026-07-28", "30 July 2026"
        year, month, day = date.split("-")
        rendered = "{0} {1} {2}".format(int(day), _MONTHS[int(month) - 1], year)
        self.assertNotEqual(rendered, heading)

    def test_a_missing_attribute_is_caught(self) -> None:
        block = '<section class="release">\n<h2 class="eyebrow">1 Jan</h2>'
        self.assertIsNone(re.search(r'data-drop-date="([^"]*)"', block))


class SiteNotesFrontendTest(unittest.TestCase):
    """Site notes are operator text on a page that must not break.

    They are an addition to release notes that already shipped inside the
    build, so the page has to render completely without them. And they are
    free-form text written by a person, so they follow the same rule as
    comments and test output: textContent, never innerHTML.
    """

    def test_notes_reach_the_dom_as_text(self) -> None:
        code = _strip_comments(read("whatsnew.js"))
        self.assertNotIn("innerHTML", code)
        self.assertIn("textContent", code)

    def test_the_fetch_failure_path_is_silent(self) -> None:
        """No error banner: the real content is already on screen."""
        code = _strip_comments(read("whatsnew.js"))
        self.assertIn("catch", code)
        self.assertNotIn("showError", code)

    def test_it_asks_the_documented_endpoint(self) -> None:
        self.assertEqual(
            len(fetch_sites(read("whatsnew.js"), "api/site-notes")), 1)

    def test_the_nav_marker_never_blocks_a_page(self) -> None:
        """nav.js runs on every page, so its failure path matters most."""
        code = _strip_comments(read("nav.js"))
        self.assertNotIn("innerHTML", code)
        self.assertNotIn("showError", code)
        self.assertIn("catch", code)



class SummaryPartsFetchTest(unittest.TestCase):
    """The home page must never go back to the monolithic summary.

    The split exists so the tiles and charts paint without waiting for
    the slowest queue, and so a "Take" during triage refreshes one
    queue's rows rather than six. Both properties die silently — the
    page still WORKS if someone reverts to the full payload; it is just
    slow again — so they are pinned here the way the coalescer is.
    """

    def test_the_page_asks_for_the_headline_not_the_monolith(self) -> None:
        """WP-24: `qs.append("parts", "headline")` became `parts:
        "headline"` in the params object passed to apiUrl()."""
        body = _function_body(read("app.js"), "function summaryUrl()")
        self.assertIn(
            'parts: "headline"', body,
            "summaryUrl() no longer asks for parts=headline; the home "
            "page is back to downloading every queue's rows on every "
            "refresh")

    def test_queue_rows_are_fetched_in_exactly_two_places(self) -> None:
        """loadQueue (first paint / tab click) and the coalescer.

        A third site is the per-action pile-up coming back, one queue
        at a time.

        Widened for the Flaky tab (WP-44): the two sites share rowsRequest(),
        which asks for the Flaky tab's own request in place of a queue
        part when that tab is the one asked for, so the one fetch of a
        queue part lives there and rowsRequest() has exactly the two
        callers the queue part had.
        """
        source = _strip_comments(read("app.js"))
        sites = [
            number for number, line in enumerate(source.split("\n"), 1)
            if "fetchJson(queueUrl(" in line
        ]
        self.assertEqual(
            len(sites), 1,
            "app.js should fetch queue rows in one place, rowsRequest(); "
            "found lines " + repr(sites))
        self.assertIn("fetchJson(queueUrl(kind))",
                      _function_body(source, "function rowsRequest("))
        self.assertEqual(len(_call_arguments(source, "rowsRequest")), 2)
        for caller in ("async function loadQueue(",
                       "async function fetchSummary()"):
            self.assertIn("rowsRequest(kind)",
                          _function_body(source, caller), caller)

    def test_a_tab_click_does_not_refetch_a_cached_queue(self) -> None:
        """Clicking between tabs must cost zero requests once loaded."""
        source = _strip_comments(read("app.js"))
        self.assertIn("if (!state.queues[", source)

    def test_an_unloaded_queue_says_it_is_loading(self) -> None:
        """The feedback line, not a blank table, while rows are in flight."""
        body = _function_body(read("app.js"), "function renderQueueTable()")
        self.assertIn("allEntries === null", body)
        self.assertIn("Loading queue", body)


class ProductSwitcherTest(unittest.TestCase):
    """products.js (WP-20, docs/STREAMS_PLAN.md §2.3): the header switcher.

    There is no JavaScript runtime here (see this file's module
    docstring), so "at least two products shows it, fewer hides it" is
    asserted the same way every other property in this file is: against
    the source, not by executing it.
    """

    def test_the_switcher_hides_below_two_products(self) -> None:
        body = _function_body(read("products.js"), "export function renderSwitcher(")
        self.assertIn("products.length < 2", body)
        self.assertIn("container.hidden = true", body)

    def test_selection_persists_in_local_storage(self) -> None:
        code = _strip_comments(read("products.js"))
        self.assertIn("window.localStorage.getItem", code)
        self.assertIn("window.localStorage.setItem", code)

    def test_a_stale_stored_selection_is_clamped_back_to_all_products(
        self
    ) -> None:
        """A product this browser remembered but that has since been
        renamed away must not pin the page to a filter nobody chose."""
        body = _function_body(read("products.js"), "export function renderSwitcher(")
        self.assertIn("names.indexOf(selected) === -1", body)

    def test_it_fails_quietly_like_nav_js(self) -> None:
        """Decoration on someone else's page: a failed fetch must leave
        the page exactly as it shipped, never an error banner."""
        code = _strip_comments(read("products.js"))
        self.assertNotIn("showError", code)
        self.assertIn("catch", code)

    def test_it_is_mounted_only_where_it_can_do_something(self) -> None:
        """§2.3 says the header gains the switcher, not every page —
        whatsnew.html/script.html/test.html show no scoped data, so
        mounting it there would be a new heavyweight request for a
        control that changes nothing. watch.html WIDENED this test's
        exception list rather than weakening it (CLAUDE.md) -- ADDENDUM
        to the perf round: Watch is cross-product BY DEFINITION
        (docs/STREAMS_PLAN.md §0.9), so a switcher that scopes the page
        to ONE product is not "changes nothing", it is actively wrong;
        see WatchHasNoProductSwitcherTest for the removal itself and
        why products.js is still loaded there regardless."""
        for name in ("whatsnew.html", "script.html", "test.html"):
            self.assertNotIn(
                'src="products.js"', read_text(name),
                name + " should not load products.js")
        for name in ("index.html", "actions.html", "time.html",
                     "timeline.html"):
            self.assertIn(
                'src="products.js"', read_text(name),
                name + " is missing the product switcher")
            self.assertIn(
                'id="product-switcher"', read_text(name),
                name + " has no switcher mount point")

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", read("products.js"))

    def test_changing_the_switcher_rewrites_the_url_not_a_bare_reload(
        self
    ) -> None:
        """WP-23 bugfix, caught before it shipped: a page reached via a
        scope-self-sufficient ?product= link (a Watch card, any shared
        URL) would have adoptProductFromUrl() immediately overwrite a
        fresh switcher pick right back to the URL's product on the very
        next load, if the change handler only called
        window.location.reload() -- the same URL, product param and
        all. The switcher must rewrite `product` in the URL itself.

        WP-24: the hand-rolled `url.searchParams.set/delete` triple
        (product, then the stream/baseline/environment reset) moved
        into urls.js's withProduct(), which every scope-mutation
        helper's caller now goes through — same assertion intent
        (rewrites the URL itself, never a bare reload), now checking
        the withProduct() call site rather than the inline
        searchParams mechanics that no longer exist here."""
        body = _function_body(
            _strip_comments(read("products.js")),
            "export function renderSwitcher(")
        change_at = body.index('addEventListener("change"')
        handler = body[change_at:]
        self.assertNotIn("location.reload()", handler)
        self.assertIn("withProduct(select.value)", handler)
        self.assertIn("window.location.href = withProduct(select.value)",
                       handler)
        self.assertIn(
            "withProduct", _imported_names(read("products.js")),
            "products.js calls withProduct() without importing it")

    def test_the_stale_selection_clamp_runs_even_below_two_products(
        self
    ) -> None:
        """A bogus or renamed ?product= adopted from a stale/hand-typed
        URL must not stick forever on a single-product or no-products
        install, which has no switcher UI to ever clear it again --
        the clamp has to run BEFORE the `< 2` early return, not after
        it."""
        body = _function_body(
            read("products.js"), "export function renderSwitcher(")
        clamp_at = body.index("names.indexOf(selected) === -1")
        early_return_at = body.index("products.length < 2")
        self.assertLess(clamp_at, early_return_at)


class ProductUrlAdoptionTest(unittest.TestCase):
    """"The URL wins, and winning makes it stick" (WP-23 bugfix,
    docs/STREAMS_PLAN.md §0.9): a `?product=` param is adopted as both
    the rendered scope and the new stored selection — found live, a
    Watch card link for one product rendered scoped to whatever
    product this browser's switcher had last remembered instead."""

    def test_adoption_runs_at_module_evaluation_time(self) -> None:
        """Must be a plain top-level call, not inside init()'s async
        body — ES modules evaluate an import's top-level code before
        resuming the importing module's own, which is what guarantees
        this lands before any page's first getSelectedProduct() call.
        Buried inside async init() would race that guarantee away."""
        code = _strip_comments(read("products.js"))
        self.assertIn("\nadoptProductFromUrl();\n", code)

    def test_a_present_param_overwrites_the_stored_selection(self) -> None:
        body = _function_body(
            read("products.js"), "function adoptProductFromUrl(")
        self.assertIn('params.has("product")', body)
        self.assertIn("setSelectedProduct(params.get(\"product\") || \"\")",
                       body)

    def test_an_absent_param_leaves_storage_untouched(self) -> None:
        """No `product` key at all means today's behaviour — read
        whatever is already stored, never clear it."""
        body = _function_body(
            read("products.js"), "function adoptProductFromUrl(")
        self.assertIn("if (!params.has(\"product\")) {\n    return;", body)

    def test_an_empty_param_clears_the_selection_not_just_the_render(
        self
    ) -> None:
        """setSelectedProduct("") is the SAME clear path a manual
        switch to "All products" already uses (see setSelectedProduct
        above) — an empty ?product= is not merely "render unscoped
        this once", it is a real adoption."""
        body = _function_body(
            read("products.js"), "function adoptProductFromUrl(")
        self.assertIn("params.get(\"product\") || \"\"", body)


class ProductSwitcherHostManagedTest(unittest.TestCase):
    """index.html and actions.html already fetch /api/summary for their
    own headline data, so products.js's independent fetch of the same
    endpoint would be a second request per load on the two heaviest
    pages in the app. Those two pages mark their mount point
    `data-host-managed` and call `renderSwitcher` themselves once their
    own fetch lands; products.js's self-init skips its own fetch when it
    sees that attribute. time.html/timeline.html/watch.html fetch no
    summary of their own, so they keep the self-init path unmarked.
    """

    _SELF_INIT_PAGES = ("time.html", "timeline.html", "watch.html")
    _HOST_MANAGED = (("index.html", "app.js"), ("actions.html", "actions.js"))

    def test_init_skips_its_own_fetch_when_host_managed(self) -> None:
        body = _function_body(read("products.js"), "async function init()")
        self.assertIn("data-host-managed", body)
        self.assertIn("hasAttribute", body)

    def test_host_managed_pages_mark_the_mount_point(self) -> None:
        for html, _ in self._HOST_MANAGED:
            code = read_text(html)
            self.assertIn(
                "data-host-managed", code,
                html + " must mark its mount point host-managed")

    def test_self_init_pages_do_not_mark_the_mount_point(self) -> None:
        for html in self._SELF_INIT_PAGES:
            code = read_text(html)
            self.assertNotIn(
                "data-host-managed", code,
                html + " has no host fetch of its own — it must keep "
                "relying on products.js's self-init")

    def test_host_pages_import_and_call_renderSwitcher(self) -> None:
        for _, js in self._HOST_MANAGED:
            code = read(js)
            self.assertIn(
                "renderSwitcher", _imported_names(code),
                js + " calls renderSwitcher() without importing it")
            self.assertIn(
                "renderSwitcher(", _strip_comments(code),
                js + " never calls renderSwitcher()")

    def test_host_pages_still_load_products_js_for_getSelectedProduct(
        self
    ) -> None:
        """The self-init skip must not turn into removing the script
        tag — these pages still need getSelectedProduct() for their own
        filtering, and the module must still be on the page to import."""
        for html, _ in self._HOST_MANAGED:
            self.assertIn('src="products.js"', read_text(html), html)

    def test_host_pages_guard_the_mount_before_calling_renderSwitcher(
        self
    ) -> None:
        """renderSwitcher() opens with clearNode(container); calling it
        with a null container throws, and on these two pages that throw
        would happen mid-render — inside renderHeadline()/refresh(),
        after the tiles/charts/queues (app.js) or the owner filters
        (actions.js) render but before the function returns, taking the
        whole first paint down with it. products.js's own init() guards
        the same lookup; these call sites must too."""
        for _, js in self._HOST_MANAGED:
            code = _strip_comments(read(js))
            call_at = code.index("renderSwitcher(")
            before = code[:call_at]
            self.assertIn(
                "getElementById(\"product-switcher\")", before, js)
            # The most recent conditional before the call must guard it.
            guard_at = before.rindex("if (")
            guarded = code[guard_at:call_at]
            self.assertNotIn("}", guarded,
                js + " calls renderSwitcher() outside the nearest "
                "preceding if-guard")


class StreamPickerTest(unittest.TestCase):
    """streams.js (WP-21, docs/STREAMS_PLAN.md §3.6): the Build picker.

    Mirrors ProductSwitcherTest's shape for the analogous WP-20 control —
    same "assert against the source" method, same reasons (no JS runtime
    here, see this file's module docstring).
    """

    def test_the_picker_hides_with_no_streams(self) -> None:
        body = _function_body(read("streams.js"), "export function renderPicker(")
        self.assertIn("streams.length === 0", body)
        self.assertIn("container.hidden = true", body)

    def test_both_datalist_combos_clear_on_focus_and_restore_on_blur(
            self) -> None:
        """A datalist filters its suggestions by the input's CURRENT
        text, so a combo pre-filled with the current scope offers
        NOTHING until the user hand-deletes it — reported by the first
        person to switch off mainline (2026-08-09). Both combos (the
        Build picker and Compare-to) must empty themselves on focus and
        put the old value back on blur when nothing was chosen: the box
        doubles as the only statement of the current scope, so an
        aborted click must never leave it blank."""
        picker = _function_body(
            read("streams.js"), "export function renderPicker(")
        compare_to = _function_body(
            read("compare.js"), "function renderCompareToControl(")
        for name, body in (("streams.js renderPicker", picker),
                           ("compare.js renderCompareToControl",
                            compare_to)):
            for needle in ("dataset.restore", 'input.value = ""'):
                self.assertIn(
                    needle, body,
                    "{0}: missing the clear-on-focus/restore-on-blur "
                    "pattern ({1!r})".format(name, needle))

    def test_selection_lives_in_the_url_not_local_storage(self) -> None:
        """Unlike the product switcher, a branch is something you are
        looking AT, not a standing preference — docs/STREAMS_PLAN.md
        §0.9's "the URL is the whole configuration" rule.

        WP-24: streams.js no longer touches `searchParams` directly —
        selection reading/writing goes through urls.js's
        currentScope()/withStream(), which is itself pinned to
        location.search only (UrlsModuleTest). Same assertion intent
        (URL, never localStorage), now checking that streams.js
        actually imports the URL-reading/writing helpers rather than
        keeping its own copy."""
        code = _strip_comments(read("streams.js"))
        self.assertNotIn("localStorage", code)
        for name in ("currentScope", "withStream"):
            self.assertIn(
                name, _imported_names(read("streams.js")),
                "streams.js uses " + name + " without importing it")

    def test_it_fails_quietly_like_products_js(self) -> None:
        code = _strip_comments(read("streams.js"))
        self.assertNotIn("showError", code)
        self.assertIn("catch", code)

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", read("streams.js"))

    def test_it_is_mounted_on_every_stream_aware_page(self) -> None:
        """WIDENED (ADDENDUM to the perf round; was
        "mounted only on the dashboard", CLAUDE.md: widen a guard test's
        scope, never weaken its assertion, and say so — this is that).
        Originally the Build picker lived in index.html's toolbar only
        (docs/STREAMS_PLAN.md §3.6's original "Toolbar gains the Build
        picker"); it now also lives on time.html and timeline.html —
        the same pages nav.js's carryScopeIntoNav() targets, and the
        two pages that could already be ASKED for a stream on the wire
        (F7) but had no way to get there directly. A page carrying no
        #stream-picker mount is simply never touched by streams.js's
        init(); actions.html/watch.html/test.html/script.html/
        whatsnew.html are still correctly excluded — see
        NavScopeCarriageTest's STREAM_AWARE_HREFS pin for the same
        three-page list, kept in agreement by this test."""
        for name in ("index.html", "time.html", "timeline.html"):
            self.assertIn('src="streams.js"', read_text(name), name)
            self.assertIn('id="stream-picker"', read_text(name), name)
        for name in ("actions.html", "watch.html", "test.html",
                     "script.html", "whatsnew.html"):
            self.assertNotIn(
                'src="streams.js"', read_text(name),
                name + " should not load streams.js")


class DeltaViewTest(unittest.TestCase):
    """compare.js (WP-21, docs/STREAMS_PLAN.md §3.6): the dashboard's
    branch-vs-mainline delta view, and the mainline-zero-visible-change
    rule it exists to preserve."""

    def test_the_selected_stream_comes_only_from_the_url(self) -> None:
        body = _function_body(
            read("compare.js"), "export function getSelectedStreamId(")
        self.assertIn("window.location.search", body)
        self.assertNotIn("localStorage", body)

    def test_a_mainline_page_load_never_reaches_the_delta_view(self) -> None:
        """The whole feature's footprint on a mainline page has to be
        ONE guarded call, not a code path that merely renders nothing —
        a fetch that still fires and is then hidden is not zero visible
        change, it is zero visible change AND a wasted request.

        WIDENED for WP-23 (docs/STREAMS_PLAN.md §5.2), not weakened:
        init() no longer calls initDeltaView() directly — it calls
        initBranchDashboard(), which then picks between the branch
        dashboard's two tabs ("its own results" / "difference from
        mainline"). The invariant this test protects is unchanged (a
        mainline load must never fire the delta view's fetch); it is
        now checked through the new two-hop call chain instead of the
        old direct one, with an explicit assertion that initDeltaView
        does not leak into init()'s own body and that the guarded
        branch never falls into the mainline continuation.
        """
        code = read("app.js")
        body = _function_body(code, "function init()")
        self.assertIn("getSelectedStreamId()", body)
        call_at = body.index("initBranchDashboard(")
        # The nearest preceding "if" must guard the call, and that
        # branch must return before falling into the mainline code below
        # it (wireMainlineControls/refreshAll and everything after them).
        guard_at = body.rindex("if (", 0, call_at)
        branch = body[guard_at:body.index("}", call_at)]
        self.assertIn("return", branch)
        self.assertNotIn("wireMainlineControls(", branch)
        self.assertNotIn("refreshAll(", branch)
        # initDeltaView itself must never appear in init()'s own body —
        # only reachable through initBranchDashboard()/activateDiffTab(),
        # both entirely inside the guarded branch checked above.
        self.assertNotIn("initDeltaView(", body)
        diff_body = _function_body(code, "function activateDiffTab(")
        self.assertIn("initDeltaView(", diff_body)

    def test_the_new_sections_ship_hidden(self) -> None:
        """branch-band and delta-section must be hidden in the SHIPPED
        markup, not merely hidden by JS after load — a page that never
        runs any script (or runs a stale cached bundle) must still show
        the ordinary mainline layout, not a flash of delta-view markup."""
        html = read_text("index.html")
        self.assertIn('id="branch-band" class="branch-band" hidden', html)
        delta_at = html.index('id="delta-section"')
        self.assertIn("hidden", html[delta_at:delta_at + 60])

    def test_initDeltaView_hides_every_mainline_section(self) -> None:
        code = _strip_comments(read("compare.js"))
        self.assertIn("MAINLINE_SECTIONS", code)
        list_at = code.index("MAINLINE_SECTIONS = [")
        list_block = code[list_at:code.index("];", list_at)]
        for section_id in ("status-section", "charts-section",
                            "triage-section", "browse-section"):
            self.assertIn(section_id, list_block)
        body = _function_body(
            read("compare.js"), "export async function initDeltaView(")
        self.assertIn("MAINLINE_SECTIONS", body)

    def test_agree_and_coverage_are_derived_never_hardcoded(self) -> None:
        """The "N tests agree" and coverage lines both read counts.agree
        and totalCompared() (itself a sum over live data) — never a
        number that is not one of the six counts the API returned."""
        code = _strip_comments(read("compare.js"))
        self.assertIn("counts.agree", code)
        self.assertIn("totalCompared(counts)", code)

    def test_no_result_is_never_a_result_chip(self) -> None:
        """The same defect ResultEmphasisTest pins for a transition,
        extended to a comparison: an absent side must never render as
        a coloured pass/fail/etc. chip."""
        body = _function_body(read("compare.js"), "function noResultCell()")
        self.assertNotIn("resultChip(", body)
        self.assertNotIn("ghostChip(", body)
        mainline_body = _function_body(
            read("compare.js"), "export function mainlineCell(")
        self.assertIn("noResultCell()", mainline_body)
        stream_body = _function_body(
            read("compare.js"), "export function streamCell(")
        self.assertIn("noResultCell()", stream_body)

    def test_mainline_is_ghost_and_this_branch_is_solid(self) -> None:
        code = _strip_comments(read("compare.js"))
        mainline_body = _function_body(
            code, "export function mainlineCell(")
        self.assertIn("ghostChip(result)", mainline_body)
        stream_body = _function_body(code, "export function streamCell(")
        self.assertIn("resultChip(result)", stream_body)

    def test_delta_rows_offer_the_shared_assignee_select(self) -> None:
        """Triage still works from a branch (docs/STREAMS_PLAN.md
        §0.4/§3.6): the SAME control the dashboard's queue rows use, not
        a second one."""
        code = _strip_comments(read("compare.js"))
        self.assertIn("assigneeSelect", _imported_names(read("compare.js")))
        body = _function_body(code, "function buildDeltaRow(")
        self.assertIn("assigneeSelect(entry", body)

    def test_delta_rows_never_offer_retirement(self) -> None:
        """Retirement is a MAINLINE decision (§3.4) — review.js's own
        retire gate is `isStale(entry, opts.staleBefore) &&
        !entry.retired_at`, and isStale() short-circuits false when
        staleBefore is falsy. deltaReviewOptions() must never set it —
        the shared panel cannot be told "no retire" any other way,
        since it explicitly cannot know whose page it is on (see
        ReviewPanelTest)."""
        body = _function_body(
            read("compare.js"), "function deltaReviewOptions(")
        self.assertNotIn("staleBefore", body)

    def test_the_review_button_is_absent_without_a_run_to_review(
        self
    ) -> None:
        """A no_result row has no stream-side run — no button that
        opens onto nothing, per CompareRow's own invariant."""
        body = _function_body(read("compare.js"), "function buildDeltaRow(")
        review_at = body.index("review-btn")
        guard_at = body.rindex("if (", 0, review_at)
        guarded = body[guard_at:body.index("}", review_at)]
        self.assertIn("row.stream_run_id !== null", guarded)

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", read("compare.js"))

    def test_delta_rows_carry_the_stream_into_the_test_link(self) -> None:
        """A delta row's link must include ?stream=, or the branch-scoped
        test page (history, analytics, output — all built for exactly
        this drill-down) is unreachable by clicking: the reader lands on
        the MAINLINE view of a test they opened FROM a branch, which
        reads as "the branch has no detail pages". Found by the first
        human to use the branch dashboard, 2026-08-08 — the DOM-shim
        checks rendered rows, not where their links lead.

        WP-24, corrected by the coordinator fix round: the original
        conversion computed `streamId` but never passed it to pageUrl(),
        relying instead on pageUrl()'s DEFAULT scope carriage to supply
        `stream` -- but naming `product` in the overrides object resets
        every level it contains (stream included) unless that same
        object also names them, so the default never fired and this link
        silently dropped `stream=` (historical bug classes #1 and #5,
        reintroduced; found live, 2026-08-08 — the DOM-shim checks
        rendered rows, not where their links lead). The fix names
        `stream: streamId` explicitly in the overrides object, so this
        pins that literal object whole rather than merely checking that
        getSelectedStreamId() is called somewhere above the pageUrl()
        call -- a call reading streamId without ever handing it to
        pageUrl() must fail here again."""
        body = _function_body(read("compare.js"), "function buildDeltaRow(")
        self.assertIn("getSelectedStreamId()", body)
        stream_at = body.index("getSelectedStreamId()")
        rest = body[stream_at:]
        self.assertIn('link.href = pageUrl("test"', rest)
        self.assertIn(
            "{ product: null, baseline: null, stream: streamId }", rest)
        self.assertIn(
            "pageUrl", _imported_names(read("compare.js")),
            "compare.js calls pageUrl() without importing it")

    def test_review_entry_carries_the_streams_id_for_f2(self) -> None:
        """F2 (docs/STREAMS_PLAN.md §5.2 "as built"): review.js's shared
        panel appends stream= to its links whenever entry.stream_id is
        truthy. That check is generic -- it also has to be satisfied by
        a DELTA row's entry (the case the bug report actually named:
        "View in timeline" from a branch delta row), not only by a row
        read from a branch's own dashboard tab. reviewEntry() is what
        supplies it here; buildDeltaRow must pass its OWN streamId
        through, not the entry's/row's environment (a delta row has no
        stream_id field of its own -- see CompareRow's fields in
        api.py's /api/compare docstring)."""
        entry_body = _function_body(read("compare.js"), "function reviewEntry(")
        self.assertIn("stream_id: streamId", entry_body)
        row_body = _function_body(read("compare.js"), "function buildDeltaRow(")
        self.assertIn("reviewEntry(row, streamId)", row_body)


class DeltaEnvironmentFilterTest(unittest.TestCase):
    """WP-33: the Environment filter on a build's "Difference from" tab.

    The filter is the toolbar's existing select, shared with "Its own
    results" -- which is what makes it one control in one place, and
    also what makes it dangerous: app.js wired that select (and the
    Refresh button beside it) for the dashboard, those listeners
    outlive a tab switch, and on the difference tab state.streamId is
    null. Left unguarded, changing the filter there loads MAINLINE's
    dashboard and un-hides it under the comparison. Most of this class
    is about that hand-over, not about the filter.
    """

    def test_the_filter_is_no_longer_hidden_on_the_difference_tab(
            self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "export async function initDeltaView("))
        self.assertNotIn("env-filter-field", body)
        self.assertIn("renderEnvironmentFilter(data, streamId)", body)

    def test_options_and_selection_come_from_the_response(self) -> None:
        """The list the server compared across and the filter it
        applied -- never a list or a value the page worked out."""
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        self.assertIn("data.environments", body)
        self.assertIn("data.environment ", body)
        self.assertIn("select.value = applied", body)
        self.assertNotIn("location.search", body)
        self.assertNotIn("currentScope(", body)

    def test_a_server_without_the_list_keeps_the_field_hidden(
            self) -> None:
        """Static files are read from disk per request; Python is not.
        A process that was not restarted serves this script against a
        handler that ignores `environment=` -- the control would then
        change the address bar and nothing else."""
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        guard_at = body.index("Array.isArray(data.environments)")
        hide_at = body.index("field.hidden = true", guard_at)
        show_at = body.index("field.hidden = false")
        self.assertLess(guard_at, hide_at)
        self.assertLess(hide_at, show_at)
        self.assertIn("return", body[hide_at:show_at])

    def test_a_filter_outside_the_comparison_is_never_shown_as_all(
            self) -> None:
        """select.value falls back to the first option when nothing
        matches -- "All environments", over a filter that is in force
        and matching nothing. The stray value gets its own option."""
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        stray_at = body.index("applied && !covered")
        assign_at = body.index("select.value = applied")
        self.assertLess(stray_at, assign_at)
        self.assertIn("stray.value = applied", body[stray_at:assign_at])

    def test_changing_it_rewrites_the_address_bar_through_urls_js(
            self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        self.assertIn(
            "replaceState(null, \"\", withEnvironment(select.value))",
            body)
        self.assertIsNone(_ENVIRONMENT_PARAM_RE.search(
            _strip_comments(read("compare.js"))))

    def test_changing_it_keeps_the_category_tab(self) -> None:
        change = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        self.assertIn(
            "initDeltaView(streamId, { keepCategory: true })", change)
        init = _strip_comments(_function_body(
            read("compare.js"), "export async function initDeltaView("))
        reset_at = init.index("deltaState.category = CATEGORY_ORDER[0]")
        guard_at = init.rindex("if (", 0, reset_at)
        self.assertIn("keepCategory", init[guard_at:reset_at])

    def test_with_environment_changes_that_level_and_no_other(
            self) -> None:
        body = _strip_comments(_function_body(
            read("urls.js"), "export function withEnvironment("))
        self.assertIn("currentUrlWithScope(", body)
        self.assertIn("environment:", body)
        for level in ("product", "stream", "baseline"):
            self.assertNotIn(level + ":", body)

    def test_the_change_handler_stands_down_off_the_difference_tab(
            self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        handler = body[body.index("select.onchange"):]
        guard_at = handler.index("!deltaState.active")
        act_at = handler.index("replaceState(")
        self.assertLess(guard_at, act_at)
        self.assertIn("return", handler[guard_at:act_at])

    def _guarded(self, listener: str, action: str) -> None:
        self.assertIn("isDeltaViewActive()", listener)
        guard_at = listener.index("isDeltaViewActive()")
        act_at = listener.index(action)
        self.assertLess(guard_at, act_at)
        self.assertIn("return", listener[guard_at:act_at])

    def test_the_dashboards_own_listeners_stand_down_on_that_tab(
            self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "function wireMainlineControls("))
        env_at = body.index('envSelect.addEventListener("change"')
        self._guarded(
            body[env_at:body.index("scriptSelect.addEventListener")],
            "setEnvironment(")
        reload_at = body.index('getElementById("reload-btn")')
        self._guarded(
            body[reload_at:body.index('getElementById("show-more")')],
            "refreshAll(")

    def test_own_results_takes_the_controls_back(self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "function activateOwnResultsTab("))
        self.assertIn("leaveDeltaView()", body)
        self.assertLess(
            body.index("leaveDeltaView()"), body.index("refreshAll("))
        leave = _strip_comments(_function_body(
            read("compare.js"), "export function leaveDeltaView("))
        self.assertIn("deltaState.active = false", leave)
        self.assertIn("reload.onclick = null", leave)

    def test_own_results_rereads_the_filter_on_every_activation(
            self) -> None:
        """wireMainlineControls() reads the address bar once, the first
        time it runs. The difference tab can have changed it since."""
        body = _strip_comments(_function_body(
            read("app.js"), "function activateOwnResultsTab("))
        wired_at = body.index("wireMainlineControls()")
        read_at = body.index(
            "state.environment = currentScope().environment")
        load_at = body.index("refreshAll(")
        self.assertLess(wired_at, read_at)
        self.assertLess(read_at, load_at)

    def test_switching_to_the_difference_abandons_a_load_in_flight(
            self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "function activateDiffTab("))
        self.assertLess(
            body.index("state.requestSeq++"),
            body.index("initDeltaView("))
        self.assertIn("state.browseSeq++", body)

    def test_the_filter_in_force_is_stated_from_the_servers_echo(
            self) -> None:
        html = read("index.html")
        note_at = html.index('id="delta-environment-note"')
        self.assertIn("hidden", html[note_at:note_at + 60])
        self.assertLess(note_at, html.index('id="delta-tiles"'))
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderEnvironmentFilter("))
        self.assertIn("note.hidden = true", body)
        shown_at = body.index("note.hidden = false")
        guard_at = body.rindex("if (", 0, shown_at)
        self.assertIn("applied", body[guard_at:shown_at])
        self.assertIn("applied +", body[guard_at:shown_at])


class BuildDeleteControlTest(unittest.TestCase):
    """WP-34: deleting what a build holds for one environment, from the
    page. The dashboard has no login, so the control is the safeguard a
    login would otherwise be -- and each part of it is something a
    later tidy-up could remove without anything looking broken."""

    def _body(self, signature: str) -> str:
        return _strip_comments(
            _function_body(read("buildadmin.js"), signature))

    def test_the_section_ships_hidden_and_collapsed(self) -> None:
        html = read("index.html")
        at = html.index('id="build-admin"')
        tag = html[html.rindex("<", 0, at):html.index(">", at)]
        self.assertIn("<details", tag)
        self.assertIn("hidden", tag)
        self.assertNotIn(" open", tag)
        self.assertIn('src="buildadmin.js"', html)

    def test_the_delete_button_ships_disabled(self) -> None:
        html = read("index.html")
        at = html.index('id="build-admin-delete"')
        self.assertIn("disabled", html[at:html.index(">", at)])

    def test_a_mainline_page_never_shows_it_or_fetches_for_it(
            self) -> None:
        body = self._body("function init()")
        guard_at = body.index("streamId === null")
        shown_at = body.index("section.hidden = false")
        self.assertLess(guard_at, shown_at)
        self.assertIn("return", body[guard_at:shown_at])
        self.assertNotIn("fetchJson(", body)
        self.assertNotIn("postJson(", body)

    def test_nothing_is_fetched_until_it_is_opened(self) -> None:
        body = self._body("function init()")
        toggle_at = body.index('addEventListener("toggle"')
        handler = body[toggle_at:body.index("});", toggle_at)]
        self.assertIn("section.open", handler)
        self.assertIn("load()", handler)
        self.assertEqual(body.count("load()"), 1)
        code = _strip_comments(read("buildadmin.js"))
        self.assertEqual(code.count("fetchJson("), 1)
        self.assertIn(
            "fetchJson(", self._body("async function load()"))

    def test_the_button_needs_a_target_a_reason_and_the_exact_name(
            self) -> None:
        body = self._body("function readyToDelete()")
        self.assertIn("state.target !== null", body)
        self.assertIn('"build-admin-reason").value.trim() !== ""', body)
        self.assertIn(
            '"build-admin-confirm").value === state.stream.name', body)
        self.assertNotIn("toLowerCase", body)
        self.assertNotIn("||", body)
        sync = self._body("function syncDeleteButton()")
        self.assertIn("disabled = !readyToDelete()", sync)

    def test_the_button_is_rechecked_as_either_field_changes(
            self) -> None:
        body = self._body("function init()")
        for field in ("build-admin-reason", "build-admin-confirm"):
            self.assertIn(
                '"' + field + '").addEventListener("input", '
                'syncDeleteButton)', body)

    def test_a_click_is_rechecked_before_anything_is_sent(self) -> None:
        body = self._body("async function submit()")
        guard_at = body.index("!readyToDelete()")
        post_at = body.index("postJson(")
        self.assertLess(guard_at, post_at)
        self.assertIn("return", body[guard_at:post_at])

    def test_a_name_is_required_and_sent(self) -> None:
        body = self._body("async function submit()")
        name_at = body.index("requireUsername()")
        post_at = body.index("postJson(")
        self.assertLess(name_at, post_at)
        self.assertIn("return", body[name_at:post_at])
        sent = body[post_at:body.index("});", post_at)]
        for field in ("username:", "reason:", "confirm:"):
            self.assertIn(field, sent)

    def test_the_request_names_one_environment_and_carries_no_scope(
            self) -> None:
        body = self._body("function deleteUrl(")
        self.assertIn("encodeURIComponent(environment)", body)
        self.assertIn('"/delete"', body)
        self.assertIn("NO_SCOPE", body)
        self.assertIn("NO_SCOPE", self._body("function environmentsUrl()"))
        code = _strip_comments(read("buildadmin.js"))
        block = code[code.index("const NO_SCOPE"):]
        block = block[:block.index("};")]
        for level in ("product", "stream", "baseline", "environment"):
            self.assertIn(level + ": null", block)

    def test_what_will_go_is_stated_from_the_servers_counts(self) -> None:
        body = self._body("function openForm(")
        for source in ("row.tests", "row.runs", "row.environment",
                       "state.stream.name"):
            self.assertIn(source, body)
        self.assertIn("cannot be undone", body)

    def test_it_says_a_feeder_will_put_the_results_back(self) -> None:
        """The delete does not block a re-import. A person who does not
        know that deletes, sees the build return ten minutes later, and
        reports the delete as broken."""
        body = self._body("function openForm(")
        self.assertIn("feeder", body)
        self.assertIn("come back", body)

    def test_the_last_environment_is_called_out(self) -> None:
        body = self._body("function openForm(")
        self.assertIn("state.environments.length === 1", body)
        self.assertIn("build itself will disappear", body)

    def test_after_a_delete_the_page_is_not_left_stale(self) -> None:
        body = self._body("async function submit()")
        gone_at = body.index("result.stream_deleted")
        self.assertIn("withStream(null)", body[gone_at:])
        self.assertIn('byId("reload-btn")', body[gone_at:])
        self.assertIn("await load()", body[gone_at:])

    def test_every_word_reaches_the_page_as_text(self) -> None:
        code = _strip_comments(read("buildadmin.js"))
        self.assertNotIn("innerHTML", code)
        self.assertNotIn("insertAdjacentHTML", code)


class BuildCommentTest(unittest.TestCase):
    """WP-35: a comment made from a build's page is recorded against
    that build however it is made, and the build's difference list
    shows it.

    Three ways to comment from a build's row existed or now exist --
    the Review panel, the note on a bulk assignment, the bulk "Comment
    only" -- and before this work package only a fourth, the test
    page's own box, said where the comment came from. Each is pinned
    separately because each was, or would be, silently untagged.
    """

    def test_the_review_panel_says_where_a_comment_was_posted_from(
            self) -> None:
        code = _strip_comments(read("review.js"))
        post_at = code.index('"/comments"')
        block = code[code.rindex("const body", 0, post_at):post_at]
        self.assertIn("entry.stream_id", block)
        self.assertIn("body.stream_id = entry.stream_id", block)
        sent = code[post_at:code.index(";", post_at)]
        self.assertIn("body", sent)
        self.assertNotIn("{ username: me, text: text }", sent)

    def test_a_mainline_rows_comment_still_carries_no_origin(
            self) -> None:
        """Guarded, not unconditional: an absent stream_id must stay
        absent rather than become `stream_id: undefined` or null."""
        code = _strip_comments(read("review.js"))
        assign_at = code.index("body.stream_id = entry.stream_id")
        guard_at = code.rindex("if (", 0, assign_at)
        self.assertIn("entry.stream_id", code[guard_at:assign_at])

    def test_the_panel_reports_the_servers_own_timestamp(self) -> None:
        code = _strip_comments(read("review.js"))
        at = code.index('kind: "commented"')
        self.assertIn(
            "posted.comment.created_at", code[at:at + 300])

    def test_the_difference_list_has_a_comment_column(self) -> None:
        html = read("index.html")
        head = html[html.index('id="delta-table"'):]
        head = head[:head.index("</thead>")]
        self.assertLess(
            head.index('id="delta-col-stream"'),
            head.index('id="delta-col-comment"'))
        row = _strip_comments(_function_body(
            read("compare.js"), "function buildDeltaRow("))
        self.assertIn(
            "fillDeltaCommentCell(commentTd, row.stream_comment)", row)

    def test_the_column_shows_the_builds_comment_and_no_other(
            self) -> None:
        code = _strip_comments(read("compare.js"))
        self.assertNotIn("latest_comment", code)
        fill = _function_body(code, "function fillDeltaCommentCell(")
        self.assertNotIn("no comments", fill)
        self.assertIn("comment.text", fill)
        self.assertIn("comment.author", fill)

    def test_the_heading_names_what_is_being_compared(self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderBaselineCard("))
        self.assertIn('"Comment on this " + streamNoun', body)

    def test_a_posted_comment_updates_its_row_in_place(self) -> None:
        """Reloading the page of rows would close every open panel on
        a list that is being worked down."""
        row = _strip_comments(_function_body(
            read("compare.js"), "function buildDeltaRow("))
        handler = row[row.index("const onCommented"):]
        handler = handler[:handler.index("renderCommentedLine();") + 22]
        self.assertIn("row.stream_comment =", handler)
        self.assertIn("fillDeltaCommentCell(", handler)
        self.assertIn("renderCommentedLine()", handler)
        self.assertNotIn("loadCategory(", handler)
        self.assertEqual(
            row.count("deltaReviewOptions(onCommented)"), 2)

    def test_an_open_panel_is_reopened_only_once_the_row_is_attached(
            self) -> None:
        """Reopening inserts the panel as the row's next sibling, which
        needs a parent. buildDeltaRow() returns a row that has none, and
        called reopenIfOpen() itself -- so Refresh, or any bulk action,
        with a panel open threw and left the list cut short. A bulk
        comment makes that re-render routine."""
        code = _strip_comments(read("compare.js"))
        build = _function_body(code, "function buildDeltaRow(")
        self.assertEqual(build.count("reopenIfOpen("), 1)
        self.assertIn("reopenAfterAttach.set(tr, () => reopenIfOpen(",
                      build)
        load = _function_body(code, "async function loadCategory(")
        attach_at = load.index("body.appendChild(tr)")
        reopen_at = load.index("reopenAfterAttach.get(tr)")
        self.assertLess(attach_at, reopen_at)

    def test_delta_review_options_still_offer_no_retirement(self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "function deltaReviewOptions("))
        self.assertNotIn("staleBefore", body)
        self.assertNotIn("onRetired", body)
        self.assertIn('change.kind === "commented"', body)

    def test_the_count_is_worded_from_the_rows_on_the_page(self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "function renderCommentedLine("))
        self.assertIn("deltaState.rows", body)
        self.assertIn("row.stream_comment", body)
        self.assertIn("shown >= deltaState.total", body)
        self.assertIn("shown so far", body)
        self.assertIn("deltaState.total", body)
        html = read("index.html")
        at = html.index('id="delta-commented"')
        self.assertIn("hidden", html[at:at + 60])

    def test_the_count_follows_the_rows_as_they_load(self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "async function loadCategory("))
        reset = body[body.index("if (reset)"):]
        reset = reset[:reset.index("}")]
        self.assertIn("deltaState.rows = []", reset)
        self.assertIn("deltaState.rows.push(row)", body)
        self.assertIn("renderCommentedLine()", body)

    def test_comment_only_needs_a_note_and_a_selection(self) -> None:
        body = _strip_comments(_function_body(
            read("selection.js"), "function updateCommentButtonState("))
        self.assertIn('noteInputEl.value.trim() === ""', body)
        self.assertIn("selected.size === 0", body)
        self.assertIn("||", body)
        bar = _strip_comments(_function_body(
            read("selection.js"), "function ensureBar("))
        self.assertIn("commentBtnEl.disabled = true", bar)
        self.assertIn(
            'noteInputEl.addEventListener("input", '
            'updateCommentButtonState)', bar)

    def test_comment_only_is_rechecked_with_the_selection(self) -> None:
        """The bar re-renders as boxes are ticked; the button has to
        follow a selection that empties as well as a note that does."""
        body = _strip_comments(_function_body(
            read("selection.js"), "function updateAssignButtonState("))
        self.assertIn("updateCommentButtonState()", body)

    def test_comment_only_posts_the_selection_and_no_assignment(
            self) -> None:
        body = _strip_comments(_function_body(
            read("selection.js"), "async function doComment("))
        self.assertIn("requireUsername()", body)
        post_at = body.index("postJson(")
        sent = body[post_at:body.index(");", post_at)]
        self.assertIn("bulkCommentsUrl()", sent)
        self.assertIn("tests: testsPayload()", sent)
        self.assertIn("text: note", sent)
        self.assertNotIn("assigned_by", sent)
        self.assertNotIn("bulkAssignmentsUrl", body)
        self.assertIn("notifyChanged()", body)

    def test_each_selected_test_carries_its_own_origin(self) -> None:
        body = _strip_comments(_function_body(
            read("selection.js"), "function testsPayload("))
        self.assertIn("out.stream_id = entry.stream_id", body)

    def test_the_bulk_comment_request_carries_no_page_scope(self) -> None:
        body = _strip_comments(_function_body(
            read("selection.js"), "function bulkCommentsUrl("))
        self.assertIn('"api/comments/bulk"', body)
        for level in ("product", "stream", "baseline", "environment"):
            self.assertIn(level + ": null", body)


class AskForLessTest(unittest.TestCase):
    """WP-36: pages that were asking the server for far more than they
    read. Each of these still WORKS if it is reverted -- it is just
    slow again, on every page load, for everyone -- so each is pinned
    the way SummaryPartsFetchTest pins the split it guards."""

    def test_the_product_list_is_fetched_as_a_product_list(self) -> None:
        for name in ("products.js", "watch.js"):
            code = _strip_comments(read(name))
            self.assertIn("fetchProducts()", code, name)
            self.assertNotIn("api/summary", code, name)

    def test_it_falls_back_for_a_server_not_yet_restarted(self) -> None:
        """Static files are read from disk per request; Python is not.
        This script can arrive before the endpoint it asks for."""
        body = _strip_comments(_function_body(
            read("api.js"), "export async function fetchProducts("))
        first = body.index('"api/products"')
        caught = body.index("catch", first)
        self.assertIn(
            '"api/summary?parts=headline"', body[caught:])
        self.assertEqual(body.count("fetchJson("), 2)

    def test_importing_the_helper_has_no_side_effects(self) -> None:
        """It lives in api.js, not products.js: importing products.js
        adopts ?product= from the address bar, which the Watch page --
        whose URL is its own grammar -- must not start doing."""
        self.assertNotIn(
            "products.js", _strip_comments(read("watch.js")))
        self.assertNotIn(
            "function fetchProducts", _strip_comments(read("products.js")))

    def test_a_builds_page_asks_who_before_it_asks_how_many(self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "async function initBranchDashboard("))
        self.assertIn("fetchCompareIdentity(", body)
        self.assertNotIn("fetchCompare(", body)
        identity = _strip_comments(_function_body(
            read("compare.js"),
            "export async function fetchCompareIdentity("))
        self.assertIn('counts: "0"', identity)
        self.assertIn("stream: streamId", identity)
        self.assertIn("baseline:", identity)

    def test_its_own_results_never_runs_a_comparison(self) -> None:
        code = _strip_comments(read("app.js"))
        self.assertNotIn("fetchCompare(", code)
        body = _function_body(code, "function activateOwnResultsTab(")
        self.assertNotIn("initDeltaView(", body)

    def test_the_difference_tab_opens_on_one_request(self) -> None:
        """The header and the first page of rows come from the same
        answer. Asked separately, the comparison behind the counts ran
        twice, in sequence, before a row was shown."""
        body = _strip_comments(_function_body(
            read("compare.js"), "export async function initDeltaView("))
        first = body[body.index("fetchCompare("):]
        first = first[:first.index(");")]
        self.assertIn("deltaState.category", first)
        self.assertNotIn("null, 0", first)
        self.assertIn("loadCategory(true, data)", body)
        load = _strip_comments(_function_body(
            read("compare.js"), "async function loadCategory("))
        self.assertIn("fetched || await fetchCompare(", load)

    def test_later_pages_and_tabs_still_fetch_their_own(self) -> None:
        code = _strip_comments(read("compare.js"))
        self.assertIn("loadCategory(false)", code)
        tabs = _function_body(code, "function renderTabs(")
        self.assertIn("loadCategory(true)", tabs)
        self.assertNotIn("loadCategory(true, ", tabs)

    def test_the_difference_tab_runs_it_once_itself(self) -> None:
        body = _strip_comments(_function_body(
            read("compare.js"), "export async function initDeltaView("))
        self.assertEqual(body.count("fetchCompare("), 1)
        self.assertNotIn("fetchCompareIdentity(", body)


class MetricsPageTest(unittest.TestCase):
    """WP-37: the Metrics page. It was asked for on one condition --
    that it not cost response time -- and the half of that the front
    end owns is that the page asks once and then leaves the server
    alone."""

    #: Every page with the site's nav bar.
    _PAGES = (
        "actions.html", "index.html", "metrics.html", "script.html",
        "test.html", "time.html", "timeline.html", "watch.html",
        "whatsnew.html",
    )

    def test_there_is_no_timer_on_the_page(self) -> None:
        """A page of counters that polled would be traffic of its own
        in the figures it shows, and the sizes behind it are queries."""
        code = _strip_comments(read("metrics.js"))
        for name in ("setInterval", "setTimeout", "requestAnimationFrame",
                     "visibilitychange"):
            self.assertNotIn(name, code, name)

    def test_one_request_fills_it(self) -> None:
        code = _strip_comments(read("metrics.js"))
        self.assertEqual(code.count("fetchJson("), 1)
        self.assertEqual(len(fetch_sites(code, "api/metrics")), 2)
        load = _function_body(code, "async function load()")
        self.assertIn('"api/metrics"', load)
        self.assertEqual(code.count("postJson("), 1)

    def test_it_is_asked_for_on_open_and_on_refresh_only(self) -> None:
        code = _strip_comments(read("metrics.js"))
        init = _function_body(code, "function init()")
        self.assertIn('byId("reload-btn").addEventListener("click", load)',
                      init)
        self.assertEqual(init.count("load()"), 1)
        callers = [
            line for line in code.split("\n")
            if "load()" in line and "function load" not in line]
        self.assertEqual(len(callers), 2, callers)

    def test_its_requests_carry_no_page_scope(self) -> None:
        code = _strip_comments(read("metrics.js"))
        block = code[code.index("const NO_SCOPE"):]
        block = block[:block.index("};")]
        for level in ("product", "stream", "baseline", "environment"):
            self.assertIn(level + ": null", block)
        self.assertEqual(code.count("NO_SCOPE)"), 2)

    def test_a_requests_target_reaches_the_page_as_text(self) -> None:
        """It carries search text and test names typed by users."""
        code = _strip_comments(read("metrics.js"))
        self.assertNotIn("innerHTML", code)
        self.assertNotIn("insertAdjacentHTML", code)
        self.assertIn("entry.target", code)

    def test_reset_asks_again_afterwards(self) -> None:
        body = _strip_comments(_function_body(
            read("metrics.js"), "async function reset()"))
        post_at = body.index('"api/metrics/reset"')
        self.assertLess(post_at, body.index("await load()"))

    def test_a_server_that_is_not_collecting_is_said_to_be(self) -> None:
        body = _strip_comments(_function_body(
            read("metrics.js"), "function renderActivity("))
        guard_at = body.index("!activity.collecting")
        self.assertIn("--no-metrics", body[guard_at:])
        self.assertIn("return", body[guard_at:])
        self.assertIn(
            'byId("metrics-reset").disabled = !activity.collecting', body)

    def test_nothing_about_the_server_is_assumed(self) -> None:
        """Workers, engine, schema version and the histogram's edges
        are the response's to say."""
        code = _strip_comments(read("metrics.js"))
        for source in ("database.engine", "database.version",
                       "database.schema_version", "database.connections",
                       "database.kept_seconds",
                       "activity.bucket_edges_ms"):
            self.assertIn(source, code, source)
        for literal in ("SQLite", "MariaDB", "5000"):
            self.assertNotIn(literal, code, literal)

    def test_the_page_says_what_the_run_count_is(self) -> None:
        body = _strip_comments(_function_body(
            read("metrics.js"), "function renderDatabase("))
        self.assertIn("database.rows_not_counted", body)
        self.assertIn("not counted", body)

    def test_every_page_links_to_it(self) -> None:
        for name in self._PAGES:
            html = read(name)
            nav = html[html.index('<nav class="site-nav">'):]
            nav = nav[:nav.index("</nav>")]
            self.assertEqual(nav.count('href="metrics.html"'), 1, name)
            self.assertLess(
                nav.index('href="metrics.html"'),
                nav.index('href="whatsnew.html"'), name)

    def test_the_link_is_current_on_its_own_page_only(self) -> None:
        for name in self._PAGES:
            html = read(name)
            at = html.index('href="metrics.html"')
            tag = html[html.rindex("<a", 0, at):html.index(">", at)]
            self.assertEqual(
                "aria-current" in tag, name == "metrics.html", name)

    def test_the_page_is_not_a_scoped_nav_target(self) -> None:
        """It is about the server. A product or a build carried onto it
        would be a filter it silently ignores."""
        code = _strip_comments(read("urls.js"))
        block = code[code.index("NAV_SCOPE_PAGES = ["):]
        block = block[:block.index("];")]
        self.assertNotIn("metrics", block)


def _module_imports(name: str) -> List[str]:
    source = _strip_comments(read(name))
    return sorted(set(
        re.findall(r'from\s+"\./([a-z_]+\.js)"', source)
        + re.findall(r'import\s+"\./([a-z_]+\.js)"', source)))


def _module_closure(scripts: List[str]) -> List[str]:
    seen = []  # type: List[str]
    frontier = list(scripts)
    while frontier:
        name = frontier.pop(0)
        if name in seen:
            continue
        seen.append(name)
        frontier.extend(_module_imports(name))
    return seen


class ModulePreloadTest(unittest.TestCase):
    """WP-38: every page names, up front, every module it will import.

    Without it a browser discovers the import graph a level at a time:
    the page's own scripts, then what they import, then what those
    import -- one dependent round trip per level, each needing a free
    worker, before the first request for data can be made. With it the
    whole graph is fetched at once. The list is only worth having if it
    is complete and current, so it is pinned to the import graph
    itself, on every page.
    """

    def _pages(self) -> List[str]:
        return sorted(
            name for name in os.listdir(STATIC_DIR)
            if name.endswith(".html"))

    def test_the_preload_list_is_the_import_closure(self) -> None:
        for page in self._pages():
            html = read(page)
            scripts = re.findall(
                r'<script type="module" src="([^"]+)"', html)
            preloads = re.findall(
                r'<link rel="modulepreload" href="([^"]+)"', html)
            self.assertEqual(
                sorted(preloads), sorted(_module_closure(scripts)), page)
            self.assertEqual(len(preloads), len(set(preloads)), page)

    def test_it_is_in_the_head_before_any_script(self) -> None:
        for page in self._pages():
            html = read(page)
            first_preload = html.index('rel="modulepreload"')
            self.assertLess(first_preload, html.index("</head>"), page)
            self.assertLess(
                first_preload, html.index('<script type="module"'), page)

    def test_the_closure_helper_sees_a_real_graph(self) -> None:
        """The detector must be able to fail: app.js imports plenty."""
        self.assertGreater(len(_module_closure(["app.js"])), 6)
        self.assertIn("api.js", _module_closure(["app.js"]))


class FirstPaintTest(unittest.TestCase):
    """WP-38: the home page has a frame on screen before any script
    has loaded -- "hangs for a few hundred ms before rendering
    anything" was the report from production."""

    def test_every_mainline_section_ships_visible(self) -> None:
        """The frame, all of it: a section that appeared only when its
        data landed would push everything under it down."""
        html = read("index.html")
        for section in ("status-section", "charts-section",
                        "triage-section", "browse-section"):
            at = html.index('id="' + section + '"')
            tag = html[html.rindex("<section", 0, at):html.index(">", at)]
            self.assertNotIn("hidden", tag, section)

    def test_the_queue_renders_when_it_lands_not_when_the_headline_does(
            self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "async function loadQueue("))
        self.assertNotIn("if (state.summary)", body)
        self.assertIn("renderQueueTabs()", body)
        self.assertIn("renderQueueTable()", body)
        self.assertIn(
            'getElementById("triage-section").hidden = false', body)
        self.assertIn("payload.queue.stale_before = payload.stale_before",
                      body)

    def test_the_browse_page_renders_when_it_lands(self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "async function refreshAll("))
        browse_at = body.index("renderBrowse(state.browseRows, false)")
        self.assertIn(
            'getElementById("browse-section").hidden = false',
            body[browse_at:browse_at + 200])

    def test_a_badge_that_is_not_yet_known_is_not_a_zero(self) -> None:
        tabs = _strip_comments(_function_body(
            read("app.js"), "function renderQueueTabs()"))
        self.assertIn('queueCountKnown(tab.id) ? String(count) : "…"', tabs)
        self.assertIn("every(queueCountKnown)", tabs)
        known = _strip_comments(_function_body(
            read("app.js"), "function queueCountKnown("))
        self.assertIn("state.queues[queueId]", known)
        self.assertIn("state.summary.queue_totals", known)

    def test_the_queue_table_needs_nothing_from_the_headline(self) -> None:
        code = _strip_comments(read("app.js"))
        table = _function_body(code, "function renderQueueTable()")
        columns = _function_body(code, "function queueColumns(")
        options = _function_body(code, "function reviewOptions()")
        for body, name in ((table, "renderQueueTable"),
                           (columns, "queueColumns")):
            self.assertNotIn("state.summary.", body, name)
        self.assertIn("serverClock()", columns)
        # The cutoff: the headline's when it has landed, else the
        # queue's own -- never a read of the headline that assumes it.
        self.assertIn("state.summary ? state.summary.stale_before", options)
        self.assertIn("queue.stale_before", options)

    def test_the_placeholders_are_the_real_tiles(self) -> None:
        """Same labels, same order, same classes: the numbers replace
        the placeholders and nothing on the page moves."""
        html = read("index.html")
        row = html[html.index('id="stat-tiles"'):]
        row = row[:row.index("</div>\n    </section>")]
        placeholders = re.findall(
            r'<span class="tile-label">([^<]+)</span>', row)
        code = _strip_comments(_function_body(
            read("app.js"), "function renderStatus()"))
        real = re.findall(r'label: "([^"]+)"', code)
        self.assertEqual(placeholders, real)
        self.assertEqual(row.count('class="tile '), len(real))
        self.assertIn('class="tile tile-hero tile-skeleton"', row)
        self.assertEqual(row.count("tile-skeleton"), len(real))

    def test_the_numbers_replace_the_placeholders(self) -> None:
        body = _strip_comments(_function_body(
            read("app.js"), "function renderStatus()"))
        self.assertIn("clearNode(container)", body)
        self.assertLess(
            body.index("clearNode(container)"),
            body.index("buildTile("))

    def test_a_builds_page_hides_the_frame_before_its_first_request(
            self) -> None:
        body = _strip_comments(_function_body(read("app.js"), "function init()"))
        guard_at = body.index("streamId !== null")
        hide_at = body.index("hidden = true", guard_at)
        fetch_at = body.index("initBranchDashboard(", guard_at)
        self.assertLess(hide_at, fetch_at)
        self.assertIn("SECTIONS", body[guard_at:fetch_at])


class ScopeCarriageLinkMatrixTest(unittest.TestCase):
    """PART A of a follow-up link-matrix audit (after the F1-F7
    usability sweep): three more test.html links that only became
    reachable under stream scope in recent rounds, but still silently
    dropped it — the same DeltaViewTest link-carriage style, applied to
    app.js's triage queue, app.js's browse table, and timeline.js's
    expanded run rows. (PART B of the same audit — the app's
    script.html links, and script-page parity itself — is covered
    separately, in ScriptPageParityTest and friends.)"""

    def test_queue_rows_carry_the_stream_into_the_test_link(self) -> None:
        """WP-24, corrected by the coordinator fix round: the original
        conversion nulled product/baseline and left `stream` to pageUrl()'s
        DEFAULT carriage -- but naming `product` in an overrides object
        resets every level it contains (stream included) UNLESS that same
        object also names them, so the default never fired and this link
        silently dropped `stream=` (historical bug classes #1 and #5,
        reintroduced). The fix names `stream: state.streamId` explicitly
        in the SAME overrides object as product/baseline, so this pins the
        literal overrides object whole -- a future edit that nulls product
        without naming stream alongside it fails here again, rather than
        passing because product/baseline alone were checked."""
        body = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn(
            "{ product: null, baseline: null, stream: state.streamId }", body)

    def test_browse_rows_carry_the_stream_into_the_test_link(self) -> None:
        """WP-24, corrected by the coordinator fix round: same trap and
        same fix as the queue rows above -- see that test's docstring."""
        body = _strip_comments(_function_body(read("app.js"), "function buildRow("))
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn(
            "{ product: null, baseline: null, stream: state.streamId }", body)

    def test_timeline_run_rows_carry_the_stream_into_the_test_link(
        self
    ) -> None:
        """WP-24: the hand-rolled params/"test.html?" pair became one
        pageUrl() call with an explicit `stream: state.streamId` scope
        override -- same intent (a run row's link carries this page's
        own stream scope)."""
        body = _strip_comments(_function_body(
            read("timeline.js"), "function renderDetail("))
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn("stream: state.streamId", body)


class TimelineTimePickerParityTest(unittest.TestCase):
    """ADDENDUM to the perf round: the Build picker was mounted only on
    index.html -- time.html and timeline.html could already be ASKED
    for a stream on the wire (state.streamId, F7) but had no way to
    GET there except a link from a branch's own dashboard. streams.js's
    renderPicker() is page-agnostic (its own docstring says so); this
    is just the missing mount points."""

    def test_time_html_mounts_the_picker_and_loads_streams_js(
        self
    ) -> None:
        html = read_text("time.html")
        mount_at = html.index('id="stream-picker"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])
        self.assertIn('src="streams.js"', html)

    def test_timeline_html_mounts_the_picker_and_loads_streams_js(
        self
    ) -> None:
        html = read_text("timeline.html")
        mount_at = html.index('id="stream-picker"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])
        self.assertIn('src="streams.js"', html)

    def test_neither_mount_duplicates_the_id(self) -> None:
        for page in ("time.html", "timeline.html"):
            html = read_text(page)
            self.assertEqual(
                html.count('id="stream-picker"'), 1,
                "{} has {} stream-picker mounts".format(
                    page, html.count('id="stream-picker"')),
            )


class NavScopeCarriageTest(unittest.TestCase):
    """ADDENDUM to the perf round: nav.js's header links were bare
    hrefs, so Dashboard -> Timeline (or any of the three stream-aware
    pages) from a scoped page silently landed on mainline -- the same
    family of bug as ScopeCarriageLinkMatrixTest above, in the nav bar
    itself this time. carryScopeIntoNav() is the pure function that
    does the rewrite; live behaviour (a real walk through the rewritten
    link) was also driven against a scratch server this round -- see
    the commit message."""

    def test_only_carries_when_the_url_actually_has_a_scoping_param(
        self
    ) -> None:
        body = _function_body(
            read("nav.js"), "export function carryScopeIntoNav(")
        self.assertIn("params.has(name)", body)
        self.assertIn("carry.length === 0", body)

    def test_the_carried_param_list_is_exactly_the_scoping_family(
        self
    ) -> None:
        code = _strip_comments(read("nav.js"))
        self.assertIn(
            'const CARRIED_PARAMS = ["stream", "product", "environment"];',
            code,
        )

    def test_only_index_time_and_timeline_are_targets(self) -> None:
        """Pins the array to EXACTLY these three -- not "contains", so
        a future edit that adds actions.html/watch.html/whatsnew.html
        (sending a param to a page that would misread it) has to touch
        this test, not slip through unnoticed.

        WP-24: the allowlist itself moved to urls.js as
        NAV_SCOPE_PAGES (data shared by every pageUrl() caller that
        needs to know which pages are nav-bar targets); nav.js keeps
        only the separate CARRIED_PARAMS question. Assertion intent is
        unchanged -- still pinned to exactly these three, still failing
        if a fourth page is added silently."""
        code = _strip_comments(read("urls.js"))
        self.assertIn(
            'export const NAV_SCOPE_PAGES = '
            '["index.html", "time.html", "timeline.html"];',
            code,
        )
        self.assertIn(
            'from "./urls.js"', _strip_comments(read("nav.js")),
            "nav.js must import NAV_SCOPE_PAGES rather than redefining it")

    def test_non_matching_links_are_left_alone(self) -> None:
        """WP-24: the allowlist check itself is unchanged in shape
        (still a NAV_SCOPE_PAGES.indexOf(href) === -1 guard before
        `continue`) -- only the array's home moved, see the test
        above."""
        body = _function_body(
            read("nav.js"), "export function carryScopeIntoNav(")
        self.assertIn("NAV_SCOPE_PAGES.indexOf(href) === -1", body)
        self.assertIn("continue", body)

    def test_init_calls_it_independently_of_the_whatsnew_fetch(
        self
    ) -> None:
        """The scope-carriage rewrite must not be gated behind the
        What's new date fetch succeeding -- a network hiccup fetching
        whatsnew.html must not also silently break the nav bar."""
        body = _strip_comments(_function_body(
            read("nav.js"), "async function init("))
        call_at = body.index("carryScopeIntoNav(")
        fetch_at = body.index('fetch("whatsnew.html"')
        self.assertLess(
            call_at, fetch_at,
            "carryScopeIntoNav must run before the whatsnew fetch, not "
            "depend on it succeeding")

    def test_no_innerHTML(self) -> None:
        # _strip_comments first: nav.js's own docstring literally
        # explains "no innerHTML anywhere near it" in prose, which a
        # naive check would flag as a false positive.
        self.assertNotIn("innerHTML", _strip_comments(read("nav.js")))


class BuildBaselineWordingTest(unittest.TestCase):
    """WP-22 (docs/STREAMS_PLAN.md §4.1): every place that used to say
    the literal word "mainline" now reads it from the baseline's own
    identity -- a build's baseline is routinely a predecessor build."""

    def test_streamLabel_is_the_one_place_the_wording_is_built(self) -> None:
        body = _function_body(
            read("compare.js"), "export function streamLabel(")
        self.assertIn('"mainline"', body)
        self.assertIn("meta.kind", body)
        self.assertIn("meta.name", body)

    def test_baseline_card_never_hardcodes_mainline_in_its_prose(
            self) -> None:
        body = _function_body(
            read("compare.js"), "function renderBaselineCard(")
        self.assertIn("streamLabel(baselineMeta)", body)
        self.assertNotIn('"mainline last ran', body)
        self.assertNotIn('"this branch', body)

    def test_branch_band_accepts_an_optional_baseline(self) -> None:
        body = _function_body(
            read("compare.js"), "export function renderBranchBand(")
        self.assertIn("baselineMeta", body)
        self.assertIn("streamLabel(baselineMeta)", body)

    def test_watch_card_reads_baseline_identity_from_the_card(
            self) -> None:
        code = _strip_comments(read("watch.js"))
        body = _function_body(code, "function buildStreamCard(")
        self.assertIn("baselineLabel(card)", body)
        label_body = _function_body(code, "function baselineLabel(")
        self.assertIn("card.baseline_kind", label_body)
        self.assertIn("card.baseline_name", label_body)


class BuildVerdictLineTest(unittest.TestCase):
    """F5 (docs/STREAMS_PLAN.md §5.2 "as built"); restored after a WP-25
    fix-round finding that the kind collapse over-deleted a user-visible
    feature instead of data-gating it (docs/ONE_KIND_PLAN.md §1.4 asks
    for kind-GATES to become data-gates, not for kind-gated BEHAVIOR to
    be deleted). A delta view compared against only one baseline at a
    time answers "is this RC good?" incompletely. The verdict line names
    BOTH canonical baselines -- the previous build and mainline -- built
    from one extra counts-only /api/compare call for whichever of the
    two is not already loaded, fired lazily so it never delays first
    paint. The predecessor lookup mirrors Storage.previous_builds'
    ordering rule via a dedicated helper (findPredecessorBuild) rather
    than the deleted default-baseline picker -- that function stays gone
    per WP-25 (default baseline is mainline, always); this one only ever
    LABELS the verdict line, never chooses what the page actually
    compares against."""

    def test_the_mount_ships_hidden_in_the_markup(self) -> None:
        html = read_text("index.html")
        mount_at = html.index('id="delta-verdict"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])

    def test_hidden_for_mainline_scope(self) -> None:
        """WP-25 collapsed 'not a build' to 'is mainline' -- the only
        other kind there is now -- so this is the same hide-gate as
        before, worded for the one-kind world."""
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        kind_at = body.index('data.stream.kind === "mainline"')
        self.assertIn("line.hidden = true", body[kind_at:kind_at + 80])

    def test_hidden_with_no_predecessor_build(self) -> None:
        """A product's first build has nothing to name as "the previous
        build" — the line stays hidden rather than half-naming one
        side of a two-sided sentence."""
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        pred_at = body.index("predecessor === null")
        self.assertIn("line.hidden = true", body[pred_at:pred_at + 80])

    def test_the_predecessor_helper_is_not_the_deleted_default_picker(
        self
    ) -> None:
        """WP-25 (docs/ONE_KIND_PLAN.md §1.3): the default baseline is
        mainline, always -- there is no default-baseline picker left to
        drive the actual comparison. This helper is a DIFFERENT function
        with a different job (labeling only); it must not have come
        back under the old name."""
        self.assertIn("function findPredecessorBuild(", read("compare.js"))
        self.assertNotIn("pickDefaultBuildBaseline", read("compare.js"))

    def test_viewing_vs_a_picked_build_baseline_names_mainline(
        self
    ) -> None:
        """The direction that is now the UNCOMMON one under WP-25 (the
        default is mainline; a picked build baseline only happens via
        the Compare-to control) still names mainline's own verdict --
        the same branch of the original two-sided logic, worth pinning
        in its own right now that it is no longer the default path."""
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        self.assertIn(
            "currentBaselineId === null ? data.counts : null", body)
        fetch_at = body.index("if (mainlineCounts === null) {")
        self.assertIn(
            "fetchCompare(streamId, null, 0, null)",
            body[fetch_at:fetch_at + 120])

    def test_reuses_already_loaded_counts_instead_of_refetching(
        self
    ) -> None:
        """Whichever baseline this page was actually opened with must
        NOT be fetched a second time — that is the whole "ONE extra
        call" saving."""
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        self.assertIn(
            "currentBaselineId === predecessor.id ? data.counts : null",
            body)
        self.assertIn(
            "currentBaselineId === null ? data.counts : null", body)

    def test_a_failed_extra_fetch_hides_rather_than_shows_an_error(
        self
    ) -> None:
        """Enrichment only — a failure here must not put an error
        banner over a delta view that otherwise loaded fine."""
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        catch_at = body.index("} catch (err) {")
        catch_block = body[catch_at:body.index("}", catch_at + 20) + 1]
        self.assertIn("line.hidden = true", catch_block)
        self.assertNotIn("showError", catch_block)

    def test_guards_against_a_stale_render(self) -> None:
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        self.assertIn("deltaState.streamId !== streamId", body)

    def test_wording_names_both_baselines_and_both_counts(self) -> None:
        body = _function_body(
            read("compare.js"), "async function renderBuildVerdict(")
        self.assertIn('"vs " + streamLabel(predecessor)', body)
        self.assertIn("predecessorCounts.new_failures", body)
        self.assertIn("predecessorCounts.new_passes", body)
        self.assertIn('" fixed"', body)
        self.assertIn('" — vs mainline: "', body)
        self.assertIn("mainlineCounts.new_failures", body)

    def test_initDeltaView_calls_it_without_awaiting(self) -> None:
        """The whole point: this call must not sit on the critical path
        between the main fetch resolving and delta-section becoming
        visible."""
        body = _strip_comments(
            _function_body(
                read("compare.js"), "export async function initDeltaView("))
        call_at = body.index("renderBuildVerdict(streamId, data, productStreams)")
        self.assertNotIn(
            "await renderBuildVerdict",
            body[max(0, call_at - 10):call_at + 10])
        visible_at = body.index('document.getElementById("delta-section").hidden = false')
        self.assertLess(call_at, visible_at,
                         "fired before the section is shown, not after")

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("compare.js")))


class CompareToControlTest(unittest.TestCase):
    """The dashboard's "Compare to" datalist combo (WP-22,
    docs/STREAMS_PLAN.md §4.1; un-gated by WP-25, docs/ONE_KIND_PLAN.md
    §1.3) -- shown for EVERY non-mainline stream, the default baseline
    always mainline unless this control is used to name another."""

    def test_the_markup_ships_hidden(self) -> None:
        html = read_text("index.html")
        field_at = html.index('id="compare-to-field"')
        self.assertIn("hidden", html[field_at:field_at + 60])
        self.assertIn('id="compare-to-input"', html)
        self.assertIn('id="compare-to-options"', html)
        self.assertIn('list="compare-to-options"', html)

    def test_the_control_hides_only_for_mainline(self) -> None:
        body = _function_body(
            read("compare.js"), "function renderCompareToControl(")
        self.assertIn('streamMeta.kind === "mainline"', body)
        self.assertIn("field.hidden = true", body)

    def test_pickDefaultBuildBaseline_is_gone(self) -> None:
        """WP-25 (docs/ONE_KIND_PLAN.md §1.3, user decision, explicit):
        default baseline is mainline, always -- the Compare-to control
        above is how a predecessor gets chosen now, not an automatic
        pick. Deleted with its feature, not merely unused."""
        self.assertNotIn("pickDefaultBuildBaseline", read("compare.js"))

    def test_scope_changes_reset_narrower_scopes(self) -> None:
        """Changing a scope resets every narrower scope beneath it
        (product > stream > baseline; product > environment). Found by
        auditing the whole control family after the choosing-mainline
        sentinel bug (2026-08-09): the Build picker carried an RC's
        baseline into branches/mainline, and the product switcher
        carried another product's stream/baseline/environment — either
        contradictory or never-chosen.

        WP-24: the resets themselves are no longer hand-rolled
        `searchParams.delete(...)` calls in each of the two controls —
        they are urls.js's resolveScope() hierarchy rule (product
        resets stream/baseline/environment; stream resets baseline),
        exercised through withStream()/withProduct(). Same assertion
        intent (a scope change resets what it contains), now checking
        that both controls actually call the shared helper rather than
        keeping their own copy of the reset logic — and UrlsModuleTest
        pins the reset logic itself directly."""
        picker = _function_body(
            read("streams.js"), "export function renderPicker(")
        self.assertIn("withStream(target || null)", picker)
        self.assertIn(
            "withStream", _imported_names(read("streams.js")),
            "streams.js calls withStream() without importing it")
        switcher_body = _function_body(
            read("products.js"), "export function renderSwitcher(")
        self.assertIn("withProduct(select.value)", switcher_body)
        self.assertIn(
            "withProduct", _imported_names(read("products.js")),
            "products.js calls withProduct() without importing it")

    def test_choosing_mainline_sets_an_explicit_baseline_param(self) -> None:
        """Choosing "Mainline nightlies" must write baseline=1, never
        DELETE the param: on a build page "no baseline" already means
        "default to the previous build", so an absence-encoded mainline
        choice is indistinguishable from no choice and snaps back to
        the predecessor — reported live by the first person to switch
        an RC's comparison back to mainline (2026-08-09). Mainline's
        stream id is 1 by migration-9 invariant (MAINLINE_STREAM_ID).

        WP-24: this encoding is now urls.js's withBaseline("mainline"),
        the ONE call site allowed to write the explicit "1"
        (UrlsModuleTest pins the "1" encoding itself). Same assertion
        intent, now checking the call site rather than the inline
        searchParams.set that no longer exists here."""
        body = _function_body(
            read("compare.js"), "function renderCompareToControl(")
        self.assertIn('withBaseline("mainline")', body)
        self.assertNotIn('searchParams.delete("baseline")', body)
        self.assertIn(
            "withBaseline", _imported_names(read("compare.js")),
            "compare.js calls withBaseline() without importing it")

    def test_the_baseline_param_lives_only_in_the_url(self) -> None:
        body = _function_body(
            read("compare.js"), "export function getSelectedBaselineId(")
        self.assertIn("window.location.search", body)
        self.assertNotIn("localStorage", body)

    def test_a_mainline_scoped_page_never_fetches_the_product_stream_list(
            self) -> None:
        """WIDENED for WP-25 (docs/ONE_KIND_PLAN.md §1.3): the extra
        /api/streams round trip the Compare-to control needs is paid by
        every NON-MAINLINE page now (the control is un-gated from
        kind='build' to "any non-mainline stream"), not only a
        build-scoped one -- but a hand-crafted `?stream=1` must still
        skip it, the same defence the old branch/build split gave for
        free."""
        body = _function_body(
            read("compare.js"), "export async function initDeltaView(")
        fetch_at = body.index("fetchProductStreams(")
        guard_at = body.rindex('!== "mainline"', 0, fetch_at)
        self.assertLess(guard_at, fetch_at)


class BuildPickerGroupingTest(unittest.TestCase):
    """WP-22 (docs/STREAMS_PLAN.md §4.1): the Build picker is a
    substring-searchable datalist combo (a native <select>'s type-ahead
    only prefix-matches, which would find nothing for "rc2" typed
    against "2026.9.1-rc2"). WIDENED for WP-25 (docs/ONE_KIND_PLAN.md
    §1.4): the branch/build group split is gone -- one flat group,
    newest first by last_seen, since streams.kind is only ever
    'mainline' or 'build' now."""

    def test_picker_is_a_datalist_combo_not_a_select(self) -> None:
        body = _function_body(
            read("streams.js"), "export function renderPicker(")
        self.assertIn("datalist", body)
        self.assertNotIn("createElement(\"select\")", body)

    def test_the_group_split_is_gone(self) -> None:
        """No separate branches/builds arrays -- one filter-free sort
        over the whole list."""
        body = _strip_comments(_function_body(
            read("streams.js"), "export function renderPicker("))
        self.assertNotIn('kind === "branch"', body)
        self.assertNotIn("branches.concat(builds)", body)

    def test_an_unrecognised_typed_value_is_a_no_op(self) -> None:
        """A typo or a stale suggestion must not navigate anywhere --
        the same rule compare.js's Compare-to control follows."""
        body = _function_body(
            read("streams.js"), "export function renderPicker(")
        self.assertIn("target === undefined", body)
        return_at = body.index("target === undefined")
        self.assertIn("return", body[return_at:return_at + 40])

    def test_streams_are_sorted_newest_first(self) -> None:
        code = _strip_comments(read("streams.js"))
        self.assertIn(".sort(byNewest)", code)
        sort_body = _function_body(code, "function byNewest(")
        self.assertIn("a.last_seen < b.last_seen", sort_body)


class CompareStripTest(unittest.TestCase):
    """test.js's compare strip (WP-21 §3.6): mainline's result beside a
    branch's, on the test-detail page, only when scoped."""

    def test_the_strip_hides_when_unscoped(self) -> None:
        body = _function_body(read("test.js"), "async function loadCompareStrip(")
        self.assertIn("streamId === null", body)
        self.assertIn("strip.hidden = true", body)

    def test_it_calls_the_shared_renderer(self) -> None:
        code = _strip_comments(read("test.js"))
        self.assertIn("renderCompareStrip", _imported_names(read("test.js")))
        self.assertIn("renderCompareStrip(", code)

    def test_history_and_detail_carry_the_stream_param(self) -> None:
        """WP-24: test.js's own local `withStream(url)` helper (append-
        stream-to-an-already-built-URL-string) is GONE — a genuine name
        collision with urls.js's exported withStream() (a different,
        picker-navigation helper: "current page with one scope level
        changed"), so it was deleted rather than kept alongside a
        same-named import. Both call sites now go through apiUrl()
        with an explicit `stream: streamApiScope()` override
        (streamApiScope() is the NaN-guard the old helper carried
        inline); same assertion intent (both fetches carry this page's
        own stream scope).

        WIDENED 2026-08-09 (persona-walk round): the count grew from 2
        to 3 when renderLatest() gained the "View in timeline" link,
        which carries the same page scope through the same override
        (TestPageTimelineLinkTest pins that site's own shape). The
        assertion's intent is unchanged — every carrier of this page's
        stream scope uses the NaN-guarded form, and the count pin still
        fails if one of the three sites loses it."""
        code = _strip_comments(read("test.js"))
        self.assertNotIn("function withStream(", code)
        self.assertIn("function streamApiScope()", code)
        self.assertIn("apiUrl(myPath(), {}, {", code)
        self.assertIn('apiUrl(myPath("/history"), {', code)
        self.assertEqual(code.count("stream: streamApiScope()"), 3)


class TestPageBranchBandTest(unittest.TestCase):
    """The branch band on test.html (WP-21 §3.6, found in first human
    use): a reader deep in a test's history/analytics/compare strip had
    no loud indication they were scoped to a branch at all. Shares
    compare.js's renderBranchBand with the dashboard rather than a
    second implementation, the same "one shared control" rule as the
    assignee select / review panel."""

    def test_the_band_ships_hidden_in_the_markup(self) -> None:
        html = read_text("test.html")
        band_at = html.index('id="branch-band"')
        self.assertIn("hidden", html[band_at:band_at + 60])

    def test_the_mount_ids_match_the_dashboards(self) -> None:
        """renderBranchBand() is one function, shared — it can only work
        unmodified on both pages if the element ids agree."""
        for name in ("index.html", "test.html"):
            html = read_text(name)
            self.assertIn('id="branch-band"', html, name)
            self.assertIn('id="branch-band-text"', html, name)
            self.assertIn('id="branch-band-back"', html, name)

    def test_test_js_calls_the_shared_renderer(self) -> None:
        code = _strip_comments(read("test.js"))
        self.assertIn("renderBranchBand", _imported_names(read("test.js")))
        self.assertIn("renderBranchBand(", code)

    def test_the_renderer_guards_a_missing_mount(self) -> None:
        """Every page that might not carry the band (none do NOT carry
        it today, but the guard is what makes that safe to change)
        must not throw mid-render — the same rule
        ProductSwitcherHostManagedTest pins for the product switcher's
        host-managed call sites."""
        body = _function_body(
            read("compare.js"), "export function renderBranchBand(")
        self.assertIn('getElementById("branch-band")', body)
        guard_at = body.index("if (!container")
        self.assertIn("return", body[guard_at:guard_at + 40])

    def test_the_back_link_strips_only_the_stream_param(self) -> None:
        """"Back to mainline" must preserve whatever else the page
        needs (environment/script/test_name on test.html) — a fixed
        target like index.html would land test.html's reader on the
        wrong page entirely.

        WP-24: this is now withStream(null) (urls.js) — same intent
        (this URL, with stream cleared, everything else untouched),
        pinned in UrlsModuleTest for the general mechanism and here
        for the call site."""
        body = _function_body(
            read("compare.js"), "export function renderBranchBand(")
        self.assertIn("backLink.href = withStream(null)", body)
        self.assertIn(
            "withStream", _imported_names(read("compare.js")),
            "compare.js calls withStream() without importing it")


class SuiteLinkParityTest(unittest.TestCase):
    """F3, superseded (docs/STREAMS_PLAN.md §5.2 "as built"):
    script.html originally had no stream support of its own, so a
    stream-scoped test page's suite link silently jumped back to
    MAINLINE's execution history — F3 could only say so (a title plus
    a visible "(mainline)" note), not fix it. Script-page parity (the
    FINAL ROUND) gives script.html real stream support, so the suite
    link now carries `stream=` through instead — the same scope-self-
    sufficient pattern every other inbound link to a scoped page
    follows — and the honesty label is gone because the thing it was
    warning about no longer happens.
    """

    def test_the_link_carries_the_pages_own_stream(self) -> None:
        """WP-24: the hand-rolled params/"script.html?" pair became one
        pageUrl() call with an explicit `stream: streamId` override —
        same intent (the page's own raw, un-NaN-guarded streamId, same
        as before this conversion)."""
        body = _function_body(read("test.js"), "function renderDetail(")
        self.assertIn('suiteLink.href = pageUrl("script"', body)
        self.assertIn("{ stream: streamId, product: null, baseline: null }",
                       body)

    def test_the_mainline_honesty_label_is_gone(self) -> None:
        """F3's "(mainline)" title/note both depended on
        detail.stream_identity being truthy — neither should exist any
        more now that the link honours the page's own scope instead of
        silently ignoring it. Scoped to just the suite-link-building
        portion of renderDetail(): the function legitimately reads
        detail.stream_identity elsewhere too (state.streamIdentity,
        unrelated to this link)."""
        body = _strip_comments(
            _function_body(read("test.js"), "function renderDetail("))
        link_section = body[
            body.index("identity.appendChild(document.createTextNode("):
            body.index("identity.appendChild(suiteLink);")]
        self.assertNotIn("(mainline)", link_section)
        self.assertNotIn("detail.stream_identity", link_section)

    def test_the_title_is_a_plain_constant_again(self) -> None:
        body = _function_body(read("test.js"), "function renderDetail(")
        self.assertIn(
            'suiteLink.title = "Execution history for this suite";', body)

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("test.js")))


class ScriptPageParityTest(unittest.TestCase):
    """Script-page parity, PART B of the FINAL ROUND's link-matrix
    audit follow-up (docs/STREAMS_PLAN.md §5.2 "as built"): script.html
    was the last mainline-only tool. It now accepts `?stream=`, forwards
    it to every request it makes, renders the shared branch band, and
    every inbound link to it (from app.js, timeline.js, test.js) carries
    the scope through — the same pattern the rest of the app already
    follows."""

    def test_state_reads_stream_from_the_url(self) -> None:
        body = _function_body(read("script.js"), "function init()")
        self.assertIn('url.searchParams.get("stream")', body)
        self.assertIn("state.streamId = rawStream ? parseInt", body)

    def test_executions_fetch_forwards_the_stream(self) -> None:
        """WP-24: `path += "&stream=" + state.streamId` became an
        explicit `stream: state.streamId` scope override on the
        apiUrl() call -- same intent (this fetch forwards the page's
        own stream scope)."""
        body = _function_body(
            read("script.js"), "async function loadExecutions()")
        self.assertIn("apiUrl(", body)
        self.assertIn("stream: state.streamId", body)

    def test_the_tests_table_fetch_also_forwards_the_stream(self) -> None:
        """The "tests in this suite" table reads /api/dashboard, a
        DIFFERENT endpoint from /executions — found by the same
        discipline F7's timeline.js fix used: every outbound request a
        stream-scoped page makes needs the param, not only the first
        one.

        WP-24: `qs.append("stream", ...)` became the same `stream:
        state.streamId` scope override pattern as loadExecutions()
        above, now on an apiUrl() call."""
        body = _function_body(read("script.js"), "async function loadTests(")
        self.assertIn("apiUrl(", body)
        self.assertIn("stream: state.streamId", body)

    def test_renders_the_shared_branch_band_when_scoped(self) -> None:
        self.assertIn(
            "renderBranchBand", _imported_names(read("script.js")))
        body = _function_body(
            read("script.js"), "function renderExecutions(")
        self.assertIn(
            "if (state.streamId !== null && data.stream_identity) {", body)
        self.assertIn("renderBranchBand(data.stream_identity)", body)

    def test_the_tests_table_links_carry_the_stream(self) -> None:
        """WP-24: the hand-rolled URLSearchParams/"test.html?" pair
        became one pageUrl() call with an explicit `stream:
        state.streamId` scope override."""
        body = _function_body(read("script.js"), "function renderTests(")
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn("stream: state.streamId", body)

    def test_the_band_mount_ships_hidden_in_the_markup(self) -> None:
        html = read_text("script.html")
        self.assertIn('id="branch-band" class="branch-band" hidden', html)

    def test_the_mount_ids_match_the_other_pages(self) -> None:
        html = read_text("script.html")
        self.assertIn('id="branch-band-text"', html)
        self.assertIn('id="branch-band-back"', html)

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("script.js")))

    def test_app_js_scriptLink_carries_the_stream(self) -> None:
        """WP-24, corrected by the coordinator fix round: the original
        conversion nulled product/baseline and left `stream` to pageUrl()'s
        default carriage -- but naming `product` resets every level it
        contains (stream included) unless that same overrides object also
        names them, so the default never fired and this link silently
        dropped `stream=` (historical bug classes #1 and #5, reintroduced).
        The fix names `stream: state.streamId` explicitly alongside
        product/baseline, so this pins the overrides object whole rather
        than product/baseline alone."""
        body = _strip_comments(_function_body(
            read("app.js"), "function scriptLink("))
        self.assertIn('link.href = pageUrl("script"', body)
        self.assertIn(
            "{ product: null, baseline: null, stream: state.streamId }", body)

    def test_timeline_js_block_link_carries_the_stream(self) -> None:
        """The audit's PART A note: the block row's own script.html
        link was found unscoped in the same pass as the run-row test
        links, but left for script-page parity to fix here, since a
        stream= param on it was only useful once script.html could
        read one.

        WP-24: same pageUrl() conversion as the run-row test link
        above."""
        body = _strip_comments(_function_body(
            read("timeline.js"), "function buildRow("))
        self.assertIn('link.href = pageUrl("script"', body)
        self.assertIn("stream: state.streamId", body)


class EveryBuildAndStreamSwitcherTest(unittest.TestCase):
    """test.js's WP-22 additions (docs/STREAMS_PLAN.md §4.1): the
    per-stream result dropdown near the top of the page, and the "Every
    build" disclosure table below the analytics section."""

    def test_the_switcher_markup_ships_hidden(self) -> None:
        html = read_text("test.html")
        field_at = html.index('id="stream-switcher-field"')
        self.assertIn("hidden", html[field_at:field_at + 60])
        self.assertIn('id="stream-switcher-select"', html)

    def test_the_every_build_section_ships_hidden(self) -> None:
        html = read_text("test.html")
        section_at = html.index('id="every-build-section"')
        self.assertIn("hidden", html[section_at:section_at + 60])
        self.assertIn('id="every-build-body"', html)

    def test_the_switcher_hides_with_fewer_than_two_options(self) -> None:
        """A one-entry "switcher" is not one -- this is only useful once
        there is somewhere else to switch TO."""
        body = _function_body(
            read("test.js"), "function renderStreamSwitcher(")
        self.assertIn("results.length < 2", body)
        self.assertIn("field.hidden = true", body)

    def test_switching_never_changes_which_test_is_shown(self) -> None:
        """WP-24: the params.append() triple became one pageUrl() call
        whose `params` argument names all three identity fields — same
        intent, switching streams never changes environment/script/
        test_name."""
        body = _function_body(read("test.js"), "function testPageUrl(")
        self.assertIn("environment: ident.environment", body)
        self.assertIn("script: ident.script", body)
        self.assertIn("test_name: ident.test_name", body)

    def test_the_dropdown_only_lists_results_never_widened(self) -> None:
        """docs/STREAMS_PLAN.md §4.1: the dropdown wants exactly the
        per-triple results list -- widening it with NO RESULT entries
        (the "Every build" table's job) would offer a destination with
        nothing to switch to."""
        body = _function_body(
            read("test.js"), "function renderStreamSwitcher(")
        self.assertNotIn("productStreams", body)
        self.assertNotIn("withoutResult", body)

    def test_no_result_is_never_a_chip_in_the_every_build_table(
            self) -> None:
        """The same defect ResultEmphasisTest/compare.js's
        noResultCell() pin elsewhere: an absent result must never render
        as a coloured chip variant."""
        body = _function_body(
            read("test.js"), "function buildEveryBuildRow(")
        self.assertIn('"no result"', body)
        self.assertNotIn("ghostChip(", body)

    def test_the_union_keeps_the_per_triple_results_intact(self) -> None:
        """docs/STREAMS_PLAN.md §4.1's absence rule + the general "union
        by id, do not rebuild from the picker list" reasoning: the
        results the per-triple endpoint returned must never be dropped
        just because a stream is absent from /api/streams (e.g. after a
        product remap) -- only ADDED to, for the streams present there
        that have no result."""
        body = _function_body(read("test.js"), "async function loadEveryBuild(")
        self.assertIn("data.results.slice()", body)
        self.assertIn("resultKeys", body)

    def test_a_single_row_disclosure_stays_hidden(self) -> None:
        body = _function_body(read("test.js"), "async function loadEveryBuild(")
        self.assertIn("rows.length < 2", body)

    def test_init_calls_load_every_build(self) -> None:
        body = _function_body(read("test.js"), "async function init(")
        self.assertIn("loadEveryBuild()", body)


class CommentStreamTagTest(unittest.TestCase):
    """The "posted from" tag on each comment (WP-21 §3.6) — every
    comment shows in full regardless of the page's current scope, tagged
    with the stream it was posted from, batch-resolved by the server."""

    def test_comment_node_takes_the_batch_resolved_streams_map(self) -> None:
        body = _function_body(read("test.js"), "function commentNode(")
        self.assertIn("streams[String(comment.stream_id)]", body)
        self.assertIn("posted from", body)

    def test_load_comments_passes_the_servers_streams_map_through(self) -> None:
        body = _function_body(
            read("test.js"), "async function loadComments()")
        self.assertIn("data.streams", body)
        self.assertIn("commentNode(comment, streams)", body)

    def test_a_comment_posted_from_a_scoped_page_is_tagged(self) -> None:
        """Posting while scoped to a branch sends stream_id, so the
        thread records what was true when the note was written."""
        body = _function_body(
            read("test.js"), "async function onCommentSubmit(")
        self.assertIn("body.stream_id = streamId", body)

    def test_an_assignment_made_from_a_scoped_page_is_tagged(self) -> None:
        """The assignee PUT sends stream_id under the SAME guard the
        comment POST above has always used. Found by the 2026-08-09
        persona walk: assigning from a build-scoped test page landed as
        mainline-origin — no origin tag in Open Actions, origin filter
        never appeared — while the identical action through the
        dashboard's review panel (api.js assigneeSelect) carried it.
        One action, two on-screen controls, one behavior."""
        body = _function_body(
            read("test.js"), "async function onAssigneeChange()")
        self.assertIn("body.stream_id = streamId", body)


class WatchStreamCardTest(unittest.TestCase):
    """watch.js's `s:` card (WP-21 §3.6): a branch/build verdict card on
    the Watchlist."""

    def test_stream_cards_use_the_shared_category_labels(self) -> None:
        body = _function_body(read("watch.js"), "function buildStreamCard(")
        self.assertIn("CATEGORY_ORDER", body)
        self.assertIn("CATEGORY_LABELS", body)

    def test_the_open_link_uses_the_stream_id_not_its_name(self) -> None:
        """Two products can each have a "feat/x" branch — only the id is
        unambiguous (docs/STREAMS_PLAN.md §3.6).

        WP-24: cardLink() composes through pageUrl() (its own
        docstring's exemption note) — `params.set("stream", ...)`
        became `stream: card.id` in the explicit scope override."""
        body = _function_body(read("watch.js"), "function cardLink(")
        stream_at = body.index('card.kind === "stream"')
        self.assertIn("stream: card.id", body[stream_at:])

    def test_the_open_link_also_carries_the_streams_own_product(
        self
    ) -> None:
        """WP-23 bugfix: a stream card's link must be scope-self-
        sufficient — landing on index.html with only ?stream= set would
        render under whatever product this browser's switcher last had
        stored, not necessarily the stream's own.

        WP-24: `params.set("product", ...)` became `product:
        card.product || ""` in the same scope override."""
        body = _function_body(read("watch.js"), "function cardLink(")
        stream_at = body.index('card.kind === "stream"')
        self.assertIn('product: card.product || ""', body[stream_at:])

    def test_the_add_picker_offers_a_branch_build_option(self) -> None:
        self.assertIn('<option value="s">', read_text("watch.html"))

    def test_stream_picker_entries_are_keyed_by_id(self) -> None:
        body = _function_body(read("watch.js"), "async function populatePicker(")
        self.assertIn("value: String(stream.id)", body)


class CardLinkScopeSelfSufficiencyTest(unittest.TestCase):
    """WP-23 bugfix: a Watch card whose scope differs from whatever
    product this browser's switcher last had selected must still land
    on the RIGHT scope, not the OLD one with a now-mismatched
    environment filter (docs/STREAMS_PLAN.md §0.9's "the URL is the
    whole configuration", extended to a card's OWN link)."""

    def test_environment_card_link_carries_its_own_product(self) -> None:
        """WP-24: `params.set("product", ...)` became `product:
        card.product || ""` in the explicit scope override passed to
        pageUrl() alongside `{ environment: card.name }`."""
        body = _function_body(read("watch.js"), "function cardLink(")
        env_at = body.index('card.kind === "environment"')
        stream_at = body.index('card.kind === "stream"')
        environment_branch = body[env_at:stream_at]
        self.assertIn('product: card.product || ""', environment_branch)
        self.assertIn('{ environment: card.name }', environment_branch)

    def test_product_card_link_is_unchanged(self) -> None:
        """A product card already names the product by construction —
        no second field to add.

        WP-24: the single params.set() call became one pageUrl() call
        with a single-key scope override — same intent, checking the
        override carries exactly `product`, no `environment`/`stream`
        alongside it."""
        body = _function_body(read("watch.js"), "function cardLink(")
        product_at = body.index('card.kind === "product"')
        env_at = body.index('card.kind === "environment"')
        product_branch = body[product_at:env_at]
        self.assertIn('pageUrl("index", {}, { product: card.name })',
                       product_branch)


class WatchUnassignedStatLinkTest(unittest.TestCase):
    """F4(b) (docs/STREAMS_PLAN.md §5.2 "as built"): a Watch card's
    "Unassigned failing" stat is a way IN, not only a number — a
    product/environment card's stat scopes the dashboard's browse
    table straight to the failing, unassigned rows; a stream card's
    stat goes to its own branch-scoped dashboard instead, since that
    view already shows every row's assignee inline (no result=/
    unassigned= filters of its own to receive them)."""

    def test_product_and_environment_cards_add_result_and_unassigned(
        self
    ) -> None:
        body = _function_body(
            read("watch.js"), "function unassignedStatLink(")
        self.assertIn('params.set("result", "FAIL")', body)
        self.assertIn('params.set("unassigned", "1")', body)

    def test_stream_cards_also_get_result_and_unassigned(self) -> None:
        """Advisor-caught regression risk: a special case that omitted
        these params for kind === 'stream' (reasoning "the delta view
        already shows assignees inline") is WRONG for a long-running
        branch — 2+ covered passes (OWN_RESULTS_DEFAULT_PASSES, app.js)
        defaults to "Its own results", the SAME browse table these
        params are built for, not the delta view at all. Applying them
        unconditionally is safe either way: the diff tab's own render
        path never reads result=/unassigned= from the URL, so they are
        inert there rather than wrong."""
        body = _function_body(
            read("watch.js"), "function unassignedStatLink(")
        self.assertNotIn('card.kind === "stream"', body)
        self.assertIn('params.set("result", "FAIL")', body)
        self.assertIn('params.set("unassigned", "1")', body)

    def test_the_hero_links_the_count_only_when_nonzero(self) -> None:
        """The F4 link moved into the hero with the count (2026-08-10
        redesign) and kept its one behavioral rule: a zero renders as a
        plain muted number, never a dead link. The count itself now
        ALWAYS shows — the redesign's point is that "0 unassigned" is
        the good news a morning scan looks for — so the old
        zero-adds-no-stat pin is superseded, not weakened: the
        click-through gate is what it always protected, and it still
        holds."""
        body = _function_body(read("watch.js"), "function buildHero(")
        gate_at = body.index("if (count > 0) {")
        else_at = body.index("} else {", gate_at)
        self.assertIn("unassignedStatLink(card)", body[gate_at:else_at])
        self.assertNotIn(
            "unassignedStatLink", body[else_at:])

    def test_both_card_builders_render_the_hero(self) -> None:
        for fn in ("function buildOkCard(", "function buildStreamCard("):
            body = _function_body(read("watch.js"), fn)
            self.assertIn("buildHero(card, nowMs)", body, fn)

    def test_the_supporting_stats_are_plain_numbers_again(self) -> None:
        """buildStat's optional href argument died with its one caller
        when the link moved to the hero — deleted with the feature, so
        a second rendering path cannot drift back in.

        Widened for WP-40 (not weakened): the signature may now carry
        ONE more argument, the count of muted failures to name beside a
        failing figure -- "(+3 muted)" -- and the href ban stands, as
        does the rule that a stat is a plain value and a label.

        Widened again for WP-44 item 7 (not weakened): one more count,
        the flaking failures set aside beside the muted ones -- "(+3
        muted, +4 flaking)"; still no href and no link."""
        body = _function_body(read("watch.js"), "function buildStat(")
        self.assertIn("function buildStat(label, value, muted, flaking)", body)
        self.assertNotIn("href", _strip_comments(body))
        self.assertNotIn("<a", _strip_comments(body))
        self.assertNotIn("createElement(\"a\")", _strip_comments(body))
    # No dedicated no-innerHTML check here — WatchPageTest already pins
    # that invariant for the whole file (correctly, via _strip_comments
    # — this file's own module docstring literally contains the word
    # "innerHTML" while explaining why there is none).


class OpenActionsSummaryScopeTest(unittest.TestCase):
    """actions.js's /api/summary fetch must carry the product scope.

    The rows (listUrl) carried product= from WP-20 day one; the summary
    fetch feeding the ENVIRONMENT FILTER and assignee list did not, so
    a product-scoped page offered every product's environments — found
    by the user twice on this page (2026-08-09). The server-side
    catalog-scoping fix (0a42855) only helps callers who send the
    param; this pins the sending.
    """

    def test_summary_is_fetched_through_the_scoped_helper(self) -> None:
        """WP-24: the appendProduct(qs) helper is gone -- the helper
        now calls apiUrl() with a product scope from
        selectedProductScope() (getSelectedProduct() || null, so it is
        OMITTED rather than sent empty when nothing is selected -- the
        same behaviour appendProduct()'s `if (product)` guard gave).
        Same assertion intent (the summary fetch carries the product
        scope), now checking the apiUrl() call site.

        WP-44: the page's one summary request is the names part, and its
        helper is ``namesUrl()`` (was ``summaryUrl()``); widened to that
        helper and to the relative spelling of a bare fetch, every scope
        assertion kept."""
        src = read("actions.js")
        self.assertIn("function namesUrl()", src)
        self.assertIn("fetchJson(namesUrl())", src)
        for bare in ('fetchJson("/api/summary")', 'fetchJson("api/summary")'):
            self.assertNotIn(bare, src,
                             "a bare unscoped summary fetch is the bug "
                             "coming back")
        body = _function_body(src, "function namesUrl()")
        self.assertIn("apiUrl(", body)
        self.assertIn("selectedProductScope()", body)


def _unsliced_summary_requests(source: str) -> List[int]:
    """Line numbers where comment-stripped *source* names ``api/summary``
    in a statement that does not ask for the names part.

    Each ``api/summary`` is read to the end of its statement (the next
    ``;``), which must carry ``parts: "names"`` (the apiUrl() params
    spelling) or ``parts=names`` (a hand-built query string)."""
    code = _strip_comments(source)
    hits = []  # type: List[int]
    for match in re.finditer(r"api/summary", code):
        end = code.find(";", match.end())
        statement = code[match.start():end if end != -1 else len(code)]
        if not re.search(r'parts:\s*"names"|parts=names\b', statement):
            hits.append(code.count("\n", 0, match.start()) + 1)
    return hits


class OpenActionsCatalogueTest(unittest.TestCase):
    """WP-44: Open Actions takes its catalogue from the names part.

    The page reads five fields of the summary (environments, assignees,
    products, assignment streams, stale_before) and used to wait for the
    whole unsliced payload, every headline number and queue of the
    board, on its first paint. ``GET /api/summary?parts=names`` returns
    those five and nothing else. Reverting to the full summary still
    WORKS, it is only slow again, so it is pinned here the way
    ``SummaryPartsFetchTest`` pins the home page's split.

    First half: the names request. Second half: the Failing mode
    sends FAIL and FLAKING, and the footer line says what the list
    leaves out where a flaky mark exists.
    """

    def test_every_summary_request_asks_for_the_names_part(self) -> None:
        src = read("actions.js")
        self.assertIn("api/summary", _strip_comments(src),
                      "nothing to check: the page names no summary request")
        self.assertEqual(
            _unsliced_summary_requests(src), [],
            "actions.js requests /api/summary without parts=names: the "
            "Open Actions first paint waits on the whole board again")
        self.assertIn('parts: "names"',
                      _function_body(src, "function namesUrl()"))

    def test_the_detector_catches_a_full_summary_request(self) -> None:
        """Planted: the shipped helper with its part dropped, a bare
        fetch, another part, and a hand-built query string."""
        src = read("actions.js")
        self.assertIn('{ parts: "names" }', src)
        dropped = src.replace('{ parts: "names" }', "{}")
        self.assertNotEqual(_unsliced_summary_requests(dropped), [])
        for planted in (
            'const s = await fetchJson("api/summary");\n',
            'const u = apiUrl("api/summary", { parts: "headline" }, {});\n',
            'fetchJson("api/summary?product=" + p);\n',
            'fetchJson("api/summary?parts=namesake");\n',
        ):
            with self.subTest(planted=planted):
                self.assertEqual(
                    _unsliced_summary_requests(src + planted),
                    [len(_strip_comments(src + planted).split("\n")) - 1])
        self.assertEqual(_unsliced_summary_requests(
            'fetchJson("api/summary?parts=names&product=" + p);\n'), [])

    def test_the_catalogue_gate_is_a_loaded_flag(self) -> None:
        """With nobody assigned, ``state.assignees.length`` stayed 0 and
        every refresh refetched the catalogue. The flag is set where the
        answer is applied, after the stale-answer check, and nowhere
        else, so a failed or superseded request leaves it unset."""
        code = _strip_comments(read("actions.js"))
        body = _function_body(code, "async function refresh(")
        self.assertNotIn("assignees.length", body)
        self.assertIn("catalogueLoaded: false,", code)
        # The flag alone: an append asks again while it is unset, or its
        # clearError() hides the failure behind empty filters.
        self.assertRegex(body, r"state\.catalogueLoaded\s*\?")
        self.assertNotIn("catalogueLoaded || append", body)
        self.assertEqual(code.count("state.catalogueLoaded = true"), 1)
        self.assertNotIn("catalogueLoaded = false", code)
        self.assertLess(body.index("seq !== state.seq"),
                        body.index("state.catalogueLoaded = true"))

    def test_first_paint_is_still_names_and_rows_then_expiring(self) -> None:
        """Two awaited requests started together, names first, then the
        expiring mutes, which nothing awaits (WP-40); nothing added."""
        body = _function_body(
            _strip_comments(read("actions.js")), "async function refresh(")
        order = [body.index(token) for token in (
            "Promise.all([", "fetchJson(namesUrl())",
            "fetchJson(listUrl(offset))", "loadExpiring();",
            "await requests")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(body.count("fetchJson("), 2)
        self.assertEqual(body.count("await "), 1)
        self.assertNotIn("await loadExpiring", body)

    def test_a_failed_catalogue_still_draws_the_rows(self) -> None:
        """The names request's own failure is caught beside it, so the
        rows still draw; its message is shown after them."""
        body = _function_body(
            _strip_comments(read("actions.js")), "async function refresh(")
        self.assertIn("fetchJson(namesUrl()).catch(", body)
        self.assertLess(body.index("renderRows(page.tests, append)"),
                        body.index("showError(namesError.message)"))

    # ---- second half: the Failing mode sends FAIL and FLAKING; the
    # footer line. ----

    def test_the_failing_mode_sends_fail_and_flaking(self) -> None:
        """FAIL alone now leaves out every row reading FLAKING, an
        assigned one included (design §2 B.3(b)); the two together are
        every failing test. The values come from api.js, the one place
        FLAKING is spelled, and are shared by the rows and bulk assign."""
        body = _strip_comments(_function_body(
            read("actions.js"), "function filterParams("))
        self.assertIn('const failing = state.result === "FAIL";', body)
        self.assertRegex(body, r"const results = open \? null\s*: \(failing "
                               r"\? FAIL_AND_FLAKING : \[state\.result\]\);")
        self.assertIn('open: open || failing ? "1" : null', body)
        api = _strip_comments(read("api.js"))
        self.assertIn('export const FAIL_AND_FLAKING = ["FAIL", '
                      'FLAKING_FILTER];', api)
        self.assertIn('export const FLAKING_FILTER = displayResult("FAIL", '
                      '"flaking", false);', api)
        for caller in ("function listUrl(", "function bulkUrl("):
            self.assertIn("filterParams()", _function_body(
                read("actions.js"), caller), caller)

    def test_the_failing_mode_detector_can_fail(self) -> None:
        """Planted: Failing sending FAIL alone, the regression that drops
        an assigned flaking test from the mode."""
        body = _strip_comments(_function_body(
            read("actions.js"), "function filterParams("))
        planted = body.replace("(failing ? FAIL_AND_FLAKING : "
                               "[state.result])", "[state.result]")
        self.assertNotEqual(planted, body)
        self.assertIsNone(re.search(r"\? FAIL_AND_FLAKING :", planted))

    def test_the_footer_line_shows_only_where_a_mark_exists(self) -> None:
        html = read("actions.html")
        footer = html[html.index('<div class="browse-footer">'):]
        footer = footer[:footer.index("</section>")]
        self.assertIn('<p id="actions-flaky-note" class="muted cap-note" '
                      'hidden></p>', footer)
        self.assertLess(footer.index('id="actions-count"'),
                        footer.index('id="actions-flaky-note"'))
        body = _strip_comments(_function_body(
            read("actions.js"), "async function refresh("))
        self.assertIn("flakyNote.hidden = !names.flaky_marks;", body)
        self.assertIn("? OPEN_ACTIONS_FLAKING_NOTE", body)
        self.assertNotIn("innerHTML", body)

    # NODE is defined further down this module; the decorator runs here.
    @unittest.skipIf(shutil.which("node") is None, "node is not on PATH: "
                     "the DOM-shim walks are optional dev tooling, the "
                     "source guards still run")
    def test_each_mode_and_the_footer_over_the_shim(self) -> None:
        """The real actions.js: the rows request of each mode, and the
        footer under the count only where ``flaky_marks`` is true (an
        older server sends no such field)."""
        for marks in (True, False, None):
            with self.subTest(flaky_marks=marks):
                answer = _actions_modes_walk(marks)
                self.assertEqual(answer["modes"], {
                    "open": [["open", "1"]],
                    "FAIL": [["result", "FAIL"], ["result", "FLAKING"],
                             ["open", "1"]],
                    "UNEXPECTED_PASS": [["result", "UNEXPECTED_PASS"]],
                })
                if marks:
                    self.assertEqual(answer["note"], [
                        False, "Unassigned tests that are flaking are left "
                        "out until they fail past their cadence."])
                else:
                    self.assertEqual(answer["note"], [True, ""])


class OpenActionsOriginFilterTest(unittest.TestCase):
    """Open Actions' build/mainline origin filter and per-row tag
    (WP-21, docs/STREAMS_PLAN.md §3.6, found in first human use; the
    non-mainline value was spelled "branch" until WP-25 collapsed the
    stream kinds — the 2026-08-09 sanity net caught this filter still
    speaking the dead spelling AFTER the collapse, hence the chip-value
    pin below)."""

    def test_the_filter_is_server_side_not_a_client_reshuffle(self) -> None:
        """The same rule SortingTest pins for this page's sort: a paged
        table filtered in the browser shows "build items that happen
        to be on this page", not every build item.

        WP-24: `qs.append("origin", state.origin)` became `origin:
        state.origin` in the params object passed to apiUrl() -- same
        intent, the origin filter still travels on every request.
        WP-26: that params object moved from listUrl() into the shared
        filterParams() (also used by the bulk assign/unassign
        control's own request) -- checked at its new home; listUrl()
        itself now only composes filterParams()'s result in."""
        body = _function_body(read("actions.js"), "function filterParams(")
        self.assertIn("origin: state.origin", body)

    def test_the_filter_hides_with_no_stream_originated_assignments(
        self
    ) -> None:
        body = _function_body(
            read("actions.js"), "function renderOriginFilter(")
        self.assertIn("state.assignmentStreams.length === 0", body)
        self.assertIn("container.hidden = true", body)

    def test_the_chips_speak_the_one_kind_dialect(self) -> None:
        """The 2026-08-09 sanity-net finding, pinned: after WP-25 there
        is no 'branch' anywhere — the chip's wire value is "build", its
        label "Build-originated", and the dead spelling may not appear
        in the filter at all (a chip value the server 400s would be a
        filter that silently breaks)."""
        body = _function_body(
            read("actions.js"), "function renderOriginFilter(")
        self.assertIn('{ value: "build", label: "Build-originated" }',
                      body)
        stripped = _strip_comments(body)
        self.assertNotIn('"branch"', stripped)
        self.assertNotIn("Branch-originated", stripped)

    def test_the_mount_ships_hidden_in_the_markup(self) -> None:
        html = read_text("actions.html")
        mount_at = html.index('id="origin-filters"')
        self.assertIn("hidden", html[mount_at:mount_at + 120])

    def test_the_row_tag_is_resolved_from_the_pages_own_streams_map(
        self
    ) -> None:
        """The same batched-resolution shape the comments endpoint
        uses — never a lookup per row."""
        body = _function_body(read("actions.js"), "function buildOwnerCell(")
        self.assertIn("state.streams[String(row.assignment_stream_id)]",
                       body)

    def test_a_null_assignment_stream_id_gets_no_tag(self) -> None:
        body = _function_body(read("actions.js"), "function buildOwnerCell(")
        self.assertIn("row.assignment_stream_id === null", body)

    def test_summary_and_page_streams_are_both_read(self) -> None:
        """WP-44: the summary's answer is the names part now, held as
        ``names`` (was ``summary``); the same field is read from it."""
        code = _strip_comments(read("actions.js"))
        self.assertIn("names.assignment_streams", code)
        self.assertIn("page.streams", code)

    def test_a_build_originated_row_links_to_the_streams_own_test_page(
        self
    ) -> None:
        """F1: a row whose CURRENT assignment carries a non-mainline
        origin must land on that stream's own test page — landing on
        mainline's view of "why is this broken in the RC" is the exact
        ambiguity the origin tag beside this link already warns about.
        Resolved from the same batched state.streams map the tag uses,
        never a lookup per row.

        WP-24: the params.append() pair became one pageUrl() call whose
        `linkScope` argument names the origin stream's own id/product —
        same intent, checked via the linkScope assignment instead of
        the params.append() calls that no longer exist."""
        body = _function_body(read("actions.js"), "function buildRow(")
        self.assertIn(
            "row.assignment_stream_id === null", body)
        self.assertIn(
            "state.streams[String(row.assignment_stream_id)]", body)
        self.assertIn("stream: originStream.id,", body)
        self.assertIn('product: originStream.product || ""', body)
        self.assertIn('link.href = pageUrl("test"', body)

    def test_a_mainline_originated_row_link_is_unchanged(self) -> None:
        """A null assignment_stream_id must not carry a stream/product
        scope — the ordinary test.html link every row had before this
        feature. WP-24: `if (originStream) { params.append(...) }`
        became `if (originStream) { linkScope = {...} }`, still gated
        by the same truthy check."""
        body = _strip_comments(
            _function_body(read("actions.js"), "function buildRow(")
        )
        self.assertIn("if (originStream) {", body)
        self.assertIn(
            "let linkScope = { stream: null, product: null, "
            "baseline: null };", body)


class OpenActionsTruthfulResultTest(unittest.TestCase):
    """ADDENDUM to the perf round: a row whose assignment origin is a
    non-mainline stream must not show ONLY mainline's result chip — an
    "assigned from the RC" row reading PASS while the RC failure it
    represents is live was a contradiction on its face
    (docs/STREAMS_PLAN.md §5.4). Reuses the SAME compare-strip visual
    language test.js's own compare strip already uses (ghost mainline
    chip, solid origin-stream chip, "no result" text rather than a
    colour for absence) — never a new visual vocabulary."""

    def test_renderCompareStrip_is_imported_from_compare_js(self) -> None:
        code = _strip_comments(read("actions.js"))
        self.assertIn(
            'import { renderCompareStrip } from "./compare.js";', code)

    def test_a_non_mainline_origin_row_uses_the_compare_strip(self) -> None:
        body = _strip_comments(
            _function_body(read("actions.js"), "function buildRow("))
        self.assertIn("if (originStream) {", body)
        strip_at = body.index("if (originStream) {")
        self.assertIn(
            "renderCompareStrip(resultCell, originStream, row.result,",
            body[strip_at:],
        )
        self.assertIn("row.origin_result)", body[strip_at:])

    def test_a_mainline_origin_row_keeps_the_plain_single_chip(
        self
    ) -> None:
        """Zero visible change for a mainline-origin row: the plain
        resultChip() path, not the compare strip.

        Widened for WP-44 item 7: the plain chip draws the row's one
        displayed result (rowResult(row), FLAKING for an assigned test
        under its cadence), the value the row edge takes too; the
        compare strip above still gets the raw `row.result`."""
        body = _strip_comments(
            _function_body(read("actions.js"), "function buildRow("))
        self.assertIn("} else {", body)
        else_at = body.index("} else {", body.index("if (originStream) {"))
        self.assertIn("resultChip(shown)", body[else_at:])
        self.assertIn("const shown = rowResult(row);", body)
        self.assertLess(body.index("const shown = rowResult(row);"),
                        body.index("if (originStream) {"))

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("actions.js")))


class ProductColumnTest(unittest.TestCase):
    """The Product column on the browse/triage tables (WP-20 §2.3).

    Shown exactly when the page SPANS products; when scoped to one, the
    footer states it instead — "a column of identical values is noise",
    an established finding this project has made before, so the exact
    wording is pinned rather than merely "some note exists".
    """

    #: (page script, table body id) — the two tables that gain the column.
    _TABLES = (("app.js", "dashboard-body"), ("actions.js", "actions-body"))

    def test_the_scoped_footer_wording_is_exact(self) -> None:
        for name, _ in self._TABLES:
            code = _strip_comments(read(name))
            self.assertIn(
                "— no product column needed.", code,
                name + " is missing the established footer wording")

    def test_the_column_is_hidden_below_two_products(self) -> None:
        for name, _ in self._TABLES:
            body = _function_body(read(name), "function updateProductColumn()")
            self.assertIn("products.length >= 2", body, name)

    def test_product_is_appended_to_the_list_request_when_selected(
        self
    ) -> None:
        """WP-24: the per-file `appendProduct(qs)` helper (each ending
        in `qs.append("product", ...)`) is gone from both pages —
        product scope now flows through apiUrl()'s `scope` argument,
        built from getSelectedProduct() and normalised to null (so
        apiUrl omits it) rather than "" when nothing is selected. Same
        assertion intent (the list request carries the selected product
        when there is one), now checking that each page's own list URL
        builder actually calls apiUrl() with a product scope derived
        from getSelectedProduct()."""
        for name, _ in self._TABLES:
            code = _strip_comments(read(name))
            self.assertIn("getSelectedProduct() || null", code, name)
            self.assertIn("apiUrl(", code, name)
            self.assertIn(
                "apiUrl", _imported_names(read(name)),
                name + " calls apiUrl() without importing it")

    def test_getSelectedProduct_is_imported_where_used(self) -> None:
        for name, _ in self._TABLES:
            code = read(name)
            self.assertIn(
                "getSelectedProduct", _imported_names(code),
                name + " calls getSelectedProduct() without importing it")

    def test_new_rows_start_with_the_cell_hidden(self) -> None:
        """A row built before the current scope is known must default to
        hidden — updateProductColumn() corrects it once data lands,
        never the other way around (a flash of a column that then hides
        would be worse than a brief absence)."""
        for name, body_id in self._TABLES:
            code = _strip_comments(read(name))
            self.assertIn('"product-col"', code, name)
            self.assertIn("productCell.hidden = true", code, name)


class BulkAssignUnmappedTest(unittest.TestCase):
    """The upgrade-day bulk "assign every unmapped environment to
    product X" action (WP-23 addendum, docs/STREAMS_PLAN.md §2) —
    static/actions.html + static/actions.js. Explicit, one-time,
    never a standing default (see the decision recorded in
    docs/STREAMS_PLAN.md §2)."""

    def test_the_control_ships_hidden_in_the_markup(self) -> None:
        html = read_text("actions.html")
        mount_at = html.index('id="envs-bulk-assign"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])

    def test_the_control_lives_inside_the_envs_details_section(
        self
    ) -> None:
        """Loaded lazily with the rest of the environment-expectations
        section, not a separate always-fetched control."""
        html = read_text("actions.html")
        details_at = html.index('id="envs-details"')
        bulk_at = html.index('id="envs-bulk-assign"')
        table_at = html.index('id="envs-table"')
        self.assertLess(details_at, bulk_at)
        self.assertLess(bulk_at, table_at)

    def test_load_envs_refreshes_the_bulk_control_from_the_same_fetch(
        self
    ) -> None:
        """No second request for the unmapped list — it is
        data.environments, already in memory from the one fetch
        loadEnvs() already makes."""
        body = _function_body(read("actions.js"), "async function loadEnvs(")
        self.assertIn("updateBulkAssignControl(data.environments)", body)

    def test_the_control_filters_by_unmapped_product(self) -> None:
        body = _function_body(
            read("actions.js"), "function updateBulkAssignControl(")
        self.assertIn("!item.product", body)

    def test_the_control_hides_when_nothing_is_unmapped(self) -> None:
        """Zero visible change: an estate with every environment already
        declared (or none using products at all) never shows this."""
        body = _function_body(
            read("actions.js"), "function updateBulkAssignControl(")
        self.assertIn("unmappedEnvs.length === 0", body)

    def test_bulk_assign_reuses_the_existing_per_environment_endpoint(
        self
    ) -> None:
        """No new API surface (the addendum's explicit instruction):
        the SAME PUT saveEnvProduct already issues, once per unmapped
        environment."""
        body = _function_body(
            read("actions.js"), "async function bulkAssignUnmapped(")
        self.assertIn('"api/environments/"', body)
        self.assertIn('"/product"', body)
        self.assertIn("for (const item of unmappedEnvs)", body)

    def test_a_partial_failure_still_refreshes_the_table(self) -> None:
        """A failure partway through the loop (env 3 of 5, say) still
        committed 1 and 2 server-side — the table/count must not keep
        showing the pre-click unmapped count against what the server
        actually holds now. loadEnvs() belongs in a finally, not only
        the success path."""
        body = _function_body(
            read("actions.js"), "async function bulkAssignUnmapped(")
        finally_at = body.index("} finally {")
        self.assertIn("await loadEnvs()", body[finally_at:])

    def test_bulk_assign_requires_a_typed_product_name(self) -> None:
        """The confirm-before-do gate: a required, validated input —
        the same idiom review.js's retirement reason box uses — not a
        browser confirm() dialog."""
        body = _function_body(
            read("actions.js"), "async function bulkAssignUnmapped(")
        self.assertIn("if (!product)", body)

    def test_bulk_assign_requires_a_username(self) -> None:
        body = _function_body(
            read("actions.js"), "async function bulkAssignUnmapped(")
        self.assertIn("requireUsername()", body)

    def test_no_confirm_dialog_is_introduced(self) -> None:
        """Precedent check, pinned: nothing in this codebase uses
        window.confirm() anywhere, so this feature must not be the
        first — the required-input gate above is the established
        substitute."""
        self.assertNotIn("confirm(", _strip_comments(read("actions.js")))


class OpenActionsBulkAssignmentsTest(unittest.TestCase):
    """The bulk assign/unassign control on Open Actions (2026-08-10,
    found while cleaning up assignments a dead build left behind) —
    static/actions.html + static/actions.js. Follows the SAME
    required-explicit-input gate this file's bulk product-assign
    control (BulkAssignUnmappedTest, envs-bulk-assign) already uses —
    never window.confirm(), which nothing in this codebase uses
    anywhere.
    """

    def test_the_control_ships_hidden_in_the_markup(self) -> None:
        html = read_text("actions.html")
        mount_at = html.index('id="bulk-actions"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])

    def test_the_control_lives_inside_the_actions_section(self) -> None:
        html = read_text("actions.html")
        section_at = html.index('id="actions-section"')
        bulk_at = html.index('id="bulk-actions"')
        table_at = html.index('id="actions-table"')
        self.assertLess(section_at, bulk_at)
        self.assertLess(bulk_at, table_at)

    def test_updateBulkActionsControl_hides_on_zero_total(self) -> None:
        body = _function_body(
            read("actions.js"), "function updateBulkActionsControl(")
        self.assertIn("state.total === 0", body)
        self.assertIn('container.hidden = state.total === 0', body)

    def test_counts_come_from_state_total(self) -> None:
        """The exact same total the page header already shows for the
        current filters — not a second count derived independently."""
        body = _function_body(
            read("actions.js"), "function updateBulkActionsControl(")
        self.assertIn("state.total.toLocaleString()", body)
        self.assertIn('"bulk-actions-count"', body)
        self.assertIn('"bulk-unassign-count"', body)

    def test_the_assign_gate_is_a_required_username_input(self) -> None:
        """Assign's analogue of bulkAssignUnmapped()'s required
        product field — disabled until a name is present, checked both
        at render time and on every keystroke (never only in the click
        handler, which would let a stale disabled state linger)."""
        render_body = _function_body(
            read("actions.js"), "function updateBulkActionsControl(")
        self.assertIn('"bulk-assign-btn"', render_body)
        self.assertIn('.disabled = !username', render_body)
        click_body = _function_body(
            read("actions.js"), "async function bulkAssign(")
        self.assertIn("if (!username)", click_body)

    def test_the_unassign_gate_is_requireUsername_only(self) -> None:
        """No second input to validate — the gate is who is doing this
        (requireUsername()) plus the stated count and the hidden-at-
        zero rule pinned above."""
        body = _function_body(
            read("actions.js"), "async function bulkUnassign(")
        self.assertIn("requireUsername()", body)

    def test_bulk_assign_requires_a_username(self) -> None:
        body = _function_body(
            read("actions.js"), "async function bulkAssign(")
        self.assertIn("requireUsername()", body)

    def test_bulkUrl_composes_through_apiUrl(self) -> None:
        """WP-24: no hand-built URL anywhere — this is the one place a
        new endpoint's request could have reintroduced one."""
        body = _function_body(read("actions.js"), "function bulkUrl(")
        self.assertIn('apiUrl("api/assignments/bulk"', body)

    def test_bulkUrl_shares_listUrls_filter_params(self) -> None:
        """WP-26: the SAME filterParams() listUrl() uses — never a
        second hand-rolled copy of the current filters that can drift
        from what the page's own row fetch sends."""
        body = _function_body(read("actions.js"), "function bulkUrl(")
        self.assertIn("filterParams()", body)

    def test_bulk_requests_use_postJson(self) -> None:
        assign_body = _function_body(
            read("actions.js"), "async function bulkAssign(")
        self.assertIn("postJson(bulkUrl()", assign_body)
        unassign_body = _function_body(
            read("actions.js"), "async function bulkUnassign(")
        self.assertIn("postJson(bulkUrl()", unassign_body)

    def test_postJson_is_imported(self) -> None:
        self.assertIn("postJson", _imported_names(read("actions.js")))

    def test_bulk_unassign_sends_a_null_username(self) -> None:
        body = _function_body(
            read("actions.js"), "async function bulkUnassign(")
        self.assertIn("username: null", body)

    def test_neither_request_ever_sends_a_stream_id(self) -> None:
        """Open Actions is unscoped (docs/STREAMS_PLAN.md §0.4): a bulk
        action never records an assignment origin, so the body built
        for either operation must not carry stream_id at all — the
        server has no field to receive it either."""
        assign_body = _strip_comments(
            _function_body(read("actions.js"), "async function bulkAssign("))
        unassign_body = _strip_comments(
            _function_body(
                read("actions.js"), "async function bulkUnassign("))
        self.assertNotIn("stream_id", assign_body)
        self.assertNotIn("stream_id", unassign_body)

    def test_both_operations_refresh_on_success(self) -> None:
        for fn in ("async function bulkAssign(",
                   "async function bulkUnassign("):
            body = _function_body(read("actions.js"), fn)
            self.assertIn("await refresh(false)", body, fn)

    def test_comment_is_only_sent_when_present(self) -> None:
        """Optional field: bulkAssign's body only gains "comment" when
        the input actually holds text, matching the API's own "if
        non-empty, post it" reading."""
        body = _function_body(
            read("actions.js"), "async function bulkAssign(")
        self.assertIn("if (comment) {", body)
        self.assertIn("body.comment = comment", body)

    def test_no_confirm_dialog_is_introduced(self) -> None:
        self.assertNotIn("confirm(", _strip_comments(read("actions.js")))


class TimeAndTimelineProductTest(unittest.TestCase):
    """docs/STREAMS_PLAN.md §2.3: "Time/Timeline pages: product scoping
    only via the existing environment filter semantics" — Time appends
    product= to its own request (the server resolves it); Timeline is
    inherently single-environment, so it scopes its ENVIRONMENT PICKER
    instead of adding a request parameter that endpoint does not read
    from this page's shape.
    """

    def test_time_appends_product_to_its_request(self) -> None:
        """WP-24: `qs.append("product", product)` became `product:
        getSelectedProduct() || null` in the scope object passed to
        apiUrl() — same intent (a selected product is sent; nothing
        selected omits it, matching the old `if (product)` guard)."""
        code = _strip_comments(read("time.js"))
        self.assertIn("getSelectedProduct", _imported_names(read("time.js")))
        self.assertIn("product: getSelectedProduct() || null", code)

    def test_timeline_scopes_its_environment_picker(self) -> None:
        code = _strip_comments(read("timeline.js"))
        self.assertIn(
            "getSelectedProduct", _imported_names(read("timeline.js")))
        body = _function_body(read("timeline.js"), "async function loadEnvironments()")
        self.assertIn("item.product === product", body)

    def test_timeline_falls_back_when_the_product_has_no_environments(
        self
    ) -> None:
        """A stale/renamed product selection must not empty the picker."""
        body = _function_body(
            read("timeline.js"), "async function loadEnvironments()")
        self.assertIn("scoped.length ? scoped : data.environments", body)


class TimeAndTimelineStreamScopingTest(unittest.TestCase):
    """F7: time.js and timeline.js never read ?stream= from their own
    URL nor forwarded it to their APIs — WP-23 added `stream=` to
    `/api/time`/`/api/timeline` server-side, but the PAGES could not
    use it, so a branch's own time/timeline was unreachable from the
    UI (docs/STREAMS_PLAN.md §5.2 "as built")."""

    def test_both_pages_read_stream_from_the_url(self) -> None:
        for name, signature in (
            ("time.js", "function init()"),
            ("timeline.js", "async function init()"),
        ):
            body = _function_body(read(name), signature)
            self.assertIn('params.get("stream")', body, name)
            self.assertIn("state.streamId", body, name)

    def test_time_forwards_stream_to_its_request(self) -> None:
        """WP-24: `qs.append("stream", ...)` became an explicit `stream:
        state.streamId` scope override on the apiUrl() call."""
        body = _function_body(read("time.js"), "function url()")
        self.assertIn("apiUrl(", body)
        self.assertIn("stream: state.streamId", body)

    def test_timeline_forwards_stream_to_every_one_of_its_requests(
        self
    ) -> None:
        """Not just the top-level page load: the row-expansion fetch
        (runsUrl) and the test-search suggestions fetch
        (fetchSearchMatches) both hit different endpoints and would
        otherwise silently read MAINLINE's data while the page itself
        is scoped to a branch.

        WP-24: each `qs.append("stream", ...)` became an explicit
        `stream: state.streamId` scope override on an apiUrl() call --
        same intent, checked per call site."""
        code = read("timeline.js")
        for signature in (
            "function timelineUrl()",
            "function runsUrl(",
            "async function fetchSearchMatches(",
        ):
            body = _function_body(code, signature)
            self.assertIn("apiUrl(", body, signature)
            self.assertIn("stream: state.streamId", body, signature)

    def test_both_pages_render_the_branch_band_when_scoped(self) -> None:
        for name in ("time.js", "timeline.js"):
            code = _strip_comments(read(name))
            self.assertIn(
                "renderBranchBand", _imported_names(read(name)), name)
            self.assertIn("renderBranchBand(data.stream_identity)", code,
                           name)
            # Same guard test.js's own call site uses: never call it on
            # a mainline load, even if the server ever sent identity
            # data unprompted.
            self.assertIn("state.streamId !== null && data.stream_identity",
                           code, name)

    def test_both_pages_carry_a_branch_band_mount_point(self) -> None:
        for name in ("time.html", "timeline.html"):
            html = read_text(name)
            mount_at = html.index('id="branch-band"')
            self.assertIn("hidden", html[mount_at:mount_at + 60], name)

    def test_timeline_preserves_stream_across_a_replaced_url(self) -> None:
        """syncUrl() rewrites the address bar on every block/day
        change — an omission here would silently drop the scope from
        a reload or a copied link even though in-memory state still
        had it right for the NEXT fetch.

        WP-24: syncUrl() rebuilds the query string through pageUrl()
        (same shape timelineUrl() already sends the server) rather than
        mutating searchParams in place -- same intent, checking the
        `stream: state.streamId` override passed to it."""
        body = _function_body(read("timeline.js"), "function syncUrl()")
        self.assertIn("pageUrl(", body)
        self.assertIn("stream: state.streamId", body)


class StreamEnvironmentEmptyStateTest(unittest.TestCase):
    """WP-25 (docs/ONE_KIND_PLAN.md §2b.1, user-reported 2026-08-09): a
    build that ran on one environment showed a bare empty page on every
    OTHER environment. `stream_environments` (present only when scoped
    and genuinely empty) is rendered as links that switch only the
    environment param, or "no runs anywhere" when the list is empty."""

    def test_the_shared_renderer_handles_both_cases(self) -> None:
        body = _function_body(
            read("compare.js"),
            "export function renderStreamEnvironmentHint(")
        self.assertIn("!environments.length", body)
        self.assertIn("This stream has no runs on any environment", body)
        self.assertIn("createElement(\"a\")", body)
        self.assertIn("link.href = linkFor(environment)", body)

    def test_the_shared_renderer_uses_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("compare.js")))

    def test_both_pages_import_the_shared_renderer(self) -> None:
        for name in ("time.js", "timeline.js"):
            self.assertIn(
                "renderStreamEnvironmentHint", _imported_names(read(name)),
                name)

    def test_both_pages_render_it_only_when_the_field_is_present(
        self
    ) -> None:
        for name, signature in (
            ("time.js", "function render(data)"),
            ("timeline.js", "function render(data)"),
        ):
            body = _function_body(read(name), signature)
            self.assertIn("data.stream_environments", body, name)
            self.assertIn("renderStreamEnvironmentHint(", body, name)

    def test_both_link_builders_switch_only_the_environment_param(
        self
    ) -> None:
        """`stream`/`product` carry through; `environment` is the only
        param that changes -- a reader must never lose their scope by
        following the hint.

        WP-24: both builders are now one pageUrl("time"/"timeline",
        {environment: ...}, {stream: ..., product: ..., baseline:
        null}) call each -- same intent (environment is the identity
        param that changes; stream/product are explicit overrides
        carrying the page's own values, not pageUrl()'s default
        carriage, since a hint link is built from a DIFFERENT
        environment than the one currently in the URL)."""
        for name in ("time.js", "timeline.js"):
            body = _function_body(read(name), "function environmentSwitchUrl(")
            self.assertIn("{ environment: environment }", body)
            self.assertIn("stream: state.streamId", body)
            self.assertIn("getSelectedProduct()", body)

    def test_time_link_builder_drops_the_script_drill(self) -> None:
        """A script from the environment being LEFT may not exist under
        the new one -- the link must not carry a stale/dead combination
        forward."""
        body = _function_body(read("time.js"), "function environmentSwitchUrl(")
        self.assertNotIn("state.script", body)

    def test_timeline_link_builder_drops_the_chosen_window(self) -> None:
        """A window chosen for the old environment has no meaning for
        the new one -- the link must land on its own newest block."""
        body = _function_body(
            read("timeline.js"), "function environmentSwitchUrl(")
        self.assertNotIn("state.days", body)
        self.assertNotIn("state.from", body)
        self.assertNotIn("state.to", body)


class BranchQuickLinksTest(unittest.TestCase):
    """F6: the branch dashboard's "Its own results" tab links to that
    SAME branch's own Time and Timeline pages (needs F7,
    docs/STREAMS_PLAN.md §5.2 "as built")."""

    def test_the_mount_ships_hidden_in_the_markup(self) -> None:
        html = read_text("index.html")
        mount_at = html.index('id="branch-quick-links"')
        self.assertIn("hidden", html[mount_at:mount_at + 60])

    def test_both_links_carry_the_streams_own_id(self) -> None:
        """WP-24: the hand-rolled URLSearchParams/"time.html?"/
        "timeline.html?" trio became two pageUrl() calls with an
        explicit `stream: streamId` scope override each -- same
        assertion intent (both links name the branch's own stream id),
        now checking the pageUrl() call sites."""
        body = _function_body(
            read("app.js"), "function renderBranchQuickLinks(")
        self.assertIn('timeLink.href = pageUrl("time"', body)
        self.assertIn('timelineLink.href = pageUrl("timeline"', body)
        self.assertEqual(body.count("stream: streamId"), 2)

    def test_a_null_stream_id_hides_the_mount(self) -> None:
        """The own-results tab's own concept — the diff tab and a
        mainline load must never show it."""
        body = _function_body(
            read("app.js"), "function renderBranchQuickLinks(")
        null_at = body.index("streamId === null")
        self.assertIn("mount.hidden = true", body[null_at:null_at + 60])

    def test_the_diff_tab_explicitly_hides_the_links(self) -> None:
        body = _function_body(read("app.js"), "function activateDiffTab(")
        self.assertIn("renderBranchQuickLinks(null)", body)

    def test_own_results_activation_renders_the_links(self) -> None:
        body = _function_body(
            read("app.js"), "function activateOwnResultsTab()")
        self.assertIn("renderBranchQuickLinks(state.streamId)", body)

    def test_changing_the_environment_filter_updates_the_links(
        self
    ) -> None:
        """The Timeline link names the CURRENT environment filter —
        stale after a filter change would point at the wrong one."""
        body = _function_body(read("app.js"), "function setEnvironment(")
        self.assertIn("renderBranchQuickLinks(state.streamId)", body)

    def test_the_timeline_link_only_names_an_environment_when_one_is_set(
        self
    ) -> None:
        """No environment filter set -- the Timeline link must still
        work (that page picks a sensible default itself), not send an
        empty/undefined environment param.

        WP-24: the explicit `if (state.environment) {...}` guard is
        gone -- `environment: state.environment` is passed straight to
        pageUrl()'s scope object, and urls.js's own encoding (pinned by
        UrlsModuleTest) omits both "" and null the same way the old
        guard did. Checking the pageUrl() call carries state.environment
        (never a hardcoded value) preserves the same intent: an unset
        filter must not send an empty/undefined environment param."""
        body = _function_body(
            read("app.js"), "function renderBranchQuickLinks(")
        timeline_at = body.index('timelineLink.href = pageUrl("timeline"')
        self.assertIn(
            "environment: state.environment",
            body[timeline_at:timeline_at + 200])

    def test_both_links_are_scope_self_sufficient_with_a_product_param(
        self
    ) -> None:
        """Advisor-caught regression: Time/Timeline both load products.js
        and adopt ?product= into localStorage (commit 4725bbc). Without
        this param these links silently reintroduce that exact bug --
        opened from a browser whose stored product differs from the
        branch's own, Timeline's environment picker (filtered by the
        stored product) can fail to even list the branch's environment."""
        body = _function_body(
            read("app.js"), "function renderBranchQuickLinks(")
        self.assertEqual(
            body.count('product: state.streamProduct || ""'), 2)

    def test_init_branch_dashboard_stashes_the_streams_product(self) -> None:
        """state.streamProduct must be set from the SAME fetchCompare
        payload the branch band already reads, before either tab can
        render -- a link built before this line ran would carry a stale
        or empty product regardless of the branch's real one."""
        body = _function_body(
            read("app.js"), "async function initBranchDashboard(")
        band_at = body.index("renderBranchBand(")
        stash_at = body.index("state.streamProduct = data.stream.product")
        self.assertGreater(
            stash_at, band_at,
            "stash reads the same payload renderBranchBand already used")
        self.assertLess(
            stash_at, body.index("tabs = document.getElementById"),
            "must be stashed before either tab (own/diff) can render")


class OwnResultsAlwaysReachableTest(unittest.TestCase):
    """WP-31 (2026-09-23, first real use with branch builds): a build's
    "Its own results" tab must ALWAYS be reachable from a stream-scoped
    dashboard load, whatever the build's cadence.

    WP-23 shipped both tabs unconditionally with the covered-pass
    threshold (OWN_RESULTS_DEFAULT_PASSES) choosing only the default;
    WP-25 made that same threshold gate the header's EXISTENCE, so a
    build with fewer than two covered passes was delta-only. In use
    that hid the one view that answers "what has actually run on this
    build?" -- the delta view lists differences only -- and the tester
    had no way through to the whole dashboard. These tests pin the
    WP-23 shape back in place: the threshold may pick the default, it
    may never hide the tab.
    """

    def body(self) -> str:
        return _strip_comments(_function_body(
            read("app.js"), "async function initBranchDashboard("))

    def test_the_tab_header_is_never_hidden_on_a_stream_scoped_load(
        self
    ) -> None:
        """The WP-25 gate was literally `tabs.hidden = true` inside this
        function. Comments are stripped first so the history note in
        the source cannot satisfy or trip the assertion."""
        body = self.body()
        self.assertNotIn("tabs.hidden = true", body)
        self.assertIn("tabs.hidden = false", body)
        self.assertNotIn("caption.hidden = true", body)

    def test_the_threshold_picks_only_the_default_tab(self) -> None:
        """Both outcomes of the threshold must end in selectBranchTab()
        with the header shown -- an early `return` before the tab
        wiring is the gate coming back under another name."""
        body = self.body()
        self.assertIn("OWN_RESULTS_DEFAULT_PASSES", body)
        wiring_at = body.index("tabs.hidden = false")
        threshold_at = body.index("coveredPasses >= OWN_RESULTS_DEFAULT_PASSES")
        self.assertLess(
            wiring_at, threshold_at,
            "the header is shown BEFORE the threshold is consulted, so "
            "no outcome of the threshold can leave it hidden")
        self.assertNotIn("return", body[wiring_at:],
                         "no early exit after the header is shown")
        self.assertIn('which = "own"', body)
        self.assertIn('which = "diff"', body)
        self.assertIn("selectBranchTab(which, streamId)", body)

    def test_every_caption_states_the_count_and_the_threshold(self) -> None:
        """docs/STREAMS_PLAN.md §5.2: the heuristic "must be stated in the
        UI caption, not buried" -- from data, never a silent constant.
        One shared `cadence` sentence carries both numbers and every
        branch of the default choice includes it."""
        body = self.body()
        cadence_at = body.index("const cadence =")
        cadence_line = body[cadence_at:body.index(";", cadence_at)]
        self.assertIn("coveredPasses", cadence_line)
        self.assertIn("OWN_RESULTS_DEFAULT_PASSES", cadence_line)
        self.assertEqual(
            body.count("+ cadence"), 3,
            "all three default outcomes (filters / meets / below) "
            "carry the cadence sentence")

    def test_a_url_carrying_browse_filters_opens_its_own_results(
        self
    ) -> None:
        """A Watch card's "N unassigned failures" click-through lands on
        index.html?stream=<id>&result=FAIL&unassigned=1 -- filters only
        the own-results BROWSE TABLE reads (BrowseFilterUrlInitTest).
        On the diff tab they were silently inert, so such a URL must
        open the own-results tab regardless of cadence."""
        src = read("app.js")
        helper = _strip_comments(_function_body(
            src, "function urlAsksForBrowseFilters("))
        self.assertIn('getAll("result")', helper)
        self.assertIn('get("unassigned") === "1"', helper)
        self.assertIn('get("stale") === "1"', helper)
        body = self.body()
        filters_at = body.index("urlAsksForBrowseFilters()")
        self.assertIn('which = "own"', body[filters_at:filters_at + 120])
        # The filter override is consulted BEFORE the cadence rule, so
        # it wins over a below-threshold build's "diff" default.
        self.assertLess(filters_at, body.index("meetsThreshold)"))

    def test_the_own_tab_reuses_the_mainline_body_unchanged(self) -> None:
        """The whole point: the own-results tab IS the home dashboard
        (status tiles, charts, triage, browse) scoped to the stream --
        activateOwnResultsTab() still wires the mainline controls and
        refreshes, and hides the delta section. Nothing new to render."""
        body = _function_body(
            read("app.js"), "function activateOwnResultsTab()")
        self.assertIn("wireMainlineControls()", body)
        self.assertIn("refreshAll()", body)
        self.assertIn('"delta-section").hidden = true', body)


class TimelineLiveRefreshTest(unittest.TestCase):
    """WP-32 (2026-09-24): results now arrive WHILE a run is going (a
    full run can take seven hours), so the Timeline gained a Refresh
    button and a Follow mode that re-reads the selected run on a timer.
    Front-end only -- the newest block already grows server-side as
    hours fill in (analytics.complete_passes trims at the OLD edge of
    the lookback only), so a re-fetch of "newest block" is the whole
    mechanism.

    What these pin is the shape that keeps it safe to leave on for
    hours: chained timeouts (never an interval that can stack requests),
    no fetch while the tab is hidden, a quiet failure path that never
    reaches the error banner, a fingerprint so an unchanged response
    touches nothing, open rows restored after a re-render, and Follow
    confined to the newest run.
    """

    def js(self) -> str:
        return _strip_comments(read("timeline.js"))

    def test_the_controls_ship_in_the_markup(self) -> None:
        html = read_text("timeline.html")
        self.assertIn('id="timeline-refresh"', html)
        follow_at = html.index('id="timeline-follow"')
        self.assertIn('aria-pressed="false"', html[follow_at:follow_at + 120])
        self.assertIn('id="timeline-refreshed"', html)
        for page in ("timeline.html",):
            self.assertEqual(read_text(page).count('id="timeline-follow"'), 1)

    def test_follow_is_a_chain_of_timeouts_never_an_interval(self) -> None:
        code = self.js()
        self.assertNotIn("setInterval", code)
        tick = _function_body(code, "async function followTick(")
        self.assertIn("await load({ preserve: true, quiet: true })", tick)
        # The next check is scheduled AFTER the awaited load, not before.
        self.assertGreater(tick.index("scheduleFollow()", tick.index("await load")),
                           tick.index("await load"))
        schedule = _function_body(code, "function scheduleFollow(")
        self.assertIn("clearTimeout(state.followTimer)", schedule)
        self.assertIn("setTimeout(followTick, FOLLOW_POLL_MS)", schedule)

    def test_a_hidden_tab_skips_the_fetch_and_catches_up_when_shown(
        self
    ) -> None:
        code = self.js()
        tick = _function_body(code, "async function followTick(")
        hidden_at = tick.index("document.hidden === true")
        self.assertLess(hidden_at, tick.index("await load"))
        self.assertIn("scheduleFollow()", tick[hidden_at:hidden_at + 80])
        init = _function_body(code, "async function init(")
        self.assertIn('"visibilitychange"', init)
        self.assertIn("document.hidden === false", init)

    def test_a_quiet_check_never_reaches_the_error_banner(self) -> None:
        body = _function_body(self.js(), "async function load(")
        catch_at = body.index("} catch (err) {")
        catch_body = body[catch_at:]
        quiet_at = catch_body.index("if (opts.quiet)")
        self.assertIn("state.checkFailed = err.message",
                      catch_body[quiet_at:catch_body.index("} else {")])
        self.assertIn("showError(err.message)",
                      catch_body[catch_body.index("} else {"):])
        # And a quiet check does not clear a banner the reader is
        # looking at, either -- clearError() is gated the same way.
        self.assertIn("if (!opts.quiet) {\n    clearError();", body)

    def test_an_unchanged_response_touches_nothing(self) -> None:
        body = _function_body(self.js(), "async function load(")
        same_at = body.index("fingerprint === state.fingerprint")
        self.assertIn("return", body[same_at:same_at + 80])
        self.assertLess(same_at, body.index("render(data)"))
        fp = _function_body(self.js(), "function fingerprintOf(")
        for field in ("blocks", "window", "rows"):
            self.assertIn(field, fp)

    def test_open_rows_and_scroll_survive_a_refresh(self) -> None:
        code = self.js()
        body = _function_body(code, "async function load(")
        keys_at = body.index("openRowKeys()")
        render_at = body.index("render(data)")
        self.assertLess(keys_at, render_at, "keys captured BEFORE re-render")
        self.assertGreater(body.index("reopenRows(reopen)"), render_at)
        self.assertGreater(body.index("window.scrollTo(0, scrollY)"), render_at)
        row = _function_body(code, "function buildRow(")
        self.assertIn("key: rowKey(row)", row)
        self.assertIn('isOpen: () => toggle.getAttribute("aria-expanded") === "true"', row)
        reopen = _function_body(code, "function reopenRows(")
        self.assertIn("controller.openTests()", reopen)

    def test_follow_is_confined_to_the_newest_run(self) -> None:
        code = self.js()
        status = _function_body(code, "function renderLiveStatus(")
        self.assertIn("followBtn.disabled = !newest", status)
        init = _function_body(code, "async function init(")
        stop_at = init.index("if (!isNewest && state.follow)")
        self.assertIn("setFollow(false)", init[stop_at:stop_at + 300])
        # A refresh of the newest run re-derives its window rather than
        # keeping edges that would trim what arrived since.
        norm = _function_body(code, "function normaliseNewestWindow(")
        self.assertIn("state.from = null", norm)
        for fn in ("function refresh(", "async function followTick("):
            self.assertIn("normaliseNewestWindow()", _function_body(code, fn))

    def test_follow_travels_in_the_url_and_the_cadence_is_not_a_literal(
        self
    ) -> None:
        code = self.js()
        self.assertIn('follow: state.follow ? "1" : null',
                      _function_body(code, "function syncUrl("))
        self.assertIn('state.follow = params.get("follow") === "1"',
                      _function_body(code, "async function init("))
        status = _function_body(code, "function renderLiveStatus(")
        self.assertIn("FOLLOW_POLL_MS / 1000", status)
        html = re.sub(r"<!--.*?-->", "", read_text("timeline.html"), flags=re.S)
        self.assertNotIn("minute", html.lower(),
                         "the cadence is stated from the constant, in JS")


class BrowseFilterUrlInitTest(unittest.TestCase):
    """F4(a) (docs/STREAMS_PLAN.md §5.2 "as built"): the browse filter
    row's state can be set from the page's own URL at load, so a deep
    link (a Watch card's stat link, or any other) can land pre-
    filtered rather than landing on the unfiltered table with the
    reader having to reapply the same filters by hand.

    The server's own contract is ``result=`` (singular, repeatable —
    see api.py's _parse_results_param), not ``results=`` — pinned here
    since the coordinator's own usability-batch message used the wrong
    (plural) name.
    """

    def test_wiring_reads_result_unassigned_and_stale_from_the_url(
        self
    ) -> None:
        body = _function_body(
            read("app.js"), "function wireMainlineControls(")
        self.assertIn('url.searchParams.getAll("result")', body)
        self.assertIn(
            'url.searchParams.get("unassigned") === "1"', body)
        self.assertIn('url.searchParams.get("stale") === "1"', body)
        # Never the plural form -- that is not a param the server reads.
        self.assertNotIn('"results"', body)

    def test_url_state_is_read_before_the_toggles_are_painted(self) -> None:
        """Reading state.activeResults/staleOnly/unassignedOnly AFTER
        buildResultToggles()/the sync calls paint the controls would
        leave their initial aria-pressed out of step with the URL that
        was just used to set them."""
        body = _strip_comments(_function_body(
            read("app.js"), "function wireMainlineControls("))
        url_read_at = body.index('url.searchParams.getAll("result")')
        build_at = body.index("buildResultToggles()")
        self.assertLess(url_read_at, build_at)
        self.assertIn("syncResultToggles()", body[build_at:])
        self.assertIn("syncStaleToggle()", body[build_at:])
        self.assertIn("syncUnassignedToggle()", body[build_at:])

    def test_only_known_result_values_are_accepted(self) -> None:
        """An unrecognised ?result= value must not silently poison
        state.activeResults with a string the server would 400 on the
        very next request."""
        body = _function_body(
            read("app.js"), "function wireMainlineControls(")
        self.assertIn("RESULTS.indexOf(raw) !== -1", body)

    def test_browse_url_carries_unassigned_only_when_the_toggle_is_on(
        self
    ) -> None:
        """WP-24: the `if (state.unassignedOnly) { qs.append(...) }`
        guard became a ternary inside the params object passed to
        apiUrl() -- appendParams() (urls.js) omits null the same way
        the old guard omitted the whole append call, so the on/off
        behaviour is unchanged; only the shape of stating it moved."""
        body = _function_body(read("app.js"), "function browseUrl(")
        self.assertIn(
            'unassigned: state.unassignedOnly ? "1" : null', body)

    def test_the_toggle_chip_exists_and_ships_unpressed(self) -> None:
        html = read_text("index.html")
        toggle_at = html.index('id="unassigned-toggle"')
        self.assertIn('aria-pressed="false"', html[toggle_at:toggle_at + 80])

    def test_the_toggle_is_wired_to_state_and_refilters(self) -> None:
        body = _function_body(
            read("app.js"), "function wireMainlineControls(")
        click_at = body.index('getElementById("unassigned-toggle")')
        handler = body[click_at:click_at + 250]
        self.assertIn("state.unassignedOnly = !state.unassignedOnly", handler)
        self.assertIn("syncUnassignedToggle()", handler)
        self.assertIn("refilterBrowse()", handler)


class WatchPageTest(unittest.TestCase):
    """watch.js / watch.html (WP-20, docs/STREAMS_PLAN.md §2.4).

    The Watchlist's URL grammar IS its persistence mechanism — no
    account, no server-side saved view — so the round-trip property
    (parse the URL, then rebuild the identical URL from what was parsed)
    is the one this page cannot ship broken.
    """

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", _strip_comments(read("watch.js")))

    def test_spec_round_trips_through_split_and_join(self) -> None:
        """splitSpec/joinSpec must be exact inverses, or a shared URL
        silently mutates every time the page that built it re-saves it."""
        code = _strip_comments(read("watch.js"))
        self.assertIn("export function splitSpec(", code)
        self.assertIn("export function joinSpec(", code)
        self.assertIn("export function buildUrl(", code)
        self.assertIn("export function parseSpecs(", code)

    def test_the_split_is_at_the_first_colon_only(self) -> None:
        """A product or environment name may itself contain a colon; the
        kind letter never does. docs/STREAMS_PLAN.md §2.4 is explicit:
        split at the FIRST colon."""
        body = _function_body(read("watch.js"), "export function splitSpec(")
        self.assertIn("indexOf(\":\")", body)
        self.assertIn("slice(at + 1)", body)

    def test_get_all_c_preserves_request_order(self) -> None:
        """URLSearchParams.getAll keeps repeated params in the order they
        appear — the property the whole page's ordering guarantee rests
        on, on both the request out and the cards back."""
        body = _function_body(read("watch.js"), "export function parseSpecs(")
        self.assertIn('getAll("c")', body)

    def test_the_default_is_read_from_local_storage(self) -> None:
        code = _strip_comments(read("watch.js"))
        self.assertIn("window.localStorage.getItem", code)
        self.assertIn("window.localStorage.setItem", code)

    def test_the_copy_link_input_is_a_plain_readonly_field(self) -> None:
        """docs/STREAMS_PLAN.md §2.4: a VISIBLE readonly input, no
        clipboard-API dependency."""
        html = read_text("watch.html")
        self.assertIn('id="watch-link"', html)
        self.assertIn("readonly", html)
        self.assertNotIn("navigator.clipboard", read("watch.js"))

    def test_error_cards_are_rendered_and_ok_cards_are_distinguished(
        self
    ) -> None:
        code = _strip_comments(read("watch.js"))
        self.assertIn("card.ok", code)
        self.assertIn("buildErrorCard", code)
        self.assertIn("buildOkCard", code)

    def test_card_controls_offer_remove_and_reorder(self) -> None:
        """Add-a-card picker, remove, drag-free reorder — the editing
        surface docs/STREAMS_PLAN.md §2.4 asks for (up/down buttons:
        "keyboard-reachable beats drag")."""
        code = _strip_comments(read("watch.js"))
        self.assertIn("function moveCard(", code)
        self.assertIn("function removeCard(", code)
        self.assertIn("function addCard(", code)

    def test_the_nav_entry_exists_on_every_page(self) -> None:
        """§2.4: the page joins the header nav on every page — unlike
        the switcher, it is NOT hidden for a single-product deployment
        (environment cards still benefit)."""
        for name in sorted(os.listdir(STATIC_DIR)):
            if not name.endswith(".html"):
                continue
            self.assertIn(
                'href="watch.html"', read_text(name),
                name + " has no Watch nav entry")

    def test_a_bare_visit_loads_the_saved_default(self) -> None:
        body = _function_body(read("watch.js"), "function init()")
        self.assertIn("readDefault", body)

    def test_an_empty_watchlist_shows_how_to_not_a_blank_page(self) -> None:
        html = read_text("watch.html")
        self.assertIn('id="empty-state"', html)
        code = _strip_comments(read("watch.js"))
        self.assertIn('getElementById("empty-state")', code)

    def test_a_stray_param_no_longer_discards_the_saved_default(
        self
    ) -> None:
        """ADDENDUM to the perf round: init() used to branch on whether
        the URL had ANY query string at all, not on whether it had a
        `c=` card -- so a bare ?product=Atlas (the switcher's own
        navigation, or any stale link) took the "the URL has cards"
        branch, found none, and silently discarded a saved default
        entirely: a shareable Watchlist saved as "my default" rendered
        EMPTY the moment an unrelated param showed up beside it. Live
        reproduction and fix confirmed via the node DOM-shim harness --
        see the commit message. This pins the source-level fix: the
        branch condition must ask about `c` specifically, not `search`
        as a whole."""
        body = _function_body(read("watch.js"), "function init()")
        self.assertIn('new URLSearchParams(search).has("c")', body)
        self.assertNotIn("state.specs = search\n", body)


class WatchHasNoProductSwitcherTest(unittest.TestCase):
    """ADDENDUM to the perf round: the Watch page wrongly behaved
    product-scoped. Watch is cross-product BY DEFINITION
    (docs/STREAMS_PLAN.md §0.9 -- a manager composes cards across
    products; the URL is the whole configuration), so a global product
    switcher that scopes the WHOLE page to one product is not a
    no-op there the way it is on a single-product install -- it is
    actively wrong, and switching it used to navigate destructively
    (see WatchPageTest.test_a_stray_param_no_longer_discards_the_saved_default
    for the other half of that same bug)."""

    def test_the_mount_is_gone(self) -> None:
        html = read_text("watch.html")
        self.assertNotIn('id="product-switcher"', html)

    def test_products_js_is_still_loaded(self) -> None:
        """Kept for adoptProductFromUrl()'s site-wide "the URL wins"
        behaviour, and because products.js's own init() already guards
        a missing mount (StreamsSwitcherTest-style null check) -- there
        is nothing here that NEEDS removing the script tag too, and
        removing it would be a bigger diff for no behavioural gain."""
        self.assertIn('src="products.js"', read_text("watch.html"))

    def test_products_js_survives_a_missing_mount_without_throwing(
        self
    ) -> None:
        """The WP-20 null-deref fix this addendum leans on (products.js's
        own init(), not a page-specific one): it must return before
        calling renderSwitcher() with a null container."""
        body = _function_body(read("products.js"), "async function init()")
        guard_at = body.index("if (!container)")
        return_at = body.index("return", guard_at)
        render_at = body.index("renderSwitcher(")
        self.assertLess(guard_at, return_at)
        self.assertLess(return_at, render_at)

    def test_watch_js_never_reads_the_global_product(self) -> None:
        """The composer (populatePicker) and everything else on this
        page must never filter by getSelectedProduct() -- every card
        already carries its own scope, and the page is cross-product by
        design. (Verified this was ALREADY true before this addendum,
        live via the node DOM-shim harness with localStorage set to one
        product and a second product's streams still offered -- see the
        commit message; this pins it stays that way.)"""
        self.assertNotIn("getSelectedProduct", read("watch.js"))

    def test_the_composer_fetches_every_products_streams_in_parallel(
        self
    ) -> None:
        """populatePicker()'s own claim, pinned: one /api/streams
        request per known product (plus the implicit "" product), never
        filtered to a single selected one."""
        body = _function_body(
            read("watch.js"), "async function populatePicker(")
        self.assertIn('[""].concat(productNames)', body)
        self.assertIn("Promise.all(", body)
        self.assertIn('"api/streams?product="', body)


class WatchStalenessGrammarTest(unittest.TestCase):
    """The "@<n>h"/"@<n>d" declared-staleness suffix (WP-23,
    docs/STREAMS_PLAN.md §2.4) — watch.js's mirror of
    testboard/api.py's _parse_watch_spec()/_EXPECTED_SUFFIX, so a URL
    built by the composer and a hand-typed one parse identically."""

    #: Round-trip specs watch.js's splitSpec/joinSpec must satisfy.
    #: There is no JS engine in this suite (every other frontend test
    #: in this file is a static source-pattern check on the same
    #: basis), so these are exercised for REAL here against the
    #: PYTHON side instead (testboard/api.py's _parse_watch_spec,
    #: which watch.js's splitExpectedSuffix/EXPECTED_SUFFIX is a
    #: byte-for-byte mirror of, pinned by
    #: test_the_regex_matches_the_servers_grammar_exactly above) — a
    #: genuine round-trip failure in the shared grammar fails here,
    #: rather than being asserted only in prose. The JS side itself
    #: was additionally re-verified by hand against a live server (see
    #: the drop note) since a source-pattern check cannot execute
    #: splitSpec/joinSpec directly.
    _ROUND_TRIP_SPECS = [
        "e:linux-sim",
        "e:win-sim@36h",
        "p:Atlas@7d",
        "s:2@1d",
        "p:release@2026",       # "@" present, no valid suffix
        "p:release@2026@1d",    # "@" present twice, suffix at the last
        "e:foo@3w",              # invalid unit, stays part of the name
    ]

    def test_the_regex_matches_the_servers_grammar_exactly(self) -> None:
        """testboard/api.py's _EXPECTED_SUFFIX is r"^\\d+[hd]$" —
        pinned here so the two patterns cannot drift apart silently."""
        code = _strip_comments(read("watch.js"))
        self.assertIn("EXPECTED_SUFFIX = /^\\d+[hd]$/", code)

    def test_the_shared_grammar_round_trips_for_real(self) -> None:
        """Executes the actual round trip against the PYTHON side of
        the shared grammar (see _ROUND_TRIP_SPECS's own comment for
        why Python, not JS) — split, then rebuild the same way
        joinSpec does, and the result must be the exact input."""
        from testboard import api
        for spec in self._ROUND_TRIP_SPECS:
            kind, name, expected = api._parse_watch_spec(spec)
            rebuilt = kind + ":" + name + (
                "@" + expected if expected else "")
            self.assertEqual(rebuilt, spec, spec)

    def test_a_bare_kind_with_no_colon_does_not_round_trip(self) -> None:
        """The one known asymmetry, pinned rather than left as
        folklore: a spec with no colon at all (already an error card,
        never a valid kind) parses to an empty name and rebuilds with
        a trailing colon it did not have — "garbage" in, "garbage:"
        out. Harmless (this shape is never round-tripped through the
        UI, which always writes a real kind), but real, so it is
        asserted here rather than silently assumed away."""
        from testboard import api
        kind, name, expected = api._parse_watch_spec("garbage")
        rebuilt = kind + ":" + name + ("@" + expected if expected else "")
        self.assertEqual(rebuilt, "garbage:")
        self.assertNotEqual(rebuilt, "garbage")

    def test_split_uses_the_last_at_not_the_first(self) -> None:
        body = _function_body(read("watch.js"), "function splitExpectedSuffix(")
        self.assertIn("lastIndexOf(\"@\")", body)

    def test_join_only_appends_when_expected_is_set(self) -> None:
        body = _function_body(read("watch.js"), "export function joinSpec(")
        self.assertIn("entry.expected", body)
        self.assertIn('"@"', body)

    def test_the_composer_offers_a_cadence_choice(self) -> None:
        html = read_text("watch.html")
        self.assertIn('id="add-cadence"', html)
        self.assertIn('value="1d"', html)
        self.assertIn('value="7d"', html)
        self.assertIn('value="custom"', html)
        self.assertIn('id="add-cadence-hours"', html)

    def test_read_cadence_builds_the_same_suffix_grammar(self) -> None:
        body = _function_body(read("watch.js"), "function readCadence(")
        self.assertIn('"custom"', body)
        self.assertIn('hours + "h"', body)

    def test_add_card_carries_the_cadence_through(self) -> None:
        body = _function_body(read("watch.js"), "function addCard(")
        self.assertIn("expected: readCadence()", body)

    def test_the_hero_leads_with_unowned_and_freshness(self) -> None:
        """The 2026-08-10 user redesign: the two numbers a morning scan
        reads — unassigned failures and last-result age — lead every ok
        card, from the SAME per-kind timestamp the staleness judgment
        uses (cardFreshnessIso) and phrased from data (ageText — the
        WindowWordingTest discipline), with a product card naming its
        laggard so the big number is never read as "every environment
        is this fresh". The count shows even at zero (that IS the good
        news); the ACCENT keeps its nonzero gate in applyWatchAccent,
        which is the zero-visible-change rule's surviving half."""
        body = _function_body(read("watch.js"), "function buildHero(")
        self.assertIn("cardFreshnessIso(card)", body)
        self.assertIn("ageText(freshIso, nowMs)", body)
        self.assertIn("card.laggard.environment", body)
        self.assertIn('"unassigned failures"', body)
        accent = _function_body(
            read("watch.js"), "function applyWatchAccent(")
        self.assertIn("card.unassigned_failing", accent)

    def test_accent_precedence_is_unassigned_failure_over_staleness(
        self
    ) -> None:
        """docs/STREAMS_PLAN.md §2.4's decision: the border shows the
        unassigned-failure accent first; staleness only gets the
        border when there is no unassigned failure to show instead —
        but its text line (stalenessText) is independent of this and
        always renders when declared."""
        body = _function_body(read("watch.js"), "function applyWatchAccent(")
        fail_at = body.index("watch-card-accent-fail")
        stale_at = body.index("watch-card-accent-stale")
        self.assertLess(fail_at, stale_at)
        self.assertIn("else if", body)

    def test_staleness_text_uses_both_real_halves(self) -> None:
        """Not a hidden constant on either side: the suffix comes from
        the card (echoed from the URL) and the age from ageText() over
        the card's own freshness timestamp."""
        body = _function_body(read("watch.js"), "function stalenessText(")
        self.assertIn("card.expected", body)
        self.assertIn("ageText(", body)

    def test_staleness_line_is_absent_with_no_declared_expectation(
        self
    ) -> None:
        code = _strip_comments(read("watch.js"))
        for signature in (
            "function buildOkCard(", "function buildStreamCard("
        ):
            body = _function_body(code, signature)
            self.assertIn("stalenessText(card", body, signature)

    def test_stream_cards_own_freshness_is_last_seen(self) -> None:
        body = _function_body(read("watch.js"), "function cardFreshnessIso(")
        self.assertIn("card.last_seen", body)

    def test_product_cards_own_freshness_is_its_laggard(self) -> None:
        body = _function_body(read("watch.js"), "function cardFreshnessIso(")
        self.assertIn("card.laggard.last_reported", body)

    def test_the_stale_accent_is_not_the_result_palette(self) -> None:
        """House rule: staleness is a timing/coverage fact, not a
        failure, so its accent must reuse the SAME amber precedent
        .tl-partial established — never --c-fail or --c-fae."""
        css = read_text("style.css")
        rule_at = css.index(".watch-card-accent-stale")
        rule = css[rule_at:css.index("}", rule_at)]
        self.assertIn("#8a6d00", rule)
        self.assertNotIn("--c-fail", rule)
        self.assertNotIn("--c-fae", rule)


class PlantedWatchRegressionTest(unittest.TestCase):
    """Prove the round-trip and ordering detectors can fail."""

    def test_a_split_at_the_last_colon_would_be_caught(self) -> None:
        """The wrong implementation: a name containing ':' would lose
        everything after its own first colon."""
        planted = (
            "function splitSpec(spec) {\n"
            "  const at = spec.lastIndexOf(':');\n"
            "  return { kind: spec.slice(0, at), name: spec.slice(at + 1) };\n"
            "}\n"
        )
        self.assertNotIn("indexOf(\":\")", planted)

    def test_a_missing_watch_link_would_be_caught(self) -> None:
        planted = "<input id=\"share-link\" type=\"text\">"
        self.assertNotIn('id="watch-link"', planted)

    def test_a_first_at_split_would_be_caught(self) -> None:
        """The wrong implementation: splitting the "@" suffix at the
        FIRST "@" would truncate a name like "release@2026@1d" down to
        "release", losing "2026" — the detector wants the LAST one."""
        planted = (
            "function splitExpectedSuffix(rest) {\n"
            "  const atSign = rest.indexOf('@');\n"
            "  return { name: rest.slice(0, atSign) };\n"
            "}\n"
        )
        self.assertNotIn("lastIndexOf(\"@\")", planted)


#: Matches a hand-built page-URL literal: '"test.html?"', '"watch.html?"',
#: etc. — the shape every converted call site used to write before
#: calling pageUrl()/apiUrl() instead (WP-24, docs/SCOPED_URLS_PLAN.md §4
#: rule 1).
_HTML_CONCAT_RE = re.compile(r'"[\w-]*\.html\?"')

#: Matches `.append("product", ...)` / `.set('stream', ...)` / etc. — a
#: scoped param appended to a URLSearchParams by hand (rule 2). Only the
#: three levels that are NEVER a legitimate page-owned data param anywhere
#: in this app; `environment` is handled separately (also a plain filter
#: on some pages, so it is individually allowlisted rather than banned).
_SCOPE_PARAM_RE = re.compile(
    r'\.(?:append|set)\(\s*["\'](product|stream|baseline)["\']')

_ENVIRONMENT_PARAM_RE = re.compile(
    r'\.(?:append|set)\(\s*["\']environment["\']')


class ScopedUrlConstructionTest(unittest.TestCase):
    """WP-24 (docs/SCOPED_URLS_PLAN.md §4): the guard that ends the
    seven-plus-incident family in that document's §1 — a scoped URL
    (a page-navigation href, or an /api/... fetch's query string) built
    by hand anywhere outside static/urls.js, silently dropping
    stream=/product=/baseline= (or, on the one page it matters,
    carrying a stale one across a scope change). Every hand-built site
    that existed at the start of this work package has been converted;
    this class is what keeps the NEXT one from landing unnoticed.
    """

    #: Files exempt from the ".html?" concatenation check (rule 1), and
    #: why — checked individually below, not by a blanket skip.
    _HTML_CONCAT_EXEMPT = frozenset(["urls.js", "watch.js"])

    def test_no_hand_built_html_concatenation_outside_urls_js(self) -> None:
        """Every OTHER file must build a page-navigation href through
        pageUrl(). watch.js is exempt for exactly one reason —
        buildUrl(), the Watch page's OWN `c=` grammar
        (docs/STREAMS_PLAN.md §2.4), which has nothing to do with the
        product/stream/baseline/environment scope model this module
        owns and round-trips through its own dedicated tests
        (WatchPageTest, WatchStalenessGrammarTest) — checked
        separately, and NOT a licence for a second hand-built site to
        hide in the same file (the next test pins that)."""
        offenders = {}  # type: Dict[str, List[int]]
        for name, source in scripts().items():
            if name in self._HTML_CONCAT_EXEMPT:
                continue
            hits = [
                number for number, line in enumerate(
                    _strip_comments(source).split("\n"), 1)
                if _HTML_CONCAT_RE.search(line)
            ]
            if hits:
                offenders[name] = hits
        self.assertEqual(
            offenders, {},
            "hand-built page-URL literal(s) outside urls.js/watch.js -- "
            "every page-navigation href must be built through "
            "pageUrl(): " + repr(offenders))

    def test_watch_js_has_exactly_one_html_concat_site(self) -> None:
        """The exemption above is a file-wide skip; this is what keeps
        it from quietly covering a SECOND hand-built site in the same
        file — only buildUrl()'s own `"watch.html?" + qs` may remain."""
        hits = [
            number for number, line in enumerate(
                _strip_comments(read("watch.js")).split("\n"), 1)
            if _HTML_CONCAT_RE.search(line)
        ]
        self.assertEqual(len(hits), 1, hits)
        body = _function_body(read("watch.js"), "export function buildUrl(")
        self.assertIn('"watch.html?"', body)

    def test_watch_js_cardLink_still_composes_through_pageUrl(self) -> None:
        """The Watch exemption is the likeliest place for the next bug
        to hide (docs/SCOPED_URLS_PLAN.md §7): cardLink() builds the
        NON-`c=` part of a card's link (index.html?product=/
        environment=/stream=) and must call pageUrl() for it, same as
        every other page's row links — only buildUrl()'s `c=` grammar
        itself is exempt from rule 1, not the whole file."""
        body = _function_body(read("watch.js"), "function cardLink(")
        self.assertIn("pageUrl(", body)
        self.assertNotIn(".html?", body)

    def test_no_hand_built_product_stream_or_baseline_params(self) -> None:
        """`product`/`stream`/`baseline` are NEVER a legitimate
        page-owned data param anywhere in this app (unlike
        `environment`, see below) — a literal first argument naming
        one of them to `.append(`/`.set(` on a URLSearchParams outside
        urls.js is always the regression this module exists to end."""
        offenders = {}  # type: Dict[str, List[Tuple[int, str]]]
        for name, source in scripts().items():
            if name == "urls.js":
                continue
            hits = []
            for number, line in enumerate(
                    _strip_comments(source).split("\n"), 1):
                match = _SCOPE_PARAM_RE.search(line)
                if match:
                    hits.append((number, match.group(1)))
            if hits:
                offenders[name] = hits
        self.assertEqual(
            offenders, {},
            "hand-built product=/stream=/baseline= param(s) outside "
            "urls.js: " + repr(offenders))

    #: (file, function signature) -> why this ONE hand-built
    #: `environment=` site is grandfathered rather than converted. A
    #: NEW site anywhere else still fails (see the test below) --
    #: `environment` is also a plain page-owned filter on some pages
    #: (unlike product/stream/baseline, which are only ever cross-page
    #: SCOPE), so it is individually allowlisted rather than banned
    #: outright, per docs/SCOPED_URLS_PLAN.md §4 rule 2.
    _ENVIRONMENT_ALLOWLIST = {
        ("app.js", "function setEnvironment("):
            "the dashboard's OWN filter toggle, echoed to the address "
            "bar via history.replaceState -- must preserve every OTHER "
            "existing param verbatim (result=/unassigned=/stale=, "
            "F4/F4a's deep-link filters), which none of pageUrl() "
            "(builds fresh) or withStream()/withBaseline()/withProduct() "
            "(reset their own sibling levels by design) are shaped to "
            "do. See the comment at the call site.",
    }

    def test_environment_literal_sites_are_individually_allowlisted(
        self
    ) -> None:
        allowlisted_files = frozenset(
            name for name, _ in self._ENVIRONMENT_ALLOWLIST)
        offenders = {}  # type: Dict[str, List[int]]
        for name, source in scripts().items():
            if name == "urls.js":
                continue
            hits = [
                number for number, line in enumerate(
                    _strip_comments(source).split("\n"), 1)
                if _ENVIRONMENT_PARAM_RE.search(line)
            ]
            if hits:
                offenders[name] = hits
        self.assertEqual(
            set(offenders) - allowlisted_files, set(),
            "a NEW hand-built environment= site appeared outside the "
            "allowlist -- convert it through pageUrl()/apiUrl(), or "
            "add it to _ENVIRONMENT_ALLOWLIST with a reason if it "
            "genuinely cannot be: " + repr(offenders))

    def test_the_environment_allowlist_stays_accurate(self) -> None:
        """The other half of the test above: an allowlisted site that
        stopped hand-appending environment= (converted, or deleted)
        must have its entry removed, or the allowlist silently stops
        proving anything."""
        for name, signature in self._ENVIRONMENT_ALLOWLIST:
            body = _function_body(read(name), signature)
            self.assertTrue(
                _ENVIRONMENT_PARAM_RE.search(_strip_comments(body)),
                name + "'s " + signature + " is allowlisted for a "
                "hand-built environment= site that no longer exists -- "
                "remove the entry")

    def test_a_planted_html_concat_would_be_caught(self) -> None:
        """Detectors must be SHOWN able to fail (the compat-gate
        pattern used throughout this repo, e.g.
        test_python36_compat.py's planted-regression tests) -- this is
        rule 1's literal, applied directly to the exact shape a
        reintroduced hand-built link would take."""
        planted = 'link.href = "test.html?" + params.toString();'
        self.assertTrue(_HTML_CONCAT_RE.search(planted))

    def test_a_planted_scope_param_would_be_caught(self) -> None:
        """Rule 2's literal, for all three banned keys."""
        for key in ("product", "stream", "baseline"):
            planted = 'params.append("{0}", String(streamId));'.format(key)
            match = _SCOPE_PARAM_RE.search(planted)
            self.assertIsNotNone(match, planted)
            self.assertEqual(match.group(1), key)

    def test_a_planted_environment_param_would_be_caught(self) -> None:
        planted = 'qs.append("environment", state.environment);'
        self.assertTrue(_ENVIRONMENT_PARAM_RE.search(planted))


class OpenActionsNeedsActionIncludesAssignedTest(unittest.TestCase):
    """"Needs action" includes every assigned test, whatever its result
    (2026-08-10). Found in the first morning of build-verify manual
    testing: an assignment on a mainline-passing test — routine once
    people assign from a build's no-result/failure rows — was reachable
    through NO view (every result option and the dashboard's "My
    actions" queue gated on FAIL/UNEXPECTED_PASS). The first cut was a
    separate fourth Result option, "All assigned (any result)"; the
    user's verdict the same morning — one more mode adds confusion, the
    default should simply not lie — folded it into "Needs action" as
    the server-side `open=1` composite instead. That option must NOT
    come back; the default view's label says what it now includes."""

    def test_the_default_option_says_what_it_includes(self) -> None:
        html = read("actions.html")
        self.assertIn(
            '<option value="open">Needs action (failing, stale '
            'annotation, or assigned)</option>', html)
        self.assertNotIn("All assigned (any result)", html)

    def test_the_default_mode_sends_open_and_no_result_filter(
            self) -> None:
        """WP-26: this logic moved from listUrl() into filterParams()
        (shared with the bulk assign/unassign control's own request) —
        same assertion, now against the function that actually builds
        it; listUrl() composes filterParams()'s return value in
        rather than duplicating it."""
        body = _function_body(read("actions.js"), "function filterParams(")
        self.assertIn('const open = state.result === "open"', body)
        # WP-44: Failing's two values join the expression; the
        # default mode still sends no result at all.
        self.assertRegex(body, r"const results = open \? null\s*:")
        self.assertRegex(body, r'open: open (\|\| failing )?\? "1" : null')
        list_body = _function_body(read("actions.js"), "function listUrl(")
        self.assertIn("filterParams()", list_body)

    def test_the_narrow_modes_send_their_result_and_no_open(self) -> None:
        """FAIL/UNEXPECTED_PASS stay plain result filters — `open` is
        the default mode's composite, never a standing param. WP-26:
        checked against filterParams(), where this now lives.

        Widened for WP-44 (design §2 B.3(b)): Failing also sends
        `open=1`, the composite the server leaves its unassigned flaking
        tests out of, beside FAIL and FLAKING; Stale annotations is still
        a plain result filter. The requests each mode makes are pinned
        over the shim in OpenActionsCatalogueTest's second half."""
        body = _function_body(read("actions.js"), "function filterParams(")
        self.assertNotIn('open: "1",', body)
        self.assertIn('const failing = state.result === "FAIL";', body)
        self.assertIn('open: open || failing ? "1" : null', body)


class TestPageTimelineLinkTest(unittest.TestCase):
    """test.html's route to the Timeline (2026-08-09 persona walk): the
    delver journey reads "test page → history → timeline of that
    night", and the page had NO on-screen route to the Timeline at all
    — the scoped "View in timeline" link existed only on the
    dashboard's review panel, one step earlier, so anyone who followed
    "Open full test page" lost it. The latest-run line now carries the
    same link, same wording, same `at=` semantics as review.js's."""

    def test_the_latest_run_line_links_into_the_timeline(self) -> None:
        body = _function_body(read("test.js"), "function renderLatest(")
        self.assertIn('pageUrl("timeline"', body)
        self.assertIn("at: latest.start_time", body)
        self.assertIn("View in timeline", body)

    def test_the_link_names_stream_alongside_product_null(self) -> None:
        """Naming `product` in an overrides object suppresses default
        carriage for every level it contains — the WP-24 four-site fix
        round's lesson, pinned here the same way as at those sites."""
        body = _function_body(read("test.js"), "function renderLatest(")
        self.assertIn(
            "{ stream: streamApiScope(), product: null, baseline: null }",
            body)


class OneKindVisibleWordingTest(unittest.TestCase):
    """WP-25's done-when: no UI STRING distinguishes branch from build.
    The 2026-08-09 persona walk found three VISIBLE strings the
    collapse missed — watch.html's composer option ("Branch/build"),
    index.html's tab-list aria-label ("Branch dashboard view"), and the
    delta table's static column header ("This branch") — invisible to
    the per-file guards because nothing pinned visible wording as a
    CLASS. This sweep does: rendered text nodes and aria-label/title
    attribute values in the shipped markup may not say "branch".
    Element ids and class names (branch-band, branch-tabs) are exempt —
    invisible plumbing, and renaming them is churn with no user-visible
    gain. HTML comments are stripped first: historical reasoning keeps
    its vocabulary."""

    @staticmethod
    def _visible_branch_hits(html: str) -> List[str]:
        stripped = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        hits = []  # type: List[str]
        for match in re.finditer(r">([^<>]*)<", stripped):
            if "branch" in match.group(1).lower():
                hits.append(match.group(1).strip())
        for match in re.finditer(
                r'(?:aria-label|title)="([^"]*)"', stripped):
            if "branch" in match.group(1).lower():
                hits.append(match.group(1))
        return hits

    def test_no_visible_branch_wording_in_any_page(self) -> None:
        for name in sorted(os.listdir(STATIC_DIR)):
            if not name.endswith(".html"):
                continue
            hits = self._visible_branch_hits(read(name))
            self.assertEqual(
                [], hits,
                "{} shows branch-kind wording to a user: {!r}".format(
                    name, hits))

    def test_the_detector_can_fail(self) -> None:
        """Planted regression — the compat-gate pattern: a detector
        that has never failed is not known to work."""
        self.assertEqual(
            ["This branch"],
            self._visible_branch_hits(
                "<th>This branch</th><!-- a branch comment -->"))
        self.assertEqual(
            ["Branch dashboard view"],
            self._visible_branch_hits(
                '<div aria-label="Branch dashboard view"></div>'))


class SelectionModuleTest(unittest.TestCase):
    """selection.js (2026-08-10, multi-select): the one shared module
    that owns every table's checkbox column and the one sticky bottom
    action bar. Same "assert against the source" method every other
    class in this file uses (no JS runtime here — see the module
    docstring at the top of this file).
    """

    def test_the_public_surface_is_mountSelectableTable(self) -> None:
        code = read("selection.js")
        self.assertIn("export function mountSelectableTable(", code)

    def test_the_mount_returns_header_row_and_reset(self) -> None:
        body = _function_body(
            read("selection.js"), "export function mountSelectableTable(")
        for name in ("headerCell", "rowCell", "reset"):
            self.assertIn(name, body, name)

    def test_no_confirm_dialog_anywhere(self) -> None:
        self.assertNotIn("confirm(", _strip_comments(read("selection.js")))

    def test_both_bulk_actions_use_postJson(self) -> None:
        code = _strip_comments(read("selection.js"))
        assign_body = _function_body(code, "async function doAssign(")
        unassign_body = _function_body(code, "async function doUnassign(")
        self.assertIn("postJson(", assign_body)
        self.assertIn("postJson(", unassign_body)

    def test_postJson_and_apiUrl_are_imported(self) -> None:
        imported = _imported_names(read("selection.js"))
        self.assertIn("postJson", imported)
        self.assertIn("apiUrl", imported)

    def test_the_bulk_url_composes_through_apiUrl(self) -> None:
        body = _function_body(
            read("selection.js"), "function bulkAssignmentsUrl(")
        self.assertIn('apiUrl(', body)
        self.assertIn('"api/assignments/bulk"', body)

    def test_the_bulk_url_clears_every_scope_level_explicitly(self) -> None:
        """apiUrl() with no scope argument CARRIES the current page's
        own product/stream/baseline/environment (WP-24's whole point,
        docs/SCOPED_URLS_PLAN.md) -- exactly wrong for a request that
        must never pick up a stray `environment=`/`stream=` from
        whatever page happens to have mounted this bar (list mode's
        own mutual-exclusion 400 would trip on the former). Every one
        of the four levels must be named null, not merely omitted."""
        body = _function_body(
            read("selection.js"), "function bulkAssignmentsUrl(")
        for level in ("product", "stream", "baseline", "environment"):
            self.assertIn(level + ": null", body, level)

    def test_no_username_free_text_input_in_the_bar(self) -> None:
        """The action bar's user field is the standard dropdown
        (userPickerSelect(), api.js) -- never a typed username, which
        is exactly the bug the Open Actions fix (below) also removes.
        The note field is DELIBERATELY still a plain text input (an
        optional free-text comment, never an identity) -- this checks
        the variable the bar assigns from userPickerSelect() is never
        ALSO given `.type = "text"`, rather than banning text inputs
        outright."""
        code = _strip_comments(read("selection.js"))
        self.assertIn("userSelectEl = userPickerSelect(", code)
        self.assertNotIn('userSelectEl.type = "text"', code)

    def test_userPickerSelect_is_imported(self) -> None:
        self.assertIn(
            "userPickerSelect", _imported_names(read("selection.js")))

    def test_entryKey_is_reused_not_reimplemented(self) -> None:
        """The NUL-composite row-identity key already has one owner
        (review.js's entryKey, which moved there from app.js — see
        CoverageTest's own docstring on this file). A second
        hand-rolled `environment + "\\0" + script + ...` here is
        exactly what test_sorting_is_implemented_once punishes for
        sorting, one file over."""
        code = _strip_comments(read("selection.js"))
        self.assertIn("entryKey", _imported_names(read("selection.js")))
        self.assertIn("entryKey(", code)
        self.assertNotIn('"\\0"', code)
        self.assertNotIn('"\\x00"', code)

    def test_assign_is_gated_on_a_chosen_user_and_a_nonempty_selection(
        self
    ) -> None:
        body = _function_body(
            read("selection.js"), "function updateAssignButtonState(")
        self.assertIn("userSelectEl.value", body)
        self.assertIn("selected.size", body)

    def test_a_fresh_render_reset_clears_only_that_mounts_own_namespace(
        self
    ) -> None:
        """Selection is per-rendered-view (the module docstring): reset()
        must not sweep every OTHER mounted table's selection off the
        page too, or ticking rows in the browse table would vanish the
        moment the triage queue re-renders."""
        body = _function_body(read("selection.js"), "function reset(")
        self.assertIn("namespace", body)

    def test_select_all_snapshots_intent_before_the_loop(self) -> None:
        """Found by review: each row's own "change" handler calls
        updateHeaderCheckboxState(box) -- the SAME header checkbox node
        the click handler's closure holds -- which recomputes and
        overwrites box.checked from partial progress mid-loop. Reading
        the live box.checked on every iteration (instead of a snapshot
        taken once, before the loop starts) meant only the FIRST row
        ever actually toggled on a "select all" click; every later row
        compared against a header state its own predecessor's side
        effect had already changed. The snapshot is what select-all
        selecting more than one row depends on."""
        body = _function_body(read("selection.js"), "function headerCell(")
        self.assertIn("const target = box.checked", body)
        self.assertIn("rowBox.checked !== target", body)
        self.assertNotIn("rowBox.checked !== box.checked", body)


class SelectionColumnExclusivityTest(unittest.TestCase):
    """selection.js is the ONLY place a checkbox table column may be
    built (2026-08-10) -- four tables needed one; a fifth hand-rolled
    copy is a fifth "does it clear on re-render" bug to get right
    independently, the same reasoning sorting.js/urls.js/review.js
    each already end for their own concern.

    watch.js is excluded from this scan -- not because a checkbox
    there would be fine, but because a second agent is restyling that
    page concurrently in another worktree not touched by this feature;
    this detector's job is this diff's blast radius, not a standing
    claim about a file this work never opens.
    """

    _EXEMPT = frozenset(["selection.js", "watch.js"])

    _CHECKBOX_RE = re.compile(r'\.type\s*=\s*"checkbox"|type\s*=\s*"checkbox"')

    def test_no_hand_rolled_checkbox_column_outside_selection_js(
        self
    ) -> None:
        offenders = {}  # type: Dict[str, List[int]]
        for name, source in scripts().items():
            if name in self._EXEMPT:
                continue
            hits = [
                number for number, line in enumerate(
                    _strip_comments(source).split("\n"), 1)
                if self._CHECKBOX_RE.search(line)
            ]
            if hits:
                offenders[name] = hits
        self.assertEqual(
            offenders, {},
            "a checkbox table column outside selection.js (and the "
            "watch.js exemption above) -- multi-select's checkbox "
            "column belongs in selection.js, mounted via "
            "mountSelectableTable(): " + repr(offenders))

    def test_a_planted_checkbox_column_would_be_caught(self) -> None:
        planted = (
            'const box = document.createElement("input");\n'
            'box.type = "checkbox";\n'
        )
        self.assertTrue(self._CHECKBOX_RE.search(planted))


class SelectionMountSitesTest(unittest.TestCase):
    """Each of the four table families actually mounts selection.js
    (2026-08-10) -- pins the call site so a refactor that drops one
    silently (a table that quietly stops offering multi-select) fails
    the build instead of only a manual click-through.
    """

    #: (file, a regex fragment matching that file's mountSelectableTable
    #: call, description) — whitespace-tolerant (re.DOTALL), since each
    #: call is written across more than one line.
    _SITES = (
        ("actions.js",
         r'mountSelectableTable\(\s*document\.getElementById\('
         r'"actions-table"\)',
         "Open Actions rows"),
        ("app.js",
         r'mountSelectableTable\(\s*document\.getElementById\('
         r'"queue-table"\)',
         "the triage queue table"),
        ("app.js",
         r'mountSelectableTable\(\s*document\.getElementById\('
         r'"dashboard-table"\)',
         "the browse (all tests) table"),
        ("compare.js",
         r'mountSelectableTable\(\s*document\.getElementById\('
         r'"delta-table"\)',
         "the build delta category table"),
    )

    def test_every_site_mounts_selection(self) -> None:
        for filename, pattern, description in self._SITES:
            with self.subTest(site=description):
                code = _strip_comments(read(filename))
                self.assertRegex(code, re.compile(pattern, re.S), (
                    "{} ({}) does not mount selection.js's "
                    "checkbox column".format(description, filename)))

    def test_every_mount_imports_mountSelectableTable(self) -> None:
        for filename in ("actions.js", "app.js", "compare.js"):
            with self.subTest(file=filename):
                self.assertIn(
                    "mountSelectableTable",
                    _imported_names(read(filename)), filename)

    def test_build_delta_rows_carry_the_page_stream_id_as_origin(
        self
    ) -> None:
        """Point 3 of the feature spec: a selection made on a build's
        delta table carries that build's stream_id as origin, the same
        entry object reviewEntry() already builds for the row-level
        assignee picker beside it -- no separate plumbing, so this pins
        that the SAME entry is handed to both."""
        code = _strip_comments(read("compare.js"))
        body = _function_body(code, "function buildDeltaRow(row) {")
        self.assertIn("reviewEntry(row, streamId)", body)
        self.assertIn("deltaSelectionMount.rowCell(entry)", body)
        self.assertIn("assigneeSelect(entry", body)

    def test_dashboard_rows_reuse_tagStreams_own_annotation(self) -> None:
        """The dashboard/queue tables carry a stream_id only when
        tagStream() already stamped one (a branch's own-results tab) --
        rowCell() is handed the row object itself, not a stripped
        subset, so this is "for free" rather than a second mechanism
        parallel to assigneeSelect()'s own entry.stream_id read."""
        code = _strip_comments(read("app.js"))
        self.assertIn("browseSelectionMount.rowCell(row)", code)
        self.assertIn("queueSelectionMount.rowCell(entry)", code)

    def test_the_browse_mounts_refresh_callback_is_not_registered_on_a_branch_page(
        self
    ) -> None:
        """Found by review: a bulk action from the build delta table
        (compare.js's OWN mount, a separate namespace) calls every
        registered onChanged listener, not only its own -- registering
        app.js's refreshAll() unconditionally at module load meant a
        branch-scoped page (`?stream=`) fired an unscoped
        /api/summary + queue + /api/dashboard fetch after a delta-table
        bulk action, into sections init() itself documents must never
        be fetched there ("no unscoped /api/summary fetch, no queues,
        no browse table" -- what keeps a branch-scoped page at zero
        visible change). The registration must be conditional on NOT
        being stream-scoped, decided from the URL at module load time
        (state.streamId is not set yet when this const is built)."""
        code = _strip_comments(read("app.js"))
        self.assertIn("getSelectedStreamId() === null", code)
        self.assertIn("registerRefresh ? { onChanged:", code)


class OpenActionsUsernameDropdownTest(unittest.TestCase):
    """Open Actions' bulk-assign control used a free-text username
    <input> ("just lazy" — the user's words, 2026-08-10): a typo
    silently assigned work to a name nobody could act on, with no
    active-user check and no autocomplete, unlike every OTHER assignee
    control in this app. Fixed to the same standard dropdown
    (userPickerSelect(), api.js) assigneeSelect() itself builds on.
    """

    def test_the_markup_has_no_free_text_username_input(self) -> None:
        html = read_text("actions.html")
        bulk_at = html.index('id="bulk-actions"')
        table_at = html.index('id="actions-table"')
        section = html[bulk_at:table_at]
        self.assertNotIn('id="bulk-assign-username"', section)
        self.assertIn('id="bulk-assign-username-slot"', section)

    def test_the_comment_input_stays_free_text(self) -> None:
        """Only the USERNAME field was the bug -- the optional note is
        still (and should stay) a plain text box."""
        html = read_text("actions.html")
        self.assertIn(
            '<input id="bulk-assign-comment" type="text"', html)

    def test_init_builds_the_standard_dropdown_and_keeps_the_id(
        self
    ) -> None:
        """Every OTHER function in this file reads the control by
        document.getElementById("bulk-assign-username") -- the fix
        must keep that id on the built <select>, not rename every
        caller."""
        body = _function_body(read("actions.js"), "function init() {")
        self.assertIn("userPickerSelect(", body)
        self.assertIn('bulkAssignUsername.id = "bulk-assign-username"', body)
        self.assertIn('"bulk-assign-username-slot"', body)

    def test_userPickerSelect_is_imported(self) -> None:
        self.assertIn(
            "userPickerSelect", _imported_names(read("actions.js")))

    def test_the_change_gate_listens_on_change_not_input(self) -> None:
        """A <select> is a discrete choice; "input" is the wrong event
        to gate a select on in every engine this project has to run
        against (some fire it, some do not) -- "change" always fires."""
        body = _function_body(read("actions.js"), "function init() {")
        self.assertIn(
            'bulkAssignUsername.addEventListener("change", '
            "updateBulkActionsControl)", body)
        self.assertNotIn(
            '.addEventListener("input", updateBulkActionsControl)', body)

    def test_a_planted_free_text_input_would_be_caught(self) -> None:
        """Planted regression — proves the markup check above actually
        distinguishes an input from a select."""
        planted = (
            '<div id="bulk-actions">'
            '<input id="bulk-assign-username" type="text">'
            '</div>'
        )
        self.assertIn('id="bulk-assign-username"', planted)


#: Quote characters (string literal AND JSDoc backtick) whose presence
#: immediately before "/api" is what distinguishes a NETWORK CALL from
#: prose naming an endpoint ("GET /api/summary" in a comment has no
#: quote in front of it at all).
_QUOTE_CHARS = "'\"`"

#: A quote immediately followed by "/api" and then either another "/"
#: (the common case, "/api/summary") or a closing quote (the bare
#: "/api" form). Comments are stripped before this ever runs (see the
#: callers), so a JSDoc mention like `` `/api/users` `` — a backtick
#: pair documenting an endpoint's name, not calling it — never reaches
#: this pattern at all.
_ROOT_ABSOLUTE_API_RE = re.compile(
    "[" + _QUOTE_CHARS + "]/api(?:/|[" + _QUOTE_CHARS + "])")


def _root_absolute_api_hits(source: str) -> List[int]:
    """Line numbers where comment-stripped *source* names a root-absolute
    ``/api/...`` literal — see :data:`_ROOT_ABSOLUTE_API_RE`."""
    code = _strip_comments(source)
    return [
        number for number, line in enumerate(code.split("\n"), 1)
        if _ROOT_ABSOLUTE_API_RE.search(line)
    ]


class RootAbsoluteApiUrlTest(unittest.TestCase):
    """No ``/api/...`` literal may be root-absolute anywhere in static/
    (WP-28; see docs/UPGRADE_PLAN_STATUS.md, the tooling night).

    nginx proxies a prod deployment at a path prefix it does NOT strip
    (``/testboard/`` arrives at the backend verbatim), so every API
    call has to resolve RELATIVE to the page that made it —
    ``"api/summary"``, never ``"/api/summary"`` — to land inside the
    prefix from a page served at ``/testboard/index.html`` and at the
    bare root alike (every page in static/ is flat, so "relative to the
    page" is simply "no leading slash", the fix this drop made
    everywhere such a literal was found). A single surviving
    ``"/api/..."`` literal silently escapes the prefix the moment
    nginx is in front of the server — invisible in review (it still
    works perfectly against a bare, unprefixed server, which is every
    dev/local run), silent in production until someone notices a
    request left the proxy. Exactly the shape SCOPED_URLS_PLAN.md's
    incident list already catalogued for hand-built scope URLs, one
    path segment further in — hence a guard in the same style as that
    plan's ``ScopedUrlConstructionTest``, not a narrower one-off.
    """

    #: Files known to carry a legitimate NUL byte (the "\\0" composite
    #: key separator — see ``CoverageTest``), which a shell/ripgrep-
    #: style scan treats as binary and silently SKIPS rather than
    #: erroring. Both were nearly missed while fixing this exact bug
    #: family (11 of the literals this drop fixed lived in these two
    #: files) precisely because a first-pass grep skipped them without
    #: comment. ``scripts()``/``read()`` decode bytes explicitly and
    #: are immune, but this asserts the scan actually reached them
    #: rather than trusting that silently. (WP-44 item 17: test.js now
    #: spells its NUL as the escape and left this list; the list is the
    #: one NoNulByteTest holds equal to every NUL-bearing file under
    #: static/, so the scan is asserted to reach all of them.)
    _NUL_FILES = _NUL_BYTE_FILES

    def test_the_nul_byte_files_are_still_in_scope(self) -> None:
        found = scripts()
        for name in self._NUL_FILES:
            self.assertIn(name, found, name + " missing from the scan")
            self.assertIn(
                "\x00", found[name],
                name + " no longer contains a NUL byte -- if that "
                "changed on purpose, swap in a different NUL-bearing "
                "example rather than losing this guard's coverage of "
                "the blind spot silently")

    def test_no_root_absolute_api_literal_anywhere(self) -> None:
        offenders = {
            name: hits
            for name, source in scripts().items()
            for hits in [_root_absolute_api_hits(source)]
            if hits
        }
        self.assertEqual(
            offenders, {},
            "root-absolute /api/... literal(s) found -- must be "
            "relative (\"api/...\", no leading slash) so a request "
            "resolves inside the URL prefix from whatever page made "
            "it, bare or under /testboard/ alike: " + repr(offenders))

    def test_no_root_absolute_href_or_src_in_html(self) -> None:
        """The other half of the audit (WP-28; see
        docs/UPGRADE_PLAN_STATUS.md): every page's own nav/asset
        links, pinned relative too — locks in the phase's recon
        finding rather than trusting it to stay true unchecked."""
        pattern = re.compile(r'''(?:href|src)=["']/''')
        offenders = {}  # type: Dict[str, List[int]]
        for name in sorted(os.listdir(STATIC_DIR)):
            if not name.endswith(".html"):
                continue
            with io.open(os.path.join(STATIC_DIR, name), "rb") as handle:
                source = handle.read().decode("utf-8")
            hits = [
                number for number, line in enumerate(source.split("\n"), 1)
                if pattern.search(line)
            ]
            if hits:
                offenders[name] = hits
        self.assertEqual(
            offenders, {},
            "root-absolute href=\"/...\" or src=\"/...\" found in "
            "static/*.html: " + repr(offenders))


class PlantedRootAbsoluteApiRegressionTest(unittest.TestCase):
    """Prove the detector can fail, not merely that it passes today."""

    def test_a_wrapper_call_would_be_caught(self) -> None:
        planted = 'return apiUrl("/api/dashboard", {}, {});'
        self.assertEqual(len(_root_absolute_api_hits(planted)), 1)

    def test_a_direct_fetch_outlier_would_be_caught_too(self) -> None:
        """Not just fetchJson/postJson/putJson/apiUrl call sites -- the
        SAME pattern must catch a bare fetch() outlier, the exact shape
        whatsnew.js's own direct call had before this drop fixed it."""
        planted = 'const response = await fetch("/api/site-notes");'
        self.assertEqual(len(_root_absolute_api_hits(planted)), 1)

    def test_a_bare_api_literal_with_no_trailing_slash_is_caught(self) -> None:
        planted = 'const path = "/api";'
        self.assertEqual(len(_root_absolute_api_hits(planted)), 1)

    def test_a_relative_literal_is_not_flagged(self) -> None:
        """The fixed shape must not itself trip the guard."""
        planted = 'return apiUrl("api/dashboard", {}, {});'
        self.assertEqual(_root_absolute_api_hits(planted), [])

    def test_a_jsdoc_mention_is_not_flagged(self) -> None:
        """The false positive this guard must NOT trip on: a backtick-
        quoted endpoint name inside a comment, documenting behaviour
        rather than calling it (api.js's own loadUsers() docstring, of
        all places -- `/api/users` mentioned in prose one line below
        the real, relative call site)."""
        planted = (
            "/**\n"
            " * carry, now here): `/api/users` returns active users "
            "only.\n"
            " */\n"
            'const real = fetchJson("api/users");\n'
        )
        self.assertEqual(_root_absolute_api_hits(planted), [])


class MuteControlsTest(unittest.TestCase):
    """WP-40 as amended 2026-10-01: durations are a dropdown of HOURS,
    built in ONE place, and the selection bar's Mute reuses the Assign
    owner and note inputs instead of owning its own."""

    SURFACES = ("selection.js", "review.js", "test.js", "actions.js")

    def test_every_duration_control_is_the_one_shared_select(self) -> None:
        for name in self.SURFACES:
            code = _strip_comments(read(name))
            self.assertIn("muteDurationSelect(", code, name)
            self.assertNotIn("inputMode", code, name)
            self.assertNotIn("days:", code, name)
        api = _strip_comments(read("api.js"))
        for label in ("12 hours", "1 day", "2 days", "3 days", "5 days",
                      "7 days"):
            self.assertIn('label: "' + label + '"', api)
        self.assertIn("hours: 168", api)

    def test_the_wire_carries_hours_never_days(self) -> None:
        for name in self.SURFACES:
            code = _strip_comments(read(name))
            self.assertIn("hours:", code, name)

    def test_extend_is_one_click_on_seven_days(self) -> None:
        for name in ("review.js", "test.js", "actions.js"):
            code = _strip_comments(read(name))
            self.assertIn("muteDurationSelect(\"review-duration-select\", 168)",
                          code, name)
            self.assertNotIn("Extend 7 days", code, name)
        self.assertNotIn("Extend all 7 days", read("actions.html"))

    def test_the_bar_mute_is_gated_on_owner_note_and_duration(self) -> None:
        code = _strip_comments(read("selection.js"))
        gate = _function_body(code, "function updateMuteButtonState(")
        for needle in ("selected.size === 0", "userSelectEl.value",
                       "noteInputEl.value.trim()", "muteDurationEl.value"):
            self.assertIn(needle, gate)
        # The bar has no input of its own for the reason or the owner.
        self.assertNotIn("muteReasonEl", code)
        do = _function_body(code, "async function doMute(")
        self.assertIn("reason: reason", do)
        self.assertIn("assignee: owner", do)
        self.assertIn("hours: hours", do)
        self.assertIn("noteInputEl.value = \"\"", do)
        self.assertIn("muteDurationEl.value = \"\"", do)
        # Both listeners that already drive the Assign button reach it.
        self.assertIn("muteDurationEl.addEventListener(\"change\"", code)
        comment_gate = _function_body(
            code, "function updateCommentButtonState(")
        self.assertIn("updateMuteButtonState()", comment_gate)


class MutedBesideFailingTest(unittest.TestCase):
    """The user's rule (2026-10-01): "(+X muted) everywhere we have a
    'failing'". There is no Muted tile; the tiles, charts and Watch
    cards carry the muted count beside the failing one."""

    def test_there_is_no_muted_tile_but_the_tab_stays(self) -> None:
        index = read("index.html")
        row = index[index.index('id="stat-tiles"'):]
        row = row[:row.index("</div>\n    </section>")]
        self.assertNotIn(">Muted<", row)
        status = _strip_comments(_function_body(
            read("app.js"), "function renderStatus()"))
        self.assertNotIn('label: "Muted"', status)
        self.assertIn('{ id: "muted", label: "Muted" }', read("app.js"))

    def test_the_two_failing_tiles_name_their_muted_failures(self) -> None:
        status = _strip_comments(_function_body(
            read("app.js"), "function renderStatus()"))
        self.assertIn("status.muted_new_failures", status)
        self.assertIn("status.muted_still_failing", status)

    def test_the_charts_draw_a_grey_extension(self) -> None:
        """Widened at WP-43 from "the spelling `extra: entry.muted || 0`
        appears twice" to "both bar callers draw a muted segment": both
        now go through failingBar(), which draws the muted failures as
        the last segment of the split or, for a payload without the
        split, as the WP-40 grey extension; barRows draws either grey."""
        app = _strip_comments(read("app.js"))
        bars = _function_body(app, "function renderBars()")
        self.assertEqual(bars.count("failingBar("), 2)
        bar = _function_body(app, "function failingBar(")
        self.assertIn("extra: entry.muted || 0", bar)
        self.assertIn('{ value: split.muted, className: "bar-muted" }', bar)
        charts = _strip_comments(read("charts.js"))
        self.assertIn("item.extra", charts)
        self.assertIn('className: "bar-muted"', charts)

    def test_the_watch_failing_stat_carries_the_suffix(self) -> None:
        """Widened for WP-44 item 7: the Failing stat names the flaking
        failures beside the muted ones, from one bracket helper, and a
        stream card still names neither (decision 9)."""
        card = _strip_comments(_function_body(
            read("watch.js"), "function buildOkCard("))
        self.assertRegex(card, r'buildStat\("Failing", card\.failing, '
                               r'card\.muted,\s*card\.flaking\)')
        self.assertNotIn('buildStat("Muted"', card)
        self.assertNotIn('buildStat("Flaking"', card)
        stat = _strip_comments(_function_body(
            read("watch.js"), "function buildStat("))
        self.assertIn("setAsideBracket(muted, flaking)", stat)
        stream = _strip_comments(_function_body(
            read("watch.js"), "function buildStreamCard("))
        self.assertIn("buildStat(CATEGORY_LABELS[key], card[key])", stream)
        self.assertNotIn("flaking", stream)

    def test_the_tiles_name_the_flaking_failures_too(self) -> None:
        """WP-44 item 7 (design §3 U6): each failing tile's suffix names
        its own flaking figure after its muted one, through the one
        set-aside helper, and the New failures line says "or flaking"
        only where the scope has a mark."""
        status = _strip_comments(_function_body(
            read("app.js"), "function renderStatus()"))
        self.assertRegex(status, r"withSetAside\(newFailuresSub\("
                                 r"status\.flaky_total\),\s*status\."
                                 r"muted_new_failures, status\."
                                 r"flaking_new_failures\)")
        self.assertRegex(status, r'withSetAside\("failed before too", '
                                 r"status\.muted_still_failing,\s*status\."
                                 r"flaking_still_failing\)")
        helper = _strip_comments(_function_body(
            read("app.js"), "function withSetAside("))
        self.assertIn("setAsideSuffix(muted, flaking)", helper)
        api = _strip_comments(read("api.js"))
        self.assertIn('"were passing or flaking before"', _function_body(
            api, "export function newFailuresSub("))
        self.assertIn('"were passing before"', _function_body(
            api, "export function newFailuresSub("))
        for gone in ("mutedSuffix", "failingWithMuted", "withMutedSuffix"):
            for name, source in scripts().items():
                self.assertNotIn(gone, _strip_comments(source), name)


class AssignedSplitTest(unittest.TestCase):
    """WP-43 (docs/design/charts-assigned-split-r0.md §3): the three
    charts split the red three ways, unassigned, assigned, muted, so a
    glance says how much of it is nobody's yet. The order, the words and
    the fallback die silently: a swapped pair still draws, and an older
    server's payload drawn as all-red still looks like a chart."""

    _ORDER = ("FAIL_UNASSIGNED", "FAIL_ASSIGNED", "FAIL_MUTED",
              "UNEXPECTED_PASS")

    def _app(self) -> str:
        return _strip_comments(read("app.js"))

    def test_both_bar_callers_draw_through_one_split(self) -> None:
        bars = _function_body(self._app(), "function renderBars()")
        self.assertEqual(bars.count("failingBar("), 2, bars)
        self.assertEqual(bars.count("failingLegend("), 2)

    def test_the_bars_split_unassigned_assigned_muted(self) -> None:
        """Widened for WP-44 item 7 (design §3 U7): a fourth part, the
        flaking failures, after the grey, in the chart's mauve; the split
        is told the flaking count only where the scope has a mark, so a
        scope with none is the three-part split exactly."""
        bar = _function_body(self._app(), "function failingBar(")
        order = [bar.index(token) for token in (
            '{ value: split.unassigned, className: "fill-fail" }',
            '{ value: split.assigned, className: "bar-assigned" }',
            '{ value: split.muted, className: "bar-muted" }',
            "{ value: split.flaking, className: FLAKING_BAR.className }")]
        self.assertEqual(order, sorted(order))
        self.assertRegex(
            bar, r"failingSplit\(failing, entry\.assigned, entry\.muted,"
                 r"\s*flakyScope\(\) \? entry\.flaking \|\| 0 : null\)")
        self.assertIn("return state.summary.status.flaky_total > 0;",
                      _function_body(self._app(), "function flakyScope()"))
        legend = _function_body(self._app(), "function failingLegend(")
        self.assertIn("FAILING_SPLIT_LEGEND.concat([FLAKING_BAR])", legend)
        self.assertIn("flakyScope()", legend)
        api = _strip_comments(read("api.js"))
        split = _function_body(api, "export function failingSplit(")
        self.assertIn('const keyed = typeof flaking === "number";', split)
        self.assertIn('[{ key: "flaking", swatchClass: '
                      'FLAKING_BAR.swatchClass }] : []', split)
        self.assertRegex(api, r'className: "bar-flaking"')
        self.assertRegex(api, r'swatchClass: "swatch-flaking"')
        self.assertIn("tooltipRows: split.rows.concat(moreRows)", bar)
        self.assertIn("split.words", bar)
        # An older payload: the WP-40 bar, never all red.
        self.assertIn('typeof entry.assigned !== "number"', bar)
        self.assertLess(bar.index('typeof entry.assigned !== "number"'),
                        bar.index("failingSplit("))

    def test_the_trend_series_bottom_first(self) -> None:
        app = self._app()
        series = app[app.index("const TREND_SPLIT_SERIES = ["):]
        series = series[:series.index("];")]
        keys = re.findall(r'key: "(\w+)"', series)
        self.assertEqual(tuple(keys), self._ORDER)
        self.assertIn("const FAILING_SPLIT_LEGEND = "
                      "TREND_SPLIT_SERIES.slice(0, 3)", app)
        # WP-44 (design §3 U7): the flaking runs after the grey,
        # before unexpected passes, in the chart token.
        self.assertRegex(app, r"const TREND_FLAKING_SERIES = "
                              r"FAILING_SPLIT_LEGEND\.concat\(\s*\[FLAKING_BAR\],"
                              r" TREND_SPLIT_SERIES\.slice\(3\)\)")
        bar = _strip_comments(read("api.js"))
        bar = bar[bar.index("export const FLAKING_BAR = {"):]
        bar = bar[:bar.index("};")]
        self.assertIn('key: "FAIL_FLAKING"', bar)
        self.assertIn('segClass: "seg-flaking"', bar)

    def test_the_unassigned_part_is_derived_and_floored(self) -> None:
        """Widened for WP-44 (design §3 U7): where the flaking series is drawn
        its runs leave the unassigned part too (counted once)."""
        body = _function_body(self._app(), "function splitNights(")
        self.assertRegex(
            body, r"FAIL_UNASSIGNED: Math\.max\(\s*0, night\.FAIL - "
                  r"part\.FAIL_ASSIGNED - part\.FAIL_MUTED - flaking\)")
        self.assertIn("const flaking = withFlaking ? part.FAIL_FLAKING : 0;",
                      body)
        # Merged by date, and a night the part does not carry means the
        # whole card draws today's two series.
        self.assertIn("byDate.get(night.date)", body)
        self.assertIn("return null", body)

    def test_the_home_trend_counts_days(self) -> None:
        body = _function_body(self._app(), "function renderTrend()")
        self.assertIn('{ unit: "day" }', body)
        # Widened for WP-44 (design §3 U7): the split's series gain the flaking
        # part where trendFlaking() says so; no split is still two.
        self.assertIn("isSplit ? splitSeries : TREND_SERIES", body)
        self.assertIn("const splitSeries = flaking ? TREND_FLAKING_SERIES "
                      ": TREND_SPLIT_SERIES;", body)

    def test_the_caption_ships_under_the_plot(self) -> None:
        html = read("index.html")
        caption = ("Assigned and muted are as they stand now: a day's "
                   "columns recolour as tests are taken.")
        self.assertEqual(html.count(caption), 1)
        at = html.index(caption)
        self.assertLess(html.index('id="trend-chart"'), at)
        self.assertLess(at, html.index("Data table", at))
        tag = html[html.rindex("<p", 0, at):at]
        self.assertIn('id="trend-split-caption"', tag)
        # The no-split state is the markup's default: nothing about a
        # split shows under an empty or two-series plot.
        self.assertIn(" hidden>", tag)

    def test_the_table_twin_has_the_three_parts_after_failed(self) -> None:
        html = read("index.html")
        table = html[html.index('id="trend-table"'):]
        table = table[:table.index("</thead>")]
        # Widened for WP-44 (design §3 U7): a Flaking column after Muted, hidden
        # unless the flaking series is drawn.
        self.assertEqual(
            re.findall(r"<th[^>]*>([^<]+)</th>", table),
            ["Day", "Failed", "Unassigned", "Assigned", "Muted", "Flaking",
             "Unexpected pass", "Runs"])
        self.assertEqual(table.count("trend-split-col"), 3)
        self.assertEqual(table.count('trend-split-col" hidden>'), 3)
        self.assertEqual(table.count('<th class="num trend-mark-col" '
                                     'hidden>Flaking</th>'), 1)
        body = _function_body(self._app(), "function renderTrend()")
        self.assertRegex(
            body, r"\[night\.FAIL, night\.FAIL_UNASSIGNED, "
                  r"night\.FAIL_ASSIGNED,\s*night\.FAIL_MUTED\]\.concat\("
                  r"flaking \? \[night\.FAIL_FLAKING\] : \[\],\s*"
                  r"\[night\.UNEXPECTED_PASS, night\.total\]\)")
        self.assertIn("th.hidden = !(isSplit && flaking);", body)

    def test_the_words_have_one_helper(self) -> None:
        api = _strip_comments(read("api.js"))
        helper = _function_body(api, "export function failingSplit(")
        self.assertIn('"none failing"', helper)
        for key in ("unassigned", "assigned", "muted"):
            self.assertIn('key: "' + key + '"', helper)
        app = self._app()
        self.assertEqual(app.count("failingSplit("), 1)
        self.assertEqual(
            _unassigned_literals(app), [],
            "app.js spells the unassigned words itself; failingSplit() "
            "in api.js is their one home")

    def test_the_unassigned_detector_can_fail(self) -> None:
        planted = 'value: split.unassigned + " unassigned",'
        self.assertEqual(_unassigned_literals(planted), ['" unassigned"'])

    def test_every_new_class_has_a_rule_and_a_user(self) -> None:
        css = read("style.css")
        code = "".join(_strip_comments(text) for text in scripts().values())
        self.assertIn("--c-assigned: #3b6cc0;", css)
        for name in ("swatch-assigned", "swatch-muted", "seg-assigned",
                     "seg-muted", "bar-assigned"):
            self.assertRegex(css, r"\." + name + r"\b", name)
            self.assertIn('"' + name + '"', code, name)
        for rule in (".tt-key.swatch-assigned", ".tt-key.swatch-muted",
                     ".legend-swatch.swatch-assigned",
                     ".legend-swatch.swatch-muted"):
            self.assertIn(rule, css)
        # Widened for WP-44 item 7: the flaking part's two classes, named
        # in api.js because they spell a flaky word.
        self.assertIn("--c-flaking: #a66993;", css)
        for name in ("swatch-flaking", "bar-flaking", "seg-flaking"):
            self.assertRegex(css, r"\." + name + r"\b", name)
            self.assertIn('"' + name + '"', _strip_comments(read("api.js")),
                          name)
        for rule in (".tt-key.swatch-flaking", ".legend-swatch.swatch-flaking"):
            self.assertIn(rule, css)

    def test_barrows_draws_segments_on_one_path(self) -> None:
        body = _strip_comments(_function_body(
            read("charts.js"), "export function barRows("))
        self.assertIn("item.segments ||", body)
        self.assertIn('className: "bar-muted"', body)
        loop = body[body.index("for (const item of items)"):]
        self.assertIn("segmentsOf(item)", loop.split("\n")[1])
        self.assertIn("index === 0 || segment.value > 0", body)
        treemap = _function_body(
            read("charts.js"), "export function treemapBoxes(")
        self.assertNotIn("segments", treemap)


def _unassigned_literals(source: str) -> List[str]:
    """String literals in *source* that spell the word "unassigned",
    bar the wire parameter and the browse chip's element id."""
    return [
        literal for literal in re.findall(r'"[^"\n]*"', source)
        if re.search(r"\bunassigned\b", literal)
        and literal not in ('"unassigned"', '"unassigned-toggle"')
    ]


#: Spellings of a request for the trend's split.
_TREND_REQUEST_TOKENS = ("fetchAssignedTrend(", "loadTrendSplit(",
                         "assignedTrendUrl(")


def _trend_before_headline(source: str) -> List[str]:
    """Requests for the trend's split in *source*'s refreshAll() that go
    out before the headline has been drawn: anywhere but the headline's
    own block after its ``renderHeadline();``. The part is never on the
    first paint, so the first batch never names it."""
    refresh = _function_body(_strip_comments(source),
                             "async function refreshAll(")
    start = refresh.find("renderHeadline();")
    end = refresh.find("const queuePart = ")
    if end == -1:
        end = len(refresh)
    found = []  # type: List[str]
    for token in _TREND_REQUEST_TOKENS:
        for match in re.finditer(re.escape(token), refresh):
            if start == -1 or not start < match.start() < end:
                found.append("{} at {}".format(token, match.start()))
    return found


class AssignedTrendFetchTest(unittest.TestCase):
    """WP-43 §1, decisions 8 and 13: the trend's split is its own part.
    Since WP-44 the part also reads every night's flaking runs, so it is
    never on the first paint: Home's first batch is three requests (the
    headline, the active queue, the browse page), and the split is sent
    once the headline has been drawn, by one function on every path (the
    first load, every refreshAll(), and the coalescer once per burst).
    Only the trend card waits for it, drawing the headline's totals until
    it lands. A split back in the first batch would still work, only
    making the first paint pay for it, which is why it is pinned."""

    def _app(self) -> str:
        return _strip_comments(read("app.js"))

    def test_the_part_carries_the_headlines_scope(self) -> None:
        app = self._app()
        url = _function_body(app, "function assignedTrendUrl()")
        self.assertIn('parts: "assigned_trend"', url)
        self.assertIn("apiScope()", url)
        self.assertIn("apiScope()", _function_body(app, "function summaryUrl()"))

    def test_it_is_fetched_after_the_headline_and_after_a_take(
            self) -> None:
        """The pin is now: one request site, inside loadTrendSplit(),
        called once by refreshAll() after renderHeadline() and once by
        the coalescer after its loop. Nothing is asked for before the
        generation's headline has been drawn, and only the newest split
        asked for, in a current generation, is drawn."""
        app = self._app()
        self.assertEqual(app.count("fetchJson(assignedTrendUrl())"), 1)
        self.assertEqual(len(_call_arguments(app, "fetchAssignedTrend")), 1)
        load = _function_body(app, "async function loadTrendSplit(")
        self.assertIn("await fetchAssignedTrend()", load)
        self.assertLess(
            load.index("if (seq !== state.requestSeq || state.headlineDrawn "
                       "!== seq) {"),
            load.index("await fetchAssignedTrend()"))
        self.assertLess(load.index("const sent = state.splitSends;"),
                        load.index("await fetchAssignedTrend()"))
        guard = ("if (seq !== state.requestSeq || sent !== state.splitSends) "
                 "{")
        self.assertLess(load.index("await fetchAssignedTrend()"),
                        load.index(guard))
        self.assertLess(load.index(guard),
                        load.index("state.trendSplit = split;"))
        refresh_all = _function_body(app, "async function refreshAll(")
        self.assertLess(refresh_all.index("state.headlineDrawn = seq;"),
                        refresh_all.index("loadTrendSplit(seq);"))
        self.assertIn("renderTrend();", load)
        self.assertIn('getElementById("trend-card").classList.remove('
                      '"is-loading")', load)
        self.assertEqual(len(_call_arguments(app, "loadTrendSplit")), 2)
        refresh = _function_body(app, "async function refreshAll(")
        # The page-wide dim waits for the headline, the queue and the
        # browse page only; the trend card's own dim waits for the split.
        self.assertIn(
            "await Promise.all([headlinePart, queuePart, browsePart])",
            refresh)
        self.assertIn('trendCard.classList.add("is-loading")', refresh)
        self.assertIn('trendCard.classList.remove("is-loading")', refresh)
        self.assertEqual(_trend_before_headline(read("app.js")), [])
        # A new generation never draws an older scope's split.
        self.assertLess(refresh.index("state.trendSplit = null;"),
                        refresh.index("const headlinePart = "))
        coalescer = _function_body(app, "async function fetchSummary(")
        self.assertNotIn("fetchAssignedTrend", coalescer)
        loop_end = coalescer.index("summaryInFlight = null;")
        self.assertIn("loadTrendSplit(seq)", coalescer[loop_end:])
        self.assertIn("window.setTimeout(() => loadTrendSplit(seq), 0);",
                      coalescer)

    def test_the_first_paint_does_not_wait_for_it(self) -> None:
        """The headline draws the trend's totals itself (renderTrend()
        in refreshAll() after renderHeadline()), never a split."""
        app = self._app()
        headline = _function_body(app, "function renderHeadline()")
        self.assertIn("renderBars()", headline)
        self.assertNotIn("renderTrend()", headline)
        self.assertNotIn("renderCharts()", headline)
        refresh = _function_body(app, "async function refreshAll(")
        part = refresh[refresh.index("const headlinePart = "):
                       refresh.index("const queuePart = ")]
        order = [part.index(token) for token in (
            "renderHeadline();", "renderTrend();", "loadTrendSplit(seq);")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("renderTrend()", _function_body(
            app, "async function refreshSummary()"))

    def test_the_detector_can_fail(self) -> None:
        """Planted: the split back in the first batch, beside the
        headline, or before the headline has been drawn."""
        source = read("app.js")
        at = source.index("  const headlinePart = (async () => {")
        for planted in ("  const trendPart = fetchAssignedTrend();\n",
                        "  loadTrendSplit(seq);\n"):
            with self.subTest(planted=planted):
                self.assertNotEqual(_trend_before_headline(
                    source[:at] + planted + source[at:]), [])
        early = source.replace(
            "      renderHeadline();\n",
            "      loadTrendSplit(seq);\n      renderHeadline();\n", 1)
        self.assertNotEqual(early, source)
        self.assertNotEqual(_trend_before_headline(early), [])

    def test_it_never_rejects_and_never_hangs(self) -> None:
        body = _function_body(self._app(), "function fetchAssignedTrend()")
        self.assertIn("window.setTimeout(", body)
        self.assertIn("() => resolve(null), ASSIGNED_TREND_TIMEOUT_MS", body)
        self.assertIn("resolve(null); });", body)


NODE = shutil.which("node")
REPO_DIR = os.path.dirname(STATIC_DIR)

#: The shim walk: the real ES modules over tools/dev/net/domshim.mjs with
#: fetch answered from a fixture file. argv: repo root, fixture path.
_SHIM_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, "http://127.0.0.1/index.html");
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
const shape = (container) => container.children
  .filter((row) => row.className === "bar-row")
  .map((row) => {
    const track = row.children.find((c) => c.className.startsWith("bar-track"));
    return { track: track.className,
      fills: track.children.map((f) => [f.className, f.style.width]) };
  });
const out = {};
if (fixture.bars) {
  const { barRows } = await import(pathToFileURL(root + "/static/charts.js").href);
  for (const [name, call] of Object.entries(fixture.bars)) {
    const box = document.createElement("div");
    barRows(box, call.items, call.options);
    out[name] = shape(box);
  }
} else {
  const asked = [];
  globalThis.fetch = (url) => {
    const u = new URL(String(url), "http://127.0.0.1/");
    asked.push(u.searchParams.get("parts") || u.pathname);
    const part = u.searchParams.get("parts");
    let status = 200;
    let body = [];
    if (part === "headline") {
      body = fixture.headline;
    } else if (part === "assigned_trend") {
      if (fixture.part === "hang") {
        return new Promise(() => {});
      }
      status = fixture.part === null ? 400 : 200;
      body = fixture.part === null ? { error: "parts: unknown" } : fixture.part;
    } else if (part === "queue") {
      body = { generated_at: fixture.headline.generated_at,
        stale_before: fixture.headline.stale_before,
        queue: { tests: [], total: 0 } };
    } else if (u.pathname.endsWith("api/dashboard")) {
      body = { tests: [], total: 0 };
    }
    return Promise.resolve({ ok: status < 400, status,
      text: async () => JSON.stringify(body) });
  };
  await import(pathToFileURL(root + "/static/app.js").href);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const legend = (node) => {
    const row = node.children.find((c) => c.className === "chart-legend");
    return row ? row.children.map((item) => item.textContent) : null;
  };
  const classes = (node, found) => {
    for (const child of node.children || []) {
      found.push(...String(child.className || "").split(/\s+/));
      classes(child, found);
    }
    return found;
  };
  const firstRow = byId["trend-table-body"].children[0];
  Object.assign(out, {
    asked,
    tilesPainted: byId["stat-tiles"].children.length > 0,
    dimmed: Object.keys(byId).filter(
      (id) => byId[id].classList.contains("is-loading")).sort(),
    trendLegend: legend(byId["trend-chart"]),
    envLegend: legend(byId["env-chart"]),
    scriptsLegend: legend(byId["scripts-chart"]),
    envAssigned: classes(byId["env-chart"], []).includes("bar-assigned"),
    envRows: shape(byId["env-chart"]),
    envAria: byId["env-chart"].children
      .filter((c) => c.className === "bar-row")
      .map((c) => c.attributes["aria-label"] || null),
    captionHidden: byId["trend-split-caption"].hidden,
    tableRow: firstRow ? firstRow.children.map((td) => td.textContent) : null,
  });
}
await driverAnswer(out);
"""

#: Prepended by _shim() to every driver: the one way a driver answers.
#: Its JSON goes out on one line, ASCII only (the reader decodes stdout
#: in the locale's encoding, and the board's words carry "·" and "…"),
#: and the process ends only from the write's callback, once the whole
#: line has been handed to the pipe. On POSIX a pipe write is
#: asynchronous past the pipe's buffer: a process.exit() straight after
#: console.log() ends node with all but the first 64 KB of a large answer
#: unwritten. The promise it returns never settles, so ``await
#: driverAnswer(x)`` also stops a driver that answers early.
_DRIVER_PRELUDE = r"""
globalThis.driverAnswer = (value) => {
  const line = JSON.stringify(value).replace(/[\u007f-￿]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  process.stdout.write(line + "\n", () => process.exit(0));
  return new Promise(() => {});
};
"""


def _driver_endings(source: str) -> List[str]:
    """The ways *source*, a shim driver, could end node before its answer
    is out: any process.exit() or console.log() of its own (the prelude
    holds the one exit, in the write's callback), or no awaited
    driverAnswer() at all."""
    found = [token for token in ("process.exit(", "console.log(",
                                 "process.stdout.write(")
             if token in source]
    if "await driverAnswer(" not in source:
        found.append("no await driverAnswer(")
    return found


def _shim(
    fixture: Dict[str, Any], source: str = _SHIM_DRIVER,
    root: str = REPO_DIR
) -> Dict[str, Any]:
    """Run the shim driver *source* (the home page's by default) over
    *fixture*, against the static files under *root* (the repository's,
    or a planted copy); its JSON answer, typed at the boundary by the
    caller's assertions. The driver answers through _DRIVER_PRELUDE's
    driverAnswer(), so an answer of any size arrives whole."""
    with tempfile.TemporaryDirectory() as scratch:
        driver = os.path.join(scratch, "driver.mjs")
        data = os.path.join(scratch, "fixture.json")
        with io.open(driver, "w", encoding="utf-8") as handle:
            handle.write(_DRIVER_PRELUDE + source)
        with io.open(data, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(fixture))
        done = subprocess.run(
            [str(NODE), driver, root, data], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError("shim driver failed: " + done.stderr[-2000:])
    answer = json.loads(done.stdout.strip().split("\n")[-1])  # type: Dict[str, Any]
    return answer


#: A driver whose answer is as large as its fixture says, with one
#: non-ASCII character at the end. argv: repo root, fixture path.
_BIG_ANSWER_DRIVER = r"""
import { readFileSync } from "node:fs";
const fixture = JSON.parse(readFileSync(process.argv[3], "utf-8"));
await driverAnswer({ size: fixture.size,
  big: "x".repeat(fixture.size) + "·" });
"""


def _drivers() -> Dict[str, str]:
    """Every shim driver in this module, by name."""
    return {name: value for name, value in sorted(globals().items())
            if name.endswith("_DRIVER") and isinstance(value, str)}


class ShimAnswerTest(unittest.TestCase):
    """A shim driver's answer arrives whole, whatever its size. On POSIX
    node writes to a pipe asynchronously past the pipe's 64 KB buffer, so
    a driver that ended with ``console.log(...); process.exit(0)`` handed
    the reader its first 65,536 characters and nothing more. Every driver
    now answers through _DRIVER_PRELUDE's driverAnswer(), which exits only
    from the write's callback."""

    def test_every_driver_answers_through_the_prelude(self) -> None:
        drivers = _drivers()
        self.assertGreaterEqual(len(drivers), 8, sorted(drivers))
        self.assertEqual(
            {name: found for name, found in
             ((name, _driver_endings(source))
              for name, source in drivers.items()) if found}, {})

    def test_the_prelude_ends_node_only_once_the_line_is_written(
            self) -> None:
        self.assertEqual(_DRIVER_PRELUDE.count("process.exit("), 1)
        self.assertIn('process.stdout.write(line + "\\n", () => '
                      'process.exit(0));', _DRIVER_PRELUDE)
        self.assertNotIn("console.log(", _DRIVER_PRELUDE)
        self.assertIn("return new Promise(() => {});", _DRIVER_PRELUDE)

    def test_the_detector_can_fail(self) -> None:
        """Planted: the ending that cut answers at 64 KB, an exit of its
        own anywhere, and a driver that never answers."""
        for planted in (
                "console.log(JSON.stringify(out));\nprocess.exit(0);\n",
                "await driverAnswer(out);\nprocess.exit(0);\n",
                'process.stdout.write(JSON.stringify(out));\n'
                'await driverAnswer(out);\n',
                "const out = {};\n"):
            with self.subTest(planted=planted):
                self.assertNotEqual(_driver_endings(planted), [])
        self.assertEqual(_driver_endings("await driverAnswer(out);\n"), [])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_a_300_kb_answer_arrives_whole(self) -> None:
        size = 300 * 1024
        answer = _shim({"size": size}, _BIG_ANSWER_DRIVER)
        self.assertEqual(answer["size"], size)
        self.assertEqual(len(answer["big"]), size + 1)
        self.assertEqual(answer["big"][-2:], "x·")


#: The class lists and widths ``barRows`` drew at the base commit
#: (the pre-WP-43 charts.js) for a caller passing neither ``segments`` nor
#: ``extra``, and for the WP-40 ``extra`` caller; captured over the shim
#: from that commit's charts.js. One path draws both now, and it must
#: draw exactly these.
_BASE_BARS = {
    "plain": {
        "items": [{"label": "a", "value": 5}, {"label": "b", "value": 0},
                  {"label": "c", "value": 1}],
        "options": {},
        "drawn": [
            {"track": "bar-track", "fills": [["bar-fill fill-fail", "100%"]]},
            {"track": "bar-track", "fills": [["bar-fill fill-fail", "0%"]]},
            {"track": "bar-track", "fills": [["bar-fill fill-fail", "20%"]]},
        ],
    },
    "extra": {
        "items": [{"label": "a", "value": 7, "extra": 3},
                  {"label": "b", "value": 0, "extra": 2},
                  {"label": "c", "value": 4, "extra": 0},
                  {"label": "d", "value": 1}],
        "options": {},
        "drawn": [
            {"track": "bar-track bar-track-split",
             "fills": [["bar-fill fill-fail bar-fill-open", "70%"],
                       ["bar-fill bar-muted", "30%"]]},
            {"track": "bar-track bar-track-split",
             "fills": [["bar-fill fill-fail bar-fill-open", "0%"],
                       ["bar-fill bar-muted", "20%"]]},
            {"track": "bar-track", "fills": [["bar-fill fill-fail", "40%"]]},
            {"track": "bar-track", "fills": [["bar-fill fill-fail", "10%"]]},
        ],
    },
    "fillClass": {
        "items": [{"label": "a", "value": 6}],
        "options": {"fillClass": "fill-time"},
        "drawn": [
            {"track": "bar-track", "fills": [["bar-fill fill-time", "100%"]]},
        ],
    },
}  # type: Dict[str, Dict[str, Any]]


def _index_ids() -> List[str]:
    return re.findall(r'id="([^"]+)"', read("index.html"))


def _headline(assigned: bool) -> Dict[str, Any]:
    """A small headline payload in the server's shape; ``assigned=False``
    is a server from before WP-43."""
    nights = [
        {"date": "2026-10-0" + str(day), "PASS": 50, "FAIL": 9,
         "FAILED_AS_EXPECTED": 1, "UNEXPECTED_PASS": 2, "total": 62}
        for day in (1, 2, 3)
    ]
    environments = [
        {"environment": "env-a", "total_tests": 80, "failed": 14,
         "new_failures": 3, "unexpected_passes": 1, "not_run": 0,
         "muted": 4, "assigned": 6},
        {"environment": "env-b", "total_tests": 40, "failed": 0,
         "new_failures": 0, "unexpected_passes": 0, "not_run": 0,
         "muted": 2, "assigned": 0},
    ]
    scripts_rows = [{"environment": "env-a", "script": "s1.py",
                     "failing": 5, "muted": 1, "assigned": 2}]
    if not assigned:
        for entry in environments + scripts_rows:
            del entry["assigned"]
    return {
        "generated_at": "2026-10-03T12:00:00", "environment": None,
        "product": None, "stream": 1, "covered_passes": 0, "products": [],
        "environments": ["env-a", "env-b"], "scripts": ["s1.py"],
        "assignees": [], "assignment_streams": [], "recent_hours": 36,
        "stale_before": "2026-10-02T00:00:00",
        "latest_run_time": "2026-10-03T04:00:00",
        "environment_updated": {"env-a": "2026-10-03T04:00:00",
                                "env-b": "2026-10-03T04:00:00"},
        "queue_cap": 500,
        "status": {
            "total_tests": 120, "ran_recently": 120, "not_run": 0,
            "retired": 0,
            "results": {"PASS": 98, "FAIL": 14, "FAILED_AS_EXPECTED": 6,
                        "UNEXPECTED_PASS": 2},
            "recent_results": {"PASS": 98, "FAIL": 14,
                               "FAILED_AS_EXPECTED": 6,
                               "UNEXPECTED_PASS": 2},
            "new_failures": 3, "still_failing": 11, "fixed": 0,
            "assigned_open": 6, "muted": 6, "muted_new_failures": 0,
            "muted_still_failing": 6, "muted_total": 6},
        "trend": {"days": 3, "from": "2026-10-01", "to": "2026-10-03",
                  "nights": nights},
        "by_environment": environments,
        "top_failing_scripts": scripts_rows,
        "queue_totals": {"new_failures": 3, "still_failing": 11,
                         "muted": 6, "fixed": 0, "unexpected_passes": 2,
                         "not_run": 0, "assigned": 6, "mine": 0},
    }


def _part() -> Dict[str, Any]:
    return {"trend": {"days": 3, "from": "2026-10-01", "to": "2026-10-03",
                      "nights": [
                          {"date": "2026-10-0" + str(day),
                           "FAIL_ASSIGNED": 3, "FAIL_MUTED": 2}
                          for day in (1, 2, 3)]}}


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class AssignedSplitDomTest(unittest.TestCase):
    """WP-43 over the DOM shim (``tools/dev/net/domshim.mjs``): the real
    modules, run in node where it is installed, with fetch answered from
    a fixture. No browser; it proves DOM shape and wiring, not colour."""

    def test_barrows_draws_the_base_dom_for_legacy_callers(self) -> None:
        answer = _shim({"ids": [], "bars": {
            name: {"items": case["items"], "options": case["options"]}
            for name, case in _BASE_BARS.items()}})
        for name, case in _BASE_BARS.items():
            self.assertEqual(answer[name], case["drawn"], name)

    def test_segments_skip_later_zeros_and_open_all_but_the_last(
            self) -> None:
        answer = _shim({"ids": [], "bars": {"split": {"options": {}, "items": [
            {"label": "a", "value": 10, "segments": [
                {"value": 4, "className": "fill-fail"},
                {"value": 6, "className": "bar-assigned"},
                {"value": 0, "className": "bar-muted"}]},
            {"label": "b", "value": 0, "segments": [
                {"value": 0, "className": "fill-fail"},
                {"value": 0, "className": "bar-assigned"},
                {"value": 5, "className": "bar-muted"}]},
            {"label": "c", "value": 2, "segments": [
                {"value": 0, "className": "fill-fail"},
                {"value": 2, "className": "bar-assigned"},
                {"value": 0, "className": "bar-muted"}]},
        ]}}})
        self.assertEqual(answer["split"], [
            {"track": "bar-track bar-track-split",
             "fills": [["bar-fill fill-fail bar-fill-open", "40%"],
                       ["bar-fill bar-assigned", "60%"]]},
            {"track": "bar-track bar-track-split",
             "fills": [["bar-fill fill-fail bar-fill-open", "0%"],
                       ["bar-fill bar-muted", "50%"]]},
            {"track": "bar-track bar-track-split",
             "fills": [["bar-fill fill-fail bar-fill-open", "0%"],
                       ["bar-fill bar-assigned", "20%"]]},
        ])

    def test_an_older_server_draws_todays_charts(self) -> None:
        """No ``assigned`` and a 400 for the part: two series, the
        four-column table, no blue, no bar legend, the WP-40 bars."""
        answer = _shim({"ids": _index_ids(),
                        "headline": _headline(False), "part": None})
        self.assertEqual(answer["trendLegend"], ["Failed", "Unexpected pass"])
        self.assertEqual(answer["tableRow"], ["Oct 1", "9", "2", "62"])
        self.assertTrue(answer["captionHidden"])
        self.assertFalse(answer["envAssigned"])
        self.assertIsNone(answer["envLegend"])
        self.assertIsNone(answer["scriptsLegend"])
        self.assertEqual(answer["envAria"], [None, None])
        self.assertEqual(
            [fill[0] for fill in answer["envRows"][0]["fills"]],
            ["bar-fill fill-fail bar-fill-open", "bar-fill bar-muted"])

    def test_the_split_draws_in_all_three_charts(self) -> None:
        answer = _shim({"ids": _index_ids(),
                        "headline": _headline(True), "part": _part()})
        self.assertEqual(answer["trendLegend"], [
            "Unassigned", "Assigned", "Muted", "Unexpected pass"])
        self.assertEqual(answer["tableRow"],
                         ["Oct 1", "9", "4", "3", "2", "2", "62"])
        self.assertFalse(answer["captionHidden"])
        self.assertEqual(answer["envLegend"], ["Unassigned", "Assigned", "Muted"])
        self.assertEqual(answer["scriptsLegend"],
                         ["Unassigned", "Assigned", "Muted"])
        self.assertEqual(answer["envAria"], [
            "env-a: 14 failing, 8 unassigned, 6 assigned, 4 muted",
            "env-b: 0 failing, 2 muted"])
        self.assertEqual(
            [fill[0] for fill in answer["envRows"][0]["fills"]],
            ["bar-fill fill-fail bar-fill-open",
             "bar-fill bar-assigned bar-fill-open", "bar-fill bar-muted"])

    def test_a_failed_part_costs_the_trend_its_split_only(self) -> None:
        answer = _shim({"ids": _index_ids(),
                        "headline": _headline(True), "part": None})
        self.assertEqual(answer["trendLegend"], ["Failed", "Unexpected pass"])
        self.assertEqual(answer["envLegend"], ["Unassigned", "Assigned", "Muted"])

    def test_a_hung_part_holds_up_nothing_but_the_trend(self) -> None:
        """The fetch-sequencing gate, behaviourally. The pin is now: the
        part is never on the first paint, so it is asked for after the
        three requests of the first batch, once the headline has been
        drawn; while it hangs the tiles and both bar charts paint, and
        the trend shows the headline's totals and keeps its dim."""
        answer = _shim({"ids": _index_ids(),
                        "headline": _headline(True), "part": "hang"})
        asked = answer["asked"]
        self.assertIn("assigned_trend", asked)
        self.assertLess(asked.index("headline"), asked.index("queue"))
        for first in ("headline", "queue", "/api/dashboard"):
            self.assertLess(asked.index(first), asked.index("assigned_trend"))
        self.assertEqual(asked.count("assigned_trend"), 1)
        self.assertTrue(answer["tilesPainted"])
        self.assertEqual(answer["envLegend"], ["Unassigned", "Assigned", "Muted"])
        self.assertEqual(answer["trendLegend"], ["Failed", "Unexpected pass"])
        # Only the trend card is still dimmed; every section has lifted.
        self.assertEqual(answer["dimmed"], ["trend-card"])

    def test_both_inputs_in_lift_every_dim(self) -> None:
        answer = _shim({"ids": _index_ids(),
                        "headline": _headline(True), "part": _part()})
        self.assertEqual(answer["dimmed"], [])


def _call_arguments(source: str, callee: str) -> List[str]:
    """The argument text of every ``callee(...)`` call, paren-matched.

    A ``function callee(`` declaration is not a call and is skipped.
    """
    found = []  # type: List[str]
    for match in re.finditer(r"\b" + re.escape(callee) + r"\s*\(", source):
        if source[max(0, match.start() - 9):match.start()] == "function ":
            continue
        depth = 0
        for index in range(match.end() - 1, len(source)):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    found.append(source[match.end():index].strip())
                    break
    return found


#: A copy getter may only hand over the API's string: ``run.output`` /
#: ``full.output`` directly, or a local ``output`` the viewer assigned
#: from one (checked separately). Anything else -- ``pre.textContent``,
#: ``innerText``, a slice -- is the truncated tail, not the output.
_FULL_OUTPUT_GETTER_RE = re.compile(r"^\(\)\s*=>\s*(?:\w+\.)?output$")

#: What only copyOutputButton() may say: a second copy path, or a
#: second button wearing its label.
_COPY_TOKENS = ("clipboard", "execCommand", '"Copy output"')


def _copy_tokens_outside_helper(name: str, source: str,
                                helper: str) -> List[str]:
    """The copy tokens *source* uses outside copyOutputButton().

    ``helper`` is the helper's comment-stripped body; it is only
    subtracted for api.js, where the label is also a module constant.
    """
    code = _strip_comments(source)
    offenders = []  # type: List[str]
    for token in _COPY_TOKENS:
        outside = code.count(token)
        if name == "api.js":
            outside -= helper.count(token)
            if token == '"Copy output"':
                outside -= code.count(
                    'const COPY_OUTPUT_LABEL = "Copy output"')
        if outside > 0:
            offenders.append(token)
    return offenders


class CopyOutputTest(unittest.TestCase):
    """One Copy button, on every output viewer, copying the WHOLE output.

    fillOutput() puts at most the last MAX_INLINE_OUTPUT characters in
    the ``<pre>``, so selecting it by hand never gives a long output in
    full -- which is the one thing the button is for. The button is
    therefore handed the string the viewer fetched from the API, and a
    getter that read the ``<pre>`` would look like it works on every
    short output while silently copying a tail on exactly the long ones.
    """

    #: Viewer -> how many Copy buttons it builds, and inside which
    #: functions. Four viewers in three files; no other page shows output.
    _VIEWERS = {
        "review.js": ["async function buildReviewPanel("],
        "timeline.js": ["function renderDetail("],
        "test.js": ["async function showRunOutput(",
                    "function appendHistoryRow("],
    }

    def test_the_helper_is_defined_in_exactly_one_file(self) -> None:
        self.assertIn("export function copyOutputButton(getText)",
                      read("api.js"))
        offenders = sorted(
            name for name, source in scripts().items()
            if name != "api.js" and "function copyOutputButton" in source)
        self.assertEqual(offenders, [])

    def test_only_the_helper_touches_the_clipboard(self) -> None:
        """No second copy path, and nothing else building a copy button."""
        helper = _strip_comments(_function_body(
            read("api.js"), "export function copyOutputButton("))
        self.assertIn("navigator.clipboard.writeText(text)", helper)
        self.assertIn('document.execCommand("copy")', helper)
        offenders = {
            name: _copy_tokens_outside_helper(name, source, helper)
            for name, source in scripts().items()
        }
        self.assertEqual(
            {name: found for name, found in offenders.items() if found}, {},
            "copy tokens used outside copyOutputButton()")

    def test_every_output_viewer_has_the_button(self) -> None:
        for name, functions in sorted(self._VIEWERS.items()):
            source = read(name)
            self.assertIn("copyOutputButton", _imported_names(source), name)
            code = _strip_comments(source)
            self.assertEqual(
                len(_call_arguments(code, "copyOutputButton")),
                len(functions), name)
            for signature in functions:
                body = _function_body(code, signature)
                self.assertTrue(body, signature)
                self.assertIn("copyOutputButton(", body, signature)

    def test_every_file_rendering_output_offers_the_copy(self) -> None:
        """A fifth viewer cannot arrive without the button."""
        callers = sorted(
            name for name, source in scripts().items()
            if name != "api.js"
            and re.search(r"\bfillOutput\s*\(", _strip_comments(source)))
        self.assertEqual(callers, ["review.js", "test.js", "timeline.js"])
        # No dead imports either: whoever imports fillOutput calls it.
        importers = sorted(
            name for name, source in scripts().items()
            if "fillOutput" in _imported_names(source))
        self.assertEqual(importers, callers)
        for name in callers:
            self.assertIn("copyOutputButton", _imported_names(read(name)),
                          name)

    def test_no_copy_path_reads_an_output_pre(self) -> None:
        for name, functions in sorted(self._VIEWERS.items()):
            code = _strip_comments(read(name))
            for signature in functions:
                body = _function_body(code, signature)
                for getter in _call_arguments(body, "copyOutputButton"):
                    self.assertRegex(getter, _FULL_OUTPUT_GETTER_RE,
                                     name + " " + signature)
                    if getter.endswith("> output"):
                        # A local: it must be assigned from the API's
                        # response, and from nothing else.
                        assigned = re.findall(
                            r"(?<!let )\boutput = ([^;]+);", body)
                        self.assertTrue(assigned, signature)
                        for value in assigned:
                            self.assertRegex(value, r"^\w+\.output$",
                                             signature)
        helper = _strip_comments(_function_body(
            read("api.js"), "export function copyOutputButton("))
        self.assertIn("const text = getText();", helper)
        self.assertNotIn("innerText", helper)
        # The helper only ever WRITES textContent (its own label).
        self.assertEqual(
            re.findall(r"textContent(?!\s*=[^=])", helper), [])

    def test_disabled_until_the_full_text_is_in_hand(self) -> None:
        helper = _strip_comments(_function_body(
            read("api.js"), "export function copyOutputButton("))
        self.assertIn('el("button", "review-btn"', helper)
        self.assertIn('button.type = "button"', helper)
        self.assertIn("button.disabled = true;", helper)
        for name, functions in sorted(self._VIEWERS.items()):
            code = _strip_comments(read(name))
            for signature in functions:
                body = _function_body(code, signature)
                self.assertRegex(
                    body, r"copy\.disabled = !(?:\w+\.)?output;", signature)
        self.assertIn(".review-btn:disabled", read("style.css"))

    def test_the_timeline_button_is_not_in_the_clickable_run_row(self) -> None:
        """The run row toggles its output on click; the button is elsewhere.

        It lives in the output row, which has no click handler, so a
        click on Copy can never collapse the output it is copying.
        """
        body = _strip_comments(_function_body(
            read("timeline.js"), "function renderDetail("))
        self.assertIn("outputHead.appendChild(copy);", body)
        self.assertIn("outputCell.appendChild(outputHead);", body)
        self.assertNotIn("tr.appendChild(copy", body)
        self.assertNotIn("outputRow.addEventListener", body)


class PlantedCopyOutputRegressionTest(unittest.TestCase):
    """Prove the copy detectors can fail."""

    def test_a_getter_reading_the_pre_would_be_caught(self) -> None:
        for planted in ("() => pre.textContent", "() => pre.innerText",
                        "() => output.slice(-20000)"):
            self.assertNotRegex(planted, _FULL_OUTPUT_GETTER_RE)
        self.assertRegex("() => run.output", _FULL_OUTPUT_GETTER_RE)

    def test_a_second_clipboard_call_would_be_caught(self) -> None:
        """The real guard's counting, over planted sources."""
        helper = _strip_comments(_function_body(
            read("api.js"), "export function copyOutputButton("))
        planted = (
            "btn.addEventListener('click', () => {\n"
            "  navigator.clipboard.writeText(pre.textContent);\n"
            "});\n")
        self.assertEqual(
            _copy_tokens_outside_helper("timeline.js", planted, helper),
            ["clipboard"])
        # In api.js itself, but outside the helper.
        self.assertEqual(
            _copy_tokens_outside_helper(
                "api.js", read("api.js") + "\n" + planted, helper),
            ["clipboard"])
        self.assertEqual(
            _copy_tokens_outside_helper(
                "test.js", 'const b = el("button", "x", "Copy output");',
                helper),
            ['"Copy output"'])
        # And the shipped api.js, helper included, is clean.
        self.assertEqual(
            _copy_tokens_outside_helper("api.js", read("api.js"), helper), [])

    def test_call_arguments_are_paren_matched(self) -> None:
        planted = ("function copyOutputButton(getText) {}\n"
                   "const a = copyOutputButton(() => f(run).output);\n")
        self.assertEqual(_call_arguments(planted, "copyOutputButton"),
                         ["() => f(run).output"])


#: The Open Actions walk: the real actions.js over the shim, fetch
#: answered from the fixture and every request recorded. It loads the
#: page, presses load-more, changes the environment filter, then presses
#: Reload, and reports what was asked for at each stage. argv: repo root,
#: fixture path.
_ACTIONS_SHIM_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom, Element } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, "http://127.0.0.1/actions.html");
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
// The markup marks the switcher host-managed, nests "Extend all" in a
// header the duration select is inserted into, and init() swaps its
// username slot for a picker; the shim parses no HTML and has no
// replaceWith, so all three are supplied here as the page would find
// them.
byId["product-switcher"].setAttribute("data-host-managed", "");
new Element("div").appendChild(byId["extend-all-btn"]);
Element.prototype.replaceWith = function (node) {
  if (node.id) byId[node.id] = node;
};
let namesFailures = fixture.namesFailures;
const asked = [];
globalThis.fetch = (url) => {
  const u = new URL(String(url), "http://127.0.0.1/");
  const part = u.searchParams.get("parts");
  const path = u.pathname.replace(/^\//, "");
  asked.push(part ? path + "?parts=" + part : path);
  let status = 200;
  let body = {};
  if (path === "api/summary") {
    if (namesFailures > 0) {
      namesFailures -= 1;
      status = 500;
      body = { error: "names unavailable" };
    } else {
      body = fixture.names;
    }
  } else if (path === "api/dashboard") {
    body = { tests: [fixture.row], total: 1, streams: fixture.streams || {} };
  } else if (path === "api/mutes") {
    body = { mutes: [] };
  } else if (path === "api/users") {
    body = { users: [] };
  }
  return Promise.resolve({ ok: status < 400, status,
    text: async () => JSON.stringify(body) });
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
const banner = () => (byId["error-banner"].hidden
  ? null : byId["error-banner"].textContent);
await import(pathToFileURL(root + "/static/actions.js").href);
await settle();
// The first row's edge and its result cell's chip and pills (WP-44).
const firstTr = byId["actions-body"].children[0];
const resultTd = firstTr ? firstTr.children.find((td) => td.children.some(
  (c) => String(c.className).startsWith("chip"))) : null;
const out = {
  first: asked.slice(),
  firstBanner: banner(),
  firstRows: byId["actions-body"].children.length,
  envOptions: byId["filter-environment"].children.map((o) => o.value),
  firstRow: resultTd ? [firstTr.className].concat(resultTd.children.map(
    (c) => c.className + ":" + c.textContent)) : null,
};
await byId["show-more"].click();
await settle();
Object.assign(out, {
  more: asked.slice(out.first.length),
  moreBanner: banner(),
  moreRows: byId["actions-body"].children.length,
  moreEnvOptions: byId["filter-environment"].children.map((o) => o.value),
});
const beforeLater = asked.length;
byId["filter-environment"].value = fixture.names.environments[0];
await byId["filter-environment"].dispatch("change");
await settle();
await byId["reload-btn"].click();
await settle();
Object.assign(out, {
  later: asked.slice(beforeLater),
  laterBanner: banner(),
  laterRows: byId["actions-body"].children.length,
  laterEnvOptions: byId["filter-environment"].children.map((o) => o.value),
});
await driverAnswer(out);
"""


def _actions_ids() -> List[str]:
    return re.findall(r'id="([^"]+)"', read("actions.html"))


def _names_part(assignees: List[str]) -> Dict[str, Any]:
    """A ``parts=names`` answer in the server's shape (WP-44 item 1)."""
    return {
        "generated_at": "2026-11-11T12:00:00", "environment": None,
        "product": None, "stream": 1, "products": [],
        "environments": ["env-a", "env-b"], "assignees": assignees,
        "assignment_streams": [], "stale_before": "2026-11-10T00:00:00",
    }


#: One Open Actions row in ``/api/dashboard``'s shape (with_comment,
#: with_streak), as the play server returned it.
_ACTIONS_ROW = {
    "environment": "env-a", "product": "", "script": "s1.py",
    "test_name": "test_one", "run_id": 7, "result": "FAIL",
    "start_time": "2026-11-11T02:07:13", "end_time": "2026-11-11T02:07:17",
    "duration_seconds": 3.4, "known_failure_reason": None,
    "source_link": None, "assignee": None, "assignment_stream_id": None,
    "retired_at": None, "retired_by": None, "mute": None,
    "latest_comment": None, "failing_since": "2026-11-09T02:07:37",
    "last_pass_time": "2026-11-08T02:07:04",
    "stability": {"classification": "no-data", "transitions": 0,
                  "score": 0.0, "runs": 0, "recent_results": []},
}  # type: Dict[str, Any]


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class OpenActionsCatalogueDomTest(unittest.TestCase):
    """WP-44 over the DOM shim: the real actions.js, its requests
    recorded through a load, a filter change and a Reload. No browser;
    it proves which requests are made and what is drawn, not how."""

    def _walk(self, assignees: List[str], failures: int) -> Dict[str, Any]:
        return _shim({"ids": _actions_ids(), "names": _names_part(assignees),
                      "row": _ACTIONS_ROW, "namesFailures": failures},
                     _ACTIONS_SHIM_DRIVER)

    def test_first_paint_asks_for_names_then_rows_then_expiring(self) -> None:
        answer = self._walk(["alice"], 0)
        self.assertEqual(
            [entry for entry in answer["first"] if entry != "api/users"],
            ["api/summary?parts=names", "api/dashboard", "api/mutes"])
        self.assertEqual(answer["envOptions"], ["", "env-a", "env-b"])
        self.assertEqual(answer["firstRows"], 1)
        self.assertIsNone(answer["firstBanner"])

    def test_nobody_assigned_still_fetches_the_catalogue_once(self) -> None:
        """The old gate, ``state.assignees.length``, refetched it on the
        filter change and on Reload when nobody was assigned."""
        answer = self._walk([], 0)
        self.assertEqual(answer["first"].count("api/summary?parts=names"), 1)
        self.assertNotIn("api/summary?parts=names", answer["later"])
        self.assertEqual(answer["later"].count("api/dashboard"), 2)
        self.assertEqual(answer["laterEnvOptions"], ["", "env-a", "env-b"])

    def test_load_more_with_the_catalogue_loaded_is_one_request(self) -> None:
        for assignees in (["alice"], []):
            with self.subTest(assignees=assignees):
                answer = self._walk(assignees, 0)
                self.assertEqual(answer["more"], ["api/dashboard"])
                self.assertEqual(answer["moreRows"], 2)

    def test_a_failed_catalogue_shows_its_message_draws_rows_and_retries(
            self) -> None:
        """The first refresh after the failure asks again, a load-more
        included, and lands it; nothing after that asks."""
        answer = self._walk(["alice"], 1)
        self.assertEqual(answer["firstBanner"], "names unavailable")
        self.assertEqual(answer["firstRows"], 1)
        self.assertEqual(answer["envOptions"], [],
                         "nothing to fill the filter from yet")
        self.assertEqual(answer["more"],
                         ["api/summary?parts=names", "api/dashboard"])
        self.assertIsNone(answer["moreBanner"])
        self.assertEqual(answer["moreRows"], 2)
        self.assertEqual(answer["moreEnvOptions"], ["", "env-a", "env-b"])
        self.assertNotIn("api/summary?parts=names", answer["later"])
        self.assertIsNone(answer["laterBanner"])

    def test_load_more_never_hides_a_catalogue_that_is_still_missing(
            self) -> None:
        """Found by review: load-more's clearError() wiped the message
        and, the gate skipping the catalogue on an append, left a page
        that looked healthy with no filters. Failing twice, the message
        is still up after load-more, the rows appended."""
        answer = self._walk(["alice"], 2)
        self.assertEqual(answer["more"],
                         ["api/summary?parts=names", "api/dashboard"])
        self.assertEqual(answer["moreBanner"], "names unavailable")
        self.assertEqual(answer["moreRows"], 2)
        self.assertEqual(answer["moreEnvOptions"], [])
        self.assertEqual(answer["later"].count("api/summary?parts=names"), 1)
        self.assertEqual(answer["later"][0], "api/summary?parts=names")
        self.assertIsNone(answer["laterBanner"])
        self.assertEqual(answer["laterEnvOptions"], ["", "env-a", "env-b"])


#: The words a flaky mark is spoken in (WP-44, docs/design/flaky-mark-r0.md
#: §3 U2), matched case-insensitively inside string literals: FLAKING and
#: the sentences that say where a marked test stands, and "flips", the
#: computed signal's word since decision 6 gave "flaky" to the mark.
_FLAKY_WORDS = ("flaking", "marked flaky", "flips")

#: Any one-line JavaScript string literal, in either quote, or a template.
_STRING_LITERAL_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`')


def _flaky_wording_offenders(source: str) -> List[str]:
    """The string literals in *source*'s code that spell a flaky word,
    or start the way cadenceWords() ends (" in a row", someone rebuilding
    "3 in a row" by hand)."""
    found = []  # type: List[str]
    for literal in _STRING_LITERAL_RE.findall(_strip_comments(source)):
        text = literal[1:-1].lower()
        if (any(word in text for word in _FLAKY_WORDS)
                or re.match(r"\s*in a row", text)):
            found.append(literal[1:-1])
    return found


#: The helpers the surfaces call (U2's list as built so far, item 5's
#: then item 7's; the rest of U2 arrives with the items that call it).
_FLAKY_HELPERS = (
    "displayResult", "flakyTag", "flakySentence", "flakyDetail",
    "flakyMarkWords", "cadenceWords", "cadenceSentence",
    "flakyCadenceSelect", "stabilityWord",
    "rowResult", "previousShown", "setAsideSuffix", "setAsideBracket",
    "failingWithSetAside", "setAsideWords", "newFailuresSub", "failingSplit",
    # The Flaking tab's and the browse filter's. (WP-44 item 15:
    # failedOfRuns() went with the tab's "Failed" column, and the Streak
    # column's flakyStreakWords() takes its place.)
    "flakyStreakWords", "failToggleTitle",
)


class FlakyWordingTest(unittest.TestCase):
    """WP-44: every word about a flaky mark is made in api.js and nowhere
    else, so the selection bar, the Review panel, the test page and the
    lists to come cannot drift apart. FLAKING is a displayed result made
    by one helper; the wire, sorting and filters keep FAIL."""

    def test_the_flaky_words_are_spelled_only_in_api_js(self) -> None:
        offenders = {
            name: _flaky_wording_offenders(source)
            for name, source in scripts().items() if name != "api.js"
        }
        self.assertEqual(
            {name: found for name, found in offenders.items() if found}, {},
            "flaky wording outside api.js; call its helpers instead")
        # And api.js does spell them, or the scan proves nothing.
        api = _flaky_wording_offenders(read("api.js"))
        for needle in ("FLAKING", "Flaking: ", "Marked flaky, ",
                       " in a row", "flips about 1 run in ", "flips often"):
            self.assertTrue(any(needle in text for text in api), needle)

    def test_the_detector_can_fail(self) -> None:
        """Planted: each way a page could make the words itself."""
        for planted in (
                'resultCell.appendChild(el("span", "chip", "FLAKING"));',
                'if (entry.flaky && entry.flaky.state === "flaking") {}',
                "const words = cadence + ' in a row';",
                'line.textContent = "Marked flaky, " + n;',
                'return "flaky — flips about 1 run in " + period;'):
            self.assertTrue(_flaky_wording_offenders(planted), planted)
        self.assertEqual(_flaky_wording_offenders(
            '// FLAKING is made in api.js\nconst a = "Mark as flaky";'), [])

    def test_displayResult_is_the_one_place_FLAKING_is_made(self) -> None:
        api = _strip_comments(read("api.js"))
        body = _function_body(
            api, "export function displayResult(result, flakyState, muted)")
        self.assertIn('"FLAKING"', body)
        self.assertIn('flakyState === "flaking"', body)
        self.assertIn("!muted", body)
        # It returns a label; it never rewrites the entry it read.
        self.assertIsNone(re.search(r"\.result\s*=[^=]", body))
        allowed = sum(
            _function_body(api, signature).count('"FLAKING"')
            for signature in ("export function displayResult(",
                              "export function resultClass(",
                              "export function flakyTag("))
        self.assertEqual(api.count('"FLAKING"'), allowed)
        self.assertIn('case "FLAKING": return "result-flaking";',
                      _function_body(api, "export function resultClass("))

    def test_every_flaky_helper_has_a_caller(self) -> None:
        """No dead helpers: each one this item writes is called."""
        code = {name: _strip_comments(source)
                for name, source in scripts().items()}
        api_exports = _exported_names(read("api.js"))
        for helper in _FLAKY_HELPERS:
            self.assertIn(helper, api_exports, helper)
            calls = sum(
                len(_call_arguments(source, helper))
                for source in code.values())
            self.assertGreater(calls, 0, helper + " has no caller")

    def test_the_computed_signal_says_flips(self) -> None:
        """Decision 6: "flaky" on screen means the mark."""
        api = _strip_comments(read("api.js"))
        sentence = _function_body(api, "export function stabilitySentence(")
        self.assertIn('"flips about 1 run in "', sentence)
        self.assertNotIn("flaky —", sentence)
        self.assertIn('"flips often"',
                      _function_body(api, "export function stabilityWord("))
        card = _function_body(
            _strip_comments(read("test.js")), "function renderFlakiness(")
        self.assertIn("stabilityWord(flakiness.classification)", card)
        self.assertNotIn('"big", flakiness.classification', card)

    def test_the_two_tokens_and_the_flaking_class(self) -> None:
        css = read_text("style.css")
        root = css[css.index(":root {"):css.index("}", css.index(":root {"))]
        self.assertIn("--c-flaking: #a66993;", root)
        self.assertIn("--c-flaking-strong: #8f638b;", root)
        # Whole rules, selector groups and all: each selector naming
        # .result-flaking, with the declarations it takes.
        rules = {}  # type: Dict[str, str]
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}",
                                 re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
            for selector in match.group(1).split(","):
                selector = selector.strip()
                if "result-flaking" in selector:
                    rules[selector] = match.group(2)
        ink = [selector for selector in rules
               if selector.startswith('.toggle[aria-pressed="true"]:not(')]
        # Widened for WP-44 item 7: the outlined chip of a promotion
        # row's "FLAKING → FAIL" joins them, in the same strong step;
        # the browse filter: the pressed FLAKING toggle and its dot, and the pressed
        # ink rule, which names it only to leave it out.
        self.assertEqual(len(ink), 1)
        self.assertTrue(ink[0].endswith(":not(.result-flaking)"))
        del rules[ink[0]]
        self.assertEqual(sorted(rules),
                         [".chip.chip-ghost.result-flaking",
                          ".chip.result-flaking",
                          ".toggle.result-flaking::before",
                          '.toggle.result-flaking[aria-pressed="true"]',
                          "tr.result-flaking"])
        for selector, body in rules.items():
            # Chips, row edges and the toggle take the strong step, never
            # the chart's.
            self.assertIn("var(--c-flaking-strong)", body, selector)
            self.assertNotIn("var(--c-flaking)", body, selector)
        for selector in (".chip.result-flaking",
                         '.toggle.result-flaking[aria-pressed="true"]'):
            self.assertIn("color: #fff", rules[selector], selector)


#: Spellings that would name an owner for a mark (decision 12): the
#: person marking owns it, so a flaky act never reads the bar's
#: "Assign to" box nor sends an assignee.
_FLAKY_OWNER_TOKENS = ("userSelectEl", "userPickerSelect", "assignee",
                       "marked_by")


def _owner_tokens(body: str) -> List[str]:
    return [token for token in _FLAKY_OWNER_TOKENS if token in body]


def _offer_tints(body: str) -> List[str]:
    """The declarations in a CSS rule *body* that would tint the test
    page's flaky offer like the mark (WP-44 item 17): any background
    other than ``none``, or anything in the flaking colour."""
    found = []  # type: List[str]
    for declaration in body.split(";"):
        name, _colon, value = declaration.partition(":")
        name, value = name.strip(), value.strip()
        if ((name.startswith("background") and value != "none")
                or "flaking" in value):
            found.append(name + ": " + value)
    return found


class FlakyMarkControlsTest(unittest.TestCase):
    """WP-44 item 5: mark, change the cadence and unmark from the
    selection bar, the Review panel and the test page, each built like
    its mute sibling and refreshed the way a mute refreshes."""

    def test_the_two_urls_clear_every_scope_level(self) -> None:
        urls = _strip_comments(read("urls.js"))
        for signature, endpoint in (
                ("export function bulkFlakyUrl(", '"api/flaky/bulk"'),
                ("export function unmarkFlakyUrl(", '"api/flaky/unmark"')):
            body = _function_body(urls, signature)
            self.assertIn(endpoint, body)
            for level in ("product", "stream", "baseline", "environment"):
                self.assertIn(level + ": null", body, signature + level)

    def test_the_bar_group_follows_mute(self) -> None:
        bar = _function_body(
            _strip_comments(read("selection.js")), "function ensureBar(")
        order = [bar.index(needle) for needle in (
            "barEl.appendChild(unmuteBtnEl)",
            'flakyCadenceEl = flakyCadenceSelect("selection-cadence-select")',
            "barEl.appendChild(flakyBtnEl)",
            "barEl.appendChild(unmarkBtnEl)",
            '"Clear selection"')]
        self.assertEqual(order, sorted(order))

    def test_the_placeholders_say_what_is_chosen(self) -> None:
        """WP-44 item 18: the cadence select opens on "fail after…", so
        it reads "fail after… / 3 in a row"; its twin, the mute duration,
        opens on "for…". Each is one ellipsis character, on an option of
        value "" drawn before the choices; the bar, the Review panel and
        the test page all build theirs from these two."""
        api = _strip_comments(read("api.js"))
        for signature, words in (
                ("export function flakyCadenceSelect(", "fail after…"),
                ("export function muteDurationSelect(", "for…")):
            body = _function_body(api, signature)
            self.assertIn('placeholder.value = "";', body, signature)
            self.assertIn('placeholder.textContent = "' + words + '";',
                          body, signature)
            self.assertLess(body.index("select.appendChild(placeholder)"),
                            body.index("for (const "), signature)
        self.assertNotIn('"at…"', api)

    def test_the_bar_asks_for_the_reason_before_the_server_does(
            self) -> None:
        code = _strip_comments(read("selection.js"))
        gate = _function_body(code, "function updateFlakyButtonState(")
        for needle in ("selected.size === 0", "!flakyCadenceEl.value",
                       "noteInputEl.value.trim()", "allSelectedMarked()",
                       '"Set cadence"', '"Mark as flaky"'):
            self.assertIn(needle, gate)
        self.assertIn("updateFlakyButtonState()", _function_body(
            code, "function updateCommentButtonState("))
        self.assertIn(
            'flakyCadenceEl.addEventListener("change", updateFlakyButtonState)',
            code)
        self.assertIn("marked: Boolean(entry.flaky)",
                      _function_body(code, "function rowCell("))

    def test_the_bar_acts_post_the_mark_and_name_no_owner(self) -> None:
        code = _strip_comments(read("selection.js"))
        mark = _function_body(code, "async function doMarkFlaky(")
        unmark = _function_body(code, "async function doUnmarkFlaky(")
        gate = _function_body(code, "function updateFlakyButtonState(")
        for needle in ("postJson(bulkFlakyUrl(), body)", "cadence: cadence",
                       "tests: testsPayload()", "body.reason = reason",
                       'noteInputEl.value = ""', 'flakyCadenceEl.value = ""',
                       "clearSelection()", "notifyChanged()"):
            self.assertIn(needle, mark)
        self.assertIn(
            "postJson(unmarkFlakyUrl(), { username: me, tests: testsPayload() })",
            unmark)
        for body in (mark, unmark, gate):
            self.assertEqual(_owner_tokens(body), [])

    def test_an_owner_read_would_be_caught(self) -> None:
        planted = ("async function doMarkFlaky() {\n"
                   "  const owner = userSelectEl.value;\n"
                   "  await postJson(bulkFlakyUrl(), { assignee: owner });\n"
                   "}\n")
        self.assertEqual(_owner_tokens(planted), ["userSelectEl", "assignee"])

    def test_the_review_panel_offers_both_forms(self) -> None:
        code = _strip_comments(read("review.js"))
        group = _function_body(code, "function buildFlakyGroup(")
        for needle in ("entry.flaky || null", "flakySentence(flaky, muteLive)",
                       "flakyDetail(flaky)",
                       'flakyCadenceSelect("review-cadence-select", flaky.cadence)',
                       'flakyCadenceSelect("review-cadence-select")',
                       '"Set cadence"', '"Unmark"', '"Mark as flaky"',
                       "bulkFlakyUrl()", "unmarkFlakyUrl()",
                       'afterStandingChange(opts, "flaky")'):
            self.assertIn(needle, group)
        self.assertEqual(_owner_tokens(group), [])
        self.assertIn("buildFlakyGroup(entry, opts)",
                      _function_body(code, "function buildReviewActions("))
        # A mark refreshes exactly as a mute does.
        self.assertIn('afterStandingChange(opts, "muted")',
                      _function_body(code, "function buildMuteGroup("))

    def test_the_test_page_draws_the_mark_from_the_detail(self) -> None:
        """The mark and its history ride the detail: nothing fetches."""
        code = _strip_comments(read("test.js"))
        self.assertIn("detail.flaky || null",
                      _function_body(code, "function renderFlaky("))
        self.assertIn("detail.flaky_history",
                      _function_body(code, "function renderFlaky("))
        for signature in ("function renderFlaky(", "function renderFlakyBanner(",
                          "function renderFlakyCard(", "function openFlakyForm(",
                          "function renderFlakyHistory("):
            body = _function_body(code, signature)
            self.assertTrue(body, signature)
            self.assertNotIn("fetchJson(", body, signature)
            self.assertEqual(_owner_tokens(body), [], signature)
        refresh = _function_body(code, "async function refreshActs(")
        for needle in ("fetchJson(detailUrl())", "renderLatest(detail.latest, detail)",
                       "renderMute(detail)", "renderFlaky(detail)"):
            self.assertIn(needle, refresh)
        self.assertIn("await refreshActs()",
                      _function_body(code, "async function postAct("))
        html = read("test.html")
        self.assertLess(html.index('id="mute-banner"'),
                        html.index('id="flaky-banner"'))
        self.assertLess(html.index('id="flakiness"'),
                        html.index('id="flaky-mark"'))
        history = html[html.index('id="flaky-history-section"'):]
        history = history[:history.index("</section>")]
        self.assertEqual(re.findall(r"<th>([^<]+)</th>", history),
                         ["Action", "By", "When (UTC)", "Cadence", "Reason"])

    def test_one_form_in_the_header_and_a_link_in_the_card(self) -> None:
        """WP-44 item 17: every act on the test sits in the header. The
        offer is drawn in the mark's slot for any unmarked test, whatever
        its result; the form is drawn there and nowhere else; the card's
        link opens it and draws none of its own."""
        code = _strip_comments(read("test.js"))
        banner = _function_body(code, "function renderFlakyBanner(")
        for needle in ('classList.toggle("is-offer", !flaky)',
                       'el("span", "kf-label", "Flaky")', '"Mark as flaky"',
                       'addEventListener("click", openFlakyForm)',
                       "open.title = MARK_FLAKY_TITLE"):
            self.assertIn(needle, banner)
        # A mark is made while the test passes too: nothing but the mark
        # gates the offer, neither the result nor a mute.
        offer = banner[:banner.index("return;")]
        self.assertIn("if (!flaky) {", offer)
        for needle in ("latest", "FAIL", "failing", "if (muteLive"):
            self.assertNotIn(needle, offer)
        form = _function_body(code, "function openFlakyForm(")
        self.assertIn('document.getElementById("flaky-banner")', form)
        self.assertIn("renderFlakyBanner(null)", form)
        # The submit explains the act as the opener does, as Mute's act
        # button carries its own: one constant, used twice.
        self.assertIn("mark.title = MARK_FLAKY_TITLE", form)
        self.assertEqual(code.count("MARK_FLAKY_TITLE"), 3)
        card = _function_body(code, "function renderFlakyCard(")
        for needle in ('el("button", "linkish", "Mark as flaky")',
                       "openFlakyForm()",
                       'getElementById("flaky-banner").scrollIntoView('):
            self.assertIn(needle, card)
        for needle in ("createElement(", "flakyCadenceSelect(",
                       "bulkFlakyUrl(", "postAct("):
            self.assertNotIn(needle, card)
        # One form on the page: one reason box, one mark request with a
        # reason.
        self.assertEqual(code.count("FLAKY_REASON_PLACEHOLDER;"), 1)
        self.assertEqual(code.count("reason: text, cadence: chosen"), 1)
        # The offer is quieter than the mark: no tint, a neutral edge and
        # a muted label, and no rule naming the offer may tint it again.
        css = re.sub(r"/\*.*?\*/", "", read_text("style.css"), flags=re.S)
        rules = {}  # type: Dict[str, str]
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            for selector in match.group(1).split(","):
                if "is-offer" in selector:
                    rules[selector.strip()] = match.group(2)
        self.assertEqual(sorted(rules), [
            ".known-failure.is-flaky.is-offer",
            ".known-failure.is-flaky.is-offer .kf-label"])
        slot = rules[".known-failure.is-flaky.is-offer"]
        self.assertIn("border-left-color: var(--grid)", slot)
        self.assertIn("background: none", slot)
        self.assertEqual(_offer_tints(slot), [])
        self.assertEqual(
            rules[".known-failure.is-flaky.is-offer .kf-label"].strip(),
            "color: var(--muted);")
        # The mark's banner keeps the annotation's tint.
        self.assertIn("background: #fdf8ec",
                      re.search(r"\n\.known-failure\s*\{([^}]*)\}",
                                css).group(1))

    def test_a_tinted_offer_would_be_caught(self) -> None:
        for planted in ("background: #fdf8ec;", "background-color: #fff;",
                        "border-left-color: var(--c-flaking-strong);",
                        "background: none; background: #fdf8ec;"):
            self.assertTrue(_offer_tints(planted), planted)
        self.assertEqual(_offer_tints(
            "border-left-color: var(--grid);\n  background: none;"), [])

    def test_the_header_reads_the_mark_and_history_stays_raw(self) -> None:
        """Decision 9: a run is what happened, so run history and the
        compare strip show the wire result; the header shows what the
        test reads as now."""
        code = _strip_comments(read("test.js"))
        latest = _function_body(code, "function renderLatest(")
        self.assertRegex(latest, r"resultChip\(\s*displayResult\(latest\.result")
        self.assertNotIn("resultChip(latest.result)", latest)
        self.assertIn("flakyTag(", latest)
        self.assertIn("resultChip(run.result)",
                      _function_body(code, "function appendHistoryRow("))
        for signature in ("function appendHistoryRow(",
                          "async function loadCompareStrip("):
            self.assertNotIn("displayResult(", _function_body(code, signature))

    def test_one_mark_controls_share_their_words_from_api_js(self) -> None:
        """Review: the panel's and the page's titles were two copies,
        and only the page's reason box had an accessible label."""
        api = _strip_comments(read("api.js"))
        for name in ("FLAKY_CADENCE_TITLE", "FLAKY_UNMARK_TITLE",
                     "FLAKY_REASON_PLACEHOLDER", "FLAKY_REASON_LABEL"):
            self.assertIn("export const " + name + " = ", api)
        for name in ("review.js", "test.js"):
            code = _strip_comments(read(name))
            for needle in ("change.title = FLAKY_CADENCE_TITLE",
                           "unmark.title = FLAKY_UNMARK_TITLE",
                           "reason.placeholder = FLAKY_REASON_PLACEHOLDER",
                           'reason.setAttribute("aria-label", '
                           "FLAKY_REASON_LABEL)",
                           "cadenceChanged(cadence, flaky)"):
                self.assertIn(needle, code, name)
            for words in ("The mark keeps its owner", "Remove the flaky mark",
                          "Why is this test flaky"):
                self.assertNotIn(words, code, name)

    def test_no_user_string_reaches_innerHTML(self) -> None:
        for name in ("selection.js", "review.js", "test.js", "api.js"):
            self.assertNotIn("innerHTML", _strip_comments(read(name)), name)


def _iso_days_ago(days: float) -> str:
    moment = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days))
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _flaky(state: Any, streak: Any = None, runs: Any = None) -> Dict[str, Any]:
    """A test detail's or a row's ``flaky`` in the server's shape,
    marked 12 days ago."""
    return {"cadence": 3, "marked_by": "dev1",
            "marked_at": _iso_days_ago(12.1), "reason": "race in teardown",
            "state": state, "streak": streak, "runs": runs}


_LIVE_MUTE = {
    "live": True, "muted_by": "dev2", "reason": "infra outage",
    "expires_at": _iso_days_ago(-2), "extensions": 0,
}  # type: Dict[str, Any]


def _test_detail(result: str, flaky: Any = "absent",
                 mute: Any = None) -> Dict[str, Any]:
    """``GET /api/tests/...`` in the server's shape; ``flaky="absent"``
    is a server from before WP-44."""
    detail = {
        "environment": "env-a", "script": "s1.py", "test_name": "t1",
        "source_link": None, "assignee": None,
        "latest": {"run_id": 7, "result": result,
                   "start_time": "2026-11-10T02:14:00",
                   "duration_seconds": 41.2, "known_failure_reason": None},
        "analytics": {
            "window": {"max_days": 90, "max_runs": 200, "run_count": 12,
                       "from": "2026-10-01T02:00:00",
                       "to": "2026-11-10T02:14:00"},
            "failing_since": None, "last_pass_before_failure": None,
            "flakiness": {"classification": "flaky", "score": 0.4,
                          "transitions": 5},
            "duration_seconds": None, "day_of_week": [], "by_day": [],
        },
        "stream": None, "stream_identity": None, "mute": mute,
        "mute_history": [],
    }  # type: Dict[str, Any]
    if flaky != "absent":
        detail["flaky"] = flaky
        detail["flaky_history"] = [] if flaky is None else [
            {"id": 1, "action": "mark", "cadence": 3,
             "reason": "race in teardown", "actor": "dev1",
             "acted_at": "2026-11-01T02:00:00"}]
    return detail


#: The flaky walks: the real test.js, selection.js or review.js over the
#: shim, fetch answered from the fixture and every request recorded, one
#: act performed. argv: repo root, fixture path.
_FLAKY_SHIM_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom, Element } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, fixture.url);
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
window.localStorage.setItem("testboard.username", "dev1");
// What took the focus last and what was scrolled to, by class or id.
let focused = null;
const scrolled = [];
Element.prototype.focus = function () { focused = this.className; };
Element.prototype.scrollIntoView = function () {
  scrolled.push(Object.keys(byId).find((id) => byId[id] === this)
    || this.className);
};
const asked = [];
let acted = false;
globalThis.fetch = (url, opts) => {
  const u = new URL(String(url), "http://127.0.0.1/");
  const path = decodeURIComponent(u.pathname.replace(/^\//, ""));
  const method = (opts && opts.method) || "GET";
  asked.push({ path, method,
    body: opts && opts.body ? JSON.parse(opts.body) : null });
  let body = {};
  if (method === "POST") {
    acted = true;
    body = path.endsWith("unmark") ? { unmarked: 1 }
      : { marked: 1, changed: 0, unchanged: 0, unknown: 0 };
  } else if (path === "api/users") {
    body = { users: [] };
  } else if (path.startsWith("api/runs/")) {
    body = { output: "boom" };
  } else if (path.endsWith("/history")) {
    body = { runs: [] };
  } else if (path.endsWith("/comments")) {
    body = { comments: [], streams: {} };
  } else if (path.endsWith("/streams")) {
    body = { results: [], product: null };
  } else if (path.startsWith("api/tests/")) {
    body = acted && fixture.after ? fixture.after : fixture.detail;
  }
  const answer = (status) => ({ ok: status < 400, status,
    text: async () => JSON.stringify(
      status < 400 ? body : { error: "server says no" }) });
  if (method === "POST" && fixture.hold) {
    // Held until the walk calls release(), answering postStatus then.
    return new Promise((resolve) => {
      release = () => resolve(answer(fixture.postStatus || 200));
    });
  }
  return Promise.resolve(answer(200));
};
let release = null;
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
const button = (node, text) => node.find(
  (c) => c.tagName === "BUTTON" && c.textContent === text)[0] || null;
const errorShown = () => (byId["error-banner"].hidden
  ? null : byId["error-banner"].textContent);
const posts = () => asked.filter((a) => a.method === "POST")
  .map((a) => [a.path, a.body]);
const words = (node) => node.children
  .filter((c) => c.tagName !== "SELECT").map((c) => c.textContent);
const out = {};
if (fixture.surface === "test") {
  const page = () => {
    const latest = byId["latest-run"];
    const chip = latest.children.find(
      (c) => String(c.className).startsWith("chip"));
    const tag = latest.children.find((c) => c.className === "flaky-tag");
    return {
      chip: chip ? [chip.className, chip.textContent] : null,
      tag: tag ? [tag.textContent, tag.title] : null,
      banner: byId["flaky-banner"].hidden ? null : words(byId["flaky-banner"]),
      offer: byId["flaky-banner"].classList.contains("is-offer"),
      card: words(byId["flaky-mark"]),
      cardTags: byId["flaky-mark"].children.map((c) => c.tagName),
      signal: byId["flakiness"].children[0].textContent,
      historyHidden: byId["flaky-history-section"].hidden,
      history: byId["flaky-history-body"].children.map((tr) => words(tr)),
    };
  };
  await import(pathToFileURL(root + "/static/test.js").href);
  await settle();
  out.first = page();
  out.firstAsked = asked.map((a) => a.method + " " + a.path);
  const before = asked.length;
  const banner = byId["flaky-banner"];
  const form = () => banner.children.map((c) => c.tagName);
  const reasonBox = () => banner.children.find((c) => c.tagName === "INPUT");
  const fill = () => {
    reasonBox().value = "race in teardown";
    banner.children.find((c) => c.tagName === "SELECT").value = "3";
  };
  if (fixture.act === "mark") {
    // The header's offer opens the form in the same slot.
    await button(banner, "Mark as flaky").click();
    out.form = form();
    out.formWords = words(banner);
    out.focused = focused;
    out.cardWhileOpen = byId["flaky-mark"].children.map((c) => c.tagName);
    await button(banner, "Mark as flaky").click();
    out.emptyError = errorShown();
    out.emptyPosts = posts().length;
    fill();
    await button(banner, "Mark as flaky").click();
  } else if (fixture.act === "card") {
    // The card's link: the header's form, focused and scrolled to; again
    // while open, what was typed stays; Cancel, the offer; then mark.
    const link = button(byId["flaky-mark"], "Mark as flaky");
    out.linkClass = link.className;
    await link.click();
    out.form = form();
    out.focused = focused;
    out.scrolled = scrolled.slice();
    out.cardWhileOpen = byId["flaky-mark"].children.map((c) => c.tagName);
    reasonBox().value = "race";
    focused = null;
    await link.click();
    out.again = [form(), reasonBox().value, focused];
    await button(banner, "Cancel").click();
    out.cancelled = [words(banner), banner.classList.contains("is-offer")];
    await link.click();
    out.fresh = reasonBox().value;
    fill();
    await button(banner, "Mark as flaky").click();
  } else if (fixture.act === "cadence") {
    const banner = byId["flaky-banner"];
    const select = banner.children.find((c) => c.tagName === "SELECT");
    const set = button(banner, "Set cadence");
    out.selectValue = select.value;
    out.setDisabledAtFirst = set.disabled;
    select.value = "5";
    await select.dispatch("change");
    out.setDisabledAfter = set.disabled;
    await set.click();
  } else if (fixture.act === "unmark") {
    await button(byId["flaky-banner"], "Unmark").click();
  } else if (fixture.act === "cadence-fail") {
    // Choose 5, send, and while it is out go back to the mark's own 3;
    // the request then fails.
    const banner = byId["flaky-banner"];
    const select = banner.children.find((c) => c.tagName === "SELECT");
    const set = button(banner, "Set cadence");
    const unmark = button(banner, "Unmark");
    select.value = "5";
    await select.dispatch("change");
    set.click();
    await settle();
    select.value = "3";
    await select.dispatch("change");
    out.inFlight = [set.disabled, unmark.disabled];
    set.click();
    unmark.click();
    await settle();
    out.postsInFlight = posts().length;
    release();
    await settle();
    out.afterFail = [set.disabled, unmark.disabled, errorShown()];
    select.value = "5";
    await select.dispatch("change");
    out.rechosen = set.disabled;
  }
  await settle();
  out.after = page();
  out.later = asked.slice(before).map((a) => a.method + " " + a.path);
} else if (fixture.surface === "bar") {
  const { mountSelectableTable } = await import(
    pathToFileURL(root + "/static/selection.js").href);
  let changed = 0;
  const mount = mountSelectableTable(document.createElement("table"),
    { onChanged: () => { changed += 1; } });
  const bar = document.body.children.find((c) => c.className === "selection-bar");
  const part = (cls) => bar.children.find((c) => c.className === cls);
  const flakyBtn = part("selection-flaky-btn");
  const cadence = part("selection-cadence-select");
  const note = part("selection-note-input");
  const tick = async () => {
    for (const row of fixture.rows) {
      const box = mount.rowCell(row).children[0];
      box.checked = true;
      await box.dispatch("change");
    }
  };
  const seen = () => [flakyBtn.textContent, flakyBtn.disabled];
  out.order = bar.children.map((c) => c.className || c.textContent);
  const unmarkBtn = part("selection-unmark-btn");
  if (fixture.act === "midflight") {
    // Send a mark, then while it is out type, re-choose, tick a row and
    // click both buttons: nothing may re-arm or send.
    await tick();
    cadence.value = "3";
    await cadence.dispatch("change");
    note.value = "race";
    await note.dispatch("input");
    flakyBtn.click();
    await settle();
    out.inFlight = [];
    note.value = "race in teardown";
    await note.dispatch("input");
    out.inFlight.push([flakyBtn.disabled, unmarkBtn.disabled]);
    await cadence.dispatch("change");
    out.inFlight.push([flakyBtn.disabled, unmarkBtn.disabled]);
    const box = mount.rowCell(fixture.extra).children[0];
    box.checked = true;
    await box.dispatch("change");
    out.inFlight.push([flakyBtn.disabled, unmarkBtn.disabled]);
    flakyBtn.click();
    unmarkBtn.click();
    await settle();
    out.postsInFlight = posts().length;
    release();
    await settle();
    out.afterRelease = [bar.hidden, changed];
    out.posts = posts();
    await driverAnswer(out);
  }
  await tick();
  out.ticked = seen();
  cadence.value = "3";
  await cadence.dispatch("change");
  out.withCadence = seen();
  note.value = "race in teardown";
  await note.dispatch("input");
  out.withReason = seen();
  await flakyBtn.click();
  await settle();
  out.afterMark = { hidden: bar.hidden, changed, note: note.value,
    cadence: cadence.value };
  await tick();
  await part("selection-unmark-btn").click();
  await settle();
  out.afterUnmark = { hidden: bar.hidden, changed };
} else {
  const { toggleReview } = await import(
    pathToFileURL(root + "/static/review.js").href);
  let reloads = 0;
  const tbody = document.createElement("tbody");
  const row = document.createElement("tr");
  row.appendChild(document.createElement("td"));
  tbody.appendChild(row);
  toggleReview(fixture.entry, row, document.createElement("button"),
    { onMuted: () => { reloads += 1; } });
  await settle();
  const panel = row.nextSibling;
  out.labels = panel.find((c) => c.className === "review-label")
    .map((c) => c.textContent);
  const group = panel.find((c) => c.className === "review-label"
    && c.textContent === "Flaky")[0].parentNode;
  out.group = group.children.map((c) => [c.tagName,
    c.tagName === "SELECT" ? c.value
      : c.tagName === "SPAN" ? c.children.map((x) => x.textContent)
        : (c.placeholder || c.textContent)]);
  if (fixture.act === "mark") {
    await button(group, "Mark as flaky").click();
    out.emptyError = errorShown();
    out.emptyPosts = posts().length;
    group.children.find((c) => c.tagName === "INPUT").value = "race in teardown";
    group.children.find((c) => c.tagName === "SELECT").value = "3";
    await button(group, "Mark as flaky").click();
  } else if (fixture.act === "cadence") {
    const select = group.children.find((c) => c.tagName === "SELECT");
    const set = button(group, "Set cadence");
    out.setDisabledAtFirst = set.disabled;
    select.value = "5";
    await select.dispatch("change");
    out.setDisabledAfter = set.disabled;
    await set.click();
  } else if (fixture.act === "cadence-fail") {
    const select = group.children.find((c) => c.tagName === "SELECT");
    const set = button(group, "Set cadence");
    const unmark = button(group, "Unmark");
    select.value = "5";
    await select.dispatch("change");
    set.click();
    await settle();
    select.value = "3";
    await select.dispatch("change");
    out.inFlight = [set.disabled, unmark.disabled];
    set.click();
    unmark.click();
    await settle();
    out.postsInFlight = posts().length;
    release();
    await settle();
    out.afterFail = [set.disabled, unmark.disabled, errorShown()];
    select.value = "5";
    await select.dispatch("change");
    out.rechosen = set.disabled;
  } else {
    await button(group, "Unmark").click();
  }
  await settle();
  out.reloads = reloads;
}
out.posts = posts();
await driverAnswer(out);
"""

_ONE_TEST = {"environment": "env-a", "script": "s1.py",
             "test_name": "t1"}  # type: Dict[str, Any]


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class FlakyMarkDomTest(unittest.TestCase):
    """WP-44 item 5 over the DOM shim: the test page, the selection bar
    and the Review panel, each made to mark, change a cadence and
    unmark. No browser; it proves the requests and the DOM drawn, not
    the look."""

    #: A POST held until the walk releases it, then failing.
    _HELD_FAILING = {"hold": True, "postStatus": 500}  # type: Dict[str, Any]

    def _page(self, detail: Dict[str, Any], act: Any = None,
              after: Any = None, **extra: Any) -> Dict[str, Any]:
        fixture = {
            "surface": "test", "act": act, "detail": detail, "after": after,
            "ids": re.findall(r'id="([^"]+)"', read("test.html")),
            "url": "http://127.0.0.1/test.html?environment=env-a"
                   "&script=s1.py&test_name=t1"}  # type: Dict[str, Any]
        fixture.update(extra)
        return _shim(fixture, _FLAKY_SHIM_DRIVER)

    def _bar(self, rows: List[Dict[str, Any]],
             **extra: Any) -> Dict[str, Any]:
        fixture = {"surface": "bar", "rows": rows, "ids": ["error-banner"],
                   "url": "http://127.0.0.1/index.html"}  # type: Dict[str, Any]
        fixture.update(extra)
        return _shim(fixture, _FLAKY_SHIM_DRIVER)

    def _panel(self, entry: Dict[str, Any], act: str,
               **extra: Any) -> Dict[str, Any]:
        fixture = {"surface": "panel", "entry": entry, "act": act,
                   "ids": ["error-banner"],
                   "url": "http://127.0.0.1/index.html"}  # type: Dict[str, Any]
        fixture.update(extra)
        return _shim(fixture, _FLAKY_SHIM_DRIVER)

    def test_nothing_rearms_the_bar_while_a_flaky_request_is_out(
            self) -> None:
        """Review: a keystroke in the note box, a cadence change or a
        ticked row re-enabled a Flaky button mid-flight, and a second
        click sent a second request. One in-flight flag now holds both."""
        answer = self._bar([dict(_ONE_TEST)], act="midflight", hold=True,
                           extra=dict(_ONE_TEST, test_name="t2"))
        self.assertEqual(answer["inFlight"],
                         [[True, True], [True, True], [True, True]])
        self.assertEqual(answer["postsInFlight"], 1)
        self.assertEqual(answer["afterRelease"], [True, 1])
        self.assertEqual([post[0] for post in answer["posts"]],
                         ["api/flaky/bulk"])

    def test_a_failed_set_cadence_returns_to_the_derived_state(
            self) -> None:
        """Review: a failure re-enabled every button, so Set cadence came
        back enabled on the mark's own cadence. Now: disabled while out,
        no second request, and after the failure disabled again while
        the cadence is unchanged, enabled once it differs."""
        marked = _test_detail("FAIL", _flaky("flaking", 1))
        entry = dict(_ONE_TEST, run_id=7, result="FAIL",
                     start_time="2026-11-10T02:14:00", assignee=None,
                     mute=None, flaky=_flaky("flaking", 1))
        for surface, answer in (
                ("test page", self._page(marked, "cadence-fail",
                                         **self._HELD_FAILING)),
                ("panel", self._panel(entry, "cadence-fail",
                                      **self._HELD_FAILING))):
            with self.subTest(surface=surface):
                self.assertEqual(answer["inFlight"], [True, True])
                self.assertEqual(answer["postsInFlight"], 1)
                self.assertEqual(answer["afterFail"],
                                 [True, False, "server says no"])
                self.assertFalse(answer["rechosen"])
        self.assertEqual(self._panel(entry, "cadence-fail",
                                     **self._HELD_FAILING)["reloads"], 0)

    def test_an_unmarked_test_page_is_todays_page_with_the_offer(
            self) -> None:
        """No ``flaky`` field (an older server) and a null one draw the
        same: the raw chip, no pill, no history, the offer in the
        header's slot (WP-44 item 17, widened from "no banner": the
        slot now offers the mark, in the offer's edge, not the mark's),
        the card's link, and the same requests."""
        older = self._page(_test_detail("FAIL"))
        newer = self._page(_test_detail("FAIL", None))
        for answer in (older, newer):
            first = answer["first"]
            self.assertEqual(first["chip"], ["chip result-fail", "FAIL"])
            self.assertIsNone(first["tag"])
            self.assertEqual(first["banner"],
                             ["Flaky", "Mark as flaky"])
            self.assertTrue(first["offer"])
            self.assertTrue(first["historyHidden"])
            self.assertEqual(first["card"], ["Mark as flaky"])
            self.assertEqual(first["cardTags"], ["BUTTON"])
            self.assertEqual(first["signal"], "flips often")
            self.assertEqual(answer["posts"], [])
        self.assertEqual(older["firstAsked"], newer["firstAsked"])
        self.assertEqual(sorted(set(older["firstAsked"])), [
            "GET api/tests/env-a/s1.py/t1",
            "GET api/tests/env-a/s1.py/t1/comments",
            "GET api/tests/env-a/s1.py/t1/history",
            "GET api/tests/env-a/s1.py/t1/streams",
            "GET api/users"])

    def test_the_offer_shows_whatever_the_result_and_beside_a_mute(
            self) -> None:
        """WP-44 item 17: a mark is a standing property of the test, so
        the header offers it for a passing test as for a failing one,
        and beside a live mute (the card offered it there too); the
        requests are the failing page's, so nothing is fetched for it."""
        failing = self._page(_test_detail("FAIL", None))
        for name, detail in (
                ("passing", _test_detail("PASS", None)),
                ("muted", _test_detail("FAIL", None, _LIVE_MUTE))):
            with self.subTest(name):
                answer = self._page(detail)
                first = answer["first"]
                self.assertEqual(first["banner"],
                                 ["Flaky", "Mark as flaky"])
                self.assertTrue(first["offer"])
                self.assertEqual(first["card"], ["Mark as flaky"])
                self.assertEqual(sorted(set(answer["firstAsked"])),
                                 sorted(set(failing["firstAsked"])))

    def test_a_flaking_test_reads_FLAKING_with_banner_card_and_history(
            self) -> None:
        first = self._page(
            _test_detail("FAIL", _flaky("flaking", 1)))["first"]
        self.assertEqual(first["chip"], ["chip result-flaking", "FLAKING"])
        self.assertIsNone(first["tag"])
        self.assertEqual(first["banner"], [
            "Marked flaky, 3 in a row",
            "by dev1 · 12 days ago · race in teardown",
            "Flaking: failed its last run; counts as failing at 3 in a row",
            "Set cadence", "Unmark"])
        # Marked: the mark's banner in the mark's edge, no offer anywhere.
        self.assertFalse(first["offer"])
        self.assertEqual(first["card"], ["Marked flaky, 3 in a row"])
        self.assertFalse(first["historyHidden"])
        self.assertEqual(first["history"], [[
            "mark", "dev1", "2026-11-01 02:00:00", "3 in a row",
            "race in teardown"]])

    def test_every_other_state_keeps_its_chip_and_carries_the_pill(
            self) -> None:
        cases = (
            ("FAIL", _flaky("flaking", 2), _LIVE_MUTE,
             ["chip result-fail", "FAIL"],
             "Marked flaky; the mute comes first until it runs out"),
            ("FAIL", _flaky("failing", 3, 3), None,
             ["chip result-fail", "FAIL"],
             "Failing: its last 3 runs all failed; marked flaky, 3 in a row"),
            ("FAIL", _flaky("failing", 2, 2), None,
             ["chip result-fail", "FAIL"],
             "Failing: every run here has failed (2 of 2); marked flaky, "
             "3 in a row"),
            ("FAIL", _flaky("failing", 1, 1), None,
             ["chip result-fail", "FAIL"],
             "Failing: its only run here failed; marked flaky, 3 in a row"),
            ("PASS", _flaky("passing", 0), None,
             ["chip result-pass", "PASS"],
             "Marked flaky: counts as failing once its last 3 runs have "
             "failed"),
            ("PASS", _flaky(None), None, ["chip result-pass", "PASS"],
             "Marked flaky, 3 in a row; no run here yet"),
        )
        for result, flaky, mute, chip, sentence in cases:
            with self.subTest(sentence=sentence):
                first = self._page(_test_detail(result, flaky, mute))["first"]
                self.assertEqual(first["chip"], chip)
                self.assertEqual(first["tag"], ["flaky", sentence])
                self.assertEqual(first["banner"][2], sentence)

    def test_marking_from_the_header_asks_for_the_reason_first(self) -> None:
        """WP-44 item 17 (widened from the card's form, which moved): the
        header's offer opens the one form in its slot, the label kept as
        the Mute offer keeps its own; the act, its refusals, the request
        and the refresh are the card's form's, unchanged."""
        answer = self._page(_test_detail("FAIL", None), "mark",
                            _test_detail("FAIL", _flaky("flaking", 1)))
        self.assertEqual(answer["form"],
                         ["SPAN", "INPUT", "SELECT", "BUTTON", "BUTTON"])
        self.assertEqual(answer["formWords"],
                         ["Flaky", "", "Mark as flaky", "Cancel"])
        self.assertEqual(answer["focused"], "flaky-reason-input")
        self.assertEqual(answer["cardWhileOpen"], ["BUTTON"])
        self.assertEqual(answer["emptyPosts"], 0)
        self.assertTrue(answer["emptyError"].startswith("Say why"))
        self.assertEqual(answer["posts"], [["api/flaky/bulk", {
            "username": "dev1", "reason": "race in teardown", "cadence": 3,
            "tests": [_ONE_TEST]}]])
        # One request after the act, the detail, as after a mute.
        self.assertEqual(answer["later"], [
            "POST api/flaky/bulk", "GET api/tests/env-a/s1.py/t1"])
        self.assertEqual(answer["after"]["chip"],
                         ["chip result-flaking", "FLAKING"])
        self.assertEqual(answer["after"]["banner"][0],
                         "Marked flaky, 3 in a row")
        self.assertFalse(answer["after"]["offer"])
        self.assertEqual(answer["after"]["card"], ["Marked flaky, 3 in a row"])

    def test_a_passing_test_and_a_build_page_mark_from_the_header(
            self) -> None:
        """The act is the same whatever the result; on a page opened for
        a build the request carries the page's stream, as the card's
        form always sent it (the mark itself covers every stream)."""
        passing = self._page(_test_detail("PASS", None), "mark",
                             _test_detail("PASS", _flaky("passing", 0)))
        self.assertEqual(passing["posts"], [["api/flaky/bulk", {
            "username": "dev1", "reason": "race in teardown", "cadence": 3,
            "tests": [_ONE_TEST]}]])
        self.assertFalse(passing["after"]["offer"])
        build = self._page(
            _test_detail("FAIL", None), "mark",
            _test_detail("FAIL", _flaky("flaking", 1)),
            url="http://127.0.0.1/test.html?environment=env-a"
                "&script=s1.py&test_name=t1&stream=4")
        self.assertTrue(build["first"]["offer"])
        self.assertEqual(build["posts"], [["api/flaky/bulk", {
            "username": "dev1", "reason": "race in teardown", "cadence": 3,
            "tests": [dict(_ONE_TEST, stream_id=4)]}]])
        self.assertEqual(build["later"][0], "POST api/flaky/bulk")

    def test_the_cards_link_opens_the_header_form(self) -> None:
        """WP-44 item 17: the Flakiness card keeps an entry point, a
        link-styled button, that opens the header's form, puts the focus
        in its reason box and scrolls the slot into view; it draws no
        form of its own, and a second click keeps what was typed."""
        answer = self._page(_test_detail("FAIL", None), "card",
                            _test_detail("FAIL", _flaky("flaking", 1)))
        self.assertEqual(answer["linkClass"], "linkish")
        self.assertEqual(answer["form"],
                         ["SPAN", "INPUT", "SELECT", "BUTTON", "BUTTON"])
        self.assertEqual(answer["focused"], "flaky-reason-input")
        self.assertEqual(answer["scrolled"], ["flaky-banner"])
        self.assertEqual(answer["cardWhileOpen"], ["BUTTON"])
        self.assertEqual(answer["again"], [
            ["SPAN", "INPUT", "SELECT", "BUTTON", "BUTTON"], "race",
            "flaky-reason-input"])
        self.assertEqual(answer["cancelled"],
                         [["Flaky", "Mark as flaky"], True])
        self.assertEqual(answer["fresh"], "")
        self.assertEqual(answer["posts"], [["api/flaky/bulk", {
            "username": "dev1", "reason": "race in teardown", "cadence": 3,
            "tests": [_ONE_TEST]}]])
        self.assertEqual(answer["after"]["card"], ["Marked flaky, 3 in a row"])
        self.assertFalse(answer["after"]["offer"])

    def test_the_banner_changes_the_cadence_and_unmarks(self) -> None:
        marked = _test_detail("FAIL", _flaky("flaking", 1))
        answer = self._page(marked, "cadence", marked)
        self.assertEqual(answer["selectValue"], "3")
        self.assertTrue(answer["setDisabledAtFirst"])
        self.assertFalse(answer["setDisabledAfter"])
        self.assertEqual(answer["posts"], [["api/flaky/bulk", {
            "username": "dev1", "cadence": 5, "tests": [_ONE_TEST]}]])
        self.assertEqual(answer["later"], [
            "POST api/flaky/bulk", "GET api/tests/env-a/s1.py/t1"])
        answer = self._page(marked, "unmark", _test_detail("FAIL", None))
        self.assertEqual(answer["posts"], [["api/flaky/unmark", {
            "username": "dev1", "tests": [_ONE_TEST]}]])
        # Widened for WP-44 item 17 (was: no banner): after Unmark the
        # slot offers the mark again.
        self.assertEqual(answer["after"]["banner"],
                         ["Flaky", "Mark as flaky"])
        self.assertTrue(answer["after"]["offer"])
        self.assertEqual(answer["after"]["chip"], ["chip result-fail", "FAIL"])
        self.assertEqual(answer["after"]["card"], ["Mark as flaky"])

    def test_the_bar_marks_with_the_note_and_names_no_owner(self) -> None:
        rows = [dict(_ONE_TEST),
                dict(_ONE_TEST, test_name="t2", flaky=None)]
        answer = self._bar(rows)
        order = answer["order"]
        self.assertLess(order.index("selection-unmute-btn"),
                        order.index("Flaky "))
        self.assertLess(order.index("Flaky "),
                        order.index("selection-cadence-select"))
        self.assertLess(order.index("selection-flaky-btn"),
                        order.index("selection-unmark-btn"))
        self.assertEqual(answer["ticked"], ["Mark as flaky", True])
        self.assertEqual(answer["withCadence"], ["Mark as flaky", True])
        self.assertEqual(answer["withReason"], ["Mark as flaky", False])
        tests = [_ONE_TEST, dict(_ONE_TEST, test_name="t2")]
        self.assertEqual(answer["posts"], [
            ["api/flaky/bulk", {"username": "dev1", "cadence": 3,
                                "reason": "race in teardown", "tests": tests}],
            ["api/flaky/unmark", {"username": "dev1", "tests": tests}]])
        self.assertEqual(answer["afterMark"], {
            "hidden": True, "changed": 1, "note": "", "cadence": ""})
        self.assertEqual(answer["afterUnmark"], {"hidden": True, "changed": 2})

    def test_the_bar_sets_a_cadence_when_every_row_is_marked(self) -> None:
        marked = dict(_ONE_TEST, flaky=_flaky("flaking", 1))
        answer = self._bar([marked, dict(marked, test_name="t2")])
        self.assertEqual(answer["ticked"], ["Set cadence", True])
        self.assertEqual(answer["withCadence"], ["Set cadence", False])
        mixed = self._bar([marked, dict(_ONE_TEST, test_name="t2")])
        self.assertEqual(mixed["withCadence"], ["Mark as flaky", True])

    def test_the_panel_marks_an_unmarked_row(self) -> None:
        entry = dict(_ONE_TEST, run_id=7, result="FAIL",
                     start_time="2026-11-10T02:14:00", assignee=None,
                     mute=None, stream_id=4)
        answer = self._panel(entry, "mark")
        self.assertEqual(answer["labels"][-2:], ["Mute", "Flaky"])
        self.assertEqual(answer["group"], [
            ["LABEL", "Flaky"],
            ["INPUT", "Why is this test flaky? (required)"],
            ["SELECT", ""], ["BUTTON", "Mark as flaky"]])
        self.assertEqual(answer["emptyPosts"], 0)
        self.assertTrue(answer["emptyError"].startswith("Say why"))
        self.assertEqual(answer["posts"], [["api/flaky/bulk", {
            "username": "dev1", "reason": "race in teardown", "cadence": 3,
            "tests": [dict(_ONE_TEST, stream_id=4)]}]])
        self.assertEqual(answer["reloads"], 1)

    def test_the_panel_changes_and_removes_a_mark(self) -> None:
        entry = dict(_ONE_TEST, run_id=7, result="PASS",
                     start_time="2026-11-10T02:14:00", assignee=None,
                     mute=None, flaky=_flaky("passing", 0))
        answer = self._panel(entry, "cadence")
        self.assertEqual(answer["labels"][-1], "Flaky")
        self.assertNotIn("Mute", answer["labels"])
        self.assertEqual(answer["group"], [
            ["LABEL", "Flaky"],
            ["SPAN", ["Marked flaky: counts as failing once its last 3 "
                      "runs have failed",
                      "by dev1 · 12 days ago · race in teardown"]],
            ["SELECT", "3"], ["BUTTON", "Set cadence"],
            ["BUTTON", "Unmark"]])
        self.assertTrue(answer["setDisabledAtFirst"])
        self.assertFalse(answer["setDisabledAfter"])
        self.assertEqual(answer["posts"], [["api/flaky/bulk", {
            "username": "dev1", "cadence": 5, "tests": [_ONE_TEST]}]])
        self.assertEqual(answer["reloads"], 1)
        answer = self._panel(entry, "unmark")
        self.assertEqual(answer["posts"], [["api/flaky/unmark", {
            "username": "dev1", "tests": [_ONE_TEST]}]])
        self.assertEqual(answer["reloads"], 1)


#: The Home walk of WP-44 item 7: the real app.js over the shim, every
#: request answered from the fixture, each queue tab opened in turn and
#: the browse rows ticked in the sets the fixture names. Every region is
#: serialized whole (tag, class, hidden, title, aria-label, width, own
#: text, children), so a digest of it pins a DOM byte for byte.
#: argv: repo root, fixture path.
_HOME_STATE_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom, Element } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, "http://127.0.0.1/index.html");
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
window.localStorage.setItem("testboard.username", "dev1");
Element.prototype.focus = function () {};
const asked = [];
globalThis.fetch = (url) => {
  const u = new URL(String(url), "http://127.0.0.1/");
  const part = u.searchParams.get("parts");
  asked.push(part || u.pathname.replace(/^\//, ""));
  let body = {};
  if (part === "headline") {
    body = fixture.headline;
  } else if (part === "assigned_trend") {
    body = fixture.part;
  } else if (part === "queue") {
    body = { generated_at: fixture.headline.generated_at,
      stale_before: fixture.headline.stale_before,
      queue: fixture.queues[u.searchParams.get("queue")]
        || { tests: [], total: 0 } };
  } else if (u.pathname.endsWith("api/dashboard")) {
    body = fixture.dashboard;
  } else if (u.pathname.endsWith("api/users")) {
    body = { users: [] };
  }
  return Promise.resolve({ ok: true, status: 200,
    text: async () => JSON.stringify(body) });
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
const dom = (node) => (node.children === undefined ? ["#", node.textContent]
  : [node.tagName, String(node.className || ""), node.hidden ? "hidden" : "",
    node.title || "", node.attributes["aria-label"] || "",
    node.style.width || "", node._text, node.children.map(dom)]);
await import(pathToFileURL(root + "/static/app.js").href);
await new Promise((resolve) => setTimeout(resolve, 150));
const out = { asked: asked.slice(), regions: {}, queues: {}, ticked: [] };
for (const id of fixture.regions) {
  out.regions[id] = dom(byId[id]);
}
const tabLabel = (tab) => tab.children[0].textContent;
const activeTab = () => byId["queue-tabs"].children.filter(
  (t) => t.attributes["aria-selected"] === "true").map(tabLabel);
const aside = byId["all-clear-aside"];
out.aside = { hidden: aside.hidden, text: aside.textContent,
  parts: aside.children.map((c) => (c.tagName ? c.tagName : "#")
    + ":" + c.textContent) };
const labels = byId["queue-tabs"].children.map(tabLabel);
for (const label of labels) {
  const tab = byId["queue-tabs"].children.find((t) => tabLabel(t) === label);
  await tab.click();
  await settle();
  out.queues[label] = ["queue-result-note", "queue-head-row", "queue-body",
    "queue-empty"].map((id) => dom(byId[id]));
}
// After the tabs, so the walk opens them in the same order as at base.
await byId["queue-tabs"].children.find(
  (t) => tabLabel(t) === "New failures").click();
await settle();
const asideLink = aside.children.find((c) => c.tagName === "BUTTON");
if (asideLink) {
  await asideLink.click();
  await settle();
  out.aside.opened = activeTab();
}
const bar = document.body.children.find((c) => c.className === "selection-bar");
const boxes = byId["dashboard-body"].children.map(
  (tr) => tr.children[0].children[0]);
for (const set of fixture.tick || []) {
  for (const index of set) {
    boxes[index].checked = true;
    await boxes[index].dispatch("change");
  }
  const flakyBtn = bar.children.find(
    (c) => c.className === "selection-flaky-btn");
  out.ticked.push(flakyBtn.textContent);
  for (const index of set) {
    boxes[index].checked = false;
    await boxes[index].dispatch("change");
  }
}
await driverAnswer(out);
"""

#: The regions of Home the state walk serializes.
_HOME_REGIONS = ("stat-tiles", "all-clear", "triage-meta", "env-chart",
                 "scripts-chart", "dashboard-body")

#: The rows of the walk, one per state of design §3 U9 and two more:
#: name -> (result, flaky, mute, prev_result, assignee).
_STATE_ROWS = (
    ("flaking", "FAIL", ("flaking", 1, None), None, "PASS", None),
    ("over", "FAIL", ("failing", 3, 3), None, "FAIL", None),
    ("passing", "PASS", ("passing", 0, None), None, "FAIL", None),
    ("up", "UNEXPECTED_PASS", ("passing", 0, None), None, "FAIL", None),
    ("muted", "FAIL", ("flaking", 2, None), "live", "FAIL", None),
    ("plain", "FAIL", None, None, "PASS", None),
    ("mine", "FAIL", ("flaking", 1, None), None, "FAIL", "dev1"),
)  # type: Tuple[Tuple[str, str, Any, Any, str, Any], ...]


def _state_row(name: str, marks: str) -> Dict[str, Any]:
    """One row of the walk in the server's shape. *marks* is "flaky"
    (the mark as the state table has it), "zero" (``flaky: null``, a
    scope with no marks) or "older" (no ``flaky`` field at all)."""
    for row_name, result, flaky, mute, prev, assignee in _STATE_ROWS:
        if row_name == name:
            break
    row = {
        "environment": "env-a", "product": "", "script": "s1.py",
        "test_name": name, "run_id": 1, "result": result,
        "prev_result": prev, "start_time": "2026-10-03T02:00:00",
        "end_time": "2026-10-03T02:00:05", "duration_seconds": 5.0,
        "known_failure_reason": None, "source_link": None,
        "assignee": assignee, "assignment_stream_id": None,
        "retired_at": None, "retired_by": None, "latest_comment": None,
        "failing_since": "2026-10-01T02:00:00" if result == "FAIL" else None,
        "last_pass_time": None,
        "mute": dict(_LIVE_MUTE) if mute else None,
    }  # type: Dict[str, Any]
    if marks == "flaky":
        row["flaky"] = None if flaky is None else _flaky(*flaky)
    elif marks == "zero":
        row["flaky"] = None
    return row


def _state_fixture(marks: str, clear: bool = False, tick: Any = None,
                   muted: int = 3) -> Dict[str, Any]:
    """The Home walk's fixture. ``clear`` empties the three queues "All
    clear" is about, with *muted* (and, marked, 6) failing tests set
    aside: *muted* new and *muted* plus three still failing."""
    headline = _headline(True)
    status = headline["status"]
    status.update({"muted_new_failures": muted,
                   "muted_still_failing": muted + 3 if muted else 0})
    if marks != "older":
        flaky = marks == "flaky"
        status.update({
            "flaking": 6 if flaky else 0,
            "flaking_new_failures": 4 if flaky else 0,
            "flaking_still_failing": 2 if flaky else 0,
            "flaky_total": 9 if flaky else 0})
        for entry, count in zip(headline["by_environment"], (3, 0)):
            entry["flaking"] = count if flaky else 0
        headline["by_environment"].append({
            "environment": "env-c", "total_tests": 10, "failed": 0,
            "new_failures": 0, "unexpected_passes": 0, "not_run": 0,
            "muted": 0, "assigned": 0, "flaking": 2 if flaky else 0})
        headline["top_failing_scripts"][0]["flaking"] = 2 if flaky else 0
    rows = {name: _state_row(name, marks) for name, *_ in _STATE_ROWS}
    queues = {
        "new_failures": [rows["over"], rows["plain"]],
        "still_failing": [rows["over"], dict(rows["plain"], prev_result="FAIL")],
        "muted": [rows["muted"]],
        # The Flaking tab's queue (WP-44 item 15), asked for only where
        # the tab is drawn: never with no mark, so the base digests hold.
        "flaking": [rows["flaking"]],
        "fixed": [rows["passing"]],
        "unexpected_passes": [rows["up"]],
        "not_run": [rows["flaking"]],
        "mine": [rows["mine"]],
    }
    if clear:
        status.update({"new_failures": 0, "still_failing": 0})
        status["results"]["UNEXPECTED_PASS"] = 0
        for kind in ("new_failures", "still_failing", "unexpected_passes"):
            queues[kind] = []
            headline["queue_totals"][kind] = 0
    return {
        "ids": _index_ids(), "headline": headline, "part": _part(),
        "regions": list(_HOME_REGIONS),
        "queues": {kind: {"tests": tests, "total": len(tests)}
                   for kind, tests in queues.items()},
        "dashboard": {"tests": [rows[name] for name, *_ in _STATE_ROWS],
                      "total": len(_STATE_ROWS)},
        "tick": tick or [],
    }


#: The digests of Home's regions and every queue tab as the base of WP-44
#: item 7 drew them (commit 531348e, the WP-43 DOM), captured over the
#: shim from that commit's static files before any of item 7 was
#: written, keyed by (clear, muted). The older payload and a scope with
#: ``flaky_total: 0`` must still draw exactly these.
_BASE_HOME_DIGESTS = {
    (False, 3):
        "ef1a9e2433b2bc928c6e04c450007b0a216f396f84142897130142195f90910e",
    (True, 3):
        "a02dbc651b442c4ac6456975b9d564761479fdb5125a381f0654370ad97824cf",
    (True, 0):
        "fafe7bb5226953eaabaad858df0b8446d47a9acf2b58115333bf7bc0b677d7d4",
}  # type: Dict[Tuple[bool, int], str]


def _home_digest(answer: Dict[str, Any]) -> str:
    blob = json.dumps([answer["regions"], answer["queues"]], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _dom_text(node: Any) -> str:
    """A serialized node's textContent."""
    if node[0] == "#":
        return str(node[1])
    return "".join(_dom_text(kid) for kid in node[7]) + str(node[6] or "")


def _dom_walk(node: Any) -> List[Any]:
    """A serialized node's element descendants, in document order."""
    found = []  # type: List[Any]
    for kid in node[7]:
        if kid[0] != "#":
            found.append(kid)
            found.extend(_dom_walk(kid))
    return found


def _row_shape(tr: Any) -> List[Any]:
    """A row's edge class, then each chip and pill in it, in order: the
    class and text, and a flaky pill's title."""
    marks = []  # type: List[Any]
    for node in _dom_walk(tr):
        classes = str(node[1]).split()
        if "chip" in classes or "mute-tag" in classes:
            marks.append([node[1], _dom_text(node)])
        elif "flaky-tag" in classes:
            marks.append([node[1], _dom_text(node), node[3]])
    return [tr[1], marks]


def _ghost_offenders(source: str) -> List[str]:
    """Calls in *source* that draw a row's displayed result as its
    previous one: a ghostChip(), or the "was" argument of a
    resultTransition(), fed from displayResult() or rowResult()."""
    displayed = re.compile(r"\b(?:displayResult|rowResult)\s*\(")
    code = _strip_comments(source)
    found = [
        "ghostChip(" + args + ")"
        for args in _call_arguments(code, "ghostChip")
        if displayed.search(args)
    ]
    for args in _call_arguments(code, "resultTransition"):
        depth = 0
        start = 0
        parts = []  # type: List[str]
        for index, char in enumerate(args):
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(args[start:index])
                start = index + 1
        parts.append(args[start:])
        if len(parts) > 1 and displayed.search(parts[1]):
            found.append("resultTransition(" + args + ")")
    return found


class AllClearSetAsideTest(unittest.TestCase):
    """WP-44 item 7 (design §3 U6, §13): "All clear" names what it leaves
    out, "Set aside: 3 muted · 4 flaking", so a clear board still says
    what is tolerated. Each part opens the tab that lists it: "3 muted"
    the Muted tab, "4 flaking" the Flaking tab."""

    def test_the_line_ships_hidden_inside_the_banner(self) -> None:
        html = read("index.html")
        banner = html[html.index('id="all-clear"'):]
        banner = banner[:banner.index('<div class="table-wrap">')]
        self.assertIn('<div id="all-clear-aside" class="muted" hidden></div>',
                      banner)

    def test_it_is_drawn_from_the_set_aside_words(self) -> None:
        app = _strip_comments(read("app.js"))
        self.assertIn("renderAllClearAside();",
                      _function_body(app, "function renderQueueTabs()"))
        body = _function_body(app, "function renderAllClearAside()")
        self.assertRegex(body, r"setAsideWords\(\s*\(status\.muted_new_failures"
                               r" \|\| 0\) \+ \(status\.muted_still_failing"
                               r" \|\| 0\),\s*status\.flaking\)")
        self.assertIn("aside.hidden = words.length === 0;", body)
        self.assertIn('const muted = part.key === "muted";', body)
        # Each part a link-styled button opening its tab (design §13: the
        # flaking part opens the Flaking tab, no longer the browse
        # filter), its words through textContent, never markup.
        self.assertIn('el("button", "link-btn", part.text)', body)
        self.assertIn('link.title = muted ? "Open the Muted tab" : '
                      'FLAKING_LINK_TITLE;', body)
        self.assertIn('openQueue(muted ? "muted" : FLAKING_QUEUE)', body)
        self.assertNotIn("innerHTML", body)
        for gone in ("function flakingLink(", "openFlakingInBrowse"):
            self.assertNotIn(gone, app)
        api = _strip_comments(read("api.js"))
        self.assertIn('export const FLAKING_LINK_TITLE = "Open the Flaking '
                      'tab";', api)
        words = _function_body(api, "export function setAsideWords(")
        self.assertIn("setAsideParts(muted, flaking)", words)

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_flaking_part_opens_the_flaking_tab(self) -> None:
        """"6 flaking" opens the Flaking tab and asks for its queue, and
        nothing else: the browse filters are left as they were. The
        scripts chart's bar still opens All tests (FlakingFilterTest)."""
        load, opened = _lists_walk("flaky", [["asideLink", "6 flaking"]],
                                   clear=True)
        self.assertEqual(opened["asked"], [_FLAKING_QUEUE_ASK])
        self.assertEqual([tab[0] for tab in opened["tabs"]
                          if tab[2] == "true"], ["Flaking"])
        self.assertEqual([t[2] for t in opened["toggles"]],
                         [t[2] for t in load["toggles"]])
        self.assertEqual(opened["head"], _FLAKING_HEAD)


class PromotionTransitionTest(unittest.TestCase):
    """WP-44 item 7 (design §3 U6, decision 25): on the night a marked
    test goes over its cadence its New failures row reads "FLAKING →
    FAIL" with the "flaky" pill: the failure before was within the
    cadence. The previous result is previousShown(), never the row's own
    displayResult(), which says what the test is now."""

    def test_previousShown_is_the_one_promotion_rule(self) -> None:
        api = _strip_comments(read("api.js"))
        body = _function_body(api, "export function previousShown(entry)")
        for clause in ('flaky.state === "failing"', 'entry.result === "FAIL"',
                       'entry.prev_result === "FAIL"',
                       "!(entry.mute && entry.mute.live)"):
            self.assertIn(clause, body)
        # The word comes from displayResult(), the one place it is made.
        self.assertIn('displayResult(entry.prev_result, "flaking", false)',
                      body)
        self.assertIn(": entry.prev_result;", body)

    def test_both_transition_columns_use_it(self) -> None:
        columns = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertEqual(columns.count(
            'el("td"), previousShown(entry), shown), entry)'), 2)
        self.assertNotIn("entry.prev_result", columns)

    def test_ghost_chips_never_take_the_displayed_result(self) -> None:
        offenders = {name: _ghost_offenders(source)
                     for name, source in scripts().items()}
        self.assertEqual({k: v for k, v in offenders.items() if v}, {})

    def test_the_ghost_detector_can_fail(self) -> None:
        for planted in (
                "wrap.appendChild(ghostChip(displayResult(r, s, m)));",
                'resultTransition(el("td"), rowResult(entry), shown);',
                'resultTransition(el("td"),\n  displayResult(entry.prev_result,'
                ' s, m), shown);'):
            self.assertTrue(_ghost_offenders(planted), planted)
        for fine in (
                'resultTransition(el("td"), previousShown(entry), shown);',
                'resultTransition(el("td"), entry.prev_result, rowResult(e));',
                "ghostChip(previous)"):
            self.assertEqual(_ghost_offenders(fine), [], fine)


#: Calls into api.js's helpers over the shim: the real module, each
#: ``[name, args]`` of the fixture answered with its JSON-able result.
#: argv: repo root, fixture path.
_API_SHIM_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
installDom([], "http://127.0.0.1/index.html");
const api = await import(pathToFileURL(root + "/static/api.js").href);
const out = fixture.calls.map(([name, args]) => {
  const value = api[name](...args);
  return value === undefined ? "undefined" : value;
});
await driverAnswer(out);
"""


def _api_calls(calls: List[Tuple[str, List[Any]]]) -> List[Any]:
    answer = _shim({"calls": [[name, args] for name, args in calls]},
                   _API_SHIM_DRIVER)  # type: Any
    return list(answer)


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class FlakyStateDomTest(unittest.TestCase):
    """WP-44 item 7 over the DOM shim: Home, the real app.js, with every
    queue tab opened, U9's five states on the rows, the tiles, "All
    clear", the two bar charts and the selection bar; Open Actions' row;
    api.js's set-aside helpers byte for byte. No browser; it proves the
    DOM drawn and the requests made, not colour or layout."""

    _walks = {}  # type: Dict[Tuple[str, bool, int], Dict[str, Any]]

    def _walk(self, marks: str, clear: bool = False,
              muted: int = 3) -> Dict[str, Any]:
        key = (marks, clear, muted)
        if key not in self._walks:
            self._walks[key] = _shim(
                _state_fixture(marks, clear, [[0, 1], [1, 5]], muted),
                _HOME_STATE_DRIVER)
        return self._walks[key]

    def test_no_marks_draws_the_base_dom(self) -> None:
        """The older payload (no new field anywhere) and a scope with no
        marks (``flaky: null``, every new figure 0) draw the WP-43 DOM,
        byte for byte, in the tiles, "All clear", both bar charts, the
        browse rows and every queue tab, with the same requests."""
        for marks in ("older", "zero"):
            for (clear, muted), digest in _BASE_HOME_DIGESTS.items():
                with self.subTest(marks=marks, clear=clear, muted=muted):
                    answer = self._walk(marks, clear, muted)
                    self.assertEqual(_home_digest(answer), digest)
                    self.assertEqual(answer["ticked"],
                                     ["Mark as flaky", "Mark as flaky"])
        # The one line the design adds with no mark: "All clear" names
        # the muted failures it leaves out (design §3 U6).
        self.assertTrue(self._walk("zero", True, 0)["aside"]["hidden"])
        self.assertEqual(self._walk("zero", True, 0)["aside"]["text"], "")
        self.assertEqual(self._walk("older", True, 3)["aside"]["text"],
                         "Set aside: 9 muted")

    def test_no_request_is_added(self) -> None:
        """Home stays at four data requests (with the product list and
        the user list): the figures ride the headline and the rows. The
        pin is now: three in the first batch, then the trend's split once
        the headline has been drawn, never on the first paint."""
        asked = ["api/products", "api/users", "headline", "queue",
                 "api/dashboard", "assigned_trend"]
        for marks in ("older", "zero", "flaky"):
            with self.subTest(marks=marks):
                self.assertEqual(self._walk(marks)["asked"], asked)

    def test_the_five_states_on_the_browse_rows(self) -> None:
        rows = [_row_shape(tr) for tr in
                self._walk("flaky")["regions"]["dashboard-body"][7]]
        self.assertEqual(rows, [
            # Under its cadence, no mute: FLAKING, its own edge, no pill.
            ["result-flaking", [["chip result-flaking", "FLAKING"]]],
            # Over its cadence: FAIL, red, the pill.
            ["result-fail", [
                ["chip result-fail", "FAIL"],
                ["flaky-tag", "flaky", "Failing: its last 3 runs all "
                 "failed; marked flaky, 3 in a row"]]],
            # Marked and passing.
            ["result-pass", [
                ["chip result-pass", "PASS"],
                ["flaky-tag", "flaky", "Marked flaky: counts as failing "
                 "once its last 3 runs have failed"]]],
            # Marked, at an unexpected pass: its own chip, the pill.
            ["result-unexpected-pass", [
                ["chip result-unexpected-pass", "UNEXPECTED_PASS"],
                ["flaky-tag", "flaky", "Marked flaky: counts as failing "
                 "once its last 3 runs have failed"]]],
            # A live mute wins: the result, "muted", "flaky".
            ["result-fail", [
                ["chip result-fail", "FAIL"], ["mute-tag", "muted"],
                ["flaky-tag", "flaky", "Marked flaky; the mute comes first "
                 "until it runs out"]]],
            # Unmarked: today's row.
            ["result-fail", [["chip result-fail", "FAIL"]]],
            # Assigned and flaking: FLAKING, as anywhere.
            ["result-flaking", [["chip result-flaking", "FLAKING"]]],
        ])

    def test_every_queue_tab_shows_one_result_per_row(self) -> None:
        queues = self._walk("flaky")["queues"]

        def rows(label: str) -> List[Any]:
            return [_row_shape(tr) for tr in queues[label][2][7]]

        over_title = ("Failing: its last 3 runs all failed; marked flaky, "
                      "3 in a row")
        self.assertEqual(rows("Not run"), [
            ["result-flaking", [["chip result-flaking", "FLAKING"]]]])
        self.assertEqual(rows("My actions"), [
            ["result-flaking", [["chip result-flaking", "FLAKING"]]]])
        # The invariant queues state their chip once; the pill follows
        # the test's name.
        self.assertEqual(rows("Still failing"), [
            ["result-fail", [["flaky-tag", "flaky", over_title]]],
            ["result-fail", []]])
        self.assertEqual(rows("Stale annotations"), [
            ["result-unexpected-pass", [["flaky-tag", "flaky", "Marked "
             "flaky: counts as failing once its last 3 runs have failed"]]]])
        # The Flaking tab (item 15): every row reads FLAKING, said once
        # above the table, so the row has its edge and no chip or pill.
        self.assertEqual(rows("Flaking"), [["result-flaking", []]])
        # A live mute: raw before and now, the pill.
        self.assertEqual(rows("Muted"), [["result-fail", [
            ["chip chip-ghost result-fail", "FAIL"],
            ["chip result-fail", "FAIL"],
            ["flaky-tag", "flaky", "Marked flaky; the mute comes first "
             "until it runs out"]]]])

    def test_the_promotion_row_and_the_fix(self) -> None:
        queues = self._walk("flaky")["queues"]
        new = [_row_shape(tr) for tr in queues["New failures"][2][7]]
        self.assertEqual(new, [
            ["result-fail", [
                ["chip chip-ghost result-flaking", "FLAKING"],
                ["chip result-fail", "FAIL"],
                ["flaky-tag", "flaky", "Failing: its last 3 runs all "
                 "failed; marked flaky, 3 in a row"]]],
            ["result-fail", [["chip chip-ghost result-pass", "PASS"],
                             ["chip result-fail", "FAIL"]]]])
        fixed = [_row_shape(tr) for tr in queues["Fixed"][2][7]]
        self.assertEqual(fixed, [["result-pass", [
            ["chip chip-ghost result-fail", "FAIL"],
            ["chip result-pass", "PASS"],
            ["flaky-tag", "flaky", "Marked flaky: counts as failing once "
             "its last 3 runs have failed"]]]])

    def test_the_tiles_name_what_is_set_aside(self) -> None:
        tiles = self._walk("flaky")["regions"]["stat-tiles"][7]
        subs = {_dom_text(tile[7][0]): _dom_text(tile[7][2])
                for tile in tiles}
        self.assertEqual(
            subs["New failures"],
            "were passing or flaking before · +3 muted · +4 flaking")
        self.assertEqual(subs["Still failing"],
                         "failed before too · +6 muted · +2 flaking")
        # Tiles stay buttons; the suffix is their text.
        self.assertEqual([tile[0] for tile in tiles[2:4]],
                         ["BUTTON", "BUTTON"])
        self.assertFalse(any(node[0] in ("A", "BUTTON")
                             for tile in tiles for node in _dom_walk(tile)))

    def test_all_clear_names_what_is_set_aside(self) -> None:
        aside = self._walk("flaky", True)["aside"]
        self.assertFalse(aside["hidden"])
        self.assertEqual(aside["text"], "Set aside: 9 muted · 6 flaking")
        # Each part is a link to the tab that lists it: "9 muted" the
        # Muted tab, "6 flaking" the Flaking tab (design §13).
        self.assertEqual(aside["parts"], [
            "#:Set aside: ", "BUTTON:9 muted", "#: · ", "BUTTON:6 flaking"])
        self.assertEqual(aside["opened"], ["Muted"])
        flaking_only = self._walk("flaky", True, 0)["aside"]
        self.assertEqual(flaking_only["parts"],
                         ["#:Set aside: ", "BUTTON:6 flaking"])
        self.assertEqual(flaking_only["opened"], ["Flaking"])

    def test_the_bars_gain_a_flaking_segment_and_key(self) -> None:
        regions = self._walk("flaky")["regions"]
        env = regions["env-chart"][7]
        self.assertEqual([_dom_text(item) for item in env[0][7]],
                         ["Unassigned", "Assigned", "Muted", "Flaking"])
        self.assertEqual(
            [str(item[7][0][1]) for item in env[0][7]][-1],
            "legend-swatch swatch-flaking")
        bars = [row for row in env if row[1] == "bar-row"]
        self.assertEqual([row[4] for row in bars], [
            "env-a: 14 failing, 8 unassigned, 6 assigned, 4 muted, "
            "3 flaking",
            "env-b: 0 failing, 2 muted",
            # All flaking: shown as an all-muted environment is, its
            # failing figure 0.
            "env-c: 0 failing, 2 flaking"])
        fills = [[fill[1] for fill in row[7][1][7]] for row in bars]
        self.assertEqual(fills[0], [
            "bar-fill fill-fail bar-fill-open",
            "bar-fill bar-assigned bar-fill-open",
            "bar-fill bar-muted bar-fill-open", "bar-fill bar-flaking"])
        self.assertEqual(fills[2], ["bar-fill fill-fail bar-fill-open",
                                    "bar-fill bar-flaking"])
        scripts_chart = regions["scripts-chart"][7]
        self.assertEqual([_dom_text(item) for item in scripts_chart[0][7]],
                         ["Unassigned", "Assigned", "Muted", "Flaking"])
        self.assertEqual(scripts_chart[1][4],
                         "s1.py in env-a: 5 failing tests, 3 unassigned, "
                         "2 assigned, 1 muted, 2 flaking")

    def test_the_bar_offers_set_cadence_for_marked_rows_only(self) -> None:
        """Item 5's Set cadence is reachable now that rows carry the
        mark: two marked rows ticked offer it; a marked and an unmarked
        row offer Mark as flaky."""
        self.assertEqual(self._walk("flaky")["ticked"],
                         ["Set cadence", "Mark as flaky"])

    def test_an_assigned_flaking_row_on_open_actions(self) -> None:
        def first_row(row: Dict[str, Any], streams: Any = None) -> Any:
            return _shim({"ids": _actions_ids(), "names": _names_part([]),
                          "row": row, "namesFailures": 0,
                          "streams": streams or {}},
                         _ACTIONS_SHIM_DRIVER)["firstRow"]

        # Assigned from a build: the compare strip stays raw (decision 9)
        # and the displayed result is said beside it, in words.
        build = {"4": {"id": 4, "kind": "build", "name": "2026.9.0",
                       "product": ""}}
        from_build = dict(_ACTIONS_ROW, assignee="dev1",
                          assignment_stream_id=4, origin_result="FAIL")
        self.assertEqual(first_row(dict(
            from_build, flaky=_flaky("flaking", 1)), build), [
            "result-flaking", "chip-transition:mainlineFAILbuild:2026.9.0FAIL",
            "chip result-flaking:FLAKING"])
        for flaky in (None, _flaky("failing", 3, 3)):
            shape = first_row(dict(from_build, flaky=flaky), build)
            self.assertEqual(shape[:2], [
                "result-fail",
                "chip-transition:mainlineFAILbuild:2026.9.0FAIL"])
            self.assertNotIn("chip result-fail:FAIL", shape)

        self.assertEqual(first_row(dict(
            _ACTIONS_ROW, assignee="dev1", flaky=_flaky("flaking", 1))),
            ["result-flaking", "chip result-flaking:FLAKING"])
        self.assertEqual(first_row(dict(
            _ACTIONS_ROW, flaky=_flaky("failing", 3, 3))),
            ["result-fail", "chip result-fail:FAIL", "flaky-tag:flaky"])
        for row in (_ACTIONS_ROW, dict(_ACTIONS_ROW, flaky=None)):
            self.assertEqual(first_row(row),
                             ["result-fail", "chip result-fail:FAIL"])

    def test_the_set_aside_helpers_byte_for_byte(self) -> None:
        """With nothing flaking each prints what mutedSuffix() and
        failingWithMuted() printed before them."""
        promoted = {"result": "FAIL", "prev_result": "FAIL", "mute": None,
                    "flaky": _flaky("failing", 3, 3)}
        answer = _api_calls([
            ("setAsideSuffix", [3, 0]), ("setAsideSuffix", [3, 4]),
            ("setAsideSuffix", [0, 4]), ("setAsideSuffix", [0, None]),
            ("failingWithSetAside", [12, 3, None]),
            ("failingWithSetAside", [1234, 0, None]),
            ("failingWithSetAside", [12, 3, 4]),
            ("setAsideBracket", [0, 4]), ("setAsideBracket", [None, None]),
            ("setAsideWords", [3, 4]), ("setAsideWords", [0, 0]),
            ("newFailuresSub", [9]), ("newFailuresSub", [0]),
            ("newFailuresSub", [None]),
            ("previousShown", [promoted]),
            ("previousShown", [dict(promoted, mute=_LIVE_MUTE)]),
            ("previousShown", [dict(promoted, prev_result="PASS")]),
            ("previousShown", [dict(promoted, flaky=_flaky("flaking", 2))]),
            ("previousShown", [dict(promoted, flaky=None)]),
            ("rowResult", [dict(promoted, flaky=_flaky("flaking", 2))]),
            ("rowResult", [dict(promoted, flaky=_flaky("flaking", 2),
                                mute=_LIVE_MUTE)]),
            ("rowResult", [{"result": "FAIL"}]),
            ("failingSplit", [5, 2, 1, None]), ("failingSplit", [5, 2, 1, 0]),
        ])
        self.assertEqual(answer[:14], [
            "+3 muted", "+3 muted · +4 flaking", "+4 flaking", "",
            "12 (+3 muted)", "1,234", "12 (+3 muted, +4 flaking)",
            " (+4 flaking)", "",
            [{"key": "muted", "text": "3 muted"},
             {"key": "flaking", "text": "4 flaking"}], [],
            "were passing or flaking before", "were passing before",
            "were passing before"])
        self.assertEqual(answer[14:19],
                         ["FLAKING", "FAIL", "PASS", "FAIL", "FAIL"])
        self.assertEqual(answer[19:22], ["FLAKING", "FAIL", "FAIL"])
        unkeyed, keyed = answer[22], answer[23]
        self.assertEqual([row["label"] for row in unkeyed["rows"]],
                         ["unassigned", "assigned", "muted"])
        self.assertEqual(unkeyed["words"], "3 unassigned, 2 assigned, 1 muted")
        self.assertEqual([row["label"] for row in keyed["rows"]],
                         ["unassigned", "assigned", "muted", "flaking"])
        self.assertEqual(keyed["rows"][-1],
                         {"swatchClass": "swatch-flaking", "label": "flaking",
                          "value": "0"})
        self.assertEqual(keyed["words"], unkeyed["words"])


#: Home over the shim, driven by a list of steps (WP-44): every
#: data request is recorded with its query, and a snapshot of the parts
#: the lists draw is taken after the load and after each step. argv: repo
#: root, fixture path.
_LISTS_HOME_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom, Element } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, fixture.url);
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
window.localStorage.setItem("testboard.username", "dev1");
Element.prototype.focus = function () {};
// The shim keeps the queue's header row (#queue-head-row) apart from its
// table, so attachSorting() would find no header buttons there; its one
// query is pointed at that row, so a header click sorts as in a browser.
const ownQuery = Element.prototype.querySelectorAll;
Element.prototype.querySelectorAll = function (selector) {
  if (selector === "thead .sort-btn" && this === byId["queue-table"]) {
    return byId["queue-head-row"].find((c) => c.className === "sort-btn");
  }
  return ownQuery.call(this, selector);
};
let asked = [];
// An act (any request that is not a GET) moves the board on: from then
// on the headline is `headlineAfter` and the queues are `queuesAfter`,
// where the fixture gives them; every act is kept in `posts`.
let headline = fixture.headline;
let queues = fixture.queues;
let posts = [];
globalThis.fetch = (url, opts) => {
  const u = new URL(String(url), "http://127.0.0.1/");
  const path = u.pathname.replace(/^\//, "");
  const part = u.searchParams.get("parts");
  if (path !== "api/users" && path !== "api/products") {
    asked.push(path + u.search);
  }
  if (opts && opts.method && opts.method !== "GET") {
    headline = fixture.headlineAfter || headline;
    queues = fixture.queuesAfter || queues;
    posts.push([path, opts.body ? JSON.parse(opts.body) : null]);
  }
  let body = {};
  if (part === "headline") {
    body = headline;
  } else if (part === "assigned_trend") {
    // `parts`, where given, answers each split request in turn.
    body = fixture.parts && fixture.parts.length
      ? fixture.parts.shift() : fixture.part;
  } else if (part === "queue") {
    // `queueRow`, where given, is every queue's one row, whatever kind
    // is asked for (QueueTabReviewTest).
    body = { generated_at: fixture.headline.generated_at,
      stale_before: fixture.headline.stale_before,
      queue: fixture.queueRow ? { tests: [fixture.queueRow], total: 1 }
        : queues[u.searchParams.get("queue")] || { tests: [], total: 0 } };
  } else if (path.startsWith("api/runs/")) {
    body = { output: "boom" };
  } else if (path === "api/dashboard") {
    body = fixture.dashboard;
  } else if (path === "api/users") {
    body = { users: [] };
  }
  const answer = { ok: true, status: 200,
    text: async () => JSON.stringify(body) };
  // `headlineDelay` holds every headline's answer back, so what is asked
  // for before it has been drawn can be seen; `delays` gives the
  // headline and the split one hold each, in the order they are asked.
  const queued = (fixture.delays || {})[part];
  const held = queued && queued.length ? queued.shift()
    : (part === "headline" ? fixture.headlineDelay || 0 : 0);
  events.push("ask:" + (part || path));
  return new Promise((resolve) => setTimeout(() => {
    events.push("land:" + (part || path));
    resolve(answer);
  }, held));
};
let events = [];
const delay = fixture.headlineDelay || 0;
const settle = (wait) => new Promise((resolve) => setTimeout(
  resolve, wait === undefined || wait === null ? 50 + delay : wait));
const tabLabel = (tab) => tab.children[0].textContent;
const legend = (node) => {
  const row = node.children.find((c) => c.className === "chart-legend");
  return row ? row.children.map((item) => item.textContent) : null;
};
const describe = (c) => c.className + ":" + c.textContent
  + (c.title ? ":" + c.title : "");
// The queue's header after the selection column: a sortable one as
// "sort:" and its key (the shim drops a button's own text once the
// arrow span is appended to it, as a browser would not), the rest by
// their text.
const headCells = () => byId["queue-head-row"].children.slice(1).map((th) => {
  const sort = th.children.find((c) => c.className === "sort-btn");
  return sort ? "sort:" + sort.dataset.key : th.textContent;
});
// The queue's test rows, and the open Review panel's row if any.
const testRows = () => byId["queue-body"].children.filter(
  (tr) => tr.className !== "review-row");
const panelRow = () => byId["queue-body"].children.find(
  (tr) => tr.className === "review-row") || null;
// What an open Review panel offers: its groups' labels, and the Flaky
// group's line (the sentence, then the small print) and controls.
const panelShape = () => {
  const panel = panelRow();
  if (!panel) {
    return null;
  }
  const labels = panel.find((c) => c.className === "review-label");
  const flaky = labels.find((c) => c.textContent === "Flaky");
  return {
    labels: labels.map((c) => c.textContent),
    flaky: flaky ? flaky.parentNode.children.slice(1).map((c) => [c.tagName,
      c.tagName === "SELECT" ? c.value
        : c.tagName === "SPAN" ? c.children.map((x) => x.textContent)
          : c.textContent]) : null,
  };
};
const snap = () => {
  const note = byId["queue-result-note"];
  const firstNight = byId["trend-table-body"].children[0];
  const out = {
    asked,
    posts,
    tabs: byId["queue-tabs"].children.map((t) => [tabLabel(t),
      t.children[1].textContent, t.attributes["aria-selected"]]),
    toggles: byId["result-toggles"].children.map((b) => [b.textContent,
      b.hidden, b.attributes["aria-pressed"], b.title || "", b.className]),
    head: headCells(),
    rows: testRows().map((tr) => ({
      edge: tr.className,
      cells: tr.children.slice(1, 6).map((td) => td.textContent),
      titles: tr.children.slice(1, 6).map((td) => td.title || ""),
      take: tr.find((c) => c.className === "take-btn").length,
      review: tr.find((c) => c.className === "review-btn")
        .map((c) => c.textContent),
      marks: tr.find((c) => /^(chip|mute-tag|flaky-tag)/
        .test(String(c.className))).map(describe),
    })),
    panel: panelShape(),
    // The header showing a sort arrow, as "key:direction".
    sorted: byId["queue-head-row"].children.slice(1).filter((th) =>
      th.attributes["aria-sort"] && th.attributes["aria-sort"] !== "none")
      .map((th) => th.children[0].dataset.key + ":"
        + th.attributes["aria-sort"]),
    tableHidden: byId["queue-table"].hidden,
    sortOff: byId["queue-table"].classList.contains("sort-disabled"),
    empty: [byId["queue-empty"].hidden, byId["queue-empty"].textContent],
    cap: [byId["queue-cap-note"].hidden, byId["queue-cap-note"].textContent],
    // The one-state line above the table: hidden, its text, and every
    // chip and control in it.
    note: [note.hidden, note.textContent,
      note.find((c) => c.tagName === "BUTTON"
        || /^chip/.test(String(c.className))).map(describe)],
    trendLegend: legend(byId["trend-chart"]),
    trendDim: byId["trend-card"].classList.contains("is-loading"),
    events,
    trendRow: firstNight ? firstNight.children.map((td) => td.textContent)
      : null,
    caption: [byId["trend-split-caption"].hidden,
      byId["trend-mark-caption"].hidden,
      byId["trend-mark-caption"].textContent],
    aside: byId["all-clear-aside"].children.map(
      (c) => (c.tagName ? c.tagName : "#") + ":" + c.textContent),
  };
  asked = [];
  events = [];
  posts = [];
  return out;
};
await import(pathToFileURL(root + "/static/app.js").href);
let early = null;
if (delay) {
  await new Promise((resolve) => setTimeout(resolve, delay / 2));
  early = asked.slice();
}
await new Promise((resolve) => setTimeout(resolve, 150 + delay));
const snaps = [Object.assign(snap(), { early })];
const bar = () => document.body.children.find(
  (c) => c.className === "selection-bar");
for (const [action, arg, wait] of fixture.steps) {
  if (action === "wait") {
    // Nothing but the wait: a snapshot of what has landed by then.
  } else if (action === "tab") {
    await byId["queue-tabs"].children.find((t) => tabLabel(t) === arg).click();
  } else if (action === "click") {
    await byId[arg].click();
  } else if (action === "asideLink") {
    await byId["all-clear-aside"].children.find(
      (c) => c.tagName === "BUTTON" && c.textContent === arg).click();
  } else if (action === "scriptBar") {
    await byId["scripts-chart"].children.filter(
      (c) => c.className === "bar-row")[arg].click();
  } else if (action === "take") {
    await byId["queue-body"].find((c) => c.className === "take-btn")[0]
      .click();
  } else if (action === "env") {
    await byId["env-updated"].children.find(
      (c) => c.tagName === "BUTTON").click();
  } else if (action === "assign") {
    const select = byId["queue-body"].find(
      (c) => String(c.className).startsWith("assignee-select"))[0];
    select.value = arg;
    await select.dispatch("change");
  } else if (action === "sort") {
    // A click on the header whose sort key is arg.
    await byId["queue-head-row"].find((c) => c.className === "sort-btn"
      && c.dataset.key === arg)[0].click();
  } else if (action === "review") {
    // Open the Review panel of the arg-th row, by its Output column.
    await testRows()[arg].find((c) => c.className === "review-btn")[0]
      .click();
  } else if (action === "panelUnmark") {
    await panelRow().find((c) => c.tagName === "BUTTON"
      && c.textContent === "Unmark")[0].click();
  } else if (action === "barUnmark") {
    const boxes = testRows().map((tr) => tr.children[0].children[0]);
    for (const index of arg) {
      boxes[index].checked = true;
      await boxes[index].dispatch("change");
    }
    await bar().children.find(
      (c) => c.className === "selection-unmark-btn").click();
  } else if (action === "reviewEach") {
    // Every tab drawn, in turn: its rows' Review buttons, and what one
    // click on the first opens (QueueTabReviewTest).
    const each = [];
    const labels = byId["queue-tabs"].children.map(tabLabel);
    for (const label of labels) {
      await byId["queue-tabs"].children.find(
        (t) => tabLabel(t) === label).click();
      await settle(wait);
      const rows = testRows();
      const buttons = rows.map((tr) => tr.find(
        (c) => c.className === "review-btn").length);
      const review = rows.length && buttons[0]
        ? rows[0].find((c) => c.className === "review-btn")[0] : null;
      if (review) {
        await review.click();
        await settle(wait);
      }
      each.push({ tab: label, head: headCells(), buttons,
        panel: panelShape() });
      if (review) {
        // Closed again: every tab's one row is the same test, and a panel
        // left open would be reopened on the next tab's row by itself.
        await review.click();
      }
    }
    snaps.push({ each });
    continue;
  } else if (action === "tick") {
    const boxes = testRows().map((tr) => tr.children[0].children[0]);
    for (const index of arg) {
      boxes[index].checked = true;
      await boxes[index].dispatch("change");
    }
    const flakyBtn = bar().children.find(
      (c) => c.className === "selection-flaky-btn");
    const unmark = bar().children.find(
      (c) => c.className === "selection-unmark-btn");
    snaps.push({ ticked: [flakyBtn.textContent,
      unmark ? unmark.disabled : null] });
    for (const index of arg) {
      boxes[index].checked = false;
      await boxes[index].dispatch("change");
    }
    continue;
  }
  await settle(wait);
  snaps.push(snap());
}
await driverAnswer(snaps);
"""


def _flaking_row(name: str, streak: int, cadence: int = 3,
                 **extra: Any) -> Dict[str, Any]:
    """One row of ``queue=flaking`` in the server's shape (design §14): a
    queue row, key for key what ``still_failing`` returns, FAIL, no live
    mute, its ``flaky`` never null: state "flaking", ``streak`` at least
    1 and below ``cadence``."""
    row = _state_row("plain", "flaky")
    row.update({"test_name": name, "prev_result": "FAIL",
                "flaky": dict(_flaky("flaking", streak), cadence=cadence)})
    row.update(extra)
    return row


#: The Flaking tab's rows in the server's order, nearest to their cadence
#: first (one run left, one run left, two), then environment, script and
#: name; not alphabetical, so a page that re-sorted them would show it.
_FLAKING_ROWS = [
    _flaking_row("dns_timeout", 2),
    _flaking_row("login_retry", 4, 5, environment="env-b", assignee="dev1",
                 latest_comment={"text": "retry storm", "author": "dev1",
                                 "created_at": "2026-10-02T09:00:00"}),
    _flaking_row("cache_warm", 1),
]

#: The Flaking tab's one request, as any queue's: its queue part.
_FLAKING_QUEUE_ASK = "api/summary?parts=queue&queue=flaking&assignee=dev1"

#: The Flaking tab's columns: Still failing's, the Streak where its two
#: times stand (design §14), the Output column's Review last. Sortable
#: ones by their key (the lists driver's headCells()): Test, Streak (by
#: the runs left), Latest comment, Assignee, Output.
_FLAKING_HEAD = ["sort:test_name", "sort:runs_left", "Latest comment",
                 "sort:assignee", "Output"]


def _trend_part(flaking: Any) -> Dict[str, Any]:
    """``parts=assigned_trend`` with FAIL_FLAKING; *flaking* None is a
    server from before it (no FAIL_FLAKING)."""
    part = _part()
    for night in part["trend"]["nights"]:
        if flaking is not None:
            night["FAIL_FLAKING"] = flaking
    return part


def _lists_walk(marks: str, steps: List[List[Any]], clear: bool = False,
                url: str = "http://127.0.0.1/index.html",
                rows: Any = None, total: Any = None, part: Any = "flaking",
                flaking: Any = None, flaking_after: Any = None,
                headline_delay: int = 0, delays: Any = None,
                parts: Any = None, rows_after: Any = None,
                queue_row: Any = None,
                root: str = REPO_DIR) -> List[Dict[str, Any]]:
    """Home over the shim with the lists driver. *marks* as
    ``_state_fixture`` ("flaky", "zero", "older"); *rows* the Flaking
    tab's queue (default _FLAKING_ROWS) and *total* its ``total``
    (default its length), *rows_after* its rows once an act has been
    sent; *queue_row*, where given, every queue's one row whatever kind
    is asked for; *part* the trend part ("flaking": with FAIL_FLAKING, 1
    a night; "older": a server that sends none); *flaking* overrides
    ``status.flaking``, and *flaking_after* is its value once an act has
    been sent; *headline_delay* (ms) holds every headline answer back,
    and the load's snapshot then carries ``early``, what was asked for
    halfway through that wait. *delays* holds the headline and the split
    back by request, in order (``{"headline": [0, 300]}``); *parts*
    answers each split request in turn (None: no split to be had);
    *root* is the tree whose static files are walked. A step may carry a
    third element, the ms to wait before its snapshot."""
    fixture = _state_fixture(marks, clear)
    if flaking is not None:
        fixture["headline"]["status"]["flaking"] = flaking
    if flaking_after is not None:
        after = json.loads(json.dumps(fixture["headline"]))
        after["status"]["flaking"] = flaking_after
        fixture["headlineAfter"] = after
    flaking_rows = _FLAKING_ROWS if rows is None else rows
    fixture["queues"]["flaking"] = {
        "tests": flaking_rows,
        "total": len(flaking_rows) if total is None else total}
    if rows_after is not None:
        fixture["queuesAfter"] = dict(fixture["queues"], flaking={
            "tests": rows_after, "total": len(rows_after)})
    fixture["part"] = _trend_part(1 if part == "flaking" else None)
    fixture.update({
        "url": url, "steps": steps, "headlineDelay": headline_delay,
        "delays": delays or {}, "parts": parts or [], "queueRow": queue_row,
    })
    answer = _shim(fixture, _LISTS_HOME_DRIVER, root)  # type: Any
    return list(answer)


def _query(asked: str) -> List[List[str]]:
    """The query of a recorded request as [name, value] pairs, in order."""
    query = asked.split("?", 1)[1] if "?" in asked else ""
    return [list(pair) for pair in urllib.parse.parse_qsl(query)]


def _results_of(asked: List[str]) -> List[List[str]]:
    """The ``result=`` values of every browse rows request in *asked*."""
    return [[value for name, value in _query(entry) if name == "result"]
            for entry in asked if entry.startswith("api/dashboard")]


#: Functions of app.js that must never name the Flaking tab's rows: the
#: refresh, the coalescer and the first paint around them. They reach
#: its queue only as the open tab's, through loadQueue(state.activeQueue)
#: and the coalescer's rowsRequest(kind), as any tab's (WP-44 item 15).
_NO_FLAKY_REQUEST_IN = ("async function refreshAll(",
                        "async function fetchSummary()",
                        "function renderHeadline()", "function init()")
_FLAKY_REQUEST_TOKENS = ("api/flaky", "FLAKING_QUEUE")


def _flaky_request_offenders(source: str) -> List[str]:
    """The guarded functions of *source* that name the tab's request."""
    code = _strip_comments(source)
    return [signature for signature in _NO_FLAKY_REQUEST_IN
            if any(token in _function_body(code, signature)
                   for token in _FLAKY_REQUEST_TOKENS)]


#: What the Flaking tab drew before it was a queue (WP-44 items 9 and 12),
#: gone with item 15: its own request and pager, its own table and
#: columns, their wording and their rule.
_FLAKING_TAB_GONE = (
    "FLAKY_TAB", "FLAKY_PAGE", "flakyUrl", "fetchFlakyPage",
    "loadFlakyMore", "renderFlakyTable", "appendFlakyRows", "flakyColumns",
    "failedOfRuns", "queue-more", "Newest mark first", "recent_failures",
    "recent_runs")


class FlakyTabTest(unittest.TestCase):
    """WP-44 (design §13 and §14): the Flaking tab lists the tests
    flaking now, marked and failing within their cadence, and nothing
    else, and is a queue like its neighbours: its rows are the queue part
    ``queue=flaking``, asked for through queueUrl() and rowsRequest() on
    the tab's first click and as the open tab's rows after that, never by
    the first paint; drawn by renderQueueTable()'s shared loop with the
    Output column's Review on every row; Still failing's columns with the
    Streak where Failing since and Last pass stand. It is drawn only
    where ``status.flaking`` is above zero, or while it is the open tab.
    The one state is said once above the table, with the order."""

    def test_the_rows_are_a_queue_part(self) -> None:
        code = _strip_comments(read("app.js"))
        self.assertEqual(_flaky_request_offenders(read("app.js")), [])
        self.assertRegex(_function_body(code, "function rowsRequest("),
                         r"^function rowsRequest\(kind\) \{\s+return "
                         r"fetchJson\(queueUrl\(kind\)\);\s+\}$")
        self.assertIn('export const FLAKING_QUEUE = "flaking";',
                      _strip_comments(read("api.js")))
        # Nothing asks for GET /api/flaky any more: the only addresses
        # under it are the two acts' POSTs.
        for name, source in scripts().items():
            for literal in _STRING_LITERAL_RE.findall(
                    _strip_comments(source)):
                if "api/flaky" in literal:
                    self.assertIn(literal[1:-1], ("api/flaky/bulk",
                                                  "api/flaky/unmark"), name)
        for name in list(scripts()) + ["index.html", "style.css"]:
            text = read_text(name)
            for gone in _FLAKING_TAB_GONE:
                self.assertNotIn(gone, text, name + ": " + gone)

    def test_the_detector_can_fail(self) -> None:
        """Planted: the tab's request in the refresh, the coalescer and
        the first paint."""
        source = read("app.js")
        for signature, planted in (
                ("async function refreshAll(",
                 "  fetchJson(queueUrl(FLAKING_QUEUE));\n"),
                ("async function fetchSummary()",
                 '  fetchJson("api/flaky");\n'),
                ("function renderHeadline()",
                 "  loadQueue(FLAKING_QUEUE, state.requestSeq);\n")):
            at = source.index(signature)
            at = source.index("{", at) + 1
            with self.subTest(signature=signature):
                self.assertEqual(_flaky_request_offenders(
                    source[:at] + "\n" + planted + source[at:]), [signature])

    def test_the_tab_sits_beside_muted_and_shows_only_while_flaking(
            self) -> None:
        code = _strip_comments(read("app.js"))
        tabs = code[code.index("const QUEUE_TABS = ["):]
        tabs = tabs[:tabs.index("];")]
        self.assertLess(
            tabs.index('{ id: "muted"'),
            tabs.index("{ id: FLAKING_QUEUE, label: FLAKING_TAB_LABEL }"))
        self.assertLess(tabs.index("{ id: FLAKING_QUEUE"),
                        tabs.index('{ id: "fixed"'))
        self.assertIn('export const FLAKING_TAB_LABEL = "Flaking";',
                      _strip_comments(read("api.js")))
        self.assertIn("if (tab.id === FLAKING_QUEUE && !flakyTabShown()) {",
                      _function_body(code, "function renderQueueTabs()"))
        # Drawn by the tests flaking now, not by any mark (design §13):
        # `status.flaky_total` stays the FLAKING toggle's and the charts'.
        shown = _function_body(code, "function flakyTabShown()")
        self.assertIn(
            "state.activeQueue === FLAKING_QUEUE || flakingNow() > 0", shown)
        # The badge: `status.flaking` until the queue's own total lands
        # (a virtual kind, not in `queue_totals`).
        count = _function_body(code, "function queueCount(")
        self.assertLess(count.index("return queue.total;"),
                        count.index("return flakingNow();"))
        self.assertIn("state.summary.status.flaking || 0",
                      _function_body(code, "function flakingNow()"))
        for body in (shown, count):
            self.assertNotIn("flaky_total", body)
            self.assertNotIn("flakyScope()", body)

    def test_the_rows_are_drawn_by_the_shared_loop(self) -> None:
        """No branch of the tab's own anywhere in the loop, the fetch or
        the refresh: its row is built, edged and given Review as every
        queue's is."""
        code = _strip_comments(read("app.js"))
        for signature in ("function renderQueueTable()",
                          "async function loadQueue(",
                          "async function refreshQueueCounts()",
                          "function rowsRequest("):
            self.assertNotIn("FLAKING_QUEUE", _function_body(code, signature),
                             signature)
        columns = _function_body(code, "function queueColumns(")
        # Still failing's row, the Streak where its two times stand: the
        # one difference (design §14).
        self.assertRegex(columns, r"case FLAKING_QUEUE:\s+return \[testCol, "
                                  r"streakCol, commentCol, assigneeCol\];")
        self.assertRegex(columns, r'case "still_failing":\s+return \[testCol, '
                                  r'failingSinceCol,\s+when\("Last pass", '
                                  r'"last_pass_time"\),\s+commentCol,\s+'
                                  r'assigneeCol\];')

    def test_the_streak_column(self) -> None:
        """Its words from api.js's helper beside flakySentence(), its
        title that sentence, and its order the runs left before the test
        counts as failing, as the server orders the rows."""
        code = _strip_comments(read("app.js"))
        columns = _function_body(code, "function queueColumns(")
        streak = columns[columns.index("const streakCol = {"):]
        streak = streak[:streak.index("\n  };") + 4]
        self.assertIn('header: "Streak",', streak)
        self.assertIn('sortKey: "runs_left",', streak)
        # Read defensively, as the Review panel reads a mark: a row with
        # no `flaky` draws an empty cell and sorts last (null).
        self.assertRegex(streak, r"sortValue: \(entry\) => \(entry\.flaky\s+"
                                 r"\? entry\.flaky\.cadence - "
                                 r"entry\.flaky\.streak : null\),")
        self.assertRegex(streak, r"if \(entry\.flaky\) \{\s+cell\.textContent"
                                 r" = flakyStreakWords\(entry\.flaky\);\s+"
                                 r"cell\.title = flakySentence\(entry\.flaky,")
        self.assertNotIn("innerHTML", streak)
        # The loop sorts on a column's sortValue where it names one, by
        # the one shared sortRows(), and not past the cap.
        render = _function_body(code, "function renderQueueTable()")
        self.assertIn("const sortValue = sortColumn ? sortColumn.sortValue "
                      ": null;", render)
        self.assertIn("if (!capped && sortValue) {", render)
        self.assertIn("value: sortValue(entry)", render)
        # Only a key one of this tab's columns has sorts it (QueueSortTest).
        self.assertIn("} else if (!capped && sortColumn) {", render)
        self.assertNotIn("state.queueSortKey || \"\"", render)
        api = _strip_comments(read("api.js"))
        self.assertIn(
            'return cadenceWords(flaky.streak) + ", failing at " '
            '+ flaky.cadence;',
            _function_body(api, "export function flakyStreakWords(flaky)"))
        self.assertLess(api.index("export function flakySentence("),
                        api.index("export function flakyStreakWords("))

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_streak_words_byte_for_byte(self) -> None:
        """They agree with the sentence (its title) and the cadence's own
        words: "N in a row" is cadenceWords(), "failing at" the sentence's
        "counts as failing at"."""
        answer = _api_calls([
            ("flakyStreakWords", [_flaky("flaking", 1)]),
            ("flakyStreakWords", [_flaky("flaking", 2)]),
            ("flakyStreakWords",
             [dict(_flaky("flaking", 9), cadence=10)]),
            ("flakySentence", [_flaky("flaking", 2), False]),
            ("cadenceWords", [3]),
        ])
        self.assertEqual(answer, [
            "1 in a row, failing at 3", "2 in a row, failing at 3",
            "9 in a row, failing at 10",
            "Flaking: failed its last 2 runs; counts as failing at 3 in a row",
            "3 in a row"])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_a_row_without_its_mark_still_draws(self) -> None:
        """A row with ``flaky: null`` (an older server behind a reload, a
        race) draws whole, with its Review, an empty Streak cell and no
        title, and sorts last by Streak in either direction."""
        bare = dict(_FLAKING_ROWS[0], test_name="no_mark", flaky=None)
        opened, ascending, descending = _lists_walk(
            "flaky", [["tab", "Flaking"], ["sort", "runs_left"],
                      ["sort", "runs_left"]],
            rows=[bare, _FLAKING_ROWS[0], _FLAKING_ROWS[2]])[1:]
        self.assertEqual([row["cells"][0][:7] for row in opened["rows"]],
                         ["no_mark", "dns_tim", "cache_w"])
        self.assertEqual(opened["rows"][0]["cells"][1], "")
        self.assertEqual(opened["rows"][0]["titles"][1], "")
        self.assertEqual([row["review"] for row in opened["rows"]],
                         [["Review"]] * 3)
        self.assertEqual([row["cells"][0][:7] for row in ascending["rows"]],
                         ["dns_tim", "cache_w", "no_mark"])
        self.assertEqual([row["cells"][0][:7] for row in descending["rows"]],
                         ["cache_w", "dns_tim", "no_mark"])

    def test_the_one_state_is_said_once_above_the_table(self) -> None:
        """By QUEUE_INVARIANT_RESULT, the mechanism Still failing and
        Stale annotations use, its words from api.js; the old order line,
        its element, its rule and its "N flaking now" link are gone."""
        code = _strip_comments(read("app.js"))
        self.assertIn("[FLAKING_QUEUE]: { result: FLAKING_FILTER, text: "
                      "FLAKING_TAB_NOTE },", code)
        api = _strip_comments(read("api.js"))
        # Not "failing": a mark changes what counts as a failure, and a
        # test on this tab is by that definition not failing.
        self.assertIn('export const FLAKING_TAB_NOTE = "Every test here '
                      'failed its latest run, "\n  + "but fewer times in a '
                      'row than its cadence. Nearest to its cadence "\n'
                      '  + "first.";', api.replace("\r\n", "\n"))
        self.assertIn('export const FLAKING_TAB_EMPTY = "Nothing is '
                      'flaking.";', api)
        for name, text in (("app.js", code), ("api.js", api),
                           ("index.html", read("index.html")),
                           ("style.css", read_text("style.css"))):
            for gone in ("queue-flaky-note", "flakingNowWords",
                         "FLAKY_TAB_ORDER", "List them in All tests",
                         "renderFlakyNote", "retired-tag"):
                self.assertNotIn(gone, text, name + ": " + gone)

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_nothing_flaking_no_tab_and_no_request(self) -> None:
        """No mark, or marks with none flaking now (``flaky_total`` 9,
        ``flaking`` 0): no tab, and the tab's queue is never asked for."""
        for marks, flaking in (("zero", None), ("older", None),
                               ("flaky", 0)):
            with self.subTest(marks=marks, flaking=flaking):
                load = _lists_walk(marks, [], flaking=flaking)[0]
                self.assertEqual([tab[0] for tab in load["tabs"]], [
                    "New failures", "Still failing", "Muted", "Fixed",
                    "Stale annotations", "Not run", "My actions"])
                self.assertFalse(any("queue=flaking" in a or
                                     a.startswith("api/flaky")
                                     for a in load["asked"]))

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_first_click_asks_once_and_draws_the_rows(self) -> None:
        load, opened, again, reopened = _lists_walk("flaky", [
            ["tab", "Flaking"], ["tab", "Muted"], ["tab", "Flaking"]],
            flaking=3)
        # Before the tab's own answer, its badge is `status.flaking`.
        self.assertEqual([tab[:2] for tab in load["tabs"]][2:4],
                         [["Muted", "6"], ["Flaking", "3"]])
        self.assertFalse(any("queue=flaking" in a for a in load["asked"]))
        # Home's load is still four data requests: three in the first
        # batch, then the trend's split once the headline is drawn.
        self.assertEqual(len(load["asked"]), 4)
        self.assertTrue(load["asked"][-1].startswith(
            "api/summary?parts=assigned_trend"))
        self.assertEqual(opened["asked"], [_FLAKING_QUEUE_ASK])
        self.assertEqual(again["asked"],
                         ["api/summary?parts=queue&queue=muted&assignee=dev1"])
        self.assertEqual(reopened["asked"], [])
        # The badge is the tab's own total once it has landed: the rows.
        self.assertEqual(opened["tabs"][3], ["Flaking", "3", "true"])
        self.assertEqual(opened["head"], _FLAKING_HEAD)
        self.assertFalse(opened["tableHidden"])
        self.assertEqual(opened["note"], [
            False, "FLAKINGEvery test here failed its latest run, but fewer "
            "times in a row than its cadence. Nearest to its cadence first.",
            ["chip result-flaking:FLAKING"]])
        rows = opened["rows"]
        # In the server's order, nearest to its cadence first: the test,
        # the streak, the latest comment.
        self.assertEqual([row["cells"][:2] for row in rows], [
            ["dns_timeoutenv-a · s1.py", "2 in a row, failing at 3"],
            ["login_retryenv-b · s1.py", "4 in a row, failing at 5"],
            ["cache_warmenv-a · s1.py", "1 in a row, failing at 3"]])
        self.assertEqual([row["cells"][2][:18] for row in rows],
                         ["—", "retry stormdev1 · ", "—"])
        self.assertEqual([row["cells"][4] for row in rows], ["Review"] * 3)
        self.assertEqual([row["titles"][1] for row in rows], [
            "Flaking: failed its last 2 runs; counts as failing at "
            "3 in a row",
            "Flaking: failed its last 4 runs; counts as failing at "
            "5 in a row",
            "Flaking: failed its last run; counts as failing at 3 in a row"])
        # The row's edge reads flaking; no chip, no pill in the row.
        self.assertEqual([row["edge"] for row in rows], ["result-flaking"] * 3)
        self.assertEqual([row["marks"] for row in rows], [[], [], []])
        # The assignee cell every queue has, Take where nobody has it,
        # and the Output column's Review on every row.
        self.assertEqual([row["take"] for row in rows], [1, 0, 1])
        self.assertEqual([row["review"] for row in rows], [["Review"]] * 3)
        self.assertEqual(opened["cap"][0], True)
        self.assertFalse(opened["sortOff"])
        # Another tab draws its own line (Muted has none).
        self.assertEqual(again["note"][:2], [True, ""])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_cap_note_and_sort_as_on_every_tab(self) -> None:
        """A queue capped below its total says so and switches its sorts
        off with the reason, the Flaking tab's as every tab's; the badge
        is the queue's ``total``."""
        opened = _lists_walk("flaky", [["tab", "Flaking"]], flaking=600,
                             total=600)[1]
        self.assertEqual(len(opened["rows"]), 3)
        self.assertEqual(opened["tabs"][3][:2], ["Flaking", "600"])
        self.assertEqual(opened["cap"], [
            False, "Showing the first 3 of 600 — filter by environment to "
            "narrow the queue."])
        self.assertTrue(opened["sortOff"])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_states_loading_and_empty(self) -> None:
        # Loading reads as every queue's does: the tab is one of them.
        render = _strip_comments(_function_body(
            read("app.js"), "function renderQueueTable()"))
        self.assertIn('emptyNote.textContent = "Loading queue…";', render)
        self.assertNotIn('"Loading…"', render)
        empty = _lists_walk("flaky", [["tab", "Flaking"]], rows=[])[1]
        self.assertTrue(empty["tableHidden"])
        self.assertEqual(empty["empty"], [False, "Nothing is flaking."])
        self.assertTrue(empty["note"][0])
        self.assertEqual(empty["tabs"][3], ["Flaking", "0", "true"])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_an_act_refetches_the_queue_and_the_counts(self) -> None:
        """Take (the coalescer) and Reload (``refreshAll``) each ask for
        the open tab's queue again with the headline, as on any tab; on
        another tab they do not."""
        opened, take, reload = _lists_walk("flaky", [
            ["tab", "Flaking"], ["take", None], ["click", "reload-btn"]])[1:]
        self.assertIn(_FLAKING_QUEUE_ASK, take["asked"])
        self.assertTrue(any("parts=headline" in a for a in take["asked"]))
        self.assertIn(_FLAKING_QUEUE_ASK, reload["asked"])
        self.assertTrue(any("parts=headline" in a for a in reload["asked"]))
        elsewhere = _lists_walk("flaky", [["click", "reload-btn"]])[1]
        self.assertFalse(any("queue=flaking" in a
                             for a in elsewhere["asked"]))

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_selection_bar_works_on_its_rows(self) -> None:
        """Every row is marked, so ticking any of them offers Set cadence
        and Unmark, as on any queue's rows."""
        ticked = _lists_walk("flaky", [["tab", "Flaking"], ["tick", [0, 1]]])[2]
        self.assertEqual(ticked["ticked"], ["Set cadence", False])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_review_on_a_flaking_row_and_unmark_from_the_panel(self) -> None:
        """Review on a Flaking row opens the panel Still failing's opens,
        group for group: assign, comment, mute and the Flaky group with
        the mark's sentence, owner, age and reason, Set cadence and
        Unmark. Unmark from it posts the one test, and the reload that
        follows brings the queue back without it, the badge with it."""
        still = _lists_walk("flaky", [["tab", "Still failing"],
                                      ["review", 0]])[2]
        load, _, flaking, unmarked = _lists_walk(
            "flaky", [["tab", "Flaking"], ["review", 0],
                      ["panelUnmark", None]], flaking=3, flaking_after=2,
            rows_after=_FLAKING_ROWS[1:])
        self.assertEqual(load["tabs"][3][:2], ["Flaking", "3"])
        self.assertEqual(flaking["panel"]["labels"], still["panel"]["labels"])
        self.assertEqual(flaking["panel"]["labels"],
                         ["Assign to", "Add a comment", "Mute", "Flaky"])
        self.assertEqual(flaking["panel"]["flaky"], [
            ["SPAN", ["Flaking: failed its last 2 runs; counts as failing "
                      "at 3 in a row", "by dev1 · 12 days ago · race in "
                      "teardown"]],
            ["SELECT", "3"], ["BUTTON", "Set cadence"], ["BUTTON", "Unmark"]])
        self.assertEqual([row["review"] for row in flaking["rows"]][0],
                         ["Close"])
        self.assertEqual(unmarked["posts"], [["api/flaky/unmark", {
            "username": "dev1", "tests": [{
                "environment": "env-a", "script": "s1.py",
                "test_name": "dns_timeout"}]}]])
        self.assertIn(_FLAKING_QUEUE_ASK, unmarked["asked"])
        self.assertEqual([row["cells"][0] for row in unmarked["rows"]],
                         ["login_retryenv-b · s1.py",
                          "cache_warmenv-a · s1.py"])
        self.assertEqual(unmarked["tabs"][3], ["Flaking", "2", "true"])
        self.assertIsNone(unmarked["panel"])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_unmark_from_the_selection_bar(self) -> None:
        """The bar's Unmark on a ticked Flaking row: the one test posted,
        then the board reloaded, the row gone and the badge following."""
        acted = _lists_walk("flaky", [["tab", "Flaking"],
                                      ["barUnmark", [2]]],
                            flaking=3, flaking_after=2,
                            rows_after=_FLAKING_ROWS[:2])[2]
        self.assertEqual(acted["posts"], [["api/flaky/unmark", {
            "username": "dev1", "tests": [{
                "environment": "env-a", "script": "s1.py",
                "test_name": "cache_warm"}]}]])
        self.assertIn(_FLAKING_QUEUE_ASK, acted["asked"])
        self.assertEqual([row["cells"][0] for row in acted["rows"]],
                         ["dns_timeoutenv-a · s1.py",
                          "login_retryenv-b · s1.py"])
        self.assertEqual(acted["tabs"][3], ["Flaking", "2", "true"])

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_act_that_clears_the_last_row_leaves_the_empty_state(
            self) -> None:
        """Unmarking the last test flaking leaves the reader on the open
        tab's empty state; leaving it, the tab is gone."""
        cleared, left = _lists_walk(
            "flaky", [["tab", "Flaking"], ["review", 0],
                      ["panelUnmark", None], ["tab", "Muted"]],
            rows=_FLAKING_ROWS[:1], flaking=1, flaking_after=0,
            rows_after=[])[3:]
        self.assertEqual(cleared["tabs"][3], ["Flaking", "0", "true"])
        self.assertTrue(cleared["tableHidden"])
        self.assertEqual(cleared["empty"], [False, "Nothing is flaking."])
        self.assertNotIn("Flaking", [tab[0] for tab in left["tabs"]])


#: Triage tabs allowed to draw a row without the Output column's Review,
#: each with its reason. None: Triage is the morning touch point, and
#: every tab in it is a queue with the run's output and the Review panel
#: on every row (CLAUDE.md, "Triage is the morning touch point"; design
#: flaky-mark-r0 §14). A tab added here needs the owner's word, named in
#: the reason.
_TABS_WITHOUT_REVIEW = {}  # type: Dict[str, str]


def _tabs_without_review(each: List[Dict[str, Any]]) -> List[str]:
    """The tabs of a ``reviewEach`` walk whose row has no Review button,
    or whose Review opens no panel with the groups every tab's has."""
    return [tab["tab"] for tab in each
            if tab["buttons"] != [1] or tab["head"][-1:] != ["Output"]
            or not tab["panel"]
            or tab["panel"]["labels"][:2] != ["Assign to", "Add a comment"]
            or "Flaky" not in tab["panel"]["labels"]]


class QueueTabReviewTest(unittest.TestCase):
    """WP-44 item 15: every Triage tab's row has the Output column's
    Review, and it opens the panel. The Flaking tab was built without
    them, as a table of its own beside the shared loop, and nothing
    failed; this walks every tab in QUEUE_TABS, each given the same one
    row, so a tab added later without them fails here unless it is named
    in _TABS_WITHOUT_REVIEW with its reason."""

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_every_tabs_row_has_review_and_it_opens_the_panel(self) -> None:
        each = _lists_walk("flaky", [["reviewEach", None]], flaking=1,
                           queue_row=_FLAKING_ROWS[0])[1]["each"]
        code = _strip_comments(read("app.js"))
        tabs = code[code.index("const QUEUE_TABS = ["):]
        tabs = tabs[:tabs.index("];")]
        # Every tab of QUEUE_TABS was drawn and walked.
        self.assertEqual(len(each), tabs.count("{ id: "))
        self.assertEqual(len(each), 8)
        self.assertEqual(
            [label for label in _tabs_without_review(each)
             if label not in _TABS_WITHOUT_REVIEW], [])
        # The same row opens the same panel on every tab.
        self.assertEqual(
            {json.dumps(tab["panel"]) for tab in each
             if tab["tab"] not in _TABS_WITHOUT_REVIEW},
            {json.dumps(each[0]["panel"])})

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_detector_can_fail(self) -> None:
        """Planted: a copy of the static tree whose Fixed tab draws its
        rows without Review, as the Flaking tab once did."""
        with tempfile.TemporaryDirectory() as scratch:
            shutil.copytree(STATIC_DIR, os.path.join(scratch, "static"))
            os.makedirs(os.path.join(scratch, "tools", "dev", "net"))
            shutil.copy(
                os.path.join(REPO_DIR, "tools", "dev", "net", "domshim.mjs"),
                os.path.join(scratch, "tools", "dev", "net"))
            app = os.path.join(scratch, "static", "app.js")
            with io.open(app, encoding="utf-8") as handle:
                source = handle.read()
            planted = source.replace(
                "    actionCell.appendChild(reviewBtn);\n",
                '    if (queueId !== "fixed") {\n'
                "      actionCell.appendChild(reviewBtn);\n    }\n", 1)
            self.assertNotEqual(planted, source)
            with io.open(app, "w", encoding="utf-8") as handle:
                handle.write(planted)
            each = _lists_walk("flaky", [["reviewEach", None]], flaking=1,
                               queue_row=_FLAKING_ROWS[0],
                               root=scratch)[1]["each"]
        self.assertEqual(_tabs_without_review(each), ["Fixed"])


class QueueSortTest(unittest.TestCase):
    """The reader's sort key is kept across the Triage tabs, and applies
    only where it names one of the open tab's own columns. Every queue row
    carries ``start_time``, ``failing_since`` and the rest, so before this
    a key from another tab's column reordered a tab's rows with no arrow
    on any header, under a line stating the server's order ("Nearest to
    its cadence first"); now such a key leaves the server's order."""

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_only_a_key_the_tab_has_sorts_it(self) -> None:
        # Started at different times, so a sort on New failures' "Failed
        # at" would put them in another order than the server's.
        rows = [dict(row, start_time=at) for row, at in zip(
            _FLAKING_ROWS, ("2026-10-03T03:00:00", "2026-10-03T01:00:00",
                            "2026-10-03T02:00:00"))]
        def names(snap: Dict[str, Any]) -> List[str]:
            return [row["cells"][0].split("env-")[0] for row in snap["rows"]]

        failed_at, flaking, by_assignee, muted, back = _lists_walk(
            "flaky", [["sort", "start_time"], ["tab", "Flaking"],
                      ["sort", "assignee"], ["tab", "Muted"],
                      ["tab", "Flaking"]], rows=rows)[1:]
        self.assertEqual(failed_at["sorted"], ["start_time:ascending"])
        # Another tab's key: the server's order, and no arrow.
        self.assertEqual(names(flaking),
                         ["dns_timeout", "login_retry", "cache_warm"])
        self.assertEqual(flaking["sorted"], [])
        # A key this tab has sorts it, with its arrow (nobody's last).
        self.assertEqual(names(by_assignee),
                         ["login_retry", "dns_timeout", "cache_warm"])
        self.assertEqual(by_assignee["sorted"], ["assignee:ascending"])
        # And carries to another tab that has the column, and back.
        self.assertEqual(muted["sorted"], ["assignee:ascending"])
        self.assertEqual(back["sorted"], ["assignee:ascending"])
        self.assertEqual(names(back), names(by_assignee))


class FlakingFilterTest(unittest.TestCase):
    """WP-44 (design §3 U9, decision 24): All tests' Result filter
    gains FLAKING, right after FAIL, its wire word as its label. It is a
    value on the rows request already made, never a request of its own;
    hidden until the headline says the scope has a mark, or while it is
    pressed; read from the address bar at load by the same loop as the
    other values. While it shows, FAIL's title says FAIL leaves flaking
    tests out."""

    def test_after_fail_in_one_list(self) -> None:
        api = _strip_comments(read("api.js"))
        self.assertIn('export const BROWSE_RESULTS = ["PASS", "FAIL", '
                      'FLAKING_FILTER,\n  "FAILED_AS_EXPECTED", '
                      '"UNEXPECTED_PASS"];', api.replace("\r\n", "\n"))
        self.assertNotIn("export const RESULTS", api)
        code = _strip_comments(read("app.js"))
        self.assertIn("for (const result of BROWSE_RESULTS) {",
                      _function_body(code, "function buildResultToggles("))
        self.assertIn("BROWSE_RESULTS.indexOf(raw) !== -1",
                      _function_body(code, "function wireMainlineControls("))
        self.assertIn('return "Failing, flaking tests left out";',
                      _function_body(api, "export function failToggleTitle("))

    def test_it_rides_the_existing_rows_request(self) -> None:
        code = _strip_comments(read("app.js"))
        self.assertIn("result: state.activeResults,",
                      _function_body(code, "function browseUrl("))
        sync = _function_body(code, "function syncResultToggles()")
        self.assertNotIn("fetch", sync)
        self.assertIn("state.activeResults.has(FLAKING_FILTER)", sync)
        self.assertIn("(Boolean(state.summary) && flakyScope())", sync)
        self.assertIn("btn.hidden = !flakingShown;", sync)
        self.assertIn("failToggleTitle()", sync)
        self.assertIn("syncResultToggles();",
                      _function_body(code, "function renderHeadline()"))

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_hidden_until_a_mark_then_shown_after_fail(self) -> None:
        for marks in ("zero", "older"):
            with self.subTest(marks=marks):
                toggles = _lists_walk(marks, [])[0]["toggles"]
                self.assertEqual([t[0] for t in toggles], [
                    "PASS", "FAIL", "FLAKING", "FAILED_AS_EXPECTED",
                    "UNEXPECTED_PASS"])
                self.assertEqual([t[0] for t in toggles if not t[1]], [
                    "PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS"])
                self.assertEqual(toggles[1][3], "", "FAIL keeps no title")
        toggles = _lists_walk("flaky", [])[0]["toggles"]
        self.assertEqual(toggles[2], ["FLAKING", False, "false", "",
                                      "toggle result-flaking"])
        self.assertEqual(toggles[1][3], "Failing, flaking tests left out")

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_read_at_load_and_shown_while_pressed(self) -> None:
        """A link (or a bookmark) pressing FLAKING on a scope with no mark
        still shows the toggle it pressed; on a build it opens "Its own
        results", as any ``result=`` does."""
        load = _lists_walk("zero", [], url="http://127.0.0.1/index.html"
                           "?result=FLAKING&result=FAIL")[0]
        self.assertEqual(load["toggles"][2][:3], ["FLAKING", False, "true"])
        self.assertEqual(load["toggles"][1][:3], ["FAIL", False, "true"])
        self.assertEqual(_results_of(load["asked"]), [["FLAKING", "FAIL"]])
        self.assertIn("params.getAll(\"result\").length > 0", _function_body(
            _strip_comments(read("app.js")), "function urlAsksForBrowseFilters("))

    @unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                     "are optional dev tooling, the source guards still run")
    def test_the_scripts_bar_lists_what_it_draws(self) -> None:
        """FAIL, plus FLAKING when the bar has a flaking part; with no
        mark, FAIL alone, as before."""
        flaky = _lists_walk("flaky", [["scriptBar", 0]])[1]
        self.assertEqual(_results_of(flaky["asked"]), [["FAIL", "FLAKING"]])
        plain = _lists_walk("zero", [["scriptBar", 0]])[1]
        self.assertEqual(_results_of(plain["asked"]), [["FAIL"]])


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class FlakingTrendTest(unittest.TestCase):
    """WP-44 (design §3 U7, decisions 2 and 5): the per-day chart's
    fourth series, FAIL_FLAKING from ``parts=assigned_trend``, after the
    grey, with key, tooltip row, table column and caption only where the
    scope has a mark; an older part without the field draws today's."""

    def test_drawn_where_the_scope_has_a_mark(self) -> None:
        load = _lists_walk("flaky", [])[0]
        self.assertEqual(load["trendLegend"], [
            "Unassigned", "Assigned", "Muted", "Flaking", "Unexpected pass"])
        # Failed 9 = 3 unassigned + 3 assigned + 2 muted + 1 flaking.
        self.assertEqual(load["trendRow"],
                         ["Oct 1", "9", "3", "3", "2", "1", "2", "62"])
        self.assertEqual(load["caption"], [
            False, False,
            " Flaking: the tests flaking that night, by today's marks."])

    def test_no_mark_or_an_older_part_draws_todays_chart(self) -> None:
        for marks, part in (("zero", "flaking"), ("older", "flaking"),
                            ("flaky", "older")):
            with self.subTest(marks=marks, part=part):
                load = _lists_walk(marks, [], part=part)[0]
                self.assertEqual(load["trendLegend"], [
                    "Unassigned", "Assigned", "Muted", "Unexpected pass"])
                self.assertEqual(load["trendRow"],
                                 ["Oct 1", "9", "4", "3", "2", "2", "62"])
                self.assertEqual(load["caption"], [False, True, ""])


def _parts(asked: List[str]) -> List[str]:
    """Each recorded data request as its summary part, or its path."""
    named = []  # type: List[str]
    for entry in asked:
        parts = [value for name, value in _query(entry) if name == "parts"]
        named.append(parts[0] if parts else entry.split("?")[0])
    return named


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class TrendSplitAfterHeadlineDomTest(unittest.TestCase):
    """The trend's split is never on the first paint (WP-43's part reads
    every night's runs, and with flaky marks a per-night read besides):
    Home's first batch is the headline, the active queue and the browse
    page; the split goes out once the headline has been drawn, once per
    load and per refresh. Every headline answer is held back here, so
    what is asked for before it is drawn can be seen."""

    _DELAY = 200

    def test_the_first_batch_is_three_and_the_split_follows(self) -> None:
        for marks in ("zero", "flaky"):
            with self.subTest(marks=marks):
                load = _lists_walk(marks, [],
                                   headline_delay=self._DELAY)[0]
                self.assertEqual(sorted(_parts(load["early"])),
                                 ["api/dashboard", "headline", "queue"])
                self.assertEqual(_parts(load["asked"])[-1], "assigned_trend")
                self.assertEqual(_parts(load["asked"]).count(
                    "assigned_trend"), 1)
                # Once it lands, the trend carries the split.
                self.assertEqual(load["trendLegend"][:3],
                                 ["Unassigned", "Assigned", "Muted"])

    def test_every_refresh_asks_once_after_its_headline(self) -> None:
        """Reload, an environment chosen and a Take (the coalescer)."""
        load, reload, scoped, take = _lists_walk(
            "flaky", [["click", "reload-btn"], ["env", None],
                      ["take", None]], headline_delay=self._DELAY)
        for name, snap in (("reload", reload), ("environment", scoped),
                           ("take", take)):
            with self.subTest(refresh=name):
                parts = _parts(snap["asked"])
                self.assertEqual(parts.count("assigned_trend"), 1, parts)
                self.assertEqual(parts.count("headline"), 1, parts)
                self.assertLess(parts.index("headline"),
                                parts.index("assigned_trend"))
                self.assertEqual(snap["trendLegend"][:3],
                                 ["Unassigned", "Assigned", "Muted"])

    _SPLIT = ["Unassigned", "Assigned", "Muted", "Flaking", "Unexpected pass"]
    _TOTALS = ["Failed", "Unexpected pass"]

    def test_a_reload_keeps_its_chart_until_the_new_split(self) -> None:
        """Where a split is on screen, a reload does not redraw the chart
        between its headline and its split: the previous chart stays,
        dimmed, and the new split replaces it. A first load draws the
        headline's totals meanwhile; so does a load after a split that
        could not be had, which ends with the totals drawn, undimmed."""
        load, mid, landed = _lists_walk(
            "flaky", [["click", "reload-btn", 120], ["wait", None, 400]],
            delays={"assigned_trend": [0, 300]})
        self.assertEqual((load["trendLegend"], load["trendDim"]),
                         (self._SPLIT, False))
        self.assertIn("land:headline", mid["events"])
        self.assertIn("ask:assigned_trend", mid["events"])
        self.assertNotIn("land:assigned_trend", mid["events"])
        self.assertEqual((mid["trendLegend"], mid["trendDim"]),
                         (self._SPLIT, True))
        self.assertEqual((landed["trendLegend"], landed["trendDim"]),
                         (self._SPLIT, False))
        first, after = _lists_walk(
            "flaky", [["wait", None, 400]],
            delays={"assigned_trend": [300]})
        self.assertEqual((first["trendLegend"], first["trendDim"]),
                         (self._TOTALS, True))
        self.assertEqual((after["trendLegend"], after["trendDim"]),
                         (self._SPLIT, False))
        load, failed, mid, landed = _lists_walk(
            "flaky", [["click", "reload-btn"], ["click", "reload-btn", 120],
                      ["wait", None, 400]],
            parts=[_trend_part(1), None, _trend_part(1)],
            delays={"assigned_trend": [0, 0, 300]})
        self.assertEqual((failed["trendLegend"], failed["trendDim"]),
                         (self._TOTALS, False))
        self.assertEqual(failed["caption"][0], True)
        self.assertEqual((mid["trendLegend"], mid["trendDim"]),
                         (self._TOTALS, True))
        self.assertEqual(landed["trendLegend"], self._SPLIT)

    def test_an_act_before_a_reloads_headline_asks_once_after_it(
            self) -> None:
        """A Take whose refresh finishes while a reload's headline is
        still held back asks for no split of its own: the reload asks,
        once, after its headline has landed and been drawn."""
        _, reload, take = _lists_walk(
            "flaky", [["click", "reload-btn", 0], ["take", None, 600]],
            delays={"headline": [0, 300, 0]})
        self.assertNotIn("ask:assigned_trend", reload["events"])
        events = take["events"]
        self.assertEqual(events.count("ask:assigned_trend"), 1, events)
        last_headline = max(index for index, event in enumerate(events)
                            if event == "land:headline")
        self.assertGreater(events.index("ask:assigned_trend"), last_headline)
        self.assertEqual(events.count("land:headline"), 2, events)
        self.assertEqual((take["trendLegend"], take["trendDim"]),
                         (self._SPLIT, False))

    def test_an_older_bursts_split_landing_last_is_not_drawn(self) -> None:
        """Two Takes in one refresh generation: the first burst's split is
        answered after the second's, and the second's is what stays
        drawn (Assigned 2, not 1)."""
        def part(assigned: int) -> Dict[str, Any]:
            answer = _trend_part(1)
            for night in answer["trend"]["nights"]:
                night["FAIL_ASSIGNED"] = assigned
            return answer

        _, first, second = _lists_walk(
            "flaky", [["take", None, 100], ["take", None, 600]],
            parts=[part(3), part(1), part(2)],
            delays={"assigned_trend": [0, 400, 0]})
        self.assertEqual(first["events"].count("ask:assigned_trend"), 1)
        self.assertNotIn("land:assigned_trend", first["events"])
        self.assertEqual(second["events"].count("land:assigned_trend"), 2)
        # Failed 9 = 4 unassigned + 2 assigned + 2 muted + 1 flaking.
        self.assertEqual(second["trendRow"],
                         ["Oct 1", "9", "4", "2", "2", "1", "2", "62"])
        self.assertFalse(second["trendDim"])


#: Open Actions over the shim (WP-44): the rows request of each
#: Result mode, and the footer line. argv: repo root, fixture path.
_ACTIONS_MODES_DRIVER = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [root, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));
const { installDom, Element } = await import(
  pathToFileURL(root + "/tools/dev/net/domshim.mjs").href);
const byId = installDom(fixture.ids, "http://127.0.0.1/actions.html");
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
byId["product-switcher"].setAttribute("data-host-managed", "");
new Element("div").appendChild(byId["extend-all-btn"]);
Element.prototype.replaceWith = function (node) {
  if (node.id) byId[node.id] = node;
};
let rows = [];
globalThis.fetch = (url) => {
  const u = new URL(String(url), "http://127.0.0.1/");
  const path = u.pathname.replace(/^\//, "");
  let body = {};
  if (path === "api/summary") {
    body = fixture.names;
  } else if (path === "api/dashboard") {
    rows.push(Array.from(u.searchParams.entries()).filter(
      ([name]) => name === "result" || name === "open"));
    body = { tests: [fixture.row], total: 1, streams: {} };
  } else if (path === "api/mutes") {
    body = { mutes: [] };
  } else if (path === "api/users") {
    body = { users: [] };
  }
  return Promise.resolve({ ok: true, status: 200,
    text: async () => JSON.stringify(body) });
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
await import(pathToFileURL(root + "/static/actions.js").href);
await settle();
const note = byId["actions-flaky-note"];
const out = { note: [note.hidden, note.textContent], modes: {} };
out.modes.open = rows[0];
for (const mode of ["FAIL", "UNEXPECTED_PASS"]) {
  rows = [];
  byId["filter-result"].value = mode;
  await byId["filter-result"].dispatch("change");
  await settle();
  out.modes[mode] = rows[0];
}
const firstTr = byId["actions-body"].children[0];
out.firstRow = firstTr ? [firstTr.className].concat(firstTr.find(
  (c) => String(c.className).startsWith("chip")).map(
  (c) => c.className + ":" + c.textContent)) : null;
await driverAnswer(out);
"""


def _actions_modes_walk(marks: Any, row: Any = None) -> Dict[str, Any]:
    """Open Actions with ``flaky_marks`` *marks* (None: an older server
    that sends no such field)."""
    names = _names_part([])
    if marks is not None:
        names["flaky_marks"] = marks
    answer = _shim({"ids": _actions_ids(), "names": names,
                    "row": row or _ACTIONS_ROW},
                   _ACTIONS_MODES_DRIVER)  # type: Dict[str, Any]
    return answer


@unittest.skipIf(NODE is None, "node is not on PATH: the DOM-shim walks "
                 "are optional dev tooling, the source guards still run")
class OpenActionsFlakingDomTest(unittest.TestCase):
    """WP-44 over the shim: an assigned flaking row on Open
    Actions reads FLAKING, with the footer line under the count."""

    def test_an_assigned_flaking_row_reads_flaking_above_the_footer(
            self) -> None:
        answer = _actions_modes_walk(True, dict(
            _ACTIONS_ROW, assignee="dev1", flaky=_flaky("flaking", 1)))
        self.assertEqual(answer["firstRow"],
                         ["result-flaking", "chip result-flaking:FLAKING"])
        self.assertFalse(answer["note"][0])
        self.assertTrue(answer["note"][1].startswith("Unassigned tests that "
                                                     "are flaking"))


if __name__ == "__main__":
    unittest.main()
