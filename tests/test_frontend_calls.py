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

import io
import os
import re
import unittest
from typing import Dict, List, Tuple

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def read(name: str) -> str:
    """Return a frontend file's text.

    Read as bytes and decoded rather than opened as text: ``app.js``
    contains a legitimate ``join("\\0")`` separator, which makes tools
    that sniff for NUL treat the file as binary. A test that silently
    matched nothing would pass forever.
    """
    with io.open(os.path.join(STATIC_DIR, name), "rb") as handle:
        return handle.read().decode("utf-8")


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
            for number in fetch_sites(source, "/api/users"):
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
            for number in fetch_sites(source, "/api/users"):
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
    """Read any file from the static directory as text."""
    with io.open(os.path.join(STATIC_DIR, name), "rb") as handle:
        return handle.read().decode("utf-8")


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
    "compare.js",
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
            len(fetch_sites(read("whatsnew.js"), "/api/site-notes")), 1)

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
        """
        source = read("app.js")
        sites = [
            number for number, line in enumerate(source.split("\n"), 1)
            if "fetchJson(queueUrl(" in line
        ]
        self.assertEqual(
            len(sites), 2,
            "app.js should fetch queue rows in exactly two places "
            "(loadQueue and fetchSummary); found lines " + repr(sites))

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

        WP-24: the hand-rolled params/href pair became one pageUrl()
        call, whose DEFAULT scope carriage is what now supplies
        `stream` (this page's own — read the same way
        getSelectedStreamId() already does). Same assertion intent
        (the link is built off this page's own selected stream, via
        pageUrl -- not a hand-rolled "test.html?" concatenation
        anywhere in this file, which the enforcement test polices
        globally), now checking the pageUrl() call site."""
        body = _function_body(read("compare.js"), "function buildDeltaRow(")
        self.assertIn("getSelectedStreamId()", body)
        stream_at = body.index("getSelectedStreamId()")
        self.assertIn('link.href = pageUrl("test"', body[stream_at:])
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
        """WP-24: appendStream(params)+"test.html?"+params.toString()
        became one pageUrl("test", ..., {product: null, baseline: null})
        call -- `stream` is supplied by pageUrl()'s DEFAULT scope
        carriage (this page's own, a no-op on mainline exactly as
        appendStream() was), so there is no separate "appended before
        the href" ordering left to check; the assertion that matters
        now is that product/baseline are explicitly excluded (this link
        never carried either) rather than silently inherited."""
        body = _strip_comments(_function_body(
            read("app.js"), "function queueColumns("))
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn("{ product: null, baseline: null }", body)

    def test_browse_rows_carry_the_stream_into_the_test_link(self) -> None:
        """WP-24: same conversion as the queue rows above."""
        body = _strip_comments(_function_body(read("app.js"), "function buildRow("))
        self.assertIn('link.href = pageUrl("test"', body)
        self.assertIn("{ product: null, baseline: null }", body)

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
        code = _strip_comments(read("test.js"))
        self.assertIn("withStream(myPath())", code)
        self.assertIn('withStream(myPath("/history")', code)


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
        body = _function_body(read("test.js"), "function renderDetail(")
        self.assertIn("if (streamId !== null) {", body)
        stream_at = body.index("if (streamId !== null) {")
        self.assertIn('params.append("stream", String(streamId))',
                       body[stream_at:stream_at + 100])
        self.assertLess(
            stream_at, body.index('"script.html?"'),
            "the stream param must be appended BEFORE the href is built")

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
            body.index("const params = new URLSearchParams();"):
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
        """WP-24: appendStream(params)+"script.html?" became one
        pageUrl("script", ..., {product: null, baseline: null}) call --
        `stream` comes from pageUrl()'s default scope carriage, same as
        every other converted row link in this file."""
        body = _strip_comments(_function_body(
            read("app.js"), "function scriptLink("))
        self.assertIn('link.href = pageUrl("script"', body)
        self.assertIn("{ product: null, baseline: null }", body)

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
        body = _function_body(read("test.js"), "function testPageUrl(")
        self.assertIn('params.append("environment"', body)
        self.assertIn('params.append("script"', body)
        self.assertIn('params.append("test_name"', body)

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

    def test_both_call_sites_pass_the_link_only_when_nonzero(self) -> None:
        """Zero visible change (the same rule the stat's own existence
        already follows): a card with no unassigned failures gets
        neither the stat nor a dead link."""
        for fn in ("function buildOkCard(", "function buildStreamCard("):
            body = _function_body(read("watch.js"), fn)
            guard_at = body.index("if (card.unassigned_failing) {")
            block = body[guard_at:body.index("}", guard_at) + 1]
            self.assertIn("unassignedStatLink(card)", block)

    def test_the_linked_stat_reuses_the_shared_stat_markup(self) -> None:
        """buildStat's existing (label, value) call sites must be
        unaffected — this is an optional third argument, not a parallel
        rendering path that could drift from the plain stat's markup."""
        body = _function_body(read("watch.js"), "function buildStat(")
        self.assertIn("function buildStat(label, value, href)", body)
        self.assertIn("if (href) {", body)
        # The unlinked branch is the exact call the page always made.
        self.assertIn(
            'el("span", "watch-stat-value", String(value))', body)
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
        """WP-24: the appendProduct(qs) helper is gone -- summaryUrl()
        now calls apiUrl() with a product scope from
        selectedProductScope() (getSelectedProduct() || null, so it is
        OMITTED rather than sent empty when nothing is selected -- the
        same behaviour appendProduct()'s `if (product)` guard gave).
        Same assertion intent (the summary fetch carries the product
        scope), now checking the apiUrl() call site."""
        src = read("actions.js")
        self.assertIn("function summaryUrl()", src)
        self.assertIn("fetchJson(summaryUrl())", src)
        self.assertNotIn('fetchJson("/api/summary")', src,
                         "a bare unscoped summary fetch is the bug "
                         "coming back")
        body = _function_body(src, "function summaryUrl()")
        self.assertIn("apiUrl(", body)
        self.assertIn("selectedProductScope()", body)


