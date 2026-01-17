"""Jarvis text chat endpoint with streaming responses."""

import asyncio
import logging
import uuid
from datetime import datetime
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sse_starlette.sse import EventSourceResponse

from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.routers.jarvis_sse import stream_run_events
from zerg.routers.jarvis_supervisor import _pop_supervisor_task
from zerg.routers.jarvis_supervisor import _register_supervisor_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisChatRequest(BaseModel):
    """Request for text chat with Supervisor."""

    message: str = Field(..., description="User message text")
    message_id: str = Field(..., description="Client-generated message ID (UUID)")
    model: Optional[str] = Field(None, description="Model to use for this request (e.g., gpt-5.2)")
    reasoning_effort: Optional[str] = Field(None, description="Reasoning effort: none, low, medium, high")
    replay_scenario: Optional[str] = Field(None, description="Replay scenario name (dev only, requires REPLAY_MODE_ENABLED=true)")


async def _replay_stream_generator(
    run_id: int,
    owner_id: int,
    thread_id: int,
    message: str,
    message_id: str,
    trace_id: str,
    replay_scenario: str,
):
    """Generate SSE events for replay mode (deterministic video recording).

    This generator emits pre-defined events from a scenario file instead of
    running the real supervisor. Used for creating reproducible demo videos.
    """
    from zerg.services.replay_service import run_replay_conversation

    async def _run_replay_with_error_handling():
        """Wrapper that emits error events if replay fails."""
        try:
            success = await run_replay_conversation(
                scenario_name=replay_scenario,
                user_message=message,
                run_id=run_id,
                thread_id=thread_id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=trace_id,
            )
            if not success:
                # No matching conversation found - emit error so stream closes
                logger.warning(f"Replay failed: no matching conversation for '{message[:50]}...'")
                await event_bus.publish(
                    EventType.ERROR,
                    {
                        "event_type": "error",
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "message": "Replay mode: no matching conversation for message",
                        "trace_id": trace_id,
                    },
                )
                await event_bus.publish(
                    EventType.SUPERVISOR_COMPLETE,
                    {
                        "event_type": "supervisor_complete",
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "owner_id": owner_id,
                        "message_id": message_id,
                        "status": "failed",
                        "result": "Replay mode: no matching conversation found",
                        "trace_id": trace_id,
                    },
                )
        except Exception as e:
            logger.exception(f"Replay error: {e}")
            await event_bus.publish(
                EventType.ERROR,
                {
                    "event_type": "error",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "message": f"Replay error: {e}",
                    "trace_id": trace_id,
                },
            )
            await event_bus.publish(
                EventType.SUPERVISOR_COMPLETE,
                {
                    "event_type": "supervisor_complete",
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "owner_id": owner_id,
                    "message_id": message_id,
                    "status": "failed",
                    "result": f"Replay error: {e}",
                    "trace_id": trace_id,
                },
            )

    task_started = False
    async for event in stream_run_events(run_id, owner_id):
        yield event

        if not task_started:
            task_started = True
            logger.info(f"Replay SSE: starting replay for run {run_id}, scenario={replay_scenario}", extra={"tag": "JARVIS"})

            # Run replay in background with error handling
            asyncio.create_task(_run_replay_with_error_handling())


async def _chat_stream_generator(
    run_id: int,
    owner_id: int,
    message: str,
    message_id: str,
    trace_id: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
):
    """Generate SSE events for chat streaming.

    Subscribes to supervisor events and streams assistant responses.
    The background task is started from within this generator to avoid race conditions.

    IMPORTANT: We must not start the supervisor before the SSE stream has subscribed
    to events, otherwise early events (e.g. supervisor_started) can be missed.
    """
    task_handle: Optional[asyncio.Task] = None

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
                    message_id=message_id,
                    trace_id=trace_id,
                    timeout=600,  # 10 min safety net; deferred state kicks in before this
                    model_override=model,
                    reasoning_effort=reasoning_effort,
                    return_on_deferred=False,
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

    # Stream events, and only start the supervisor AFTER the stream generator has
    # yielded its first event (which implies subscriptions are registered).
    started_background = False
    async for event in stream_run_events(run_id, owner_id):
        yield event

        if not started_background:
            started_background = True
            logger.info(f"Chat SSE: starting background supervisor for run {run_id}", extra={"tag": "JARVIS"})
            task_handle = asyncio.create_task(run_supervisor_background())
            await _register_supervisor_task(run_id, task_handle)


