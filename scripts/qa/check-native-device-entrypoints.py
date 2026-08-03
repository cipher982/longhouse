#!/usr/bin/env python3
"""Validate the native device command contract."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
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


def _validate_facade(contract: dict[str, Any], facade: Path) -> list[str]:
    """Ensure the installed public binary parses every available contract route."""
    errors: list[str] = []
    for command in contract.get("commands", []):
        if command.get("status") != "available":
            continue
        argv = shlex.split(str(command["native_target_command"]))[1:]
        argv = ["/tmp" if value.startswith("<") else value for value in argv]
        result = subprocess.run([str(facade), *argv, "--help"], text=True, capture_output=True, check=False)
        if result.returncode:
            errors.append(f"{command.get('id')}: installed facade does not parse route: {result.stderr.strip()}")
    cursor = next((item for item in contract.get("commands", []) if item.get("id") == "cursor-managed"), None)
    if cursor and cursor.get("status") == "available":
        result = subprocess.run([str(facade), "cursor", "--resume-session", "00000000-0000-0000-0000-000000000000", "--help"], text=True, capture_output=True, check=False)
        if result.returncode:
            errors.append("cursor-managed: Runtime Host attach command does not parse against installed facade")
    return errors


def _resolve_facade(explicit: Path | None) -> Path | None:
    """Find a longhouse facade to check routes against.

    Without one this script only validates the contract JSON, which is how a
    route stayed marked `available` for eleven days while the command it named
    could not feed its only consumer.
    """
    if explicit:
        return explicit
    candidate = Path.home() / ".local" / "bin" / "longhouse"
    if candidate.is_file():
        return candidate
    which = shutil.which("longhouse")
    return Path(which) if which else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_root())
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--facade", type=Path, help="Built installed longhouse facade to parse-check")
    parser.add_argument(
        "--require-facade",
        action="store_true",
        help="Fail when no facade is available instead of reporting routes as unverified",
    )
    args = parser.parse_args()
    path = args.contract or args.root / "config/native_device_entrypoints.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    errors = _validate(contract)
    facade = _resolve_facade(args.facade)
    if facade:
        errors.extend(_validate_facade(contract, facade))
    elif args.require_facade:
        errors.append("no longhouse facade available: contract routes were not verified against a binary")
    if args.json:
        print(
            json.dumps(
                {
                    "contract": contract,
                    "errors": errors,
                    "facade": str(facade) if facade else None,
                    "routes_verified": bool(facade),
                },
                indent=2,
            )
        )
    else:
        print("native device commands")
        for command in contract.get("commands", []):
            print(f"- {command.get('id')}: {command.get('native_target_command')} ({command.get('status')})")
        if facade:
            print(f"routes verified against {facade}")
        else:
            # Say so out loud. A silent skip reads identically to a pass.
            print("routes NOT verified: no longhouse facade found", file=sys.stderr)
    if errors:
        print(*errors, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
