"""Hermetic Runtime Host title dependency recovery assurance producer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.title_dependency_oracles import artifact_manifest
from zerg.qa.title_dependency_oracles import run_hermetic_title_dependency_oracle

ASSERTION_ID = "dependency_incident_recovery"
REGISTRATION = ProducerRegistration(
    producer_id="longhouse.title_dependency_recovery.v1",
    producer_revision=2,
    scenario_id="title_dependency_recovery",
    scenario_revision=2,
    assertion_cells=((ASSERTION_ID, None),),
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("runtime_host",),
    evidence_classes=("hermetic",),
    observed_activity=(
        "concurrent_hidden_title_obligations",
        "provider_shaped_availability_failure",
        "bounded_model_concurrency",
        "bounded_scheduled_worker_creation",
        "aged_backlog_health_degradation",
        "legacy_terminal_timeout_reentry",
        "row_scoped_terminal_negative_control",
        "runtime_restart",
        "credential_generation_advanced",
        "incident_scoped_recovery",
    ),
    acquisition_methods=(),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "catalog_observation",
        "loopback_stub_receipt",
        "runtime_request_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("runtime_host_stopped", "loopback_stub_stopped", "temporary_runtime_removed"),
    implementation="server/zerg/qa/title_dependency_recovery_producer.py",
    oracle_source="server/zerg/qa/title_dependency_oracles.py",
    oracle_entrypoint="run_hermetic_title_dependency_oracle",
    executable_module="zerg.qa.title_dependency_recovery_producer",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def run(evidence_root: Path) -> dict[str, Any]:
    try:
        oracle = run_hermetic_title_dependency_oracle(
            evidence_root=evidence_root,
            repo_root=Path(__file__).resolve().parents[3],
        )
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_title_dependency_recovery_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "hermetic",
            "status": "pass" if oracle["passed"] else "fail",
            "observation": oracle["observation"],
            "assertions": {ASSERTION_ID: bool(oracle["passed"])},
        }
    except Exception as exc:  # noqa: BLE001 - typed failure evidence is the producer contract
        evidence_root.mkdir(parents=True, exist_ok=True)
        if not (evidence_root / "cleanup-receipt.json").exists():
            _write_json(
                evidence_root / "cleanup-receipt.json",
                {"status": "fail", "orphan_count": 0, "error_type": type(exc).__name__},
            )
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_title_dependency_recovery_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "hermetic",
            "status": "fail",
            "failure_code": "title_dependency_recovery_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "observation": {},
            "assertions": {ASSERTION_ID: False},
        }
    result["artifact_manifest"] = artifact_manifest(evidence_root)
    _write_json(evidence_root / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(arguments)
    result = run(args.evidence_root)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
