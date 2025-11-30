"""
Integration tests for envelope format across all node types.

Comprehensive testing of the envelope format system to ensure:
1. All node types produce correct envelope outputs
2. Variable resolution works with envelope format
3. Backward compatibility is maintained
4. Workflow engine properly handles envelope routing
"""

from unittest.mock import patch

import pytest

from zerg.models.models import Workflow
from zerg.schemas.node_output import is_envelope_format
from zerg.schemas.workflow import Position
from zerg.schemas.workflow import WorkflowData
from zerg.schemas.workflow import WorkflowEdge
from zerg.schemas.workflow import WorkflowNode
from zerg.services.workflow_engine import workflow_engine


@pytest.mark.asyncio
async def test_envelope_format_all_node_types(db, test_user, sample_agent):
    """Test that all node types produce proper envelope format outputs."""

    # Mock tool for testing
    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: {"score": 95, "grade": "A"}

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "grading_tool" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner
        async def mock_run_thread(db, thread):
            # Create mock objects that look like ThreadMessage models
            mock_msg1 = type("MockMessage", (), {})()
            mock_msg1.id = 1
            mock_msg1.role = "assistant"
            mock_msg1.content = "Analysis complete. Grade is excellent."
            mock_msg1.sent_at = None
            mock_msg1.thread_id = thread.id

            mock_msg2 = type("MockMessage", (), {})()
            mock_msg2.id = 2
            mock_msg2.role = "assistant"
            mock_msg2.content = "Recommendation: Student performed very well."
            mock_msg2.sent_at = None
            mock_msg2.thread_id = thread.id

            return [mock_msg1, mock_msg2]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow with all node types
            workflow_data = WorkflowData(
                nodes=[
                    # Trigger node (start)
                    WorkflowNode(
                        id="trigger-1",
                        type="trigger",
                        position=Position(x=50, y=100),
                        config={
                            "trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}
                        },
                    ),
                    # Tool node
                    WorkflowNode(
                        id="tool-1",
                        type="tool",
                        position=Position(x=200, y=100),
                        config={"tool_name": "grading_tool", "static_params": {"assignment": "final_exam"}},
                    ),
                    # Conditional node
                    WorkflowNode(
                        id="conditional-1",
                        type="conditional",
                        position=Position(x=350, y=100),
                        config={"condition": "${tool-1.value.score} >= 90", "condition_type": "expression"},
                    ),
                    # Agent node (high score branch)
                    WorkflowNode(
                        id="agent-1",
                        type="agent",
                        position=Position(x=500, y=50),
                        config={
                            "agent_id": sample_agent.id,
                            "message": "Analyze this excellent score: ${tool-1.value.score} (${tool-1.value.grade})",
                        },
                    ),
                    # Agent node (low score branch)
                    WorkflowNode(
                        id="agent-2",
                        type="agent",
                        position=Position(x=500, y=150),
                        config={"agent_id": sample_agent.id, "message": "Analyze this score: ${tool-1.value.score}"},
                    ),
                ],
                edges=[
                    WorkflowEdge(**{"from_node_id": "trigger-1", "to_node_id": "tool-1"}),
                    WorkflowEdge(**{"from_node_id": "tool-1", "to_node_id": "conditional-1"}),
                    WorkflowEdge(
                        **{"from_node_id": "conditional-1", "to_node_id": "agent-1", "config": {"branch": "true"}}
                    ),
                    WorkflowEdge(
                        **{"from_node_id": "conditional-1", "to_node_id": "agent-2", "config": {"branch": "false"}}
                    ),
                ],
            )

            workflow = Workflow(
                owner_id=test_user.id,
                name="Envelope Format Integration Test",
                description="Test all node types with envelope format",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)
            assert execution_id is not None

            # Verify execution success
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Check all node execution states and verify envelope format
            from zerg.models.models import NodeExecutionState

            node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

            executed_nodes = {state.node_id: state for state in node_states}

            # Verify all nodes completed successfully and have envelope format
            expected_nodes = ["trigger-1", "tool-1", "conditional-1", "agent-1"]
            for node_id in expected_nodes:
                assert node_id in executed_nodes, f"Node {node_id} should have executed"
                state = executed_nodes[node_id]
                assert state.phase == "finished", f"Node {node_id} should be finished"
                assert state.result == "success", f"Node {node_id} should be successful"
                assert is_envelope_format(state.output), f"Node {node_id} should have envelope format"
                assert "value" in state.output, f"Node {node_id} should have 'value' field"
                assert "meta" in state.output, f"Node {node_id} should have 'meta' field"
                assert state.output["meta"]["phase"] == "finished", f"Node {node_id} meta should show finished"
                assert state.output["meta"]["result"] == "success", f"Node {node_id} meta should show success"

            # Verify specific envelope content
            trigger_state = executed_nodes["trigger-1"]
            assert trigger_state.output["value"]["triggered"]
            assert trigger_state.output["meta"]["node_type"] == "trigger"

            tool_state = executed_nodes["tool-1"]
            assert tool_state.output["value"]["score"] == 95
            assert tool_state.output["value"]["grade"] == "A"
            assert tool_state.output["meta"]["node_type"] == "tool"

            conditional_state = executed_nodes["conditional-1"]
            assert conditional_state.output["value"]["result"]  # 95 >= 90
            assert conditional_state.output["value"]["branch"] == "true"
            assert conditional_state.output["meta"]["node_type"] == "conditional"

            agent_state = executed_nodes["agent-1"]
            assert "messages" in agent_state.output["value"]
            assert agent_state.output["value"]["messages_created"] == 2
            assert agent_state.output["meta"]["node_type"] == "agent"

            # Verify high branch executed, low branch did not
            assert "agent-2" not in executed_nodes


