from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.session_execution_home import ManagedSessionTransport


def _wrapped_inner(command: str) -> str:
    parts = shlex.split(command)
    assert parts[:2] == ["zsh", "-lc"]
    return parts[2]


def test_build_managed_local_attach_command_uses_engine_bridge_for_codex_app_server():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert 'engine="$(command -v longhouse-engine || true)"' in inner
    assert "command -v codex" in inner
    assert 'exec "$engine" codex-bridge attach --session-id session-123' in inner


def test_build_managed_local_attach_command_uses_native_claude_session_id_for_channel_bridge():
    session = SimpleNamespace(
        id="session-123",
        provider_session_id="provider-123",
        cwd="/tmp/demo",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert "export LONGHOUSE_MANAGED_SESSION_ID=session-123" in inner
    assert "export LONGHOUSE_CHANNEL_SESSION_ID=session-123" in inner
    assert "export LONGHOUSE_PROVIDER_SESSION_ID=provider-123" in inner
    assert (
        "exec claude --dangerously-skip-permissions --session-id provider-123 "
        "--dangerously-load-development-channels server:longhouse-channel" in inner
    )
    assert "--channels server:longhouse-channel" not in inner


def test_build_managed_local_attach_command_uses_opencode_bridge_for_opencode_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert "exec longhouse opencode-bridge inspect --session-id session-123" in inner


def test_build_managed_local_attach_command_uses_opencode_server_bridge():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value,
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert "command -v opencode" in inner
    assert "exec longhouse opencode-channel attach --session-id session-123" in inner


def test_build_managed_local_attach_command_resumes_cursor_native_conversation():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CURSOR_HELM.value,
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert "command -v cursor-agent" in inner
    assert "exec longhouse cursor --resume-session session-123" in inner


def test_build_managed_local_attach_command_is_empty_for_antigravity_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_PROCESS.value,
    )

    assert build_managed_local_attach_command(session=session) is None


def test_build_managed_local_attach_command_is_empty_for_antigravity_hook_inbox():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX.value,
    )

    assert build_managed_local_attach_command(session=session) is None


def test_transport_for_provider():
    assert ManagedSessionTransport.for_provider("codex") == ManagedSessionTransport.CODEX_APP_SERVER
    assert ManagedSessionTransport.for_provider("claude") == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
    assert ManagedSessionTransport.for_provider("opencode") == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE
    assert ManagedSessionTransport.for_provider("antigravity") == ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX
