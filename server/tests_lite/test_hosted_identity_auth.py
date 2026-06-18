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

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.session_tokens import _encode_jwt
from zerg.auth.strategy import HostedCPAuthStrategy
from zerg.database import Base
from zerg.dependencies import browser_auth
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.models import User
from zerg.routers.auth_sso import NativeHandoffRequest
from zerg.routers.auth_sso import accept_handoff_request
from zerg.routers.auth_sso import accept_native_handoff
from zerg.routers.auth_sso import refresh_runtime_token


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
async def test_accept_handoff_allows_code_only_control_plane_open_instance(monkeypatch, db_session):
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
        lambda: SimpleNamespace(
            control_plane_url="https://control.longhouse.ai",
            internal_api_secret="secret",
            auth_disabled=False,
            testing=False,
        ),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso._exchange_handoff_code", exchange)
    monkeypatch.setattr("zerg.dependencies.auth._get_strategy", lambda: Strategy())

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/accept-handoff",
            "headers": [],
            "query_string": b"code=one-use-code",
        }
    )

    redirect = await accept_handoff_request(request, "one-use-code", Response(), db=db_session)

    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/timeline"
    assert calls == {
        "control_plane_url": "https://control.longhouse.ai",
        "internal_api_secret": "secret",
        "code": "one-use-code",
        "tenant": "david010",
        "tenant_state": None,
    }


@pytest.mark.asyncio
async def test_accept_handoff_requires_cookie_for_tenant_state(monkeypatch, db_session):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/accept-handoff",
            "headers": [],
            "query_string": b"code=one-use-code&tenant_state=state-1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await accept_handoff_request(request, "one-use-code", Response(), tenant_state="state-1", db=db_session)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Missing login state"


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
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
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


def _refresh_request(*, auth_header: str | None):
    headers = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/refresh-runtime-token",
            "headers": headers,
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_refresh_runtime_token_proxies_bearer_to_cp(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"runtime_token": "cp.fresh.jwt", "expires_in": 3600}

    def fake_post(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)

    result = await refresh_runtime_token(_refresh_request(auth_header="Bearer cp.current.jwt"))

    assert result == {"runtime_token": "cp.fresh.jwt", "expires_in": 3600}
    assert captured["url"] == "https://control.longhouse.ai/api/identity/refresh-runtime-token"
    assert captured["headers"] == {"Authorization": "Bearer cp.current.jwt"}
    assert captured["timeout"] == 10.0


@pytest.mark.asyncio
async def test_refresh_runtime_token_rejects_missing_bearer(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    with pytest.raises(HTTPException) as exc:
        await refresh_runtime_token(_refresh_request(auth_header=None))
    assert exc.value.status_code == 401
    assert "Missing bearer" in exc.value.detail


@pytest.mark.asyncio
async def test_refresh_runtime_token_rejects_device_token(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    with pytest.raises(HTTPException) as exc:
        await refresh_runtime_token(_refresh_request(auth_header="Bearer zdt_abc"))
    assert exc.value.status_code == 401
    assert "Runtime token required" in exc.value.detail


@pytest.mark.asyncio
async def test_refresh_runtime_token_returns_404_when_not_hosted(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url=None),
    )
    with pytest.raises(HTTPException) as exc:
        await refresh_runtime_token(_refresh_request(auth_header="Bearer cp.jwt"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_refresh_runtime_token_propagates_cp_rejection(monkeypatch):
    class FakeResponse:
        status_code = 401
        def json(self):
            return {"detail": "Invalid or expired runtime token"}

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", lambda *a, **k: FakeResponse())

    with pytest.raises(HTTPException) as exc:
        await refresh_runtime_token(_refresh_request(auth_header="Bearer cp.expired.jwt"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_runtime_token_returns_502_on_cp_network_error(monkeypatch):
    def fake_post(*a, **k):
        raise httpx.HTTPError("connection refused")

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)

    with pytest.raises(HTTPException) as exc:
        await refresh_runtime_token(_refresh_request(auth_header="Bearer cp.jwt"))
    assert exc.value.status_code == 502
