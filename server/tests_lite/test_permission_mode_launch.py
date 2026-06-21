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
