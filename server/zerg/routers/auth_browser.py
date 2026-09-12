"""Browser-session login and status routes for tenant auth."""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse
import uuid
from collections import OrderedDict
from collections import deque
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from threading import Lock
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from zerg.auth import refresh_tokens
from zerg.auth.catalog_gateway import create_refresh
from zerg.auth.catalog_gateway import resolve_local_user
from zerg.auth.catalog_gateway import revoke_refresh_family
from zerg.auth.catalog_gateway import rotate_refresh
from zerg.auth.client_ip import get_client_ip
from zerg.auth.hosted import MAX_TENANT_LOGIN_ATTEMPTS
from zerg.auth.hosted import TENANT_LOGIN_ATTEMPT_MAX_AGE
from zerg.auth.hosted import hosted_cookie_origin_is_secure
from zerg.auth.hosted import hosted_instance_id
from zerg.auth.hosted import is_tenant_login_cookie_name
from zerg.auth.hosted import new_tenant_login_state
from zerg.auth.hosted import tenant_cookie_secure
from zerg.auth.hosted import tenant_handoff_attempt_cookie_name
from zerg.auth.hosted import tenant_login_attempt_cookie_name
from zerg.auth.hosted import tenant_login_ready_cookie_name
from zerg.auth.redirects import normalize_local_return_to
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
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post
from zerg.dependencies.form_post_origin import require_browser_auth_header
from zerg.routers.auth_sso import _hosted_refresh_cookie_max_age
from zerg.routers.auth_sso import _refresh_native_session_payload
from zerg.routers.auth_sso import _revoke_native_session_payload
from zerg.schemas.schemas import TokenOut


class RefreshOut(BaseModel):
    expires_in: int
    token_type: str = "bearer"


router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)
# Local refresh-token cookie max-age: 90 days.
_REFRESH_COOKIE_MAX_AGE = 90 * 24 * 60 * 60
_HANDOFF_ATTEMPT_MAX_AGE = 60
_HANDOFF_ATTEMPT_MAX_COUNT = 4


def _control_plane_url(settings: Any | None = None) -> str | None:
    settings = settings or get_settings()
    return getattr(settings, "control_plane_url", None) or None


def _refresh_failure_response(*, status_code: int, detail: str | dict[str, str]) -> Response:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    _clear_session_cookie(response)
    _clear_refresh_cookie(response)
    _set_no_store(response)
    return response


def _set_no_store(response: Response) -> None:
    response.headers["cache-control"] = "no-store"
    response.headers["pragma"] = "no-cache"
    response.headers["vary"] = "Cookie"


def _handoff_attempt_count(value: str | None, *, reset: bool = False) -> int:
    if reset or not value:
        return 0
    try:
        count_text, started_text = value.split(":", 1)
        count = int(count_text)
        started_at = float(started_text)
    except (TypeError, ValueError):
        return 0
    if count < 0 or time.time() - started_at > _HANDOFF_ATTEMPT_MAX_AGE:
        return 0
    return min(count, _HANDOFF_ATTEMPT_MAX_COUNT)


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
    _set_no_store(response)

    return TokenOut(access_token=access_token, expires_in=at_seconds)


_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS = 5
_PASSWORD_RATE_LIMIT_WINDOW_SECONDS = 60
# Hard ceiling on distinct keys we track, so a spoofed key space can't grow the dict.
_PASSWORD_RATE_LIMIT_MAX_KEYS = 1024
_PASSWORD_RATE_LIMIT_BUCKETS: OrderedDict[str, deque[float]] = OrderedDict()
_PASSWORD_RATE_LIMIT_LOCK = Lock()
_PASSWORD_ACTIVE_ATTEMPTS: dict[str, int] = {}


def _check_password_rate_limit(key: str) -> int | None:
    """Atomically admit one password verification for this client key."""
    with _PASSWORD_RATE_LIMIT_LOCK:
        now = time.monotonic()
        window_start = now - _PASSWORD_RATE_LIMIT_WINDOW_SECONDS
        bucket = _PASSWORD_RATE_LIMIT_BUCKETS.get(key)
        if bucket is not None:
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if not bucket:
                del _PASSWORD_RATE_LIMIT_BUCKETS[key]
                bucket = None

        active = _PASSWORD_ACTIVE_ATTEMPTS.get(key, 0)
        if len(_PASSWORD_ACTIVE_ATTEMPTS) >= _PASSWORD_RATE_LIMIT_MAX_KEYS and key not in _PASSWORD_ACTIVE_ATTEMPTS:
            return _PASSWORD_RATE_LIMIT_WINDOW_SECONDS + 1
        if bucket is not None and len(bucket) + active >= _PASSWORD_RATE_LIMIT_MAX_ATTEMPTS:
            retry_after = int(_PASSWORD_RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0])) + 1
            return max(retry_after, 1)
        if bucket is None and active >= _PASSWORD_RATE_LIMIT_MAX_ATTEMPTS:
            return _PASSWORD_RATE_LIMIT_WINDOW_SECONDS + 1
        _PASSWORD_ACTIVE_ATTEMPTS[key] = active + 1
        return None


