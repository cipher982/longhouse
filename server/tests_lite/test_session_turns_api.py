import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_DURABLE
from zerg.services.session_turns import SESSION_TURN_STATE_TERMINAL


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_turns_api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _get_client(session_factory):
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="turns-api", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def _seed_session(db, *, managed_transport: str | None = None) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="zerg",
        started_at=datetime(2026, 4, 16, 10, 0, 0),
        user_messages=3,
        assistant_messages=3,
        tool_calls=1,
        managed_transport=managed_transport,
    )
    db.add(session)
    db.flush()

    # Session-identity-kernel cleanup: managed_transport derives from
    # session_connections.control_plane on the primary thread's latest run.
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionThread

    thread = SessionThread(
        id=uuid4(),
        session_id=session.id,
        provider=session.provider,
        is_primary=1,
    )
    db.add(thread)
    db.flush()
    session.primary_thread_id = thread.id

    if managed_transport is not None:
        plane_map = {
            "claude_channel_bridge": "claude_channel_bridge",
            "codex_app_server": "codex_app_server",
            "opencode_process": "opencode_process",
            "antigravity_process": "antigravity_process",
        }
        control_plane = plane_map.get(managed_transport, managed_transport)
        run = SessionRun(
            id=uuid4(),
            thread_id=thread.id,
            provider=session.provider,
            host_id=session.device_id,
            started_at=datetime(2026, 4, 16, 10, 0, 0),
        )
        db.add(run)
        db.flush()
        db.add(
            SessionConnection(
                run_id=run.id,
                control_plane=control_plane,
                acquisition_kind="spawned_control",
                state="attached",
            )
        )
    db.commit()
    db.refresh(session)
    return session


def _seed_turn(
    db,
    *,
    session_id,
    request_id: str,
    state: str,
    user_submitted_at: datetime,
    created_at: datetime,
    updated_at: datetime,
    terminal_phase: str | None = None,
    send_accepted_at: datetime | None = None,
    active_phase_observed_at: datetime | None = None,
    terminal_at: datetime | None = None,
    durable_at: datetime | None = None,
    user_event_id: int | None = None,
    durable_assistant_event_id: int | None = None,
):
    turn = SessionTurn(
        session_id=session_id,
        request_id=request_id,
        state=state,
        terminal_phase=terminal_phase,
        user_event_id=user_event_id,
        durable_assistant_event_id=durable_assistant_event_id,
        baseline_event_id=10,
        baseline_observation_cursor=5,
        user_submitted_at=user_submitted_at,
        send_accepted_at=send_accepted_at,
        active_phase_observed_at=active_phase_observed_at,
        terminal_at=terminal_at,
        durable_at=durable_at,
        created_at=created_at,
        updated_at=updated_at,
    )
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def test_list_session_turns_defaults_to_stable_ascending_order(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db)
        late = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-late",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=datetime(2026, 4, 16, 10, 5, 0),
            created_at=datetime(2026, 4, 16, 10, 5, 1),
            updated_at=datetime(2026, 4, 16, 10, 5, 2),
            send_accepted_at=datetime(2026, 4, 16, 10, 5, 0),
            active_phase_observed_at=datetime(2026, 4, 16, 10, 5, 1),
        )
        earliest = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-early-a",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=datetime(2026, 4, 16, 10, 1, 0),
            created_at=datetime(2026, 4, 16, 10, 1, 5),
            updated_at=datetime(2026, 4, 16, 10, 1, 8),
            terminal_phase="completed",
            send_accepted_at=datetime(2026, 4, 16, 10, 1, 1),
            active_phase_observed_at=datetime(2026, 4, 16, 10, 1, 2),
            terminal_at=datetime(2026, 4, 16, 10, 1, 7),
            durable_at=datetime(2026, 4, 16, 10, 1, 8),
            user_event_id=101,
            durable_assistant_event_id=201,
        )
        second = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-early-b",
            state=SESSION_TURN_STATE_TERMINAL,
            user_submitted_at=datetime(2026, 4, 16, 10, 1, 0),
            created_at=datetime(2026, 4, 16, 10, 1, 6),
            updated_at=datetime(2026, 4, 16, 10, 1, 7),
            terminal_phase="completed",
            send_accepted_at=datetime(2026, 4, 16, 10, 1, 3),
            terminal_at=datetime(2026, 4, 16, 10, 1, 7),
        )
    finally:
        db.close()

    for client in _get_client(session_factory):
        response = client.get(f"/agents/sessions/{session.id}/turns")
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["total"] == 3
        assert [item["id"] for item in data["turns"]] == [earliest.id, second.id, late.id]
        assert [item["request_id"] for item in data["turns"]] == ["req-early-a", "req-early-b", "req-late"]
        assert data["turns"][0]["timing"] == {
            "submit_to_send_ms": 1000,
            "submit_to_active_ms": 2000,
            "submit_to_terminal_ms": 7000,
            "active_to_terminal_ms": 5000,
            "terminal_to_durable_ms": 1000,
            "total_turn_time_ms": 8000,
        }
        assert data["turns"][1]["timing"] == {
            "submit_to_send_ms": 3000,
            "submit_to_active_ms": None,
            "submit_to_terminal_ms": 7000,
            "active_to_terminal_ms": None,
            "terminal_to_durable_ms": None,
            "total_turn_time_ms": 7000,
        }
        assert data["turns"][2]["timing"] == {
            "submit_to_send_ms": 0,
            "submit_to_active_ms": 1000,
            "submit_to_terminal_ms": None,
            "active_to_terminal_ms": None,
            "terminal_to_durable_ms": None,
            "total_turn_time_ms": None,
        }


