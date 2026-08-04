from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime

import pytest

from zerg.services.provider_capability_proof import LEGACY_PROOF_SCHEMA_VERSION
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


def _variant_record(**changes) -> ProviderCapabilityProofRecord:
    record = _record(
        scenario_id="codex_native_resume",
        assertion_id="native_provider_resume_proven",
        assertion_variant="clean_exit",
        factory_source_sha="f" * 40,
        accepted_epoch_id="helm-resume-v1-test",
        accepted_epoch_digest="sha256:epoch",
        verifier_bundle_digest="sha256:verifier",
        compile_report_digest="sha256:compile",
        plan_digest="sha256:plan",
        sandbox_receipt_digest="sha256:sandbox",
        cleanup_receipt_digest="sha256:cleanup",
        worker_id="factory-worker-1",
        worker_census_digest="sha256:census",
        acquisition_provenance={"method": "staged_release"},
        auth_mechanism="factory_token_v1",
        observed_activity=("native_resume_command", "post_resume_provider_activity"),
        credential_binding_facts={"codex_provider_token": "admitted"},
    )
    return replace(record, **changes)


def _variant_requirement(record: ProviderCapabilityProofRecord, **changes) -> ProofRequirement:
    requirement = _requirement(
        provider=record.provider,
        provider_version=record.provider_version,
        provider_executable_identity=record.provider_executable_identity,
        assertion_id=record.assertion_id,
        scenario_id=record.scenario_id,
        trusted_artifact_ids=frozenset({record.artifact_id}),
        assertion_variant="clean_exit",
        factory_source_sha=record.factory_source_sha,
        accepted_epoch_digest=record.accepted_epoch_digest,
        verifier_bundle_digest=record.verifier_bundle_digest,
        compile_report_digest=record.compile_report_digest,
        plan_digest=record.plan_digest,
        worker_id=record.worker_id,
        worker_census_digest=record.worker_census_digest,
        available_content_digests=frozenset(record.referenced_content_digests()),
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


def test_variant_applicability_requires_exact_v3_variant_and_retained_content() -> None:
    record = _variant_record()
    requirement = _variant_requirement(record)

    assert evaluate_proof_applicability(record, requirement, observed_at=NOW).applicable is True

    wrong_variant = _variant_record(assertion_variant="process_loss")
    wrong = evaluate_proof_applicability(
        wrong_variant,
        replace(requirement, trusted_artifact_ids=frozenset({wrong_variant.artifact_id})),
        observed_at=NOW,
    )
    assert "proof_assertion_variant_mismatch" in wrong.reason_codes

    missing_blob = evaluate_proof_applicability(
        record,
        replace(requirement, available_content_digests=frozenset()),
        observed_at=NOW,
    )
    assert "proof_referenced_content_missing" in missing_blob.reason_codes


def test_legacy_proof_cannot_satisfy_variant_scoped_requirement() -> None:
    legacy = _variant_record(assertion_variant=None, schema_version=LEGACY_PROOF_SCHEMA_VERSION)
    requirement = _variant_requirement(
        legacy,
        assertion_variant="clean_exit",
        trusted_artifact_ids=frozenset({legacy.artifact_id}),
    )

    result = evaluate_proof_applicability(legacy, requirement, observed_at=NOW)

    assert result.applicable is False
    assert "proof_assertion_variant_mismatch" in result.reason_codes
    assert "proof_schema_legacy" in result.reason_codes


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
