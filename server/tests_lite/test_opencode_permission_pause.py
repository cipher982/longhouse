"""The OpenCode plugin emits a pause_request runtime event for permission.asked;
the server must ingest it as an answerable permission_prompt pause request with
the managed-push reply transport (so Phase 2 dispatch pushes the answer back via
the bridge)."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-opencode-perm")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.services.session_pause_requests import is_pull_reply_transport
from zerg.services.session_pause_requests import is_user_facing_pause_request
from zerg.services.session_pause_requests import load_active_pause_request_for_session
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'opencode_perm.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(db):
    session = AgentSession(
        provider="opencode",
        environment="test",
        project="opencode-perm",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
    )
    db.add(session)
    db.flush()
    db.refresh(session)
    return session


def test_opencode_permission_asked_becomes_answerable_push_pause_request(tmp_path):
    SF = _make_db(tmp_path)
    with SF() as db:
        session = _seed(db)
        runtime_key = f"opencode:{session.id}"
        # Mirror exactly what the embedded opencode plugin emits for permission.asked.
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="opencode",
                    device_id="cinder",
                    source="opencode_event",
                    kind="pause_request",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key="oc-perm-1",
                    payload={
                        "request_id": "perm-abc",
                        "provider_request_id": "perm-abc",
                        "kind": "permission_prompt",
                        "can_respond": True,
                        "provider_ref": {
                            "source": "opencode_bridge",
                            "reply_transport": "managed_push",
                            "opencode_request_id": "perm-abc",
                        },
                        "tool_name": "bash",
                        "title": "Permission: bash",
                        "summary": "OpenCode wants to use bash",
                    },
                )
            ],
        )
        db.commit()

        row = load_active_pause_request_for_session(db, session.id)
        assert row is not None
        assert row.kind == "permission_prompt"
        assert row.can_respond is True
        assert row.provider_request_id == "perm-abc"
        assert is_user_facing_pause_request(row) is True
        # OpenCode answers PUSH over the bridge — must NOT resolve in place.
        assert is_pull_reply_transport(row) is False
        assert (row.provider_ref_json or {}).get("reply_transport") == "managed_push"


def _seed_opencode_session(db, *, request_id="perm-xyz"):
    from uuid import uuid4

    from zerg.models.enums import UserRole
    from zerg.models.user import User
    from zerg.services.session_pause_requests import upsert_pause_request
    from zerg.services.session_runtime import runtime_key_for_session

    user = User(email=f"oc-{uuid4().hex[:6]}@t.local", role=UserRole.USER.value)
    db.add(user)
    db.flush()
    sid = uuid4()
    db.add(
        AgentSession(
            id=sid,
            provider="opencode",
            environment="test",
            project="oc-perm",
            device_id="cinder",
            cwd="/tmp/oc",
            started_at=datetime.now(timezone.utc),
        )
    )
    rk = runtime_key_for_session("opencode", str(sid))
    row, _ = upsert_pause_request(
        db,
        session_id=sid,
        runtime_key=rk,
        provider="opencode",
        request_key=f"opencode:{rk}:{request_id}",
        occurred_at=datetime.now(timezone.utc),
        provider_request_id=request_id,
        provider_ref={"source": "opencode_bridge", "reply_transport": "managed_push", "opencode_request_id": request_id},
        kind="permission_prompt",
        can_respond=True,
    )
    db.commit()
    return sid, user.id, row.id


def test_opencode_permission_answer_pushes_via_bridge(monkeypatch, tmp_path):
    """Answering an opencode permission prompt delivers the decision through the
    bridge permission_reply (not the engine websocket) and resolves the row."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from zerg.cli import opencode_bridge
    from zerg.database import get_db
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.browser_route_auth import get_current_browser_route_user
    from zerg.models.enums import UserRole

    SF = _make_db(tmp_path)
    with SF() as db:
        sid, owner_id, pause_id = _seed_opencode_session(db)

    calls: list[dict] = []

    def _fake_reply(*, session_id, request_id, decision, state_root, config_dir, wait_secs):
        calls.append({"session_id": session_id, "request_id": request_id, "decision": decision})

    monkeypatch.setattr(opencode_bridge, "permission_reply", _fake_reply)

    from zerg.main import api_app
    from zerg.main import app

    def _override_db():
        db = SF()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(
        id=owner_id, email="oc@test.local", role=UserRole.USER.value
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    client = TestClient(app, backend="asyncio")
    try:
        resp = client.post(
            f"/api/sessions/{sid}/pause-requests/{pause_id}/response",
            json={"decision": "answer"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resolved"
        # The decision went out over the bridge as allow.
        assert calls == [{"session_id": str(sid), "request_id": "perm-xyz", "decision": "allow"}]
    finally:
        api_app.dependency_overrides.clear()


def test_opencode_permission_deny_pushes_deny(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from zerg.cli import opencode_bridge
    from zerg.database import get_db
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.browser_route_auth import get_current_browser_route_user
    from zerg.models.enums import UserRole

    SF = _make_db(tmp_path)
    with SF() as db:
        sid, owner_id, pause_id = _seed_opencode_session(db, request_id="perm-deny")

    calls: list[dict] = []
    monkeypatch.setattr(
        opencode_bridge,
        "permission_reply",
        lambda **kw: calls.append({"request_id": kw["request_id"], "decision": kw["decision"]}),
    )

    from zerg.main import api_app
    from zerg.main import app

    def _override_db():
        db = SF()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(
        id=owner_id, email="oc@test.local", role=UserRole.USER.value
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    client = TestClient(app, backend="asyncio")
    try:
        resp = client.post(
            f"/api/sessions/{sid}/pause-requests/{pause_id}/response",
            json={"decision": "reject"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        assert calls == [{"request_id": "perm-deny", "decision": "deny"}]
    finally:
        api_app.dependency_overrides.clear()
