"""Oikos chat endpoint and run lifecycle (cancel)."""

import asyncio
import logging
import os
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
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.database import get_test_commis_id
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.routers.oikos_auth import _is_tool_enabled
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.routers.stream import stream_run_events_live
from zerg.services.oikos_context import reset_seq

logger = logging.getLogger(__name__)


router = APIRouter(prefix="", tags=["oikos"])


class OikosChatRequest(BaseModel):
    """Request for text chat with Oikos."""

    message: str = Field(..., description="User message text")
    message_id: uuid.UUID = Field(..., description="Client-generated message ID (UUID)")
    model: Optional[str] = Field(None, description="Model to use for this request (e.g., gpt-5.2)")
    reasoning_effort: Optional[str] = Field(None, description="Reasoning effort: none, low, medium, high")
    replay_scenario: Optional[str] = Field(
        None,
        description="Replay scenario name (dev only, requires REPLAY_MODE_ENABLED=true)",
    )


async def _replay_stream_generator(
    run_id: int,
    owner_id: int,
    thread_id: int,
    message: str,
    message_id: str,
    trace_id: str,
    replay_scenario: str,
    test_commis_id: str | None = None,
):
    """Generate SSE events for replay mode (deterministic video recording).

    This generator emits pre-defined events from a scenario file instead of
    running the real oikos. Used for creating reproducible demo videos.
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
                    EventType.OIKOS_COMPLETE,
                    {
                        "event_type": "oikos_complete",
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
                EventType.OIKOS_COMPLETE,
                {
                    "event_type": "oikos_complete",
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
    async for event in stream_run_events_live(run_id, owner_id, test_commis_id=test_commis_id):
        yield event

        if not task_started:
            task_started = True
            logger.info(
                f"Replay SSE: starting replay for run {run_id}, scenario={replay_scenario}",
                extra={"tag": "OIKOS"},
            )

            # Run replay in background with error handling
            asyncio.create_task(_run_replay_with_error_handling())


@router.post("/chat")
async def oikos_chat(
    request: OikosChatRequest,
    current_user=Depends(get_current_oikos_user),
) -> EventSourceResponse:
    """Text chat with Oikos — returns an SSE stream of run events.

    Validates the request, invokes Oikos via the transport-agnostic
    ``invoke_oikos()``, and wires the resulting run to an SSE stream.
    """
    from zerg.models_config import get_default_model_id
    from zerg.models_config import get_model_by_id
    from zerg.services.oikos_service import invoke_oikos
    from zerg.services.quota import assert_can_start_run
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import warn_if_test_model

    # --- auth gate ---
    if not _is_tool_enabled(current_user.context or {}, "oikos"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tool disabled: oikos")

    # --- resolve model + reasoning preferences ---
    ctx = current_user.context or {}
    saved_prefs = (ctx.get("preferences", {}) or {}) if isinstance(ctx, dict) else {}

    model_to_use = request.model or saved_prefs.get("chat_model") or get_default_model_id()
    if is_test_model(model_to_use):
        warn_if_test_model(model_to_use)
    else:
        model_cfg = get_model_by_id(model_to_use)
        if not model_cfg:
            # Unknown model (removed from config) — fall back to instance default
            logger.warning(f"Saved model '{model_to_use}' not in config, falling back to default")
            model_to_use = get_default_model_id()
            model_cfg = get_model_by_id(model_to_use)
        # Check that the required API key is actually available
        if model_cfg:
            from zerg.models_config import _get_api_key_env_var

            key_env = _get_api_key_env_var(model_cfg)
            if not os.getenv(key_env):
                # Saved preference references a model whose key isn't configured — fall back
                logger.warning(f"Model '{model_to_use}' requires {key_env} which is not set, falling back to default")
                model_to_use = get_default_model_id()

    reasoning_effort = (request.reasoning_effort or saved_prefs.get("reasoning_effort") or "none").lower()
    if reasoning_effort not in {"none", "low", "medium", "high"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid reasoning_effort: {reasoning_effort}")

    message_id = str(request.message_id)
    test_commis_id = get_test_commis_id()

    # --- quota check (short-lived DB) ---
    from zerg.database import db_session

    with db_session() as db:
        assert_can_start_run(db, user=current_user)

    # --- replay mode (HTTP-only concern, deterministic video recording) ---
    from zerg.services.replay_service import is_replay_enabled

    if request.replay_scenario and is_replay_enabled():
        from zerg.services.oikos_service import create_oikos_run

        setup = await create_oikos_run(
            current_user.id,
            model=model_to_use,
            reasoning_effort=reasoning_effort,
        )
        logger.info(f"Oikos chat: REPLAY MODE for run {setup.run_id}, scenario={request.replay_scenario}", extra={"tag": "OIKOS"})
        return EventSourceResponse(
            _replay_stream_generator(
                setup.run_id,
                current_user.id,
                setup.thread_id,
                request.message,
                message_id,
                str(setup.trace_id),
                request.replay_scenario,
                test_commis_id=test_commis_id,
            )
        )

    # --- normal path: invoke + SSE stream ---
    run_id = await invoke_oikos(
        owner_id=current_user.id,
        message=request.message,
        message_id=message_id,
        source="web",
        model=model_to_use,
        reasoning_effort=reasoning_effort,
    )

    return EventSourceResponse(stream_run_events_live(run_id, current_user.id, test_commis_id=test_commis_id))


# ---------------------------------------------------------------------------
# Run cancellation
# ---------------------------------------------------------------------------


class OikosRunCancelResponse(BaseModel):
    """Response from oikos cancellation."""

    run_id: int = Field(..., description="The cancelled run ID")
    status: str = Field(..., description="Run status after cancellation")
    message: str = Field(..., description="Human-readable status message")


@router.post("/run/{run_id}/cancel", response_model=OikosRunCancelResponse)
async def oikos_run_cancel(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosRunCancelResponse:
    """Cancel a running oikos investigation."""
    from zerg.models.enums import RunStatus

    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    fiche = db.query(Fiche).filter(Fiche.id == run.fiche_id).first()
    if not fiche or fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    terminal_statuses = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}
    if run.status in terminal_statuses:
        return OikosRunCancelResponse(
            run_id=run_id,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            message="Run already completed",
        )

    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()

    logger.info(f"Oikos run {run_id} cancelled by user {current_user.id}")

    await event_bus.publish(
        EventType.OIKOS_COMPLETE,
        {
            "event_type": "oikos_complete",
            "run_id": run_id,
            "owner_id": current_user.id,
            "status": "cancelled",
            "message": "Investigation cancelled by user",
        },
    )

    reset_seq(run_id)

    return OikosRunCancelResponse(
        run_id=run_id,
        status="cancelled",
        message="Investigation cancelled",
    )
