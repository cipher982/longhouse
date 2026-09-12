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
# The control plane accepts the tenant nonce as an opaque URL-safe value
# matching [A-Za-z0-9_-]{1,128}. Keep the attempt id fixed-width so the tenant
# can derive the per-attempt cookie name without putting the secret in it.
MAX_TENANT_LOGIN_STATE_LENGTH = 128
_ATTEMPT_ID_RE = re.compile(r"^[0-9]{13}_[0-9a-f]{12}$")
_ATTEMPT_ID_LENGTH = 26
_STATE_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_STATE_SEPARATOR = "-"


def tenant_cookie_secure(settings=None) -> bool:
    """Choose secure cookie names from the effective public browser scheme.

    Production defaults to secure cookies. An explicit local HTTP public URL or
    ``LONGHOUSE_COOKIE_SECURE=0`` is required to opt out; this keeps an
    auth-enabled HTTPS deployment from silently falling back to legacy names.
    """
    if settings is None:
        settings = get_settings()
    if bool(getattr(settings, "auth_disabled", False) or getattr(settings, "testing", False)):
        return False

    override = os.getenv("LONGHOUSE_COOKIE_SECURE", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    if override in {"1", "true", "yes", "on"}:
        return True

    public_url = getattr(settings, "public_site_url", None) or getattr(settings, "app_public_url", None)
    scheme = urlparse(str(public_url)).scheme.lower() if public_url else ""
    if scheme == "http":
        return False
    return True


def tenant_login_cookie_prefix(*, secure: bool) -> str:
    return "__Host-lh_login_" if secure else "lh_login_"


def tenant_handoff_attempt_cookie_name(*, secure: bool) -> str:
    return "__Host-lh_handoff_attempt" if secure else "lh_handoff_attempt"


def new_tenant_login_state(*, secure: bool | None = None) -> tuple[str, str, str]:
    """Return (state, cookie_name, cookie_secret) for one browser attempt.

    The state is deliberately a single URL-safe opaque value. Its fixed-width
    attempt-id prefix lets the tenant use an isolated host-only cookie per tab,
    while the control plane can carry the value through its bounded state
    validator without a cross-repository grammar mismatch.
    """
    if secure is None:
        secure = tenant_cookie_secure()
    attempt_id = f"{int(time.time() * 1000):013d}_{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    state = f"{attempt_id}{_STATE_SEPARATOR}{secret}"
    return state, f"{tenant_login_cookie_prefix(secure=secure)}{attempt_id}", secret


def _split_tenant_login_state(state: str | None) -> tuple[str, str] | None:
    if not isinstance(state, str) or len(state) > MAX_TENANT_LOGIN_STATE_LENGTH:
        return None
    if len(state) <= _ATTEMPT_ID_LENGTH or state[_ATTEMPT_ID_LENGTH] != _STATE_SEPARATOR:
        return None
    attempt_id = state[:_ATTEMPT_ID_LENGTH]
    secret = state[_ATTEMPT_ID_LENGTH + 1 :]
    if not _ATTEMPT_ID_RE.fullmatch(attempt_id) or not _STATE_SECRET_RE.fullmatch(secret):
        return None
    return attempt_id, secret


def tenant_login_cookie_name(state: str | None, *, secure: bool) -> str | None:
    """Map a state to its isolated host-only cookie name, or reject it."""
    parsed = _split_tenant_login_state(state)
    if parsed is None:
        return None
    attempt_id, _secret = parsed
    return f"{tenant_login_cookie_prefix(secure=secure)}{attempt_id}"


def tenant_login_cookie_secret(state: str | None) -> str | None:
    """Return the secret half of a syntactically valid login state."""
    parsed = _split_tenant_login_state(state)
    return parsed[1] if parsed is not None else None


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
