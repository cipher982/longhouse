from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.services.managed_local_transport import ManagedLocalTransportError
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_local_transport import build_managed_local_interrupt_command
from zerg.services.managed_local_transport import build_managed_local_send_text_command
from zerg.services.managed_local_transport import build_managed_local_steer_text_command
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
        "--channels server:longhouse-channel" in inner
    )


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


def test_build_managed_local_send_text_command_uses_engine_bridge_for_codex_app_server():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        provider="codex",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert 'engine="$(command -v longhouse-engine || true)"' in inner
    assert '"$engine" codex-bridge send --session-id session-123 --text continue' in inner


def test_build_managed_local_send_text_command_appends_attachments_json_for_codex():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        provider="codex",
    )
    refs = [
        {
            "id": "att_1",
            "mime_type": "image/png",
            "sha256": "a" * 64,
            "blob_url": "/api/agents/sessions/session-123/inputs/1/attachments/att_1/blob",
        }
    ]
    command = build_managed_local_send_text_command(
        session=session,
        text="hello",
        attachments=refs,
    )
    inner = _wrapped_inner(command)
    assert "--attachments-json" in inner
    # Either the JSON is single-quote-shell-quoted; assert it contains the id and sha.
    assert "att_1" in inner
    assert "a" * 64 in inner


def test_build_managed_local_send_text_command_rejects_attachments_for_claude_channel():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider="claude",
    )
    with pytest.raises(ManagedLocalTransportError):
        build_managed_local_send_text_command(
            session=session,
            text="hello",
            attachments=[
                {
                    "id": "x",
                    "mime_type": "image/png",
                    "sha256": "a" * 64,
                    "blob_url": "/x",
                }
            ],
        )


def test_build_managed_local_send_text_command_uses_local_bridge_for_claude_channel_transport():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider="claude",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse claude-channel send --session-id session-123 --text continue" in inner


def test_build_managed_local_send_text_command_uses_opencode_server_bridge():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value,
        provider="opencode",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse opencode-channel send --session-id session-123 --text continue" in inner


def test_build_managed_local_send_text_command_uses_antigravity_hook_inbox():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX.value,
        provider="antigravity",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse antigravity-channel send --session-id session-123 --text continue" in inner


def test_build_managed_local_steer_text_command_uses_local_bridge_for_claude_channel_transport():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider="claude",
    )

    command = build_managed_local_steer_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse claude-channel send --session-id session-123 --text continue --meta intent=steer" in inner


def test_build_managed_local_steer_text_command_rejects_attachments_for_claude_channel():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider="claude",
    )

    with pytest.raises(ManagedLocalTransportError, match="Attachments are only supported"):
        build_managed_local_steer_text_command(
            session=session,
            text="continue",
            attachments=[
                {
                    "id": "x",
                    "mime_type": "image/png",
                    "sha256": "a" * 64,
                    "blob_url": "/x",
                }
            ],
        )


def test_build_managed_local_interrupt_command_uses_engine_bridge_for_codex_app_server():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
    )

    command = build_managed_local_interrupt_command(session=session)
    inner = _wrapped_inner(command)
    assert 'engine="$(command -v longhouse-engine || true)"' in inner
    assert '"$engine" codex-bridge interrupt --session-id session-123' in inner


def test_build_managed_local_interrupt_command_uses_local_bridge_for_claude_channel_transport():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
    )

    command = build_managed_local_interrupt_command(session=session)
    inner = _wrapped_inner(command)
    assert "exec longhouse claude-channel interrupt --session-id session-123" in inner


def test_build_managed_local_interrupt_command_uses_opencode_server_bridge():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value,
    )

    command = build_managed_local_interrupt_command(session=session)
    inner = _wrapped_inner(command)
    assert "exec longhouse opencode-channel interrupt --session-id session-123" in inner


def test_build_managed_local_send_text_command_rejects_unsupported_transport():
    session = SimpleNamespace(
        id="session-123",
        managed_transport="tmux",
    )

    with pytest.raises(ManagedLocalTransportError, match="Unsupported managed local transport: tmux"):
        build_managed_local_send_text_command(session=session, text="continue")


def test_build_managed_local_send_text_command_uses_opencode_bridge_for_opencode_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse opencode-bridge send --session-id session-123 --text continue" in inner


def test_build_managed_local_steer_text_command_rejects_opencode_server_bridge():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value,
    )

    with pytest.raises(ManagedLocalTransportError, match="opencode_server_bridge"):
        build_managed_local_steer_text_command(session=session, text="continue")


def test_build_managed_local_send_text_command_rejects_antigravity_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_PROCESS.value,
    )

    with pytest.raises(ManagedLocalTransportError, match="does not support remote text sends yet"):
        build_managed_local_send_text_command(session=session, text="continue")


def test_build_managed_local_steer_text_command_rejects_antigravity_hook_inbox():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX.value,
    )

    with pytest.raises(ManagedLocalTransportError, match="antigravity_hook_inbox"):
        build_managed_local_steer_text_command(session=session, text="continue")


def test_build_managed_local_interrupt_command_uses_opencode_bridge_for_opencode_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
    )

    command = build_managed_local_interrupt_command(session=session)
    inner = _wrapped_inner(command)
    assert "exec longhouse opencode-bridge interrupt --session-id session-123" in inner


def test_build_managed_local_steer_command_rejects_opencode_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
    )
    from zerg.services.managed_local_transport import build_managed_local_steer_text_command

    with pytest.raises(ManagedLocalTransportError, match="opencode_process"):
        build_managed_local_steer_text_command(session=session, text="abort and switch")


def test_build_managed_local_steer_command_rejects_attachments_for_opencode_process_as_unsupported():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
    )
    from zerg.services.managed_local_transport import build_managed_local_steer_text_command

    with pytest.raises(ManagedLocalTransportError, match="opencode_process"):
        build_managed_local_steer_text_command(
            session=session,
            text="hi",
            attachments=[{"id": "x", "mime_type": "image/png", "sha256": "a" * 64, "blob_url": "/x"}],
        )


def test_build_managed_local_interrupt_command_rejects_antigravity_process():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_PROCESS.value,
    )

    with pytest.raises(ManagedLocalTransportError, match="does not support remote interrupts yet"):
        build_managed_local_interrupt_command(session=session)


def test_build_managed_local_interrupt_command_rejects_antigravity_hook_inbox():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX.value,
    )

    with pytest.raises(ManagedLocalTransportError, match="does not support remote interrupts yet"):
        build_managed_local_interrupt_command(session=session)


def test_transport_for_provider():
    assert ManagedSessionTransport.for_provider("codex") == ManagedSessionTransport.CODEX_APP_SERVER
    assert ManagedSessionTransport.for_provider("claude") == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
    assert ManagedSessionTransport.for_provider("opencode") == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE
    assert ManagedSessionTransport.for_provider("antigravity") == ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX
