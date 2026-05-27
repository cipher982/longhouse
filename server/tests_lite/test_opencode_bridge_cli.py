from __future__ import annotations

import json
import os

import httpx
import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.cli import opencode_bridge as bridge_cli
from zerg.cli.main import app
from zerg.services import opencode_bridge_state as bridge_state


def _write_state(tmp_path, *, session_id="session-123", opencode_session_id=None, opencode_pid=12345):
    return bridge_state.write_opencode_bridge_state(
        session_id=session_id,
        server_url="http://127.0.0.1:54321",
        server_password="test-password",
        cwd=str(tmp_path),
        opencode_pid=opencode_pid,
        opencode_session_id=opencode_session_id,
        config_dir=tmp_path / "config",
    )


def _make_transport(handler):
    """Build an httpx.MockTransport from a request -> Response handler."""

    def _wrapped(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(_wrapped)


def _patch_client_factory(monkeypatch, transport):
    real = bridge_cli._client_from_state

    def fake(state):
        client, url = real(state)
        client.close()
        new_client = httpx.Client(
            base_url=url,
            auth=client.auth,
            timeout=2.0,
            transport=transport,
        )
        return new_client, url

    monkeypatch.setattr(bridge_cli, "_client_from_state", fake)


def test_send_posts_message_and_creates_session_when_missing(monkeypatch, tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "oc-sess-1"})
        if request.method == "POST" and request.url.path == "/session/oc-sess-1/message":
            posted.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "send",
            "--session-id",
            "session-123",
            "--text",
            "hi from longhouse",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert posted == [{"parts": [{"type": "text", "text": "hi from longhouse"}]}]


def test_send_uses_explicit_opencode_session_id(monkeypatch, tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/session/explicit-id/message":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "send",
            "--session-id",
            "session-123",
            "--text",
            "hi",
            "--opencode-session-id",
            "explicit-id",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    # No /session listing/creation — explicit override means the bridge skips discovery.
    assert seen_paths == ["POST /session/explicit-id/message"]


def test_send_reports_busy_session(monkeypatch, tmp_path):
    _write_state(tmp_path, opencode_session_id="oc-sess-1")
    runner = CliRunner()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session/oc-sess-1/message":
            return httpx.Response(409, text="busy")
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "send",
            "--session-id",
            "session-123",
            "--text",
            "hi",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 1
    assert "busy" in result.output.lower() or "409" in result.output


def test_interrupt_calls_abort(monkeypatch, tmp_path):
    _write_state(tmp_path, opencode_session_id="oc-sess-1")
    runner = CliRunner()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/session/oc-sess-1/abort":
            return httpx.Response(204)
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "interrupt",
            "--session-id",
            "session-123",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "POST /session/oc-sess-1/abort" in calls


def test_interrupt_falls_back_to_sigint_when_abort_fails(monkeypatch, tmp_path):
    _write_state(tmp_path, opencode_session_id="oc-sess-1", opencode_pid=99999)
    runner = CliRunner()
    sent_signals: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session/oc-sess-1/abort":
            return httpx.Response(500, text="kaboom")
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))
    monkeypatch.setattr(bridge_cli.os, "kill", lambda pid, sig: sent_signals.append((pid, sig)))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "interrupt",
            "--session-id",
            "session-123",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent_signals and sent_signals[0][0] == 99999


def test_interrupt_no_session_no_fallback_errors(monkeypatch, tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "interrupt",
            "--session-id",
            "session-123",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
            "--no-fallback-signal",
        ],
    )
    assert result.exit_code == 1


def test_steer_aborts_then_sends(monkeypatch, tmp_path):
    _write_state(tmp_path, opencode_session_id="oc-sess-1")
    runner = CliRunner()
    calls: list[str] = []
    list_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if request.method == "POST" and path == "/session/oc-sess-1/abort":
            return httpx.Response(204)
        if request.method == "GET" and path == "/session":
            list_calls["count"] += 1
            # Report busy briefly, then idle so steer waits and proceeds.
            busy = list_calls["count"] <= 1
            return httpx.Response(
                200,
                json=[{"id": "oc-sess-1", "busy": busy}],
            )
        if request.method == "POST" and path == "/session/oc-sess-1/message":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "steer",
            "--session-id",
            "session-123",
            "--text",
            "stop and switch",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
            "--idle-timeout-secs",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "POST /session/oc-sess-1/abort" in calls
    assert "POST /session/oc-sess-1/message" in calls


def test_permission_reply_posts_decision(monkeypatch, tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/permission/req-42/reply":
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(204)
        return httpx.Response(404)

    _patch_client_factory(monkeypatch, _make_transport(handler))

    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "permission-reply",
            "--session-id",
            "session-123",
            "--request-id",
            "req-42",
            "--decision",
            "allow",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert bodies == [{"decision": "allow"}]


def test_permission_reply_rejects_invalid_decision(tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "permission-reply",
            "--session-id",
            "session-123",
            "--request-id",
            "req-42",
            "--decision",
            "maybe",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "decision" in result.output.lower()


def test_inspect_redacts_password_by_default(tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "inspect",
            "--session-id",
            "session-123",
            "--config-dir",
            str(tmp_path / "config"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "test-password" not in result.output
    assert "<redacted>" in result.output


def test_inspect_can_disable_redaction(tmp_path):
    _write_state(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "inspect",
            "--session-id",
            "session-123",
            "--config-dir",
            str(tmp_path / "config"),
            "--no-redact-password",
        ],
    )
    assert result.exit_code == 0
    assert "test-password" in result.output


def test_send_reports_missing_state(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "opencode-bridge",
            "send",
            "--session-id",
            "session-does-not-exist",
            "--text",
            "hi",
            "--config-dir",
            str(tmp_path / "config"),
            "--wait-secs",
            "0",
        ],
    )
    assert result.exit_code == 1
    combined = (result.output or "")
    assert "bridge state" in combined.lower() or "no opencode" in combined.lower()


def test_resolve_target_session_id_picks_newest(monkeypatch, tmp_path):
    """Direct unit test for ordering when state has no opencode_session_id."""

    listing = [
        {"id": "old-1", "updated": 100.0},
        {"id": "newer-2", "updated": 300.0},
        {"id": "middle-3", "updated": 200.0},
    ]

    class FakeClient:
        def get(self, path):
            assert path == "/session"

            class R:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return listing

            return R()

    sid = bridge_cli._resolve_target_session_id(
        FakeClient(),  # type: ignore[arg-type]
        explicit=None,
        fallback=None,
        create_if_missing=False,
    )
    assert sid == "newer-2"


def test_format_error_includes_status():
    request = httpx.Request("POST", "http://example/abort")
    response = httpx.Response(500, text="boom", request=request)
    err = httpx.HTTPStatusError("server error", request=request, response=response)
    formatted = bridge_cli._format_error(err)
    assert "500" in formatted
    assert "boom" in formatted


def test_client_from_state_requires_url_and_password():
    with pytest.raises(bridge_cli._BridgeError):
        bridge_cli._client_from_state({"server_password": "pw"})
    with pytest.raises(bridge_cli._BridgeError):
        bridge_cli._client_from_state({"server_url": "http://x"})
