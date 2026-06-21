from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.services.session_pause_requests import resolve_pause_request


def _make_db(tmp_path):
    db_path = tmp_path / "test_permission_gate_routes.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_local):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, backend="asyncio"), api_app


def _seed_session(session_local):
    session_id = uuid4()
    with session_local() as db:
        user = User(email=f"perm-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.flush()
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="Cinder",
                project="perm-gate",
                device_id="cinder",
                cwd="/tmp/perm-gate",
                started_at=datetime.now(timezone.utc),
                provider_session_id=f"claude-{uuid4().hex[:8]}",
                execution_home="managed_local",
            )
        )
        db.commit()
    return session_id


def test_register_then_poll_returns_decision_after_resolve(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        tool_use_id = "toolu_abc123"
        resp = client.post(
            "/api/agents/permission-requests",
            json={
                "session_id": str(session_id),
                "tool_use_id": tool_use_id,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
        )
        assert resp.status_code == 200, resp.text
        ack = resp.json()
        assert ack["status"] == "pending"
        request_key = ack["request_key"]

        # Before an answer, the hook poll sees pending (no decision yet).
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        )
        assert poll.status_code == 200, poll.text
        assert poll.json() == {"decision": None, "reason": None, "resolved": False}

        # The held request is stored as an answerable permission_prompt pause request.
        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.request_key == request_key).one()
            assert row.kind == "permission_prompt"
            assert row.can_respond is True
            assert row.provider_request_id == tool_use_id
            resolve_pause_request(
                db,
                request_key=request_key,
                status="resolved",
                response_payload={"permissionDecision": "allow", "permissionDecisionReason": "approved in test"},
                response_text="approved in test",
            )
            db.commit()

        # Now the hook poll returns the decision.
        poll2 = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        )
        assert poll2.status_code == 200, poll2.text
        assert poll2.json() == {"decision": "allow", "reason": "approved in test", "resolved": True}
    finally:
        api_app.dependency_overrides.clear()


def test_poll_unknown_tool_use_id_is_pending(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": "toolu_never_registered"},
        )
        assert poll.status_code == 200, poll.text
        assert poll.json() == {"decision": None, "reason": None, "resolved": False}
    finally:
        api_app.dependency_overrides.clear()


def test_register_unknown_session_is_404(tmp_path):
    session_local = _make_db(tmp_path)
    _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        resp = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(uuid4()), "tool_use_id": "toolu_x", "tool_name": "Bash"},
        )
        assert resp.status_code == 404, resp.text
    finally:
        api_app.dependency_overrides.clear()


def test_deny_resolution_maps_to_deny_decision(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        tool_use_id = "toolu_deny"
        ack = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        ).json()
        with session_local() as db:
            resolve_pause_request(db, request_key=ack["request_key"], status="rejected")
            db.commit()
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        )
        assert poll.json()["decision"] == "deny"
        assert poll.json()["resolved"] is True
    finally:
        api_app.dependency_overrides.clear()
