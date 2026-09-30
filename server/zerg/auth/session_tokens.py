"""Shared tenant JWT + browser session cookie helpers."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional

import jwt
from fastapi import Response
from zerg.auth.hosted import refresh_cookie_name
from zerg.auth.hosted import refresh_cookie_path
from zerg.auth.hosted import session_cookie_name
from zerg.auth.strategy import SESSION_TOKEN_KIND
from zerg.config import get_settings

_settings = get_settings()

JWT_SECRET = _settings.jwt_secret
SESSION_COOKIE_PATH = "/"

# Cookie names and the ``Secure`` flag follow the request's scheme (see
# ``zerg.auth.hosted.cookie_secure_for_scheme``), so every helper takes the
# ``secure`` decision the caller made for its request.
LEGACY_SESSION_COOKIE_NAME = "longhouse_session"
LEGACY_REFRESH_COOKIE_NAME = "longhouse_refresh"


def _clear_legacy_cookies(response: Response, *, secure: bool) -> None:
    """Retire pre-__Host cookies when a secure browser touches auth."""
    if not secure:
        return  # the bare names are the live ones
    response.delete_cookie(
        key=LEGACY_SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie(
        key=LEGACY_REFRESH_COOKIE_NAME,
        path="/api/auth",
        httponly=True,
        secure=True,
        samesite="lax",
    )


# Access token lifetime — kept short; refresh tokens handle longevity.
ACCESS_TOKEN_LIFETIME = timedelta(minutes=10)


def _set_session_cookie(response: Response, token: str, max_age: int, *, secure: bool) -> None:
    """Set the browser session cookie with the standard Longhouse flags."""
    response.set_cookie(
        key=session_cookie_name(secure),
        value=token,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    _clear_legacy_cookies(response, secure=secure)


def _clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Clear the browser session cookie."""
    response.delete_cookie(
        key=session_cookie_name(secure),
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    _clear_legacy_cookies(response, secure=secure)


def _set_refresh_cookie(response: Response, token: str, max_age: int, *, secure: bool) -> None:
    """Set the refresh token cookie (scoped to /api/auth unless ``__Host-`` forces ``/``)."""
    response.set_cookie(
        key=refresh_cookie_name(secure),
        value=token,
        max_age=max_age,
        path=refresh_cookie_path(secure),
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    _clear_legacy_cookies(response, secure=secure)


def _clear_refresh_cookie(response: Response, *, secure: bool) -> None:
    """Clear the refresh token cookie."""
    response.delete_cookie(
        key=refresh_cookie_name(secure),
        path=refresh_cookie_path(secure),
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    _clear_legacy_cookies(response, secure=secure)


def _issue_access_token(
    user_id: int,
    email: str,
    *,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    expires_delta: timedelta = ACCESS_TOKEN_LIFETIME,
) -> str:
    """Return signed HS256 access token including optional profile fields."""
    expiry = datetime.now(timezone.utc) + expires_delta
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "typ": SESSION_TOKEN_KIND,
        "exp": int(expiry.timestamp()),
    }

    if display_name is not None:
        payload["display_name"] = display_name

    if avatar_url is not None:
        payload["avatar_url"] = avatar_url

    return _encode_jwt(payload, JWT_SECRET)


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    """Encode a compact HS256 JWT."""

    return jwt.encode(payload, secret, algorithm="HS256")


__all__ = [
    "ACCESS_TOKEN_LIFETIME",
    "JWT_SECRET",
    "SESSION_TOKEN_KIND",
    "_clear_refresh_cookie",
    "_clear_session_cookie",
    "_encode_jwt",
    "_issue_access_token",
    "_set_refresh_cookie",
    "_set_session_cookie",
]
