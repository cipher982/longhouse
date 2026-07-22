"""Authenticate provider-capability records against verified CI run metadata.

The caller must obtain ``VerifiedCIRun`` from an authenticated CI API. Record
metadata never creates trust by itself; this module binds exact artifact bytes
and record IDs to the independently verified run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping


@dataclass(frozen=True)
class VerifiedCIRun:
    repository: str
    workflow_ref: str
    run_id: str
    run_attempt: int
    head_sha: str
    conclusion: str
    artifact_sha256: str


@dataclass(frozen=True)
class CITrustPolicy:
    repository: str
    workflow_ref: str


@dataclass(frozen=True)
class VerifiedProofBundle:
    records: tuple[ProviderCapabilityProofRecord, ...]
    trusted_artifact_ids: frozenset[str]


def verify_ci_proof_bundle(
    payload: bytes,
    *,
    run: VerifiedCIRun,
    policy: CITrustPolicy,
) -> VerifiedProofBundle:
    """Return exact trusted IDs only after artifact and workflow verification."""

    if run.repository != policy.repository or run.workflow_ref != policy.workflow_ref:
        raise ValueError("capability proof CI authority does not match trust policy")
    if run.conclusion != "success":
        raise ValueError("capability proof CI run did not succeed")
    if run.run_attempt < 1:
        raise ValueError("capability proof CI run attempt must be positive")
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != run.artifact_sha256:
        raise ValueError("capability proof CI artifact digest mismatch")

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("capability proof CI artifact is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("artifact_kind") != "provider_capability_proof_bundle":
        raise ValueError("capability proof CI artifact kind is invalid")
    raw_records = document.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("capability proof CI artifact records must be a non-empty list")

    expected_invocation = f"{run.run_id}:{run.run_attempt}"
    expected_reference = f"github-actions://{run.repository}/actions/runs/{run.run_id}/attempts/{run.run_attempt}"
    records: list[ProviderCapabilityProofRecord] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValueError("capability proof CI record must be an object")
        record = proof_record_from_mapping(raw_record)
        if record.producer_class != "release_ci":
            raise ValueError("capability proof CI record has an invalid producer class")
        if record.invocation_id != expected_invocation or record.run_reference != expected_reference:
            raise ValueError("capability proof CI record does not match the verified run")
        if record.longhouse_git_sha != run.head_sha:
            raise ValueError("capability proof CI record commit does not match the verified run")
        if not record.raw_reference_digests:
            raise ValueError("capability proof CI record must bind raw evidence digests")
        records.append(record)

    return VerifiedProofBundle(
        records=tuple(records),
        trusted_artifact_ids=frozenset(record.artifact_id for record in records),
    )
