"""Tests for the trigger system using bearer token authentication."""

import time

from zerg.services.scheduler_service import scheduler_service


def test_webhook_trigger_flow(client):
    """Creating a trigger and firing it should invoke run_agent_task."""

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

    # 3. Monkey-patch SchedulerService.run_agent_task so we can assert it was called
    called = {"flag": False, "agent_id": None, "trigger": None}

    async def _stub_run_agent_task(agent_id: int, trigger: str = "schedule"):  # type: ignore
        called["flag"] = True
        called["agent_id"] = agent_id
        called["trigger"] = trigger

    original = scheduler_service.run_agent_task  # Save original
    scheduler_service.run_agent_task = _stub_run_agent_task  # type: ignore

    try:
        # 4. Fire the trigger via webhook using bearer token authentication
        event_body = {"some": "payload"}

        headers = {"Authorization": f"Bearer {trigger_secret}"}

        fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json=event_body, headers=headers)
        assert fire_resp.status_code == 202, fire_resp.text

        # Give the event-loop running inside TestClient a brief moment to
        # execute the `asyncio.create_task` that the trigger handler spawned.
        time.sleep(0.01)

        assert called["flag"], "run_agent_task should have been invoked by trigger"
        assert called["agent_id"] == agent_id
        assert called["trigger"] == "webhook", "Webhook trigger should pass trigger='webhook'"
    finally:
        # Restore original coroutine to avoid cross-test contamination
        scheduler_service.run_agent_task = original  # type: ignore


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
