"""Signed authority for one Longhouse-managed session.

The token is provider-neutral and carries an explicit scope. Hook adapters and
coordination adapters receive different tokens so model-accessible tools never
inherit permission-gate authority, and hook environments never inherit
agent-to-agent send authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _encode_jwt
from zerg.auth.strategy import _decode_jwt_fallback
from zerg.auth.strategy import _hosted_audience
from zerg.config import get_settings

MANAGED_SESSION_TOKEN_KIND = "managed_session"
MANAGED_SESSION_TOKEN_PREFIX = "zst_"
MANAGED_SESSION_TOKEN_LIFETIME = timedelta(hours=72)
MANAGED_SESSION_TOKEN_ISSUER = "longhouse-runtime-host"
MANAGED_SESSION_SELF_HOST_AUDIENCE = "self-host"
MANAGED_SESSION_SCOPE_HOOK = "hook"
MANAGED_SESSION_SCOPE_COORDINATION = "coordination"
MANAGED_SESSION_SCOPES = {
    MANAGED_SESSION_SCOPE_HOOK,
    MANAGED_SESSION_SCOPE_COORDINATION,
}


def _managed_session_audience() -> str:
    """Instance identity a managed-session token is bound to.

    Every hosted tenant container currently receives the same ``JWT_SECRET``, so
    a token minted for one tenant must not validate on another. ``INSTANCE_ID``
    is that tenant identity; hosted runtimes fail closed when it is unset.
    """

    settings = get_settings()
    if getattr(settings, "control_plane_url", None):
        return _hosted_audience(settings)
    return os.getenv("INSTANCE_ID", "").strip() or MANAGED_SESSION_SELF_HOST_AUDIENCE


@dataclass(frozen=True)
class ManagedSessionToken:
    owner_id: int
    session_id: str
    scope: str
    project: str | None = None
    device_id: str | None = None
    expires_at: datetime | None = None


def issue_managed_session_token(
    *,
    owner_id: int,
    session_id: str,
    project: str | None,
    device_id: str | None,
    scope: str,
    expires_delta: timedelta = MANAGED_SESSION_TOKEN_LIFETIME,
) -> str:
    """Issue signed authority for one session and one adapter scope."""

    normalized_session_id = str(session_id).strip()
    UUID(normalized_session_id)
    normalized_project = str(project or "").strip() or None
    normalized_device_id = str(device_id or "").strip() or None
    normalized_scope = str(scope or "").strip()
    if normalized_scope not in MANAGED_SESSION_SCOPES:
        raise ValueError(f"unsupported managed-session token scope: {normalized_scope}")
    expiry = datetime.now(timezone.utc) + expires_delta
    payload: dict[str, Any] = {
        "sub": str(int(owner_id)),
        "sid": normalized_session_id,
        "typ": MANAGED_SESSION_TOKEN_KIND,
        "scp": normalized_scope,
        "iss": MANAGED_SESSION_TOKEN_ISSUER,
        "aud": _managed_session_audience(),
        "exp": int(expiry.timestamp()),
    }
    if normalized_project is not None:
        payload["prj"] = normalized_project
    if normalized_device_id is not None:
        payload["did"] = normalized_device_id
    return MANAGED_SESSION_TOKEN_PREFIX + _encode_jwt(payload, JWT_SECRET)


def validate_managed_session_token(token: str) -> ManagedSessionToken | None:
    """Validate signed managed-session authority."""

    raw = str(token or "").strip()
    if not raw.startswith(MANAGED_SESSION_TOKEN_PREFIX):
        return None
    encoded = raw[len(MANAGED_SESSION_TOKEN_PREFIX) :].strip()
    if not encoded:
        return None

    try:
        expected_audience = _managed_session_audience()
    except Exception:
        return None

    try:
        from jose import jwt  # type: ignore

        payload = jwt.decode(
            encoded,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=expected_audience,
            issuer=MANAGED_SESSION_TOKEN_ISSUER,
        )
    except ModuleNotFoundError:
        try:
            payload = _decode_jwt_fallback(encoded, JWT_SECRET)
        except Exception:
            return None
    except Exception:
        return None

    if str(payload.get("typ") or "") != MANAGED_SESSION_TOKEN_KIND:
        return None
    # The fallback decoder checks signature and expiry only, so bind the token
    # to this instance here: the shared secret makes another tenant's token
    # otherwise indistinguishable from ours.
    if str(payload.get("iss") or "") != MANAGED_SESSION_TOKEN_ISSUER:
        return None
    if str(payload.get("aud") or "") != expected_audience:
        return None
    scope = str(payload.get("scp") or "").strip()
    if scope not in MANAGED_SESSION_SCOPES:
        return None

    try:
        owner_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        return None

    session_id = str(payload.get("sid") or "").strip()
    try:
        UUID(session_id)
    except ValueError:
        return None

    expires_at_raw = payload.get("exp")
    expires_at = None
    if isinstance(expires_at_raw, (int, float)):
        expires_at = datetime.fromtimestamp(float(expires_at_raw), tz=timezone.utc)

    return ManagedSessionToken(
        owner_id=owner_id,
        session_id=session_id,
        scope=scope,
        project=str(payload.get("prj") or "").strip() or None,
        device_id=str(payload.get("did") or "").strip() or None,
        expires_at=expires_at,
    )


__all__ = [
    "MANAGED_SESSION_SCOPE_COORDINATION",
    "MANAGED_SESSION_SCOPE_HOOK",
    "MANAGED_SESSION_TOKEN_ISSUER",
    "MANAGED_SESSION_TOKEN_KIND",
    "MANAGED_SESSION_TOKEN_LIFETIME",
    "MANAGED_SESSION_TOKEN_PREFIX",
    "ManagedSessionToken",
    "issue_managed_session_token",
    "validate_managed_session_token",
]
