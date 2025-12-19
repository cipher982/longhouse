"""Tests for the trigger system using bearer token authentication."""

import time
from unittest.mock import AsyncMock, patch

from zerg.events import EventType


def test_webhook_trigger_flow(client):
    """Creating a trigger and firing it should publish TRIGGER_FIRED event."""

    # 1. Create an agent first
    agent_payload = {
        "name": "Trigger Agent",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }

    resp = client.post("/api/agents/", json=agent_payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]

    # 2. Create a webhook trigger for that agent
    trg_payload = {"agent_id": agent_id, "type": "webhook"}
    trg_resp = client.post("/api/triggers/", json=trg_payload)
    assert trg_resp.status_code == 201, trg_resp.text
    trigger_data = trg_resp.json()
    trigger_id = trigger_data["id"]
    trigger_secret = trigger_data["secret"]

    # 3. Track TRIGGER_FIRED events
    published_events = []

    async def mock_publish(event_type, data):
        published_events.append({"event_type": event_type, "data": data})

    with patch("zerg.routers.triggers.event_bus.publish", new=mock_publish):
        # 4. Fire the trigger via webhook using bearer token authentication
        event_body = {"some": "payload"}
        headers = {"Authorization": f"Bearer {trigger_secret}"}

        fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json=event_body, headers=headers)
        assert fire_resp.status_code == 202, fire_resp.text

        # Give the event-loop a brief moment to process the create_task
        time.sleep(0.05)

    # 5. Verify TRIGGER_FIRED event was published
    trigger_fired_events = [e for e in published_events if e["event_type"] == EventType.TRIGGER_FIRED]
    assert len(trigger_fired_events) == 1, f"Expected 1 TRIGGER_FIRED event, got {len(trigger_fired_events)}"

    event_data = trigger_fired_events[0]["data"]
    assert event_data["trigger_id"] == trigger_id
    assert event_data["agent_id"] == agent_id
    assert event_data["payload"] == event_body
    assert event_data["trigger_type"] == "webhook"


def test_webhook_trigger_invalid_token(client):
    """Firing a trigger with an invalid token should return 404."""

    # 1. Create an agent and trigger
    agent_payload = {
        "name": "Test Agent",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }
    resp = client.post("/api/agents/", json=agent_payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]

    trg_payload = {"agent_id": agent_id, "type": "webhook"}
    trg_resp = client.post("/api/triggers/", json=trg_payload)
    assert trg_resp.status_code == 201, trg_resp.text
    trigger_id = trg_resp.json()["id"]

    # 2. Try to fire with invalid token
    headers = {"Authorization": "Bearer invalid-token-xyz"}
    fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json={}, headers=headers)
    assert fire_resp.status_code == 404, "Invalid token should return 404"
    assert fire_resp.json()["detail"] == "Not found"


def test_webhook_trigger_unknown_trigger(client):
    """Firing a non-existent trigger should return 404."""

    # Try to fire a trigger that doesn't exist
    headers = {"Authorization": "Bearer some-token"}
    fire_resp = client.post("/api/triggers/99999/events", json={}, headers=headers)
    assert fire_resp.status_code == 404, "Unknown trigger should return 404"
    assert fire_resp.json()["detail"] == "Not found"


def test_webhook_trigger_missing_auth_header(client):
    """Firing a trigger without Authorization header should return 404."""

    # 1. Create an agent and trigger
    agent_payload = {
        "name": "Test Agent",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }
    resp = client.post("/api/agents/", json=agent_payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]

    trg_payload = {"agent_id": agent_id, "type": "webhook"}
    trg_resp = client.post("/api/triggers/", json=trg_payload)
    assert trg_resp.status_code == 201, trg_resp.text
    trigger_id = trg_resp.json()["id"]

    # 2. Try to fire without Authorization header
    fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json={})
    assert fire_resp.status_code == 404, "Missing auth header should return 404"
    assert fire_resp.json()["detail"] == "Not found"


def test_webhook_trigger_body_too_large(client):
    """Firing a trigger with oversized payload should return 413."""

    # 1. Create an agent and trigger
    agent_payload = {
        "name": "Test Agent",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }
    resp = client.post("/api/agents/", json=agent_payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]

    trg_payload = {"agent_id": agent_id, "type": "webhook"}
    trg_resp = client.post("/api/triggers/", json=trg_payload)
    assert trg_resp.status_code == 201, trg_resp.text
    trigger_data = trg_resp.json()
    trigger_id = trigger_data["id"]
    trigger_secret = trigger_data["secret"]

    # 2. Create a payload larger than 256 KiB
    large_payload = {"data": "x" * (256 * 1024 + 1)}  # Slightly over 256 KiB

    headers = {"Authorization": f"Bearer {trigger_secret}"}
    fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json=large_payload, headers=headers)
    assert fire_resp.status_code == 413, "Oversized payload should return 413"
    assert "too large" in fire_resp.json()["detail"].lower()
