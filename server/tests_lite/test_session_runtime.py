"""Tests for runtime event ingestion and materialized runtime state."""

from __future__ import annotations

import pytest

# This suite leans heavily on the pre-kernel ``ManagedSessionControlState``
# table and the legacy ``project_current_session_capabilities_from_facts``
# helper, both of which were removed in the session-identity-kernel cleanup.
# The kernel-projection tests in test_session_runtime_kernel.py cover the
# replacement contract; this file is retired.
pytest.skip(
    "session runtime projection moved to the kernel; legacy tests retired",
    allow_module_level=True,
)

import asyncio
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from tests_lite._capability_test_helper import build_session_capabilities
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import ManagedSessionControlState
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_capabilities import project_current_session_capabilities_from_facts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_pubsub import reset_pubsub_for_test
from zerg.services.session_pubsub import topic_session
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import build_fallback_runtime_view
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime import current_presence_state_for_session
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import managed_codex_liveness_invariant_counts
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_views import build_session_response
from zerg.services.unmanaged_bindings import load_binding_overlay


def _runtime_observations(db, runtime_key: str) -> list[SessionObservation]:
    return (
        db.query(SessionObservation)
        .filter(SessionObservation.runtime_key == runtime_key)
        .filter(SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL)
        .order_by(SessionObservation.id.asc())
        .all()
    )


def _runtime_observation_payload(observation: SessionObservation) -> dict:
    return json.loads(observation.payload_json or "{}")


def _make_db(tmp_path, name="session_runtime.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    started_at: datetime | None = None,
    provider: str = "claude",
) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="test",
        project="runtime",
        started_at=started_at or datetime.now(timezone.utc) - timedelta(minutes=5),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        summary="runtime",
        summary_title="runtime",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _client(factory):
    from zerg.main import api_app

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="runtime-device", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        with TestClient(api_app) as client:
            yield client
    finally:
        api_app.dependency_overrides.clear()


def test_runtime_reducer_materializes_phase_progress_and_terminal(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_reducer.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="running",
                    tool_name="bash",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=phase_freshness_ms("running"),
                    dedupe_key="phase-1",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="progress-1",
                    payload={"progress_kind": "tool_result"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="terminal-1",
                    payload={
                        "terminal_state": "finished",
                        "terminal_reason": "provider_exit",
                        "terminal_source": "claude_hook",
                    },
                ),
            ],
        )
        db.commit()

        assert result.accepted == 3
        assert result.duplicates == 0
        assert result.updated_runtime_keys == [runtime_key]

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert str(state.session_id) == str(session.id)
        assert state.phase == "finished"
        assert state.terminal_state == "finished"
        assert state.terminal_reason == "provider_exit"
        assert state.terminal_source == "claude_hook"
        assert state.active_tool is None
        assert state.last_progress_at is not None
        assert state.timeline_anchor_at is not None
        assert int(state.runtime_version) >= 3

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.status == "completed"
        assert view.display_phase == "Completed"
        assert view.confidence == "stale"
        assert view.runtime_phase == "finished"
        assert view.runtime_source == "semantic"
        assert view.terminal_reason == "provider_exit"
        assert view.terminal_source == "claude_hook"

    engine.dispose()


def test_transcript_progress_clears_blocked_attention_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_progress_clears_blocked.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1), provider="claude")
        runtime_key = runtime_key_for_session("claude", str(session.id))

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=phase_freshness_ms("blocked"),
                    dedupe_key="blocked-ask-user",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="agents_ingest",
                    kind="progress_signal",
                    occurred_at=now,
                    dedupe_key="ask-user-answer-progress",
                    payload={"progress_kind": "transcript_append"},
                ),
            ],
        )
        db.commit()

        assert result.accepted == 2
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.active_tool is None
        assert state.freshness_expires_at is None
        assert state.last_progress_at.replace(tzinfo=timezone.utc) == now

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.presence_state is None
        assert view.active_tool is None
        assert view.signal_tier == "transcript_progress"
        assert view.status == "idle"
        assert view.display_phase == "Inactive"

    engine.dispose()


def test_older_transcript_progress_does_not_clear_newer_blocked_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_progress_keeps_newer_blocked.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1), provider="claude")
        runtime_key = runtime_key_for_session("claude", str(session.id))

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=now,
                    freshness_ms=phase_freshness_ms("blocked"),
                    dedupe_key="newer-blocked-ask-user",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="agents_ingest",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=30),
                    dedupe_key="older-progress",
                    payload={"progress_kind": "transcript_append"},
                ),
            ],
        )
        db.commit()

        assert result.accepted == 2
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "blocked"
        assert state.phase_source == "semantic"
        assert state.active_tool == "AskUserQuestion"
        assert state.last_progress_at is None

    engine.dispose()


def test_managed_session_ended_terminal_overrides_nearby_newer_lease(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_session_ended_after_lease_race.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(minutes=20),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        runtime_key = runtime_key_for_session("codex", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="control path",
                    occurred_at=now,
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="lease-newer",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(milliseconds=50),
                    dedupe_key="terminal-slightly-older",
                    payload={
                        "terminal_state": "session_ended",
                        "terminal_reason": "bridge_stop",
                        "terminal_source": "codex_bridge",
                    },
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        view = build_runtime_view(state=state, session=stored_session, now=now)

        assert state.phase == "finished"
        assert state.terminal_state == "session_ended"
        assert state.terminal_reason == "bridge_stop"
        assert stored_session.ended_at.replace(tzinfo=None) == (now - timedelta(milliseconds=50)).replace(tzinfo=None)
        assert view.status == "completed"

    engine.dispose()


def test_managed_claude_wrapper_session_ended_wins_over_scan_process_gone(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_managed_claude_terminal_precedence.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        scan_first = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=20))
        scan_first.execution_home = "managed_local"
        scan_first.managed_transport = "claude_channel_bridge"
        scan_first_key = runtime_key_for_session("claude", str(scan_first.id))

        wrapper_first = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=20))
        wrapper_first.execution_home = "managed_local"
        wrapper_first.managed_transport = "claude_channel_bridge"
        wrapper_first_key = runtime_key_for_session("claude", str(wrapper_first.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=scan_first_key,
                    session_id=scan_first.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_channel_scan",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="scan-first-process-gone",
                    payload={
                        "terminal_state": "process_gone",
                        "terminal_reason": "channel_state_gone",
                        "terminal_source": "claude_channel_scan",
                    },
                ),
                RuntimeEventIngest(
                    runtime_key=scan_first_key,
                    session_id=scan_first.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_channel_wrapper",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(milliseconds=500),
                    dedupe_key="scan-first-wrapper-ended",
                    payload={
                        "terminal_state": "session_ended",
                        "terminal_reason": "provider_exit",
                        "terminal_source": "claude_channel_wrapper",
                    },
                ),
                RuntimeEventIngest(
                    runtime_key=wrapper_first_key,
                    session_id=wrapper_first.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_channel_wrapper",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="wrapper-first-ended",
                    payload={
                        "terminal_state": "session_ended",
                        "terminal_reason": "provider_exit",
                        "terminal_source": "claude_channel_wrapper",
                    },
                ),
                RuntimeEventIngest(
                    runtime_key=wrapper_first_key,
                    session_id=wrapper_first.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_channel_scan",
                    kind="terminal_signal",
                    occurred_at=now + timedelta(milliseconds=500),
                    dedupe_key="wrapper-first-process-gone",
                    payload={
                        "terminal_state": "process_gone",
                        "terminal_reason": "channel_state_gone",
                        "terminal_source": "claude_channel_scan",
                    },
                ),
            ],
        )
        db.commit()

        for runtime_key in (scan_first_key, wrapper_first_key):
            state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
            assert state.phase == "finished"
            assert state.terminal_state == "session_ended"
            assert state.terminal_reason == "provider_exit"
            assert state.terminal_source == "claude_channel_wrapper"

    engine.dispose()


def test_runtime_reducer_preserves_terminal_disconnected_reason(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_terminal_disconnected.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1), provider="codex")
        runtime_key = runtime_key_for_session("codex", str(session.id))

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="work-laptop",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="terminal-disconnected-1",
                    payload={
                        "terminal_state": "session_ended",
                        "terminal_reason": "terminal_disconnected",
                        "terminal_source": "codex_bridge",
                    },
                ),
            ],
        )
        db.commit()

        assert result.accepted == 1
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.terminal_state == "session_ended"
        assert state.terminal_reason == "terminal_disconnected"

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.status == "completed"
        assert view.terminal_reason == "terminal_disconnected"

    engine.dispose()


