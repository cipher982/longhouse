from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime

from zerg.services.provider_capability_projection import NEVER_PROVEN
from zerg.services.provider_capability_projection import STALE
from zerg.services.provider_capability_projection import UNACCEPTABLE_EVIDENCE
from zerg.services.provider_capability_projection import project_capabilities
from zerg.services.provider_capability_proof import LEGACY_PROOF_SCHEMA_VERSION
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_schema import CapabilityAssertion


def _assertion(
    *, provider: str = "codex", assertion_id: str = "interrupt_terminal_cancelled", variant: str | None = None, max_age_seconds: int = 3600
) -> CapabilityAssertion:
    return CapabilityAssertion(
        scenario_id="interrupt_cancel",
        assertion_id=assertion_id,
        variant=variant,
        minimum_scenario_revision=1,
        provider=provider,
        capability="interrupt",
        oracle_source="codex_helm_interrupt",
        acceptable_evidence=("live_token",),
        max_age_seconds=max_age_seconds,
    )


def _record(
    *,
    provider: str = "codex",
    assertion_id: str = "interrupt_terminal_cancelled",
    outcome: AssertionOutcome = AssertionOutcome.PASS,
    generated_at: str = "2026-07-29T00:00:00+00:00",
    evidence_class: EvidenceClass = EvidenceClass.LIVE_TOKEN,
) -> ProviderCapabilityProofRecord:
    return ProviderCapabilityProofRecord(
        provider=provider,
        provider_version="1.2.3",
        provider_executable_identity="sha256:" + "a" * 64,
        provider_contract_digest="sha256:" + "b" * 64,
        adapter_digest="sha256:" + "c" * 64,
        scenario_id="interrupt_cancel",
        scenario_revision=1,
        oracle_digest="sha256:" + "d" * 64,
        assertion_id=assertion_id,
        outcome=outcome,
        evidence_class=evidence_class,
        generated_at=generated_at,
        producer_class="release_factory",
        producer_version="1",
        invocation_id="test-invocation",
        factory_source_sha="f" * 40,
        accepted_epoch_id="factory-v3-test",
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
        observed_activity=("provider_activity",),
        credential_binding_facts={"provider_token": "admitted"},
    )


def test_never_proven_capability_is_labeled_not_omitted():
    projections = project_capabilities((_assertion(),), [])
    assert len(projections) == 1
    assert projections[0].declared is True
    assert projections[0].proof_status == NEVER_PROVEN
    assert projections[0].generated_at is None


def test_fresh_passing_proof_is_attached():
    now = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
    record = _record(generated_at="2026-07-29T00:30:00+00:00")
    projections = project_capabilities((_assertion(),), [record], now=now)
    assert projections[0].proof_status == "pass"
    assert projections[0].generated_at == "2026-07-29T00:30:00+00:00"
    assert projections[0].evidence_class == "live_token"


def test_legacy_proof_is_historical_for_non_variant_assertion():
    now = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
    legacy = replace(
        _record(generated_at="2026-07-29T00:30:00+00:00"),
        schema_version=LEGACY_PROOF_SCHEMA_VERSION,
    )

    projection = project_capabilities((_assertion(),), [legacy], now=now)[0]

    assert projection.proof_status == UNACCEPTABLE_EVIDENCE
    assert "proof_schema_legacy" in projection.admissibility_reasons


def test_proof_older_than_max_age_is_stale_not_silently_pass():
    now = datetime(2026, 7, 29, 3, 0, 0, tzinfo=UTC)
    record = _record(generated_at="2026-07-29T00:00:00+00:00")
    projections = project_capabilities((_assertion(max_age_seconds=3600),), [record], now=now)
    assert projections[0].proof_status == STALE


def test_semantic_failure_is_not_masked_by_freshness():
    now = datetime(2026, 7, 29, 0, 5, 0, tzinfo=UTC)
    record = _record(generated_at="2026-07-29T00:00:00+00:00", outcome=AssertionOutcome.SEMANTIC_FAIL)
    projections = project_capabilities((_assertion(),), [record], now=now)
    assert projections[0].proof_status == "semantic_fail"


