from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

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
    write_calls: list[dict] = []
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
    monkeypatch.setattr(
        opencode_channel,
        "_write_opencode_runtime_config_content",
        lambda **kwargs: write_calls.append(kwargs) or config_path,
    )
    monkeypatch.setattr(opencode_channel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode_channel, "_wait_for_server_url", lambda *_args, **_kwargs: "http://127.0.0.1:57777")
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Mon May 27 00:00:00 2026", "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"),
    )

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
    assert write_calls[0]["model"] is None

    state_path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["provider_session_id"] == "ses_test123"
    assert state["password"] == proc.env["OPENCODE_SERVER_PASSWORD"]
    assert "zdt_test_token" not in state_path.read_text(encoding="utf-8")
    assert state["schema_version"] == 1
    assert state["process_command"] == "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"
    assert state["process_start_time"] == "Mon May 27 00:00:00 2026"

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


def test_launch_opencode_server_bridge_writes_model_config(monkeypatch, tmp_path):
    session_id = str(uuid4())
    popen_calls: list[_FakePopen] = []
    create_calls: list[dict] = []
    config_path = tmp_path / "config-content.json"

    def fake_write_config(**kwargs):
        content = json.dumps({"plugin": [], "model": kwargs["model"]}, separators=(",", ":"))
        config_path.write_text(content + "\n", encoding="utf-8")
        return config_path

    monkeypatch.setattr(
        opencode_channel,
        "_resolve_opencode_binary",
        lambda explicit=None: "/opt/homebrew/bin/opencode",
    )
    monkeypatch.setattr(opencode_channel, "_write_opencode_runtime_config_content", fake_write_config)
    monkeypatch.setattr(
        opencode_channel.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append(_FakePopen(*args, **kwargs)) or popen_calls[-1],
    )
    monkeypatch.setattr(opencode_channel, "_wait_for_server_url", lambda *_args, **_kwargs: "http://127.0.0.1:57777")
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Mon May 27 00:00:00 2026", "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"),
    )
    monkeypatch.setattr(
        opencode_channel,
        "_create_opencode_session",
        lambda **kwargs: create_calls.append(kwargs) or "ses_test123",
    )

    opencode_channel.launch_opencode_server_bridge(
        session_id=session_id,
        cwd=tmp_path,
        api_url="https://longhouse.test",
        api_token="zdt_test_token",
        device_id="work-laptop",
        config_dir=tmp_path / "config",
        model="openrouter/z-ai/glm-5.2",
    )

    env_config = json.loads(popen_calls[0].env["OPENCODE_CONFIG_CONTENT"])
    assert env_config["model"] == "openrouter/z-ai/glm-5.2"
    written_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert written_config["model"] == "openrouter/z-ai/glm-5.2"
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
        "noReply": True,
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


def test_write_private_json_is_atomic_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "nested" / "state.json"
    opencode_channel._write_private_json(path, {"hello": "world"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"hello": "world"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # No leftover temp files from the temp+replace dance.
    siblings = [p.name for p in path.parent.iterdir()]
    assert siblings == ["state.json"]


def test_write_private_json_overwrite_never_truncates_in_place(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    opencode_channel._write_private_json(path, {"v": 1})

    # If a writer crashes mid-write, os.replace guarantees the reader sees
    # either the old or new full file, never a truncated one. Simulate a crash
    # during serialization and confirm the original content survives intact.
    real_dump = opencode_channel.json.dump

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(opencode_channel.json, "dump", boom)
    try:
        opencode_channel._write_private_json(path, {"v": 2})
    except RuntimeError:
        pass
    monkeypatch.setattr(opencode_channel.json, "dump", real_dump)

    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_stop_bridge_kills_when_identity_matches(monkeypatch, tmp_path):
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    killed: list[int] = []

    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    # No recorded identity on legacy state -> fall back to confirming the live
    # pid is still an `opencode serve` process, then kill.
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Mon May 27 00:00:00 2026", "opencode serve --hostname 127.0.0.1 --port 0"),
    )
    monkeypatch.setattr(opencode_channel, "_terminate_pid", lambda pid: killed.append(pid))

    result = opencode_channel.stop_opencode_server_bridge(
        session_id=session_id,
        config_dir=tmp_path / "config",
    )

    assert killed == [4242]
    assert result["stopped"] is True


def test_stop_bridge_without_recorded_identity_rejects_reused_non_opencode_pid(monkeypatch, tmp_path):
    # State with no recorded identity whose pid is now an unrelated process
    # must NOT be killed.
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    killed: list[int] = []
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Tue Jun 02 12:00:00 2026", "/usr/bin/python unrelated.py"),
    )
    monkeypatch.setattr(opencode_channel, "_terminate_pid", lambda pid: killed.append(pid))

    result = opencode_channel.stop_opencode_server_bridge(session_id=session_id, config_dir=tmp_path / "config")

    assert killed == []
    assert result["stopped"] is False


def test_stop_bridge_rejects_reused_pid(monkeypatch, tmp_path):
    session_id = str(uuid4())
    # State records a specific identity; the live pid reports a different one.
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
        "process_start_time": "Mon May 27 00:00:00 2026",
        "process_command": "opencode serve --hostname 127.0.0.1 --port 0 --print-logs",
    }
    path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    opencode_channel._write_private_json(path, state)

    killed: list[int] = []
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    # PID was reused by an unrelated process: different start time + command.
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Tue Jun 02 12:00:00 2026", "/usr/bin/python some_other_proc"),
    )
    monkeypatch.setattr(opencode_channel, "_terminate_pid", lambda pid: killed.append(pid))

    result = opencode_channel.stop_opencode_server_bridge(
        session_id=session_id,
        config_dir=tmp_path / "config",
    )

    assert killed == []  # never signalled the reused pid
    assert result["stopped"] is False


