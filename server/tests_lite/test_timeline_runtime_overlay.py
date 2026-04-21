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
from zerg.models.agents import SessionRuntimeState
from zerg.session_execution_home import SessionExecutionHome


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
    continuation_kind: str | None = None,
    origin_label: str | None = None,
    user_messages: int = 2,
    assistant_messages: int = 2,
    tool_calls: int = 0,
    execution_home: str | None = None,
    managed_transport: str | None = None,
    source_runner_id: int | None = None,
    managed_session_name: str | None = None,
):
    session = AgentSession(
        provider="claude",
        environment=environment,
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        continuation_kind=continuation_kind,
        origin_label=origin_label,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        summary="Timeline runtime test",
        summary_title="Timeline runtime test",
        execution_home=execution_home,
        managed_transport=managed_transport,
        source_runner_id=source_runner_id,
        managed_session_name=managed_session_name,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _upsert_runtime_state(
    db,
    *,
    session_id: str,
    phase: str,
    updated_at: datetime,
    tool_name: str | None = None,
    provider: str = "claude",
    freshness_window: timedelta = timedelta(minutes=5),
):
    runtime_key = f"{provider}:{session_id}"
    row = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.runtime_key == runtime_key)
        .first()
    )
    if row is None:
        row = SessionRuntimeState(
            runtime_key=runtime_key,
            session_id=session_id,
            provider=provider,
            phase=phase,
            phase_source="semantic",
            active_tool=tool_name,
            phase_started_at=updated_at,
            last_runtime_signal_at=updated_at,
            last_progress_at=updated_at,
            last_live_at=updated_at,
            timeline_anchor_at=updated_at,
            freshness_expires_at=updated_at + freshness_window,
            runtime_version=1,
        )
        db.add(row)
    else:
        row.phase = phase
        row.phase_source = "semantic"
        row.active_tool = tool_name
        row.phase_started_at = updated_at
        row.last_runtime_signal_at = updated_at
        row.last_progress_at = updated_at
        row.last_live_at = updated_at
        row.timeline_anchor_at = updated_at
        row.freshness_expires_at = updated_at + freshness_window
        row.runtime_version = (row.runtime_version or 0) + 1
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
        _upsert_runtime_state(
            db,
            session_id=str(old_live.id),
            phase="running",
            updated_at=now - timedelta(seconds=20),
            tool_name="bash",
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


def test_sessions_list_exposes_home_label_from_existing_session_metadata(tmp_path):
    factory = _make_db(tmp_path, "execution_home.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        legacy = _seed_session(
            db,
            started_at=now - timedelta(hours=4),
            ended_at=now - timedelta(hours=3),
            project="legacy-local",
            origin_label="cinder",
            environment="production",
        )
        cloud = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            project="cloud-branch",
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=10", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = {row["project"]: row for row in resp.json()["sessions"]}
        assert rows["legacy-local"]["id"] == str(legacy.id)
        assert rows["legacy-local"]["home_label"] is None
        assert rows["cloud-branch"]["id"] == str(cloud.id)
        assert rows["cloud-branch"]["home_label"] is None  # cloud labels hidden for launch


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
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="thinking",
            updated_at=now - timedelta(seconds=15),
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


def test_sessions_list_uses_runtime_anchor_for_old_runtime_only_session(tmp_path):
    factory = _make_db(tmp_path, "runtime_anchor_sessions.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime",
        )
        _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            project="recent-history",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{old_runtime.id}",
                session_id=old_runtime.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(seconds=30),
                last_runtime_signal_at=now - timedelta(seconds=30),
                last_progress_at=now - timedelta(seconds=15),
                last_live_at=now - timedelta(seconds=30),
                timeline_anchor_at=now - timedelta(seconds=15),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=2,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["total"] >= 1
        row = payload["sessions"][0]
        assert row["id"] == str(old_runtime.id)
        assert row["project"] == "old-runtime"
        assert row["status"] == "working"
        assert row["display_phase"] == "Running bash"
        assert row["timeline_anchor_at"] is not None


def test_active_sessions_uses_runtime_anchor_for_old_runtime_only_session(tmp_path):
    factory = _make_db(tmp_path, "runtime_anchor_active.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime-active",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{old_runtime.id}",
                session_id=old_runtime.id,
                provider="claude",
                device_id="cinder",
                phase="thinking",
                phase_source="semantic",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=10),
                last_runtime_signal_at=now - timedelta(seconds=10),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=10),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=1),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == str(old_runtime.id)
        assert row["status"] == "working"
        assert row["presence_state"] == "thinking"
        assert row["display_phase"] == "Thinking"


