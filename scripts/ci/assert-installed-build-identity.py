#!/usr/bin/env python3
"""Assert that an installed Longhouse CLI reports the expected build identity."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-commit",
        required=True,
        help="Expected git commit. Full SHAs and unambiguous prefixes are accepted.",
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
    proc = subprocess.run(
        [longhouse_bin, "version", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"{longhouse_bin} version --json failed: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{longhouse_bin} version --json did not emit JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{longhouse_bin} version --json emitted a non-object payload")
    build = payload.get("build")
    if not isinstance(build, dict):
        raise RuntimeError(f"{longhouse_bin} version --json payload missing build object")
    return build


def commit_matches(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    return actual == expected or actual.startswith(expected) or expected.startswith(actual)


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