def test_stop_bridge_accepts_timezone_shifted_lstart_when_started_at_matches(monkeypatch, tmp_path):
    session_id = str(uuid4())
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
        "started_at": "2026-07-04T23:14:53.525006Z",
        "updated_at": "2026-07-04T23:14:53.525006Z",
        "process_start_time": "Sat Jul  4 18:14:52 2026",
        "process_command": "opencode serve --hostname 127.0.0.1 --port 0 --print-logs",
    }
    path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    opencode_channel._write_private_json(path, state)
    killed: list[int] = []

    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Sat Jul  4 17:14:52 2026", "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"),
    )
    monkeypatch.setattr(
        opencode_channel,
        "_lstart_matches_started_at",
        lambda live, started: live == "Sat Jul  4 17:14:52 2026" and started == "2026-07-04T23:14:53.525006Z",
    )
    monkeypatch.setattr(opencode_channel, "_terminate_pid", lambda pid: killed.append(pid))

    result = opencode_channel.stop_opencode_server_bridge(session_id=session_id, config_dir=tmp_path / "config")

    assert killed == [4242]
    assert result["stopped"] is True


def test_existing_live_state_reuses_timezone_shifted_identity(monkeypatch, tmp_path):
    session_id = str(uuid4())
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
        "started_at": "2026-07-04T23:14:53.525006Z",
        "updated_at": "2026-07-04T23:14:53.525006Z",
        "process_start_time": "Sat Jul  4 18:14:52 2026",
        "process_command": "opencode serve --hostname 127.0.0.1 --port 0 --print-logs",
    }
    path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    opencode_channel._write_private_json(path, state)

    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Sat Jul  4 17:14:52 2026", "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"),
    )
    monkeypatch.setattr(opencode_channel, "_lstart_matches_started_at", lambda _live, _started: True)
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **_kwargs: None)

    result = opencode_channel._existing_live_state_result(session_id=session_id, config_dir=tmp_path / "config")

    assert result is not None
    assert result["provider_session_id"] == "ses_test123"


def test_stop_bridge_rejects_same_command_pid_far_from_started_at(monkeypatch, tmp_path):
    session_id = str(uuid4())
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
        "started_at": "2026-07-04T23:14:53.525006Z",
        "updated_at": "2026-07-04T23:14:53.525006Z",
        "process_start_time": "Sat Jul  4 18:14:52 2026",
        "process_command": "opencode serve --hostname 127.0.0.1 --port 0 --print-logs",
    }
    path = opencode_channel._opencode_server_state_path(session_id, tmp_path / "config")
    opencode_channel._write_private_json(path, state)
    killed: list[int] = []

    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda _pid: ("Sun Jul  5 17:14:52 2026", "opencode serve --hostname 127.0.0.1 --port 0 --print-logs"),
    )
    monkeypatch.setattr(opencode_channel, "_lstart_matches_started_at", lambda _live, _started: False)
    monkeypatch.setattr(opencode_channel, "_terminate_pid", lambda pid: killed.append(pid))

    result = opencode_channel.stop_opencode_server_bridge(session_id=session_id, config_dir=tmp_path / "config")

    assert killed == []
    assert result["stopped"] is False


def test_pid_matches_recorded_identity_when_ps_cannot_confirm(monkeypatch, tmp_path):
    state = opencode_channel.OpenCodeServerBridgeState(
        schema_version=1,
        session_id=str(uuid4()),
        provider_session_id="ses_test123",
        server_url="http://127.0.0.1:57777",
        pid=4242,
        cwd=str(tmp_path),
        username="opencode",
        password="server-secret",
        log_path=str(tmp_path / "server.log"),
        config_content_path=str(tmp_path / "config.json"),
        started_at="2026-05-27T00:00:00Z",
        updated_at="2026-05-27T00:00:00Z",
        process_start_time="Mon May 27 00:00:00 2026",
        process_command="opencode serve --hostname 127.0.0.1 --port 0 --print-logs",
    )
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    # ps returns nothing -> cannot confirm -> must NOT treat as a match.
    monkeypatch.setattr(opencode_channel, "_process_identity", lambda _pid: None)

    assert opencode_channel._pid_matches_recorded_identity(state) is False


