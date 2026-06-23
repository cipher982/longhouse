"""permission_mode flows model -> attach command (server-side plumbing).

Proves a remote_approve session produces a Claude exec command WITHOUT
--dangerously-skip-permissions and WITH the gate engaged, while the default
bypass keeps the autonomous flags. The CLI session-scoped-token swap is the
separate live-launch step (3d-cli).
"""

from __future__ import annotations

import os
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-permmode")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from types import SimpleNamespace

from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.session_execution_home import ManagedSessionTransport


def _fake_claude_session(permission_mode: str):
    # Minimal non-ORM fixture: build_managed_local_attach_command reads
    # managed_transport + permission_mode directly when no DB session is attached.
    return SimpleNamespace(
        id=uuid4(),
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        provider_session_id="prov-123",
        cwd="/tmp/demo",
        primary_thread_id=None,
        permission_mode=permission_mode,
    )


def test_bypass_session_attach_command_keeps_autonomous_flags():
    cmd = build_managed_local_attach_command(session=_fake_claude_session("bypass"))
    assert cmd is not None
    assert "--dangerously-skip-permissions" in cmd
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=0" in cmd


def test_remote_approve_session_attach_command_engages_gate():
    cmd = build_managed_local_attach_command(session=_fake_claude_session("remote_approve"))
    assert cmd is not None
    assert "--dangerously-skip-permissions" not in cmd
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=1" in cmd


def test_missing_permission_mode_defaults_to_bypass():
    session = _fake_claude_session("bypass")
    delattr(session, "permission_mode")
    cmd = build_managed_local_attach_command(session=session)
    assert cmd is not None
    assert "--dangerously-skip-permissions" in cmd
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=0" in cmd


def test_launch_response_mints_scoped_hook_token_only_for_remote_approve():
    """remote_approve launches return a session-scoped zht_ hook token bound to
    the session; bypass launches return none (gate dormant, device token rejected)."""
    from uuid import uuid4

    import zerg.services.session_chat_impl as impl
    from zerg.auth.managed_local_hook_tokens import validate_managed_local_hook_token
    from zerg.services.session_kernel_projection import SessionKernelProjection
    from zerg.session_execution_home import SessionExecutionHome
    from zerg.session_execution_home import ManagedSessionTransport

    class _Caps:
        live_control_available = True
        host_reattach_available = True
        execution_home = SessionExecutionHome.MANAGED_LOCAL
        managed_transport = ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
        connection_id = None

    sid = uuid4()
    session = SimpleNamespace(
        id=sid,
        provider="claude",
        provider_session_id="prov-1",
        loop_mode="assist",
        source_runner_id=None,
        source_runner_name="cinder",
        managed_session_name="lh-x",
        project="proj",
        device_id="cinder",
        permission_mode="remote_approve",
    )
    result = SimpleNamespace(
        session=session,
        attach_command=(
            "zsh -lc 'export LONGHOUSE_PROVIDER_SESSION_ID=prov-1; "
            "exec claude --session-id prov-1 --dangerously-load-development-channels server:longhouse-channel'"
        ),
    )

    # The subject of this test is the hook_token minting logic. Stub the control
    # projections the response builder calls so we don't reproduce their internals.
    control_stub = SimpleNamespace(source_runner_id=None, source_runner_name="cinder", managed_session_name="lh-x")
    saved = impl.project_session_kernel_fields
    impl.project_session_kernel_fields = lambda db, session: SessionKernelProjection(
        capabilities=_Caps(),
        lineage=SimpleNamespace(),
        control=control_stub,
        provider_session_id="prov-1",
    )
    try:
        resp = impl._managed_local_launch_response(None, result, owner_id=42)
        assert resp.permission_mode == "remote_approve"
        assert resp.hook_token and resp.hook_token.startswith("zht_")
        decoded = validate_managed_local_hook_token(resp.hook_token)
        assert decoded is not None and decoded.session_id == str(sid)

        session.permission_mode = "bypass"
        resp_bypass = impl._managed_local_launch_response(None, result, owner_id=42)
        assert resp_bypass.permission_mode == "bypass"
        assert resp_bypass.hook_token is None
    finally:
        impl.project_session_kernel_fields = saved
