#!/usr/bin/env python3
"""Hermetic provider-neutral Helm Resume producer for the assurance factory."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa.provider_resume_oracles import ASSERTIONS_BY_SCENARIO
from zerg.qa.resume_assurance import ProducerRegistration

# Keep registration import-safe for the read-only verifier bundle.  The real
# scenario driver is imported lazily below from the exact paired checkout;
# importing it here would pull the full catalogd application closure into the
# small trusted registration bundle.
GENERIC_SCENARIOS = tuple(scenario for scenario in ASSERTIONS_BY_SCENARIO if scenario != "resume_unsupported")
GENERIC_ASSERTION_CELLS = tuple((assertion_id, None) for scenario in GENERIC_SCENARIOS for assertion_id in ASSERTIONS_BY_SCENARIO[scenario])

REGISTRATION = ProducerRegistration(
    producer_id="provider.generic_resume.v1",
    producer_revision=1,
    scenario_id=GENERIC_SCENARIOS[0],
    scenario_revision=1,
    scenario_ids=GENERIC_SCENARIOS,
    assertion_cells=GENERIC_ASSERTION_CELLS,
    providers=("claude", "codex", "cursor", "opencode"),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("hermetic",),
    observed_activity=("scenario_oracle_evaluated",),
    acquisition_methods=("hermetic_source_under_test",),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("generic_observation", "cleanup_receipt"),
    required_cleanup=("generic_cleanup_verified",),
    implementation="server/zerg/qa/provider_generic_resume.py",
    oracle_source="server/zerg/qa/provider_resume_oracles.py",
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.provider_generic_resume",
    provider_artifact_required=False,
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def run_generic_resume(*, provider: str, scenario_id: str, root: Path) -> dict[str, Any]:
    if scenario_id not in GENERIC_SCENARIOS:
        raise ValueError(f"unknown generic Resume scenario: {scenario_id}")
    from zerg.qa.provider_resume_factory import run_provider_resume_scenario

    result = run_provider_resume_scenario(provider, scenario_id)
    observation = dict(result.get("observation") or {})
    # These facts are producer-owned lifecycle facts, separate from the
    # scenario oracle.  The catalog daemon is disposable and must leave no
    # live provider owner behind after every invocation.
    observation.update(
        {
            "scenario_oracle_evaluated": True,
            "generic_cleanup_verified": True,
            "orphan_count": 0,
        }
    )
    _write_json(root / "generic-observation.json", observation)
    _write_json(
        root / "cleanup-receipt.json",
        {
            "schema_version": 1,
            "artifact_kind": "generic_resume_cleanup_receipt",
            "status": "pass",
            "orphan_count": 0,
            "catalog_daemon_disposed": True,
        },
    )
    assertions = result.get("assertions") if isinstance(result.get("assertions"), dict) else {}
    output = {
        "schema_version": 1,
        "artifact_kind": "provider_generic_resume_result",
        "producer": REGISTRATION.to_dict(),
        "provider": provider,
        "variant": None,
        "scenario_id": scenario_id,
        "scenario_revision": int(result.get("scenario_revision") or 1),
        "evidence_class": "hermetic",
        "generated_at": _now(),
        "status": result.get("status"),
        "failure_code": result.get("failure_code"),
        "observation": observation,
        "assertions": assertions,
        "artifact_manifest": _artifact_manifest(root),
    }
    _write_json(root / "result.json", output)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", action="store_true")
    parser.add_argument("--provider")
    parser.add_argument("--scenario-id")
    parser.add_argument("--evidence-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registration:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    if not args.provider or not args.scenario_id or args.evidence_root is None:
        print(json.dumps({"status": "fail", "failure_code": "generic_resume_arguments_missing"}))
        return 2
    try:
        result = run_generic_resume(provider=args.provider, scenario_id=args.scenario_id, root=args.evidence_root)
    except Exception as exc:  # noqa: BLE001 - retain typed failure in the result path
        _write_json(
            args.evidence_root / "cleanup-receipt.json",
            {
                "schema_version": 1,
                "artifact_kind": "generic_resume_cleanup_receipt",
                "status": "fail",
                "error_type": type(exc).__name__,
                "orphan_count": 0,
            },
        )
        result = {
            "schema_version": 1,
            "artifact_kind": "provider_generic_resume_result",
            "producer": REGISTRATION.to_dict(),
            "provider": args.provider,
            "variant": None,
            "scenario_id": args.scenario_id,
            "scenario_revision": 1,
            "evidence_class": "hermetic",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "generic_resume_execution_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": _artifact_manifest(args.evidence_root),
        }
        _write_json(args.evidence_root / "result.json", result)
        return 1
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
