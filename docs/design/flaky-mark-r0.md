# Mark a test as flaky — design review, round 0

**Status: ROUND 0 BEING WRITTEN (2026-11-11). §0 is agreed in chat; the owner's
verdicts so far are captured verbatim in §7. Nothing is built.**
The sections are written by the owning agents against §0 and the candidate
contract in §1: §B by `backend-engineer`, §UI by `ui-engineer`, §P the
performance design pass by `performance-engineer`. §4 and §5 collect every
decision and open question in one place. Reading bar: twenty minutes.

Four terms used throughout. A **mark** is the standing record that a test is
accepted as flaky. The **cadence** is the mark's number N: the test counts as
failing only once its N most recent runs have all failed. A marked test whose
latest failure is under the cadence is **flaking**: its failure is tolerated.
A marked test whose failures have reached the cadence is **over its cadence**
and is an ordinary failure.

## §0 What we are building

A developer can **mark a test as flaky**: a standing, owned acceptance that
this test fails now and then, with a cadence of the form "count it as failing
only once its N most recent runs have all failed", N from 2 to 10. The mark is
made by a named user with a required reason, covers the test in one
environment across mainline and every release build (as an assignment does),
and stands until someone removes it; it never expires. Each stream judges the
cadence on its own runs, and a stream with fewer than N runs of the test is
over the cadence when every run it has is a failure. While a marked test is
under its cadence it is *flaking*: the failure leaves New failures and Still
failing, is drawn in its own subdued colour and reported beside the failing
figures ("12 (+4 flaking)"), and still counts against the pass rate, because
it is not a pass. When the test passes it is an ordinary pass carrying a small
"flaky" tag. Once it is over its cadence it is an ordinary red failure,
nobody's unless it is separately assigned, and the mark stays, so it is
tolerated again after its next pass. A live mute wins over the mark: a muted
test shows as muted until the mute runs out. The mark's owner is a record of
who accepted the flakiness and is not an assignment: marking a test puts
nothing on anyone's Open Actions, and a test that is assigned as well as
marked stays on its assignee's list with a "flaking" state marker. A Flaky tab
on Home, beside Muted, lists every mark with its owner, cadence, age and how
often the test has actually failed, so a stale mark is visible without anyone
being nagged. Any named user may remove a mark or change its cadence; every
act is recorded. The action is available on every product. It is for the
developers and managers of products that have accepted some tests as flaky, so
those tests stop crowding the failures that need triage.

Done means: mark, change and unmark from the selection bar and the test page;
the tiles, the three home charts (a fourth segment), Watch, the browse rows,
Open Actions and the Flaky tab all agree on what is flaking; mute behaves
exactly as before; and the headline, browse and chart reads measure the same
as today on an estate with no marks, on SQLite and MariaDB alike.

