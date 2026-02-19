"""Oikos run history endpoints."""

import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.models.enums import RunStatus
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.models.models import ThreadMessage
from zerg.models.run_event import RunEvent
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


class OikosRunSummary(UTCBaseModel):
    """Minimal run summary for Oikos Task Inbox."""

    id: int
    fiche_id: int
    thread_id: Optional[int] = None
    fiche_name: str
    status: str
    summary: Optional[str] = None
    signal: Optional[str] = None
    signal_source: Optional[str] = None
    error: Optional[str] = None
    last_event_type: Optional[str] = None
    last_event_message: Optional[str] = None
    last_event_at: Optional[datetime] = None
    continuation_of_run_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/runs", response_model=List[OikosRunSummary])
def list_oikos_runs(
    limit: int = 50,
    fiche_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[OikosRunSummary]:
    """List recent fiche runs for Oikos Task Inbox.

    Returns recent run history with summaries, filtered by fiche if specified.
    This powers the Task Inbox UI in Oikos showing all automated activity.

    Args:
        limit: Maximum number of runs to return (default 50)
        fiche_id: Optional filter by specific fiche
        db: Database session
        current_user: Authenticated user (Oikos service account)

    Returns:
        List of run summaries ordered by created_at descending
    """
    # Get recent runs
    # TODO: Add crud method for filtering by fiche_id and ordering by created_at
    # For now, get all runs and filter/sort in memory

    # Multi-tenant SaaS: Oikos shows only the logged-in user's runs.
    query = db.query(Run).options(selectinload(Run.fiche)).join(Fiche, Fiche.id == Run.fiche_id).filter(Fiche.owner_id == current_user.id)

    if fiche_id:
        query = query.filter(Run.fiche_id == fiche_id)

    runs = query.order_by(Run.created_at.desc()).limit(limit).all()

    run_ids = [run.id for run in runs]
    thread_ids = [run.thread_id for run in runs if run.thread_id]

    last_events_by_run = _get_latest_run_events(db, run_ids)
    last_messages_by_thread = _get_latest_assistant_messages(db, thread_ids)

    summaries = []
    for run in runs:
        fiche_name = run.fiche.name if run.fiche else f"Fiche {run.fiche_id}"

        # Extract summary from run (will be populated in Phase 2.3)
        summary = getattr(run, "summary", None)

        last_event = last_events_by_run.get(run.id)
        last_event_type = getattr(last_event, "event_type", None) if last_event else None
        last_event_at = getattr(last_event, "created_at", None) if last_event else None
        last_event_message = _extract_event_message(getattr(last_event, "payload", None)) if last_event else None

        signal = summary if summary else None
        signal_source = "summary" if summary else None

        if not signal:
            run_error = getattr(run, "error", None)
            if run_error:
                signal = run_error
                signal_source = "error"

        if not signal and run.thread_id:
            last_message = last_messages_by_thread.get(run.thread_id)
            if last_message:
                signal = last_message
                signal_source = "last_message"

        if not signal and last_event_message:
            signal = last_event_message
            signal_source = "last_event"

        signal = _truncate_signal(signal, 240)

        summaries.append(
            OikosRunSummary(
                id=run.id,
                fiche_id=run.fiche_id,
                thread_id=run.thread_id,
                fiche_name=fiche_name,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                summary=summary,
                signal=signal,
                signal_source=signal_source,
                error=getattr(run, "error", None),
                last_event_type=last_event_type,
                last_event_message=last_event_message,
                last_event_at=last_event_at,
                continuation_of_run_id=getattr(run, "continuation_of_run_id", None),
                created_at=run.created_at,
                updated_at=run.updated_at,
                completed_at=run.finished_at,
            )
        )

    return summaries


def _extract_text_from_message_content(content: Any) -> Optional[str]:
    """Extract text from ThreadMessage content payloads."""
    if not content:
        return None

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
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts) if text_parts else None

    return str(content) if content else None


def _get_last_assistant_message(db: Session, thread_id: int) -> Optional[str]:
    """Get the last assistant message from a thread.

    Args:
        db: Database session
        thread_id: Thread ID to query

    Returns:
        Content of the last assistant message, or None if not found
    """

    last_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.desc())
        .first()
    )

    if not last_msg or not last_msg.content:
        return None

    return _extract_text_from_message_content(last_msg.content)