def test_stopper_is_idempotent_and_stops_once(monkeypatch, tmp_path):
    session_id = str(uuid4())
    stop_calls: list[dict] = []
    monkeypatch.setattr(
        opencode_channel,
        "stop_opencode_server_bridge",
        lambda **kwargs: stop_calls.append(kwargs) or {},
    )
    stopper = opencode_channel.OpenCodeServerBridgeStopper(session_id, config_dir=tmp_path / "config")

    assert stopper.stop_for_terminal_disconnect() is None
    assert stopper.stop_for_terminal_disconnect() is None  # second call no-ops
    assert stop_calls == [{"session_id": session_id, "config_dir": tmp_path / "config"}]


def test_stopper_returns_error_message_on_failure(monkeypatch, tmp_path):
    session_id = str(uuid4())

    def boom(**_kwargs):
        raise opencode_channel.OpenCodeServerBridgeError("no state")

    monkeypatch.setattr(opencode_channel, "stop_opencode_server_bridge", boom)
    stopper = opencode_channel.OpenCodeServerBridgeStopper(session_id, config_dir=tmp_path / "config")

    assert stopper.stop_for_terminal_disconnect() == "no state"


def test_signal_cleanup_install_and_restore(monkeypatch):
    import signal as signal_module

    session_id = str(uuid4())
    stopped: list[bool] = []

    class _Stopper:
        def stop_for_terminal_disconnect(self):
            stopped.append(True)
            return None

    original_sighup = signal_module.getsignal(signal_module.SIGHUP)
    original_sigterm = signal_module.getsignal(signal_module.SIGTERM)

    previous = opencode_channel._install_opencode_signal_cleanup(_Stopper())
    # Handlers were replaced.
    assert signal_module.getsignal(signal_module.SIGHUP) is not original_sighup
    assert signal_module.getsignal(signal_module.SIGTERM) is not original_sigterm

    with pytest.raises(SystemExit) as exc_info:
        signal_module.getsignal(signal_module.SIGHUP)(signal_module.SIGHUP, None)
    assert exc_info.value.code == 128 + signal_module.SIGHUP
    assert stopped == []

    opencode_channel._restore_signal_handlers(previous)
    assert signal_module.getsignal(signal_module.SIGHUP) is original_sighup
    assert signal_module.getsignal(signal_module.SIGTERM) is original_sigterm
    assert session_id  # keep linter calm about unused
    assert stopped == []


def test_launch_mode_and_owner_persisted_then_read_back(monkeypatch, tmp_path):
    session_id = str(uuid4())
    config_path = tmp_path / "config-content.json"
    config_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        opencode_channel,
        "_resolve_opencode_binary",
        lambda explicit=None: "/opt/homebrew/bin/opencode",
    )
    monkeypatch.setattr(opencode_channel, "_write_opencode_runtime_config_content", lambda **_kwargs: config_path)
    monkeypatch.setattr(opencode_channel.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(opencode_channel, "_wait_for_server_url", lambda *_a, **_k: "http://127.0.0.1:57777")
    monkeypatch.setattr(opencode_channel, "_assert_health_ready", lambda **_k: None)
    monkeypatch.setattr(opencode_channel, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(opencode_channel, "_create_opencode_session", lambda **_k: "ses_test123")
    monkeypatch.setattr(
        opencode_channel,
        "_process_identity",
        lambda pid: ("Mon May 27 00:00:00 2026", f"cmd-for-{pid}"),
    )

    opencode_channel.launch_opencode_server_bridge(
        session_id=session_id,
        cwd=tmp_path,
        api_url="https://longhouse.test",
        api_token="zdt_test_token",
        device_id="work-laptop",
        config_dir=tmp_path / "config",
        launch_mode=opencode_channel.LAUNCH_MODE_KEEP_SERVER,
        owner_wrapper_pid=9988,
    )

    state = opencode_channel.read_opencode_server_bridge_state(session_id, config_dir=tmp_path / "config")
    assert state.launch_mode == opencode_channel.LAUNCH_MODE_KEEP_SERVER
    assert state.owner_wrapper_pid == 9988
    assert state.owner_wrapper_start_time == "Mon May 27 00:00:00 2026"


def test_launch_rejects_unknown_launch_mode(tmp_path):
    import pytest

    with pytest.raises(opencode_channel.OpenCodeServerBridgeError, match="launch_mode"):
        opencode_channel.launch_opencode_server_bridge(
            session_id=str(uuid4()),
            cwd=tmp_path,
            api_url="https://longhouse.test",
            api_token="zdt_test_token",
            device_id="work-laptop",
            launch_mode="bogus",
        )


def test_state_with_missing_optional_fields_reads_as_empty(tmp_path):
    # Defensive parse: a state file missing the optional identity/launch_mode
    # fields (e.g. a partial write) reads back as safe empty defaults.
    session_id = str(uuid4())
    _write_state(tmp_path, session_id=session_id)
    state = opencode_channel.read_opencode_server_bridge_state(session_id, config_dir=tmp_path / "config")
    assert state.launch_mode == ""
    assert state.owner_wrapper_pid == 0
    assert state.process_command == ""


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
