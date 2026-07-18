#!/usr/bin/env python3
"""Validate the canonical session-state contract against shipped surfaces.

Phase 1 deliberately validates representations rather than generating a new
runtime model. ``session_state_contract.py`` remains the only projector and
OpenAPI remains the source of generated TypeScript/Swift DTOs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "session_state_contract.yml"
PYTHON_PROJECTOR = ROOT / "server" / "zerg" / "services" / "session_state_contract.py"
OPENAPI = ROOT / "openapi.json"
TYPESCRIPT = ROOT / "web" / "src" / "generated" / "openapi-types.ts"
SWIFT = ROOT / "ios" / "Sources" / "Shared" / "Generated" / "SessionAPI.generated.swift"
MANAGED_PROVIDERS = ROOT / "schemas" / "managed_providers.yml"


def _load(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def _require_text(path: Path, values: list[object]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [str(value) for value in values if str(value) not in text]


def validate() -> list[str]:
    schema = _load(SCHEMA)
    enums = schema.get("enums")
    presentation = schema.get("presentation")
    if not isinstance(enums, dict) or not isinstance(presentation, dict):
        return ["schema requires enums and presentation objects"]

    errors: list[str] = []
    if schema.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if schema.get("state_contract_version") != 1 or schema.get("presentation_policy_version") != 1:
        errors.append("contract and presentation policy versions must be 1")

    python_text = PYTHON_PROJECTOR.read_text(encoding="utf-8")
    for constant, expected in (
        ("STATE_CONTRACT_VERSION", schema["state_contract_version"]),
        ("PRESENTATION_POLICY_VERSION", schema["presentation_policy_version"]),
    ):
        if not re.search(rf"^{constant} = {expected}$", python_text, re.MULTILINE):
            errors.append(f"Python projector {constant} differs from schema")

    for name in ("activity", "run_lifecycle", "connection", "action"):
        values = enums.get(name)
        if not isinstance(values, list) or not values:
            errors.append(f"enum {name} must be a non-empty list")
            continue
        missing = _require_text(PYTHON_PROJECTOR, values)
        if missing:
            errors.append(f"Python projector missing {name}: {', '.join(missing)}")

    keys = [*presentation.get("primary_keys", []), *presentation.get("access_keys", [])]
    if not keys:
        errors.append("presentation keys must be non-empty")
    else:
        missing = _require_text(PYTHON_PROJECTOR, keys)
        if missing:
            errors.append(f"Python projector missing presentation keys: {', '.join(missing)}")

    providers = schema.get("providers")
    if not isinstance(providers, list) or not providers:
        errors.append("providers must be a non-empty list")
    else:
        managed = _load(MANAGED_PROVIDERS).get("providers") or []
        managed_names = [item.get("provider") for item in managed if isinstance(item, dict)]
        if sorted(providers) != sorted(managed_names):
            errors.append("provider adapter declarations differ from managed_providers.yml")

    for path, version_fields in (
        (OPENAPI, ["state_contract_version", "presentation_policy_version"]),
        (TYPESCRIPT, ["state_contract_version", "presentation_policy_version"]),
        (SWIFT, ["stateContractVersion", "presentationPolicyVersion"]),
    ):
        missing = _require_text(path, version_fields)
        if missing:
            errors.append(f"{path.relative_to(ROOT)} is missing contract-version fields")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate checked-in contract representations.")
    args = parser.parse_args()
    if not args.check:
        parser.error("use --check; this validator has no generated runtime output")
    errors = validate()
    if errors:
        print("session-state contract drift:", *[f"- {error}" for error in errors], sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
