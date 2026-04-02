from __future__ import annotations

import os

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import coordination as coordination_cli
from zerg.cli.main import app


class _FakeResponse:
    def __init__(self, *, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json_data


class _FakeClient:
    def __init__(self, *, get_response: _FakeResponse | None = None, post_response: _FakeResponse | None = None):
        self.get_response = get_response
        self.post_response = post_response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, *, headers: dict[str, str], params: dict | None = None) -> _FakeResponse:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "params": params,
            }
        )
        assert self.get_response is not None
        return self.get_response

    def post(self, url: str, *, headers: dict[str, str], json: dict) -> _FakeResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
            }
        )
        assert self.post_response is not None
        return self.post_response


def test_peers_command_lists_live_peer_sessions(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "sessions": [
                    {
                        "session_id": "11111111-1111-1111-1111-111111111111",
                        "has_live_presence": True,
                        "device_name": "laptop",
                        "provider": "claude",
                        "presence_state": "idle",
                        "summary_title": "Current",
                        "git_branch": "main",
                    },
                    {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "has_live_presence": True,
                        "device_name": "cube",
                        "provider": "codex",
                        "presence_state": "thinking",
                        "summary_title": "Peer",
                        "git_branch": "feature/messaging",
                    },
                    {
                        "session_id": "33333333-3333-3333-3333-333333333333",
                        "has_live_presence": False,
                        "device_name": "idle-box",
                        "provider": "gemini",
                        "presence_state": None,
                        "summary_title": "Inactive peer",
                        "git_branch": "main",
                    },
                ],
                "total": 3,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(
        coordination_cli,
        "_resolve_repo_context",
        lambda **_kwargs: ("/tmp/repo", "11111111-1111-1111-1111-111111111111"),
    )
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["peers"])

    assert result.exit_code == 0, result.output
    assert "Found 1 peer session" in result.output
    assert "22222222-2222-2222-2222-222222222222" in result.output
    assert "feature/messaging" in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/wall",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": {"repo": "/tmp/repo", "days": 7, "limit": 50},
        }
    ]


def test_peers_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "sessions": [
                    {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "has_live_presence": True,
                        "device_name": "cube",
                        "provider": "codex",
                        "presence_state": "thinking",
                        "summary_title": "Peer",
                        "git_branch": "main",
                    }
                ],
                "total": 1,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(
        coordination_cli,
        "_resolve_repo_context",
        lambda **_kwargs: ("git@github.com:cipher982/longhouse.git", None),
    )
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["peers", "--json"])

    assert result.exit_code == 0, result.output
    assert '"repo": "git@github.com:cipher982/longhouse.git"' in result.output
    assert '"total": 1' in result.output
    assert '"session_id": "22222222-2222-2222-2222-222222222222"' in result.output


def test_message_command_uses_from_session_header(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        post_response=_FakeResponse(
            status_code=201,
            json_data={
                "id": 7,
                "delivery_status": "queued",
                "delivered_via": None,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(
        app,
        [
            "message",
            "22222222-2222-2222-2222-222222222222",
            "hello from cli",
            "--from-session",
            "11111111-1111-1111-1111-111111111111",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Message created." in result.output
    assert "Status: queued" in result.output
    assert fake_client.calls == [
        {
            "method": "POST",
            "url": "https://longhouse.test/api/agents/messages",
            "headers": {
                "X-Agents-Token": "zdt_test_token",
                "X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111",
            },
            "json": {
                "to_session_id": "22222222-2222-2222-2222-222222222222",
                "text": "hello from cli",
            },
        }
    ]


def test_message_command_uses_session_env_when_from_session_omitted(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        post_response=_FakeResponse(
            status_code=201,
            json_data={
                "id": 8,
                "delivery_status": "stored_only",
                "delivered_via": "stored_only",
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setenv("LONGHOUSE_SESSION_ID", "11111111-1111-1111-1111-111111111111")

    result = runner.invoke(
        app,
        [
            "message",
            "22222222-2222-2222-2222-222222222222",
            "hello from env",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"delivery_status": "stored_only"' in result.output
    assert '"delivered_via": "stored_only"' in result.output


def test_tail_command_prints_recent_events(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "session_id": "22222222-2222-2222-2222-222222222222",
                "events": [
                    {
                        "id": 1,
                        "role": "user",
                        "content": "Investigate the retry bug.",
                        "tool_name": None,
                        "timestamp": "2026-04-02T12:00:00+00:00",
                    },
                    {
                        "id": 2,
                        "role": "assistant",
                        "content": "I found the stale wait path in the sync loop.",
                        "tool_name": None,
                        "timestamp": "2026-04-02T12:00:05+00:00",
                    },
                ],
                "total": 2,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["tail", "22222222-2222-2222-2222-222222222222", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "Session: 22222222-2222-2222-2222-222222222222" in result.output
    assert "Investigate the retry bug." in result.output
    assert "I found the stale wait path in the sync loop." in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222/tail",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": {"limit": 2},
        }
    ]


def test_tail_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "session_id": "22222222-2222-2222-2222-222222222222",
                "events": [],
                "total": 0,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["tail", "22222222-2222-2222-2222-222222222222", "--json"])

    assert result.exit_code == 0, result.output
    assert '"session_id": "22222222-2222-2222-2222-222222222222"' in result.output
    assert '"total": 0' in result.output
