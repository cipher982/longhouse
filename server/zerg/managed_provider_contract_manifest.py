from __future__ import annotations

import hashlib
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
    "run_once",
    "reattach",
    "send_input",
    "interrupt",
    "steer_active_turn",
    "answer_pause",
    "terminate",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "startup_coordination_context",
    "can_resume",
    "turn_start",
)
_STRING_LIST_FIELDS = ("control_plane_aliases", "machine_control_supports")
_OPERATION_EVIDENCE_FIELDS = (
    "launch_local",
    "run_once",
    "reattach",
    "send_input",
    "interrupt",
    "steer_active_turn",
    "answer_pause",
    "terminate",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "turn_start",
)
MACHINE_CONTROL_SUPPORT_OPERATION_BY_SUFFIX = {
    "send": "send_input",
    "interrupt": "interrupt",
    "steer": "steer_active_turn",
    "answer_pause": "answer_pause",
    "terminate": "terminate",
    "run_once": "run_once",
    "resume_run_once": "run_once",
    "turn_start": "turn_start",
    "turn_interrupt": "interrupt",
}
_MACHINE_CONTROL_SUPPORT_EXTRA_REQUIREMENTS = {
    "resume_run_once": ("can_resume",),
}
_OPERATION_EVIDENCE_LEVELS = frozenset(
    {
        "none",
        "source_review",
        "hermetic",
        "live_no_token",
        "live_token",
    }
)
_CAPABILITY_DISPOSITIONS = frozenset({"implemented", "not_implemented", "upstream_absent", "policy_disabled"})
_CAPABILITY_ACTION_GATES = frozenset({"ceiling", "warn", "strict"})
_CAPABILITY_EVIDENCE_CLASSES = frozenset({"hermetic", "live_no_token", "live_token"})
_CAPABILITY_REASON_CODES = frozenset(
    {"semantic_proof_missing", "upstream_unavailable", "upstream_unknown", "longhouse_unimplemented", "policy_disabled"}
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
    extra = [field for field in evidence if field not in _OPERATION_EVIDENCE_FIELDS]
    if extra:
        joined = ", ".join(str(field) for field in extra)
        raise ValueError(f"managed provider contract {provider}: operation_evidence has unknown keys {joined}")
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


def _validate_machine_control_supports(item: dict[str, Any]) -> None:
    provider = str(item.get("provider") or "<unknown>")
    for support in item.get("machine_control_supports") or ():
        prefix, separator, suffix = str(support).partition(".")
        if separator != "." or not prefix or not suffix:
            raise ValueError(
                f"managed provider contract {provider}: machine_control_supports entry {support!r} " "must be provider.operation"
            )
        if prefix != provider:
            raise ValueError(
                f"managed provider contract {provider}: machine_control_supports entry {support!r} " f"must use provider prefix {provider}"
            )
        operation = MACHINE_CONTROL_SUPPORT_OPERATION_BY_SUFFIX.get(suffix)
        if operation is None:
            raise ValueError(
                f"managed provider contract {provider}: machine_control_supports entry {support!r} " f"has unknown operation {suffix!r}"
            )
        if item.get(operation) is not True:
            raise ValueError(
                f"managed provider contract {provider}: machine_control_supports entry {support!r} " f"requires {operation}=true"
            )
        for extra_operation in _MACHINE_CONTROL_SUPPORT_EXTRA_REQUIREMENTS.get(suffix, ()):
            if item.get(extra_operation) is not True:
                raise ValueError(
                    f"managed provider contract {provider}: machine_control_supports entry {support!r} " f"requires {extra_operation}=true"
                )


def _validate_capabilities(item: dict[str, Any]) -> None:
    provider = str(item.get("provider") or "<unknown>")
    capabilities = item.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise ValueError(f"managed provider contract {provider}: capabilities must be an object")
    for capability_id, declaration in capabilities.items():
        prefix = f"managed provider contract {provider}: capabilities.{capability_id}"
        if not isinstance(capability_id, str) or "." not in capability_id:
            raise ValueError(f"{prefix} must use a dotted semantic ID")
        if not isinstance(declaration, dict):
            raise ValueError(f"{prefix} must be an object")
        if declaration.get("disposition") not in _CAPABILITY_DISPOSITIONS:
            raise ValueError(f"{prefix}.disposition must be one of {sorted(_CAPABILITY_DISPOSITIONS)}")
        if declaration.get("action_gate") not in _CAPABILITY_ACTION_GATES:
            raise ValueError(f"{prefix}.action_gate must be one of {sorted(_CAPABILITY_ACTION_GATES)}")
        _validate_capability_string(prefix, declaration, "reason_code")
        if declaration["reason_code"] not in _CAPABILITY_REASON_CODES:
            raise ValueError(f"{prefix}.reason_code is unknown")
        _validate_capability_string(prefix, declaration, "policy_key")
        contexts = declaration.get("contexts", {})
        if not isinstance(contexts, dict):
            raise ValueError(f"{prefix}.contexts must be an object")
        modes = contexts.get("modes", [])
        if not isinstance(modes, list) or not all(isinstance(mode, str) and mode for mode in modes):
            raise ValueError(f"{prefix}.contexts.modes must be a string list")
        assertions = declaration.get("required_assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError(f"{prefix}.required_assertions must be a non-empty list")
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise ValueError(f"{prefix}.required_assertions entries must be objects")
            for field in ("id", "scenario_id"):
                _validate_capability_string(f"{prefix}.required_assertions", assertion, field)
            revision = assertion.get("minimum_scenario_revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError(f"{prefix}.required_assertions minimum_scenario_revision must be positive")
            evidence = assertion.get("acceptable_evidence")
            if not isinstance(evidence, list) or not evidence or not set(evidence) <= _CAPABILITY_EVIDENCE_CLASSES:
                raise ValueError(f"{prefix}.required_assertions acceptable_evidence is invalid")
            max_age = assertion.get("max_age_seconds")
            if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 1:
                raise ValueError(f"{prefix}.required_assertions max_age_seconds must be positive")


def _validate_capability_string(prefix: str, payload: dict[str, Any], field: str) -> None:
    if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
        raise ValueError(f"{prefix}.{field} must be a non-empty string")


@lru_cache(maxsize=1)
def managed_provider_contract_manifest() -> dict[str, Any]:
    contract_path = Path(__file__).resolve().parent / "config" / "managed_provider_contracts.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    return normalize_contract_manifest(payload)


def normalize_contract_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("managed provider contract manifest root must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("managed provider contract manifest schema_version must be 1")
    providers = payload.get("providers")
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
        _validate_machine_control_supports(item)
        _validate_capabilities(item)
        items.append(dict(item))
    return {
        "schema_version": 1,
        "providers": items,
    }


def render_contract_manifest_json(payload: dict[str, Any]) -> str:
    return json.dumps(normalize_contract_manifest(payload), indent=2, ensure_ascii=False) + "\n"


@lru_cache(maxsize=1)
def managed_provider_contract_items() -> tuple[dict[str, Any], ...]:
    providers = managed_provider_contract_manifest().get("providers")
    if not isinstance(providers, list):
        raise ValueError("managed provider contract manifest must contain providers[]")
    items: list[dict[str, Any]] = []
    for item in providers:
        if not isinstance(item, dict):
            raise ValueError("managed provider contract provider entries must be objects")
        items.append(dict(item))
    return tuple(items)


def managed_provider_contract_entry_digest(provider: str) -> str:
    item = next((item for item in managed_provider_contract_items() if item.get("provider") == provider), None)
    if item is None:
        raise ValueError(f"unknown managed provider: {provider}")
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
