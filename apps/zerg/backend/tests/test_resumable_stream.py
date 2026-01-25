"""Tests for Resumable SSE v1 streaming endpoint.

This test suite verifies the /api/stream/runs/{run_id} endpoint that supports:
- Replay of historical events from database
- Live streaming of new events
- Reconnection with Last-Event-ID header
- DEFERRED run streaming
- Token filtering

These tests use httpx.AsyncClient with ASGITransport for proper async streaming,
which is compatible with pytest-xdist parallel execution.
"""

import json
import asyncio
from typing import List

import httpx
import pytest
from httpx import ASGITransport

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


def parse_sse_events(sse_text: str) -> List[dict]:
    """Parse SSE text into list of event dictionaries."""
    events = []
    current_event = {}

    for line in sse_text.strip().split("\n"):
        if not line.strip():
            # Empty line marks end of event
            if current_event:
                events.append(current_event)
                current_event = {}
        elif line.startswith("id: "):
            current_event["id"] = line[4:]
        elif line.startswith("event: "):
            current_event["event"] = line[7:]
        elif line.startswith("data: "):
            try:
                current_event["data"] = json.loads(line[6:])
            except json.JSONDecodeError:
                current_event["data"] = line[6:]

    # Don't forget the last event if there's no trailing newline
    if current_event:
        events.append(current_event)

    return events


async def collect_sse_events(response, max_events: int = 100) -> List[dict]:
    """Collect SSE events from an async streaming response."""
    events = []
    current_event = {}

    async for line in response.aiter_lines():
        if not line.strip():
            if current_event:
                events.append(current_event)
                current_event = {}
                if len(events) >= max_events:
                    break
        elif line.startswith("id: "):
            current_event["id"] = line[4:]
        elif line.startswith("event: "):
            current_event["event"] = line[7:]
        elif line.startswith("data: "):
            try:
                current_event["data"] = json.loads(line[6:])
            except json.JSONDecodeError:
                current_event["data"] = line[6:]

    # Include last event if stream ends without empty line
    if current_event:
        events.append(current_event)

    return events


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

    # Connect to stream with async client
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

            events = await collect_sse_events(response)

    # Verify we got all historical events
    assert len(events) >= 4, f"Expected at least 4 events, got {len(events)}: {events}"

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
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream, resuming from event2_id
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "GET",
            f"/api/stream/runs/{test_run.id}?after_event_id={event2_id}",
            headers=auth_headers,
        ) as response:
            assert response.status_code == 200
            events = await collect_sse_events(response)

    # Filter out heartbeats
    events = [e for e in events if e.get("event") != "heartbeat"]

    # Should only get event3 (after event2_id)
    assert len(events) >= 1
    assert events[0]["event"] == "supervisor_complete"
    assert events[0]["data"]["payload"]["result"] == "done"


@pytest.mark.asyncio
async def test_stream_with_last_event_id_header(db_session, test_run, test_user, auth_headers):
    """Test SSE standard Last-Event-ID header for automatic reconnect."""
    # Emit historical events
    event1_id = await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "thinking", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect with Last-Event-ID header (SSE standard)
    headers = {**auth_headers, "Last-Event-ID": str(event1_id)}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=headers) as response:
            assert response.status_code == 200
            events = await collect_sse_events(response)

    # Filter out heartbeats
    events = [e for e in events if e.get("event") != "heartbeat"]

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
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "GET",
            f"/api/stream/runs/{test_run.id}?include_tokens=false",
            headers=auth_headers,
        ) as response:
            assert response.status_code == 200
            events = await collect_sse_events(response)

    # Get event types (excluding heartbeats)
    event_types = [e.get("event") for e in events if e.get("event") != "heartbeat"]

    # Should only get non-token events
    assert "supervisor_started" in event_types
    assert "supervisor_complete" in event_types
    assert "supervisor_token" not in event_types


# NOTE: DEFERRED run streaming is not unit-tested here because SSE streams for
# DEFERRED/RUNNING runs never close naturally, and all timeout mechanisms fail
# due to how test frameworks handle streaming connections.
#
# The DEFERRED behavior is verified through:
# 1. Code inspection: stream.py:195 - status check is clear
# 2. E2E tests with Playwright (browser handles SSE properly)
# 3. Manual testing


