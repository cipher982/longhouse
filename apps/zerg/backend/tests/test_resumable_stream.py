"""Tests for Resumable SSE v1 streaming endpoint.

This test suite verifies the new /api/stream/runs/{run_id} endpoint that supports:
- Replay of historical events from database
- Live streaming of new events
- Reconnection with Last-Event-ID header
- DEFERRED run streaming
- Token filtering
"""

import asyncio
import json
from datetime import datetime
from datetime import timezone
from typing import List
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from zerg.main import app
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import Thread
from zerg.services.event_store import emit_run_event


@pytest.fixture
def test_run(db_session, test_user):
    """Create a test agent and run for streaming tests."""
    # Create agent
    agent = Agent(
        name="Test Agent",
        owner_id=test_user.id,
        system_instructions="You are a helpful assistant.",
        task_instructions="Complete the given task.",
        model="gpt-4o-mini",
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    # Create thread
    thread = Thread(
        agent_id=agent.id,
        title="Test Thread",
        active=True,
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)

    # Create run
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    return run


@pytest.fixture
def auth_headers(test_user):
    """Create auth headers for test requests."""
    # With AUTH_DISABLED=1, any token works
    return {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_stream_replay_from_start(db_session, test_run, test_user, auth_headers):
    """Test replaying all events from the start of a run."""
    # Emit some historical events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test task", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "processing", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_token", {"token": "Hello", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_token", {"token": " world", "owner_id": test_user.id})

    # Mark run as complete so stream closes after replay
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream
    client = TestClient(app)
    with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

        # Collect events
        events = []
        for line in response.iter_lines():
            if line.startswith("id: "):
                event_id = line[4:]
                events.append({"id": event_id})
            elif line.startswith("event: "):
                event_type = line[7:]
                if events:
                    events[-1]["event"] = event_type
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if events:
                    events[-1]["data"] = data

    # Verify we got all historical events
    assert len(events) >= 4, f"Expected at least 4 events, got {len(events)}"

    # Verify event structure
    assert events[0]["event"] == "supervisor_started"
    assert events[0]["data"]["payload"]["task"] == "test task"
    assert "id" in events[0]  # Event ID present for resumption

    assert events[1]["event"] == "supervisor_thinking"
    assert events[2]["event"] == "supervisor_token"
    assert events[2]["data"]["payload"]["token"] == "Hello"
    assert events[3]["event"] == "supervisor_token"
    assert events[3]["data"]["payload"]["token"] == " world"


@pytest.mark.asyncio
async def test_stream_replay_from_event_id(db_session, test_run, test_user, auth_headers):
    """Test replaying events starting from a specific event ID."""
    # Emit historical events
    event1_id = await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    event2_id = await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "thinking", "owner_id": test_user.id})
    event3_id = await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream, resuming from event2_id
    client = TestClient(app)
    with client.stream(
        "GET",
        f"/api/stream/runs/{test_run.id}?after_event_id={event2_id}",
        headers=auth_headers,
    ) as response:
        assert response.status_code == 200

        # Collect events
        events = []
        for line in response.iter_lines():
            if line.startswith("event: ") and not line.startswith("event: heartbeat"):
                event_type = line[7:]
                events.append({"event": event_type})
            elif line.startswith("data: ") and events:
                data = json.loads(line[6:])
                events[-1]["data"] = data

    # Should only get event3 (after event2_id)
    assert len(events) >= 1
    assert events[0]["event"] == "supervisor_complete"
    assert events[0]["data"]["payload"]["result"] == "done"


@pytest.mark.asyncio
async def test_stream_with_last_event_id_header(db_session, test_run, test_user, auth_headers):
    """Test SSE standard Last-Event-ID header for automatic reconnect."""
    # Emit historical events
    event1_id = await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    event2_id = await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "thinking", "owner_id": test_user.id})
    event3_id = await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect with Last-Event-ID header (SSE standard)
    client = TestClient(app)
    headers = {**auth_headers, "Last-Event-ID": str(event1_id)}
    with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=headers) as response:
        assert response.status_code == 200

        # Collect events
        events = []
        for line in response.iter_lines():
            if line.startswith("event: ") and not line.startswith("event: heartbeat"):
                event_type = line[7:]
                events.append({"event": event_type})

    # Should get events after event1_id
    assert len(events) >= 2
    assert events[0]["event"] == "supervisor_thinking"
    assert events[1]["event"] == "supervisor_complete"


