from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import coordination as coordination_cli
from zerg.cli.main import app

SOURCE_ID = "11111111-1111-1111-1111-111111111111"
TARGET_ID = "22222222-2222-2222-2222-222222222222"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, *, headers: dict[str, str], params: dict | None = None) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self.response

    def post(self, url: str, *, headers: dict[str, str], json: dict) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.response


def _wire(monkeypatch, fake_client: _FakeClient) -> None:
    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_device")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)


def test_peers_lists_other_live_sessions(monkeypatch):
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "sessions": [
                    {"session_id": SOURCE_ID, "has_live_presence": True},
                    {
                        "session_id": TARGET_ID,
                        "has_live_presence": True,
                        "device_name": "demo-machine",
                        "provider": "codex",
                        "presence_state": "thinking",
                        "summary_title": "Peer",
                        "git_branch": "feature/directed-input",
                        "kernel_control_label": "live",
                    },
                ]
            },
        )
    )
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        coordination_cli,
        "_resolve_repo_context",
        lambda **_kwargs: ("/tmp/longhouse", SOURCE_ID),
    )

    result = CliRunner().invoke(app, ["peers"])

    assert result.exit_code == 0, result.output
    assert "Found 1 peer session" in result.output
    assert TARGET_ID in result.output
    assert "control: live" in result.output
    assert fake.calls[0] == {
        "method": "GET",
        "url": "https://longhouse.test/api/agents/sessions/wall",
        "headers": {"X-Agents-Token": "zdt_device"},
        "params": {"repo": "/tmp/longhouse", "days": 7, "limit": 50},
    }


def test_tail_reads_recent_session_events_with_device_token(monkeypatch):
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "session_id": TARGET_ID,
                "events": [{"id": 1, "role": "assistant", "content": "Found it.", "timestamp": "now"}],
                "total": 1,
            },
        )
    )
    _wire(monkeypatch, fake)

    result = CliRunner().invoke(app, ["tail", TARGET_ID, "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "Found it." in result.output
    assert fake.calls[0] == {
        "method": "GET",
        "url": f"https://longhouse.test/api/agents/sessions/{TARGET_ID}/tail",
        "headers": {"X-Agents-Token": "zdt_device"},
        "params": {"limit": 1},
    }


def test_send_uses_scoped_coordination_token_and_session_identity(monkeypatch):
    fake = _FakeClient(
        _FakeResponse(
            201,
            {"id": 7, "source_session_id": SOURCE_ID, "target_session_id": TARGET_ID, "input_receipt": None},
        )
    )
    _wire(monkeypatch, fake)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", SOURCE_ID)
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")

    result = CliRunner().invoke(app, ["send", TARGET_ID, "check this", "--client-request-id", "req-1"])

    assert result.exit_code == 0, result.output
    assert "Directed input created." in result.output
    assert fake.calls[0] == {
        "method": "POST",
        "url": "https://longhouse.test/api/agents/directed-inputs",
        "headers": {
            "X-Agents-Token": "zst_coordination",
            "X-Longhouse-Session-Id": SOURCE_ID,
        },
        "json": {"target_session_id": TARGET_ID, "text": "check this", "client_request_id": "req-1"},
    }


def test_inbox_is_cursor_based_without_ack_or_unread_state(monkeypatch):
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "directed_inputs": [
                    {
                        "id": 9,
                        "source_session_id": TARGET_ID,
                        "target_session_id": SOURCE_ID,
                        "text": "Please verify.",
                        "created_at": "now",
                        "input_receipt": None,
                    }
                ],
                "next_cursor": 9,
            },
        )
    )
    _wire(monkeypatch, fake)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", SOURCE_ID)
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")

    result = CliRunner().invoke(app, ["inbox", "--after-cursor", "4"])

    assert result.exit_code == 0, result.output
    assert "Please verify." in result.output
    assert "Next cursor: 9" in result.output
    assert fake.calls[0] == {
        "method": "GET",
        "url": "https://longhouse.test/api/agents/directed-inputs",
        "headers": {
            "X-Agents-Token": "zst_coordination",
            "X-Longhouse-Session-Id": SOURCE_ID,
        },
        "params": {"direction": "inbound", "after_id": 4, "limit": 50},
    }


def test_reply_routes_by_input_id(monkeypatch):
    fake = _FakeClient(_FakeResponse(201, {"id": 10, "reply_to_id": 9}))
    _wire(monkeypatch, fake)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", SOURCE_ID)
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")

    result = CliRunner().invoke(app, ["reply", "9", "verified"])

    assert result.exit_code == 0, result.output
    assert "Reply created." in result.output
    assert fake.calls[0] == {
        "method": "POST",
        "url": "https://longhouse.test/api/agents/directed-inputs/9/reply",
        "headers": {
            "X-Agents-Token": "zst_coordination",
            "X-Longhouse-Session-Id": SOURCE_ID,
        },
        "json": {"text": "verified"},
    }


def test_directed_commands_fail_without_session_scoped_authority(monkeypatch):
    fake = _FakeClient(_FakeResponse(500, {}))
    _wire(monkeypatch, fake)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", SOURCE_ID)
    monkeypatch.delenv("LONGHOUSE_COORDINATION_TOKEN", raising=False)

    result = CliRunner().invoke(app, ["send", TARGET_ID, "must fail"])

    assert result.exit_code == 1
    assert "requires session-scoped coordination authority" in result.output
    assert fake.calls == []
