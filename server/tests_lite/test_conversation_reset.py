from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.qa.antigravity_conversation_reset import ISOLATED_WORKER_ENABLE_ENV
from zerg.qa.antigravity_conversation_reset import _accept_workspace_trust_if_prompted
from zerg.qa.antigravity_conversation_reset import run as run_antigravity_reset
from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import evaluate_reset_observation
from zerg.qa.conversation_reset import execution_summary
from zerg.qa.conversation_reset import generated_fake_observation
from zerg.qa.conversation_reset import longhouse_provider_aliases
from zerg.qa.conversation_reset import longhouse_source_binding
from zerg.qa.conversation_reset import observation_exit_code
from zerg.qa.conversation_reset import produce_live_reset_artifact
from zerg.qa.conversation_reset import reset_artifact_env
from zerg.qa.conversation_reset import tail_sequence
from zerg.qa.conversation_reset_qualification import ASSERTIONS as QUALIFICATION_ASSERTIONS
from zerg.qa.conversation_reset_qualification import PROFILE_BY_PROVIDER
from zerg.qa.conversation_reset_qualification import _executor as qualification_executor
from zerg.qa.provider_adapters.antigravity import AntigravityHarnessAdapter
from zerg.qa.provider_adapters.claude import ClaudeCodeHarnessAdapter
from zerg.qa.provider_adapters.codex import CodexOpenAIHarnessAdapter
from zerg.qa.provider_adapters.cursor import CursorHarnessAdapter
from zerg.qa.provider_adapters.opencode import OpenCodeHarnessAdapter
from zerg.qa.provider_build_store import ProviderBuildRef
from zerg.qa.universal_agent_harness import AdapterConfig
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import run_scenario
from zerg.services.antigravity_hook_inbox import _ANTIGRAVITY_HOOK_SCRIPT


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
    reused = evaluate_reset_observation(generated_fake_observation("antigravity", allocation="not_applicable", transition="reused"))

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


def test_reset_oracle_reports_stale_active_alias() -> None:
    observation = generated_fake_observation("claude")
    observation["longhouse"].update(
        provider_alias_ids=[observation["before"]["provider_session_id"]],
        provider_alias_matches_before=True,
        provider_alias_matches_after=False,
    )

    result = evaluate_reset_observation(observation)

    assert result["status"] == "fail"
    assert result["failed_assertions"] == ["longhouse_alias_targets_active_identity"]


def test_reset_oracle_fails_an_observed_unbound_source() -> None:
    observation = generated_fake_observation("opencode")
    observation["longhouse"]["source_binding_matches"] = False

    result = evaluate_reset_observation(observation)

    assert result["status"] == "fail"
    assert result["failed_assertions"] == ["longhouse_source_bound_to_managed_session"]


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
        assert (tmp_path / "evidence" / provider / "conversation_reset" / "observations" / "conversation_reset.json").is_file()


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


def test_real_reset_resume_fails_closed_until_targeted_live_evidence_exists(tmp_path: Path) -> None:
    build_root = tmp_path / "builds" / "claude"
    build_root.mkdir(parents=True)
    result = run_scenario(
        _adapter("claude", build_root, provenance="staged_release"),
        "conversation_reset_resume",
        evidence_root=tmp_path / "evidence",
    )

    assert result.status == "blocked"
    assert result.failure_code == "conversation_reset_resume_live_adapter_missing"


def test_tail_sequence_uses_event_order_not_json_metadata() -> None:
    marker_a = "RESET_MARKER_A"
    marker_b = "RESET_MARKER_B"
    payload = {
        "metadata": f"{marker_b} /clear {marker_a}",
        "events": [
            {"content": marker_a},
            {"content": "<command-name>/clear</command-name>"},
            {"content": marker_b},
        ],
    }

    result = tail_sequence(payload, marker_a, "/clear", marker_b)

    assert result["reset_boundary_observable"] is True
    assert result["event_indices"] == {"marker_a": 0, "reset": 1, "marker_b": 2}
    assert result["tail_marker_order"][1] == "reset"


