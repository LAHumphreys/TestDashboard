---
name: drop
description: Ship a dated drop. Runs the release ritual in order from named commands - verify green, the publication gate, the operator note (migration yes or no first), the What's new section for developers, the handover rewrite, the PR whose body is the squash-commit message, then the after-deploy bookkeeping once the operator reports. Every number comes from a command run in the main checkout.
---

# /drop <YYYY-MM-DD>

Changes ship as a dated drop: batched, verified together, deployed as one
group. Two documents with two readers (`PROCESS.md` §5); this skill writes
both and the PR, in this order, and refuses to skip a step.

## 0. Preconditions

- The drop branch (`drop-<date>` or the single `wp-<n>-<slug>`) is rebased on
  `origin/master`; `/verify` is green on it in the main checkout, both legs;
  every package on it passed `/review`, and `/perf-ab` where its paths
  required it.
- Every migration on the branch has its registry row in `docs/UPGRADE_PLAN.md`
  §1, its ledger step in the upgrade tool, and its table in the exporter's
  `ddl()`; `LedgerTest` is the proof.
- The publication gate has run on every commit; it runs again on the drop
  note, What's new and the PR body below.

## 1. The operator note: `docs/drops/<date>.md`

Written for whoever deploys, *before* it ships. Use the newest existing
note as the template; the sections are mandatory (`PROCESS.md` §5): suite
count and schema version and **whether a migration runs**, stated first;
what changed, fixes first; the exact commands in stop/copy/pull/start order,
including the upgrade tool's dry run and its `SERVER:` line for a MariaDB
schema change; how to check it came up; how to roll back; new flags and any
decision the operator must make; **what was not verified** (no browser has
ever rendered this UI before a drop; say so). Numbers come from `/verify`'s
output and the registry, never from memory or a worktree. One screen per
section. Delegate the first draft to a Sonnet agent given the merged commit
messages and the previous note; the main session edits it.

## 2. What's new: `static/whatsnew.html`

A dated section at the top, `data-drop-date="<date>"` matching its heading,
written for a developer who glances at the board: what changed, where to find
it, what it means for them, in the fewest lines that say so. Nothing goes in
that is not in the build; nothing user-visible ships without a line. Run
`python -m unittest tests.test_frontend_calls` for `DropDateTest`. The
`ui-engineer` never adds this section; the main session owns it.

## 3. The status log and the handover

Append the drop's entry to `docs/UPGRADE_PLAN_STATUS.md` (what shipped, what
was measured, what was decided). Run `/handover`: the branch table shows the
drop branch and its PR; "First ten minutes" carries the new suite count.

## 4. The PR

Title: "The drop of <date>: <what, in a clause>". Body: the operator note's
"what changed" in prose, the migration line, the verification line, then the
attribution footer. **The body is the squash-commit message**; when the PR is
green and the owner says so, squash-merge with the PR text, delete the
branch.

## 5. After "deployed"

The owner reports; nothing is assumed. Record in the log what was reported
(the dry run's `SERVER:` line, start-up output, timings, the Metrics page's
schema version) and list what was not reported as unknowns to ask. Stamp the
operator note "Shipped <date> (PR #n, <sha>)". Run `/handover` again. Update
the Known slow table only from a `/perf-ab` run on the merged commit.
