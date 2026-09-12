"""Shared helpers for hosted tenant auth flows."""

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi import status
from zerg.config import get_settings
from zerg.config import normalize_instance_id

logger = logging.getLogger(__name__)

# The per-attempt secret is a CSRF binding and must expire with the OAuth
# transaction. The generation marker is non-sensitive and survives longer so
# a dashboard-originated handoff can still be associated with the latest
# explicit login/logout action.
TENANT_LOGIN_STATE_MAX_AGE = 10 * 60
TENANT_LOGIN_ATTEMPT_MAX_AGE = 30 * 24 * 60 * 60
TENANT_LOGIN_COOKIE_PREFIX = "__Host-lh_login_"
TENANT_HANDOFF_ATTEMPT_COOKIE = "__Host-lh_handoff_attempt"
MAX_TENANT_LOGIN_ATTEMPTS = 4
# Current control-plane state is URL-safe ``[A-Za-z0-9_-]``. Legacy releases
# used ``.`` between the fixed-width attempt id and secret; retain that
# bounded grammar during rolling deploys so an in-flight callback is not
# discarded solely because one tier upgraded first.
MAX_TENANT_LOGIN_STATE_LENGTH = 128
_ATTEMPT_ID_RE = re.compile(r"^[0-9]{13}_[0-9a-f]{12}$")
_ATTEMPT_ID_LENGTH = 26
_STATE_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_STATE_SEPARATOR = "-"
_STATE_SEPARATORS = {"-", "."}


def _is_local_cookie_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _is_loopback_cookie_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def tenant_cookie_secure(settings=None) -> bool:
    """Choose cookie generation without weakening hosted tenant isolation.

    Hosted tenants always use ``Secure``/``__Host-`` cookies. Insecure cookies
    are only available for auth-disabled/test surfaces or explicitly local
    self-host URLs, where there is no shared public parent domain to protect.
    """
    if settings is None:
        settings = get_settings()
    hosted = bool(getattr(settings, "control_plane_url", None))
    if hosted:
        # Hosted auth must remain secure even when a test/development setting
        # accidentally accompanies CONTROL_PLANE_URL.
        if os.getenv("LONGHOUSE_COOKIE_SECURE", "").strip().lower() in {"0", "false", "no", "off"}:
            logger.warning("ignoring insecure cookie override for hosted tenant")
        return True
    if bool(getattr(settings, "auth_disabled", False) or getattr(settings, "testing", False)):
        return False

    override = os.getenv("LONGHOUSE_COOKIE_SECURE", "").strip().lower()
    public_url = getattr(settings, "public_site_url", None) or getattr(settings, "app_public_url", None)
    parsed_url = urlparse(str(public_url)) if public_url else None
    host = parsed_url.hostname if parsed_url else None
    is_local_host = _is_local_cookie_host(host)

    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        if is_local_host:
            return False
        logger.warning("ignoring insecure cookie override for non-local host")
        return True
    if parsed_url and parsed_url.scheme.lower() == "http" and is_local_host:
        return False
    return True


def hosted_cookie_origin_is_secure(settings=None) -> bool:
    """Return whether hosted auth can set its required secure cookies."""
    if settings is None:
        settings = get_settings()
    if not getattr(settings, "control_plane_url", None):
        return True
    public_url = getattr(settings, "public_site_url", None) or getattr(settings, "app_public_url", None)
    if not public_url:
        logger.error("hosted_auth_public_origin_missing")
        return False
    parsed = urlparse(str(public_url))
    return parsed.scheme.lower() == "https" or _is_loopback_cookie_host(parsed.hostname)


def tenant_login_ready_cookie_name(*, secure: bool) -> str:
    """Name the short-lived client-visible successful-handoff signal."""
    return "__Host-lh_login_ready" if secure else "lh_login_ready"


def tenant_login_attempt_cookie_name(*, secure: bool) -> str:
    """Name the short-lived client-visible marker for a started login."""
    return "__Host-lh_login_attempt" if secure else "lh_login_attempt"


def tenant_login_cookie_prefix(*, secure: bool) -> str:
    return "__Host-lh_login_" if secure else "lh_login_"


def is_tenant_login_cookie_name(name: str, *, secure: bool) -> bool:
    """Return whether ``name`` is an actual per-attempt state cookie.

    The login-ready and login-attempt markers share the historical prefix but
    are not outstanding OAuth attempts and must not consume attempt slots.
    """

    prefix = tenant_login_cookie_prefix(secure=secure)
    return bool(_ATTEMPT_ID_RE.fullmatch(name.removeprefix(prefix))) if name.startswith(prefix) else False


def tenant_handoff_attempt_cookie_name(*, secure: bool) -> str:
    return "__Host-lh_handoff_attempt" if secure else "lh_handoff_attempt"


def new_tenant_login_state(*, secure: bool | None = None) -> tuple[str, str, str]:
    """Return (state, cookie_name, cookie_secret) for one browser attempt.

    The URL state carries only a public nonce. The cookie value is an HMAC of
    that state under the tenant's server-side internal secret, so a callback
    URL capture cannot forge the browser binding.
    """
    if secure is None:
        secure = tenant_cookie_secure()
    attempt_id = f"{int(time.time() * 1000):013d}_{secrets.token_hex(6)}"
    nonce = secrets.token_urlsafe(32)
    state = f"{attempt_id}{_STATE_SEPARATOR}{nonce}"
    cookie_secret = tenant_login_cookie_secret(state)
    if cookie_secret is None:
        raise RuntimeError("Unable to derive tenant login cookie binding")
    return state, f"{tenant_login_cookie_prefix(secure=secure)}{attempt_id}", cookie_secret


def _split_tenant_login_state(state: str | None) -> tuple[str, str] | None:
    if not isinstance(state, str) or len(state) > MAX_TENANT_LOGIN_STATE_LENGTH:
        return None
    if len(state) <= _ATTEMPT_ID_LENGTH or state[_ATTEMPT_ID_LENGTH] not in _STATE_SEPARATORS:
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
    """Return the server-bound cookie secret for a valid login state."""
    parsed = _split_tenant_login_state(state)
    if parsed is None:
        return None
    attempt_id, nonce = parsed
    canonical_state = f"{attempt_id}{_STATE_SEPARATOR}{nonce}"
    settings = get_settings()
    binding_secret = str(getattr(settings, "internal_api_secret", "") or "")
    if not binding_secret:
        raise RuntimeError("INTERNAL_API_SECRET is required for hosted login state")
    return hmac.new(
        binding_secret.encode("utf-8"),
        canonical_state.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hosted_instance_id() -> str:
    """Return the deployment's explicit tenant identity.

    A public hostname is presentation data, not an authority identifier.
    Deriving the tenant from its first DNS label makes a mistyped apex or
    shared ingress address look like a valid tenant, so hosted deployments
    must configure INSTANCE_ID explicitly.
    """

    instance_id = normalize_instance_id(os.getenv("INSTANCE_ID"))
    if instance_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INSTANCE_ID is not configured",
        )
    return instance_id
