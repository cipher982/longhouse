#!/usr/bin/env python3
"""Assert that an installed native Longhouse CLI reports the expected build identity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-commit",
        required=True,
        help="Expected full git commit SHA.",
    )
    parser.add_argument(
        "--expected-version",
        help="Expected release version, without a leading v. When omitted, only the commit is checked.",
    )
    parser.add_argument(
        "--longhouse-bin",
        default="longhouse",
        help="Longhouse executable to inspect. Default: longhouse from PATH.",
    )
    return parser.parse_args(argv)


def load_installed_build(longhouse_bin: str) -> dict[str, Any]:
    # The Runtime Host wheel and the native device facade intentionally have
    # different entrypoints. Keep one strict checker for both release lanes.
    # Prefer the interface associated with the executable name, then accept the
    # other interface so explicit wrapper paths remain usable in local checks.
    if Path(longhouse_bin).name == "longhouse-server":
        commands = (("version", "build"), ("build-identity", "facade"))
    else:
        commands = (("build-identity", "facade"), ("version", "build"))
    errors: list[str] = []
    for command, field in commands:
        proc = subprocess.run(
            [longhouse_bin, command, "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            errors.append(f"{command} --json failed: {detail}")
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"{command} --json did not emit JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{command} --json emitted a non-object payload")
            continue
        build = payload.get(field)
        if isinstance(build, dict):
            return build
        errors.append(f"{command} --json payload missing {field} object")
    raise RuntimeError(f"{longhouse_bin} build identity probe failed: {'; '.join(errors)}")


def commit_matches(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    return actual == expected


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_commit = args.expected_commit.strip()
    expected_version = (args.expected_version or "").strip().removeprefix("v")

    try:
        build = load_installed_build(args.longhouse_bin)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    actual_commit = str(build.get("commit") or "")
    actual_version = str(build.get("version") or "")

    errors: list[str] = []
    if not commit_matches(actual_commit, expected_commit):
        errors.append(f"commit mismatch: expected {expected_commit}, got {actual_commit or '<missing>'}")
    if expected_version and actual_version != expected_version:
        errors.append(f"version mismatch: expected {expected_version}, got {actual_version or '<missing>'}")

    if errors:
        print("Longhouse installed build identity mismatch:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print("Installed build:", json.dumps(build, sort_keys=True), file=sys.stderr)
        return 1

    print(f"Installed Longhouse build matches commit {actual_commit[:12]} version {actual_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
