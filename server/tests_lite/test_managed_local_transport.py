from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.managed_local_transport import ManagedLocalTransportNotImplementedError
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_local_transport import build_managed_local_launch_transport_plan
from zerg.services.managed_local_transport import build_managed_local_send_text_command
from zerg.services.managed_local_transport import coerce_managed_transport
from zerg.services.managed_local_transport import managed_local_transport_supports_interactive_chat
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.session_execution_home import ManagedSessionTransport


def _wrapped_inner(command: str) -> str:
    parts = shlex.split(command)
    assert parts[:2] == ["zsh", "-lc"]
    return parts[2]


def test_coerce_managed_transport_accepts_codex_app_server_and_default():
    assert coerce_managed_transport("codex_app_server") == ManagedSessionTransport.CODEX_APP_SERVER
    assert coerce_managed_transport(None, default=ManagedSessionTransport.TMUX) == ManagedSessionTransport.TMUX


def test_build_managed_local_launch_transport_plan_wraps_tmux_commands():
    plan = build_managed_local_launch_transport_plan(
        transport=ManagedSessionTransport.TMUX,
        session_name="lh-demo",
        cwd="/tmp/demo",
        entry_command="codex --enable codex_hooks",
        tmux_tmpdir="/tmp/lh-transport",
    )

    assert plan.transport == ManagedSessionTransport.TMUX
    assert "start-server" in _wrapped_inner(plan.launch_command)
    assert f"attach -t lh-demo" in _wrapped_inner(str(plan.attach_command))
    assert f"has-session -t lh-demo" in _wrapped_inner(plan.verify_session_command)
    assert f"kill-session -t lh-demo" in _wrapped_inner(str(plan.cleanup_command))


def test_build_managed_local_launch_transport_plan_rejects_unimplemented_transport():
    try:
        build_managed_local_launch_transport_plan(
            transport=ManagedSessionTransport.CODEX_APP_SERVER,
            session_name="lh-demo",
            cwd="/tmp/demo",
            entry_command="codex app-server",
        )
    except ManagedLocalTransportNotImplementedError as exc:
        assert "codex_app_server" in str(exc)
    else:
        raise AssertionError("expected ManagedLocalTransportNotImplementedError")


def test_build_managed_local_attach_command_returns_none_for_non_tmux_transport():
    session = SimpleNamespace(
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        managed_session_name="lh-demo",
        managed_tmux_tmpdir="/tmp/lh-transport",
    )

    assert build_managed_local_attach_command(session=session) is None


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


def test_managed_local_transport_supports_interactive_chat_only_for_tmux():
    assert managed_local_transport_supports_interactive_chat(ManagedSessionTransport.TMUX.value) is True
    assert managed_local_transport_supports_interactive_chat(ManagedSessionTransport.CODEX_APP_SERVER.value) is False
