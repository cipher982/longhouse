"""
Simplified Workflow Execution Engine

Clean, focused workflow execution using WorkflowData schema.
~150 lines vs the previous 600+ line monolith.
"""

import asyncio
import logging
import operator
from typing import Annotated
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import TypedDict
from typing import Union

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph

from zerg.database import get_session_factory
from zerg.events import EventType
from zerg.events.publisher import publish_event
from zerg.models.enums import FailureKind
from zerg.models.models import Workflow
from zerg.models.models import WorkflowExecution
from zerg.schemas.workflow import WorkflowData
from zerg.services.execution_state import ExecutionStateMachine
from zerg.services.node_executors import create_node_executor
from zerg.utils.time import utc_now_naive
from zerg.websocket.langgraph_mapper import LangGraphMapper

logger = logging.getLogger(__name__)


def merge_dicts(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two dictionaries, with right taking precedence."""
    return {**left, **right}


def first_error(left: Union[str, None], right: Union[str, None]) -> Union[str, None]:
    """Return the first non-None error (fail-fast semantics)."""
    return left if left is not None else right


class WorkflowState(TypedDict):
    """State passed between nodes in the workflow.

    Uses Annotated types with reducers for concurrency-safe parallel execution:
    - node_outputs: merges outputs from parallel nodes
    - completed_nodes: concatenates completion lists from parallel branches
    - error: takes first error (fail-fast semantics for parallel failures)

    execution_id is passed via config, not state, as it's immutable metadata.
    """

    node_outputs: Annotated[Dict[str, Any], merge_dicts]
    completed_nodes: Annotated[List[str], operator.add]
    error: Annotated[Union[str, None], first_error]


class WorkflowEngine:
    """Simplified workflow engine using WorkflowData schema."""

    def __init__(self):
        """Initialize the workflow engine with task tracking."""
        self._running_tasks: Dict[int, asyncio.Task] = {}

    async def execute_workflow(self, workflow_id: int, trigger_type: str = "manual") -> int:
        """Execute workflow and return execution ID."""
        logger.info(f"[WorkflowEngine] Starting execution – workflow_id={workflow_id}")

        session_factory = get_session_factory()
        with session_factory() as db:
            # Create execution record in WAITING state
            execution = WorkflowExecution(
                workflow_id=workflow_id,
                started_at=utc_now_naive(),
                triggered_by=trigger_type,
            )
            db.add(execution)
            db.commit()

            try:
                # Mark as running using state machine
                ExecutionStateMachine.mark_running(execution)
                db.commit()

                await self._execute_workflow_internal(workflow_id, execution, db)
                return execution.id
            except Exception as e:
                # Mark as failed using state machine only if not already finished
                if ExecutionStateMachine.can_finish(execution):
                    ExecutionStateMachine.mark_failure(execution, error_message=str(e), failure_kind=FailureKind.SYSTEM)
                    execution.finished_at = utc_now_naive()
                    db.commit()

                await self._publish_execution_finished(
                    execution_id=execution.id, execution=execution, duration_ms=self._duration_ms(execution)
                )
                logger.exception(f"[WorkflowEngine] Execution failed – execution_id={execution.id}")
                raise

    async def _execute_workflow_internal(self, workflow_id: int, execution: WorkflowExecution, db):
        """Load and execute workflow."""
        # Load workflow
        workflow_model = db.query(Workflow).filter_by(id=workflow_id, is_active=True).first()
        if not workflow_model:
            raise ValueError("Workflow not found or inactive")

        try:
            workflow_data = WorkflowData(**workflow_model.canvas)
            logger.info(f"[WorkflowEngine] Loaded workflow with {len(workflow_data.nodes)} nodes")
        except Exception as e:
            raise ValueError(f"Invalid workflow data: {e}")

        # Handle empty workflows
        if not workflow_data.nodes:
            logger.info(f"[WorkflowEngine] Empty workflow {workflow_id} - completing immediately")
            ExecutionStateMachine.mark_success(execution)
            execution.finished_at = utc_now_naive()
            db.commit()
            await publish_event(
                EventType.EXECUTION_FINISHED,
                {
                    "execution_id": execution.id,
                    "workflow_id": workflow_id,
                    "status": ExecutionStateMachine.get_display_label(execution),
                    "event_type": EventType.EXECUTION_FINISHED,
                },
            )
            return

        # Build and execute graph
        graph = self._build_langgraph(workflow_data, execution.id)
        await self._execute_graph(graph, execution, db, workflow_id)

    def _build_langgraph(self, workflow_data: WorkflowData, execution_id: int):
        """Build LangGraph from WorkflowData."""
        graph = StateGraph(WorkflowState)

        # Add nodes (execution_id will be passed via config, not state)
        for node in workflow_data.nodes:
            executor = create_node_executor(node, self._publish_node_event)
            graph.add_node(node.id, executor.execute)
            logger.info(f"[WorkflowEngine] Added node: {node.id} (type: {node.type})")

        # Find start and end nodes
        target_nodes = {edge.to_node_id for edge in workflow_data.edges}
        source_nodes = {edge.from_node_id for edge in workflow_data.edges}

        start_nodes = [node.id for node in workflow_data.nodes if node.id not in target_nodes]
        end_nodes = [node.id for node in workflow_data.nodes if node.id not in source_nodes]

        logger.info(f"[WorkflowEngine] Start nodes: {start_nodes}, End nodes: {end_nodes}")
        logger.info(f"[WorkflowEngine] Edges: {[(e.from_node_id, e.to_node_id) for e in workflow_data.edges]}")

        # Connect graph
        for start_node in start_nodes:
            graph.add_edge(START, start_node)
            logger.info(f"[WorkflowEngine] Connected START -> {start_node}")

        # Group edges by source node to handle conditional routing
        edges_by_source = {}
        for edge in workflow_data.edges:
            if edge.from_node_id not in edges_by_source:
                edges_by_source[edge.from_node_id] = []
            edges_by_source[edge.from_node_id].append(edge)

        # Add edges with conditional routing support
        for source_node_id, edges in edges_by_source.items():
            from_node = next((n for n in workflow_data.nodes if n.id == source_node_id), None)

            if from_node and from_node.type == "conditional":
                # For conditional nodes, create a router that handles all outgoing edges
                true_targets = [e.to_node_id for e in edges if e.config.get("branch", "true") == "true"]
                false_targets = [e.to_node_id for e in edges if e.config.get("branch", "true") == "false"]

                def make_conditional_router(true_list, false_list):
                    def conditional_router(state):
                        """Route based on conditional node result."""
                        if source_node_id in state["node_outputs"]:
                            conditional_result = state["node_outputs"][source_node_id]

                            # Envelope format only
                            if isinstance(conditional_result, dict) and "value" in conditional_result:
                                result_value = conditional_result["value"]
                                if isinstance(result_value, dict) and "branch" in result_value:
                                    branch = result_value["branch"]
                                else:
                                    branch = "false"
                            else:
                                branch = "false"

                            if branch == "true" and true_list:
                                return true_list[0]  # Take first true target
                            elif branch == "false" and false_list:
                                return false_list[0]  # Take first false target
                        return END  # End workflow if no valid route

                    return conditional_router

                # Build the routing map
                route_map = {}
                if true_targets:
                    route_map[true_targets[0]] = true_targets[0]
                if false_targets:
                    route_map[false_targets[0]] = false_targets[0]
                route_map[END] = END

                graph.add_conditional_edges(
                    source_node_id, make_conditional_router(true_targets, false_targets), route_map
                )
            else:
                # Regular edges - add them normally
                for edge in edges:
                    graph.add_edge(edge.from_node_id, edge.to_node_id)
                    logger.info(f"[WorkflowEngine] Connected {edge.from_node_id} -> {edge.to_node_id}")

        for end_node in end_nodes:
            graph.add_edge(end_node, END)
            logger.info(f"[WorkflowEngine] Connected {end_node} -> END")

        checkpointer = MemorySaver()
        return graph.compile(checkpointer=checkpointer)

    async def _execute_graph(self, graph, execution: WorkflowExecution, db, workflow_id: int):
        """Execute the compiled graph."""
        # Remove execution_id from state - it's immutable metadata, passed via config
        initial_state = {
            "node_outputs": {},
            "completed_nodes": [],
            "error": None,
        }

        # Pass execution_id as immutable config, not mutable state
        config = {
            "configurable": {
                "thread_id": f"workflow_{execution.id}",
                "execution_id": execution.id,  # Immutable metadata
            }
        }
        logger.info(f"[WorkflowEngine] Starting streaming execution – workflow_id={workflow_id}")

        # Emit execution started event
        started_envelope = LangGraphMapper.create_execution_started_envelope(execution.id)
        logger.info(f"🚀🚀🚀 WORKFLOW_ENGINE: About to publish EXECUTION_STARTED for execution_id={execution.id}")
        await publish_event(EventType.EXECUTION_STARTED, started_envelope)
        logger.info("🚀🚀🚀 WORKFLOW_ENGINE: Published EXECUTION_STARTED successfully")

        try:
            # Stream execution for real-time updates with granular node state events
            # Using stream_mode=["updates"] to get per-node state transitions
            async for chunk in graph.astream(initial_state, config, stream_mode=["updates"]):
                # Debug: log chunk type and structure
                logger.info(f"[WorkflowEngine] Processing chunk type: {type(chunk)}, value: {chunk}")

                if chunk:
                    # Map LangGraph chunks to our event envelopes
                    envelopes = LangGraphMapper.map_chunk_to_envelopes(chunk, execution.id)

                    # Broadcast each envelope
                    for envelope in envelopes:
                        event_type = envelope.pop("event_type")  # Extract event type
                        logger.info(f"🔥🔥🔥 WORKFLOW_ENGINE: Publishing {event_type} for execution {execution.id}, envelope={envelope}")
                        await publish_event(event_type, envelope)
                        logger.info(f"✅✅✅ WORKFLOW_ENGINE: Published {event_type} successfully")

            # Mark as successful using state machine
            ExecutionStateMachine.mark_success(execution)
            execution.finished_at = utc_now_naive()
            db.commit()

            await self._publish_execution_finished(
                execution_id=execution.id, execution=execution, duration_ms=self._duration_ms(execution)
            )
            logger.info(f"[WorkflowEngine] Execution completed – execution_id={execution.id}")

        except Exception:
            # Log error but don't publish EXECUTION_FINISHED here - let outer handler do it once
            logger.exception(f"[WorkflowEngine] Graph execution failed – execution_id={execution.id}")
            raise  # Re-raise to trigger state machine handling and single EXECUTION_FINISHED event

    # Event publishing methods
    async def _publish_node_event(self, *, execution_id: int, node_id: str, node_state, output: Any):
        # Clean Phase/Result architecture - no legacy fields
        await publish_event(
            EventType.NODE_STATE_CHANGED,
            {
                "execution_id": execution_id,
                "node_id": node_id,
                "phase": node_state.phase,
                "result": node_state.result,
                "attempt_no": node_state.attempt_no,
                "failure_kind": node_state.failure_kind,
                "error_message": node_state.error_message,
                "output": output,
                "event_type": EventType.NODE_STATE_CHANGED,
            },
        )

    async def _publish_streaming_progress(
        self, *, execution_id: int, completed_nodes: List[str], node_outputs: Dict[str, Any], error: str
    ):
        await publish_event(
            EventType.WORKFLOW_PROGRESS,
            {
                "execution_id": execution_id,
                "completed_nodes": completed_nodes,
                "node_outputs": node_outputs,
                "error": error,
                "event_type": EventType.WORKFLOW_PROGRESS,
            },
        )

    async def _publish_execution_finished(self, *, execution_id: int, execution: WorkflowExecution, duration_ms: int):
        # Clean Phase/Result architecture - no legacy fields
        await publish_event(
            EventType.EXECUTION_FINISHED,
            {
                "execution_id": execution_id,
                "result": execution.result,
                "attempt_no": execution.attempt_no,
                "failure_kind": execution.failure_kind,
                "error_message": execution.error_message,
                "duration_ms": duration_ms,
                "event_type": EventType.EXECUTION_FINISHED,
            },
        )

    @staticmethod
    def _duration_ms(execution: WorkflowExecution) -> Union[int, None]:
        if execution.started_at and execution.finished_at:
            delta = execution.finished_at - execution.started_at
            return int(delta.total_seconds() * 1000)
        return None

    def start_workflow_in_background(self, workflow_id: int, execution_id: int) -> None:
        """Start a workflow execution in the background with proper tracking.

        Args:
            workflow_id: ID of the workflow to execute
            execution_id: ID of the pre-created execution record
        """

        async def run_workflow():
            """Run the workflow in background."""
            logger.info(f"[WorkflowEngine] Background execution starting for execution_id={execution_id}")
            session_factory = get_session_factory()
            with session_factory() as db:
                execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
                if not execution:
                    logger.error(f"[WorkflowEngine] Execution {execution_id} not found")
                    return

                try:
                    await self._execute_workflow_internal(workflow_id, execution, db)
                    logger.info(f"[WorkflowEngine] Background execution completed for execution_id={execution_id}")
                except Exception as e:
                    msg = f"[WorkflowEngine] Background execution failed for execution_id={execution_id}: {e}"
                    logger.exception(msg)
                finally:
                    # Clean up task tracking
                    self._running_tasks.pop(execution_id, None)

        # Create and track the task
        task = asyncio.create_task(run_workflow())
        self._running_tasks[execution_id] = task
        logger.info(f"[WorkflowEngine] Task created for execution_id={execution_id}")

    async def wait_for_completion(self, execution_id: int, timeout: Optional[float] = None) -> bool:
        """Wait for a workflow execution to complete.

        Args:
            execution_id: ID of the execution to wait for
            timeout: Optional timeout in seconds

        Returns:
            True if execution completed, False if timed out or not found
        """
        start_time = utc_now_naive()

        # Retry loop to handle startup delay (race between /start response and background task registration)
        while True:
            task = self._running_tasks.get(execution_id)
            if task:
                break

            # Check if execution already completed or is pending
            session_factory = get_session_factory()
            with session_factory() as db:
                execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()
                if execution and execution.phase == "finished":
                    logger.info(f"[WorkflowEngine] Execution {execution_id} already completed")
                    return True
                if not execution:
                    logger.warning(f"[WorkflowEngine] Execution {execution_id} not found in DB")
                    return False

            # Check timeout
            if timeout and (utc_now_naive() - start_time).total_seconds() > timeout:
                logger.warning(f"[WorkflowEngine] Timeout waiting for task registration execution_id={execution_id}")
                return False

            # Wait for task to appear (handle the 100ms delay in start_workflow_execution)
            await asyncio.sleep(0.05)

        try:
            # Adjust timeout for time spent waiting for registration
            remaining_timeout = None
            if timeout:
                elapsed = (utc_now_naive() - start_time).total_seconds()
                remaining_timeout = max(0.1, timeout - elapsed)

            await asyncio.wait_for(task, timeout=remaining_timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[WorkflowEngine] Timeout waiting for execution_id={execution_id}")
            return False
        except Exception as e:
            logger.error(f"[WorkflowEngine] Error waiting for execution_id={execution_id}: {e}")
            return False

    def get_running_executions(self) -> List[int]:
        """Get list of currently running execution IDs."""
        return list(self._running_tasks.keys())

    async def shutdown(self):
        """Gracefully shutdown the engine and wait for running tasks."""
        if not self._running_tasks:
            return

        logger.info(f"[WorkflowEngine] Waiting for {len(self._running_tasks)} running executions to complete")

        # Wait for all tasks with timeout
        pending_tasks = list(self._running_tasks.values())
        try:
            await asyncio.wait_for(asyncio.gather(*pending_tasks, return_exceptions=True), timeout=30.0)
            logger.info("[WorkflowEngine] All executions completed gracefully")
        except asyncio.TimeoutError:
            logger.warning("[WorkflowEngine] Timeout waiting for executions, cancelling remaining tasks")
            for task in self._running_tasks.values():
                if not task.done():
                    task.cancel()

        self._running_tasks.clear()


# Singleton instance
workflow_engine = WorkflowEngine()
