# Adapted for Stage-5 HMAC hardened trigger endpoint.

import hashlib
import hmac
import json
import time

from zerg import constants
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
    trigger_id = trg_resp.json()["id"]

    # 3. Monkey‑patch SchedulerService.run_agent_task so we can assert it was called
    called = {"flag": False, "agent_id": None}

    async def _stub_run_agent_task(agent_id: int):  # type: ignore
        called["flag"] = True
        called["agent_id"] = agent_id

    original = scheduler_service.run_agent_task  # Save original
    scheduler_service.run_agent_task = _stub_run_agent_task  # type: ignore

    try:
        # 4. Fire the trigger via webhook using new HMAC headers
        event_body = {"some": "payload"}

        # deterministic JSON serialisation
        body_serialised = json.dumps(event_body, separators=(",", ":"), sort_keys=True)
        timestamp = str(int(time.time()))
        data_to_sign = f"{timestamp}.{body_serialised}".encode()
        signature = hmac.new(
            constants.TRIGGER_SIGNING_SECRET.encode(),
            data_to_sign,
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "X-Zerg-Timestamp": timestamp,
            "X-Zerg-Signature": signature,
        }

        fire_resp = client.post(f"/api/triggers/{trigger_id}/events", json=event_body, headers=headers)
        assert fire_resp.status_code == 202, fire_resp.text

        # Give the event‐loop running inside TestClient a brief moment to
        # execute the `asyncio.create_task` that the trigger handler spawned.
        time.sleep(0.01)

        assert called["flag"], "run_agent_task should have been invoked by trigger"
        assert called["agent_id"] == agent_id
    finally:
        # Restore original coroutine to avoid cross‑test contamination
        scheduler_service.run_agent_task = original  # type: ignore
