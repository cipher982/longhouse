#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6,<7"]
# ///
"""Generate the managed-provider runtime manifest from the schema source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "managed_providers.yml"
OUTPUT_PATH = ROOT / "server" / "zerg" / "config" / "managed_provider_contracts.json"
# The engine include_str!s this copy. It omits the source digests (adapter_digest,
# oracle_digest): the engine reads neither, and they change whenever any adapter
# or oracle file does, which rebuilt the engine on unrelated edits.
ENGINE_OUTPUT_PATH = ROOT / "engine" / "src" / "managed_provider_contracts.generated.json"
ENGINE_OMITTED_KEYS = frozenset({"adapter_digest", "oracle_digest"})

sys.path.insert(0, str(ROOT / "server"))

from zerg.managed_provider_contract_manifest import CONTRACT_OPERATIONS  # noqa: E402
from zerg.managed_provider_contract_manifest import OPERATION_EVIDENCE_LEVELS  # noqa: E402
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


def _without_digests(value):
    if isinstance(value, dict):
        return {key: _without_digests(item) for key, item in value.items() if key not in ENGINE_OMITTED_KEYS}
    if isinstance(value, list):
        return [_without_digests(item) for item in value]
    return value


def _render_engine_json(rendered: str) -> str:
    payload = _without_digests(json.loads(rendered))
    # The vocabulary the engine validates the manifest against, from the same
    # Python authority that validates it server-side, instead of a Rust copy.
    payload["operations"] = list(CONTRACT_OPERATIONS)
    payload["evidence_levels"] = sorted(OPERATION_EVIDENCE_LEVELS)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


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

    if args.check and args.init_from_current_json:
        parser.error("--check cannot be combined with --init-from-current-json")

    if args.init_from_current_json:
        _write_schema_from_current_json()

    rendered = render_contract_manifest_json(_load_schema())
    outputs = {OUTPUT_PATH: rendered, ENGINE_OUTPUT_PATH: _render_engine_json(rendered)}

    if args.check:
        stale = [
            path
            for path, text in outputs.items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != text
        ]
        for path in stale:
            print(
                f"{path} is out of date; run scripts/generate/generate_managed_provider_contracts.py --write",
                file=sys.stderr,
            )
        return 1 if stale else 0

    if args.write:
        for path, text in outputs.items():
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