def _record_password_failure(key: str) -> None:
    with _PASSWORD_RATE_LIMIT_LOCK:
        active = _PASSWORD_ACTIVE_ATTEMPTS.get(key, 0)
        if active <= 1:
            _PASSWORD_ACTIVE_ATTEMPTS.pop(key, None)
        else:
            _PASSWORD_ACTIVE_ATTEMPTS[key] = active - 1
        now = time.monotonic()
        window_start = now - _PASSWORD_RATE_LIMIT_WINDOW_SECONDS
        for stale in [k for k, b in _PASSWORD_RATE_LIMIT_BUCKETS.items() if not b or b[-1] < window_start]:
            del _PASSWORD_RATE_LIMIT_BUCKETS[stale]
        _PASSWORD_RATE_LIMIT_BUCKETS.setdefault(key, deque()).append(now)
        _PASSWORD_RATE_LIMIT_BUCKETS.move_to_end(key)
        while len(_PASSWORD_RATE_LIMIT_BUCKETS) > _PASSWORD_RATE_LIMIT_MAX_KEYS:
            _PASSWORD_RATE_LIMIT_BUCKETS.popitem(last=False)


def _release_password_attempt(key: str) -> None:
    """Release a successful verification without counting it as a failure."""
    with _PASSWORD_RATE_LIMIT_LOCK:
        active = _PASSWORD_ACTIVE_ATTEMPTS.get(key, 0)
        if active <= 1:
            _PASSWORD_ACTIVE_ATTEMPTS.pop(key, None)
        else:
            _PASSWORD_ACTIVE_ATTEMPTS[key] = active - 1


def _clear_password_failures(key: str) -> None:
    with _PASSWORD_RATE_LIMIT_LOCK:
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


async def _verify_password_attempt(
    *,
    password: str,
    plain_password: str,
    password_hash: str | None,
) -> bool:
    """Verify one attempt without leaking an in-flight rate-limit reservation."""
    if not password_hash:
        return secrets.compare_digest(password.encode("utf-8"), plain_password.encode("utf-8"))

    verification = asyncio.create_task(asyncio.to_thread(_verify_password_hash, password, password_hash))
    try:
        return await asyncio.shield(verification)
    except BaseException:
        # Cancellation does not cancel the worker thread. Wait for it before
        # the caller releases its reservation, otherwise cancellation becomes
        # a way to queue unlimited expensive verifications.
        try:
            await asyncio.shield(verification)
        except BaseException:
            pass
        raise


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
async def google_sign_in(request: Request, response: Response, body: dict[str, str]) -> TokenOut:
    reject_cross_origin_form_post(request)
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
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_no_store(response)
    return response


@router.get("/status")
def auth_status(response: Response, _request: Request, user=Depends(get_optional_browser_user)):
    _set_no_store(response)
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
async def logout(request: Request, response: Response, everywhere: bool = False):
    require_browser_auth_header(request)
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    settings = get_settings()
    revocation_failed = False
    revocation_rejected = False
    authority_missing = False
    if raw_rt:
        try:
            if _control_plane_url(settings):
                revoke_kwargs = {
                    "settings": settings,
                    "refresh_token": raw_rt,
                }
                if everywhere:
                    revoke_kwargs["revoke_authority"] = True
                await asyncio.to_thread(
                    _revoke_native_session_payload,
                    **revoke_kwargs,
                )
            else:
                await asyncio.to_thread(
                    revoke_refresh_family,
                    token_hash=refresh_tokens._hash_token(raw_rt),
                    now=datetime.now(timezone.utc),
                )
        except HTTPException as exc:
            if everywhere and exc.status_code == status.HTTP_409_CONFLICT:
                revocation_rejected = True
                logger.warning("account-wide logout rejected because the session changed")
            else:
                revocation_failed = True
                logger.warning("session revocation failed during logout", exc_info=True)
        except Exception:
            # Local logout is unconditional. Surface a degraded CP revoke so
            # callers do not mistake an outage for a fully revoked session.
            revocation_failed = True
            logger.warning("session revocation failed during logout", exc_info=True)
    elif everywhere:
        authority_missing = True
        logger.warning("account-wide logout requested without a refresh credential")

    if authority_missing:
        response.status_code = status.HTTP_401_UNAUTHORIZED
        response.headers["x-longhouse-error-code"] = "missing_authority"
    elif revocation_rejected:
        response.status_code = status.HTTP_409_CONFLICT
        response.headers["x-longhouse-error-code"] = "revocation_not_authorized"
    elif revocation_failed:
        logger.warning("session revocation degraded during logout")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["retry-after"] = "5"

    _clear_session_cookie(response)
    _clear_refresh_cookie(response)
    cookie_secure = tenant_cookie_secure(settings)
    # Keep the non-sensitive generation marker so a dashboard-originated
    # handoff can prove it belongs after this logout. Login-state and
    # handoff-attempt cookies are disposable and are cleared here.
    for cookie_name in request.cookies:
        if is_tenant_login_cookie_name(cookie_name, secure=cookie_secure) or cookie_name == tenant_handoff_attempt_cookie_name(
            secure=cookie_secure
        ):
            response.delete_cookie(
                cookie_name,
                path="/",
                secure=cookie_secure,
                samesite="lax",
            )
    response.delete_cookie(
        tenant_login_ready_cookie_name(secure=cookie_secure),
        path="/",
        secure=cookie_secure,
        samesite="lax",
    )
    _set_no_store(response)


