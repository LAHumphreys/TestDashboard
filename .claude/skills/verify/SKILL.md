---
name: verify
description: The gate before review and before any commit. Runs the suite on SQLite, then the MariaDB leg against the main session's own sacrificial database, and reports two counts and the failures only. Skipped for a diff that touches no code. Run it in the main checkout; counts from a worktree are never written into a document.
---

# /verify

Run in the main checkout (or the worktree under review, labelled as such):

```
python -m unittest discover > <scratch>/sqlite.log 2>&1; echo "exit $?"
TESTBOARD_TEST_DB_CNF=<absolute path to the main session's sacrificial cnf> python -m unittest discover > <scratch>/mariadb.log 2>&1; echo "exit $?"
grep -E "^Ran |^OK|^FAILED" <scratch>/sqlite.log <scratch>/mariadb.log
```

- Capture to a file and read the exit code of `unittest` itself. Never pipe
  the run through `tail`: a test that prints to stdout at exit buries the
  summary, and the pipeline's exit code is `tail`'s, not the suite's. That
  happened on the process PR and cost a re-run.
- Report the two `Ran N tests` lines and `OK`/`FAILED`, then only the failing
  test names with their first assertion line. Never paste the full output.
- The expected SQLite count is in `docs/SESSION_HANDOVER.md` ("First ten
  minutes"). A count that moved is a finding: say by how many and why.
- Red goes back to the implementer by `SendMessage` with the failing names,
  not to the main session's editor.
- A diff that touches only `docs/`, `PROCESS.md`, `CLAUDE.md`, `.claude/` or
  `static/whatsnew.html` skips the run, except that any change to
  `whatsnew.html` runs `python -m unittest tests.test_frontend_calls`
  for `DropDateTest`.
- The local MariaDB and its option files are described in the handover's
  "Verification tooling". If the server is not up, say so and report the
  SQLite leg alone as partial; CI's two 10.3 legs remain the authority for
  production's stream.
