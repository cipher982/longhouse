from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID

import zerg.database as database_module
from zerg.routers import agents_sessions
from zerg.services.managed_control_state import _load_live_managed_control_state_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_views import latest_live_launch_readiness


def _fail_live_sqlite():
    raise AssertionError("Runtime Host must not open live SQLite")


def test_hot_fact_consumers_use_catalog_batch_without_sqlite(monkeypatch):
    session_id = "11111111-1111-4111-8111-111111111111"
    now = datetime.now(UTC).replace(microsecond=0)
    facts = {
        "catalog": {"session_id": session_id},
        "runtime": {
            "runtime_key": f"codex:{session_id}",
            "session_id": session_id,
            "provider": "codex",
            "phase": "quiescent",
            "phase_source": "hook",
            "timeline_anchor_at": now.isoformat(),
            "runtime_version": 4,
            "updated_at": now.isoformat(),
        },
        "readiness": {
            "session_id": session_id,
            "owner_id": "7",
            "provider": "codex",
            "device_id": "cinder",
            "execution_lifetime": "live_control",
            "state": "adopted",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
        "control_leases": [
            {
                "id": 1,
                "session_id": session_id,
                "provider": "codex",
                "device_id": "cinder",
                "machine_id": "cinder",
                "state": "attached",
                "sequence": 9,
                "heartbeat_at": now.isoformat(),
                "payload_json": (
                    '{"bridge_status":"ready","thread_subscription_status":"subscribed","lease_ttl_ms":900000}'
                ),
                "updated_at": now.isoformat(),
            }
        ],
    }
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", _fail_live_sqlite)
    monkeypatch.setattr("zerg.services.catalog_facts.session_facts_map", lambda _ids: {session_id: facts})

    runtime = load_runtime_state_map(None, [UUID(session_id)])
    controls = _load_live_managed_control_state_map([UUID(session_id)])
    readiness = latest_live_launch_readiness([UUID(session_id)], now=now)

    assert runtime[session_id].phase == "quiescent"
    assert controls[UUID(session_id)].control_state == "online"
    assert readiness[UUID(session_id)].launch_state == "live"


def test_active_candidate_ids_use_catalog_rpc_without_sqlite(monkeypatch):
    session_id = "22222222-2222-4222-8222-222222222222"
    now = datetime.now(UTC)
    monkeypatch.setattr(agents_sessions, "live_store_configured", lambda: True)
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "get_live_session_factory", _fail_live_sqlite)
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.active_session_ids",
        lambda **_kwargs: {"session_ids": [session_id], "commit_seq": "4"},
    )

    result = agents_sessions._active_live_session_candidates(limit=50, days_back=14, now=now)

    assert result == [UUID(session_id)]
