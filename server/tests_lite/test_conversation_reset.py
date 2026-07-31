from __future__ import annotations

from pathlib import Path

from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import evaluate_reset_observation
from zerg.qa.conversation_reset import generated_fake_observation
from zerg.qa.provider_build_store import ProviderBuildRef
from zerg.qa.universal_agent_harness import AdapterConfig
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import run_scenario


def _build(provider: str, root: Path, *, provenance: str = "generated_fake") -> ProviderBuildRef:
    binary = root / provider
    binary.write_text("fake", encoding="utf-8")
    return ProviderBuildRef(
        provider=provider,
        version="fake-test",
        platform="darwin",
        architecture="aarch64",
        artifact_provenance=provenance,
        closure_manifest_version=2,
        closure_granularity="single_asset",
        closure_digest=f"digest-{provider}",
        build_root=root,
        entrypoint_relative=provider,
    )


def _adapter(provider: str, root: Path, *, provenance: str = "generated_fake") -> UniversalProviderAdapter:
    build = _build(provider, root, provenance=provenance)
    return UniversalProviderAdapter(
        AdapterConfig(provider=provider, binary_name=provider, binary_env=None),
        provider_bin=build.entrypoint,
        provider_build=build,
    )


def test_identity_transition_classification() -> None:
    assert classify_identity_transition("before", "after") == "rotated"
    assert classify_identity_transition("same", "same") == "reused"
    assert classify_identity_transition("before", None) == "unobserved"


def test_generated_fake_reset_oracle_accepts_eager_lazy_and_reused() -> None:
    eager = evaluate_reset_observation(generated_fake_observation("claude", allocation="eager"))
    lazy = evaluate_reset_observation(generated_fake_observation("codex", allocation="lazy"))
    reused = evaluate_reset_observation(
        generated_fake_observation("antigravity", allocation="not_applicable", transition="reused")
    )

    assert eager["status"] == "pass"
    assert lazy["status"] == "pass"
    assert reused["status"] == "pass"
    assert reused["observed_identity_transition"] == "reused"


def test_reset_oracle_reports_independent_archive_and_provider_failures() -> None:
    observation = generated_fake_observation("cursor")
    observation["archive"]["pre_reset_raw_preserved"] = False
    observation["provider_transition"]["post_reset_turn_bound_to_active_identity"] = False

    result = evaluate_reset_observation(observation)

    assert result["status"] == "fail"
    assert result["failed_assertions"] == [
        "post_reset_turn_bound_to_active_identity",
        "pre_reset_raw_preserved",
    ]


def test_generated_fake_harness_runs_reset_for_every_provider(tmp_path: Path) -> None:
    for provider in ("codex", "claude", "opencode", "antigravity", "cursor"):
        provider_root = tmp_path / "builds" / provider
        provider_root.mkdir(parents=True)
        result = run_scenario(
            _adapter(provider, provider_root),
            "conversation_reset",
            evidence_root=tmp_path / "evidence",
        )

        assert result.status == "pass"
        assert result.data is not None
        assert result.data["identity_transition"] == "rotated"
        assert (
            tmp_path
            / "evidence"
            / provider
            / "conversation_reset"
            / "observations"
            / "conversation_reset.json"
        ).is_file()


def test_reset_resume_is_not_applicable_for_antigravity(tmp_path: Path) -> None:
    build_root = tmp_path / "builds" / "antigravity"
    build_root.mkdir(parents=True)
    result = run_scenario(
        _adapter("antigravity", build_root),
        "conversation_reset_resume",
        evidence_root=tmp_path / "evidence",
    )

    assert result.status == "not_applicable"
    assert result.failure_code is None


def test_real_build_fails_closed_without_provider_adapter(tmp_path: Path) -> None:
    build_root = tmp_path / "builds" / "claude"
    build_root.mkdir(parents=True)
    result = run_scenario(
        _adapter("claude", build_root, provenance="staged_release"),
        "conversation_reset",
        evidence_root=tmp_path / "evidence",
    )

    assert result.status == "blocked"
    assert result.failure_code == "conversation_reset_live_adapter_missing"
