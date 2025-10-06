"""API router for Triggers (milestone M1).

Currently only supports simple *webhook* triggers that, when invoked, publish
an EventType.TRIGGER_FIRED event.  The SchedulerService listens for that event
and executes the associated agent immediately.
"""

# typing and forward-ref convenience
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
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
from zerg.services.scheduler_service import scheduler_service

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


@router.post("/{trigger_id}/events", status_code=status.HTTP_202_ACCEPTED)
async def fire_trigger_event(
    *,
    trigger_id: int = Path(..., gt=0),
    payload: Dict = Body(default={}),  # Arbitrary JSON body
    x_zerg_timestamp: str = Header(..., alias="X-Zerg-Timestamp"),
    x_zerg_signature: str = Header(..., alias="X-Zerg-Signature"),
    db: Session = Depends(get_db),
):
    """Webhook endpoint that fires a trigger event.

    Security: the caller must sign the request body using HMAC-SHA256.

    Signature string to hash:
        "{timestamp}.{raw_body}"

    where *timestamp* is the same value sent in `X-Zerg-Timestamp` header and
    *raw_body* is the exact JSON body (no whitespace changes).  The hex-encoded
    digest is provided via `X-Zerg-Signature` header.
    """

    # 1) Validate timestamp (prevents replay attacks)
    try:
        ts_int = int(x_zerg_timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid X-Zerg-Timestamp header")

    now = int(time.time())
    if abs(now - ts_int) > constants.TRIGGER_TIMESTAMP_TOLERANCE_S:
        raise HTTPException(status_code=400, detail="Timestamp skew too large")

    # 2) Recompute HMAC and compare (constant-time)
    signing_secret = constants.TRIGGER_SIGNING_SECRET.encode()

    # We must use the *raw* body as delivered on the wire, not the already-
    # parsed `payload` dict.  FastAPI gives us access via request.body() but
    # we'd need Request object; instead re-serialise deterministically.
    body_serialised = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    data_to_sign = f"{x_zerg_timestamp}.{body_serialised}".encode()
    expected_sig = hmac.new(signing_secret, data_to_sign, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, x_zerg_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # 3) Check trigger exists
    trg = crud.get_trigger(db, trigger_id)
    if trg is None:
        raise HTTPException(status_code=404, detail="Trigger not found")

    # 4) Publish event on internal bus
    await event_bus.publish(
        EventType.TRIGGER_FIRED,
        {"trigger_id": trg.id, "agent_id": trg.agent_id, "payload": payload},
    )

    # Metrics -----------------------------------------------------------
    try:
        trigger_fired_total.inc()
    except Exception:  # pragma: no cover – guard against misconfig
        pass

    # 5) Fire agent immediately (non-blocking)
    await scheduler_service.run_agent_task(trg.agent_id)  # type: ignore[arg-type]

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
