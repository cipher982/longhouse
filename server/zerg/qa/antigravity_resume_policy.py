#!/usr/bin/env python3
"""Typed Antigravity Resume policy proof with no registration or process launch."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_resume_oracles import assertions_for
from zerg.qa.resume_assurance import ProducerRegistration

REGISTRATION = ProducerRegistration(
    producer_id="antigravity.resume_policy.v1",
    producer_revision=1,
    scenario_id="resume_unsupported",
    # 2: the assertion is the whole verdict (false while the contract still
    # reattaches) and a run that raises leaves a typed failure.
    scenario_revision=2,
    assertion_cells=(("unsupported_resume_is_typed_and_side_effect_free", "policy_disabled"),),
    providers=("antigravity",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("hermetic",),
    observed_activity=("typed_policy_result", "zero_registration", "zero_provider_spawn"),
    acquisition_methods=(),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("cleanup_receipt",),
    required_cleanup=("no_orphan_provider_processes",),
    implementation="server/zerg/qa/antigravity_resume_policy.py",
    oracle_source="server/zerg/qa/provider_resume_oracles.py",
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.antigravity_resume_policy",
    provider_artifact_required=False,
)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="direct_native_resume_result",
    failure_code="antigravity_resume_policy_failed",
    needs=("evidence_root", "repo_root", "engine", "longhouse_cli"),
    variants=("policy_disabled",),
)


def _policy_contract(repo_root: Path) -> tuple[dict[str, object], str]:
    manifest_path = repo_root / "server/zerg/config/managed_provider_contracts.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        raise ValueError("managed provider contract manifest must contain providers[]")
    contract = next(
        (item for item in providers if isinstance(item, dict) and item.get("provider") == "antigravity"),
        None,
    )
    if contract is None:
        raise ValueError("managed provider contract manifest has no antigravity entry")
    encoded = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return contract, hashlib.sha256(encoded).hexdigest()


def run_policy(args: argparse.Namespace) -> dict[str, object]:
    def body(run: Run) -> None:
        contract, contract_entry_digest = _policy_contract(args.repo_root)
        resume_capability = dict(dict(contract.get("capabilities") or {}).get("session.resume.helm") or {})
        observation = {
            "disposition": resume_capability.get("disposition"),
            "registration_count": 0,
            "provider_spawn_count": 0,
        }
        # A contract that still reattaches has not disabled resume, whatever its disposition says.
        assertions = {
            assertion_id: held and contract.get("reattach") is False
            for assertion_id, held in assertions_for("resume_unsupported", observation).items()
        }
        passed = all(assertions.values())
        policy = {
            "status": "pass" if passed else "fail",
            "failure_code": None if passed else "resume_unsupported_oracle_failed",
            "provider": "antigravity",
            "scenario": "resume_unsupported",
            "evidence_class": "hermetic",
            "scenario_revision": REGISTRATION.scenario_revision,
            "observation": observation,
            "assertions": assertions,
        }
        run.write(
            "policy-source-receipt.json",
            {
                "contract_entry_digest": contract_entry_digest,
                "reattach": contract.get("reattach"),
                "resume_capability": resume_capability,
                "scenario_result": policy,
            },
        )
        run.write("cleanup-receipt.json", {"verified": True, "orphan_count": 0, "provider_spawn_count": 0})
        run.observation = observation
        run.assertions = assertions

    return PRODUCER.execute(args.evidence_root, body, variant="policy_disabled")


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_policy, argv)


if __name__ == "__main__":
    raise SystemExit(main())
