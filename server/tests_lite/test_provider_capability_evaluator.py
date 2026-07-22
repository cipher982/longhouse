from __future__ import annotations

from datetime import UTC
from datetime import datetime

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_contract import ProductAction
from zerg.services.provider_capability_contract import RuntimeState
from zerg.services.provider_capability_contract import VerificationState
from zerg.services.provider_capability_evaluator import EvaluationContext
from zerg.services.provider_capability_evaluator import ProviderProofIdentity
from zerg.services.provider_capability_evaluator import evaluate_capability
from zerg.services.provider_capability_evaluator import proof_identity_for_declaration
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord


NOW = datetime(2026, 7, 22, 17, 0, tzinfo=UTC)


def _contract_and_declaration():
    contract = contract_for_provider("codex")
    assert contract is not None
    return contract, contract.capabilities["coordination.message.send"]


def _context(**changes) -> EvaluationContext:
    values = {
        "machine_id": "machine-1",
        "session_id": "session-1",
        "provider": "codex",
        "provider_version": "0.145.0",
        "provider_executable_identity": "sha256:provider",
        "mode": "helm",
        "observed_at": NOW,
        "runtime": RuntimeState.READY,
    }
    values.update(changes)
    return EvaluationContext(**values)


def _identity() -> ProviderProofIdentity:
    return ProviderProofIdentity(
        adapter_digest="sha256:adapter",
        oracle_digests={"codex_coordination_message": "sha256:oracle"},
    )


def _record(contract_digest: str, **changes) -> ProviderCapabilityProofRecord:
    values = {
        "provider": "codex",
        "provider_version": "0.145.0",
        "provider_executable_identity": "sha256:provider",
        "provider_contract_digest": contract_digest,
        "adapter_digest": "sha256:adapter",
        "scenario_id": "codex_coordination_message",
        "scenario_revision": 1,
        "oracle_digest": "sha256:oracle",
        "assertion_id": "directed_message_persisted_and_delivered",
        "outcome": AssertionOutcome.PASS,
        "evidence_class": EvidenceClass.HERMETIC,
        "generated_at": "2026-07-22T16:00:00Z",
        "producer_class": "release_ci",
        "producer_version": "2",
        "invocation_id": "run-1",
        "mode": "helm",
    }
    values.update(changes)
    return ProviderCapabilityProofRecord(**values)


def test_shadow_decision_is_inconclusive_without_identity_or_proof() -> None:
    contract, declaration = _contract_and_declaration()

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(),
    )

    assert decision.verification is VerificationState.INCONCLUSIVE
    assert decision.action is ProductAction.DISABLED
    assert decision.reason_codes == ("semantic_proof_missing",)


def test_generated_declaration_supplies_scoped_adapter_and_oracle_identity() -> None:
    contract, declaration = _contract_and_declaration()

    identity = proof_identity_for_declaration(adapter_digest=contract.adapter_digest, declaration=declaration)

    assert identity.adapter_digest == contract.adapter_digest
    assert set(identity.oracle_digests) == {"codex_coordination_message"}
    assert len(identity.oracle_digests["codex_coordination_message"]) == 64


def test_provider_wide_context_cannot_enable_session_action() -> None:
    contract, declaration = _contract_and_declaration()
    record = _record(contract.contract_entry_digest)

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(session_id=None),
        records=(record,),
        proof_identity=_identity(),
        trusted_producer_classes=frozenset({"release_ci"}),
    )

    assert decision.verification is VerificationState.PROVEN
    assert decision.runtime is RuntimeState.UNKNOWN
    assert decision.action is ProductAction.DISABLED
    assert "runtime_unavailable" in decision.reason_codes


def test_exact_proof_and_session_context_enable_strict_action() -> None:
    contract, declaration = _contract_and_declaration()
    record = _record(contract.contract_entry_digest)

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(),
        records=(record,),
        proof_identity=_identity(),
        trusted_producer_classes=frozenset({"release_ci"}),
    )

    assert decision.verification is VerificationState.PROVEN
    assert decision.action is ProductAction.ENABLED
    assert decision.qualifying_artifact_ids == (record.artifact_id,)
    assert len(decision.input_bundle_digest) == 64


def test_resolved_policy_disables_proven_action() -> None:
    contract, declaration = _contract_and_declaration()
    record = _record(contract.contract_entry_digest)

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(resolved_policy={"provider.codex.coordination_message": False}),
        records=(record,),
        proof_identity=_identity(),
        trusted_producer_classes=frozenset({"release_ci"}),
    )

    assert decision.action is ProductAction.DISABLED
    assert "policy_disabled" in decision.reason_codes


def test_context_outside_authored_modes_is_hidden() -> None:
    contract, declaration = _contract_and_declaration()

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(mode="shadow"),
    )

    assert decision.action is ProductAction.HIDDEN


def test_expired_proof_is_stale_and_cannot_enable_strict_action() -> None:
    contract, declaration = _contract_and_declaration()
    record = _record(contract.contract_entry_digest, generated_at="2026-07-01T16:00:00Z")

    decision = evaluate_capability(
        capability_id="coordination.message.send",
        declaration=declaration,
        provider_contract_digest=contract.contract_entry_digest,
        context=_context(),
        records=(record,),
        proof_identity=_identity(),
        trusted_producer_classes=frozenset({"release_ci"}),
    )

    assert decision.verification is VerificationState.STALE
    assert decision.action is ProductAction.DISABLED
