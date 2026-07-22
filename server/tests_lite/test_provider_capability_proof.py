from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime

import pytest

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProofRequirement
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import evaluate_proof_applicability
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof import select_proof


NOW = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)


def _record(**changes) -> ProviderCapabilityProofRecord:
    record = ProviderCapabilityProofRecord(
        provider="codex",
        provider_version="0.145.0",
        provider_executable_identity="sha256:provider",
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        scenario_id="codex_active_steer",
        scenario_revision=2,
        oracle_digest="sha256:oracle",
        assertion_id="nonce_observed_before_turn_end",
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at="2026-07-22T15:00:00Z",
        producer_class="release_ci",
        producer_version="2",
        invocation_id="run-123",
        mode="helm",
        platform="darwin",
        architecture="arm64",
        raw_reference_digests=("sha256:transcript",),
        longhouse_git_sha="abc123",
    )
    return replace(record, **changes)


def _requirement(**changes) -> ProofRequirement:
    requirement = ProofRequirement(
        provider="codex",
        provider_version="0.145.0",
        provider_executable_identity="sha256:provider",
        assertion_id="nonce_observed_before_turn_end",
        scenario_id="codex_active_steer",
        minimum_scenario_revision=2,
        acceptable_evidence=frozenset({EvidenceClass.LIVE_TOKEN}),
        trusted_artifact_ids=frozenset({_record().artifact_id}),
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        oracle_digest="sha256:oracle",
        mode="helm",
        platform="darwin",
        architecture="arm64",
        max_age_seconds=7200,
    )
    return replace(requirement, **changes)


def test_record_round_trips_with_content_derived_identity() -> None:
    record = _record()

    parsed = proof_record_from_mapping(record.serialize())

    assert parsed == record
    assert parsed.artifact_id == record.artifact_id
    assert len(record.artifact_id) == 64


def test_record_rejects_tampered_artifact_identity() -> None:
    payload = _record().serialize()
    payload["artifact_id"] = "wrong"

    with pytest.raises(ValueError, match="artifact_id does not match"):
        proof_record_from_mapping(payload)


@pytest.mark.parametrize(
    ("record", "requirement", "reason"),
    [
        (_record(provider_version="0.146.0"), _requirement(), "proof_provider_version_mismatch"),
        (_record(provider_executable_identity="other"), _requirement(), "proof_executable_mismatch"),
        (_record(provider_contract_digest="other"), _requirement(), "proof_manifest_mismatch"),
        (_record(adapter_digest="other"), _requirement(), "proof_adapter_mismatch"),
        (_record(scenario_revision=1), _requirement(), "proof_scenario_revision_mismatch"),
        (_record(producer_class="manual"), _requirement(), "proof_untrusted_producer"),
        (_record(evidence_class=EvidenceClass.HERMETIC), _requirement(), "evidence_class_insufficient"),
        (_record(platform="linux"), _requirement(), "proof_platform_mismatch"),
        (_record(generated_at="2026-07-22T12:00:00Z"), _requirement(), "semantic_proof_stale"),
        (_record(outcome=AssertionOutcome.SEMANTIC_FAIL), _requirement(), "semantic_proof_failed"),
    ],
)
def test_applicability_rejects_scoped_mismatches(record, requirement, reason) -> None:
    result = evaluate_proof_applicability(record, requirement, observed_at=NOW)

    assert result.applicable is False
    assert reason in result.reason_codes


def test_applicability_accepts_exact_trusted_pass() -> None:
    result = evaluate_proof_applicability(_record(), _requirement(), observed_at=NOW)

    assert result.applicable is True
    assert result.reason_codes == ()


def test_later_failure_does_not_erase_unexpired_applicable_pass() -> None:
    earlier_pass = _record(generated_at="2026-07-22T14:00:00Z")
    latest_failure = _record(
        generated_at="2026-07-22T15:30:00Z",
        outcome=AssertionOutcome.INFRASTRUCTURE_ERROR,
        invocation_id="run-456",
    )

    selection = select_proof(
        [earlier_pass, latest_failure],
        _requirement(trusted_artifact_ids=frozenset({earlier_pass.artifact_id, latest_failure.artifact_id})),
        observed_at=NOW,
    )

    assert selection.qualifying_pass == earlier_pass
    assert selection.latest_run == latest_failure
    assert selection.latest_run_failed is True
    assert selection.rejected[0][0] == latest_failure.artifact_id


def test_other_assertion_failure_does_not_contaminate_selection() -> None:
    relevant = _record(outcome=AssertionOutcome.INFRASTRUCTURE_ERROR)
    unrelated = _record(
        assertion_id="different_assertion",
        scenario_id="different_scenario",
        outcome=AssertionOutcome.SEMANTIC_FAIL,
        generated_at="2026-07-22T15:30:00Z",
    )

    selection = select_proof([relevant, unrelated], _requirement(), observed_at=NOW)

    assert selection.latest_run == relevant
    assert selection.latest_run_failed is True
    assert all(artifact_id != unrelated.artifact_id for artifact_id, _ in selection.rejected)


def test_untrusted_records_remain_visible_but_never_qualify() -> None:
    manual = _record(producer_class="manual")

    selection = select_proof([manual], _requirement(), observed_at=NOW)

    assert selection.qualifying_pass is None
    assert selection.latest_run == manual
    assert selection.rejected == ((manual.artifact_id, ("proof_untrusted_producer",)),)
