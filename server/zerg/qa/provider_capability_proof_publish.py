"""Translate executed scenario assertions into immutable capability proof records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


@dataclass(frozen=True)
class ScenarioProofIdentity:
    provider: str
    provider_version: str
    provider_executable_identity: str
    provider_contract_digest: str
    adapter_digest: str
    scenario_id: str
    scenario_revision: int
    oracle_digest: str
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


def publish_scenario_assertions(
    *,
    identity: ScenarioProofIdentity,
    assertions: Mapping[str, bool],
    store: ProviderCapabilityProofStore,
) -> tuple[ProviderCapabilityProofRecord, ...]:
    """Publish one record per observed assertion without inferring outcomes."""

    if not assertions:
        raise ValueError("scenario proof requires at least one assertion")
    records: list[ProviderCapabilityProofRecord] = []
    for assertion_id, passed in sorted(assertions.items()):
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise ValueError("scenario assertion IDs must be non-empty strings")
        if not isinstance(passed, bool):
            raise ValueError(f"scenario assertion {assertion_id} outcome must be boolean")
        record = ProviderCapabilityProofRecord(
            provider=identity.provider,
            provider_version=identity.provider_version,
            provider_executable_identity=identity.provider_executable_identity,
            provider_contract_digest=identity.provider_contract_digest,
            adapter_digest=identity.adapter_digest,
            scenario_id=identity.scenario_id,
            scenario_revision=identity.scenario_revision,
            oracle_digest=identity.oracle_digest,
            assertion_id=assertion_id,
            outcome=AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
            evidence_class=identity.evidence_class,
            generated_at=identity.generated_at,
            producer_class=identity.producer_class,
            producer_version=identity.producer_version,
            invocation_id=identity.invocation_id,
            mode=identity.mode,
            permission_mode=identity.permission_mode,
            platform=identity.platform,
            architecture=identity.architecture,
            run_reference=identity.run_reference,
            raw_reference_digests=identity.raw_reference_digests,
            longhouse_build_id=identity.longhouse_build_id,
            longhouse_git_sha=identity.longhouse_git_sha,
        )
        store.write(record)
        records.append(record)
    return tuple(records)
