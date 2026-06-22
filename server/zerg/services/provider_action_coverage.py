"""Derived provider action coverage.

Humans author capability questions and proof requirements. They do not author
support states. This module derives the first narrow coverage slice from the
facts Longhouse already trusts: managed provider contracts and harness proof
assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zerg.services.managed_provider_contracts import contract_for_provider


class ActionCoverageState(StrEnum):
    SUPPORTED = "supported"
    READ_ONLY = "read_only"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProofRef:
    scenario: str
    assertion: str


@dataclass(frozen=True)
class ActionQuestion:
    id: str
    product_label: str
    contract_operation: str | None = None
    support_requires: tuple[ProofRef, ...] = ()
    observe_requires: tuple[ProofRef, ...] = ()


@dataclass(frozen=True)
class ActionCoverage:
    id: str
    product_label: str
    state: ActionCoverageState
    proof_refs: tuple[ProofRef, ...]
    reason: str


OPENCODE_ORCHESTRATION_PROJECTION = "opencode_orchestration_projection"

ACTION_QUESTIONS: tuple[ActionQuestion, ...] = (
    ActionQuestion(
        id="send_prompt",
        product_label="Send prompt",
        contract_operation="send_input",
    ),
    ActionQuestion(
        id="abort",
        product_label="Interrupt turn",
        contract_operation="interrupt",
    ),
    ActionQuestion(
        id="reattach",
        product_label="Reattach",
        contract_operation="reattach",
    ),
    ActionQuestion(
        id="classify_subagents",
        product_label="Subagent threads",
        support_requires=(
            ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "task_child_attached_to_primary_parent"),
            ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "nested_subagent_attached_to_subagent_parent"),
        ),
    ),
    ActionQuestion(
        id="fork",
        product_label="Forks",
        observe_requires=(ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "fork_remains_timeline_visible"),),
    ),
)

_OPERATION_EVIDENCE_PROOFS: Mapping[str, tuple[ProofRef, ...]] = {
    "opencode_subagent_projection": (ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "task_child_attached_to_primary_parent"),),
    "universal_opencode_subagent_projection": (ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "task_child_attached_to_primary_parent"),),
    "opencode_nested_subagent_projection": (ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "nested_subagent_attached_to_subagent_parent"),),
    "universal_opencode_nested_subagent_projection": (
        ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "nested_subagent_attached_to_subagent_parent"),
    ),
    "opencode_fork_projection": (ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "fork_remains_timeline_visible"),),
    "universal_opencode_fork_projection": (ProofRef(OPENCODE_ORCHESTRATION_PROJECTION, "fork_remains_timeline_visible"),),
}


def derive_provider_action_coverage(
    provider: str,
    *,
    proof_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ActionCoverage]:
    """Return derived action coverage for ``provider``.

    ``proof_results`` is keyed by harness scenario name. Each value may be the
    raw scenario payload with an ``assertions`` object, or an object whose keys
    are assertion names.
    """

    normalized_provider = str(provider or "").strip().lower()
    proofs = dict(proof_results or {})
    coverage: dict[str, ActionCoverage] = {}
    for question in ACTION_QUESTIONS:
        if question.contract_operation is not None:
            state, reason = _state_from_contract_operation(
                provider=normalized_provider,
                operation=question.contract_operation,
            )
            proof_refs: tuple[ProofRef, ...] = ()
        elif question.support_requires:
            if normalized_provider != "opencode":
                state = ActionCoverageState.UNKNOWN
                reason = "No provider-specific proof requirement is declared."
            elif _all_proofs_pass(question.support_requires, proofs):
                state = ActionCoverageState.SUPPORTED
                reason = "Required harness assertions passed."
            else:
                state = ActionCoverageState.UNKNOWN
                reason = "Required harness assertions are missing or not passing."
            proof_refs = question.support_requires
        elif question.observe_requires:
            if normalized_provider != "opencode":
                state = ActionCoverageState.UNKNOWN
                reason = "No provider-specific observation proof is declared."
            elif _all_proofs_pass(question.observe_requires, proofs):
                state = ActionCoverageState.READ_ONLY
                reason = "Observation proof passed, but no Longhouse control contract exists."
            else:
                state = ActionCoverageState.UNKNOWN
                reason = "Observation proof is missing or not passing."
            proof_refs = question.observe_requires
        else:
            state = ActionCoverageState.UNKNOWN
            reason = "No proof requirement is declared."
            proof_refs = ()

        coverage[question.id] = ActionCoverage(
            id=question.id,
            product_label=question.product_label,
            state=state,
            proof_refs=proof_refs,
            reason=reason,
        )
    return coverage


def derive_provider_action_coverage_from_artifact(
    artifact: Mapping[str, Any],
    *,
    provider: str | None = None,
) -> dict[str, ActionCoverage]:
    """Derive coverage from a provider proof or universal-harness artifact."""

    return derive_provider_action_coverage(
        provider or _artifact_provider(artifact),
        proof_results=provider_action_proof_results_from_artifact(artifact),
    )


def provider_action_proof_results_from_artifact(
    artifact: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Extract scenario proof assertions from existing proof artifact shapes.

    Supported inputs are intentionally artifact-shaped, not path-shaped:
    universal harness run payloads, individual scenario payloads, and normalized
    provider-release-proof payloads with ``operation_evidence``. Callers own
    loading JSON from disk or object storage before entering this pure seam.
    """

    proofs: dict[str, dict[str, Any]] = {}
    _merge_scenario_payload_if_present(proofs, artifact)
    _merge_universal_results(proofs, artifact.get("results"))
    _merge_operation_evidence(proofs, artifact.get("operation_evidence"))

    for nested_key in ("normalized", "universal_harness"):
        nested = artifact.get(nested_key)
        if isinstance(nested, Mapping):
            _merge_scenario_payload_if_present(proofs, nested)
            _merge_universal_results(proofs, nested.get("results"))
            _merge_operation_evidence(proofs, nested.get("operation_evidence"))

    return proofs