@pytest.mark.asyncio
async def test_stream_run_not_found(auth_headers):
    """Test 404 error when run doesn't exist or not owned by user."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/stream/runs/99999", headers=auth_headers)
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_stream_completed_run_closes_immediately(db_session, test_run, test_user, auth_headers):
    """Test that completed runs return events and close stream (no live wait)."""
    import time

    # Emit all events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as SUCCESS
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # Connect to stream and time how long it takes
    start_time = time.time()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=5.0) as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
            assert response.status_code == 200
            events = await collect_sse_events(response)

    elapsed = time.time() - start_time

    # Should have closed quickly (within 1 second)
    assert elapsed < 1.0, f"Stream took {elapsed}s to close for completed run"

    # Filter out heartbeats
    event_types = [e.get("event") for e in events if e.get("event") != "heartbeat"]

    # Should have all events
    assert "supervisor_started" in event_types
    assert "supervisor_complete" in event_types


@pytest.mark.asyncio
async def test_stream_event_ids_are_monotonic(db_session, test_run, test_user, auth_headers):
    """Test that event IDs are monotonically increasing for resumption."""
    # Emit several events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "a", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "b", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
            events = await collect_sse_events(response)

    # Filter to events with IDs
    event_ids = [int(e["id"]) for e in events if "id" in e]

    # Verify IDs are monotonically increasing
    assert len(event_ids) >= 4
    for i in range(1, len(event_ids)):
        assert event_ids[i] > event_ids[i - 1], f"Event IDs not monotonic: {event_ids}"


@pytest.mark.asyncio
async def test_stream_resumption_after_reconnect(db_session, test_run, test_user, auth_headers):
    """Test that reconnecting with Last-Event-ID doesn't miss events."""
    # Emit several events
    await emit_run_event(db_session, test_run.id, "supervisor_started", {"task": "test", "owner_id": test_user.id})
    event2_id = await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "step1", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_thinking", {"thought": "step2", "owner_id": test_user.id})
    await emit_run_event(db_session, test_run.id, "supervisor_complete", {"result": "done", "owner_id": test_user.id})

    # Mark run as complete
    test_run.status = RunStatus.SUCCESS
    db_session.commit()

    # First connection - get all events
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
            all_events = await collect_sse_events(response)

    # Reconnect with Last-Event-ID set to event2
    headers = {**auth_headers, "Last-Event-ID": str(event2_id)}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=headers) as response:
            resumed_events = await collect_sse_events(response)

    # Filter heartbeats
    all_event_types = [e.get("event") for e in all_events if e.get("event") != "heartbeat"]
    resumed_event_types = [e.get("event") for e in resumed_events if e.get("event") != "heartbeat"]

    # All events should have all 4
    assert len(all_event_types) == 4

    # Resumed should only have events after event2 (2 events: step2 thinking + complete)
    assert len(resumed_event_types) == 2
    assert resumed_event_types[0] == "supervisor_thinking"
    assert resumed_event_types[1] == "supervisor_complete"


@pytest.mark.asyncio
async def test_stream_closes_on_supervisor_complete_with_pending_workers(db_session, test_run, test_user, auth_headers):
    """Stream should close on supervisor_complete even if workers are still pending."""
    from zerg.models.models import WorkerJob

    # Create a worker job to reference in worker_spawned payload
    job = WorkerJob(
        owner_id=test_user.id,
        task="Long task",
        model="gpt-5-mini",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    async def emit_events():
        # Let the stream start listening before emitting
        await asyncio.sleep(0.1)
        await emit_run_event(
            db_session,
            test_run.id,
            "worker_spawned",
            {
                "job_id": job.id,
                "tool_call_id": "tool-123",
                "task": "Long task",
                "model": "gpt-5-mini",
                "owner_id": test_user.id,
            },
        )
        await asyncio.sleep(0.1)
        await emit_run_event(
            db_session,
            test_run.id,
            "supervisor_complete",
            {"result": "done", "owner_id": test_user.id},
        )

    emitter_task = asyncio.create_task(emit_events())

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=5.0) as client:
        async with client.stream("GET", f"/api/stream/runs/{test_run.id}", headers=auth_headers) as response:
            assert response.status_code == 200
            events = await collect_sse_events(response, max_events=10)

    await emitter_task

    event_types = [e.get("event") for e in events if e.get("event") != "heartbeat"]
    assert "worker_spawned" in event_types
    assert "supervisor_complete" in event_types
