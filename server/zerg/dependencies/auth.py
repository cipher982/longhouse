"""FastAPI dependencies that expose the *current user* and *admin guard*.

The heavy lifting (development bypass vs. JWT validation) is implemented in
strategy classes under :pymod:`zerg.auth.strategy`.  At *import time* we pick
the concrete implementation based on :pydata:`settings.auth_disabled` so that
the actual request handlers remain branch-free and therefore faster and
easier to test.
"""

from __future__ import annotations

import os

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.auth.strategy import SESSION_COOKIE_NAME
from zerg.auth.strategy import DevAuthStrategy
from zerg.auth.strategy import HostedCPAuthStrategy
from zerg.auth.strategy import JWTAuthStrategy
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

# ---------------------------------------------------------------------------
# Strategy selector – returns singleton per mode, toggles when flag patched.
# ---------------------------------------------------------------------------


_strategy_cache: dict[str, object] = {}


def _no_auth_db():
    """Production auth never asks FastAPI to open a SQLAlchemy session."""

    yield None


# Select the dependency graph once at startup. Ordinary focused tests retain
# their explicit ``get_db`` override, while the browser E2E Runtime Host uses
# the real catalog owner and must not open the retired archive dependency just
# because it also carries TESTING=1.
_e2e_catalog_auth = _settings.testing and _settings.environment == "test:e2e"
_auth_compat_db = get_db if ((_settings.testing or os.getenv("NODE_ENV") == "test") and not _e2e_catalog_auth) else _no_auth_db


def _get_strategy():  # noqa: D401 – internal helper
    """Return *singleton* strategy instance based on ``AUTH_DISABLED`` flag."""

    global AUTH_DISABLED  # tests might monkeypatch the flag at runtime

    if AUTH_DISABLED:
        if "dev" not in _strategy_cache:
            _strategy_cache["dev"] = DevAuthStrategy()
        return _strategy_cache["dev"]  # type: ignore[return-value]

    if getattr(_settings, "control_plane_url", None):
        if "hosted-cp" not in _strategy_cache:
            _strategy_cache["hosted-cp"] = HostedCPAuthStrategy()
        return _strategy_cache["hosted-cp"]  # type: ignore[return-value]

    if "jwt" not in _strategy_cache:
        _strategy_cache["jwt"] = JWTAuthStrategy()
    return _strategy_cache["jwt"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public dependencies
# ---------------------------------------------------------------------------


def get_current_user(request: Request, db=Depends(_auth_compat_db)):
    """Return the authenticated *User* row or raise **401**.

    Accepts auth from:
    1. Authorization: Bearer <token> header
    2. longhouse_session cookie (browser auth)
    """
    # Check for either bearer token or session cookie
    has_bearer = "Authorization" in request.headers
    has_cookie = SESSION_COOKIE_NAME in request.cookies

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


# ---------------------------------------------------------------------------
# WebSocket authentication helper
# ---------------------------------------------------------------------------


def validate_ws_jwt(token: str | None, db=None):
    """Return user for a valid WebSocket token – *None* when invalid."""

    return _get_strategy().validate_ws_token(token, db)


__all__ = [
    "get_current_user",
    "require_admin",
    "validate_ws_jwt",
]
