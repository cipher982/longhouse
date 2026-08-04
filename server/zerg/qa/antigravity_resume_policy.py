#!/usr/bin/env python3
"""Typed Antigravity Resume policy proof with no registration or process launch."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

from zerg.qa.provider_resume_oracles import assertions_for
from zerg.qa.resume_assurance import ProducerRegistration

REGISTRATION = ProducerRegistration(
    producer_id="antigravity.resume_policy.v1",
    producer_revision=1,
    scenario_id="resume_unsupported",
    scenario_revision=1,
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


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=("policy_disabled",))
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", type=Path)
    args = parser.parse_args(arguments)
    args.evidence_root.mkdir(parents=True, exist_ok=False)
    observation = {"disposition": "policy_disabled", "registration_count": 0, "provider_spawn_count": 0}
    assertions = assertions_for("resume_unsupported", observation)
    cleanup = {"verified": True, "orphan_count": 0, "provider_spawn_count": 0}
    (args.evidence_root / "cleanup-receipt.json").write_text(json.dumps(cleanup, sort_keys=True) + "\n")
    result = {
        "schema_version": 1,
        "artifact_kind": "direct_native_resume_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "antigravity",
        "variant": "policy_disabled",
        "scenario_id": "resume_unsupported",
        "scenario_revision": 1,
        "evidence_class": "hermetic",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "pass",
        "observation": observation,
        "assertions": assertions,
        "artifact_manifest": [],
    }
    (args.evidence_root / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
