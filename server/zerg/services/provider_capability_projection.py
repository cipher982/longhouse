"""Current assurance projection of authored assertions and immutable proofs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import v3_provenance_gaps
from zerg.services.provider_capability_schema import CapabilityAssertion

NEVER_PROVEN = "never_proven"
STALE = "stale"
UNACCEPTABLE_EVIDENCE = "unacceptable_evidence"


@dataclass(frozen=True)
class CapabilityProjection:
    provider: str
    capability: str
    assertion_id: str
    variant: str | None
    scenario_id: str
    declared: bool
    proof_status: str
    generated_at: str | None
    evidence_class: str | None
    proof_artifact_id: str | None = None
    latest_proof_artifact_id: str | None = None
    latest_outcome: str | None = None
    admissibility_reasons: tuple[str, ...] = ()
    accepted_epoch_id: str | None = None
    accepted_epoch_digest: str | None = None
    plan_digest: str | None = None
    compile_report_digest: str | None = None
    producer_id: str | None = None
    worker_id: str | None = None
    open_case_id: str | None = None
    baseline_outcome: str | None = None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _record_order(record: ProviderCapabilityProofRecord) -> tuple[datetime, str]:
    return (_parse_timestamp(record.generated_at) or datetime.min.replace(tzinfo=UTC), record.artifact_id)


def _rejection_reasons(
    assertion: CapabilityAssertion,
    record: ProviderCapabilityProofRecord,
    *,
    moment: datetime,
    integrity_reasons: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    reasons: list[str] = list(integrity_reasons.get(record.artifact_id, ()))
    if record.assertion_variant != assertion.variant:
        reasons.append("proof_assertion_variant_mismatch")
    if assertion.variant is not None:
        reasons.extend(v3_provenance_gaps(record))
    if record.scenario_id != assertion.scenario_id:
        reasons.append("proof_scenario_mismatch")
    if record.scenario_revision < assertion.minimum_scenario_revision:
        reasons.append("proof_scenario_revision_mismatch")
    if record.evidence_class.value not in assertion.acceptable_evidence:
        reasons.append("evidence_class_insufficient")
    generated = _parse_timestamp(record.generated_at)
    if generated is None or generated > moment:
        reasons.append("proof_time_invalid")
    elif (moment - generated).total_seconds() > assertion.max_age_seconds:
        reasons.append("semantic_proof_stale")
    if record.outcome is not AssertionOutcome.PASS:
        reasons.append("semantic_proof_failed")
    return tuple(dict.fromkeys(reasons))


def _status_for_rejection(record: ProviderCapabilityProofRecord, reasons: tuple[str, ...]) -> str:
    non_temporal = set(reasons) - {"semantic_proof_stale", "proof_time_invalid", "semantic_proof_failed"}
    if non_temporal:
        return UNACCEPTABLE_EVIDENCE
    if record.outcome in {
        AssertionOutcome.SEMANTIC_FAIL,
        AssertionOutcome.INFRASTRUCTURE_ERROR,
        AssertionOutcome.BLOCKED,
        AssertionOutcome.SKIPPED,
    }:
        return record.outcome.value
    if "semantic_proof_stale" in reasons or "proof_time_invalid" in reasons:
        return STALE
    return UNACCEPTABLE_EVIDENCE


def project_capabilities(
    assertions: tuple[CapabilityAssertion, ...],
    proof_records: list[ProviderCapabilityProofRecord],
    *,
    now: datetime | None = None,
    integrity_reasons: Mapping[str, tuple[str, ...]] | None = None,
    open_cases: Mapping[tuple[str, str, str, str | None], str] | None = None,
    baselines: Mapping[tuple[str, str, str, str | None], str] | None = None,
) -> tuple[CapabilityProjection, ...]:
    """Join every exact assertion variant to its currently admissible proof.

    A newer failure remains visible through ``latest_*`` but does not erase an
    older, still-current admissible pass.  This preserves both release safety
    and the causal signal that the latest factory run failed.
    """

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    integrity_reasons = integrity_reasons or {}
    open_cases = open_cases or {}
    baselines = baselines or {}
    by_provider_assertion: dict[tuple[str, str], list[ProviderCapabilityProofRecord]] = {}
    for record in proof_records:
        by_provider_assertion.setdefault((record.provider, record.assertion_id), []).append(record)

    projections: list[CapabilityProjection] = []
    for assertion in assertions:
        key = (assertion.provider, assertion.capability, assertion.assertion_id, assertion.variant)
        nearby = sorted(
            by_provider_assertion.get((assertion.provider, assertion.assertion_id), []),
            key=_record_order,
            reverse=True,
        )
        exact = [record for record in nearby if record.assertion_variant == assertion.variant]
        evaluated = [
            (record, _rejection_reasons(assertion, record, moment=moment, integrity_reasons=integrity_reasons)) for record in exact
        ]
        qualifying = next((record for record, reasons in evaluated if not reasons), None)
        latest = exact[0] if exact else nearby[0] if nearby else None
        if qualifying is not None:
            support = qualifying
            status = AssertionOutcome.PASS.value
            reasons: tuple[str, ...] = ()
        elif latest is not None:
            support = latest
            reasons = _rejection_reasons(assertion, latest, moment=moment, integrity_reasons=integrity_reasons)
            status = _status_for_rejection(latest, reasons)
        else:
            support = None
            reasons = ("semantic_proof_missing",)
            status = NEVER_PROVEN
        projections.append(
            CapabilityProjection(
                provider=assertion.provider,
                capability=assertion.capability,
                assertion_id=assertion.assertion_id,
                variant=assertion.variant,
                scenario_id=assertion.scenario_id,
                declared=True,
                proof_status=status,
                generated_at=support.generated_at if support else None,
                evidence_class=support.evidence_class.value if support else None,
                proof_artifact_id=qualifying.artifact_id if qualifying else None,
                latest_proof_artifact_id=latest.artifact_id if latest else None,
                latest_outcome=latest.outcome.value if latest else None,
                admissibility_reasons=reasons,
                accepted_epoch_id=support.accepted_epoch_id if support else None,
                accepted_epoch_digest=support.accepted_epoch_digest if support else None,
                plan_digest=support.plan_digest if support else None,
                compile_report_digest=support.compile_report_digest if support else None,
                producer_id=support.producer_version if support else None,
                worker_id=support.worker_id if support else None,
                open_case_id=open_cases.get(key),
                baseline_outcome=baselines.get(key),
            )
        )
    return tuple(projections)


__all__ = [
    "CapabilityProjection",
    "NEVER_PROVEN",
    "STALE",
    "UNACCEPTABLE_EVIDENCE",
    "project_capabilities",
]
