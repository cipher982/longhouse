from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import zerg.routers.agents_sessions as agents_sessions_router
from zerg.services.session_capabilities import build_session_capabilities


def _make_session(**overrides):
    now = datetime.now(timezone.utc)
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
    assert capabilities.cloud_branch_available is True
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False
    assert capabilities.home_label == "Moved to cloud"
    assert capabilities.requires_managed_local_tmux_reconcile is False


def test_build_session_capabilities_marks_managed_local_tmux_for_reconcile():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.execution_home.value == "managed_local"
    assert capabilities.managed_transport is not None
    assert capabilities.managed_transport.value == "tmux"
    assert capabilities.live_control_available is True
    assert capabilities.cloud_branch_available is False
    assert capabilities.host_reattach_available is True
    assert capabilities.reply_to_live_session_available is True
    assert capabilities.home_label == "On this Mac"
    assert capabilities.requires_managed_local_tmux_reconcile is True


@pytest.mark.asyncio
async def test_load_surface_runtime_state_map_reconciles_tmux_sessions(monkeypatch):
    session = _make_session(
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=17,
    )
    load_calls: list[list[object]] = []
    reconcile_calls: list[dict[str, object]] = []

    def fake_load_runtime_state_map(_db, session_ids):
        load_calls.append(list(session_ids))
        return {str(session.id): {"pass": len(load_calls)}}

    async def fake_reconcile(_db, *, sessions, owner_id, occurred_at):
        reconcile_calls.append(
            {
                "session_ids": [item.id for item in sessions],
                "owner_id": owner_id,
                "occurred_at": occurred_at,
            }
        )
        return {session.id}

    monkeypatch.setattr(agents_sessions_router, "load_runtime_state_map", fake_load_runtime_state_map)
    monkeypatch.setattr(agents_sessions_router, "reconcile_managed_local_tmux_sessions", fake_reconcile)

    runtime_state_map = await agents_sessions_router._load_surface_runtime_state_map(
        db=object(),
        sessions=[session],
        owner_id=7,
        occurred_at=datetime.now(timezone.utc),
    )

    assert load_calls == [[session.id], [session.id]]
    assert reconcile_calls
    assert reconcile_calls[0]["session_ids"] == [session.id]
    assert reconcile_calls[0]["owner_id"] == 7
    assert runtime_state_map == {str(session.id): {"pass": 2}}
