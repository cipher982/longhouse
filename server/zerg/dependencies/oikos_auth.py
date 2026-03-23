"""Authentication helpers for browser-owned Oikos routes."""

from __future__ import annotations

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import _get_strategy
from zerg.dependencies.browser_auth import get_current_browser_user


def get_current_oikos_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Query(
        None,
        description="Optional JWT token (used by EventSource/SSE which can't send Authorization headers).",
    ),
):
    """Resolve the authenticated user for Oikos endpoints.

    Oikos is a browser-owned product surface and uses the normal browser
    session cookie for fetch/XHR traffic.

    - For normal fetch/XHR: use the `longhouse_session` cookie
    - For SSE/EventSource: pass `token=<jwt>` as a query param when cookies
      are not available
    """
    if token:
        user = _get_strategy().validate_ws_token(token, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    return get_current_browser_user(request, db)


def _is_tool_enabled(ctx: dict, tool_key: str) -> bool:
    """Check if a tool is enabled in user context."""
    tool_config = (ctx or {}).get("tools", {}) or {}
    return bool(tool_config.get(tool_key, True))


__all__ = [
    "_is_tool_enabled",
    "get_current_oikos_user",
]
