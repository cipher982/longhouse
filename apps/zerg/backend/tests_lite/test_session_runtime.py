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
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path, name="session_runtime.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _seed_session(db, *, started_at: datetime | None = None) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
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
        yield TestClient(api_app)
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
