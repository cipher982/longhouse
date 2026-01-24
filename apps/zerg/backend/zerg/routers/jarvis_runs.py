"""Jarvis run history endpoints."""

import json
import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.models.agent_run_event import AgentRunEvent
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
from zerg.routers.jarvis_auth import get_current_jarvis_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisRunSummary(BaseModel):
    """Minimal run summary for Jarvis Task Inbox."""

    id: int
    agent_id: int
    thread_id: Optional[int] = None
    agent_name: str
    status: str
    summary: Optional[str] = None
    continuation_of_run_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/runs", response_model=List[JarvisRunSummary])
def list_jarvis_runs(
    limit: int = 50,
    agent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> List[JarvisRunSummary]:
    """List recent agent runs for Jarvis Task Inbox.

    Returns recent run history with summaries, filtered by agent if specified.
    This powers the Task Inbox UI in Jarvis showing all automated activity.

    Args:
        limit: Maximum number of runs to return (default 50)
        agent_id: Optional filter by specific agent
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        List of run summaries ordered by created_at descending
    """
    # Get recent runs
    # TODO: Add crud method for filtering by agent_id and ordering by created_at
    # For now, get all runs and filter/sort in memory

    # Multi-tenant SaaS: Jarvis shows only the logged-in user's runs.
    query = (
        db.query(AgentRun)
        .options(selectinload(AgentRun.agent))
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(Agent.owner_id == current_user.id)
    )

    if agent_id:
        query = query.filter(AgentRun.agent_id == agent_id)

    runs = query.order_by(AgentRun.created_at.desc()).limit(limit).all()

    summaries = []
    for run in runs:
        agent_name = run.agent.name if run.agent else f"Agent {run.agent_id}"

        # Extract summary from run (will be populated in Phase 2.3)
        summary = getattr(run, "summary", None)

        summaries.append(
            JarvisRunSummary(
                id=run.id,
                agent_id=run.agent_id,
                thread_id=run.thread_id,
                agent_name=agent_name,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                summary=summary,
                continuation_of_run_id=getattr(run, "continuation_of_run_id", None),
                created_at=run.created_at,
                updated_at=run.updated_at,
                completed_at=run.finished_at,
            )
        )

    return summaries


def _get_last_assistant_message(db: Session, thread_id: int) -> Optional[str]:
    """Get the last assistant message from a thread.

    Args:
        db: Database session
        thread_id: Thread ID to query

    Returns:
        Content of the last assistant message, or None if not found
    """
    import json

    last_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.desc())
        .first()
    )

    if not last_msg or not last_msg.content:
        return None

    content = last_msg.content

    # Handle string content (most common case)
    if isinstance(content, str):
        # Try to parse as JSON if it looks like structured content
        if content.startswith("[") or content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    # Handle array of content blocks
                    text_parts = []
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    return " ".join(text_parts) if text_parts else content
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON, return as-is
        return content

    # Handle native list (if column supports JSON type)
    elif isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts) if text_parts else None

    return str(content) if content else None


class RunStatusResponse(BaseModel):
    """Detailed status of a specific run."""

    run_id: int
    status: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[str] = None


