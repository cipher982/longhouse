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

    # Fiche events (formerly Agent events)
    FICHE_CREATED = "fiche_created"
    FICHE_UPDATED = "fiche_updated"
    FICHE_DELETED = "fiche_deleted"
    # Backwards compatibility aliases
    AGENT_CREATED = "fiche_created"
    AGENT_UPDATED = "fiche_updated"
    AGENT_DELETED = "fiche_deleted"

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

    # Workflow execution events (visual canvas)
    EXECUTION_STARTED = "execution_started"
    NODE_STATE_CHANGED = "node_state_changed"
    WORKFLOW_PROGRESS = "workflow_progress"
    EXECUTION_FINISHED = "execution_finished"
    NODE_LOG = "node_log"

    # Ops dashboard events
    BUDGET_DENIED = "budget_denied"

    # Oikos/Commis events (Super Siri architecture)
    SUPERVISOR_STARTED = "oikos_started"
    SUPERVISOR_THINKING = "oikos_thinking"
    SUPERVISOR_TOKEN = "oikos_token"  # Real-time LLM token streaming
    SUPERVISOR_COMPLETE = "oikos_complete"
    SUPERVISOR_DEFERRED = "oikos_deferred"  # Timeout migration: still running, caller stopped waiting
    SUPERVISOR_WAITING = "oikos_waiting"  # Interrupted waiting for commis (oikos resume)
    SUPERVISOR_RESUMED = "oikos_resumed"  # Resumed from interrupt after commis completed
    WORKER_SPAWNED = "commis_spawned"
    WORKER_STARTED = "commis_started"
    WORKER_COMPLETE = "commis_complete"
    WORKER_SUMMARY_READY = "commis_summary_ready"

    # Commis tool events (roundabout monitoring)
    WORKER_TOOL_STARTED = "commis_tool_started"
    WORKER_TOOL_COMPLETED = "commis_tool_completed"
    WORKER_TOOL_FAILED = "commis_tool_failed"
    WORKER_OUTPUT_CHUNK = "commis_output_chunk"

    # Oikos tool events (inline display in conversation)
    SUPERVISOR_TOOL_STARTED = "oikos_tool_started"
    SUPERVISOR_TOOL_PROGRESS = "oikos_tool_progress"
    SUPERVISOR_TOOL_COMPLETED = "oikos_tool_completed"
    SUPERVISOR_TOOL_FAILED = "oikos_tool_failed"
    SHOW_SESSION_PICKER = "show_session_picker"

    # Heartbeat events (Phase 6: prevent false "no progress" warnings during LLM reasoning)
    SUPERVISOR_HEARTBEAT = "oikos_heartbeat"
    WORKER_HEARTBEAT = "commis_heartbeat"

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

        # SUPERVISOR_TOKEN is emitted per-token and can spam logs when DEBUG is enabled.
        if event_type != EventType.SUPERVISOR_TOKEN:
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
