#!/usr/bin/env python3
"""Assert that the Runtime Host package does not publish a device command."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

DEVICE_COMMANDS = ("claude", "codex", "opencode", "cursor", "agy", "antigravity", "machine", "local-health", "provider-live", "connect")


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def check(project_root: Path) -> list[str]:
    errors: list[str] = []
    package = tomllib.loads((project_root / "server/pyproject.toml").read_text(encoding="utf-8"))
    scripts = package.get("project", {}).get("scripts", {})
    if set(scripts) != {"longhouse-server"}:
        errors.append("the Runtime Host package must publish only longhouse-server")
    main = (project_root / "server/zerg/cli/main.py").read_text(encoding="utf-8")
    for command in DEVICE_COMMANDS:
        if f'name="{command}"' in main or f'name="{command}",' in main:
            errors.append(f"Runtime Host publishes device command {command}")
    release_workflow = (project_root / ".github/workflows/local-runtime-release.yml").read_text(encoding="utf-8")
    if 'version_line="$(longhouse --version' in release_workflow:
        errors.append("release verification still invokes the retired Python device CLI")
    if "--longhouse-bin longhouse-server" not in release_workflow:
        errors.append("release build-identity verification must use longhouse-server")
    dogfood = (project_root / "scripts/dev/dogfood-runtime.sh").read_text(encoding="utf-8")
    if "zerg.cli.main" in dogfood or "connect --install" in dogfood:
        errors.append("dogfood refresh still invokes the retired Python device CLI")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root())
    args = parser.parse_args()
    errors = check(args.root)
    if errors:
        print(*errors, sep="\n")
        return 1
    print("Runtime Host publishes only longhouse-server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
