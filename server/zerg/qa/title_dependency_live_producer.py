"""Live Runtime Host title dependency health assurance producer."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.title_dependency_oracles import TitleDependencyTemporarilyUnavailable
from zerg.qa.title_dependency_oracles import artifact_manifest
from zerg.qa.title_dependency_oracles import run_live_title_dependency_oracle

ASSERTION_ID = "dependency_health"
REGISTRATION = ProducerRegistration(
    producer_id="longhouse.title_dependency_health.v1",
    producer_revision=8,
    scenario_id="title_dependency_live_health",
    scenario_revision=7,
    assertion_cells=((ASSERTION_ID, None),),
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("runtime_host",),
    evidence_classes=("live_token",),
    observed_activity=(
        "typed_hidden_title_assurance_obligation",
        "factory_machine_identity_verified",
        "claude_semantic_path_consumed",
        "runtime_host_session_projection",
        "runtime_host_title_provenance",
        "runtime_host_dependency_health",
        "runtime_host_title_backlog_health",
    ),
    acquisition_methods=(),
    credential_binding_ids=("runtime_host_control",),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("live_runtime_observation", "runtime_request_receipt", "cleanup_receipt"),
    required_cleanup=("no_owned_processes",),
    implementation="server/zerg/qa/title_dependency_live_producer.py",
    oracle_source="server/zerg/qa/title_dependency_oracles.py",
    oracle_entrypoint="run_live_title_dependency_oracle",
    executable_module="zerg.qa.title_dependency_live_producer",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def run(evidence_root: Path) -> dict[str, Any]:
    try:
        oracle = run_live_title_dependency_oracle(evidence_root=evidence_root)
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_title_dependency_health_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "status": "pass" if oracle["passed"] else "fail",
            "observation": oracle["observation"],
            "assertions": {ASSERTION_ID: bool(oracle["passed"])},
        }
    except Exception as exc:  # noqa: BLE001 - typed failure evidence is the producer contract
        evidence_root.mkdir(parents=True, exist_ok=True)
        if not (evidence_root / "cleanup-receipt.json").exists():
            _write_json(
                evidence_root / "cleanup-receipt.json",
                {"status": "fail", "orphan_count": 0, "owned_process_count": 0, "error_type": type(exc).__name__},
            )
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_title_dependency_health_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "status": "fail",
            "failure_code": (
                "title_dependency_temporarily_unavailable"
                if isinstance(exc, TitleDependencyTemporarilyUnavailable)
                else "title_dependency_health_failed"
            ),
            "error": f"{type(exc).__name__}: {exc}",
            "observation": {},
            "assertions": {ASSERTION_ID: False},
        }
    result["generated_at"] = datetime.now(UTC).isoformat()
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
