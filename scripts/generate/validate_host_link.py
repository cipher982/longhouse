#!/usr/bin/env python3
"""Validate the canonical host-link and runtime lifecycle wire contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schemas" / "host_link.yml"

EXPECTED = {
    "schema_version": 1,
    "states": {
        "host_link": ["serving", "updating", "slow_update", "unreachable"],
        "host_lifecycle": ["updating", "serving"],
        "admission": ["open", "pending", "draining"],
    },
    "host_link": {
        "fields": {
            "state": ["serving", "updating", "slow_update", "unreachable"],
            "since": "rfc3339_utc",
            "claim": "HostLifecycle|null",
            "claim_started_at": "rfc3339_utc|null",
            "last_acknowledged_at": "rfc3339_utc|null",
            "fresh_horizon_secs": "integer|null",
            "runtime_epoch": "string|null",
        }
    },
    "host_lifecycle": {
        "type": "host.lifecycle",
        "fields": {
            "state": ["updating", "serving"],
            "runtime_epoch": "string",
            "attempt_id": "string|null",
            "phase": "deployer_phase|null",
            "expected_back_by": "rfc3339_utc|null",
            "deadline": "rfc3339_utc|null",
            "cutoff": "rfc3339_utc|null",
        },
        "deployer_phases": [
            "prepare",
            "drain",
            "stop",
            "recovery_point",
            "migrate",
            "start",
            "readiness",
            "probe",
            "reopen",
            "rollback_stop",
            "rollback_start",
            "rollback_readiness",
            "rollback_probe",
            "rollback_reopen",
        ],
    },
    "k1": {
        "codes": ["runtime_restarting", "runtime_unreachable"],
        "response_fields": ["code", "retryable", "runtime_epoch", "admission", "claim"],
        "retry_after_min_seconds": 1,
    },
    "k3": {"admission_enum": ["open", "pending", "draining"]},
    "default_horizons_seconds": {
        "draining": {"expected_back_by": 30, "deadline": 360, "cutoff": 960},
        "pending": {"expected_back_by": 15, "deadline": 300, "cutoff": 960},
    },
    "copy": {
        "updating.headline": "Longhouse is updating",
        "updating.detail": "Your agents keep running on this Mac. Nothing is lost; updates resume in a few seconds.",
        "updating.web_bar": "Updating Longhouse · back in a moment",
        "updating.dock": "Updates paused · Longhouse is updating",
        "slow_update.headline": "Update is taking longer than usual",
        "slow_update.elapsed": "elapsed time since claim_started_at",
        "unreachable.headline": "Can't reach Longhouse",
        "send_queued": "Queued, sends when the update finishes",
        "web_reload": "Longhouse updated · Reload",
    },
}


def _matches(value: object, expected: object, path: str, errors: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(value, dict):
            errors.append(f"{path} must be a YAML object")
            return
        for key, expected_value in expected.items():
            if key not in value:
                errors.append(f"{path}.{key} is required")
            else:
                _matches(value[key], expected_value, f"{path}.{key}", errors)
        unexpected_keys = value.keys() - expected.keys()
        if unexpected_keys:
            keys = ", ".join(sorted(map(str, unexpected_keys)))
            errors.append(f"{path} has unexpected keys: {keys}")
        return
    if type(value) is not type(expected) or value != expected:
        errors.append(f"{path} must equal {expected!r}")


def validate(path: Path = SCHEMA) -> list[str]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot load schema: {exc}"]
    if not isinstance(value, dict):
        return ["schema must contain a YAML object"]
    errors: list[str] = []
    _matches(value, EXPECTED, "schema", errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate the checked-in host-link contract.")
    parser.add_argument("--schema", type=Path, default=SCHEMA, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.check:
        parser.error("use --check; this validator has no generated runtime output")
    errors = validate(args.schema)
    if errors:
        print("host-link contract drift:", *[f"- {error}" for error in errors], sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
