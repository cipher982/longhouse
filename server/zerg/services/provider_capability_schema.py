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

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


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
    provider: str
    capability: str
    oracle_source: str
    acceptable_evidence: tuple[str, ...]
    max_age_seconds: int


def _load_schema() -> dict:
    schema_path = _resolve_schema_path()
    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
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
                out.append(
                    CapabilityAssertion(
                        scenario_id=assertion["scenario_id"],
                        assertion_id=assertion["id"],
                        variant=assertion.get("variant"),
                        provider=provider,
                        capability=capability,
                        oracle_source=assertion["oracle_source"],
                        acceptable_evidence=tuple(assertion.get("acceptable_evidence") or ()),
                        max_age_seconds=int(assertion["max_age_seconds"]),
                    )
                )
    return tuple(out)


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
