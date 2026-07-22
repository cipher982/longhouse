"""Pure contextual projection for authored provider capability declarations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from zerg.services.provider_capability_contract import ActionGate
from zerg.services.provider_capability_contract import CapabilityDisposition
from zerg.services.provider_capability_contract import ProductAction
from zerg.services.provider_capability_contract import RuntimeState
from zerg.services.provider_capability_contract import VerificationState
from zerg.services.provider_capability_contract import project_product_action
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProofRequirement
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import select_proof


@dataclass(frozen=True)
class EvaluationContext:
    machine_id: str
    provider: str
    observed_at: datetime
    session_id: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    platform: str | None = None
    architecture: str | None = None
    provider_version: str | None = None
    provider_executable_identity: str | None = None
    runtime: RuntimeState = RuntimeState.UNKNOWN
    resolved_policy: Mapping[str, bool] | None = None
    policy_provenance: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ProviderProofIdentity:
    adapter_digest: str
    oracle_digests: Mapping[str, str]


@dataclass(frozen=True)
class CapabilityDecision:
    capability_id: str
    disposition: CapabilityDisposition
    verification: VerificationState
    runtime: RuntimeState
    action: ProductAction
    reason_codes: tuple[str, ...]
    qualifying_artifact_ids: tuple[str, ...]
    rejected_artifact_ids: tuple[str, ...]
    latest_run_failed: bool
    input_bundle_digest: str

    def serialize(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in ("disposition", "verification", "runtime", "action"):
            payload[field] = getattr(self, field).value
        return payload


def proof_identity_for_declaration(
    *,
    adapter_digest: str,
    declaration: Mapping[str, Any],
) -> ProviderProofIdentity:
    oracle_digests: dict[str, str] = {}
    for assertion in declaration.get("required_assertions") or ():
        scenario_id = str(assertion["scenario_id"])
        oracle_digest = str(assertion["oracle_digest"])
        existing = oracle_digests.get(scenario_id)
        if existing is not None and existing != oracle_digest:
            raise ValueError(f"scenario {scenario_id} declares conflicting oracle digests")
        oracle_digests[scenario_id] = oracle_digest
    return ProviderProofIdentity(adapter_digest=adapter_digest, oracle_digests=oracle_digests)


def evaluate_capability(
    *,
    capability_id: str,
    declaration: Mapping[str, Any],
    provider_contract_digest: str,
    context: EvaluationContext,
    records: tuple[ProviderCapabilityProofRecord, ...] = (),
    proof_identity: ProviderProofIdentity | None = None,
    trusted_producer_classes: frozenset[str] = frozenset(),
) -> CapabilityDecision:
    disposition = CapabilityDisposition(str(declaration["disposition"]))
    policy_key = str(declaration["policy_key"])
    if context.resolved_policy is not None and context.resolved_policy.get(policy_key, True) is False:
        disposition = CapabilityDisposition.POLICY_DISABLED

    runtime_prerequisites = tuple(declaration.get("runtime_prerequisites") or ())
    runtime = context.runtime if context.session_id else RuntimeState.UNKNOWN
    if not runtime_prerequisites:
        runtime = RuntimeState.NOT_REQUIRED
    reason_codes: list[str] = []
    qualifying: list[str] = []
    rejected: list[str] = []
    latest_run_failed = False
    missing_states: list[VerificationState] = []

    contexts = declaration.get("contexts") or {}
    allowed_modes = set(contexts.get("modes") or ())
    applicable = not allowed_modes or context.mode in allowed_modes

    if proof_identity is None or not context.provider_version or not context.provider_executable_identity:
        verification = VerificationState.INCONCLUSIVE
        reason_codes.append("cli_unavailable" if not context.provider_executable_identity else "semantic_proof_missing")
    else:
        for assertion in declaration.get("required_assertions") or ():
            scenario_id = str(assertion["scenario_id"])
            oracle_digest = proof_identity.oracle_digests.get(scenario_id)
            if not oracle_digest:
                missing_states.append(VerificationState.INCONCLUSIVE)
                reason_codes.append("semantic_proof_missing")
                continue
            requirement = ProofRequirement(
                provider=context.provider,
                provider_version=context.provider_version,
                provider_executable_identity=context.provider_executable_identity,
                assertion_id=str(assertion["id"]),
                scenario_id=scenario_id,
                minimum_scenario_revision=int(assertion["minimum_scenario_revision"]),
                acceptable_evidence=frozenset(EvidenceClass(value) for value in assertion["acceptable_evidence"]),
                trusted_producer_classes=trusted_producer_classes,
                provider_contract_digest=provider_contract_digest,
                adapter_digest=proof_identity.adapter_digest,
                oracle_digest=oracle_digest,
                mode=context.mode,
                permission_mode=context.permission_mode,
                platform=context.platform,
                architecture=context.architecture,
                max_age_seconds=int(assertion["max_age_seconds"]),
            )
            selection = select_proof(records, requirement, observed_at=context.observed_at)
            latest_run_failed = latest_run_failed or selection.latest_run_failed
            if selection.qualifying_pass is not None:
                qualifying.append(selection.qualifying_pass.artifact_id)
                continue
            if selection.latest_run is None:
                reason_codes.append("semantic_proof_missing")
            rejected.extend(artifact_id for artifact_id, _ in selection.rejected)
            rejected_reasons = {reason for _, reasons in selection.rejected for reason in reasons}
            reason_codes.extend(sorted(rejected_reasons))
            if "semantic_proof_stale" in rejected_reasons:
                missing_states.append(VerificationState.STALE)
            elif "proof_untrusted_producer" in rejected_reasons:
                missing_states.append(VerificationState.INCONCLUSIVE)
            elif selection.latest_run is not None and selection.latest_run.outcome is AssertionOutcome.SEMANTIC_FAIL:
                missing_states.append(VerificationState.FAILED)
            else:
                missing_states.append(VerificationState.MISSING)

        if not missing_states:
            verification = VerificationState.PROVEN
        elif VerificationState.FAILED in missing_states:
            verification = VerificationState.FAILED
        elif VerificationState.INCONCLUSIVE in missing_states:
            verification = VerificationState.INCONCLUSIVE
        elif VerificationState.STALE in missing_states:
            verification = VerificationState.STALE
        else:
            verification = VerificationState.MISSING

    if disposition is CapabilityDisposition.POLICY_DISABLED:
        reason_codes.append("policy_disabled")
    elif disposition is not CapabilityDisposition.IMPLEMENTED:
        reason_codes.append(str(declaration.get("reason_code") or "longhouse_unimplemented"))
    if runtime is RuntimeState.UNKNOWN:
        reason_codes.append("runtime_unavailable")

    action = project_product_action(
        disposition=disposition,
        verification=verification,
        runtime=runtime,
        gate=ActionGate(str(declaration["action_gate"])),
        applicable=applicable,
    )
    input_bundle_digest = _input_bundle_digest(
        capability_id=capability_id,
        declaration=declaration,
        context=context,
        record_ids=tuple(record.artifact_id for record in records),
        proof_identity=proof_identity,
        trusted_producer_classes=trusted_producer_classes,
    )
    return CapabilityDecision(
        capability_id=capability_id,
        disposition=disposition,
        verification=verification,
        runtime=runtime,
        action=action,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        qualifying_artifact_ids=tuple(dict.fromkeys(qualifying)),
        rejected_artifact_ids=tuple(dict.fromkeys(rejected)),
        latest_run_failed=latest_run_failed,
        input_bundle_digest=input_bundle_digest,
    )


def _input_bundle_digest(
    *,
    capability_id: str,
    declaration: Mapping[str, Any],
    context: EvaluationContext,
    record_ids: tuple[str, ...],
    proof_identity: ProviderProofIdentity | None,
    trusted_producer_classes: frozenset[str],
) -> str:
    context_payload = asdict(context)
    context_payload["observed_at"] = context.observed_at.isoformat()
    context_payload["runtime"] = context.runtime.value
    payload = {
        "capability_id": capability_id,
        "declaration": declaration,
        "context": context_payload,
        "record_ids": sorted(record_ids),
        "proof_identity": asdict(proof_identity) if proof_identity else None,
        "trusted_producer_classes": sorted(trusted_producer_classes),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
