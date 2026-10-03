# PROCESS.md — how work gets done in this repository

The human-readable twin of the rules in `CLAUDE.md`, the agent definitions
under `.claude/agents/` and the skills under `.claude/skills/`. **When any of
those changes, this file changes in the same commit**, with a dated line in
§9. The design it came from is `docs/design/agent-process-r0.md`.

## §1 Why the process looks like this

Three goals, in priority order:

0. **The users' time.** They are developers maintaining products, reviewing
   nightly test runs on mainline and release builds. Green should cost a
   glance; red should cost a quick triage (review, comment, assign); and when
   a failure needs investigating, the depth (captured output, run history,
   build-against-mainline history, the comparison tool) must be a click away
   and complete. The board is a purpose-built tool, not the driver of their
   day. Every other goal serves this one.
0b. **Responsiveness, and staying lean.** Page load time and responsiveness
   throughout the app are a top priority, valued above features and tested
   for. Even as features land the board stays a lean, purpose-built tool,
   never commercial bloatware with a hundred plugins installed. A change
   that would slow anything is flagged at design time (the brief's
   Performance impact section), designed to minimise the cost, measured
   cold before review, and reported with numbers. Regressions block.
1. **Quality through specialists.** Each layer of the code has one implementer
   that reads only that layer's traps on spawn, writes the code and its tests,
   and verifies on both backends. No rule may trade output quality for tokens.
2. **The main session's context is the scarce resource.** It designs, briefs,
   reviews and ships; it never types code. Reports are capped so that what
   comes back is a decision, not a transcript.
3. **Nothing in flight dies with a context.** Every task has a ledger on disk
   that a fresh agent can restart from; every drop has an operator note written
   before it ships; the handover is rewritten, never appended.

Measured on the first agent-driven drop (2026-10-01): the single largest
context cost was the main session implementing the hard part itself; the
second was reviewing what a cheaper implementer had under-polished.

## §2 Who does what

| Role | Model | Owns | Never |
|---|---|---|---|
| Main session | Fable | Specs, briefs, reviews, commits, drops, `CLAUDE.md`, `PROCESS.md`, `.claude/`, the `docs/` state files, git and PRs | Edits a file under `testboard/`, `static/`, `tests/`, `tools/`, `clients/`, `feeder/` |
| `backend-engineer` | Opus | `testboard/`, `tools/`, `clients/`, `feeder/`, the two `run_*.py`, `tests/` except the frontend guards, the runbook, the feeder template, the registry row it claims | Writes frontend; weakens a guard; adds a host dependency; edits `MIGRATIONS[0]`; commits |
| `ui-engineer` | Opus | `static/`, `tests/test_frontend_calls.py`; may edit `whatsnew.html` but never adds a release section | Writes backend; blocks the progressive load; puts a user string through `innerHTML`; commits |
| `performance-engineer` (round 2) | Opus | The design pass and the measured A/B pass on the trigger paths; `tools/dev/perf/` and the Known slow baseline | Edits product code; runs on a light change off the trigger paths |
| Fresh-eyes reviewer | Sonnet, spawned by `/review` | A diff against its acceptance list, then the mechanical sweep, then the publication gate's judgement pass | Edits anything |
| Recon and sweeps | Sonnet (`Explore`, `general-purpose`) | Reading, measuring, drafting a docs section into a ledger | Edits repo code |

Why two implementers: the storage/API seam is one injected object and an API
change almost always needs the storage signature in the same head. Parallelism
comes from the backend/UI split, which is a real contract (the JSON the API
emits). Why no docs agent: every document here has a reader and a rule, and the
counts in them must come from the main checkout; `/drop` and `/handover` run in
the main session and delegate only drafting.

## §3 The life of a request

| Kind | Path |
|---|---|
| Question | Answered from the code; no agent |
| Small change | `/brief`, spawn in a worktree, report, `/verify`, `/review`, commit, log entry |
| Large change | Design doc in `docs/design/`, review rounds, sizing, then `/overnight` (round 3) or a run of the small path |
| Drop | `/drop` (round 2), PR, green, squash, deploy, `/handover` with the deploy answers |

One item is one brief plus one ledger, one fresh agent, one commit. A new
request is a fresh spawn; `SendMessage` continues only judgement that lives in
an agent's context, such as a review finding going back to its implementer.
Hand off at durable artefacts, never mid-build; there is no turn budget.

### The performance pass, and light changes

