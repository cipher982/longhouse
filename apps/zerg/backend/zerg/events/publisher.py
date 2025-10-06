"""
Carmack-Style Event Publishing

Single source of truth for event publishing with zero complexity.
No more asyncio loop juggling - one way to publish events.

Key principles:
1. One function to rule them all
2. Simple, reliable, predictable
3. No manual loop management
4. Clear semantics for fire-and-forget vs awaitable
5. Proper task lifecycle management to prevent resource leaks
"""

import asyncio
import logging
from typing import Any
from typing import Dict

from . import EventType
from . import event_bus

logger = logging.getLogger(__name__)

# Track fire-and-forget tasks to prevent resource leaks
_active_tasks: set = set()


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

    This creates a tracked task and doesn't wait for completion.
    Tasks are automatically cleaned up when completed.
    Use sparingly - prefer the async version when possible.

    Args:
        event_type: The event type to publish
        data: Event data dictionary

    Usage:
        publish_event_fire_and_forget(EventType.NODE_STATE_CHANGED, {"node_id": "test"})
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_publish_event_safe(event_type, data))

        # Only add to tracking and set callback if we have a proper Task object
        if hasattr(task, "add_done_callback"):
            # Track the task to prevent resource leaks
            _active_tasks.add(task)
            task.add_done_callback(_cleanup_task)
        else:
            # Refuse coroutine objects that slipped through - they'll never finish
            logger.error(f"create_task returned non-Task object for {event_type}: {type(task)!r}")
            return

    except RuntimeError:
        # No running loop - this is a programming error
        logger.error(f"Cannot publish fire-and-forget event {event_type} - no running event loop")
    except Exception as e:
        # Handle any other potential issues with task creation
        logger.error(f"Unexpected error creating fire-and-forget task for {event_type}: {e}")


def _cleanup_task(task) -> None:
    """Remove task from tracking and log any exceptions."""
    # Explicitly remove from tracking set
    _active_tasks.discard(task)

    # Log any exceptions for debugging
    if hasattr(task, "done") and task.done() and not getattr(task, "cancelled", lambda: False)():
        try:
            if hasattr(task, "result"):
                task.result()  # This will raise if the task had an exception
        except Exception as e:
            logger.error(f"Fire-and-forget event publishing task failed: {e}")


async def _publish_event_safe(event_type: EventType, data: Dict[str, Any]) -> None:
    """Internal safe event publishing."""
    try:
        await event_bus.publish(event_type, data)
    except Exception as e:
        logger.error(f"Failed to publish fire-and-forget event {event_type}: {e}")


async def shutdown_event_publisher() -> None:
    """
    Gracefully shutdown the event publisher.

    Waits for all active fire-and-forget tasks to complete.
    Call this during application shutdown to prevent resource leaks.
    """
    if not _active_tasks:
        return

    logger.info(f"Waiting for {len(_active_tasks)} active event publishing tasks to complete")

    # Create a copy since tasks will modify the original set when they complete
    pending_tasks = list(_active_tasks)

    try:
        # Wait up to 10 seconds for tasks to complete gracefully
        await asyncio.wait_for(asyncio.gather(*pending_tasks, return_exceptions=True), timeout=10.0)
        logger.info("All event publishing tasks completed gracefully")
    except asyncio.TimeoutError:
        # Cancel remaining tasks if timeout is reached
        logger.warning(f"Timeout waiting for {len(_active_tasks)} event publishing tasks, cancelling them")
        for task in _active_tasks:
            if not task.done():
                task.cancel()


def get_active_task_count() -> int:
    """Get the number of active fire-and-forget tasks (for monitoring/debugging)."""
    return len(_active_tasks)


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
