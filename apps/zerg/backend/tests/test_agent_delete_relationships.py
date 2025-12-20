"""Regression test: deleting an agent that owns threads/messages should
return 204 *No Content*, remove all related objects and publish the
*agent_deleted* event without crashing the server.

Before commit 3e5fd10 the DELETE endpoint returned the raw SQLAlchemy model
while still declaring a 204 response.  FastAPI tried to serialise that model,
hit a cyclic relationship (agent -> thread -> agent …) and raised
RecursionError.  This test covers that scenario so we never regress again.
"""

# We *deliberately* do not depend on the custom ``event_tracker`` fixture used
# in other tests because it is defined in *test_agent_events.py* (module-local
# scope).  Instead we create a minimal ad-hoc tracker right here so the test
# remains self-contained and robust against fixture refactors.

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_MODEL
from zerg.events import EventType
from zerg.events import event_bus


@pytest.mark.asyncio
async def test_delete_agent_with_relationships(client: TestClient):
    """Create agent → thread → message, then delete the agent."""

    # ------------------------------------------------------------------
    # Prepare an in-memory list to collect published events
    # ------------------------------------------------------------------
    events: list[dict] = []

    async def _track(evt):
        events.append(evt)

    event_bus.subscribe(EventType.AGENT_DELETED, _track)

    # ------------------------------------------------------------------
    # 1. Create agent
    # ------------------------------------------------------------------
    agent_payload = {
        "name": "Agent With Threads",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": TEST_MODEL,
    }

    resp = client.post("/api/agents", json=agent_payload)
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    # ------------------------------------------------------------------
    # 2. Create a thread for that agent
    # ------------------------------------------------------------------
    thread_payload = {
        "title": "t1",
        "agent_id": agent_id,
        "active": True,
        "memory_strategy": "buffer",
    }

    resp = client.post("/api/threads", json=thread_payload)
    assert resp.status_code == 201
    thread_id = resp.json()["id"]

    # ------------------------------------------------------------------
    # 3. Add a user message to the thread (creates deeper relationship)
    # ------------------------------------------------------------------
    msg_payload = {"role": "user", "content": "hello"}
    resp = client.post(f"/api/threads/{thread_id}/messages", json=msg_payload)
    assert resp.status_code == 201

    # ------------------------------------------------------------------
    # 4. Delete the agent and assert 204 No Content with *empty* body
    # ------------------------------------------------------------------
    resp = client.delete(f"/api/agents/{agent_id}")
    assert resp.status_code == 204
    assert resp.content == b""  # Starlette sends an empty body for 204

    # ------------------------------------------------------------------
    # 5. Verify the agent and its thread are gone
    # ------------------------------------------------------------------
    assert client.get(f"/api/agents/{agent_id}").status_code == 404
    assert client.get(f"/api/threads/{thread_id}").status_code == 404

    # ------------------------------------------------------------------
    # 6. The *agent_deleted* event should have been published with the id
    # ------------------------------------------------------------------
    assert any(evt.get("id") == agent_id for evt in events)

    # Clean up subscription to avoid leaking callbacks between tests
    event_bus.unsubscribe(EventType.AGENT_DELETED, _track)
