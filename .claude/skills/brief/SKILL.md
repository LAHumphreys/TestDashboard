---
name: brief
description: Draft an agent brief from .claude/brief-template.md for a described task, pick the owning agent by path, assign a worktree base and a sacrificial database, create the ledger, and show the brief before anything is spawned. Use for every hand-off of a code change; the main session never implements.
---

# /brief <task>

1. **Classify.** A question is answered from the code; stop here. A change
   gets a brief. A change touching both `static/` and the backend is two
   briefs against an agreed JSON contract, backend first or in parallel.
2. **Owner by path.** `static/` or `tests/test_frontend_calls.py` →
   `ui-engineer`; anything else under `testboard/`, `tools/`, `clients/`,
   `feeder/`, `tests/`, the two `run_*.py`, the runbook or the feeder
   template → `backend-engineer`, except `tools/dev/perf/` and
   `tests/test_dev_perf.py`, which are `performance-engineer`'s. `CLAUDE.md`,
   `PROCESS.md`, `.claude/`, the `docs/` state files are the main session's;
   they need no brief.
3. **Fill every heading** of `.claude/brief-template.md`. Acceptance lines are
   checkable; context is paths; out-of-scope names the other layer, docs and
   What's new. Verification names the exact commands. **Performance impact**
   is answered, never blank: "none, and here is why" or the cost and where it
   lands.
3b. **The performance design pass, on the trigger paths only.** If the brief
   touches `testboard/storage.py`, `testboard/api.py`, the push path or a
   page's first paint, spawn `performance-engineer` with the brief before it
   is shown: its answer (cost, where it lands, cheaper shape, verdict, at
   most 15 lines) is appended under Performance impact, and a cheaper shape
   it proposes becomes the brief's design unless the main session says why
   not. Off those paths, with "none, and here is why", nothing is spawned:
   light changes are not held up.
4. **Worktree and database.** Base commit is the branch head the change
   builds on (`git rev-parse <branch>`), written as a sha. Assign one of the
   sacrificial MariaDB option files listed in `docs/SESSION_HANDOVER.md`
   (Verification tooling) that no other live agent holds; the main session's
   own `/verify` keeps the last one. Spawn with `isolation: "worktree"` and
   tell the agent to verify the base commit before touching anything.
5. **Ledger.** Create `.claude/work/<slug>.md` from `TEMPLATE.md` with the
   brief's tasks as the task list.
6. **Show the brief and wait.** Nothing is spawned until the owner has seen it,
   unless the session is an unattended run that has already been gated.
7. **Spawn fresh.** One brief, one agent. A follow-up to a finished agent is a
   new brief and a new spawn; `SendMessage` is only for a review finding
   going back to the implementer whose context holds the judgement.
