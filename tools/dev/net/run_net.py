#!/usr/bin/env python3
"""The sanity net's one entry point.

    python tools/dev/net/run_net.py [--base-db PATH] [--db existing.db] [--keep]
    python tools/dev/net/run_net.py --url-prefix testboard

Copies the generated dev estate (default: the repo-root testboard.db) to
a temp directory -- the original is never opened -- boots THIS
checkout's server on port 8931 against the copy, seeds the
demonstration estate, runs every API check (classes 3/4/5/6 plus two
extra findings) and every DOM-shim walk (classes 1/2), tears the server
down, and prints one PASS/FAIL summary line per check with one-line
evidence for every failure. See README.md beside this file.

The DOM-shim walks need ``node`` on PATH (optional dev tooling, no npm
packages). Without it the API checks still run and the walks are
reported as SKIPPED, not failed.

--url-prefix PREFIX (WP-28, default "", i.e. bare paths): loads every
walked page via BASE + "/" + PREFIX + "/pagename.html" instead of
BASE + "/pagename.html", and resolves every relative href/fetch the
walk harvests against that SAME prefixed base -- so a link that renders
relative but resolves OUTSIDE the prefix (the one failure class grep
cannot see: source review shows a relative href, but only resolving it
against the actual page URL shows where it really lands) is caught the
same way a real browser behind a reverse proxy would hit it. The server
needs no separate boot for this: WP-28's "accept both shapes, always"
rule means ONE server answers both a bare run and a --url-prefix run
identically -- run this script twice, once with the flag and once
without, for the two required passes.

Nothing here writes into the tracked tree: the DB copy and the server
log live in a temp directory, removed on exit unless --keep.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NET_DIR = Path(__file__).resolve().parent
REPO_ROOT = NET_DIR.parent.parent.parent
DEFAULT_BASE_DB = REPO_ROOT / "testboard.db"

sys.path.insert(0, str(NET_DIR))
import server_util  # noqa: E402
import api_checks  # noqa: E402

WALK_SCRIPTS = [
    "walk_nav_scope.mjs",
    "walk_index_scope.mjs",
    "walk_picker_transitions.mjs",
]


def seed(db_path: Path) -> None:
    seeds_dir = NET_DIR / "seeds"
    scripts = [
        seeds_dir / "seed_wp23_v3_net.py",
        seeds_dir / "seed_corvus_net.py",
    ]
    for script in scripts:
        print("-- seeding: %s --" % script.name)
        result = subprocess.call(
            [sys.executable, str(script)], cwd=str(NET_DIR),
        )
        if result != 0:
            raise RuntimeError("seed script failed: %s" % script)

    addendum = seeds_dir / "addendum3_net.py"
    print("-- seeding: %s (direct Storage write, server stays up) --"
          % addendum.name)
    result = subprocess.call(
        [sys.executable, str(addendum), str(db_path)], cwd=str(NET_DIR),
    )
    if result != 0:
        raise RuntimeError("seed script failed: %s" % addendum)


def find_node() -> Optional[str]:
    """The node executable, or None: node is optional dev tooling."""
    return shutil.which("node")


def run_node_walk(node: str, script_name: str) -> Dict[str, Any]:
    path = NET_DIR / script_name
    start = time.time()
    proc = subprocess.Popen(
        [node, str(path)], cwd=str(NET_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    elapsed = time.time() - start
    fail_lines = [
        line.strip() for line in stdout.splitlines()
        if line.strip().startswith("FAIL")
    ]
    ok = proc.returncode == 0
    return {
        "name": script_name,
        "ok": ok,
        "elapsed": elapsed,
        "fail_lines": fail_lines,
        "stdout": stdout,
        "stderr": stderr,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-db", default=str(DEFAULT_BASE_DB),
                        help="the generated dev estate to copy (default: "
                             "the repo-root testboard.db; it is copied, "
                             "never opened)")
    parser.add_argument("--db", default=None,
                        help="reuse an existing already-seeded scratch DB "
                             "instead of creating+seeding a fresh one")
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the scratch DB and log on exit")
    parser.add_argument("--url-prefix", default="",
                        help="WP-28: walk pages via BASE/PREFIX/page.html "
                             "instead of BASE/page.html, and resolve "
                             "harvested hrefs/fetches against that same "
                             "prefixed base. Default '' is bare paths; "
                             "pass e.g. 'testboard' for the second "
                             "required run. The server itself needs no "
                             "matching flag -- its own default accepts "
                             "both shapes always")
    args = parser.parse_args(argv)
    if args.url_prefix:
        os.environ["NET_URL_PREFIX"] = args.url_prefix.strip("/")
        print("URL prefix for this walk: /%s (server still answers bare "
              "paths too -- only the WALK's own page URLs change)"
              % os.environ["NET_URL_PREFIX"])
    else:
        os.environ.pop("NET_URL_PREFIX", None)
        print("URL prefix for this walk: none (bare paths)")

    node = find_node()
    if node is None:
        print("SKIP: node is not on PATH, so the DOM-shim walks will not "
              "run; the API checks still do. node is optional dev "
              "tooling (no npm packages) -- see tools/dev/net/README.md.")

    overall_start = time.time()
    work_dir = Path(tempfile.mkdtemp(prefix="testboard-net-"))
    db_path = work_dir / "net_run.db"
    reused = False
    if args.db:
        db_path = Path(args.db)
        reused = True
    else:
        base_db = Path(args.base_db)
        if not base_db.exists():
            print("FATAL: base DB not found at %s -- generate the dev "
                  "estate (tools/generate_demo_data.py) or pass "
                  "--base-db. It is copied, never opened." % base_db)
            shutil.rmtree(str(work_dir), True)
            return 2
        shutil.copyfile(str(base_db), str(db_path))

    log_path = work_dir / "net_server.log"
    server = server_util.NetServer(str(db_path), str(log_path))
    print("Booting %s's server on :8931 against %s ..." % (REPO_ROOT, db_path))
    server.start()
    print("Server up. Log: %s" % log_path)

    all_failures = []  # type: List[Dict[str, str]]
    node_results = []  # type: List[Dict[str, Any]]
    try:
        if not reused:
            seed_start = time.time()
            seed(db_path)
            print("Seeding took %.1fs" % (time.time() - seed_start))
        else:
            print("Reusing existing DB, skipping seed step.")

        api_check_fns = [
            ("class5_seed_coverage", api_checks.class5_seed_coverage),
            ("class6_contract_honesty", api_checks.class6_contract_honesty),
            ("class3_unscoped_catalogs", api_checks.class3_unscoped_catalogs),
            ("class4_dishonest_empties", api_checks.class4_dishonest_empties),
            ("extra_leftover_kind_checks",
             api_checks.extra_leftover_kind_checks),
        ]  # type: List[Tuple[str, Callable[[], List[Dict[str, str]]]]]
        for name, fn in api_check_fns:
            print("-- running: %s --" % name)
            start = time.time()
            failures = fn()
            elapsed = time.time() - start
            print("   %s: %d failure(s) in %.1fs"
                  % (name, len(failures), elapsed))
            for f in failures:
                all_failures.append(f)

        for script in WALK_SCRIPTS:
            if node is None:
                print("-- skipped: %s (no node) --" % script)
                continue
            print("-- running: %s --" % script)
            result = run_node_walk(node, script)
            node_results.append(result)
            print("   %s: %s in %.1fs (%d FAIL line(s))"
                  % (script, "OK" if result["ok"] else "FAILED",
                     result["elapsed"], len(result["fail_lines"])))
            if not result["ok"] and not result["fail_lines"]:
                # crashed before printing any FAIL lines -- surface stderr
                print("   (no FAIL lines parsed; stderr follows)")
                print("   " + result["stderr"].replace("\n", "\n   "))
    finally:
        server.stop()
        if not args.keep:
            shutil.rmtree(str(work_dir), True)
        else:
            print("Kept: %s" % work_dir)

    total_elapsed = time.time() - overall_start

    print("\n" + "=" * 70)
    print("SANITY NET SUMMARY")
    print("=" * 70)
    total_fail = len(all_failures) + sum(
        len(r["fail_lines"]) for r in node_results)
    total_walk_crashes = sum(
        1 for r in node_results if not r["ok"] and not r["fail_lines"])

    if total_fail == 0 and total_walk_crashes == 0:
        if node is None:
            print("PASS -- every API check passed; %d DOM-shim walk(s) "
                  "SKIPPED (node not on PATH)." % len(WALK_SCRIPTS))
        else:
            print("PASS -- every check passed.")
    else:
        print("FAIL -- %d failing check(s):\n" % total_fail)
        for f in all_failures:
            print("  [%s] %s" % (f["check"], f["detail"]))
        for r in node_results:
            for line in r["fail_lines"]:
                print("  [%s] %s" % (r["name"], line))
            if not r["ok"] and not r["fail_lines"]:
                print("  [%s] node process crashed (exit %s) -- see stderr "
                      "above" % (r["name"], "non-zero"))

    print("\nTotal runtime: %.1fs" % total_elapsed)
    return 0 if (total_fail == 0 and total_walk_crashes == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
