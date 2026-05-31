from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import coordination as coordination_cli
from zerg.cli import sessions as sessions_cli
from zerg.cli.main import app


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        json_data: dict | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        stream_lines: list[str] | None = None,
        require_read_before_json: bool = False,
    ):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.headers = headers or {}
        self._stream_lines = stream_lines or []
        self._require_read_before_json = require_read_before_json
        self._read_called = False

    def json(self) -> dict:
        if self._require_read_before_json and not self._read_called:
            raise RuntimeError("response.json() requires read() first")
        return self._json_data

    def read(self) -> bytes:
        self._read_called = True
        if self.text:
            return self.text.encode()
        if self._json_data:
            return json.dumps(self._json_data).encode()
        return b""

    def iter_lines(self):
        yield from self._stream_lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeClient:
    def __init__(
        self,
        *,
        get_response: _FakeResponse | None = None,
        post_response: _FakeResponse | None = None,
        stream_response: _FakeResponse | None = None,
    ):
        self.get_response = get_response
        self.post_response = post_response
        self.stream_response = stream_response
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

    def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict) -> _FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
            }
        )
        assert self.stream_response is not None
        return self.stream_response


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
                        "device_name": "demo-machine",
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


def test_wall_command_prints_raw_sessions(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "sessions": [
                    {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "device_name": "demo-machine",
                        "provider": "codex",
                        "presence_state": "thinking",
                        "summary_title": "Peer",
                        "git_branch": "feature/messaging",
                        "git_repo": "git@github.com:cipher982/longhouse.git",
                        "last_event_at": "2026-04-02T12:00:00+00:00",
                    }
                ],
                "total": 1,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["wall", "--repo", "longhouse"])

    assert result.exit_code == 0, result.output
    assert "Found 1 wall session" in result.output
    assert "22222222-2222-2222-2222-222222222222" in result.output
    assert "git@github.com:cipher982/longhouse.git" in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/wall",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": {"repo": "longhouse", "days": 7, "limit": 50},
        }
    ]


def test_wall_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "sessions": [
                    {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "device_name": "demo-machine",
                        "provider": "codex",
                        "presence_state": "thinking",
                        "summary_title": "Peer",
                        "git_branch": "main",
                        "git_repo": "git@github.com:cipher982/longhouse.git",
                        "last_event_at": "2026-04-02T12:00:00+00:00",
                    }
                ],
                "total": 1,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["wall", "--project", "zerg", "--json"])

    assert result.exit_code == 0, result.output
    assert '"total": 1' in result.output
    assert '"session_id": "22222222-2222-2222-2222-222222222222"' in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/wall",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": {"project": "zerg", "days": 7, "limit": 50},
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
                        "device_name": "demo-machine",
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
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")

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


def test_messages_command_prints_inbox(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "messages": [
                    {
                        "id": 9,
                        "from_session_id": "11111111-1111-1111-1111-111111111111",
                        "to_session_id": "22222222-2222-2222-2222-222222222222",
                        "text": "Please confirm the migration status.",
                        "delivery_status": "stored_only",
                        "created_at": "2026-04-02T12:45:00+00:00",
                    }
                ],
                "total": 1,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "22222222-2222-2222-2222-222222222222")

    result = runner.invoke(app, ["messages"])

    assert result.exit_code == 0, result.output
    assert "Session: 22222222-2222-2222-2222-222222222222" in result.output
    assert "Please confirm the migration status." in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/messages",
            "headers": {
                "X-Agents-Token": "zdt_test_token",
                "X-Longhouse-Session-Id": "22222222-2222-2222-2222-222222222222",
            },
            "params": {
                "direction": "inbound",
                "unacknowledged_only": True,
                "limit": 50,
            },
        }
    ]


