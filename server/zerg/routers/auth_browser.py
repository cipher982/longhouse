"""Browser-session login and status routes for tenant auth."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
import uuid
from collections import defaultdict
from collections import deque
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from zerg.auth import refresh_tokens
from zerg.auth.catalog_gateway import create_refresh
from zerg.auth.catalog_gateway import resolve_local_user
from zerg.auth.catalog_gateway import revoke_refresh_family
from zerg.auth.catalog_gateway import rotate_refresh
from zerg.auth.hosted import TENANT_LOGIN_STATE_COOKIE
from zerg.auth.session_tokens import ACCESS_TOKEN_LIFETIME
from zerg.auth.session_tokens import REFRESH_COOKIE_NAME
from zerg.auth.session_tokens import _clear_refresh_cookie
from zerg.auth.session_tokens import _clear_session_cookie
from zerg.auth.session_tokens import _issue_access_token
from zerg.auth.session_tokens import _set_refresh_cookie
from zerg.auth.session_tokens import _set_session_cookie
from zerg.config import get_settings
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_auth import get_optional_browser_user
from zerg.schemas.schemas import TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])
# Refresh token cookie max-age: 90 days (matches absolute lifetime in refresh_tokens module).
_REFRESH_COOKIE_MAX_AGE = 90 * 24 * 60 * 60


def _control_plane_url(settings: Any | None = None) -> str | None:
    settings = settings or get_settings()
    return getattr(settings, "control_plane_url", None) or None


async def _issue_session(
    response: Response,
    user,
    *,
    display_name: str | None = None,
    avatar_url: str | None = None,
) -> TokenOut:
    """Issue an access-token cookie + refresh-token cookie in one shot.

    Every browser login flow should call this instead of manually wiring
    ``_issue_access_token`` / ``_set_session_cookie``.
    """
    at_seconds = int(ACCESS_TOKEN_LIFETIME.total_seconds())
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=display_name or getattr(user, "display_name", None),
        avatar_url=avatar_url or getattr(user, "avatar_url", None),
    )

    raw_rt = refresh_tokens._generate_token()
    now = datetime.now(timezone.utc)
    family_id = uuid.uuid4().hex
    result = await asyncio.to_thread(
        create_refresh,
        user_id=int(user.id),
        token_hash=refresh_tokens._hash_token(raw_rt),
        family_id=family_id,
        parent_id=None,
        created_at=now,
        absolute_expires_at=now + refresh_tokens.ABSOLUTE_LIFETIME,
        idle_expires_at=now + refresh_tokens.IDLE_LIFETIME,
    )
    if not (result.get("created") is True or result.get("exact_replay") is True):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Catalog refresh issuance failed")
    _set_session_cookie(response, access_token, at_seconds)
    _set_refresh_cookie(response, raw_rt, _REFRESH_COOKIE_MAX_AGE)

    return TokenOut(access_token=access_token, expires_in=at_seconds)


_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS = 5
_PASSWORD_RATE_LIMIT_WINDOW_SECONDS = 60
_PASSWORD_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_password_rate_limit(key: str) -> int | None:
    now = time.monotonic()
    window_start = now - _PASSWORD_RATE_LIMIT_WINDOW_SECONDS
    bucket = _PASSWORD_RATE_LIMIT_BUCKETS[key]
    while bucket and bucket[0] < window_start:
        bucket.popleft()
    if len(bucket) >= _PASSWORD_RATE_LIMIT_MAX_ATTEMPTS:
        retry_after = int(_PASSWORD_RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0])) + 1
        return max(retry_after, 1)
    return None


def _record_password_failure(key: str) -> None:
    _PASSWORD_RATE_LIMIT_BUCKETS[key].append(time.monotonic())


def _clear_password_failures(key: str) -> None:
    _PASSWORD_RATE_LIMIT_BUCKETS.pop(key, None)


def _verify_pbkdf2_sha256(password: str, stored: str) -> bool:
    try:
        _, iterations_str, salt_b64, hash_b64 = stored.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64.encode("utf-8"))
        expected = base64.b64decode(hash_b64.encode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid pbkdf2_sha256 hash format") from exc

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(derived, expected)


def _verify_password_hash(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        try:
            return _verify_pbkdf2_sha256(password, stored)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid LONGHOUSE_PASSWORD_HASH format",
            ) from exc

    if stored.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher  # type: ignore
            from argon2.exceptions import InvalidHash  # type: ignore
            from argon2.exceptions import VerifyMismatchError  # type: ignore
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="argon2-cffi not installed for LONGHOUSE_PASSWORD_HASH",
            ) from exc

        hasher = PasswordHasher()
        try:
            return hasher.verify(stored, password)
        except VerifyMismatchError:
            return False
        except InvalidHash as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid LONGHOUSE_PASSWORD_HASH format",
            ) from exc

    if stored.startswith("$2"):
        try:
            import bcrypt  # type: ignore
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="bcrypt not installed for LONGHOUSE_PASSWORD_HASH",
            ) from exc

        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid LONGHOUSE_PASSWORD_HASH format",
            ) from exc

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unsupported LONGHOUSE_PASSWORD_HASH format",
    )


def _verify_google_id_token(id_token_str: str) -> dict[str, Any]:
    settings = get_settings()
    valid_client_ids = [cid for cid in [settings.google_client_id, settings.google_ios_client_id] if cid]
    if not valid_client_ids:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GOOGLE_CLIENT_ID not set")

    from google.auth.transport import requests as google_requests  # type: ignore
    from google.oauth2 import id_token  # type: ignore

    request = google_requests.Request()
    last_exc: Exception | None = None
    for client_id in valid_client_ids:
        try:
            return id_token.verify_oauth2_token(id_token_str, request, client_id)
        except Exception as exc:
            last_exc = exc
            continue

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid Google token: {str(last_exc)}",
    ) from last_exc


@router.post("/dev-login", response_model=TokenOut)
async def dev_login(response: Response) -> TokenOut:
    settings = get_settings()
    if not settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev login only available when AUTH_DISABLED=1",
        )

    user = await asyncio.to_thread(
        resolve_local_user,
        email="dev@local",
        provider="dev",
        provider_user_id="dev-user-1",
        role="ADMIN",
        adopt_existing=False,
        require_email_match=False,
        max_users=None,
        promote_role=True,
    )
    return await _issue_session(response, user, display_name=user.display_name or "Dev User")


@router.post("/service-login", response_model=TokenOut, include_in_schema=False)
async def service_login(request: Request, response: Response) -> TokenOut:
    settings = get_settings()
    if _control_plane_url(settings):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted local service login is disabled")
    secret = request.headers.get("X-Service-Secret") or ""
    expected = settings.smoke_test_secret or ""
    run_id = (request.headers.get("X-Smoke-Run-Id") or "").strip()

    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    email = "smoke@service.local"
    provider_user_id = "smoke-test"
    display_name = "Smoke Test" + (f" ({run_id[:20]})" if run_id else "")

    user = await asyncio.to_thread(
        resolve_local_user,
        email=email,
        provider="service",
        provider_user_id=provider_user_id,
        role="USER",
        adopt_existing=False,
        require_email_match=False,
        max_users=None,
        promote_role=False,
    )
    return await _issue_session(response, user, display_name=display_name)


@router.post("/google", response_model=TokenOut)
async def google_sign_in(response: Response, body: dict[str, str]) -> TokenOut:
    if _control_plane_url():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted local Google login is disabled")

    raw_token = body.get("id_token")
    if not raw_token or not isinstance(raw_token, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="id_token must be provided")

    claims = _verify_google_id_token(raw_token)
    if claims.get("email_verified") is False:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified")

    email: str = claims.get("email")  # type: ignore[assignment]
    sub: str = claims.get("sub")

    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token missing email claim")

    settings = get_settings()
    if settings.single_tenant and not settings.testing:
        from zerg.services.single_tenant import is_owner_email

        if not is_owner_email(email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This Zerg instance is configured for a specific owner. Sign-in with the owner email.",
            )

    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    is_admin = email.lower() in admin_emails
    try:
        user = await asyncio.to_thread(
            resolve_local_user,
            email=email,
            provider="google",
            provider_user_id=sub,
            role="ADMIN" if is_admin else "USER",
            adopt_existing=False,
            require_email_match=bool(settings.single_tenant and not settings.testing),
            max_users=(settings.max_users if not settings.testing and not is_admin and not settings.single_tenant else None),
            promote_role=is_admin,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT and settings.single_tenant:
            exc.detail = "Single-tenant mode: instance already has an owner. Cannot create additional users."
        elif exc.status_code == status.HTTP_409_CONFLICT:
            exc.status_code = status.HTTP_403_FORBIDDEN
            exc.detail = "Sign-ups disabled: user limit reached"
        raise

    return await _issue_session(response, user)


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def verify_session(_request: Request, _user=Depends(get_current_browser_user)):
    settings = get_settings()
    if settings.auth_disabled:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/status")
def auth_status(_request: Request, user=Depends(get_optional_browser_user)):
    if not user:
        return {"authenticated": False, "user": None}

    return {
        "authenticated": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": getattr(user, "display_name", None),
            "avatar_url": getattr(user, "avatar_url", None),
            "is_active": getattr(user, "is_active", True),
            "email_verified": getattr(user, "email_verified", True),
            "created_at": getattr(user, "created_at", None),
            "last_login": getattr(user, "last_login", None),
            "prefs": getattr(user, "prefs", None),
            "role": getattr(user, "role", "USER"),
        },
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(request: Request, response: Response):
    # Revoke the refresh token family so the RT can't be replayed after logout.
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_rt:
        await asyncio.to_thread(
            revoke_refresh_family,
            token_hash=refresh_tokens._hash_token(raw_rt),
            now=datetime.now(timezone.utc),
        )

    _clear_session_cookie(response)
    _clear_refresh_cookie(response)


@router.post("/refresh", response_model=TokenOut)
async def refresh_session(request: Request, response: Response) -> TokenOut:
    """Exchange a valid refresh token for a new access token + rotated refresh token.

    This is the silent-refresh endpoint called by the frontend on 401.
    """
    if _control_plane_url():
        _clear_session_cookie(response)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted refresh is not available")

    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_rt:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    next_raw = refresh_tokens._generate_token()
    now = datetime.now(timezone.utc)
    result = await asyncio.to_thread(
        rotate_refresh,
        token_hash=refresh_tokens._hash_token(raw_rt),
        next_token_hash=refresh_tokens._hash_token(next_raw),
        now=now,
        idle_expires_at=now + refresh_tokens.IDLE_LIFETIME,
        reuse_grace_seconds=refresh_tokens.REUSE_GRACE_SECONDS,
    )
    if result.get("status") not in {"rotated", "exact_replay"}:
        # Token invalid, expired, or revoked — clear cookies and force re-login.
        _clear_session_cookie(response)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired or revoked")

    user = result["user"]

    at_seconds = int(ACCESS_TOKEN_LIFETIME.total_seconds())
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=getattr(user, "display_name", None),
        avatar_url=getattr(user, "avatar_url", None),
    )
    _set_session_cookie(response, access_token, at_seconds)
    _set_refresh_cookie(response, next_raw, _REFRESH_COOKIE_MAX_AGE)

    return TokenOut(access_token=access_token, expires_in=at_seconds)


@router.get("/methods")
def get_auth_methods():
    settings = get_settings()
    control_plane_url = _control_plane_url(settings)
    sso_base = control_plane_url.rstrip("/") if control_plane_url else None
    return {
        "google": bool(settings.google_client_id) and not bool(control_plane_url),
        "password": bool(settings.longhouse_password or settings.longhouse_password_hash) and not bool(control_plane_url),
        "sso": bool(control_plane_url),
        "sso_url": sso_base,
        # Hosted tenants send the browser to /auth/start, which renders a
        # tenant-aware login page. Self-host tenants still have their own
        # login surface and ignore this URL.
        "sso_login_url": f"{sso_base}/auth/start" if sso_base else None,
    }


class PasswordLoginRequest(BaseModel):
    password: str


def _resolve_password_user():
    settings = get_settings()

    if settings.single_tenant and not settings.testing:
        import os

        from zerg.services.single_tenant import get_owner_email

        owner_email = get_owner_email().strip().lower()
        owner_email_explicit = bool(os.getenv("OWNER_EMAIL", "").strip())
        try:
            return resolve_local_user(
                email=owner_email,
                provider="password",
                provider_user_id=None,
                role="ADMIN",
                adopt_existing=not owner_email_explicit,
                require_email_match=owner_email_explicit,
                max_users=None,
                promote_role=False,
            )
        except HTTPException as exc:
            if owner_email_explicit and exc.status_code == status.HTTP_409_CONFLICT:
                exc.detail = "Password auth is bound to the configured owner. Existing user does not match OWNER_EMAIL."
            raise

    return resolve_local_user(
        email="local@longhouse",
        provider="password",
        provider_user_id=None,
        role="USER",
        adopt_existing=False,
        require_email_match=False,
        max_users=None,
        promote_role=False,
    )


@router.post("/password", response_model=TokenOut)
async def password_login(
    request: Request,
    response: Response,
    body: PasswordLoginRequest,
) -> TokenOut:
    settings = get_settings()
    if _control_plane_url(settings):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted local password login is disabled")
    if not settings.longhouse_password and not settings.longhouse_password_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password auth not configured")

    client_ip = _get_client_ip(request)
    retry_after = _check_password_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if settings.longhouse_password_hash:
        password_ok = _verify_password_hash(body.password, settings.longhouse_password_hash)
    else:
        password_ok = secrets.compare_digest(body.password, settings.longhouse_password)

    if not password_ok:
        _record_password_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")

    _clear_password_failures(client_ip)
    user = await asyncio.to_thread(_resolve_password_user)

    return await _issue_session(response, user, display_name=user.display_name or "Local User")


class CLILoginRequest(BaseModel):
    password: str


@router.post("/cli-login")
async def cli_login(
    request: Request,
    body: CLILoginRequest,
) -> dict[str, str]:
    settings = get_settings()
    if _control_plane_url(settings):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted local CLI login is disabled")
    if not settings.longhouse_password and not settings.longhouse_password_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password auth not configured")

    client_ip = _get_client_ip(request)
    retry_after = _check_password_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if settings.longhouse_password_hash:
        password_ok = _verify_password_hash(body.password, settings.longhouse_password_hash)
    else:
        password_ok = secrets.compare_digest(body.password, settings.longhouse_password)

    if not password_ok:
        _record_password_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")

    _clear_password_failures(client_ip)
    user = await asyncio.to_thread(_resolve_password_user)
    access_token = _issue_access_token(
        user.id,
        user.email,
        expires_delta=timedelta(minutes=5),
    )
    return {"token": access_token}


@router.get("/start-handoff")
def start_handoff(
    request: Request,
    tenant: str | None = None,
    return_to: str | None = None,
) -> RedirectResponse:
    """Browser entry point for hosted login.

    302s to the control plane `/auth/start?tenant=...&return_to=...`
    after setting a tenant-side CSRF cookie. The CP renders a
    tenant-aware login page; after auth the CP mints a one-use handoff
    code and 302s to the tenant's `/api/auth/accept-handoff`.

    Self-host tenants (no CONTROL_PLANE_URL) get a redirect to the
    local `/login` React route instead, which renders the tenant's
    own login form.

    """
    settings = get_settings()
    control_plane_url = _control_plane_url(settings)
    if not control_plane_url:
        safe_return_to = return_to or "/timeline"
        return RedirectResponse(
            f"/login?return_to={urllib.parse.quote(safe_return_to, safe='')}",
            status_code=302,
        )

    # Derive the tenant from the request host if not explicitly given.
    # This makes the React LoginPage simpler — it doesn't have to know
    # its own subdomain.
    resolved_tenant = (tenant or "").strip().lower()
    if not resolved_tenant:
        host = (request.url.hostname or "").lower()
        # Strip the root domain suffix (longhouse.ai or localhost).
        # The tenant subdomain is everything before the first dot.
        if host.endswith(".longhouse.ai"):
            resolved_tenant = host[: -len(".longhouse.ai")]
        elif host.endswith(".localhost"):
            resolved_tenant = host[: -len(".localhost")]
        # else: leave empty; CP will reject unknown tenant.

    safe_return_to = return_to or "/timeline"
    tenant_state = secrets.token_urlsafe(32)

    cp_base = control_plane_url.rstrip("/")
    target = f"{cp_base}/auth/start"
    params: list[tuple[str, str]] = [("return_to", safe_return_to), ("tenant_state", tenant_state)]
    if resolved_tenant:
        params.append(("tenant", resolved_tenant))
    target += "?" + urllib.parse.urlencode(params)

    redirect = RedirectResponse(target, status_code=302)
    redirect.set_cookie(
        TENANT_LOGIN_STATE_COOKIE,
        tenant_state,
        max_age=600,
        path="/",
        httponly=True,
        secure=not settings.auth_disabled and not settings.testing,
        samesite="lax",
    )
    return redirect


__all__ = [
    "CLILoginRequest",
    "PasswordLoginRequest",
    "_resolve_password_user",
    "router",
]
