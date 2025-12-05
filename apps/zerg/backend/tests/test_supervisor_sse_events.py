"""Regression tests for supervisor SSE event naming and payloads."""

import asyncio
import json

import pytest

from zerg.events import EventType, event_bus
from zerg.routers.jarvis import _supervisor_event_generator


@pytest.mark.asyncio
async def test_supervisor_sse_emits_named_events():
    """SSE generator should emit events named after event_type in payload."""
    run_id = 123
    owner_id = 456

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a supervisor_started event carrying event_type
        await event_bus.publish(
            EventType.SUPERVISOR_STARTED,
            {
                "event_type": EventType.SUPERVISOR_STARTED,
                "run_id": run_id,
                "owner_id": owner_id,
            },
        )

        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == EventType.SUPERVISOR_STARTED

        payload = json.loads(event["data"])
        assert payload["type"] == EventType.SUPERVISOR_STARTED
        assert payload["payload"]["run_id"] == run_id
    finally:
        await gen.aclose()