def test_messages_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "messages": [
                    {
                        "id": 9,
                        "from_session_id": "11111111-1111-1111-1111-111111111111",
                        "to_session_id": "22222222-2222-2222-2222-222222222222",
                        "text": "Please confirm the migration status.",
                        "delivery_status": "stored_only",
                        "created_at": "2026-04-02T12:45:00+00:00",
                    }
                ],
                "total": 1,
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "22222222-2222-2222-2222-222222222222")

    result = runner.invoke(app, ["messages", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["messages"][0]["id"] == 9


def test_messages_ack_command_posts_ack(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        post_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": 9,
                "delivery_status": "stored_only",
                "acknowledged_at": "2026-04-02T12:46:00+00:00",
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(
        app,
        [
            "messages",
            "ack",
            "9",
            "--session",
            "22222222-2222-2222-2222-222222222222",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Message acknowledged." in result.output
    assert "Acknowledged at: 2026-04-02T12:46:00+00:00" in result.output
    assert fake_client.calls == [
        {
            "method": "POST",
            "url": "https://longhouse.test/api/agents/messages/9/ack",
            "headers": {
                "X-Agents-Token": "zdt_test_token",
                "X-Longhouse-Session-Id": "22222222-2222-2222-2222-222222222222",
            },
            "json": {},
        }
    ]


def test_messages_ack_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        post_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": 9,
                "delivery_status": "stored_only",
                "acknowledged_at": "2026-04-02T12:46:00+00:00",
            },
        )
    )

    monkeypatch.setattr(coordination_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(coordination_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(coordination_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(
        app,
        [
            "messages",
            "ack",
            "9",
            "--session",
            "22222222-2222-2222-2222-222222222222",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == 9
    assert payload["acknowledged_at"] == "2026-04-02T12:46:00+00:00"


def test_sessions_get_command_prints_session_summary(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": "22222222-2222-2222-2222-222222222222",
                "provider": "codex",
                "project": "zerg",
                "status": "working",
                "started_at": "2026-04-02T12:50:00+00:00",
                "git_branch": "main",
                "git_repo": "git@github.com:cipher982/longhouse.git",
                "summary_title": "Coordination slice",
                "first_user_message": "Add CLI session inspection commands.",
            },
        )
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["sessions", "get", "22222222-2222-2222-2222-222222222222"])

    assert result.exit_code == 0, result.output
    assert "22222222-2222-2222-2222-222222222222" in result.output
    assert "provider: codex  project: zerg  status: working" in result.output
    assert "title: Coordination slice" in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": None,
        }
    ]


def test_sessions_get_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": "22222222-2222-2222-2222-222222222222",
                "provider": "codex",
                "project": "zerg",
                "status": "working",
            },
        )
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["sessions", "get", "22222222-2222-2222-2222-222222222222", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "22222222-2222-2222-2222-222222222222"
    assert payload["provider"] == "codex"


def test_sessions_events_command_prints_filtered_events(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "events": [
                    {
                        "id": 1,
                        "role": "assistant",
                        "content_text": "I added the session events command.",
                        "tool_name": None,
                        "tool_output_text": None,
                        "timestamp": "2026-04-02T12:51:00+00:00",
                    }
                ],
                "total": 1,
                "branch_mode": "head",
                "abandoned_events": 0,
            },
        )
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(
        app,
        [
            "sessions",
            "events",
            "22222222-2222-2222-2222-222222222222",
            "--roles",
            "assistant",
            "--limit",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Session: 22222222-2222-2222-2222-222222222222" in result.output
    assert "I added the session events command." in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222/events",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": {
                "context_mode": "forensic",
                "branch_mode": "head",
                "limit": 20,
                "offset": 0,
                "roles": "assistant",
            },
        }
    ]


def test_sessions_events_command_json_output(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "events": [
                    {
                        "id": 1,
                        "role": "assistant",
                        "content_text": "I added the session events command.",
                        "tool_name": None,
                        "tool_output_text": None,
                        "timestamp": "2026-04-02T12:51:00+00:00",
                    }
                ],
                "total": 1,
                "branch_mode": "head",
                "abandoned_events": 0,
            },
        )
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)

    result = runner.invoke(app, ["sessions", "events", "22222222-2222-2222-2222-222222222222", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["events"][0]["id"] == 1


def test_sessions_continue_command_prints_managed_local_acceptance(monkeypatch):
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": "22222222-2222-2222-2222-222222222222",
                "execution_home": "managed_local",
                "source_runner_id": 77,
                "capabilities": {
                    "live_control_available": True,
                },
            },
        ),
        stream_response=_FakeResponse(
            status_code=200,
            json_data={
                "accepted": True,
                "session_id": "22222222-2222-2222-2222-222222222222",
                "dispatch_ms": 12.4,
            },
            headers={"content-type": "application/json"},
            require_read_before_json=True,
        )
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.delenv("LONGHOUSE_MANAGED_SESSION_ID", raising=False)

    result = runner.invoke(
        app,
        [
            "continue",
            "22222222-2222-2222-2222-222222222222",
            "follow up on the failing test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Accepted by session 22222222-2222-2222-2222-222222222222" in result.output
    assert "dispatch_ms: 12.4" in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": None,
        },
        {
            "method": "POST",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222/send-live",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "json": {"message": "follow up on the failing test"},
        }
    ]



def test_sessions_continue_always_uses_send_live(monkeypatch):
    """Even without a capabilities dict, continue always routes to send-live."""
    runner = CliRunner()
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            status_code=200,
            json_data={
                "id": "22222222-2222-2222-2222-222222222222",
                "execution_home": "managed_local",
                "source_runner_id": 77,
            },
        ),
        stream_response=_FakeResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            json_data={
                "accepted": True,
                "session_id": "22222222-2222-2222-2222-222222222222",
                "request_id": "req-1",
                "dispatch_ms": 8.0,
            },
        ),
    )

    monkeypatch.setattr(sessions_cli, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(sessions_cli, "load_token", lambda _config_dir: "zdt_test_token")
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.delenv("LONGHOUSE_MANAGED_SESSION_ID", raising=False)

    result = runner.invoke(
        app,
        [
            "continue",
            "22222222-2222-2222-2222-222222222222",
            "follow up without explicit capabilities",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Accepted by session 22222222-2222-2222-2222-222222222222" in result.output
    assert fake_client.calls == [
        {
            "method": "GET",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "params": None,
        },
        {
            "method": "POST",
            "url": "https://longhouse.test/api/agents/sessions/22222222-2222-2222-2222-222222222222/send-live",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "json": {"message": "follow up without explicit capabilities"},
        },
    ]
