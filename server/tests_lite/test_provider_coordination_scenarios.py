import shutil
from pathlib import Path

import pytest

from zerg.qa.provider_coordination_scenarios import observe_codex_post_compaction_bootstrap
from zerg.qa.provider_coordination_scenarios import publish_codex_bootstrap_noise_proof
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
def test_codex_compaction_driver_emits_no_visible_startup_cards() -> None:
    observation = observe_codex_post_compaction_bootstrap(compactions=4)

    assert observation["visible_bootstrap_count"] == 0
    assert observation["mcp_coordination_instructions_present"] is True


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
def test_codex_compaction_driver_publishes_real_hermetic_assertion(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)

    artifact_id = publish_codex_bootstrap_noise_proof(
        provider_version="codex-cli 0.145.0",
        provider_executable_identity="sha256:provider",
        store=store,
        producer_class="local_diagnostic",
        producer_version="2",
        invocation_id="run-1",
        generated_at="2026-07-22T18:00:00Z",
    )

    [record] = store.records("codex")
    assert record.artifact_id == artifact_id
    assert record.assertion_id == "no_duplicate_visible_bootstrap"
    assert record.outcome is AssertionOutcome.PASS
