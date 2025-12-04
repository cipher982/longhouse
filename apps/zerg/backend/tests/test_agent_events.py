"""Tests for agent event publishing."""

import pytest
from fastapi.testclient import TestClient

from zerg.events import EventType
from zerg.events import event_bus
from zerg.models import Agent
from tests.conftest import TEST_MODEL, TEST_WORKER_MODEL


@pytest.fixture
def event_tracker():
    """Fixture to track events published during tests."""
    events = []

    async def track_event(data: dict):
        events.append(data)

    return events, track_event


@pytest.mark.asyncio
async def test_create_agent_event(client: TestClient, event_tracker):
    """Test that creating an agent publishes an event."""
    events, track_event = event_tracker

    # Subscribe to agent created events
    event_bus.subscribe(EventType.AGENT_CREATED, track_event)

    # Create an agent
    agent_data = {
        "name": "Event Test Agent",
        "system_instructions": "Test system instructions",
        "task_instructions": "Test task instructions",
        "model": TEST_MODEL,
    }

    response = client.post("/api/agents", json=agent_data)
    assert response.status_code == 201
    _ = response.json()

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    # Name is auto-generated as "New Agent" in create_agent
    assert event_data["name"] == "New Agent"
    assert event_data["id"] is not None


@pytest.mark.asyncio
async def test_update_agent_event(client: TestClient, event_tracker, sample_agent: Agent):
    """Test that updating an agent publishes an event."""
    events, track_event = event_tracker

    # Subscribe to agent updated events
    event_bus.subscribe(EventType.AGENT_UPDATED, track_event)

    # Update the agent
    update_data = {"name": "Updated Event Test Agent", "status": "processing"}

    response = client.put(f"/api/agents/{sample_agent.id}", json=update_data)
    assert response.status_code == 200

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    assert event_data["name"] == update_data["name"]
    assert event_data["status"] == update_data["status"]
    assert event_data["id"] == sample_agent.id


@pytest.mark.asyncio
async def test_delete_agent_event(client: TestClient, event_tracker, sample_agent: Agent):
    """Test that deleting an agent publishes an event."""
    events, track_event = event_tracker

    # Subscribe to agent deleted events
    event_bus.subscribe(EventType.AGENT_DELETED, track_event)

    # Delete the agent
    response = client.delete(f"/api/agents/{sample_agent.id}")
    assert response.status_code == 204

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    assert event_data["id"] == sample_agent.id
    assert "name" in event_data  # We include the name in delete events
