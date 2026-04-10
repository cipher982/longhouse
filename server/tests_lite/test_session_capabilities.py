from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

import zerg.routers.agents_sessions as agents_sessions_router
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


def test_build_session_capabilities_infers_cloud_takeover_from_saved_branch_context():
    session = _make_session(
        execution_home="legacy",
        continuation_kind="cloud",
        origin_label="Cloud",
        environment="cloud",
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.execution_home.value == "cloud_takeover"
    assert capabilities.managed_transport is None
    assert capabilities.live_control_available is False
    assert capabilities.cloud_branch_available is False  # frozen for launch
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False
    assert capabilities.home_label is None  # cloud labels hidden for launch


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
    assert capabilities.cloud_branch_available is False
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
    assert capabilities.cloud_branch_available is False  # frozen for launch


@pytest.mark.asyncio
async def test_load_surface_runtime_state_map_returns_loaded_state(monkeypatch):
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
    )
    load_calls: list[list[object]] = []

    def fake_load_runtime_state_map(_db, session_ids):
        load_calls.append(list(session_ids))
        return {str(session.id): {"pass": len(load_calls)}}

    monkeypatch.setattr(agents_sessions_router, "load_runtime_state_map", fake_load_runtime_state_map)

    runtime_state_map = await agents_sessions_router._load_surface_runtime_state_map(
        db=object(),
        sessions=[session],
        owner_id=7,
        occurred_at=datetime.now(timezone.utc),
    )

    assert load_calls == [[session.id]]
    assert runtime_state_map == {str(session.id): {"pass": 1}}
