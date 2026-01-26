"""Resumable SSE streaming endpoints (Phase 3).

This module implements the new /api/stream/courses/{course_id} endpoint that supports
replay + live streaming. It enables clients to reconnect and catch up on missed
events by replaying from the database event store, then continuing with live events.

Key features:
- Replay historical events from CourseEvent table
- Continue with live events via EventBus subscription
- Handle DEFERRED courses correctly (streamable, not treated as complete)
- SSE format with id: field for client resumption
- Token filtering support
- SHORT-LIVED DB sessions for replay (critical for test isolation)
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import List
from typing import Tuple

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sse_starlette.sse import EventSourceResponse

from zerg.database import db_session
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.enums import CourseStatus
from zerg.models.models import Course
from zerg.models.models import Fiche
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.event_store import EventStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])


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


def _load_historical_events(
    course_id: int,
    after_event_id: int,
    include_tokens: bool,
) -> List[Tuple[int, str, dict, str]]:
    """Load historical events from DB using a SHORT-LIVED session.

    This is critical for test isolation: the DB session is opened, events are
    loaded into memory, and the session is immediately closed. This prevents
    the streaming connection from holding a DB connection indefinitely.

    Args:
        course_id: Course identifier
        after_event_id: Resume from this event ID (0 = from start)
        include_tokens: Whether to include CONCIERGE_TOKEN events

    Returns:
        List of (event_id, event_type, payload, timestamp_str) tuples
    """
    events: List[Tuple[int, str, dict, str]] = []

    with db_session() as db:
        historical = EventStore.get_events_after(
            db=db,
            course_id=course_id,
            after_id=after_event_id,
            include_tokens=include_tokens,
        )
        # Load all events into memory before closing session
        for event in historical:
            events.append(
                (
                    event.id,
                    event.event_type,
                    event.payload,
                    event.created_at.isoformat().replace("+00:00", "Z"),
                )
            )
    # Session is now closed - no DB connection held during streaming

    return events


# Backpressure: max events to buffer per client before closing stream
# Client should reconnect with Last-Event-ID for resumable replay
STREAM_QUEUE_MAX_SIZE = 1000


async def _replay_and_stream(
    course_id: int,
    owner_id: int,
    status: CourseStatus,
    after_event_id: int,
    include_tokens: bool,
    *,
    include_replay: bool = True,
    allow_continuation_courses: bool = False,
):
    """Generator that optionally replays historical events, then streams live.

    This is the unified SSE streaming implementation used by both:
    - /api/stream/courses/{course_id} - resumable SSE with replay
    - /api/jarvis/chat - live-only SSE for initial chat

    The function can operate in two modes:
    1. Replay + Live (include_replay=True): Load historical events, then stream live
    2. Live-only (include_replay=False): Stream live events only (jarvis chat)

    IMPORTANT: This function does NOT hold a DB connection open during streaming.
    Historical events are loaded into memory first, then the DB session is closed
    before any SSE events are yielded.

    Backpressure: The queue is bounded to STREAM_QUEUE_MAX_SIZE events. If a slow
    client causes overflow, the stream closes gracefully. The client should reconnect
    with Last-Event-ID to resume from the durable event store.

    Args:
        course_id: Course identifier
        owner_id: Owner ID for security filtering
        status: Current course status (RUNNING, DEFERRED, SUCCESS, etc.)
        after_event_id: Resume from this event ID (0 = from start)
        include_tokens: Whether to include CONCIERGE_TOKEN events
        include_replay: If True, replay historical events before streaming live
        allow_continuation_courses: If True, also stream events from continuation courses

    Yields:
        SSE events in format: {"id": str, "event": str, "data": str}
    """
    # 1. Subscribe to live events FIRST (before replaying) to avoid race condition
    # Bounded queue for backpressure - overflow triggers graceful stream closure
    queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAX_SIZE)
    last_sent_event_id = 0
    pending_commis = 0
    concierge_done = False
    overflow_event = asyncio.Event()  # Signal overflow without queue sentinel
    continuation_cache: dict[int, bool] = {}  # Cache for continuation course lookups

    def _is_direct_continuation(candidate_course_id: int) -> bool:
        """Return True if candidate_course_id is a continuation of course_id.

        This allows a single client SSE stream to receive the follow-up concierge
        synthesis that happens in a new course (durable courses v2.2).
        """
        if candidate_course_id in continuation_cache:
            return continuation_cache[candidate_course_id]

        try:
            from zerg.database import db_session
            from zerg.models.models import Course

            with db_session() as db:
                candidate = db.query(Course).filter(Course.id == candidate_course_id).first()
                is_cont = bool(candidate and candidate.continuation_of_course_id == course_id)
                continuation_cache[candidate_course_id] = is_cont
                return is_cont
        except Exception:
            # Best-effort only; if lookup fails, do not leak events across courses.
            continuation_cache[candidate_course_id] = False
            return False

    async def event_handler(event):
        """Filter and queue relevant events (non-blocking)."""
        if overflow_event.is_set():
            return  # Already overflowed, drop subsequent events

        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # Filter by course_id, with optional continuation course support
        if "course_id" in event and event.get("course_id") != course_id:
            if allow_continuation_courses:
                candidate_course_id = event.get("course_id")
                if isinstance(candidate_course_id, int) and _is_direct_continuation(candidate_course_id):
                    # Alias continuation course_id back to the original for UI stability
                    event = dict(event)
                    event["course_id"] = course_id
                else:
                    return
            else:
                return

        # Tool events MUST have course_id to prevent leaking across courses
        event_type = event.get("event_type") or event.get("type")
        if event_type in ("commis_tool_started", "commis_tool_completed", "commis_tool_failed"):
            if "course_id" not in event:
                logger.warning(f"Tool event missing course_id, dropping: {event_type}")
                return

        # Non-blocking put with overflow handling
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow_event.set()  # Signal overflow via Event (no sentinel needed)
            logger.warning(f"Stream queue overflow for course {course_id}, signaling client to reconnect")

    # Subscribe to all relevant events
    event_bus.subscribe(EventType.CONCIERGE_STARTED, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_THINKING, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_TOKEN, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_COMPLETE, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_DEFERRED, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_WAITING, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.CONCIERGE_RESUMED, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.CONCIERGE_HEARTBEAT, event_handler)
    event_bus.subscribe(EventType.COMMIS_SPAWNED, event_handler)
    event_bus.subscribe(EventType.COMMIS_STARTED, event_handler)
    event_bus.subscribe(EventType.COMMIS_COMPLETE, event_handler)
    event_bus.subscribe(EventType.COMMIS_SUMMARY_READY, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.COMMIS_TOOL_FAILED, event_handler)
    # Concierge tool events (for chat UI tool activity display)
    event_bus.subscribe(EventType.CONCIERGE_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.CONCIERGE_TOOL_FAILED, event_handler)

    try:
        # 2. Optionally load and replay historical events
        if include_replay:
            # Load historical events using a SHORT-LIVED DB session
            # This ensures we don't hold a DB connection during streaming
            historical_events = _load_historical_events(course_id, after_event_id, include_tokens)

            # Yield historical events with SSE id: field
            for event_id, event_type, payload, timestamp_str in historical_events:
                last_sent_event_id = event_id

                yield {
                    "id": str(event_id),  # SSE last-event-id for resumption
                    "event": event_type,
                    "data": json.dumps(
                        {
                            "type": event_type,
                            "payload": payload,
                            "timestamp": timestamp_str,
                        },
                        default=_json_default,
                    ),
                }
        else:
            # Live-only mode: emit connected event for Jarvis chat
            connected_payload = {
                "type": "connected",
                "course_id": course_id,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            yield {
                "event": "connected",
                "data": json.dumps(connected_payload, default=_json_default),
            }

        # 4. If course is complete (not RUNNING / DEFERRED / WAITING), close stream
        if status not in (CourseStatus.RUNNING, CourseStatus.DEFERRED, CourseStatus.WAITING):
            logger.debug(f"Stream closed: course {course_id} is {status.value}, not streamable")
            return

        # 5. Stream live events (filtering out already-replayed ones)
        logger.debug(f"Starting live stream for course {course_id} (status={status.value}, last_sent_id={last_sent_event_id})")

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

        # Stream live events until concierge completes or errors
        complete = False
        while not complete:
            # Check overflow signal (set by event_handler when queue is full)
            if overflow_event.is_set():
                logger.warning(f"Stream overflow for course {course_id}, closing (client should reconnect with Last-Event-ID)")
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
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                event_type = event.get("event_type") or event.get("type") or "event"

                # Skip tokens if not requested
                if not include_tokens and event_type == "concierge_token":
                    continue

                # CRITICAL: Skip events that were already in the replay
                # This prevents duplicates when events arrive between DB query and live streaming
                event_id = event.get("event_id")
                if event_id and event_id <= last_sent_event_id:
                    logger.debug(f"Skipping duplicate event {event_id} (already replayed)")
                    continue

                # CONCIERGE_TOKEN is emitted per-token and will spam logs when DEBUG is enabled
                if event_type != EventType.CONCIERGE_TOKEN.value:
                    logger.debug(f"Stream: received live event {event_type} for course {course_id}")

                # Track commis lifecycle so we don't close the stream until commis finish
                if event_type == "commis_spawned":
                    pending_commis += 1
                elif event_type == "commis_complete" and pending_commis > 0:
                    pending_commis -= 1
                elif event_type == "commis_summary_ready" and pending_commis > 0:
                    pending_commis -= 1
                elif event_type == "concierge_complete":
                    concierge_done = True
                elif event_type == "concierge_deferred":
                    # v2.2: Timeout migration default is to close the stream, but some
                    # DEFERRED states (e.g., waiting for commis continuations) should
                    # keep the stream open so the connected client receives the final answer.
                    if event.get("close_stream", True):
                        complete = True
                elif event_type == "error":
                    complete = True

                # Close once concierge is done AND all commis for this course have finished
                if concierge_done and pending_commis == 0:
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

    except asyncio.CancelledError:
        logger.info(f"Stream disconnected for course {course_id}")
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.CONCIERGE_STARTED, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_THINKING, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_TOKEN, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_DEFERRED, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_WAITING, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.CONCIERGE_RESUMED, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.CONCIERGE_HEARTBEAT, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_SPAWNED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_STARTED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_SUMMARY_READY, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.COMMIS_TOOL_FAILED, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.CONCIERGE_TOOL_FAILED, event_handler)


async def stream_course_events_live(
    course_id: int,
    owner_id: int,
):
    """Stream live course events without replay (for Jarvis chat).

    This is a convenience wrapper around _replay_and_stream for the Jarvis chat
    use case. It:
    - Streams live events only (no replay from DB)
    - Supports continuation course aliasing (follow-up concierge courses)
    - Emits a "connected" event on start

    Args:
        course_id: Course identifier
        owner_id: Owner ID for security filtering

    Yields:
        SSE events in format: {"event": str, "data": str}
    """
    # For live-only, we use RUNNING status to allow streaming
    # The status check happens later if the course completes during streaming
    async for event in _replay_and_stream(
        course_id=course_id,
        owner_id=owner_id,
        status=CourseStatus.RUNNING,  # Assume running for live streaming
        after_event_id=0,
        include_tokens=True,
        include_replay=False,
        allow_continuation_courses=True,
    ):
        yield event


@router.get("/courses/{course_id}")
async def stream_course_replay(
    course_id: int,
    request: Request,
    after_event_id: int = 0,
    include_tokens: bool = True,
    current_user=Depends(get_current_jarvis_user),
):
    """Stream course events with replay support (Resumable SSE v1).

    This endpoint enables clients to reconnect and catch up on missed events by:
    1. Replaying historical events from the database
    2. Continuing with live events via EventBus

    For completed courses: Replays all events and closes the stream.
    For active courses (RUNNING/DEFERRED): Replays historical + streams live events.

    Args:
        course_id: Course identifier
        request: HTTP request (for Last-Event-ID header)
        after_event_id: Resume from this event ID (0 = from start)
        include_tokens: Whether to include CONCIERGE_TOKEN events (default: true)
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        EventSourceResponse for SSE streaming

    Raises:
        HTTPException: 404 if course not found or not owned by user

    SSE Format:
        id: {event.id}
        event: {event.event_type}
        data: {"type": "...", "payload": {...}, "timestamp": "..."}

    Examples:
        # Start from beginning
        GET /api/stream/courses/123

        # Resume from last-event-id (standard SSE reconnect)
        GET /api/stream/courses/123
        Last-Event-ID: 456

        # Resume from specific event ID
        GET /api/stream/courses/123?after_event_id=456

        # Skip token events (for bandwidth optimization)
        GET /api/stream/courses/123?include_tokens=false
    """
    # Security: verify ownership using SHORT-LIVED session
    # CRITICAL: Don't use Depends(get_db) here - it holds the session open
    # for the entire SSE stream duration, blocking TRUNCATE during E2E resets.
    with db_session() as db:
        course = (
            db.query(Course)
            .join(Fiche, Fiche.id == Course.fiche_id)
            .filter(Course.id == course_id)
            .filter(Fiche.owner_id == current_user.id)
            .first()
        )

        if not course:
            raise HTTPException(status_code=404, detail="Course not found")

        # Capture values we need before session closes
        course_status = course.status
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
        f"Streaming course {course_id} (status={course_status.value}, "
        f"after_event_id={after_event_id}, "
        f"include_tokens={include_tokens})"
    )

    return EventSourceResponse(
        _replay_and_stream(
            course_id=course_id,
            owner_id=current_user.id,
            status=course_status,
            after_event_id=after_event_id,
            include_tokens=include_tokens,
        )
    )
