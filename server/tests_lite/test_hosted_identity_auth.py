from __future__ import annotations

import json
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
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from zerg.auth import cp_jwks
from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.hosted import new_tenant_login_state
from zerg.auth.hosted import tenant_cookie_secure
from zerg.auth.hosted import tenant_login_cookie_name
from zerg.auth.hosted import tenant_login_cookie_secret
from zerg.auth.session_tokens import _encode_jwt
from zerg.auth.strategy import HostedCPAuthStrategy
from zerg.database import Base
from zerg.dependencies import browser_auth
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.dependencies.form_post_origin import require_browser_auth_header
from zerg.models.models import User
from zerg.routers import auth_browser
from zerg.routers import auth_sso
from zerg.routers.auth_browser import logout
from zerg.routers.auth_browser import refresh_session
from zerg.routers.auth_browser import start_handoff
from zerg.routers.auth_sso import NativeHandoffRequest
from zerg.routers.auth_sso import NativeRefreshRequest
from zerg.routers.auth_sso import NativeRevokeRequest
from zerg.routers.auth_sso import _runtime_payload
from zerg.routers.auth_sso import accept_handoff_request
from zerg.routers.auth_sso import accept_native_handoff
from zerg.routers.auth_sso import refresh_native_session
from zerg.routers.auth_sso import refresh_runtime_token
from zerg.routers.auth_sso import revoke_native_session


def test_tenant_login_state_matches_control_plane_opaque_grammar():
    state, cookie_name, secret = new_tenant_login_state(secure=True)

    assert len(state) <= 128
    assert state.count("-") >= 1
    assert "." not in state
    assert all(character.isalnum() or character in "_-" for character in state)
    assert tenant_login_cookie_name(state, secure=True) == cookie_name
    assert tenant_login_cookie_secret(state) == secret
    assert tenant_login_cookie_name(f"{state}.legacy", secure=True) is None


def test_hosted_cookie_policy_never_downgrades_public_tenants(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_COOKIE_SECURE", "0")
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        control_plane_url="https://control.longhouse.ai",
        public_site_url="http://david010.longhouse.ai",
        app_public_url=None,
    )

    assert tenant_cookie_secure(settings) is True


def test_self_host_http_cookie_policy_is_local_only(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_COOKIE_SECURE", raising=False)
    local_settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        control_plane_url=None,
        public_site_url="http://127.0.0.1:8000",
        app_public_url=None,
    )
    public_settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        control_plane_url=None,
        public_site_url="http://example.test",
        app_public_url=None,
    )

    assert tenant_cookie_secure(local_settings) is False
    assert tenant_cookie_secure(public_settings) is True


def test_native_handoff_rate_limit_binds_untrusted_attempts_to_ip(monkeypatch):
    monkeypatch.setattr(auth_sso, "_HANDOFF_RATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(auth_sso, "_NATIVE_HANDOFF_IP_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(auth_sso, "_NATIVE_HANDOFF_TENANT_MAX_ATTEMPTS", 10)
    tenant = f"rate-test-{id(object())}"
    try:
        for index in range(2):
            auth_sso._enforce_handoff_rate_limit(
                tenant=tenant,
                surface="native",
                attempt_id=f"untrusted-{index}",
                client_ip="198.51.100.7",
            )

        with pytest.raises(HTTPException) as exc:
            auth_sso._enforce_handoff_rate_limit(
                tenant=tenant,
                surface="native",
                attempt_id="untrusted-final",
                client_ip="198.51.100.7",
            )
        assert exc.value.status_code == 429
    finally:
        with auth_sso._HANDOFF_RATE_LOCK:
            for key in list(auth_sso._HANDOFF_RATE_BUCKETS):
                if tenant in key:
                    del auth_sso._HANDOFF_RATE_BUCKETS[key]


def test_native_auth_payloads_are_bounded_before_proxying():
    with pytest.raises(ValidationError):
        NativeHandoffRequest(code="one-use-code", tenant_state="x" * 129)
    with pytest.raises(ValidationError):
        NativeRefreshRequest(refresh_token="x" * 513)
    with pytest.raises(ValidationError):
        NativeRevokeRequest(refresh_token="x" * 513)


def test_native_rate_limit_hashes_untrusted_attempt_ids(monkeypatch):
    monkeypatch.setattr(auth_sso, "_HANDOFF_RATE_MAX_ATTEMPTS", 10)
    tenant = f"hash-test-{id(object())}"
    attempt_id = "x" * 128
    try:
        auth_sso._enforce_handoff_rate_limit(
            tenant=tenant,
            surface="native",
            attempt_id=attempt_id,
            client_ip="198.51.100.7",
        )
        with auth_sso._HANDOFF_RATE_LOCK:
            keys = list(auth_sso._HANDOFF_RATE_BUCKETS)
        assert all(attempt_id not in key for key in keys)
    finally:
        with auth_sso._HANDOFF_RATE_LOCK:
            for key in list(auth_sso._HANDOFF_RATE_BUCKETS):
                if tenant in key:
                    del auth_sso._HANDOFF_RATE_BUCKETS[key]


def test_native_rate_limit_uses_trusted_proxy_client_ip(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/accept-native-handoff",
            "client": ("127.0.0.1", 1234),
            "headers": [(b"x-forwarded-for", b"203.0.113.9, 198.51.100.7")],
            "query_string": b"",
        }
    )

    assert auth_sso.get_client_ip(request) == "198.51.100.7"


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
        token_id="rt_test",
        device_session_id="nds_test",
    )


