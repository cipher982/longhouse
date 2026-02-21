#!/usr/bin/env python3
"""Run readme-test blocks embedded in Markdown files.

Extracts JSON blocks fenced with ```readme-test from Markdown files,
then executes each block's steps in order. Fails fast on first error.

Usage:
    python scripts/run-readme-tests.py [--mode smoke|full] [files...]

If no files specified, scans README.md and packages/*/README.md and apps/*/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_TIMEOUT = 120
DEFAULT_MODE = "smoke"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_blocks(path: Path) -> list[dict]:
    """Extract and parse all readme-test JSON blocks from a Markdown file."""
    blocks: list[dict] = []
    in_block = False
    lines: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.rstrip()
        if stripped == "```readme-test":
            in_block = True
            lines = []
        elif in_block and stripped == "```":
            in_block = False
            raw = "\n".join(lines)
            try:
                block = json.loads(raw)
                block["_source"] = str(path.relative_to(REPO_ROOT))
                blocks.append(block)
            except json.JSONDecodeError as e:
                print(f"  ⚠  Bad JSON in {path}: {e}", file=sys.stderr)
        elif in_block:
            lines.append(line)

    return blocks


def collect_blocks(paths: list[Path], mode: str) -> list[dict]:
    """Collect blocks matching the given mode from all paths."""
    all_blocks: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        for block in extract_blocks(path):
            block_mode = block.get("mode", "smoke")
            # smoke mode runs only smoke; full mode runs both smoke and full
            if mode == "smoke" and block_mode != "smoke":
                continue
            all_blocks.append(block)
    return all_blocks


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_block(block: dict) -> bool:
    """Execute a readme-test block. Returns True on success."""
    name = block.get("name", "unnamed")
    source = block.get("_source", "?")
    steps = block.get("steps", [])
    cleanup = block.get("cleanup", [])
    timeout = block.get("timeout", DEFAULT_TIMEOUT)
    env_extra = block.get("env", {})
    workdir = REPO_ROOT / block.get("workdir", ".")

    print(f"\n{'─' * 60}")
    print(f"  {name}  ({source})")
    print(f"{'─' * 60}")

    env = {**os.environ, **{k: str(v) for k, v in env_extra.items()}}

    # Write steps as a single shell script so variables persist across steps
    script = "\n".join(["set -euo pipefail", *steps])

    success = False
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=workdir,
            env=env,
            timeout=timeout,
            capture_output=False,  # stream to terminal
        )
        success = result.returncode == 0
        if success:
            print(f"  ✓  PASS")
        else:
            print(f"  ✗  FAIL (exit {result.returncode})", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"  ✗  TIMEOUT after {timeout}s", file=sys.stderr)
    except Exception as e:
        print(f"  ✗  ERROR: {e}", file=sys.stderr)
    finally:
        if cleanup:
            print("  → cleanup")
            cleanup_script = "\n".join(cleanup)
            subprocess.run(
                ["bash", "-c", cleanup_script],
                cwd=workdir,
                env=env,
                timeout=60,
                capture_output=True,  # suppress cleanup output
            )

    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def default_paths() -> list[Path]:
    """Default set of README files to scan."""
    candidates = [
        REPO_ROOT / "README.md",
        *sorted((REPO_ROOT / "packages").glob("*/README.md")),
        *sorted((REPO_ROOT / "apps").glob("*/README.md")),
    ]
    return [p for p in candidates if p.exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run readme-test blocks")
    parser.add_argument("--mode", choices=["smoke", "full"], default=DEFAULT_MODE)
    parser.add_argument("files", nargs="*", type=Path, help="README files to scan")
    args = parser.parse_args()

    paths = [Path(f).resolve() for f in args.files] if args.files else default_paths()
    blocks = collect_blocks(paths, args.mode)

    if not blocks:
        print(f"No readme-test blocks found for mode='{args.mode}'.")
        return 0

    print(f"\nRunning {len(blocks)} readme-test block(s)  [mode={args.mode}]")

    passed = 0
    failed = 0
    for block in blocks:
        if run_block(block):
            passed += 1
        else:
            failed += 1
            # Fail fast
            break

    print(f"\n{'─' * 60}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'─' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
