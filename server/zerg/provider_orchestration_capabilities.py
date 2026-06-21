from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def provider_orchestration_capability_manifest() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "config" / "provider_orchestration_capabilities.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_manifest(payload)
    return payload


def provider_orchestration_capabilities(provider: str) -> dict[str, dict[str, str]]:
    providers = provider_orchestration_capability_manifest()["providers"]
    return dict(providers.get(provider, {}))


def _validate_manifest(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("provider orchestration capability manifest root must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("provider orchestration capability manifest schema_version must be 1")

    capabilities = payload.get("capabilities")
    states = payload.get("states")
    providers = payload.get("providers")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities):
        raise ValueError("provider orchestration capabilities must be non-empty strings")
    if not isinstance(states, list) or not all(isinstance(item, str) and item for item in states):
        raise ValueError("provider orchestration states must be non-empty strings")
    if not isinstance(providers, dict) or not providers:
        raise ValueError("provider orchestration providers must be a non-empty object")

    capability_set = set(capabilities)
    state_set = set(states)
    for provider, table in providers.items():
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider orchestration provider names must be non-empty strings")
        if not isinstance(table, dict):
            raise ValueError(f"provider orchestration {provider}: capability table must be an object")
        missing = capability_set - set(table)
        extra = set(table) - capability_set
        if missing:
            raise ValueError(f"provider orchestration {provider}: missing capabilities {sorted(missing)}")
        if extra:
            raise ValueError(f"provider orchestration {provider}: unknown capabilities {sorted(extra)}")
        for capability, entry in table.items():
            if not isinstance(entry, dict):
                raise ValueError(f"provider orchestration {provider}.{capability}: entry must be an object")
            state = entry.get("state")
            source = entry.get("source")
            if state not in state_set:
                raise ValueError(f"provider orchestration {provider}.{capability}: invalid state {state!r}")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"provider orchestration {provider}.{capability}: source must be non-empty")
