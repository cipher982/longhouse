from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.models.models import Fiche


def test_read_fiches_empty(client: TestClient):
    """Test the GET /api/fiches endpoint with an empty database"""
    response = client.get("/api/fiches")
    assert response.status_code == 200
    assert response.json() == []


def test_read_fiches(client: TestClient, sample_fiche: Fiche):
    """Test the GET /api/fiches endpoint with a sample fiche"""
    response = client.get("/api/fiches")
    assert response.status_code == 200
    fiches = response.json()
    assert len(fiches) == 1
    assert fiches[0]["id"] == sample_fiche.id
    assert fiches[0]["name"] == sample_fiche.name
    assert fiches[0]["system_instructions"] == sample_fiche.system_instructions
    assert fiches[0]["task_instructions"] == sample_fiche.task_instructions
    assert fiches[0]["model"] == sample_fiche.model
    assert fiches[0]["status"] == sample_fiche.status


def test_create_fiche(client: TestClient):
    """Test the POST /api/fiches endpoint"""
    fiche_data = {
        "system_instructions": "System instructions for new test fiche",
        "task_instructions": "This is a new test fiche",
        "model": TEST_MODEL,
    }

    response = client.post("/api/fiches", json=fiche_data)
    assert response.status_code == 201
    created_fiche = response.json()
    # Backend auto-generates "New Fiche" as placeholder
    assert created_fiche["name"] == "New Fiche"
    assert created_fiche["system_instructions"] == fiche_data["system_instructions"]
    assert created_fiche["task_instructions"] == fiche_data["task_instructions"]
    assert created_fiche["model"] == fiche_data["model"]
    assert created_fiche["status"] == "idle"  # Default value
    assert "id" in created_fiche

    # Verify the fiche was created in the database by fetching it
    response = client.get(f"/api/fiches/{created_fiche['id']}")
    assert response.status_code == 200
    fetched_fiche = response.json()
    assert fetched_fiche["id"] == created_fiche["id"]
    assert fetched_fiche["name"] == "New Fiche"


def test_create_and_rename_fiche(client: TestClient):
    """Test that user can rename fiche after creation"""
    # Create fiche
    fiche_data = {
        "system_instructions": "System instructions",
        "task_instructions": "Task instructions",
        "model": TEST_MODEL,
    }
    response = client.post("/api/fiches", json=fiche_data)
    assert response.status_code == 201
    created_fiche = response.json()
    assert created_fiche["name"] == "New Fiche"

    # Rename the fiche
    update_data = {"name": "My Custom Fiche"}
    response = client.put(f"/api/fiches/{created_fiche['id']}", json=update_data)
    assert response.status_code == 200
    updated_fiche = response.json()
    assert updated_fiche["name"] == "My Custom Fiche"


def test_read_fiche(client: TestClient, sample_fiche: Fiche):
    """Test the GET /api/fiches/{fiche_id} endpoint"""
    response = client.get(f"/api/fiches/{sample_fiche.id}")
    assert response.status_code == 200
    fetched_fiche = response.json()
    assert fetched_fiche["id"] == sample_fiche.id
    assert fetched_fiche["name"] == sample_fiche.name
    assert fetched_fiche["system_instructions"] == sample_fiche.system_instructions
    assert fetched_fiche["task_instructions"] == sample_fiche.task_instructions
    assert fetched_fiche["model"] == sample_fiche.model
    assert fetched_fiche["status"] == sample_fiche.status


def test_read_fiche_not_found(client: TestClient):
    """Test the GET /api/fiches/{fiche_id} endpoint with a non-existent ID"""
    response = client.get("/api/fiches/999")  # Assuming this ID doesn't exist
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_update_fiche(client: TestClient, sample_fiche: Fiche):
    """Test the PUT /api/fiches/{fiche_id} endpoint"""
    update_data = {
        "name": "Updated Fiche Name",
        "system_instructions": "Updated system instructions",
        "task_instructions": "Updated task instructions",
        "model": TEST_COMMIS_MODEL,
        "status": "processing",
    }

    response = client.put(f"/api/fiches/{sample_fiche.id}", json=update_data)
    assert response.status_code == 200
    updated_fiche = response.json()
    assert updated_fiche["id"] == sample_fiche.id
    assert updated_fiche["name"] == update_data["name"]
    assert updated_fiche["system_instructions"] == update_data["system_instructions"]
    assert updated_fiche["task_instructions"] == update_data["task_instructions"]
    assert updated_fiche["model"] == update_data["model"]
    assert updated_fiche["status"] == update_data["status"]

    # Verify the fiche was updated in the database
    response = client.get(f"/api/fiches/{sample_fiche.id}")
    assert response.status_code == 200
    fetched_fiche = response.json()
    assert fetched_fiche["name"] == update_data["name"]


