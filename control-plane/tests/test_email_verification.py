"""Tests for email verification flow in the control plane."""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set required env vars before importing app code
os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-for-tests")
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite:///")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger")

from control_plane.db import Base, get_db  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import User  # noqa: E402
from control_plane.routers.auth import (  # noqa: E402
    _decode_jwt,
    _encode_jwt,
    _hash_password,
    _issue_session_token,
    _issue_verify_token,
)
from control_plane.config import settings  # noqa: E402


@pytest.fixture()
def db_session(tmp_path):
    """Create an in-memory SQLite DB for each test."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    """TestClient with DB override."""

    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, base_url="https://control.longhouse.ai") as c:
        yield c
    app.dependency_overrides.clear()


def _create_user(db_session, email="test@example.com", verified=False, password="testpass123") -> User:
    user = User(
        email=email,
        password_hash=_hash_password(password),
        email_verified=verified,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login_cookie(user: User) -> dict[str, str]:
    token = _issue_session_token(user)
    return {"cp_session": token}


# ---------------------------------------------------------------------------
# Signup creates unverified user
# ---------------------------------------------------------------------------


class TestSignup:
    @patch("control_plane.routers.auth._send_verification")
    def test_signup_creates_unverified_user(self, mock_send, client, db_session):
        resp = client.post(
            "/auth/signup",
            data={"email": "new@example.com", "password": "password123", "password_confirm": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/verify-email" in resp.headers["location"]

        user = db_session.query(User).filter(User.email == "new@example.com").first()
        assert user is not None
        assert user.email_verified is False
        mock_send.assert_called_once_with(user)

    @patch("control_plane.routers.auth._send_verification")
    def test_signup_sets_session_cookie(self, mock_send, client, db_session):
        resp = client.post(
            "/auth/signup",
            data={"email": "new@example.com", "password": "password123", "password_confirm": "password123"},
            follow_redirects=False,
        )
        assert "cp_session" in resp.cookies


# ---------------------------------------------------------------------------
# Verification token round-trip
# ---------------------------------------------------------------------------


class TestVerifyToken:
    def test_verify_token_round_trip(self, client, db_session):
        user = _create_user(db_session, verified=False)
        token = _issue_verify_token(user)

        resp = client.get(f"/auth/verify?token={token}", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dashboard" in resp.headers["location"]
        assert "cp_session" in resp.cookies

        db_session.refresh(user)
        assert user.email_verified is True

    def test_expired_token_rejected(self, client, db_session):
        user = _create_user(db_session, verified=False)
        # Issue a token that's already expired
        expired_token = _encode_jwt(
            {"sub": str(user.id), "purpose": "email_verify", "exp": int(time.time()) - 10},
            settings.jwt_secret,
        )

        resp = client.get(f"/auth/verify?token={expired_token}", follow_redirects=False)
        assert resp.status_code == 302
        assert "expired" in resp.headers["location"].lower()

        db_session.refresh(user)
        assert user.email_verified is False

    def test_bad_token_rejected(self, client, db_session):
        resp = client.get("/auth/verify?token=garbage.token.here", follow_redirects=False)
        assert resp.status_code == 302
        assert "invalid" in resp.headers["location"].lower()

    def test_wrong_purpose_rejected(self, client, db_session):
        user = _create_user(db_session, verified=False)
        # Session token (no 'purpose' field) should not verify email
        session_token = _issue_session_token(user)

        resp = client.get(f"/auth/verify?token={session_token}", follow_redirects=False)
        assert resp.status_code == 302
        assert "invalid" in resp.headers["location"].lower()

        db_session.refresh(user)
        assert user.email_verified is False


# ---------------------------------------------------------------------------
# Checkout gating
# ---------------------------------------------------------------------------


class TestCheckoutGating:
    def test_unverified_user_blocked_from_dashboard_checkout(self, client, db_session):
        user = _create_user(db_session, verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/dashboard/checkout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/verify-email" in resp.headers["location"]

    def test_verified_user_can_reach_checkout(self, client, db_session):
        """Verified user hits the Stripe path (which will fail since Stripe isn't configured, but that's OK)."""
        user = _create_user(db_session, verified=True)
        client.cookies.update(_login_cookie(user))

        # This will either redirect to Stripe or return an error about Stripe not being configured
        # Either way, it should NOT redirect to /verify-email
        resp = client.post("/dashboard/checkout", follow_redirects=False)
        location = resp.headers.get("location", "")
        assert "/verify-email" not in location

    def test_billing_api_checkout_blocked_for_unverified(self, client, db_session):
        user = _create_user(db_session, verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/billing/checkout")
        assert resp.status_code == 403
        assert "not verified" in resp.json()["detail"].lower()

    def test_billing_api_checkout_allowed_for_verified(self, client, db_session):
        """Verified user can call billing checkout (will fail on Stripe config, but not on verification)."""
        user = _create_user(db_session, verified=True)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/billing/checkout")
        # Should fail with 503 (Stripe not configured) or 409 (already subscribed), NOT 403
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Resend verification
# ---------------------------------------------------------------------------


class TestResendVerification:
    @patch("control_plane.routers.auth._send_verification")
    def test_resend_works(self, mock_send, client, db_session):
        user = _create_user(db_session, verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/auth/resend-verification", follow_redirects=False)
        assert resp.status_code == 303
        assert "resent=1" in resp.headers["location"]
        mock_send.assert_called_once()

    @patch("control_plane.routers.auth._send_verification")
    def test_resend_skips_if_verified(self, mock_send, client, db_session):
        user = _create_user(db_session, verified=True)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/auth/resend-verification", follow_redirects=False)
        assert resp.status_code == 303
        assert "/dashboard" in resp.headers["location"]
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Dashboard redirect for unverified
# ---------------------------------------------------------------------------


class TestDashboardGating:
    def test_unverified_user_redirected_to_verify(self, client, db_session):
        user = _create_user(db_session, verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/verify-email" in resp.headers["location"]

    def test_verified_user_sees_dashboard(self, client, db_session):
        user = _create_user(db_session, verified=True)
        client.cookies.update(_login_cookie(user))

        resp = client.get("/dashboard", follow_redirects=False)
        # Should render 200 or redirect to provisioning/instance — not verify-email
        location = resp.headers.get("location", "")
        assert "/verify-email" not in location


# ---------------------------------------------------------------------------
# Google OAuth creates verified user
# ---------------------------------------------------------------------------


class TestGoogleOAuthVerified:
    @patch("control_plane.routers.auth._get_userinfo")
    @patch("control_plane.routers.auth._exchange_code")
    @patch("control_plane.routers.auth._require_oauth")
    def test_google_creates_verified_user(self, mock_req, mock_exchange, mock_userinfo, client, db_session):
        mock_exchange.return_value = {"access_token": "fake-token"}
        mock_userinfo.return_value = {"email": "google@example.com"}

        resp = client.get("/auth/google/callback?code=fakecode", follow_redirects=False)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.email == "google@example.com").first()
        assert user is not None
        assert user.email_verified is True

    @patch("control_plane.routers.auth._get_userinfo")
    @patch("control_plane.routers.auth._exchange_code")
    @patch("control_plane.routers.auth._require_oauth")
    def test_google_login_verifies_existing_unverified(self, mock_req, mock_exchange, mock_userinfo, client, db_session):
        # Create an unverified email user first
        user = _create_user(db_session, email="both@example.com", verified=False)

        mock_exchange.return_value = {"access_token": "fake-token"}
        mock_userinfo.return_value = {"email": "both@example.com"}

        resp = client.get("/auth/google/callback?code=fakecode", follow_redirects=False)
        assert resp.status_code == 302

        db_session.refresh(user)
        assert user.email_verified is True


# ---------------------------------------------------------------------------
# Verify-email page
# ---------------------------------------------------------------------------


class TestVerifyEmailPage:
    def test_unauthenticated_redirected_to_home(self, client):
        resp = client.get("/verify-email", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_verified_user_redirected_to_dashboard(self, client, db_session):
        user = _create_user(db_session, verified=True)
        client.cookies.update(_login_cookie(user))

        resp = client.get("/verify-email", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dashboard" in resp.headers["location"]

    def test_unverified_user_sees_page(self, client, db_session):
        user = _create_user(db_session, verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.get("/verify-email")
        assert resp.status_code == 200
        assert "Check Your Email" in resp.text
        assert user.email in resp.text
