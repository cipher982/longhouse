"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import ingest_runtime_events

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
        result = ingest_runtime_events(db, payload.events)
        db.commit()
        return result
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest runtime events",
        ) from exc