Layers: backend (a new table for the marks and one for their history, so a
migration with its MariaDB ledger step; the cadence evaluation; the counts;
the endpoints) and UI (the action, the colour, the figures, the tab, the
state markers). On the performance trigger paths: yes (`storage.py`, `api.py`,
the home page's first paint), so round 0 carries §P and the build carries a
`/perf-ab`.

**Not in this arc, by the owner's direction:** a general clean-up of Open
Actions (an easy way to deal with passing tests and clear them in bulk) is a
follow-up design. An assigned test that is flaking is seen as a special case
of that, which is why it stays on Open Actions here with only a state marker.

## §1 The candidate contract

Written by the main session for the two layers to write against in parallel.
It is a starting position: either section may push back on any line, and the
reconciled contract replaces this one when the sections are assembled.

- **Identity.** A mark is per test in one environment: (environment, script,
  test name), the same key an assignment uses. At most one current mark per
  test. Fields: `cadence` (integer, 2 to 10), `marked_by`, `marked_at`,
  `reason`.
- **The rule, per stream.** A marked test is **flaking** on a stream when its
  latest result there is a failure and at least one of its N most recent runs
  on that stream is not a failure. It is **over its cadence** when its latest
  min(N, runs it has) runs on that stream are all failures. Which results
  count as "a failure" for this rule is §B's to define; the existing
  flakiness signal already has a definition and the two should not disagree.
- **State on a row.** Every row that carries `mute` today also carries
  `flaky`: null, or `{cadence, marked_by, marked_at, reason, state}` with
  `state` one of `"passing"`, `"flaking"`, `"failing"` for the row's stream.
  A live mute takes precedence in what is drawn and counted; the `flaky`
  object is still present so the tag can be shown.
- **Counts.** Wherever a payload carries `muted` beside a failing figure it
  gains `flaking` beside it, defined the same way: failing, unretired, marked,
  under the cadence, no live mute. Those tests leave `new_failures` and
  `still_failing` and the per-environment and per-script failing figures, as
  muted ones do; the pass-rate figures are unchanged. Candidate keys:
  `status.flaking`, `status.flaky_total` (every mark), `by_environment[].flaking`,
  `top_failing_scripts[].flaking`, `products[].flaking`, the Watch cards'
  `flaking`.
- **The trend.** The per-day chart gains a flaking series beside
  `FAIL_ASSIGNED` and `FAIL_MUTED`, served by the same off-first-paint part.
  What "flaking" means for a past day (as the test stands now, like the other
  two, or as it stood that night) is an open decision for §B and §P.
- **The Flaky tab.** A `flaky` queue beside `muted`, listing every mark
  whatever the test's result, fetched only when the tab is opened. Each entry:
  the test's key, `cadence`, `marked_by`, `marked_at`, `reason`, the state and
  latest result on the request's stream, and a measure of how often the test
  has actually failed (shape for §B to propose and cost).
- **Acts.** `POST /api/flaky/bulk` with `{username, reason, cadence, tests}`
  marks unmarked tests and changes the cadence of marked ones (the owner does
  not change); `POST /api/flaky/unmark` with `{username, tests}`. The reason
  is required for a fresh mark and is posted as a comment on each test, as a
  mute's is. Every act is a history row; the test detail carries the current
  mark and its history.
- **Open Actions.** Its rows carry the same `flaky` object so an assigned,
  flaking test can show its state marker. Nothing is added to or removed from
  the list by a mark.
- **The cost bar.** With no marks, every existing read costs what it costs
  today. With marks, the cadence is evaluated only for marked tests whose
  latest result is a failure, and reads at most 10 runs each.

## §2 Backend (§B)

*To be written by `backend-engineer`.*

## §3 UI (§UI)

*To be written by `ui-engineer`.*

## §3a Performance design pass (§P)

*To be written by `performance-engineer` against §B.*

## §4 Decisions

*Assembled by the main session from the sections.*

## §5 Open questions

*Assembled by the main session from the sections.*

## §7 The owner's verdicts so far (2026-11-11, in chat, captured verbatim)

**The request, as written:**

> One of the original key tennants of the board was that flakey tests were not
> to be tollerated. We have now widdened the net of our products we have
> onboarded to include products that have accepted certain tests as flakey. I
> want to add a new first-class "Mark as flakey" action that can be applied to
> a test. My idea is that a named user can mark a test as flakey with a
> cadence (e.g "Fail if the 3 most recent runs fail"). Flakey tests will not
> clutter up know failures, and will have their own muted colour (just like
> "muted" tests are grey). They don't count as "passes" - but they don't
> clutter things up. A mute is owned by a user, by they shouldn't clutter up
> the "open action" screen

The main session read the last sentence as "a flaky mark is owned by a user";
the owner did not correct it.

**Questions put in chat, each with a recommended position, and the owner's
answer.** Where the answer is the option's label, the label is quoted; where
the owner wrote their own, it is quoted in full.

| # | Question | Answer |
|---|---|---|
| 1 | Where does the mark apply? | "All streams, one env (Recommended)": one mark covers the test in that environment on mainline and on every release build, like an assignment; the cadence is judged within each stream's own runs |
| 2 | Does a flaky mark run out? | "Stands until removed (Recommended)": a Flaky list shows every mark with its owner, age and how often it has actually failed, so stale marks are visible without nagging anyone |
| 3 | How does a marked test count in the pass rate? | "Like mute (Recommended)": a pass when it passes, with a small flaky marker; a tolerated failure leaves New / Still failing and shows as "+N flaky", but still counts against the pass rate |
| 4 | What happens when it goes over its cadence? | "Fails, nobody's": it counts as a normal red, unassigned failure like any other; the mark's owner is only a record of who accepted the flakiness; the mark stays. (The recommended position, putting it on the mark owner's Open Actions, was not taken) |
| 5 | What shapes of cadence? | "N in a row, 2 to 10 (Recommended)" |
| 6 | A test is assigned and marked; while flaking, does it show on the assignee's Open Actions? | "It stays in open actiosn for now - we will need a state marker ("flaking"?). We need to do a follow-up design to clean up open-actions in general. We need an easy way to deal with "passing" tests and bulk clear them. I view your scenario as a special case of that" |
| 7 | How do mute and the mark combine? | "Mute wins": both may exist, and a live mute takes precedence; the failure shows grey as muted until the mute runs out, then as flaky or failing. (The recommended position, independent, was not taken) |
| 8 | A release build has fewer runs than the cadence needs. What is it? | "Failing if all runs fail": with fewer than N runs, the test is over its cadence when every run the build has is a failure. (The recommended position, tolerated until N runs, was not taken) |
| 9 | Every product, or switched on per product? | "Every product (Recommended)" |
| 10 | Is a reason required? | "Required, posted as comment (Recommended)" |
| 11 | Who can remove a mark or change its cadence? | "Anyone, recorded (Recommended)": changing the cadence does not change the owner |
| 12 | Where does the Flaky list live? | "A Home tab beside Muted (Recommended)": loaded only when opened |
| 13 | How do flaking failures appear on the three home charts? | "Own fourth segment (Recommended)": after the grey, with a legend entry, on all three charts |
| 14 | What are the states called on screen? | "Flaky mark, 'flaking' state (Recommended)": the action is "Mark as flaky" and the tab is "Flaky"; a tolerated failure carries the state marker "flaking"; a marked test that passed shows a small "flaky" tag; one over its cadence is just failing, with the tag |
| 15 | Should the test page's existing flakiness signal offer the action? | "Button beside the signal (Recommended)" |
| 16 | Start round 0? | "Yes, start round 0" |
