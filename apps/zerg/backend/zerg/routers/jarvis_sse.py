"""SSE streaming helpers for Jarvis."""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Optional

from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.generated.sse_events import SSEEventType

logger = logging.getLogger(__name__)

# Backpressure: max events to buffer per client before closing stream
# Client should reconnect with Last-Event-ID for resumable replay
SSE_QUEUE_MAX_SIZE = 1000


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


async def stream_run_events(
    run_id: int,
    owner_id: int,
    client_correlation_id: Optional[str] = None,
):
    """Generate SSE events for a specific run.

    Subscribes to all supervisor and worker events filtered by run_id.
    This is used for both initial chat and re-attaching to an in-progress run.

    Backpressure: The queue is bounded to SSE_QUEUE_MAX_SIZE events. If a slow
    client causes overflow, the stream closes gracefully. The client should reconnect
    with Last-Event-ID to resume from the durable event store.
    """
    # Bounded queue for backpressure - overflow triggers graceful stream closure
    queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_MAX_SIZE)
    pending_workers = 0
    supervisor_done = False
    continuation_cache: dict[int, bool] = {}
    overflow = False  # Track if queue has overflowed

    def _is_direct_continuation(candidate_run_id: int) -> bool:
        """Return True if candidate_run_id is a continuation of run_id.

        This allows a single client SSE stream to receive the follow-up supervisor
        synthesis that happens in a new run (durable runs v2.2).
        """
        if candidate_run_id in continuation_cache:
            return continuation_cache[candidate_run_id]

        try:
            from zerg.database import db_session
            from zerg.models.models import AgentRun

            with db_session() as db:
                candidate = db.query(AgentRun).filter(AgentRun.id == candidate_run_id).first()
                is_cont = bool(candidate and candidate.continuation_of_run_id == run_id)
                continuation_cache[candidate_run_id] = is_cont
                return is_cont
        except Exception:
            # Best-effort only; if lookup fails, do not leak events across runs.
            continuation_cache[candidate_run_id] = False
            return False

    async def event_handler(event):
        """Filter and queue relevant events (non-blocking)."""
        nonlocal overflow
        if overflow:
            return  # Already overflowed, drop subsequent events

        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # Filter by run_id, but allow direct continuation runs to flow through
        # so the connected client sees the supervisor's follow-up response.
        if "run_id" in event and event.get("run_id") != run_id:
            candidate_run_id = event.get("run_id")
            if not isinstance(candidate_run_id, int) or not _is_direct_continuation(candidate_run_id):
                return
            # Alias continuation run_id back to the original for UI stability.
            event = dict(event)
            event["run_id"] = run_id

        # Tool events MUST have run_id to prevent leaking across runs
        event_type = event.get("event_type") or event.get("type")
        if event_type in ("worker_tool_started", "worker_tool_completed", "worker_tool_failed"):
            if "run_id" not in event:
                logger.warning(f"Tool event missing run_id, dropping: {event_type}")
                return

        # Non-blocking put with overflow handling
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow = True
            logger.warning(f"SSE queue overflow for run {run_id}, signaling client to reconnect")
            # Push overflow sentinel (make room by being already full, try once)
            try:
                queue.put_nowait({"_overflow": True})
            except asyncio.QueueFull:
                pass  # Stream will timeout and close anyway

    # Subscribe to all relevant events
    event_bus.subscribe(EventType.SUPERVISOR_STARTED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_THINKING, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_TOKEN, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_DEFERRED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_WAITING, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.SUPERVISOR_RESUMED, event_handler)  # Interrupt/resume pattern
    event_bus.subscribe(EventType.SUPERVISOR_HEARTBEAT, event_handler)
    event_bus.subscribe(EventType.WORKER_SPAWNED, event_handler)
    event_bus.subscribe(EventType.WORKER_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_COMPLETE, event_handler)
    event_bus.subscribe(EventType.WORKER_SUMMARY_READY, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_FAILED, event_handler)
    # Subscribe to supervisor tool events (inline tool cards in chat)
    event_bus.subscribe(EventType.SUPERVISOR_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_TOOL_FAILED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_TOOL_PROGRESS, event_handler)

    try:
        # Send initial heartbeat to confirm connection immediately
        # This prevents test timeouts and improves perceived responsiveness
        heartbeat_data = {
            "message": "Stream connected",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        yield {
            "event": SSEEventType.HEARTBEAT.value,
            "data": json.dumps(heartbeat_data, default=_json_default),
        }

        # Stream events until supervisor completes or errors
        complete = False
        while not complete:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                # Handle overflow sentinel - close stream gracefully
                if event.get("_overflow"):
                    logger.warning(f"SSE overflow for run {run_id}, closing (client should reconnect)")
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

                event_type = event.get("event_type") or event.get("type") or "event"
                # SUPERVISOR_TOKEN is emitted per-token and will spam logs when DEBUG is enabled.
                if event_type != EventType.SUPERVISOR_TOKEN.value:
                    logger.debug(f"Chat SSE: received event {event_type} for run {run_id}")

                # Track worker lifecycle so we don't close the stream until workers finish
                if event_type == "worker_spawned":
                    pending_workers += 1
                elif event_type == "worker_complete" and pending_workers > 0:
                    pending_workers -= 1
                elif event_type == "worker_summary_ready" and pending_workers > 0:
                    pending_workers -= 1
                elif event_type == "supervisor_complete":
                    supervisor_done = True
                elif event_type == "supervisor_deferred":
                    # v2.2: Timeout migration default is to close the stream, but some
                    # DEFERRED states (e.g., waiting for worker continuations) should
                    # keep the stream open so the connected client receives the final answer.
                    if event.get("close_stream", True):
                        complete = True
                elif event_type == "error":
                    complete = True

                # Close once supervisor is done AND all workers for this run have finished
                if supervisor_done and pending_workers == 0:
                    complete = True

                # Format payload - extract event_id before filtering
                event_id = event.get("event_id")
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type", "owner_id", "event_id"}}

                sse_event = {
                    "event": event_type,
                    "data": json.dumps(
                        {
                            "type": event_type,
                            "payload": payload,
                            "client_correlation_id": client_correlation_id,
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        },
                        default=_json_default,
                    ),
                }

                # Add id field if event_id is present
                if event_id is not None:
                    sse_event["id"] = str(event_id)

                yield sse_event

            except asyncio.TimeoutError:
                # Send heartbeat
                heartbeat_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                yield {
                    "event": SSEEventType.HEARTBEAT.value,
                    "data": json.dumps(heartbeat_data, default=_json_default),
                }

    except asyncio.CancelledError:
        logger.info(f"SSE stream disconnected for run {run_id}")
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.SUPERVISOR_STARTED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_THINKING, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_TOKEN, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_DEFERRED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_WAITING, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.SUPERVISOR_RESUMED, event_handler)  # Interrupt/resume pattern
        event_bus.unsubscribe(EventType.SUPERVISOR_HEARTBEAT, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SPAWNED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SUMMARY_READY, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, event_handler)
        # Unsubscribe from supervisor tool events
        event_bus.unsubscribe(EventType.SUPERVISOR_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_TOOL_FAILED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_TOOL_PROGRESS, event_handler)
