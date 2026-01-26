"""Ops events bridge: normalize EventBus events to an `ops:events` ticker.

Subscribes to core domain events (courses, fiches, threads) and broadcasts
compact, color-codable frames to the `ops:events` WebSocket topic.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict

from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.generated.ws_messages import MessageType
from zerg.generated.ws_messages import OpsEventData
from zerg.generated.ws_messages import create_typed_emitter
from zerg.websocket.manager import topic_manager

logger = logging.getLogger(__name__)


OPS_TOPIC = "ops:events"

# Typed emitter bound to our topic manager broadcaster
typed_emitter = create_typed_emitter(topic_manager.broadcast_to_topic)


class OpsEventsBridge:
    """Subscribe to EventBus and broadcast normalized ops ticker frames."""

    _started: bool = False

    async def _handle_course_event(self, data: Dict[str, Any]) -> None:
        # Normalize COURSE_* events into course_started/course_success/course_failed
        status = data.get("status")
        fiche_id = data.get("fiche_id")
        course_id = data.get("course_id") or data.get("id")
        if not fiche_id or not course_id:
            return

        if status == "running":
            msg_type = "course_started"
        elif status == "success":
            msg_type = "course_success"
        elif status == "failed":
            msg_type = "course_failed"
        else:
            # Ignore queued and unknown statuses for the ticker
            return

        payload = OpsEventData(
            type=msg_type,
            fiche_id=fiche_id,
            course_id=course_id,
            thread_id=data.get("thread_id"),
            duration_ms=data.get("duration_ms"),
            error=data.get("error"),
        )
        await typed_emitter.send_typed(OPS_TOPIC, MessageType.OPS_EVENT, payload)

    async def _handle_fiche_event(self, data: Dict[str, Any]) -> None:
        fiche_id = data.get("id")
        if not fiche_id:
            return
        event_type = "fiche_updated"
        # Try to infer created
        if data.get("event_type") == "fiche_created":
            event_type = "fiche_created"
        payload = OpsEventData(
            type=event_type,
            fiche_id=fiche_id,
            fiche_name=data.get("name"),
            status=data.get("status"),
        )
        await typed_emitter.send_typed(OPS_TOPIC, MessageType.OPS_EVENT, payload)

    async def _handle_thread_message(self, data: Dict[str, Any]) -> None:
        thread_id = data.get("thread_id")
        if not thread_id:
            return
        payload = OpsEventData(type="thread_message_created", thread_id=thread_id)
        await typed_emitter.send_typed(OPS_TOPIC, MessageType.OPS_EVENT, payload)

    async def _handle_budget_denied(self, data: Dict[str, Any]) -> None:
        # Data expected: { scope, percent, used_usd, limit_cents, user_email }
        scope = data.get("scope")
        if not scope:
            return
        payload = OpsEventData(
            type="budget_denied",
            scope=scope,
            percent=data.get("percent"),
            used_usd=data.get("used_usd"),
            limit_cents=data.get("limit_cents"),
            user_email=data.get("user_email"),
        )
        await typed_emitter.send_typed(OPS_TOPIC, MessageType.OPS_EVENT, payload)

    def start(self) -> None:
        if self._started:
            return
        event_bus.subscribe(EventType.COURSE_CREATED, self._handle_course_event)
        event_bus.subscribe(EventType.COURSE_UPDATED, self._handle_course_event)
        event_bus.subscribe(EventType.FICHE_CREATED, self._handle_fiche_event)
        event_bus.subscribe(EventType.FICHE_UPDATED, self._handle_fiche_event)
        event_bus.subscribe(EventType.THREAD_MESSAGE_CREATED, self._handle_thread_message)
        event_bus.subscribe(EventType.BUDGET_DENIED, self._handle_budget_denied)
        self._started = True
        logger.info("OpsEventsBridge subscribed to core events")

    def stop(self) -> None:
        if not self._started:
            return
        try:
            event_bus.unsubscribe(EventType.COURSE_CREATED, self._handle_course_event)
            event_bus.unsubscribe(EventType.COURSE_UPDATED, self._handle_course_event)
            event_bus.unsubscribe(EventType.FICHE_CREATED, self._handle_fiche_event)
            event_bus.unsubscribe(EventType.FICHE_UPDATED, self._handle_fiche_event)
            event_bus.unsubscribe(EventType.THREAD_MESSAGE_CREATED, self._handle_thread_message)
            event_bus.unsubscribe(EventType.BUDGET_DENIED, self._handle_budget_denied)
        finally:
            self._started = False


# Global instance used by app startup/shutdown
ops_events_bridge = OpsEventsBridge()
