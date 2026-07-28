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
from typing import Dict, List

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

    #: Pages that show counts bounded by the recency cutoff.
    _PAGES = ("app.js", "time.js")

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
        for name in ("app.js", "time.js", "actions.js"):
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


if __name__ == "__main__":
    unittest.main()
