"""
Test conditional workflow execution to verify conditional node routing works correctly.
"""

from unittest.mock import patch

import pytest

from zerg.models.models import Workflow
from zerg.schemas.workflow import Position
from zerg.schemas.workflow import WorkflowData
from zerg.schemas.workflow import WorkflowEdge
from zerg.schemas.workflow import WorkflowNode
from zerg.services.workflow_engine import workflow_engine


def create_conditional_workflow_data(agent_id: int) -> WorkflowData:
    """Create a test workflow with conditional logic."""
    return WorkflowData(
        nodes=[
            # Tool node that generates a random number
            WorkflowNode(
                id="tool-1",
                type="tool",
                position=Position(x=100, y=100),
                config={"tool_name": "random_number", "static_params": {"min": 1, "max": 100}},
            ),
            # Conditional node that checks if number > 50
            WorkflowNode(
                id="conditional-1",
                type="conditional",
                position=Position(x=300, y=100),
                config={"condition": "${tool-1} > 50", "condition_type": "expression"},
            ),
            # Agent node for "high" branch (true)
            WorkflowNode(
                id="agent-high",
                type="agent",
                position=Position(x=500, y=50),
                config={"agent_id": agent_id, "message": "The number ${tool-1} is greater than 50!"},
            ),
            # Agent node for "low" branch (false)
            WorkflowNode(
                id="agent-low",
                type="agent",
                position=Position(x=500, y=150),
                config={"agent_id": agent_id, "message": "The number ${tool-1} is 50 or less."},
            ),
        ],
        edges=[
            # Tool -> Conditional
            WorkflowEdge(**{"from_node_id": "tool-1", "to_node_id": "conditional-1"}),
            # Conditional -> High branch (true)
            WorkflowEdge(**{"from_node_id": "conditional-1", "to_node_id": "agent-high", "config": {"branch": "true"}}),
            # Conditional -> Low branch (false)
            WorkflowEdge(**{"from_node_id": "conditional-1", "to_node_id": "agent-low", "config": {"branch": "false"}}),
        ],
    )


@pytest.mark.asyncio
async def test_conditional_workflow_high_branch(db, test_user, sample_agent):
    """Test conditional workflow routing to high branch when condition is true."""

    # Mock the random_number tool to return a high value (> 50)
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: 75  # Return 75 (> 50)

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "random_number" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner to avoid actual LLM calls
        async def mock_run_thread(db, thread):
            from datetime import datetime
            from datetime import timezone

            from zerg.models.models import ThreadMessage

            # Create a proper ThreadMessage object mock with sent_at field
            mock_msg = ThreadMessage(
                id=999,
                thread_id=thread.id,
                role="assistant",
                content="High branch executed",
                sent_at=datetime.now(timezone.utc),
                processed=True,
            )
            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow
            workflow_data = create_conditional_workflow_data(sample_agent.id)
            workflow = Workflow(
                owner_id=test_user.id,
                name="Test Conditional Workflow",
                description="Test conditional routing",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Verify execution completed successfully
            assert execution_id is not None

            # Check execution record
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution is not None
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Check node execution states
            from zerg.models.models import NodeExecutionState

            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            # Should have executed: tool-1, conditional-1, agent-high
            executed_nodes = {state.node_id for state in node_states}
            assert "tool-1" in executed_nodes
            assert "conditional-1" in executed_nodes
            assert "agent-high" in executed_nodes
            assert "agent-low" not in executed_nodes  # Should NOT execute low branch


@pytest.mark.asyncio
async def test_conditional_workflow_low_branch(db, test_user, sample_agent):
    """Test conditional workflow routing to low branch when condition is false."""

    # Mock the random_number tool to return a low value (<= 50)
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: 25  # Return 25 (<= 50)

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "random_number" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner to avoid actual LLM calls
        async def mock_run_thread(db, thread):
            from datetime import datetime
            from datetime import timezone

            from zerg.models.models import ThreadMessage

            # Create a proper ThreadMessage object mock with sent_at field
            mock_msg = ThreadMessage(
                id=998,
                thread_id=thread.id,
                role="assistant",
                content="Low branch executed",
                sent_at=datetime.now(timezone.utc),
                processed=True,
            )
            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow
            workflow_data = create_conditional_workflow_data(sample_agent.id)
            workflow = Workflow(
                owner_id=test_user.id,
                name="Test Conditional Workflow Low",
                description="Test conditional routing low branch",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Verify execution completed successfully
            assert execution_id is not None

            # Check execution record
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution is not None
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Check node execution states
            from zerg.models.models import NodeExecutionState

            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            # Should have executed: tool-1, conditional-1, agent-low
            executed_nodes = {state.node_id for state in node_states}
            assert "tool-1" in executed_nodes
            assert "conditional-1" in executed_nodes
            assert "agent-low" in executed_nodes
            assert "agent-high" not in executed_nodes  # Should NOT execute high branch


@pytest.mark.asyncio
async def test_conditional_node_variable_resolution(db, test_user, sample_agent):
    """Test that conditional nodes properly resolve variables from previous node outputs."""

    # Mock the tool to return a specific value we can test
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: {"result": 85, "status": "completed"}  # Return structured output

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "data_processor" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner
        async def mock_run_thread(db, thread):
            return [{"role": "assistant", "content": "Variable resolved correctly"}]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow with complex variable resolution
            workflow_data = WorkflowData(
                nodes=[
                    WorkflowNode(
                        id="tool-1",
                        type="tool",
                        position=Position(x=100, y=100),
                        config={"tool_name": "data_processor", "static_params": {"operation": "calculate"}},
                    ),
                    WorkflowNode(
                        id="conditional-1",
                        type="conditional",
                        position=Position(x=300, y=100),
                        config={
                            "condition": "${tool-1.result} >= 80",  # Access tool result field: 85 >= 80 = true
                            "condition_type": "expression",
                        },
                    ),
                    WorkflowNode(
                        id="agent-success",
                        type="agent",
                        position=Position(x=500, y=100),
                        config={
                            "agent_id": sample_agent.id,
                            "message": "Processing result ${tool-1.result} with phase ${tool-1.meta.phase} and result ${tool-1.meta.result}",
                        },
                    ),
                ],
                edges=[
                    WorkflowEdge(**{"from_node_id": "tool-1", "to_node_id": "conditional-1"}),
                    WorkflowEdge(
                        **{"from_node_id": "conditional-1", "to_node_id": "agent-success", "config": {"branch": "true"}}
                    ),
                ],
            )

            workflow = Workflow(
                owner_id=test_user.id,
                name="Test Variable Resolution",
                description="Test conditional with variable resolution",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Verify execution completed successfully
            assert execution_id is not None

            # Check that conditional node resolved variables correctly
            from zerg.models.models import NodeExecutionState

            conditional_state = (
                db.query(NodeExecutionState)
                .filter_by(workflow_execution_id=execution_id, node_id="conditional-1")
                .first()
            )

            assert conditional_state is not None
            assert conditional_state.phase == "finished"
            assert conditional_state.result == "success"
            assert conditional_state.output["value"]["result"] is True
            assert conditional_state.output["value"]["branch"] == "true"


if __name__ == "__main__":
    # Run tests manually for development
    import os
    import sys

    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    pytest.main([__file__, "-v"])
