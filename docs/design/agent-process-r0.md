# Agents, skills and Claude files for testboard — design review, round 0

**Status: reviewed 2026-10-03; verdicts recorded below. Nothing here is built.**
Round 1 turns the decisions into files. The Claude Doc is the viewer; this file is
the record.

## §0 What we are building

A working process for this repo in which the main session (Fable) designs,
briefs, reviews and ships, and never types code; two path-scoped implementer
agents own the code; a small set of skills carry the rituals that are currently
re-derived from a 210-line CLAUDE.md and a 4,300-line log every session; and
memory is split so that each reader loads only what it needs. The model is the
process a sibling project settled on, adapted to what is different here: a stdlib-only Python 3.6 server with two
equal database backends, a drop discipline with two documents per release, and
a performance bar that has to be measured cold and in-process.

The reason to do it now is in two numbers: the code is ~79,000 lines across
`testboard/`, `static/`, `tests/`, `tools/` and `clients/`, and last night's
WP-40 build was carried by one Fable context that ran from spec to deploy and
compacted on the way. That worked once. It is not a process.

## §1 What the sibling project settled on, and what it bought

A sibling project's shape (private; not named here, this repo is public):

| Element | What it is | Verdict for us |
|---|---|---|
| "Who implements what" table in CLAUDE.md, with a mechanical path check | Main session never edits code; `web/` goes to `ui-designer`, everything else to `model-scientist`; no size exception | **Adopt.** The rule was breached repeatedly while it had exceptions; it held once it had none |
| Two Opus implementers, each a domain authority with its own memory | Each is sole owner of a path territory and also the reviewer of the other's design docs | **Adopt the shape**; the model choice is decision D2 |
| Brief template (8 headings, report capped at 25 lines) | Goal, acceptance, in/out of scope, context as paths, verification, cleanup contract, ledger, report cap | **Adopt verbatim**, plus a worktree/database heading (§3.6) |
| Work ledger per task, deleted at commit | Restart point for a killed agent; survived five session-limit kills | **Adopt** |
| Fresh-eyes review before commit, never by the implementer | `/code-review` medium or a Sonnet agent; findings go back via SendMessage | **Adopt**, and keep the Fable walkthrough as a second, judgement-level pass for UI |
| `PROCESS.md` with a dated decisions log, updated in the same commit as any process change | The process's own memory; every rule traces to an incident | **Adopt** |
| Per-agent memory in the repo tree | Index plus topic notes; the index bullets grew into long status lines | Keep it in the tree but **gitignored** (this repo is public); **cap the index** (§3.5) |
| `/design-review`, then a sizing pass, then `/autonomous-build` with a cron heartbeat armed first | Unattended 12-hour runs, parking not stopping, one AskUserQuestion at the gate | **Adopt a trimmed version** after two supervised runs (D8) |
| Commands without frontmatter; no skills; no hooks | Everything is prose in `.claude/commands/*.md` | Use **skills** with frontmatter here (D6); no hooks in round 0 (D10) |
| Token measurement from the transcripts | Showed the cost driver is long agent runs, not model tier; a turn budget was rejected to protect quality | Borrow the method |

What drifted there is one pattern, so we design it out: state living inside
process text (a memory rule that no longer matched the repo, a mechanism
described two ways, a register growing inside a command file, superseded
notes still indexed). The rule for us: **process files hold rules; state
lives in the handover, the log, or a ledger, never in a skill or an agent
definition.**

## §2 What last night taught us here

The WP-40 build (muted failures, migration 11) was the first agent-driven drop
in this repo. The evidence it left:

| Worked | Hurt |
|---|---|
| Fable wrote the spec and the storage core, then reviewed every agent diff | Fable also typed the storage core, the single largest context cost of the night |
| Sonnet agents in worktrees with separate sacrificial MariaDB databases ran storage, API, UI, docs and perf in parallel | Worktrees were cut from the wrong base; every agent needed `git reset --hard <base>` by hand. One worktree is still locked by a stray process |
| The perf A/B agent caught a real regression (the mute join on the count queries, +15 to 31 ms) | The A/B harness lives in gitignored `.scratch/`, invisible to any fresh agent and to CI |
| The docs agent produced the drop note, What's new and the handover | It quoted test counts from its own worktree, not the main checkout; corrected by hand |
| The storage and API agents ran in parallel | They collided once (a rollup gained an argument on one branch only) |
| Review-by-diff found the polish gap | The owner noticed it before the review did, and attributed it to the cheaper implementers. The fixes (a tooltip string hack, a trailing ellipsis, dead CSS) were cheap individually and expensive as a Fable walkthrough |
| One context ran spec to deploy | It compacted mid-build. Nothing was lost, by luck: no ledger existed to recover from |

