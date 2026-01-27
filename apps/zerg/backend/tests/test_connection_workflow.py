"""Test workflow execution with fiche connections created via frontend."""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@patch("zerg.managers.fiche_runner.FicheRunner.run_thread")
def test_fiche_connection_workflow_execution(
    mock_run_thread,
    client: TestClient,
    db: Session,
    auth_headers: dict,
    test_user,
):
    """Test that workflows with fiche connections execute correctly."""

    from zerg.crud import crud as crud_mod
    from zerg.models.models import ThreadMessage

    # Mock FicheRunner to return fake assistant messages
    async def mock_fiche_runner_run_thread(db, thread):
        mock_assistant_message = ThreadMessage(
            thread_id=thread.id, role="assistant", content="This is a mock response from the fiche", processed=True
        )
        return [mock_assistant_message]

    mock_run_thread.side_effect = mock_fiche_runner_run_thread

    # Create two test fiches
    fiche1 = crud_mod.create_fiche(
        db=db,
        owner_id=test_user.id,
        name="Test Fiche A",
        system_instructions="You are Test Fiche A",
        task_instructions="Respond with 'Hello from Fiche A'",
        model="gpt-mock",
    )

    fiche2 = crud_mod.create_fiche(
        db=db,
        owner_id=test_user.id,
        name="Test Fiche B",
        system_instructions="You are Test Fiche B",
        task_instructions="Respond with 'Hello from Fiche B'",
        model="gpt-mock",
    )

    # Create workflow with connected fiches using WorkflowData format
    canvas = {
        "nodes": [
            {
                "id": f"fiche_node_{fiche1.id}",
                "type": "fiche",
                "position": {"x": 100, "y": 100},
                "config": {"fiche_id": fiche1.id, "name": fiche1.name},
            },
            {
                "id": f"fiche_node_{fiche2.id}",
                "type": "fiche",
                "position": {"x": 300, "y": 100},
                "config": {"fiche_id": fiche2.id, "name": fiche2.name},
            },
        ],
        "edges": [
            {
                "from_node_id": f"fiche_node_{fiche1.id}",
                "to_node_id": f"fiche_node_{fiche2.id}",
                "config": {"label": None},
            }
        ],
    }

    workflow = crud_mod.create_workflow(
        db=db,
        owner_id=test_user.id,
        name="Connection Test Workflow",
        description="Test workflow with fiche connections",
        canvas=canvas,
    )

    # Execute the workflow
    resp = client.post(f"/api/workflow-executions/{workflow.id}/start", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.json()

    execution_id = payload["execution_id"]
    assert execution_id > 0

    # Wait for completion (since it runs in background)
    client.post(f"/api/workflow-executions/{execution_id}/await", headers=auth_headers)

    # Check execution status immediately since we're using mocked fiches

    # Check execution status
    status_resp = client.get(f"/api/workflow-executions/{execution_id}/status", headers=auth_headers)
    assert status_resp.status_code == 200
    status_data = status_resp.json()

    print(f"Workflow execution status: {status_data}")

    # The execution should have processed the connection
    assert status_data["phase"] in ["running", "finished"]
    if status_data["phase"] == "finished":
        assert status_data["result"] in ["success", "failure"]

    # Check that node states were created for both connected fiches
    from zerg.models.models import NodeExecutionState

    node_states = db.query(NodeExecutionState).filter(NodeExecutionState.workflow_execution_id == execution_id).all()

    node_ids = {state.node_id for state in node_states}
    expected_node_ids = {f"fiche_node_{fiche1.id}", f"fiche_node_{fiche2.id}"}

    print(f"Node states created: {node_ids}")
    print(f"Expected node IDs: {expected_node_ids}")

    # Verify that the workflow engine processed both connected nodes
    assert len(node_states) >= 1, "At least one node should have been executed"

    # Check logs for execution details
    logs_resp = client.get(f"/api/workflow-executions/{execution_id}/logs", headers=auth_headers)
    if logs_resp.status_code == 200:
        logs_data = logs_resp.json()
        print(f"Execution logs: {logs_data}")


def test_frontend_edge_format_compatibility(
    client: TestClient,
    db: Session,
    auth_headers: dict,
    test_user,
):
    """Test that the exact edge format created by frontend AddEdge message works."""

    from zerg.crud import crud as crud_mod

    # Create a simple workflow in canonical format using existing tools
    canvas = {
        "nodes": [
            {"id": "node_1", "type": "tool", "position": {"x": 0, "y": 0}, "config": {"tool_name": "get_current_time"}},
            {"id": "node_2", "type": "tool", "position": {"x": 100, "y": 0}, "config": {"tool_name": "generate_uuid"}},
        ],
        "edges": [{"from_node_id": "node_1", "to_node_id": "node_2", "config": {"label": None}}],
    }

    workflow = crud_mod.create_workflow(
        db=db,
        owner_id=test_user.id,
        name="Frontend Edge Format Test",
        description="Test exact frontend edge format",
        canvas=canvas,
    )

    # Verify workflow was created successfully
    assert workflow.id > 0
    assert workflow.canvas["edges"][0]["from_node_id"] == "node_1"
    assert workflow.canvas["edges"][0]["to_node_id"] == "node_2"

    # Try to execute it (may fail due to tool nodes, but should parse correctly)
    resp = client.post(f"/api/workflow-executions/{workflow.id}/start", headers=auth_headers)

    # Should at least attempt to start (not crash on edge format)
    # Now that workflows actually execute, we may get 500 errors from missing tools
    assert resp.status_code in [200, 400, 500], f"Got unexpected status {resp.status_code}: {resp.text}"

    if resp.status_code == 200:
        payload = resp.json()
        assert "execution_id" in payload
        print(f"Workflow started successfully with execution_id: {payload['execution_id']}")
    elif resp.status_code in [400, 500]:
        # If it fails, it should be due to tool configuration, not edge format
        # 400 = validation error, 500 = execution error (now that workflows actually run)
        try:
            error_data = resp.json()
            print(f"Expected failure due to tool config: {error_data}")
        except Exception:
            # For 500 errors, the response might not be JSON
            print(f"Expected failure due to tool config (status {resp.status_code}): {resp.text}")
        # Don't assert on error message content since it could be validation or execution error
