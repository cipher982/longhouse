from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import zerg.qa.title_dependency_live_producer as live_producer
import zerg.qa.title_dependency_oracles as oracles
import zerg.qa.title_dependency_recovery_producer as recovery_producer
from zerg.qa.title_dependency_live_producer import REGISTRATION as LIVE_REGISTRATION
from zerg.qa.title_dependency_recovery_producer import REGISTRATION as RECOVERY_REGISTRATION
from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_CWD
from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_PROJECT
from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_SURFACE
from zerg.services.internal_sessions import PROVIDER_FACTORY_MACHINE_ID


def test_title_product_registrations_have_no_provider_axis():
    for registration in (RECOVERY_REGISTRATION, LIVE_REGISTRATION):
        payload = registration.to_dict()
        assert payload["subject_kind"] == "longhouse_product"
        assert payload["providers"] == []
        assert payload["provider_artifact_required"] is False
        assert payload["sandbox_policy"] == "provider-qualification-bwrap-v3"
        assert payload["network_policy"] == "shared_provider_egress"


@pytest.mark.parametrize(
    ("module", "oracle_name"),
    (
        (live_producer, "run_live_title_dependency_oracle"),
        (recovery_producer, "run_hermetic_title_dependency_oracle"),
    ),
)
def test_title_producer_results_have_aware_generation_time(tmp_path, monkeypatch, module, oracle_name):
    monkeypatch.setattr(module, oracle_name, lambda **_kwargs: {"passed": True, "observation": {}})

    result = module.run(tmp_path / module.__name__.rsplit(".", 1)[-1])

    assert result["status"] == "pass"
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None


def test_live_title_oracle_only_uses_runtime_host_authority(tmp_path, monkeypatch):
    monkeypatch.setenv(oracles.RUNTIME_API_URL_ENV, "https://runtime.example")
    monkeypatch.setenv(oracles.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    calls: list[str] = []
    session_id = "5ad7f89a-f51a-4937-bca8-4ffc05497574"

    monkeypatch.setattr(
        oracles,
        "_capabilities",
        lambda *_args, **_kwargs: {"tenant_id": "tenant", "machine_id": PROVIDER_FACTORY_MACHINE_ID},
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
            "environment": "local",
            "project": FACTORY_TITLE_ASSURANCE_PROJECT,
            "cwd": FACTORY_TITLE_ASSURANCE_CWD,
            "device_id": PROVIDER_FACTORY_MACHINE_ID,
            "origin_kind": "console",
            "hidden_from_default_timeline": True,
            "launch_actor": "automation",
            "launch_surface": FACTORY_TITLE_ASSURANCE_SURFACE,
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
    assert result["observation"]["factory_machine_identity_verified"] is True
    assert result["observation"]["typed_title_assurance_identity_persisted"] is True
    assert result["observation"]["direct_provider_probe_count"] == 0
    assert result["observation"]["credential_rotation_count"] == 0
    request_receipt = (tmp_path / "evidence" / "runtime-request-receipt.json").read_text()
    assert 'direct_provider_paths": []' in request_receipt
    assert 'credential_mutations": []' in request_receipt


def test_live_title_envelope_exercises_native_claude_semantic_projection():
    session_id, payload = oracles._envelope(tenant_id="tenant", machine_id=PROVIDER_FACTORY_MACHINE_ID, message="Human request")

    assert payload["session_id"] == session_id
    assert payload["provider"] == "claude"
    assert payload["machine_id"] == PROVIDER_FACTORY_MACHINE_ID
    assert payload["session"]["environment"] == "local"
    assert payload["session"]["project"] == FACTORY_TITLE_ASSURANCE_PROJECT
    assert payload["session"]["cwd"] == FACTORY_TITLE_ASSURANCE_CWD
    assert payload["session"]["origin_kind"] == "console"
    assert payload["session"]["hidden_from_default_timeline"] is True
    assert payload["session"]["launch_actor"] == "automation"
    assert payload["session"]["launch_surface"] == FACTORY_TITLE_ASSURANCE_SURFACE
    raw = payload["records"][0]["data_b64"]
    assert raw
    assert oracles.RUNTIME_AGENTS_TOKEN_ENV == "LONGHOUSE_RUNTIME_AGENTS_TOKEN"
    assert LIVE_REGISTRATION.producer_revision == 7
    assert LIVE_REGISTRATION.scenario_revision == 6
    assert "typed_hidden_title_assurance_obligation" in LIVE_REGISTRATION.observed_activity
    assert "claude_semantic_path_consumed" in LIVE_REGISTRATION.observed_activity
    assert "runtime_host_title_provenance" in LIVE_REGISTRATION.observed_activity
    assert RECOVERY_REGISTRATION.producer_revision == 7
    assert RECOVERY_REGISTRATION.scenario_revision == 7
    assert "legacy_exact_provider_proof_excluded_from_title_debt" in RECOVERY_REGISTRATION.observed_activity


def test_hermetic_runtime_owns_an_ephemeral_fernet_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("FERNET_SECRET", "poisoned-ambient-secret")
    runtime = oracles._RuntimeHost(
        repo_root=Path(__file__).resolve().parents[2],
        root=tmp_path,
        base_url="http://127.0.0.1:1/v1",
        token_file=tmp_path / "title-token",
    )

    environment = runtime._child_environment(Path(__file__).resolve().parents[1])

    assert environment["FERNET_SECRET"] != "poisoned-ambient-secret"
    Fernet(environment["FERNET_SECRET"].encode("ascii"))
    assert environment.get("OPENROUTER_API_KEY") is None


@pytest.mark.timeout(120)
def test_hermetic_title_oracle_proves_incident_restart_and_recovery(tmp_path, monkeypatch):
    ambient_secret = "poisoned-ambient-secret"
    ephemeral_secret = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("FERNET_SECRET", ambient_secret)
    monkeypatch.setattr(oracles, "_ephemeral_fernet_secret", lambda: ephemeral_secret)
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
    for artifact in evidence_root.rglob("*"):
        if artifact.is_file():
            retained = artifact.read_text(encoding="utf-8", errors="replace")
            assert ambient_secret not in retained
            assert ephemeral_secret not in retained
