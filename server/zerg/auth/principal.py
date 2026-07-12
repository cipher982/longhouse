"""Detached authenticated identity shared by every request transport."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class AuthenticatedUser:
    """Scalar user identity; never carries a SQLAlchemy session or ORM state."""

    id: int
    email: str
    provider: str | None = None
    provider_user_id: str | None = None
    cp_user_id: int | None = None
    email_verified: bool = True
    is_active: bool = True
    role: str = "USER"
    display_name: str | None = None
    avatar_url: str | None = None
    prefs: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    last_login: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_payload(cls, payload: object) -> AuthenticatedUser:
        if not isinstance(payload, dict):
            raise ValueError("catalog user must be an object")
        try:
            user_id = int(payload["id"])
            email = payload["email"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("catalog user identity is incomplete") from exc
        if user_id <= 0 or not isinstance(email, str) or not email:
            raise ValueError("catalog user identity is invalid")
        return cls(
            id=user_id,
            email=email,
            provider=_optional_string(payload.get("provider")),
            provider_user_id=_optional_string(payload.get("provider_user_id")),
            cp_user_id=_optional_int(payload.get("cp_user_id")),
            email_verified=_required_bool(payload, "email_verified"),
            is_active=_required_bool(payload, "is_active"),
            role=_required_string(payload, "role"),
            display_name=_optional_string(payload.get("display_name")),
            avatar_url=_optional_string(payload.get("avatar_url")),
            prefs=_optional_dict(payload.get("prefs")),
            context=_optional_dict(payload.get("context")) or {},
            last_login=_optional_datetime(payload.get("last_login")),
            created_at=_optional_datetime(payload.get("created_at")),
            updated_at=_optional_datetime(payload.get("updated_at")),
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("catalog user string field is invalid")
    return value


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"catalog user {key} is invalid")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("catalog user integer field is invalid")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog user integer field is invalid") from exc


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"catalog user {key} is invalid")
    return value


def _optional_dict(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("catalog user object field is invalid")
    return dict(value)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("catalog user datetime field is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("catalog user datetime must include a timezone")
    return parsed


__all__ = ["AuthenticatedUser"]
