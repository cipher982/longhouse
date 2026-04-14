"""Unit tests for the user-chosen subdomain picker feature.

Covers:
- _is_valid_subdomain() validation rules
- _derive_subdomain_from_email() collision handling
- GET /api/instances/subdomain-check endpoint (public)
- POST /onboarding/set-subdomain (CSRF, validation, storage)
- GET /onboarding/choose-subdomain (page renders, prefill)
- Dashboard redirect to picker for unpaid users
- Dashboard shows chosen URL once pending_subdomain is set
- Webhook: uses pending_subdomain, falls back to email, clears after use
- Webhook: race condition — pending slug claimed between set and webhook
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Env vars before any app import
os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-for-subdomain-tests")
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite:///")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger")

_mock_stripe = MagicMock()
_mock_stripe.error = MagicMock()
_mock_stripe.error.SignatureVerificationError = type("SignatureVerificationError", (Exception,), {})
sys.modules.setdefault("stripe", _mock_stripe)

from control_plane.db import Base, get_db  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import Instance, User  # noqa: E402
from control_plane.routers.auth import _hash_password, _issue_session_token  # noqa: E402
from control_plane.routers.instances import (  # noqa: E402
    RESERVED_SUBDOMAINS,
    _derive_subdomain_from_email,
    _is_valid_subdomain,
)
from control_plane.routers.ui import _csrf_token, _verify_csrf  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path):
    db_url = f"sqlite:///{tmp_path}/subdomain_test.db"
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


def _make_user(db, email="u@test.com", verified=True, pending=None) -> User:
    user = User(
        email=email,
        password_hash=_hash_password("pass1234"),
        email_verified=verified,
        pending_subdomain=pending,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_instance(db, user, subdomain="taken") -> Instance:
    inst = Instance(
        user_id=user.id,
        subdomain=subdomain,
        container_name=f"lh-{subdomain}",
        status="active",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _auth(user: User) -> dict:
    return {"cp_session": _issue_session_token(user)}


# ---------------------------------------------------------------------------
# _is_valid_subdomain
# ---------------------------------------------------------------------------


class TestIsValidSubdomain:
    def test_valid_simple(self):
        assert _is_valid_subdomain("myteam")

    def test_valid_with_numbers(self):
        assert _is_valid_subdomain("team42")

    def test_valid_with_hyphen(self):
        assert _is_valid_subdomain("my-team")

    def test_valid_max_length(self):
        assert _is_valid_subdomain("a" * 63)

    def test_too_short(self):
        assert not _is_valid_subdomain("ab")

    def test_too_long(self):
        assert not _is_valid_subdomain("a" * 64)

    def test_leading_hyphen(self):
        assert not _is_valid_subdomain("-myteam")

    def test_trailing_hyphen(self):
        assert not _is_valid_subdomain("myteam-")

    def test_uppercase_rejected(self):
        assert not _is_valid_subdomain("MyTeam")

    def test_spaces_rejected(self):
        assert not _is_valid_subdomain("my team")

    def test_dot_rejected(self):
        assert not _is_valid_subdomain("my.team")

    def test_reserved_admin(self):
        assert not _is_valid_subdomain("admin")

    def test_reserved_www(self):
        assert not _is_valid_subdomain("www")

    def test_reserved_api(self):
        assert not _is_valid_subdomain("api")

    def test_empty(self):
        assert not _is_valid_subdomain("")


# ---------------------------------------------------------------------------
# _derive_subdomain_from_email
# ---------------------------------------------------------------------------


class TestDeriveSubdomainFromEmail:
    def test_simple_email(self, db_session):
        result = _derive_subdomain_from_email("alice@example.com", db_session)
        assert result == "alice"

    def test_dots_become_hyphens(self, db_session):
        result = _derive_subdomain_from_email("john.doe@example.com", db_session)
        assert result == "john-doe"

    def test_collision_appends_counter(self, db_session):
        owner = _make_user(db_session, email="owner@x.com")
        _make_instance(db_session, owner, subdomain="alice")
        result = _derive_subdomain_from_email("alice@example.com", db_session)
        assert result == "alice-1"

    def test_double_collision(self, db_session):
        owner = _make_user(db_session, email="owner@x.com")
        _make_instance(db_session, owner, subdomain="alice")
        owner2 = _make_user(db_session, email="owner2@x.com")
        _make_instance(db_session, owner2, subdomain="alice-1")
        result = _derive_subdomain_from_email("alice@example.com", db_session)
        assert result == "alice-2"


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------


class TestCsrfHelpers:
    def test_token_verifies(self):
        tok = _csrf_token(user_id=1)
        assert _verify_csrf(1, tok)

    def test_wrong_user_rejected(self):
        tok = _csrf_token(user_id=1)
        assert not _verify_csrf(2, tok)

    def test_wrong_token_rejected(self):
        assert not _verify_csrf(1, "deadbeef" * 4)

    def test_yesterday_token_still_valid(self, monkeypatch):
        """Token from yesterday passes (handles midnight boundary)."""
        yesterday = int(time.time()) - 86400
        tok = _csrf_token.__wrapped__(1) if hasattr(_csrf_token, "__wrapped__") else None
        # Compute yesterday's token directly
        import hashlib
        day = int(time.time()) // 86400 - 1
        from control_plane.config import settings
        expected = hashlib.sha256(
            f"{settings.jwt_secret}:1:{day}".encode()
        ).hexdigest()[:32]
        assert _verify_csrf(1, expected)


# ---------------------------------------------------------------------------
# GET /api/instances/subdomain-check
# ---------------------------------------------------------------------------


class TestSubdomainCheckEndpoint:
    def test_available(self, client):
        resp = client.get("/api/instances/subdomain-check?subdomain=available-slug")
        assert resp.status_code == 200
        assert resp.json() == {"available": True, "reason": None}

    def test_taken(self, client, db_session):
        owner = _make_user(db_session, email="owner@t.com")
        _make_instance(db_session, owner, subdomain="taken-slug")
        resp = client.get("/api/instances/subdomain-check?subdomain=taken-slug")
        assert resp.json()["available"] is False
        assert resp.json()["reason"] == "taken"

    def test_reserved(self, client):
        resp = client.get("/api/instances/subdomain-check?subdomain=admin")
        assert resp.json()["available"] is False
        assert resp.json()["reason"] == "reserved"

    def test_invalid_too_short(self, client):
        resp = client.get("/api/instances/subdomain-check?subdomain=ab")
        assert resp.json()["available"] is False
        assert resp.json()["reason"] == "invalid"

    def test_invalid_leading_hyphen(self, client):
        resp = client.get("/api/instances/subdomain-check?subdomain=-bad")
        assert resp.json()["available"] is False

    def test_public_no_auth_required(self, client):
        """Endpoint must be accessible without a session cookie."""
        resp = client.get("/api/instances/subdomain-check?subdomain=publictest")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /onboarding/choose-subdomain
# ---------------------------------------------------------------------------


class TestChooseSubdomainPage:
    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/onboarding/choose-subdomain")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_unverified_redirects_to_verify(self, client, db_session):
        user = _make_user(db_session, email="unverified@t.com", verified=False)
        resp = client.get("/onboarding/choose-subdomain", cookies=_auth(user))
        assert resp.status_code == 302
        assert "verify-email" in resp.headers["location"]

    def test_verified_user_sees_picker(self, client, db_session):
        user = _make_user(db_session, email="fresh@t.com", verified=True)
        resp = client.get("/onboarding/choose-subdomain", cookies=_auth(user))
        assert resp.status_code == 200
        assert b"Choose your URL" in resp.content

    def test_prefills_pending_subdomain(self, client, db_session):
        user = _make_user(db_session, email="u@t.com", verified=True, pending="mypick")
        resp = client.get("/onboarding/choose-subdomain", cookies=_auth(user))
        assert resp.status_code == 200
        assert b"mypick" in resp.content

    def test_has_csrf_token_in_form(self, client, db_session):
        user = _make_user(db_session, email="csrf@t.com", verified=True)
        resp = client.get("/onboarding/choose-subdomain", cookies=_auth(user))
        assert b"csrf_token" in resp.content

    def test_user_with_active_instance_redirects_to_dashboard(self, client, db_session):
        user = _make_user(db_session, email="has-inst@t.com", verified=True)
        _make_instance(db_session, user, subdomain="existing-inst")
        resp = client.get("/onboarding/choose-subdomain", cookies=_auth(user))
        assert resp.status_code == 302
        assert "/dashboard" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /onboarding/set-subdomain
# ---------------------------------------------------------------------------


class TestSetSubdomainEndpoint:
    def _post(self, client, user, subdomain, csrf=None):
        tok = csrf if csrf is not None else _csrf_token(user.id)
        return client.post(
            "/onboarding/set-subdomain",
            data={"subdomain": subdomain, "csrf_token": tok},
            cookies=_auth(user),
        )

    def test_valid_slug_stored(self, client, db_session):
        user = _make_user(db_session, email="setter@t.com")
        resp = self._post(client, user, "myslug")
        assert resp.status_code == 303
        db_session.refresh(user)
        assert user.pending_subdomain == "myslug"

    def test_redirects_to_dashboard(self, client, db_session):
        user = _make_user(db_session, email="redir@t.com")
        resp = self._post(client, user, "validslug")
        assert "/dashboard" in resp.headers["location"]

    def test_invalid_slug_redirects_with_error(self, client, db_session):
        user = _make_user(db_session, email="invalid@t.com")
        resp = self._post(client, user, "ab")  # too short
        assert resp.status_code == 303
        assert "choose-subdomain" in resp.headers["location"]
        assert "error" in resp.headers["location"]

    def test_taken_slug_rejected(self, client, db_session):
        owner = _make_user(db_session, email="own@t.com")
        _make_instance(db_session, owner, subdomain="takenslug")
        user = _make_user(db_session, email="taker@t.com")
        resp = self._post(client, user, "takenslug")
        assert resp.status_code == 303
        assert "choose-subdomain" in resp.headers["location"]

    def test_reserved_slug_rejected(self, client, db_session):
        user = _make_user(db_session, email="res@t.com")
        resp = self._post(client, user, "admin")
        assert resp.status_code == 303
        assert "choose-subdomain" in resp.headers["location"]

    def test_bad_csrf_rejected(self, client, db_session):
        user = _make_user(db_session, email="bcsrf@t.com")
        resp = self._post(client, user, "goodslug", csrf="notavalidtoken1234567890123456")
        assert resp.status_code == 303
        assert "choose-subdomain" in resp.headers["location"]
        db_session.refresh(user)
        assert user.pending_subdomain is None

    def test_unauthenticated_redirects(self, client):
        resp = client.post(
            "/onboarding/set-subdomain",
            data={"subdomain": "whatever", "csrf_token": "x"},
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Dashboard redirect behaviour
# ---------------------------------------------------------------------------


class TestDashboardSubdomainRedirect:
    def test_unpaid_no_pending_redirects_to_picker(self, client, db_session):
        user = _make_user(db_session, email="nopending@t.com", verified=True)
        resp = client.get("/dashboard", cookies=_auth(user))
        assert resp.status_code == 302
        assert "choose-subdomain" in resp.headers["location"]

    def test_unpaid_with_pending_shows_dashboard(self, client, db_session):
        user = _make_user(db_session, email="haspending@t.com", verified=True, pending="mypick")
        resp = client.get("/dashboard", cookies=_auth(user))
        assert resp.status_code == 200
        assert b"mypick" in resp.content

    def test_dashboard_shows_change_link(self, client, db_session):
        user = _make_user(db_session, email="change@t.com", verified=True, pending="slug1")
        resp = client.get("/dashboard", cookies=_auth(user))
        assert resp.status_code == 200
        assert b"choose-subdomain" in resp.content  # "Change" link points there


# ---------------------------------------------------------------------------
# Webhook: pending_subdomain consumed at checkout
# Call _handle_checkout_completed directly to avoid the background reconciler.
# ---------------------------------------------------------------------------


class TestWebhookSubdomainConsumption:
    """Unit tests for _handle_checkout_completed: pending_subdomain lifecycle."""

    def _fire(self, db_session, user, mock_prov=None):
        from unittest.mock import patch, MagicMock
        from control_plane.routers.webhooks import _handle_checkout_completed
        from control_plane.services.provisioner import ProvisionResult

        if mock_prov is None:
            result = ProvisionResult(
                container_name=f"lh-{user.email.split('@')[0]}",
                data_path="/tmp/test",
                password="pass",
                password_hash="hash",
            )
            mock_prov = MagicMock(provision_instance=MagicMock(return_value=result))

        event_data = {
            "client_reference_id": str(user.id),
            "customer": "cus_test",
            "subscription": "sub_test",
        }
        with patch("control_plane.services.provisioner.Provisioner", return_value=mock_prov):
            _handle_checkout_completed(event_data, db_session)

    def test_uses_pending_subdomain(self, db_session):
        user = _make_user(db_session, email="webhook@t.com", pending="mychosen")
        self._fire(db_session, user)
        db_session.refresh(user)
        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.subdomain == "mychosen"

    def test_clears_pending_subdomain_after_use(self, db_session):
        user = _make_user(db_session, email="clear@t.com", pending="clearthis")
        self._fire(db_session, user)
        db_session.refresh(user)
        assert user.pending_subdomain is None

    def test_falls_back_to_email_when_no_pending(self, db_session):
        user = _make_user(db_session, email="fallback@t.com", pending=None)
        self._fire(db_session, user)
        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.subdomain == "fallback"

    def test_falls_back_when_pending_is_taken(self, db_session):
        """If pending slug was claimed by another tenant before webhook fires, fall back."""
        other = _make_user(db_session, email="other@t.com")
        _make_instance(db_session, other, subdomain="raced")
        user = _make_user(db_session, email="loser@t.com", pending="raced")
        self._fire(db_session, user)
        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.subdomain != "raced"

    def test_pending_cleared_even_on_provisioning_failure(self, db_session):
        """pending_subdomain must be None after webhook even if provisioner raises."""
        from unittest.mock import MagicMock
        user = _make_user(db_session, email="fail@t.com", pending="willbecleared")
        boom = MagicMock(provision_instance=MagicMock(side_effect=RuntimeError("boom")))
        self._fire(db_session, user, mock_prov=boom)
        db_session.refresh(user)
        assert user.pending_subdomain is None
        db_session.refresh(user)
        assert user.pending_subdomain is None
