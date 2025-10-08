"""Failing test for thread‑history subscription bug.

The desired behaviour: subscribing to a thread that already contains
messages should yield a ``thread_history`` payload with those messages.

Current behaviour (before the bug fix): an ``error`` payload is returned.
This test therefore fails – confirming the issue is reproducible.
"""

from fastapi.testclient import TestClient

from zerg.models.models import Thread


def test_read_thread_messages_via_rest_returns_history(
    test_client: TestClient,
    sample_thread: Thread,
    sample_thread_messages,  # populated by fixture
):
    """Reading thread messages via REST should return existing history."""
    # Ensure fixture created at least one message for the thread
    assert sample_thread.messages, "Fixture must create initial messages"

    # Call REST API to fetch thread messages
    url = f"/api/threads/{sample_thread.id}/messages"
    response = test_client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()

    # Verify the number of returned messages matches the DB
    assert isinstance(data, list)
    assert len(data) == len(sample_thread.messages)
    # Optionally, verify each message has expected fields
    for msg in data:
        assert msg.get("id") is not None
        assert msg.get("thread_id") == sample_thread.id
        assert msg.get("content") is not None
        assert msg.get("message_type") in {
            "user_message",
            "assistant_message",
            "tool_output",
            "system_message",
        }
