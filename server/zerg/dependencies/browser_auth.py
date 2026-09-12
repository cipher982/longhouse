"""Cookie-session dependencies for browser-owned tenant routes."""

from __future__ import annotations

import os

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

import zerg.dependencies.auth as auth_deps
from zerg.auth.caller import Caller
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.config import get_settings
from zerg.database import catalog_db_session
from zerg.dependencies.form_post_origin import require_browser_auth_header

# Compatibility seam for tests/extensions; catalog_db_session chooses the live
# catalog in Runtime Hosts and the read-only archive in helper processes.
db_session = catalog_db_session


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _stamp_principal(request: Request, user):
    """Record who this request resolved to, for the access log.

    The access-log middleware reads scope["state"], which the route handler
    shares, so stamping here is what turns every log line from "anonymous"
    into an answer to "who read this transcript".
    """
    if user is not None:
        try:
            request.state.principal = f"user:{user.id}"
        except Exception:  # pragma: no cover - state is always present in ASGI
            pass
    return user


def _get_browser_session_user(request: Request, db=None):
    """Validate one explicit credential or the browser session cookie."""
    if auth_deps.AUTH_DISABLED:
        return _stamp_principal(request, auth_deps._get_strategy().get_current_user(request, db))

    auth_header = request.headers.get("Authorization")
    bearer = _bearer_token(request)
    if auth_header is not None:
        # An explicit credential is terminal. In particular, never turn a
        # malformed or invalid device/JWT bearer into a valid cookie session.
        if bearer is None:
            return None
        if bearer.startswith("zdt_"):
            user = auth_deps._get_strategy().validate_ws_token(bearer, db)
        elif getattr(get_settings(), "control_plane_url", None):
            user = auth_deps._get_strategy().validate_ws_token(bearer, db)
        else:
            user = None
        return _stamp_principal(request, user)

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = auth_deps._get_strategy().validate_ws_token(session_token, db)
        if user is not None:
            return _stamp_principal(request, user)

    return None


def get_current_browser_user(request: Request, db=Depends(auth_deps._auth_compat_db)):
    """Return the authenticated browser user or raise **401**."""
    is_mutation = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    auth_header = request.headers.get("Authorization")
    if not auth_deps.AUTH_DISABLED and auth_header is not None:
        user = _get_browser_session_user(request, db)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired authorization credential",
            )
        return user
    if is_mutation and auth_header is None:
        require_browser_auth_header(request)
    user = _get_browser_session_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_current_browser_caller(user=Depends(get_current_browser_user)) -> Caller:
    """Return the browser principal through the shared owner-scoped boundary."""

    try:
        owner_id = int(user.id)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated browser identity has no valid owner id",
        ) from None
    return Caller(owner_id=owner_id, principal=user)


def _get_current_browser_user_short_lived(request: Request):
    """Authenticate a browser stream without pinning a DB connection.

    FastAPI keeps generator dependencies alive until a streaming response ends.
    Timeline SSE routes only need auth at connect time, so use an explicit
    context-managed session instead of a dependency-managed one.
    """
    if get_settings().testing or os.getenv("NODE_ENV") == "test":
        with db_session() as db:
            user = _get_browser_session_user(request, db)
    else:
        user = _get_browser_session_user(request)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_current_browser_user_id_short_lived(request: Request) -> int:
    user = _get_current_browser_user_short_lived(request)
    return int(user.id)


def get_current_browser_caller_short_lived(
    owner_id: int = Depends(get_current_browser_user_id_short_lived),
) -> Caller:
    return Caller(owner_id=int(owner_id))


def require_current_browser_user_short_lived(request: Request) -> None:
    _get_current_browser_user_short_lived(request)


def get_optional_browser_user(request: Request, db=Depends(auth_deps._auth_compat_db)):
    """Return the authenticated browser user or **None**."""
    return _get_browser_session_user(request, db)


__all__ = [
    "_get_browser_session_user",
    "get_current_browser_user",
    "get_current_browser_caller",
    "get_current_browser_caller_short_lived",
    "get_current_browser_user_id_short_lived",
    "get_optional_browser_user",
    "require_current_browser_user_short_lived",
]