def test_bridge_transcript_event_stores_latest_live_overlay_observation(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "bridge_transcript_preview.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(minutes=1))
        runtime_key = runtime_key_for_session("codex", str(session.id))
        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge_live",
                    kind="progress_signal",
                    occurred_at=now,
                    dedupe_key="bridge:live:s:t:turn:1",
                    payload={
                        "progress_kind": "bridge_live_transcript_delta",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "seq": 1,
                        "method": "item/agentMessage/delta",
                        "delta": "hel",
                        "live_text": "hel",
                    },
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge_live",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="bridge:live:s:t:turn:2",
                    payload={
                        "progress_kind": "bridge_live_transcript_delta",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "seq": 2,
                        "method": "item/agentMessage/delta",
                        "delta": "lo",
                        "live_text": "hello",
                        "turn_completed": True,
                    },
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge_live",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="bridge:live:s:t:turn:2",
                    payload={
                        "progress_kind": "bridge_live_transcript_delta",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "seq": 2,
                        "method": "item/agentMessage/delta",
                        "delta": "lo",
                        "live_text": "hello",
                        "turn_completed": True,
                    },
                ),
            ],
        )

        events = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()
        observations = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session.id)
            .order_by(SessionObservation.id.asc())
            .all()
        )
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first()

    assert result.accepted == 2
    assert result.duplicates == 1
    assert result.updated_runtime_keys == [runtime_key]
    assert state is None
    assert events == []
    assert len(observations) == 2
    assert preview.text == "hello"
    assert preview.event_origin == "live_provisional"
    assert preview.provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:2"
    assert preview.provisional_complete is True
    engine.dispose()


def test_managed_codex_bridge_signal_does_not_shorten_attached_lease_freshness(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_managed_codex_bridge_freshness.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(hours=1))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        runtime_key = runtime_key_for_session("codex", str(session.id))
        lease_freshness_ms = 15 * 60 * 1000

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now,
                    freshness_ms=lease_freshness_ms,
                    dedupe_key="lease-1",
                    payload={"state": "attached"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now + timedelta(seconds=30),
                    dedupe_key="bridge-thinking-1",
                    payload={"managed_transport": "codex_app_server"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "thinking"
        assert state.last_runtime_signal_at is not None
        assert state.last_runtime_signal_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=30)
        assert state.freshness_expires_at is not None
        assert state.freshness_expires_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=30, minutes=15)
        assert managed_codex_liveness_invariant_counts(db) == {
            "ended_without_session_ended": 0,
            "short_freshness": 0,
        }

    engine.dispose()


def test_managed_codex_liveness_invariant_counts_surface_parser_end_and_short_freshness(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_managed_codex_liveness_invariants.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        parser_ended = _seed_session(db, provider="codex", started_at=now - timedelta(hours=2))
        parser_ended.execution_home = "managed_local"
        parser_ended.managed_transport = "codex_app_server"
        parser_ended.ended_at = now - timedelta(hours=1)

        non_managed_ended = _seed_session(db, provider="codex", started_at=now - timedelta(hours=2))
        non_managed_ended.ended_at = now - timedelta(hours=1)

        short_freshness = _seed_session(db, provider="codex", started_at=now - timedelta(hours=2))
        short_freshness.execution_home = "managed_local"
        short_freshness.managed_transport = "codex_app_server"
        short_runtime_key = runtime_key_for_session("codex", str(short_freshness.id))
        generic_short_freshness = _seed_session(db, provider="codex", started_at=now - timedelta(hours=2))
        generic_short_freshness.execution_home = "managed_local"
        generic_short_freshness.managed_transport = "codex_app_server"
        generic_runtime_key = runtime_key_for_session("codex", str(generic_short_freshness.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=short_runtime_key,
                    session_id=short_freshness.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now,
                    freshness_ms=90 * 1000,
                    dedupe_key="short-managed-freshness",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=generic_runtime_key,
                    session_id=generic_short_freshness.id,
                    provider="codex",
                    device_id="cinder",
                    source="generic_codex_hint",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now,
                    freshness_ms=90 * 1000,
                    dedupe_key="short-generic-freshness",
                    payload={},
                ),
            ],
        )
        db.commit()

        assert managed_codex_liveness_invariant_counts(db) == {
            "ended_without_session_ended": 1,
            "short_freshness": 1,
        }

        runtime_key = runtime_key_for_session("codex", str(parser_ended.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=parser_ended.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="explicit-session-ended",
                    payload={"terminal_state": "session_ended"},
                )
            ],
        )
        db.commit()

        assert managed_codex_liveness_invariant_counts(db) == {
            "ended_without_session_ended": 0,
            "short_freshness": 1,
        }

    engine.dispose()


def test_runtime_batch_endpoint_is_idempotent(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_endpoint.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

    for client in _client(SessionLocal):
        payload = {
            "events": [
                {
                    "runtime_key": runtime_key,
                    "session_id": str(session.id),
                    "provider": "claude",
                    "device_id": "cinder",
                    "source": "claude_hook",
                    "kind": "phase_signal",
                    "phase": "thinking",
                    "occurred_at": (now - timedelta(seconds=10)).isoformat(),
                    "freshness_ms": phase_freshness_ms("thinking"),
                    "dedupe_key": "dup-1",
                    "payload": {},
                }
            ]
        }

        first = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert first.status_code == 200, first.text
        assert first.json() == {
            "accepted": 1,
            "duplicates": 0,
            "updated_runtime_keys": [runtime_key],
        }

        second = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert second.status_code == 200, second.text
        assert second.json() == {
            "accepted": 0,
            "duplicates": 1,
            "updated_runtime_keys": [],
        }

    with SessionLocal() as db:
        assert len(_runtime_observations(db, runtime_key)) == 1
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert str(state.session_id) == str(session.id)
        assert state.phase == "thinking"
        assert int(state.runtime_version) == 1

    engine.dispose()


def test_runtime_batch_duplicate_skips_push_prep(tmp_path, monkeypatch):
    engine, SessionLocal = _make_db(tmp_path, "runtime_duplicate_fast_path.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

    from zerg.routers import runtime as runtime_router

    call_counts = {
        "targets": 0,
        "widget": 0,
        "runtime_map": 0,
    }

    def count(name):
        def _inner(*args, **kwargs):
            call_counts[name] += 1
            if name == "runtime_map":
                return {}
            return None

        return _inner

    monkeypatch.setattr(runtime_router, "active_ios_targets_for_owner", count("targets"))
    monkeypatch.setattr(runtime_router, "prepare_widget_timeline_push", count("widget"))
    monkeypatch.setattr(runtime_router, "load_runtime_state_map", count("runtime_map"))

    payload = {
        "events": [
            {
                "runtime_key": runtime_key,
                "session_id": str(session.id),
                "provider": "claude",
                "device_id": "cinder",
                "source": "claude_hook",
                "kind": "phase_signal",
                "phase": "thinking",
                "occurred_at": (now - timedelta(seconds=10)).isoformat(),
                "freshness_ms": phase_freshness_ms("thinking"),
                "dedupe_key": "duplicate-fast-path-1",
                "payload": {},
            }
        ]
    }

    for client in _client(SessionLocal):
        first = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert first.status_code == 200, first.text
        assert first.json()["updated_runtime_keys"] == [runtime_key]

        call_counts.update({"targets": 0, "widget": 0, "runtime_map": 0})
        second = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert second.status_code == 200, second.text
        assert second.json()["updated_runtime_keys"] == []

    assert call_counts == {
        "targets": 0,
        "widget": 0,
        "runtime_map": 0,
    }

    engine.dispose()


def test_runtime_batch_endpoint_wakes_subscribers_for_applied_event(tmp_path):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_endpoint_pubsub.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

    for client in _client(SessionLocal):
        payload = {
            "events": [
                {
                    "runtime_key": runtime_key,
                    "session_id": str(session.id),
                    "provider": "claude",
                    "device_id": "cinder",
                    "source": "claude_hook",
                    "kind": "phase_signal",
                    "phase": "thinking",
                    "occurred_at": now.isoformat(),
                    "freshness_ms": phase_freshness_ms("thinking"),
                    "dedupe_key": "pubsub-1",
                    "payload": {},
                }
            ]
        }

        first = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert first.status_code == 200, first.text
        assert first.json()["updated_runtime_keys"] == [runtime_key]

        bus = get_pubsub()
        with bus.subscribe(topic_session(str(session.id)), since_seq=0) as session_sub:
            msg = asyncio.run(session_sub.next_message(timeout=0.1))
            assert msg is not None
            assert msg.payload["kind"] == "runtime"
            assert msg.payload["session_id"] == str(session.id)
            assert msg.payload["provider"] == "claude"
            assert msg.payload["source"] == "claude_hook"
            assert isinstance(msg.payload.get("server_fanout_at_ms"), int)
        timeline_seq = bus.peek_latest_seq(TOPIC_TIMELINE)
        assert timeline_seq == 1

        second = client.post("/agents/runtime/events/batch", json=payload, headers={"X-Agents-Token": "dev"})
        assert second.status_code == 200, second.text
        assert second.json()["updated_runtime_keys"] == []
        assert bus.peek_latest_seq(TOPIC_TIMELINE) == timeline_seq

    reset_pubsub_for_test()
    engine.dispose()


def test_opencode_runtime_events_materialize_live_and_terminal_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_opencode_endpoint.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="opencode", started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("opencode", str(session.id))

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    {
                        "runtime_key": runtime_key,
                        "session_id": str(session.id),
                        "provider": "opencode",
                        "device_id": "work-laptop",
                        "source": "opencode_event",
                        "kind": "phase_signal",
                        "phase": "running",
                        "tool_name": "bash",
                        "occurred_at": (now - timedelta(seconds=10)).isoformat(),
                        "dedupe_key": "opencode-running-1",
                        "payload": {"opencodeStatus": {"type": "busy"}},
                    },
                    {
                        "runtime_key": runtime_key,
                        "session_id": str(session.id),
                        "provider": "opencode",
                        "device_id": "work-laptop",
                        "source": "opencode_event",
                        "kind": "terminal_signal",
                        "phase": "finished",
                        "occurred_at": now.isoformat(),
                        "dedupe_key": "opencode-terminal-1",
                        "payload": {"terminal_state": "session_ended", "exit_code": 0},
                    },
                ]
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "accepted": 2,
            "duplicates": 0,
            "updated_runtime_keys": [runtime_key],
        }

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        assert state.provider == "opencode"
        assert state.phase == "finished"
        assert state.terminal_state == "session_ended"
        assert state.active_tool is None
        assert stored_session.ended_at is not None
        assert stored_session.ended_at.replace(tzinfo=timezone.utc) == now

    engine.dispose()


