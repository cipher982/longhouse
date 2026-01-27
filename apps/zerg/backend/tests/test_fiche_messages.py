from fastapi.testclient import TestClient

from tests.conftest import TEST_MODEL
from zerg.models.models import Fiche


def test_read_fiche_messages_empty(client: TestClient, sample_fiche: Fiche):
    """Test the GET /api/fiches/{fiche_id}/messages endpoint with no messages"""
    # First delete any sample messages that might have been created
    response = client.get(f"/api/fiches/{sample_fiche.id}")
    _ = response.json()

    # Create a new fiche with no messages
    fiche_data = {
        "name": "Fiche Without Messages",
        "system_instructions": "System instructions for fiche without messages",
        "task_instructions": "This fiche has no messages",
        "model": TEST_MODEL,
    }
    response = client.post("/api/fiches", json=fiche_data)
    new_fiche = response.json()

    # Get messages for the new fiche
    response = client.get(f"/api/fiches/{new_fiche['id']}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 0
    assert messages == []


def test_read_fiche_messages(client: TestClient, sample_fiche: Fiche, sample_messages):
    """Test the GET /api/fiches/{fiche_id}/messages endpoint with sample messages"""
    response = client.get(f"/api/fiches/{sample_fiche.id}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 3  # We created 3 sample messages

    # Verify each message has expected properties
    for message in messages:
        assert "id" in message
        assert message["fiche_id"] == sample_fiche.id
        assert "role" in message
        assert "content" in message
        assert "timestamp" in message


def test_read_fiche_messages_not_found(client: TestClient):
    """Test the GET /api/fiches/{fiche_id}/messages endpoint with a non-existent fiche"""
    response = client.get("/api/fiches/999/messages")
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_create_fiche_message(client: TestClient, sample_fiche: Fiche):
    """Test the POST /api/fiches/{fiche_id}/messages endpoint"""
    message_data = {"role": "user", "content": "This is a new test message"}

    response = client.post(f"/api/fiches/{sample_fiche.id}/messages", json=message_data)
    assert response.status_code == 201
    created_message = response.json()
    assert created_message["role"] == message_data["role"]
    assert created_message["content"] == message_data["content"]
    assert created_message["fiche_id"] == sample_fiche.id
    assert "id" in created_message
    assert "timestamp" in created_message

    # Verify the message was added to the fiche's messages
    response = client.get(f"/api/fiches/{sample_fiche.id}/messages")
    assert response.status_code == 200
    messages = response.json()
    message_ids = [m["id"] for m in messages]
    assert created_message["id"] in message_ids


def test_create_fiche_message_not_found(client: TestClient):
    """Test the POST /api/fiches/{fiche_id}/messages endpoint with a non-existent fiche"""
    message_data = {"role": "user", "content": "This fiche doesn't exist"}

    response = client.post("/api/fiches/999/messages", json=message_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_create_fiche_message_validation(client: TestClient, sample_fiche: Fiche):
    """Test validation for the POST /api/fiches/{fiche_id}/messages endpoint"""
    # Test with missing required field
    message_data = {"content": "This message is missing the role field"}

    response = client.post(f"/api/fiches/{sample_fiche.id}/messages", json=message_data)
    assert response.status_code == 422  # Unprocessable Entity

    # Test with empty content
    message_data = {"role": "user", "content": ""}

    response = client.post(f"/api/fiches/{sample_fiche.id}/messages", json=message_data)
    assert response.status_code == 201  # Empty content is actually allowed
