#!/usr/bin/env python3
"""Validate the Phase 2 native device-entrypoint contract."""

from __future__ import annotations

import argparse
import json
import runpy
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

VALID_COMMAND_STATUSES = {"planned", "native", "transitional_shim", "excluded"}
VALID_NATIVE_OWNER_STATUSES = {"planned", "native"}
VALID_SHIM_STATUSES = {"transitional_shim", "legacy_compat"}
VALID_PROVIDER_OWNERSHIP = {"user_owned", "not_applicable", "excluded_until_provider_surface"}
VALID_TOKEN_POLICIES = {"env_or_state_file", "no_token", "not_applicable"}
VALID_CWD_POLICIES = {"strict_absolute_or_existing", "inherits_existing", "not_applicable"}
VALID_PHASES = {"phase2", "phase3", "phase4", "phase5", "phase6", "phase7"}
TRANSITIONAL_CATEGORIES = {"transitional_device", "legacy_compat"}
FORBIDDEN_NATIVE_COMMAND_BINS = {"longhouse", "python", "python3", "uv", "pip"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_contract(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("native device entrypoint contract must be a JSON object")
    return payload


def _load_phase1_inventory(root: Path) -> list[dict[str, Any]]:
    script_path = root / "scripts/qa/check-no-python-device-path.py"
    namespace = runpy.run_path(str(script_path))
    inventory = namespace.get("DEFAULT_INVENTORY")
    if not isinstance(inventory, tuple):
        raise ValueError("Phase 1 no-Python inventory did not expose DEFAULT_INVENTORY")
    return [dict(item) for item in inventory]


def _packaged_console_scripts(root: Path) -> dict[str, str]:
    path = root / "server/pyproject.toml"
    if not path.exists():
        return {}
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    scripts = payload.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(target) for name, target in scripts.items() if isinstance(target, str)}


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _native_command_bin(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _providers(value: Any) -> list[str]:
    if value == "all":
        return ["all"]
    return _as_string_list(value)


def _validate_contract(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    inventory = _load_phase1_inventory(root)
    inventory_by_id = {str(item.get("id") or ""): item for item in inventory}
    transitional_inventory_ids = {
        item_id
        for item_id, item in inventory_by_id.items()
        if item.get("category") in TRANSITIONAL_CATEGORIES
    }

    if contract.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    owner = contract.get("native_owner")
    if not isinstance(owner, dict):
        errors.append("native_owner is required")
        owner = {}
    owner_binary = str(owner.get("binary") or "").strip()
    owner_namespace = str(owner.get("namespace") or "").strip()
    if owner_binary != "longhouse-engine":
        errors.append("native_owner.binary must be longhouse-engine")
    if owner_namespace != "device":
        errors.append("native_owner.namespace must be device")
    owner_status = str(owner.get("status") or "").strip()
    if owner_status not in VALID_NATIVE_OWNER_STATUSES:
        errors.append(f"native_owner.status must be one of {sorted(VALID_NATIVE_OWNER_STATUSES)}")
    native_prefix = f"{owner_binary} {owner_namespace}".strip()

    shim_entries = contract.get("compatibility_shims")
    if not isinstance(shim_entries, list):
        errors.append("compatibility_shims must be a list")
        shim_entries = []
    shim_by_script: dict[str, dict[str, Any]] = {}
    for shim in shim_entries:
        if not isinstance(shim, dict):
            errors.append("compatibility_shims entries must be objects")
            continue
        script = str(shim.get("script") or "").strip()
        if not script:
            errors.append("compatibility shim is missing script")
            continue
        if script in shim_by_script:
            errors.append(f"compatibility shim {script} is duplicated")
        shim_by_script[script] = shim
        status = str(shim.get("status") or "").strip()
        if status not in VALID_SHIM_STATUSES:
            errors.append(f"compatibility shim {script} status must be one of {sorted(VALID_SHIM_STATUSES)}")
        removal_phase = str(shim.get("removal_phase") or "").strip()
        if removal_phase and removal_phase not in VALID_PHASES:
            errors.append(f"compatibility shim {script} removal_phase must be one of {sorted(VALID_PHASES)}")
        target = str(shim.get("target") or "").strip()
        packaged_target = _packaged_console_scripts(root).get(script)
        if packaged_target and target != packaged_target:
            errors.append(f"compatibility shim {script} target {target!r} does not match packaged target {packaged_target!r}")
        delegates_to = str(shim.get("delegates_to") or "").strip()
        if delegates_to and not delegates_to.startswith(native_prefix):
            errors.append(f"compatibility shim {script} delegates_to must start with {native_prefix!r}")
        for item_id in _as_string_list(shim.get("phase1_inventory_ids")):
            if item_id not in inventory_by_id:
                errors.append(f"compatibility shim {script} references unknown Phase 1 inventory id {item_id}")

    for script in sorted(_packaged_console_scripts(root)):
        if script not in shim_by_script:
            errors.append(f"packaged console script {script} has no native entrypoint compatibility plan")

    commands = contract.get("commands")
    if not isinstance(commands, list):
        errors.append("commands must be a list")
        commands = []

    covered_inventory_ids: set[str] = set()
    seen_command_ids: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            errors.append("commands entries must be objects")
            continue
        command_id = str(command.get("id") or "").strip()
        if not command_id:
            errors.append("command entry is missing id")
            continue
        if command_id in seen_command_ids:
            errors.append(f"command id {command_id} is duplicated")
        seen_command_ids.add(command_id)

        status = str(command.get("status") or "").strip()
        if status not in VALID_COMMAND_STATUSES:
            errors.append(f"{command_id}: status must be one of {sorted(VALID_COMMAND_STATUSES)}")

        for required in ("implementation_phase", "native_target_command", "provider_binary_ownership", "token_policy", "cwd_policy", "notes"):
            if not str(command.get(required) or "").strip():
                errors.append(f"{command_id}: {required} is required")

        implementation_phase = str(command.get("implementation_phase") or "").strip()
        if implementation_phase and implementation_phase not in VALID_PHASES:
            errors.append(f"{command_id}: implementation_phase must be one of {sorted(VALID_PHASES)}")

        legacy_commands = _as_string_list(command.get("legacy_commands"))
        if not legacy_commands:
            errors.append(f"{command_id}: legacy_commands must contain at least one command")

        native_command = str(command.get("native_target_command") or "").strip()
        if native_command:
            native_bin = _native_command_bin(native_command)
            if native_bin in FORBIDDEN_NATIVE_COMMAND_BINS:
                errors.append(f"{command_id}: native_target_command must not route through {native_bin}")
            if native_prefix and not native_command.startswith(native_prefix):
                errors.append(f"{command_id}: native_target_command must start with {native_prefix!r}")

        referenced_inventory_ids = _as_string_list(command.get("phase1_inventory_ids"))
        if not referenced_inventory_ids:
            errors.append(f"{command_id}: phase1_inventory_ids must contain at least one id")
        for item_id in referenced_inventory_ids:
            item = inventory_by_id.get(item_id)
            if item is None:
                errors.append(f"{command_id}: references unknown Phase 1 inventory id {item_id}")
                continue
            if item.get("category") not in TRANSITIONAL_CATEGORIES:
                errors.append(f"{command_id}: Phase 1 inventory id {item_id} is not transitional/legacy device debt")
            covered_inventory_ids.add(item_id)
            if status == "native" and item.get("category") in TRANSITIONAL_CATEGORIES:
                errors.append(f"{command_id}: cannot be native while Phase 1 inventory id {item_id} is still {item.get('category')}")

        provider_values = _providers(command.get("providers"))
        if not provider_values:
            errors.append(f"{command_id}: providers must be 'all' or a non-empty list")
        provider_ownership = str(command.get("provider_binary_ownership") or "").strip()
        if provider_ownership not in VALID_PROVIDER_OWNERSHIP:
            errors.append(f"{command_id}: provider_binary_ownership must be one of {sorted(VALID_PROVIDER_OWNERSHIP)}")
        if provider_values != ["all"] and provider_ownership != "user_owned":
            errors.append(f"{command_id}: provider command plans must keep provider binaries user_owned")

        token_policy = str(command.get("token_policy") or "").strip()
        if token_policy not in VALID_TOKEN_POLICIES:
            errors.append(f"{command_id}: token_policy must be one of {sorted(VALID_TOKEN_POLICIES)}")
        if token_policy == "argv":
            errors.append(f"{command_id}: token_policy must not be argv")

        cwd_policy = str(command.get("cwd_policy") or "").strip()
        if cwd_policy not in VALID_CWD_POLICIES:
            errors.append(f"{command_id}: cwd_policy must be one of {sorted(VALID_CWD_POLICIES)}")
        if provider_values != ["all"] and cwd_policy == "not_applicable":
            errors.append(f"{command_id}: provider command plans must declare a concrete cwd_policy")

    missing = sorted(transitional_inventory_ids - covered_inventory_ids)
    for item_id in missing:
        errors.append(f"Phase 1 transitional inventory id {item_id} has no native device entrypoint plan")

    return errors


def _print_report(contract: dict[str, Any]) -> None:
    owner = contract.get("native_owner", {})
    print("native device entrypoint plan")
    print("")
    print(f"- owner: {owner.get('binary')} {owner.get('namespace')} ({owner.get('status')})")
    print("- compatibility shims:")
    for shim in contract.get("compatibility_shims", []):
        print(f"  - {shim.get('script')} -> {shim.get('delegates_to')} ({shim.get('status')})")
    print("- command groups:")
    for command in contract.get("commands", []):
        print(
            f"  - {command.get('id')}: {command.get('native_target_command')} "
            f"({command.get('status')}, {command.get('implementation_phase')})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_repo_root())
    parser.add_argument("--contract", type=Path, default=None, help="JSON contract override for tests")
    parser.add_argument("--json", action="store_true", help="Emit JSON result instead of text report")
    args = parser.parse_args()

    root = args.root.resolve()
    contract_path = args.contract or root / "config/native_device_entrypoints.json"
    contract = _load_contract(contract_path)
    errors = _validate_contract(root, contract)
    if args.json:
        print(json.dumps({"contract": contract, "errors": errors}, indent=2))
    else:
        _print_report(contract)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("native device entrypoint check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