def test_presence_endpoint_mirrors_into_runtime_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_presence_mirror.db")
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    runtime_key = runtime_key_for_session("claude", str(session_id))

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/presence",
            json={
                "session_id": str(session_id),
                "state": "blocked",
                "tool_name": "Bash",
                "cwd": "/tmp/runtime",
                "provider": "claude",
                "occurred_at": now.isoformat(),
                "dedupe_key": "presence-blocked-1",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert str(state.session_id) == str(session_id)
        assert state.phase == "blocked"
        assert state.active_tool == "Bash"
        assert state.last_runtime_signal_at is not None
        assert state.freshness_expires_at is not None
        assert len(_runtime_observations(db, runtime_key)) == 1

    engine.dispose()


def test_presence_endpoint_uses_provider_source_and_wakes_subscribers(tmp_path):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_presence_codex_source.db")
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    runtime_key = runtime_key_for_session("codex", str(session_id))

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/presence",
            json={
                "session_id": str(session_id),
                "state": "thinking",
                "provider": "codex",
                "occurred_at": now.isoformat(),
                "dedupe_key": "codex-presence-thinking-1",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        observation = _runtime_observations(db, runtime_key)[0]
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert observation.provider == "codex"
        assert observation.source == "codex_hook"
        assert state.provider == "codex"
        assert state.phase == "thinking"

    bus = get_pubsub()
    with bus.subscribe(topic_session(str(session_id)), since_seq=0) as session_sub:
        msg = asyncio.run(session_sub.next_message(timeout=0.1))
        assert msg is not None
        assert msg.payload["kind"] == "runtime"
        assert msg.payload["session_id"] == str(session_id)
        assert msg.payload["provider"] == "codex"
        assert msg.payload["source"] == "codex_hook"
        assert isinstance(msg.payload.get("server_fanout_at_ms"), int)
    with bus.subscribe(TOPIC_TIMELINE, since_seq=0) as timeline_sub:
        msg = asyncio.run(timeline_sub.next_message(timeout=0.1))
        assert msg is not None
        assert msg.payload["kind"] == "runtime"
        assert msg.payload["session_id"] == str(session_id)
        assert msg.payload["provider"] == "codex"
        assert msg.payload["source"] == "codex_hook"

    reset_pubsub_for_test()


def test_runtime_event_ingest_stamps_received_at_in_python(tmp_path, monkeypatch):
    from zerg.services import session_runtime as runtime_module

    engine, SessionLocal = _make_db(tmp_path, "runtime_received_at_precision.db")
    received_at = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return received_at.replace(tzinfo=None)
            return received_at.astimezone(tz)

    monkeypatch.setattr(runtime_module, "datetime", FrozenDateTime)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=received_at - timedelta(minutes=5))
        runtime_key = runtime_key_for_session("codex", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="progress_signal",
                    occurred_at=received_at - timedelta(milliseconds=250),
                    dedupe_key="progress-with-subsecond-received-at",
                    payload={"progress_kind": "assistant_message"},
                )
            ],
        )
        db.commit()

        observation = _runtime_observations(db, runtime_key)[0]
        assert observation.received_at.replace(tzinfo=timezone.utc) == received_at

    engine.dispose()


def test_current_presence_state_for_session_uses_runtime_overlay(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_current_presence_state.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=10))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="bash",
                    occurred_at=now - timedelta(seconds=5),
                    freshness_ms=phase_freshness_ms("blocked"),
                    dedupe_key="blocked-live",
                    payload={},
                )
            ],
        )
        db.commit()

        assert current_presence_state_for_session(db, session.id, session=session, now=now) == "blocked"

    engine.dispose()


def test_heartbeat_attached_managed_codex_lease_materializes_control_without_runtime_phase(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_codex_lease.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=3),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        session.ended_at = now - timedelta(hours=2)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        db.commit()
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))

    before_request = datetime.now(timezone.utc)
    observed_at = before_request
    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 42,
                        "state": "attached",
                        "phase": "idle",
                        "tool_name": None,
                        "bridge_status": "ready",
                        "thread_subscription_status": "subscribed",
                        "observed_at": observed_at.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text
    after_request = datetime.now(timezone.utc)

    with SessionLocal() as db:
        assert db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first() is None
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()

        assert control.control_state == "online"
        assert control.reason is None
        assert control.source == "machine_heartbeat"
        assert control.lease_observed_at.replace(tzinfo=timezone.utc) == observed_at
        assert before_request <= control.last_control_seen_at.replace(tzinfo=timezone.utc) <= after_request
        assert control.control_expires_at.replace(tzinfo=timezone.utc) >= before_request + timedelta(minutes=15)
        assert stored_session.ended_at is None

        response = build_session_response(
            AgentsStore(db),
            stored_session,
            last_activity_at=stored_session.last_activity_at,
            runtime_overlay=None,
            owner_id=None,
            control_overlay=control,
        )
        assert response.runtime_display is None
        assert response.capabilities.live_control_available is True

    engine.dispose()


def test_heartbeat_lease_uses_observed_at_so_stale_lease_cannot_override_newer_hook(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_stale_lease_ordering.db")
    now = datetime.now(timezone.utc)
    stale_lease_at = now - timedelta(seconds=60)
    hook_at = now - timedelta(seconds=5)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "claude_relay"
        session_id = session.id
        runtime_key = runtime_key_for_session("claude", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="claude",
                    device_id="runtime-device",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=hook_at,
                    freshness_ms=phase_freshness_ms("thinking"),
                    dedupe_key="fresh-hook-thinking",
                    payload={},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 44,
                        "state": "attached",
                        "phase": "blocked",
                        "tool_name": "AskUserQuestion",
                        "bridge_status": "ready",
                        "thread_subscription_status": "subscribed",
                        "observed_at": stale_lease_at.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text
    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        observations = _runtime_observations(db, runtime_key)
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()

        assert state.phase == "thinking"
        assert state.active_tool is None
        assert state.last_runtime_signal_at.replace(tzinfo=timezone.utc) == hook_at
        assert len(observations) == 1
        assert observations[0].observed_at.replace(tzinfo=timezone.utc) == hook_at
        assert control.control_state == "online"
        assert control.lease_observed_at.replace(tzinfo=timezone.utc) == stale_lease_at

    engine.dispose()


def test_heartbeat_lease_refreshes_same_phase_even_when_provider_phase_timestamp_is_old(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_stale_same_phase_lease.db")
    now = datetime.now(timezone.utc)
    stale_phase_at = now - timedelta(minutes=11)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        session_id = session.id
        runtime_key = runtime_key_for_session("claude", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="claude",
                    device_id="runtime-device",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="needs_user",
                    occurred_at=stale_phase_at,
                    freshness_ms=phase_freshness_ms("needs_user"),
                    dedupe_key="old-needs-user",
                    payload={},
                )
            ],
        )
        db.commit()

    before_request = datetime.now(timezone.utc)
    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 45,
                        "state": "attached",
                        "phase": "needs_user",
                        "tool_name": None,
                        "bridge_status": "ready",
                        "thread_subscription_status": None,
                        "observed_at": stale_phase_at.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text
    after_request = datetime.now(timezone.utc)

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=after_request)
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        capabilities = build_session_capabilities(stored_session)
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=capabilities,
            last_activity_at=stored_session.last_activity_at,
            binding_host_state="online",
            control_overlay=control,
            now=after_request,
        )
        projected = project_current_session_capabilities_from_facts(
            capabilities,
            liveness_facts=facts,
            now=after_request,
        )

        assert state.phase == "needs_user"
        assert state.last_runtime_signal_at.replace(tzinfo=timezone.utc) == stale_phase_at
        assert state.last_live_at is not None
        assert state.last_live_at.replace(tzinfo=timezone.utc) == stale_phase_at
        assert state.freshness_expires_at is not None
        assert state.freshness_expires_at.replace(tzinfo=timezone.utc) < before_request
        assert control.control_state == "online"
        assert before_request <= control.last_control_seen_at.replace(tzinfo=timezone.utc) <= after_request
        assert control.control_expires_at.replace(tzinfo=timezone.utc) >= before_request + timedelta(minutes=15)
        assert facts.phase.kind is None
        assert facts.control.state == "online"
        assert projected.live_control_available is True

    engine.dispose()


