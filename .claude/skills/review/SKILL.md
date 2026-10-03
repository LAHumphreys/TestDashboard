---
name: review
description: The review before a commit. Spawns a Sonnet fresh-eyes reviewer with the brief's acceptance list and the diff, runs the main session's own judgement walkthrough for UI changes, and ends with the publication-gate pass over everything tracked that the commit would publish. The implementer never self-certifies.
---

# /review <ledger slug>

Runs after `/verify` is green, before the commit. Three passes, in order.

## 1. Fresh eyes (Sonnet, never the implementer)

Spawn a `general-purpose` agent on Sonnet with: the brief's **Acceptance**
list, the **Out of scope** list, and `git diff <base>..<head>` of the
worktree. Its instructions, verbatim:

> First question, before anything mechanical: does this diff deliver every
> acceptance line, and nothing outside scope? Answer line by line. Then the
> sweep: Python 3.6 and stdlib only; annotations; a count query carrying a
> join its WHERE does not read; a guard test weakened rather than widened; a
> memo computed from more than one stream; user strings via `innerHTML`; a
> constant-derived window phrase; dead CSS or a helper with one caller;
> anything that holds up the first paint. Report findings only, each with
> file and line, at most 25 lines. Do not edit.

Findings go back to the implementer by `SendMessage`; the reviewer's report
is not forwarded whole.

## 2. The walkthrough (main session, UI diffs only)

Start a play server on a copy of `testboard.db` and read every surface the
diff touches, with the ui-engineer's bar from `.claude/agents/ui-engineer.md`
as the checklist: first paint unblocked, wording from the data, helpers over
string hacks, verbs without ellipses, nothing dead, consistent with its
neighbours. This pass found the three polish defects of the first
agent-driven drop; it is not optional and it is not delegated.

## 3. The publication gate (every commit)

The repository is public. Over the diff of tracked text files and the draft
commit message, one question: would any of this read as internal to an
outsider? A person's name or handle; an employer, team, product or site name;
a real host, address, port or internal URL; a ticket id; a lesson from
internal use rather than a fact about this code; the sibling project named.
Until round 2's scanner exists this pass is a Sonnet agent given the diff and
the message and that paragraph, reporting hits only. A hit blocks the commit
until the text is changed; the gate never rewrites history.
