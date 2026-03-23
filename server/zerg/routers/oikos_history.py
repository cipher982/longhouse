"""Compatibility Oikos history endpoints (GET, DELETE)."""

import logging
from collections import deque
from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.models import ThreadMessage
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CommisToolInfo(BaseModel):
    """Tool executed by a commis."""

    tool_name: str
    status: str
    duration_ms: Optional[int] = None
    result_preview: Optional[str] = None
    error: Optional[str] = None


class CommisInfo(BaseModel):
    """Commis spawned by spawn_workspace_commis tool."""

    job_id: int
    task: str
    status: str
    summary: Optional[str] = None
    tools: List[CommisToolInfo] = []


class ToolCallInfo(BaseModel):
    """Tool call made by oikos."""

    tool_call_id: str
    tool_name: str
    args: Optional[dict] = None
    result: Optional[str] = None
    commis: Optional[CommisInfo] = None


class OikosChatMessage(UTCBaseModel):
    """Single chat message in history."""

    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")
    origin_surface_id: Optional[str] = Field(None)
    delivery_surface_id: Optional[str] = Field(None)
    visibility: Optional[str] = Field(None)
    usage: Optional[dict] = Field(None)
    tool_calls: Optional[List[ToolCallInfo]] = Field(None)


class OikosHistoryResponse(BaseModel):
    """Chat history response."""

    messages: List[OikosChatMessage] = Field(..., description="List of messages")
    total: int = Field(..., description="Total message count")


# ---------------------------------------------------------------------------
# Commis activity helper
# ---------------------------------------------------------------------------


def _fetch_commis_activity(db: Session, fiche_id: int, tool_call_ids: list[str]) -> dict[str, dict]:
    """Fetch commis activity for spawn_workspace_commis tool calls.

    Queries RunEvent to build a complete picture of commis execution
    (spawned → started → tool calls → complete → summary).
    """
    from zerg.models import Run
    from zerg.models import RunEvent

    if not tool_call_ids:
        return {}

    run_ids = [r.id for r in db.query(Run.id).filter(Run.fiche_id == fiche_id).all()]
    if not run_ids:
        return {}

    events = (
        db.query(RunEvent)
        .filter(
            RunEvent.run_id.in_(run_ids),
            RunEvent.event_type.in_(
                [
                    "oikos_tool_started",
                    "commis_spawned",
                    "commis_started",
                    "commis_tool_started",
                    "commis_tool_completed",
                    "commis_tool_failed",
                    "commis_complete",
                    "commis_summary_ready",
                ]
            ),
        )
        .order_by(RunEvent.id.asc())
        .all()
    )

    job_activity: dict[int, dict] = {}
    result: dict[str, dict] = {}
    for e in events:
        payload = e.payload or {}
        job_id = payload.get("job_id")

        if e.event_type == "commis_spawned" and job_id:
            job_activity[job_id] = {
                "job_id": job_id,
                "task": payload.get("task", ""),
                "status": "spawned",
                "summary": None,
                "tools": [],
            }
            tc_id = payload.get("tool_call_id")
            if tc_id and tc_id in tool_call_ids:
                result[tc_id] = job_activity[job_id]
        elif e.event_type == "commis_started" and job_id and job_id in job_activity:
            job_activity[job_id]["status"] = "running"
        elif e.event_type == "commis_tool_started" and job_id and job_id in job_activity:
            job_activity[job_id]["tools"].append(
                {
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_name": payload.get("tool_name", "unknown"),
                    "status": "running",
                }
            )
        elif e.event_type == "commis_tool_completed" and job_id and job_id in job_activity:
            tc_id = payload.get("tool_call_id")
            for t in job_activity[job_id]["tools"]:
                if t["tool_call_id"] == tc_id:
                    t["status"] = "completed"
                    t["duration_ms"] = payload.get("duration_ms")
                    t["result_preview"] = payload.get("result_preview")
                    break
        elif e.event_type == "commis_tool_failed" and job_id and job_id in job_activity:
            tc_id = payload.get("tool_call_id")
            for t in job_activity[job_id]["tools"]:
                if t["tool_call_id"] == tc_id:
                    t["status"] = "failed"
                    t["duration_ms"] = payload.get("duration_ms")
                    t["error"] = payload.get("error")
                    break
        elif e.event_type == "commis_complete" and job_id and job_id in job_activity:
            job_activity[job_id]["status"] = "complete" if payload.get("status") == "success" else "failed"
        elif e.event_type == "commis_summary_ready" and job_id and job_id in job_activity:
            job_activity[job_id]["summary"] = payload.get("summary")

    # Fallback: correlate oikos_tool_started → commis_spawned for legacy events
    events_by_run: dict[int, list] = {}
    for e in events:
        events_by_run.setdefault(e.run_id, []).append(e)

    for run_events in events_by_run.values():
        pending: deque[str] = deque()
        for e in run_events:
            payload = e.payload or {}
            if e.event_type == "oikos_tool_started" and payload.get("tool_name") == "spawn_workspace_commis":
                tc_id = payload.get("tool_call_id")
                if tc_id in tool_call_ids and tc_id not in result:
                    pending.append(tc_id)
            elif e.event_type == "commis_spawned" and not payload.get("tool_call_id"):
                if not pending:
                    continue
                job_id = payload.get("job_id")
                if job_id and job_id in job_activity:
                    tc_id = pending.popleft()
                    if tc_id not in result:
                        result[tc_id] = job_activity[job_id]

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _surface_info(msg: ThreadMessage) -> tuple[Optional[str], Optional[str], Optional[str]]:
    metadata = msg.message_metadata or {}
    surface = metadata.get("surface") if isinstance(metadata, dict) else None
    if not isinstance(surface, dict):
        return None, None, None
    return (
        surface.get("origin_surface_id"),
        surface.get("delivery_surface_id"),
        surface.get("visibility"),
    )


