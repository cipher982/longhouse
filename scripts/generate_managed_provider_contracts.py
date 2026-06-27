#!/usr/bin/env python3
"""Generate the managed-provider runtime manifest from the schema source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "managed_providers.yml"
OUTPUT_PATH = ROOT / "server" / "zerg" / "config" / "managed_provider_contracts.json"

sys.path.insert(0, str(ROOT / "server"))

from zerg.managed_provider_contract_manifest import normalize_contract_manifest  # noqa: E402
from zerg.managed_provider_contract_manifest import render_contract_manifest_json  # noqa: E402


def _load_schema() -> dict:
    payload = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{SCHEMA_PATH} must contain a YAML object")
    return payload


def _write_schema_from_current_json() -> None:
    payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    normalized = normalize_contract_manifest(payload)
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(yaml.safe_dump(normalized, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write the generated JSON manifest.")
    parser.add_argument(
        "--check", action="store_true", help="Fail if the generated JSON differs from the checked-in manifest."
    )
    parser.add_argument(
        "--init-from-current-json",
        action="store_true",
        help="Initialize schemas/managed_providers.yml from the current JSON manifest before generating.",
    )
    args = parser.parse_args()

    if args.init_from_current_json:
        _write_schema_from_current_json()

    rendered = render_contract_manifest_json(_load_schema())
    current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""

    if args.check:
        if rendered != current:
            print(
                f"{OUTPUT_PATH} is out of date; run scripts/generate_managed_provider_contracts.py --write",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.write:
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
