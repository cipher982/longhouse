"""Tests for explicit signed session share links."""

from __future__ import annotations

import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "lh-share-tests-secret")
os.environ.setdefault("INTERNAL_API_SECRET", "lh-test-internal")
os.environ.setdefault("GOOGLE_CLIENT_ID", "lh-test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "lh-test-google-client")

import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.auth.session_tokens import _encode_jwt
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.session_share import SessionShare
from zerg.models.session_share import SessionShareEvent
from zerg.services.session_shares import SessionShareMisconfigured
from zerg.services.session_shares import create_session_share
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_shares.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int, email: str, display_name: str | None) -> User:
    user = User(id=user_id, email=email, display_name=display_name, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(db) -> str:
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="development",
        project="share-test",
        device_name="cinder",
        summary="Private implementation details stay out of public previews.",
        summary_title="Signed Share Test",
        cwd="/Users/example/git/zerg",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        ended_at=None,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    return str(session.id)


def _seed_session_input_owner(db, *, session_id: str, owner_id: int) -> None:
    db.add(
        SessionInput(
            session_id=session_id,
            owner_id=owner_id,
            body="shareable prompt",
            intent="auto",
            status="delivered",
        )
    )
    db.commit()


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {"sub": str(user_id), "exp": int(time.time()) + 300},
        auth_deps.JWT_SECRET,
    )


def _make_client(session_local) -> "TestClient":
    from fastapi.testclient import TestClient

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    from unittest.mock import patch

    return patch.object(auth_deps, "AUTH_DISABLED", False)


def _seed_users_and_session(session_local) -> str:
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        _seed_user(db, user_id=2, email="david@example.com", display_name="David Rose")
        _seed_user(db, user_id=3, email="other@example.com", display_name="Other")
        return _seed_session(db)


def _create_share(client, session_id: str, *, user_id: int = 2, note: str | None = "for review"):
    with _force_browser_jwt_mode():
        client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=user_id))
        return client.post(
            f"/timeline/sessions/{session_id}/shares",
            json={"note": note, "expires_in_days": 30},
        )


def test_create_share_returns_signed_url_and_stores_only_hash(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    client = _make_client(session_local)

    try:
        response = _create_share(client, session_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == session_id
        assert body["token"].startswith("lhshr_")
        assert body["share_url"] == f"/share/{body['token']}"
        assert body["sharer"] == {"id": 2, "display_name": "David Rose"}
        assert body["expires_at"] is not None

        with session_local() as db:
            share = db.query(SessionShare).one()
            assert share.token_hash != body["token"]
            assert len(share.token_hash) == 64
            assert share.note == "for review"
            events = db.query(SessionShareEvent).all()
            assert [event.event_type for event in events] == ["created"]
    finally:
        api_app.dependency_overrides.clear()


def test_create_share_requires_matching_input_owner_when_owner_signal_exists(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    with session_local() as db:
        _seed_session_input_owner(db, session_id=session_id, owner_id=2)
    client = _make_client(session_local)

    try:
        blocked = _create_share(client, session_id, user_id=3)
        assert blocked.status_code == 404, blocked.text

        allowed = _create_share(client, session_id, user_id=2)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["sharer"] == {"id": 2, "display_name": "David Rose"}
    finally:
        api_app.dependency_overrides.clear()


def test_create_share_fails_closed_when_signing_secret_is_weak(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)

    from unittest.mock import patch

    with session_local() as db:
        with patch.object(auth_deps, "JWT_SECRET", ""):
            with pytest.raises(SessionShareMisconfigured):
                create_session_share(db, session_id=session_id, created_by_user_id=2)


def test_public_preview_is_safe_and_resolve_audits_access(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    client = _make_client(session_local)

    try:
        created = _create_share(client, session_id, note="look at the launch path")
        assert created.status_code == 200, created.text
        token = created.json()["token"]

        preview = client.get(f"/public/session-shares/{token}/preview")
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["provider"] == "codex"
        assert preview_body["device_name"] == "cinder"
        assert preview_body["note"] == "look at the launch path"
        assert preview_body["sharer"] == {"id": 2, "display_name": "David Rose"}
        assert "session_id" not in preview_body
        assert "summary" not in preview_body
        assert "cwd" not in preview_body

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            resolved = client.get(f"/timeline/session-shares/{token}/resolve")
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["session_id"] == session_id

        with session_local() as db:
            share = db.query(SessionShare).one()
            assert share.access_count == 1
            assert share.last_accessed_at is not None
            events = db.query(SessionShareEvent).order_by(SessionShareEvent.id).all()
            assert [event.event_type for event in events] == ["created", "resolved"]
            assert events[1].actor_user_id == 1
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_share_token_resolves_sharer_and_hides_self(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    client = _make_client(session_local)

    try:
        created = _create_share(client, session_id, user_id=2)
        token = created.json()["token"]

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&share_token={token}")
        assert response.status_code == 200, response.text
        assert response.json()["session"]["sharer"] == {"id": 2, "display_name": "David Rose"}

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=2))
            self_response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&share_token={token}")
        assert self_response.status_code == 200, self_response.text
        assert self_response.json()["session"]["sharer"] is None

        with session_local() as db:
            # The landing-page resolve endpoint owns auditing. The workspace
            # param is attribution only, so polling does not inflate access_count.
            assert db.query(SessionShare).one().access_count == 0
    finally:
        api_app.dependency_overrides.clear()


def test_share_token_supersedes_legacy_shared_by(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    client = _make_client(session_local)

    try:
        created = _create_share(client, session_id, user_id=2)
        token = created.json()["token"]

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(
                f"/timeline/sessions/{session_id}/workspace?limit=10&share_token={token}&shared_by=3"
            )
        assert response.status_code == 200, response.text
        assert response.json()["session"]["sharer"] == {"id": 2, "display_name": "David Rose"}
    finally:
        api_app.dependency_overrides.clear()


def test_revoked_expired_and_tampered_share_links_are_rejected(tmp_path):
    session_local = _make_db(tmp_path)
    session_id = _seed_users_and_session(session_local)
    client = _make_client(session_local)

    try:
        created = _create_share(client, session_id, user_id=2)
        token = created.json()["token"]
        share_id = created.json()["id"]

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=3))
            forbidden = client.delete(f"/timeline/session-shares/{share_id}")
        assert forbidden.status_code == 404

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=2))
            revoked = client.delete(f"/timeline/session-shares/{share_id}")
        assert revoked.status_code == 200, revoked.text

        assert client.get(f"/public/session-shares/{token}/preview").status_code == 410
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            workspace = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&share_token={token}")
        assert workspace.status_code == 410

        expired = _create_share(client, session_id, user_id=2)
        expired_token = expired.json()["token"]
        with session_local() as db:
            share = db.query(SessionShare).filter(SessionShare.id == expired.json()["id"]).one()
            share.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        assert client.get(f"/public/session-shares/{expired_token}/preview").status_code == 410

        tampered_token = expired_token[:-1] + ("a" if expired_token[-1] != "a" else "b")
        assert client.get(f"/public/session-shares/{tampered_token}/preview").status_code == 404

        with session_local() as db:
            events = db.query(SessionShareEvent).order_by(SessionShareEvent.id).all()
            assert "revoked" in [event.event_type for event in events]
    finally:
        api_app.dependency_overrides.clear()
