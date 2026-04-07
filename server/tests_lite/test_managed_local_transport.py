from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_local_transport import build_managed_local_interrupt_command
from zerg.services.managed_local_transport import build_managed_local_launch_transport_plan
from zerg.services.managed_local_transport import build_managed_local_send_text_command
from zerg.session_execution_home import ManagedSessionTransport


def _wrapped_inner(command: str) -> str:
    parts = shlex.split(command)
    assert parts[:2] == ["zsh", "-lc"]
    return parts[2]


def test_build_managed_local_launch_transport_plan_wraps_tmux_commands():
    plan = build_managed_local_launch_transport_plan(
        session_name="lh-demo",
        cwd="/tmp/demo",
        entry_command="codex --enable codex_hooks",
        session_id="session-123",
        provider="codex",
        tmux_tmpdir="/tmp/lh-transport",
    )

    assert plan.transport == ManagedSessionTransport.TMUX
    assert "start-server" in _wrapped_inner(plan.launch_command)
    assert 'LONGHOUSE_MANAGED_ARTIFACT_DIR="${HOME}/.claude/longhouse-managed/session-123"' in _wrapped_inner(
        plan.launch_command
    )
    assert "attach -t lh-demo" in _wrapped_inner(str(plan.attach_command))
    assert "has-session -t lh-demo" in _wrapped_inner(plan.verify_session_command)
    assert "kill-session -t lh-demo" in _wrapped_inner(str(plan.cleanup_command))


def test_build_managed_local_attach_command_uses_engine_bridge_for_codex_app_server():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        managed_session_name="lh-demo",
        managed_tmux_tmpdir="/tmp/lh-transport",
    )

    command = build_managed_local_attach_command(session=session)
    assert command is not None
    inner = _wrapped_inner(command)
    assert 'engine="$(command -v longhouse-engine || true)"' in inner
    assert "command -v codex" in inner
    assert 'exec "$engine" codex-bridge attach --session-id session-123' in inner


def test_build_managed_local_attach_command_uses_native_claude_resume_for_channel_bridge():
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
        "exec claude --dangerously-skip-permissions --resume provider-123 "
        "--dangerously-load-development-channels server:longhouse-channel" in inner
    )


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


def test_build_managed_local_send_text_command_uses_local_bridge_for_claude_channel_transport():
    session = SimpleNamespace(
        id="session-123",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider="claude",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert "exec longhouse claude-channel send --session-id session-123 --text continue" in inner


def test_build_managed_local_send_text_command_uses_codex_bracketed_paste_for_tmux():
    session = SimpleNamespace(
        managed_transport=ManagedSessionTransport.TMUX.value,
        managed_session_name="lh-demo",
        managed_tmux_tmpdir="/tmp/lh-transport",
        provider="codex",
    )

    command = build_managed_local_send_text_command(session=session, text="continue")
    inner = _wrapped_inner(command)
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} set-buffer -b send-lh-demo continue" in inner
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} paste-buffer -dpr -b send-lh-demo -t lh-demo" in inner


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


def test_build_managed_local_interrupt_command_uses_tmux_c_c_for_tmux():
    session = SimpleNamespace(
        managed_transport=ManagedSessionTransport.TMUX.value,
        managed_session_name="lh-demo",
        managed_tmux_tmpdir="/tmp/lh-transport",
    )

    command = build_managed_local_interrupt_command(session=session)
    inner = _wrapped_inner(command)
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo C-c" in inner


def test_transport_for_provider():
    assert ManagedSessionTransport.for_provider("codex") == ManagedSessionTransport.CODEX_APP_SERVER
    assert ManagedSessionTransport.for_provider("claude") == ManagedSessionTransport.TMUX
    assert (
        ManagedSessionTransport.for_provider("claude", machine_name="work-laptop")
        == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
    )
    assert (
        ManagedSessionTransport.for_provider(
            "claude",
            machine_name="work-laptop",
            native_claude_channels_available=False,
        )
        == ManagedSessionTransport.TMUX
    )
