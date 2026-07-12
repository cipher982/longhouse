"""Authentication helpers for browser-owned API routes."""

from __future__ import annotations

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status

from zerg.config import get_settings
from zerg.dependencies.auth import _auth_compat_db
from zerg.dependencies.auth import _get_strategy
from zerg.dependencies.browser_auth import get_current_browser_user


def get_current_browser_route_user(
    request: Request,
    db=Depends(_auth_compat_db),
    token: str | None = Query(
        None,
        description="Optional JWT token (used by EventSource/SSE which can't send Authorization headers).",
    ),
):
    """Resolve the authenticated browser user for routes that also support SSE tokens."""
    if token:
        if getattr(get_settings(), "control_plane_url", None) and not token.startswith("zdt_"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Query tokens are not valid for hosted user auth",
            )
        user = _get_strategy().validate_ws_token(token, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    return get_current_browser_user(request, db)


__all__ = [
    "get_current_browser_route_user",
]