def test_execution_summary_distinguishes_completed_canary_from_semantic_failure(tmp_path: Path) -> None:
    observation = generated_fake_observation("codex")
    observation["archive"]["reset_boundary_observable"] = False
    summary = execution_summary(
        observation,
        observation_path=tmp_path / "observation.json",
        terminal_path=tmp_path / "terminal.raw",
    )

    assert summary["execution_status"] == "completed"
    assert summary["semantic_status"] == "fail"
    assert summary["failed_assertions"] == ["reset_boundary_observable"]


def test_observation_exit_code_tracks_semantic_result() -> None:
    passing = generated_fake_observation("codex")
    failing = generated_fake_observation("codex")
    failing["archive"]["reset_boundary_observable"] = False

    assert observation_exit_code(passing) == 0
    assert observation_exit_code(failing) == 1


def test_binding_queries_honor_longhouse_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path))
    db = tmp_path / "agent" / "longhouse-shipper.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE source_epoch_registry (
                source_epoch TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_session_id TEXT,
                bound_session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO source_epoch_registry VALUES (?, 'opencode', ?, 'longhouse-1', ?, ?)",
            [
                ("epoch-a", "ses_a", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
                ("epoch-b", "ses_b", "2026-08-01T00:01:00Z", "2026-08-01T00:01:00Z"),
            ],
        )

    assert longhouse_source_binding("opencode", "ses_b") == "longhouse-1"
    assert longhouse_provider_aliases("opencode", "longhouse-1") == ("ses_a", "ses_b")


def test_live_producer_does_not_reuse_a_stale_observation(tmp_path: Path, monkeypatch) -> None:
    stale = tmp_path / "20260731T000000Z" / "conversation-reset-observation.json"
    stale.parent.mkdir()
    stale.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "zerg.qa.conversation_reset.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="failed", stderr="boom", returncode=1),
    )

    produced = produce_live_reset_artifact("claude", tmp_path / "claude", tmp_path)

    assert produced is None


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


def test_cursor_adapter_projects_live_reset_observation(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "cursor-agent"
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then echo 2026.07.23-test; exit 0; fi\nexit 2\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    observation = generated_fake_observation("cursor")
    observation.update(
        evidence_class="live_token",
        provider_version="2026.07.23-test",
        provider_executable_identity=f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}",
    )
    artifact = tmp_path / "cursor-reset.json"
    artifact.write_text(json.dumps(observation), encoding="utf-8")
    monkeypatch.setenv(reset_artifact_env("cursor"), str(artifact))
    adapter = CursorHarnessAdapter(
        AdapterConfig(provider="cursor", binary_name="cursor-agent", binary_env=None),
        provider_bin=binary,
    )

    result = run_scenario(adapter, "conversation_reset", evidence_root=tmp_path / "evidence")

    assert result.status == "pass"
    assert result.data is not None
    assert result.data["synthetic"] is False
    assert result.data["operation_evidence"]["conversation_reset"]["level"] == "live_token"


@pytest.mark.parametrize(
    ("provider", "adapter_type"),
    (
        ("claude", ClaudeCodeHarnessAdapter),
        ("codex", CodexOpenAIHarnessAdapter),
        ("opencode", OpenCodeHarnessAdapter),
        ("antigravity", AntigravityHarnessAdapter),
    ),
)
def test_provider_adapter_consumes_exact_binary_live_reset_artifact(
    provider: str,
    adapter_type: type[UniversalProviderAdapter],
    tmp_path: Path,
    monkeypatch,
) -> None:
    binary = tmp_path / provider
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then echo 1.2.3-test; exit 0; fi\nexit 2\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    observation = generated_fake_observation(provider)
    observation.update(
        evidence_class="live_token",
        provider_version="1.2.3-test",
        provider_executable_identity=f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}",
    )
    artifact = tmp_path / f"{provider}-reset.json"
    artifact.write_text(json.dumps(observation), encoding="utf-8")
    monkeypatch.setenv(reset_artifact_env(provider), str(artifact))
    adapter = adapter_type(
        AdapterConfig(provider=provider, binary_name=provider, binary_env=None),
        provider_bin=binary,
    )

    result = run_scenario(adapter, "conversation_reset", evidence_root=tmp_path / "evidence")

    assert result.status == "pass"
    assert result.data is not None
    assert result.data["synthetic"] is False
    assert result.data["operation_evidence"]["conversation_reset"]["level"] == "live_token"


