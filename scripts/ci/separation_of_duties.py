#!/usr/bin/env python3
"""Separation of duties: no single commit changes both the verifier and the code it judges.

The provider factory's verifier decides whether the product works. An agent that can edit the oracle and the
product in one commit can make a red check green by weakening the check, and nothing downstream can tell. Each
commit may therefore touch only one of these classes:

  verifier  server/zerg/qa/**, the provider-control canary script, server/zerg/provider_contract/** (shared proof
            vocabulary both sides read, so it moves the verdict like the oracles do)
  subject   server/zerg/** (the rest), engine/**, schemas/**, config/**
  guard     the code that enforces this rule and the verifier boundary (GUARD_FILES), and, as later phases of
            control-plane docs/specs/multi-agent-dev-loop-speed.md (change 6) land, the replay guard and corpus

Files in none of these (web, ios, docs, tests, workflows, the boundary allowlist, the generated contract manifests in
DERIVED_FILES) combine with anything. The
allowlist `scripts/ci/verifier-boundary.allow` is ratcheted by its own check (shrink-only) and so is not a class.

The unit is the commit, not the push: two commits in one push are fine. A commit that exactly restores the
content a landed commit replaced (a `git revert`) is exempt, since it adds nothing new; the check compares blobs
to the reverted commit's parent instead of trusting the message.

  separation_of_duties.py check [--base REV] [--head REV]     every non-merge commit in base..head
  separation_of_duties.py commit REV                          one commit

Exit 1 on a violation, with the commit, the files of each class and the way out (split it).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

VERIFIER_PREFIXES = ("server/zerg/qa/", "server/zerg/provider_contract/")
VERIFIER_FILES = ("scripts/qa/provider-control-e2e-canary.py",)
SUBJECT_PREFIXES = ("server/zerg/", "engine/", "schemas/", "config/")
GUARD_FILES = (
    "scripts/ci/separation_of_duties.py",
    "scripts/tests/separation-of-duties.test.py",
    "scripts/ci/verifier_boundary.py",
    "scripts/tests/verifier-boundary.test.py",
)
GUARD_PREFIXES = ("scripts/ci/verifier_guard/",)
# Files generated from a source of one class and re-derived by `make validate` / the pre-commit autofix, which embed
# digests of the other class (the contract manifests carry each oracle's `oracle_digest`). They are not a place to hide
# a change: CI regenerates them and fails on any difference, so a hand edit cannot survive. Without this exemption every
# oracle edit would need a second commit that the pre-commit hook folds back into the first.
DERIVED_FILES = (
    "server/zerg/config/managed_provider_contracts.json",
    "engine/src/managed_provider_contracts.generated.json",
    "docs/generated/provider_census.json",
)

VERIFIER, SUBJECT, GUARD = "verifier", "subject", "guard"


def classify(path: str) -> str | None:
    if path in DERIVED_FILES:
        return None
    if path in GUARD_FILES or path.startswith(GUARD_PREFIXES):
        return GUARD
    if path.startswith(VERIFIER_PREFIXES) or path in VERIFIER_FILES:
        return VERIFIER
    if path.startswith(SUBJECT_PREFIXES):
        return SUBJECT
    return None


def classes(paths: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in paths:
        kind = classify(path)
        if kind:
            out.setdefault(kind, []).append(path)
    return out


def git(*args: str, root: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if check and result.returncode:
        raise SystemExit(f"separation-of-duties: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(commit: str, root: Path = ROOT) -> list[str]:
    # -m would split merges; merges are skipped by the caller. --no-renames: a rename is a delete plus an add.
    out = git("diff-tree", "--root", "-r", "--no-renames", "--name-only", "-z", commit, root=root)
    return [p for p in out.split("\0") if p]


def _blob(rev: str, path: str, root: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{rev}:{path}"], cwd=root, text=True,
                            capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def is_exact_revert(commit: str, files: list[str], root: Path = ROOT) -> bool:
    """True when every file of the commit returns to its content before the commit it names as reverted."""
    body = git("show", "-s", "--format=%B", commit, root=root)
    names = re.findall(r"This reverts commit ([0-9a-f]{40})", body)
    if len(names) != 1:
        return False
    reverted = names[0]
    if subprocess.run(["git", "merge-base", "--is-ancestor", reverted, commit], cwd=root).returncode != 0:
        return False
    before = f"{reverted}^"
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", before], cwd=root, capture_output=True).returncode:
        return False
    return all(_blob(commit, p, root) == _blob(before, p, root) for p in files)


def violation(commit: str, root: Path = ROOT) -> dict[str, list[str]] | None:
    files = changed_files(commit, root)
    found = classes(files)
    if len(found) < 2:
        return None
    if is_exact_revert(commit, files, root):
        return None
    return found


def check(base: str | None, head: str, root: Path = ROOT) -> int:
    rng = f"{base}..{head}" if base else head
    args = ["rev-list", "--no-merges", "--reverse"] + ([rng] if base else ["-n", "1", head])
    commits = git(*args, root=root).split()
    bad = 0
    for commit in commits:
        found = violation(commit, root)
        if not found:
            continue
        bad += 1
        subject = git("show", "-s", "--format=%s", commit, root=root).strip()
        print(f"separation-of-duties: {commit[:10]} {subject!r} changes more than one class of files:", file=sys.stderr)
        for kind in (VERIFIER, SUBJECT, GUARD):
            for path in found.get(kind, [])[:8]:
                print(f"  {kind:<8} {path}", file=sys.stderr)
            if len(found.get(kind, [])) > 8:
                print(f"  {kind:<8} ... and {len(found[kind]) - 8} more", file=sys.stderr)
    if bad:
        print(
            f"separation-of-duties: {bad} commit(s) refused. A commit may change the verifier, the product it judges, "
            "or the guard, never two of them: split it into separate commits (verifier first when the product change "
            "depends on it) and land each. Spec: control-plane docs/specs/multi-agent-dev-loop-speed.md, change 6.",
            file=sys.stderr,
        )
        return 1
    print(f"separation-of-duties: OK ({len(commits)} commit(s) checked)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--base")
    c.add_argument("--head", default="HEAD")
    one = sub.add_parser("commit")
    one.add_argument("rev")
    args = parser.parse_args()
    if args.cmd == "commit":
        return check(None, args.rev)
    if args.cmd in (None, "check"):
        base = getattr(args, "base", None)
        if base is None:
            base = "origin/main"
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=ROOT,
                          capture_output=True).returncode:
            print(f"separation-of-duties: no base {base}; nothing to compare", file=sys.stderr)
            return 0
        return check(base, getattr(args, "head", "HEAD"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
