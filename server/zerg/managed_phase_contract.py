from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ManagedPhaseDefinition:
    raw_phase: str
    display_label: str
    attention: str
    tool_display_format: str | None = None
    local_health_only: bool = False

    @property
    def normalized_raw_phase(self) -> str:
        return self.raw_phase.strip().lower()

    @property
    def normalized_display_label(self) -> str:
        return self.display_label.strip().lower()

    @property
    def display_prefix(self) -> str | None:
        if not self.tool_display_format or "{tool_name}" not in self.tool_display_format:
            return None
        prefix, _sep, _suffix = self.tool_display_format.partition("{tool_name}")
        normalized = prefix.lower()
        return normalized if normalized else None

    def display_for_tool(self, tool_name: str | None) -> str:
        normalized_tool = (tool_name or "").strip()
        if self.tool_display_format and normalized_tool:
            return self.tool_display_format.replace("{tool_name}", normalized_tool)
        return self.display_label


@lru_cache(maxsize=1)
def managed_phase_definitions() -> tuple[ManagedPhaseDefinition, ...]:
    contract_path = Path(__file__).resolve().parent / "config" / "managed_phase_contract.json"
    payload = json.loads(contract_path.read_text())
    return tuple(
        ManagedPhaseDefinition(
            raw_phase=item["raw_phase"],
            display_label=item["display_label"],
            attention=item["attention"],
            tool_display_format=item.get("tool_display_format"),
            local_health_only=bool(item.get("local_health_only", False)),
        )
        for item in payload["phases"]
    )


@lru_cache(maxsize=1)
def managed_phase_definition_by_raw() -> dict[str, ManagedPhaseDefinition]:
    return {item.normalized_raw_phase: item for item in managed_phase_definitions()}


def definition_for_raw_phase(raw_phase: str | None) -> ManagedPhaseDefinition | None:
    normalized_phase = (raw_phase or "").strip().lower()
    if not normalized_phase:
        return None
    return managed_phase_definition_by_raw().get(normalized_phase)


def is_known_raw_phase(raw_phase: str | None) -> bool:
    return definition_for_raw_phase(raw_phase) is not None


def display_label_for_phase(raw_phase: str | None, tool_name: str | None) -> str | None:
    normalized_phase = (raw_phase or "").strip().lower()
    if not normalized_phase:
        return None
    definition = definition_for_raw_phase(raw_phase)
    if definition is None:
        return "unknown phase"
    return definition.display_for_tool(tool_name)


def attention_for_display_phase(display_phase: str | None) -> str | None:
    normalized_display = (display_phase or "").strip().lower()
    if not normalized_display:
        return None
    for definition in managed_phase_definitions():
        if normalized_display == definition.normalized_display_label:
            return definition.attention
        prefix = definition.display_prefix
        if prefix and normalized_display.startswith(prefix):
            return definition.attention
    return None


def raw_phases() -> tuple[str, ...]:
    return tuple(item.normalized_raw_phase for item in managed_phase_definitions())
