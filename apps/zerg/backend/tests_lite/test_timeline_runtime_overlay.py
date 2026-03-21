"""HTTP-level tests for timeline runtime overlay and recent-activity ordering.

Covers:
- /agents/sessions uses recent activity anchor (presence/event activity), not raw started_at
- Open sessions without fresh live signals return an idle runtime overlay instead of implicit active
- Fresh presence overrides ended_at for /agents/sessions/active
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionPresence


def _make_db(tmp_path, name="timeline_runtime_overlay.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    project: str = "zerg",
    environment: str = "production",
    user_messages: int = 2,
    assistant_messages: int = 2,
    tool_calls: int = 0,
):
    session = AgentSession(
        provider="claude",
        environment=environment,
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        summary="Timeline runtime test",
        summary_title="Timeline runtime test",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _upsert_presence(
    db,
    *,
    session_id: str,
    state: str,
    updated_at: datetime,
    tool_name: str | None = None,
    project: str = "zerg",
):
    row = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
    if row is None:
        row = SessionPresence(
            session_id=session_id,
            state=state,
            tool_name=tool_name,
            cwd="/tmp/zerg",
            project=project,
            provider="claude",
            updated_at=updated_at,
        )
        db.add(row)
    else:
        row.state = state
        row.tool_name = tool_name
        row.updated_at = updated_at
    db.commit()


def _client(factory):
    from zerg.main import api_app

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="timeline-runtime", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        yield TestClient(api_app)
    finally:
        api_app.dependency_overrides.clear()


def test_sessions_list_uses_recent_activity_anchor_for_old_live_session(tmp_path):
    factory = _make_db(tmp_path, "recent_anchor.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_live = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-live",
        )
        recent_idle = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1, minutes=30),
            project="recent-idle",
        )
        _upsert_presence(
            db,
            session_id=str(old_live.id),
            state="running",
            updated_at=now - timedelta(seconds=20),
            tool_name="bash",
            project="old-live",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["total"] >= 1
        top = payload["sessions"][0]
        assert top["id"] == str(old_live.id)
        assert top["project"] == "old-live"
        assert top["status"] == "working"
        assert top["presence_state"] == "running"
        assert top["active_tool"] == "bash"
        assert top["display_phase"] == "Running bash"
        assert top["confidence"] == "live"
        assert top["timeline_anchor_at"] is not None
        assert top["timeline_anchor_at"] >= recent_idle.started_at.isoformat().replace("+00:00", "Z")


def test_sessions_list_marks_old_open_session_idle_without_live_signal(tmp_path):
    factory = _make_db(tmp_path, "open_idle.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=3),
            ended_at=None,
            project="open-idle",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["sessions"][0]
        assert data["id"] == str(session.id)
        assert data["status"] == "idle"
        assert data["display_phase"] == "Idle"
        assert data["presence_state"] is None
        assert data["confidence"] is None


def test_active_sessions_fresh_presence_beats_ended_at(tmp_path):
    factory = _make_db(tmp_path, "presence_beats_ended.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=2),
            project="fresh-presence",
        )
        _upsert_presence(
            db,
            session_id=str(session.id),
            state="thinking",
            updated_at=now - timedelta(seconds=15),
            project="fresh-presence",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == str(session.id)
        assert row["status"] == "working"
        assert row["presence_state"] == "thinking"
        assert row["display_phase"] == "Thinking"
        assert row["confidence"] == "live"
        assert row["timeline_anchor_at"] is not None
