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
    client_correlation_id: Optional[str] = Field(None, description="Client-generated correlation ID")
    model: Optional[str] = Field(None, description="Model to use for this request (e.g., gpt-5.2)")
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
    logger.info(f"Chat SSE: starting background supervisor for run {run_id}", extra={"tag": "JARVIS"})
    task_handle = asyncio.create_task(run_supervisor_background())
    await _register_supervisor_task(run_id, task_handle)

    # Delegate to shared generator for event streaming
    async for event in stream_run_events(run_id, owner_id, client_correlation_id):
        yield event


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

    from zerg.config import get_settings
    from zerg.models_config import get_default_model_id_str
    from zerg.models_config import get_model_by_id
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import require_testing_mode

    model_to_use = request.model or saved_prefs.get("chat_model") or get_default_model_id_str()

    # Validate model: test models require TESTING=1, production models must exist in config
    if is_test_model(model_to_use):
        try:
            require_testing_mode(model_to_use, get_settings())
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
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

        # Create run record
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
            correlation_id=request.client_correlation_id,  # Phase 1: Store correlation ID
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        # Capture values we need before session closes
        run_id = run.id
        agent_id = agent.id
        thread_id = thread.id
        run_status_value = run.status.value
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

    # Return SSE stream - background task is started inside the generator
    # to avoid race conditions with event subscriptions
    return EventSourceResponse(
        _chat_stream_generator(
            run_id,
            current_user.id,
            request.message,
            request.client_correlation_id,
            model=model_to_use,
            reasoning_effort=reasoning_effort,
        )
    )
