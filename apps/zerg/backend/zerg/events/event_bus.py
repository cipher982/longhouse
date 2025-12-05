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

    # Agent events
    AGENT_CREATED = "agent_created"
    AGENT_UPDATED = "agent_updated"
    AGENT_DELETED = "agent_deleted"

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

    # Supervisor/Worker events (Super Siri architecture)
    SUPERVISOR_STARTED = "supervisor_started"
    SUPERVISOR_THINKING = "supervisor_thinking"
    SUPERVISOR_COMPLETE = "supervisor_complete"
    WORKER_SPAWNED = "worker_spawned"
    WORKER_STARTED = "worker_started"
    WORKER_COMPLETE = "worker_complete"
    WORKER_SUMMARY_READY = "worker_summary_ready"

    # Worker tool events (roundabout monitoring)
    WORKER_TOOL_STARTED = "worker_tool_started"
    WORKER_TOOL_COMPLETED = "worker_tool_completed"
    WORKER_TOOL_FAILED = "worker_tool_failed"


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
        # Debug: Always log subscriber count
        subscriber_count = len(self._subscribers.get(event_type, set()))
        print(f"🔥🔥🔥 EVENT_BUS.publish({event_type}): {subscriber_count} subscribers", flush=True)

        if event_type not in self._subscribers:
            print(f"❌ NO SUBSCRIBERS for {event_type}", flush=True)
            return

        logger.debug("Publishing event %s with data: %s", event_type, data)

        # ------------------------------------------------------------------
        # Fan-out **concurrently** so that a slow subscriber can no longer
        # block the entire publish call.  We keep return_exceptions=True so
        # every callback runs – any raised error is logged individually.
        # ------------------------------------------------------------------

        import asyncio

        print(f"📞 Calling {len(self._subscribers[event_type])} handler(s) for {event_type}", flush=True)
        tasks = [callback(data) for callback in self._subscribers[event_type]]

        if not tasks:
            print(f"⚠️ No tasks created for {event_type}", flush=True)
            return

        print(f"🚀 Awaiting {len(tasks)} task(s) for {event_type}", flush=True)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        print(f"✅ gather() completed for {event_type}, {len(results)} results", flush=True)

        # Log any exceptions that were captured by gather()
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                print(f"❌ Handler {i} for {event_type} raised: {result}", flush=True)
                logger.error("Error in event handler for %s: %s", event_type, result)
            else:
                print(f"✅ Handler {i} for {event_type} completed successfully", flush=True)

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
