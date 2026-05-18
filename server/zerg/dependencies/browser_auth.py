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
from zerg.models.user import User
from zerg.routers.device_tokens import validate_device_token


def _get_user_from_device_token_header(request: Request, db: Session):
    """Allow `Authorization: Bearer zdt_...` to authenticate browser routes as the
    token's owner. Used by the local dev-proxy so the UI can run against a remote
    backend without a browser cookie. Read-only by virtue of the token's existing
    scope; no broader privileges are granted here."""
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("zdt_"):
        return None
    device_token = validate_device_token(token, db)
    if device_token is None:
        return None
    return db.query(User).filter(User.id == device_token.owner_id).first()


def _get_browser_session_user(request: Request, db: Session):
    """Validate only the browser session cookie and return the authenticated user."""
    if auth_deps.AUTH_DISABLED:
        return auth_deps._get_strategy().get_current_user(request, db)

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = auth_deps._get_strategy().validate_ws_token(session_token, db)
        if user is not None:
            return user

    return _get_user_from_device_token_header(request, db)


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