@router.get("/history", response_model=OikosHistoryResponse, deprecated=True)
def oikos_history(
    limit: int = 50,
    offset: int = 0,
    surface_id: str = "web",
    view: str = "surface",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosHistoryResponse:
    """Get conversation history from Oikos thread."""
    if view not in {"surface", "all"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="view must be 'surface' or 'all'")

    from zerg.services.oikos_service import OikosService

    oikos_service = OikosService(db)
    fiche = oikos_service.get_or_create_oikos_fiche(current_user.id)
    thread = oikos_service.get_or_create_oikos_thread(current_user.id, fiche)

    query = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),
            ThreadMessage.internal.is_(False),
        )
        .order_by(ThreadMessage.sent_at.asc())
    )

    all_messages = query.all()

    def _visible(msg: ThreadMessage) -> bool:
        origin, delivery, visibility = _surface_info(msg)
        if visibility == "internal":
            return False
        if view == "all":
            return True
        if not origin and not delivery:
            return surface_id == "web"
        return origin == surface_id or delivery == surface_id

    filtered = [msg for msg in all_messages if _visible(msg)]
    total = len(filtered)
    page = filtered[offset : offset + limit]

    # Batch-fetch commis activity for spawn_workspace_commis tool calls
    spawn_tc_ids = []
    for msg in page:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "spawn_workspace_commis" and tc.get("id"):
                    spawn_tc_ids.append(tc["id"])

    commis_map: dict[str, dict] = {}
    if spawn_tc_ids:
        commis_map = _fetch_commis_activity(db, fiche.id, spawn_tc_ids)

    # Map tool_call_id → result from tool messages
    tool_results: dict[str, str] = {}
    tool_msgs = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role == "tool",
            ThreadMessage.tool_call_id.isnot(None),
        )
        .all()
    )
    for tm in tool_msgs:
        if tm.tool_call_id:
            tool_results[tm.tool_call_id] = tm.content or ""

    # Build response
    chat_messages = []
    for msg in page:
        origin, delivery, visibility = _surface_info(msg)
        tool_calls_info = None
        if msg.role == "assistant" and msg.tool_calls:
            tool_calls_info = []
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                tc_name = tc.get("name", "unknown")

                commis_info = None
                if tc_name == "spawn_workspace_commis" and tc_id in commis_map:
                    wa = commis_map[tc_id]
                    commis_info = CommisInfo(
                        job_id=wa["job_id"],
                        task=wa["task"],
                        status=wa["status"],
                        summary=wa.get("summary"),
                        tools=[
                            CommisToolInfo(
                                tool_name=t["tool_name"],
                                status=t["status"],
                                duration_ms=t.get("duration_ms"),
                                result_preview=t.get("result_preview"),
                                error=t.get("error"),
                            )
                            for t in wa.get("tools", [])
                        ],
                    )

                tool_calls_info.append(
                    ToolCallInfo(
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        args=tc.get("args"),
                        result=tool_results.get(tc_id),
                        commis=commis_info,
                    )
                )

        chat_messages.append(
            OikosChatMessage(
                role=msg.role,
                content=msg.content or "",
                timestamp=msg.sent_at,
                origin_surface_id=origin,
                delivery_surface_id=delivery,
                visibility=visibility,
                usage=(msg.message_metadata or {}).get("usage") if msg.role == "assistant" else None,
                tool_calls=tool_calls_info,
            )
        )

    return OikosHistoryResponse(messages=chat_messages, total=total)


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT, deprecated=True)
def oikos_clear_history(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> None:
    """Compatibility alias for resetting Oikos memory."""
    from zerg.services.oikos_service import OikosService

    service = OikosService(db)
    service.clear_thread_and_surface_conversation(
        owner_id=current_user.id,
        surface_id="web",
        external_conversation_id="web:main",
    )
