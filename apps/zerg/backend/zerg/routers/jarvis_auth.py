"""Jarvis authentication helpers.

Authentication dependency and helpers for Jarvis endpoints.
"""

import logging

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db

logger = logging.getLogger(__name__)


def get_current_jarvis_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Query(
        None,
        description="Optional JWT token (used by EventSource/SSE which can't send Authorization headers).",
    ),
):
    """Resolve the authenticated user for Jarvis endpoints.

    SaaS model: Jarvis is just another client UI and uses standard auth.

    - For normal fetch/XHR: use `Authorization: Bearer <token>`
    - For SSE/EventSource: pass `token=<jwt>` as a query param
    """
    from zerg.dependencies.auth import _get_strategy

    if token:
        user = _get_strategy().validate_ws_token(token, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    return _get_strategy().get_current_user(request, db)


def _is_tool_enabled(ctx: dict, tool_key: str) -> bool:
    """Check if a tool is enabled in user context."""
    tool_config = (ctx or {}).get("tools", {}) or {}
    return bool(tool_config.get(tool_key, True))


def _tool_key_from_mcp_call(name: str) -> str | None:
    """Extract tool key from MCP call name."""
    if name.startswith("location."):
        return "location"
    if name.startswith("whoop."):
        return "whoop"
    if name.startswith("obsidian."):
        return "obsidian"
    return None