def _state_from_contract_operation(*, provider: str, operation: str) -> tuple[ActionCoverageState, str]:
    contract = contract_for_provider(provider)
    if contract is None:
        return ActionCoverageState.UNKNOWN, "No managed provider contract exists for provider."
    if not contract.supports_contract_operation(operation):
        return ActionCoverageState.UNSUPPORTED, f"Managed provider contract declares {operation}=false."
    evidence = contract.operation_evidence_for(operation)
    if str(evidence.get("level") or "none") == "none":
        return ActionCoverageState.UNKNOWN, f"Managed provider contract lacks proof evidence for {operation}."
    return ActionCoverageState.SUPPORTED, f"Managed provider contract declares and proves {operation}."


def _all_proofs_pass(refs: tuple[ProofRef, ...], proofs: Mapping[str, Mapping[str, Any]]) -> bool:
    return bool(refs) and all(_proof_passes(ref, proofs) for ref in refs)


def _proof_passes(ref: ProofRef, proofs: Mapping[str, Mapping[str, Any]]) -> bool:
    payload = dict(proofs.get(ref.scenario) or {})
    assertions = payload.get("assertions")
    if isinstance(assertions, Mapping):
        # Proofs must be explicit bool True. Missing, truthy strings, and
        # provider-shaped partials all stay unproven.
        return assertions.get(ref.assertion) is True
    return payload.get(ref.assertion) is True


def _artifact_provider(artifact: Mapping[str, Any]) -> str:
    provider = artifact.get("provider")
    if provider:
        return str(provider)
    normalized = artifact.get("normalized")
    if isinstance(normalized, Mapping) and normalized.get("provider"):
        return str(normalized["provider"])
    return ""


def _merge_universal_results(proofs: dict[str, dict[str, Any]], results: Any) -> None:
    if not isinstance(results, list):
        return
    for result in results:
        if not isinstance(result, Mapping):
            continue
        data = result.get("data")
        if isinstance(data, Mapping):
            _merge_scenario_payload_if_present(proofs, data, fallback_scenario=result.get("scenario"))
            _merge_operation_evidence(proofs, data.get("operation_evidence"))


def _merge_scenario_payload_if_present(
    proofs: dict[str, dict[str, Any]],
    payload: Mapping[str, Any],
    *,
    fallback_scenario: Any = None,
) -> None:
    scenario = str(payload.get("scenario") or fallback_scenario or "").strip()
    if not scenario:
        return
    scenario_payload = dict(proofs.get(scenario) or {})
    assertions = dict(scenario_payload.get("assertions") or {})
    if isinstance(payload.get("assertions"), Mapping):
        assertions.update(dict(payload["assertions"]))
    scenario_payload.update(dict(payload))
    if assertions:
        scenario_payload["assertions"] = assertions
    proofs[scenario] = scenario_payload


def _merge_operation_evidence(proofs: dict[str, dict[str, Any]], operation_evidence: Any) -> None:
    if not isinstance(operation_evidence, Mapping):
        return
    for operation, evidence in operation_evidence.items():
        refs = _OPERATION_EVIDENCE_PROOFS.get(str(operation))
        if refs is None or not isinstance(evidence, Mapping):
            continue
        passed = _status_passes(evidence.get("status"))
        for ref in refs:
            scenario_payload = dict(proofs.get(ref.scenario) or {})
            assertions = dict(scenario_payload.get("assertions") or {})
            assertions[ref.assertion] = passed
            scenario_payload["assertions"] = assertions
            proofs[ref.scenario] = scenario_payload


def _status_passes(status: Any) -> bool:
    return str(status or "").strip().lower() == "pass"
