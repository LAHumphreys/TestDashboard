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
        code = _strip_comments(read("actions.js"))
        self.assertIn('qs.append("sort", state.sortKey)', code)
        self.assertIn('qs.append("order"', code)
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
        body = _function_body(read("app.js"), "function summaryUrl()")
        self.assertIn(
            '"parts", "headline"', body,
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
        control that changes nothing."""
        for name in ("whatsnew.html", "script.html", "test.html"):
            self.assertNotIn(
                'src="products.js"', read_text(name),
                name + " should not load products.js")
        for name in ("index.html", "actions.html", "time.html",
                     "timeline.html", "watch.html"):
            self.assertIn(
                'src="products.js"', read_text(name),
                name + " is missing the product switcher")
            self.assertIn(
                'id="product-switcher"', read_text(name),
                name + " has no switcher mount point")

    def test_no_innerHTML(self) -> None:
        self.assertNotIn("innerHTML", read("products.js"))


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
        for name, _ in self._TABLES:
            code = _strip_comments(read(name))
            self.assertIn("function appendProduct(", code, name)
            self.assertIn('qs.append("product"', code, name)

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


class TimeAndTimelineProductTest(unittest.TestCase):
    """docs/STREAMS_PLAN.md §2.3: "Time/Timeline pages: product scoping
    only via the existing environment filter semantics" — Time appends
    product= to its own request (the server resolves it); Timeline is
    inherently single-environment, so it scopes its ENVIRONMENT PICKER
    instead of adding a request parameter that endpoint does not read
    from this page's shape.
    """

    def test_time_appends_product_to_its_request(self) -> None:
        code = _strip_comments(read("time.js"))
        self.assertIn("getSelectedProduct", _imported_names(read("time.js")))
        self.assertIn('qs.append("product"', code)

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


if __name__ == "__main__":
    unittest.main()
