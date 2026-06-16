from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.session_tokens import _encode_jwt
from zerg.auth.strategy import HostedCPAuthStrategy
from zerg.database import Base
from zerg.dependencies import browser_auth
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.models import User
from zerg.routers.auth_sso import NativeHandoffRequest
from zerg.routers.auth_sso import accept_native_handoff


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _claims(*, cp_user_id: int, email: str, email_verified: bool = True) -> CPTokenClaims:
    return CPTokenClaims(
        cp_user_id=cp_user_id,
        email=email,
        email_verified=email_verified,
        display_name="CP User",
        avatar_url=None,
        audience="david010",
        issuer="https://control.longhouse.ai",
        expires_at=9999999999,
    )


def test_verified_cp_email_can_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="david010@gmail.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.cp_user_id == 123
    assert resolved.provider == "control-plane"
    assert resolved.email_verified is True


def test_unverified_cp_email_cannot_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        strategy._resolve_claims_user(  # noqa: SLF001
            db_session,
            _claims(cp_user_id=456, email="david010@gmail.com", email_verified=False),
        )

    assert exc.value.status_code == 403
    db_session.refresh(user)
    assert user.cp_user_id is None


def test_hosted_browser_route_rejects_query_jwt(monkeypatch, db_session):
    monkeypatch.setattr(
        "zerg.dependencies.browser_route_auth.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})

    with pytest.raises(HTTPException) as exc:
        get_current_browser_route_user(request, db_session, token="header.payload.signature")

    assert exc.value.status_code == 401


def test_hosted_browser_auth_accepts_runtime_bearer(monkeypatch, db_session):
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    class Strategy:
        def validate_ws_token(self, token, db):
            assert token == "cp.runtime.jwt"
            return user

    monkeypatch.setattr(browser_auth, "get_settings", lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"))
    monkeypatch.setattr(browser_auth.auth_deps, "AUTH_DISABLED", False)
    monkeypatch.setattr(browser_auth.auth_deps, "_get_strategy", lambda: Strategy())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/verify",
            "headers": [(b"authorization", b"Bearer cp.runtime.jwt")],
            "query_string": b"",
        }
    )

    assert get_current_browser_user(request, db_session).id == user.id


def test_hosted_browser_auth_rejects_legacy_jwt_bearer(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    legacy_token = _encode_jwt(
        {
            "sub": str(user.id),
            "email": user.email,
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
        },
        "test-jwt-secret-1234",
    )

    monkeypatch.setattr(browser_auth, "get_settings", lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"))
    monkeypatch.setattr(browser_auth.auth_deps, "AUTH_DISABLED", False)
    monkeypatch.setattr(browser_auth.auth_deps, "_get_strategy", lambda: HostedCPAuthStrategy())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/verify",
            "headers": [(b"authorization", f"Bearer {legacy_token}".encode())],
            "query_string": b"",
        }
    )

    with pytest.raises(HTTPException) as exc:
        get_current_browser_user(request, db_session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_accept_native_handoff_exchanges_one_use_code(monkeypatch, db_session):
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    calls = {}

    def exchange(**kwargs):
        calls.update(kwargs)
        return ("cp.runtime.jwt", 3600)

    class Strategy:
        def validate_ws_token(self, token, db):
            assert token == "cp.runtime.jwt"
            return user

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso._hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso._exchange_handoff_code", exchange)
    monkeypatch.setattr("zerg.dependencies.auth._get_strategy", lambda: Strategy())

    result = await accept_native_handoff(NativeHandoffRequest(code="one-use-code", tenant_state="verifier"), db_session)

    assert result == {"runtime_token": "cp.runtime.jwt", "expires_in": 3600}
    assert calls == {
        "control_plane_url": "https://control.longhouse.ai",
        "internal_api_secret": "secret",
        "code": "one-use-code",
        "tenant": "david010",
        "tenant_state": "verifier",
    }