class OpenActionsOriginFilterTest(unittest.TestCase):
    """Open Actions' branch/mainline origin filter and per-row tag
    (WP-21, docs/STREAMS_PLAN.md §3.6, found in first human use)."""

    def test_the_filter_is_server_side_not_a_client_reshuffle(self) -> None:
        """The same rule SortingTest pins for this page's sort: a paged
        table filtered in the browser shows "branch items that happen
        to be on this page", not every branch item.

        WP-24: `qs.append("origin", state.origin)` became `origin:
        state.origin` in the params object passed to apiUrl() -- same
        intent, the origin filter still travels on every request."""
        body = _function_body(read("actions.js"), "function listUrl(")
        self.assertIn("origin: state.origin", body)

    def test_the_filter_hides_with_no_stream_originated_assignments(
        self
    ) -> None:
        body = _function_body(
            read("actions.js"), "function renderOriginFilter(")
        self.assertIn("state.assignmentStreams.length === 0", body)
        self.assertIn("container.hidden = true", body)

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
        code = _strip_comments(read("actions.js"))
        self.assertIn("summary.assignment_streams", code)
        self.assertIn("page.streams", code)

    def test_a_branch_originated_row_links_to_the_streams_own_test_page(
        self
    ) -> None:
        """F1: a row whose CURRENT assignment carries a non-mainline
        origin must land on that stream's own test page — landing on
        mainline's view of "why is this broken in the RC" is the exact
        ambiguity the origin tag beside this link already warns about.
        Resolved from the same batched state.streams map the tag uses,
        never a lookup per row."""
        body = _function_body(read("actions.js"), "function buildRow(")
        self.assertIn(
            "row.assignment_stream_id === null", body)
        self.assertIn(
            "state.streams[String(row.assignment_stream_id)]", body)
        self.assertIn('params.append("stream", String(originStream.id))',
                       body)
        self.assertIn(
            'params.append("product", originStream.product || "")', body)

    def test_a_mainline_originated_row_link_is_unchanged(self) -> None:
        """A null assignment_stream_id must not append either param —
        the ordinary test.html link every row had before this feature."""
        body = _strip_comments(
            _function_body(read("actions.js"), "function buildRow("))
        self.assertIn("if (originStream) {", body)


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
        resultChip() path, not the compare strip."""
        body = _strip_comments(
            _function_body(read("actions.js"), "function buildRow("))
        self.assertIn("} else {", body)
        else_at = body.index("} else {", body.index("if (originStream) {"))
        self.assertIn("resultChip(row.result)", body[else_at:])

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
        self.assertIn('"/api/environments/"', body)
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
        self.assertIn('"/api/streams?product="', body)


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

    def test_unassigned_failing_is_a_stat_only_when_nonzero(self) -> None:
        """Zero visible change: the established house rule, pinned for
        both card builders — a zero count adds neither the stat nor
        the accent class."""
        code = _strip_comments(read("watch.js"))
        for signature in (
            "function buildOkCard(", "function buildStreamCard("
        ):
            body = _function_body(code, signature)
            self.assertIn("if (card.unassigned_failing)", body, signature)

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


if __name__ == "__main__":
    unittest.main()
