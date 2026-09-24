"""Console create refuses a provider its engine reports as unable to run."""

from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.main import api_app  # noqa: E402


class _Registry:
    def __init__(self, readiness):
        self.readiness = readiness

    @staticmethod
    def supports(*, owner_id, device_id, capability):
        return capability == "codex.turn_start"

    def provider_readiness_state(self, *, owner_id, device_id, provider):
        return self.readiness.get(provider)


def _post(monkeypatch, readiness, created_calls):
    from zerg.routers import agents_sessions

    async def _fake_create(db, **kwargs):
        created_calls.append(kwargs)
        return SimpleNamespace(
            session_id="00000000-0000-0000-0000-000000000001", thread_id="00000000-0000-0000-0000-000000000002", created=True
        )

    monkeypatch.setattr(agents_sessions, "get_machine_control_channel_registry", lambda: _Registry(readiness))
    monkeypatch.setattr(agents_sessions, "create_empty_console_session", _fake_create)
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=1, device_id="workbench", id="t")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            return client.post("/agents/sessions", json={"provider": "codex", "device_id": "workbench", "cwd": "/tmp"})
    finally:
        api_app.dependency_overrides.clear()


def test_signed_out_provider_is_refused_before_a_thread_exists(monkeypatch):
    calls: list[dict] = []
    response = _post(
        monkeypatch,
        {"codex": {"state": "not_authenticated", "remediation": "Sign in to codex on this machine"}},
        calls,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "provider_not_ready",
        "reason": "not_authenticated",
        "message": "Sign in to codex on this machine",
    }
    assert calls == []


def test_unknown_or_unreported_readiness_still_launches(monkeypatch):
    # "unknown" means Longhouse cannot probe that provider, and an engine that
    # predates readiness reports nothing; neither is evidence of a problem.
    for readiness in ({"codex": {"state": "unknown"}}, {}):
        calls: list[dict] = []
        response = _post(monkeypatch, readiness, calls)
        assert response.status_code == 201, response.text
        assert len(calls) == 1
