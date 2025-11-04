"""
Node execution handlers for workflow engine.

Single format: NodeOutputEnvelope {"value": ..., "meta": {...}}
No backward compatibility. Clean, direct implementation.
"""

import logging
from typing import Any
from typing import Dict

from zerg.callbacks.token_stream import set_current_user_id
from zerg.crud import crud
from zerg.database import get_session_factory
from zerg.managers.agent_runner import AgentRunner
from zerg.models.enums import FailureKind
from zerg.models.models import Agent
from zerg.models.models import NodeExecutionState
from zerg.schemas.node_output import create_agent_envelope
from zerg.schemas.node_output import create_conditional_envelope
from zerg.schemas.node_output import create_tool_envelope
from zerg.schemas.node_output import create_trigger_envelope
from zerg.services.execution_state import ExecutionStateMachine
from zerg.services.expression_evaluator import safe_evaluator
from zerg.services.variable_resolver import resolve_variables
from zerg.tools.unified_access import get_tool_resolver

logger = logging.getLogger(__name__)


class BaseNodeExecutor:
    """Base class for node executors. Envelope format only."""

    def __init__(self, node, publish_event_callback, node_type: str):
        self.node = node
        self.node_id = node.id
        self.publish_event = publish_event_callback
        self.node_type = node_type

    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the node and return updated state."""
        session_factory = get_session_factory()

        with session_factory() as db:
            # Create execution state in WAITING phase
            node_state = NodeExecutionState(workflow_execution_id=state["execution_id"], node_id=self.node_id)
            db.add(node_state)
            db.commit()

            # Mark as running using state machine
            ExecutionStateMachine.mark_running(node_state)
            db.commit()

            await self.publish_event(
                execution_id=state["execution_id"], node_id=self.node_id, node_state=node_state, output=None
            )

            try:
                output = await self._execute_node_logic(db, state)

                # Mark as successful using state machine
                ExecutionStateMachine.mark_success(node_state)
                # Store envelope output directly
                node_state.output = output.model_dump()
                db.commit()

                await self.publish_event(
                    execution_id=state["execution_id"],
                    node_id=self.node_id,
                    node_state=node_state,
                    output=str(output),
                )

                # Store envelope output in state
                return {
                    **state,
                    "node_outputs": {**state["node_outputs"], self.node_id: output.model_dump()},
                    "completed_nodes": state["completed_nodes"] + [self.node_id],
                }

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[{self.__class__.__name__}] Error in node {self.node_id}: {error_msg}")

                # Mark as failed using state machine
                ExecutionStateMachine.mark_failure(node_state, error_message=error_msg, failure_kind=FailureKind.SYSTEM)
                error_output = self._create_error_output(error_msg)
                node_state.output = error_output.model_dump()
                db.commit()

                await self.publish_event(
                    execution_id=state["execution_id"],
                    node_id=self.node_id,
                    node_state=node_state,
                    output=None,
                )

                return {
                    **state,
                    "error": f"{self.node.type} node {self.node_id} failed: {error_msg}",
                    "completed_nodes": state["completed_nodes"] + [self.node_id],
                }

    async def _execute_node_logic(self, db, state):
        """Override this in subclasses."""
        raise NotImplementedError

    def _create_envelope_output(self, value, node_type, **kwargs):
        """Create envelope output based on node type."""
        if node_type == "tool":
            return create_tool_envelope(value, **kwargs)
        elif node_type == "agent":
            return create_agent_envelope(value, **kwargs)
        elif node_type == "conditional":
            return create_conditional_envelope(value, **kwargs)
        elif node_type == "trigger":
            return create_trigger_envelope(value, **kwargs)
        else:
            return create_tool_envelope(value, **kwargs)

    def _create_error_output(self, error_msg):
        """Create error envelope using stored node type."""
        return self._create_envelope_output(
            value=None, node_type=self.node_type, phase="finished", result="failure", error_message=error_msg
        )


class AgentNodeExecutor(BaseNodeExecutor):
    """Executes agent nodes. Envelope format only."""

    async def _execute_node_logic(self, db, state):
        # Resolve variables in node configuration
        node_outputs = state.get("node_outputs", {})
        resolved_config = resolve_variables(self.node.config, node_outputs)

        agent_id = resolved_config.get("agent_id")
        message = resolved_config.get("message", "Execute this task")

        if not agent_id:
            raise ValueError(f"Agent node {self.node_id} missing agent_id in config")

        logger.info(f"[AgentNode] Starting execution – node_id={self.node_id}, agent_id={agent_id}")

        # Get agent
        agent = db.query(Agent).filter_by(id=agent_id).first()
        if not agent:
            raise ValueError(f"Agent {agent_id} not found in database")

        logger.info(f"[AgentNode] Found agent: {agent.name} (id={agent.id})")

        # Create thread and execute
        thread = crud.create_thread(
            db=db,
            agent_id=agent_id,
            title=f"Workflow execution {state['execution_id']}",
        )

        crud.create_thread_message(
            db=db,
            thread_id=thread.id,
            role="user",
            content=message,
            processed=False,
        )

        # Execute via AgentRunner
        runner = AgentRunner(agent)

        # Set user context for token streaming
        set_current_user_id(agent.owner_id)

        try:
            created_messages = await runner.run_thread(db, thread)
        finally:
            # Clean up user context
            set_current_user_id(None)

        # Convert SQLAlchemy objects to serializable dictionaries
        serialized_messages = []
        for msg in created_messages:
            serialized_messages.append(
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
                    "thread_id": msg.thread_id,
                }
            )

        # Return envelope format only
        return self._create_envelope_output(
            value={
                "messages": serialized_messages,
                "messages_created": len(created_messages),
            },
            node_type="agent",
            phase="finished",
            result="success",
            agent_id=agent_id,
            agent_name=agent.name,
            thread_id=thread.id,
        )


class ToolNodeExecutor(BaseNodeExecutor):
    """Executes tool nodes. Envelope format only."""

    async def _execute_node_logic(self, db, state):
        # Resolve variables in node configuration
        node_outputs = state.get("node_outputs", {})
        resolved_config = resolve_variables(self.node.config, node_outputs)

        tool_name = resolved_config.get("tool_name")

        if not tool_name:
            raise ValueError(f"Tool node {self.node_id} missing tool_name in config")

        logger.info(f"[ToolNode] Starting execution – node_id={self.node_id}, tool_name={tool_name}")

        # Get tool resolver and execute
        tool_resolver = get_tool_resolver()
        tool = tool_resolver.get_tool(tool_name)

        if not tool:
            raise ValueError(f"Tool {tool_name} not found")

        static_params = resolved_config.get("static_params", {})
        output = tool.run(static_params)

        # Return envelope format only
        return self._create_envelope_output(
            value=output,
            node_type="tool",
            phase="finished",
            result="success",
            tool_name=tool_name,
            parameters=static_params,
        )


class TriggerNodeExecutor(BaseNodeExecutor):
    """Executes trigger nodes. Envelope format only."""

    async def _execute_node_logic(self, db, state):
        logger.info(f"[TriggerNode] Executing trigger node: {self.node_id}")

        # Resolve typed trigger meta (strict, typed-only)
        from zerg.schemas.workflow import resolve_trigger_meta

        meta = resolve_trigger_meta(self.node)
        ttype = meta.get("type", "manual")
        tconf = meta.get("config", {})

        # Return envelope format only
        return self._create_envelope_output(
            value={"triggered": True},
            node_type="trigger",
            phase="finished",
            result="success",
            trigger_type=ttype,
            trigger_config=tconf,
        )


class ConditionalNodeExecutor(BaseNodeExecutor):
    """Executes conditional nodes. Envelope format only."""

    async def _execute_node_logic(self, db, state):
        logger.info(f"[ConditionalNode] Executing conditional node: {self.node_id}")

        # Resolve variables in node configuration
        node_outputs = state.get("node_outputs", {})
        resolved_config = resolve_variables(self.node.config, node_outputs)

        condition = resolved_config.get("condition", "")
        condition_type = resolved_config.get("condition_type", "expression")

        if not condition:
            raise ValueError(f"Conditional node {self.node_id} missing condition")

        # Evaluate the condition
        condition_result = self._evaluate_condition(condition, condition_type, node_outputs)

        logger.info(f"[ConditionalNode] Condition '{condition}' evaluated to {condition_result}")

        # Return envelope format only
        branch = "true" if condition_result else "false"
        return self._create_envelope_output(
            value={"result": condition_result, "branch": branch},
            node_type="conditional",
            phase="finished",
            result="success",
            condition=condition,
            evaluation_method="ast_safe",
        )

    def _evaluate_condition(self, condition: str, condition_type: str, node_outputs: Dict[str, Any]) -> bool:
        """Evaluate condition. Clean, direct evaluation."""

        if condition_type == "expression":
            try:
                # Resolve variables in condition
                resolved_condition = resolve_variables(condition, node_outputs)

                # If condition resolved to a single value, check truthiness
                if not isinstance(resolved_condition, str):
                    return bool(resolved_condition)

                # Evaluate the resolved expression
                result = safe_evaluator.evaluate(resolved_condition, {})
                return bool(result)

            except Exception as e:
                logger.error(f"Failed to evaluate condition '{condition}': {e}")
                return False

        elif condition_type == "exists":
            try:
                resolve_variables(f"${{{condition}}}", node_outputs)
                return True
            except Exception:
                return False

        else:
            raise ValueError(f"Unsupported condition type: {condition_type}")


def create_node_executor(node, publish_event_callback) -> BaseNodeExecutor:
    """Factory function to create node executor. Envelope format only."""
    if node.type == "agent":
        return AgentNodeExecutor(node, publish_event_callback, "agent")
    elif node.type == "tool":
        return ToolNodeExecutor(node, publish_event_callback, "tool")
    elif node.type == "trigger":
        return TriggerNodeExecutor(node, publish_event_callback, "trigger")
    elif node.type == "conditional":
        return ConditionalNodeExecutor(node, publish_event_callback, "conditional")
    else:
        # Placeholder for unknown types
        class PlaceholderExecutor(BaseNodeExecutor):
            async def _execute_node_logic(self, db, state):
                logger.warning(f"[PlaceholderNode] Unknown node type: {self.node_id}")
                return self._create_envelope_output(
                    value={"skipped": True}, node_type="placeholder", phase="finished", result="success"
                )

        return PlaceholderExecutor(node, publish_event_callback, "placeholder")
