"""Cookie-session dependencies for browser-owned tenant routes."""

from __future__ import annotations

import os

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.config import get_settings
from zerg.database import catalog_db_session

# Compatibility seam for tests/extensions; catalog_db_session chooses the live
# catalog in Runtime Hosts and the read-only archive in helper processes.
db_session = catalog_db_session


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _device_token_bearer(request: Request) -> str | None:
    """Return a `zdt_...` device token from the Authorization header, or None.

    Browser routes deliberately reject generic JWT bearers in self-host — that's
    the cookie boundary. Device tokens are a separate, owner-scoped credential
    issued by the CLI; allowing them lets the local dev proxy drive the UI
    without a browser cookie.
    """
    token = _bearer_token(request)
    return token if token and token.startswith("zdt_") else None


def _hosted_runtime_bearer(request: Request) -> str | None:
    token = _bearer_token(request)
    if not token or token.startswith("zdt_"):
        return None
    if not getattr(get_settings(), "control_plane_url", None):
        return None
    return token


def _get_browser_session_user(request: Request, db=None):
    """Validate browser auth (cookie or device-token bearer) and return user."""
    if auth_deps.AUTH_DISABLED:
        return auth_deps._get_strategy().get_current_user(request, db)

    hosted_bearer = _hosted_runtime_bearer(request)
    if hosted_bearer:
        user = auth_deps._get_strategy().validate_ws_token(hosted_bearer, db)
        if user is not None:
            return user

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = auth_deps._get_strategy().validate_ws_token(session_token, db)
        if user is not None:
            return user

    device_token = _device_token_bearer(request)
    if device_token:
        return auth_deps._get_strategy().validate_ws_token(device_token, db)

    return None


def get_current_browser_user(request: Request, db=Depends(auth_deps._auth_compat_db)):
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
    if get_settings().testing or os.getenv("NODE_ENV") == "test":
        with db_session() as db:
            user = _get_browser_session_user(request, db)
    else:
        user = _get_browser_session_user(request)

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


def get_optional_browser_user(request: Request, db=Depends(auth_deps._auth_compat_db)):
    """Return the authenticated browser user or **None**."""
    return _get_browser_session_user(request, db)


__all__ = [
    "_get_browser_session_user",
    "get_current_browser_user",
    "get_current_browser_user_id_short_lived",
    "get_optional_browser_user",
    "require_current_browser_user_short_lived",
]
