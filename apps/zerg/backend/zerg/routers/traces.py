"""Trace Explorer API endpoints.

Admin-only endpoints for debugging traces across concierge runs, commis, and LLM calls.
"""

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_admin
from zerg.services.trace_debugger import TraceDebugger

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("/")
async def list_traces(
    limit: int = Query(20, le=100, description="Maximum traces to return"),
    offset: int = Query(0, ge=0, description="Number of traces to skip"),
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """List recent traces (admin only).

    Returns recent traces sorted by creation time for discovery.
    Use this to find trace IDs to debug.
    """
    debugger = TraceDebugger(db)
    return debugger.list_recent(limit, offset)


@router.get("/{trace_id}")
async def get_trace(
    trace_id: str,
    level: Literal["summary", "full", "errors"] = Query("summary", description="Detail level"),
    max_events: int = Query(100, le=500, description="Maximum events to return"),
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """Get unified trace timeline (admin only).

    Returns a unified timeline of events across concierge runs, commis, and LLM calls.

    Levels:
    - summary: High-level timeline with key events (default)
    - full: Include LLM message details
    - errors: Only show errors and anomalies
    """
    debugger = TraceDebugger(db)
    result = debugger.get_trace(trace_id, level, max_events=max_events)

    if not result:
        raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")

    # Redact sensitive data (API keys, secrets in tool outputs)
    return debugger.redact_secrets(result)
