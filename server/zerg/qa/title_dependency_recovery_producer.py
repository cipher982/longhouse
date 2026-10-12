"""Hermetic Runtime Host title dependency recovery assurance producer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.title_dependency_oracles import run_hermetic_title_dependency_oracle

ASSERTION_ID = "dependency_incident_recovery"
REGISTRATION = ProducerRegistration(
    producer_id="longhouse.title_dependency_recovery.v1",
    producer_revision=9,
    scenario_id="title_dependency_recovery",
    scenario_revision=9,
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
        "terminal_empty_response_reentry",
        "healthy_transport_empty_response_isolation",
        "row_scoped_terminal_negative_control",
        "provider_proof_workspace_excluded_from_title_debt",
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


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="longhouse_title_dependency_recovery_result",
    failure_code="title_dependency_recovery_failed",
    variant_required=False,
)


def run(evidence_root: Path) -> dict[str, Any]:
    def body(run: Run) -> None:
        oracle = run_hermetic_title_dependency_oracle(evidence_root=evidence_root, repo_root=Path(__file__).resolve().parents[3])
        run.observation = oracle["observation"]
        run.assertions = {ASSERTION_ID: bool(oracle["passed"])}

    return PRODUCER.execute(
        evidence_root,
        body,
        creates_root=False,
        failure_cleanup={
            "status": "fail",
            "orphan_count": 0,
            "runtime_host_stopped": False,
            "loopback_stub_stopped": False,
            "temporary_runtime_removed": False,
        },
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(lambda args: run(args.evidence_root), argv)


if __name__ == "__main__":
    raise SystemExit(main())