def test_update_fiche_allowed_tools(client: TestClient, sample_fiche: Fiche):
    """Test updating fiche allowed_tools field with various list formats"""
    # Test with list of tools
    update_data = {"allowed_tools": ["tool1", "tool2", "tool3"]}
    response = client.put(f"/api/fiches/{sample_fiche.id}", json=update_data)
    assert response.status_code == 200
    updated_fiche = response.json()
    assert updated_fiche["allowed_tools"] == ["tool1", "tool2", "tool3"]

    # Test with empty list (restrict to no tools)
    update_data = {"allowed_tools": []}
    response = client.put(f"/api/fiches/{sample_fiche.id}", json=update_data)
    assert response.status_code == 200
    updated_fiche = response.json()
    assert updated_fiche["allowed_tools"] == []

    # Test with wildcards
    update_data = {"allowed_tools": ["http_*", "mcp:github/*"]}
    response = client.put(f"/api/fiches/{sample_fiche.id}", json=update_data)
    assert response.status_code == 200
    updated_fiche = response.json()
    assert updated_fiche["allowed_tools"] == ["http_*", "mcp:github/*"]


def test_update_fiche_not_found(client: TestClient):
    """Test the PUT /api/fiches/{fiche_id} endpoint with a non-existent ID"""
    update_data = {"name": "This fiche doesn't exist"}

    response = client.put("/api/fiches/999", json=update_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_delete_fiche(client: TestClient, sample_fiche: Fiche):
    """Test the DELETE /api/fiches/{fiche_id} endpoint"""
    response = client.delete(f"/api/fiches/{sample_fiche.id}")
    assert response.status_code == 204

    # Verify the fiche was deleted
    response = client.get(f"/api/fiches/{sample_fiche.id}")
    assert response.status_code == 404


def test_delete_fiche_not_found(client: TestClient):
    """Test the DELETE /api/fiches/{fiche_id} endpoint with a non-existent ID"""
    response = client.delete("/api/fiches/999")
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_run_fiche_thread(client: TestClient, sample_fiche: Fiche, db_session):
    """Test running an fiche via a thread"""
    # Create a thread for the fiche
    thread_data = {
        "title": "Test Thread for Run",
        "fiche_id": sample_fiche.id,
        "active": True,
        "memory_strategy": "buffer",
    }

    # Create the thread
    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 201
    thread = response.json()

    # Create a message for the thread
    message_data = {"role": "user", "content": "Hello, test assistant"}
    response = client.post(f"/api/threads/{thread['id']}/messages", json=message_data)
    assert response.status_code == 201

    # Run the thread with mocked FicheRunner
    with patch("zerg.managers.fiche_runner.FicheRunner") as mock_fiche_runner_class:
        mock_fiche_runner = MagicMock()
        mock_fiche_runner_class.return_value = mock_fiche_runner

        # Mock the run_thread method to return a list with one message
        mock_created_row = MagicMock()
        mock_created_row.role = "assistant"
        mock_created_row.content = "Test response"

        # Set up the async mock return value
        async def mock_run_thread(*args, **kwargs):
            return [mock_created_row]

        mock_fiche_runner.run_thread.side_effect = mock_run_thread

        # Run the thread
        response = client.post(f"/api/threads/{thread['id']}/run", json={"content": "Test message"})
        assert response.status_code == 202

        # Verify the fiche status in the database is still valid
        response = client.get(f"/api/fiches/{sample_fiche.id}")
        assert response.status_code == 200
        fetched_fiche = response.json()
        assert fetched_fiche["id"] == sample_fiche.id


def test_run_fiche_not_found(client: TestClient):
    """Test running a non-existent fiche via a thread"""
    # Create a thread for a non-existent fiche
    thread_data = {
        "title": "Test Thread for Non-existent Fiche",
        "fiche_id": 999,  # Assuming this ID doesn't exist
        "active": True,
        "memory_strategy": "buffer",
    }

    # Try to create the thread
    response = client.post("/api/threads", json=thread_data)
    assert response.status_code == 404
    assert "detail" in response.json()
    assert response.json()["detail"] == "Fiche not found"


def test_run_fiche_task(client: TestClient, sample_fiche: Fiche, db_session):
    """Test running an fiche's main task via the /api/fiches/{id}/task endpoint."""
    # Patch the new helper so the test is independent of FicheRunner logic
    with patch("zerg.services.task_runner.execute_fiche_task", autospec=True) as mock_exec:
        mock_thread = MagicMock(id=123)

        async def _fake_execute(db, fiche, thread_type="manual"):
            return mock_thread

        mock_exec.side_effect = _fake_execute

        response = client.post(f"/api/fiches/{sample_fiche.id}/task")

        assert response.status_code == 202
        data = response.json()
        assert data["thread_id"] == 123
