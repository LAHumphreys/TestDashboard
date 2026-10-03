# Agent brief template

Every hand-off from the main session to `backend-engineer` or `ui-engineer`
uses this shape. Fill every heading; write "none" rather than omitting one.
The brief carries intent; the agent reads the code.

## Goal
One paragraph: what must be true when you are done, and why (the reason a
tester or an operator would give).

## Acceptance
- [ ] concrete, checkable statements: behaviour, named tests, measured numbers
      with the database they are taken on

## In scope
Files and modules you are expected to touch. Pointers, not instructions.

## Out of scope
What not to touch or "improve": neighbouring code, the other layer, docs,
What's new. Cleanup-only findings go in the report, not the diff.

## Context you need
Paths, not summaries: the `docs/ARCHITECTURE.md` bullets that bind this
change, the memory notes, the prior commits that carry this area's traps.

## Worktree
- Path: `<worktree path>`
- Base commit: `<sha>` — verify with `git rev-parse HEAD` before anything else
- Database: `<absolute path to the sacrificial option file assigned to you>`
  (SQLite needs nothing; the MariaDB leg drops and recreates that database)

## Verification
Exactly what to run and what evidence the report must cite. Default:
`python -m unittest discover` on SQLite, then with `TESTBOARD_TEST_DB_CNF`
set to the file above. UI: the walkthrough against a play server. Backend
touching `storage.py` or `api.py`: the cold, in-process A/B (round 2's
`/perf-ab`; until then, the method in `docs/SESSION_HANDOVER.md`).

## Cleanup contract
Any live state you may create (a play server, database copies, files outside
the worktree) and how it is removed before you finish. "none" if read-only.
**Only what your run created.** A dirty file you did not write is never yours
to revert; report it and the main session decides.

## Ledger
`.claude/work/<slug>.md`, created from `.claude/work/TEMPLATE.md` by the main
session with this brief. Keep it current at milestones (tick tasks, one-line
decisions and traps); if you stop early, fill Hand-off. On a restart, read it
first. The main session deletes it at commit.

## Report (at most 25 lines)
Return only: decisions you made that the brief did not settle; risks and open
questions; what you verified and how (named tests, measured numbers with their
database, surfaces walked); files changed. No file dumps, no narration of
steps, no restating the brief.

Why the cap: reports land in the main session's context, which is the scarce
resource. Anything longer belongs in a memory note or a doc the report points at.
