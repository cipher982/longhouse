from __future__ import annotations

import os

import pytest

# The session-identity-kernel cleanup deleted the ManagedSessionControlState
# table and the legacy ``project_current_session_capabilities*`` helpers that
# this module exercises end-to-end. The kernel projection
# (``project_session_capabilities``) is the new contract and has its own
# coverage in tests_lite/test_session_runtime.py and the timeline runtime
# tests. Keep this file as a placeholder so the module path stays valid for
# any inbound references; the assertions are obsolete.
pytest.skip(
    "session capability projection moved to the kernel; legacy tests retired",
    allow_module_level=True,
)

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from tests_lite._capability_test_helper import build_session_capabilities
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_liveness_facts import ActivityObservation
from zerg.services.session_liveness_facts import ControlObservation
from zerg.services.session_liveness_facts import HostObservation
from zerg.services.session_liveness_facts import LifecycleFact
from zerg.services.session_liveness_facts import PhaseObservation
from zerg.services.session_liveness_facts import ProcessObservation
from zerg.services.session_liveness_facts import SessionLivenessFacts
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_views import build_session_capabilities_response
from zerg.services.session_views import build_session_response
from zerg.session_execution_home import ManagedSessionTransport

NOW = datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc)


def _make_session(**overrides):
    values = {
        "id": uuid4(),
        "provider": "claude",
        "execution_home": "unmanaged_local",
        "continuation_kind": None,
        "origin_label": None,
        "environment": "development",
        "managed_transport": None,
        "source_runner_id": None,
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_session_capabilities.db'}")
    initialize_database(engine)
    return engine, make_sessionmaker(engine)


def _seed_agent_session(db, *, seed_kernel_rows: bool = True, **overrides) -> AgentSession:
    values = {
        "id": uuid4(),
        "provider": "codex",
        "environment": "test",
        "project": "zerg",
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "provider_session_id": str(uuid4()),
        "thread_root_session_id": None,
        "user_messages": 1,
        "assistant_messages": 1,
        "tool_calls": 0,
        "execution_home": "managed_local",
        "managed_transport": "codex_app_server",
        "source_runner_id": 17,
        "source_runner_name": "Demo MacBook",
    }
    values.update(overrides)
    if values["thread_root_session_id"] is None:
        values["thread_root_session_id"] = values["id"]
    session = AgentSession(**values)
    db.add(session)
    db.commit()
    db.refresh(session)
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    provider = str(session.provider or "").strip().lower()
    transport = str(session.managed_transport or "").strip().lower()
    home = str(session.execution_home or "").strip().lower()
    if seed_kernel_rows and home == "managed_local":
        if provider == "codex" or transport == "codex_app_server":
            kernel_plane = "codex_bridge"
        elif transport == "opencode_process":
            kernel_plane = "opencode_process"
        elif provider == "opencode" or transport == "opencode_server_bridge":
            kernel_plane = "opencode_server_bridge"
        elif provider == "antigravity" or transport == "antigravity_process":
            kernel_plane = "antigravity_process"
        else:
            kernel_plane = "claude_channel_bridge"
        seed_managed_kernel_rows(db, session, control_plane=kernel_plane)
        db.commit()
    return session


def _upsert_runtime_state(
    db,
    session: AgentSession,
    *,
    phase: str = "running",
    phase_source: str = "semantic",
    observed_at: datetime | None = None,
    freshness_expires_at: datetime | None = None,
    terminal_state: str | None = None,
) -> SessionRuntimeState:
    now = observed_at or datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session(str(session.provider or "codex"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first()
    values = {
        "runtime_key": runtime_key,
        "session_id": session.id,
        "provider": str(session.provider or "codex"),
        "device_id": session.device_id,
        "phase": phase,
        "phase_source": phase_source,
        "active_tool": "shell" if phase == "running" else None,
        "phase_started_at": now - timedelta(seconds=5),
        "last_runtime_signal_at": now - timedelta(seconds=5),
        "last_progress_at": now - timedelta(seconds=5),
        "last_live_at": now - timedelta(seconds=5),
        "timeline_anchor_at": now - timedelta(seconds=5),
        "freshness_expires_at": freshness_expires_at,
        "terminal_state": terminal_state,
        "terminal_at": now if terminal_state is not None else None,
        "runtime_version": int(getattr(state, "runtime_version", 0) or 0) + 1,
    }
    if state is None:
        state = SessionRuntimeState(**values)
        db.add(state)
    else:
        for key, value in values.items():
            setattr(state, key, value)
    db.commit()
    db.refresh(state)
    return state


def _upsert_control_state(
    db,
    session: AgentSession,
    *,
    control_state: str = "online",
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> ManagedSessionControlState:
    now = datetime.now(timezone.utc)
    row = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session.id).first()
    values = {
        "session_id": session.id,
        "provider": str(session.provider or "codex"),
        "device_id": session.device_id,
        "machine_id": session.device_id,
        "transport": session.managed_transport,
        "lease_state": "attached" if control_state == "online" else control_state,
        "control_state": control_state,
        "reason": reason,
        "source": "machine_heartbeat",
        "sequence": 1,
        "last_control_seen_at": now,
        "lease_observed_at": now,
        "lease_ttl_ms": 900_000,
        "control_expires_at": expires_at or now + timedelta(minutes=15),
    }
    if row is None:
        row = ManagedSessionControlState(**values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def _runtime_display(**overrides):
    values = {
        "lifecycle": "open",
        "host_state": "online",
        "activity_recency": "live",
        "state": "idle",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_opencode_process_transport_is_managed_but_not_remote_controllable():
    session = _make_session(
        provider="opencode",
        execution_home="managed_local",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS.value,
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.managed_transport == ManagedSessionTransport.OPENCODE_PROCESS
    assert capabilities.live_control_available is False
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False
    assert capabilities.can_queue_next_input is False
    assert capabilities.can_steer_active_turn is False


def test_opencode_server_bridge_transport_is_live_send_capable_but_not_steerable():
    session = _make_session(
        provider="opencode",
        execution_home="managed_local",
        managed_transport=ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value,
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.managed_transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE
    assert capabilities.live_control_available is True
    assert capabilities.host_reattach_available is True
    assert capabilities.reply_to_live_session_available is True
    assert capabilities.can_queue_next_input is True
    assert capabilities.can_steer_active_turn is False


def test_antigravity_process_transport_is_managed_but_not_remote_controllable():
    session = _make_session(
        provider="antigravity",
        execution_home="managed_local",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_PROCESS.value,
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.managed_transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS
    assert capabilities.live_control_available is False
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False
    assert capabilities.can_queue_next_input is False
    assert capabilities.can_steer_active_turn is False


def _liveness_facts(
    *,
    control_path: str = "managed",
    control_state: str = "online",
    control_reason: str | None = None,
    control_expires_at: datetime | None = NOW + timedelta(minutes=5),
    host_state: str = "online",
    process_status: str = "unknown",
    lifecycle_state: str = "open",
    lifecycle_reason: str | None = None,
    phase_kind: str | None = "idle",
    phase_tool: str | None = None,
    phase_expires_at: datetime | None = NOW + timedelta(minutes=1),
) -> SessionLivenessFacts:
    phase_source = "semantic" if phase_kind is not None else None
    process_source = "machine_process_scan" if process_status != "unknown" else None
    process_state = (
        "closed" if lifecycle_state == "closed" else "running" if process_status == "observed" else "unknown"
    )
    effective_control_state = "none" if control_path == "unmanaged" else control_state
    return SessionLivenessFacts(
        control_path=control_path,
        control=ControlObservation(
            state=effective_control_state,
            reason=control_reason,
            source="managed_control_lease" if control_path == "managed" else None,
            last_seen_at=NOW if effective_control_state not in {"unknown", "none"} else None,
            expires_at=control_expires_at,
            transport="claude_channel_bridge" if control_path == "managed" else None,
        ),
        process_state=process_state,
        host=HostObservation(
            state=host_state,
            last_seen_at=NOW if host_state != "unknown" else None,
            source="machine_heartbeat" if host_state != "unknown" else None,
        ),
        process=ProcessObservation(
            status=process_status,
            observed_at=NOW if process_status == "observed" else None,
            last_seen_at=NOW if process_status != "unknown" else None,
            source=process_source,
        ),
        phase=PhaseObservation(
            kind=phase_kind,
            tool=phase_tool,
            source=phase_source,
            observed_at=NOW if phase_kind is not None else None,
            expires_at=phase_expires_at if phase_kind is not None else None,
        ),
        activity=ActivityObservation(
            last_transcript_at=NOW,
            last_runtime_signal_at=NOW if phase_kind is not None else None,
            last_progress_at=None,
        ),
        lifecycle=LifecycleFact(
            state=lifecycle_state,
            reason=lifecycle_reason,
            observed_at=NOW if lifecycle_state != "unknown" else None,
        ),
    )


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


def test_managed_local_without_runner_metadata_is_observe_only_but_reattachable():
    session = _make_session(
        execution_home="managed_local",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        source_runner_id=None,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.execution_home.value == "managed_local"
    assert capabilities.managed_transport == ManagedSessionTransport.CODEX_APP_SERVER
    assert capabilities.live_control_available is False
    assert capabilities.reply_to_live_session_available is False
    assert capabilities.can_queue_next_input is False
    assert capabilities.can_steer_active_turn is False
    assert capabilities.host_reattach_available is True


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


def test_capability_response_prefers_source_runner_name_for_display_label():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="Demo MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(),
    )

    assert response.display_label == "Live on Demo MacBook"
    assert response.display_tone == "success"


def test_capability_response_marks_unmanaged_sessions_read_only():
    session = _make_session()
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(activity_recency="recent"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.display_label == "Read only"
    assert response.display_tone == "neutral"


def test_capability_response_does_not_claim_live_without_runtime_truth():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="Demo MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(session=session, capability_flags=capabilities)

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.display_label == "Control offline"
    assert response.display_tone == "warning"


def test_capability_response_marks_closed_managed_session_not_live_or_reattachable():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="Demo MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(lifecycle="closed", host_state="offline", activity_recency="stale"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.reply_to_live_session_available is False
    assert response.can_queue_next_input is False
    assert response.can_steer_active_turn is False
    assert response.display_label == "Closed"
    assert response.display_tone == "neutral"


def test_capability_response_marks_disconnected_managed_session_control_offline():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="Demo MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(host_state="stale"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.reply_to_live_session_available is False
    assert response.display_label == "Control offline"
    assert response.display_tone == "warning"


def test_current_capability_projection_only_allows_steer_during_active_runtime():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    idle = project_current_session_capabilities(capabilities, runtime_display=_runtime_display(state="idle"))
    running = project_current_session_capabilities(capabilities, runtime_display=_runtime_display(state="running"))

    assert idle.live_control_available is True
    assert idle.can_steer_active_turn is False
    assert running.live_control_available is True
    assert running.can_steer_active_turn is True


def test_fact_capability_projection_allows_managed_live_send_and_steer():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(phase_kind="running", phase_tool="shell"),
        now=NOW,
    )

    assert projected.live_control_available is True
    assert projected.reply_to_live_session_available is True
    assert projected.can_queue_next_input is True
    assert projected.can_steer_active_turn is True
    assert projected.host_reattach_available is False


def test_fact_capability_projection_marks_managed_offline_as_reattach_only():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            host_state="offline",
            phase_kind="running",
            control_state="offline",
            control_reason="host_offline",
            control_expires_at=NOW - timedelta(seconds=1),
        ),
        now=NOW,
    )

    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is True


def test_fact_capability_projection_leaves_unmanaged_observed_read_only():
    session = _make_session()
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            control_path="unmanaged",
            process_status="observed",
            phase_kind=None,
        ),
        now=NOW,
    )

    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is False


def test_fact_capability_projection_leaves_unmanaged_unknown_read_only():
    session = _make_session()
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            control_path="unmanaged",
            host_state="unknown",
            process_status="unknown",
            lifecycle_state="unknown",
            phase_kind=None,
        ),
        now=NOW,
    )

    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is False


def test_fact_capability_projection_disables_closed_managed_session():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            lifecycle_state="closed",
            lifecycle_reason="session_ended",
            phase_kind="running",
            phase_tool="shell",
        ),
        now=NOW,
    )

    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is False


