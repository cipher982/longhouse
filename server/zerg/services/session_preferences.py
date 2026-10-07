"""Canonical user-owned session preferences from the bounded live catalog."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID

logger = logging.getLogger(__name__)
_stamp_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class SessionPreferences:
    user_state: str = "active"
    notification_muted: bool = False
    user_hidden_from_timeline: bool = False
    last_read_at: datetime | None = None
    last_user_input_at: datetime | None = None
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
    last_user_input_at: datetime | None = None,
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
            "last_user_input_at": last_user_input_at.isoformat() if last_user_input_at is not None else None,
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
        last_user_input_at=_parse_optional_datetime(preferences.get("last_user_input_at")),
    )


async def stamp_owner_input(session_id: UUID | str, *, owner_id: int, client_request_id: str) -> None:
    """Record that the owner sent input; Recent sorts by it.

    Only owner-facing composer routes call this, after the route returned.
    Machine, directed-input, wake and notice paths never do
    (docs/specs/recent-by-last-user-input.md). The stamp is the input
    receipt's own creation time: a request rejected before a receipt existed
    stamps nothing, and an idempotent replay re-stamps the original time,
    which the max-write ignores. Ordering is cosmetic, so a failure logs and
    never fails the send.
    """

    from zerg.services.live_session_inputs import load_live_input_receipt_by_client_request

    try:
        receipt = await load_live_input_receipt_by_client_request(
            owner_id=owner_id,
            session_id=session_id,
            client_request_id=client_request_id,
        )
        if receipt is None or receipt.created_at is None:
            return
        await update_session_preferences(session_id, owner_id=owner_id, last_user_input_at=receipt.created_at)
    except Exception:
        logger.warning("Failed to stamp owner input on session %s", session_id, exc_info=True)


def stamp_owner_input_soon(session_id: UUID | str, *, owner_id: int, client_request_id: str) -> None:
    """Schedule :func:`stamp_owner_input` without delaying the send path."""

    task = asyncio.create_task(stamp_owner_input(session_id, owner_id=owner_id, client_request_id=client_request_id))
    _stamp_tasks.add(task)
    task.add_done_callback(_stamp_tasks.discard)


def _parse_optional_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
