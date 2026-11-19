"""Load a SQLite fixture into a sacrificial MariaDB database, for a by-hand
EXPLAIN and timing. Development only.

    python tools/dev/perf/mariadb_load.py --config <sacrificial.cnf> \\
        --db <fixture.db> --recreate [--tree <checkout>]

The migration tool does the work (export, load, verify), run from *tree*
with this interpreter (``sys.executable``), never through a client
installed on the host. Around the load this script, through that tool's
own connection (the vendored driver, behind the tool):

- drops the database the option file names and creates it again as
  utf8mb4 / utf8mb4_nopad_bin, which the tool's collation gate requires;
  ``--recreate`` says you mean it, and nothing is touched without it;
- raises the server's global ``max_allowed_packet`` to 64 MB, below which
  the tool's preflight stops, and puts the old value back whatever
  happens;
- runs ``ANALYZE TABLE`` on ``runs`` and ``latest_runs`` so the plans are
  a loaded server's, then prints what the database holds.

Use the tree whose schema the fixture is at: the base checkout for a
fixture at migration 12, a checkout at WP-44 item 22 or later for one
that holds a flaky default (migration 13). The fixture is read, never
written. README.md has the recipe.
"""

import argparse
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import urllib.parse
from typing import Callable, List, NamedTuple, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

#: The tool's preflight stops below this (runbook §C.2).
PACKET_BYTES = 64 * 1024 * 1024

#: The environment the fixtures write, for the summary after the load.
ENVIRONMENT = "atlas-lab-alpha"


class LoadPlan(NamedTuple):
    """What a load will do, checked before anything is touched."""

    config: str
    db: str
    tree: str
    tool: str
    max_blob_bytes: int


def _is_checkout(folder: str) -> bool:
    return (os.path.isfile(os.path.join(folder, "run_server.py"))
            and os.path.isdir(os.path.join(folder, "testboard")))


def plan_load(config: str, db: str, tree: str, recreate: bool) -> LoadPlan:
    """Validate the arguments and measure the fixture's largest output;
    raise ValueError before any connection is made."""
    if not recreate:
        raise ValueError("this drops and recreates the database the option "
                         "file names: pass --recreate, and only for a "
                         "sacrificial database")
    full = os.path.abspath(db)
    if (os.path.basename(full) == "testboard.db"
            and _is_checkout(os.path.dirname(full))):
        raise ValueError("refusing the repo-root testboard.db ({}): load a "
                         "fixture built by seed.py".format(full))
    if not os.path.isfile(full):
        raise ValueError("no fixture at {}".format(full))
    if not os.path.isfile(config):
        raise ValueError("no option file at {}".format(config))
    root = os.path.abspath(tree)
    tool = os.path.join(root, "tools", "migrate_to_mariadb.py")
    if not _is_checkout(root) or not os.path.isfile(tool):
        raise ValueError("{} is not a checkout with tools/migrate_to_mariadb"
                         ".py".format(root))
    conn = sqlite3.connect("file:{}?mode=ro".format(urllib.parse.quote(
        full.replace("\\", "/"), safe="/:")), uri=True)
    try:
        largest = conn.execute(
            "SELECT MAX(LENGTH(output)) FROM run_outputs").fetchone()[0]
    finally:
        conn.close()
    return LoadPlan(config=os.path.abspath(config), db=full, tree=root,
                    tool=tool, max_blob_bytes=int(largest or 0))


