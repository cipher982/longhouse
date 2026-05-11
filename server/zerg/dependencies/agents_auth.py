"""Device-token dependencies for machine-owned agents surfaces."""

from __future__ import annotations

import logging

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.auth.managed_local_hook_tokens import validate_managed_local_hook_token
from zerg.config import get_settings
from zerg.database import get_db
from zerg.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

_MANAGED_LOCAL_HOOK_ALLOWED_ROUTES = {
    ("GET", "/agents/sessions"),
    ("GET", "/agents/sessions/startup-context"),
    ("POST", "/agents/ingest"),
    ("POST", "/agents/presence"),
}


def _normalized_agents_path(request: Request) -> str:
    path = request.url.path or ""
    if path.startswith("/api/"):
        return path[4:]
    return path


def _managed_local_hook_token_allowed(request: Request) -> bool:
    return (request.method.upper(), _normalized_agents_path(request)) in _MANAGED_LOCAL_HOOK_ALLOWED_ROUTES


def verify_agents_token(request: Request, db: Session = Depends(get_db)) -> DeviceToken | ManagedLocalHookToken | None:
    """Verify the agents API token for write operations."""
    settings = get_settings()
    if settings.auth_disabled:
        request.state.agents_rate_key = "auth-disabled"
        return None

    provided_token = request.headers.get("X-Agents-Token")
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
    else:
        hook_token = validate_managed_local_hook_token(provided_token)
        if hook_token:
            if not _managed_local_hook_token_allowed(request):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managed-local hook token is not allowed on this endpoint",
                )
            request.state.agents_rate_key = f"managed-local-hook:{hook_token.session_id}"
            return hook_token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked device token",
    )


def require_single_tenant() -> None:
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
