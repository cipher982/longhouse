"""Browser-session login and status routes for tenant auth."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from collections import deque
from datetime import timedelta
from typing import Any
from typing import Callable
from typing import TypeVar

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth import refresh_tokens
from zerg.auth.session_tokens import ACCESS_TOKEN_LIFETIME
from zerg.auth.session_tokens import REFRESH_COOKIE_NAME
from zerg.auth.session_tokens import _clear_refresh_cookie
from zerg.auth.session_tokens import _clear_session_cookie
from zerg.auth.session_tokens import _issue_access_token
from zerg.auth.session_tokens import _set_refresh_cookie
from zerg.auth.session_tokens import _set_session_cookie
from zerg.config import get_settings
from zerg.crud import count_users
from zerg.crud import create_user
from zerg.crud import get_connectors
from zerg.crud import get_user_by_email
from zerg.crud import update_user
from zerg.database import get_db
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_auth import get_optional_browser_user
from zerg.routers.auth_gmail import _gmail_setup_state
from zerg.routers.auth_gmail import _normalize_email_address
from zerg.schemas.schemas import TokenOut
from zerg.services.write_serializer import get_write_serializer

router = APIRouter(prefix="/auth", tags=["auth"])

# Refresh token cookie max-age: 90 days (matches absolute lifetime in refresh_tokens module).
_REFRESH_COOKIE_MAX_AGE = 90 * 24 * 60 * 60
_RefreshWriteResult = TypeVar("_RefreshWriteResult")


async def _run_refresh_session_write(
    db: Session,
    fn: Callable[[Session], _RefreshWriteResult],
) -> _RefreshWriteResult:
    ws = get_write_serializer()
    return await ws.execute_or_direct(fn, db, label="refresh-session")


async def _issue_session(
    response: Response,
    db: Session,
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
    _set_session_cookie(response, access_token, at_seconds)

    raw_rt = await _run_refresh_session_write(db, lambda current_db: refresh_tokens.create(current_db, user_id=user.id))
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
async def dev_login(response: Response, db: Session = Depends(get_db)) -> TokenOut:
    settings = get_settings()
    if not settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev login only available when AUTH_DISABLED=1",
        )

    user = get_user_by_email(db, "dev@local")
    if not user:
        user = create_user(
            db,
            email="dev@local",
            provider="dev",
            provider_user_id="dev-user-1",
            role="ADMIN",
            skip_notification=True,
        )

    return await _issue_session(response, db, user, display_name=user.display_name or "Dev User")


@router.post("/service-login", response_model=TokenOut, include_in_schema=False)
async def service_login(request: Request, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    settings = get_settings()
    secret = request.headers.get("X-Service-Secret") or ""
    expected = settings.smoke_test_secret or ""
    run_id = (request.headers.get("X-Smoke-Run-Id") or "").strip()

    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    email = "smoke@service.local"
    provider_user_id = "smoke-test"
    display_name = "Smoke Test" + (f" ({run_id[:20]})" if run_id else "")

    user = get_user_by_email(db, email)
    if not user:
        try:
            user = create_user(
                db,
                email=email,
                provider="service",
                provider_user_id=provider_user_id,
                role="USER",
                skip_notification=True,
            )
        except Exception:
            db.rollback()
            user = get_user_by_email(db, email)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create service user",
                )

    return await _issue_session(response, db, user, display_name=display_name)


@router.post("/google", response_model=TokenOut)
async def google_sign_in(response: Response, body: dict[str, str], db: Session = Depends(get_db)) -> TokenOut:
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

    user = get_user_by_email(db, email)
    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    is_admin = email.lower() in admin_emails

    if not user:
        if settings.single_tenant and not settings.testing:
            from zerg.services.single_tenant import can_create_user_locked

            if not can_create_user_locked(db):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Single-tenant mode: instance already has an owner. Cannot create additional users.",
                )

        if not settings.testing and not is_admin:
            total = count_users(db)
            if settings.max_users and total >= settings.max_users:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Sign-ups disabled: user limit reached",
                )

        role = "ADMIN" if is_admin else "USER"
        user = create_user(db, email=email, provider="google", provider_user_id=sub, role=role)
    else:
        if is_admin and getattr(user, "role", None) != "ADMIN":
            try:
                _ = update_user(db, user.id, display_name=user.display_name)
                user.role = "ADMIN"  # type: ignore[assignment]
                db.commit()
                db.refresh(user)
            except Exception:
                pass

    return await _issue_session(response, db, user)


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def verify_session(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if settings.auth_disabled:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    get_current_browser_user(request, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    user = get_optional_browser_user(request, db)
    if not user:
        return {"authenticated": False, "user": None}

    gmail_connectors = get_connectors(db, owner_id=user.id, type="email", provider="gmail")
    gmail_connector = gmail_connectors[0] if gmail_connectors else None
    gmail_config = dict(gmail_connector.config or {}) if gmail_connector else {}
    gmail_connector_connected = bool(gmail_config.get("refresh_token"))
    gmail_connected = bool(gmail_connector_connected or getattr(user, "gmail_connected", False))

    gmail_watch_status = gmail_config.get("watch_status")
    gmail_watch_error = gmail_config.get("watch_error")

    if gmail_connector_connected and gmail_watch_status not in {"active", "failed", "not_configured"}:
        gmail_watch_status = "failed"
        gmail_watch_error = gmail_watch_error or "Reconnect Gmail to finish email sync."

    return {
        "authenticated": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": getattr(user, "display_name", None),
            "avatar_url": getattr(user, "avatar_url", None),
            "is_active": getattr(user, "is_active", True),
            "created_at": getattr(user, "created_at", None),
            "last_login": getattr(user, "last_login", None),
            "prefs": getattr(user, "prefs", None),
            "role": getattr(user, "role", "USER"),
            "gmail_connected": gmail_connected,
            "gmail_mailbox_email": _normalize_email_address(gmail_config.get("emailAddress")),
            "gmail_watch_status": gmail_watch_status,
            "gmail_watch_error": gmail_watch_error,
            "gmail_watch_expiry": gmail_config.get("watch_expiry"),
        },
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    # Revoke the refresh token family so the RT can't be replayed after logout.
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_rt:

        def _revoke_refresh_cookie_family(current_db: Session) -> None:
            token_hash = refresh_tokens._hash_token(raw_rt)
            row = current_db.query(refresh_tokens.RefreshSession).filter_by(token_hash=token_hash).first()
            if row:
                refresh_tokens.revoke_family(current_db, row.family_id)

        await _run_refresh_session_write(db, _revoke_refresh_cookie_family)

    _clear_session_cookie(response)
    _clear_refresh_cookie(response)


@router.post("/refresh", response_model=TokenOut)
async def refresh_session(request: Request, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    """Exchange a valid refresh token for a new access token + rotated refresh token.

    This is the silent-refresh endpoint called by the frontend on 401.
    """
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_rt:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    result = await _run_refresh_session_write(db, lambda current_db: refresh_tokens.rotate(current_db, raw_rt))
    if result is None:
        # Token invalid, expired, or revoked — clear cookies and force re-login.
        _clear_session_cookie(response)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired or revoked")

    from zerg.crud import get_user

    user = get_user(db, result.user_id)
    if user is None or not getattr(user, "is_active", True):
        await _run_refresh_session_write(
            db,
            lambda current_db: refresh_tokens.revoke_family(current_db, result.family_id),
        )
        _clear_session_cookie(response)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    at_seconds = int(ACCESS_TOKEN_LIFETIME.total_seconds())
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=getattr(user, "display_name", None),
        avatar_url=getattr(user, "avatar_url", None),
    )
    _set_session_cookie(response, access_token, at_seconds)
    _set_refresh_cookie(response, result.raw_token, _REFRESH_COOKIE_MAX_AGE)

    return TokenOut(access_token=access_token, expires_in=at_seconds)


@router.get("/methods")
def get_auth_methods():
    settings = get_settings()
    gmail_ready, gmail_setup_message = _gmail_setup_state(settings)
    sso_base = settings.control_plane_url.rstrip("/") if settings.control_plane_url else None
    return {
        "google": bool(settings.google_client_id) and not bool(settings.control_plane_url),
        "password": bool(settings.longhouse_password or settings.longhouse_password_hash),
        "sso": bool(settings.control_plane_url),
        "sso_url": sso_base,
        "sso_login_url": f"{sso_base}/dashboard/open-instance" if sso_base else None,
        "gmail_ready": gmail_ready,
        "gmail_setup_message": gmail_setup_message,
    }


class PasswordLoginRequest(BaseModel):
    password: str


def _resolve_password_user(db: Session):
    settings = get_settings()

    if settings.single_tenant and not settings.testing:
        import os

        from fastapi import HTTPException
        from fastapi import status

        from zerg.models import User
        from zerg.services.single_tenant import get_owner_email

        owner_email = get_owner_email().strip().lower()
        user = get_user_by_email(db, owner_email)
        if user:
            return user

        from sqlalchemy import or_

        # NB: provider is nullable; `!= "service"` excludes NULL in SQL, so OR in
        # the NULL case to avoid missing a real pre-existing owner.
        existing = db.query(User).filter(or_(User.provider != "service", User.provider.is_(None))).order_by(User.id.asc()).first()
        if existing:
            # When OWNER_EMAIL is explicitly configured, password auth is bound
            # to that identity — a different existing user is a real misconfig,
            # so fail closed. But a password-auth self-hoster with NO explicit
            # OWNER_EMAIL is just upgrading a prior local (no-auth) instance:
            # adopt the single existing owner instead of locking them out.
            owner_email_explicit = bool(os.getenv("OWNER_EMAIL", "").strip())
            if owner_email_explicit:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Password auth is bound to the configured owner. Existing user does not match OWNER_EMAIL.",
                )
            return existing

        return create_user(db, email=owner_email, provider="password", role="ADMIN", skip_notification=True)

    user = get_user_by_email(db, "local@longhouse")
    if not user:
        user = create_user(db, email="local@longhouse", provider="password", skip_notification=True)
    return user


@router.post("/password", response_model=TokenOut)
async def password_login(
    request: Request,
    response: Response,
    body: PasswordLoginRequest,
    db: Session = Depends(get_db),
) -> TokenOut:
    settings = get_settings()
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
    user = _resolve_password_user(db)

    return await _issue_session(response, db, user, display_name=user.display_name or "Local User")


class CLILoginRequest(BaseModel):
    password: str


@router.post("/cli-login")
def cli_login(
    request: Request,
    body: CLILoginRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    settings = get_settings()
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
    user = _resolve_password_user(db)
    access_token = _issue_access_token(
        user.id,
        user.email,
        expires_delta=timedelta(minutes=5),
    )
    return {"token": access_token}


__all__ = [
    "CLILoginRequest",
    "PasswordLoginRequest",
    "_resolve_password_user",
    "router",
]
