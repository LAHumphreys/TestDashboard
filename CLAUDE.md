# CLAUDE.md

Guidance for Claude Code in this repository. The process this file points at is
`PROCESS.md`: read that once, this every turn. **This repository is public.**
Nothing tracked may carry a person's name, a real host, or anything that reads
as a lesson from internal use; `PROCESS.md` §6 is the gate.

## Project state

testboard is live in production (since 2026-07-26) and in daily use: ~79k
lines, 2,640 tests (3,626 with the MariaDB variants active), schema at
migration 11. **Production serves MariaDB** and is the only deployment since
2026-10-01. SQLite and MariaDB are equal, permanently supported backends.

**Who the board is for.** Its users are developers (and their managers)
maintaining products, reviewing the nightly runs of their automated tests on
mainline and on their release builds. The board exists so that keeping the
tests in good shape costs them as little of their day as possible. When
everything is green a glance is enough. When something breaks they triage it:
review, comment, assign. When triage is not enough the depth is there:
captured output, a test's run history with the output of each run, a build's
history against mainline's, and the comparison tool for what a branch has
gained or is missing. It is a tool with a purpose, reached for when needed; it
is not the main driver of a developer's day, and no design decision may assume
it is.

**Start every session by reading `docs/SESSION_HANDOVER.md`**: one screen of
state, rewritten rather than appended. Then, as needed:

| Document | What it is |
|---|---|
| `docs/ARCHITECTURE.md` | The design decisions, most bought with incidents. `backend-engineer` reads it on spawn |
| `docs/UPGRADE_PLAN.md` | Open work orders and the migration version registry (§1): claim a version there, in the same commit, before writing one |
| `docs/UPGRADE_PLAN_STATUS.md` | Append-only log of what was done, measured and decided. **Read by grep and range, never whole** |
| `docs/drops/YYYY-MM-DD.md` | Operator note for one drop, written before it ships (`PROCESS.md` §5) |
| `docs/MARIADB_MIGRATION.md` | Runbook for MariaDB: the schema, the upgrade-tool ledger (§G), the feeders (§G.3) |
| `docs/STREAMS_PLAN.md` | Products/streams decisions (§0) and cross-cutting rules (§6) |
| `docs/FEEDER_TEMPLATE.md` | Frozen contract for a new product's feeder: additive changes only |
| `docs/design/` | Design docs under review |
| `static/whatsnew.html` | What the users see. Every user-visible change has a line there; nothing is there that is not in the build |
| `docs/BRIEF_*.md`, `docs/FEEDER_BRIEF.md` | Historical briefs. `FEEDER_BRIEF.md` is still the accurate reference for the one product on `run_feeder.py` |

## Who implements what — check the path before touching any file

**The main session does not edit code. No size exception.**

| Path | Owner |
|---|---|
| `testboard/`, `tools/`, `clients/`, `feeder/`, `run_server.py`, `run_feeder.py`, `tests/` except `test_frontend_calls.py`, `docs/MARIADB_MIGRATION.md`, `docs/FEEDER_TEMPLATE.md` | `backend-engineer` (Opus) |
| `static/`, `tests/test_frontend_calls.py` | `ui-engineer` (Opus) |
| `CLAUDE.md`, `PROCESS.md`, `.claude/`, the `docs/` state files (handover, log, drops, design), git, PRs, drops | main session |

A change that spans both layers is two briefs against an agreed JSON contract.
`Explore` and `general-purpose` agents research, measure and draft; they never
edit repo code. Every hand-off is a brief from `.claude/brief-template.md`
with a ledger in `.claude/work/`, run in its own worktree with its own
sacrificial database, reported in at most 25 lines. The rituals are skills:
`/brief`, `/verify`, `/review`, `/handover`.

## Hard constraints (apply to all code)

1. **Python 3.6 exactly.** No 3.7+ features. Specifically forbidden: `dataclasses` (use `typing.NamedTuple` class syntax), `http.server.ThreadingHTTPServer` (hand-compose one on `http.server.HTTPServer`; it must be a fixed worker POOL, not `socketserver.ThreadingMixIn`), `datetime.fromisoformat` (one `strptime`-based ISO-8601 parser, unit-tested, used everywhere), `subprocess.run(capture_output=...)`, `typing.Protocol`/`Literal`, walrus operator, positional-only params, `breakpoint()`, `contextlib.nullcontext`. f-strings and `async`/`await` are fine.
2. **Standard library only.** No pip installs, no build step, nothing to set up on the server; that property is the point, not the letter of the rule. `unittest` (not pytest), `sqlite3`, `http.server`, `json`, `urllib.request`.
   **One narrow exemption:** pure-Python third-party source may be *vendored* under `third_party/`, where the stdlib has no equivalent and the package has no dependencies of its own. Vendored code is *present*, not *installed*. It is exempt from this project's style rules but **not** from the 3.6 parse gate; `tests/test_python36_compat.py::VendoredCodeTest` enforces the split. Currently: PyMySQL 1.0.2, used by `tools/migrate_to_mariadb.py` and the MariaDB backend (`testboard/mariadb.py`); `tests/test_vendored_driver.py::DriverImportAllowlistTest` holds the driver's serving-path blast radius to that one module. Do not add anything else without the same justification.
   **Never shell out to a tool that would have to be installed.** An RPM, a system binary or a CLI on `PATH` is a dependency in exactly the same way as a pip package and fails the same way, at deployment. The MariaDB work talks to the server through `third_party/pymysql`, **never** through the `mysql` client via `subprocess`. If a guard test appears to forbid using a vendored package for a legitimate purpose, amend the guard with its reasoning updated; do not route around it by adding a host dependency. `subprocess` is for things that are unambiguously part of the interpreter's own environment (`sys.executable`).
