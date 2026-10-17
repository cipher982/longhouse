#!/usr/bin/env python3
"""Fail when a workflow or action uses GitHub Actions artifact or cache storage.

On a private repository that storage is metered, and it is reachable only through
GitHub. A fact one run records for a later gate is a CI receipt in the artifact
object store (scripts/ops/ci_receipts.py); evidence about a failed run stays on the
runner that produced it; a build cache lives on the runner's disk. This check keeps
the storage from coming back by habit.

The allowlist (scripts/ci/actions-storage.allow, one `<file> <pattern>` per line, `#`
comments) names the exceptions. It only shrinks: each entry must say why in a comment
above it, and an entry for a use that no longer exists is itself an error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = {
    "upload-artifact": re.compile(r"actions/upload-artifact@"),
    "download-artifact": re.compile(r"actions/download-artifact@"),
    "actions-cache": re.compile(r"actions/cache(/restore|/save)?@"),
    "buildx-gha-cache": re.compile(r"type=gha\b"),
    "gh-run-download": re.compile(r"gh run download|\"run\",\s*\"download\""),
    "artifacts-api": re.compile(r"/actions/(runs/[^\s\"']+/)?artifacts"),
}
# Workflows and actions are scanned for every pattern; scripts only for the two ways code
# reaches the storage from outside a workflow. This file and the tests name them on purpose.
SCANNED = {
    ".github/workflows": tuple(PATTERNS),
    ".github/actions": tuple(PATTERNS),
    "scripts": ("gh-run-download", "artifacts-api"),
}
SKIPPED = ("scripts/ci/check-actions-storage.py", "scripts/tests/")
ALLOW = "scripts/ci/actions-storage.allow"


def uses(root: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for base, names in SCANNED.items():
        for path in sorted((root / base).rglob("*")):
            rel = path.relative_to(root).as_posix()
            if not path.is_file() or path.suffix not in {".yml", ".yaml", ".py", ".sh"} or rel.startswith(SKIPPED):
                continue
            # Whole file, comments dropped: the call can span lines (a Python argv list).
            text = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
            found.update((rel, name) for name in names if PATTERNS[name].search(text))
    return found


def allowed(root: Path) -> set[tuple[str, str]]:
    entries: set[tuple[str, str]] = set()
    allow = root / ALLOW
    if not allow.is_file():
        return entries
    for raw in allow.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[1] not in PATTERNS:
            raise SystemExit(f"{ALLOW}: expected '<file> <{'|'.join(PATTERNS)}>', got {raw!r}")
        entries.add((parts[0], parts[1]))
    return entries


def check(root: Path) -> list[str]:
    found, allow = uses(root), allowed(root)
    problems = [
        f"{path}: uses {name}; record a CI receipt (scripts/ops/ci_receipts.py), keep evidence on the runner, "
        f"or, for a transient result handed to a person on a machine with no store access, add it to {ALLOW} with the reason"
        for path, name in sorted(found - allow)
    ]
    problems += [f"{ALLOW}: stale entry '{path} {name}' (no such use remains); delete it" for path, name in sorted(allow - found)]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    problems = check(parser.parse_args().root)
    for line in problems:
        print(f"actions-storage: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
