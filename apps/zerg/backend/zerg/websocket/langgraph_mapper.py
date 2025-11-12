"""
LangGraph Stream Chunk to WebSocket Envelope Mapper

Transforms LangGraph streaming chunks (from stream_mode=["updates", "values"])
into our standardized WebSocket event envelopes for real-time workflow visualization.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from zerg.events import EventType

logger = logging.getLogger(__name__)


class LangGraphMapper:
    """Transforms LangGraph streaming chunks into WebSocket event envelopes."""

    @staticmethod
    def map_chunk_to_envelopes(chunk: Any, execution_id: int) -> List[Dict[str, Any]]:
        """
        Convert a single LangGraph chunk into 0+ WebSocket envelopes.

        Args:
            chunk: LangGraph stream chunk - can be dict or tuple depending on stream_mode
            execution_id: Current workflow execution ID

        Returns:
            List of envelope dicts ready for broadcast via event bus
        """
        envelopes = []

        logger.debug(f"[LangGraphMapper] Processing chunk type: {type(chunk)}, value: {chunk}")

        # Handle tuple format: (node_name, state_update)
        if isinstance(chunk, tuple) and len(chunk) == 2:
            node_id, state_update = chunk
            logger.debug(f"[LangGraphMapper] Tuple format - node: {node_id}, update type: {type(state_update)}")

            # Skip special keys
            if isinstance(node_id, str) and node_id.startswith("__"):
                logger.debug(f"[LangGraphMapper] Skipping special key: {node_id}")
                return envelopes

            # Map this node's update to a node_state envelope
            envelope = LangGraphMapper._map_node_update(node_id, state_update, execution_id)
            if envelope:
                envelopes.append(envelope)

            # Also generate workflow_progress if this update includes completed nodes
            if isinstance(state_update, dict) and "completed_nodes" in state_update:
                progress_envelope = LangGraphMapper._map_workflow_progress(state_update, execution_id)
                if progress_envelope:
                    envelopes.append(progress_envelope)

            return envelopes

        # Handle dict format: {node_name: state_update}
        if isinstance(chunk, dict):
            for node_id, state_update in chunk.items():
                # Skip special keys
                if node_id.startswith("__"):
                    logger.debug(f"[LangGraphMapper] Skipping special key: {node_id}")
                    continue

                logger.debug(f"[LangGraphMapper] Dict format - node: {node_id}, update type: {type(state_update)}")

                # Map this node's update to a node_state envelope
                envelope = LangGraphMapper._map_node_update(node_id, state_update, execution_id)
                if envelope:
                    envelopes.append(envelope)

                # Also generate workflow_progress if this update includes completed nodes
                if isinstance(state_update, dict) and "completed_nodes" in state_update:
                    progress_envelope = LangGraphMapper._map_workflow_progress(state_update, execution_id)
                    if progress_envelope:
                        envelopes.append(progress_envelope)

            return envelopes

        # Unknown format
        logger.warning(f"[LangGraphMapper] Unknown chunk format: {type(chunk)}")
        return envelopes

    @staticmethod
    def _map_node_update(node_id: str, update: Any, execution_id: int) -> Optional[Dict[str, Any]]:
        """
        Map a single node update to a node_state envelope.

        Args:
            node_id: The node's identifier
            update: The state update for this node (could be dict or any value)
            execution_id: Current workflow execution ID

        Returns:
            node_state envelope or None if update is not processable
        """
        # Handle different update structures
        if not isinstance(update, dict):
            # Simple value update - node likely just produced output
            logger.debug(f"[LangGraphMapper] Node {node_id} produced simple value")
            return {
                "event_type": EventType.NODE_STATE_CHANGED,
                "execution_id": execution_id,
                "node_id": node_id,
                "phase": "finished",
                "result": "success",
                "attempt_no": 1,
                "failure_kind": None,
                "error_message": None,
                "output": update,
                "timestamp": datetime.utcnow().isoformat(),
            }

        # Dict update - extract phase, result, error info
        phase = update.get("phase", "finished")  # Default to finished if not specified
        result = update.get("result")
        error = update.get("error")

        # Infer result from error if not explicitly set
        if result is None:
            result = "failure" if error else "success"

        # Extract output
        output = update.get("output") or update.get("node_outputs", {}).get(node_id)

        logger.debug(
            f"[LangGraphMapper] Node {node_id}: phase={phase}, result={result}, has_error={bool(error)}, has_output={bool(output)}"
        )

        return {
            "event_type": EventType.NODE_STATE_CHANGED,
            "execution_id": execution_id,
            "node_id": node_id,
            "phase": phase,
            "result": result,
            "attempt_no": update.get("attempt", 1),
            "failure_kind": update.get("error_kind") or update.get("failure_kind"),
            "error_message": str(error) if error else None,
            "output": output,
            "timestamp": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _map_workflow_progress(state: Dict[str, Any], execution_id: int) -> Optional[Dict[str, Any]]:
        """
        Map workflow state to a workflow_progress envelope.

        Args:
            state: The workflow state containing completed_nodes and node_outputs
            execution_id: Current workflow execution ID

        Returns:
            workflow_progress envelope or None if state doesn't contain progress info
        """
        completed_nodes = state.get("completed_nodes", [])
        node_outputs = state.get("node_outputs", {})

        if not completed_nodes and not node_outputs:
            return None

        logger.debug(
            f"[LangGraphMapper] Workflow progress: {len(completed_nodes)} completed, {len(node_outputs)} outputs"
        )

        return {
            "event_type": EventType.WORKFLOW_PROGRESS,
            "execution_id": execution_id,
            "completed_nodes": completed_nodes,
            "node_outputs": node_outputs,
            "error": state.get("error"),
            "timestamp": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def create_execution_started_envelope(execution_id: int) -> Dict[str, Any]:
        """
        Create an execution_started event (not a standard EventType, but useful for UI).

        Args:
            execution_id: Workflow execution ID

        Returns:
            Execution started envelope
        """
        return {
            "event_type": "execution_started",  # Custom event type for UI
            "execution_id": execution_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def create_execution_finished_envelope(
        execution_id: int,
        result: str,
        duration_ms: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create the final execution_finished envelope.

        Args:
            execution_id: Workflow execution ID
            result: "success" or "failure"
            duration_ms: Execution duration in milliseconds
            error_message: Error message if failed

        Returns:
            execution_finished envelope
        """
        logger.info(f"[LangGraphMapper] Execution {execution_id} finished: {result}")

        return {
            "event_type": EventType.EXECUTION_FINISHED,
            "execution_id": execution_id,
            "result": result,
            "duration_ms": duration_ms,
            "error_message": error_message,
            "timestamp": datetime.utcnow().isoformat(),
        }
