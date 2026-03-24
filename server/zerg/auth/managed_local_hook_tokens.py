"""Session-scoped auth tokens for managed-local hook traffic.

These tokens are intentionally narrower than durable device tokens:
- scoped to one Longhouse session id
- optionally scoped to one project for SessionStart context lookup
- short enough-lived to avoid embedding an indefinite credential in runner job logs
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _encode_jwt
from zerg.auth.strategy import _decode_jwt_fallback

MANAGED_LOCAL_HOOK_TOKEN_KIND = "managed_local_hook"
MANAGED_LOCAL_HOOK_TOKEN_PREFIX = "zht_"
MANAGED_LOCAL_HOOK_TOKEN_LIFETIME = timedelta(hours=72)


@dataclass(frozen=True)
class ManagedLocalHookToken:
    owner_id: int
    session_id: str
    project: str | None = None
    device_id: str | None = None
    expires_at: datetime | None = None


def issue_managed_local_hook_token(
    *,
    owner_id: int,
    session_id: str,
    project: str | None,
    device_id: str | None,
    expires_delta: timedelta = MANAGED_LOCAL_HOOK_TOKEN_LIFETIME,
) -> str:
    """Issue a signed session-scoped hook token."""

    normalized_session_id = str(session_id).strip()
    UUID(normalized_session_id)
    normalized_project = str(project or "").strip() or None
    normalized_device_id = str(device_id or "").strip() or None
    expiry = datetime.now(timezone.utc) + expires_delta
    payload: dict[str, Any] = {
        "sub": str(int(owner_id)),
        "sid": normalized_session_id,
        "typ": MANAGED_LOCAL_HOOK_TOKEN_KIND,
        "exp": int(expiry.timestamp()),
    }
    if normalized_project is not None:
        payload["prj"] = normalized_project
    if normalized_device_id is not None:
        payload["did"] = normalized_device_id
    return MANAGED_LOCAL_HOOK_TOKEN_PREFIX + _encode_jwt(payload, JWT_SECRET)


def validate_managed_local_hook_token(token: str) -> ManagedLocalHookToken | None:
    """Validate a signed managed-local hook token."""

    raw = str(token or "").strip()
    if not raw.startswith(MANAGED_LOCAL_HOOK_TOKEN_PREFIX):
        return None
    encoded = raw[len(MANAGED_LOCAL_HOOK_TOKEN_PREFIX) :].strip()
    if not encoded:
        return None

    try:
        from jose import jwt  # type: ignore

        payload = jwt.decode(encoded, JWT_SECRET, algorithms=["HS256"])
    except ModuleNotFoundError:
        try:
            payload = _decode_jwt_fallback(encoded, JWT_SECRET)
        except Exception:
            return None
    except Exception:
        return None

    if str(payload.get("typ") or "") != MANAGED_LOCAL_HOOK_TOKEN_KIND:
        return None

    try:
        owner_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        return None

    session_id = str(payload.get("sid") or "").strip()
    if not session_id:
        return None
    try:
        UUID(session_id)
    except ValueError:
        return None

    project = str(payload.get("prj") or "").strip() or None
    device_id = str(payload.get("did") or "").strip() or None
    expires_at_raw = payload.get("exp")
    expires_at = None
    if isinstance(expires_at_raw, (int, float)):
        expires_at = datetime.fromtimestamp(float(expires_at_raw), tz=timezone.utc)

    return ManagedLocalHookToken(
        owner_id=owner_id,
        session_id=session_id,
        project=project,
        device_id=device_id,
        expires_at=expires_at,
    )


__all__ = [
    "MANAGED_LOCAL_HOOK_TOKEN_KIND",
    "MANAGED_LOCAL_HOOK_TOKEN_LIFETIME",
    "MANAGED_LOCAL_HOOK_TOKEN_PREFIX",
    "ManagedLocalHookToken",
    "issue_managed_local_hook_token",
    "validate_managed_local_hook_token",
]
