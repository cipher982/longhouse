"""What schemas/managed_providers.yml declares, loaded for the Runtime Host.

This is the narrow slice of the provider contract that a Runtime Host serving
real traffic needs: the declared capability -> assertion mapping behind
`GET /api/agents/provider-capabilities`. It lives under `zerg/services/`
rather than `zerg/qa/` because `zerg/qa/` is the provider factory's test
machinery and is deliberately excluded from the published wheel (see
`scripts/qa/check-wheel.py`); a mounted router must not import from a package
that does not ship.

`zerg/qa/provider_factory_model.py` re-exports everything here, so the factory
keeps its single import surface. Everything the factory needs *beyond* the
declared contract -- build provenance, qualification profiles, harness
scenarios, credential policy, the plan model -- stays there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _resolve_schema_path() -> Path:
    """Where schemas/managed_providers.yml actually lives, in each of the
    three environments this module runs in.

    Found live in production (2026-07-29): this repo's local/CI checkout has
    server/zerg/<pkg>/<this file>, four levels below the repo root, so
    `ROOT = parents[3]` lands on the repo root and `ROOT / "schemas" / ...`
    is correct there. The deployed Runtime Host image is not a full repo
    checkout -- docker/runtime.dockerfile copies only server/'s *contents*
    into /app (so this file lives at /app/zerg/<pkg>/..., one level
    shallower), which makes the exact same `parents[3]` arithmetic land on
    `/` by accident, and schemas/ was never copied there at all -- a real
    FileNotFoundError the first live call to this endpoint hit. Explicit
    candidates instead of relying on that directory-depth coincidence to
    keep meaning "the repo root" in two structurally different layouts.

    The third candidate is the pip-installed wheel, which is neither of the
    above: no repo checkout above it and no /schemas. pyproject force-includes
    the schema at `zerg/_config/managed_providers.yml` for exactly this case.
    Without it every `pip install longhouse` self-hoster's capability endpoint
    500s on FileNotFoundError the same way the hosted image did.
    """
    candidates = _schema_path_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("schemas/managed_providers.yml not found at any known location: " + ", ".join(str(c) for c in candidates))


def _schema_path_candidates() -> tuple[Path, ...]:
    return (
        ROOT / "schemas" / "managed_providers.yml",  # local dev / CI checkout
        Path("/schemas/managed_providers.yml"),  # deployed runtime image (docker/runtime.dockerfile)
        PACKAGE_ROOT / "_config" / "managed_providers.yml",  # pip-installed wheel (pyproject force-include)
    )


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


def _edge_assertion(provider: str, capability: str, disposition: str, assertion: dict) -> CapabilityAssertion:
    scalar_age = int(assertion["max_age_seconds"])
    class_ages = dict(_evidence_class_fields(assertion)[1])
    live_age = min(scalar_age, class_ages.get("live_token", scalar_age))
    return CapabilityAssertion(
        scenario_id=assertion["scenario_id"],
        assertion_id=assertion["id"],
        variant=assertion.get("variant"),
        minimum_scenario_revision=int(assertion["minimum_scenario_revision"]),
        provider=provider,
        capability=capability,
        oracle_source=assertion["oracle_source"],
        acceptable_evidence=("live_token",),
        max_age_seconds=live_age,
        required_evidence_classes=("live_token",),
        max_age_seconds_by_evidence_class=(("live_token", live_age),),
        disposition=disposition,
        assurance_priority=str(assertion.get("assurance_priority") or "release_gate"),
    )


def load_chip_edge_assertions() -> dict[str, dict[str, tuple[CapabilityAssertion, ...] | None]]:
    """Provider -> landing chip -> the live-token assertions behind it.

    None marks a chip with no proof edge at all. Operation edges carry the
    capability label ``operation.<name>`` so a projection row names where its
    requirement was declared.
    """

    from zerg.provider_contract.provider_chip_edges import CHIP_CAPABILITIES
    from zerg.provider_contract.provider_chip_edges import CHIP_OPERATIONS
    from zerg.provider_contract.provider_chip_edges import chip_edges

    out: dict[str, dict[str, tuple[CapabilityAssertion, ...] | None]] = {}
    for entry in _load_schema()["providers"]:
        provider = str(entry["provider"])
        chips: dict[str, tuple[CapabilityAssertion, ...] | None] = {}
        for chip, assertions in chip_edges(entry).items():
            if assertions is None:
                chips[chip] = None
                continue
            if chip in CHIP_CAPABILITIES:
                labels = [CHIP_CAPABILITIES[chip]] * len(assertions)
            else:
                labels = [
                    f"operation.{operation}"
                    for operation in CHIP_OPERATIONS[chip]
                    for _ in ((entry.get("operation_evidence") or {}).get(operation) or {}).get("required_assertions") or ()
                ]
            chips[chip] = tuple(
                _edge_assertion(provider, label, "implemented", dict(assertion))
                for label, assertion in zip(labels, assertions, strict=True)
            )
        out[provider] = chips
    return out


def load_capability_assertions() -> tuple[CapabilityAssertion, ...]:
    """Public, narrow entry point for callers that only need the declared
    capability -> assertion mapping (schemas/managed_providers.yml), not the
    rest of `load_facts()`'s I/O (Makefile parsing, the weekly release
    schedule). Used by the live capability-projection endpoint
    (zerg/routers/provider_capability_proofs.py) -- a Runtime Host serving
    real traffic has no reason to depend on the Makefile or the weekly-cron
    schedule file being present just to answer "what does the contract
    declare," and `load_facts()`'s other three I/O calls have their own,
    separate real-environment assumptions this endpoint should not inherit
    silently."""
    return _load_capability_assertions()
