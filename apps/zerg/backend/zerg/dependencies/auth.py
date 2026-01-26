"""FastAPI dependencies that expose the *current user* and *admin guard*.

The heavy lifting (development bypass vs. JWT validation) is implemented in
strategy classes under :pymod:`zerg.auth.strategy`.  At *import time* we pick
the concrete implementation based on :pydata:`settings.auth_disabled` so that
the actual request handlers remain branch-free and therefore faster and
easier to test.
"""

from __future__ import annotations

import hmac

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.strategy import DevAuthStrategy
from zerg.auth.strategy import JWTAuthStrategy
from zerg.auth.strategy import _decode_jwt_fallback as _decode_jwt_fallback  # type: ignore
from zerg.config import get_settings
from zerg.database import get_db

# ---------------------------------------------------------------------------
# Choose strategy once per interpreter – no per-request branching.
# ---------------------------------------------------------------------------


# Settings ------------------------------------------------------------------

_settings = get_settings()

# External tests patch this constant to toggle dev ↔ prod behaviour.  We keep
# the flag for backwards compatibility even though the new strategy pattern
# renders it largely redundant.
AUTH_DISABLED: bool = _settings.auth_disabled  # noqa: N816 – keep legacy name

# The JWT secret is still re-exported so the test-suite can decode tokens via
# the fallback helper.
JWT_SECRET: str = _settings.jwt_secret  # noqa: N816 – legacy export

# Also expose the tiny decoder for tests that want to introspect JWT content.


# Dev e-mail constant used in a handful of assertions
DEV_EMAIL: str = "dev@local"  # noqa: N816 – keep legacy name


# ---------------------------------------------------------------------------
# Strategy selector – returns singleton per mode, toggles when flag patched.
# ---------------------------------------------------------------------------


_strategy_cache: dict[str, object] = {}


def _get_strategy():  # noqa: D401 – internal helper
    """Return *singleton* strategy instance based on ``AUTH_DISABLED`` flag."""

    global AUTH_DISABLED  # tests might monkeypatch the flag at runtime

    if AUTH_DISABLED:
        if "dev" not in _strategy_cache:
            _strategy_cache["dev"] = DevAuthStrategy()
        return _strategy_cache["dev"]  # type: ignore[return-value]

    if "jwt" not in _strategy_cache:
        _strategy_cache["jwt"] = JWTAuthStrategy()
    return _strategy_cache["jwt"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public dependencies
# ---------------------------------------------------------------------------


def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated *User* row or raise **401**.

    Accepts auth from:
    1. Authorization: Bearer <token> header
    2. swarmlet_session cookie (browser auth)
    """
    # Check for either bearer token or session cookie
    has_bearer = "Authorization" in request.headers
    has_cookie = "swarmlet_session" in request.cookies

    if not has_bearer and not has_cookie and not AUTH_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _get_strategy().get_current_user(request, db)


def require_admin(current_user=Depends(get_current_user)):
    """FastAPI dependency that ensures the user has role == ``ADMIN``."""

    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")

    return current_user


def require_super_admin(current_user=Depends(get_current_user)):
    """FastAPI dependency that ensures the user is in ADMIN_EMAILS list (super admin)."""

    # First check if they're an admin
    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")

    # In test/dev environments with auth disabled, any admin user is considered super admin
    settings = get_settings()
    if settings.auth_disabled or settings.testing:
        return current_user

    # Then check if they're a super admin (in ADMIN_EMAILS)
    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    user_email = getattr(current_user, "email", "").lower()

    if user_email not in admin_emails:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin privileges required")

    return current_user


def require_internal_call(request: Request):
    """FastAPI dependency that ensures the call is internal (backend-to-backend).

    Internal endpoints should only be called from within the backend process,
    not exposed to external clients. This uses a shared secret token approach:
    1. In production, requires X-Internal-Token header matching INTERNAL_API_SECRET
    2. In dev mode (auth disabled), allows all calls (trusted environment)

    This is a security measure for endpoints like /internal/runs/{course_id}/continue
    that are called by background tasks.
    """
    settings = get_settings()

    # In dev mode with auth disabled, allow all internal calls (trusted environment)
    if settings.auth_disabled:
        return True

    # In production, require shared secret token
    internal_token = request.headers.get("X-Internal-Token")
    expected_token = settings.internal_api_secret

    if not expected_token:
        # INTERNAL_API_SECRET not configured - fail secure
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal API secret not configured")

    if internal_token and hmac.compare_digest(internal_token, expected_token):
        return True

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Internal endpoint - external access forbidden")


# ---------------------------------------------------------------------------
# WebSocket authentication helper
# ---------------------------------------------------------------------------


def validate_ws_jwt(token: str | None, db: Session):
    """Return user for a valid WebSocket token – *None* when invalid."""

    return _get_strategy().validate_ws_token(token, db)


# ---------------------------------------------------------------------------
# Re-export strategy so tests can monkey-patch
# ---------------------------------------------------------------------------


# Expose the strategy getter for testing monkey-patching
_strategy = _get_strategy

__all__ = [
    "get_current_user",
    "require_admin",
    "require_super_admin",
    "require_internal_call",
    "validate_ws_jwt",
    "_strategy",  # exported for test monkey-patching
]
