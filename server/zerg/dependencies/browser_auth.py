"""Cookie-session dependencies for browser-owned tenant routes."""

from __future__ import annotations

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.database import db_session
from zerg.database import get_db


def _device_token_bearer(request: Request) -> str | None:
    """Return a `zdt_...` device token from the Authorization header, or None.

    Browser routes deliberately reject generic JWT bearers — that's the cookie
    boundary. Device tokens are a separate, owner-scoped credential issued by
    the CLI; allowing them lets the local dev proxy drive the UI without a
    browser cookie. JWT bearers still fall through to None here.
    """
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if token.startswith("zdt_") else None


def _get_browser_session_user(request: Request, db: Session):
    """Validate browser auth (cookie or device-token bearer) and return user."""
    if auth_deps.AUTH_DISABLED:
        return auth_deps._get_strategy().get_current_user(request, db)

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = auth_deps._get_strategy().validate_ws_token(session_token, db)
        if user is not None:
            return user

    device_token = _device_token_bearer(request)
    if device_token:
        return auth_deps._get_strategy().validate_ws_token(device_token, db)

    return None


def get_current_browser_user(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated browser user or raise **401**."""
    user = _get_browser_session_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def _get_current_browser_user_short_lived(request: Request):
    """Authenticate a browser stream without pinning a DB connection.

    FastAPI keeps generator dependencies alive until a streaming response ends.
    Timeline SSE routes only need auth at connect time, so use an explicit
    context-managed session instead of a dependency-managed one.
    """
    with db_session() as db:
        user = _get_browser_session_user(request, db)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_current_browser_user_id_short_lived(request: Request) -> int:
    user = _get_current_browser_user_short_lived(request)
    return int(user.id)


def require_current_browser_user_short_lived(request: Request) -> None:
    _get_current_browser_user_short_lived(request)


def get_optional_browser_user(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated browser user or **None**."""
    return _get_browser_session_user(request, db)


__all__ = [
    "_get_browser_session_user",
    "get_current_browser_user",
    "get_current_browser_user_id_short_lived",
    "get_optional_browser_user",
    "require_current_browser_user_short_lived",
]