@pytest.mark.asyncio
async def test_envelope_format_variable_resolution_edge_cases(db, test_user, sample_agent):
    """Test envelope format variable resolution with complex scenarios."""

    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        # Tool returns nested data structure
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: {
            "analysis": {
                "metrics": {"accuracy": 0.95, "precision": 0.87},
                "summary": "High performance model",
                "tags": ["production-ready", "validated"],
            },
            "metadata": {"version": "2.1.0", "timestamp": "2024-01-15T10:30:00Z"},
        }

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "ml_analyzer" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner
        async def mock_run_thread(db, thread):
            # Create mock objects that look like ThreadMessage models
            mock_msg = type("MockMessage", (), {})()
            mock_msg.id = 1
            mock_msg.role = "assistant"
            mock_msg.content = "Analysis processed successfully"
            mock_msg.sent_at = None
            mock_msg.thread_id = thread.id

            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow with complex variable resolution
            workflow_data = WorkflowData(
                nodes=[
                    WorkflowNode(
                        id="tool-complex",
                        type="tool",
                        position=Position(x=100, y=100),
                        config={"tool_name": "ml_analyzer", "static_params": {"model_id": "bert-v2"}},
                    ),
                    WorkflowNode(
                        id="conditional-nested",
                        type="conditional",
                        position=Position(x=300, y=100),
                        config={
                            # Test deeply nested access: tool result -> analysis -> metrics -> accuracy
                            "condition": "${tool-complex.value.analysis.metrics.accuracy} > 0.9",
                            "condition_type": "expression",
                        },
                    ),
                    WorkflowNode(
                        id="agent-complex",
                        type="agent",
                        position=Position(x=500, y=100),
                        config={
                            "agent_id": sample_agent.id,
                            "message": (
                                "Model analysis complete:\n"
                                "- Summary: ${tool-complex.value.analysis.summary}\n"
                                "- Accuracy: ${tool-complex.value.analysis.metrics.accuracy}\n"
                                "- Precision: ${tool-complex.value.analysis.metrics.precision}\n"
                                "- Version: ${tool-complex.meta.tool_name} v${tool-complex.value.metadata.version}\n"
                                "- Tags: ${tool-complex.value.analysis.tags.0}, ${tool-complex.value.analysis.tags.1}"
                            ),
                        },
                    ),
                ],
                edges=[
                    WorkflowEdge(**{"from_node_id": "tool-complex", "to_node_id": "conditional-nested"}),
                    WorkflowEdge(
                        **{
                            "from_node_id": "conditional-nested",
                            "to_node_id": "agent-complex",
                            "config": {"branch": "true"},
                        }
                    ),
                ],
            )

            workflow = Workflow(
                owner_id=test_user.id,
                name="Complex Variable Resolution Test",
                description="Test envelope format with complex nested variable access",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)
            assert execution_id is not None

            # Verify execution success
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Verify conditional evaluation worked with nested access
            from zerg.models.models import NodeExecutionState

            conditional_state = (
                db.query(NodeExecutionState)
                .filter_by(workflow_execution_id=execution_id, node_id="conditional-nested")
                .first()
            )

            assert conditional_state.phase == "finished"
            assert conditional_state.result == "success"
            assert conditional_state.output["value"]["result"]  # 0.95 > 0.9
            assert conditional_state.output["value"]["branch"] == "true"

            # Verify agent was executed (condition was true)
            agent_state = (
                db.query(NodeExecutionState)
                .filter_by(workflow_execution_id=execution_id, node_id="agent-complex")
                .first()
            )
            assert agent_state is not None
            assert agent_state.phase == "finished"
            assert agent_state.result == "success"


