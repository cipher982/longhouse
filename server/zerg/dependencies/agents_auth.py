"""Device-token dependencies for machine-owned agents surfaces."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.auth.managed_local_hook_tokens import validate_managed_local_hook_token
from zerg.config import get_settings
from zerg.database import get_session_factory
from zerg.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-token sliding-window rate limit for agents write endpoints.
#
# The device/hook token authorizes a trusted machine, but a buggy or runaway
# agent (or, if a token leaks, an attacker) can still flood the ingest/presence
# write path. This is a cheap in-process backstop: a sliding window per
# rate-key. Defaults are generous for healthy engines (which batch) and can be
# tuned via env. Disabled when auth is disabled (local/dev) or under TESTING.
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("AGENTS_RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("AGENTS_RATE_LIMIT_MAX_REQUESTS", "600"))
_rate_buckets: dict[str, deque[float]] = {}
_rate_lock = threading.Lock()


def _enforce_rate_limit(rate_key: str) -> None:
    """Sliding-window limiter keyed on the authenticated token. Raises 429."""
    if _RATE_LIMIT_MAX_REQUESTS <= 0:
        return
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_SECONDS
    with _rate_lock:
        # Bound memory: when the keyspace grows large, drop buckets whose entire
        # window has expired (idle/rotated tokens) so the dict can't grow without
        # limit across many device/hook tokens over the process lifetime.
        if len(_rate_buckets) > 4096:
            for key in [k for k, b in _rate_buckets.items() if not b or b[-1] < window_start]:
                del _rate_buckets[key]
        bucket = _rate_buckets.setdefault(rate_key, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            retry_after = max(1, int(_RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0])) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded for agents API token.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


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


def _validate_device_token_for_request(token: str) -> DeviceToken | None:
    """Validate a device token without holding a DB session for the request lifetime."""

    from zerg.routers.device_tokens import validate_device_token

    db = get_session_factory()()
    try:
        device_token = validate_device_token(token, db)
        if device_token is None:
            return None

        # Load the scalar fields used by downstream request handlers, then
        # detach the row so FastAPI does not keep this auth session checked out
        # while write-heavy endpoints wait on the SQLite WriteSerializer.
        _ = (
            device_token.id,
            device_token.owner_id,
            device_token.device_id,
            device_token.token_hash,
            device_token.created_at,
            device_token.last_used_at,
            device_token.revoked_at,
        )
        db.expunge(device_token)
        return device_token
    finally:
        db.close()


def verify_agents_token(request: Request) -> DeviceToken | ManagedLocalHookToken | None:
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
        device_token = _validate_device_token_for_request(provided_token)
        if device_token:
            logger.debug("Device token validated for device %s", device_token.device_id)
            rate_key = f"device:{device_token.id}"
            request.state.agents_rate_key = rate_key
            if not settings.testing:
                _enforce_rate_limit(rate_key)
            return device_token
    else:
        hook_token = validate_managed_local_hook_token(provided_token)
        if hook_token:
            if not _managed_local_hook_token_allowed(request):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managed-local hook token is not allowed on this endpoint",
                )
            rate_key = f"managed-local-hook:{hook_token.session_id}"
            request.state.agents_rate_key = rate_key
            if not settings.testing:
                _enforce_rate_limit(rate_key)
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
