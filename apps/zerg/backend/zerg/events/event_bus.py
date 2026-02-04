"""Event bus implementation for decoupled event handling."""

import logging
from enum import Enum
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Dict
from typing import Set

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Standardized event types for the system."""

    # Fiche events
    FICHE_CREATED = "fiche_created"
    FICHE_UPDATED = "fiche_updated"
    FICHE_DELETED = "fiche_deleted"

    # Thread events
    THREAD_CREATED = "thread_created"
    THREAD_UPDATED = "thread_updated"
    THREAD_DELETED = "thread_deleted"
    THREAD_MESSAGE_CREATED = "thread_message_created"

    # Run events (new run history feature)
    RUN_CREATED = "run_created"
    RUN_UPDATED = "run_updated"

    # Trigger events (external webhook or other sources)
    TRIGGER_FIRED = "trigger_fired"

    # System events
    SYSTEM_STATUS = "system_status"
    ERROR = "error"

    # User events (profile updates, etc.)
    USER_UPDATED = "user_updated"

    # Ops dashboard events
    BUDGET_DENIED = "budget_denied"

    # Oikos/Commis events (Super Siri architecture)
    OIKOS_STARTED = "oikos_started"
    OIKOS_THINKING = "oikos_thinking"
    OIKOS_TOKEN = "oikos_token"  # Real-time LLM token streaming
    OIKOS_COMPLETE = "oikos_complete"
    OIKOS_DEFERRED = "oikos_deferred"  # Timeout migration: still running, caller stopped waiting
    OIKOS_WAITING = "oikos_waiting"  # Interrupted waiting for commis (oikos resume)
    OIKOS_RESUMED = "oikos_resumed"  # Resumed from interrupt after commis completed
    COMMIS_SPAWNED = "commis_spawned"
    COMMIS_STARTED = "commis_started"
    COMMIS_COMPLETE = "commis_complete"
    COMMIS_SUMMARY_READY = "commis_summary_ready"

    # Commis tool events (roundabout monitoring)
    COMMIS_TOOL_STARTED = "commis_tool_started"
    COMMIS_TOOL_COMPLETED = "commis_tool_completed"
    COMMIS_TOOL_FAILED = "commis_tool_failed"
    COMMIS_OUTPUT_CHUNK = "commis_output_chunk"

    # Oikos tool events (inline display in conversation)
    OIKOS_TOOL_STARTED = "oikos_tool_started"
    OIKOS_TOOL_PROGRESS = "oikos_tool_progress"
    OIKOS_TOOL_COMPLETED = "oikos_tool_completed"
    OIKOS_TOOL_FAILED = "oikos_tool_failed"
    SHOW_SESSION_PICKER = "show_session_picker"

    # Heartbeat events (Phase 6: prevent false "no progress" warnings during LLM reasoning)
    OIKOS_HEARTBEAT = "oikos_heartbeat"
    COMMIS_HEARTBEAT = "commis_heartbeat"

    # Stream lifecycle control (explicit keep_open/close signals)
    STREAM_CONTROL = "stream_control"

    SESSION_ENDED = "session_ended"  # External session (Claude Code, Codex, etc.) ended


class EventBus:
    """Central event bus for publishing and subscribing to events."""

    def __init__(self):
        """Initialize an empty event bus."""
        self._subscribers: Dict[EventType, Set[Callable[[Dict[str, Any]], Awaitable[None]]]] = {}

    async def publish(self, event_type: EventType, data: Dict[str, Any]) -> None:
        """Publish an event to all subscribers.

        Args:
            event_type: The type of event being published
            data: Event payload data
        """
        subscriber_count = len(self._subscribers.get(event_type, set()))
        if subscriber_count == 0:
            logger.debug("No subscribers for %s", event_type)
            return

        # OIKOS_TOKEN is emitted per-token and can spam logs when DEBUG is enabled.
        if event_type != EventType.OIKOS_TOKEN:
            logger.debug("Publishing event %s to %s subscriber(s)", event_type, subscriber_count)

        # ------------------------------------------------------------------
        # Fan-out **concurrently** so that a slow subscriber can no longer
        # block the entire publish call.  We keep return_exceptions=True so
        # every callback runs – any raised error is logged individually.
        # ------------------------------------------------------------------

        import asyncio

        tasks = [callback(data) for callback in self._subscribers[event_type]]

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any exceptions that were captured by gather()
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Error in event handler %s for %s", i, event_type, exc_info=result)

    def subscribe(self, event_type: EventType, callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Subscribe to an event type.

        Args:
            event_type: The event type to subscribe to
            callback: Async callback function to handle the event
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = set()

        self._subscribers[event_type].add(callback)
        logger.debug(f"Added subscriber for event {event_type}")

    def unsubscribe(self, event_type: EventType, callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Unsubscribe from an event type.

        Args:
            event_type: The event type to unsubscribe from
            callback: The callback function to remove
        """
        if event_type in self._subscribers:
            self._subscribers[event_type].discard(callback)
            logger.debug(f"Removed subscriber for event {event_type}")

            # Clean up empty subscriber sets
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]


# Global event bus instance
event_bus = EventBus()
