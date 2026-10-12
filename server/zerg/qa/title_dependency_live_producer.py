"""Live Runtime Host title dependency health assurance producer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.title_dependency_oracles import TitleDependencyTemporarilyUnavailable
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


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="longhouse_title_dependency_health_result",
    failure_code=lambda exc: (
        "title_dependency_temporarily_unavailable"
        if isinstance(exc, TitleDependencyTemporarilyUnavailable)
        else "title_dependency_health_failed"
    ),
    variant_required=False,
)


def run(evidence_root: Path) -> dict[str, Any]:
    def body(run: Run) -> None:
        oracle = run_live_title_dependency_oracle(evidence_root=evidence_root)
        run.observation = oracle["observation"]
        run.assertions = {ASSERTION_ID: bool(oracle["passed"])}

    return PRODUCER.execute(
        evidence_root,
        body,
        creates_root=False,
        failure_cleanup={"status": "fail", "orphan_count": 0, "owned_process_count": 0},
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(lambda args: run(args.evidence_root), argv)


if __name__ == "__main__":
    raise SystemExit(main())
