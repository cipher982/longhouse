#!/usr/bin/env python3
"""Validate the native device command contract."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

VALID_STATUSES = {"available", "excluded"}
VALID_OWNERSHIP = {"user_owned", "not_applicable", "excluded_until_provider_surface"}
VALID_TOKEN_POLICIES = {"env_or_state_file", "no_token", "not_applicable"}
VALID_CWD_POLICIES = {"strict_absolute_or_existing", "not_applicable"}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validate(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if contract.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    owner = contract.get("native_owner")
    if owner != {"binary": "longhouse", "namespace": "device", "status": "available"}:
        errors.append("native_owner must be the available longhouse device command")
    commands = contract.get("commands")
    if not isinstance(commands, list) or not commands:
        return [*errors, "commands must be a non-empty list"]
    seen: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            errors.append("commands entries must be objects")
            continue
        command_id = str(command.get("id") or "").strip()
        if not command_id or command_id in seen:
            errors.append("each command requires a unique id")
        seen.add(command_id)
        if command.get("status") not in VALID_STATUSES:
            errors.append(f"{command_id}: status must be one of {sorted(VALID_STATUSES)}")
        target = str(command.get("native_target_command") or "")
        try:
            binary = shlex.split(target)[0]
        except (IndexError, ValueError):
            binary = ""
        if binary != "longhouse":
            errors.append(f"{command_id}: native_target_command must start with longhouse")
        if command.get("provider_binary_ownership") not in VALID_OWNERSHIP:
            errors.append(f"{command_id}: invalid provider_binary_ownership")
        if command.get("token_policy") not in VALID_TOKEN_POLICIES:
            errors.append(f"{command_id}: invalid token_policy")
        if command.get("cwd_policy") not in VALID_CWD_POLICIES:
            errors.append(f"{command_id}: invalid cwd_policy")
        if not str(command.get("notes") or "").strip():
            errors.append(f"{command_id}: notes are required")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_root())
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    path = args.contract or args.root / "config/native_device_entrypoints.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    errors = _validate(contract)
    if args.json:
        print(json.dumps({"contract": contract, "errors": errors}, indent=2))
    else:
        print("native device commands")
        for command in contract.get("commands", []):
            print(f"- {command.get('id')}: {command.get('native_target_command')} ({command.get('status')})")
    if errors:
        print(*errors, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
