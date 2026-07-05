"""Quota helpers for launch-era session actions.

The retired automation-run ledger no longer backs quota accounting. Keep this
module as a policy hook, but do not query dropped run tables.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from zerg.models.models import User as UserModel


def _is_admin(user: UserModel | None) -> bool:
    return getattr(user, "role", "USER") == "ADMIN"


def assert_can_start_run(db: Session, *, user: UserModel) -> None:
    """Compatibility hook for callers that still ask before starting work."""
    _ = (db, user)
    return
