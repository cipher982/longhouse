"""Tests for fiche event publishing."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_MODEL
from zerg.events import EventType
from zerg.events import event_bus
from zerg.models import Fiche


@pytest.fixture
def event_tracker():
    """Fixture to track events published during tests."""
    events = []

    async def track_event(data: dict):
        events.append(data)

    return events, track_event


@pytest.mark.asyncio
async def test_create_fiche_event(client: TestClient, event_tracker):
    """Test that creating an fiche publishes an event."""
    events, track_event = event_tracker

    # Subscribe to fiche created events
    event_bus.subscribe(EventType.FICHE_CREATED, track_event)

    # Create an fiche
    fiche_data = {
        "name": "Event Test Fiche",
        "system_instructions": "Test system instructions",
        "task_instructions": "Test task instructions",
        "model": TEST_MODEL,
    }

    response = client.post("/api/fiches", json=fiche_data)
    assert response.status_code == 201
    _ = response.json()

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    # Name is auto-generated as "New Fiche" in create_fiche
    assert event_data["name"] == "New Fiche"
    assert event_data["id"] is not None


@pytest.mark.asyncio
async def test_update_fiche_event(client: TestClient, event_tracker, sample_fiche: Fiche):
    """Test that updating an fiche publishes an event."""
    events, track_event = event_tracker

    # Subscribe to fiche updated events
    event_bus.subscribe(EventType.FICHE_UPDATED, track_event)

    # Update the fiche
    update_data = {"name": "Updated Event Test Fiche", "status": "processing"}

    response = client.put(f"/api/fiches/{sample_fiche.id}", json=update_data)
    assert response.status_code == 200

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    assert event_data["name"] == update_data["name"]
    assert event_data["status"] == update_data["status"]
    assert event_data["id"] == sample_fiche.id


@pytest.mark.asyncio
async def test_delete_fiche_event(client: TestClient, event_tracker, sample_fiche: Fiche):
    """Test that deleting an fiche publishes an event."""
    events, track_event = event_tracker

    # Subscribe to fiche deleted events
    event_bus.subscribe(EventType.FICHE_DELETED, track_event)

    # Delete the fiche
    response = client.delete(f"/api/fiches/{sample_fiche.id}")
    assert response.status_code == 204

    # Verify event was published
    assert len(events) == 1
    event_data = events[0]
    assert event_data["id"] == sample_fiche.id
    assert "name" in event_data  # We include the name in delete events
