import json
from pathlib import Path

import pytest

from zerg.qa.provider_build_store import GENERATED_FAKE_PROVENANCE
from zerg.qa.provider_build_store import ProviderBuildRef
from zerg.qa.provider_interaction_semantics import generated_fake_observation
from zerg.qa.qualification_request import semantic_digest
from zerg.qa.universal_agent_harness import AdapterConfig
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter


def _adapter(
    tmp_path: Path,
    request=None,
    interaction_artifact_path: Path | None = None,
    provider_build: ProviderBuildRef | None = None,
) -> UniversalProviderAdapter:
    return UniversalProviderAdapter(
        AdapterConfig(provider="claude", binary_name="claude", binary_env=None),
        provider_bin=tmp_path / "claude",
        qualification_request=request,
        interaction_artifact_path=interaction_artifact_path,
        provider_build=provider_build,
    )


def _live_request(tmp_path: Path) -> dict:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "live_no_token"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    return request


def test_ambient_interaction_flag_does_not_enable_live_probe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_PROVIDER_INTERACTION_LIVE", "1")
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path).interaction_semantics(package)

    assert result["status"] == "blocked"
    assert result["failure_code"] == "interaction_live_policy_missing"


def test_qualification_request_policy_enables_live_probe(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_probe(
        provider,
        *,
        provider_bin,
        artifact_root,
        qualification_request_digest,
        evidence_class,
        timeout=60.0,
    ):
        calls.append((provider, provider_bin, artifact_root, timeout))
        observation = generated_fake_observation(provider)
        observation.update(
            {
                "evidence_class": evidence_class,
                "synthetic": False,
                "provider_version": "2.1.219",
                "provider_executable_identity": "sha256:" + "a" * 64,
                "qualification_request_digest": qualification_request_digest,
            }
        )
        return observation

    from zerg.qa import provider_interaction_probe

    monkeypatch.setattr(provider_interaction_probe, "produce_live_observation", fake_probe)
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "live_no_token"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request).interaction_semantics(package)

    assert calls and calls[0][0] == "claude"
    assert result["scenario"] == "interaction_semantics"


def test_live_token_request_reaches_the_credentialed_probe(monkeypatch, tmp_path: Path) -> None:
    request = _live_request(tmp_path)
    request["scenario_evidence"]["fixture"] = "live_token"
    request["scenario_evidence"]["interaction_semantics"] = "live_token"
    request["evidence_class"] = "live_token"
    request["auth_mode"] = "factory_token"
    request["semantic_digest"] = semantic_digest(request)
    calls = []

    def fake_probe(
        provider,
        *,
        provider_bin,
        artifact_root,
        qualification_request_digest,
        evidence_class,
        timeout=60.0,
    ):
        calls.append(evidence_class)
        observation = generated_fake_observation(provider)
        observation.update(
            {
                "evidence_class": evidence_class,
                "synthetic": False,
                "provider_version": "2.1.219",
                "provider_executable_identity": "sha256:" + "a" * 64,
                "qualification_request_digest": qualification_request_digest,
            }
        )
        return observation

    from zerg.qa import provider_interaction_probe

    monkeypatch.setattr(provider_interaction_probe, "produce_live_observation", fake_probe)
    package = EvidencePackage(root=tmp_path / "evidence-live-token", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request).interaction_semantics(package)

    assert calls == ["live_token"]
    assert result["scenario"] == "interaction_semantics"


def test_live_probe_setup_block_binds_to_the_qualification_request(monkeypatch, tmp_path: Path) -> None:
    request = _live_request(tmp_path)

    def failing_probe(*_args, **_kwargs):
        raise RuntimeError("isolated provider setup failed")

    from zerg.qa import provider_interaction_probe

    monkeypatch.setattr(provider_interaction_probe, "produce_live_observation", failing_probe)
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request).interaction_semantics(package)

    assert result["status"] == "blocked"
    assert result["failure_code"] == "interaction_live_probe_setup_failed"
    assert result["qualification_request_digest"] == request["semantic_digest"]


@pytest.mark.parametrize("mutation", ("synthetic", "version", "executable", "provider", "digest"))
def test_live_probe_output_must_bind_to_request(monkeypatch, tmp_path: Path, mutation: str) -> None:
    request = _live_request(tmp_path)

    def fake_probe(
        provider,
        *,
        provider_bin,
        artifact_root,
        qualification_request_digest,
        evidence_class,
        timeout=60.0,
    ):
        observation = generated_fake_observation(provider)
        observation.update(
            {
                "evidence_class": evidence_class,
                "synthetic": False,
                "provider_version": "2.1.219",
                "provider_executable_identity": "sha256:" + "a" * 64,
                "qualification_request_digest": qualification_request_digest,
            }
        )
        if mutation == "synthetic":
            observation["synthetic"] = True
        elif mutation == "version":
            observation["provider_version"] = "9.9.9"
        elif mutation == "executable":
            observation["provider_executable_identity"] = "sha256:" + "9" * 64
        elif mutation == "provider":
            observation["provider"] = "opencode"
        else:
            observation["qualification_request_digest"] = "sha256:" + "9" * 64
        return observation

    from zerg.qa import provider_interaction_probe

    monkeypatch.setattr(provider_interaction_probe, "produce_live_observation", fake_probe)
    package = EvidencePackage(root=tmp_path / f"evidence-{mutation}", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request).interaction_semantics(package)

    assert result["status"] == "fail"
    assert result["failure_code"] in {
        "interaction_live_probe_synthetic",
        "interaction_live_probe_identity_mismatch",
    }


