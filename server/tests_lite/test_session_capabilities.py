from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.services.session_capabilities import build_session_capabilities


def _make_session(**overrides):
    values = {
        "id": uuid4(),
        "provider": "claude",
        "execution_home": "legacy",
        "continuation_kind": None,
        "origin_label": None,
        "environment": "development",
        "managed_transport": None,
        "source_runner_id": None,
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_session_capabilities_marks_native_managed_local_session():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.execution_home.value == "managed_local"
    assert capabilities.managed_transport is not None
    assert capabilities.managed_transport.value == "claude_channel_bridge"
    assert capabilities.live_control_available is True
    assert capabilities.host_reattach_available is True
    assert capabilities.reply_to_live_session_available is True
    assert capabilities.home_label == "On this Mac"


def test_build_session_capabilities_drops_legacy_tmux_sessions_out_of_live_control():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.managed_transport is None
    assert capabilities.live_control_available is False
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False
