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

class CommisStatus(str, Enum):
    """Commis execution result"""

    SUCCESS = "success"
    FAILED = "failed"

class ConnectedPayload(BaseModel):
    """Payload for ConnectedPayload"""

    message: str = Field(description='Connection confirmation message')
    course_id: int = Field(ge=1, description='Course ID for this SSE stream')
    client_correlation_id: Optional[str] = Field(default=None, description='Optional client-provided correlation ID')

class HeartbeatPayload(BaseModel):
    """Payload for HeartbeatPayload"""

    message: Optional[str] = Field(default=None, description='Optional heartbeat message')
    timestamp: Optional[str] = Field(default=None, description='ISO 8601 timestamp')

class ConciergeStartedPayload(BaseModel):
    """Payload for ConciergeStartedPayload"""

    course_id: Optional[int] = Field(default=None, ge=1, description='Course ID (may be omitted in legacy events)')
    thread_id: int = Field(ge=1, description='Thread ID for this conversation')
    task: str = Field(min_length=1, description='User\'s task/question')
    message_id: str = Field(description='Unique identifier for the assistant message (stable across tokens/completion)')
    continuation_of_message_id: Optional[str] = Field(default=None, description='For continuation courses, the message_id of the original course\'s message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging (copy from UI for fiche debugging)')

