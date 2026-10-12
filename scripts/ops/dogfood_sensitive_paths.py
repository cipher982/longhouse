#!/usr/bin/env python3
"""Classify the dogfood range for fast-lane eligibility.

A matching path means the change must use the canary-qualified dogfood path.
This classifier is used only to choose between dogfood promotion lanes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

POLICY = Path(__file__).with_name("dogfood-sensitive-paths.toml")


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve(repo: str | Path, rev: str) -> str:
    resolved = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False).strip()
    if not resolved:
        raise ValueError(f"{rev} is not a commit in this checkout")
    return resolved


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Match the repo-relative glob subset used by the previous sensitive-path policy."""
    out, i = ["^"], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def load_patterns(policy: str | Path = POLICY) -> tuple[list[str], list[re.Pattern[str]]]:
    data = tomllib.loads(Path(policy).read_text(encoding="utf-8"))
    paths = data.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
        raise ValueError(f"{policy} must contain a non-empty paths list")
    return paths, [compile_glob(path) for path in paths]


def changed_paths(repo: str | Path, base: str, head: str) -> list[str]:
    """Paths changed by any commit in base..head, including merge-only resolutions.

    Inspecting each commit rather than the final tree diff keeps a sensitive edit
    out of the fast lane even if a later commit reverts it before the target SHA.
    """
    base_sha, head_sha = resolve(repo, base), resolve(repo, head)
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base_sha, head_sha],
                              capture_output=True)
    if ancestry.returncode == 1:
        raise ValueError(f"{base_sha[:12]} is not an ancestor of {head_sha[:12]}")
    if ancestry.returncode:
        raise RuntimeError(f"git merge-base failed: {ancestry.stderr.decode(errors='replace').strip()[:300]}")

    out = git(repo, "log", "--diff-merges=off", "--reverse", "--no-renames", "--name-only",
              "--format=%x01%H%x02%P", f"{base_sha}..{head_sha}")
    paths: set[str] = set()
    for chunk in out.split("\x01")[1:]:
        if not chunk:
            continue
        header, _, names = chunk.partition("\n")
        sha, parents = header.split("\x02", 1)
        if len(parents.split()) > 1:
            if len(parents.split()) > 2:
                raise ValueError(f"{sha[:12]} is an octopus merge; its own changes cannot be measured")
            merge_names = git(repo, "show", "--remerge-diff", "--no-renames", "--name-only", "--format=", sha)
            paths.update(name for name in merge_names.splitlines() if name)
        else:
            paths.update(name for name in names.splitlines() if name)
    return sorted(paths)


def matching_sensitive_paths(repo: str | Path, base: str, head: str,
                             policy: str | Path = POLICY) -> list[str]:
    _, patterns = load_patterns(policy)
    return [path for path in changed_paths(repo, base, head)
            if any(pattern.fullmatch(path) for pattern in patterns)]


def has_sensitive_paths(repo: str | Path, base: str, head: str,
                        policy: str | Path = POLICY) -> bool:
    return bool(matching_sensitive_paths(repo, base, head, policy))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".", help="repository (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check whether base..head may use the dogfood fast lane")
    check.add_argument("--base", required=True)
    check.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    try:
        matches = matching_sensitive_paths(args.repo, args.base, args.head)
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"dogfood-sensitive-paths: could not decide: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if matches:
        print("dogfood-sensitive-paths: sensitive paths require the canary-qualified path:", file=sys.stderr)
        for path in matches:
            print(f"  {path}", file=sys.stderr)
        return 1
    print("dogfood-sensitive-paths: range contains no sensitive paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