def test_sessions_list_prefers_materialized_runtime_state_when_present(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_state.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=7),
            ended_at=now - timedelta(minutes=1),
            project="runtime-state",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(seconds=20),
                last_runtime_signal_at=now - timedelta(seconds=20),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=3,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "working"
        assert row["presence_state"] == "running"
        assert row["display_phase"] == "Running bash"
        assert row["runtime_phase"] == "running"
        assert row["runtime_source"] == "semantic"
        assert row["runtime_version"] == 3
        assert row["confidence"] == "live"


def test_sessions_list_keeps_progress_runtime_overlay_for_recent_closed_session(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_progress.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=2),
            ended_at=now - timedelta(minutes=1),
            project="runtime-progress",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="idle",
                phase_source="progress",
                active_tool=None,
                phase_started_at=now - timedelta(minutes=1),
                last_runtime_signal_at=None,
                last_progress_at=now - timedelta(seconds=20),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=20),
                freshness_expires_at=now - timedelta(seconds=1),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "active"
        assert row["display_phase"] == "Recent progress"
        assert row["runtime_phase"] is None
        assert row["runtime_source"] == "progress"
        assert row["presence_state"] is None
        assert row["last_live_at"] is not None
        assert row["confidence"] == "inferred"


def test_sessions_list_marks_materialized_needs_user_as_active_attention(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_needs_user.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=6),
            ended_at=None,
            project="runtime-needs-user",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="needs_user",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=30),
                last_runtime_signal_at=now - timedelta(seconds=30),
                last_progress_at=now - timedelta(seconds=25),
                last_live_at=now - timedelta(seconds=30),
                timeline_anchor_at=now - timedelta(seconds=25),
                freshness_expires_at=now + timedelta(minutes=10),
                terminal_state=None,
                terminal_at=None,
                runtime_version=4,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "active"
        assert row["presence_state"] == "needs_user"
        assert row["display_phase"] == "Needs you"
        assert row["runtime_phase"] == "needs_user"
        assert row["runtime_source"] == "managed_local_transport"
        assert row["confidence"] == "live"


def test_active_sessions_recent_progress_fallback_is_non_executing(tmp_path):
    factory = _make_db(tmp_path, "active_sessions_progress_fallback.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=1),
            ended_at=None,
            project="progress-fallback",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        row = next(item for item in rows if item["id"] == str(session.id))
        assert row["status"] == "active"
        assert row["presence_state"] is None
        assert row["display_phase"] == "Recent progress"
        assert row["runtime_phase"] == "idle"
        assert row["confidence"] == "inferred"


def test_sessions_surfaces_ignore_stale_presence_payload_after_newer_blocked_signal(tmp_path):
    factory = _make_db(tmp_path, "stale_presence_surface.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="stale-presence-surface",
        )
    finally:
        db.close()

    for client in _client(factory):
        blocked = client.post(
            "/agents/presence",
            json={
                "session_id": str(session.id),
                "state": "blocked",
                "tool_name": "Bash",
                "provider": "claude",
                "occurred_at": now.isoformat(),
                "dedupe_key": "blocked-new",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert blocked.status_code == 204, blocked.text

        stale_idle = client.post(
            "/agents/presence",
            json={
                "session_id": str(session.id),
                "state": "idle",
                "provider": "claude",
                "occurred_at": (now - timedelta(seconds=30)).isoformat(),
                "dedupe_key": "idle-old",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert stale_idle.status_code == 204, stale_idle.text

        active_resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert active_resp.status_code == 200, active_resp.text
        active_row = next(item for item in active_resp.json()["sessions"] if item["id"] == str(session.id))
        assert active_row["status"] == "active"
        assert active_row["presence_state"] == "blocked"
        assert active_row["display_phase"] == "Blocked on Bash"
        assert active_row["runtime_phase"] == "blocked"
        assert active_row["confidence"] == "live"

        list_resp = client.get("/agents/sessions?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert list_resp.status_code == 200, list_resp.text
        list_row = next(item for item in list_resp.json()["sessions"] if item["id"] == str(session.id))
        assert list_row["presence_state"] == "blocked"
        assert list_row["display_phase"] == "Blocked on Bash"
