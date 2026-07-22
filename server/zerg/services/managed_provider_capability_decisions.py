"""Load authored managed-provider declarations into the shared evaluator."""

from __future__ import annotations

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_evaluator import CapabilityDecision
from zerg.services.provider_capability_evaluator import EvaluationContext
from zerg.services.provider_capability_evaluator import evaluate_capability
from zerg.services.provider_capability_evaluator import proof_identity_for_declaration
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord


def evaluate_managed_provider_capability(
    *,
    capability_id: str,
    context: EvaluationContext,
    records: tuple[ProviderCapabilityProofRecord, ...] = (),
    trusted_artifact_ids: frozenset[str] = frozenset(),
) -> CapabilityDecision | None:
    """Evaluate one declaration, or return ``None`` when it is undeclared."""

    contract = contract_for_provider(context.provider)
    if contract is None:
        return None
    declaration = contract.capabilities.get(capability_id)
    if declaration is None:
        return None
    return evaluate_capability(
        capability_id=capability_id,
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=context,
        records=records,
        proof_identity=proof_identity_for_declaration(
            adapter_digest=contract.adapter_digest,
            declaration=declaration,
        ),
        trusted_artifact_ids=trusted_artifact_ids,
    )