def test_heartbeat_lease_refresh_after_progress_restores_control_projection(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_progress_projection.db")
    now = datetime.now(timezone.utc)
    progress_at = now - timedelta(minutes=1)
    stale_phase_at = now - timedelta(minutes=11)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        session_id = session.id
        runtime_key = runtime_key_for_session("claude", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="claude",
                    device_id="runtime-device",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=progress_at,
                    dedupe_key="progress-before-lease",
                    payload={"progress_kind": "assistant_message"},
                )
            ],
        )
        db.commit()

    before_request = datetime.now(timezone.utc)
    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 46,
                        "state": "attached",
                        "phase": "idle",
                        "tool_name": None,
                        "bridge_status": "ready",
                        "thread_subscription_status": None,
                        "observed_at": stale_phase_at.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text
    after_request = datetime.now(timezone.utc)

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=after_request)
        capabilities = build_session_capabilities(stored_session)
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=capabilities,
            last_activity_at=stored_session.last_activity_at,
            binding_host_state="online",
            control_overlay=control,
            now=after_request,
        )
        projected = project_current_session_capabilities_from_facts(
            capabilities,
            liveness_facts=facts,
            now=after_request,
        )

        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.last_runtime_signal_at is None
        assert state.last_live_at is None
        assert control.control_state == "online"
        assert before_request <= control.last_control_seen_at.replace(tzinfo=timezone.utc) <= after_request
        assert control.control_expires_at.replace(tzinfo=timezone.utc) >= before_request + timedelta(minutes=15)
        assert view.runtime_source == "progress"
        assert view.runtime_phase is None
        assert facts.phase.kind is None
        assert facts.control.state == "online"
        assert facts.control.source == "machine_heartbeat"
        assert projected.live_control_available is True
        assert projected.host_reattach_available is False

    engine.dispose()


def test_heartbeat_managed_lease_writes_control_state_without_phase_dependency(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_control_state.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="claude_channel_bridge")
        session_id = session.id
        db.commit()

    before_request = datetime.now(timezone.utc)
    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 46,
                        "state": "attached",
                        "phase": None,
                        "tool_name": None,
                        "bridge_status": "ready",
                        "thread_subscription_status": None,
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text
    after_request = datetime.now(timezone.utc)

    with SessionLocal() as db:
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).first() is None
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        assert control.control_state == "online"
        assert control.reason is None
        assert control.source == "machine_heartbeat"
        assert before_request <= control.last_control_seen_at.replace(tzinfo=timezone.utc) <= after_request
        assert control.control_expires_at.replace(tzinfo=timezone.utc) > after_request

        fallback_view = build_fallback_runtime_view(
            session=stored_session,
            last_activity_at=stored_session.last_activity_at,
            now=after_request,
        )
        facts = build_session_liveness_facts(
            runtime_view=fallback_view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
            binding_host_state="online",
            control_overlay=load_managed_control_state_map(db, [session_id]).get(session_id),
            now=after_request,
        )
        projected = project_current_session_capabilities_from_facts(
            build_session_capabilities(stored_session),
            liveness_facts=facts,
            now=after_request,
        )

        assert facts.phase.kind is None
        assert facts.control.state == "online"
        assert projected.live_control_available is True
        response = build_session_response(
            AgentsStore(db),
            stored_session,
            last_activity_at=stored_session.last_activity_at,
            runtime_overlay=None,
            owner_id=None,
            control_overlay=control,
        )
        assert response.runtime_display is None
        assert response.capabilities.live_control_available is True

    engine.dispose()


def test_heartbeat_attached_lease_with_bad_bridge_status_is_not_control_online(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_control_bridge_down.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(hours=1))
        session.execution_home = "managed_local"
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 17
        session_id = session.id
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 46,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "bridge_down",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        control = load_managed_control_state_map(db, [session_id])[session_id]
        facts = build_session_liveness_facts(
            runtime_view=build_fallback_runtime_view(
                session=stored_session,
                last_activity_at=stored_session.last_activity_at,
                now=now,
            ),
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
            binding_host_state="online",
            control_overlay=control,
            now=now,
        )
        projected = project_current_session_capabilities_from_facts(
            build_session_capabilities(stored_session),
            liveness_facts=facts,
            now=now,
        )

        assert facts.control.state == "degraded"
        assert facts.control.reason == "bridge_unavailable"
        assert projected.live_control_available is False
        assert projected.host_reattach_available is True

    engine.dispose()


def test_empty_managed_snapshot_marks_only_same_device_control_offline(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_control_missing_snapshot.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        first = _seed_session(db, provider="claude", started_at=now - timedelta(hours=1))
        first.execution_home = "managed_local"
        first.managed_transport = "claude_channel_bridge"
        second = _seed_session(db, provider="claude", started_at=now - timedelta(hours=1))
        second.execution_home = "managed_local"
        second.managed_transport = "claude_channel_bridge"
        db.add(
            ManagedSessionControlState(
                session_id=first.id,
                provider="claude",
                device_id="runtime-device",
                machine_id="cinder",
                transport="claude_channel_bridge",
                lease_state="attached",
                control_state="online",
                source="machine_heartbeat",
                last_control_seen_at=now,
                lease_observed_at=now,
                lease_ttl_ms=900_000,
                control_expires_at=now + timedelta(minutes=15),
            )
        )
        db.add(
            ManagedSessionControlState(
                session_id=second.id,
                provider="claude",
                device_id="other-device",
                machine_id="demo-machine",
                transport="claude_channel_bridge",
                lease_state="attached",
                control_state="online",
                source="machine_heartbeat",
                last_control_seen_at=now,
                lease_observed_at=now,
                lease_ttl_ms=900_000,
                control_expires_at=now + timedelta(minutes=15),
            )
        )
        first_id = first.id
        second_id = second.id
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        rows = load_managed_control_state_map(db, [first_id, second_id])
        assert rows[first_id].control_state == "offline"
        assert rows[first_id].reason == "missing_from_snapshot"
        assert rows[second_id].control_state == "online"
        assert rows[second_id].reason is None

    engine.dispose()


def test_heartbeat_lease_does_not_resurrect_session_ended_managed_codex_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_codex_session_ended.db")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(minutes=5)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=ended_at,
                    dedupe_key="session-ended-before-lease",
                    payload={"terminal_state": "session_ended"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 44,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "ready",
                        "thread_subscription_status": "subscribed",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))

        assert state.phase == "finished"
        assert state.terminal_state == "session_ended"
        assert state.freshness_expires_at == state.terminal_at
        assert stored_session.ended_at is not None
        assert stored_session.ended_at.replace(tzinfo=timezone.utc) == ended_at
        assert view.status == "completed"
        assert view.display_phase == "Completed"

    engine.dispose()