Two of those rows are the whole argument for D2 and D9.

## §3 Proposed shape

### 3.1 Roles

| Role | Where defined | Model | Owns | Never |
|---|---|---|---|---|
| Main session | nowhere; it is the session | Fable | Specs, briefs, reviews, commits, drops, `CLAUDE.md`, `.claude/**`, the `docs/` state files, git | Edits a file under `testboard/`, `static/`, `tests/`, `tools/`, `clients/`, `feeder/` |
| `backend-engineer` | `.claude/agents/` | Opus (D2) | `testboard/`, `tools/`, `clients/`, `feeder/`, `run_feeder.py`, `run_server.py`, `tests/` except `test_frontend_calls.py`, `docs/MARIADB_MIGRATION.md`, `docs/FEEDER_TEMPLATE.md`, the migration registry row it claims | Writes frontend; weakens a guard test; adds a host dependency; edits `MIGRATIONS[0]` |
| `ui-engineer` | `.claude/agents/` | Opus (D2) | `static/**`, `tests/test_frontend_calls.py`, the user-facing section of `static/whatsnew.html` | Writes backend; blocks the progressive load; puts a user string through `innerHTML` |
| Fresh-eyes reviewer | not a file | Sonnet | A diff against its spec section, then the mechanical sweep | Edits anything |
| Recon and sweeps | `Explore`, `general-purpose` | Sonnet | Reading; measuring; drafting a docs section into a ledger | Edits repo code |

Why two implementers and not three. The storage/API seam is one injected
object; an API change almost always needs the storage signature in the same
head, and last night's one collision was exactly that seam. Parallelism comes
from the backend/UI split, which is a real contract (the JSON the API emits).
If a package is storage-heavy and API-heavy at once, it is one agent working two
commits, not two agents.

