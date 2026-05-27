from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_REQUIRED_STRING_FIELDS = (
    "provider",
    "provider_cli_binary",
    "managed_transport",
    "control_plane",
)
_REQUIRED_BOOL_FIELDS = (
    "requires_longhouse_cli",
    "launch_local",
    "launch_remote",
    "reattach",
    "send_input",
    "interrupt",
    "steer_active_turn",
    "terminate",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "can_resume",
)
_STRING_LIST_FIELDS = ("control_plane_aliases", "machine_control_supports")


def _validate_string_field(item: dict[str, Any], field: str) -> None:
    if not isinstance(item.get(field), str) or not str(item.get(field)).strip():
        provider = item.get("provider") or "<unknown>"
        raise ValueError(f"managed provider contract {provider}: {field} must be a non-empty string")


def _validate_bool_field(item: dict[str, Any], field: str) -> None:
    if not isinstance(item.get(field), bool):
        provider = item.get("provider") or "<unknown>"
        raise ValueError(f"managed provider contract {provider}: {field} must be a boolean")


def _validate_string_list_field(item: dict[str, Any], field: str) -> None:
    value = item.get(field)
    if not isinstance(value, list) or not all(isinstance(entry, str) and entry.strip() for entry in value):
        provider = item.get("provider") or "<unknown>"
        raise ValueError(f"managed provider contract {provider}: {field} must be a list of non-empty strings")


@lru_cache(maxsize=1)
def managed_provider_contract_manifest() -> dict[str, Any]:
    contract_path = Path(__file__).resolve().parent / "config" / "managed_provider_contracts.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("managed provider contract manifest root must be an object")
    return payload


@lru_cache(maxsize=1)
def managed_provider_contract_items() -> tuple[dict[str, Any], ...]:
    providers = managed_provider_contract_manifest().get("providers")
    if not isinstance(providers, list):
        raise ValueError("managed provider contract manifest must contain providers[]")
    items: list[dict[str, Any]] = []
    for item in providers:
        if not isinstance(item, dict):
            raise ValueError("managed provider contract provider entries must be objects")
        for field in _REQUIRED_STRING_FIELDS:
            _validate_string_field(item, field)
        if "provider_cli_env" in item and item["provider_cli_env"] is not None:
            _validate_string_field(item, "provider_cli_env")
        for field in _REQUIRED_BOOL_FIELDS:
            _validate_bool_field(item, field)
        for field in _STRING_LIST_FIELDS:
            _validate_string_list_field(item, field)
        items.append(dict(item))
    return tuple(items)
