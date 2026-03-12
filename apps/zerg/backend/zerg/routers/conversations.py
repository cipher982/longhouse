"""Read/search APIs for canonical conversations."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.conversation import ConversationMessage
from zerg.schemas.conversation_schemas import ConversationBindingResponse
from zerg.schemas.conversation_schemas import ConversationDetailResponse
from zerg.schemas.conversation_schemas import ConversationListResponse
from zerg.schemas.conversation_schemas import ConversationMessageResponse
from zerg.schemas.conversation_schemas import ConversationMessagesResponse
from zerg.schemas.conversation_schemas import ConversationReplyRequest
from zerg.schemas.conversation_schemas import ConversationReplyResponse
from zerg.schemas.conversation_schemas import ConversationSummaryResponse
from zerg.services.conversation_reply_service import ConversationReplyError
from zerg.services.conversation_reply_service import ConversationReplyRequest as ConversationReplyServiceRequest
from zerg.services.conversation_reply_service import ConversationReplyService
from zerg.services.conversation_service import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"], dependencies=[Depends(get_current_user)])


def _serialize_summary(db: Session, owner_id: int, conversation) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        id=conversation.id,
        kind=conversation.kind,
        title=conversation.title,
        status=conversation.status,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=ConversationService.count_messages(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
        ),
        binding_count=ConversationService.count_bindings(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
        ),
        conversation_metadata=conversation.conversation_metadata,
    )


@router.get("", response_model=ConversationListResponse)
def list_conversations(
    kind: str | None = Query(default=None),
    status_filter: str | None = Query(default="active", alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ConversationListResponse:
    conversations = ConversationService.list_conversations(
        db,
        owner_id=current_user.id,
        kind=kind,
        status=status_filter,
        limit=limit,
    )
    return ConversationListResponse(
        conversations=[_serialize_summary(db, current_user.id, conversation) for conversation in conversations],
        total=len(conversations),
    )


@router.get("/search", response_model=ConversationListResponse)
def search_conversations(
    q: str = Query(..., min_length=1),
    kind: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ConversationListResponse:
    conversations = ConversationService.search_conversations(
        db,
        owner_id=current_user.id,
        query=q,
        kind=kind,
        limit=limit,
    )
    return ConversationListResponse(
        conversations=[_serialize_summary(db, current_user.id, conversation) for conversation in conversations],
        total=len(conversations),
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ConversationDetailResponse:
    conversation = ConversationService.get_conversation(
        db,
        owner_id=current_user.id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    bindings = ConversationService.list_bindings(
        db,
        owner_id=current_user.id,
        conversation_id=conversation.id,
    )
    summary = _serialize_summary(db, current_user.id, conversation)
    return ConversationDetailResponse(
        **summary.model_dump(),
        bindings=[
            ConversationBindingResponse(
                id=binding.id,
                surface_id=binding.surface_id,
                provider=binding.provider,
                binding_scope=binding.binding_scope,
                connector_id=binding.connector_id,
                external_conversation_id=binding.external_conversation_id,
                binding_metadata=binding.binding_metadata,
                created_at=binding.created_at,
                updated_at=binding.updated_at,
            )
            for binding in bindings
        ],
    )


@router.get("/{conversation_id}/messages", response_model=ConversationMessagesResponse)
def list_messages(
    conversation_id: int,
    include_internal: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ConversationMessagesResponse:
    try:
        messages = ConversationService.list_messages(
            db,
            owner_id=current_user.id,
            conversation_id=conversation_id,
            include_internal=include_internal,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return ConversationMessagesResponse(
        messages=[
            ConversationMessageResponse(
                id=message.id,
                conversation_id=message.conversation_id,
                role=message.role,
                direction=message.direction,
                sender_kind=message.sender_kind,
                sender_display=message.sender_display,
                content=message.content,
                content_blocks=message.content_blocks,
                external_message_id=message.external_message_id,
                parent_message_id=message.parent_message_id,
                archive_relpath=message.archive_relpath,
                message_metadata=message.message_metadata,
                internal=message.internal,
                sent_at=message.sent_at,
                created_at=message.created_at,
                updated_at=message.updated_at,
            )
            for message in messages
        ],
        total=len(messages),
    )


@router.post("/{conversation_id}/reply", response_model=ConversationReplyResponse)
def reply_to_conversation(
    conversation_id: int,
    payload: ConversationReplyRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ConversationReplyResponse:
    service = ConversationReplyService(db)
    try:
        result = service.reply(
            ConversationReplyServiceRequest(
                owner_id=current_user.id,
                conversation_id=conversation_id,
                body_text=payload.body,
                reply_all=payload.reply_all,
                role="user",
                sender_kind="human",
                sender_display=getattr(current_user, "display_name", None) or current_user.email,
            )
        )
        message = db.get(ConversationMessage, result.message_id)
    except ConversationReplyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Reply message not found after send",
        )

    return ConversationReplyResponse(
        conversation_id=result.conversation_id,
        provider=result.provider,
        thread_id=result.thread_id,
        subject=result.subject,
        reply_all=payload.reply_all,
        to_emails=list(result.to_emails),
        cc_emails=list(result.cc_emails),
        message=ConversationMessageResponse(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,
            direction=message.direction,
            sender_kind=message.sender_kind,
            sender_display=message.sender_display,
            content=message.content,
            content_blocks=message.content_blocks,
            external_message_id=message.external_message_id,
            parent_message_id=message.parent_message_id,
            archive_relpath=message.archive_relpath,
            message_metadata=message.message_metadata,
            internal=message.internal,
            sent_at=message.sent_at,
            created_at=message.created_at,
            updated_at=message.updated_at,
        ),
    )