@router.post("/chat")
async def jarvis_chat(
    request: JarvisChatRequest,
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """Text chat endpoint - streams responses from Supervisor.

    This endpoint provides a simpler alternative to /supervisor for text-only
    chat. It still uses the Supervisor under the hood but returns an SSE stream
    directly instead of requiring a separate connection.

    Args:
        request: Chat request with user message
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
    from zerg.database import db_session
    from zerg.services.supervisor_service import SupervisorService

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
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import warn_if_test_model

    model_to_use = request.model or saved_prefs.get("chat_model") or get_default_model_id_str()

    # Allow test models (logs warning but doesn't block)
    if is_test_model(model_to_use):
        warn_if_test_model(model_to_use)
    else:
        model_config = get_model_by_id(model_to_use)
        if not model_config:
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

    # CRITICAL: Use SHORT-LIVED session for all DB operations
    # Don't use Depends(get_db) - it holds the session open for the entire
    # SSE stream duration, blocking TRUNCATE during E2E resets.
    from zerg.models.enums import RunStatus
    from zerg.models.enums import RunTrigger

    with db_session() as db:
        supervisor_service = SupervisorService(db)

        # Get or create supervisor components
        agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
        thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

        # Generate trace_id for end-to-end debugging
        trace_id = uuid.uuid4()

        # Create run record
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
            assistant_message_id=request.message_id,  # Client-generated message ID
            model=model_to_use,  # Store resolved model for continuation inheritance
            reasoning_effort=reasoning_effort,  # Store for continuation inheritance
            trace_id=trace_id,  # End-to-end tracing
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        # Capture values we need before session closes
        run_id = run.id
        agent_id = agent.id
        thread_id = thread.id
        run_status_value = run.status.value
        trace_id_str = str(trace_id)  # Convert to string for JSON serialization
    # Session is now closed - no DB connection held during streaming

    # v2.2: Notify dashboard of new run
    await event_bus.publish(
        EventType.RUN_CREATED,
        {
            "event_type": "run_created",
            "agent_id": agent_id,
            "run_id": run_id,
            "status": run_status_value,
            "thread_id": thread_id,
            "owner_id": current_user.id,
        },
    )
    await event_bus.publish(
        EventType.RUN_UPDATED,
        {
            "event_type": "run_updated",
            "agent_id": agent_id,
            "run_id": run_id,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "owner_id": current_user.id,
        },
    )

    logger.info(
        f"Jarvis chat: created run {run_id} for user {current_user.id}, "
        f"message: {request.message[:50]}..., model: {model_to_use}, reasoning: {reasoning_effort}",
        extra={"tag": "JARVIS"},
    )

    # Check for replay mode (deterministic video recording)
    from zerg.services.replay_service import is_replay_enabled

    if request.replay_scenario and is_replay_enabled():
        logger.info(
            f"Jarvis chat: using REPLAY MODE for run {run_id}, scenario={request.replay_scenario}",
            extra={"tag": "JARVIS"},
        )
        return EventSourceResponse(
            _replay_stream_generator(
                run_id,
                current_user.id,
                thread_id,
                request.message,
                request.message_id,
                trace_id_str,
                request.replay_scenario,
            )
        )

    # Return SSE stream - background task is started inside the generator
    # to avoid race conditions with event subscriptions
    return EventSourceResponse(
        _chat_stream_generator(
            run_id,
            current_user.id,
            request.message,
            request.message_id,
            trace_id_str,
            model=model_to_use,
            reasoning_effort=reasoning_effort,
        )
    )
