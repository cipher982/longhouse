"""Capability projection from the contract, joined with proof status.

Phase 5 of docs/specs/provider-factory-coherence.md: "Capability projection
from the contract, proof status attached separately, both rendered." The
serving pieces on either side of this join already existed before this
module: `schemas/managed_providers.yml`'s per-capability `required_assertions`
list is parsed into `CapabilityAssertion` tuples by
`provider_factory_model._load_capability_assertions()` (Phase 1), and real
proof evidence already flows factory -> `TrustedProofPublisher` ->
`POST /internal/provider-capability-proofs` -> `ProviderCapabilityProofStore`
-> `GET /agents/provider-capability-proofs` (raw records, unjoined). Nothing
joined the two: a capability the contract declares stays invisible unless a
caller already knows which `assertion_id` strings prove it and manually
matches them against raw records. This module is that join, kept pure and
separate from both stores it reads from -- callers own I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Mapping

from zerg.qa.provider_factory_model import CapabilityAssertion
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord

# A capability whose latest proof predates this epic's own tracked history,
# or that has never been proven at all, is not "unknown" in some vague sense
# -- it is exactly one of these two states, named so a caller can render them
# differently from an actual failing proof.
NEVER_PROVEN = "never_proven"
STALE = "stale"


@dataclass(frozen=True)
class CapabilityProjection:
    provider: str
    capability: str
    assertion_id: str
    scenario_id: str
    declared: bool
    proof_status: str
    generated_at: str | None
    evidence_class: str | None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _latest_by_assertion(
    records: Mapping[str, ProviderCapabilityProofRecord] | list[ProviderCapabilityProofRecord],
) -> dict[str, ProviderCapabilityProofRecord]:
    """Reduce a (possibly unordered, possibly multi-record-per-assertion)
    collection to the single freshest record per assertion_id. Freshest by
    `generated_at`, not by store insertion order -- callers should not have
    to pre-sort."""
    iterable = records.values() if isinstance(records, Mapping) else records
    latest: dict[str, ProviderCapabilityProofRecord] = {}
    latest_ts: dict[str, datetime] = {}
    for record in iterable:
        parsed = _parse_timestamp(record.generated_at)
        current_ts = latest_ts.get(record.assertion_id)
        if parsed is None:
            if record.assertion_id not in latest:
                latest[record.assertion_id] = record
            continue
        if current_ts is None or parsed > current_ts:
            latest[record.assertion_id] = record
            latest_ts[record.assertion_id] = parsed
    return latest


def project_capabilities(
    assertions: tuple[CapabilityAssertion, ...],
    proof_records: list[ProviderCapabilityProofRecord],
    *,
    now: datetime | None = None,
) -> tuple[CapabilityProjection, ...]:
    """Join declared capability assertions against real proof records.

    Pure: no I/O. Callers load `assertions` from
    `provider_factory_model.load_facts().capability_assertions` and
    `proof_records` from `ProviderCapabilityProofStore`/the publication
    endpoint themselves. Every declared assertion produces exactly one
    `CapabilityProjection`, even when no proof exists for it at all
    (`proof_status=NEVER_PROVEN`) -- the contract is the source of truth for
    what should exist; proof evidence is attached, not required, to render a
    row.
    """
    moment = now or datetime.now(UTC)
    by_provider_assertion: dict[tuple[str, str], list[ProviderCapabilityProofRecord]] = {}
    for record in proof_records:
        by_provider_assertion.setdefault((record.provider, record.assertion_id), []).append(record)

    projections: list[CapabilityProjection] = []
    for assertion in assertions:
        candidates = by_provider_assertion.get((assertion.provider, assertion.assertion_id), [])
        latest = _latest_by_assertion(candidates).get(assertion.assertion_id) if candidates else None
        if latest is None:
            projections.append(
                CapabilityProjection(
                    provider=assertion.provider,
                    capability=assertion.capability,
                    assertion_id=assertion.assertion_id,
                    scenario_id=assertion.scenario_id,
                    declared=True,
                    proof_status=NEVER_PROVEN,
                    generated_at=None,
                    evidence_class=None,
                )
            )
            continue
        parsed = _parse_timestamp(latest.generated_at)
        age_seconds = (moment - parsed).total_seconds() if parsed is not None else None
        is_stale = age_seconds is not None and age_seconds > assertion.max_age_seconds
        proof_status = STALE if is_stale else latest.outcome.value
        projections.append(
            CapabilityProjection(
                provider=assertion.provider,
                capability=assertion.capability,
                assertion_id=assertion.assertion_id,
                scenario_id=assertion.scenario_id,
                declared=True,
                proof_status=proof_status,
                generated_at=latest.generated_at,
                evidence_class=latest.evidence_class.value,
            )
        )
    return tuple(projections)
