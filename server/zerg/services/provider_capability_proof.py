"""Pure executable provider-capability proof records and qualification.

Schema v3 keeps v2 records readable as immutable history. A v2 record can
never satisfy a current requirement: it has no accepted epoch, compiled plan,
worker, or retained-content identity to make that claim.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import Any

PROOF_SCHEMA_VERSION = 3
LEGACY_PROOF_SCHEMA_VERSION = 2
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
    provider_build_identity: str | None = None
    provider_build_granularity: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    platform: str | None = None
    architecture: str | None = None
    run_reference: str | None = None
    raw_reference_digests: tuple[str, ...] = ()
    longhouse_build_id: str | None = None
    longhouse_git_sha: str | None = None
    # v3 assurance subject and execution provenance.
    assertion_variant: str | None = None
    factory_source_sha: str | None = None
    accepted_epoch_id: str | None = None
    accepted_epoch_digest: str | None = None
    verifier_bundle_digest: str | None = None
    compile_report_digest: str | None = None
    plan_digest: str | None = None
    sandbox_receipt_digest: str | None = None
    cleanup_receipt_digest: str | None = None
    worker_id: str | None = None
    worker_census_digest: str | None = None
    acquisition_provenance: Mapping[str, Any] = field(default_factory=dict)
    auth_mechanism: str | None = None
    observed_activity: tuple[str, ...] = ()
    credential_binding_facts: Mapping[str, Any] = field(default_factory=dict)
    predecessor_failure_artifact_id: str | None = None
    remediation_successor_artifact_id: str | None = None
    provenance_extension: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROOF_SCHEMA_VERSION
    artifact_kind: str = PROOF_ARTIFACT_KIND

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["evidence_class"] = self.evidence_class.value
        payload["raw_reference_digests"] = list(self.raw_reference_digests)
        payload["observed_activity"] = list(self.observed_activity)
        if self.provider_build_identity is None:
            payload.pop("provider_build_identity")
        if self.provider_build_granularity is None:
            payload.pop("provider_build_granularity")
        if self.schema_version == LEGACY_PROOF_SCHEMA_VERSION:
            for name in _V3_FIELDS:
                payload.pop(name, None)
        return payload

    @property
    def artifact_id(self) -> str:
        encoded = json.dumps(self.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def serialize(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, **self.canonical_payload()}

    def referenced_content_digests(self) -> tuple[str, ...]:
        values = [
            *self.raw_reference_digests,
            self.accepted_epoch_digest,
            self.verifier_bundle_digest,
            self.worker_census_digest,
            self.provider_build_identity,
            self.compile_report_digest,
            self.plan_digest,
            self.sandbox_receipt_digest,
            self.cleanup_receipt_digest,
        ]
        return tuple(dict.fromkeys(value for value in values if value))


_V3_FIELDS = (
    "assertion_variant",
    "factory_source_sha",
    "accepted_epoch_id",
    "accepted_epoch_digest",
    "verifier_bundle_digest",
    "compile_report_digest",
    "plan_digest",
    "sandbox_receipt_digest",
    "cleanup_receipt_digest",
    "worker_id",
    "worker_census_digest",
    "acquisition_provenance",
    "auth_mechanism",
    "observed_activity",
    "credential_binding_facts",
    "predecessor_failure_artifact_id",
    "remediation_successor_artifact_id",
    "provenance_extension",
)


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
    assertion_variant: str | None = None
    factory_source_sha: str | None = None
    accepted_epoch_digest: str | None = None
    verifier_bundle_digest: str | None = None
    compile_report_digest: str | None = None
    plan_digest: str | None = None
    worker_id: str | None = None
    worker_census_digest: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    platform: str | None = None
    architecture: str | None = None
    max_age_seconds: int | None = None
    available_content_digests: frozenset[str] | None = None


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


def _required_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider capability proof {field_name} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider capability proof {field_name} must be null or a non-empty string")
    return value.strip()


def _string_tuple(payload: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    value = payload.get(field_name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"provider capability proof {field_name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _mapping(payload: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    value = payload.get(field_name, {})
    if not isinstance(value, dict):
        raise ValueError(f"provider capability proof {field_name} must be an object")
    return dict(value)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provider capability proof generated_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider capability proof generated_at must include a timezone")
    return parsed.astimezone(UTC)


def proof_record_from_mapping(payload: Mapping[str, Any]) -> ProviderCapabilityProofRecord:
    schema_version = payload.get("schema_version")
    if schema_version not in {LEGACY_PROOF_SCHEMA_VERSION, PROOF_SCHEMA_VERSION}:
        raise ValueError(f"provider capability proof schema_version must be {LEGACY_PROOF_SCHEMA_VERSION} or {PROOF_SCHEMA_VERSION}")
    if payload.get("artifact_kind") != PROOF_ARTIFACT_KIND:
        raise ValueError(f"provider capability proof artifact_kind must be {PROOF_ARTIFACT_KIND}")
    scenario_revision = payload.get("scenario_revision")
    if not isinstance(scenario_revision, int) or isinstance(scenario_revision, bool) or scenario_revision < 1:
        raise ValueError("provider capability proof scenario_revision must be a positive integer")
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
        provider_build_identity=_optional_string(payload, "provider_build_identity"),
        provider_build_granularity=_optional_string(payload, "provider_build_granularity"),
        mode=_optional_string(payload, "mode"),
        permission_mode=_optional_string(payload, "permission_mode"),
        platform=_optional_string(payload, "platform"),
        architecture=_optional_string(payload, "architecture"),
        run_reference=_optional_string(payload, "run_reference"),
        raw_reference_digests=_string_tuple(payload, "raw_reference_digests"),
        longhouse_build_id=_optional_string(payload, "longhouse_build_id"),
        longhouse_git_sha=_optional_string(payload, "longhouse_git_sha"),
        assertion_variant=_optional_string(payload, "assertion_variant"),
        factory_source_sha=_optional_string(payload, "factory_source_sha"),
        accepted_epoch_id=_optional_string(payload, "accepted_epoch_id"),
        accepted_epoch_digest=_optional_string(payload, "accepted_epoch_digest"),
        verifier_bundle_digest=_optional_string(payload, "verifier_bundle_digest"),
        compile_report_digest=_optional_string(payload, "compile_report_digest"),
        plan_digest=_optional_string(payload, "plan_digest"),
        sandbox_receipt_digest=_optional_string(payload, "sandbox_receipt_digest"),
        cleanup_receipt_digest=_optional_string(payload, "cleanup_receipt_digest"),
        worker_id=_optional_string(payload, "worker_id"),
        worker_census_digest=_optional_string(payload, "worker_census_digest"),
        acquisition_provenance=_mapping(payload, "acquisition_provenance"),
        auth_mechanism=_optional_string(payload, "auth_mechanism"),
        observed_activity=_string_tuple(payload, "observed_activity"),
        credential_binding_facts=_mapping(payload, "credential_binding_facts"),
        predecessor_failure_artifact_id=_optional_string(payload, "predecessor_failure_artifact_id"),
        remediation_successor_artifact_id=_optional_string(payload, "remediation_successor_artifact_id"),
        provenance_extension=_mapping(payload, "provenance_extension"),
        schema_version=schema_version,
    )
    artifact_id = payload.get("artifact_id")
    if artifact_id is not None and artifact_id != record.artifact_id:
        raise ValueError("provider capability proof artifact_id does not match canonical content")
    return record


def v3_provenance_gaps(record: ProviderCapabilityProofRecord) -> tuple[str, ...]:
    if record.schema_version != PROOF_SCHEMA_VERSION:
        return ("proof_schema_legacy",)
    required = (
        "factory_source_sha",
        "accepted_epoch_id",
        "accepted_epoch_digest",
        "verifier_bundle_digest",
        "compile_report_digest",
        "plan_digest",
        "sandbox_receipt_digest",
        "cleanup_receipt_digest",
        "worker_id",
        "worker_census_digest",
        "auth_mechanism",
    )
    gaps = [f"proof_{name}_missing" for name in required if not getattr(record, name)]
    if not record.acquisition_provenance:
        gaps.append("proof_acquisition_provenance_missing")
    if not record.observed_activity:
        gaps.append("proof_observed_activity_missing")
    if not record.credential_binding_facts:
        gaps.append("proof_credential_binding_facts_missing")
    return tuple(gaps)


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
    if record.assertion_variant != requirement.assertion_variant:
        reasons.append("proof_assertion_variant_mismatch")
    reasons.extend(v3_provenance_gaps(record))
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
    for name in (
        "factory_source_sha",
        "accepted_epoch_digest",
        "verifier_bundle_digest",
        "compile_report_digest",
        "plan_digest",
        "worker_id",
        "worker_census_digest",
        "mode",
        "permission_mode",
        "platform",
        "architecture",
    ):
        expected = getattr(requirement, name)
        if expected is not None and getattr(record, name) != expected:
            reasons.append(f"proof_{name}_mismatch")
    if requirement.available_content_digests is not None:
        missing = set(record.referenced_content_digests()) - requirement.available_content_digests
        if missing:
            reasons.append("proof_referenced_content_missing")
    generated_at = _parse_timestamp(record.generated_at)
    observed_at = observed_at.astimezone(UTC)
    if generated_at > observed_at:
        reasons.append("proof_clock_skew")
    elif requirement.max_age_seconds is not None and (observed_at - generated_at).total_seconds() > requirement.max_age_seconds:
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
        and record.assertion_variant == requirement.assertion_variant
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


__all__ = [
    "AssertionOutcome",
    "EvidenceClass",
    "LEGACY_PROOF_SCHEMA_VERSION",
    "PROOF_SCHEMA_VERSION",
    "ProofApplicability",
    "ProofRequirement",
    "ProofSelection",
    "ProviderCapabilityProofRecord",
    "evaluate_proof_applicability",
    "proof_record_from_mapping",
    "select_proof",
    "v3_provenance_gaps",
]
