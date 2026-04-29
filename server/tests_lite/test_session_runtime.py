"""Tests for runtime event ingestion and materialized runtime state."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeEvent
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime import current_presence_state_for_session
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import managed_codex_liveness_invariant_counts
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path, name="session_runtime.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
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
                    payload={"terminal_state": "finished"},
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
        assert db.query(SessionRuntimeEvent).count() == 1
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert str(state.session_id) == str(session.id)
        assert state.phase == "thinking"
        assert int(state.runtime_version) == 1

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
        assert db.query(SessionRuntimeEvent).filter(SessionRuntimeEvent.runtime_key == runtime_key).count() == 1

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


def test_heartbeat_attached_managed_codex_lease_materializes_live_idle_state(tmp_path):
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
        session.ended_at = now - timedelta(hours=2)
        db.commit()
        session_id = session.id
        runtime_key = runtime_key_for_session("codex", str(session_id))

    before_request = datetime.now(timezone.utc)
    observed_at = now - timedelta(days=1)
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
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=after_request)

        assert state.phase == "idle"
        assert state.last_runtime_signal_at is not None
        last_signal = state.last_runtime_signal_at.replace(tzinfo=timezone.utc)
        assert before_request <= last_signal <= after_request
        assert last_signal != observed_at
        assert state.freshness_expires_at is not None
        lease_expiry = state.freshness_expires_at.replace(tzinfo=timezone.utc)
        assert lease_expiry >= before_request + timedelta(minutes=15)
        assert view.status == "idle"
        assert view.presence_state == "idle"
        assert view.display_phase == "Idle"
        assert view.confidence == "live"
        assert stored_session.ended_at is None

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
        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        stored_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        view = build_runtime_view(state=state, session=stored_session, now=datetime.now(timezone.utc))

        assert state.phase == "blocked"
        assert state.active_tool == "control path"
        assert state.terminal_state is None
        assert view.status == "active"
        assert view.presence_state == "blocked"
        assert view.display_phase == "Blocked on control path"

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

        event_kinds = {
            row.kind
            for row in db.query(SessionRuntimeEvent)
            .filter(SessionRuntimeEvent.runtime_key == runtime_key)
            .all()
        }
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
        assert view.status == "active"
        assert view.display_phase == "Needs you"
        assert view.confidence == "live"

    engine.dispose()


def test_runtime_view_hides_semantic_phase_for_inferred_progress(tmp_path):
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

        assert state.phase == "running"
        assert state.phase_source == "progress"
        assert view.runtime_phase is None
        assert view.status == "active"
        assert view.display_phase == "Recent progress"
        assert view.confidence == "inferred"

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
        assert state.phase == "running"
        assert state.phase_source == "progress"
        assert state.terminal_state is None
        assert state.last_progress_at == (now - timedelta(seconds=5)).replace(tzinfo=None)

        view = build_runtime_view(state=state, session=session, now=now)
        assert view.runtime_phase is None
        assert view.status == "active"
        assert view.display_phase == "Recent progress"
        assert view.confidence == "inferred"

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

        assert state.phase == "running"
        assert state.phase_source == "progress"
        assert state.last_progress_at == now.replace(tzinfo=None)
        assert state.last_runtime_signal_at is None
        assert view.runtime_phase is None
        assert view.display_phase == "Recent progress"
        assert view.confidence == "inferred"

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

        assert state.phase == "running"
        assert state.phase_source == "progress"
        assert state.terminal_state is None
        assert state.terminal_at is None
        assert state.last_progress_at == now.replace(tzinfo=None)
        assert view.runtime_phase is None
        assert view.display_phase == "Recent progress"
        assert view.confidence == "inferred"

    engine.dispose()
