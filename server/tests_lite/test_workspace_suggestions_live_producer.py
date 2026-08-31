from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import Mock

import httpx

from zerg.qa import workspace_suggestions_live_producer as producer


def _response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://runtime.test"))


def test_live_workspace_producer_accepts_responsive_human_only_projection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")

    def fake_get(url, **_kwargs):
        if url.endswith("/api/agents/machines"):
            return _response(
                200,
                {"machines": [{"device_id": "cinder", "control_channel_status": "connected"}]},
            )
        return _response(
            200,
            {"device_id": "cinder", "workspaces": [{"path": "/Users/david/git/zerg"}]},
        )

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "pass"
    assert result["assertions"] == {producer.ASSERTION_ID: True}
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert result["observation"]["proof_path_leak_count"] == 0
    assert (tmp_path / "evidence/live-runtime-observation.json").is_file()


def test_live_workspace_producer_rejects_provider_proof_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")

    def fake_get(url, **_kwargs):
        if url.endswith("/api/agents/machines"):
            return _response(
                200,
                {"machines": [{"device_id": "cinder", "control_channel_status": "connected"}]},
            )
        return _response(
            200,
            {
                "device_id": "cinder",
                "workspaces": [{"path": "/Users/d/.longhouse/canaries/provider-live/claude/qualification-r5"}],
            },
        )

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "fail"
    assert result["assertions"] == {producer.ASSERTION_ID: False}
    assert result["observation"]["proof_path_leak_count"] == 1


def test_live_workspace_registration_is_a_providerless_product_cell() -> None:
    registration = producer.REGISTRATION.to_dict()
    assert producer.RUNTIME_AGENTS_TOKEN_ENV == "LONGHOUSE_RUNTIME_AGENTS_TOKEN"
    assert registration["producer_revision"] == 5
    assert registration["subject_kind"] == "longhouse_product"
    assert registration["providers"] == []
    assert registration["assertion_cells"] == [{"assertion_id": producer.ASSERTION_ID, "variant": None}]
    assert json.loads(json.dumps(registration))["scenario_id"] == "workspace_suggestions_live"


def test_live_workspace_producer_retries_transient_projection_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    monkeypatch.setattr(producer.time, "sleep", Mock())
    projection_attempts = 0

    def fake_get(url, **_kwargs):
        nonlocal projection_attempts
        if url.endswith("/api/agents/machines"):
            return _response(200, {"machines": [{"device_id": "cinder"}]})
        projection_attempts += 1
        if projection_attempts == 1:
            return _response(503, {"detail": {"code": "catalog_unavailable"}})
        return _response(200, {"workspaces": [{"path": "/Users/davidrose/git/zerg"}]})

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "pass"
    assert result["observation"]["reads"][0]["attempts"] == 2
    assert projection_attempts == 2


def test_transient_projection_exhaustion_is_harness_failure_not_missing_coverage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    monkeypatch.setattr(producer.time, "sleep", Mock())

    def fake_get(url, **_kwargs):
        if url.endswith("/api/agents/machines"):
            return _response(200, {"machines": [{"device_id": "cinder"}]})
        return _response(503, {"detail": {"code": "catalog_unavailable"}})

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "fail"
    assert result["failure_code"] == "workspace_suggestions_live_failed"
    assert "failed status=503" in result["error"]
    assert result["assertions"] == {}


def test_no_human_workspace_is_blocked_not_a_product_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")

    def fake_get(url, **_kwargs):
        if url.endswith("/api/agents/machines"):
            return _response(
                200,
                {"machines": [{"device_id": "cube", "control_channel_status": "connected"}]},
            )
        return _response(200, {"device_id": "cube", "workspaces": []})

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "blocked"
    assert result["failure_code"] == "human_workspace_prerequisite_unavailable"
    assert result["assertions"] == {}


def test_durable_workspace_projection_does_not_require_a_live_control_channel(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "https://runtime.test")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")

    def fake_get(url, **_kwargs):
        if url.endswith("/api/agents/machines"):
            return _response(
                200,
                {
                    "machines": [
                        {"device_id": "provider-factory-resume", "control_channel_status": "connected"},
                        {"device_id": "cinder", "control_channel_status": "disconnected"},
                    ]
                },
            )
        assert "/cinder/workspaces" in url
        return _response(200, {"device_id": "cinder", "workspaces": [{"path": "/Users/davidrose/git/zerg"}]})

    monkeypatch.setattr(producer.httpx, "get", fake_get)
    result = producer.run(tmp_path / "evidence")

    assert result["status"] == "pass"
    assert result["observation"]["human_machine_count"] == 1
    assert result["observation"]["connected_human_machine_count"] == 0
    assert result["observation"]["reads"][0]["device_id"] == "cinder"
