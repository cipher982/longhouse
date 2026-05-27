from __future__ import annotations

import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

import zerg.dependencies.agents_auth as agents_auth_deps
import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.auth.session_tokens import _encode_jwt
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTurn
from zerg.services.managed_local_transport import build_managed_local_attach_command


def _make_db(tmp_path):
    db_path = tmp_path / "test_timeline_api_auth_boundary.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1) -> User:
    user = User(id=user_id, email="owner@example.com", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_runner(db, *, runner_id: int, name: str, owner_id: int = 1):
    """Seed a Runner row so the post-cleanup property derivations
    (source_runner_id from device_id->Runner.name) can find a match."""
    from zerg.models.models import Runner

    runner = Runner(
        id=runner_id,
        owner_id=owner_id,
        name=name,
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="x",
        runner_metadata={},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return runner


def _seed_session(db) -> str:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="timeline-auth",
        device_id="dev-machine",
        cwd="/tmp/timeline-auth",
        git_repo=None,
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    return str(session.id)


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {
            "sub": str(user_id),
            "exp": int(time.time()) + 300,
        },
        auth_deps.JWT_SECRET,
    )


def _make_client(session_local) -> TestClient:
    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    return patch.object(auth_deps, "AUTH_DISABLED", False)


def _force_agents_token_mode():
    return patch.object(
        agents_auth_deps,
        "get_settings",
        return_value=type("S", (), {"auth_disabled": False})(),
    )


def test_timeline_sessions_accept_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/timeline/sessions")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["sessions"][0]["thread_id"] == session_id
        assert payload["sessions"][0]["head"]["project"] == "timeline-auth"
        assert payload["sessions"][0]["detail"]["project"] == "timeline-auth"
        assert "list_threads;dur=" in response.headers["server-timing"]
        assert "build_cards;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_filters_use_cache_control_and_cache_hit_timing(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            first = client.get("/timeline/filters?days_back=14")
            second = client.get("/timeline/filters?days_back=14")

        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, max-age=60"
        assert "distinct_filters;dur=" in first.headers["server-timing"]

        assert second.status_code == 200
        assert second.headers["cache-control"] == "private, max-age=60"
        assert "cache_hit;dur=" in second.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_managed_local_codex_app_server(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_runner(db, runner_id=9, name="cinder")
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=9,
            source_runner_name="cinder",
            managed_session_name="lh-codex-managed-local",
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["home_label"] == "On this Mac"
        # Codex managed control runs through the Machine Agent channel — no
        # remote-command Runner association regardless of seeded Runner row.
        assert payload["control"] == {
            "source_runner_id": None,
            "source_runner_name": "cinder",
            "attach_command": build_managed_local_attach_command(session=session),
        }
        assert "attach_command" not in payload
        assert "source_runner_name" not in payload
        assert "load_session;dur=" in response.headers["server-timing"]
        assert "build_response;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_native_claude_bridge(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_runner(db, runner_id=9, name="work-laptop")
        session = AgentSession(
            id=uuid4(),
            provider="claude",
            environment="development",
            project="timeline-auth",
            device_id="work-laptop",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            provider_session_id="provider-123",
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=9,
            source_runner_name="work-laptop",
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="claude_channel_bridge")
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["home_label"] == "On this Mac"
        assert payload["control"] == {
            "source_runner_id": 9,
            "source_runner_name": "work-laptop",
            "attach_command": build_managed_local_attach_command(session=session),
        }
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_turns_accept_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime(2026, 3, 22, 22, 0, tzinfo=timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        turn = SessionTurn(
            session_id=session_id,
            request_id="req-1",
            state="active",
            user_submitted_at=datetime(2026, 3, 22, 22, 3, 45, tzinfo=timezone.utc),
            send_accepted_at=datetime(2026, 3, 22, 22, 3, 46, tzinfo=timezone.utc),
            active_phase_observed_at=datetime(2026, 3, 22, 22, 3, 47, tzinfo=timezone.utc),
        )
        db.add(session)
        db.add(turn)
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/turns")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["turns"][0]["request_id"] == "req-1"
        assert payload["turns"][0]["state"] == "active"
        assert payload["turns"][0]["user_submitted_at"].endswith("Z")
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_events_anchor_tail_accepts_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime(2026, 3, 22, 22, 0, tzinfo=timezone.utc),
            ended_at=None,
            user_messages=5,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)
        for idx in range(1, 6):
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="user",
                    content_text=f"event {idx}",
                    timestamp=datetime(2026, 3, 22, 22, idx, tzinfo=timezone.utc),
                )
            )
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(
                f"/timeline/sessions/{session_id}/events",
                params={"limit": 2, "anchor": "tail", "branch_mode": "head"},
            )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 5
        assert [row["content_text"] for row in payload["events"]] == ["event 4", "event 5"]

        with _force_browser_jwt_mode():
            response = client.get(
                f"/timeline/sessions/{session_id}/events",
                params={"anchor": "middle"},
            )

        assert response.status_code == 400
        assert "anchor" in response.json()["detail"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_turn_detail_accepts_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime(2026, 3, 22, 22, 0, tzinfo=timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        turn = SessionTurn(
            session_id=session_id,
            request_id="req-1",
            state="active",
            user_submitted_at=datetime(2026, 3, 22, 22, 3, 45, tzinfo=timezone.utc),
            send_accepted_at=datetime(2026, 3, 22, 22, 3, 46, tzinfo=timezone.utc),
            active_phase_observed_at=datetime(2026, 3, 22, 22, 3, 47, tzinfo=timezone.utc),
        )
        db.add(session)
        db.add(turn)
        db.commit()
        db.refresh(turn)
        turn_id = turn.id

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/turns/{turn_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["turn"]["id"] == turn_id
        assert payload["turn"]["request_id"] == "req-1"
        assert payload["turn"]["state"] == "active"
        assert payload["turn"]["user_submitted_at"].endswith("Z")
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_workspace_bootstraps_session_thread_and_projection(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")

        assert response.status_code == 200
        payload = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert payload["session"]["id"] == session_id
        assert payload["thread"]["head_session_id"] == session_id
        assert payload["thread"]["sessions"][0]["id"] == session_id
        assert payload["projection"]["focus_session_id"] == session_id
        assert payload["projection"]["total"] == 0
        assert "load_thread;dur=" in response.headers["server-timing"]
        assert "load_projection;dur=" in response.headers["server-timing"]
        assert "build_projection;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_workspace_projects_claude_channel_display_text(tmp_path):
    session_local = _make_db(tmp_path)
    raw_text = '<channel source="longhouse">\ncontinue the migration\n</channel>'
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)
        event = AgentEvent(
            session_id=session_id,
            role="user",
            content_text=raw_text,
            timestamp=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        event_id = event.id

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")

        assert response.status_code == 200
        payload = response.json()
        events = [item["event"] for item in payload["projection"]["items"] if item["kind"] == "event"]
        assert events == [
            {
                "id": event_id,
                "role": "user",
                "content_text": "continue the migration",
                "raw_content_text": raw_text,
                "input_origin": {
                    "authored_via": "terminal",
                    "session_input_id": None,
                    "client_request_id": None,
                },
                "tool_name": None,
                "tool_input_json": None,
                "tool_output_text": None,
                "tool_output_truncated": False,
                "tool_output_original_chars": None,
                "tool_call_id": None,
                "timestamp": events[0]["timestamp"],
                "in_active_context": True,
                "branch_id": None,
                "is_head_branch": True,
                "event_origin": "durable",
                "provisional_state": None,
                "provisional_cursor": None,
                "provisional_complete": False,
                "reconciled_event_id": None,
                "tool_call_state": None,
            }
        ]

        with session_local() as db:
            stored = db.query(AgentEvent).filter(AgentEvent.id == event_id).one()
            assert stored.content_text == raw_text
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_mobile_tail_returns_compact_tail_and_detects_drift(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)
        base = datetime.now(timezone.utc)
        events = [
            AgentEvent(session_id=session_id, role="user", content_text="event 1", timestamp=base),
            AgentEvent(
                session_id=session_id, role="assistant", content_text="event 2", timestamp=base + timedelta(seconds=1)
            ),
            AgentEvent(
                session_id=session_id,
                role="tool",
                tool_name="Bash",
                tool_output_text="x" * 2500,
                timestamp=base + timedelta(seconds=2),
            ),
            AgentEvent(
                session_id=session_id, role="assistant", content_text="event 4", timestamp=base + timedelta(seconds=3)
            ),
        ]
        db.add_all(events)
        db.commit()
        event_ids = [event.id for event in events]

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/mobile-tail?limit=2")

        assert response.status_code == 200
        payload = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert "load_runtime;dur=" in response.headers["server-timing"]
        assert "runtime_state;dur=" in response.headers["server-timing"]
        assert "provisional_preview;dur=" in response.headers["server-timing"]
        assert "binding_overlay;dur=" in response.headers["server-timing"]
        assert "thread" not in payload
        assert payload["session"]["id"] == session_id
        assert payload["snapshot_event_id"] == event_ids[-1]
        assert payload["projection"]["total"] == 4
        assert payload["projection"]["page_offset"] == 2
        tail_events = [item["event"] for item in payload["projection"]["items"] if item["kind"] == "event"]
        assert [event["id"] for event in tail_events] == event_ids[-2:]
        assert len(tail_events[0]["tool_output_text"]) == 2000
        assert tail_events[0]["tool_output_truncated"] is True
        assert tail_events[0]["tool_output_original_chars"] == 2500

        with _force_browser_jwt_mode():
            response = client.get(
                f"/timeline/sessions/{session_id}/mobile-tail?limit=2&offset=2&snapshot_event_id={payload['snapshot_event_id']}"
            )

        assert response.status_code == 200
        older_events = [item["event"] for item in response.json()["projection"]["items"] if item["kind"] == "event"]
        assert [event["id"] for event in older_events] == event_ids[:2]

        with session_local() as db:
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="assistant",
                    content_text="event 5",
                    timestamp=base + timedelta(seconds=4),
                )
            )
            db.commit()

        with _force_browser_jwt_mode():
            response = client.get(
                f"/timeline/sessions/{session_id}/mobile-tail?limit=2&offset=2&snapshot_event_id={payload['snapshot_event_id']}"
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "projection_drift"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_mobile_tail_skips_provisional_preview_for_ended_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)
        base = datetime.now(timezone.utc)
        db.add(AgentEvent(session_id=session_id, role="assistant", content_text="done", timestamp=base))
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            with patch(
                "zerg.services.session_workspace.load_active_provisional_preview_map",
                side_effect=AssertionError("ended sessions should not scan provisional preview observations"),
            ):
                response = client.get(f"/timeline/sessions/{session_id}/mobile-tail?limit=1")

        assert response.status_code == 200
        assert response.json()["session"]["transcript_preview"] is None
        assert "provisional_preview;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_workspace_projects_longhouse_input_origin(tmp_path):
    session_local = _make_db(tmp_path)
    submitted_at = datetime.now(timezone.utc)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)
        event = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="sent from phone",
            timestamp=submitted_at,
        )
        db.add(event)
        db.flush()
        session_input = SessionInput(
            session_id=session_id,
            body="sent from phone",
            owner_id=1,
            intent="auto",
            status="delivered",
            client_request_id="ios-origin-1",
            delivery_request_id="delivery-origin-1",
            delivered_at=submitted_at,
        )
        db.add(session_input)
        db.flush()
        db.add(
            SessionTurn(
                session_id=session_id,
                request_id="delivery-origin-1",
                session_input_id=session_input.id,
                state="send_accepted",
                user_event_id=event.id,
                user_submitted_at=submitted_at,
                send_accepted_at=submitted_at,
            )
        )
        assistant_event = AgentEvent(
            session_id=session_id,
            role="assistant",
            content_text="ack",
            timestamp=submitted_at + timedelta(seconds=1),
        )
        db.add(assistant_event)
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")

        assert response.status_code == 200
        payload = response.json()
        events = [item["event"] for item in payload["projection"]["items"] if item["kind"] == "event"]
        assert len(events) == 2
        assert events[0]["input_origin"] == {
            "authored_via": "longhouse",
            "session_input_id": 1,
            "client_request_id": "ios-origin-1",
        }
        assert events[1]["input_origin"] is None
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_workspace_suppresses_origin_identity_off_head(tmp_path):
    session_local = _make_db(tmp_path)
    submitted_at = datetime.now(timezone.utc)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)
        old_branch = AgentSessionBranch(session_id=session_id, branch_reason="root", is_head=0)
        head_branch = AgentSessionBranch(session_id=session_id, branch_reason="rewrite", is_head=1)
        db.add_all([old_branch, head_branch])
        db.flush()
        old_event = AgentEvent(
            session_id=session_id,
            branch_id=old_branch.id,
            role="user",
            content_text="sent from phone",
            timestamp=submitted_at,
        )
        head_event = AgentEvent(
            session_id=session_id,
            branch_id=head_branch.id,
            role="assistant",
            content_text="new head",
            timestamp=submitted_at + timedelta(seconds=1),
        )
        db.add_all([old_event, head_event])
        db.flush()
        session_input = SessionInput(
            session_id=session_id,
            body="sent from phone",
            owner_id=1,
            intent="auto",
            status="delivered",
            client_request_id="ios-off-head-1",
            delivery_request_id="delivery-off-head-1",
            delivered_at=submitted_at,
        )
        db.add(session_input)
        db.flush()
        db.add(
            SessionTurn(
                session_id=session_id,
                request_id="delivery-off-head-1",
                session_input_id=session_input.id,
                state="send_accepted",
                user_event_id=old_event.id,
                user_submitted_at=submitted_at,
                send_accepted_at=submitted_at,
            )
        )
        old_event_id = int(old_event.id)
        head_event_id = int(head_event.id)
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?branch_mode=all&limit=50")

        assert response.status_code == 200
        events = [item["event"] for item in response.json()["projection"]["items"] if item["kind"] == "event"]
        old = next(event for event in events if event["id"] == old_event_id)
        head = next(event for event in events if event["id"] == head_event_id)
        assert old["is_head_branch"] is False
        assert old["input_origin"] is None
        assert head["is_head_branch"] is True
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_and_agents_session_workspace_return_same_body(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)

    client = _make_client(session_local)
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            timeline_response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")
            agents_response = client.get(f"/agents/sessions/{session_id}/workspace?limit=50")

        assert timeline_response.status_code == 200
        assert agents_response.status_code == 200
        assert timeline_response.json() == agents_response.json()
        assert timeline_response.headers["cache-control"] == "no-store"
        assert agents_response.headers["cache-control"] == "no-store"
        assert "load_projection;dur=" in timeline_response.headers["server-timing"]
        assert "load_projection;dur=" in agents_response.headers["server-timing"]
        assert "build_projection;dur=" in timeline_response.headers["server-timing"]
        assert "build_projection;dur=" in agents_response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_workspace_does_not_claim_live_control_without_runner_truth(tmp_path):
    session_local = _make_db(tmp_path)
    now = datetime.now(timezone.utc)
    with session_local() as db:
        _seed_user(db)
        session = AgentSession(
            id=uuid4(),
            provider="claude",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=now,
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=9,
            source_runner_name="cinder",
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(
            db, session, control_plane="claude_channel_bridge", state="detached"
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="idle",
                phase_source="semantic",
                phase_started_at=now,
                last_runtime_signal_at=now,
                last_progress_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now + timedelta(minutes=5),
                runtime_version=1,
            )
        )
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")

        assert response.status_code == 200
        capabilities = response.json()["session"]["capabilities"]
        assert capabilities["live_control_available"] is False
        assert capabilities["reply_to_live_session_available"] is False
        assert capabilities["can_queue_next_input"] is False
        assert capabilities["display_label"] == "Control offline"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_native_managed_local_codex(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_runner(db, runner_id=9, name="cinder")
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=9,
            source_runner_name="cinder",
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["home_label"] == "On this Mac"
        # Codex managed control runs through the Machine Agent channel — no
        # remote-command Runner association.
        assert payload["control"] == {
            "source_runner_id": None,
            "source_runner_name": "cinder",
            "attach_command": build_managed_local_attach_command(session=session),
        }
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_sessions_reject_agents_header_without_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get("/timeline/sessions", headers={"X-Agents-Token": "dev"})

        assert response.status_code == 401
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_browser_cookie_without_agents_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode(), _force_agents_token_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/agents/sessions")

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_bearer_device_token_without_agents_header(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"Authorization": "Bearer zdt_fake"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_legacy_non_device_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"X-Agents-Token": "legacy-token"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or revoked device token"
    finally:
        api_app.dependency_overrides.clear()


def test_timeline_sessions_clamps_oversized_limit(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        for _ in range(150):
            _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/timeline/sessions?limit=500")

        assert response.status_code == 200
        payload = response.json()
        assert len(payload["sessions"]) <= 100
        assert response.headers.get("X-Limit-Cap") == "100"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_sessions_summary_clamps_oversized_limit(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        for _ in range(150):
            _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/timeline/sessions/summary?limit=500")

        assert response.status_code == 200
        payload = response.json()
        assert len(payload["sessions"]) <= 100
        assert response.headers.get("X-Limit-Cap") == "100"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_turns_clamps_oversized_limit(tmp_path):
    from zerg.dependencies.browser_auth import get_current_browser_user

    session_local = _make_db(tmp_path)
    with session_local() as db:
        user = _seed_user(db)
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime(2026, 3, 22, 22, 0, tzinfo=timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add(session)
        for idx in range(120):
            db.add(
                SessionTurn(
                    session_id=session_id,
                    request_id=f"req-{idx}",
                    state="active",
                    user_submitted_at=datetime(2026, 3, 22, 22, 3, idx % 60, tzinfo=timezone.utc),
                )
            )
        db.commit()
        user_id = user.id

    client = _make_client(session_local)
    api_app.dependency_overrides[get_current_browser_user] = lambda: type(
        "U", (), {"id": user_id, "email": "owner@example.com", "role": "ADMIN"}
    )()

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/turns?limit=500")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload["turns"]) <= 100
        assert response.headers.get("X-Limit-Cap") == "100"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_sessions_stream_clamps_oversized_limit(tmp_path, monkeypatch):
    """The SSE stream endpoint must clamp limit and surface X-Limit-Cap.

    We don't actually consume the stream — we replace the generator with an
    immediately-completing async generator and verify the EventSourceResponse
    carries our header and that the underlying params were clamped.
    """
    from zerg.dependencies.browser_auth import get_current_browser_user_id_short_lived
    from zerg.dependencies.browser_auth import require_current_browser_user_short_lived

    session_local = _make_db(tmp_path)
    with session_local() as db:
        user = _seed_user(db)
        _seed_session(db)
        user_id = user.id

    client = _make_client(session_local)
    api_app.dependency_overrides[get_current_browser_user_id_short_lived] = lambda: user_id
    api_app.dependency_overrides[require_current_browser_user_short_lived] = lambda: None

    captured: dict = {}

    async def _fake_stream(request, *, session_factory, params, skip_initial_replay, owner_id=None):
        captured["limit"] = params.limit
        # Immediately end the stream so TestClient can return headers + close.
        if False:
            yield {}
        return

    import zerg.routers.timeline as timeline_router

    monkeypatch.setattr(timeline_router, "stream_timeline_sessions_for_browser", _fake_stream)

    try:
        with client.stream("GET", "/timeline/sessions/stream?limit=500") as response:
            assert response.status_code == 200
            assert response.headers.get("X-Limit-Cap") == "100"
            # Drain the (empty) stream so the context manager closes cleanly.
            for _ in response.iter_bytes():
                pass

        assert captured["limit"] == 100
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()