@pytest.mark.asyncio
async def test_envelope_format_alias_support(db, test_user, sample_agent):
    """Test that envelope format properly supports legacy aliases (${node.result} → ${node.value})."""

    with patch("zerg.services.node_executors.get_tool_resolver") as mock_resolver:
        mock_tool = type("MockTool", (), {})()
        mock_tool.run = lambda params: 42  # Simple numeric result

        mock_resolver_instance = type("MockResolver", (), {})()
        mock_resolver_instance.get_tool = lambda name: mock_tool if name == "number_gen" else None
        mock_resolver.return_value = mock_resolver_instance

        # Mock AgentRunner
        async def mock_run_thread(db, thread):
            # Create mock objects that look like ThreadMessage models
            mock_msg = type("MockMessage", (), {})()
            mock_msg.id = 1
            mock_msg.role = "assistant"
            mock_msg.content = "Number processed"
            mock_msg.sent_at = None
            mock_msg.thread_id = thread.id

            return [mock_msg]

        with patch("zerg.services.node_executors.AgentRunner") as mock_agent_runner:
            mock_runner_instance = type("MockRunner", (), {})()
            mock_runner_instance.run_thread = mock_run_thread
            mock_agent_runner.return_value = mock_runner_instance

            # Create workflow using legacy ${node.result} alias
            workflow_data = WorkflowData(
                nodes=[
                    WorkflowNode(
                        id="tool-alias",
                        type="tool",
                        position=Position(x=100, y=100),
                        config={"tool_name": "number_gen", "static_params": {}},
                    ),
                    WorkflowNode(
                        id="conditional-alias",
                        type="conditional",
                        position=Position(x=300, y=100),
                        config={
                            # Use envelope format: ${tool-alias} gets the value directly
                            "condition": "${tool-alias} == 42",
                            "condition_type": "expression",
                        },
                    ),
                    WorkflowNode(
                        id="agent-alias",
                        type="agent",
                        position=Position(x=500, y=100),
                        config={
                            "agent_id": sample_agent.id,
                            # Envelope format only
                            "message": "Tool result: ${tool-alias}, Value: ${tool-alias.value}, Tool name: ${tool-alias.meta.tool_name}",
                        },
                    ),
                ],
                edges=[
                    WorkflowEdge(**{"from_node_id": "tool-alias", "to_node_id": "conditional-alias"}),
                    WorkflowEdge(
                        **{
                            "from_node_id": "conditional-alias",
                            "to_node_id": "agent-alias",
                            "config": {"branch": "true"},
                        }
                    ),
                ],
            )

            workflow = Workflow(
                owner_id=test_user.id,
                name="Envelope Format Test",
                description="Test envelope format access patterns",
                canvas=workflow_data.model_dump(),
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            # Execute workflow
            execution_id = await workflow_engine.execute_workflow(workflow.id)
            assert execution_id is not None

            # Verify execution success
            from zerg.models.models import WorkflowExecution

            execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
            assert execution.phase == "finished"
            assert execution.result == "success"

            # Verify conditional worked with alias
            from zerg.models.models import NodeExecutionState

            conditional_state = (
                db.query(NodeExecutionState)
                .filter_by(workflow_execution_id=execution_id, node_id="conditional-alias")
                .first()
            )

            assert conditional_state.phase == "finished"
            assert conditional_state.result == "success"
            assert conditional_state.output["value"]["result"]  # 42 == 42

            # Verify agent was executed
            agent_state = (
                db.query(NodeExecutionState)
                .filter_by(workflow_execution_id=execution_id, node_id="agent-alias")
                .first()
            )
            assert agent_state is not None
            assert agent_state.phase == "finished"
            assert agent_state.result == "success"


if __name__ == "__main__":
    # Run tests manually for development
    import os
    import sys

    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    pytest.main([__file__, "-v"])
