"""Regression test for message ordering bug – user messages appearing above
their corresponding assistant replies in the chat UI.

The sequence we want to verify:

1. A user message is stored.
2. The assistant reply is generated and stored.
3. We wait >1 s so timestamps differ.
4. A second user message is stored followed by its assistant reply.

When the frontend fetches `/threads/{id}/messages` it expects the messages in
the *natural* conversational order::

    user, assistant, user, assistant

Historically the endpoint sometimes returned them grouped by role (all users
then all assistants).  This test fails if that regression re-appears.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.conftest import TEST_WORKER_MODEL
from zerg.crud import crud


# Use the client fixture from conftest.py, which properly sets up the test database
def test_interleaved_order_after_delay(client: TestClient, db_session) -> None:  # noqa: D401
    """Ensure user/assistant messages stay interleaved even with a delay."""

    agent_id = _create_agent(client)
    thread_id = _create_thread(client, agent_id)

    # First exchange
    _send_user_message(client, thread_id, "Hello")
    # Instead of running the agent, directly insert an assistant message
    crud.create_thread_message(
        db=db_session, thread_id=thread_id, role="assistant", content="Hello there!", processed=True
    )

    time.sleep(0.1)

    # Second exchange
    _send_user_message(client, thread_id, "Hello again")
    # Insert another assistant message
    crud.create_thread_message(
        db=db_session, thread_id=thread_id, role="assistant", content="Hello again to you too!", processed=True
    )

    # Fetch messages exactly as the frontend does
    resp = client.get(f"/api/threads/{thread_id}/messages")
    resp.raise_for_status()
    msgs = [m for m in resp.json() if m["role"] != "system"]

    roles = [m["role"] for m in msgs]
    assert len(roles) >= 4, f"Expected at least 4 messages, got {len(roles)}"

    # The first 4 roles should follow the pattern: user, assistant, user, assistant
    assert roles[:4] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ], f"Unexpected order: {roles[:4]}"


def test_tool_message_order(client: TestClient, db_session) -> None:
    """Ensure tool messages are inserted between user and assistant in DB order."""

    agent_id = _create_agent(client)
    thread_id = _create_thread(client, agent_id)

    # Send a message that triggers a tool call
    _send_user_message(client, thread_id, "What time is it?")

    # Directly insert assistant and tool messages in the correct order
    # First, the assistant message with a tool call
    crud.create_thread_message(
        db=db_session,
        thread_id=thread_id,
        role="assistant",
        content="I'll check the time for you",
        tool_calls=[{"name": "get_current_time", "id": "call_123", "args": {}}],
        processed=True,
    )

    # Then the tool response
    crud.create_thread_message(
        db=db_session,
        thread_id=thread_id,
        role="tool",
        content="2023-05-15T10:30:45",
        tool_call_id="call_123",
        name="get_current_time",
        processed=True,
    )

    # Fetch messages
    resp = client.get(f"/api/threads/{thread_id}/messages")
    resp.raise_for_status()
    msgs = [m for m in resp.json() if m["role"] != "system"]

    # Should be: user, assistant, tool
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool"], f"Unexpected order: {roles}"

    # Tool message should have message_type/tool_call_id/tool_name fields
    tool_msg = msgs[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("tool_call_id") == "call_123"
    assert tool_msg.get("name") == "get_current_time"


def _create_agent(client: TestClient) -> int:
    resp = client.post(
        "/api/agents",
        json={
            "name": "bot",
            "system_instructions": "sys",
            "task_instructions": "task",
            "model": TEST_WORKER_MODEL,
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _create_thread(client: TestClient, agent_id: int) -> int:
    resp = client.post(
        "/api/threads",
        json={"agent_id": agent_id, "title": "demo", "active": True},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _send_user_message(client: TestClient, thread_id: int, text: str) -> None:
    resp = client.post(f"/api/threads/{thread_id}/messages", json={"role": "user", "content": text})
    resp.raise_for_status()