The performance pass runs only when a change touches `testboard/storage.py`,
`testboard/api.py`, the push path, or anything on a page's first paint, or
when the main session calls it on judgement. A change whose brief answers
Performance impact with "none, and here is why" and touches none of those
paths goes straight to fresh-eyes review: **light changes with obviously no
performance impact are never held up by process.**

### Worktrees and databases

Every implementer runs in its own worktree, always, even when alone. Two
incidents came from sharing a checkout: a `git stash` that ate another agent's
live edits, and worktrees cut from the wrong base commit. The brief's
**Worktree** heading names the base commit, which the agent verifies with
`git rev-parse HEAD` before touching anything, and which sacrificial MariaDB
database the agent may drop. The main session merges; agents never merge each
other and never commit.

## §4 How to ask so the flow works

- Say what must be true when it is done, not how to do it; the brief carries
  intent and the agent reads the code.
- A question and a change are different requests; ask them separately.
- A design doc goes through `docs/design/` with a round number; comments on
  it are captured verbatim into the file before anything is rewritten.
- "Quick" is not a size class. The one-line change goes through the same path;
  the path is cheap when the change is.

## §5 How a drop ships

Changes ship as a dated drop: batched, verified together, deployed as one
group. Two documents, two readers, neither substitutes for the other.

- **`static/whatsnew.html`** gets a dated section at the top (newest first),
  written for a *developer who glances at the board*: what changed, where to
  find it, what it means for them, in the fewest lines that say so. Nothing user-visible ships without a line there, and nothing appears
  there that is not in the build. Every section carries
  `data-drop-date="YYYY-MM-DD"` matching its heading; `DropDateTest` fails the
  build otherwise. `/drop` drafts it from the merged commits; the main session
  owns it.
- **`docs/drops/YYYY-MM-DD.md`** is the operator note, written for whoever
  deploys it, and written *before* it ships so it is a plan and not a memoir.
  It must contain:

  | Section | Why it is not optional |
  |---|---|
  | Suite count, schema version, whether a **migration runs** | Decides whether the rollback is `git checkout` or a database restore. Say it explicitly even when the answer is "none" |
  | What changed, fixes first | The operator is triaging, not reading a changelog |
  | The **exact commands**, including the stop/copy/pull/start order | "Restarting the server is not optional" has still been forgotten twice |
  | How to **check it came up**, and how to **roll back** | A rollback improvised during an incident is a second incident |
  | New flags and any **decision the operator has to make** | A flag nobody was told about is a flag nobody uses |
  | **What was not verified** | No browser has ever rendered this project's UI before a drop; say so every time rather than letting green tests imply otherwise |

  One screen per section; lead with the number that changes the plan.
- The PR body is the squash-commit message. After the deploy, `/handover`
  records what the operator reported and lists what was not reported as
  unknowns, never as assumptions.

## §6 The publication gate

**This repository is public.** Everything that reaches origin is published:
commit messages, PR bodies, the handover, the status log, drop notes, What's
new, design docs, agent definitions. None of them may carry a person's name
or handle; an employer, team, product or site name; a real hostname, address,
port or internal URL; a ticket id; or anything that reads as a lesson from
internal use rather than a fact about this code. Estate-scale numbers already
in `CLAUDE.md` are the accepted ceiling. A sibling project whose process this
one borrows is referred to as "a sibling project", never by name.

Two layers:

- **Mechanical** (round 2): a term list that is itself never committed
  (`.claude/private/`, gitignored), scanned by a stdlib script in `tools/dev/`
  that local `commit-msg` and `pre-commit` hooks run; a hit blocks the commit.
- **Judgement** (now): `/review` and `/handover` end with a pass over the diff
  of tracked text and the commit message asking one question, "would this read
  as internal to an outsider", reporting hits and never editing.

The gate never rewrites history. Removing something already published is the
owner's decision, taken in chat, not a skill. Agent memory lives in the tree
at `.claude/agent-memory/` and is gitignored for the same reason; the durable
onboarding an agent needs after a reinstall lives in its definition and in
`docs/`, which are written to be public.

## §7 The instruments

