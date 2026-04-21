#!/usr/bin/env python3
"""Assert that .build/build-identity.json matches git HEAD.

The generator (`scripts/build/generate_build_identity.py`) is supposed to
run immediately before any consumer (cargo, docker build, iOS stage, wheel
pack). When it doesn't — as with the 2026-04 dogfood dogfood engine bug —
consumers silently stamp a stale SHA into their binaries.

This script lets CI and local scripts turn that class of bug into a loud
error. Exit 0 iff the staged commit matches `git rev-parse HEAD`.

Intentionally strict:
- no fallbacks
- no "git unavailable, skip" — callers that don't have git (e.g. a Docker
  build stage reading the already-baked file) simply shouldn't run this.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--identity",
        type=Path,
        default=repo_root / ".build" / "build-identity.json",
        help="Path to build-identity.json (default: .build/build-identity.json at repo root)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=repo_root,
        help="Repo root for `git rev-parse HEAD` (default: repo root)",
    )
    args = parser.parse_args(argv)

    if not args.identity.is_file():
        print(f"error: build identity missing at {args.identity}", file=sys.stderr)
        print("       run scripts/build/generate_build_identity.py first", file=sys.stderr)
        return 1

    try:
        payload = json.loads(args.identity.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: build identity at {args.identity} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    staged = payload.get("commit")
    if not isinstance(staged, str) or not staged:
        print(f"error: build identity at {args.identity} has invalid commit={staged!r}", file=sys.stderr)
        return 1

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=args.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"error: git rev-parse HEAD failed in {args.repo}: {exc.stderr}", file=sys.stderr)
        return 1

    if head != staged:
        print(
            f"error: build identity at {args.identity} is stale: "
            f"commit={staged} but git HEAD={head}",
            file=sys.stderr,
        )
        print("       run scripts/build/generate_build_identity.py and retry", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