@router.post("/refresh", response_model=RefreshOut)
async def refresh_session(request: Request, response: Response) -> RefreshOut | Response:
    """Rotate the hosted CP refresh family or the local catalog family."""
    require_browser_auth_header(request)
    settings = get_settings()
    if _control_plane_url(settings):
        raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
        if not raw_rt:
            return _refresh_failure_response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No refresh token",
            )
        try:
            payload = await asyncio.to_thread(
                _refresh_native_session_payload,
                settings=settings,
                refresh_token=raw_rt,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code in {
                status.HTTP_502_BAD_GATEWAY,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            } or detail.get("code") in {"cp_unavailable", "tenant_internal_auth_failed"}:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "cp_unavailable"},
                ) from exc
            if exc.status_code in {400, 401, 403, 410, 422}:
                return _refresh_failure_response(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Refresh token expired or revoked",
                )
            raise
        next_raw = payload.get("refresh_token")
        if not isinstance(next_raw, str) or not next_raw:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "cp_unavailable"},
            )
        at_seconds = int(payload["expires_in"])
        browser_response = JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"expires_in": at_seconds, "token_type": "bearer"},
        )
        _set_session_cookie(browser_response, payload["runtime_token"], at_seconds)
        _set_refresh_cookie(browser_response, next_raw, _hosted_refresh_cookie_max_age(payload))
        _set_no_store(browser_response)
        return browser_response

    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_rt:
        return _refresh_failure_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token",
        )

    next_raw = refresh_tokens._derive_rotation_token(raw_rt)
    now = datetime.now(timezone.utc)
    result = await asyncio.to_thread(
        rotate_refresh,
        token_hash=refresh_tokens._hash_token(raw_rt),
        next_token_hash=refresh_tokens._hash_token(next_raw),
        now=now,
        idle_expires_at=now + refresh_tokens.IDLE_LIFETIME,
        reuse_grace_seconds=refresh_tokens.REUSE_GRACE_SECONDS,
    )
    result_status = result.get("status")
    if result_status == "stale_replay":
        current_hash = result.get("current_token_hash")
        candidate = raw_rt
        for _ in range(32):
            candidate = refresh_tokens._derive_rotation_token(candidate)
            if current_hash and hmac.compare_digest(
                refresh_tokens._hash_token(candidate),
                str(current_hash),
            ):
                next_raw = candidate
                break
        else:
            # A pre-cutover random child cannot be reconstructed from its
            # parent. Preserve the browser's cookies rather than turning this
            # recoverable response race into a logout.
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "Refresh retry superseded"},
                headers={"Cache-Control": "no-store", "Vary": "Cookie"},
            )
    elif result_status not in {"rotated", "exact_replay"}:
        return _refresh_failure_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired or revoked",
        )

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
    _set_no_store(response)
    return RefreshOut(expires_in=at_seconds)