def test_multiple_records_for_the_same_assertion_pick_the_freshest():
    now = datetime(2026, 7, 29, 0, 5, 0, tzinfo=UTC)
    older_pass = _record(generated_at="2026-07-28T00:00:00+00:00", outcome=AssertionOutcome.PASS)
    newer_fail = _record(generated_at="2026-07-29T00:00:00+00:00", outcome=AssertionOutcome.SEMANTIC_FAIL)
    projections = project_capabilities((_assertion(),), [older_pass, newer_fail], now=now)
    assert projections[0].proof_status == "semantic_fail"
    assert projections[0].generated_at == "2026-07-29T00:00:00+00:00"


def test_records_for_a_different_provider_do_not_leak_across():
    record = _record(provider="claude")
    projections = project_capabilities((_assertion(provider="codex"),), [record])
    assert projections[0].proof_status == NEVER_PROVEN


def test_stale_semantic_failure_is_not_masked_as_merely_stale():
    # Review 2026-07-29 (Fable/Grok, independently): the original join
    # collapsed staleness and outcome into one field, so an old
    # semantic_fail rendered as "stale" -- visually upgrading a known break
    # to "merely old" and losing the one fact that matters most. Staleness
    # must only demote a passing result.
    now = datetime(2026, 7, 29, 3, 0, 0, tzinfo=UTC)
    record = _record(generated_at="2026-07-29T00:00:00+00:00", outcome=AssertionOutcome.SEMANTIC_FAIL)
    projections = project_capabilities((_assertion(max_age_seconds=3600),), [record], now=now)
    assert projections[0].proof_status == "semantic_fail"


def test_wrong_evidence_class_does_not_count_as_proof():
    # Review 2026-07-29: acceptable_evidence was attached for display but
    # never joined into proof_status. A hermetic proof for an assertion
    # whose schema entry only accepts live_token never counted as real
    # proof of anything -- it must not render as "pass".
    assertion = _assertion()
    assert assertion.acceptable_evidence == ("live_token",)
    record = _record(evidence_class=EvidenceClass.HERMETIC, outcome=AssertionOutcome.PASS)
    projections = project_capabilities((assertion,), [record])
    assert projections[0].proof_status == UNACCEPTABLE_EVIDENCE


def test_wrong_evidence_class_applies_even_to_a_failing_outcome():
    # A hermetic "fail" for a live_token-only assertion is just as
    # uninformative as a hermetic "pass" -- the evidence class disqualifies
    # the record before outcome is ever considered.
    record = _record(evidence_class=EvidenceClass.HERMETIC, outcome=AssertionOutcome.SEMANTIC_FAIL)
    projections = project_capabilities((_assertion(),), [record])
    assert projections[0].proof_status == UNACCEPTABLE_EVIDENCE


def test_unparseable_timestamp_fails_safe_to_stale_not_eternally_fresh():
    # Review 2026-07-29: an unparseable generated_at produced age_seconds is
    # None, which the original join treated as "not stale" -- a proof with a
    # garbage timestamp rendered as fresh forever. It must fail toward
    # distrust: unverifiable freshness is stale, not fresh.
    record = _record(generated_at="not-a-real-timestamp", outcome=AssertionOutcome.PASS)
    projections = project_capabilities((_assertion(),), [record])
    assert projections[0].proof_status == STALE


def test_every_declared_assertion_produces_exactly_one_row():
    assertions = (
        _assertion(assertion_id="a"),
        _assertion(assertion_id="b"),
        _assertion(assertion_id="c"),
    )
    projections = project_capabilities(assertions, [_record(assertion_id="b")])
    assert {p.assertion_id for p in projections} == {"a", "b", "c"}
    assert len(projections) == 3


def test_non_variant_record_cannot_prove_a_variant_requirement():
    assertion = _assertion(assertion_id="native_provider_resume_proven", variant="clean_exit")
    record = _record(assertion_id="native_provider_resume_proven", evidence_class=EvidenceClass.LIVE_TOKEN)

    projection = project_capabilities((assertion,), [record])[0]

    assert projection.variant == "clean_exit"
    assert projection.proof_status == UNACCEPTABLE_EVIDENCE