| Instrument | What it is for |
|---|---|
| `.claude/brief-template.md` | Every hand-off; `/brief` fills it and shows it before spawning |
| `.claude/work/TEMPLATE.md` | The ledger: restart point for a killed agent; deleted at commit |
| `/verify` | The gate before review: the suite on SQLite, then the MariaDB leg with the brief's database; quiet output |
| `/review` | The fresh-eyes pass, the UI walkthrough, the publication-gate judgement |
| `/handover` | Rewrites `docs/SESSION_HANDOVER.md`; refuses to append |
| `docs/UPGRADE_PLAN_STATUS.md` | The record: what was done, measured and decided, including design-review rounds |
| `.claude/agent-memory/<agent>/` | What an agent learned since its definition was written; never state; gitignored |
| Round 2 adds | `performance-engineer` and its `/perf-ab` (cold, in-process, alternated A/B of two trees), `/drop`, `tools/dev/` (the DOM-shim net, seeders, A/B runner, the gate's scanner) |
| Round 3 adds | `/overnight`: the unattended stage loop with a cron heartbeat armed before the first spawn |

## §8 How to spot the process not working

| Symptom | Meaning | Action |
|---|---|---|
| The main session is reading a 9,000-line test file whole | It is about to implement | Stop; `/brief` |
| A report is longer than 25 lines | The agent is narrating | Send it back for the report, not the story |
| A number in a doc has no command behind it | It was estimated, or taken in a worktree | Re-measure in the main checkout; label the database |
| A ledger outlived its commit | The item was never closed | Read it, close it or park it, delete it |
| A guard test was edited to pass | A finding was weakened | Widen the scope instead, and say so in the commit |
| Two agents edited one checkout | No worktree | Isolate, then reset each to its base |
| A What's new line with nothing behind it | The note was written from the plan | Remove it; users report its absence as a bug |

## §9 Decisions log

Dated, newest last, each with the incident behind it. Append only.

- **2026-10-01.** The first agent-driven drop (WP-40, muted failures). What it
  taught, each now a rule above: the main session typed the storage core (§2,
  no size exception); worktrees were cut from the wrong base and one checkout
  was shared (§3, worktree always, base verified); the performance A/B harness
  lived in a gitignored scratch directory invisible to agents and CI (§7,
  round 2 promotes it); the docs agent quoted suite counts from its own
  worktree (§8, numbers come from the main checkout); the storage and API
  agents collided on one signature (§2, two territories not three); the
  cheaper implementers' UI needed a polish walkthrough that cost more than the
  tier saved (§2, Opus implementers); the context compacted mid-build with no
  ledger to recover from (§7, the ledger).
- **2026-10-03.** Design review round 0 of this process accepted: thirteen
  decisions, recorded in `docs/design/agent-process-r0.md` §4 with the
  owner's verdicts. Two revisions during review: agent memory is gitignored
  rather than committed, and a publication gate screens every commit, both
  because the repository is public. The sibling project whose process is the
  model is not named in tracked text. Round 1 (this commit): `PROCESS.md`,
  the `CLAUDE.md` split with `docs/ARCHITECTURE.md`, the two agent
  definitions, the templates, four prose skills. Round 2: the gate's scanner
  and hooks, `tools/dev/`, `/perf-ab`, `/drop`, two supervised packages.
  Round 3: `/overnight`.

- **2026-10-03, later.** The owner corrected who the board is for: developers
  maintaining products, reviewing nightly test runs, not "testers". Green
  costs a glance, red a quick triage, and an investigation has the depth it
  needs (output, run history, build versus mainline, the comparison tool);
  the board is purpose-built, not the driver of their day. The round-1
  ui-engineer definition had said "read all day", and the first correction
  overshot to "nothing after triage"; both were wrong. Now §1 goal 0, `CLAUDE.md` "Who the board is for", and
  the first line of the ui-engineer's bar; the word "tester" is gone from
  every process file.

- **2026-10-03, later still.** The owner asked for a strong guide to both
  agents that load times and responsiveness throughout the app are a top
  priority, tested for, with anything that would hurt them flagged at design
  time and designed to minimise the cost. Now §1 goal 0b, `CLAUDE.md`
  "Responsiveness is a top priority", a Performance impact heading in the
  brief template, a section in each agent definition, and a line in
  `/review`'s sweep.

- **2026-10-03, evening.** The owner proposed a dedicated performance
  engineer: a design pass and a measured A/B pass in its own context,
  owning the harness, so the implementers stay focused while staying
  conscious of cost. Agreed, with three pushbacks accepted: the
  implementers keep the design-time duty; the pass runs on a path trigger,
  not by default, so light changes with obviously no performance impact
  are never held up; it is a reviewer with an instrument, not a third
  implementer. D14 in the design doc; built in round 2.

## Maintaining this document

A rule without a dated entry in §9 is a preference. A change to any agent
definition, skill, template or `CLAUDE.md` rule lands with its line here, in
the same commit, and that commit passes the gate in §6 like any other.