@router.get("/methods")
def get_auth_methods(response: Response):
    _set_no_store(response)
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
    reject_cross_origin_form_post(request)
    settings = get_settings()
    if _control_plane_url(settings):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted local password login is disabled")
    if not settings.longhouse_password and not settings.longhouse_password_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password auth not configured")

    client_ip = get_client_ip(request)
    retry_after = _check_password_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    # Reserve the attempt before expensive password verification so concurrent
    # requests cannot all pass the admission check and queue CPU work.

    try:
        password_ok = await _verify_password_attempt(
            password=body.password,
            plain_password=settings.longhouse_password,
            password_hash=settings.longhouse_password_hash,
        )
    except BaseException:
        _release_password_attempt(client_ip)
        raise

    if not password_ok:
        _record_password_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
    _release_password_attempt(client_ip)
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

    client_ip = get_client_ip(request)
    retry_after = _check_password_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        password_ok = await _verify_password_attempt(
            password=body.password,
            plain_password=settings.longhouse_password,
            password_hash=settings.longhouse_password_hash,
        )
    except BaseException:
        _release_password_attempt(client_ip)
        raise

    if not password_ok:
        _record_password_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
    _release_password_attempt(client_ip)
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
    reset_attempt: int = 0,
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
        safe_return_to = normalize_local_return_to(return_to) or "/timeline"
        redirect = RedirectResponse(
            f"/login?return_to={urllib.parse.quote(safe_return_to, safe='')}",
            status_code=302,
        )
        redirect.headers["cache-control"] = "no-store"
        redirect.headers["referrer-policy"] = "no-referrer"
        return redirect
    if not hosted_cookie_origin_is_secure(settings):
        logger.error("hosted_auth_requires_https_public_origin")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_misconfigured", "message": "Hosted authentication requires an HTTPS public origin."},
        )
    cookie_secure = tenant_cookie_secure(settings)
    attempt_cookie_name = tenant_handoff_attempt_cookie_name(secure=cookie_secure)
    attempt_count = _handoff_attempt_count(
        request.cookies.get(attempt_cookie_name),
        reset=reset_attempt > 0,
    )
    if attempt_count >= _HANDOFF_ATTEMPT_MAX_COUNT:
        safe_return_to = normalize_local_return_to(return_to) or "/timeline"
        query = urllib.parse.urlencode(
            {"return_to": safe_return_to, "auth_error": "cookie_loop"},
        )
        redirect = RedirectResponse(f"/login?{query}", status_code=303)
        redirect.headers["cache-control"] = "no-store"
        redirect.headers["referrer-policy"] = "no-referrer"
        redirect.delete_cookie(
            attempt_cookie_name,
            path="/",
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )
        return redirect
    next_attempt = f"{attempt_count + 1}:{time.time()}"

    canonical_tenant = hosted_instance_id().strip().lower()
    requested_tenant = (tenant or "").strip().lower()
    if requested_tenant and requested_tenant != canonical_tenant:
        logger.warning("hosted_auth_tenant_override_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant does not match this runtime",
        )
    resolved_tenant = canonical_tenant
    tenant_state, login_cookie_name, login_cookie_secret = new_tenant_login_state(secure=cookie_secure)
    existing_login_cookies = sorted(name for name in request.cookies if is_tenant_login_cookie_name(name, secure=cookie_secure))
    safe_return_to = normalize_local_return_to(return_to) or "/timeline"

    cp_base = control_plane_url.rstrip("/")
    target = f"{cp_base}/auth/start"
    params: list[tuple[str, str]] = [("return_to", safe_return_to), ("tenant_state", tenant_state)]
    if resolved_tenant:
        params.append(("tenant", resolved_tenant))
    target += "?" + urllib.parse.urlencode(params)

    redirect = RedirectResponse(target, status_code=302)
    redirect.headers["cache-control"] = "no-store"
    redirect.headers["referrer-policy"] = "no-referrer"
    stale_count = max(0, len(existing_login_cookies) - (MAX_TENANT_LOGIN_ATTEMPTS - 1))
    for stale_cookie in existing_login_cookies[:stale_count]:
        redirect.delete_cookie(
            stale_cookie,
            path="/",
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )
    redirect.set_cookie(
        login_cookie_name,
        login_cookie_secret,
        max_age=TENANT_LOGIN_ATTEMPT_MAX_AGE,
        path="/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )
    redirect.set_cookie(
        attempt_cookie_name,
        next_attempt,
        max_age=_HANDOFF_ATTEMPT_MAX_AGE,
        path="/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )
    login_attempt_marker = request.cookies.get(tenant_login_attempt_cookie_name(secure=cookie_secure)) or "1"
    redirect.set_cookie(
        tenant_login_attempt_cookie_name(secure=cookie_secure),
        login_attempt_marker,
        max_age=TENANT_LOGIN_ATTEMPT_MAX_AGE,
        path="/",
        httponly=False,
        secure=cookie_secure,
        samesite="lax",
    )
    return redirect


__all__ = [
    "CLILoginRequest",
    "PasswordLoginRequest",
    "_resolve_password_user",
    "router",
]
