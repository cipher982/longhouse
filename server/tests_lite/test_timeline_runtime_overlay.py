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


def test_sessions_list_exposes_execution_home_from_existing_session_metadata(tmp_path):
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
        assert rows["legacy-local"]["execution_home"] == "legacy"
        assert rows["cloud-branch"]["id"] == str(cloud.id)
        assert rows["cloud-branch"]["execution_home"] == "cloud_takeover"


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


def test_sessions_list_reconciles_dead_managed_local_tmux_session(tmp_path, monkeypatch):
    factory = _make_db(tmp_path, "managed_local_tmux_dead_sessions.db")
    now = datetime.now(timezone.utc)

    class _FakeDispatcher:
        async def dispatch_job(self, **_kwargs):
            return {
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": "1\t0\tzsh",
                    "stderr": "",
                },
            }

    monkeypatch.setattr(
        "zerg.services.managed_local_runtime.get_runner_job_dispatcher",
        lambda: _FakeDispatcher(),
    )

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="managed-local-dead",
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="tmux",
            source_runner_id=1,
            managed_session_name="lh-managed-local-dead",
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
                phase_started_at=now - timedelta(seconds=20),
                last_runtime_signal_at=now - timedelta(seconds=20),
                last_progress_at=now - timedelta(seconds=18),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=18),
                freshness_expires_at=now + timedelta(minutes=10),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = next(item for item in resp.json()["sessions"] if item["id"] == str(session.id))
        assert row["status"] == "completed"
        assert row["display_phase"] == "Completed"
        assert row["runtime_phase"] == "finished"
        assert row["confidence"] == "stale"
        assert row["ended_at"] is not None

    db = factory()
    try:
        refreshed = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert refreshed.terminal_state == "finished"
        assert refreshed.phase == "finished"
    finally:
        db.close()


def test_active_sessions_reconciles_missing_managed_local_tmux_session(tmp_path, monkeypatch):
    factory = _make_db(tmp_path, "managed_local_tmux_missing_active.db")
    now = datetime.now(timezone.utc)

    class _FakeDispatcher:
        async def dispatch_job(self, **_kwargs):
            return {
                "ok": True,
                "data": {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "no server running",
                },
            }

    monkeypatch.setattr(
        "zerg.services.managed_local_runtime.get_runner_job_dispatcher",
        lambda: _FakeDispatcher(),
    )

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="managed-local-missing",
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="tmux",
            source_runner_id=1,
            managed_session_name="lh-managed-local-missing",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="thinking",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=10),
                last_runtime_signal_at=now - timedelta(seconds=10),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=10),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=5),
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
        row = next(item for item in resp.json()["sessions"] if item["id"] == str(session.id))
        assert row["status"] == "completed"
        assert row["display_phase"] == "Completed"
        assert row["runtime_phase"] == "finished"
        assert row["confidence"] == "stale"
