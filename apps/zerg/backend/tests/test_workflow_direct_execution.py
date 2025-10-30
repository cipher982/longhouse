"""
Direct workflow execution tests to isolate the hanging issue.
"""

from unittest.mock import patch

import pytest

from zerg.models.models import NodeExecutionState
from zerg.models.models import WorkflowExecution
from zerg.services.workflow_engine import workflow_engine


@pytest.mark.asyncio
async def test_direct_workflow_execution(db, test_user, sample_agent):
    """Test workflow execution directly without HTTP layer."""

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
            mock_msg.sent_at = None
            mock_msg.thread_id = thread.id
            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow
            from zerg.models.models import Workflow

            workflow_canvas = {
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
            }

            workflow = Workflow(
                name="Direct Test Workflow",
                description="Test workflow for direct execution",
                owner_id=test_user.id,
                canvas=workflow_canvas,
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow directly
            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Check execution completed
            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution is not None
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Check all nodes executed
            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()
            assert len(node_states) == 3

            for state in node_states:
                assert state.phase == "finished"
                assert state.result == "success"