def test_heartbeat_reattach_does_not_clear_explicit_session_ended_runtime_terminal(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_session_ended_survives_reattach.db")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(minutes=5)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=1),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="terminal_signal",
                    occurred_at=ended_at,
                    dedupe_key="explicit-session-ended-before-reattach",
                    payload={"terminal_state": "session_ended"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 45,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "ready",
                        "thread_subscription_status": "subscribed",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        response = build_session_response(
            AgentsStore(db),
            stored_session,
            last_activity_at=stored_session.last_activity_at,
            runtime_overlay=view,
            owner_id=None,
            control_overlay=control,
        )

        assert state.phase == "finished"
        assert state.terminal_state == "session_ended"
        assert state.terminal_source == "engine_attached_lease"
        assert stored_session.ended_at is not None
        assert stored_session.ended_at.replace(tzinfo=timezone.utc) == ended_at
        assert response.capabilities.live_control_available is False

    engine.dispose()


def test_heartbeat_detached_managed_codex_lease_is_recoverable_control_loss(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_codex_detached.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=3),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge", state="detached")
        db.commit()
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 43,
                        "state": "detached",
                        "phase": None,
                        "bridge_status": "ready",
                        "thread_subscription_status": "subscribed",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        assert db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first() is None
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        response = build_session_response(
            AgentsStore(db),
            stored_session,
            last_activity_at=stored_session.last_activity_at,
            runtime_overlay=None,
            owner_id=None,
            control_overlay=control,
        )

        assert control.control_state == "offline"
        assert control.reason == "detached"
        assert response.runtime_display is None
        assert response.capabilities.live_control_available is False
        assert response.capabilities.host_reattach_available is True

    engine.dispose()


def test_phase_signal_same_blocked_phase_with_new_tool_resets_phase_started_at(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_blocked_tool_reanchors.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("codex", str(session.id))
        first_at = now - timedelta(minutes=10)
        second_at = now - timedelta(minutes=2)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="control path",
                    occurred_at=first_at,
                    dedupe_key="blocked-control-path",
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="bash",
                    occurred_at=second_at,
                    dedupe_key="blocked-bash",
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "blocked"
        assert state.active_tool == "bash"
        assert state.phase_started_at.replace(tzinfo=timezone.utc) == second_at

    engine.dispose()


def test_heartbeat_empty_unmanaged_snapshot_closes_stale_unbound_codex_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_unmanaged_unbound_missing.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(minutes=20))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(minutes=10)
        runtime_key = runtime_key_for_session("codex", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="codex_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=10),
                    freshness_ms=90 * 1000,
                    dedupe_key="unbound-codex-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
        )

        assert state.phase == "finished"
        assert state.terminal_state == "process_gone"
        assert view.status == "completed"
        assert facts.control_path == "unmanaged"
        assert facts.lifecycle.state == "closed"
        assert facts.lifecycle.reason == "process_gone"
        assert facts.process_state == "closed"

    engine.dispose()


def test_heartbeat_omitted_unmanaged_snapshot_does_not_close_stale_unbound_codex_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_unmanaged_snapshot_omitted.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(minutes=20))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(minutes=10)
        runtime_key = runtime_key_for_session("codex", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="codex_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=10),
                    freshness_ms=90 * 1000,
                    dedupe_key="unbound-codex-phase-omitted-snapshot",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "legacy-engine",
                "daemon_pid": 123,
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        assert state.phase == "thinking"
        assert state.terminal_state is None

    engine.dispose()


def test_heartbeat_empty_unmanaged_snapshot_keeps_fresh_unbound_codex_session_open(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_unmanaged_unbound_fresh.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(minutes=5))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(seconds=20)
        runtime_key = runtime_key_for_session("codex", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="codex_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(seconds=20),
                    freshness_ms=90 * 1000,
                    dedupe_key="fresh-unbound-codex-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        assert state.phase == "thinking"
        assert state.terminal_state is None

    engine.dispose()


def test_heartbeat_claude_binding_marks_process_running(tmp_path):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_claude_process_binding.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(seconds=30)
        runtime_key = runtime_key_for_session("claude", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="runtime-device",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=90 * 1000,
                    dedupe_key="claude-binding-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "claude",
                        "provider_session_id": str(session_id),
                        "source_path": f"/tmp/{session_id}.jsonl",
                        "pid": 69257,
                        "process_start_time": (now - timedelta(minutes=3)).isoformat(),
                        "cwd": "/tmp/runtime",
                        "source_mtime": (now - timedelta(seconds=5)).isoformat(),
                        "observed_at": now.isoformat(),
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    bus = get_pubsub()
    with bus.subscribe(topic_session(str(session_id)), since_seq=0) as session_sub:
        msg = asyncio.run(session_sub.next_message(timeout=0.1))
        assert msg is not None
        assert msg.payload["kind"] == "runtime"
        assert msg.payload["session_id"] == str(session_id)
        assert msg.payload["source"] == "engine_process_snapshot"

    with SessionLocal() as db:
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        overlay = load_binding_overlay(db, [session_id], now=now)[session_id]
        view = build_runtime_view(state=state, session=stored_session, now=now)
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
            binding_overlay=overlay,
        )

        assert facts.control_path == "unmanaged"
        assert facts.process_state == "running"
        assert facts.process.pid == 69257
        assert facts.lifecycle.state == "open"
        assert facts.lifecycle.reason == "process_observed"

    reset_pubsub_for_test()
    engine.dispose()


def test_heartbeat_repeat_unmanaged_binding_does_not_republish(tmp_path):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_claude_process_binding_noop.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session_id = session.id
        db.commit()

    binding = {
        "machine_id": "cinder",
        "provider": "claude",
        "provider_session_id": str(session_id),
        "source_path": f"/tmp/{session_id}.jsonl",
        "pid": 69257,
        "process_start_time": (now - timedelta(minutes=3)).isoformat(),
        "cwd": "/tmp/runtime",
        "source_mtime": (now - timedelta(seconds=5)).isoformat(),
        "source_offset": 100,
        "observed_at": now.isoformat(),
    }
    for client in _client(SessionLocal):
        first = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [binding],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert first.status_code == 204, first.text

        bus = get_pubsub()
        topic = topic_session(str(session_id))
        first_seq = bus.peek_latest_seq(topic)
        assert first_seq > 0

        repeat_binding = {
            **binding,
            "source_mtime": (now + timedelta(seconds=1)).isoformat(),
            "source_offset": 200,
            "observed_at": (now + timedelta(seconds=2)).isoformat(),
        }
        repeat = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [repeat_binding],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert repeat.status_code == 204, repeat.text

    with get_pubsub().subscribe(topic_session(str(session_id)), since_seq=first_seq) as session_sub:
        msg = asyncio.run(session_sub.next_message(timeout=0.05))
        assert msg is None

    reset_pubsub_for_test()
    engine.dispose()


def test_heartbeat_empty_unmanaged_snapshot_closes_stale_unbound_gemini_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_gemini_unbound_missing.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="gemini", started_at=now - timedelta(minutes=20))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(minutes=10)
        runtime_key = runtime_key_for_session("gemini", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="gemini",
                    device_id="runtime-device",
                    source="gemini_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=10),
                    freshness_ms=90 * 1000,
                    dedupe_key="unbound-gemini-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
        )

        assert state.phase == "finished"
        assert state.terminal_state == "process_gone"
        assert view.status == "completed"
        assert facts.control_path == "unmanaged"
        assert facts.lifecycle.state == "closed"
        assert facts.lifecycle.reason == "process_gone"
        assert facts.process_state == "closed"

    engine.dispose()


def test_heartbeat_empty_unmanaged_snapshot_closes_stale_unbound_claude_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_claude_unbound_missing.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=20))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        session.last_activity_at = now - timedelta(minutes=10)
        runtime_key = runtime_key_for_session("claude", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="runtime-device",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=10),
                    freshness_ms=90 * 1000,
                    dedupe_key="unbound-claude-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
        )

        assert state.terminal_state == "process_gone"
        assert facts.lifecycle.state == "closed"
        assert facts.lifecycle.reason == "process_gone"
        assert facts.process_state == "closed"

    engine.dispose()


def test_heartbeat_empty_unmanaged_snapshot_closes_previously_bound_claude_session(tmp_path):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_claude_bound_missing.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        session.execution_home = "unmanaged_local"
        session.provider_session_id = str(session.id)
        runtime_key = runtime_key_for_session("claude", str(session.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="runtime-device",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=90 * 1000,
                    dedupe_key="bound-claude-phase",
                    payload={},
                )
            ],
        )
        db.commit()
        session_id = session.id

    for client in _client(SessionLocal):
        observed = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "claude",
                        "provider_session_id": str(session_id),
                        "pid": 69257,
                        "process_start_time": (now - timedelta(minutes=3)).isoformat(),
                        "observed_at": (now - timedelta(minutes=1)).isoformat(),
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert observed.status_code == 204, observed.text

        missing = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "unmanaged_session_bindings": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert missing.status_code == 204, missing.text

    bus = get_pubsub()
    session_messages = []
    with bus.subscribe(topic_session(str(session_id)), since_seq=0) as session_sub:
        while True:
            msg = asyncio.run(session_sub.next_message(timeout=0.01))
            if msg is None:
                break
            session_messages.append(msg.payload)
    assert any(
        message.get("session_id") == str(session_id) and message.get("source") == "engine_process_snapshot"
        for message in session_messages
    )

    with SessionLocal() as db:
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        overlay = load_binding_overlay(db, [session_id], now=now)[session_id]
        view = build_runtime_view(state=state, session=stored_session, now=now)
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
            binding_overlay=overlay,
        )

        assert overlay.terminal_reason == "process_gone"
        assert facts.process_state == "closed"
        assert facts.lifecycle.state == "closed"

    reset_pubsub_for_test()
    engine.dispose()


def test_heartbeat_missing_managed_lease_closes_immediately_with_stable_anchor(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_detached.db")
    now = datetime.now(timezone.utc)
    last_real_lease_at = now - timedelta(minutes=2)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=3),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=last_real_lease_at,
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="managed-lease-before-missing",
                    payload={"state": "attached"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))
        facts = build_session_liveness_facts(
            runtime_view=view,
            capabilities=build_session_capabilities(stored_session),
            last_activity_at=stored_session.last_activity_at,
        )

        assert state.phase == "finished"
        assert state.active_tool is None
        assert state.terminal_state == "process_gone"
        assert state.timeline_anchor_at.replace(tzinfo=timezone.utc) == last_real_lease_at
        assert view.status == "completed"
        assert view.presence_state is None
        assert view.display_phase == "Completed"
        assert facts.control_path == "managed"
        assert facts.lifecycle.state == "closed"
        assert facts.lifecycle.reason == "process_gone"
        assert facts.process_state == "closed"

    engine.dispose()


