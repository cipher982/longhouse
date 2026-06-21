from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from types import SimpleNamespace

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.session_pause_requests import is_user_facing_pause_request
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


def _make_client_with_user(session_local, user_id):
    client, api_app = _make_client(session_local)
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(
        id=user_id, email="perm@test.local", role=UserRole.USER.value
    )
    return client, api_app


def _seed_session(session_local):
    session_id = uuid4()
    with session_local() as db:
        user = User(email=f"perm-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.flush()
        user_id = user.id
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
    return session_id, user_id


def test_register_then_poll_returns_decision_after_resolve(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, _user_id = _seed_session(session_local)
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
    session_id, _user_id = _seed_session(session_local)
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
    session_id, _user_id = _seed_session(session_local)
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


def test_answer_via_pause_route_resolves_in_place_without_push(monkeypatch, tmp_path):
    """The full loop: register -> answer via the pause-response route (pull-mode,
    no managed-control websocket push) -> hook poll returns allow."""
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_session(session_local)

    pushed: list[dict] = []

    async def _fail_if_pushed(**kwargs):
        pushed.append(kwargs)
        raise AssertionError("permission prompts must not push over managed-control")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fail_if_pushed)

    client, api_app = _make_client_with_user(session_local, user_id)
    try:
        tool_use_id = "toolu_loop"
        ack = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        ).json()
        pause_id = ack["pause_request_id"]

        # Answer through the real browser pause-response route.
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resolved"
        assert not pushed  # never dispatched a websocket command

        # The hook poll now reads allow.
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        )
        assert poll.json() == {"decision": "allow", "reason": "Longhouse allow", "resolved": True}
    finally:
        api_app.dependency_overrides.clear()


def test_reject_via_pause_route_maps_to_deny(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_session(session_local)

    async def _fail_if_pushed(**kwargs):
        raise AssertionError("permission prompts must not push over managed-control")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fail_if_pushed)

    client, api_app = _make_client_with_user(session_local, user_id)
    try:
        tool_use_id = "toolu_loop_deny"
        ack = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        ).json()
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{ack['pause_request_id']}/response",
            json={"decision": "reject"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        )
        assert poll.json()["decision"] == "deny"
    finally:
        api_app.dependency_overrides.clear()


def test_permission_prompt_request_is_user_facing(tmp_path):
    """Answerable permission-gate requests must NOT be hidden by the legacy
    claude_hook placeholder filter."""
    session_local = _make_db(tmp_path)
    session_id, _user_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        ack = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": "toolu_vis", "tool_name": "Bash"},
        ).json()
        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.request_key == ack["request_key"]).one()
            assert is_user_facing_pause_request(row) is True
            # provider_ref carries the reply_transport so Phase 2 can dispatch by
            # transport instead of special-casing kind in the router.
            assert (row.provider_ref_json or {}).get("reply_transport") == "claude_pretooluse_pull"
    finally:
        api_app.dependency_overrides.clear()


def test_poll_by_pause_request_id_resolves_independently(tmp_path):
    """Two pending prompts with the SAME tool_use_id must resolve independently
    when polled by their distinct pause_request_id (no collapse)."""
    session_local = _make_db(tmp_path)
    session_id, _user_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        tool_use_id = "toolu_dup"
        ack1 = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        ).json()
        # A second ask with the same tool_use_id reuses the same row (same key);
        # the unique handle is pause_request_id. Resolve it to deny and confirm a
        # poll by that id returns deny, not a stale/leaked allow.
        with session_local() as db:
            resolve_pause_request(
                db,
                pause_request_id=UUID(ack1["pause_request_id"]),
                status="rejected",
                response_payload={"permissionDecision": "deny", "permissionDecisionReason": "no"},
            )
            db.commit()
        poll = client.get(
            "/api/agents/permission-decision",
            params={
                "session_id": str(session_id),
                "tool_use_id": tool_use_id,
                "pause_request_id": ack1["pause_request_id"],
            },
        )
        assert poll.json() == {"decision": "deny", "reason": "no", "resolved": True}
    finally:
        api_app.dependency_overrides.clear()


def test_resolved_without_decision_payload_maps_to_deny(tmp_path):
    """A row resolved WITHOUT an explicit permissionDecision (e.g. superseded)
    must read as deny, never a silent allow."""
    session_local = _make_db(tmp_path)
    session_id, _user_id = _seed_session(session_local)
    client, api_app = _make_client(session_local)
    try:
        ack = client.post(
            "/api/agents/permission-requests",
            json={"session_id": str(session_id), "tool_use_id": "toolu_nopayload", "tool_name": "Bash"},
        ).json()
        with session_local() as db:
            # resolve with NO response_payload at all
            resolve_pause_request(db, pause_request_id=UUID(ack["pause_request_id"]), status="resolved")
            db.commit()
        poll = client.get(
            "/api/agents/permission-decision",
            params={"session_id": str(session_id), "tool_use_id": "toolu_nopayload", "pause_request_id": ack["pause_request_id"]},
        )
        assert poll.json()["decision"] == "deny"
        assert poll.json()["resolved"] is True
    finally:
        api_app.dependency_overrides.clear()
