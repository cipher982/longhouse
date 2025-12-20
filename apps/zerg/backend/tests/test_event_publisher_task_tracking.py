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
        """Test that tasks are properly tracked and cleaned up.

        Note: With the shared async runner implementation, fire-and-forget events
        execute synchronously, so task tracking always returns 0. This test verifies
        the API contract is maintained.
        """
        # Start with clean state
        initial_count = get_active_task_count()
        assert initial_count == 0, "Shared runner should have no tracked tasks"

        # Publish several fire-and-forget events
        event_count = 3
        for i in range(event_count):
            publish_event_fire_and_forget(
                EventType.NODE_STATE_CHANGED,
                {
                    "node_id": f"test-{i}",
                    "execution_id": f"exec-{i}",  # Required by _handle_node_state_event
                },
            )

        # With shared runner, tasks execute synchronously
        active_count = get_active_task_count()
        assert active_count == 0, "Shared runner executes synchronously, no pending tasks"

        # Count should remain stable
        await asyncio.sleep(0.1)
        final_count = get_active_task_count()
        assert final_count == 0

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self):
        """Test that shutdown waits for active tasks.

        Note: With the shared async runner, events execute synchronously,
        so shutdown has no pending tasks to wait for.
        """
        # Publish some events
        for i in range(2):
            publish_event_fire_and_forget(EventType.EXECUTION_FINISHED, {"execution_id": i})

        # With shared runner, tasks execute synchronously
        assert get_active_task_count() == 0, "Shared runner has no pending tasks"

        # Shutdown should complete immediately (no-op with shared runner)
        await shutdown_event_publisher()

        # Count should still be zero
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
        """Test that get_active_task_count provides accurate monitoring.

        Note: With the shared async runner, the count always returns 0
        since events execute synchronously.
        """
        initial_count = get_active_task_count()
        assert isinstance(initial_count, int)
        assert initial_count == 0, "Shared runner has no tracked tasks"

        # Publish an event
        publish_event_fire_and_forget(
            EventType.NODE_STATE_CHANGED,
            {
                "test": "monitoring",
                "execution_id": "test-exec",  # Required by _handle_node_state_event
            },
        )

        # With shared runner, count stays at 0 (synchronous execution)
        new_count = get_active_task_count()
        assert new_count == 0, "Shared runner executes synchronously"

        # Wait a bit and verify count is still stable
        await asyncio.sleep(0.1)
        final_count = get_active_task_count()
        assert final_count == 0
