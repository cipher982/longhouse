from __future__ import annotations

from pathlib import Path

import pytest

import zerg.qa.title_dependency_oracles as oracles
from zerg.qa.title_dependency_live_producer import REGISTRATION as LIVE_REGISTRATION
from zerg.qa.title_dependency_recovery_producer import REGISTRATION as RECOVERY_REGISTRATION


def test_title_product_registrations_have_no_provider_axis():
    for registration in (RECOVERY_REGISTRATION, LIVE_REGISTRATION):
        payload = registration.to_dict()
        assert payload["subject_kind"] == "longhouse_product"
        assert payload["providers"] == []
        assert payload["provider_artifact_required"] is False
        assert payload["sandbox_policy"] == "provider-qualification-bwrap-v3"
        assert payload["network_policy"] == "shared_provider_egress"


def test_live_title_oracle_only_uses_runtime_host_authority(tmp_path, monkeypatch):
    monkeypatch.setenv(oracles.RUNTIME_API_URL_ENV, "https://runtime.example")
    monkeypatch.setenv(oracles.RUNTIME_API_TOKEN_ENV, "runtime-token")
    calls: list[str] = []
    session_id = "5ad7f89a-f51a-4937-bca8-4ffc05497574"

    monkeypatch.setattr(
        oracles,
        "_capabilities",
        lambda *_args, **_kwargs: {"tenant_id": "tenant", "machine_id": "machine"},
    )
    monkeypatch.setattr(
        oracles,
        "_envelope",
        lambda **_kwargs: (
            session_id,
            {
                "session_id": session_id,
                "session": {
                    "environment": "local",
                    "origin_kind": "console",
                    "hidden_from_default_timeline": True,
                },
            },
        ),
    )

    def post(*_args, **_kwargs):
        calls.append("runtime_write")
        return {"status_code": 200, "session_id": session_id, "receipt": {"raw_state": "durable"}}

    monkeypatch.setattr(oracles, "_post_envelope", post)
    monkeypatch.setattr(
        oracles,
        "_session_projection",
        lambda *_args, **_kwargs: {
            "id": session_id,
            "provider": "claude",
            "title": "Healthy Hidden Obligation",
            "anchor_title": "Healthy Hidden Obligation",
            "title_state": "ready",
            "title_source": "ai",
        },
    )
    monkeypatch.setattr(
        oracles,
        "_product_health",
        lambda *_args, **_kwargs: (
            {"check": "session_titles", "verdict": "ok", "signals": {}},
            {
                "check": "session_titles",
                "verdict": "ok",
                "signals": {"open_dependencies": 0, "terminal_sessions": 0, "overdue_sessions": 0},
            },
        ),
    )

    result = oracles.run_live_title_dependency_oracle(evidence_root=tmp_path / "evidence")

    assert result["passed"] is True, result
    assert calls == ["runtime_write"]
    assert result["observation"]["claude_semantic_path_consumed"] is True
    assert result["observation"]["direct_provider_probe_count"] == 0
    assert result["observation"]["credential_rotation_count"] == 0
    request_receipt = (tmp_path / "evidence" / "runtime-request-receipt.json").read_text()
    assert 'direct_provider_paths": []' in request_receipt
    assert 'credential_mutations": []' in request_receipt


def test_live_title_envelope_exercises_native_claude_semantic_projection():
    session_id, payload = oracles._envelope(tenant_id="tenant", machine_id="machine", message="Human request")

    assert payload["session_id"] == session_id
    assert payload["provider"] == "claude"
    assert payload["session"]["origin_kind"] == "console"
    raw = payload["records"][0]["data_b64"]
    assert raw
    assert LIVE_REGISTRATION.producer_revision == 3
    assert LIVE_REGISTRATION.scenario_revision == 3
    assert "claude_semantic_path_consumed" in LIVE_REGISTRATION.observed_activity
    assert RECOVERY_REGISTRATION.producer_revision == 4
    assert RECOVERY_REGISTRATION.scenario_revision == 4
    assert "legacy_exact_provider_proof_excluded_from_title_debt" in RECOVERY_REGISTRATION.observed_activity


@pytest.mark.timeout(120)
def test_hermetic_title_oracle_proves_incident_restart_and_recovery(tmp_path):
    evidence_root = tmp_path / "evidence"
    result = oracles.run_hermetic_title_dependency_oracle(
        evidence_root=evidence_root,
        repo_root=Path(__file__).resolve().parents[2],
    )

    assert result["passed"] is True, result
    observation = result["observation"]
    assert observation["concurrent_hidden_obligation_count"] == 8
    assert observation["one_shared_incident"] is True
    assert observation["incident_survived_restart"] is True
    assert observation["zero_new_row_attempt_consumption"] is True
    assert observation["legacy_terminal_timeout_reentered"] is True
    assert observation["terminal_empty_response_reentered"] is True
    assert observation["row_local_empty_response_isolated"] is True
    assert observation["unrelated_terminal_debt_preserved"] is True
    assert observation["legacy_exact_provider_proof_excluded_from_title_debt"] is True
    assert observation["provider_shaped_503_observed"] is True
    assert observation["model_concurrency_bounded"] is True
    assert observation["scheduled_worker_creation_bounded"] is True
    assert observation["aged_backlog_degrades_with_healthy_dependency"] is True
    assert observation["same_rows_recovered"] is True
    assert observation["storage_v2_read_count"] == 0
