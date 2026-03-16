"""Device-token dependencies for machine-owned agents surfaces."""

from __future__ import annotations

import logging

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.models.device_token import DeviceToken

logger = logging.getLogger(__name__)


def verify_agents_token(request: Request, db: Session = Depends(get_db)) -> DeviceToken | None:
    """Verify the agents API token for write operations."""
    settings = get_settings()

    if settings.auth_disabled:
        return None

    provided_token = request.headers.get("X-Agents-Token")
    if not provided_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication - provide X-Agents-Token header",
        )

    if not provided_token.startswith("zdt_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked device token",
        )

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
    "verify_agents_token",
]
