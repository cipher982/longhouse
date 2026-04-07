"""Oikos history endpoints for the web assistant surface."""

import logging
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


class ToolCallInfo(BaseModel):
    """Tool call made by oikos."""

    tool_call_id: str
    tool_name: str
    args: Optional[dict] = None
    result: Optional[str] = None


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


@router.get("/history", response_model=OikosHistoryResponse)
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
                tool_calls_info.append(
                    ToolCallInfo(
                        tool_call_id=tc.get("id", ""),
                        tool_name=tc.get("name", "unknown"),
                        args=tc.get("args"),
                        result=tool_results.get(tc.get("id", "")),
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


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
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
