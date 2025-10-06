"""
Helper functions for testing workflows.
"""

import asyncio
import time

from sqlalchemy.orm import Session

from zerg.models.models import WorkflowExecution


async def wait_for_workflow_completion(
    db: Session, execution_id: int, timeout: float = 10.0, poll_interval: float = 0.05, expected_phase: str = "finished"
) -> WorkflowExecution:
    """
    Poll for workflow completion with timeout.

    This is the proper way to wait for async workflow execution in tests,
    avoiding arbitrary sleep() calls that make tests flaky and slow.

    Args:
        db: Database session
        execution_id: ID of the workflow execution to wait for
        timeout: Maximum time to wait in seconds
        poll_interval: Initial polling interval in seconds (will use exponential backoff)
        expected_phase: Phase to wait for (default: "finished")

    Returns:
        The completed WorkflowExecution

    Raises:
        TimeoutError: If the workflow doesn't complete within timeout
        AssertionError: If the workflow fails unexpectedly
    """
    start_time = time.time()
    current_interval = poll_interval

    while time.time() - start_time < timeout:
        # Clear SQLAlchemy cache to get fresh data
        db.expire_all()

        execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()

        if not execution:
            raise AssertionError(f"Workflow execution {execution_id} not found")

        # Check if we've reached the expected phase
        if execution.phase == expected_phase:
            return execution

        # Check for early failure
        if execution.phase == "finished" and execution.result == "failure":
            # This might be expected in some tests
            return execution

        # Use exponential backoff to reduce database load
        await asyncio.sleep(current_interval)
        current_interval = min(current_interval * 1.5, 1.0)  # Max 1 second between polls

    # Timeout - get final state for debugging
    db.expire_all()
    execution = db.query(WorkflowExecution).filter_by(id=execution_id).first()

    raise TimeoutError(
        f"Workflow execution {execution_id} didn't reach phase '{expected_phase}' "
        f"within {timeout}s. Current phase: {execution.phase if execution else 'NOT FOUND'}, "
        f"result: {execution.result if execution else 'N/A'}"
    )


async def wait_for_all_nodes_complete(
    db: Session, execution_id: int, expected_nodes: list[str], timeout: float = 10.0
) -> dict[str, any]:
    """
    Wait for specific nodes to complete in a workflow.

    Args:
        db: Database session
        execution_id: Workflow execution ID
        expected_nodes: List of node IDs that should complete
        timeout: Maximum time to wait

    Returns:
        Dict mapping node_id to NodeExecutionState
    """
    from zerg.models.models import NodeExecutionState

    start_time = time.time()
    poll_interval = 0.05

    while time.time() - start_time < timeout:
        db.expire_all()

        node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()

        completed_nodes = {state.node_id: state for state in node_states if state.phase == "finished"}

        if all(node_id in completed_nodes for node_id in expected_nodes):
            return completed_nodes

        await asyncio.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 0.5)

    # Timeout - show what we did find
    db.expire_all()
    node_states = db.query(NodeExecutionState).filter_by(workflow_execution_id=execution_id).all()
    found_nodes = [state.node_id for state in node_states]

    raise TimeoutError(
        f"Not all expected nodes completed within {timeout}s. " f"Expected: {expected_nodes}, Found: {found_nodes}"
    )
