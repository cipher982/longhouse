from __future__ import annotations

from pathlib import Path

import pytest

from zerg.qa.provider_capability_proof_publish import ScenarioProofIdentity
from zerg.qa.provider_capability_proof_publish import publish_scenario_assertions
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _identity() -> ScenarioProofIdentity:
    return ScenarioProofIdentity(
        provider="codex",
        provider_version="0.145.0",
        provider_executable_identity="sha256:provider",
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        scenario_id="codex_coordination_directed_input",
        scenario_revision=1,
        oracle_digest="sha256:oracle",
        evidence_class=EvidenceClass.HERMETIC,
        generated_at="2026-07-22T17:00:00Z",
        producer_class="release_ci",
        producer_version="2",
        invocation_id="run-123",
        mode="helm",
        raw_reference_digests=("sha256:raw",),
    )


def test_publisher_emits_one_immutable_record_per_assertion(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)

    records = publish_scenario_assertions(
        identity=_identity(),
        assertions={
            "attributed_input_visible": False,
            "provider_input_receipt_linked": True,
        },
        store=store,
    )

    assert [record.assertion_id for record in records] == [
        "attributed_input_visible",
        "provider_input_receipt_linked",
    ]
    assert [record.outcome for record in records] == [AssertionOutcome.SEMANTIC_FAIL, AssertionOutcome.PASS]
    assert {record.artifact_id for record in store.records("codex")} == {record.artifact_id for record in records}


@pytest.mark.parametrize("assertions", [{}, {"assertion": "pass"}])
def test_publisher_refuses_missing_or_inferred_assertions(tmp_path: Path, assertions) -> None:
    with pytest.raises(ValueError):
        publish_scenario_assertions(
            identity=_identity(),
            assertions=assertions,
            store=ProviderCapabilityProofStore(tmp_path),
        )
