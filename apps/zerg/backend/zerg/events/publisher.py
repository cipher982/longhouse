"""
Carmack-Style Event Publishing

Single source of truth for event publishing with zero complexity.
No more asyncio loop juggling - one way to publish events.

Key principles:
1. One function to rule them all
2. Simple, reliable, predictable
3. No manual loop management
4. Clear semantics for fire-and-forget vs awaitable
"""

import logging
from typing import Any
from typing import Dict

from . import EventType
from . import event_bus

logger = logging.getLogger(__name__)


async def publish_event(event_type: EventType, data: Dict[str, Any]) -> None:
    """
    Clean event publishing - the ONLY way to publish events.

    This handles all asyncio complexity internally. Just call it.

    Args:
        event_type: The event type to publish
        data: Event data dictionary

    Usage:
        await publish_event(EventType.NODE_STATE_CHANGED, {"node_id": "test"})
    """
    try:
        await event_bus.publish(event_type, data)
    except Exception as e:
        logger.error(f"Failed to publish event {event_type}: {e}")
        # Don't re-raise - event publishing failures shouldn't break workflows


def publish_event_fire_and_forget(event_type: EventType, data: Dict[str, Any]) -> None:
    """
    Fire-and-forget event publishing for non-async contexts.

    This uses the shared async runner to execute the event publishing
    synchronously. Use sparingly - prefer the async version when possible.

    Args:
        event_type: The event type to publish
        data: Event data dictionary

    Usage:
        publish_event_fire_and_forget(EventType.NODE_STATE_CHANGED, {"node_id": "test"})
    """
    try:
        from zerg.utils.async_runner import run_in_shared_loop

        # Run the event publishing in the shared loop (blocks until complete)
        run_in_shared_loop(_publish_event_safe(event_type, data))

    except Exception as e:
        # Handle any errors with task execution
        logger.error(f"Failed to publish fire-and-forget event {event_type}: {e}")


async def _publish_event_safe(event_type: EventType, data: Dict[str, Any]) -> None:
    """Internal safe event publishing."""
    try:
        await event_bus.publish(event_type, data)
    except Exception as e:
        logger.error(f"Failed to publish fire-and-forget event {event_type}: {e}")


async def shutdown_event_publisher() -> None:
    """
    Gracefully shutdown the event publisher.

    Note: With the shared async runner, fire-and-forget events now execute
    synchronously, so there are no pending tasks to wait for. This function
    is kept for API compatibility.
    """
    logger.debug("Event publisher shutdown (no pending tasks with shared runner)")


def get_active_task_count() -> int:
    """Get the number of active fire-and-forget tasks (for monitoring/debugging).

    Note: With the shared async runner, fire-and-forget events execute
    synchronously, so this always returns 0.
    """
    return 0


# Legacy compatibility - remove after migration
async def publish_event_safe(event_type: EventType, data: Dict[str, Any], *, fire_and_forget: bool = False) -> None:
    """
    Legacy compatibility function - DEPRECATED.

    Use publish_event() or publish_event_fire_and_forget() instead.
    """
    logger.warning("publish_event_safe is deprecated - use publish_event() or publish_event_fire_and_forget()")

    if fire_and_forget:
        publish_event_fire_and_forget(event_type, data)
    else:
        await publish_event(event_type, data)
