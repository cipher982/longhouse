"""
HTTP API integration tests for workflow execution.

These tests prevent the critical bug where the HTTP API layer used different
parameter names than the internal workflow execution system. Specifically,
this ensures that workflow creation endpoints use the correct phase/result
architecture instead of legacy status parameters.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zerg.main import app
from zerg.models.models import NodeExecutionState
from zerg.models.models import WorkflowExecution


@pytest.mark.skip(
    reason="TestClient doesn't handle async background tasks properly. See test_workflow_direct_execution.py for validation of core functionality."
)
@pytest.mark.asyncio
async def test_full_workflow_http_execution(db, test_user, sample_agent, auth_headers):
    """Test complete HTTP API workflow execution flow to prevent parameter mismatches."""

    client = TestClient(app)

    # Mock tool for testing
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: {"result": "test_output"}

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "test_tool" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner
        async def mock_run_thread(db, thread):
            mock_msg = type("MockMessage", (), {})()
            mock_msg.id = 1
            mock_msg.role = "assistant"
            mock_msg.content = "Test response"
            mock_msg.timestamp = None
            mock_msg.thread_id = thread.id
            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # 1. POST /api/workflows (create workflow)
            workflow_payload = {
                "name": "HTTP Integration Test Workflow",
                "description": "Test workflow for HTTP API integration",
                "canvas": {
                    "nodes": [
                        {
                            "id": "trigger-1",
                            "type": "trigger",
                            "position": {"x": 50, "y": 100},
                            "config": {
                                "trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}
                            },
                        },
                        {
                            "id": "tool-1",
                            "type": "tool",
                            "position": {"x": 200, "y": 100},
                            "config": {"tool_name": "test_tool", "static_params": {"input": "test_data"}},
                        },
                        {
                            "id": "agent-1",
                            "type": "agent",
                            "position": {"x": 350, "y": 100},
                            "config": {"agent_id": sample_agent.id, "message": "Process: ${tool-1.value.result}"},
                        },
                    ],
                    "edges": [
                        {"from_node_id": "trigger-1", "to_node_id": "tool-1"},
                        {"from_node_id": "tool-1", "to_node_id": "agent-1"},
                    ],
                },
            }

            # Create workflow via HTTP API
            response = client.post("/api/workflows", json=workflow_payload, headers=auth_headers)
            assert response.status_code in (200, 201)
            workflow_data = response.json()
            workflow_id = workflow_data["id"]

            # 2. PATCH /api/workflows/{id}/canvas (already set in creation)
            # No additional canvas update needed for this test

            # 3. POST /api/workflow-executions/by-workflow/{workflow_id}/reserve
            response = client.post(f"/api/workflow-executions/by-workflow/{workflow_id}/reserve", headers=auth_headers)
            assert response.status_code == 200
            reserve_data = response.json()
            execution_id = reserve_data["execution_id"]

            # Verify the response format
            assert "execution_id" in reserve_data
            assert isinstance(execution_id, int)

            # 4. POST /api/workflow-executions/executions/{execution_id}/start
            response = client.post(f"/api/workflow-executions/executions/{execution_id}/start", headers=auth_headers)
            assert response.status_code == 200
            start_data = response.json()

            # Verify the response format
            assert "execution_id" in start_data
            assert start_data["execution_id"] == execution_id

            # Give the background task a moment to start
            import asyncio

            await asyncio.sleep(0.1)

            # 5. Use /await endpoint to wait for completion
            response = client.post(f"/api/workflow-executions/{execution_id}/await?timeout=10.0", headers=auth_headers)
            assert response.status_code == 200
            await_data = response.json()

            assert await_data["completed"] is True
            assert await_data["phase"] == "finished"
            assert await_data["result"] == "success"

            # Verify all nodes executed with phase/result
            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            executed_nodes = {state.node_id: state for state in node_states}
            expected_nodes = ["trigger-1", "tool-1", "agent-1"]

            for node_id in expected_nodes:
                assert node_id in executed_nodes, f"Node {node_id} should have executed"
                state = executed_nodes[node_id]

                # Critical: Verify phase/result architecture, not legacy status
                assert state.phase == "finished", f"Node {node_id} should have phase=finished"
                assert state.result == "success", f"Node {node_id} should have result=success"

                # Verify envelope format in output
                assert state.output is not None, f"Node {node_id} should have output"
                assert "value" in state.output, f"Node {node_id} should have envelope format"
                assert "meta" in state.output, f"Node {node_id} should have envelope format"

                # Critical: Verify envelope meta uses phase/result, not status
                meta = state.output["meta"]
                assert meta["phase"] == "finished", f"Node {node_id} envelope meta should use phase"
                assert meta["result"] == "success", f"Node {node_id} envelope meta should use result"
                assert "status" not in meta, f"Node {node_id} envelope meta should not contain legacy status"


@pytest.mark.asyncio
async def test_workflow_execution_parameter_consistency(db, test_user, sample_agent, auth_headers):
    """Test that HTTP endpoints and internal execution use consistent parameter names."""

    client = TestClient(app)

    # Create a minimal workflow
    workflow_payload = {
        "name": "Parameter Consistency Test",
        "description": "Test parameter name consistency",
        "canvas": {
            "nodes": [
                {
                    "id": "trigger-1",
                    "type": "trigger",
                    "position": {"x": 50, "y": 100},
                    "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}},
                }
            ],
            "edges": [],
        },
    }

    response = client.post("/api/workflows", json=workflow_payload, headers=auth_headers)
    assert response.status_code in (200, 201)
    workflow_id = response.json()["id"]

    # Test the reserve endpoint that previously had the bug
    response = client.post(f"/api/workflow-executions/by-workflow/{workflow_id}/reserve", headers=auth_headers)
    assert response.status_code == 200, f"Reserve endpoint failed: {response.text}"

    execution_id = response.json()["execution_id"]

    # Verify the execution was created with correct phase, not status
    execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
    assert execution is not None
    assert execution.phase == "waiting", "Execution should start in waiting phase"
    assert execution.result is None, "Execution should not have result initially"

    # The critical test: start the execution (this was the main bug - routing ambiguity)
    response = client.post(f"/api/workflow-executions/executions/{execution_id}/start", headers=auth_headers)
    assert response.status_code == 200, f"Start endpoint failed: {response.text}"

    # Verify the routing works correctly - execution should be marked as running
    import asyncio

    await asyncio.sleep(0.1)  # Brief wait for execution to start

    db.refresh(execution)
    assert execution.phase == "running", f"Execution should be running after start, got {execution.phase}"

    # The key success: we're operating on the SAME execution ID (not creating a new one)
    # This proves the routing ambiguity is fixed


@pytest.mark.skip(
    reason="TestClient doesn't handle async background tasks properly. See test_workflow_direct_execution.py for validation of core functionality."
)
@pytest.mark.asyncio
async def test_workflow_execution_error_handling(db, test_user, auth_headers):
    """Test that error cases also use phase/result architecture."""

    client = TestClient(app)

    # Create workflow with invalid tool (will cause error)
    workflow_payload = {
        "name": "Error Handling Test",
        "description": "Test error handling with phase/result",
        "canvas": {
            "nodes": [
                {
                    "id": "trigger-1",
                    "type": "trigger",
                    "position": {"x": 50, "y": 100},
                    "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}},
                },
                {
                    "id": "tool-invalid",
                    "type": "tool",
                    "position": {"x": 200, "y": 100},
                    "config": {
                        "tool_name": "nonexistent_tool",  # This will cause an error
                        "static_params": {},
                    },
                },
            ],
            "edges": [{"from_node_id": "trigger-1", "to_node_id": "tool-invalid"}],
        },
    }

    response = client.post("/api/workflows", json=workflow_payload, headers=auth_headers)
    assert response.status_code == 200
    workflow_id = response.json()["id"]

    # Execute workflow that will fail
    response = client.post(f"/api/workflow-executions/by-workflow/{workflow_id}/reserve", headers=auth_headers)
    assert response.status_code == 200
    execution_id = response.json()["execution_id"]

    response = client.post(f"/api/workflow-executions/executions/{execution_id}/start", headers=auth_headers)
    assert response.status_code == 200

    # Wait for async execution to complete using /await endpoint
    response = client.post(f"/api/workflow-executions/{execution_id}/await?timeout=5.0", headers=auth_headers)
    assert response.status_code == 200
    await_data = response.json()

    assert await_data["completed"] is True
    assert await_data["phase"] == "finished"
    assert await_data["result"] == "failure"  # Should be failure, not legacy "failed"

    # Verify failed node also uses phase/result
    failed_node = (
        db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id, node_id="tool-invalid").first()
    )

    assert failed_node is not None
    assert failed_node.phase == "finished"
    assert failed_node.result == "failure"

    # Verify envelope format for error
    assert failed_node.output is not None
    meta = failed_node.output["meta"]
    assert meta["phase"] == "finished"
    assert meta["result"] == "failure"
    assert "error_message" in meta
    assert "status" not in meta, "Error envelope should not contain legacy status"