Why no docs agent. Every document here has a reader and a rule (the operator
note's six sections, the handover rewritten not appended, the log append-only),
and the counts in them have to come from the main checkout. That is a skill the
main session runs (§3.4, `/drop`), delegating the drafting to a Sonnet agent
whose brief names the exact commands whose output it may quote.

### 3.2 The files

```
CLAUDE.md                      ~110 lines (§3.3)
PROCESS.md                     why the process is shaped this way; decisions log
.claude/
  settings.json                permissions.allow for unittest, python, read-only git, gh pr view
  agents/
    backend-engineer.md
    ui-engineer.md
  agent-memory/<agent>/        gitignored, never committed (D4): the repo is public
  skills/
    brief/SKILL.md             drafts a brief from brief-template.md; shows it before spawning
    verify/SKILL.md            the gate: unittest on SQLite, the MariaDB leg with the agent's cnf, quiet output
    review/SKILL.md            spawns the fresh-eyes reviewer with the spec section and the diff
    perf-ab/SKILL.md + ab.py   in-process, alternated, cold A/B of two trees (the WP-40 harness, promoted)
    drop/SKILL.md              operator note, What's new, handover, PR body, in that order, from named commands
    handover/SKILL.md          rewrite SESSION_HANDOVER.md from git state and the log's last entries
    overnight/SKILL.md         the trimmed autonomous build (D8)
  brief-template.md
  work/TEMPLATE.md             ledger skeleton; live ledgers beside it, deleted at commit
docs/
  ARCHITECTURE.md              the "Architecture (dashboard)" bullets, moved out of CLAUDE.md (D5)
  design/<slug>-r<N>.md        design docs under review (this file is the first)
  SESSION_HANDOVER.md          unchanged role
  UPGRADE_PLAN*.md, drops/     unchanged
tools/dev/                     the DOM-shim net and seeders, promoted from .scratch (§5 Q3)
```

### 3.3 CLAUDE.md: what stays, what moves

CLAUDE.md is read on every turn by every agent. Today 60 of its 210 lines are
architecture decisions that only a backend implementer acts on, and 50 are the
drop discipline that only the main session acts on. Proposed split:

| Section today | Lines | Goes to |
|---|---|---|
| Project State and the document table | 35 | Stays, cut to 15: state in two sentences; the table keeps one line per document |
| Working practice (drops, the operator-note table, commits, measure, restart, guards) | 50 | `PROCESS.md` "How a drop ships"; CLAUDE.md keeps five one-line rules (measure, do not estimate; restart after a Python change; widen guards, never weaken; one package, one branch; nothing user-visible without a What's new line) |
| Hard Constraints 1 to 5 | 35 | **Stays whole.** They bind every file every agent writes, and the compat test is the backstop, not the teacher |
| Commands | 25 | Stays, cut to the six commands an agent runs; the rest go into `/verify` and `/drop` |
| Architecture (dashboard) | 60 | `docs/ARCHITECTURE.md`, text unchanged; `backend-engineer.md` reads it on onboarding; CLAUDE.md keeps a five-line map of the package |
| Feeder | 5 | `docs/FEEDER_TEMPLATE.md` already covers it; one pointer line |
| *new* Who implements what | +20 | The table in §3.1 with the path check |

Result: about 110 lines, of which the delegation table and the constraints are
the two things that must be in front of every reader.

### 3.4 Skills

Skills over commands because a skill can carry a script beside its prose and
the main session can invoke one on its own judgement. Each is short; the rules
it enforces live in `PROCESS.md`.

- **`/brief <task>`**: fills `brief-template.md`, assigns the agent by the path
  table, assigns a worktree base and a database cnf (§3.6), creates the ledger,
  shows the brief, waits.
- **`/verify`**: `python -m unittest discover` on SQLite, then the MariaDB leg
  with `TESTBOARD_TEST_DB_CNF` set to the cnf the brief assigned; prints the
  two counts and the failures only. Doc-only diffs skip it. The Python 3.6 gate
  is already inside the suite.
- **`/review`**: spawns a Sonnet reviewer with the brief's acceptance list and
  `git diff <base>..HEAD`; its first question is "does this deliver the
  acceptance list", the mechanical sweep is second. For UI diffs the main
  session then does the judgement walkthrough itself, against a play server,
  with the ui-engineer's own design bar as the checklist. That walkthrough is
  what caught the polish gap; it is not optional and it is not delegated.
- **`/perf-ab <base> <head>`**: the in-process alternated cold comparison from
  `.scratch/net/drop-2026-09-30/wp40_ab.py`, promoted into the skill with its
  seeded-database recipe. Every package touching `storage.py` or `api.py` runs
  it before review. "Measure, do not estimate" becomes a command.
- **`/drop`**: runs the drop ritual in order: operator note from the template
  (migration yes or no first), What's new section, handover rewrite, PR body;
  every number in them comes from a command the skill runs in the main
  checkout. The squash-merge text is the PR body.
- **`/handover`**: rewrites `SESSION_HANDOVER.md`; refuses to append.
- **`/overnight`**: D8.

### 3.5 Memory, three tiers

| Tier | Where | Reader | Holds |
|---|---|---|---|
| Agent memory | `.claude/agent-memory/<agent>/`, in the tree but gitignored (public repo) | that agent, on spawn | What was learned since the definition was written; never state. The durable onboarding (traps, design bar, module maps) lives in the agent definition and `docs/`, which survive a reinstall |
| Main-session memory | `~/.claude/projects/.../memory/` (exists today, 10 notes) | Fable | Working-practice facts: the sacrificial dbs, the commit-quoting trap, the user's standing instructions |
| Repo documents | `docs/` | everyone | State (handover), record (log), contracts (runbook, feeder template) |

Rules borrowed from the sibling project's drift: an index bullet is one line and under 160
characters; a superseded note is deleted, not marked; a note that describes
state is wrong by construction. Seed each agent's memory from last night: the
backend agent gets the cold-cost rule, the memo-tagging rule, the ledger
procedure, the two-dbs recipe and the Windows `sqlite3` path trap; the UI agent
gets progressive loading, the DOM-shim net, the "no browser has ever rendered
this" fact, helpers over string hacks (`failingWithMuted` is the example), and
the three polish findings as a checklist.

### 3.6 Worktrees and databases

Every implementer runs in its own worktree, always, even when alone. The stash
incident and the base-commit incident both came from sharing a checkout. The
brief carries a **Worktree** heading: the base commit it was cut from (the
agent verifies with `git rev-parse HEAD` before touching anything), and which
of three sacrificial MariaDB databases it may drop. Three cnf files, three
databases, and a fourth reserved for the main session's `/verify`. The main
session merges; agents never merge each other.

### 3.7 The life of a request

```
question        -> answered from the code, no agent
change, small   -> /brief -> spawn (worktree) -> report -> /verify -> /review -> commit -> log entry
change, large   -> design doc in docs/design/ -> review rounds -> sizing -> /overnight, or a run of the small path
drop            -> /drop -> PR -> green -> squash -> deploy -> /handover with the deploy answers
```

One item is one brief plus one ledger, one fresh agent, one commit. A new
request is a fresh spawn; SendMessage continues only judgement that lives in an
agent's context (a review finding going back to the implementer).

### 3.8 Design review and the overnight build

Both of the sibling project's big commands come over; the first draft hid one and
omitted the other. `/autonomous-build` is D8 under the name `/overnight`.
`/design-review` was missing, and this document is a round 0 of exactly that
flow, so the gap is live now.

`/design-review`, adapted:

- A round starts by capturing the owner's comments verbatim into the repo copy of
  the doc before anything is rewritten, and classifying each as a decision, a
  question or a style instruction. That rule is the one most likely to save a round.
- The owning agent dispositions the comments under the brief template: a
  report of at most 25 lines led by a comment-to-disposition map. Cross-review
  between backend-engineer and ui-engineer only when the doc crosses the API
  contract.
- Two eyes before commit: the main session plus a Sonnet fresh-eyes pass whose
  first job is the big picture.
- The record of the round is an entry in the status log (D11), not a
  `.claude/reviews/` tree. The rendered copy is a Claude Doc; the verdicts and
  comments must land back in `docs/design/<slug>-r<N>.md`, because the
  committed file is the record and the doc is the viewer.
- Round 0 shape stays: a §0 strip, decisions as positions with a verdict each,
  numbered open questions, the 20-minute reading bar. UI docs ship wireframes
  as text mock-ups; server docs ship example payloads and rows.
- When the arc closes, a sizing pass writes the master plan, the specs freeze,
  and the build is registered as a line in the handover.

`/overnight`, the trimmed `/autonomous-build`:

- Orient from files only: `git status`, the ledgers in `.claude/work/`, the
  plan's §0, the agent list, the cron list.
- Kickoff: verify green on a clean tree, one confirmation question, then arm
  the hourly cron heartbeat before the first spawn. A heartbeat armed last once cost a
  whole run.
- The stage loop per item: brief, ledger, fresh agent in a worktree, report,
  `/verify`, `/review`, one commit, status-log line, ledger deleted, report to
  the owner in at most 10 lines, next item.
- Parking, not stopping: a blocked item is parked with its reason in the
  ledger and the loop moves on.
- Done when the plan's items are all committed or parked; the morning report
  lists the parked ones first.

### 3.9 The publication gate

The repo is public, so everything that reaches origin is published: commit
messages, PR bodies, the handover, the status log, drop notes, What's new,
design docs, agent definitions. The D4 concern applies to all of them, and
today nothing checks. Added 2026-10-03 at the owner's request.

- **What is screened:** every tracked text file in a commit's diff plus the
  commit message and the PR body. The handover, the status log and the drop
  notes are the usual carriers because they are where measurements, incidents
  and names land.
- **What is caught:** people's names and handles; employer, team, product and
  site names; hostnames, IPs and ports of real machines, internal URLs, ticket
  ids; and the judgement class, anything that reads as a lesson from internal
  use rather than a fact about this code. Estate-scale numbers already in
  CLAUDE.md stay as they are.
- **Two layers.** Mechanical: a term list that is itself never committed
  (gitignored under `.claude/private/`), scanned by a small stdlib script in
  `tools/dev/` that git's `commit-msg` and `pre-commit` hooks run locally; a
  hit blocks the commit. Judgement: `/review` and `/drop` end with a Sonnet
  pass over the diff of tracked docs and the message, asking only "would this
  read as internal to an outsider", reporting hits, never editing.
- **Where it runs:** the commit step of the stage loop, `/drop` before the PR
  is opened, `/handover` before it writes.
- **What it does not do:** rewrite history. Removing something already
  published is the owner's decision, taken in chat, not a skill.

Decided in the same breath: the sibling project is the owner's private repo
and is never named in tracked text; its name is on the gate's term list and
every published copy says "a sibling project".

### 3.10 A performance engineer

Added 2026-10-03 at the owner's suggestion, with the pushbacks agreed. The
evidence: the one real regression in the WP-40 build was caught by an A/B
agent working in its own context, not by the implementer and not by review
by diff. An implementer measuring its own design has the blind spot a
reviewer exists to cover; "never self-certifies" applies to performance too.

`performance-engineer`, Opus, with memory, three jobs, no product code:

- **Design pass**, before implementation: reads the brief or design doc and
  answers in a few lines what the change will cost, where, and whether a
  cheaper shape exists.
- **Measured pass**, after implementation and before fresh-eyes review: the
  cold, in-process, alternated A/B of the implementer's tree against the
  base on the production-scale seed, reported against the Known slow table.
  The implementer's own numbers are fast feedback; these are the ones that
  count. A regression is a finding sent back to the implementer.
- **Owns the instrument**: `tools/dev/perf/` (the A/B runner, the seeders,
  the recipe) and the Known slow baseline in the handover. `/perf-ab` is its
  command.

The performance pass runs only when a change touches `testboard/storage.py`,
`testboard/api.py`, the push path, or anything on a page's first paint, or
when the main session calls it on judgement. A change whose brief answers
Performance impact with "none, and here is why" and touches none of those
paths goes straight to fresh-eyes review: **light changes with obviously no
performance impact are never held up by process.**

The implementers keep their design-time duty (§3.9's Performance impact
heading); the performance engineer is the test, not the designer. It is a
reviewer with an instrument, not a third implementer; D3 stands. Built in
round 2 with `/perf-ab` and `tools/dev/`.

## §4 Decisions, as positions for the owner

Each is written as the position I recommend; mark it `agree` or overwrite it.

- **D1. The main session does not implement, with no size exception.** Last
  night Fable wrote the storage core because it was "the hard part"; that is
  exactly the exception that never holds. [owner: agree, 2026-10-03]
- **D2. Implementers are Opus; Sonnet reviews, measures and drafts.** [owner: agree, 2026-10-03, chosen over Sonnet-with-two-review-passes and Opus-for-UI-only] This
  reverses the owner's standing instruction of 2026-10-01 to delegate
  implementation to the cheaper tier. The instruction's goal was to stop burning Fable
  context, and D1 achieves that whichever model implements. What Sonnet
  implementers cost was the walkthrough that found their polish gaps, which is
  Fable time, the expensive kind. the saving is in who implements, not in which tier does. [owner: agree, 2026-10-03]
- **D3. Two territories, backend and UI**, not three. Reasoning in §3.1. [owner: agree, 2026-10-03]
- **D4. Agent memory lives in the tree at `.claude/agent-memory/` but is
  gitignored, never committed.** The repo is public on GitHub; lessons from
  internal use must not be broadcast. In the tree it survives branch switches
  and worktrees; a reinstall loses it, so the durable onboarding goes in the
  agent definitions and `docs/`. Revised 2026-10-03 from "committed" after
  the owner's Discuss verdict. [owner: agreed as revised]
- **D5. The architecture bullets move to `docs/ARCHITECTURE.md` verbatim;
  the hard constraints stay in CLAUDE.md.** The bullets were bought with
  incidents and are not being shortened, only relocated to where the agent that
  needs them reads them on spawn. [owner: agree, 2026-10-03]
- **D6. Skills with frontmatter and bundled scripts, not bare command files.**
  The perf A/B and the verify gate are scripts, not prose. [owner: agree, 2026-10-03]
- **D7. `PROCESS.md` at the root with a decisions log, updated in the same
  commit as any change to a rule, agent or skill.** Seed its log with the seven
  rows of §2, dated 2026-10-01. [owner: agree, 2026-10-03]
- **D8. `/autonomous-build` comes over as a trimmed `/overnight`, adopted after two supervised runs**: as a
  trimmed `/overnight`: one frozen spec, the stage loop of §3.7 per item, a
  cron heartbeat armed before the first spawn, parking not stopping, one
  question at the gate. No build register inside the skill: a registered build
  is a line in the handover. [owner: agree, 2026-10-03]
- **D9. Worktree always, database assigned in the brief, base commit verified
  by the agent.** [owner: agree, 2026-10-03]
- **D10. No hooks in round 0.** a hard block would take away the tools the main
  session needs to investigate; an instruction with no exceptions holds. Revisit if D1 is breached twice. [owner: agree, 2026-10-03]
- **D11. The status log stays the single append-only record; design-review
  records go in it, not in a `.claude/reviews/` tree.** The log is already
  "what was decided and why"; a second record would split it. Its 4,300 lines
  are read by grep and range, never whole, and `PROCESS.md` says so. [owner: agree, 2026-10-03]

- **D12. `/design-review` comes over as a skill, adapted** (§3.8): comments
  captured verbatim first, dispositions by the owning agent, two eyes before
  commit, the record in the status log, the committed `docs/design` file as
  the source and the Claude Doc as the viewer. Missing from the first draft;
  this document is a round 0 of that flow, so the gap is live now. [owner: agree, 2026-10-03]

- **D13. A publication gate screens every commit** (§3.9): message, PR body
  and the diff of tracked docs, mechanically against a never-committed term
  list via local git hooks, then by a Sonnet judgement pass inside `/review`,
  `/drop` and `/handover`. the owner's request of 2026-10-03: the repo is public and
  the handover, log, drop notes and commit messages are where internal lessons
  would leak. Nothing checks today. [owner: agree; it was the owner's own
  request, 2026-10-03]

- **D14. A dedicated `performance-engineer`** (§3.10): design pass on
  trigger, measured A/B pass before fresh-eyes review, owner of the perf
  harness and the Known slow baseline; never edits product code; never
  runs on a light change whose brief says "none, and here is why" off the
  trigger paths. The owner's proposal of 2026-10-03, with the pushbacks
  agreed. [owner: agree, 2026-10-03]

## §5 Open questions — answered 2026-10-03

1. **Moot: implementers are Opus (D2).** Original question: If Sonnet implementers stay, the
   ui-engineer's design bar and the mandatory Fable walkthrough carry more
   weight, and `/review` should run twice (Sonnet mechanical, then a second
   Sonnet against the design bar) before Fable looks.
2. **No.** backend-engineer owns `clients/` with a memory note. Original question: `clients/feeder.py`, `feeder_micro.py`
   and the Tcl engine are a frozen contract read by another repo's reviewer
   ("blast radius first"). I put them under backend-engineer with a memory
   note; a third agent would exist for three files that change twice a quarter.
3. **The net, the seeders and the A/B runner move under `tools/dev/` with a smoke test each;** databases and perf logs stay local. Original question: The six-class DOM-shim net, the
   seeders and the A/B script are the candidates. They are stdlib Python
   already; the question is whether they are worth maintaining under test in
   `tools/dev/`, or stay machine-local with absolute paths in the briefs.
4. **The handover carries a Registered builds table.** Original question: should the handover carry the registered-build table when `/overnight`
   exists, or should that be a `docs/design/<slug>.md` §0 strip as the sibling project does?
5. **`/drop` drafts each release section and the main session owns it; ui-engineer never adds one.** Original question: It is user-facing prose inside the UI
   territory. I gave the section drafting to `/drop` (main session) and the file
   to ui-engineer; the alternative is that ui-engineer writes the section as
   part of each UI item and `/drop` only checks the date attribute.
6. **No committed settings file; auto mode and per-session prompts stay.** Original question: Nothing is in `.claude/settings.json` today; everything is
   prompted or in auto mode. A committed allow-list for unittest, python,
   read-only `git` and `gh pr view` would let agents verify without prompts.
   Anything that writes (`git commit`, `gh pr merge`) stays with the main
   session.

## §6 Choices made differently from the sibling project

- Its `MEMORY.md` indexes as changelogs. Index lines here are pointers.
- A build register inside a skill. State goes in the handover.
- Two parallel memory systems per agent (`memory: project` injection plus the
  folder). Here `memory: project` is the folder; the main session's own memory
  is the user-level store it already uses.
- A `restart` command: there is no long-running dev server here; the play
  server is started per walkthrough with a copied database.
- Screenshot harnesses: there is no browser. The DOM-shim net is the
  equivalent, and the walkthrough is a text one against a play server.

## §7 Rollout: built as one set, not staged

The three-round rollout this section first described was dropped on
2026-10-03 at the owner's direction: "we're missing things by trying to
artificially stage this." Everything §3 and §4 decide is built together in
one PR: `PROCESS.md`, the `CLAUDE.md` split with `docs/ARCHITECTURE.md`, the
three agent definitions (`backend-engineer`, `ui-engineer`,
`performance-engineer`), the brief and ledger templates, the eight skills
(`/design-review`, `/brief`, `/verify`, `/perf-ab`, `/review`, `/drop`,
`/handover`, `/overnight`), the publication gate's scanner and hooks, and
`tools/dev/` with the promoted net and the performance harness. The one
piece of staging that survives is inside `/overnight` as its own readiness
gate: it refuses to run unattended until two packages have shipped through
the supervised path, or the owner says go. The whole set is reviewed against
§4's decisions and the owner's three principles (who the board is for;
responsiveness and staying lean; the public repository) before the owner
reads the PR.
