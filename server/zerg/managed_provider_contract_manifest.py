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
_OPERATION_EVIDENCE_FIELDS = (
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
)
_OPERATION_EVIDENCE_LEVELS = frozenset(
    {
        "none",
        "source_review",
        "hermetic",
        "live_no_token",
        "manual_live_token",
        "scheduled_live_token",
    }
)


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


def _validate_operation_evidence(item: dict[str, Any]) -> None:
    provider = item.get("provider") or "<unknown>"
    evidence = item.get("operation_evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"managed provider contract {provider}: operation_evidence must be an object")
    missing = [field for field in _OPERATION_EVIDENCE_FIELDS if field not in evidence]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"managed provider contract {provider}: operation_evidence missing {joined}")
    for field in _OPERATION_EVIDENCE_FIELDS:
        entry = evidence.get(field)
        if not isinstance(entry, dict):
            raise ValueError(f"managed provider contract {provider}: operation_evidence.{field} must be an object")
        level = entry.get("level")
        source = entry.get("source")
        if level not in _OPERATION_EVIDENCE_LEVELS:
            raise ValueError(
                f"managed provider contract {provider}: operation_evidence.{field}.level must be one of "
                f"{sorted(_OPERATION_EVIDENCE_LEVELS)}"
            )
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"managed provider contract {provider}: operation_evidence.{field}.source must be a non-empty string")
        if item.get(field) is True and level == "none":
            raise ValueError(f"managed provider contract {provider}: supported operation {field} cannot have evidence level none")
        if item.get(field) is False and level != "none":
            raise ValueError(f"managed provider contract {provider}: unsupported operation {field} must have evidence level none")
        if "next" in entry and (not isinstance(entry["next"], str) or not entry["next"].strip()):
            raise ValueError(f"managed provider contract {provider}: operation_evidence.{field}.next must be a non-empty string")


@lru_cache(maxsize=1)
def managed_provider_contract_manifest() -> dict[str, Any]:
    contract_path = Path(__file__).resolve().parent / "config" / "managed_provider_contracts.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("managed provider contract manifest root must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("managed provider contract manifest schema_version must be 1")
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
        _validate_operation_evidence(item)
        items.append(dict(item))
    return tuple(items)
