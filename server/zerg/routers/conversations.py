"""Conversations API — read-only query surface for email and other conversations."""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.services.conversation_service import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"], dependencies=[Depends(get_current_user)])


@router.get("", status_code=status.HTTP_200_OK)
def list_conversations(
    kind: str | None = Query(None, description="Filter by conversation kind (e.g. 'email')"),
    conv_status: str | None = Query("active", alias="status", description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    rows = ConversationService.list_conversations(
        db,
        owner_id=current_user.id,
        kind=kind,
        status=conv_status,
        limit=limit,
    )
    return [
        {
            "id": c.id,
            "kind": c.kind,
            "title": c.title,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        }
        for c in rows
    ]
