#!/usr/bin/env python3
"""Inventory Longhouse Python still used by the on-device provider-control path."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_INVENTORY: tuple[dict[str, Any], ...] = (
    {
        "id": "cli-main-provider-entrypoints",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/main.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase2",
        "reason": "Python Typer app still owns normal longhouse provider command registration.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "cli-common-scaffold",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/_common.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase2",
        "reason": "Shared Python CLI helpers still back device command behavior.",
        "device_command": False,
        "python_dependency_kind": "shared_scaffold",
    },
    {
        "id": "cli-launch-ui-scaffold",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/_launch_ui.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase2",
        "reason": "Shared Python launch UI helpers still back managed provider launch flows.",
        "device_command": False,
        "python_dependency_kind": "shared_scaffold",
    },
    {
        "id": "cli-managed-contract-scaffold",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/_managed_contract.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase2",
        "reason": "Shared Python managed-contract helpers still back provider command UX.",
        "device_command": False,
        "python_dependency_kind": "shared_scaffold",
    },
    {
        "id": "claude-launch-wrapper",
        "category": "transitional_device",
        "provider": "claude",
        "path": "server/zerg/cli/claude.py",
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Python still owns the human longhouse claude UX and Claude hook/channel config preflight.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "claude-remote-launch-native",
        "category": "native_device",
        "provider": "claude",
        "path": "engine/src/claude_channel_launch.rs",
        "symbol": "launch_detached",
        "native_dispatch_symbols": [
            "ClaudeChannelLaunchConfig",
            "build_launch_command_plan",
            "launch_detached",
        ],
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Machine Agent remote Claude launch now spawns stock Claude and waits for channel state in Rust.",
        "device_command": True,
    },
    {
        "id": "claude-channel-server-native",
        "category": "native_device",
        "provider": "claude",
        "path": "engine/src/claude_channel_server.rs",
        "symbol": "run",
        "native_dispatch_symbols": [
            "ClaudeChannelServeConfig",
            "run",
        ],
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Claude channel MCP server used by native remote launch now runs inside longhouse-engine.",
        "device_command": True,
    },
    {
        "id": "claude-channel-bridge",
        "category": "legacy_compat",
        "provider": "claude",
        "path": "server/zerg/cli/claude_channel.py",
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Python compatibility claude-channel CLI remains, but live serve/send/interrupt/inspect dispatch to longhouse-engine.",
        "device_command": True,
        "python_dependency_kind": "adapter",
    },
    {
        "id": "claude-channel-control-cli-native",
        "category": "native_device",
        "provider": "claude",
        "path": "engine/src/main.rs",
        "symbol": "ClaudeChannelCommands",
        "native_dispatch_symbols": [
            "ClaudeChannelCommands::Send",
            "ClaudeChannelCommands::Interrupt",
            "ClaudeChannelCommands::Inspect",
        ],
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Human/local claude-channel send, interrupt, and inspect compatibility commands now resolve to native longhouse-engine subcommands.",
        "device_command": True,
    },
    {
        "id": "claude-channel-helpers",
        "category": "transitional_device",
        "provider": "claude",
        "path": "server/zerg/services/claude_channel_bridge.py",
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Python still backs human-launch command construction, channel config helpers, and compatibility state path helpers.",
        "device_command": True,
        "python_dependency_kind": "adapter",
    },
    {
        "id": "claude-channel-text-server-projection",
        "category": "server_only",
        "provider": "claude",
        "path": "server/zerg/services/claude_channel_text.py",
        "owner_area": "runtime-host",
        "replacement_phase": "server-track",
        "reason": "Runtime Host text projection helper, not a device provider-control entrypoint.",
        "device_command": False,
    },
    {
        "id": "codex-launch-wrapper",
        "category": "transitional_device",
        "provider": "codex",
        "path": "server/zerg/cli/codex.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase4",
        "reason": "Python still owns the human Codex managed launch and attach wrapper.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "opencode-launch-wrapper",
        "category": "transitional_device",
        "provider": "opencode",
        "path": "server/zerg/cli/opencode.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase5",
        "reason": "Python still owns the local OpenCode managed launch UX.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "opencode-channel-compat",
        "category": "legacy_compat",
        "provider": "opencode",
        "path": "server/zerg/cli/opencode_channel.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase5",
        "reason": "Compatibility CLI remains for attach and local bridge operations while native entrypoint is designed.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "opencode-bridge-cli",
        "category": "transitional_device",
        "provider": "opencode",
        "path": "server/zerg/cli/opencode_bridge.py",
        "owner_area": "native-device-entrypoint",
        "replacement_phase": "phase5",
        "reason": "Python bridge helper remains reachable from the device CLI path.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "opencode-bridge-state",
        "category": "server_only",
        "provider": "opencode",
        "path": "server/zerg/services/opencode_bridge_state.py",
        "owner_area": "runtime-host",
        "replacement_phase": "server-track",
        "reason": "Runtime Host/server helper for bridge-state shape; native state handling lives in engine.",
        "device_command": False,
    },
    {
        "id": "antigravity-launch-wrapper",
        "category": "transitional_device",
        "provider": "antigravity",
        "path": "server/zerg/cli/antigravity.py",
        "owner_area": "antigravity-decision",
        "replacement_phase": "phase6",
        "reason": "Python still owns managed Antigravity launch and hook environment setup.",
        "device_command": True,
        "python_dependency_kind": "entrypoint",
    },
    {
        "id": "antigravity-channel",
        "category": "transitional_device",
        "provider": "antigravity",
        "path": "server/zerg/cli/antigravity_channel.py",
        "owner_area": "antigravity-decision",
        "replacement_phase": "phase6",
        "reason": "Python still owns Antigravity hook-inbox send.",
        "device_command": True,
        "python_dependency_kind": "control_shellout",
    },
    {
        "id": "antigravity-hook-inbox",
        "category": "transitional_device",
        "provider": "antigravity",
        "path": "server/zerg/services/antigravity_hook_inbox.py",
        "owner_area": "antigravity-decision",
        "replacement_phase": "phase6",
        "reason": "Python installs and manages the hook-inbox adapter used by agy.",
        "device_command": True,
        "python_dependency_kind": "control_shellout",
    },
    {
        "id": "local-health-cli-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/local_health.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python local-health CLI is still part of the device status/repair surface.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "local-health-fast-cli-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/local_health_fast.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python longhouse-local-health console script backs the macOS menu bar refresh loop.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "local-health-service-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/services/local_health.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python local-health service backs the device doctor/repair surface.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "desktop-app-health-launcher-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/services/desktop_app.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python desktop app helper launches the menu-bar local-health snapshot command.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "doctor-cli-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/doctor.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python doctor CLI is still part of device repair UX.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "machine-cli-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/machine.py",
        "owner_area": "native-health-repair",
        "replacement_phase": "phase7",
        "reason": "Python machine CLI is still the configured-machine repair/reconciliation entrypoint.",
        "device_command": True,
        "python_dependency_kind": "health_repair",
    },
    {
        "id": "provider-live-cli-python",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/provider_live.py",
        "owner_area": "native-proof",
        "replacement_phase": "phase7",
        "reason": "Python provider-live proof CLI still runs managed-provider live canaries from the device.",
        "device_command": True,
        "python_dependency_kind": "proof",
    },
    {
        "id": "control-channel-claude-shellout",
        "category": "native_device",
        "provider": "claude",
        "path": "engine/src/control_channel.rs",
        "symbol": "claude_channel_control_result",
        "native_dispatch_symbols": [
            "claude_channel_send_text",
            "claude_channel_interrupt",
            "claude_channel_control_result"
        ],
        "owner_area": "claude-native",
        "replacement_phase": "phase3",
        "reason": "Rust Machine Agent routes Claude live control through native channel-control code instead of the Python-packaged longhouse CLI.",
        "device_command": True,
    },
    {
        "id": "control-channel-antigravity-shellout",
        "category": "transitional_device",
        "provider": "antigravity",
        "path": "engine/src/control_channel.rs",
        "symbol": "run_antigravity_channel_command",
        "owner_area": "antigravity-decision",
        "replacement_phase": "phase6",
        "reason": "Rust Machine Agent shells out to the Python-packaged longhouse CLI for Antigravity send.",
        "device_command": True,
        "python_dependency_kind": "control_shellout",
    },
)

PROVIDER_CONTROL_PYTHON_GLOBS = (
    "server/zerg/cli/main.py",
    "server/zerg/cli/_common.py",
    "server/zerg/cli/_launch_ui.py",
    "server/zerg/cli/_managed_contract.py",
    "server/zerg/cli/local_health*.py",
    "server/zerg/cli/machine.py",
    "server/zerg/cli/provider_live.py",
    "server/zerg/cli/*claude*.py",
    "server/zerg/cli/*codex*.py",
    "server/zerg/cli/*opencode*.py",
    "server/zerg/cli/*antigravity*.py",
    "server/zerg/services/local_health.py",
    "server/zerg/services/desktop_app.py",
    "server/zerg/services/*claude_channel*.py",
    "server/zerg/services/*opencode*.py",
    "server/zerg/services/*antigravity*.py",
)
VALID_CATEGORIES = {"native_device", "transitional_device", "legacy_compat", "server_only", "test_only"}
DEVICE_CATEGORIES = {"native_device", "transitional_device", "legacy_compat"}
TRANSITIONAL_CATEGORIES = {"transitional_device", "legacy_compat"}
PYTHON_DEPENDENCY_KINDS = {
    "adapter",
    "control_shellout",
    "entrypoint",
    "health_repair",
    "proof",
    "shared_scaffold",
}
REMOTE_NATIVE_ONLY_KINDS = {
    "adapter",
    "entrypoint",
    "health_repair",
    "proof",
    "shared_scaffold",
}


def _load_inventory(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return [dict(item) for item in DEFAULT_INVENTORY]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("inventory override must be a JSON array")
    return [dict(item) for item in payload]


def _provider_matches(entry_provider: Any, provider: str) -> bool:
    if entry_provider == "all":
        return True
    if isinstance(entry_provider, str):
        return entry_provider == provider
    if isinstance(entry_provider, list):
        return provider in entry_provider or "all" in entry_provider
    return False


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _contract_items(root: Path) -> list[dict[str, Any]]:
    path = root / "server/zerg/config/managed_provider_contracts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    providers = payload.get("providers")
    if not isinstance(providers, list):
        raise ValueError(f"{_rel(path, root)} must contain providers[]")
    return [dict(item) for item in providers if isinstance(item, dict)]


def _packaged_console_script_modules(root: Path) -> set[str]:
    path = root / "server/pyproject.toml"
    if not path.exists():
        return set()
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    scripts = payload.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return set()

    scanned: set[str] = set()
    for target in scripts.values():
        if not isinstance(target, str):
            continue
        module = target.split(":", 1)[0].strip()
        if not module.startswith("zerg."):
            continue
        path_parts = module.split(".")
        scanned.add(("server/" + "/".join(path_parts) + ".py").replace("//", "/"))
    return scanned


def _scanned_provider_control_files(root: Path) -> set[str]:
    scanned: set[str] = set()
    for pattern in PROVIDER_CONTROL_PYTHON_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                scanned.add(_rel(path, root))
    scanned.update(path for path in _packaged_console_script_modules(root) if (root / path).is_file())
    return scanned


def _validate_inventory(root: Path, inventory: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    contracts = _contract_items(root)
    providers = {str(item.get("provider") or "").strip() for item in contracts}
    inventory_paths = {str(item.get("path") or "").strip() for item in inventory}

    seen_ids: set[str] = set()
    for item in inventory:
        item_id = str(item.get("id") or "").strip()
        category = str(item.get("category") or "").strip()
        path_value = str(item.get("path") or "").strip()
        provider = item.get("provider")
        dependency_kind = str(item.get("python_dependency_kind") or "").strip()
        if not item_id:
            errors.append("inventory entry is missing id")
            continue
        if item_id in seen_ids:
            errors.append(f"inventory id {item_id} is duplicated")
        seen_ids.add(item_id)
        if category not in VALID_CATEGORIES:
            errors.append(f"{item_id}: category {category!r} must be one of {sorted(VALID_CATEGORIES)}")
        if not path_value:
            errors.append(f"{item_id}: path is required")
        elif not (root / path_value).exists():
            errors.append(f"{item_id}: path does not exist: {path_value}")
        if provider != "all":
            provider_values = provider if isinstance(provider, list) else [provider]
            for value in provider_values:
                if value not in providers:
                    errors.append(f"{item_id}: provider {value!r} is not in managed provider manifest")
        for required in ("owner_area", "replacement_phase", "reason"):
            if not str(item.get(required) or "").strip():
                errors.append(f"{item_id}: {required} is required")
        if category in TRANSITIONAL_CATEGORIES:
            for required in ("owner_area", "replacement_phase", "reason"):
                if not str(item.get(required) or "").strip():
                    errors.append(f"{item_id}: transitional entries must include {required}")
            if not dependency_kind:
                errors.append(f"{item_id}: transitional entries must include python_dependency_kind")
            elif dependency_kind not in PYTHON_DEPENDENCY_KINDS:
                errors.append(
                    f"{item_id}: python_dependency_kind {dependency_kind!r} must be one of "
                    f"{sorted(PYTHON_DEPENDENCY_KINDS)}"
                )
        if item.get("device_command") is True and category not in DEVICE_CATEGORIES:
            errors.append(f"{item_id}: device_command=true cannot be classified as {category}")
        symbol = str(item.get("symbol") or "").strip()
        if symbol and path_value:
            text = (root / path_value).read_text(encoding="utf-8", errors="ignore") if (root / path_value).exists() else ""
            if symbol not in text:
                errors.append(f"{item_id}: symbol {symbol!r} was not found in {path_value}")
            for native_symbol in _as_string_list(item.get("native_dispatch_symbols")):
                if native_symbol not in text:
                    errors.append(f"{item_id}: native dispatch symbol {native_symbol!r} was not found in {path_value}")

    for path in sorted(_scanned_provider_control_files(root)):
        if path not in inventory_paths:
            errors.append(f"{path} is provider-control Python but is missing from no-Python device inventory")

    for contract in contracts:
        provider = str(contract.get("provider") or "").strip()
        entries = [item for item in inventory if _provider_matches(item.get("provider"), provider)]
        stance_entries = [item for item in entries if item.get("category") in DEVICE_CATEGORIES]
        if not stance_entries:
            errors.append(f"provider {provider} has no no-Python device inventory stance")
        if contract.get("requires_longhouse_cli") is True:
            transitional = [item for item in entries if item.get("category") in TRANSITIONAL_CATEGORIES]
            if not transitional:
                errors.append(f"provider {provider} requires_longhouse_cli=true but has no transitional_device inventory entry")
        else:
            native_remote_blockers = [
                item
                for item in entries
                if item.get("category") in TRANSITIONAL_CATEGORIES
                and item.get("python_dependency_kind") not in REMOTE_NATIVE_ONLY_KINDS
            ]
            for item in native_remote_blockers:
                errors.append(
                    f"provider {provider} has requires_longhouse_cli=false but {item['id']} is "
                    f"{item.get('python_dependency_kind')}; use requires_longhouse_cli only for "
                    "engine remote-control shellout requirements"
                )

    return errors


def _print_report(root: Path, inventory: list[dict[str, Any]]) -> None:
    contracts = _contract_items(root)
    print("no-Python device path inventory")
    print("")
    for contract in contracts:
        provider = str(contract.get("provider") or "").strip()
        entries = [item for item in inventory if _provider_matches(item.get("provider"), provider)]
        transitional = [item for item in entries if item.get("category") in TRANSITIONAL_CATEGORIES]
        native = [item for item in entries if item.get("category") == "native_device"]
        status = "native" if native and not transitional else "transitional"
        print(f"- {provider}: {status}; requires_longhouse_cli={bool(contract.get('requires_longhouse_cli'))}")
        for item in entries:
            if item.get("category") in {"server_only", "test_only"}:
                continue
            suffix = f"::{item['symbol']}" if item.get("symbol") else ""
            dependency = item.get("python_dependency_kind")
            dependency_suffix = f", {dependency}" if dependency else ""
            print(f"  - {item['category']}: {item['path']}{suffix} ({item['replacement_phase']}{dependency_suffix})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--inventory", type=Path, default=None, help="JSON inventory override for tests")
    parser.add_argument("--json", action="store_true", help="Emit inventory JSON instead of text report")
    args = parser.parse_args()

    root = args.root.resolve()
    inventory = _load_inventory(args.inventory)
    errors = _validate_inventory(root, inventory)
    if args.json:
        print(json.dumps({"inventory": inventory, "errors": errors}, indent=2))
    else:
        _print_report(root, inventory)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("no-Python device path check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
