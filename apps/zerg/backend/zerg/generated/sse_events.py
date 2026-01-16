# AUTO-GENERATED FILE - DO NOT EDIT
# Generated from sse-events.asyncapi.yml
# Using AsyncAPI 3.0 + SSE Protocol Code Generation
#
# This file contains strongly-typed SSE event definitions.
# To update, modify the schema file and run: python scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml

import json
from enum import Enum
from typing import Any, Dict, Optional, Literal
from pydantic import BaseModel, Field


# Event payload schemas

class UsageData(BaseModel):
    """LLM token usage statistics"""

    prompt_tokens: Optional[int] = Field(default=None, ge=0, description='')
    completion_tokens: Optional[int] = Field(default=None, ge=0, description='')
    total_tokens: Optional[int] = Field(default=None, ge=0, description='')
    reasoning_tokens: Optional[int] = Field(default=None, ge=0, description='Reasoning tokens (OpenAI o1/o3 models)')

class WorkerStatus(str, Enum):
    """Worker execution result"""

    SUCCESS = "success"
    FAILED = "failed"

class ConnectedPayload(BaseModel):
    """Payload for ConnectedPayload"""

    message: str = Field(description='Connection confirmation message')
    run_id: int = Field(ge=1, description='Run ID for this SSE stream')
    client_correlation_id: Optional[str] = Field(default=None, description='Optional client-provided correlation ID')

class HeartbeatPayload(BaseModel):
    """Payload for HeartbeatPayload"""

    message: Optional[str] = Field(default=None, description='Optional heartbeat message')
    timestamp: Optional[str] = Field(default=None, description='ISO 8601 timestamp')

class SupervisorStartedPayload(BaseModel):
    """Payload for SupervisorStartedPayload"""

    run_id: Optional[int] = Field(default=None, ge=1, description='Run ID (may be omitted in legacy events)')
    thread_id: int = Field(ge=1, description='Thread ID for this conversation')
    task: str = Field(min_length=1, description='User\'s task/question')
    message_id: str = Field(description='Unique identifier for the assistant message (stable across tokens/completion)')
    continuation_of_message_id: Optional[str] = Field(default=None, description='For continuation runs, the message_id of the original run\'s message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging (copy from UI for agent debugging)')

class SupervisorThinkingPayload(BaseModel):
    """Payload for SupervisorThinkingPayload"""

    message: str = Field(min_length=1, description='Thinking status message')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class SupervisorTokenPayload(BaseModel):
    """Payload for SupervisorTokenPayload"""

    token: str = Field(description='LLM token (may be empty string)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')

class SupervisorCompletePayload(BaseModel):
    """Payload for SupervisorCompletePayload"""

    result: str = Field(description='Final supervisor result')
    status: Literal['success', 'cancelled'] = Field(description='Completion status (\'success\' for normal completion, \'cancelled\' for user-initiated cancellation)')
    duration_ms: Optional[int] = Field(default=None, ge=0, description='Execution duration in milliseconds')
    usage: Optional[UsageData] = Field(default=None)
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    agent_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    debug_url: Optional[str] = Field(default=None, description='URL for debug/inspection')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class SupervisorDeferredPayload(BaseModel):
    """Payload for SupervisorDeferredPayload"""

    message: str = Field(min_length=1, description='Deferred status message')
    attach_url: Optional[str] = Field(default=None, description='URL to re-attach to the running execution')
    timeout_seconds: Optional[float] = Field(default=None, ge=0, description='Timeout that triggered deferral')
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    agent_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')

class SupervisorWaitingPayload(BaseModel):
    """Payload for SupervisorWaitingPayload"""

    message: str = Field(min_length=1, description='Waiting status message (e.g., worker spawned)')
    job_id: Optional[int] = Field(default=None, ge=1, description='Worker job ID (if applicable)')
    close_stream: Optional[bool] = Field(default=None, description='If false, keep SSE stream open while waiting')
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    agent_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')

class SupervisorResumedPayload(BaseModel):
    """Payload for SupervisorResumedPayload"""

    run_id: Optional[int] = Field(default=None, ge=1, description='')
    agent_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: int = Field(ge=1, description='')
    message_id: str = Field(description='Unique identifier for the assistant message')

class ErrorPayload(BaseModel):
    """Payload for ErrorPayload"""

    error: Optional[str] = Field(default=None, description='Error message')
    message: Optional[str] = Field(default=None, description='Alternative error message field')
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class WorkerSpawnedPayload(BaseModel):
    """Payload for WorkerSpawnedPayload"""

    job_id: int = Field(ge=1, description='Worker job ID')
    tool_call_id: Optional[str] = Field(default=None, description='Tool call ID for the spawn_worker invocation')
    task: str = Field(min_length=1, description='Worker task (may be truncated to 100 chars)')
    model: Optional[str] = Field(default=None, description='LLM model for worker')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class WorkerStartedPayload(BaseModel):
    """Payload for WorkerStartedPayload"""

    job_id: int = Field(ge=1, description='')
    worker_id: str = Field(min_length=1, description='Worker execution ID')
    run_id: Optional[int] = Field(default=None, ge=1, description='')
    task: Optional[str] = Field(default=None, description='Worker task (may be truncated)')

