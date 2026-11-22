"""What schemas/managed_providers.yml declares: the capability -> assertion mapping.

Read from the repository checkout by the provider qualification tooling and its
tests. The Runtime Host image and the wheel do not ship the schema or read it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _resolve_schema_path() -> Path:
    path = ROOT / "schemas" / "managed_providers.yml"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found; the provider contract is read from a repository checkout")
    return path


@dataclass(frozen=True)
class CapabilityAssertion:
    scenario_id: str
    assertion_id: str
    variant: str | None
    minimum_scenario_revision: int
    provider: str
    capability: str
    oracle_source: str
    acceptable_evidence: tuple[str, ...]
    max_age_seconds: int
    required_evidence_classes: tuple[str, ...] | None = None
    max_age_seconds_by_evidence_class: tuple[tuple[str, int], ...] = ()
    disposition: str = "implemented"
    assurance_priority: str = "release_gate"

    def max_age_for_evidence_class(self, evidence_class: str) -> int:
        for configured_class, age in self.max_age_seconds_by_evidence_class:
            if configured_class == evidence_class:
                return min(self.max_age_seconds, age)
        return self.max_age_seconds


def _evidence_class_fields(assertion: Mapping[str, Any]) -> tuple[tuple[str, ...] | None, tuple[tuple[str, int], ...]]:
    acceptable = assertion.get("acceptable_evidence") or ()
    required = assertion.get("required_evidence_classes")
    required_classes: tuple[str, ...] | None = None
    if required is not None:
        if not isinstance(required, list) or not required or any(not isinstance(item, str) or not item for item in required):
            raise ValueError("required_evidence_classes must be a non-empty string list")
        if len(set(required)) != len(required) or not set(required) <= set(acceptable):
            raise ValueError("required_evidence_classes must be unique and accepted")
        required_classes = tuple(required)
    configured = assertion.get("max_age_seconds_by_evidence_class")
    if configured is None:
        if required_classes is not None:
            raise ValueError("max_age_seconds_by_evidence_class is required with required_evidence_classes")
        return required_classes, ()
    if required_classes is None or not isinstance(configured, Mapping) or set(configured) != set(required_classes):
        raise ValueError("max_age_seconds_by_evidence_class must cover required_evidence_classes")
    ages: list[tuple[str, int]] = []
    for evidence_class, age in sorted(configured.items()):
        if not isinstance(age, int) or isinstance(age, bool) or age < 1:
            raise ValueError("per-class max age must be a positive integer")
        ages.append((evidence_class, age))
    return required_classes, tuple(ages)


def _load_schema() -> dict:
    schema_path = _resolve_schema_path()
    # libyaml parses the ~120 KB schema in ~10 ms against ~80 ms for the
    # pure-Python loader; the result is identical.
    payload = yaml.load(schema_path.read_text(encoding="utf-8"), Loader=_SAFE_LOADER)
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise SystemExit(f"{schema_path} must contain a YAML mapping with a top-level 'providers' list")
    return payload


def _iter_capabilities(node: object, prefix: str = "") -> list[tuple[str, dict]]:
    """Walk the schema's nested capability tree, yielding (dotted_key, entry)
    for every dict that declares `required_assertions`."""
    out: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if "required_assertions" in node:
            out.append((prefix, node))
        for key, value in node.items():
            if key == "required_assertions":
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            out.extend(_iter_capabilities(value, child_prefix))
    return out


def _load_capability_assertions() -> tuple[CapabilityAssertion, ...]:
    schema = _load_schema()
    out: list[CapabilityAssertion] = []
    for provider_entry in schema["providers"]:
        provider = provider_entry["provider"]
        capabilities = provider_entry.get("capabilities") or {}
        for capability, capability_entry in _iter_capabilities(capabilities):
            for assertion in capability_entry.get("required_assertions") or []:
                required_evidence_classes, class_ages = _evidence_class_fields(assertion)
                out.append(
                    CapabilityAssertion(
                        scenario_id=assertion["scenario_id"],
                        assertion_id=assertion["id"],
                        variant=assertion.get("variant"),
                        minimum_scenario_revision=int(assertion["minimum_scenario_revision"]),
                        provider=provider,
                        capability=capability,
                        oracle_source=assertion["oracle_source"],
                        acceptable_evidence=tuple(assertion.get("acceptable_evidence") or ()),
                        max_age_seconds=int(assertion["max_age_seconds"]),
                        required_evidence_classes=required_evidence_classes,
                        max_age_seconds_by_evidence_class=class_ages,
                        disposition=str(capability_entry.get("disposition") or "implemented"),
                        assurance_priority=str(assertion.get("assurance_priority") or "release_gate"),
                    )
                )
    return tuple(out)


def load_capability_assertions() -> tuple[CapabilityAssertion, ...]:
    return _load_capability_assertions()
