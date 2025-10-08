"""ExecutionStateMachine helper for managing Phase/Result state transitions.

This module provides validation and transition logic for the new execution state
architecture that separates "what's happening now" (Phase) from "how did it end" (Result).
"""

from datetime import datetime
from typing import Optional
from typing import Union

from zerg.models.enums import FailureKind
from zerg.models.enums import Phase
from zerg.models.enums import Result
from zerg.models.models import NodeExecutionState
from zerg.models.models import WorkflowExecution


class ExecutionStateMachine:
    """Helper class for managing execution state transitions with proper validation."""

    @staticmethod
    def can_start(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution can be started (must be in WAITING phase)."""
        return execution.phase == Phase.WAITING.value

    @staticmethod
    def can_finish(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution can be finished (must be in RUNNING phase)."""
        return execution.phase == Phase.RUNNING.value

    @staticmethod
    def can_retry(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution can be retried (must be finished with failure or cancelled)."""
        return execution.phase == Phase.FINISHED.value and execution.result in [
            Result.FAILURE.value,
            Result.CANCELLED.value,
        ]

    @staticmethod
    def mark_running(execution: Union[WorkflowExecution, NodeExecutionState]) -> None:
        """Transition execution to RUNNING phase."""
        if not ExecutionStateMachine.can_start(execution):
            raise ValueError(f"Cannot start execution in phase {execution.phase}")

        execution.phase = Phase.RUNNING.value
        execution.result = None  # Clear any previous result
        execution.heartbeat_ts = datetime.utcnow()

    @staticmethod
    def mark_success(execution: Union[WorkflowExecution, NodeExecutionState]) -> None:
        """Transition execution to FINISHED/SUCCESS."""
        if not ExecutionStateMachine.can_finish(execution):
            raise ValueError(f"Cannot finish execution in phase {execution.phase}")

        execution.phase = Phase.FINISHED.value
        execution.result = Result.SUCCESS.value
        execution.failure_kind = None
        execution.error_message = None
        execution.heartbeat_ts = None

    @staticmethod
    def mark_failure(
        execution: Union[WorkflowExecution, NodeExecutionState],
        error_message: Optional[str] = None,
        failure_kind: FailureKind = FailureKind.UNKNOWN,
    ) -> None:
        """Transition execution to FINISHED/FAILURE."""
        if not ExecutionStateMachine.can_finish(execution):
            raise ValueError(f"Cannot finish execution in phase {execution.phase}")

        execution.phase = Phase.FINISHED.value
        execution.result = Result.FAILURE.value
        execution.failure_kind = failure_kind.value if failure_kind else None
        execution.error_message = error_message
        execution.heartbeat_ts = None

    @staticmethod
    def mark_cancelled(execution: Union[WorkflowExecution, NodeExecutionState], reason: Optional[str] = None) -> None:
        """Transition execution to FINISHED/CANCELLED."""
        if execution.phase not in [Phase.WAITING.value, Phase.RUNNING.value]:
            raise ValueError(f"Cannot cancel execution in phase {execution.phase}")

        execution.phase = Phase.FINISHED.value
        execution.result = Result.CANCELLED.value
        execution.failure_kind = (FailureKind.USER if reason else FailureKind.SYSTEM).value
        execution.error_message = reason
        execution.heartbeat_ts = None

    @staticmethod
    def retry(execution: Union[WorkflowExecution, NodeExecutionState]) -> None:
        """Reset execution for retry - increment attempt and clear result."""
        if not ExecutionStateMachine.can_retry(execution):
            raise ValueError(f"Cannot retry execution in phase {execution.phase} with result {execution.result}")

        execution.phase = Phase.RUNNING.value
        execution.result = None  # Clear result to prevent analytics confusion
        execution.attempt_no += 1
        execution.failure_kind = None
        execution.error_message = None
        execution.heartbeat_ts = datetime.utcnow()

    @staticmethod
    def update_heartbeat(execution: Union[WorkflowExecution, NodeExecutionState]) -> None:
        """Update heartbeat timestamp for running executions."""
        if execution.phase == Phase.RUNNING.value:
            execution.heartbeat_ts = datetime.utcnow()

    @staticmethod
    def is_finished(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution is in terminal state."""
        return execution.phase == Phase.FINISHED.value

    @staticmethod
    def is_successful(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution completed successfully."""
        return execution.phase == Phase.FINISHED.value and execution.result == Result.SUCCESS.value

    @staticmethod
    def is_failed(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution failed."""
        return execution.phase == Phase.FINISHED.value and execution.result == Result.FAILURE.value

    @staticmethod
    def is_cancelled(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Check if execution was cancelled."""
        return execution.phase == Phase.FINISHED.value and execution.result == Result.CANCELLED.value

    @staticmethod
    def get_display_label(execution: Union[WorkflowExecution, NodeExecutionState]) -> str:
        """Get a human-readable label string for display purposes."""
        if execution.phase == Phase.WAITING.value:
            return "waiting"
        elif execution.phase == Phase.RUNNING.value:
            return "running"
        elif execution.phase == Phase.FINISHED.value:
            if execution.result == Result.SUCCESS.value:
                return "completed"
            elif execution.result == Result.FAILURE.value:
                return "failed"
            elif execution.result == Result.CANCELLED.value:
                return "cancelled"
            else:
                return "finished"
        else:
            return "unknown"

    @staticmethod
    def validate_state(execution: Union[WorkflowExecution, NodeExecutionState]) -> bool:
        """Validate that execution state is consistent with Phase/Result constraints."""
        # Check Phase/Result consistency constraint
        if execution.phase == Phase.FINISHED.value:
            if execution.result is None:
                return False
        else:
            if execution.result is not None:
                return False

        # Check that failure_kind is only set when result is FAILURE
        if execution.failure_kind is not None and execution.result != Result.FAILURE.value:
            return False

        # Check that heartbeat_ts is only set when phase is RUNNING
        if execution.heartbeat_ts is not None and execution.phase != Phase.RUNNING.value:
            return False

        return True
