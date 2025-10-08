"""
Test event publisher task tracking functionality.

This tests the new task tracking system to prevent resource leaks
in fire-and-forget event publishing.
"""

import asyncio

import pytest

from zerg.events import EventType
from zerg.events.publisher import get_active_task_count
from zerg.events.publisher import publish_event_fire_and_forget
from zerg.events.publisher import shutdown_event_publisher


class TestEventPublisherTaskTracking:
    """Test the event publisher task tracking system."""

    @pytest.mark.asyncio
    async def test_task_tracking_lifecycle(self):
        """Test that tasks are properly tracked and cleaned up."""
        # Start with clean state
        initial_count = get_active_task_count()

        # Publish several fire-and-forget events
        event_count = 3
        for i in range(event_count):
            publish_event_fire_and_forget(EventType.NODE_STATE_CHANGED, {"node_id": f"test-{i}"})

        # Tasks should be tracked immediately
        active_count = get_active_task_count()
        assert (
            active_count >= initial_count + event_count
        ), f"Expected at least {initial_count + event_count} active tasks, got {active_count}"

        # Wait for tasks to complete
        await asyncio.sleep(0.1)

        # Tasks should be cleaned up automatically
        final_count = get_active_task_count()
        assert (
            final_count <= initial_count
        ), f"Expected task count to return to {initial_count} or less, got {final_count}"

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self):
        """Test that shutdown waits for active tasks."""
        # Publish some events
        for i in range(2):
            publish_event_fire_and_forget(EventType.EXECUTION_FINISHED, {"execution_id": i})

        # Verify we have active tasks
        assert get_active_task_count() > 0

        # Shutdown should wait for completion
        await shutdown_event_publisher()

        # All tasks should be completed
        assert get_active_task_count() == 0

    def test_fire_and_forget_with_no_loop(self):
        """Test that fire-and-forget handles missing event loop gracefully."""
        # This should not raise an exception, just log an error
        # (We can't easily test the actual behavior without a running loop)
        try:
            # This will fail because we're not in an async context
            publish_event_fire_and_forget(EventType.NODE_STATE_CHANGED, {"test": "data"})
        except Exception as e:
            # Should not raise, just log
            pytest.fail(f"fire_and_forget should handle missing loop gracefully, but got: {e}")

    @pytest.mark.asyncio
    async def test_task_count_monitoring(self):
        """Test that get_active_task_count provides accurate monitoring."""
        initial_count = get_active_task_count()
        assert isinstance(initial_count, int)
        assert initial_count >= 0

        # Publish an event
        publish_event_fire_and_forget(EventType.NODE_STATE_CHANGED, {"test": "monitoring"})

        # Count should increase
        new_count = get_active_task_count()
        assert new_count > initial_count

        # Wait for completion
        await asyncio.sleep(0.1)

        # Count should return to initial state
        final_count = get_active_task_count()
        assert final_count <= initial_count