def test_strict_profile_executor_emits_three_independent_live_axes(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "claude"
    binary.write_text("exact provider bytes", encoding="utf-8")
    observation = generated_fake_observation("claude")
    observation.update(
        evidence_class="live_token",
        provider_version="2.1.219",
        provider_executable_identity=f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}",
    )
    artifact = tmp_path / "claude-reset.json"
    artifact.write_text(json.dumps(observation), encoding="utf-8")
    monkeypatch.setenv(reset_artifact_env("claude"), str(artifact))

    result, assertions, secrets = qualification_executor("claude", binary, tmp_path / "evidence")

    assert result["status"] == "pass"
    assert tuple(item.assertion_id for item in assertions) == QUALIFICATION_ASSERTIONS
    assert {item.outcome.value for item in assertions} == {"pass"}
    assert {item.evidence_class.value for item in assertions} == {"live_token"}
    assert secrets == ()
    assert PROFILE_BY_PROVIDER["claude"] == "claude_conversation_reset_v1"


def test_strict_profile_reports_stale_longhouse_alias_independently(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "claude"
    binary.write_text("exact provider bytes", encoding="utf-8")
    observation = generated_fake_observation("claude")
    observation.update(
        evidence_class="live_token",
        provider_version="2.1.219",
        provider_executable_identity=f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}",
    )
    observation["longhouse"].update(
        provider_alias_ids=[observation["before"]["provider_session_id"]],
        provider_alias_matches_before=True,
        provider_alias_matches_after=False,
    )
    artifact = tmp_path / "claude-reset-stale-alias.json"
    artifact.write_text(json.dumps(observation), encoding="utf-8")
    monkeypatch.setenv(reset_artifact_env("claude"), str(artifact))

    result, assertions, _secrets = qualification_executor("claude", binary, tmp_path / "evidence")

    assert result["status"] == "fail"
    assert [item.outcome.value for item in assertions] == ["pass", "pass", "semantic_fail"]


def test_antigravity_live_reset_fails_closed_outside_isolated_worker(monkeypatch) -> None:
    monkeypatch.delenv(ISOLATED_WORKER_ENABLE_ENV, raising=False)

    with pytest.raises(RuntimeError, match="isolated unwatched worker"):
        run_antigravity_reset(SimpleNamespace(longhouse_session_id="factory-session"))


def test_antigravity_live_reset_accepts_workspace_trust_prompt(tmp_path: Path) -> None:
    terminal = tmp_path / "terminal.raw"
    terminal.write_text("Do you trust the contents of this project?", encoding="utf-8")
    submitted: list[str] = []
    session = SimpleNamespace(submit_line=submitted.append, alive=lambda: True)

    assert _accept_workspace_trust_if_prompted(session, terminal, timeout=1.0) is True
    assert submitted == [""]


def test_antigravity_hook_fails_closed_when_transcript_binding_fails(tmp_path: Path) -> None:
    script = tmp_path / "hook.sh"
    script.write_text(
        _ANTIGRAVITY_HOOK_SCRIPT.replace("__LONGHOUSE_HOME__", str(tmp_path)).replace(
            "__ENGINE_PATH__", "/usr/bin/false"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    transcript = tmp_path / "conversation.json"
    transcript.write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = "11111111-1111-4111-8111-111111111111"
    env["LONGHOUSE_HOME"] = str(tmp_path)
    env["LONGHOUSE_ENGINE"] = "/usr/bin/false"

    completed = subprocess.run(
        [str(script), "Stop"],
        input=json.dumps(
            {
                "conversationId": "22222222-2222-4222-8222-222222222222",
                "transcriptPath": str(transcript),
                "workspacePaths": [str(tmp_path)],
                "fullyIdle": True,
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode != 0
    assert "python hook failed" in completed.stderr
