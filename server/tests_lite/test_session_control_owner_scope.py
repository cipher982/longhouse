"""A machine token may only load and steer sessions its owner actually owns.

Every `/api/agents/sessions/*` control route resolves the caller from the device
token and then loads the session. The load used to be unscoped, so any valid
device token on the host could send text to, interrupt, or terminate any other
user's live session, and could tell an existing session id from a made-up one.

These tests pin both halves: the ownership gate on the load, and the fact that a
non-owner's answer is identical to the answer for a session that never existed.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db  # noqa: E402
from zerg.database import initialize_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.models.user import User  # noqa: E402
from zerg.routers.device_tokens import hash_token  # noqa: E402
from zerg.services.session_chat_impl import _load_session_for_continuation  # noqa: E402
from zerg.services.session_hot_cards import upsert_timeline_card_from_session  # noqa: E402


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'owner_scope.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, email: str) -> User:
    user = User(email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_device_token(db, *, owner_id: int, device_id: str) -> str:
    # The dependency only looks up tokens carrying the real device prefix.
    token = f"zdt_{secrets.token_urlsafe(32)}"
    db.add(
        DeviceToken(
            id=uuid4(),
            owner_id=owner_id,
            device_id=device_id,
            token_hash=hash_token(token),
        )
    )
    db.commit()
    return token


def _seed_session(db, *, device_id: str) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="owner-scope",
        device_id=device_id,
        cwd="/tmp/owner-scope",
        git_repo=None,
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    db.refresh(session)
    return session


def _settings_override():
    return type("S", (), {"auth_disabled": False, "testing": True, "single_tenant": True})()


def _make_client(db):
    def override_db():
        yield db

    api_app.dependency_overrides[get_db] = override_db
    return TestClient(api_app)


def test_session_load_is_scoped_to_the_owner_that_holds_the_device(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db, email="owner@owner-scope.test")
        intruder = _seed_user(db, email="intruder@owner-scope.test")
        session = _seed_session(db, device_id="owner-laptop")
        _seed_device_token(db, owner_id=owner.id, device_id="owner-laptop")
        _seed_device_token(db, owner_id=intruder.id, device_id="intruder-laptop")

        loaded = _load_session_for_continuation(db, str(session.id), owner_id=owner.id)
        assert str(loaded.id) == str(session.id)

        unknown_session = uuid4()
        with pytest.raises(HTTPException) as intruder_error:
            _load_session_for_continuation(db, str(session.id), owner_id=intruder.id)
        with pytest.raises(HTTPException) as missing_error:
            _load_session_for_continuation(db, str(unknown_session), owner_id=intruder.id)

        # The non-owner's answer must not be distinguishable from the answer
        # for a session id that was never issued to anybody: same status, and
        # the same sentence with only the id the caller already typed in it.
        assert intruder_error.value.status_code == 404
        assert missing_error.value.status_code == 404
        assert intruder_error.value.detail == f"Session {session.id} not found"
        assert missing_error.value.detail == f"Session {unknown_session} not found"


def test_device_token_cannot_steer_a_session_it_does_not_own(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db, email="owner@owner-scope.test")
        intruder = _seed_user(db, email="intruder@owner-scope.test")
        session = _seed_session(db, device_id="owner-laptop")
        _seed_device_token(db, owner_id=owner.id, device_id="owner-laptop")
        intruder_token = _seed_device_token(db, owner_id=intruder.id, device_id="intruder-laptop")

        client = _make_client(db)
        headers = {"X-Agents-Token": intruder_token}
        unknown_session = str(uuid4())
        try:
            with (
                patch("zerg.dependencies.agents_auth.get_settings", _settings_override),
                # Token validation opens its own short-lived session rather than
                # the request's, so point that seam at this test's database and
                # keep the real hash lookup in the path.
                patch("zerg.dependencies.agents_auth.get_session_factory", return_value=session_local),
            ):
                send = client.post(
                    f"/agents/sessions/{session.id}/send-live",
                    json={"message": "whoami"},
                    headers=headers,
                )
                interrupt = client.post(f"/agents/sessions/{session.id}/interrupt-live", headers=headers)
                terminate = client.post(f"/agents/sessions/{session.id}/terminate-live", headers=headers)
                pauses = client.get(f"/agents/sessions/{session.id}/pause-requests", headers=headers)
                send_unknown = client.post(
                    f"/agents/sessions/{unknown_session}/send-live",
                    json={"message": "whoami"},
                    headers=headers,
                )

            for response in (send, interrupt, terminate, pauses):
                assert response.status_code == 404, response.text

            # An existing session the caller does not own answers exactly like a
            # session id that does not exist, so the route cannot be used to
            # enumerate ids on a shared Runtime Host.
            assert send_unknown.status_code == 404, send_unknown.text
            assert send.json()["detail"] == f"Session {session.id} not found"
            assert send_unknown.json()["detail"] == f"Session {unknown_session} not found"
        finally:
            api_app.dependency_overrides.clear()
