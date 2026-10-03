---
name: design-review
description: Start or continue a design arc. /design-review <what you want> opens a discussion with the main session that settles what is being built, then the owning agents write round 0 of a design doc in docs/design/, the owner reviews it as a Claude Doc or inline, each later round captures the owner's comments verbatim before anything is rewritten, and the closed arc hands off to a sizing pass and a build. Use for anything larger than one brief.
---

# /design-review <topic or slug>

A design arc is for anything larger than a single brief: a new feature, a
data-model change, a new page, a process change. It produces a frozen,
reviewed design doc at `docs/design/<slug>-r<N>.md` and ends with a sizing
pass that the build runs from. The owner's decisions are the record and are
captured verbatim before anything else happens to them.

## Starting an arc (no doc exists yet)

1. **Discuss first, write second.** The main session asks what the owner wants
   until it can state §0 in one paragraph: what is being built, for whom (the
   developers who glance at the board; the operator), and what "done" looks
   like. It names the layers touched (backend, UI, both) and whether the
   change is on the performance trigger paths. It does not draft anything
   until the owner agrees with the §0 paragraph in chat.
2. **Round 0 is written by the owning agents, not the main session.** Each
   layer's agent gets a brief whose Goal is "write round 0 of the design doc
   for your layer". Backend docs ship example payloads and rows, the data
   model, the migration if any, and the memo and cost reasoning. UI docs ship
   text wireframes of every surface, the states (loading, empty, error), and
   what loads first. A doc that crosses the API contract is two sections
   written against the same §0, backend first or in parallel. On the trigger
   paths, `performance-engineer` adds its design pass as its own section:
   cost, where it lands, the cheaper shape, verdict. A design doc on the
   trigger paths is not round 0 without it.
3. **The doc's shape:** §0 what we are building; decisions written as the
   recommended position each, with a verdict slot; numbered open questions;
   the 20-minute reading bar. The main session assembles the agents' sections
   into `docs/design/<slug>-r0.md`, reads it once as the owner would, and
   fixes what a reader would trip on before the owner sees it.
4. **Render for review.** Publish it as a Claude Doc (the Docs artifact type)
   with a verdict dropdown per decision and a checklist of open questions, or
   hand over the repo file for inline `// owner:` comments, whichever the
   owner asks for. The committed file is the record; the doc is the viewer.

## Each later round

1. **Capture the comments verbatim first.** Before anything is rewritten,
   the owner's comments, verdicts and chat decisions are copied into the repo
   file (a "Round N review" section), word for word, and committed with the
   status-log line for the round. Classify each as a decision, a question or
   a style instruction.
2. **Dispositions by the owning agent**, under a brief: a report of at most
   25 lines led by a comment-to-disposition map (accepted, changed to X,
   pushed back because Y). The main session may push back on the owner's
   behalf where a comment conflicts with a constraint, and says so.
3. **Two eyes before the round commits:** the main session, then a Sonnet
   fresh-eyes pass whose first job is the big picture: does the doc still
   describe one coherent thing. Cross-review between `backend-engineer` and
   `ui-engineer` only when the doc crosses the API contract.
4. **Record:** the round's entry in `docs/UPGRADE_PLAN_STATUS.md` (D11: the
   log is the record, there is no reviews tree), the file renumbered to
   `-r<N+1>.md` only if the owner wants the history side by side; otherwise
   the one file carries its rounds.

## Closing the arc

When no decision is open: a **sizing pass** turns the doc into a master plan
(items in order, each one brief, with its layer, its trigger status and what
it is gated on), the doc is marked frozen in its status line, and the build
is registered as a line in `docs/SESSION_HANDOVER.md`. Small plans run the
small path item by item; large ones run through `/overnight`. Every item
in the plan carries its trigger status, so the performance pass is planned
into the build rather than discovered during it.

## Publication gate

The doc is tracked, so it is published. No names, no hosts, no internal
lessons; the sibling project unnamed. The gate's judgement pass runs on every
round's commit as it does on any other.