def test_validated_hermetic_request_emits_explicit_synthetic_observation_for_a_real_build(tmp_path: Path) -> None:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "hermetic"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request).interaction_semantics(package)

    assert result["status"] == "pass"
    assert result["synthetic"] is True
    assert result["evidence_class"] == "hermetic"


def test_unvalidated_request_cannot_enable_live_probe(tmp_path: Path) -> None:
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(
        tmp_path,
        {"scenario_evidence": {"interaction_semantics": "live_no_token"}},
    ).interaction_semantics(package)

    assert result["status"] == "blocked"
    assert result["failure_code"] == "interaction_live_policy_missing"


def test_explicit_artifact_must_match_request_evidence(tmp_path: Path) -> None:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "live_no_token"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    artifact = generated_fake_observation("claude")
    artifact["qualification_request_digest"] = request["semantic_digest"]
    artifact_path = tmp_path / "interaction.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    artifact["provider"] = "opencode"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    provider_mismatch = _adapter(tmp_path, request, artifact_path).interaction_semantics(package)

    assert provider_mismatch["status"] == "fail"
    assert provider_mismatch["failure_code"] == "interaction_artifact_provider_mismatch"

    artifact["provider"] = "claude"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    mismatch = _adapter(tmp_path, request, artifact_path).interaction_semantics(package)

    assert mismatch["status"] == "fail"
    assert mismatch["failure_code"] == "interaction_artifact_evidence_mismatch"

    request["evidence_class"] = "hermetic"
    request["scenario_evidence"] = {"fixture": "hermetic", "interaction_semantics": "hermetic"}
    request["semantic_digest"] = semantic_digest(request)
    artifact["qualification_request_digest"] = "sha256:" + "9" * 64
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    digest_mismatch = _adapter(tmp_path, request, artifact_path).interaction_semantics(package)

    assert digest_mismatch["status"] == "fail"
    assert digest_mismatch["failure_code"] == "interaction_artifact_request_mismatch"

    artifact["qualification_request_digest"] = request["semantic_digest"]
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    result = _adapter(tmp_path, request, artifact_path).interaction_semantics(package)

    assert result["status"] == "pass"
    assert result["qualification_request_digest"] == request["semantic_digest"]


def test_generated_fake_cannot_claim_live_evidence_from_an_artifact(tmp_path: Path) -> None:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "live_no_token"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    artifact = generated_fake_observation("claude")
    artifact["evidence_class"] = "live_no_token"
    artifact_path = tmp_path / "interaction.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    build_root = tmp_path / "build"
    build_root.mkdir()
    provider_build = ProviderBuildRef(
        provider="claude",
        version="2.1.219",
        platform="darwin",
        architecture="arm64",
        artifact_provenance=GENERATED_FAKE_PROVENANCE,
        closure_manifest_version=1,
        closure_granularity="single_asset",
        closure_digest="d" * 64,
        build_root=build_root,
        entrypoint_relative="claude",
    )
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request, artifact_path, provider_build).interaction_semantics(package)

    assert result["status"] == "blocked"
    assert result["failure_code"] == "interaction_live_requires_real_build"


def test_explicit_live_artifact_rejects_synthetic_observation(tmp_path: Path) -> None:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "fixture",
        "scenario_ids": ["fixture", "interaction_semantics"],
        "scenario_evidence": {"fixture": "live_no_token", "interaction_semantics": "live_no_token"},
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "provider_bin": str(tmp_path / "claude"),
        "invocation_id": "run-1",
        "producer_class": "test",
        "producer_version": "1",
        "run_reference": "test://run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "test",
    }
    request["semantic_digest"] = semantic_digest(request)
    artifact = generated_fake_observation("claude")
    artifact["evidence_class"] = "live_no_token"
    artifact_path = tmp_path / "interaction.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    package = EvidencePackage(root=tmp_path / "evidence", provider="claude", scenario="interaction_semantics")

    result = _adapter(tmp_path, request, artifact_path).interaction_semantics(package)

    assert result["status"] == "fail"
    assert result["failure_code"] == "interaction_artifact_synthetic"
