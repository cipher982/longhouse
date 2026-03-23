"""Cookie-session dependencies for browser-owned tenant routes."""

from __future__ import annotations

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.database import get_db


def _get_browser_session_user(request: Request, db: Session):
    """Validate only the browser session cookie and return the authenticated user."""
    if auth_deps.AUTH_DISABLED:
        return auth_deps._get_strategy().get_current_user(request, db)

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None

    return auth_deps._get_strategy().validate_ws_token(session_token, db)


def get_current_browser_user(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated browser user or raise **401**."""
    user = _get_browser_session_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_optional_browser_user(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated browser user or **None**."""
    return _get_browser_session_user(request, db)


__all__ = [
    "_get_browser_session_user",
    "get_current_browser_user",
    "get_optional_browser_user",
]