class ConciergeThinkingPayload(BaseModel):
    """Payload for ConciergeThinkingPayload"""

    message: str = Field(min_length=1, description='Thinking status message')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeTokenPayload(BaseModel):
    """Payload for ConciergeTokenPayload"""

    token: str = Field(description='LLM token (may be empty string)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeCompletePayload(BaseModel):
    """Payload for ConciergeCompletePayload"""

    result: str = Field(description='Final concierge result')
    status: Literal['success', 'cancelled'] = Field(description='Completion status (\'success\' for normal completion, \'cancelled\' for user-initiated cancellation)')
    duration_ms: Optional[int] = Field(default=None, ge=0, description='Execution duration in milliseconds')
    usage: Optional[UsageData] = Field(default=None)
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    fiche_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    debug_url: Optional[str] = Field(default=None, description='URL for debug/inspection')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeDeferredPayload(BaseModel):
    """Payload for ConciergeDeferredPayload"""

    message: str = Field(min_length=1, description='Deferred status message')
    attach_url: Optional[str] = Field(default=None, description='URL to re-attach to the running course execution')
    timeout_seconds: Optional[float] = Field(default=None, ge=0, description='Timeout that triggered deferral')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    fiche_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeWaitingPayload(BaseModel):
    """Payload for ConciergeWaitingPayload"""

    message: str = Field(min_length=1, description='Waiting status message (e.g., commis spawned)')
    job_id: Optional[int] = Field(default=None, ge=1, description='Commis job ID (if applicable)')
    close_stream: Optional[bool] = Field(default=None, description='If false, keep SSE stream open while waiting')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    fiche_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: Optional[int] = Field(default=None, ge=1, description='')
    message_id: Optional[str] = Field(default=None, description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeResumedPayload(BaseModel):
    """Payload for ConciergeResumedPayload"""

    course_id: Optional[int] = Field(default=None, ge=1, description='')
    fiche_id: Optional[int] = Field(default=None, ge=1, description='')
    thread_id: int = Field(ge=1, description='')
    message_id: str = Field(description='Unique identifier for the assistant message')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ErrorPayload(BaseModel):
    """Payload for ErrorPayload"""

    error: Optional[str] = Field(default=None, description='Error message')
    message: Optional[str] = Field(default=None, description='Alternative error message field')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisSpawnedPayload(BaseModel):
    """Payload for CommisSpawnedPayload"""

    job_id: int = Field(ge=1, description='Commis job ID')
    tool_call_id: Optional[str] = Field(default=None, description='Tool call ID for the spawn_commis invocation')
    task: str = Field(min_length=1, description='Commis task (may be truncated to 100 chars)')
    model: Optional[str] = Field(default=None, description='LLM model for commis')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisStartedPayload(BaseModel):
    """Payload for CommisStartedPayload"""

    job_id: int = Field(ge=1, description='')
    commis_id: str = Field(min_length=1, description='Commis execution ID')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    task: Optional[str] = Field(default=None, description='Commis task (may be truncated)')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisCompletePayload(BaseModel):
    """Payload for CommisCompletePayload"""

    job_id: int = Field(ge=1, description='')
    commis_id: Optional[str] = Field(default=None, description='Commis execution ID')
    status: CommisStatus
    duration_ms: Optional[int] = Field(default=None, ge=0, description='')
    error: Optional[str] = Field(default=None, description='Error message (only present if status=failed)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisSummaryReadyPayload(BaseModel):
    """Payload for CommisSummaryReadyPayload"""

    job_id: int = Field(ge=1, description='')
    commis_id: Optional[str] = Field(default=None, description='Commis execution ID')
    summary: str = Field(min_length=1, description='Extracted commis summary')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisToolStartedPayload(BaseModel):
    """Payload for CommisToolStartedPayload"""

    commis_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='LangChain tool call ID')
    tool_args_preview: Optional[str] = Field(default=None, description='Preview of tool arguments (may be truncated)')
    course_id: Optional[int] = Field(default=None, ge=1, description='Required for security (prevents cross-course leakage)')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisToolCompletedPayload(BaseModel):
    """Payload for CommisToolCompletedPayload"""

    commis_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    result_preview: Optional[str] = Field(default=None, description='Preview of tool result (may be truncated)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class CommisToolFailedPayload(BaseModel):
    """Payload for CommisToolFailedPayload"""

    commis_id: str = Field(min_length=1, description='')
    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    error: str = Field(min_length=1, description='Error message')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeToolStartedPayload(BaseModel):
    """Payload for ConciergeToolStartedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='Stable ID linking all events for this tool call')
    tool_args_preview: Optional[str] = Field(default=None, description='Preview of tool arguments (may be truncated)')
    tool_args: Optional[Dict[str, Any]] = Field(default=None, description='Full tool arguments (for persistence/raw view)')
    course_id: Optional[int] = Field(default=None, ge=1, description='Concierge course ID for correlation')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeToolProgressPayload(BaseModel):
    """Payload for ConciergeToolProgressPayload"""

    tool_call_id: str = Field(min_length=1, description='')
    message: str = Field(description='Progress message (log line)')
    level: Optional[Literal['debug', 'info', 'warn', 'error']] = Field(default=None, description='Log level for styling')
    progress_pct: Optional[int] = Field(default=None, ge=0, le=100, description='Optional progress percentage')
    data: Optional[Dict[str, Any]] = Field(default=None, description='Optional structured data (metrics, artifacts preview)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeToolCompletedPayload(BaseModel):
    """Payload for ConciergeToolCompletedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    result_preview: Optional[str] = Field(default=None, description='Condensed result for collapsed view')
    result: Optional[Dict[str, Any]] = Field(default=None, description='Full result (for persistence/raw view)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class ConciergeToolFailedPayload(BaseModel):
    """Payload for ConciergeToolFailedPayload"""

    tool_name: str = Field(min_length=1, description='')
    tool_call_id: str = Field(min_length=1, description='')
    duration_ms: int = Field(ge=0, description='')
    error: str = Field(min_length=1, description='Error message')
    error_details: Optional[Dict[str, Any]] = Field(default=None, description='Full error details (stack trace, context)')
    course_id: Optional[int] = Field(default=None, ge=1, description='')
    trace_id: Optional[str] = Field(default=None, description='End-to-end trace ID for debugging')

class SSEEventType(str, Enum):
    """Enumeration of all SSE event types."""

    CONNECTED = "connected"
    HEARTBEAT = "heartbeat"
    CONCIERGE_STARTED = "concierge_started"
    CONCIERGE_THINKING = "concierge_thinking"
    CONCIERGE_TOKEN = "concierge_token"
    CONCIERGE_COMPLETE = "concierge_complete"
    CONCIERGE_DEFERRED = "concierge_deferred"
    CONCIERGE_WAITING = "concierge_waiting"
    CONCIERGE_RESUMED = "concierge_resumed"
    ERROR = "error"
    COMMIS_SPAWNED = "commis_spawned"
    COMMIS_STARTED = "commis_started"
    COMMIS_COMPLETE = "commis_complete"
    COMMIS_SUMMARY_READY = "commis_summary_ready"
    COMMIS_TOOL_STARTED = "commis_tool_started"
    COMMIS_TOOL_COMPLETED = "commis_tool_completed"
    COMMIS_TOOL_FAILED = "commis_tool_failed"
    CONCIERGE_TOOL_STARTED = "concierge_tool_started"
    CONCIERGE_TOOL_PROGRESS = "concierge_tool_progress"
    CONCIERGE_TOOL_COMPLETED = "concierge_tool_completed"
    CONCIERGE_TOOL_FAILED = "concierge_tool_failed"


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
