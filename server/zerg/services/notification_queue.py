"""Deliver notification events queued by quiet-hours policy."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.notification_event import NotificationEvent
from zerg.services.apns_sender import NOTIFICATION_CHANNEL_APNS_IOS
from zerg.services.apns_sender import NOTIFICATION_EVENT_SESSION_BLOCKED
from zerg.services.apns_sender import NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER
from zerg.services.apns_sender import NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER
from zerg.services.apns_sender import prepare_session_attention_push
from zerg.services.apns_sender import prepare_session_blocked_reminder_push
from zerg.services.apns_sender import prepare_session_needs_answer_push
from zerg.services.apns_sender import record_notification_delivery_result
from zerg.services.apns_sender import rollback_session_attention_push_stamp
from zerg.services.apns_sender import send_session_attention_push
from zerg.services.session_pause_requests import load_active_pause_request_for_session
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay

logger = logging.getLogger(__name__)


def _is_queued_event(event: NotificationEvent) -> bool:
    results = dict(getattr(event, "channel_results", None) or {})
    return bool(results.get("queued")) and event.delivered_at is None and event.resolved_at is None


async def process_queued_notification_events(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attempt delivery for queued attention notifications whose eligible_at has passed."""
    now = now or datetime.now(timezone.utc)
    result = {"checked": 0, "delivered": 0, "resolved": 0, "skipped": 0}

    candidates = (
        db.query(NotificationEvent)
        .filter(
            NotificationEvent.eligible_at <= now,
            NotificationEvent.delivered_at.is_(None),
            NotificationEvent.resolved_at.is_(None),
        )
        .order_by(NotificationEvent.eligible_at.asc())
        .limit(50)
        .all()
    )

    for event in candidates:
        if not _is_queued_event(event):
            continue
        result["checked"] += 1
        session_id_raw = str(event.session_id or "").strip()
        if not session_id_raw:
            event.resolved_at = now
            result["resolved"] += 1
            continue

        try:
            session_uuid = UUID(session_id_raw)
        except ValueError:
            event.resolved_at = now
            result["resolved"] += 1
            continue

        session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
        if session is None:
            event.resolved_at = now
            result["resolved"] += 1
            continue

        runtime_map = load_runtime_state_map(db, session_ids=[session_uuid])
        overlay = resolve_runtime_overlay(
            session,
            last_activity_at=session.last_activity_at,
            runtime_state_map=runtime_map,
            now=now,
        )
        current_state = str(overlay.presence_state or "").strip() or None
        previous_state = None

        push = None
        owner_id = int(event.owner_id)
        event_type = str(event.event_type)

        if event_type == NOTIFICATION_EVENT_SESSION_BLOCKED and current_state == "blocked":
            push = prepare_session_attention_push(
                db,
                owner_id=owner_id,
                session_id=session_uuid,
                previous_state=previous_state,
                current_state=current_state,
                occurred_at=now,
            )
        elif event_type == NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER and current_state == "blocked":
            push = prepare_session_blocked_reminder_push(
                db,
                owner_id=owner_id,
                session_id=session_uuid,
                current_state=current_state,
                occurred_at=now,
            )
        elif event_type == NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER:
            pause_request = load_active_pause_request_for_session(db, session_id=session_uuid)
            if pause_request is not None:
                push = prepare_session_needs_answer_push(
                    db,
                    owner_id=owner_id,
                    session_id=session_uuid,
                    pause_request=pause_request,
                    previous_state=previous_state,
                    occurred_at=now,
                )
        if push is None:
            event.resolved_at = now
            results = dict(event.channel_results or {})
            results["queue_expired"] = True
            event.channel_results = results
            result["resolved"] += 1
            continue

        try:
            accepted = await send_session_attention_push(push)
        except Exception:
            logger.exception("Queued notification delivery failed for event %s", event.id)
            record_notification_delivery_result(
                db,
                event_id=push.notification_event_id,
                channel=NOTIFICATION_CHANNEL_APNS_IOS,
                accepted=False,
                occurred_at=now,
            )
            rollback_session_attention_push_stamp(db, notification=push)
            result["skipped"] += 1
            continue

        record_notification_delivery_result(
            db,
            event_id=push.notification_event_id,
            channel=NOTIFICATION_CHANNEL_APNS_IOS,
            accepted=accepted,
            occurred_at=now,
        )
        if not accepted:
            rollback_session_attention_push_stamp(db, notification=push)

        if accepted:
            event.delivered_at = now
            event.resolved_at = now
            results = dict(event.channel_results or {})
            results["queue_delivered"] = True
            event.channel_results = results
            result["delivered"] += 1
        else:
            result["skipped"] += 1

    if result["checked"]:
        db.commit()
    return result
