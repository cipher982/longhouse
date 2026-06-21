"""Auth-enabled tests for the permission-gate endpoints.

These run WITHOUT AUTH_DISABLED so they exercise verify_agents_token + the
session-scoped enforcement: a managed-local hook token must match its session,
and a machine-wide durable device token must be rejected (it cannot be scoped to
one session). This is the security boundary that keeps one managed session from
registering/polling/resolving another session's permission requests.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ["JWT_SECRET"] = "test-jwt-secret-permission-auth"
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from zerg.auth.managed_local_hook_tokens import issue_managed_local_hook_token
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.enums import UserRole
from zerg.models.user import User


def _settings_override():
    # TESTING forces auth_disabled in real settings; this fake turns auth ON so
    # verify_agents_token actually validates the hook token + session scope.
    return type("S", (), {"auth_disabled": False, "testing": True, "single_tenant": True})()


@contextmanager
def _auth_enabled():
    with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
        yield


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'perm_auth.db'}")
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


def _seed(session_local):
    sid = uuid4()
    with session_local() as db:
        user = User(email=f"pa-{uuid4().hex[:6]}@t.local", role=UserRole.USER.value)
        db.add(user)
        db.flush()
        owner_id = user.id
        db.add(
            AgentSession(
                id=sid,
                provider="claude",
                environment="t",
                project="perm-auth",
                device_id="cinder",
                cwd="/tmp/perm-auth",
                started_at=datetime.now(timezone.utc),
                provider_session_id=f"claude-{uuid4().hex[:8]}",
                execution_home="managed_local",
            )
        )
        db.commit()
    return sid, owner_id


def _hook_token(owner_id: int, session_id) -> str:
    return issue_managed_local_hook_token(
        owner_id=owner_id, session_id=str(session_id), project="perm-auth", device_id="cinder"
    )


def _register(client, session_id, token, tool_use_id="toolu_auth"):
    return client.post(
        "/api/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        headers={"X-Agents-Token": token},
    )


def test_session_scoped_hook_token_can_register(tmp_path):
    session_local = _make_db(tmp_path)
    sid, owner_id = _seed(session_local)
    client, api_app = _make_client(session_local)
    try:
        with _auth_enabled():
            resp = _register(client, sid, _hook_token(owner_id, sid))
        assert resp.status_code == 200, resp.text
    finally:
        api_app.dependency_overrides.clear()


def test_missing_token_is_401(tmp_path):
    session_local = _make_db(tmp_path)
    sid, _ = _seed(session_local)
    client, api_app = _make_client(session_local)
    try:
        with _auth_enabled():
            resp = client.post(
                "/api/agents/permission-requests",
                json={"session_id": str(sid), "tool_use_id": "x", "tool_name": "Bash"},
            )
        assert resp.status_code == 401, resp.text
    finally:
        api_app.dependency_overrides.clear()


def test_hook_token_for_other_session_is_403_on_register(tmp_path):
    session_local = _make_db(tmp_path)
    sid_a, owner_id = _seed(session_local)
    sid_b, _ = _seed(session_local)
    client, api_app = _make_client(session_local)
    try:
        # Token bound to session A cannot register against session B.
        with _auth_enabled():
            resp = _register(client, sid_b, _hook_token(owner_id, sid_a))
        assert resp.status_code == 403, resp.text
    finally:
        api_app.dependency_overrides.clear()


def test_hook_token_for_other_session_is_403_on_poll(tmp_path):
    session_local = _make_db(tmp_path)
    sid_a, owner_id = _seed(session_local)
    sid_b, _ = _seed(session_local)
    client, api_app = _make_client(session_local)
    try:
        with _auth_enabled():
            resp = client.get(
                "/api/agents/permission-decision",
                params={"session_id": str(sid_b), "tool_use_id": "x"},
                headers={"X-Agents-Token": _hook_token(owner_id, sid_a)},
            )
        assert resp.status_code == 403, resp.text
    finally:
        api_app.dependency_overrides.clear()