def test_verified_cp_email_can_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@example.com")
    db_session.add(user)
    db_session.commit()

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="david010@example.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.cp_user_id == 123
    assert resolved.provider == "control-plane"
    assert resolved.email_verified is True


def test_resolved_hosted_user_does_not_commit_when_claims_are_unchanged(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(
        email="david010@example.com",
        cp_user_id=123,
        provider="control-plane",
        provider_user_id="cp:123",
        display_name="CP User",
        avatar_url="https://example.com/avatar.png",
        email_verified=True,
        is_active=True,
        last_login=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()

    commits = 0
    original_commit = db_session.commit

    def counting_commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="david010@example.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.avatar_url == "https://example.com/avatar.png"
    assert commits == 0


def test_verified_cp_email_link_commits_when_profile_fields_already_match(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(
        email="david010@example.com",
        display_name="CP User",
        email_verified=True,
        is_active=True,
        last_login=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()

    commits = 0
    original_commit = db_session.commit

    def counting_commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="david010@example.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.cp_user_id == 123
    assert resolved.provider == "control-plane"
    assert resolved.provider_user_id == "cp:123"
    assert commits == 1


def test_cp_email_update_commits_when_profile_fields_already_match(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(
        email="old@example.com",
        cp_user_id=123,
        provider="control-plane",
        provider_user_id="cp:123",
        display_name="CP User",
        email_verified=True,
        is_active=True,
        last_login=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()

    commits = 0
    original_commit = db_session.commit

    def counting_commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="new@example.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.email == "new@example.com"
    assert commits == 1


def test_unverified_cp_email_cannot_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@example.com")
    db_session.add(user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        strategy._resolve_claims_user(  # noqa: SLF001
            db_session,
            _claims(cp_user_id=456, email="david010@example.com", email_verified=False),
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
    user = User(email="david010@example.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    class Strategy:
        def validate_ws_token(self, token, db=None):
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
    user = User(email="david010@example.com")
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
async def test_accept_handoff_binds_web_code_to_tenant_cookie(monkeypatch, db_session):
    user = User(email="david010@example.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    calls = {}

    def exchange(**kwargs):
        calls.update(kwargs)
        return {
            "runtime_token": "cp.runtime.jwt",
            "expires_in": 3600,
            "refresh_token": "lhr_refresh",
            "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
        }

    class Strategy:
        def validate_ws_token(self, token, db=None):
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

    tenant_state, cookie_name, cookie_secret = new_tenant_login_state(secure=True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/accept-handoff",
            "headers": [(b"cookie", f"{cookie_name}={cookie_secret}".encode())],
            "query_string": f"code=one-use-code&tenant_state={tenant_state}".encode(),
        }
    )

    redirect = await accept_handoff_request(
        request,
        "one-use-code",
        tenant_state=tenant_state,
    )

    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/timeline"
    assert any("longhouse_session=" in value for value in redirect.headers.getlist("set-cookie"))
    assert any("longhouse_refresh=" in value for value in redirect.headers.getlist("set-cookie"))
    assert calls == {
        "control_plane_url": "https://control.longhouse.ai",
        "internal_api_secret": "secret",
        "code": "one-use-code",
        "tenant": "david010",
        "tenant_state": tenant_state,
        "client": "web",
    }
    assert any(f"{cookie_name}=" in value and "Max-Age=0" in value for value in redirect.headers.getlist("set-cookie"))


@pytest.mark.asyncio
async def test_accept_handoff_requires_cookie_for_tenant_state(monkeypatch, db_session):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(
            control_plane_url="https://control.longhouse.ai",
            auth_disabled=False,
            testing=False,
        ),
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

    redirect = await accept_handoff_request(request, "one-use-code", tenant_state="state-1")

    assert redirect.headers["location"].endswith("auth_error=login_state_missing")
    assert redirect.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_accept_native_handoff_exchanges_one_use_code(monkeypatch, db_session):
    user = User(email="david010@example.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    calls = {}

    def exchange(**kwargs):
        calls.update(kwargs)
        return {
            "runtime_token": "cp.runtime.jwt",
            "expires_in": 3600,
            "refresh_token": "lhr_refresh",
            "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
            "device_session_id": "nds_session",
        }

    class Strategy:
        def validate_ws_token(self, token, db=None):
            assert token == "cp.runtime.jwt"
            return user

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso._exchange_handoff_code", exchange)
    monkeypatch.setattr("zerg.dependencies.auth._get_strategy", lambda: Strategy())

    result = await accept_native_handoff(
        Request({"type": "http", "method": "POST", "path": "/api/auth/accept-native-handoff", "headers": []}),
        Response(),
        NativeHandoffRequest(code="one-use-code", tenant_state="verifier"),
    )

    assert result == {
        "runtime_token": "cp.runtime.jwt",
        "expires_in": 3600,
        "refresh_token": "lhr_refresh",
        "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
        "device_session_id": "nds_session",
    }
    assert calls == {
        "control_plane_url": "https://control.longhouse.ai",
        "internal_api_secret": "secret",
        "code": "one-use-code",
        "tenant": "david010",
        "tenant_state": "verifier",
        "client": "ios",
    }


@pytest.mark.asyncio
async def test_accept_native_handoff_revokes_orphan_when_runtime_validation_fails(monkeypatch):
    revoked = []

    def exchange(**kwargs):
        return {
            "runtime_token": "cp.runtime.jwt",
            "expires_in": 3600,
            "refresh_token": "lhr_refresh",
            "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
            "device_session_id": "nds_session",
        }

    class Strategy:
        def validate_ws_token(self, token, db=None):
            assert token == "cp.runtime.jwt"
            return None

    def revoke(*, settings, refresh_token, strict):
        revoked.append((refresh_token, strict))

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso._exchange_handoff_code", exchange)
    monkeypatch.setattr("zerg.routers.auth_sso._revoke_native_session_payload", revoke)
    monkeypatch.setattr("zerg.dependencies.auth._get_strategy", lambda: Strategy())

    with pytest.raises(HTTPException) as exc:
        await accept_native_handoff(
            Request({"type": "http", "method": "POST", "path": "/api/auth/accept-native-handoff", "headers": []}),
            Response(),
            NativeHandoffRequest(code="one-use-code", tenant_state="verifier"),
        )

    assert exc.value.status_code == 401
    assert revoked == [("lhr_refresh", False)]


@pytest.mark.asyncio
async def test_accept_native_handoff_revokes_orphan_when_refresh_payload_missing(monkeypatch):
    revoked = []

    def exchange(**kwargs):
        return {"runtime_token": "cp.runtime.jwt", "expires_in": 3600}

    def revoke(*, settings, refresh_token, strict):
        revoked.append((refresh_token, strict))

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso._exchange_handoff_code", exchange)
    monkeypatch.setattr("zerg.routers.auth_sso._revoke_native_session_payload", revoke)

    with pytest.raises(HTTPException) as exc:
        await accept_native_handoff(
            Request({"type": "http", "method": "POST", "path": "/api/auth/accept-native-handoff", "headers": []}),
            Response(),
            NativeHandoffRequest(code="one-use-code", tenant_state="verifier"),
        )

    assert exc.value.status_code == 502
    assert revoked == []


def test_runtime_payload_rejects_missing_or_nonpositive_credentials():
    with pytest.raises(HTTPException) as missing:
        _runtime_payload({"expires_in": 600})
    assert missing.value.status_code == 502
    assert "missing token" in str(missing.value.detail).lower()

    with pytest.raises(HTTPException) as invalid_expiry:
        _runtime_payload({"runtime_token": "runtime", "expires_in": 0})
    assert invalid_expiry.value.status_code == 502
    assert "invalid expiry" in str(invalid_expiry.value.detail).lower()


def test_start_handoff_sets_state_and_attempt_cookies(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_browser.get_settings",
        lambda: SimpleNamespace(
            control_plane_url="https://control.longhouse.ai",
            auth_disabled=False,
            testing=False,
        ),
    )
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("david010.longhouse.ai", 443),
            "client": ("127.0.0.1", 1234),
            "method": "GET",
            "path": "/api/auth/start-handoff",
            "headers": [(b"host", b"david010.longhouse.ai")],
            "query_string": b"",
        }
    )

    redirect = start_handoff(request, return_to="/timeline")

    assert redirect.status_code == 302
    assert "tenant_state=" in redirect.headers["location"]
    cookies = redirect.headers.getlist("set-cookie")
    assert any("__Host-lh_login_" in value and "Secure" in value for value in cookies)
    assert any("__Host-lh_handoff_attempt=" in value and "Max-Age=60" in value for value in cookies)
    assert redirect.headers["cache-control"] == "no-store"
    assert redirect.headers["referrer-policy"] == "no-referrer"


def _cookie_request(value: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/refresh",
            "headers": [(b"cookie", value.encode())],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_hosted_browser_refresh_rotates_cp_session(monkeypatch):
    calls = {}

    def refresh(**kwargs):
        calls.update(kwargs)
        return {
            "runtime_token": "cp.next.jwt",
            "expires_in": 3600,
            "refresh_token": "lhr_next",
            "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
        }

    settings = SimpleNamespace(
        control_plane_url="https://control.longhouse.ai",
        internal_api_secret="secret",
    )
    monkeypatch.setattr("zerg.routers.auth_browser.get_settings", lambda: settings)
    monkeypatch.setattr("zerg.routers.auth_browser._refresh_native_session_payload", refresh)
    response = Response()

    result = await refresh_session(_cookie_request("longhouse_refresh=lhr_old"), response)

    assert result.status_code == 200
    assert result.body == b'{"expires_in":3600,"token_type":"bearer"}'
    assert b"cp.next.jwt" not in result.body
    assert calls == {"settings": settings, "refresh_token": "lhr_old"}
    assert any("longhouse_session=cp.next.jwt" in value for value in result.headers.getlist("set-cookie"))
    assert any("longhouse_refresh=lhr_next" in value for value in result.headers.getlist("set-cookie"))
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["pragma"] == "no-cache"
    assert response.headers.get("set-cookie") is None


@pytest.mark.asyncio
async def test_hosted_browser_refresh_rejection_clears_cookies(monkeypatch):
    settings = SimpleNamespace(
        control_plane_url="https://control.longhouse.ai",
        internal_api_secret="secret",
    )

    def reject(**kwargs):
        raise HTTPException(status_code=401, detail="expired")

    monkeypatch.setattr("zerg.routers.auth_browser.get_settings", lambda: settings)
    monkeypatch.setattr("zerg.routers.auth_browser._refresh_native_session_payload", reject)

    result = await refresh_session(_cookie_request("longhouse_refresh=lhr_old"), Response())

    assert result.status_code == 401
    assert any("longhouse_session=" in value and "Max-Age=0" in value for value in result.headers.getlist("set-cookie"))
    assert any("longhouse_refresh=" in value and "Max-Age=0" in value for value in result.headers.getlist("set-cookie"))


@pytest.mark.asyncio
async def test_hosted_browser_refresh_preserves_cookies_when_cp_is_unavailable(monkeypatch):
    settings = SimpleNamespace(
        control_plane_url="https://control.longhouse.ai",
        internal_api_secret="secret",
    )

    def unavailable(**kwargs):
        raise HTTPException(status_code=502, detail="Control plane native session refresh failed")

    monkeypatch.setattr("zerg.routers.auth_browser.get_settings", lambda: settings)
    monkeypatch.setattr("zerg.routers.auth_browser._refresh_native_session_payload", unavailable)
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await refresh_session(_cookie_request("longhouse_refresh=lhr_old"), response)

    assert exc.value.status_code == 503
    assert response.headers.get("set-cookie") is None


@pytest.mark.asyncio
async def test_hosted_browser_logout_revokes_cp_session(monkeypatch):
    calls = {}

    def revoke(**kwargs):
        calls.update(kwargs)

    settings = SimpleNamespace(
        control_plane_url="https://control.longhouse.ai",
        internal_api_secret="secret",
    )
    monkeypatch.setattr("zerg.routers.auth_browser.get_settings", lambda: settings)
    monkeypatch.setattr("zerg.routers.auth_browser._revoke_native_session_payload", revoke)
    response = Response()

    await logout(_cookie_request("longhouse_refresh=lhr_old"), response)

    assert calls == {"settings": settings, "refresh_token": "lhr_old"}
    assert any("longhouse_session=" in value and "Max-Age=0" in value for value in response.headers.getlist("set-cookie"))
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_hosted_browser_logout_reports_cp_revocation_failure(monkeypatch):
    settings = SimpleNamespace(
        control_plane_url="https://control.longhouse.ai",
        internal_api_secret="secret",
    )

    def unavailable(**kwargs):
        raise HTTPException(status_code=503, detail={"code": "cp_unavailable"})

    monkeypatch.setattr("zerg.routers.auth_browser.get_settings", lambda: settings)
    monkeypatch.setattr("zerg.routers.auth_browser._revoke_native_session_payload", unavailable)

    result = await logout(_cookie_request("longhouse_refresh=lhr_old"), Response())

    assert result.status_code == 503
    assert json.loads(result.body)["detail"]["code"] == "logout_revocation_unavailable"
    # A transient CP outage must not erase the only credentials that can retry
    # revocation; the frontend keeps the authenticated session and retries.
    assert result.headers.getlist("set-cookie") == []


@pytest.mark.asyncio
async def test_browser_auth_mutations_require_explicit_marker():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/refresh",
            "headers": [
                (b"cookie", b"longhouse_refresh=lhr_old"),
                (b"origin", b"https://david010.longhouse.ai"),
                (b"sec-fetch-site", b"same-origin"),
            ],
            "query_string": b"",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await refresh_session(request, Response())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_browser_auth_mutations_reject_cross_origin_even_with_marker():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/refresh",
            "headers": [
                (b"cookie", b"longhouse_refresh=lhr_old"),
                (b"origin", b"https://attacker.example"),
                (b"sec-fetch-site", b"cross-site"),
                (b"x-longhouse-auth", b"1"),
            ],
            "query_string": b"",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await refresh_session(request, Response())

    assert exc.value.status_code == 403


def test_password_rate_limit_admits_bounded_concurrent_verifications(monkeypatch):
    monkeypatch.setattr(auth_browser, "_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS", 2)
    key = f"password-rate-test-{id(object())}"
    try:
        assert auth_browser._check_password_rate_limit(key) is None
        assert auth_browser._check_password_rate_limit(key) is None
        retry_after = auth_browser._check_password_rate_limit(key)
        assert retry_after is not None

        auth_browser._release_password_attempt(key)
        auth_browser._release_password_attempt(key)
        assert auth_browser._check_password_rate_limit(key) is None
        auth_browser._release_password_attempt(key)
    finally:
        with auth_browser._PASSWORD_RATE_LIMIT_LOCK:
            auth_browser._PASSWORD_ACTIVE_ATTEMPTS.pop(key, None)
            auth_browser._PASSWORD_RATE_LIMIT_BUCKETS.pop(key, None)


def test_csrf_guard_allows_explicit_dev_frontend_origin(monkeypatch):
    monkeypatch.setattr(
        "zerg.dependencies.form_post_origin.get_settings",
        lambda: SimpleNamespace(
            auth_disabled=True,
            control_plane_url=None,
            allowed_cors_origins="http://localhost:5173",
            public_site_url=None,
            public_api_url=None,
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("backend", 8000),
            "path": "/api/auth/refresh",
            "headers": [
                (b"origin", b"http://localhost:5173"),
                (b"sec-fetch-site", b"cross-site"),
                (b"x-longhouse-auth", b"1"),
            ],
            "query_string": b"",
        }
    )

    require_browser_auth_header(request)


def test_csrf_guard_rejects_hosted_apex_origin(monkeypatch):
    monkeypatch.setattr(
        "zerg.dependencies.form_post_origin.get_settings",
        lambda: SimpleNamespace(
            auth_disabled=False,
            control_plane_url="https://control.longhouse.ai",
            allowed_cors_origins="https://longhouse.ai,https://david010.longhouse.ai",
            public_site_url="https://longhouse.ai",
            public_api_url=None,
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("david010.longhouse.ai", 443),
            "path": "/api/auth/refresh",
            "headers": [
                (b"origin", b"https://longhouse.ai"),
                (b"sec-fetch-site", b"cross-site"),
                (b"x-longhouse-auth", b"1"),
            ],
            "query_string": b"",
        }
    )

    with pytest.raises(HTTPException) as exc:
        require_browser_auth_header(request)

    assert exc.value.status_code == 403


def test_unknown_jwks_kid_forced_refresh_is_backed_off(monkeypatch):
    cp_jwks.clear_jwks_cache()
    fetches: list[bool] = []

    monkeypatch.setattr(cp_jwks, "_control_plane_url", lambda: "https://cp.example")

    def fake_fetch(*, force=False):
        fetches.append(force)
        return {}

    monkeypatch.setattr(cp_jwks, "_fetch_jwks", fake_fetch)
    monkeypatch.setattr(cp_jwks.jwt, "get_unverified_header", lambda token: {"kid": "unknown"})
    try:
        for _ in range(2):
            with pytest.raises(cp_jwks.CPTokenError, match="Unknown CP token kid"):
                cp_jwks.verify_runtime_token("ignored", audience="david010")
        assert fetches == [False, True, False]
    finally:
        cp_jwks.clear_jwks_cache()


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


def _native_request(*, headers: list[tuple[bytes, bytes]] | None = None):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/native",
            "client": ("127.0.0.1", 1234),
            "headers": headers or [],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_refresh_runtime_token_proxies_bearer_to_cp(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "runtime_token": "cp.fresh.jwt",
                "expires_in": 3600,
                "refresh_token": "lhr_refresh",
                "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
                "device_session_id": "nds_session",
            }

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

    assert result == {
        "runtime_token": "cp.fresh.jwt",
        "expires_in": 3600,
        "refresh_token": "lhr_refresh",
        "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
        "device_session_id": "nds_session",
    }
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


@pytest.mark.asyncio
async def test_refresh_native_session_proxies_refresh_token_to_cp(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "runtime_token": "cp.fresh.jwt",
                "expires_in": 3600,
                "refresh_token": "lhr_next",
                "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
                "device_session_id": "nds_session",
            }

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)

    result = await refresh_native_session(_native_request(), Response(), NativeRefreshRequest(refresh_token="lhr_current"))

    assert result == {
        "runtime_token": "cp.fresh.jwt",
        "expires_in": 3600,
        "refresh_token": "lhr_next",
        "refresh_token_expires_at": "2027-01-01T00:00:00+00:00",
        "device_session_id": "nds_session",
    }
    assert captured["url"] == "https://control.longhouse.ai/api/identity/refresh-native-session"
    assert captured["headers"] == {"X-Internal-Token": "secret"}
    assert captured["json"] == {"refresh_token": "lhr_current", "tenant": "david010"}
    assert captured["timeout"] == 10.0


@pytest.mark.asyncio
async def test_refresh_native_session_propagates_cp_rejection(monkeypatch):
    class FakeResponse:
        status_code = 401

        def json(self):
            return {"detail": "revoked"}

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", lambda *a, **k: FakeResponse())

    with pytest.raises(HTTPException) as exc:
        await refresh_native_session(_native_request(), Response(), NativeRefreshRequest(refresh_token="lhr_revoked"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_native_session_returns_502_on_cp_network_error(monkeypatch):
    def fake_post(*a, **k):
        raise httpx.HTTPError("connection refused")

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)

    with pytest.raises(HTTPException) as exc:
        await refresh_native_session(_native_request(), Response(), NativeRefreshRequest(refresh_token="lhr_current"))
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_refresh_native_session_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    with pytest.raises(HTTPException) as exc:
        await refresh_native_session(_native_request(), Response(), NativeRefreshRequest(refresh_token=" "))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_revoke_native_session_proxies_to_cp(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)

    result = await revoke_native_session(_native_request(), Response(), NativeRevokeRequest(refresh_token="lhr_current"))

    assert result == {"status": "ok"}
    assert captured["url"] == "https://control.longhouse.ai/api/identity/revoke-native-session"
    assert captured["headers"] == {"X-Internal-Token": "secret"}
    assert captured["json"] == {"refresh_token": "lhr_current"}
    assert captured["timeout"] == 5.0


@pytest.mark.asyncio
async def test_revoke_native_session_ignores_empty_token(monkeypatch):
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )

    with pytest.raises(HTTPException) as exc:
        await revoke_native_session(_native_request(), Response(), NativeRevokeRequest(refresh_token=" "))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_revoke_native_session_reports_cp_rejection(monkeypatch):
    class FakeResponse:
        status_code = 500

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", lambda *a, **k: FakeResponse())
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")

    with pytest.raises(HTTPException) as exc:
        await revoke_native_session(_native_request(), Response(), NativeRevokeRequest(refresh_token="lhr_current"))

    assert exc.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 410, 422])
async def test_revoke_native_session_treats_terminal_cp_rejection_as_success(monkeypatch, status_code):
    class FakeResponse:
        pass

    FakeResponse.status_code = status_code
    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", lambda *a, **k: FakeResponse())
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")

    result = await revoke_native_session(
        _native_request(),
        Response(),
        NativeRevokeRequest(refresh_token="lhr_current"),
    )

    assert result == {"status": "ok"}

@pytest.mark.asyncio
async def test_revoke_native_session_reports_cp_network_error(monkeypatch):
    def fake_post(*a, **k):
        raise httpx.HTTPError("connection refused")

    monkeypatch.setattr(
        "zerg.routers.auth_sso.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai", internal_api_secret="secret"),
    )
    monkeypatch.setattr("zerg.routers.auth_sso.httpx.post", fake_post)
    monkeypatch.setattr("zerg.routers.auth_sso.hosted_instance_id", lambda: "david010")

    with pytest.raises(HTTPException) as exc:
        await revoke_native_session(_native_request(), Response(), NativeRevokeRequest(refresh_token="lhr_current"))

    assert exc.value.status_code == 503


def test_jwks_cache_is_bounded_across_control_plane_urls(monkeypatch):
    cp_jwks.clear_jwks_cache()
    current = {"base": "https://cp-0.example"}
    fetches = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "keys": [
                    {
                        "kid": current["base"],
                        "kty": "RSA",
                        "n": "w" + "A" * 340 + "Q",
                        "e": "AQAB",
                    }
                ]
            }

    def fake_get(url, timeout):
        fetches.append((url, timeout))
        return FakeResponse()

    monkeypatch.setattr(cp_jwks, "_control_plane_url", lambda: current["base"])
    monkeypatch.setattr(cp_jwks.httpx, "get", fake_get)
    try:
        for index in range(cp_jwks.JWKS_CACHE_MAX_ENTRIES + 3):
            current["base"] = f"https://cp-{index}.example"
            assert cp_jwks._fetch_jwks()[current["base"]]["kid"] == current["base"]

        assert len(cp_jwks._jwks_cache) == cp_jwks.JWKS_CACHE_MAX_ENTRIES
        before = len(fetches)
        cp_jwks._fetch_jwks()
        assert len(fetches) == before
    finally:
        cp_jwks.clear_jwks_cache()