def load_tool(plan: LoadPlan) -> types.ModuleType:
    """The tree's migration tool as a module: its ``connect`` is the one
    route to the server this script takes (the driver stays behind the
    tool, which is on the driver's allowlist)."""
    sys.path.insert(0, plan.tree)
    spec = importlib.util.spec_from_file_location(
        "dev_perf_migrate_to_mariadb", plan.tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


def load(plan: LoadPlan, work: str, say: Callable[[str], None]
         ) -> List[str]:
    """Export, recreate, load with the packet raised, verify, analyse.
    Returns the summary lines. *work* is the export directory (empty or
    absent)."""
    tool = load_tool(plan)
    settings = getattr(tool, "read_option_file")(plan.config)
    name = str(settings.database).replace("`", "``")
    # Connected to no database: the one named is about to be dropped.
    server = getattr(tool, "connect")(settings._replace(database=""))
    try:
        old_packet, version = server.rows(
            "SELECT @@global.max_allowed_packet, VERSION()")[0]
        say("server {} ({}); max_allowed_packet {}".format(
            settings.describe(), version, old_packet))
        subprocess.check_call([sys.executable, plan.tool, "export", "--db",
                               plan.db, "--out", work])
        say("dropping and recreating `{}`".format(settings.database))
        server.run("DROP DATABASE IF EXISTS `{}`".format(name))
        server.run("CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE "
                   "utf8mb4_nopad_bin".format(name))
        def load_and_verify() -> None:
            subprocess.check_call([
                sys.executable, plan.tool, "load", "--config", plan.config,
                "--max-blob-bytes", str(plan.max_blob_bytes),
                "--out", work])
            subprocess.check_call([
                sys.executable, plan.tool, "verify", "--config", plan.config,
                "--out", work])

        with_packet(server, int(old_packet), load_and_verify, say)
        server.run("USE `{}`".format(name))
        server.rows("ANALYZE TABLE runs, latest_runs")
        return summary(server.rows)
    finally:
        server.close()


def with_packet(server: object, old: int, work: Callable[[], None],
                say: Callable[[str], None]) -> None:
    """Run *work* with the server's global ``max_allowed_packet`` at
    :data:`PACKET_BYTES`, then put *old* back whatever happened.

    The restore runs in its own ``try``: if it fails, it is reported
    through *say* (set it by hand), and raised only when *work* itself
    succeeded, so a restore failure never replaces the failure of the
    load that is the reason anyone is reading the output."""
    run = getattr(server, "run")
    rows = getattr(server, "rows")
    run("SET GLOBAL max_allowed_packet = {:d}".format(PACKET_BYTES))
    failed = True
    try:
        work()
        failed = False
    finally:
        try:
            run("SET GLOBAL max_allowed_packet = {:d}".format(old))
            say("max_allowed_packet restored to {}".format(
                rows("SELECT @@global.max_allowed_packet")[0][0]))
        except (Exception, SystemExit) as exc:
            say("could NOT put max_allowed_packet back to {} ({}): set it "
                "by hand".format(old, exc))
            if not failed:
                raise


def summary(rows: Callable[[str], List[Tuple[object, ...]]]) -> List[str]:
    """What the loaded database holds, through *rows* (one statement, its
    rows): the lines to hold against the fixture's census (README.md)."""
    def one(sql: str) -> Tuple[object, ...]:
        return rows(sql)[0]

    def number(value: object) -> int:
        return int(str(value)) if value is not None else 0

    lines = ["schema {}".format(one(
        "SELECT MAX(version) FROM schema_version")[0])]
    lines.append("runs {:,}, outputs {:,}".format(
        number(one("SELECT COUNT(*) FROM runs")[0]),
        number(one("SELECT COUNT(*) FROM run_outputs")[0])))
    tests, failing, after = one(
        "SELECT COUNT(*), SUM(result = 'FAIL'), "
        "SUM(result = 'FAIL' AND prev_result = 'FAIL') FROM latest_runs "
        "WHERE stream_id = 1 AND environment = '{}'".format(ENVIRONMENT))
    lines.append("{} mainline: {} tests, latest {} FAIL ({} after a FAIL)"
                 .format(ENVIRONMENT, number(tests), number(failing),
                         number(after)))
    lines.append("assignments {}, mutes {}".format(
        number(one("SELECT COUNT(*) FROM current_assignments "
                   "WHERE assignee IS NOT NULL")[0]),
        number(one("SELECT COUNT(*) FROM test_mutes")[0])))
    if rows("SHOW TABLES LIKE 'environment_flaky_defaults'"):
        held = rows("SELECT environment, cadence FROM "
                    "environment_flaky_defaults ORDER BY environment")
        lines.append("flaky defaults: {}".format(", ".join(
            "{} {}".format(row[0], row[1]) for row in held) or "none"))
    else:
        lines.append("flaky defaults: no table (schema before 13)")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True, metavar="CNF",
                        help="option file naming a SACRIFICIAL database")
    parser.add_argument("--db", required=True,
                        help="the SQLite fixture (seed.py), read only")
    parser.add_argument("--tree", default=REPO_ROOT,
                        help="checkout whose migration tool, driver and "
                             "dbconfig are used (default: this one); at the "
                             "fixture's schema")
    parser.add_argument("--recreate", action="store_true",
                        help="drop and recreate the database the option "
                             "file names; required")
    parser.add_argument("--work", default="",
                        help="export directory to use and keep (default: a "
                             "temp directory, removed after)")
    args = parser.parse_args(argv)
    try:
        plan = plan_load(args.config, args.db, args.tree, args.recreate)
    except ValueError as exc:
        print("mariadb_load.py: {}".format(exc), file=sys.stderr)
        return 2
    work = args.work or os.path.join(
        tempfile.mkdtemp(prefix="testboard-mdb-"), "export")
    try:
        # Flushed, so each line lands in order with the tool's output.
        for line in load(plan, work,
                         lambda text: print(text, flush=True)):
            print(line, flush=True)
    finally:
        if not args.work:
            shutil.rmtree(os.path.dirname(work), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
