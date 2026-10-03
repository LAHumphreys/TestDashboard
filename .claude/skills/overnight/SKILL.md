---
name: overnight
description: The unattended build. Runs a frozen master plan from /design-review item by item through the full small path - brief, ledger, fresh agent in a worktree, verify, the performance pass on the trigger paths, fresh-eyes review, one commit, a log line - with a cron heartbeat armed before the first spawn, parking instead of stopping, and exactly one question at the kickoff gate. Orients from files alone so it survives compaction and killed agents. Its own readiness gate: two packages shipped through the supervised path first, or the owner's explicit go.
---

# /overnight <plan slug>

A run that drives itself for hours while the owner sleeps, built from the
same stage loop the supervised path uses. Nothing here is new behaviour;
what is new is that nobody is watching, so every piece of state lives in a
file and every step is idempotent.

## Readiness gate (its own, not a rollout stage)

Refuse to start unless one of these holds: `docs/SESSION_HANDOVER.md` records
at least two packages shipped through the full supervised path
(`/brief` → agent → `/verify` → `/review` → commit → drop), or the owner says
"go unattended" in this session for this plan. Say which applies in the
kickoff summary. A plan with no sizing pass (no ordered items with layers and
trigger status) is not a plan; send it back to `/design-review`.

## §1 Orient, from files only

Every turn starts here, including after compaction. Read, do not remember:
`git status` and `git log --oneline -3`; `ls .claude/work/` (live ledgers);
the plan's item list and its status strip in `docs/design/<slug>.md`;
`ListAgents`; `CronList`. Classify:

| State | Meaning | Do |
|---|---|---|
| Clean tree, no ledgers, no agents, no cron | Fresh start | §2 kickoff |
| No ledgers, cron armed | Between items | Next unticked item, §3 |
| A ledger, its agent alive | Item in flight | End the turn; the report will come |
| A ledger, no agent | Agent died | Spawn fresh from the ledger's Hand-off |
| A ledger whose item is committed | Outlived its commit | Delete it, record, continue |
| Dirty files no ledger claims | Foreign work | Park the run, report, stop |

## §2 Kickoff

1. `/verify` green on a clean tree, main checkout, both legs.
2. **One question, the only one of the run** (`AskUserQuestion`): the plan,
   its items in order, the readiness basis, the sacrificial databases and
   worktree base each agent will get, and "Go?".
3. On go, **arm the heartbeat first, before the first spawn**: `CronCreate`,
   hourly at an off-minute, whose prompt is `/overnight <plan slug>`. The
   heartbeat is what survives an API error between turns; a run once died
   three minutes after go because it was armed last. `CronList` at the top
   of every turn confirms it is still there.

## §3 The stage loop (one item = one brief + one ledger = one fresh agent = one commit)

1. `/brief` for the item; ledger created; worktree base and database assigned.
2. Spawn the owning agent fresh, in its worktree, in the background; end the
   turn. Both implementers may run at once on items from different layers.
3. The report arrives. `/verify` in the main checkout on the merged tree.
   Red goes back to the agent by `SendMessage` with the failing names; twice
   red parks the item.
4. On the trigger paths, `/perf-ab` by `performance-engineer`; a regression
   goes back to the implementer; twice parks.
5. `/review`: fresh eyes, the UI walkthrough where the item is UI, the
   publication gate. Findings back; twice parks.
6. One commit on the build branch, message from the ledger's decisions and
   the measured numbers, through the gate's hooks.
7. A line in `docs/UPGRADE_PLAN_STATUS.md`; the item ticked in the plan's
   status strip; the ledger deleted.
8. Report to the owner in at most 10 lines; next item for that agent.

## §4 Parking, not stopping

An item that needs the owner (`ESCALATE` in a report, a constraint conflict,
twice red) is parked: its ledger's status set to `blocked` with the reason in
Hand-off, a line in the plan's strip, and the loop moves to the next item
that does not depend on it. The run stops only when every remaining item is
parked or done, or on foreign work in the tree.

## §5 Interruptions

- **Compaction:** nothing is lost; §1 re-reads the files. Checkpoint before
  a long turn: ledgers current, plan strip current.
- **A dead agent:** its ledger is the restart point; spawn fresh, never resume.
- **Usage limit:** the cron fires again later; §1 classifies and continues.
- **Idempotency:** before any commit, check the item is not already
  committed (`git log --grep <slug>`); before any spawn, check no agent holds
  the ledger.

## §6 Done

Every item committed or parked. Final report, at most 15 lines, parked items
first with their reasons, then what shipped with its numbers, then what
`/drop` needs. Delete the cron job. The build stays a line in the handover's
registered-builds table until it is dropped.
