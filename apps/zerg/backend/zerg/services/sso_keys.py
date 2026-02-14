"""Runtime SSO key fetching from the control plane.

Instances fetch signing keys from the control plane at runtime (cached with
TTL) so that secret rotations propagate automatically without reprovisioning.

Fallback chain:
  1. Fresh fetch from control plane
  2. Stale cache (extended 60s grace period on fetch failure)
  3. CONTROL_PLANE_JWT_SECRET env var (backward compat)
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from zerg.config import get_settings

logger = logging.getLogger(__name__)

# Module-level cache --------------------------------------------------------

_lock = threading.Lock()
_cached_keys: list[str] | None = None
_cached_at: float = 0.0
_cache_ttl: float = 300.0  # 5 minutes (server-specified TTL used when available)
_stale_grace: float = 60.0  # Extra seconds to serve stale on fetch failure
_MIN_TTL: float = 30.0  # Floor to prevent tight refetch loops
_MAX_TTL: float = 3600.0  # Ceiling to prevent excessively stale keys


def _fetch_keys_from_cp() -> tuple[list[str], float]:
    """Fetch SSO keys from the control plane.

    Returns (keys, ttl_seconds).
    Raises on any network/auth error or malformed response.
    """
    settings = get_settings()
    url = f"{settings.control_plane_url.rstrip('/')}/api/instances/sso-keys"

    # INSTANCE_ID env var is set by provisioner (subdomain); fall back to app URL
    instance_id = os.getenv("INSTANCE_ID") or settings.app_public_url or ""
    headers = {
        "X-Instance-Id": instance_id,
        "X-Internal-Secret": settings.internal_api_secret,
    }
    resp = httpx.get(url, headers=headers, timeout=5.0)
    resp.raise_for_status()
    data = resp.json()

    # Validate response shape
    keys = data.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) and k for k in keys):
        raise ValueError(f"Invalid SSO keys response: expected list of non-empty strings, got {type(keys)}")

    ttl = max(_MIN_TTL, min(_MAX_TTL, float(data.get("ttl_seconds", 300))))
    return keys, ttl


def get_sso_keys() -> list[str]:
    """Return current SSO signing keys (cached, auto-refreshing).

    Thread-safe.  On fetch failure, serves stale cache for up to
    ``_stale_grace`` seconds beyond normal TTL, then falls back to
    the CONTROL_PLANE_JWT_SECRET env var.
    """
    global _cached_keys, _cached_at, _cache_ttl

    settings = get_settings()

    # No control plane URL configured — use env var directly
    if not settings.control_plane_url:
        if settings.control_plane_jwt_secret:
            return [settings.control_plane_jwt_secret]
        return []

    now = time.monotonic()
    fresh_deadline = _cached_at + _cache_ttl
    stale_deadline = _cached_at + _cache_ttl + _stale_grace

    # Cache is still fresh — return immediately
    if _cached_keys is not None and now < fresh_deadline:
        return list(_cached_keys)

    # Cache expired or missing — try to refresh
    with _lock:
        # Double-check after acquiring lock (another thread may have refreshed)
        now = time.monotonic()
        if _cached_keys is not None and now < _cached_at + _cache_ttl:
            return list(_cached_keys)

        try:
            keys, ttl = _fetch_keys_from_cp()
            _cached_keys = keys
            _cached_at = time.monotonic()
            _cache_ttl = ttl
            logger.info("SSO keys refreshed from control plane (%d keys, ttl=%ds)", len(keys), int(ttl))
            return list(keys)
        except Exception:
            logger.warning("Failed to fetch SSO keys from control plane", exc_info=True)

            # Serve stale cache within grace period
            if _cached_keys is not None and now < stale_deadline:
                logger.info("Serving stale SSO keys (within grace period)")
                return list(_cached_keys)

    # Final fallback: env var
    if settings.control_plane_jwt_secret:
        logger.info("SSO keys: falling back to CONTROL_PLANE_JWT_SECRET env var")
        return [settings.control_plane_jwt_secret]

    return []


def prefetch_sso_keys() -> None:
    """Warm the SSO key cache at startup. Non-fatal on failure."""
    settings = get_settings()
    if not settings.control_plane_url:
        logger.info("SSO key prefetch skipped (no CONTROL_PLANE_URL)")
        return

    try:
        keys = get_sso_keys()
        logger.info("SSO key prefetch complete (%d keys)", len(keys))
    except Exception:
        logger.warning("SSO key prefetch failed (will retry on first auth request)", exc_info=True)


def _reset_cache() -> None:
    """Reset the module-level cache (for testing only)."""
    global _cached_keys, _cached_at, _cache_ttl
    with _lock:
        _cached_keys = None
        _cached_at = 0.0
        _cache_ttl = 300.0
