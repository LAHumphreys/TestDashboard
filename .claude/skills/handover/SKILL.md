---
name: handover
description: Rewrite docs/SESSION_HANDOVER.md from the current git state, the open PRs and the status log's newest entries. It is a snapshot, never a log; this skill refuses to append. Ends with the publication-gate pass because the handover is the likeliest carrier of internal detail.
---

# /handover

The handover is one screen of state, current by construction because it is
rewritten. Appending to it produces a worse log; the log is
`docs/UPGRADE_PLAN_STATUS.md`.

1. **Gather, from commands, not memory:** `git fetch --prune`, `git log
   --oneline -5 origin/master`, `git branch -a`, `gh pr list --state open`,
   `ls .claude/work/`, and the last two entries of the status log by grep.
2. **Rewrite every section** of the existing file in place, keeping its
   headings: Where things stand · Next session's plan · Where the code is
   (the branch table) · Needs a person, not a commit · Known slow, measured,
   not changed · Verification tooling · First ten minutes. Facts the operator
   has not reported are listed as unknowns to ask, never assumed.
3. **Registered builds** (from round 3): a table of plan, spec, branch and
   status, under "Where the code is". State lives here, never in a skill.
4. **Numbers come from the main checkout.** The expected suite count in
   "First ten minutes" is what `/verify` printed there, never a worktree's.
5. **Date it** in the "Last rewritten" line with what prompted the rewrite.
6. **Publication gate.** Before writing, read the draft once as an outsider
   would: no names, hosts, ticket ids, or lessons from internal use; the
   sibling project unnamed. Measurements and estate-scale numbers at the
   level already in `CLAUDE.md` are fine.
