"""Jarvis text chat endpoint with streaming responses."""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.routers.jarvis_supervisor import _pop_supervisor_task
from zerg.routers.jarvis_supervisor import _register_supervisor_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisChatRequest(BaseModel):
    """Request for text chat with Supervisor."""

    message: str = Field(..., description="User message text")
    client_correlation_id: Optional[str] = Field(None, description="Client-generated correlation ID")
    model: Optional[str] = Field(None, description="Model to use for this request (e.g., gpt-5.1)")
    reasoning_effort: Optional[str] = Field(None, description="Reasoning effort: none, low, medium, high")


async def _chat_stream_generator(
    run_id: int,
    owner_id: int,
    message: str,
    client_correlation_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
):
    """Generate SSE events for chat streaming.

    Subscribes to supervisor events and streams assistant responses.
    The background task is started from within this generator to avoid race conditions.
    """
    queue: asyncio.Queue = asyncio.Queue()
    task_handle: Optional[asyncio.Task] = None
    pending_workers = 0
    supervisor_done = False

    async def event_handler(event):
        """Filter and queue relevant events."""
        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # Filter by run_id
        if "run_id" in event and event.get("run_id") != run_id:
            return

        # Tool events MUST have run_id to prevent leaking across runs
        event_type = event.get("event_type") or event.get("type")
        if event_type in ("worker_tool_started", "worker_tool_completed", "worker_tool_failed"):
            if "run_id" not in event:
                logger.warning(f"Tool event missing run_id, dropping: {event_type}")
                return

        await queue.put(event)

    async def run_supervisor_background():
        """Execute supervisor in background."""
        from zerg.database import db_session
        from zerg.services.supervisor_service import SupervisorService

        try:
            with db_session() as bg_db:
                service = SupervisorService(bg_db)
                await service.run_supervisor(
                    owner_id=owner_id,
                    task=message,
                    run_id=run_id,
                    timeout=120,
                    model_override=model,
                    reasoning_effort=reasoning_effort,
                )
        except Exception as e:
            logger.exception(f"Background supervisor execution failed for run {run_id}: {e}")
            # Emit error event so the stream knows to close
            await event_bus.publish(
                EventType.ERROR,
                {
                    "event_type": "error",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "error": str(e),
                },
            )
        finally:
            await _pop_supervisor_task(run_id)

    # Subscribe to supervisor events BEFORE starting background task
    event_bus.subscribe(EventType.SUPERVISOR_STARTED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_THINKING, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
    event_bus.subscribe(EventType.WORKER_SPAWNED, event_handler)
    event_bus.subscribe(EventType.WORKER_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_COMPLETE, event_handler)
    event_bus.subscribe(EventType.WORKER_SUMMARY_READY, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_FAILED, event_handler)

    try:
        # Send initial connection event
        yield {
            "event": "connected",
            "data": json.dumps(
                {
                    "message": "Chat stream connected",
                    "run_id": run_id,
                    "client_correlation_id": client_correlation_id,
                }
            ),
        }

        # NOW start the background task - after subscriptions are ready and connected event sent
        logger.info(f"Chat SSE: starting background supervisor for run {run_id}")
        task_handle = asyncio.create_task(run_supervisor_background())
        await _register_supervisor_task(run_id, task_handle)

        # Stream events until supervisor completes or errors
        complete = False
        while not complete:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                event_type = event.get("event_type") or event.get("type") or "event"
                logger.debug(f"Chat SSE: received event {event_type} for run {run_id}")

                # Track worker lifecycle so we don't close the stream until workers finish
                if event_type == "worker_spawned":
                    pending_workers += 1
                elif event_type == "worker_complete" and pending_workers > 0:
                    pending_workers -= 1
                elif event_type == "worker_summary_ready" and pending_workers > 0:
                    # In rare cases worker_complete may be dropped; treat summary_ready as completion
                    pending_workers -= 1
                elif event_type == "supervisor_complete":
                    supervisor_done = True
                elif event_type == "error":
                    complete = True

                # Close once supervisor is done AND all workers for this run have finished
                if supervisor_done and pending_workers == 0:
                    complete = True

                # Format payload
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type", "owner_id"}}

                yield {
                    "event": event_type,
                    "data": json.dumps(
                        {
                            "type": event_type,
                            "payload": payload,
                            "client_correlation_id": client_correlation_id,
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                }

            except asyncio.TimeoutError:
                # Send heartbeat
                yield {
                    "event": "heartbeat",
                    "data": json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                }

    except asyncio.CancelledError:
        logger.info(f"Chat SSE stream disconnected for run {run_id}")
        if task_handle and not task_handle.done():
            task_handle.cancel()
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.SUPERVISOR_STARTED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_THINKING, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SPAWNED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SUMMARY_READY, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, event_handler)


@router.post("/chat")
async def jarvis_chat(
    request: JarvisChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """Text chat endpoint - streams responses from Supervisor.

    This endpoint provides a simpler alternative to /supervisor for text-only
    chat. It still uses the Supervisor under the hood but returns an SSE stream
    directly instead of requiring a separate connection.

    Args:
        request: Chat request with user message
        db: Database session
        current_user: Authenticated user

    Returns:
        EventSourceResponse streaming chat responses

    Example:
        POST /api/jarvis/chat
        {"message": "What's the weather?"}

        Streams SSE events:
        - supervisor_started: Chat processing started
        - supervisor_thinking: Supervisor analyzing
        - supervisor_complete: Final response with result
    """
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)

    # Server-side enforcement: respect user tool configuration
    if not _is_tool_enabled(current_user.context or {}, "supervisor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool disabled: supervisor",
        )

    # Determine effective preferences:
    # - request overrides win
    # - otherwise fall back to user's saved context.preferences
    # - otherwise fall back to global default model / "none" effort
    ctx = current_user.context or {}
    saved_prefs = (ctx.get("preferences", {}) or {}) if isinstance(ctx, dict) else {}

    from zerg.models_config import get_default_model_id_str
    from zerg.models_config import get_model_by_id

    model_to_use = request.model or saved_prefs.get("chat_model") or get_default_model_id_str()
    model_config = get_model_by_id(model_to_use)
    if not model_config or model_to_use == "gpt-mock":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid model: {model_to_use}",
        )

    reasoning_effort = request.reasoning_effort or saved_prefs.get("reasoning_effort") or "none"
    valid_efforts = {"none", "low", "medium", "high"}
    if reasoning_effort.lower() not in valid_efforts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid reasoning_effort: {reasoning_effort}",
        )
    reasoning_effort = reasoning_effort.lower()

    # Get or create supervisor components
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Create run record
    from zerg.models.enums import RunStatus
    from zerg.models.enums import RunTrigger

    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    logger.info(
        f"Jarvis chat: created run {run.id} for user {current_user.id}, "
        f"message: {request.message[:50]}..., model: {model_to_use}, reasoning: {reasoning_effort}"
    )

    # Return SSE stream - background task is started inside the generator
    # to avoid race conditions with event subscriptions
    return EventSourceResponse(
        _chat_stream_generator(
            run.id,
            current_user.id,
            request.message,
            request.client_correlation_id,
            model=model_to_use,
            reasoning_effort=reasoning_effort,
        )
    )
