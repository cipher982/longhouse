"""Canonical user-owned session preferences from the bounded live catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID


@dataclass(frozen=True)
class SessionPreferences:
    user_state: str = "active"
    notification_muted: bool = False
    user_hidden_from_timeline: bool = False
    last_read_at: datetime | None = None
    read_through_rejected: bool = False


def load_session_preferences(
    session_id: UUID | str,
    *,
    owner_id: int | None,
    standalone_session=None,
) -> SessionPreferences:
    """Load preferences from live state; standalone test databases use their local row.

    ``owner_id`` is mandatory because the catalog branch below is a real read
    of another user's row when it is missing. Callers hold an owner already;
    an unresolvable owner reads as canonical defaults, never as the catalog.
    """

    from zerg import database as database_module

    if not database_module.live_store_configured():
        return SessionPreferences(
            user_state=str(getattr(standalone_session, "user_state", None) or "active"),
            notification_muted=bool(getattr(standalone_session, "notification_muted", False)),
            user_hidden_from_timeline=bool(getattr(standalone_session, "user_hidden_from_timeline", False)),
        )

    facts = getattr(standalone_session, "catalog_facts", None)
    catalog = facts.get("catalog") if isinstance(facts, dict) else None
    if isinstance(catalog, dict):
        return SessionPreferences(
            user_state=str(catalog.get("user_state") or "active"),
            notification_muted=catalog.get("notification_muted") is True,
            user_hidden_from_timeline=bool(catalog.get("user_hidden_from_timeline")),
        )
    if owner_id is None:
        return SessionPreferences()
    from zerg.services.catalog_read_gateway import session_snapshot

    result = session_snapshot(str(session_id), owner_id=int(owner_id))
    facts = result.get("facts") if result.get("found") is True else None
    catalog = facts.get("catalog") if isinstance(facts, dict) else None
    if not isinstance(catalog, dict):
        return SessionPreferences()
    return SessionPreferences(
        user_state=str(catalog.get("user_state") or "active"),
        notification_muted=catalog.get("notification_muted") is True,
        user_hidden_from_timeline=bool(catalog.get("user_hidden_from_timeline")),
    )


async def update_session_preferences(
    session_id: UUID | str,
    *,
    owner_id: int,
    user_state: str | None = None,
    notification_muted: bool | None = None,
    user_hidden_from_timeline: bool | None = None,
    last_read_at: datetime | None = None,
) -> SessionPreferences | None:
    """Update session preferences through catalogd without opening SQLite here.

    ``owner_id`` is the write predicate, not decoration: catalogd refuses the
    call without it and answers ``found: False`` for a session bound to anyone
    else, so a non-owner cannot tell "not yours" from "never existed".
    """

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise RuntimeError("Live session catalog is unavailable")
    result = await catalogd.call(
        "session.preferences.update.v2",
        {
            "session_id": str(session_id),
            "owner_id": int(owner_id),
            "user_state": user_state,
            "notification_muted": notification_muted,
            "user_hidden_from_timeline": user_hidden_from_timeline,
            "last_read_at": last_read_at.isoformat() if last_read_at is not None else None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
        timeout_seconds=1.0,
    )
    if result.get("found") is not True:
        return None
    if result.get("read_through_rejected") is True:
        return SessionPreferences(read_through_rejected=True)
    preferences = result.get("preferences")
    if not isinstance(preferences, dict):
        raise RuntimeError("Live session catalog returned invalid preferences")
    return SessionPreferences(
        user_state=str(preferences.get("user_state") or "active"),
        notification_muted=preferences.get("notification_muted") is True,
        user_hidden_from_timeline=preferences.get("user_hidden_from_timeline") is True,
        last_read_at=_parse_optional_datetime(preferences.get("last_read_at")),
    )


def _parse_optional_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
