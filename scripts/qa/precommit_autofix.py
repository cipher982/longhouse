#!/usr/bin/env python3
"""Mechanical pre-commit fixes that stage themselves, so one `git commit` succeeds.

A formatter or a generated file that is out of date used to fail the commit and send the author
around again (`ruff format`, the managed-provider contract digest, the provider census, trailing
whitespace). Those rewrites are deterministic and carry no meaning, so the hooks now apply them and
re-stage the result, but only where that is safe:

  pre-commit runs its hooks after stashing unstaged changes, so the working tree is exactly the
  commit's content and a fix computed there is a fix of the commit. Re-staging a file is only safe
  when that file had no unstaged changes: otherwise pre-commit's restore of the stash conflicts with
  the fix and aborts. Inside the hook the stash has already hidden them, so `snapshot` records them
  first: it runs as `.git/hooks/pre-commit.legacy`, which pre-commit runs before it stashes anything
  (installed by scripts/ops/install-push-gate.sh). Its record is trusted only by hooks of the same
  pre-commit process (same parent pid).

  fix          the hook: run a fixer, then for each file it changed
                 - safe (recorded, no unstaged changes): `git add` it, print what was fixed, pass;
                 - recorded with unstaged changes: undo the fix, fail, say how to fix it by hand;
                 - no record for this run (shims not installed, or a manual `pre-commit run`): leave
                   the fix in the working tree and fail, which is pre-commit's own fixer behavior.
               A fixer that errors (a syntax error, a generator crash) fails loudly and is undone.
  snapshot     the pre-commit.legacy shim: record which paths have unstaged changes.
  post-commit  the post-commit shim: `git commit -o PATHS` commits from a temporary index, so a fix
               staged there reaches the commit but not the real index, which would then show the
               fix reverted as a staged change. Re-add each fixed path whose working tree equals HEAD.

Never used for lint autofixes (`ruff --fix` removes "unused" imports, which deletes pytest
fixtures): only for rewrites that do not change what the code does.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

STATE_DIRNAME = "longhouse-autofix"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def state_dir() -> Path:
    # Per worktree (not the common dir): two worktrees committing at once must not share a record.
    return Path(git("rev-parse", "--absolute-git-dir").stdout.strip()) / STATE_DIRNAME


def snapshot() -> int:
    """Paths whose working tree differs from the index git is committing (GIT_INDEX_FILE under `commit -o`):
    exactly what pre-commit is about to stash."""
    try:
        out = git("diff", "--name-only", "-z", "--no-ext-diff", "--ignore-submodules").stdout
        directory = state_dir()
        directory.mkdir(exist_ok=True)
        pid = int(os.environ.get("LONGHOUSE_AUTOFIX_PID") or os.getppid())  # the shim passes pre-commit's pid
        (directory / "unstaged.json").write_text(json.dumps({"pid": pid, "paths": [p for p in out.split("\0") if p]}))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"autofix: could not record unstaged files ({exc}); fixes will not be staged for you this time", file=sys.stderr)
    return 0  # never the reason a commit fails


def unstaged_record() -> set[str] | None:
    try:
        record = json.loads((state_dir() / "unstaged.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("pid") != os.getppid():
        return None  # another run's record (or a manual `pre-commit run`): nothing is known
    return set(record.get("paths") or [])


def _read(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _restore(path: str, content: bytes | None) -> None:
    if content is None:
        Path(path).unlink(missing_ok=True)
    else:
        Path(path).write_bytes(content)


def fix(name: str, cmd: str, ok_exit: set[int], watch: list[str], how: str, files: list[str]) -> int:
    paths = list(dict.fromkeys([*files, *watch]))
    before = {p: _read(p) for p in paths}
    proc = subprocess.run([*shlex.split(cmd), *files], capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    changed = [p for p in paths if _read(p) != before[p]]
    if proc.returncode not in ok_exit:
        for p in changed:
            _restore(p, before[p])
        print(f"autofix: {name} failed (exit {proc.returncode}); nothing was changed:\n{output}", file=sys.stderr)
        return 1
    if not changed:
        return 0
    unstaged = unstaged_record()
    if unstaged is None:
        print(f"autofix: {name} fixed {', '.join(changed)} in the working tree. Check and `git add` them, then commit "
              "again (`make install-push-gate` once per clone makes the hook stage such fixes itself).", file=sys.stderr)
        return 1
    unsafe = [p for p in changed if p in unstaged]
    safe = [p for p in changed if p not in unstaged]
    for p in unsafe:
        _restore(p, before[p])
    if safe:
        git("add", "--", *safe)
        directory = state_dir()
        directory.mkdir(exist_ok=True)
        with open(directory / "staged", "a") as fh:
            fh.write("".join(f"{p}\n" for p in safe))
        print(f"autofix: {name}: fixed and re-staged {', '.join(safe)}")
    if unsafe:
        print(f"autofix: {name} would change {', '.join(unsafe)}, which also has unstaged changes, so it was not fixed "
              f"for you (re-staging it would conflict with them). Stage or stash those changes, or run:\n  {how}\n"
              "then `git add` the result and commit again.", file=sys.stderr)
        return 1
    return 0


def post_commit() -> int:
    # The repository's own index: under `commit -o` git may still name the temporary one it committed from.
    os.environ.pop("GIT_INDEX_FILE", None)
    record = state_dir() / "staged"
    try:
        paths = list(dict.fromkeys(p for p in record.read_text().splitlines() if p))
    except OSError:
        return 0
    record.unlink(missing_ok=True)
    # Only where the working tree is exactly what was committed: then the index becomes HEAD for that path,
    # and nothing anyone staged or changed since is touched.
    settled = [p for p in paths if Path(p).exists() and git("diff", "--quiet", "HEAD", "--", p, check=False).returncode == 0]
    if settled:
        git("add", "--", *settled, check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    f = sub.add_parser("fix", help="run a fixer on the files pre-commit passes and stage what it fixed when safe")
    f.add_argument("--name", required=True, help="what the fixer does, for messages")
    f.add_argument("--cmd", required=True, help="the fixer command; the files are appended")
    f.add_argument("--ok-exit", default="0", help="exit codes that mean success or 'fixed something' (comma-separated)")
    f.add_argument("--watch", action="append", default=[], help="a file the fixer writes that is not passed to it (generated output)")
    f.add_argument("--how", required=True, help="the command to run by hand when the fix cannot be staged")
    f.add_argument("files", nargs="*")
    sub.add_parser("snapshot", help="record unstaged paths (the pre-commit.legacy shim)")
    sub.add_parser("post-commit", help="bring the index up to HEAD for paths a fix staged (the post-commit shim)")
    args = parser.parse_args(argv)
    if args.mode == "snapshot":
        return snapshot()
    if args.mode == "post-commit":
        return post_commit()
    return fix(args.name, args.cmd, {int(c) for c in args.ok_exit.split(",")}, args.watch, args.how, args.files)


if __name__ == "__main__":
    sys.exit(main())
