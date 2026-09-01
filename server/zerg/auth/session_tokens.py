"""Shared tenant JWT + browser session cookie helpers."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional

import jwt
from fastapi import Response
from zerg.auth.strategy import SESSION_COOKIE_NAME
from zerg.auth.strategy import SESSION_TOKEN_KIND
from zerg.config import get_settings

_settings = get_settings()

JWT_SECRET = _settings.jwt_secret
SESSION_COOKIE_PATH = "/"
SESSION_COOKIE_SECURE = not _settings.auth_disabled and not _settings.testing

# Refresh token cookie — scoped to auth endpoints only (minimises exposure).
REFRESH_COOKIE_NAME = "longhouse_refresh"
REFRESH_COOKIE_PATH = "/api/auth"

# Access token lifetime — kept short; refresh tokens handle longevity.
ACCESS_TOKEN_LIFETIME = timedelta(minutes=10)


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """Set the browser session cookie with the standard Longhouse flags."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    """Clear the browser session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _set_refresh_cookie(response: Response, token: str, max_age: int) -> None:
    """Set the refresh token cookie (scoped to /api/auth only)."""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clear the refresh token cookie."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


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
    "REFRESH_COOKIE_NAME",
    "REFRESH_COOKIE_PATH",
    "SESSION_COOKIE_NAME",
    "SESSION_TOKEN_KIND",
    "_clear_refresh_cookie",
    "_clear_session_cookie",
    "_encode_jwt",
    "_issue_access_token",
    "_set_refresh_cookie",
    "_set_session_cookie",
]
