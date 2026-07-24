import json
import shutil
import sys
from pathlib import Path

import pytest

from zerg.qa.provider_coordination_scenarios import main
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


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
def test_coordination_driver_emits_ci_verifiable_bundle(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "proof" / "bundle.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provider_coordination_scenarios",
            "--store-root",
            str(tmp_path / "store"),
            "--provider-version",
            "hermetic-fixture",
            "--provider-executable-identity",
            "sha256:fixture",
            "--producer-class",
            "release_ci",
            "--invocation-id",
            "123:1",
            "--run-reference",
            "github-actions://cipher982/longhouse/actions/runs/123/attempts/1",
            "--longhouse-git-sha",
            "abc123",
            "--bundle-output",
            str(bundle),
        ],
    )

    assert main() == 0

    payload = json.loads(bundle.read_text())
    assert payload["artifact_kind"] == "provider_capability_proof_bundle"
    assert payload["records"][0]["producer_class"] == "release_ci"
    assert bundle.with_suffix(".raw.json").is_file()