def test_heartbeat_missing_managed_lease_uses_observations_without_runtime_event_projection(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_observation_only.db")
    now = datetime.now(timezone.utc)
    last_real_lease_at = now - timedelta(minutes=2)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(hours=3),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=last_real_lease_at,
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="managed-lease-observation-only",
                    payload={"state": "attached"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "finished"
        assert state.terminal_state == "process_gone"
        assert state.timeline_anchor_at.replace(tzinfo=timezone.utc) == last_real_lease_at

    engine.dispose()


def test_heartbeat_omitted_managed_sessions_field_does_not_detach_old_engine(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_legacy_engine.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(hours=3))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="managed-lease-before-legacy-heartbeat",
                    payload={"state": "attached"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "old-engine",
                "daemon_pid": 123,
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "idle"
        assert state.active_tool is None
        assert state.terminal_state is None

    engine.dispose()


def test_heartbeat_missing_managed_lease_ignores_managed_session_without_lease_history(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_no_lease_history.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(hours=3))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="codex-bridge-without-managed-lease",
                    payload={"managed_transport": "codex_app_server"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "thinking"
        assert state.active_tool is None
        assert state.terminal_state is None

    engine.dispose()


def test_heartbeat_missing_managed_lease_closes_existing_synthetic_missing_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_expired.db")
    now = datetime.now(timezone.utc)
    last_real_lease_at = now - timedelta(days=1)
    bad_missing_at = now - timedelta(minutes=6)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(days=2),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=last_real_lease_at,
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="managed-lease-before-bad-missing",
                    payload={"state": "attached"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="control path",
                    occurred_at=bad_missing_at,
                    freshness_ms=24 * 60 * 60 * 1000,
                    dedupe_key="managed-lease-bad-synthetic-missing",
                    payload={"state": "missing"},
                ),
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))

        assert state.phase == "finished"
        assert state.terminal_state == "process_gone"
        assert state.timeline_anchor_at.replace(tzinfo=timezone.utc) == last_real_lease_at
        assert stored_session.ended_at is None
        assert view.status == "completed"
        assert view.display_phase == "Completed"

    engine.dispose()


def test_heartbeat_missing_managed_lease_without_real_anchor_falls_back_to_existing_anchor(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_before_boundary.db")
    now = datetime.now(timezone.utc)
    existing_anchor = now - timedelta(hours=23, minutes=59)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(days=2),
        )
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="control path",
                    occurred_at=existing_anchor,
                    freshness_ms=24 * 60 * 60 * 1000,
                    dedupe_key="managed-lease-detached-before-boundary",
                    payload={"state": "missing"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "finished"
        assert state.active_tool is None
        assert state.terminal_state == "process_gone"
        assert state.timeline_anchor_at.replace(tzinfo=timezone.utc) == existing_anchor

    engine.dispose()


def test_heartbeat_missing_managed_lease_only_closes_missing_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_one_missing.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        present = _seed_session(db, provider="codex", started_at=now - timedelta(hours=3))
        present.execution_home = "managed_local"
        present.managed_transport = "codex_app_server"
        missing = _seed_session(db, provider="codex", started_at=now - timedelta(hours=3))
        missing.execution_home = "managed_local"
        missing.managed_transport = "codex_app_server"
        present_key = runtime_key_for_session("codex", str(present.id))
        missing_key = runtime_key_for_session("codex", str(missing.id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=present_key,
                    session_id=present.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="present-before-partial-heartbeat",
                    payload={"state": "attached"},
                ),
                RuntimeEventIngest(
                    runtime_key=missing_key,
                    session_id=missing.id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="missing-before-partial-heartbeat",
                    payload={"state": "attached"},
                ),
            ],
        )
        db.commit()
        present_id = present.id
        missing_id = missing.id

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(present_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 99,
                        "state": "attached",
                        "phase": "idle",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        present_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == present_key).one()
        missing_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == missing_key).one()
        assert present_state.phase == "idle"
        assert present_state.active_tool is None
        assert present_state.terminal_state is None
        assert missing_state.phase == "finished"
        assert missing_state.active_tool is None
        assert missing_state.terminal_state == "process_gone"
        assert missing_state.session_id == missing_id

    engine.dispose()


def test_heartbeat_missing_managed_lease_ignores_already_terminal_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_terminal_ignored.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(days=2))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now - timedelta(hours=26),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="managed-lease-before-terminal",
                    payload={"state": "attached"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(hours=25),
                    dedupe_key="managed-terminal-before-missing",
                    payload={"terminal_state": "process_gone"},
                ),
            ],
        )
        db.commit()
        observation_count = len(_runtime_observations(db, runtime_key))

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "finished"
        assert state.terminal_state == "process_gone"
        assert len(_runtime_observations(db, runtime_key)) == observation_count

    engine.dispose()


def test_heartbeat_missing_managed_lease_does_not_detach_unmanaged_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_missing_unmanaged_guard.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(hours=3))
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now - timedelta(minutes=2),
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="unmanaged-with-managed-lease-source",
                    payload={"state": "attached"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "idle"
        assert state.active_tool is None
        assert state.terminal_state is None

    engine.dispose()


def test_heartbeat_managed_reattach_can_close_again_with_new_anchor(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_flap_rearms.db")
    now = datetime.now(timezone.utc)
    first_missing_at = now - timedelta(hours=23)
    reattached_at = now - timedelta(minutes=5)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(days=2))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="control path",
                    occurred_at=first_missing_at,
                    freshness_ms=24 * 60 * 60 * 1000,
                    dedupe_key="first-missing-detached",
                    payload={"state": "missing"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="bash",
                    occurred_at=reattached_at,
                    freshness_ms=15 * 60 * 1000,
                    dedupe_key="reattached-same-phase-new-tool",
                    payload={"state": "attached"},
                ),
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "finished"
        assert state.active_tool is None
        assert state.terminal_state == "process_gone"
        assert state.timeline_anchor_at.replace(tzinfo=timezone.utc) == reattached_at

    engine.dispose()


def test_heartbeat_managed_reattach_reopens_synthetic_process_gone_terminal(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_heartbeat_managed_reattach_after_process_gone.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex", started_at=now - timedelta(days=2))
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session.source_runner_id = 17
        session.source_runner_name = "cinder"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="codex",
                    device_id="runtime-device",
                    source="engine_attached_lease",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(minutes=30),
                    dedupe_key="synthetic-process-gone-before-reattach",
                    payload={"terminal_state": "process_gone"},
                )
            ],
        )
        db.commit()

    for client in _client(SessionLocal):
        response = client.post(
            "/agents/heartbeat",
            json={
                "version": "test",
                "daemon_pid": 123,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "cinder",
                        "sequence": 101,
                        "state": "attached",
                        "phase": "idle",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 15 * 60 * 1000,
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 204, response.text

    with SessionLocal() as db:
        assert db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first() is None
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        control = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).one()
        response = build_session_response(
            AgentsStore(db),
            stored_session,
            last_activity_at=stored_session.last_activity_at,
            runtime_overlay=None,
            owner_id=None,
            control_overlay=control,
        )

        assert control.control_state == "online"
        assert response.runtime_display is None
        assert response.capabilities.live_control_available is True

    engine.dispose()


def test_agents_store_ingest_mirrors_binding_and_progress_runtime_signals(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_store_mirror.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session_id = uuid4()
        runtime_key = runtime_key_for_session("claude", str(session_id))
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="runtime",
                device_id="cinder",
                started_at=now - timedelta(minutes=10),
                events=[
                    {
                        "role": "user",
                        "content_text": "check runtime state",
                        "timestamp": now - timedelta(seconds=15),
                        "source_path": "/tmp/runtime.jsonl",
                        "source_offset": 1,
                        "raw_json": '{"type":"user","message":"check runtime state"}',
                    },
                    {
                        "role": "assistant",
                        "content_text": "working on it",
                        "timestamp": now - timedelta(seconds=5),
                        "source_path": "/tmp/runtime.jsonl",
                        "source_offset": 2,
                        "raw_json": '{"type":"assistant","message":"working on it"}',
                    },
                ],
            )
        )

        assert str(result.session_id) == str(session_id)
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert str(state.session_id) == str(session_id)
        assert state.last_progress_at is not None
        assert state.timeline_anchor_at is not None
        assert state.phase_source in {"progress", "fallback"}

        event_kinds = {_runtime_observation_payload(row)["kind"] for row in _runtime_observations(db, runtime_key)}
        assert event_kinds == {"binding_signal", "progress_signal"}

    engine.dispose()


