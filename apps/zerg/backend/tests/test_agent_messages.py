from fastapi.testclient import TestClient

from zerg.models.models import Agent


def test_read_agent_messages_empty(client: TestClient, sample_agent: Agent):
    """Test the GET /api/agents/{agent_id}/messages endpoint with no messages"""
    # First delete any sample messages that might have been created
    response = client.get(f"/api/agents/{sample_agent.id}")
    _ = response.json()

    # Create a new agent with no messages
    agent_data = {
        "name": "Agent Without Messages",
        "system_instructions": "System instructions for agent without messages",
        "task_instructions": "This agent has no messages",
        "model": "gpt-5.1-chat-latest",
    }
    response = client.post("/api/agents", json=agent_data)
    new_agent = response.json()

    # Get messages for the new agent
    response = client.get(f"/api/agents/{new_agent['id']}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 0
    assert messages == []


def test_read_agent_messages(client: TestClient, sample_agent: Agent, sample_messages):
    """Test the GET /api/agents/{agent_id}/messages endpoint with sample messages"""
    response = client.get(f"/api/agents/{sample_agent.id}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 3  # We created 3 sample messages

    # Verify each message has expected properties
    for message in messages:
        assert "id" in message
        assert message["agent_id"] == sample_agent.id
        assert "role" in message
        assert "content" in message
        assert "timestamp" in message


def test_read_agent_messages_not_found(client: TestClient):
    """Test the GET /api/agents/{agent_id}/messages endpoint with a non-existent agent"""
    response = client.get("/api/agents/999/messages")
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Agent not found"


def test_create_agent_message(client: TestClient, sample_agent: Agent):
    """Test the POST /api/agents/{agent_id}/messages endpoint"""
    message_data = {"role": "user", "content": "This is a new test message"}

    response = client.post(f"/api/agents/{sample_agent.id}/messages", json=message_data)
    assert response.status_code == 201
    created_message = response.json()
    assert created_message["role"] == message_data["role"]
    assert created_message["content"] == message_data["content"]
    assert created_message["agent_id"] == sample_agent.id
    assert "id" in created_message
    assert "timestamp" in created_message

    # Verify the message was added to the agent's messages
    response = client.get(f"/api/agents/{sample_agent.id}/messages")
    assert response.status_code == 200
    messages = response.json()
    message_ids = [m["id"] for m in messages]
    assert created_message["id"] in message_ids


def test_create_agent_message_not_found(client: TestClient):
    """Test the POST /api/agents/{agent_id}/messages endpoint with a non-existent agent"""
    message_data = {"role": "user", "content": "This agent doesn't exist"}

    response = client.post("/api/agents/999/messages", json=message_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Agent not found"


def test_create_agent_message_validation(client: TestClient, sample_agent: Agent):
    """Test validation for the POST /api/agents/{agent_id}/messages endpoint"""
    # Test with missing required field
    message_data = {"content": "This message is missing the role field"}

    response = client.post(f"/api/agents/{sample_agent.id}/messages", json=message_data)
    assert response.status_code == 422  # Unprocessable Entity

    # Test with empty content
    message_data = {"role": "user", "content": ""}

    response = client.post(f"/api/agents/{sample_agent.id}/messages", json=message_data)
    assert response.status_code == 201  # Empty content is actually allowed
