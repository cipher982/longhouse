#!/usr/bin/env python3
"""Run readme-test blocks embedded in Markdown files.

Extracts JSON blocks fenced with ```readme-test from Markdown files,
then executes each block's steps in order. Fails fast on first error.

Usage:
    python scripts/run-readme-tests.py [--mode smoke|full] [files...]

If no files specified, scans README.md and */README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT = 120
DEFAULT_MODE = "smoke"


class ReadmeTestError(RuntimeError):
    pass


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_blocks(path: Path) -> list[dict]:
    """Extract and parse all readme-test JSON blocks from a Markdown file."""
    blocks: list[dict] = []
    in_block = False
    block_start = 0
    lines: list[str] = []

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.rstrip()
        if stripped == "```readme-test":
            if in_block:
                source = display_path(path)
                raise ReadmeTestError(f"{source}:{lineno}: nested readme-test block")
            in_block = True
            block_start = lineno
            lines = []
        elif in_block and stripped == "```":
            in_block = False
            raw = "\n".join(lines)
            try:
                block = json.loads(raw)
                block["_source"] = display_path(path)
                blocks.append(block)
            except json.JSONDecodeError as e:
                source = display_path(path)
                raise ReadmeTestError(f"{source}:{block_start}: bad readme-test JSON: {e}") from e
        elif in_block:
            lines.append(line)

    if in_block:
        source = display_path(path)
        raise ReadmeTestError(f"{source}:{block_start}: unterminated readme-test block")

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

    print(f"\n{'─' * 60}", flush=True)
    print(f"  {name}  ({source})", flush=True)
    print(f"{'─' * 60}", flush=True)

    if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
        print("  ✗  ERROR: steps must be a non-empty list of shell commands", file=sys.stderr)
        return False

    env = {**os.environ, **{k: str(v) for k, v in env_extra.items()}}

    # Write steps as a single shell script so variables persist across steps
    script_lines = ["set -euo pipefail"]
    for index, step in enumerate(steps, start=1):
        script_lines.append(f'printf "\\n[readme-test] step {index}/{len(steps)}: %s\\n" {shlex.quote(step)}')
        script_lines.append(step)
    script = "\n".join(script_lines)

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
            print(f"  ✓  PASS", flush=True)
        else:
            print(f"  ✗  FAIL (exit {result.returncode})", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"  ✗  TIMEOUT after {timeout}s", file=sys.stderr)
    except Exception as e:
        print(f"  ✗  ERROR: {e}", file=sys.stderr)
    finally:
        if cleanup:
            print("  → cleanup", flush=True)
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
        *sorted(REPO_ROOT.glob("*/README.md")),
    ]
    return [p for p in candidates if p.exists()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run readme-test blocks")
    parser.add_argument("--mode", choices=["smoke", "full"], default=DEFAULT_MODE)
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit successfully when no readme-test blocks are found.",
    )
    parser.add_argument("files", nargs="*", type=Path, help="README files to scan")
    args = parser.parse_args(argv)

    paths = [Path(f).resolve() for f in args.files] if args.files else default_paths()
    try:
        blocks = collect_blocks(paths, args.mode)
    except ReadmeTestError as exc:
        print(f"README test contract error: {exc}", file=sys.stderr)
        return 1

    if not blocks:
        if args.allow_empty:
            print(f"No readme-test blocks found for mode='{args.mode}'.")
            return 0
        print(f"No readme-test blocks found for mode='{args.mode}'.")
        return 1

    print(f"\nRunning {len(blocks)} readme-test block(s)  [mode={args.mode}]", flush=True)

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
