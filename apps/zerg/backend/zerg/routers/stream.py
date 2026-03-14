"""Resumable SSE streaming.

Supports replay (historical events from RunEvent) + live streaming (EventBus).
Clients reconnect and catch up on missed events, then continue live.
Handles DEFERRED runs (streamable, not treated as complete).
- SSE format with id: field for client resumption
- Token filtering support
- SHORT-LIVED DB sessions for replay (critical for test isolation)
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sse_starlette.sse import EventSourceResponse

from zerg.database import db_session
from zerg.database import get_test_commis_id
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.enums import RunStatus
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.services.run_stream import load_historical_run_events
from zerg.services.run_stream import with_test_commis_routing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])


def _json_default(value):  # type: ignore[no-untyped-def]
    """Fallback serializer for SSE payloads.

    Some event payloads may contain datetime objects (or other non-JSON-safe
    values). We prefer emitting a string rather than crashing the SSE stream.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    return str(value)


# Backpressure: max events to buffer per client before closing stream
# Client should reconnect with Last-Event-ID for resumable replay
STREAM_QUEUE_MAX_SIZE = 1000

# Maximum TTL for stream_control:keep_open (5 minutes)
MAX_STREAM_TTL_MS = 300_000


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
    """Generator that optionally replays historical events, then streams live.

    This is the unified SSE streaming implementation used by both:
    - /api/stream/runs/{run_id} - resumable SSE with replay
    - /api/oikos/chat - live-only SSE for initial chat

    The function can operate in two modes:
    1. Replay + Live (include_replay=True): Load historical events, then stream live
    2. Live-only (include_replay=False): Stream live events only (oikos chat)

    IMPORTANT: This function does NOT hold a DB connection open during streaming.
    Historical events are loaded into memory first, then the DB session is closed
    before any SSE events are yielded.

    Backpressure: The queue is bounded to STREAM_QUEUE_MAX_SIZE events. If a slow
    client causes overflow, the stream closes gracefully. The client should reconnect
    with Last-Event-ID to resume from the durable event store.

    Args:
        run_id: Run identifier
        owner_id: Owner ID for security filtering
        status: Current run status (RUNNING, DEFERRED, SUCCESS, etc.)
        after_event_id: Resume from this event ID (0 = from start)
        include_tokens: Whether to include OIKOS_TOKEN events
        include_replay: If True, replay historical events before streaming live
        allow_continuation_runs: If True, also stream events from continuation runs

    Yields:
        SSE events in format: {"id": str, "event": str, "data": str}
    """
    # 1. Subscribe to live events FIRST (before replaying) to avoid race condition
    # Bounded queue for backpressure - overflow triggers graceful stream closure
    queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAX_SIZE)
    last_sent_event_id = 0
    pending_commiss = 0
    oikos_done = False
    saw_oikos_complete = False
    continuation_active = False
    awaiting_continuation_until: float | None = None
    commis_grace_seconds = 5.0
    overflow_event = asyncio.Event()  # Signal overflow without queue sentinel
    continuation_cache: dict[int, bool] = {}  # Cache for continuation run lookups

    # Stream control state (explicit lifecycle management)
    close_event_id: int | None = None  # event_id of stream_control:close (if seen)
    stream_lease_until: float | None = None  # TTL expiry timestamp from keep_open

    def _apply_event_state(event_type: str, event: dict, *, from_replay: bool = False) -> None:
        """Update stream lifecycle state from an event."""
        nonlocal \
            pending_commiss, \
            oikos_done, \
            saw_oikos_complete, \
            continuation_active, \
            awaiting_continuation_until, \
            complete
        nonlocal close_event_id, stream_lease_until

        # Handle stream_control events (explicit lifecycle control)
        if event_type == "stream_control":
            action = event.get("action")
            ttl_ms = event.get("ttl_ms")
            current_event_id = event.get("event_id") or event.get("_event_id")

            if action == "close":
                close_event_id = current_event_id
                logger.debug(f"Stream control: close marker set at event_id={close_event_id} for run {run_id}")
                # Don't set complete=True yet - wait until we've streamed up to this event
                return

            if action == "keep_open":
                # Extend lease (or set initial) - only for live events, not replay
                if ttl_ms and not from_replay:
                    capped_ttl = min(ttl_ms, MAX_STREAM_TTL_MS)
                    stream_lease_until = time.monotonic() + (capped_ttl / 1000.0)
                    logger.debug(f"Stream control: lease extended to {capped_ttl}ms for run {run_id}")
                # Cancel any pending close from heuristics
                awaiting_continuation_until = None
                return

        if event_type == "commis_spawned":
            pending_commiss += 1
            # Cancel any pending close while new commiss are active.
            awaiting_continuation_until = None
            return

        if event_type in ("commis_complete", "commis_summary_ready"):
            if pending_commiss > 0:
                pending_commiss -= 1

            # If oikos already finished, start a short grace window
            # to allow the inbox continuation to begin streaming.
            if pending_commiss == 0 and oikos_done and not continuation_active and not from_replay:
                if awaiting_continuation_until is None:
                    awaiting_continuation_until = time.monotonic() + commis_grace_seconds
            return

        if event_type == "oikos_started":
            if saw_oikos_complete:
                continuation_active = True
            oikos_done = False
            awaiting_continuation_until = None
            return

        if event_type == "oikos_complete":
            saw_oikos_complete = True
            oikos_done = True
            if continuation_active:
                continuation_active = False
            # If commiss are still pending, keep stream open for their events.
            if pending_commiss == 0 and not from_replay:
                complete = True
            return

        if event_type == "oikos_deferred":
            # DEFERRED states waiting for commis continuations may keep the
            # stream open so the connected client receives the final answer.
            if event.get("close_stream", True):
                complete = True
            return

        if event_type == "error":
            complete = True

    def _is_continuation_of_run(candidate_run_id: int) -> bool:
        """Return True if candidate_run_id is a continuation of run_id (including chains).

        Uses root_run_id for chain traversal so continuation-of-continuation
        chains alias back to the original run's SSE stream.
        """
        if candidate_run_id in continuation_cache:
            return continuation_cache[candidate_run_id]

        try:
            from zerg.database import db_session
            from zerg.models.models import Run

            with with_test_commis_routing(test_commis_id):
                with db_session() as db:
                    candidate = db.query(Run).filter(Run.id == candidate_run_id).first()
                    if not candidate:
                        continuation_cache[candidate_run_id] = False
                        return False
                    # Check root_run_id first (handles chains), fall back to continuation_of_run_id
                    is_cont = bool(candidate.root_run_id == run_id or candidate.continuation_of_run_id == run_id)
                    continuation_cache[candidate_run_id] = is_cont
                    return is_cont
        except Exception:
            # Best-effort only; if lookup fails, do not leak events across runs.
            continuation_cache[candidate_run_id] = False
            return False

    async def event_handler(event):
        """Filter and queue relevant events (non-blocking)."""
        if overflow_event.is_set():
            return  # Already overflowed, drop subsequent events

        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # Filter by run_id, with optional continuation run support
        if "run_id" in event and event.get("run_id") != run_id:
            if allow_continuation_runs:
                candidate_run_id = event.get("run_id")
                if isinstance(candidate_run_id, int) and _is_continuation_of_run(candidate_run_id):
                    # Alias continuation run_id back to the original for UI stability
                    event = dict(event)
                    event["run_id"] = run_id
                else:
                    return
            else:
                return

        # Tool events MUST have run_id to prevent leaking across runs
        event_type = event.get("event_type") or event.get("type")
        if event_type in ("commis_tool_started", "commis_tool_completed", "commis_tool_failed", "commis_output_chunk"):
            if "run_id" not in event:
                logger.warning(f"Tool event missing run_id, dropping: {event_type}")
                return

        # Non-blocking put with overflow handling
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow_event.set()  # Signal overflow via Event (no sentinel needed)
            logger.warning(f"Stream queue overflow for run {run_id}, signaling client to reconnect")

    # Subscribe to all relevant events
    event_bus.subscribe(EventType.OIKOS_STARTED, event_handler)
    event_bus.subscribe(EventType.OIKOS_THINKING, event_handler)
    event_bus.subscribe(EventType.OIKOS_TOKEN, event_handler)
    event_bus.subscribe(EventType.OIKOS_COMPLETE, event_handler)
    event_bus.subscribe(EventType.OIKOS_DEFERRED, event_handler)
    event_bus.subscribe(EventType.OIKOS_WAITING, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.OIKOS_RESUMED, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.OIKOS_HEARTBEAT, event_handler)
    event_bus.subscribe(EventType.COMMIS_SPAWNED, event_handler)
    event_bus.subscribe(EventType.COMMIS_STARTED, event_handler)
    event_bus.subscribe(EventType.COMMIS_COMPLETE, event_handler)
    event_bus.subscribe(EventType.COMMIS_SUMMARY_READY, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_FAILED, event_handler)
    event_bus.subscribe(EventType.COMMIS_OUTPUT_CHUNK, event_handler)
    # Oikos tool events (for chat UI tool activity display)
    event_bus.subscribe(EventType.OIKOS_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.OIKOS_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.OIKOS_TOOL_FAILED, event_handler)
    event_bus.subscribe(EventType.SHOW_SESSION_PICKER, event_handler)
    # Stream lifecycle control
    event_bus.subscribe(EventType.STREAM_CONTROL, event_handler)

    try:
        # 2. Optionally load and replay historical events
        if include_replay:
            # Load historical events using a SHORT-LIVED DB session
            # This ensures we don't hold a DB connection during streaming
            historical_events = load_historical_run_events(
                run_id,
                after_event_id,
                include_tokens,
                test_commis_id=test_commis_id,
            )

            # Yield historical events with SSE id: field
            for historical_event in historical_events:
                last_sent_event_id = historical_event.event_id
                # Inject event_id for stream_control tracking
                payload_with_id = {**historical_event.payload, "_event_id": historical_event.event_id}
                _apply_event_state(historical_event.event_type, payload_with_id, from_replay=True)

                yield {
                    "id": str(historical_event.event_id),  # SSE last-event-id for resumption
                    "event": historical_event.event_type,
                    "data": json.dumps(
                        {
                            "type": historical_event.event_type,
                            "payload": historical_event.payload,
                            "timestamp": historical_event.timestamp,
                        },
                        default=_json_default,
                    ),
                }
        else:
            # Live-only mode: emit connected event for Oikos chat
            connected_payload = {
                "type": "connected",
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            yield {
                "event": "connected",
                "data": json.dumps(connected_payload, default=_json_default),
            }

        # 4a. If we saw stream_control:close during replay and streamed past it, close now
        if close_event_id is not None and last_sent_event_id >= close_event_id:
            logger.debug(
                f"Stream closed after replay - reached close marker (event_id={close_event_id}) for run {run_id}"
            )
            return

        # 4b. If replay already includes a terminal oikos_complete and no pending commiss, close early
        # (Heuristic fallback for runs without stream_control events)
        if saw_oikos_complete and pending_commiss == 0 and not continuation_active and close_event_id is None:
            logger.debug(f"Stream closed after replay for run {run_id} (no pending commiss, heuristic fallback)")
            return

        # 5. If run is complete (not RUNNING / DEFERRED / WAITING), close stream
        if status not in (RunStatus.RUNNING, RunStatus.DEFERRED, RunStatus.WAITING):
            logger.debug(f"Stream closed: run {run_id} is {status.value}, not streamable")
            return

        # 6. Stream live events (filtering out already-replayed ones)
        logger.debug(
            f"Starting live stream for run {run_id} (status={status.value}, last_sent_id={last_sent_event_id})"
        )

        # Send initial heartbeat to confirm we're in live mode
        yield {
            "event": "heartbeat",
            "data": json.dumps(
                {
                    "message": "Live stream started",
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                default=_json_default,
            ),
        }

        # Stream live events until oikos completes or errors
        complete = False
        while not complete:
            # Check overflow signal (set by event_handler when queue is full)
            if overflow_event.is_set():
                logger.warning(
                    f"Stream overflow for run {run_id}, closing (client should reconnect with Last-Event-ID)"
                )
                yield {
                    "event": "overflow",
                    "data": json.dumps(
                        {
                            "type": "overflow",
                            "message": "Stream buffer full, please reconnect",
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        },
                        default=_json_default,
                    ),
                }
                return

            try:
                timeout_s = 30.0
                if awaiting_continuation_until is not None:
                    remaining = awaiting_continuation_until - time.monotonic()
                    # Wake up at grace expiry instead of waiting a full heartbeat interval.
                    timeout_s = max(0.1, min(30.0, remaining))

                event = await asyncio.wait_for(queue.get(), timeout=timeout_s)

                event_type = event.get("event_type") or event.get("type") or "event"

                # Skip tokens if not requested
                if not include_tokens and event_type == "oikos_token":
                    continue

                # CRITICAL: Skip events that were already in the replay
                # This prevents duplicates when events arrive between DB query and live streaming
                event_id = event.get("event_id")
                if event_id and event_id <= last_sent_event_id:
                    logger.debug(f"Skipping duplicate event {event_id} (already replayed)")
                    continue

                # OIKOS_TOKEN is emitted per-token and will spam logs when DEBUG is enabled
                if event_type != EventType.OIKOS_TOKEN.value:
                    logger.debug(f"Stream: received live event {event_type} for run {run_id}")

                # Track commis lifecycle for UI telemetry (stream no longer waits on commiss)
                _apply_event_state(event_type, event)

                # If we've drained commiss after a oikos_complete and no continuation
                # started within the grace window, close the stream.
                if awaiting_continuation_until is not None and time.monotonic() >= awaiting_continuation_until:
                    complete = True

                # Format payload (strip internal fields)
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type", "owner_id", "event_id"}}

                # Update last_sent_event_id if this event has an ID
                if event_id:
                    last_sent_event_id = event_id

                # Build SSE data payload
                sse_data: dict = {
                    "type": event_type,
                    "payload": payload,
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

                sse_event = {
                    "event": event_type,
                    "data": json.dumps(sse_data, default=_json_default),
                }
                # Only include id field when event_id exists (omit for id=null)
                if event_id:
                    sse_event["id"] = str(event_id)
                yield sse_event

                # Check if we've reached the close marker (stream_control:close)
                if close_event_id is not None and event_id and event_id >= close_event_id:
                    logger.debug(f"Stream reached close marker (event_id={close_event_id}) for run {run_id}")
                    complete = True

            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                yield {
                    "event": "heartbeat",
                    "data": json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        },
                        default=_json_default,
                    ),
                }
                # Check TTL lease expiry (from stream_control:keep_open)
                if stream_lease_until is not None and time.monotonic() >= stream_lease_until:
                    logger.debug(f"Stream lease expired for run {run_id}")
                    complete = True
                # Legacy: heuristic fallback for runs without stream_control
                elif awaiting_continuation_until is not None and time.monotonic() >= awaiting_continuation_until:
                    complete = True

    except asyncio.CancelledError:
        logger.info(f"Stream disconnected for run {run_id}")
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.OIKOS_STARTED, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_THINKING, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_TOKEN, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_DEFERRED, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_WAITING, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.OIKOS_RESUMED, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.OIKOS_HEARTBEAT, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_SPAWNED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_STARTED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_SUMMARY_READY, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_FAILED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_OUTPUT_CHUNK, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.OIKOS_TOOL_FAILED, event_handler)
        event_bus.unsubscribe(EventType.SHOW_SESSION_PICKER, event_handler)
        event_bus.unsubscribe(EventType.STREAM_CONTROL, event_handler)


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

    yield {
        "event": "connected",
        "data": json.dumps(
            {
                "type": "connected",
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            default=_json_default,
        ),
    }

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