def _get_latest_assistant_messages(db: Session, thread_ids: List[int]) -> Dict[int, str]:
    if not thread_ids:
        return {}

    subquery = (
        db.query(
            ThreadMessage.thread_id.label("thread_id"),
            ThreadMessage.content.label("content"),
            func.row_number()
            .over(
                partition_by=ThreadMessage.thread_id,
                order_by=ThreadMessage.id.desc(),
            )
            .label("rn"),
        )
        .filter(ThreadMessage.thread_id.in_(thread_ids))
        .filter(ThreadMessage.role == "assistant")
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    output: Dict[int, str] = {}
    for row in rows:
        text = _extract_text_from_message_content(row.content)
        if text:
            output[row.thread_id] = text
    return output


def _get_latest_run_events(db: Session, run_ids: List[int]) -> Dict[int, Any]:
    if not run_ids:
        return {}

    subquery = (
        db.query(
            RunEvent.run_id.label("run_id"),
            RunEvent.event_type.label("event_type"),
            RunEvent.payload.label("payload"),
            RunEvent.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=RunEvent.run_id,
                order_by=RunEvent.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(RunEvent.run_id.in_(run_ids))
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    return {row.run_id: row for row in rows}


def _extract_event_message(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload or not isinstance(payload, dict):
        return None

    for key in ("message", "summary", "error", "result", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return f"Tool: {tool_name}"

    return None


def _truncate_signal(signal: Optional[str], max_length: int) -> Optional[str]:
    if not signal:
        return signal
    normalized = " ".join(signal.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"


class RunStatusResponse(UTCBaseModel):
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
    current_user=Depends(get_current_oikos_user),
):
    """Get the user's currently running oikos run (if any).

    Returns the most recent RUNNING, WAITING, or DEFERRED run for the user's oikos fiche.
    Returns 204 No Content if no active run exists.

    This endpoint enables run reconnection after page refresh.

    Args:
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        JSONResponse with run details if found, or 204 No Content
    """
    # Import here to avoid circular dependency
    from zerg.services.oikos_service import OikosService

    oikos_service = OikosService(db)
    oikos_fiche = oikos_service.get_or_create_oikos_fiche(current_user.id)

    # Prefer RUNNING runs. DEFERRED runs are "in-flight" only if they have not
    # already produced a successful continuation.
    active_run = (
        db.query(Run).filter(Run.fiche_id == oikos_fiche.id).filter(Run.status == RunStatus.RUNNING).order_by(Run.created_at.desc()).first()
    )

    if not active_run:
        # WAITING runs are interrupted via spawn_commis (oikos resume).
        active_run = (
            db.query(Run)
            .filter(Run.fiche_id == oikos_fiche.id)
            .filter(Run.status == RunStatus.WAITING)
            .order_by(Run.created_at.desc())
            .first()
        )

    if not active_run:
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        from zerg.models.enums import RunTrigger

        Continuation = aliased(Run)
        has_terminal_continuation = exists().where(
            (Continuation.continuation_of_run_id == Run.id)
            & (Continuation.trigger == RunTrigger.CONTINUATION)
            & (Continuation.status.in_([RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED]))
        )

        active_run = (
            db.query(Run)
            .filter(Run.fiche_id == oikos_fiche.id)
            .filter(Run.status == RunStatus.DEFERRED)
            .filter(~has_terminal_continuation)
            .order_by(Run.created_at.desc())
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
    current_user=Depends(get_current_oikos_user),
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
    run = db.query(Run).join(Fiche, Fiche.id == Run.fiche_id).filter(Run.id == run_id).filter(Fiche.owner_id == current_user.id).first()

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
    current_user=Depends(get_current_oikos_user),
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
        run = db.query(Run).join(Fiche, Fiche.id == Run.fiche_id).filter(Run.id == run_id).filter(Fiche.owner_id == current_user.id).first()

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
        from zerg.routers.oikos_sse import stream_run_events

        return EventSourceResponse(
            stream_run_events(
                run_id=run_id_val,
                owner_id=current_user.id,
            )
        )
    else:
        # Run is complete/failed - return single completion event and close
        async def completed_stream():
            # Send completion event matching the format from oikos_sse.py
            event_type = "oikos_complete" if run_status == RunStatus.SUCCESS else "error"
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
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            }

        return EventSourceResponse(completed_stream())


class RunEventRecord(UTCBaseModel):
    """Single event from a run."""

    id: int
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime


class RunEventsResponse(BaseModel):
    """Response for run events query."""

    run_id: int
    events: List[RunEventRecord]
    total: int


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(
    run_id: int,
    event_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> RunEventsResponse:
    """Get events for a specific run.

    Returns events stored during run execution, optionally filtered by type.
    This endpoint is useful for E2E testing to verify tool calls and lifecycle events.

    Args:
        run_id: ID of the run to query
        event_type: Optional filter by event type (e.g., "oikos_tool_started")
        limit: Maximum number of events to return (default 100)
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        List of events for the run

    Raises:
        HTTPException: 404 if run not found or not owned by user
    """
    # Multi-tenant security: only return runs owned by the current user
    run = db.query(Run).join(Fiche, Fiche.id == Run.fiche_id).filter(Run.id == run_id).filter(Fiche.owner_id == current_user.id).first()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query events
    query = db.query(RunEvent).filter(RunEvent.run_id == run_id)

    if event_type:
        query = query.filter(RunEvent.event_type == event_type)

    events = query.order_by(RunEvent.created_at).limit(limit).all()

    return RunEventsResponse(
        run_id=run_id,
        events=[
            RunEventRecord(
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
    oikos_thinking_ms: Optional[int] = None
    commis_execution_ms: Optional[int] = None
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
    current_user=Depends(get_current_oikos_user),
) -> TimelineResponse:
    """Get timing timeline for a specific run.

    Returns structured timing data with phase-based events and summary statistics.
    This endpoint powers performance profiling and observability for Oikos chat.

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
    run = db.query(Run).join(Fiche, Fiche.id == Run.fiche_id).filter(Run.id == run_id).filter(Fiche.owner_id == current_user.id).first()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query all events for this run, ordered by created_at
    events = db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.created_at).all()

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
    oikos_started_time: Optional[datetime] = None
    oikos_complete_time: Optional[datetime] = None
    commis_spawned_time: Optional[datetime] = None
    commis_complete_time: Optional[datetime] = None
    first_tool_time: Optional[datetime] = None
    last_tool_time: Optional[datetime] = None

    for event in events:
        if event.event_type == "oikos_started" and not oikos_started_time:
            oikos_started_time = event.created_at
        elif event.event_type == "oikos_complete" and not oikos_complete_time:
            oikos_complete_time = event.created_at
        elif event.event_type == "commis_spawned" and not commis_spawned_time:
            commis_spawned_time = event.created_at
        elif event.event_type == "commis_complete" and not commis_complete_time:
            commis_complete_time = event.created_at
        elif event.event_type == "tool_started" and not first_tool_time:
            first_tool_time = event.created_at
        elif event.event_type in ("tool_completed", "tool_failed"):
            last_tool_time = event.created_at

    # Calculate derived metrics
    oikos_thinking_ms = None
    if oikos_started_time and commis_spawned_time:
        oikos_thinking_ms = int((commis_spawned_time - oikos_started_time).total_seconds() * 1000)

    commis_execution_ms = None
    if commis_spawned_time and commis_complete_time:
        commis_execution_ms = int((commis_complete_time - commis_spawned_time).total_seconds() * 1000)

    tool_execution_ms = None
    if first_tool_time and last_tool_time:
        tool_execution_ms = int((last_tool_time - first_tool_time).total_seconds() * 1000)

    summary = TimelineSummary(
        total_duration_ms=total_duration_ms,
        oikos_thinking_ms=oikos_thinking_ms,
        commis_execution_ms=commis_execution_ms,
        tool_execution_ms=tool_execution_ms,
    )

    return TimelineResponse(
        correlation_id=run.correlation_id,
        run_id=run_id,
        events=timeline_events,
        summary=summary,
    )
