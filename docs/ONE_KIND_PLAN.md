# One stream kind — design and work order (WP-25)

**User decision, 2026-08-09 (late-day):** the branch/build distinction
adds confusion and is not worth its weight in the initial streams drop.
**Collapse to one non-mainline kind: `build`.** The compare
functionality is the point; the distinction can return later if usage
demands it — and because NOTHING kind-shaped has shipped anywhere (the
contract fields, migration 9's kind values, the split UI all exist only
on unshipped branches), this is deletion before first contact, not a
migration.

**Runs OVERNIGHT (fresh session), BEFORE WP-24** (`SCOPED_URLS_PLAN.md`):
this package deletes several kind-gated URL sites WP-24 would otherwise
carefully preserve. Branch `wp-25-one-kind` off `wp-23-longrunning`'s
tip; WP-24's `wp-24-scoped-urls` then cuts from WP-25's REVIEWED tip.
Same working model as every round: Sonnet implements from this spec,
the coordinator reviews each diff, pushes, checks all four CI legs;
never touch `master`/`wp-18-timeline`/`wp-14-in-run-progress`/the
repo-root `testboard.db`. Suite baseline at spec time: **1978 OK
(skipped=1)**.

---

## 1. Decided

1. `streams.kind` ∈ {`mainline`, `build`} — amend migration 9 IN PLACE
   (the established pre-ship fold precedent; note it in the migration
   comment and the registry row the same way the assignments fold was
   noted). UNIQUE (product, kind, name) unchanged.
2. **Import contract: `build` only.** The `branch` field is REJECTED
   with a clear per-record error ("branch: removed before this contract
   ever shipped — use build:"), NOT silently ignored — unknown-key
   tolerance would file a stale script's runs into mainline, and a loud
   error costs nothing. The mutual-exclusion rule dies with the field.
   README contract section updated; `streams_seen` values become
   `build:<name>` uniformly.
3. **Default baseline is mainline, always** (user decision, explicit).
   `pickDefaultBuildBaseline` and its tests are DELETED; the Compare-to
   picker (now shown for EVERY non-mainline stream) is how you choose a
   predecessor. Happy consequence, record it in the commit: the
   choosing-mainline sentinel bug's precondition (`89012d4`) is gone —
   absence means mainline everywhere again. KEEP the explicit
   `baseline=1` encoding and its guard test anyway (harmless, and it
   keeps the collision impossible rather than merely absent).
4. **Every behavior that gated on kind now gates on DATA:**
   - The two-tab dashboard ("Its own results" / "Difference from…"):
     any stream whose covered-passes count meets the existing threshold
     gets both tabs, however it was uploaded. The caption's plain-words
     rule stands.
   - The Watch `s:` card: one wording for all streams (the current
     build wording, verdict vs mainline).
   - The picker: one flat group, newest-first by `last_seen`,
     searchable, stale-folded — as today, minus the group split.
   - The band: one label ("Build"), one look.
5. **Feeder:** `--build NAME` only; `--branch` removed (never shipped).
   Per-stream state files keep their kind prefix in the filename
   (`build-`) — one naming scheme, no migration of state files needed
   since none exist in the wild.
6. **`tools/drop_stream.py`:** `--kind` argument removed; product+name
   identify a stream.
7. Docs: `STREAMS_PLAN.md` gains a prominent as-built note at the top
   of §3 (do not rewrite history in it — the log stays; the note says
   WP-25 collapsed the kinds and why); `whatsnew.html` and the drop
   note updated (this ships inside the same combined drop); the
   UPGRADE_PLAN registry row for 9 re-annotated.

## 2. Scope of change (the estimate the user asked for)

Backend: `storage.py` (kind validation, `_stream_key_for`,
find-or-create, `list_streams` grouping), `api.py` (import validation,
streams/compare/watch payload wording), `feeder/` (flag + validation),
`tools/drop_stream.py`. Frontend: `streams.js` (one group),
`compare.js` (delete default-pick; un-gate Compare-to; band label),
`app.js` (two-tab gate on data not kind), `watch.js` (one card
wording), `test.js` (switcher labels). Tests: the bulk of the work —
merge/rename the branch-kind halves of the stream suites; every
deleted behavior's test deleted WITH its feature (never weakened while
the feature lives). Seeds: the scratchpad seed scripts switch
`branch:`→`build:` and re-run against a FRESH dev copy (the current
scratch DB contains kind='branch' rows; recreate rather than migrate
scratch data). Estimated as one medium implementer round (~2–3h) plus
review — smaller than WP-21, larger than a fix round.

## 2b. Also in this round (user-reported, 2026-08-09 evening)

1. **Stream-scoped Time/Timeline empty states must say WHERE the data
   is.** A build that ran on one environment shows a bare empty page on
   every other environment (verified live: 2026.9.1 = 0 rows on
   atlas-lab-alpha, 69 on atlas-lab-bravo — the data is honest, the
   page is not). When `stream=` is set and the current environment has
   no rows, the empty state names the environments the stream DOES have
   runs on — each a link that switches only the environment param
   (scope rules apply) — or states plainly that the stream has no runs
   anywhere. The list comes from the stream's `latest_runs` partition
   (one grouped query, O(partition), no new endpoint unless the payload
   genuinely lacks it). Same treatment on Time. Guard test per the
   empty-state patterns.
2. **Rewrite the combined drop's What's New section for scannability.**
   User verdict: "simply unreadable — no one will get past the first
   section... highlight the new functionality, not a work of
   literature." Rewrite `static/whatsnew.html`'s 2026-08-14 section
   AFTER the kind collapse (so wording matches the one-kind world):
   lead with what a tester can now DO, one short bullet per capability
   (products switcher and scoping; the Watch page and its URL-shared
   cards; uploading builds and comparing them — vs mainline or each
   other; assigning from a build's failures; per-build test history and
   suite pages; the highlights and staleness cadences), each bullet one
   sentence with WHERE to click, details deferred to the pages
   themselves. Keep `data-drop-date` and the heading in lockstep
   (`DropDateTest`); keep the tester-not-operator voice rule from
   CLAUDE.md; the operator note stays detailed — it has a different
   reader and is not this item's target.

## 3. Verification

- Full suite green every commit; dual-backend if the local MariaDB
  starts (report either way).
- DOM-shim walks re-run: RC compare flow (predecessor via picker now),
  two-tab on a cadenced stream, Watch mixed cards, `branch:` rejection
  through the real import path.
- The reseeded server demonstrates: an RC pair (compare via picker),
  a cadenced stream with both tabs, the new-tests/no-result shapes
  (see the 2026-08-09 seed addendum — carry those rows over), and
  Corvus (old-client, mainline-only, zero stream UI).
- Endpoint timings unchanged (spot-check; nothing here touches query
  shapes).

## 4. Done when

No code path, test name, payload field, UI string, or doc line
distinguishes branch from build (except the historical log and the
STREAMS_PLAN as-built note); `branch:` on the wire is a loud
per-record rejection; suite green on all four legs; WP-24 can cut from
the reviewed tip.
