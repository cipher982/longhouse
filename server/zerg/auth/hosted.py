"""Shared helpers for hosted tenant auth flows."""

from __future__ import annotations

import os
import re
import secrets
import time
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi import status
from zerg.config import get_settings

# A login attempt is an independent host-only cookie. Shared mutable cookie
# lists lose transactions when two tabs start or finish out of order.
TENANT_LOGIN_COOKIE_PREFIX = "__Host-lh_login_"
TENANT_HANDOFF_ATTEMPT_COOKIE = "__Host-lh_handoff_attempt"
TENANT_LOGIN_ATTEMPT_MAX_AGE = 600
MAX_TENANT_LOGIN_ATTEMPTS = 4
MAX_TENANT_LOGIN_STATE_LENGTH = 256
_ATTEMPT_ID_RE = re.compile(r"^[0-9]{13}_[0-9a-f]{12}$")


def tenant_cookie_secure(settings=None) -> bool:
    """Use host-prefixed cookies only on real secure browser surfaces."""
    if settings is None:
        settings = get_settings()
    return not bool(getattr(settings, "auth_disabled", False) or getattr(settings, "testing", False))


def tenant_login_cookie_prefix(*, secure: bool) -> str:
    return "__Host-lh_login_" if secure else "lh_login_"


def tenant_handoff_attempt_cookie_name(*, secure: bool) -> str:
    return "__Host-lh_handoff_attempt" if secure else "lh_handoff_attempt"


def new_tenant_login_state(*, secure: bool | None = None) -> tuple[str, str, str]:
    """Return (state, cookie_name, cookie_secret) for one browser attempt."""
    if secure is None:
        secure = tenant_cookie_secure()
    attempt_id = f"{int(time.time() * 1000):013d}_{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    state = f"{attempt_id}.{secret}"
    return state, f"{tenant_login_cookie_prefix(secure=secure)}{attempt_id}", secret


def tenant_login_cookie_name(state: str | None, *, secure: bool) -> str | None:
    """Map a state to its isolated host-only cookie name, or reject it."""
    if not isinstance(state, str) or len(state) > MAX_TENANT_LOGIN_STATE_LENGTH:
        return None
    attempt_id, separator, secret = state.partition(".")
    if not separator or not secret or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        return None
    return f"{tenant_login_cookie_prefix(secure=secure)}{attempt_id}"


def tenant_login_cookie_secret(state: str | None) -> str | None:
    """Return the secret half of a syntactically valid login state."""
    if tenant_login_cookie_name(state, secure=True) is None:
        return None
    assert state is not None
    return state.split(".", 1)[1]


def hosted_instance_id() -> str:
    instance_id = os.getenv("INSTANCE_ID", "").strip()
    if instance_id:
        return instance_id

    settings = get_settings()
    public_url = settings.app_public_url or settings.public_site_url or ""
    if public_url:
        host = urlparse(public_url).hostname or ""
        if host:
            return host.split(".")[0]

    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="INSTANCE_ID is not configured")
