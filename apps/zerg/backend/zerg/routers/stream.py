"""Resumable SSE streaming.

Supports replay (historical events from RunEvent) + live streaming (EventBus).
Clients reconnect and catch up on missed events, then continue live.
Handles DEFERRED runs (streamable, not treated as complete).
- SSE format with id: field for client resumption
- Token filtering support
- SHORT-LIVED DB sessions for replay (critical for test isolation)
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sse_starlette.sse import EventSourceResponse

from zerg.database import db_session
from zerg.database import get_test_commis_id
from zerg.models.enums import RunStatus
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.services.run_stream import encode_connected_sse
from zerg.services.run_stream import stream_run_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])


# Backpressure: max events to buffer per client before closing stream
# Client should reconnect with Last-Event-ID for resumable replay
STREAM_QUEUE_MAX_SIZE = 1000


async def _replay_and_stream(
    run_id: int,
    owner_id: int,
    status: RunStatus,
    after_event_id: int,
    include_tokens: bool,
    *,
    include_replay: bool = True,
    allow_continuation_runs: bool = False,
    test_commis_id: str | None = None,
):
    async for event in stream_run_events(
        run_id=run_id,
        owner_id=owner_id,
        status=status,
        after_event_id=after_event_id,
        include_tokens=include_tokens,
        include_replay=include_replay,
        allow_continuation_runs=allow_continuation_runs,
        test_commis_id=test_commis_id,
        queue_max_size=STREAM_QUEUE_MAX_SIZE,
    ):
        yield event


async def stream_run_events_live(
    run_id: int,
    owner_id: int,
    *,
    test_commis_id: str | None = None,
):
    """Stream run events for Oikos chat with a replay-first bootstrap.

    This is a convenience wrapper around _replay_and_stream for the Oikos chat
    use case. It:
    - Emits an initial ``connected`` event so the client knows the stream is open
    - Replays durable lifecycle events already persisted for the run
    - Supports continuation run aliasing (follow-up oikos runs)
    - Continues with live events after replay

    Replay matters here because ``invoke_oikos()`` can persist ``oikos_started``
    before the browser finishes subscribing to SSE. Without replay the first
    lifecycle event races the connection and disappears, which breaks the
    frontend's stable ``message_id`` contract.

    Args:
        run_id: Run identifier
        owner_id: Owner ID for security filtering

    Yields:
        SSE events in format: {"event": str, "data": str}
    """
    effective_test_commis_id = test_commis_id if test_commis_id is not None else get_test_commis_id()

    yield encode_connected_sse(run_id)

    # Use RUNNING status to allow the helper to stream replay + live events.
    # The helper closes once the run reaches a terminal state.
    async for event in _replay_and_stream(
        run_id=run_id,
        owner_id=owner_id,
        status=RunStatus.RUNNING,
        after_event_id=0,
        include_tokens=True,
        include_replay=True,
        allow_continuation_runs=True,
        test_commis_id=effective_test_commis_id,
    ):
        yield event


@router.get("/runs/{run_id}")
async def stream_run_replay(
    run_id: int,
    request: Request,
    after_event_id: int = 0,
    include_tokens: bool = True,
    current_user=Depends(get_current_oikos_user),
):
    """Stream run events with replay support (Resumable SSE v1).

    This endpoint enables clients to reconnect and catch up on missed events by:
    1. Replaying historical events from the database
    2. Continuing with live events via EventBus

    For completed runs: Replays all events and closes the stream.
    For active runs (RUNNING/DEFERRED): Replays historical + streams live events.

    Args:
        run_id: Run identifier
        request: HTTP request (for Last-Event-ID header)
        after_event_id: Resume from this event ID (0 = from start)
        include_tokens: Whether to include OIKOS_TOKEN events (default: true)
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        EventSourceResponse for SSE streaming

    Raises:
        HTTPException: 404 if run not found or not owned by user

    SSE Format:
        id: {event.id}
        event: {event.event_type}
        data: {"type": "...", "payload": {...}, "timestamp": "..."}

    Examples:
        # Start from beginning
        GET /api/stream/runs/123

        # Resume from last-event-id (standard SSE reconnect)
        GET /api/stream/runs/123
        Last-Event-ID: 456

        # Resume from specific event ID
        GET /api/stream/runs/123?after_event_id=456

        # Skip token events (for bandwidth optimization)
        GET /api/stream/runs/123?include_tokens=false
    """
    # Security: verify ownership using SHORT-LIVED session
    # CRITICAL: Don't use Depends(get_db) here - it holds the session open
    # for the entire SSE stream duration, blocking TRUNCATE during E2E resets.
    with db_session() as db:
        run = (
            db.query(Run)
            .join(Fiche, Fiche.id == Run.fiche_id)
            .filter(Run.id == run_id)
            .filter(Fiche.owner_id == current_user.id)
            .first()
        )

        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Capture values we need before session closes
        run_status = run.status
    # Session is now closed - no DB connection held during streaming

    # Handle Last-Event-ID header (SSE standard for automatic reconnect)
    # This takes precedence over query params
    last_event_id_header = request.headers.get("Last-Event-ID")
    if last_event_id_header:
        try:
            after_event_id = int(last_event_id_header)
            logger.debug(f"Resuming from Last-Event-ID header: {after_event_id}")
        except ValueError:
            logger.warning(f"Invalid Last-Event-ID header: {last_event_id_header}")

    logger.info(
        f"Streaming run {run_id} (status={run_status.value}, "
        f"after_event_id={after_event_id}, "
        f"include_tokens={include_tokens})"
    )

    return EventSourceResponse(
        _replay_and_stream(
            run_id=run_id,
            owner_id=current_user.id,
            status=run_status,
            after_event_id=after_event_id,
            include_tokens=include_tokens,
            test_commis_id=get_test_commis_id(),
        )
    )
