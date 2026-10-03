# Work ledgers

One file per in-flight task, created by the main session from `TEMPLATE.md`
with the brief, kept current by the agent at milestones, deleted by the main
session at commit. A ledger is the restart point when a context dies or an
agent is killed: a fresh agent reads it and resumes at the first unticked
task. Ledgers are gitignored (only this file and the template are tracked);
a ledger that outlives its commit is a symptom (`PROCESS.md` §8).
