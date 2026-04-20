"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.services.session_messages import deliver_queued_session_messages
from zerg.services.session_messages import is_session_message_deliverable_state
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.write_serializer import get_write_serializer

router = APIRouter(prefix="/agents/runtime", tags=["agents"])


@router.post("/events/batch", response_model=RuntimeEventBatchResult)
async def ingest_runtime_event_batch(
    payload: RuntimeEventBatchIngest,
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RuntimeEventBatchResult:
    """Ingest normalized runtime events and materialize runtime state."""
    try:
        ws = get_write_serializer()
        events = payload.events

        def _do(wdb: Session) -> RuntimeEventBatchResult:
            return ingest_runtime_events(wdb, events)

        result = await ws.execute_or_direct(_do, db, label="runtime-events")

        session_ids = sorted({event.session_id for event in events if event.session_id is not None}, key=str)
        if session_ids:
            sessions = db.query(AgentSession).filter(AgentSession.id.in_(session_ids)).all()
            if sessions:
                now = datetime.now(timezone.utc)
                owner_id = resolve_session_message_owner_id(db, _token)
                runtime_state_map = load_runtime_state_map(db, session_ids)
                for session in sessions:
                    current_state = resolve_runtime_overlay(
                        session,
                        last_activity_at=session.last_activity_at,
                        runtime_state_map=runtime_state_map,
                        now=now,
                    ).presence_state
                    if not is_session_message_deliverable_state(current_state):
                        continue
                    await deliver_queued_session_messages(
                        db=db,
                        owner_id=owner_id,
                        target_session_id=session.id,
                        target_presence_state=current_state,
                    )

        return result
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest runtime events",
        ) from exc
