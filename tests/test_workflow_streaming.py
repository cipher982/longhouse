#!/usr/bin/env python3
"""
Test script to verify workflow streaming events are published and received.
"""
import asyncio
import logging
import sys
sys.path.insert(0, '/Users/davidrose/git/zerg/apps/zerg/backend')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def test_event_flow():
    """Test that events flow from workflow engine through event bus to websocket manager."""
    from zerg.events import EventType, event_bus
    from zerg.websocket.manager import topic_manager

    # Track events received by the WebSocket manager
    received_events = []

    # Store original handler
    original_handler = None
    for handler in event_bus._subscribers.get(EventType.EXECUTION_STARTED, set()):
        if 'TopicConnectionManager' in str(handler):
            original_handler = handler
            break

    if not original_handler:
        logger.error("❌ TopicConnectionManager handler not found in event bus subscribers!")
        return False

    # Wrap the handler to track calls
    async def tracking_wrapper(data):
        logger.info(f"✅ TRACKING: Handler received event with data: {data}")
        received_events.append(('handler_called', data))
        # Call original handler
        await original_handler(data)

    # Replace handler temporarily
    event_bus._subscribers[EventType.EXECUTION_STARTED].remove(original_handler)
    event_bus._subscribers[EventType.EXECUTION_STARTED].add(tracking_wrapper)

    try:
        # Publish a test event
        logger.info("📤 Publishing test EXECUTION_STARTED event...")
        await event_bus.publish(EventType.EXECUTION_STARTED, {
            'execution_id': 9999,
            'workflow_id': 1,
            'event_type': EventType.EXECUTION_STARTED
        })

        # Give handlers time to run
        await asyncio.sleep(0.5)

        if len(received_events) > 0:
            logger.info(f"✅ SUCCESS: Handler was called {len(received_events)} time(s)")
            logger.info(f"   Events received: {received_events}")
            return True
        else:
            logger.error("❌ FAIL: Handler was NOT called")
            return False

    finally:
        # Restore original handler
        event_bus._subscribers[EventType.EXECUTION_STARTED].discard(tracking_wrapper)
        event_bus._subscribers[EventType.EXECUTION_STARTED].add(original_handler)

if __name__ == '__main__':
    result = asyncio.run(test_event_flow())
    sys.exit(0 if result else 1)
