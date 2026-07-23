"""Canonical user-owned session preferences from the bounded live catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID

from zerg.models.live_store import LiveSessionCatalog


@dataclass(frozen=True)
class SessionPreferences:
    user_state: str = "active"
    loop_mode: str = "assist"
    notification_muted: bool = False
    user_hidden_from_timeline: bool = False


def load_session_preferences(session_id: UUID | str, *, standalone_session=None) -> SessionPreferences:
    """Load preferences from live state; standalone test databases use their local row."""

    from zerg import database as database_module

    if not database_module.live_store_configured():
        return SessionPreferences(
            user_state=str(getattr(standalone_session, "user_state", None) or "active"),
            loop_mode=str(getattr(standalone_session, "loop_mode", None) or "assist"),
            notification_muted=bool(getattr(standalone_session, "notification_muted", False)),
            user_hidden_from_timeline=bool(getattr(standalone_session, "user_hidden_from_timeline", False)),
        )

    if database_module.live_catalog_enabled():
        facts = getattr(standalone_session, "catalog_facts", None)
        catalog = facts.get("catalog") if isinstance(facts, dict) else None
        if isinstance(catalog, dict):
            return SessionPreferences(
                user_state=str(catalog.get("user_state") or "active"),
                loop_mode=str(catalog.get("loop_mode") or "assist"),
                notification_muted=catalog.get("notification_muted") is True,
                user_hidden_from_timeline=catalog.get("user_hidden_from_timeline") is True,
            )
        from zerg.services.catalog_read_gateway import session_snapshot

        result = session_snapshot(str(session_id))
        facts = result.get("facts") if result.get("found") is True else None
        catalog = facts.get("catalog") if isinstance(facts, dict) else None
        if not isinstance(catalog, dict):
            return SessionPreferences()
        return SessionPreferences(
            user_state=str(catalog.get("user_state") or "active"),
            loop_mode=str(catalog.get("loop_mode") or "assist"),
            notification_muted=catalog.get("notification_muted") is True,
            user_hidden_from_timeline=catalog.get("user_hidden_from_timeline") is True,
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
            user_hidden_from_timeline=bool(row.user_hidden_from_timeline),
        )


async def update_session_preferences(
    session_id: UUID | str,
    *,
    user_state: str | None = None,
    loop_mode: str | None = None,
    notification_muted: bool | None = None,
    user_hidden_from_timeline: bool | None = None,
) -> SessionPreferences | None:
    """Update session preferences through catalogd without opening SQLite here."""

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise RuntimeError("Live session catalog is unavailable")
    result = await catalogd.call(
        "session.preferences.update.v2",
        {
            "session_id": str(session_id),
            "user_state": user_state,
            "loop_mode": loop_mode,
            "notification_muted": notification_muted,
            "user_hidden_from_timeline": user_hidden_from_timeline,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
        timeout_seconds=1.0,
    )
    if result.get("found") is not True:
        return None
    preferences = result.get("preferences")
    if not isinstance(preferences, dict):
        raise RuntimeError("Live session catalog returned invalid preferences")
    return SessionPreferences(
        user_state=str(preferences.get("user_state") or "active"),
        loop_mode=str(preferences.get("loop_mode") or "assist"),
        notification_muted=preferences.get("notification_muted") is True,
        user_hidden_from_timeline=preferences.get("user_hidden_from_timeline") is True,
    )
