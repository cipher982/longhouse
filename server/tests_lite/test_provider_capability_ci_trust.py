from __future__ import annotations

import hashlib
import json

import pytest

from zerg.services.provider_capability_ci_trust import CITrustPolicy
from zerg.services.provider_capability_ci_trust import VerifiedCIRun
from zerg.services.provider_capability_ci_trust import verify_ci_proof_bundle
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord


def _record(**changes) -> ProviderCapabilityProofRecord:
    values = {
        "provider": "codex",
        "provider_version": "0.145.0",
        "provider_executable_identity": "sha256:provider",
        "provider_contract_digest": "sha256:contract",
        "adapter_digest": "sha256:adapter",
        "scenario_id": "codex_coordination_directed_input",
        "scenario_revision": 1,
        "oracle_digest": "sha256:oracle",
        "assertion_id": "provider_input_receipt_linked",
        "outcome": AssertionOutcome.PASS,
        "evidence_class": EvidenceClass.LIVE_TOKEN,
        "generated_at": "2026-07-22T15:00:00Z",
        "producer_class": "release_ci",
        "producer_version": "2",
        "invocation_id": "12345:2",
        "mode": "helm",
        "run_reference": "github-actions://cipher982/longhouse/actions/runs/12345/attempts/2",
        "raw_reference_digests": ("sha256:transcript",),
        "longhouse_git_sha": "abc123",
    }
    values.update(changes)
    return ProviderCapabilityProofRecord(**values)


def _payload(record: ProviderCapabilityProofRecord) -> bytes:
    return json.dumps(
        {"artifact_kind": "provider_capability_proof_bundle", "records": [record.serialize()]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _run(payload: bytes, **changes) -> VerifiedCIRun:
    values = {
        "repository": "cipher982/longhouse",
        "workflow_ref": ".github/workflows/provider-capability-proof.yml@refs/heads/main",
        "run_id": "12345",
        "run_attempt": 2,
        "head_sha": "abc123",
        "conclusion": "success",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
    }
    values.update(changes)
    return VerifiedCIRun(**values)


POLICY = CITrustPolicy(
    repository="cipher982/longhouse",
    workflow_ref=".github/workflows/provider-capability-proof.yml@refs/heads/main",
)


def test_verified_ci_bundle_returns_exact_trusted_record_ids() -> None:
    record = _record()
    payload = _payload(record)

    verified = verify_ci_proof_bundle(payload, run=_run(payload), policy=POLICY)

    assert verified.records == (record,)
    assert verified.trusted_artifact_ids == frozenset({record.artifact_id})


def test_local_release_ci_claim_cannot_self_authenticate() -> None:
    record = _record(run_reference="local-file://forged")
    payload = _payload(record)

    with pytest.raises(ValueError, match="does not match the verified run"):
        verify_ci_proof_bundle(payload, run=_run(payload), policy=POLICY)


def test_tampered_or_wrong_workflow_artifact_is_rejected() -> None:
    payload = _payload(_record())

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_ci_proof_bundle(payload + b"\n", run=_run(payload), policy=POLICY)
    with pytest.raises(ValueError, match="authority does not match"):
        verify_ci_proof_bundle(payload, run=_run(payload, workflow_ref="untrusted.yml@refs/heads/main"), policy=POLICY)