@router.get("/runs/active")
def get_active_run(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
):
    """Get the user's currently running agent run (if any).

    Returns the most recent RUNNING, WAITING, or DEFERRED run for the user's supervisor agent.
    Returns 204 No Content if no active run exists.

    This endpoint enables run reconnection after page refresh.

    Args:
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        JSONResponse with run details if found, or 204 No Content
    """
    # Import here to avoid circular dependency
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)
    supervisor_agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)

    # Prefer RUNNING runs. DEFERRED runs are "in-flight" only if they have not
    # already produced a successful continuation.
    active_run = (
        db.query(AgentRun)
        .filter(AgentRun.agent_id == supervisor_agent.id)
        .filter(AgentRun.status == RunStatus.RUNNING)
        .order_by(AgentRun.created_at.desc())
        .first()
    )

    if not active_run:
        # WAITING runs are interrupted via spawn_worker (supervisor resume).
        active_run = (
            db.query(AgentRun)
            .filter(AgentRun.agent_id == supervisor_agent.id)
            .filter(AgentRun.status == RunStatus.WAITING)
            .order_by(AgentRun.created_at.desc())
            .first()
        )

    if not active_run:
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        from zerg.models.enums import RunTrigger

        Continuation = aliased(AgentRun)
        has_terminal_continuation = exists().where(
            (Continuation.continuation_of_run_id == AgentRun.id)
            & (Continuation.trigger == RunTrigger.CONTINUATION)
            & (Continuation.status.in_([RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED]))
        )

        active_run = (
            db.query(AgentRun)
            .filter(AgentRun.agent_id == supervisor_agent.id)
            .filter(AgentRun.status == RunStatus.DEFERRED)
            .filter(~has_terminal_continuation)
            .order_by(AgentRun.created_at.desc())
            .first()
        )

    if not active_run:
        # No active run - return 204 No Content
        return JSONResponse(status_code=204, content=None)

    # Return run details for reconnection
    return JSONResponse(
        {
            "run_id": active_run.id,
            "status": active_run.status.value,
            "created_at": active_run.created_at.isoformat(),
        }
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run_status(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> RunStatusResponse:
    """Get current status of a specific run.

    Returns detailed status including timing, errors, and result if completed.
    This endpoint is used for polling run status after async task submission.

    Args:
        run_id: ID of the run to query
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        Run status with result if completed

    Raises:
        HTTPException: 404 if run not found or not owned by user
    """
    # Multi-tenant security: only return runs owned by the current user
    run = (
        db.query(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.id == run_id)
        .filter(Agent.owner_id == current_user.id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Include result only if run succeeded
    result = None
    if run.status == RunStatus.SUCCESS:
        result = _get_last_assistant_message(db, run.thread_id)

    return RunStatusResponse(
        run_id=run.id,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        created_at=run.created_at,
        finished_at=run.finished_at,
        error=run.error,
        result=result,
    )


@router.get("/runs/{run_id}/stream")
async def attach_to_run_stream(
    run_id: int,
    current_user=Depends(get_current_jarvis_user),
):
    """Attach to an existing run's event stream.

    For RUNNING runs: Streams events via SSE as they occur.
    For completed runs: Returns a single completion event and closes.

    This enables run reconnection after page refresh.

    Args:
        run_id: ID of the run to attach to
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        EventSourceResponse for SSE streaming
    """
    from zerg.database import db_session

    # CRITICAL: Use SHORT-LIVED session for security check and data retrieval
    # Don't use Depends(get_db) - it holds the session open for the entire
    # SSE stream duration, blocking TRUNCATE during E2E resets.
    with db_session() as db:
        # Multi-tenant security: only return runs owned by the current user
        run = (
            db.query(AgentRun)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .filter(AgentRun.id == run_id)
            .filter(Agent.owner_id == current_user.id)
            .first()
        )

        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Capture all values we need before session closes
        run_id_val = run.id
        run_status = run.status
        run_error = run.error
        run_finished_at = run.finished_at
        thread_id = run.thread_id

        # For completed runs, get result now (while session is open)
        result = None
        if run_status in (RunStatus.SUCCESS, RunStatus.FAILED):
            result = _get_last_assistant_message(db, thread_id)
    # Session is now closed - no DB connection held during streaming

    # Check run status
    if run_status == RunStatus.RUNNING:
        # Stream live events using existing stream_run_events
        from zerg.routers.jarvis_sse import stream_run_events

        return EventSourceResponse(
            stream_run_events(
                run_id=run_id_val,
                owner_id=current_user.id,
            )
        )
    else:
        # Run is complete/failed - return single completion event and close
        async def completed_stream():
            # Send completion event matching the format from jarvis_sse.py
            event_type = "supervisor_complete" if run_status == RunStatus.SUCCESS else "error"
            payload = {
                "run_id": run_id_val,
                "status": run_status.value,
                "result": result,
                "error": run_error,
                "finished_at": run_finished_at.isoformat() if run_finished_at else None,
            }

            yield {
                "event": event_type,
                "data": json.dumps(
                    {
                        "type": event_type,
                        "payload": payload,
                        "timestamp": datetime.now().isoformat(),
                    }
                ),
            }

        return EventSourceResponse(completed_stream())


class RunEvent(BaseModel):
    """Single event from a run."""

    id: int
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime


class RunEventsResponse(BaseModel):
    """Response for run events query."""

    run_id: int
    events: List[RunEvent]
    total: int


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(
    run_id: int,
    event_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> RunEventsResponse:
    """Get events for a specific run.

    Returns events stored during run execution, optionally filtered by type.
    This endpoint is useful for E2E testing to verify tool calls and lifecycle events.

    Args:
        run_id: ID of the run to query
        event_type: Optional filter by event type (e.g., "supervisor_tool_started")
        limit: Maximum number of events to return (default 100)
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        List of events for the run

    Raises:
        HTTPException: 404 if run not found or not owned by user
    """
    # Multi-tenant security: only return runs owned by the current user
    run = (
        db.query(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.id == run_id)
        .filter(Agent.owner_id == current_user.id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query events
    query = db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id)

    if event_type:
        query = query.filter(AgentRunEvent.event_type == event_type)

    events = query.order_by(AgentRunEvent.created_at).limit(limit).all()

    return RunEventsResponse(
        run_id=run_id,
        events=[
            RunEvent(
                id=event.id,
                event_type=event.event_type,
                payload=event.payload or {},
                created_at=event.created_at,
            )
            for event in events
        ],
        total=len(events),
    )


class TimelineEvent(BaseModel):
    """Single event in a timeline with timing information."""

    phase: str
    timestamp: str  # ISO 8601
    offset_ms: int
    metadata: Optional[Dict[str, Any]] = None


class TimelineSummary(BaseModel):
    """Timing summary for a run."""

    total_duration_ms: int
    supervisor_thinking_ms: Optional[int] = None
    worker_execution_ms: Optional[int] = None
    tool_execution_ms: Optional[int] = None


class TimelineResponse(BaseModel):
    """Full timeline response for a run."""

    correlation_id: Optional[str]
    run_id: int
    events: List[TimelineEvent]
    summary: TimelineSummary


@router.get("/runs/{run_id}/timeline", response_model=TimelineResponse)
def get_run_timeline(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> TimelineResponse:
    """Get timing timeline for a specific run.

    Returns structured timing data with phase-based events and summary statistics.
    This endpoint powers performance profiling and observability for Jarvis chat.

    Args:
        run_id: ID of the run to query
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        Timeline with events and timing summary

    Raises:
        HTTPException: 404 if run not found or not owned by user
    """
    # Multi-tenant security: only return runs owned by the current user
    run = (
        db.query(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.id == run_id)
        .filter(Agent.owner_id == current_user.id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query all events for this run, ordered by created_at
    events = db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.created_at).all()

    if not events:
        # No events yet - return empty timeline
        return TimelineResponse(
            correlation_id=run.correlation_id,
            run_id=run_id,
            events=[],
            summary=TimelineSummary(total_duration_ms=0),
        )

    # Calculate offsets from first event
    first_timestamp = events[0].created_at
    timeline_events = []

    for event in events:
        offset_ms = int((event.created_at - first_timestamp).total_seconds() * 1000)
        timeline_events.append(
            TimelineEvent(
                phase=event.event_type,
                timestamp=event.created_at.isoformat(),
                offset_ms=offset_ms,
                metadata=event.payload if event.payload else None,
            )
        )

    # Calculate summary statistics
    last_timestamp = events[-1].created_at
    total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

    # Find key phase transitions for summary
    supervisor_started_time: Optional[datetime] = None
    supervisor_complete_time: Optional[datetime] = None
    worker_spawned_time: Optional[datetime] = None
    worker_complete_time: Optional[datetime] = None
    first_tool_time: Optional[datetime] = None
    last_tool_time: Optional[datetime] = None

    for event in events:
        if event.event_type == "supervisor_started" and not supervisor_started_time:
            supervisor_started_time = event.created_at
        elif event.event_type == "supervisor_complete" and not supervisor_complete_time:
            supervisor_complete_time = event.created_at
        elif event.event_type == "worker_spawned" and not worker_spawned_time:
            worker_spawned_time = event.created_at
        elif event.event_type == "worker_complete" and not worker_complete_time:
            worker_complete_time = event.created_at
        elif event.event_type == "tool_started" and not first_tool_time:
            first_tool_time = event.created_at
        elif event.event_type in ("tool_completed", "tool_failed"):
            last_tool_time = event.created_at

    # Calculate derived metrics
    supervisor_thinking_ms = None
    if supervisor_started_time and worker_spawned_time:
        supervisor_thinking_ms = int((worker_spawned_time - supervisor_started_time).total_seconds() * 1000)

    worker_execution_ms = None
    if worker_spawned_time and worker_complete_time:
        worker_execution_ms = int((worker_complete_time - worker_spawned_time).total_seconds() * 1000)

    tool_execution_ms = None
    if first_tool_time and last_tool_time:
        tool_execution_ms = int((last_tool_time - first_tool_time).total_seconds() * 1000)

    summary = TimelineSummary(
        total_duration_ms=total_duration_ms,
        supervisor_thinking_ms=supervisor_thinking_ms,
        worker_execution_ms=worker_execution_ms,
        tool_execution_ms=tool_execution_ms,
    )

    return TimelineResponse(
        correlation_id=run.correlation_id,
        run_id=run_id,
        events=timeline_events,
        summary=summary,
    )
