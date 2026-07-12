"""Small typed boundary between request authentication and catalogd."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi import status
from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.principal import AuthenticatedUser
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.client import call_catalogd_sync
from zerg.services.catalogd_supervisor import catalogd_paths

_AUTH_UNAVAILABLE_DETAIL = {
    "code": "catalog_unavailable",
    "message": "Catalog authentication is temporarily unavailable.",
}


def resolve_user(user_id: int, *, touch_last_login: bool) -> AuthenticatedUser | None:
    result = _call(
        "auth.user.get.v2",
        {"user_id": int(user_id), "touch_last_login": touch_last_login},
    )
    if result.get("found") is not True:
        return None
    return _principal(result.get("user"))


def resolve_device_token(token: str, *, touch_last_used: bool) -> AuthenticatedUser | None:
    result = _call(
        "auth.device.resolve.v2",
        {
            "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "touch_last_used": touch_last_used,
            "touch_interval_seconds": 300,
        },
    )
    if result.get("valid") is not True:
        return None
    return _principal(result.get("user"))


def resolve_control_plane_user(claims: CPTokenClaims) -> AuthenticatedUser:
    try:
        result = _call(
            "auth.user.resolve_cp.v2",
            {
                "cp_user_id": claims.cp_user_id,
                "email": claims.email,
                "email_verified": claims.email_verified,
                "display_name": claims.display_name,
                "avatar_url": claims.avatar_url,
            },
        )
    except CatalogRemoteError as exc:
        if exc.code == "conflict":
            reason = (exc.details or {}).get("reason") if isinstance(exc.details, dict) else None
            if reason == "email_unverified_link":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email must be verified") from exc
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account link conflict") from exc
        raise
    return _principal(result.get("user"))


def resolve_local_user(
    *,
    email: str,
    provider: str,
    provider_user_id: str | None,
    role: str,
    adopt_existing: bool,
    require_email_match: bool,
    max_users: int | None,
    promote_role: bool,
) -> AuthenticatedUser:
    try:
        result = _call(
            "auth.user.resolve_local.v2",
            {
                "email": email,
                "provider": provider,
                "provider_user_id": provider_user_id,
                "role": role,
                "adopt_existing": adopt_existing,
                "require_email_match": require_email_match,
                "max_users": max_users,
                "promote_role": promote_role,
            },
        )
    except CatalogRemoteError as exc:
        if exc.code == "conflict":
            reason = (exc.details or {}).get("reason") if isinstance(exc.details, dict) else None
            detail = "Single-tenant owner email does not match" if reason == "owner_email_mismatch" else "User limit reached"
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
        raise
    return _principal(result.get("user"))


def create_refresh(
    *,
    user_id: int,
    token_hash: str,
    family_id: str,
    parent_id: int | None,
    created_at: datetime,
    absolute_expires_at: datetime,
    idle_expires_at: datetime,
) -> dict[str, Any]:
    return _call(
        "auth.refresh.create.v2",
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "family_id": family_id,
            "parent_id": parent_id,
            "created_at": _aware_iso(created_at),
            "absolute_expires_at": _aware_iso(absolute_expires_at),
            "idle_expires_at": _aware_iso(idle_expires_at),
        },
    )


def rotate_refresh(
    *,
    token_hash: str,
    next_token_hash: str,
    now: datetime,
    idle_expires_at: datetime,
    reuse_grace_seconds: int,
) -> dict[str, Any]:
    result = _call(
        "auth.refresh.rotate.v2",
        {
            "token_hash": token_hash,
            "next_token_hash": next_token_hash,
            "now": _aware_iso(now),
            "idle_expires_at": _aware_iso(idle_expires_at),
            "reuse_grace_seconds": reuse_grace_seconds,
        },
    )
    if result.get("status") in {"rotated", "exact_replay"}:
        result = dict(result)
        result["user"] = _principal(result.get("user"))
    return result


def revoke_refresh_family(*, token_hash: str, now: datetime) -> dict[str, Any]:
    return _call(
        "auth.refresh.revoke_family.v2",
        {"token_hash": token_hash, "now": _aware_iso(now)},
    )


def update_user(
    *,
    user_id: int,
    display_name: str | None = None,
    avatar_url: str | None = None,
    prefs: dict[str, Any] | None = None,
    update_mask: list[str],
) -> dict[str, Any]:
    result = _call(
        "auth.user.update.v2",
        {
            "user_id": user_id,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "prefs": prefs,
            "update_mask": update_mask,
        },
    )
    if result.get("found") is True:
        result = dict(result)
        result["user"] = _principal(result.get("user"))
    return result


def _call(method: str, params: dict) -> dict:
    _database_path, socket_path = catalogd_paths()
    try:
        return call_catalogd_sync(socket_path, method, params=params, timeout_seconds=0.15)
    except CatalogRemoteError as exc:
        if exc.code != "conflict":
            raise _unavailable() from exc
        raise
    except CatalogUnavailable as exc:
        raise _unavailable() from exc


def _principal(payload: object) -> AuthenticatedUser:
    try:
        return AuthenticatedUser.from_payload(payload)
    except ValueError as exc:
        raise _unavailable("Catalog authentication returned an invalid response.") from exc


def _unavailable(message: str | None = None) -> HTTPException:
    detail = dict(_AUTH_UNAVAILABLE_DETAIL)
    if message:
        detail["message"] = message
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog auth datetime must include a timezone")
    return value.isoformat()


__all__ = [
    "create_refresh",
    "resolve_control_plane_user",
    "resolve_device_token",
    "resolve_local_user",
    "resolve_user",
    "revoke_refresh_family",
    "rotate_refresh",
    "update_user",
]
