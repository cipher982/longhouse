#!/usr/bin/env python3
"""Find a later green CI run with a Terminal Truth status that covers a live-preview commit with no verdict of its own.

  terminal_truth_cover.py [--repo DIR] [--main REF] COMMIT

COMMIT is the newest live-preview commit of a promotion range (dogfood_sensitive_paths.py live-preview). CI runs one at a time on main and GitHub keeps one pending run per group, so a push that is followed by another before
the lane frees up never gets a run of its own. Its run tested a tree that did not contain it. A later commit D covers
COMMIT only when D descends from COMMIT (so D's tree holds COMMIT's change), D is on main, and D carries a passing
`longhouse/terminal-truth` status posted by GitHub Actions. The caller uses this only when COMMIT has no status or a
pending one; a failed or errored status on COMMIT is never overridden.

Prints `D<TAB>target_url` when a covering commit exists, nothing otherwise. Exit 0 either way; exit 2 when the lookup
itself could not be made (the caller then treats COMMIT as having no verdict). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse")
CONTEXT = "longhouse/terminal-truth"
MAX_STATUS_LOOKUPS = 8  # newest candidates only; most CI runs carry no terminal-truth status of their own


def gh_json(*args: str):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "null")


def is_ancestor(repo: str, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode == 0


def passing(statuses) -> dict | None:
    """The newest terminal-truth status when it is a success posted by GitHub Actions."""
    rows = [s for s in statuses or [] if s.get("context") == CONTEXT]
    row = rows[0] if rows else None  # newest first
    if row and row.get("state") == "success" and (row.get("creator") or {}).get("login") == "github-actions[bot]":
        return row
    return None


def covering(repo: str, main: str, commit: str, runs, statuses_of) -> tuple[str, str] | None:
    seen: set[str] = set()
    lookups = 0
    for run in sorted(runs, key=lambda r: int(r.get("run_number") or 0), reverse=True):
        sha = str(run.get("head_sha") or "")
        if run.get("conclusion") != "success" or sha in seen or sha == commit:
            continue
        seen.add(sha)
        if not (is_ancestor(repo, commit, sha) and is_ancestor(repo, sha, main)):
            continue
        if lookups >= MAX_STATUS_LOOKUPS:
            return None
        lookups += 1
        row = passing(statuses_of(sha))
        if row:
            return sha, str(row.get("target_url") or "")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--main", default="origin/main")
    parser.add_argument("commit")
    args = parser.parse_args(argv)
    try:
        runs = gh_json("api", f"repos/{REPO}/actions/workflows/contract-first-ci.yml/runs?branch=main&status=success&per_page=30")
        if not isinstance(runs, dict):
            raise ValueError("the workflow run list is not an object")
        found = covering(args.repo, args.main, args.commit, runs.get("workflow_runs") or [],
                         lambda sha: gh_json("api", f"repos/{REPO}/commits/{sha}/statuses?per_page=30"))
    except (RuntimeError, ValueError) as exc:
        print(f"terminal-truth-cover: {exc}", file=sys.stderr)
        return 2
    if found:
        print(f"{found[0]}\t{found[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
