from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.cli import opencode_channel


class _FakePopen:
    def __init__(self, cmd, *, cwd, env, stdin, stdout, stderr, start_new_session):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.start_new_session = start_new_session
        self.pid = 4242

    def poll(self):
        return None


class _FakeResponse:
    def __init__(self, body: bytes = b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def _write_state(tmp_path: Path, *, session_id: str) -> None:
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "provider_session_id": "ses_test123",
        "server_url": "http://127.0.0.1:57777",
        "pid": 4242,
        "cwd": str(tmp_path),
        "username": "opencode",
        "password": "server-secret",
        "log_path": str(tmp_path / "server.log"),
        "config_content_path": str(tmp_path / "config.json"),
        "started_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }
    path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    opencode_channel._write_private_json(path, state)


def test_launch_opencode_server_bridge_writes_private_state_without_token_in_argv(monkeypatch, tmp_path):
    session_id = str(uuid4())
    popen_calls: list[_FakePopen] = []
    create_calls: list[dict] = []
    config_path = tmp_path / "config-content.json"
    config_path.write_text('{"plugin":[["file:///plugin.mjs",{"token":"zdt_test_token"}]]}\n', encoding="utf-8")

    def fake_popen(*args, **kwargs):
        proc = _FakePopen(*args, **kwargs)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(
        opencode_channel,
        "_resolve_opencode_binary",
        lambda explicit=None: "/opt/homebrew/bin/opencode",
    )
    monkeypatch.setattr(opencode_channel, "_write_opencode_runtime_config_content", lambda **_kwargs: config_path)
    monkeypatch.setattr(opencode_channel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode_channel, "_wait_for_server_url", lambda *_args, **_kwargs: "http://127.0.0.1:57777")
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return "ses_test123"

    monkeypatch.setattr(opencode_channel, "_create_opencode_session", fake_create)

    result = opencode_channel.launch_opencode_server_bridge(
        session_id=session_id,
        cwd=tmp_path,
        api_url="https://longhouse.test",
        api_token="zdt_test_token",
        device_id="work-laptop",
        display_name="Demo",
        config_dir=tmp_path / "config",
    )

    assert result["transport"] == "opencode_server_bridge"
    proc = popen_calls[0]
    assert proc.cmd == [
        "/opt/homebrew/bin/opencode",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        "--print-logs",
    ]
    assert proc.start_new_session is True
    assert "zdt_test_token" not in " ".join(proc.cmd)
    assert proc.env["LONGHOUSE_MANAGED_SESSION_ID"] == session_id
    assert proc.env["LONGHOUSE_DEVICE_ID"] == "work-laptop"
    assert "zdt_test_token" in proc.env["OPENCODE_CONFIG_CONTENT"]
    assert proc.env["OPENCODE_SERVER_PASSWORD"]

    state_path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["provider_session_id"] == "ses_test123"
    assert state["password"] == proc.env["OPENCODE_SERVER_PASSWORD"]
    assert "zdt_test_token" not in state_path.read_text(encoding="utf-8")

    second = opencode_channel.launch_opencode_server_bridge(
        session_id=session_id,
        cwd=tmp_path,
        api_url="https://longhouse.test",
        api_token="zdt_test_token",
        device_id="work-laptop",
        display_name="Demo",
        config_dir=tmp_path / "config",
    )

    assert second["provider_session_id"] == "ses_test123"
    assert len(popen_calls) == 1
    assert len(create_calls) == 1


def test_send_opencode_text_posts_prompt_async(monkeypatch, tmp_path):
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _FakeResponse()

    monkeypatch.setattr(opencode_channel, "urlopen", fake_urlopen)

    result = opencode_channel.send_opencode_text(
        session_id=session_id,
        text="continue",
        config_dir=tmp_path / "config",
    )

    request, timeout = requests[0]
    assert timeout == 10
    assert request.get_method() == "POST"
    assert "/session/ses_test123/prompt_async" in request.full_url
    assert "directory=" in request.full_url
    assert request.get_header("Authorization").startswith("Basic ")
    assert json.loads(request.data.decode("utf-8")) == {
        "parts": [{"type": "text", "text": "continue"}],
    }
    assert result["transport"] == "opencode_server_bridge"


def test_interrupt_opencode_session_posts_abort(monkeypatch, tmp_path):
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _FakeResponse(b"true")

    monkeypatch.setattr(opencode_channel, "urlopen", fake_urlopen)

    result = opencode_channel.interrupt_opencode_session(
        session_id=session_id,
        config_dir=tmp_path / "config",
    )

    request, _timeout = requests[0]
    assert request.get_method() == "POST"
    assert "/session/ses_test123/abort" in request.full_url
    assert result["transport"] == "opencode_server_bridge"


def test_run_opencode_attach_uses_state_password_in_env_not_argv(monkeypatch, tmp_path):
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    calls = []
    health_calls = []

    class Completed:
        returncode = 0

    def fake_run(cmd, *, cwd, env, check):
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "check": check})
        return Completed()

    monkeypatch.setattr(
        opencode_channel,
        "_resolve_opencode_binary",
        lambda explicit=None: "/opt/homebrew/bin/opencode",
    )
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **kwargs: health_calls.append(kwargs))
    monkeypatch.setattr(opencode_channel.subprocess, "run", fake_run)

    code = opencode_channel.run_opencode_attach(
        session_id=session_id,
        config_dir=tmp_path / "config",
        extra_args=("--log-level", "debug"),
    )

    assert code == 0
    call = calls[0]
    assert call["cmd"] == [
        "/opt/homebrew/bin/opencode",
        "attach",
        "http://127.0.0.1:57777",
        "--session",
        "ses_test123",
        "--log-level",
        "debug",
    ]
    assert "server-secret" not in " ".join(call["cmd"])
    assert call["env"]["OPENCODE_SERVER_PASSWORD"] == "server-secret"
    assert health_calls == [
        {
            "server_url": "http://127.0.0.1:57777",
            "username": "opencode",
            "password": "server-secret",
        }
    ]
