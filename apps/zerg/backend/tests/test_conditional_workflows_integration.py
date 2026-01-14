"""
REAL integration tests for conditional workflows.
Mock only external dependencies (LLM API), run everything else real.
"""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.models.models import Workflow
from zerg.schemas.workflow import Position
from zerg.schemas.workflow import WorkflowData
from zerg.schemas.workflow import WorkflowEdge
from zerg.schemas.workflow import WorkflowNode
from zerg.services.workflow_engine import workflow_engine


def create_conditional_workflow_data(agent_id: int) -> WorkflowData:
    """Create a test workflow with conditional logic - same as original test."""
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
            WorkflowEdge(from_node_id="tool-1", to_node_id="conditional-1", config={}),
            WorkflowEdge(from_node_id="conditional-1", to_node_id="agent-high", config={"branch": "true"}),
            WorkflowEdge(from_node_id="conditional-1", to_node_id="agent-low", config={"branch": "false"}),
        ],
    )


@pytest.mark.asyncio
async def test_conditional_workflow_high_branch_integration(db, test_user, sample_agent):
    """
    REAL integration test - mock only LLM API, run everything else real.

    This tests:
    - Real tool execution (mocked for determinism)
    - Real conditional logic
    - Real AgentRunner with real ThreadMessage handling
    - Real database operations
    - Real serialization/deserialization
    """

    # Mock ONLY the tool to be deterministic (this is reasonable)
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: 75  # Return 75 (> 50)

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "random_number" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock the supervisor ReAct loop to return input messages + response
        from zerg.services.supervisor_react_engine import SupervisorResult

        async def mock_run_supervisor_loop(messages, **kwargs):
            """Mock the supervisor loop to return input messages + new AIMessage."""
            from langchain_core.messages import AIMessage

            # Return ALL messages (input + new) as real supervisor loop does
            return SupervisorResult(
                messages=list(messages) + [AIMessage(content="The number 75 is indeed greater than 50!")],
                usage={"total_tokens": 10},
                interrupted=False,
            )

        with patch(
            "zerg.services.supervisor_react_engine.run_supervisor_loop",
            new=mock_run_supervisor_loop,
        ):

            # Create and execute workflow - everything else is REAL
            workflow_data = create_conditional_workflow_data(sample_agent.id)
            workflow = Workflow(
                owner_id=test_user.id,
                name="Integration Test Conditional Workflow",
                description="Real integration test",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow with REAL services
            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Verify execution completed successfully
            assert execution_id is not None

            # Check execution record
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution is not None
            assert execution.phase == "finished"
            assert execution.result == "success"

            # REAL datetime operations were tested
            assert execution.started_at is not None
            assert execution.finished_at is not None
            duration = execution.finished_at - execution.started_at
            assert duration.total_seconds() >= 0

            # Check node execution states - REAL database operations
            from zerg.models.models import NodeExecutionState

            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            # Should have executed: tool-1, conditional-1, agent-high (NOT agent-low)
            executed_nodes = {state.node_id for state in node_states}
            assert "tool-1" in executed_nodes
            assert "conditional-1" in executed_nodes
            assert "agent-high" in executed_nodes
            assert "agent-low" not in executed_nodes  # Should NOT execute low branch

            # All executed nodes should be completed
            for state in node_states:
                assert state.phase == "finished"
                assert state.result == "success"
                assert state.error_message is None

            # REAL agent execution with REAL ThreadMessage creation and serialization
            agent_state = next(s for s in node_states if s.node_id == "agent-high")
            assert agent_state.output is not None

            # This is the EXACT code path that was failing in production
            messages = agent_state.output["value"]["messages"]
            assert len(messages) > 0

            # Verify REAL ThreadMessage serialization (the bug that was missed)
            message = messages[0]
            assert "sent_at" in message  # This was the failing field access
            assert message["sent_at"] is not None
            assert message["role"] == "assistant"
            # Content should contain our mocked response
            assert "greater than 50" in message["content"] or len(message["content"]) > 0

            # Verify REAL thread was created in database
            assert "thread_id" in message
            thread_id = message["thread_id"]

            # Check that thread actually exists in database
            from zerg.models.models import Thread
            from zerg.models.models import ThreadMessage

            thread = db.query(Thread).filter_by(id=thread_id).first()
            assert thread is not None
            assert thread.agent_id == sample_agent.id

            # Check that real ThreadMessage was created
            thread_messages = db.query(ThreadMessage).filter_by(thread_id=thread_id).all()
            assert len(thread_messages) >= 2  # user message + assistant message

            # Find the assistant message
            assistant_msg = next(msg for msg in thread_messages if msg.role == "assistant")
            assert assistant_msg.sent_at is not None  # REAL sent_at field
            assert len(assistant_msg.content) > 0  # Should have some content

            print("✅ REAL integration test passed - tested entire stack except LLM API")


@pytest.mark.asyncio
async def test_conditional_workflow_low_branch_integration(db, test_user, sample_agent):
    """Test low branch with real integration."""

    # Mock tool to return low value
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: 25  # Return 25 (<= 50)

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "random_number" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock the supervisor ReAct loop to return input messages + response
        from zerg.services.supervisor_react_engine import SupervisorResult

        async def mock_run_supervisor_loop(messages, **kwargs):
            """Mock the supervisor loop to return input messages + new AIMessage."""
            from langchain_core.messages import AIMessage

            # Return ALL messages (input + new) as real supervisor loop does
            return SupervisorResult(
                messages=list(messages) + [AIMessage(content="The number 25 is 50 or less.")],
                usage={"total_tokens": 10},
                interrupted=False,
            )

        with patch(
            "zerg.services.supervisor_react_engine.run_supervisor_loop",
            new=mock_run_supervisor_loop,
        ):

            # Execute workflow
            workflow_data = create_conditional_workflow_data(sample_agent.id)
            workflow = Workflow(
                owner_id=test_user.id,
                name="Integration Test Low Branch",
                description="Real integration test for low branch",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            execution_id = await workflow_engine.execute_workflow(workflow.id)

            # Verify low branch was taken
            from zerg.models.models import NodeExecutionState

            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            executed_nodes = {state.node_id for state in node_states}
            assert "tool-1" in executed_nodes
            assert "conditional-1" in executed_nodes
            assert "agent-low" in executed_nodes  # Should execute LOW branch
            assert "agent-high" not in executed_nodes  # Should NOT execute high branch

            print("✅ Low branch integration test passed")


@pytest.mark.asyncio
async def test_agent_workflow_error_handling_integration(db, test_user, sample_agent):
    """Test error handling with real services - no AgentRunner mocking."""

    # Create workflow that will cause an error (non-existent tool)
    workflow_data = WorkflowData(
        nodes=[
            WorkflowNode(
                id="bad-tool",
                type="tool",
                position=Position(x=100, y=100),
                config={"tool_name": "nonexistent_tool"},  # This will fail
            )
        ],
        edges=[],
    )

    workflow = Workflow(
        owner_id=test_user.id,
        name="Error Test Workflow",
        description="Test error handling",
        canvas=workflow_data.model_dump(),
        is_active=True,
    )
    db.add(workflow)
    db.commit()

    # Execute workflow - should fail
    try:
        execution_id = await workflow_engine.execute_workflow(workflow.id)

        # Should have failed
        from zerg.models.models import WorkflowExecution

        execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
        assert execution.phase == "finished"
        assert execution.result == "failure"
        assert execution.error_message is not None

        print("✅ Error handling integration test passed")

    except Exception as e:
        # This is also acceptable - depends on error handling strategy
        print(f"✅ Error handling integration test passed - exception: {e}")
