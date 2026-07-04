"""Route-level tests for Gmail connector bootstrap state."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _encode_jwt
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models import Connector
from zerg.models import User
from zerg.utils.crypto import decrypt


def _make_db(tmp_path):
    db_path = tmp_path / "test_auth_gmail_connect.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1, email: str = "owner@example.com") -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_hosted_gmail_handoff_bootstraps_connector(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
        single_tenant=True,
        gmail_pubsub_topic="projects/demo/topics/gmail",
        app_public_url=None,
    )
    runtime_claims = SimpleNamespace(
        cp_user_id=42,
        email="owner@example.com",
        email_verified=True,
        display_name="Owner",
        avatar_url="https://example.test/avatar.png",
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
            patch("zerg.routers.auth_gmail.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.hosted_instance_id", return_value="hosted-owner"),
            patch("zerg.routers.auth_internal.verify_runtime_token", return_value=runtime_claims) as verify_token,
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "Owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", return_value={"history_id": 321, "watch_expiry": 654321}),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={
                    "refresh_token": "refresh-token",
                    "runtime_token": "cp.runtime.jwt",
                },
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        verify_token.assert_called_once_with("cp.runtime.jwt", audience="hosted-owner")
        assert response.status_code == 200
        assert response.json()["mailbox_email"] == "owner@gmail.com"
        assert response.json()["watch"]["status"] == "active"

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert connector.config["watch_status"] == "active"
            assert decrypt(connector.config["refresh_token"]) == "refresh-token"
            user = db.query(User).filter(User.email == "owner@example.com").one()
            assert user.cp_user_id == 42
            assert user.provider_user_id == "42"
    finally:
        api_app.dependency_overrides.clear()


def test_hosted_gmail_handoff_creates_cp_linked_user(tmp_path):
    session_local = _make_db(tmp_path)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
        single_tenant=False,
        gmail_pubsub_topic="projects/demo/topics/gmail",
        app_public_url=None,
    )
    runtime_claims = SimpleNamespace(
        cp_user_id=42,
        email="owner@example.com",
        email_verified=True,
        display_name="Owner",
        avatar_url="https://example.test/avatar.png",
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
            patch("zerg.routers.auth_gmail.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.hosted_instance_id", return_value="hosted-owner"),
            patch("zerg.routers.auth_internal.verify_runtime_token", return_value=runtime_claims),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "Owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", return_value={"history_id": 321, "watch_expiry": 654321}),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={
                    "refresh_token": "refresh-token",
                    "runtime_token": "cp.runtime.jwt",
                },
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        assert response.status_code == 200
        assert response.json()["watch"]["status"] == "active"

        with session_local() as db:
            user = db.query(User).filter(User.email == "owner@example.com").one()
            assert user.cp_user_id == 42
            assert user.provider == "control-plane"
            assert user.provider_user_id == "42"
            assert user.display_name == "Owner"
            assert user.avatar_url == "https://example.test/avatar.png"
            connector = db.query(Connector).filter(Connector.owner_id == user.id).one()
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert decrypt(connector.config["refresh_token"]) == "refresh-token"
    finally:
        api_app.dependency_overrides.clear()


def test_hosted_gmail_handoff_rejects_missing_identity_token(tmp_path):
    session_local = _make_db(tmp_path)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={"refresh_token": "refresh-token"},
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        assert response.status_code == 422
        assert response.json()["detail"] == "runtime_token must be provided"
    finally:
        api_app.dependency_overrides.clear()


def test_hosted_gmail_handoff_rejects_cp_user_id_conflict(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        user = _seed_user(db)
        user.cp_user_id = 7
        db.commit()

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
        single_tenant=True,
        gmail_pubsub_topic="projects/demo/topics/gmail",
        app_public_url=None,
    )
    runtime_claims = SimpleNamespace(
        cp_user_id=42,
        email="owner@example.com",
        email_verified=True,
        display_name="Owner",
        avatar_url=None,
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.hosted_instance_id", return_value="hosted-owner"),
            patch("zerg.routers.auth_internal.verify_runtime_token", return_value=runtime_claims),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={
                    "refresh_token": "refresh-token",
                    "runtime_token": "cp.runtime.jwt",
                },
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "Control-plane account does not match this tenant user."
    finally:
        api_app.dependency_overrides.clear()


def test_hosted_gmail_handoff_accepts_legacy_jwt_during_rollout(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
        single_tenant=True,
        gmail_pubsub_topic="projects/demo/topics/gmail",
        app_public_url=None,
    )
    handoff_token = _encode_jwt(
        {
            "sub": "owner@example.com",
            "email": "owner@example.com",
            "instance": "hosted-owner",
            "purpose": "hosted_gmail_connect_handoff",
            "exp": int(time.time()) + 300,
        },
        JWT_SECRET,
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
            patch("zerg.routers.auth_gmail.get_settings", return_value=settings),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "Owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", return_value={"history_id": 321, "watch_expiry": 654321}),
            patch.dict(os.environ, {"INSTANCE_ID": "hosted-owner"}, clear=False),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={
                    "refresh_token": "refresh-token",
                    "handoff_token": handoff_token,
                },
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        assert response.status_code == 200
        assert response.json()["mailbox_email"] == "owner@gmail.com"
        assert response.json()["watch"]["status"] == "active"

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert connector.config["watch_status"] == "active"
            assert decrypt(connector.config["refresh_token"]) == "refresh-token"
    finally:
        api_app.dependency_overrides.clear()