def test_fact_capability_projection_allows_live_send_with_stale_phase_when_control_online():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            phase_kind="running",
            phase_tool="shell",
            phase_expires_at=NOW - timedelta(seconds=1),
            control_state="online",
            control_expires_at=NOW + timedelta(minutes=5),
        ),
        now=NOW,
    )

    assert projected.live_control_available is True
    assert projected.reply_to_live_session_available is True
    assert projected.can_queue_next_input is True
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is False


def test_fact_capability_projection_rejects_recent_phase_when_control_offline():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=_liveness_facts(
            phase_kind="running",
            phase_tool="shell",
            phase_expires_at=NOW + timedelta(minutes=5),
            control_state="offline",
            control_reason="lease_stale",
            control_expires_at=NOW - timedelta(seconds=1),
        ),
        now=NOW,
    )

    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.can_steer_active_turn is False
    assert projected.host_reattach_available is True


def test_fact_capability_projection_unknown_control_fails_closed_without_closing_lifecycle():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    facts = _liveness_facts(
        lifecycle_state="open",
        phase_kind=None,
        control_state="unknown",
        control_reason="cold_start",
        control_expires_at=None,
    )
    projected = project_current_session_capabilities_from_facts(
        capabilities,
        liveness_facts=facts,
        now=NOW,
    )

    assert facts.lifecycle.state == "open"
    assert projected.live_control_available is False
    assert projected.reply_to_live_session_available is False
    assert projected.can_queue_next_input is False
    assert projected.host_reattach_available is True


