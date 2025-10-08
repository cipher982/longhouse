"""
Test the thread-related endpoints and functionality.

This module contains tests for the /api/threads endpoints,
including CRUD operations for threads and messages.
"""

import pytest
from fastapi.testclient import TestClient

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
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[0]["thread_id"] == sample_thread.id


def test_read_thread_messages_not_found(client: TestClient):
    """Test the GET /api/threads/{thread_id}/messages endpoint with a non-existent ID"""
    response = client.get("/api/threads/999/messages")  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Thread not found"


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
