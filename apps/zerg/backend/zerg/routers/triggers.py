"""API router for Triggers (milestone M1).

Currently only supports simple *webhook* triggers that, when invoked, publish
an EventType.TRIGGER_FIRED event.  The SchedulerService listens for that event
and executes the associated agent immediately.
"""

# typing and forward-ref convenience
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict
from typing import List
from typing import Optional

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Path
from fastapi import Query  # Added Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg import constants
from zerg.crud import crud
from zerg.database import get_db

# Auth dependency
from zerg.dependencies.auth import get_current_user
from zerg.events import EventType
from zerg.events import event_bus

# Metrics
from zerg.metrics import trigger_fired_total

# Schemas
from zerg.schemas.schemas import Trigger as TriggerSchema
from zerg.schemas.schemas import TriggerCreate

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/triggers",
    tags=["triggers"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# DELETE /triggers/{id}
# ---------------------------------------------------------------------------


@router.delete("/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trigger(
    *,
    trigger_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
):
    """Delete a trigger.

    Connector-managed providers (like Gmail) are not affected by trigger
    deletion (watch lifecycle is per-connector).
    """

    trg = crud.get_trigger(db, trigger_id)
    if trg is None:
        raise HTTPException(status_code=404, detail="Trigger not found")

    crud.delete_trigger(db, trigger_id)
    return None


@router.post("/", response_model=TriggerSchema, status_code=status.HTTP_201_CREATED)
async def create_trigger(trigger_in: TriggerCreate, db: Session = Depends(get_db)):
    """Create a new trigger for an agent.

    If the trigger is of type *email* and the provider is **gmail** we kick off
    an asynchronous helper that ensures a Gmail *watch* is registered.  The
    call is awaited so tests (which run inside the same event-loop) can verify
    the side-effects synchronously without sprinkling ``asyncio.sleep`` hacks.
    """

    # Ensure agent exists -------------------------------------------------
    agent = crud.get_agent(db, trigger_in.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Validate trigger type against allowlist
    if trigger_in.type not in {"webhook", "email"}:
        raise HTTPException(status_code=400, detail="Invalid trigger type")

    # Email triggers must reference a connector (validate before persist)
    new_config = trigger_in.config
    if trigger_in.type == "email":
        cfg = dict(new_config or {})
        connector_id = cfg.get("connector_id")
        if connector_id is None:
            raise HTTPException(status_code=400, detail="Email triggers require connector_id in config")
        from zerg.crud import crud as _crud  # local import

        conn = _crud.get_connector(db, int(connector_id))
        if conn is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        # Security: ensure connector belongs to same user as agent
        if conn.owner_id != agent.owner_id:
            raise HTTPException(status_code=403, detail="Connector belongs to different user")
        # Normalise provider field to connector provider
        cfg["provider"] = conn.provider
        new_config = cfg

    # Persist trigger -----------------------------------------------------
    trg = crud.create_trigger(
        db,
        agent_id=trigger_in.agent_id,
        trigger_type=trigger_in.type,
        config=new_config,
    )

    return trg


# Maximum request body size for webhook events (256 KiB)
MAX_WEBHOOK_BODY_SIZE = 256 * 1024


@router.post(
    "/{trigger_id}/events",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[],  # Override router-level auth - this endpoint is public
)
async def fire_trigger_event(
    *,
    request: Request,
    trigger_id: int = Path(..., gt=0),
    payload: Optional[Dict] = Body(default=None),  # Arbitrary JSON body
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Public webhook endpoint that fires a trigger event.

    Security: Bearer token authentication using the trigger's unique secret.

    Request format:
        POST /api/triggers/{trigger_id}/events
        Authorization: Bearer <trigger.secret>
        Content-Type: application/json

        {arbitrary json body or empty}

    Returns:
        202 Accepted: {"status": "accepted"} - triggered successfully
        404 Not Found: invalid token OR unknown trigger (don't leak existence)
        413 Payload Too Large: request body exceeds 256 KiB limit
    """
    # Handle None payload (use empty dict as default)
    if payload is None:
        payload = {}

    # 1) Validate body size via Content-Length header (best-effort).
    # Note: Content-Length may be missing (e.g. chunked transfer encoding), so
    # this should be paired with an upstream proxy limit (e.g. nginx
    # `client_max_body_size`) for robust enforcement.
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            content_length_int = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if content_length_int > MAX_WEBHOOK_BODY_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request body too large (max 256 KiB)",
            )
        if content_length_int < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")

    # 2) Extract bearer token from Authorization header
    if not authorization or not authorization.startswith("Bearer "):
        # Return 404 to avoid leaking trigger existence
        raise HTTPException(status_code=404, detail="Not found")

    bearer_token = authorization[7:]  # Strip "Bearer " prefix

    # 3) Fetch trigger and validate token (constant-time comparison)
    trg = crud.get_trigger(db, trigger_id)
    if trg is None:
        # Return 404 for unknown trigger
        raise HTTPException(status_code=404, detail="Not found")

    # Constant-time comparison to prevent timing attacks
    import secrets

    if not secrets.compare_digest(bearer_token, trg.secret):
        # Return 404 for invalid token (don't leak trigger existence)
        raise HTTPException(status_code=404, detail="Not found")

    # 4) Publish event on internal bus (non-blocking)
    # The SchedulerService subscribes to TRIGGER_FIRED and executes the agent.
    # We use create_task to avoid blocking the HTTP response on agent execution.
    task = asyncio.create_task(
        event_bus.publish(
            EventType.TRIGGER_FIRED,
            {"trigger_id": trg.id, "agent_id": trg.agent_id, "payload": payload, "trigger_type": "webhook"},
        )
    )

    def _log_publish_result(t: asyncio.Task) -> None:
        try:
            t.result()
        except Exception:  # noqa: BLE001
            logger.exception("TRIGGER_FIRED publish failed")

    task.add_done_callback(_log_publish_result)

    # Metrics -----------------------------------------------------------
    try:
        trigger_fired_total.inc()
    except Exception:  # pragma: no cover – guard against misconfig
        pass

    return {"status": "accepted"}


@router.get("/", response_model=List[TriggerSchema])
def list_triggers(
    db: Session = Depends(get_db),
    agent_id: Optional[int] = Query(None, description="Filter triggers by agent ID"),
):
    """
    List all triggers, optionally filtered by agent_id.
    """
    triggers = crud.get_triggers(db, agent_id=agent_id)
    return triggers
