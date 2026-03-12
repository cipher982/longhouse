"""Oikos conversation list/read/search endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.conversation import Conversation
from zerg.models.conversation import ConversationBinding
from zerg.models.conversation import ConversationMessage
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.conversation_service import ConversationService
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="", tags=["oikos"])


class ConversationBindingInfo(UTCBaseModel):
    id: int
    surface_id: str
    provider: str
    binding_scope: str
    connector_id: Optional[int] = None
    external_conversation_id: str
    binding_metadata: Optional[dict] = None


class ConversationSummary(UTCBaseModel):
    id: int
    kind: str
    title: Optional[str] = None
    status: str
    last_message_at: Optional[datetime] = None


class ConversationDetail(ConversationSummary):
    conversation_metadata: Optional[dict] = None
    message_count: int
    bindings: List[ConversationBindingInfo] = Field(default_factory=list)


class ConversationMessageInfo(UTCBaseModel):
    id: int
    role: str
    direction: str
    sender_kind: str
    sender_display: Optional[str] = None
    content: str
    external_message_id: Optional[str] = None
    archive_relpath: Optional[str] = None
    internal: bool
    timestamp: datetime
    message_metadata: Optional[dict] = None


def _to_summary(row: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=row.id,
        kind=row.kind,
        title=row.title,
        status=row.status,
        last_message_at=row.last_message_at,
    )


def _to_binding(row: ConversationBinding) -> ConversationBindingInfo:
    return ConversationBindingInfo(
        id=row.id,
        surface_id=row.surface_id,
        provider=row.provider,
        binding_scope=row.binding_scope,
        connector_id=row.connector_id,
        external_conversation_id=row.external_conversation_id,
        binding_metadata=row.binding_metadata,
    )


def _to_message(row: ConversationMessage) -> ConversationMessageInfo:
    return ConversationMessageInfo(
        id=row.id,
        role=row.role,
        direction=row.direction,
        sender_kind=row.sender_kind,
        sender_display=row.sender_display,
        content=row.content,
        external_message_id=row.external_message_id,
        archive_relpath=row.archive_relpath,
        internal=row.internal,
        timestamp=row.sent_at,
        message_metadata=row.message_metadata,
    )


@router.get("/conversations", response_model=List[ConversationSummary])
def list_oikos_conversations(
    kind: Optional[str] = None,
    status_filter: Optional[str] = "active",
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[ConversationSummary]:
    rows = ConversationService.list_conversations(
        db,
        owner_id=current_user.id,
        kind=kind,
        status=status_filter,
        limit=limit,
    )
    return [_to_summary(row) for row in rows]


@router.get("/conversations/search", response_model=List[ConversationSummary])
def search_oikos_conversations(
    q: str,
    kind: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[ConversationSummary]:
    rows = ConversationService.search_conversations(
        db,
        owner_id=current_user.id,
        query=q,
        kind=kind,
        limit=limit,
    )
    return [_to_summary(row) for row in rows]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_oikos_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> ConversationDetail:
    row = ConversationService.get_conversation(
        db,
        owner_id=current_user.id,
        conversation_id=conversation_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    bindings = (
        db.query(ConversationBinding).filter(ConversationBinding.conversation_id == row.id).order_by(ConversationBinding.id.asc()).all()
    )

    return ConversationDetail(
        id=row.id,
        kind=row.kind,
        title=row.title,
        status=row.status,
        last_message_at=row.last_message_at,
        conversation_metadata=row.conversation_metadata,
        message_count=ConversationService.count_messages(
            db,
            owner_id=current_user.id,
            conversation_id=row.id,
            include_internal=False,
        ),
        bindings=[_to_binding(binding) for binding in bindings],
    )


@router.get("/conversations/{conversation_id}/messages", response_model=List[ConversationMessageInfo])
def list_oikos_conversation_messages(
    conversation_id: int,
    include_internal: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[ConversationMessageInfo]:
    try:
        rows = ConversationService.list_messages(
            db,
            owner_id=current_user.id,
            conversation_id=conversation_id,
            include_internal=include_internal,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [_to_message(row) for row in rows]
