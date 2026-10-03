#!/usr/bin/env python3
"""Install the publication gate's git hooks (PROCESS.md section 6).

    python tools/dev/install_hooks.py            # this clone
    python tools/dev/install_hooks.py --repo DIR # another clone

Writes ``commit-msg`` (runs ``publication_gate.py --message``) and
``pre-commit`` (runs ``publication_gate.py --staged``) into the hooks
directory of ``git rev-parse --git-common-dir``, so every linked worktree
of the clone shares them. Run once per clone; running it again rewrites
the two hooks in place and changes nothing else.

Other hooks are left alone. A ``commit-msg`` or ``pre-commit`` that this
installer did not write is not overwritten: the installer says so and
exits 1, and the owner decides whether to merge the two by hand.

The hooks run the gate from the working tree being committed, so each
worktree is checked by its own copy of the gate. A branch that predates
the gate has no ``tools/dev/publication_gate.py``; there the hook prints
a warning and lets the commit through rather than blocking all work on
old branches.

Standard library only, Python 3.6; git is the only program it runs.
"""
import argparse
import os
import stat
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

#: Repository root of the checkout this file lives in (tools/dev/ -> root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Identifies a hook this installer wrote, so a rerun may replace it.
MARKER = "# testboard publication gate: written by tools/dev/install_hooks.py"

GATE = "tools/dev/publication_gate.py"

_TEMPLATE = """#!/bin/sh
{marker}
# Rerun the installer to update; edit it there, not here.
if [ ! -f {gate} ]; then
    echo "publication gate: {gate} not in this checkout; not checked" >&2
    exit 0
fi
{python_choice}
exec "$PY" {gate} {arguments}
"""

_DEFAULT_PYTHON_CHOICE = """if command -v python >/dev/null 2>&1; then
    PY=python
else
    PY=python3
fi"""


def hook_text(arguments: str, python: Optional[str] = None) -> str:
    """The body of one hook script; ``python`` pins an interpreter."""
    if python is None:
        choice = _DEFAULT_PYTHON_CHOICE
    else:
        choice = 'PY="%s"' % python.replace("\\", "/")
    return _TEMPLATE.format(marker=MARKER, gate=GATE, python_choice=choice,
                            arguments=arguments)


def hooks(python: Optional[str] = None) -> List[Tuple[str, str]]:
    """(hook name, script text) for every hook the gate installs."""
    return [
        ("commit-msg", hook_text('--message "$1"', python)),
        ("pre-commit", hook_text("--staged", python)),
    ]


def _git(args: Sequence[str], cwd: str) -> Tuple[int, str]:
    try:
        proc = subprocess.Popen(
            ["git"] + list(args), cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return 127, ""
    out, _err = proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace").strip()


def hooks_dir(repo: str) -> Optional[str]:
    """The shared hooks directory of the repository at ``repo``."""
    code, common = _git(["rev-parse", "--git-common-dir"], repo)
    if code != 0 or not common:
        return None
    return os.path.join(os.path.abspath(os.path.join(repo, common)), "hooks")


def install(repo: str, python: Optional[str] = None) -> int:
    """Write the hooks; return the process exit code."""
    directory = hooks_dir(repo)
    if directory is None:
        print("install_hooks: %s is not a git repository" % repo)
        return 2
    if not os.path.isdir(directory):
        os.makedirs(directory)

    refused = []  # type: List[str]
    for name, text in hooks(python):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as handle:
                existing = handle.read()
            if MARKER not in existing:
                refused.append(path)
                print("install_hooks: left alone (not ours): %s" % path)
                continue
            if existing == text:
                _make_executable(path)
                print("install_hooks: unchanged: %s" % path)
                continue
        # newline="\n": a CRLF after "#!/bin/sh" breaks the hook on Windows.
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        _make_executable(path)
        print("install_hooks: wrote %s" % path)

    code, hooks_path = _git(["config", "--get", "core.hooksPath"], repo)
    if code == 0 and hooks_path:
        print("install_hooks: warning: core.hooksPath is set to %s, so git "
              "will not run the hooks in %s" % (hooks_path, directory))
    if refused:
        print("install_hooks: %d existing hook(s) not replaced; merge the "
              "gate into them by hand" % len(refused))
        return 1
    return 0


def _make_executable(path: str) -> None:
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the publication gate's git hooks.")
    parser.add_argument("--repo", metavar="DIR", default=REPO_ROOT,
                        help="repository to install into (default: the "
                             "checkout this script is in)")
    parser.add_argument("--python", metavar="EXE", default=None,
                        help="interpreter the hooks run (default: python "
                             "on PATH, else python3)")
    args = parser.parse_args(argv)
    return install(os.path.abspath(args.repo), args.python)


if __name__ == "__main__":
    sys.exit(main())
