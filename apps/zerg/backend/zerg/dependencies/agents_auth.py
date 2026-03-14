"""Device-token and mixed read-access dependencies for agents surfaces."""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

_settings = get_settings()


def verify_agents_token(request: Request, db: Session = Depends(get_db)) -> DeviceToken | None:
    """Verify the agents API token for write operations."""
    if _settings.auth_disabled:
        return None

    provided_token = request.headers.get("X-Agents-Token")
    if not provided_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided_token = auth_header[7:]

    if not provided_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication - provide X-Agents-Token header",
        )

    if provided_token.startswith("zdt_"):
        from zerg.routers.device_tokens import validate_device_token

        device_token = validate_device_token(provided_token, db)
        if device_token:
            logger.debug("Device token validated for device %s", device_token.device_id)
            request.state.agents_rate_key = f"device:{device_token.id}"
            return device_token

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked device token",
        )

    expected_token = _settings.agents_api_token
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agents API not configured - create a device token or set AGENTS_API_TOKEN env var",
        )

    if not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agents API token",
        )

    token_hash = hashlib.sha256(provided_token.encode()).hexdigest()
    request.state.agents_rate_key = f"token:{token_hash}"
    return None


def verify_agents_read_access(request: Request, db: Session = Depends(get_db)) -> None:
    """Verify read access for agents endpoints.

    Accepts:
    1. Browser cookie auth (longhouse_session) - for UI access
    2. Device tokens (zdt_...) - for programmatic access
    """
    if _settings.auth_disabled:
        return

    if SESSION_COOKIE_NAME in request.cookies:
        try:
            get_current_browser_user(request, db)
            return
        except HTTPException:
            pass

    verify_agents_token(request, db)


def require_single_tenant(db: Session = Depends(get_db)) -> None:
    """Enforce single-tenant mode for agents endpoints."""
    settings = get_settings()
    if settings.testing:
        return
    if settings.single_tenant:
        return

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Multi-tenant agents API not implemented. Set SINGLE_TENANT=1 or contact support.",
    )


__all__ = [
    "require_single_tenant",
    "verify_agents_read_access",
    "verify_agents_token",
]
