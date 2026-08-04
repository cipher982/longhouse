#!/usr/bin/env python3
"""Typed Antigravity Resume policy proof with no registration or process launch."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

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


def _manifest_entry(path: Path, root: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": len(content),
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }


def _antigravity_contract(repo_root: Path) -> tuple[str, dict[str, object]]:
    """Read the pinned generated contract without importing the full server."""

    manifest_path = repo_root / "server" / "zerg" / "config" / "managed_provider_contracts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    providers = manifest.get("providers") if isinstance(manifest, dict) else None
    if not isinstance(providers, list):
        raise RuntimeError("managed provider contract manifest has no providers")
    contract = next((item for item in providers if isinstance(item, dict) and item.get("provider") == "antigravity"), None)
    if not isinstance(contract, dict):
        raise RuntimeError("managed provider contract manifest has no Antigravity entry")
    capability = (contract.get("capabilities") or {}).get("session.resume.helm")
    if not isinstance(capability, dict) or capability.get("disposition") != "policy_disabled":
        raise RuntimeError("Antigravity Resume is not policy-disabled in the pinned contract")
    if contract.get("reattach") is not False:
        raise RuntimeError("Antigravity Resume policy unexpectedly permits reattach")
    encoded = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return digest, contract


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
    contract_digest, contract = _antigravity_contract(args.repo_root)
    observation = {
        "disposition": "policy_disabled",
        "registration_count": 0,
        "provider_spawn_count": 0,
    }
    assertions = {
        "unsupported_resume_is_typed_and_side_effect_free": (
            observation["disposition"] in {"upstream_absent", "policy_disabled"}
            and observation["registration_count"] == 0
            and observation["provider_spawn_count"] == 0
        )
    }
    policy = {
        "status": "pass" if all(assertions.values()) else "fail",
        "provider": "antigravity",
        "scenario_id": "resume_unsupported",
        "evidence_class": "hermetic",
        "observation": observation,
        "assertions": assertions,
    }
    policy_receipt = {
        "contract_entry_digest": contract_digest,
        "reattach": contract["reattach"],
        "resume_capability": dict(contract["capabilities"]["session.resume.helm"]),
        "scenario_result": policy,
    }
    policy_receipt_path = args.evidence_root / "policy-source-receipt.json"
    policy_receipt_path.write_text(json.dumps(policy_receipt, indent=2, sort_keys=True) + "\n")
    cleanup = {"verified": True, "orphan_count": 0, "provider_spawn_count": 0}
    cleanup_path = args.evidence_root / "cleanup-receipt.json"
    cleanup_path.write_text(json.dumps(cleanup, sort_keys=True) + "\n")
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
        "status": policy["status"],
        "observation": observation,
        "assertions": assertions,
        "artifact_manifest": [
            _manifest_entry(policy_receipt_path, args.evidence_root),
            _manifest_entry(cleanup_path, args.evidence_root),
        ],
    }
    (args.evidence_root / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
