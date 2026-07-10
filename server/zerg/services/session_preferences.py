"""Canonical user-owned session preferences from the bounded live catalog."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from zerg.models.live_store import LiveSessionCatalog


@dataclass(frozen=True)
class SessionPreferences:
    user_state: str = "active"
    loop_mode: str = "assist"
    notification_muted: bool = False


def load_session_preferences(session_id: UUID | str, *, standalone_session=None) -> SessionPreferences:
    """Load preferences from live state; standalone test databases use their local row."""

    from zerg import database as database_module

    if not database_module.live_store_configured():
        return SessionPreferences(
            user_state=str(getattr(standalone_session, "user_state", None) or "active"),
            loop_mode=str(getattr(standalone_session, "loop_mode", None) or "assist"),
            notification_muted=bool(getattr(standalone_session, "notification_muted", False)),
        )

    factory = database_module.get_live_session_factory()
    if factory is None:
        raise RuntimeError("Live session catalog is unavailable")
    with factory() as live_db:
        row = live_db.get(LiveSessionCatalog, str(session_id))
        if row is None:
            return SessionPreferences()
        return SessionPreferences(
            user_state=str(row.user_state or "active"),
            loop_mode=str(row.loop_mode or "assist"),
            notification_muted=bool(row.notification_muted),
        )
