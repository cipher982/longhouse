from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.models.models import User

# Test data
TEST_WORKFLOW_NAME = "Test Workflow"
TEST_WORKFLOW_DESCRIPTION = "This is a test workflow."
TEST_CANVAS_DATA = {"nodes": [], "edges": []}


def create_test_workflow(db: Session, owner_id: int):
    """Helper function to create a workflow for testing."""
    return crud.create_workflow(
        db=db,
        owner_id=owner_id,
        name=TEST_WORKFLOW_NAME,
        description=TEST_WORKFLOW_DESCRIPTION,
        canvas=TEST_CANVAS_DATA,
    )


def test_create_workflow_success(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test successful creation of a workflow."""
    payload = {
        "name": "New Workflow",
        "description": "A fresh new workflow.",
        "canvas": {
            "nodes": [
                {
                    "id": "node1",
                    "type": "trigger",
                    "position": {"x": 0, "y": 0},
                    "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}},
                }
            ],
            "edges": [],
        },
    }
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code in (200, 201)
    data = response.json()
    assert data["name"] == payload["name"]
    assert data["description"] == payload["description"]
    # After cleanup, API returns canonical format instead of frontend format
    # API returns canonical format with typed trigger meta preserved
    expected_node = {
        "id": "node1",
        "type": "trigger",
        "position": {"x": 0.0, "y": 0.0},
        "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}},
    }
    assert data["canvas"]["nodes"][0] == expected_node
    assert data["owner_id"] == test_user.id
    assert "id" in data


def test_create_workflow_unauthenticated(unauthenticated_client: TestClient, monkeypatch):
    """Test that unauthenticated users cannot create workflows."""
    # Temporarily enable auth for this test
    monkeypatch.setattr("zerg.dependencies.auth.AUTH_DISABLED", False)

    payload = {
        "name": "Unauthorized Workflow",
        "description": "This should not be created.",
        "canvas": {"nodes": [], "edges": []},
    }
    response = unauthenticated_client.post("/api/workflows/", json=payload)
    assert response.status_code == 401

    # Restore the original value
    monkeypatch.setattr("zerg.dependencies.auth.AUTH_DISABLED", True)


def test_create_workflow_missing_fields(client: TestClient, auth_headers: dict):
    """Test that creating a workflow with missing required fields fails."""
    # Missing 'name'
    payload = {"description": "This is a test."}
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code == 422  # Unprocessable Entity

    # Missing 'description' is allowed; should create successfully
    payload = {"name": "Test Workflow"}
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code in (200, 201)


def test_read_workflows_success(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test reading workflows owned by the current user."""
    # Create a workflow for the user
    create_test_workflow(db, test_user.id)

    response = client.get("/api/workflows/", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    assert data[0]["name"] == TEST_WORKFLOW_NAME
    assert data[0]["owner_id"] == test_user.id


def test_read_workflows_isolation(
    client: TestClient, test_user: User, other_user: User, db: Session, auth_headers: dict
):
    """Test that a user cannot see workflows owned by another user."""
    # Create a workflow for the other user
    create_test_workflow(db, other_user.id)

    # Authenticate as the primary test user
    response = client.get("/api/workflows/", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # The user should not see the other user's workflow
    assert all(item["owner_id"] != other_user.id for item in data)


def test_rename_workflow_success(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test successfully renaming a workflow."""
    wf = create_test_workflow(db, test_user.id)
    payload = {"name": "Updated Workflow Name", "description": "Updated description."}
    response = client.patch(f"/api/workflows/{wf.id}", headers=auth_headers, json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == payload["name"]
    assert data["description"] == payload["description"]
    assert data["id"] == wf.id


def test_rename_workflow_not_found(client: TestClient, test_user: User, auth_headers: dict):
    """Test renaming a workflow that does not exist."""
    payload = {"name": "Doesn't Matter", "description": "Doesn't Matter"}
    response = client.patch("/api/workflows/99999", headers=auth_headers, json=payload)
    assert response.status_code == 404


def test_rename_workflow_unauthorized(
    client: TestClient, test_user: User, other_user: User, db: Session, auth_headers: dict
):
    """Test that a user cannot rename a workflow they do not own."""
    wf = create_test_workflow(db, other_user.id)
    payload = {"name": "Unauthorized Update", "description": "This should fail."}
    response = client.patch(f"/api/workflows/{wf.id}", headers=auth_headers, json=payload)
    assert response.status_code == 404


def test_delete_workflow_success(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test successfully soft-deleting a workflow."""
    wf = create_test_workflow(db, test_user.id)
    response = client.delete(f"/api/workflows/{wf.id}", headers=auth_headers)
    assert response.status_code == 204

    # Verify the workflow is marked as inactive
    db.refresh(wf)
    assert wf.is_active is False


def test_delete_workflow_not_found(client: TestClient, test_user: User, auth_headers: dict):
    """Test deleting a workflow that does not exist."""
    response = client.delete("/api/workflows/99999", headers=auth_headers)
    assert response.status_code == 404


def test_delete_workflow_unauthorized(
    client: TestClient, test_user: User, other_user: User, db: Session, auth_headers: dict
):
    """Test that a user cannot delete a workflow they do not own."""
    wf = create_test_workflow(db, other_user.id)
    response = client.delete(f"/api/workflows/{wf.id}", headers=auth_headers)
    assert response.status_code == 404


def test_soft_delete_and_recreate(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test that a soft-deleted workflow can be recreated with the same name."""
    # 1. Create and delete a workflow
    wf = create_test_workflow(db, test_user.id)
    response = client.delete(f"/api/workflows/{wf.id}", headers=auth_headers)
    assert response.status_code == 204

    # 2. Verify it's inactive
    db.refresh(wf)
    assert wf.is_active is False

    # 3. Recreate a workflow with the same name
    payload = {
        "name": TEST_WORKFLOW_NAME,
        "description": "A new workflow with an old name.",
        "canvas": {"nodes": [], "edges": []},
    }
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code in (200, 201)
    new_wf_data = response.json()
    assert new_wf_data["name"] == TEST_WORKFLOW_NAME
    assert new_wf_data["id"] != wf.id


def test_duplicate_workflow_name_fails(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """Test that creating a workflow with a duplicate name fails."""
    # Create an initial workflow
    create_test_workflow(db, test_user.id)

    # Attempt to create another with the same name
    payload = {
        "name": TEST_WORKFLOW_NAME,
        "description": "This should fail.",
        "canvas": {"nodes": [], "edges": []},
    }
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code == 409  # Conflict


def test_create_workflow_with_large_canvas(client: TestClient, test_user: User, auth_headers: dict):
    """Test creating a workflow with a large canvas payload."""
    large_canvas = {
        "nodes": [
            {
                "id": f"node_{i}",
                "type": "trigger",
                "position": {"x": 0, "y": 0},
                "config": {"trigger": {"type": "email", "config": {"enabled": True, "params": {}, "filters": []}}},
            }
            for i in range(1000)
        ],
        "edges": [{"from_node_id": f"node_{i}", "to_node_id": f"node_{i + 1}", "config": {}} for i in range(999)],
    }
    payload = {
        "name": "Large Canvas Workflow",
        "description": "A workflow with a very large canvas.",
        "canvas": large_canvas,
    }
    response = client.post("/api/workflows/", headers=auth_headers, json=payload)
    assert response.status_code in (200, 201), f"Response: {response.status_code} - {response.json()}"
    data = response.json()
    assert data["name"] == payload["name"]
    assert len(data["canvas"]["nodes"]) == 1000


def test_schema_evolution_robustness(client: TestClient, test_user: User, db: Session, auth_headers: dict):
    """
    Test that the API can handle records with missing fields.

    This simulates a scenario where a field was removed from the model,
    but old data still exists in the database.
    """
    from sqlalchemy import text

    from zerg.models.models import Workflow

    # 1. Create a standard workflow
    wf = create_test_workflow(db, test_user.id)

    # 2. Manually alter the data in the DB to simulate an old schema
    #    For this test, let's pretend 'description' was removed.
    db.execute(text(f"UPDATE {Workflow.__tablename__} SET description = NULL WHERE id = {wf.id}"))
    db.commit()

    # Force refresh from database to avoid stale cached objects
    db.expire_all()

    # 3. Fetch the workflows and ensure it doesn't crash
    response = client.get("/api/workflows/", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    # 4. Find our modified workflow and check that description is None
    found = False
    for item in data:
        if item["id"] == wf.id:
            assert item["description"] is None
            found = True
            break
    assert found, "Modified workflow not found in API response"
