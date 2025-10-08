"""Tests for the event bus implementation."""

import pytest

from zerg.events import EventBus
from zerg.events import EventType


@pytest.mark.asyncio
async def test_event_bus_basic_publish_subscribe():
    """Test basic publish/subscribe functionality."""
    bus = EventBus()
    received_data = None

    async def test_handler(data: dict):
        nonlocal received_data
        received_data = data

    # Subscribe to an event
    bus.subscribe(EventType.AGENT_CREATED, test_handler)

    # Publish an event
    test_data = {"agent_id": 123, "name": "Test Agent"}
    await bus.publish(EventType.AGENT_CREATED, test_data)

    # Verify handler was called with correct data
    assert received_data == test_data


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers():
    """Test multiple subscribers for the same event."""
    bus = EventBus()
    received_count = 0

    async def test_handler1(_):
        nonlocal received_count
        received_count += 1

    async def test_handler2(_):
        nonlocal received_count
        received_count += 1

    # Subscribe both handlers
    bus.subscribe(EventType.AGENT_UPDATED, test_handler1)
    bus.subscribe(EventType.AGENT_UPDATED, test_handler2)

    # Publish an event
    await bus.publish(EventType.AGENT_UPDATED, {"id": 123})

    # Verify both handlers were called
    assert received_count == 2


@pytest.mark.asyncio
async def test_event_bus_unsubscribe():
    """Test unsubscribing from events."""
    bus = EventBus()
    call_count = 0

    async def test_handler(_):
        nonlocal call_count
        call_count += 1

    # Subscribe and then unsubscribe
    bus.subscribe(EventType.AGENT_DELETED, test_handler)
    bus.unsubscribe(EventType.AGENT_DELETED, test_handler)

    # Publish an event
    await bus.publish(EventType.AGENT_DELETED, {"id": 123})

    # Verify handler was not called
    assert call_count == 0


@pytest.mark.asyncio
async def test_event_bus_error_handling():
    """Test error handling in event handlers."""
    bus = EventBus()
    success_handler_called = False

    async def error_handler(_):
        raise ValueError("Test error")

    async def success_handler(_):
        nonlocal success_handler_called
        success_handler_called = True

    # Subscribe both handlers
    bus.subscribe(EventType.SYSTEM_STATUS, error_handler)
    bus.subscribe(EventType.SYSTEM_STATUS, success_handler)

    # Publish an event - should not raise exception
    await bus.publish(EventType.SYSTEM_STATUS, {"status": "test"})

    # Verify success handler was still called despite error in first handler
    assert success_handler_called