def test_capability_response_prefers_facts_over_runtime_display_labels():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
        source_runner_name="Demo MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(state="running"),
        runtime_facts=_liveness_facts(
            host_state="unknown",
            lifecycle_state="unknown",
            phase_kind=None,
        ),
    )

    assert response.live_control_available is False
    assert response.can_queue_next_input is False
    assert response.can_steer_active_turn is False
    assert response.host_reattach_available is True
    assert response.display_label == "Control offline"


def test_current_session_capabilities_uses_liveness_facts_for_runtime_gate(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)
    try:
        with session_local() as db:
            session = _seed_agent_session(db)
            _upsert_runtime_state(
                db,
                session,
                phase="running",
                freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            _upsert_control_state(db, session)
            monkeypatch.setattr(
                "zerg.services.session_current_control.managed_runner_host_state",
                lambda _db, _session: "online",
            )

            live = current_session_capabilities(db, session)

            assert live.live_control_available is True
            assert live.can_queue_next_input is True
            assert live.can_steer_active_turn is True
            assert live.host_reattach_available is False

            monkeypatch.setattr(
                "zerg.services.session_current_control.managed_runner_host_state",
                lambda _db, _session: "offline",
            )
            _upsert_control_state(
                db,
                session,
                control_state="offline",
                reason="host_offline",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            offline = current_session_capabilities(db, session)

            assert offline.live_control_available is False
            assert offline.can_queue_next_input is False
            assert offline.can_steer_active_turn is False
            assert offline.host_reattach_available is True

            monkeypatch.setattr(
                "zerg.services.session_current_control.managed_runner_host_state",
                lambda _db, _session: "online",
            )
            _upsert_runtime_state(
                db,
                session,
                phase="running",
                freshness_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            _upsert_control_state(
                db,
                session,
                control_state="offline",
                reason="lease_stale",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            expired = current_session_capabilities(db, session)

            assert expired.live_control_available is False
            assert expired.can_queue_next_input is False
            assert expired.can_steer_active_turn is False
            assert expired.host_reattach_available is True
    finally:
        engine.dispose()


def test_current_session_capabilities_uses_engine_channel_without_runner_metadata(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                session = _seed_agent_session(db, source_runner_id=None, device_id="cinder")
                _upsert_runtime_state(
                    db,
                    session,
                    phase="running",
                    phase_source="codex_bridge",
                    freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                _upsert_control_state(
                    db,
                    session,
                    control_state="offline",
                    reason="missing_from_snapshot",
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )

                without_owner = current_session_capabilities(db, session)
                assert without_owner.live_control_available is False

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_current_control.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                live = current_session_capabilities(db, session, owner_id=7)

                assert live.live_control_available is True
                assert live.can_queue_next_input is True
                assert live.can_steer_active_turn is True
                assert live.host_reattach_available is False
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


def test_session_response_uses_owner_for_engine_channel_capability(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                session = _seed_agent_session(
                    db,
                    source_runner_id=None,
                    source_runner_name=None,
                    device_id="cinder",
                )
                _upsert_runtime_state(
                    db,
                    session,
                    phase="idle",
                    phase_source="codex_bridge",
                    freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                control_overlay = _upsert_control_state(
                    db,
                    session,
                    control_state="offline",
                    reason="missing_from_snapshot",
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_views.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                runtime_overlay = resolve_runtime_overlay(
                    session,
                    last_activity_at=session.started_at,
                    runtime_state_map=load_runtime_state_map(db, [session.id]),
                    now=datetime.now(timezone.utc),
                )

                response = build_session_response(
                    AgentsStore(db),
                    session,
                    last_activity_at=session.started_at,
                    runtime_overlay=runtime_overlay,
                    owner_id=7,
                    control_overlay=control_overlay,
                )

                assert response.runtime_display is not None
                assert response.runtime_display.host_state == "online"
                assert response.capabilities.live_control_available is True
                assert response.capabilities.reply_to_live_session_available is True
                assert response.capabilities.can_queue_next_input is True
                assert response.capabilities.host_reattach_available is False
                assert response.capabilities.display_tone == "success"
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


def test_session_response_keeps_engine_control_live_after_phase_goes_stale(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                current_now = datetime.now(timezone.utc)
                old_signal_at = current_now - timedelta(days=30)
                session = _seed_agent_session(
                    db,
                    source_runner_id=None,
                    source_runner_name=None,
                    device_id="cinder",
                    started_at=current_now - timedelta(days=45),
                )
                _upsert_runtime_state(
                    db,
                    session,
                    phase="running",
                    phase_source="codex_bridge",
                    observed_at=old_signal_at,
                    freshness_expires_at=old_signal_at + timedelta(minutes=10),
                )
                stale_control_overlay = _upsert_control_state(
                    db,
                    session,
                    control_state="offline",
                    reason="missing_from_snapshot",
                    expires_at=old_signal_at,
                )

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_views.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                runtime_overlay = resolve_runtime_overlay(
                    session,
                    last_activity_at=old_signal_at,
                    runtime_state_map=load_runtime_state_map(db, [session.id]),
                    now=current_now,
                )

                response = build_session_response(
                    AgentsStore(db),
                    session,
                    last_activity_at=old_signal_at,
                    runtime_overlay=runtime_overlay,
                    owner_id=7,
                    control_overlay=stale_control_overlay,
                )

                assert response.runtime_display is not None
                assert response.runtime_display.lifecycle == "open"
                assert response.capabilities.live_control_available is True
                assert response.capabilities.composer_enabled is True
                assert response.capabilities.composer_disabled_reason is None
                assert response.capabilities.display_label != "Control offline"
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


def test_session_response_fresh_control_lease_not_phase_age_drives_sendability(tmp_path):
    engine, session_local = _make_db(tmp_path)

    try:
        with session_local() as db:
            current_now = datetime.now(timezone.utc)
            old_signal_at = current_now - timedelta(days=30)
            session = _seed_agent_session(
                db,
                provider="claude",
                managed_transport="claude_channel_bridge",
                source_runner_id=17,
                source_runner_name="cinder",
                device_id="cinder",
                started_at=current_now - timedelta(days=45),
            )
            _upsert_runtime_state(
                db,
                session,
                phase="needs_user",
                phase_source="managed_local_transport",
                observed_at=old_signal_at,
                freshness_expires_at=old_signal_at + timedelta(minutes=10),
            )
            fresh_control_overlay = _upsert_control_state(
                db,
                session,
                control_state="online",
                expires_at=current_now + timedelta(minutes=15),
            )
            runtime_overlay = resolve_runtime_overlay(
                session,
                last_activity_at=old_signal_at,
                runtime_state_map=load_runtime_state_map(db, [session.id]),
                now=current_now,
            )

            response = build_session_response(
                AgentsStore(db),
                session,
                last_activity_at=old_signal_at,
                runtime_overlay=runtime_overlay,
                owner_id=7,
                control_overlay=fresh_control_overlay,
            )

            assert response.runtime_display is not None
            assert response.runtime_display.lifecycle == "open"
            assert response.capabilities.live_control_available is True
            assert response.capabilities.composer_enabled is True
            assert response.capabilities.composer_disabled_reason is None
            assert response.capabilities.display_label != "Control offline"
            assert response.capabilities.display_label != "Closed"
            assert response.capabilities.control_label == "live"
            assert response.capabilities.observe_only is False
            assert response.capabilities.search_only is False
            assert response.capabilities.staleness_reason is None
            assert response.capabilities.can_send_input is True
            assert response.capabilities.can_tail_output is True
    finally:
        engine.dispose()


def test_session_response_imported_session_surfaces_search_only_kernel_bucket(tmp_path):
    """An imported session with no kernel rows projects as search-only.

    Tests the kernel-bucket fields surface on SessionResponse so clients
    don't have to re-derive them from the underlying booleans.
    """

    engine, session_local = _make_db(tmp_path)
    try:
        with session_local() as db:
            session = _seed_agent_session(
                db,
                provider="claude",
                execution_home="unmanaged_local",
                managed_transport=None,
                source_runner_id=None,
                source_runner_name=None,
                seed_kernel_rows=False,
            )
            response = build_session_response(
                AgentsStore(db),
                session,
                last_activity_at=session.started_at,
                runtime_overlay=None,
            )
            assert response.capabilities.control_label == "imported"
            assert response.capabilities.search_only is True
            assert response.capabilities.observe_only is False
            assert response.capabilities.can_send_input is False
            assert response.capabilities.can_tail_output is False
            assert response.capabilities.can_resume is False
            assert response.capabilities.staleness_reason == "imported_only"
    finally:
        engine.dispose()


def test_session_response_stale_control_lease_is_offline_not_closed(tmp_path):
    engine, session_local = _make_db(tmp_path)

    try:
        with session_local() as db:
            current_now = datetime.now(timezone.utc)
            old_signal_at = current_now - timedelta(days=30)
            session = _seed_agent_session(
                db,
                source_runner_id=None,
                source_runner_name=None,
                device_id="cinder",
                started_at=current_now - timedelta(days=45),
            )
            _upsert_runtime_state(
                db,
                session,
                phase="running",
                phase_source="codex_bridge",
                observed_at=old_signal_at,
                freshness_expires_at=old_signal_at + timedelta(minutes=10),
            )
            stale_control_overlay = _upsert_control_state(
                db,
                session,
                control_state="offline",
                reason="lease_stale",
                expires_at=old_signal_at,
            )
            runtime_overlay = resolve_runtime_overlay(
                session,
                last_activity_at=old_signal_at,
                runtime_state_map=load_runtime_state_map(db, [session.id]),
                now=current_now,
            )

            response = build_session_response(
                AgentsStore(db),
                session,
                last_activity_at=old_signal_at,
                runtime_overlay=runtime_overlay,
                owner_id=7,
                control_overlay=stale_control_overlay,
            )

            assert response.runtime_display is not None
            assert response.runtime_display.lifecycle == "open"
            assert response.capabilities.live_control_available is False
            assert response.capabilities.composer_enabled is False
            assert response.capabilities.display_label == "Control offline"
            assert response.capabilities.display_label != "Closed"
    finally:
        engine.dispose()


def test_session_response_projects_unmanaged_control_observation(tmp_path):
    engine, session_local = _make_db(tmp_path)

    try:
        with session_local() as db:
            session = _seed_agent_session(
                db,
                execution_home="unmanaged_local",
                managed_transport=None,
                source_runner_id=None,
                source_runner_name=None,
            )
            _upsert_runtime_state(
                db,
                session,
                phase="idle",
                phase_source="semantic",
                freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            runtime_overlay = resolve_runtime_overlay(
                session,
                last_activity_at=session.started_at,
                runtime_state_map=load_runtime_state_map(db, [session.id]),
                now=datetime.now(timezone.utc),
            )

            response = build_session_response(
                AgentsStore(db),
                session,
                last_activity_at=session.started_at,
                runtime_overlay=runtime_overlay,
                owner_id=None,
            )

            assert response.runtime_display is not None
            assert response.runtime_display.control_path == "unmanaged"
            assert response.capabilities.live_control_available is False
    finally:
        engine.dispose()


def test_current_session_capabilities_do_not_treat_engine_online_as_bridge_attached(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                session = _seed_agent_session(db, source_runner_id=None, device_id="cinder")
                _upsert_runtime_state(
                    db,
                    session,
                    phase="running",
                    phase_source="semantic",
                    freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_current_control.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                capabilities = current_session_capabilities(db, session, owner_id=7)

                assert capabilities.live_control_available is False
                assert capabilities.can_queue_next_input is False
                assert capabilities.can_steer_active_turn is False
                assert capabilities.host_reattach_available is True
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


def test_current_session_capabilities_use_fresh_managed_control_lease_for_codex_semantic_phase(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                session = _seed_agent_session(db, source_runner_id=None, device_id="cinder")
                _upsert_runtime_state(
                    db,
                    session,
                    phase="thinking",
                    phase_source="semantic",
                    freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                _upsert_control_state(
                    db,
                    session,
                    control_state="online",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                )

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_current_control.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                capabilities = current_session_capabilities(db, session, owner_id=7)

                assert capabilities.live_control_available is True
                assert capabilities.can_queue_next_input is True
                assert capabilities.can_steer_active_turn is True
                assert capabilities.host_reattach_available is False
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


def test_session_response_uses_managed_control_lease_when_codex_phase_source_is_semantic(monkeypatch, tmp_path):
    engine, session_local = _make_db(tmp_path)

    async def _run():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        try:
            with session_local() as db:
                session = _seed_agent_session(
                    db,
                    source_runner_id=None,
                    source_runner_name=None,
                    device_id="cinder",
                )
                _upsert_runtime_state(
                    db,
                    session,
                    phase="thinking",
                    phase_source="semantic",
                    freshness_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                control_overlay = _upsert_control_state(
                    db,
                    session,
                    control_state="online",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                )

                def _runner_state_should_not_be_used(_db, _session):
                    raise AssertionError("engine-channel capability should not query runner host state")

                monkeypatch.setattr(
                    "zerg.services.session_views.managed_runner_host_state",
                    _runner_state_should_not_be_used,
                )
                await registry.register(
                    owner_id=7,
                    device_id="cinder",
                    machine_name="cinder",
                    engine_build="abc123",
                    supports=["codex.send", "codex.steer"],
                    websocket=SimpleNamespace(),
                )
                runtime_overlay = resolve_runtime_overlay(
                    session,
                    last_activity_at=session.started_at,
                    runtime_state_map=load_runtime_state_map(db, [session.id]),
                    now=datetime.now(timezone.utc),
                )

                response = build_session_response(
                    AgentsStore(db),
                    session,
                    last_activity_at=session.started_at,
                    runtime_overlay=runtime_overlay,
                    owner_id=7,
                    control_overlay=control_overlay,
                )

                assert response.runtime_display is not None
                assert response.capabilities.live_control_available is True
                assert response.capabilities.can_queue_next_input is True
                assert response.capabilities.can_steer_active_turn is True
                assert response.capabilities.host_reattach_available is False
                assert response.capabilities.display_label != "Control offline"
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()
