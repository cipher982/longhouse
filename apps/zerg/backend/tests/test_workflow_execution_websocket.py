"""Test WebSocket workflow execution subscription behavior."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.main import app


def test_workflow_execution_subscription_snapshot(db: Session):
    """Test that subscribing to a finished workflow execution returns a snapshot."""

    # Create a finished workflow execution
    workflow = crud.create_workflow(db, owner_id=1, name="Test Workflow", description="Test", canvas={})
    execution = crud.create_workflow_execution(
        db, workflow_id=workflow.id, phase="finished", result="success", triggered_by="manual"
    )
    execution.error_message = None
    db.commit()

    with TestClient(app) as client:
        # WebSocket endpoint is exposed under the API prefix ("/api/ws").
        # Using the fully-qualified path avoids accidental breakage if the
        # router is mounted under a prefix in the future.
        with client.websocket_connect("/api/ws") as websocket:
            # Subscribe to the workflow execution
            subscribe_msg = {
                "type": "subscribe",
                "topics": [f"workflow_execution:{execution.id}"],
                "message_id": "test-1",
            }
            websocket.send_json(subscribe_msg)

            # Snapshot is sent immediately for finished executions - receive it directly
            snapshot = websocket.receive_json()

            # Verify it's an execution_finished message
            assert (
                snapshot.get("type") == "execution_finished"
                or snapshot.get("data", {}).get("type") == "execution_finished"
            ), f"Expected execution_finished snapshot but got: {snapshot}"

            # Verify the message content
            if "data" in snapshot:
                data = snapshot["data"]
            else:
                data = snapshot

            assert data.get("execution_id") == execution.id or data.get("data", {}).get("execution_id") == execution.id
