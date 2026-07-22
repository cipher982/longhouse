"""Pure executable provider-capability proof records and qualification.

This module does not read files, invoke providers, or establish producer trust.
Callers supply exact artifact IDs authenticated out of band plus the expected
scoped identities for the claim they are evaluating.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import Any

PROOF_SCHEMA_VERSION = 2
PROOF_ARTIFACT_KIND = "provider_capability_assertion"


class EvidenceClass(StrEnum):
    HERMETIC = "hermetic"
    LIVE_NO_TOKEN = "live_no_token"
    LIVE_TOKEN = "live_token"


class AssertionOutcome(StrEnum):
    PASS = "pass"
    SEMANTIC_FAIL = "semantic_fail"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProviderCapabilityProofRecord:
    provider: str
    provider_version: str
    provider_executable_identity: str
    provider_contract_digest: str
    adapter_digest: str
    scenario_id: str
    scenario_revision: int
    oracle_digest: str
    assertion_id: str
    outcome: AssertionOutcome
    evidence_class: EvidenceClass
    generated_at: str
    producer_class: str
    producer_version: str
    invocation_id: str
    mode: str | None = None
    permission_mode: str | None = None
    platform: str | None = None
    architecture: str | None = None
    run_reference: str | None = None
    raw_reference_digests: tuple[str, ...] = ()
    longhouse_build_id: str | None = None
    longhouse_git_sha: str | None = None
    schema_version: int = PROOF_SCHEMA_VERSION
    artifact_kind: str = PROOF_ARTIFACT_KIND

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["evidence_class"] = self.evidence_class.value
        payload["raw_reference_digests"] = list(self.raw_reference_digests)
        return payload

    @property
    def artifact_id(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def serialize(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, **self.canonical_payload()}


@dataclass(frozen=True)
class ProofRequirement:
    provider: str
    assertion_id: str
    scenario_id: str
    minimum_scenario_revision: int
    acceptable_evidence: frozenset[EvidenceClass]
    trusted_artifact_ids: frozenset[str]
    provider_contract_digest: str
    adapter_digest: str
    oracle_digest: str
    provider_version: str
    provider_executable_identity: str
    mode: str | None = None
    permission_mode: str | None = None
    platform: str | None = None
    architecture: str | None = None
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class ProofApplicability:
    applicable: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProofSelection:
    qualifying_pass: ProviderCapabilityProofRecord | None
    latest_run: ProviderCapabilityProofRecord | None
    latest_run_failed: bool
    rejected: tuple[tuple[str, tuple[str, ...]], ...]


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider capability proof {field} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider capability proof {field} must be null or a non-empty string")
    return value.strip()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provider capability proof generated_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider capability proof generated_at must include a timezone")
    return parsed.astimezone(UTC)


def proof_record_from_mapping(payload: Mapping[str, Any]) -> ProviderCapabilityProofRecord:
    if payload.get("schema_version") != PROOF_SCHEMA_VERSION:
        raise ValueError(f"provider capability proof schema_version must be {PROOF_SCHEMA_VERSION}")
    if payload.get("artifact_kind") != PROOF_ARTIFACT_KIND:
        raise ValueError(f"provider capability proof artifact_kind must be {PROOF_ARTIFACT_KIND}")

    scenario_revision = payload.get("scenario_revision")
    if not isinstance(scenario_revision, int) or isinstance(scenario_revision, bool) or scenario_revision < 1:
        raise ValueError("provider capability proof scenario_revision must be a positive integer")

    raw_reference_digests = payload.get("raw_reference_digests", [])
    if not isinstance(raw_reference_digests, list) or not all(isinstance(item, str) and item.strip() for item in raw_reference_digests):
        raise ValueError("provider capability proof raw_reference_digests must be a list of non-empty strings")

    generated_at = _required_string(payload, "generated_at")
    _parse_timestamp(generated_at)
    try:
        outcome = AssertionOutcome(_required_string(payload, "outcome"))
        evidence_class = EvidenceClass(_required_string(payload, "evidence_class"))
    except ValueError as exc:
        raise ValueError(f"provider capability proof enum is invalid: {exc}") from exc

    record = ProviderCapabilityProofRecord(
        provider=_required_string(payload, "provider"),
        provider_version=_required_string(payload, "provider_version"),
        provider_executable_identity=_required_string(payload, "provider_executable_identity"),
        provider_contract_digest=_required_string(payload, "provider_contract_digest"),
        adapter_digest=_required_string(payload, "adapter_digest"),
        scenario_id=_required_string(payload, "scenario_id"),
        scenario_revision=scenario_revision,
        oracle_digest=_required_string(payload, "oracle_digest"),
        assertion_id=_required_string(payload, "assertion_id"),
        outcome=outcome,
        evidence_class=evidence_class,
        generated_at=generated_at,
        producer_class=_required_string(payload, "producer_class"),
        producer_version=_required_string(payload, "producer_version"),
        invocation_id=_required_string(payload, "invocation_id"),
        mode=_optional_string(payload, "mode"),
        permission_mode=_optional_string(payload, "permission_mode"),
        platform=_optional_string(payload, "platform"),
        architecture=_optional_string(payload, "architecture"),
        run_reference=_optional_string(payload, "run_reference"),
        raw_reference_digests=tuple(item.strip() for item in raw_reference_digests),
        longhouse_build_id=_optional_string(payload, "longhouse_build_id"),
        longhouse_git_sha=_optional_string(payload, "longhouse_git_sha"),
    )

    artifact_id = payload.get("artifact_id")
    if artifact_id is not None and artifact_id != record.artifact_id:
        raise ValueError("provider capability proof artifact_id does not match canonical content")
    return record


def evaluate_proof_applicability(
    record: ProviderCapabilityProofRecord,
    requirement: ProofRequirement,
    *,
    observed_at: datetime,
) -> ProofApplicability:
    reasons: list[str] = []
    if record.provider != requirement.provider:
        reasons.append("proof_provider_mismatch")
    if record.provider_version != requirement.provider_version:
        reasons.append("proof_provider_version_mismatch")
    if record.provider_executable_identity != requirement.provider_executable_identity:
        reasons.append("proof_executable_mismatch")
    if record.assertion_id != requirement.assertion_id or record.scenario_id != requirement.scenario_id:
        reasons.append("semantic_proof_missing")
    if record.scenario_revision < requirement.minimum_scenario_revision:
        reasons.append("proof_scenario_revision_mismatch")
    if record.provider_contract_digest != requirement.provider_contract_digest:
        reasons.append("proof_manifest_mismatch")
    if record.adapter_digest != requirement.adapter_digest:
        reasons.append("proof_adapter_mismatch")
    if record.oracle_digest != requirement.oracle_digest:
        reasons.append("proof_oracle_mismatch")
    if record.artifact_id not in requirement.trusted_artifact_ids:
        reasons.append("proof_untrusted_producer")
    if record.evidence_class not in requirement.acceptable_evidence:
        reasons.append("evidence_class_insufficient")
    for field in ("mode", "permission_mode", "platform", "architecture"):
        expected = getattr(requirement, field)
        if expected is not None and getattr(record, field) != expected:
            reasons.append(f"proof_{field}_mismatch")

    generated_at = _parse_timestamp(record.generated_at)
    observed_at = observed_at.astimezone(UTC)
    if generated_at > observed_at:
        reasons.append("proof_clock_skew")
    elif requirement.max_age_seconds is not None:
        if (observed_at - generated_at).total_seconds() > requirement.max_age_seconds:
            reasons.append("semantic_proof_stale")
    if record.outcome is not AssertionOutcome.PASS:
        reasons.append("semantic_proof_failed")
    return ProofApplicability(applicable=not reasons, reason_codes=tuple(dict.fromkeys(reasons)))


def select_proof(
    records: Iterable[ProviderCapabilityProofRecord],
    requirement: ProofRequirement,
    *,
    observed_at: datetime,
) -> ProofSelection:
    relevant = (
        record
        for record in records
        if record.provider == requirement.provider
        and record.assertion_id == requirement.assertion_id
        and record.scenario_id == requirement.scenario_id
    )
    ordered = sorted(relevant, key=lambda record: (_parse_timestamp(record.generated_at), record.artifact_id), reverse=True)
    latest_run = ordered[0] if ordered else None
    qualifying_pass: ProviderCapabilityProofRecord | None = None
    rejected: list[tuple[str, tuple[str, ...]]] = []
    for record in ordered:
        applicability = evaluate_proof_applicability(record, requirement, observed_at=observed_at)
        if applicability.applicable and qualifying_pass is None:
            qualifying_pass = record
        elif not applicability.applicable:
            rejected.append((record.artifact_id, applicability.reason_codes))
    return ProofSelection(
        qualifying_pass=qualifying_pass,
        latest_run=latest_run,
        latest_run_failed=latest_run is not None and latest_run.outcome is not AssertionOutcome.PASS,
        rejected=tuple(rejected),
    )