class WorkerCompletePayload(BaseModel):
    """Payload for WorkerCompletePayload"""

    job_id: int = Field(ge=1, description='')
    worker_id: Optional[str] = Field(default=None, description='Worker execution ID')
    status: WorkerStatus
    duration_ms: Optional[int] = Field(default=None, ge=0, description='')
    error: Optional[str] = Field(default=None, description='Error message (only present if status=failed)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class WorkerSummaryReadyPayload(BaseModel):
    """Payload for WorkerSummaryReadyPayload"""

    job_id: int = Field(ge=1, description='')
    worker_id: Optional[str] = Field(default=None, description='Worker execution ID')
    summary: str = Field(min_length=1, description='Extracted worker summary')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class WorkerToolStartedPayload(BaseModel):
    """Payload for WorkerToolStartedPayload"""

    worker_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='LangChain tool call ID')
    tool_args_preview: Optional[str] = Field(default=None, description='Preview of tool arguments (may be truncated)')
    run_id: Optional[int] = Field(default=None, ge=1, description='Required for security (prevents cross-run leakage)')

class WorkerToolCompletedPayload(BaseModel):
    """Payload for WorkerToolCompletedPayload"""

    worker_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    result_preview: Optional[str] = Field(default=None, description='Preview of tool result (may be truncated)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class WorkerToolFailedPayload(BaseModel):
    """Payload for WorkerToolFailedPayload"""

    worker_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    error: str = Field(min_length=1, description='Error message')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class SupervisorToolStartedPayload(BaseModel):
    """Payload for SupervisorToolStartedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='Stable ID linking all events for this tool call')
    tool_args_preview: Optional[str] = Field(default=None, description='Preview of tool arguments (may be truncated)')
    tool_args: Optional[Dict[str, Any]] = Field(default=None, description='Full tool arguments (for persistence/raw view)')
    run_id: Optional[int] = Field(default=None, ge=1, description='Supervisor run ID for correlation')

class SupervisorToolProgressPayload(BaseModel):
    """Payload for SupervisorToolProgressPayload"""

    tool_call_id: str = Field(min_length=1, description='')
    message: str = Field(description='Progress message (log line)')
    level: Optional[Literal['debug', 'info', 'warn', 'error']] = Field(default=None, description='Log level for styling')
    progress_pct: Optional[int] = Field(default=None, ge=0, le=100, description='Optional progress percentage')
    data: Optional[Dict[str, Any]] = Field(default=None, description='Optional structured data (metrics, artifacts preview)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class SupervisorToolCompletedPayload(BaseModel):
    """Payload for SupervisorToolCompletedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    result_preview: Optional[str] = Field(default=None, description='Condensed result for collapsed view')
    result: Optional[Dict[str, Any]] = Field(default=None, description='Full result (for persistence/raw view)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class SupervisorToolFailedPayload(BaseModel):
    """Payload for SupervisorToolFailedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    error: str = Field(min_length=1, description='Error message')
    error_details: Optional[Dict[str, Any]] = Field(default=None, description='Full error details (stack trace, context)')
    run_id: Optional[int] = Field(default=None, ge=1, description='')

class SSEEventType(str, Enum):
    """Enumeration of all SSE event types."""

    CONNECTED = "connected"
    HEARTBEAT = "heartbeat"
    SUPERVISOR_STARTED = "supervisor_started"
    SUPERVISOR_THINKING = "supervisor_thinking"
    SUPERVISOR_TOKEN = "supervisor_token"
    SUPERVISOR_COMPLETE = "supervisor_complete"
    SUPERVISOR_DEFERRED = "supervisor_deferred"
    SUPERVISOR_WAITING = "supervisor_waiting"
    SUPERVISOR_RESUMED = "supervisor_resumed"
    ERROR = "error"
    WORKER_SPAWNED = "worker_spawned"
    WORKER_STARTED = "worker_started"
    WORKER_COMPLETE = "worker_complete"
    WORKER_SUMMARY_READY = "worker_summary_ready"
    WORKER_TOOL_STARTED = "worker_tool_started"
    WORKER_TOOL_COMPLETED = "worker_tool_completed"
    WORKER_TOOL_FAILED = "worker_tool_failed"
    SUPERVISOR_TOOL_STARTED = "supervisor_tool_started"
    SUPERVISOR_TOOL_PROGRESS = "supervisor_tool_progress"
    SUPERVISOR_TOOL_COMPLETED = "supervisor_tool_completed"
    SUPERVISOR_TOOL_FAILED = "supervisor_tool_failed"


# Typed emitter for SSE events

def emit_sse_event(
    event_type: SSEEventType,
    payload: BaseModel,
    event_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a typed SSE event dict ready for yield.

    Returns dict with 'event', 'data', and optionally 'id' keys.
    Use like: yield emit_sse_event(SSEEventType.SUPERVISOR_STARTED, SupervisorStartedPayload(...))

    Args:
        event_type: SSE event type enum value
        payload: Pydantic model instance for the event payload
        event_id: Optional event ID for resumable streams

    Returns:
        Dict ready for SSE yield (with 'event', 'data', 'id' keys)
    """
    result = {
        "event": event_type.value,
        "data": json.dumps(payload.model_dump()),
    }

    if event_id is not None:
        result["id"] = str(event_id)

    return result
