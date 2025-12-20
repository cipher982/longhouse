"""Regression tests for supervisor SSE event naming and payloads."""

import asyncio
import json

import pytest

from zerg.events import EventType
from zerg.events import event_bus
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


@pytest.mark.asyncio
async def test_supervisor_sse_emits_tool_started_events():
    """SSE generator should forward WORKER_TOOL_STARTED events."""
    run_id = 123
    owner_id = 456
    worker_id = "2024-12-06T15-30-00_test-worker"

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a worker_tool_started event
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": "worker_tool_started",
                "run_id": run_id,
                "owner_id": owner_id,
                "worker_id": worker_id,
                "tool_name": "ssh_exec",
                "tool_call_id": "call_001",
                "tool_args_preview": "{'host': 'cube', 'command': 'df -h'}",
            },
        )

        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == "worker_tool_started"

        payload = json.loads(event["data"])
        assert payload["type"] == "worker_tool_started"
        assert payload["payload"]["worker_id"] == worker_id
        assert payload["payload"]["tool_name"] == "ssh_exec"
        assert payload["payload"]["tool_call_id"] == "call_001"
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_supervisor_sse_emits_tool_completed_events():
    """SSE generator should forward WORKER_TOOL_COMPLETED events."""
    run_id = 123
    owner_id = 456
    worker_id = "2024-12-06T15-30-00_test-worker"

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a worker_tool_completed event
        await event_bus.publish(
            EventType.WORKER_TOOL_COMPLETED,
            {
                "event_type": "worker_tool_completed",
                "run_id": run_id,
                "owner_id": owner_id,
                "worker_id": worker_id,
                "tool_name": "ssh_exec",
                "tool_call_id": "call_001",
                "duration_ms": 823,
                "result_preview": "Filesystem      Size  Used...",
            },
        )

        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == "worker_tool_completed"

        payload = json.loads(event["data"])
        assert payload["type"] == "worker_tool_completed"
        assert payload["payload"]["worker_id"] == worker_id
        assert payload["payload"]["tool_name"] == "ssh_exec"
        assert payload["payload"]["duration_ms"] == 823
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_supervisor_sse_emits_tool_failed_events():
    """SSE generator should forward WORKER_TOOL_FAILED events."""
    run_id = 123
    owner_id = 456
    worker_id = "2024-12-06T15-30-00_test-worker"

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a worker_tool_failed event
        await event_bus.publish(
            EventType.WORKER_TOOL_FAILED,
            {
                "event_type": "worker_tool_failed",
                "run_id": run_id,
                "owner_id": owner_id,
                "worker_id": worker_id,
                "tool_name": "http_request",
                "tool_call_id": "call_002",
                "duration_ms": 5000,
                "error": "Connection timeout",
            },
        )

        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == "worker_tool_failed"

        payload = json.loads(event["data"])
        assert payload["type"] == "worker_tool_failed"
        assert payload["payload"]["worker_id"] == worker_id
        assert payload["payload"]["tool_name"] == "http_request"
        assert payload["payload"]["error"] == "Connection timeout"
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_supervisor_sse_filters_tool_events_by_owner():
    """SSE generator should only forward tool events for the correct owner."""
    run_id = 123
    owner_id = 456
    wrong_owner_id = 999

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a tool event with wrong owner_id - should be filtered
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": "worker_tool_started",
                "run_id": run_id,
                "owner_id": wrong_owner_id,  # Different owner
                "worker_id": "some-worker",
                "tool_name": "ssh_exec",
                "tool_call_id": "call_001",
            },
        )

        # Now publish an event with correct owner - this should arrive
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": "worker_tool_started",
                "run_id": run_id,
                "owner_id": owner_id,  # Correct owner
                "worker_id": "correct-worker",
                "tool_name": "ssh_exec",
                "tool_call_id": "call_002",
            },
        )

        # The first (wrong owner) event should be filtered, so we receive the second
        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == "worker_tool_started"

        payload = json.loads(event["data"])
        # Verify we got the correct event (second one with correct owner)
        assert payload["payload"]["worker_id"] == "correct-worker"
        assert payload["payload"]["tool_call_id"] == "call_002"
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_supervisor_sse_requires_run_id_for_tool_events():
    """SSE generator should drop tool events that are missing run_id."""
    run_id = 123
    owner_id = 456

    gen = _supervisor_event_generator(run_id, owner_id)

    try:
        # Initial connection frame
        connected = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert connected["event"] == "connected"

        # Publish a tool event WITHOUT run_id - should be dropped
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": "worker_tool_started",
                "owner_id": owner_id,  # Has owner but no run_id
                "worker_id": "some-worker",
                "tool_name": "ssh_exec",
                "tool_call_id": "call_001",
            },
        )

        # Now publish an event WITH run_id - this should arrive
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": "worker_tool_started",
                "run_id": run_id,
                "owner_id": owner_id,
                "worker_id": "proper-worker",
                "tool_name": "ssh_exec",
                "tool_call_id": "call_002",
            },
        )

        # Should receive only the second event (with run_id)
        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert event["event"] == "worker_tool_started"

        payload = json.loads(event["data"])
        assert payload["payload"]["worker_id"] == "proper-worker"
        assert payload["payload"]["tool_call_id"] == "call_002"
    finally:
        await gen.aclose()
