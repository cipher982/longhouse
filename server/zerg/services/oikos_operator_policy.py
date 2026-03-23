"""Operator-mode policy helpers backed by user context preferences."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.user import User


@dataclass(frozen=True)
class OikosOperatorPolicy:
    """Small user-scoped policy surface for proactive Oikos operator mode."""

    enabled: bool
    shadow_mode: bool = True
    allow_continue: bool = False
    allow_notify: bool = True
    allow_small_repairs: bool = False


def operator_master_switch_enabled() -> bool:
    return os.getenv("OIKOS_OPERATOR_MODE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def policy_from_user_context(context: dict[str, Any] | None) -> OikosOperatorPolicy:
    """Build the effective operator policy from a user's stored context."""

    master_enabled = operator_master_switch_enabled()
    prefs = (context or {}).get("preferences", {}) or {}
    operator_prefs_raw = prefs.get("operator_mode", {}) or {}
    operator_prefs = operator_prefs_raw if isinstance(operator_prefs_raw, dict) else {}

    return OikosOperatorPolicy(
        enabled=master_enabled and _coerce_bool(operator_prefs.get("enabled"), True),
        shadow_mode=_coerce_bool(operator_prefs.get("shadow_mode"), True),
        allow_continue=_coerce_bool(operator_prefs.get("allow_continue"), False),
        allow_notify=_coerce_bool(operator_prefs.get("allow_notify"), True),
        allow_small_repairs=_coerce_bool(operator_prefs.get("allow_small_repairs"), False),
    )


def get_operator_policy(db: Session, owner_id: int) -> OikosOperatorPolicy:
    """Read the effective operator policy for a specific owner."""

    user = db.query(User.context).filter(User.id == owner_id).first()
    if user is None:
        return policy_from_user_context(None)
    return policy_from_user_context(user[0] or {})
