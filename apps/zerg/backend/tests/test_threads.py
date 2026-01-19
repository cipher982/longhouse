"""
Test the thread-related endpoints and functionality.

This module contains tests for the /api/threads endpoints,
including CRUD operations for threads and messages.
"""

import pytest
from fastapi.testclient import TestClient

from zerg.crud import crud
from zerg.models.models import Agent
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage


@pytest.fixture
def sample_thread(db_session, sample_agent: Agent):
    """Create a sample thread in the database"""
    thread = Thread(
        agent_id=sample_agent.id,
        title="Test Thread",
        active=True,
        agent_state={"test_key": "test_value"},
        memory_strategy="buffer",
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)
    return thread


@pytest.fixture
def sample_thread_messages(db_session, sample_thread: Thread):
    """Create sample messages for the sample thread"""
    messages = [
        ThreadMessage(
            thread_id=sample_thread.id,
            role="system",
            content="You are a test assistant",
        ),
        ThreadMessage(
            thread_id=sample_thread.id,
            role="user",
            content="Hello, test assistant",
        ),
        ThreadMessage(
            thread_id=sample_thread.id,
            role="assistant",
            content="Hello, I'm the test assistant",
        ),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()
    return messages


def test_read_threads_empty(client: TestClient):
    """Test the GET /api/threads endpoint with an empty database"""
    response = client.get("/api/threads")
    assert response.status_code == 200
    assert response.json() == []


def test_read_threads(client: TestClient, sample_thread: Thread):
    """Test the GET /api/threads endpoint with a sample thread"""
    response = client.get("/api/threads")
    assert response.status_code == 200
    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["id"] == sample_thread.id
    assert threads[0]["title"] == sample_thread.title
    assert threads[0]["agent_id"] == sample_thread.agent_id
    assert threads[0]["active"] == sample_thread.active
    assert threads[0]["agent_state"] == sample_thread.agent_state
    assert threads[0]["memory_strategy"] == sample_thread.memory_strategy


def test_read_threads_filter_by_agent(client: TestClient, sample_agent: Agent, sample_thread: Thread):
    """Test filtering threads by agent_id"""
    response = client.get(f"/api/threads?agent_id={sample_agent.id}")
    assert response.status_code == 200
    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["id"] == sample_thread.id

    # Test with non-existent agent_id
    response = client.get("/api/threads?agent_id=999")  # Assuming this ID doesn't exist
    assert response.status_code == 200
    assert response.json() == []


def test_create_thread(client: TestClient, sample_agent: Agent):
    """Test the POST /api/threads endpoint"""
    thread_data = {
        "title": "New Test Thread",
        "agent_id": sample_agent.id,
        "active": True,
        "memory_strategy": "buffer",
        "agent_state": {"test": "value"},
    }

    # No need to patch AgentRunner for thread creation
    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 201
    created_thread = response.json()
    assert created_thread["title"] == thread_data["title"]
    assert created_thread["agent_id"] == thread_data["agent_id"]
    assert created_thread["active"] == thread_data["active"]
    assert created_thread["memory_strategy"] == thread_data["memory_strategy"]
    assert created_thread["agent_state"] == thread_data["agent_state"]
    assert "id" in created_thread
    assert "created_at" in created_thread
    assert "updated_at" in created_thread

    # Verify the thread was created in the database by fetching it
    response = client.get(f"/api/threads/{created_thread['id']}")
    assert response.status_code == 200
    fetched_thread = response.json()
    assert fetched_thread["id"] == created_thread["id"]
    assert fetched_thread["title"] == thread_data["title"]


def test_create_thread_with_nonexistent_agent(client: TestClient):
    """Test creating a thread with a non-existent agent ID"""
    thread_data = {
        "title": "New Test Thread",
        "agent_id": 999,  # Assuming this ID doesn't exist
        "active": True,
    }

    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Agent not found"


def test_read_thread(client: TestClient, sample_thread: Thread):
    """Test the GET /api/threads/{thread_id} endpoint"""
    response = client.get(f"/api/threads/{sample_thread.id}")
    assert response.status_code == 200
    fetched_thread = response.json()
    assert fetched_thread["id"] == sample_thread.id
    assert fetched_thread["title"] == sample_thread.title
    assert fetched_thread["agent_id"] == sample_thread.agent_id
    assert fetched_thread["active"] == sample_thread.active
    assert fetched_thread["agent_state"] == sample_thread.agent_state
    assert fetched_thread["memory_strategy"] == sample_thread.memory_strategy


def test_read_thread_not_found(client: TestClient):
    """Test the GET /api/threads/{thread_id} endpoint with a non-existent ID"""
    response = client.get("/api/threads/999")  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


def test_update_thread(client: TestClient, sample_thread: Thread):
    """Test the PUT /api/threads/{thread_id} endpoint"""
    update_data = {
        "title": "Updated Test Thread",
        "active": False,
        "agent_state": {"updated": "state"},
        "memory_strategy": "summary",
    }

    response = client.put(f"/api/threads/{sample_thread.id}", json=update_data)
    assert response.status_code == 200
    updated_thread = response.json()
    assert updated_thread["id"] == sample_thread.id
    assert updated_thread["title"] == update_data["title"]
    assert updated_thread["active"] == update_data["active"]
    assert updated_thread["agent_state"] == update_data["agent_state"]
    assert updated_thread["memory_strategy"] == update_data["memory_strategy"]

    # Verify the thread was updated in the database
    response = client.get(f"/api/threads/{sample_thread.id}")
    assert response.status_code == 200
    fetched_thread = response.json()
    assert fetched_thread["title"] == update_data["title"]
    assert fetched_thread["active"] == update_data["active"]


def test_update_thread_not_found(client: TestClient):
    """Test the PUT /api/threads/{thread_id} endpoint with a non-existent ID"""
    update_data = {"title": "Updated Test Thread", "active": False}

    response = client.put("/api/threads/999", json=update_data)  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


def test_delete_thread(client: TestClient, sample_thread: Thread):
    """Test the DELETE /api/threads/{thread_id} endpoint"""
    response = client.delete(f"/api/threads/{sample_thread.id}")
    assert response.status_code == 204

    # Verify the thread was deleted
    response = client.get(f"/api/threads/{sample_thread.id}")
    assert response.status_code == 404


def test_delete_thread_not_found(client: TestClient):
    """Test the DELETE /api/threads/{thread_id} endpoint with a non-existent ID"""
    response = client.delete("/api/threads/999")  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


def test_read_thread_messages(client: TestClient, sample_thread: Thread, sample_thread_messages):
    """Test the GET /api/threads/{thread_id}/messages endpoint"""
    response = client.get(f"/api/threads/{sample_thread.id}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 3


def test_read_thread_messages_excludes_internal(client: TestClient, db_session, sample_thread: Thread):
    """Internal orchestration messages should not be returned to clients."""
    crud.create_thread_message(
        db=db_session,
        thread_id=sample_thread.id,
        role="user",
        content="internal-message",
        internal=True,
    )
    crud.create_thread_message(
        db=db_session,
        thread_id=sample_thread.id,
        role="user",
        content="visible-message",
        internal=False,
    )

    response = client.get(f"/api/threads/{sample_thread.id}/messages")
    assert response.status_code == 200
    contents = [m["content"] for m in response.json()]
    assert "visible-message" in contents
    assert "internal-message" not in contents


def test_read_thread_messages_not_found(client: TestClient):
    """Test the GET /api/threads/{thread_id}/messages endpoint with a non-existent ID"""
    response = client.get("/api/threads/999/messages")  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


def test_read_thread_messages_ordered_by_id(client: TestClient, sample_thread: Thread, db_session):
    """
    Regression test: Verify that GET /threads/{id}/messages returns messages
    strictly ordered by database ID (insertion order), even when timestamps
    are out of order.

    This ensures deterministic ordering regardless of timestamp precision issues.
    """
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    from zerg.models.models import ThreadMessage

    # Create messages with intentionally out-of-order timestamps
    # but they will get sequential IDs from auto-increment
    base_time = datetime.now(UTC)

    msg1 = ThreadMessage(
        thread_id=sample_thread.id,
        role="user",
        content="First message",
        sent_at=(base_time + timedelta(seconds=10)).isoformat(),  # Later sent time
    )
    msg2 = ThreadMessage(
        thread_id=sample_thread.id,
        role="user",
        content="Second message",
        sent_at=(base_time + timedelta(seconds=5)).isoformat(),  # Earlier sent time
    )
    msg3 = ThreadMessage(
        thread_id=sample_thread.id,
        role="user",
        content="Third message",
        sent_at=(base_time + timedelta(seconds=15)).isoformat(),  # Latest sent time
    )

    # Add in reverse order to emphasize that insertion order doesn't matter
    db_session.add(msg3)
    db_session.add(msg1)
    db_session.add(msg2)
    db_session.commit()

    # Refresh to get IDs assigned
    db_session.refresh(msg1)
    db_session.refresh(msg2)
    db_session.refresh(msg3)

    # Store IDs to verify ordering
    ids = [msg1.id, msg2.id, msg3.id]
    min_id = min(ids)
    max_id = max(ids)

    # API response must be ordered by ID (ascending)
    response = client.get(f"/api/threads/{sample_thread.id}/messages")
    assert response.status_code == 200
    messages = response.json()

    # Filter to only our test messages
    test_messages = [m for m in messages if m["id"] in ids]
    assert len(test_messages) == 3

    # Verify strict ID-ascending order
    message_ids = [m["id"] for m in test_messages]
    assert message_ids == sorted(message_ids), f"Messages must be ordered by ID ascending, but got: {message_ids}"

    # Verify first message has the minimum ID (not necessarily the earliest timestamp)
    assert test_messages[0]["id"] == min_id
    assert test_messages[-1]["id"] == max_id


def test_create_thread_message(client: TestClient, sample_thread: Thread):
    """Test the POST /api/threads/{thread_id}/messages endpoint (without processing)"""
    message_data = {"role": "user", "content": "Hello, assistant"}

    response = client.post(f"/api/threads/{sample_thread.id}/messages", json=message_data)
    assert response.status_code == 201

    created_message = response.json()
    assert created_message["thread_id"] == sample_thread.id
    assert created_message["role"] == message_data["role"]
    assert created_message["content"] == message_data["content"]
    assert created_message["processed"] is False  # Verify message is marked as unprocessed


def test_create_thread_message_not_found(client: TestClient):
    """Test the POST /api/threads/{thread_id}/messages endpoint with a non-existent thread ID"""
    message_data = {"role": "user", "content": "Hello, assistant"}

    response = client.post("/api/threads/999/messages", json=message_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


def test_run_thread(client: TestClient, sample_thread: Thread, db_session):
    """Test the POST /api/threads/{thread_id}/run endpoint"""

    # Add a user message
    message = ThreadMessage(thread_id=sample_thread.id, role="user", content="Hello, assistant", processed=False)
    db_session.add(message)
    db_session.commit()

    # Run the thread
    response = client.post(f"/api/threads/{sample_thread.id}/run")
    assert response.status_code == 202

    # Check if message was marked as processed
    db_session.refresh(message)
    assert message.processed is True


def test_run_thread_no_unprocessed_messages(client: TestClient, sample_thread: Thread):
    """Test running a thread with no unprocessed messages"""
    response = client.post(
        f"/api/threads/{sample_thread.id}/run",
        json={"content": "Test message"},
    )
    assert response.status_code == 202  # Changed from 200 to 202 for async operation


def test_run_thread_not_found(client: TestClient):
    """Test the POST /api/threads/{thread_id}/run endpoint with a non-existent thread ID"""
    response = client.post("/api/threads/999/run", json={"content": "Test message"})
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


# ============================================================================
# AUTOMATION THREADS API CONTRACT TESTS
# ============================================================================


def test_create_thread_with_scheduled_type(client: TestClient, sample_agent: Agent):
    """Test creating a scheduled automation thread"""
    thread_data = {
        "title": "Scheduled Automation",
        "agent_id": sample_agent.id,
        "thread_type": "scheduled",
        "active": True,
    }

    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 201
    created_thread = response.json()
    assert created_thread["thread_type"] == "scheduled"
    assert created_thread["title"] == thread_data["title"]
    assert "id" in created_thread


def test_create_thread_with_manual_type(client: TestClient, sample_agent: Agent):
    """Test creating a manual automation thread"""
    thread_data = {
        "title": "Manual Run",
        "agent_id": sample_agent.id,
        "thread_type": "manual",
        "active": True,
    }

    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 201
    created_thread = response.json()
    assert created_thread["thread_type"] == "manual"
    assert created_thread["title"] == thread_data["title"]


def test_create_thread_with_chat_type(client: TestClient, sample_agent: Agent):
    """Test creating a regular chat thread (default type)"""
    thread_data = {
        "title": "Chat Thread",
        "agent_id": sample_agent.id,
        "thread_type": "chat",
        "active": True,
    }

    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 201
    created_thread = response.json()
    assert created_thread["thread_type"] == "chat"


def test_filter_threads_by_type_scheduled(client: TestClient, sample_agent: Agent):
    """Test filtering threads by thread_type='scheduled'"""
    # Create threads of different types
    chat_thread = {
        "title": "Chat Thread",
        "agent_id": sample_agent.id,
        "thread_type": "chat",
    }
    scheduled_thread = {
        "title": "Scheduled Run",
        "agent_id": sample_agent.id,
        "thread_type": "scheduled",
    }
    manual_thread = {
        "title": "Manual Run",
        "agent_id": sample_agent.id,
        "thread_type": "manual",
    }

    client.post("/api/threads", json=chat_thread)
    client.post("/api/threads", json=scheduled_thread)
    client.post("/api/threads", json=manual_thread)

    # Filter by scheduled
    response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=scheduled")
    assert response.status_code == 200
    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["thread_type"] == "scheduled"
    assert threads[0]["title"] == "Scheduled Run"


def test_filter_threads_by_type_manual(client: TestClient, sample_agent: Agent):
    """Test filtering threads by thread_type='manual'"""
    # Create threads
    chat_thread = {
        "title": "Chat Thread",
        "agent_id": sample_agent.id,
        "thread_type": "chat",
    }
    manual_thread = {
        "title": "Manual Run",
        "agent_id": sample_agent.id,
        "thread_type": "manual",
    }

    client.post("/api/threads", json=chat_thread)
    client.post("/api/threads", json=manual_thread)

    # Filter by manual
    response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=manual")
    assert response.status_code == 200
    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["thread_type"] == "manual"
    assert threads[0]["title"] == "Manual Run"


def test_filter_threads_by_type_chat(client: TestClient, sample_agent: Agent):
    """Test filtering threads by thread_type='chat'"""
    # Create threads
    chat_thread = {
        "title": "Chat Thread",
        "agent_id": sample_agent.id,
        "thread_type": "chat",
    }
    scheduled_thread = {
        "title": "Scheduled Run",
        "agent_id": sample_agent.id,
        "thread_type": "scheduled",
    }

    client.post("/api/threads", json=chat_thread)
    client.post("/api/threads", json=scheduled_thread)

    # Filter by chat
    response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=chat")
    assert response.status_code == 200
    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["thread_type"] == "chat"
    assert threads[0]["title"] == "Chat Thread"


def test_automation_threads_api_contract(client: TestClient, sample_agent: Agent):
    """
    Contract test: Verify automation threads API returns structure expected by UI.

    The frontend displays automation runs in a collapsible section with:
    - Thread title
    - Created timestamp
    - Badge based on thread_type ('scheduled' or 'manual')

    This test ensures the API contract matches UI expectations.
    """
    # Create automation threads
    scheduled_data = {
        "title": "Scheduled Automation",
        "agent_id": sample_agent.id,
        "thread_type": "scheduled",
    }
    manual_data = {
        "title": "Manual Automation",
        "agent_id": sample_agent.id,
        "thread_type": "manual",
    }

    scheduled_response = client.post("/api/threads", json=scheduled_data)
    manual_response = client.post("/api/threads", json=manual_data)

    assert scheduled_response.status_code == 201
    assert manual_response.status_code == 201

    # Fetch scheduled threads
    response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=scheduled")
    assert response.status_code == 200
    scheduled_threads = response.json()
    assert len(scheduled_threads) == 1

    # Verify API contract for scheduled thread
    thread = scheduled_threads[0]
    assert "id" in thread
    assert "title" in thread
    assert "thread_type" in thread
    assert "created_at" in thread
    assert "updated_at" in thread
    assert thread["thread_type"] == "scheduled"

    # Fetch manual threads
    response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=manual")
    assert response.status_code == 200
    manual_threads = response.json()
    assert len(manual_threads) == 1

    # Verify API contract for manual thread
    thread = manual_threads[0]
    assert "id" in thread
    assert "title" in thread
    assert "thread_type" in thread
    assert "created_at" in thread
    assert thread["thread_type"] == "manual"


def test_automation_threads_separated_from_chat(client: TestClient, sample_agent: Agent):
    """
    Test that automation threads (scheduled/manual) are properly separated
    from chat threads when filtering by type.

    This validates the UI can fetch chat threads and automation threads
    separately for display in different sections.
    """
    # Create 2 chat threads
    client.post(
        "/api/threads",
        json={
            "title": "Chat 1",
            "agent_id": sample_agent.id,
            "thread_type": "chat",
        },
    )
    client.post(
        "/api/threads",
        json={
            "title": "Chat 2",
            "agent_id": sample_agent.id,
            "thread_type": "chat",
        },
    )

    # Create 1 scheduled thread
    client.post(
        "/api/threads",
        json={
            "title": "Scheduled",
            "agent_id": sample_agent.id,
            "thread_type": "scheduled",
        },
    )

    # Create 1 manual thread
    client.post(
        "/api/threads",
        json={
            "title": "Manual",
            "agent_id": sample_agent.id,
            "thread_type": "manual",
        },
    )

    # Fetch chat threads only
    chat_response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=chat")
    assert chat_response.status_code == 200
    chat_threads = chat_response.json()
    assert len(chat_threads) == 2
    assert all(t["thread_type"] == "chat" for t in chat_threads)

    # Fetch automation threads (scheduled)
    scheduled_response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=scheduled")
    assert scheduled_response.status_code == 200
    scheduled_threads = scheduled_response.json()
    assert len(scheduled_threads) == 1
    assert scheduled_threads[0]["thread_type"] == "scheduled"

    # Fetch automation threads (manual)
    manual_response = client.get(f"/api/threads?agent_id={sample_agent.id}&thread_type=manual")
    assert manual_response.status_code == 200
    manual_threads = manual_response.json()
    assert len(manual_threads) == 1
    assert manual_threads[0]["thread_type"] == "manual"

    # Verify no overlap
    chat_ids = {t["id"] for t in chat_threads}
    scheduled_ids = {t["id"] for t in scheduled_threads}
    manual_ids = {t["id"] for t in manual_threads}

    assert len(chat_ids & scheduled_ids) == 0, "Chat threads should not appear in scheduled"
    assert len(chat_ids & manual_ids) == 0, "Chat threads should not appear in manual"
    assert len(scheduled_ids & manual_ids) == 0, "Scheduled should not appear in manual"
