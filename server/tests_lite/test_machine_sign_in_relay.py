"""Provider sign-in relay routes: phone <-> Runtime Host <-> Machine Agent."""

from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.dependencies.browser_auth import get_current_browser_caller  # noqa: E402
from zerg.main import api_app  # noqa: E402


class _Registry:
    def __init__(self, *, online=True, supports=("codex.sign_in",), reply=None):
        self.online = online
        self._supports = set(supports)
        self.reply = reply or {"ok": True, "result": {}}
        self.sent: list[dict] = []

    def is_online(self, *, owner_id, device_id):
        return self.online

    def supports(self, *, owner_id, device_id, capability):
        return capability in self._supports

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(transport_ok=True, message=self.reply, error=None)


def _client(monkeypatch, registry):
    from zerg.services import provider_sign_in

    monkeypatch.setattr(provider_sign_in, "get_machine_control_channel_registry", lambda: registry)
    api_app.dependency_overrides[get_current_browser_caller] = lambda: SimpleNamespace(owner_id=1, id=1)
    return TestClient(api_app, raise_server_exceptions=False)


def teardown_function():
    api_app.dependency_overrides.clear()


def test_start_relays_the_cli_url_and_device_code(monkeypatch):
    registry = _Registry(
        reply={
            "ok": True,
            "result": {
                "attempt_id": "a1",
                "provider": "codex",
                "flow": "device_code",
                "verification_url": "https://auth.openai.com/codex/device",
                "user_code": "VWSN-8F9KZ",
                "prerequisite": "Enable device code authorization in ChatGPT > Settings > Security first.",
                "expires_in_secs": 900,
            },
        }
    )
    with _client(monkeypatch, registry) as client:
        response = client.post("/timeline/machines/workbench/providers/codex/sign-in")

    assert response.status_code == 200, response.text
    assert response.json()["user_code"] == "VWSN-8F9KZ"
    assert registry.sent[0]["command_type"] == "provider.sign_in.start"
    assert registry.sent[0]["payload"] == {"provider": "codex"}


def test_code_is_passed_through_to_the_waiting_cli(monkeypatch):
    registry = _Registry(supports=("claude.sign_in",), reply={"ok": True, "result": {"attempt_id": "a2", "accepted": True}})
    with _client(monkeypatch, registry) as client:
        response = client.post("/timeline/machines/workbench/sign-in/a2/code", json={"code": "abc#def"})

    assert response.status_code == 200, response.text
    assert response.json() == {"attempt_id": "a2", "accepted": True, "cancelled": None}
    assert registry.sent[0]["payload"] == {"attempt_id": "a2", "code": "abc#def"}


def test_machine_errors_keep_their_typed_code(monkeypatch):
    registry = _Registry(reply={"ok": False, "error": {"code": "sign_in_prompt_missing", "message": "no URL"}})
    with _client(monkeypatch, registry) as client:
        response = client.post("/timeline/machines/workbench/providers/codex/sign-in")

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "sign_in_prompt_missing", "message": "no URL"}


def test_unsupported_provider_and_offline_machine_are_refused_without_a_command(monkeypatch):
    unsupported = _Registry(supports=())
    with _client(monkeypatch, unsupported) as client:
        assert client.post("/timeline/machines/workbench/providers/omp/sign-in").json()["detail"]["code"] == "sign_in_unsupported"
    offline = _Registry(online=False)
    with _client(monkeypatch, offline) as client:
        assert client.post("/timeline/machines/workbench/providers/codex/sign-in").json()["detail"]["code"] == "machine_offline"
    assert unsupported.sent == [] and offline.sent == []
