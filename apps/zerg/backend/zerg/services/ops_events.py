"""Ops events bridge: normalize EventBus events to an `ops:events` ticker.

Subscribes to core domain events (runs, agents, threads) and broadcasts
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

    async def _handle_run_event(self, data: Dict[str, Any]) -> None:
        # Normalize RUN_* events into run_started/run_success/run_failed
        status = data.get("status")
        agent_id = data.get("agent_id")
        run_id = data.get("run_id") or data.get("id")
        if not agent_id or not run_id:
            return

        if status == "running":
            msg_type = "run_started"
        elif status == "success":
            msg_type = "run_success"
        elif status == "failed":
            msg_type = "run_failed"
        else:
            # Ignore queued and unknown statuses for the ticker
            return

        payload = OpsEventData(
            type=msg_type,
            agent_id=agent_id,
            run_id=run_id,
            thread_id=data.get("thread_id"),
            duration_ms=data.get("duration_ms"),
            error=data.get("error"),
        )
        await typed_emitter.send_typed(OPS_TOPIC, MessageType.OPS_EVENT, payload)

    async def _handle_agent_event(self, data: Dict[str, Any]) -> None:
        agent_id = data.get("id")
        if not agent_id:
            return
        event_type = "agent_updated"
        # Try to infer created
        if data.get("event_type") == "agent_created":
            event_type = "agent_created"
        payload = OpsEventData(
            type=event_type,
            agent_id=agent_id,
            agent_name=data.get("name"),
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
        event_bus.subscribe(EventType.RUN_CREATED, self._handle_run_event)
        event_bus.subscribe(EventType.RUN_UPDATED, self._handle_run_event)
        event_bus.subscribe(EventType.AGENT_CREATED, self._handle_agent_event)
        event_bus.subscribe(EventType.AGENT_UPDATED, self._handle_agent_event)
        event_bus.subscribe(EventType.THREAD_MESSAGE_CREATED, self._handle_thread_message)
        event_bus.subscribe(EventType.BUDGET_DENIED, self._handle_budget_denied)
        self._started = True
        logger.info("OpsEventsBridge subscribed to core events")

    def stop(self) -> None:
        if not self._started:
            return
        try:
            event_bus.unsubscribe(EventType.RUN_CREATED, self._handle_run_event)
            event_bus.unsubscribe(EventType.RUN_UPDATED, self._handle_run_event)
            event_bus.unsubscribe(EventType.AGENT_CREATED, self._handle_agent_event)
            event_bus.unsubscribe(EventType.AGENT_UPDATED, self._handle_agent_event)
            event_bus.unsubscribe(EventType.THREAD_MESSAGE_CREATED, self._handle_thread_message)
            event_bus.unsubscribe(EventType.BUDGET_DENIED, self._handle_budget_denied)
        finally:
            self._started = False


# Global instance used by app startup/shutdown
ops_events_bridge = OpsEventsBridge()