3. **No npm, no build step, no CDN.** Frontend is static HTML + vanilla ES6 JS + CSS served by the same process; assume browsers have no internet access.
4. **Full type annotations** on every function/method (3.6-compatible). No `Any` except at JSON boundaries, converted to typed structures immediately. Use `typing.List`/`Dict`/`Optional`; **never** PEP 585 builtin generics (`list[str]`) or PEP 604 unions (`int | None`), which are a runtime `TypeError` on 3.6 while looking fine on a modern interpreter.
5. **No global mutable state**; the storage object is injected into handlers.

`tests/test_python36_compat.py` enforces constraints 1 and 4 statically on every test run (`ast.parse(..., feature_version=(3, 6))`, builtin generics rejected anywhere, `typing` names whitelisted against 3.6.0, every annotation forced to evaluate, `run_server.py`/`run_feeder.py` kept Python 2-parseable) and carries planted regressions to prove the detectors can fail.

## Commands

- All tests: `python -m unittest discover`. One module: `python -m unittest tests.test_storage`. One test: `python -m unittest tests.test_storage.TestClass.test_method`.
- Server: `python run_server.py --port <p> --host <h>` with exactly one of `--db PATH` (SQLite, zero setup, never "legacy") or `--db-config CNF` (MariaDB; requires `--site-notes`). `--workers N` / `--cache-mb MB` tune the pool.
- Dual-backend suites: set `TESTBOARD_TEST_DB_CNF` to a mysql option file naming a **sacrificial** database (it is dropped and recreated); unset, the MariaDB variants do not exist. CI's `python36-mariadb` leg runs them against `mariadb:10.3`, production's stream.
- Catch a stall: `--perf-log PATH` then `python tools/perf_report.py PATH`. See what the server is doing: `metrics.html` (in-memory counters, on by default).
- Ops: `tools/drop_environment.py` (`--dry-run` first), `tools/add_site_note.py`, `tools/upgrade_mariadb_schema.py` (runbook §G).
- **Never against the repo-root `testboard.db`** for anything that migrates or writes: copy it to a temp directory first. Opening it with current code migrates it.

## Responsiveness is a top priority

Page load time and responsiveness everywhere in the app are valued above
features and are **tested for, not assumed**. Even as features are added the
board must stay a lean, purpose-built tool, never commercial bloatware with a
hundred plugins installed: every page does one job quickly, and a feature
that would make the whole feel heavier is the wrong feature even when it
works. Every change is designed with
its cost in mind, measured cold before review, and its effect reported with
numbers. A change that would slow a page, add a request to the critical path,
add a query or a join or a pass to an endpoint, or add work to the push path
is **flagged in the brief's Performance impact section and in the report**,
and designed to minimise that cost where it cannot be avoided. A regression
against the handover's "Known slow" table or a cache-guard test is a finding
that blocks the commit until it is explained and accepted by the owner.

## Five rules that have each cost a day

- **Measure, do not estimate.** Every performance or migration claim has a number behind it and says which database it was taken on. Measure cold, in-process and alternated; the repo-root `testboard.db` is generated dev data, roughly a quarter of production's size, and is never called "production".
- **Restart the server after any Python change.** Static files are read per request, so a stale process serves new HTML against old handlers, and it looks like a UI bug.
- **Guard tests encode production findings.** If your change makes one fail, widen its scope, never weaken its assertion, and say which you did in the commit message.
- **One package, one branch** `wp-<n>-<slug>`, one commit or a small ordered series. Commit messages carry the reasoning and the measurements; they are the primary record and the log summarises them.
- **Nothing user-visible ships without a What's new line**, and nothing appears there that is not in the build. Every release section carries `data-drop-date`; `tests/test_frontend_calls.py::DropDateTest` fails the build otherwise.

## Package map

`testboard/`: `model.py` (NamedTuples, `Result` enum, ISO time), `storage.py` (all SQL, qmark-canonical, sqlite migrations), `mariadb.py` (the ONE serving-path module that touches the vendored driver), `dbconfig.py`, `analytics.py` (pure functions), `api.py` (framework-free handlers returning `(status, headers, body)`), `server.py` (http.server glue, fixed worker pool, static files), `perf.py`, `metrics.py`. `static/`: vanilla JS, progressive loading. `tests/`: unittest, with guard tests and the 3.6 gate. `tools/`: migration, the upgrade-tool ledger, ops. `clients/`: feeders. The app never runs DDL on MariaDB. The reasoning behind every one of these is `docs/ARCHITECTURE.md`.
