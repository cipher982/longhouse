"""Conversation discovery tools for Oikos.

Expose canonical human-visible conversations to Oikos so it can search and
read cross-surface threads without depending on the private Oikos transcript.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from zerg.connectors.context import get_credential_resolver
from zerg.database import db_session
from zerg.services.conversation_service import ConversationService
from zerg.services.oikos_context import get_oikos_context
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.types.tools import Tool as StructuredTool


class SearchConversationsInput(BaseModel):
    """Input schema for search_conversations."""

    query: str = Field(description="Search query across conversation titles and message content")
    kind: str | None = Field(default=None, description="Optional conversation kind filter")
    limit: int = Field(default=10, ge=1, le=50, description="Max conversations to return")


class ReadConversationInput(BaseModel):
    """Input schema for read_conversation."""

    conversation_id: int = Field(description="Conversation ID")
    include_internal: bool = Field(default=False, description="Include internal-only messages")
    limit: int = Field(default=100, ge=1, le=500, description="Max messages to return")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


def _get_owner_id() -> int | None:
    ctx = get_oikos_context()
    if ctx and ctx.owner_id:
        return ctx.owner_id

    resolver = get_credential_resolver()
    if resolver and resolver.owner_id:
        return resolver.owner_id

    return None


def _serialize_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_summary(db, owner_id: int, conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "kind": conversation.kind,
        "title": conversation.title,
        "status": conversation.status,
        "last_message_at": _serialize_timestamp(conversation.last_message_at),
        "created_at": _serialize_timestamp(conversation.created_at),
        "updated_at": _serialize_timestamp(conversation.updated_at),
        "message_count": ConversationService.count_messages(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
        ),
        "conversation_metadata": conversation.conversation_metadata,
    }


def search_conversations(
    query: str,
    kind: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search canonical conversations for the current user."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot search conversations without user context",
        )

    if not query or not query.strip():
        return tool_error(ErrorType.VALIDATION_ERROR, "query cannot be empty")

    with db_session() as db:
        conversations = ConversationService.search_conversations(
            db,
            owner_id=owner_id,
            query=query.strip(),
            kind=kind,
            limit=limit,
        )
        results = [_serialize_summary(db, owner_id, conversation) for conversation in conversations]

    return tool_success(
        {
            "query": query,
            "total": len(results),
            "conversations": results,
        }
    )


def read_conversation(
    conversation_id: int,
    include_internal: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read a conversation with bindings and messages."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot read conversations without user context",
        )

    with db_session() as db:
        conversation = ConversationService.get_conversation(
            db,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        if conversation is None:
            return tool_error(ErrorType.NOT_FOUND, f"Conversation not found: {conversation_id}")

        bindings = ConversationService.list_bindings(
            db,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        messages = ConversationService.list_messages(
            db,
            owner_id=owner_id,
            conversation_id=conversation_id,
            include_internal=include_internal,
            limit=limit,
            offset=offset,
        )

        payload = _serialize_summary(db, owner_id, conversation)
        payload["bindings"] = [
            {
                "id": binding.id,
                "surface_id": binding.surface_id,
                "provider": binding.provider,
                "binding_scope": binding.binding_scope,
                "connector_id": binding.connector_id,
                "external_conversation_id": binding.external_conversation_id,
                "binding_metadata": binding.binding_metadata,
                "created_at": _serialize_timestamp(binding.created_at),
                "updated_at": _serialize_timestamp(binding.updated_at),
            }
            for binding in bindings
        ]
        payload["messages"] = [
            {
                "id": message.id,
                "role": message.role,
                "direction": message.direction,
                "sender_kind": message.sender_kind,
                "sender_display": message.sender_display,
                "content": message.content,
                "external_message_id": message.external_message_id,
                "archive_relpath": message.archive_relpath,
                "message_metadata": message.message_metadata,
                "internal": message.internal,
                "sent_at": _serialize_timestamp(message.sent_at),
            }
            for message in messages
        ]
        payload["total_messages"] = len(payload["messages"])

    return tool_success(payload)


TOOLS = [
    StructuredTool.from_function(
        func=search_conversations,
        name="search_conversations",
        description="Search canonical conversations by title and message content for the current user.",
        args_schema=SearchConversationsInput,
    ),
    StructuredTool.from_function(
        func=read_conversation,
        name="read_conversation",
        description="Read a canonical conversation with bindings and recent messages.",
        args_schema=ReadConversationInput,
    ),
]