def test_list_session_turns_supports_desc_order_pagination_and_utc_strings(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db)
        first = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-1",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=datetime(2026, 4, 16, 10, 0, 1),
            created_at=datetime(2026, 4, 16, 10, 0, 2),
            updated_at=datetime(2026, 4, 16, 10, 0, 3),
            send_accepted_at=datetime(2026, 4, 16, 10, 0, 1),
        )
        second = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-2",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=datetime(2026, 4, 16, 10, 0, 4),
            created_at=datetime(2026, 4, 16, 10, 0, 5),
            updated_at=datetime(2026, 4, 16, 10, 0, 6),
            send_accepted_at=datetime(2026, 4, 16, 10, 0, 4),
        )
        third = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-3",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=datetime(2026, 4, 16, 10, 0, 7),
            created_at=datetime(2026, 4, 16, 10, 0, 8),
            updated_at=datetime(2026, 4, 16, 10, 0, 9),
            send_accepted_at=datetime(2026, 4, 16, 10, 0, 7),
        )
    finally:
        db.close()

    for client in _get_client(session_factory):
        response = client.get(f"/agents/sessions/{session.id}/turns?order=desc&limit=2&offset=1")
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["total"] == 3
        assert [item["id"] for item in data["turns"]] == [second.id, first.id]
        assert data["turns"][0]["user_submitted_at"].endswith("Z")
        assert data["turns"][0]["created_at"].endswith("Z")
        assert third.id not in [item["id"] for item in data["turns"]]


def test_list_session_turns_materializes_managed_native_turns_from_transcript(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db, managed_transport="claude_channel_bridge")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime(2026, 4, 16, 10, 1, 0, tzinfo=timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="done",
                    timestamp=datetime(2026, 4, 16, 10, 1, 11, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    for client in _get_client(session_factory):
        response = client.get(f"/agents/sessions/{session.id}/turns")
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["total"] == 1
        assert data["turns"][0]["request_id"].startswith("native:")
        assert data["turns"][0]["state"] == SESSION_TURN_STATE_DURABLE
        assert data["turns"][0]["timing"] == {
            "submit_to_send_ms": None,
            "submit_to_active_ms": None,
            "submit_to_terminal_ms": None,
            "active_to_terminal_ms": None,
            "terminal_to_durable_ms": None,
            "total_turn_time_ms": 11000,
        }


def test_get_session_turn_detail_returns_envelope(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db)
        turn = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-detail",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=datetime(2026, 4, 16, 11, 0, 0),
            created_at=datetime(2026, 4, 16, 11, 0, 0),
            updated_at=datetime(2026, 4, 16, 11, 0, 5),
            terminal_phase="completed",
            send_accepted_at=datetime(2026, 4, 16, 11, 0, 1),
            active_phase_observed_at=datetime(2026, 4, 16, 11, 0, 2),
            terminal_at=datetime(2026, 4, 16, 11, 0, 4),
            durable_at=datetime(2026, 4, 16, 11, 0, 5),
            user_event_id=111,
            durable_assistant_event_id=222,
        )
    finally:
        db.close()

    for client in _get_client(session_factory):
        response = client.get(f"/agents/sessions/{session.id}/turns/{turn.id}")
        assert response.status_code == 200, response.text
        data = response.json()

        assert set(data.keys()) == {"turn"}
        assert data["turn"]["id"] == turn.id
        assert data["turn"]["session_id"] == str(session.id)
        assert data["turn"]["request_id"] == "req-detail"
        assert data["turn"]["state"] == SESSION_TURN_STATE_DURABLE
        assert data["turn"]["durable_assistant_event_id"] == 222
        assert data["turn"]["durable_at"].endswith("Z")
        assert data["turn"]["timing"] == {
            "submit_to_send_ms": 1000,
            "submit_to_active_ms": 2000,
            "submit_to_terminal_ms": 4000,
            "active_to_terminal_ms": 2000,
            "terminal_to_durable_ms": 1000,
            "total_turn_time_ms": 5000,
        }


def test_get_session_turns_rejects_invalid_order(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db)
    finally:
        db.close()

    for client in _get_client(session_factory):
        response = client.get(f"/agents/sessions/{session.id}/turns?order=sideways")
        assert response.status_code == 400
        assert response.json()["detail"] == "order must be one of: asc, desc"


def test_get_session_turn_detail_returns_404_for_missing_session_or_turn(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session = _seed_session(db)
        turn = _seed_turn(
            db,
            session_id=session.id,
            request_id="req-present",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=datetime(2026, 4, 16, 12, 0, 0),
            created_at=datetime(2026, 4, 16, 12, 0, 0),
            updated_at=datetime(2026, 4, 16, 12, 0, 1),
            send_accepted_at=datetime(2026, 4, 16, 12, 0, 0),
        )
        missing_session_id = uuid4()
    finally:
        db.close()

    for client in _get_client(session_factory):
        missing_session = client.get(f"/agents/sessions/{missing_session_id}/turns")
        assert missing_session.status_code == 404
        assert missing_session.json()["detail"] == f"Session {missing_session_id} not found"

        missing_turn = client.get(f"/agents/sessions/{session.id}/turns/{turn.id + 100}")
        assert missing_turn.status_code == 404
        assert missing_turn.json()["detail"] == f"Turn {turn.id + 100} not found for session {session.id}"