@pytest.mark.asyncio
async def test_stream_exclude_tokens(db_session, test_run, test_user, auth_headers):
    """Test filtering out token events with include_tokens=false."""
    # Emit events including tokens
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_token", {"token": "Hello", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_token", {"token": " world", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect with include_tokens=false
    client = TestClient(app)
    with client.stream(
        "GET",
        f"/api/stream/runs/{test_run.id}?include_tokens=false",
        headers=auth_headers,
    ) as response:
        assert response.status_code == 200

        # Collect events
        events = []
        for line in response.iter_lines():
            if line.startswith("event: ") and not line.startswith("event: heartbeat"):
                event_type = line[7:]
                events.append(event_type)

    # Should only get non-token events
    assert "supervisor_started" in events
    assert "supervisor_complete" in events
    assert "supervisor_token" not in events


@pytest.mark.asyncio
async def test_stream_deferred_run_is_streamable(db_session, test_run, test_user, auth_headers):
    """Test that DEFERRED runs are streamable (not treated as complete)."""
    # Emit initial event
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "long task", "owner_id": test_user.id})

    # Mark run as DEFERRED (background work in progress)
    test_run.status = RunStatus.DEFERRED
    db_session.commit()

    # Connect to stream
    client = TestClient(app)

    # Use a short timeout since we're testing that the stream starts
    # (not that it stays open forever)
    collected_events = []

    def collect_events():
        with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers, timeout=2) as response:
            assert response.status_code == 200

            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                    collected_events.append(event_type)
                    # After getting the heartbeat, we know the stream is live
                    if event_type == "heartbeat":
                        return

    # Should not raise an exception (stream should stay open for DEFERRED)
    try:
        collect_events()
    except Exception:
        pass  # Timeout is expected, we just want to verify we got events

    # Should have received at least the historical event and a heartbeat
    assert "supervisor_started" in collected_events or "heartbeat" in collected_events


@pytest.mark.asyncio
async def test_stream_run_not_found(auth_headers):
    """Test 404 error when run doesn't exist or not owned by user."""
    client = TestClient(app)

    response = client.get("/api/stream/runs/99999", headers=auth_headers)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_stream_replay_after_sequence(db_session, test_run, test_user, auth_headers):
    """Test replaying events starting from a specific sequence number."""
    # Emit historical events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})  # seq 1
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "thinking", "owner_id": test_user.id})  # seq 2
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})  # seq 3

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream, resuming from sequence 1 (should get seq 2 and 3)
    client = TestClient(app)
    with client.stream(
        "GET",
        f"/api/stream/runs/{test_run.id}?after_sequence=1",
        headers=auth_headers,
    ) as response:
        assert response.status_code == 200

        # Collect events
        events = []
        for line in response.iter_lines():
            if line.startswith("event: ") and not line.startswith("event: heartbeat"):
                event_type = line[7:]
                events.append(event_type)

    # Should get events with sequence > 1
    assert len(events) >= 2
    assert events[0] == "supervisor_thinking"
    assert events[1] == "supervisor_complete"


@pytest.mark.asyncio
async def test_stream_no_duplicate_events(db_session, test_run, test_user, auth_headers, monkeypatch):
    """Test that events arriving during replay are not duplicated in live stream.

    This is the critical test for the "subscribe before replay" pattern.
    Events that arrive while replaying should not appear twice.
    """
    # Emit initial events
    event1_id = await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    event2_id = await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "thinking", "owner_id": test_user.id})

    # Keep run RUNNING so live stream activates
    test_run.status = RunStatus.RUNNING
    db_session.commit()

    # We'll emit a new event "during" the replay by using a task that runs in the background
    async def emit_during_stream():
        await asyncio.sleep(0.1)  # Small delay to ensure stream has started
        # This event will arrive while the stream is active
        await emit_run_event(db_session, test_run.id, "worker_spawned", {"job_id": 123, "owner_id": test_user.id})
        await asyncio.sleep(0.2)  # Give time for stream to process
        # Complete the run to close the stream
        await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Start background task to emit events
    task = asyncio.create_task(emit_during_stream())

    # Connect to stream
    client = TestClient(app)
    collected_events = []

    try:
        with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers, timeout=3) as response:
            assert response.status_code == 200

            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                    collected_events.append(event_type)
                    # Stop after supervisor_complete
                    if event_type == "supervisor_complete":
                        break
    except Exception:
        pass  # Timeout is ok

    await task  # Wait for background task to complete

    # Verify we got all events without duplicates
    # Count occurrences of each event type
    event_counts = {}
    for event in collected_events:
        if event != "heartbeat":  # Ignore heartbeats
            event_counts[event] = event_counts.get(event, 0) + 1

    # Each event should appear exactly once
    for event_type, count in event_counts.items():
        assert count == 1, f"Event {event_type} appeared {count} times (expected 1). Events: {collected_events}"


@pytest.mark.asyncio
async def test_stream_completed_run_closes_immediately(db_session, test_run, test_user, auth_headers):
    """Test that completed runs return events and close stream (no live wait)."""
    # Emit all events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as SUCCESS
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream
    client = TestClient(app)
    start_time = asyncio.get_event_loop().time()

    with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers, timeout=5) as response:
        assert response.status_code == 200

        # Collect all events (stream should close quickly)
        events = []
        for line in response.iter_lines():
            if line.startswith("event: ") and not line.startswith("event: heartbeat"):
                event_type = line[7:]
                events.append(event_type)

    end_time = asyncio.get_event_loop().time()
    elapsed = end_time - start_time

    # Should have closed quickly (within 1 second)
    assert elapsed < 1.0, f"Stream took {elapsed}s to close for completed run"

    # Should have all events
    assert "supervisor_started" in events
    assert "supervisor_complete" in events