def test_managed_codex_ingest_does_not_write_parser_derived_ended_at(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_managed_codex_ignores_transcript_ended_at.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="test",
            project="runtime",
            started_at=now - timedelta(hours=1),
            ended_at=None,
            last_activity_at=now - timedelta(hours=1),
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            summary="runtime",
            summary_title="runtime",
            execution_home="managed_local",
            managed_transport="codex_app_server",
        )
        db.add(session)
        db.commit()

        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="runtime",
                device_id="cinder",
                execution_home="managed_local",
                started_at=now - timedelta(hours=1),
                ended_at=now,
                events=[
                    {
                        "role": "assistant",
                        "content_text": "latest transcript line",
                        "timestamp": now,
                        "source_path": "/tmp/codex.jsonl",
                        "source_offset": 2,
                        "raw_json": '{"type":"assistant","message":"latest transcript line"}',
                    }
                ],
            )
        )

        db.refresh(session)
        assert session.ended_at is None
        assert session.last_activity_at == now.replace(tzinfo=None)

    engine.dispose()


def test_managed_codex_ingest_preserves_explicit_session_ended_terminal(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_managed_codex_preserves_session_ended.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="test",
            project="runtime",
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=10),
            last_activity_at=now - timedelta(minutes=10),
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            summary="runtime",
            summary_title="runtime",
            execution_home="managed_local",
            managed_transport="codex_app_server",
        )
        db.add(session)
        db.commit()

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key_for_session("codex", str(session_id)),
                    session_id=session_id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(minutes=10),
                    dedupe_key="session-ended",
                    payload={"terminal_state": "session_ended"},
                )
            ],
        )
        db.commit()
        db.refresh(session)

        original_ended_at = session.ended_at
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="runtime",
                device_id="cinder",
                execution_home="managed_local",
                started_at=now - timedelta(hours=1),
                ended_at=now,
                events=[
                    {
                        "role": "assistant",
                        "content_text": "replayed transcript line",
                        "timestamp": now,
                        "source_path": "/tmp/codex-ended.jsonl",
                        "source_offset": 2,
                        "raw_json": '{"type":"assistant","message":"replayed transcript line"}',
                    }
                ],
            )
        )

        db.refresh(session)
        assert session.ended_at == original_ended_at

    engine.dispose()


def test_runtime_reducer_ignores_out_of_order_events(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_out_of_order.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="needs_user",
                    occurred_at=now - timedelta(seconds=5),
                    freshness_ms=phase_freshness_ms("needs_user"),
                    dedupe_key="phase-needs-user-new",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=35),
                    dedupe_key="progress-old",
                    payload={"progress_kind": "assistant_message"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="running",
                    tool_name="bash",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=phase_freshness_ms("running"),
                    dedupe_key="phase-running-old",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(seconds=40),
                    dedupe_key="terminal-old",
                    payload={"terminal_state": "finished"},
                ),
            ],
        )
        db.commit()

        assert result.accepted == 4
        assert result.duplicates == 0
        assert result.updated_runtime_keys == [runtime_key]

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "needs_user"
        assert state.phase_source == "semantic"
        assert state.active_tool is None
        assert state.last_progress_at is None
        assert state.terminal_state is None
        assert state.timeline_anchor_at == (now - timedelta(seconds=5)).replace(tzinfo=None)
        assert int(state.runtime_version) == 1

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.runtime_phase == "needs_user"
        assert view.status == "idle"
        assert view.display_phase == "Idle"
        assert view.confidence == "live"

    engine.dispose()


