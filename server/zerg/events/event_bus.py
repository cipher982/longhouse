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

    # System events
    SYSTEM_STATUS = "system_status"
    ERROR = "error"

    # User events (profile updates, etc.)
    USER_UPDATED = "user_updated"

    # Assistant events
    ASSISTANT_STARTED = "assistant_started"
    ASSISTANT_THINKING = "assistant_thinking"
    ASSISTANT_TOKEN = "assistant_token"  # Real-time LLM token streaming
    ASSISTANT_COMPLETE = "assistant_complete"
    ASSISTANT_DEFERRED = "assistant_deferred"  # Timeout migration: still running, caller stopped waiting
    # Assistant tool events (inline display in conversation)
    ASSISTANT_TOOL_STARTED = "assistant_tool_started"
    ASSISTANT_TOOL_PROGRESS = "assistant_tool_progress"
    ASSISTANT_TOOL_COMPLETED = "assistant_tool_completed"
    ASSISTANT_TOOL_FAILED = "assistant_tool_failed"
    SHOW_SESSION_PICKER = "show_session_picker"

    # Heartbeat events (Phase 6: prevent false "no progress" warnings during LLM reasoning)
    ASSISTANT_HEARTBEAT = "assistant_heartbeat"

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

        # ASSISTANT_TOKEN is emitted per-token and can spam logs when DEBUG is enabled.
        if event_type != EventType.ASSISTANT_TOKEN:
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
