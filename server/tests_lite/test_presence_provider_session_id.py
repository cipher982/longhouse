"""Managed presence payloads carry the provider-native session id.

The Claude lifecycle hook reports managed sessions under the Longhouse id and
now rides the native id along as `provider_session_id`. The presence endpoint
turns it into a binding_signal so the alias re-binds without waiting for a
transcript ship.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.services.session_kernel_projection import resolve_session_id_by_provider_session_id


@pytest.fixture()
def client_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/presence_native.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="presence-fixture", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    with TestClient(api_app) as c:
        yield c, SessionLocal
    api_app.dependency_overrides.clear()
    engine.dispose()


def _seed_session(SessionLocal, sid: str) -> None:
    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=sid,
                provider="claude",
                project="test-project",
                environment="test-machine",
                started_at=datetime.now(timezone.utc),
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
            )
        )
        db.commit()


def test_presence_with_provider_session_id_binds_alias(client_env):
    client, SessionLocal = client_env
    sid = str(uuid4())
    native_id = str(uuid4())
    _seed_session(SessionLocal, sid)

    resp = client.post(
        "/agents/presence",
        json={
            "session_id": sid,
            "state": "idle",
            "provider": "claude",
            "provider_session_id": native_id,
        },
        headers={"X-Agents-Token": "test-token"},
    )
    assert resp.status_code == 204

    with SessionLocal() as db:
        assert resolve_session_id_by_provider_session_id(db, native_id) == UUID(sid)


def test_presence_without_provider_session_id_still_accepts(client_env):
    client, SessionLocal = client_env
    sid = str(uuid4())
    _seed_session(SessionLocal, sid)

    resp = client.post(
        "/agents/presence",
        json={"session_id": sid, "state": "thinking", "provider": "claude"},
        headers={"X-Agents-Token": "test-token"},
    )
    assert resp.status_code == 204

    with SessionLocal() as db:
        assert resolve_session_id_by_provider_session_id(db, sid) is None


def test_presence_ignores_native_id_equal_to_session_id(client_env):
    """Shadow-shaped payloads (native == longhouse id) must not mint an alias."""
    client, SessionLocal = client_env
    sid = str(uuid4())
    _seed_session(SessionLocal, sid)

    resp = client.post(
        "/agents/presence",
        json={
            "session_id": sid,
            "state": "idle",
            "provider": "claude",
            "provider_session_id": sid,
        },
        headers={"X-Agents-Token": "test-token"},
    )
    assert resp.status_code == 204

    with SessionLocal() as db:
        assert resolve_session_id_by_provider_session_id(db, sid) is None