def test_runtime_view_keeps_progress_as_transcript_only_signal(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_inferred_phase_view.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=10),
                    dedupe_key="progress-recent",
                    payload={"progress_kind": "assistant_message"},
                )
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.last_live_at is None
        assert view.runtime_phase is None
        assert view.signal_tier == "transcript_progress"
        assert view.status == "idle"
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_runtime_view_does_not_keep_stale_progress_running(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_stale_progress_view.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("opencode", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="opencode",
                    device_id="cinder",
                    source="agents_ingest",
                    kind="progress_signal",
                    occurred_at=now - timedelta(hours=1),
                    dedupe_key="progress-old",
                    payload={"progress_kind": "transcript_append"},
                )
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.last_live_at is None
        assert view.presence_state is None
        assert view.signal_tier == "transcript_progress"
        assert view.status == "idle"
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_progress_signal_preserves_fresh_phase_signal_truth(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_progress_preserves_phase_signal.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="running",
                    tool_name="bash",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=phase_freshness_ms("running"),
                    dedupe_key="phase-running",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="progress-fresh",
                    payload={"progress_kind": "assistant_message"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "running"
        assert state.phase_source == "semantic"
        assert view.presence_state == "running"
        assert view.signal_tier == "phase_signal"
        assert view.status == "working"
        assert view.display_phase == "Running bash"
        assert view.confidence == "live"

    engine.dispose()


def test_progress_signal_after_stale_phase_signal_does_not_revive_phase_truth(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_progress_does_not_revive_stale_phase.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("claude", str(session.id))
        db.add(
            SessionRuntimeState(
                runtime_key=runtime_key,
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(minutes=20),
                last_runtime_signal_at=now - timedelta(minutes=20),
                last_progress_at=now - timedelta(minutes=20),
                last_live_at=now - timedelta(minutes=20),
                timeline_anchor_at=now - timedelta(minutes=20),
                freshness_expires_at=now - timedelta(minutes=10),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="progress-after-stale-phase",
                    payload={"progress_kind": "assistant_message"},
                )
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "running"
        assert state.phase_source == "semantic"
        assert view.runtime_phase is None
        assert view.presence_state is None
        assert view.signal_tier == "phase_signal"
        assert view.status == "idle"
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_needs_user_freshness_is_not_extended_by_progress(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_needs_user_progress_does_not_extend_freshness.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=1))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="needs_user",
                    occurred_at=now - timedelta(minutes=20),
                    freshness_ms=phase_freshness_ms("needs_user"),
                    dedupe_key="phase-needs-user",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=30),
                    dedupe_key="progress-after-needs-user",
                    payload={"progress_kind": "transcript_append"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "needs_user"
        assert state.phase_source == "semantic"
        assert state.freshness_expires_at == (now - timedelta(minutes=10)).replace(tzinfo=None)
        assert view.runtime_phase is None
        assert view.presence_state is None
        assert view.signal_tier == "phase_signal"
        assert view.status == "idle"
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_runtime_view_hides_stale_attention_phase(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_stale_attention_view.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(hours=2))
        runtime_key = runtime_key_for_session("claude", str(session.id))
        db.add(
            SessionRuntimeState(
                runtime_key=runtime_key,
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="needs_user",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now - timedelta(hours=2),
                last_runtime_signal_at=now - timedelta(hours=2),
                last_progress_at=now - timedelta(hours=2),
                last_live_at=now - timedelta(hours=2),
                timeline_anchor_at=now - timedelta(hours=2),
                freshness_expires_at=now - timedelta(hours=1),
                terminal_state=None,
                terminal_at=None,
                runtime_version=4,
            )
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert view.runtime_phase is None
        assert view.status == "idle"
        assert view.presence_state is None
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_newer_progress_reopens_finished_runtime_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_progress_reopens_finished.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=30))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(seconds=20),
                    dedupe_key="terminal-finished",
                    payload={"terminal_state": "finished"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=5),
                    dedupe_key="progress-after-terminal",
                    payload={"progress_kind": "assistant_message"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.terminal_state is None
        assert state.last_progress_at == (now - timedelta(seconds=5)).replace(tzinfo=None)

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.runtime_phase is None
        assert view.status == "idle"
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_runtime_view_aligns_codex_running_with_claude_semantics(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_codex_running.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            started_at=now - timedelta(minutes=20),
        )
        runtime_key = runtime_key_for_session("codex", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="running",
                    tool_name="shell",
                    occurred_at=now - timedelta(seconds=4),
                    freshness_ms=phase_freshness_ms("running"),
                    dedupe_key="codex-running-shell",
                    payload={"managed_transport": "codex_app_server"},
                )
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "running"
        assert state.phase_source == "codex_bridge"
        assert state.active_tool == "shell"
        assert view.status == "working"
        assert view.runtime_phase == "running"
        assert view.presence_state == "running"
        assert view.presence_tool == "shell"
        assert view.display_phase == "Running shell"
        assert view.confidence == "live"

    engine.dispose()


def test_progress_signal_does_not_override_attention_phase(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_attention_progress.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="bash",
                    occurred_at=now - timedelta(seconds=8),
                    freshness_ms=phase_freshness_ms("blocked"),
                    dedupe_key="blocked-live",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now - timedelta(seconds=3),
                    dedupe_key="progress-after-blocked",
                    payload={"progress_kind": "assistant_message"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "blocked"
        assert state.phase_source == "semantic"
        assert state.active_tool == "bash"
        assert state.last_progress_at == (now - timedelta(seconds=3)).replace(tzinfo=None)
        assert view.status == "active"
        assert view.runtime_phase == "blocked"
        assert view.presence_state == "blocked"
        assert view.presence_tool == "bash"
        assert view.display_phase == "Blocked on bash"
        assert view.confidence == "live"

    engine.dispose()


def test_older_phase_signal_does_not_override_newer_progress(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_old_phase_after_progress.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now,
                    dedupe_key="progress-new",
                    payload={"progress_kind": "assistant_message"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(seconds=30),
                    freshness_ms=phase_freshness_ms("thinking"),
                    dedupe_key="phase-old-thinking",
                    payload={},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.last_progress_at == now.replace(tzinfo=None)
        assert state.last_runtime_signal_at is None
        assert view.runtime_phase is None
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_older_terminal_signal_does_not_override_newer_progress(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "runtime_old_terminal_after_progress.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20))
        runtime_key = runtime_key_for_session("claude", str(session.id))

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="transcript",
                    kind="progress_signal",
                    occurred_at=now,
                    dedupe_key="progress-new",
                    payload={"progress_kind": "assistant_message"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now - timedelta(seconds=30),
                    dedupe_key="terminal-old",
                    payload={"terminal_state": "finished"},
                ),
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        view = build_runtime_view(state=state, session=session, now=now)

        assert state.phase == "idle"
        assert state.phase_source == "progress"
        assert state.terminal_state is None
        assert state.terminal_at is None
        assert state.last_progress_at == now.replace(tzinfo=None)
        assert view.runtime_phase is None
        assert view.display_phase == "Inactive"
        assert view.confidence == "stale"

    engine.dispose()


def test_runtime_batch_hoists_apns_prep_per_batch(tmp_path, monkeypatch):
    """5-session batch must call _active_ios_targets_for_owner at most twice
    (once per platform: ios + ios_widget) and prepare_widget_timeline_push exactly
    once per batch — not per session × per push type."""
    from zerg.services import apns_sender

    engine, SessionLocal = _make_db(tmp_path, "runtime_apns_hoist.db")
    now = datetime.now(timezone.utc)

    sessions: list[AgentSession] = []
    runtime_keys: list[str] = []
    with SessionLocal() as db:
        for _ in range(5):
            s = _seed_session(db, started_at=now - timedelta(minutes=20))
            sessions.append(s)
            runtime_keys.append(runtime_key_for_session("claude", str(s.id)))

    targets_call_count = {"n": 0}
    widget_prep_call_count = {"n": 0}
    runtime_state_map_call_count = {"n": 0}

    real_targets = apns_sender._active_ios_targets_for_owner

    def counting_targets(*args, **kwargs):
        targets_call_count["n"] += 1
        return real_targets(*args, **kwargs)

    real_widget = apns_sender.prepare_widget_timeline_push

    def counting_widget(*args, **kwargs):
        widget_prep_call_count["n"] += 1
        return real_widget(*args, **kwargs)

    from zerg.services import session_runtime as session_runtime_module

    real_load_runtime_state_map = session_runtime_module.load_runtime_state_map

    def counting_load_runtime_state_map(*args, **kwargs):
        runtime_state_map_call_count["n"] += 1
        return real_load_runtime_state_map(*args, **kwargs)

    # Patch in both places: the apns_sender module (used by prepare_*) and the
    # runtime router module (which imports the public alias and the prep fn directly).
    monkeypatch.setattr(apns_sender, "_active_ios_targets_for_owner", counting_targets)
    monkeypatch.setattr(apns_sender, "active_ios_targets_for_owner", counting_targets)
    monkeypatch.setattr(apns_sender, "prepare_widget_timeline_push", counting_widget)
    monkeypatch.setattr(apns_sender, "load_runtime_state_map", counting_load_runtime_state_map)
    from zerg.routers import runtime as runtime_router

    monkeypatch.setattr(runtime_router, "active_ios_targets_for_owner", counting_targets)
    monkeypatch.setattr(runtime_router, "prepare_widget_timeline_push", counting_widget)
    monkeypatch.setattr(runtime_router, "load_runtime_state_map", counting_load_runtime_state_map)

    for client in _client(SessionLocal):
        events = []
        for s, rk in zip(sessions, runtime_keys, strict=True):
            events.append(
                {
                    "runtime_key": rk,
                    "session_id": str(s.id),
                    "provider": "claude",
                    "device_id": "cinder",
                    "source": "claude_hook",
                    "kind": "phase_signal",
                    "phase": "thinking",
                    "occurred_at": (now - timedelta(seconds=10)).isoformat(),
                    "freshness_ms": phase_freshness_ms("thinking"),
                    "dedupe_key": f"hoist-{s.id}",
                    "payload": {},
                }
            )
        resp = client.post(
            "/agents/runtime/events/batch",
            json={"events": events},
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text

    # 5-session batch: targets pre-fetched once per platform (ios + ios_widget) = 2.
    # Old code: 2 prep fns × 5 sessions (ios) + 1 prep fn × 5 sessions (widget) = 15.
    assert targets_call_count["n"] == 2, f"expected 2 target lookups, got {targets_call_count['n']}"
    # Widget prep: once per batch, not per session.
    assert widget_prep_call_count["n"] == 1, f"expected 1 widget prep, got {widget_prep_call_count['n']}"
    # Runtime state map: pre-loaded once at top of batch loop, threaded into
    # prepare_session_live_activity_pushes — not re-queried per session.
    # Old code: 1 (router) + 5 (per-session inside prepare_*) = 6.
    assert (
        runtime_state_map_call_count["n"] == 1
    ), f"expected 1 load_runtime_state_map call, got {runtime_state_map_call_count['n']}"

    engine.dispose()


def test_runtime_batch_live_transcript_skips_apns_and_owner_work(tmp_path, monkeypatch):
    reset_pubsub_for_test()
    engine, SessionLocal = _make_db(tmp_path, "runtime_live_fast_path.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=20), provider="codex")
        runtime_key = runtime_key_for_session("codex", str(session.id))

    from zerg.routers import runtime as runtime_router

    call_counts = {
        "targets": 0,
        "widget": 0,
        "runtime_map": 0,
        "owner": 0,
    }

    def count(name):
        def _inner(*args, **kwargs):
            call_counts[name] += 1
            if name == "owner":
                return 1
            if name == "runtime_map":
                return {}
            return None

        return _inner

    monkeypatch.setattr(runtime_router, "active_ios_targets_for_owner", count("targets"))
    monkeypatch.setattr(runtime_router, "prepare_widget_timeline_push", count("widget"))
    monkeypatch.setattr(runtime_router, "load_runtime_state_map", count("runtime_map"))
    monkeypatch.setattr(runtime_router, "resolve_session_message_owner_id", count("owner"))

    for client in _client(SessionLocal):
        resp = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    {
                        "runtime_key": runtime_key,
                        "session_id": str(session.id),
                        "provider": "codex",
                        "device_id": "cinder",
                        "source": "codex_bridge_live",
                        "kind": "progress_signal",
                        "occurred_at": now.isoformat(),
                        "dedupe_key": f"bridge:live:{session.id}:thread-1:turn-1:1",
                        "payload": {
                            "progress_kind": "bridge_live_transcript_delta",
                            "managed_transport": "codex_app_server",
                            "thread_id": "thread-1",
                            "turn_id": "turn-1",
                            "seq": 1,
                            "method": "item/agentMessage/delta",
                            "delta": "LH",
                            "live_text": "LH",
                            "turn_completed": False,
                        },
                    }
                ]
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated_runtime_keys"] == [runtime_key]
        assert resp.headers["X-Runtime-Label"] == "runtime-live"
        assert float(resp.headers["X-Runtime-Queue-Wait-Ms"]) >= 0.0
        assert float(resp.headers["X-Runtime-Exec-Ms"]) >= 0.0

    assert call_counts == {
        "targets": 0,
        "widget": 0,
        "runtime_map": 0,
        "owner": 0,
    }

    bus = get_pubsub()
    with bus.subscribe(topic_session(str(session.id)), since_seq=0) as session_sub:
        msg = asyncio.run(session_sub.next_message(timeout=0.1))
        assert msg is not None
        assert msg.payload["kind"] == "transcript_preview"
        assert msg.payload["session_id"] == str(session.id)
        assert msg.payload["provider"] == "codex"
        assert msg.payload["source"] == "codex_bridge_live"
        assert msg.payload["transcript_preview"]["text"] == "LH"
        assert isinstance(msg.payload.get("server_fanout_at_ms"), int)
        msg = asyncio.run(session_sub.next_message(timeout=0.1))
        assert msg is not None
        assert msg.payload["kind"] == "runtime"
        assert msg.payload["session_id"] == str(session.id)
        assert msg.payload["provider"] == "codex"
        assert msg.payload["source"] == "codex_bridge_live"
        assert isinstance(msg.payload.get("server_fanout_at_ms"), int)
    assert bus.peek_latest_seq(TOPIC_TIMELINE) == 1

    reset_pubsub_for_test()
    engine.dispose()
