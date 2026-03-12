"""Schemas for canonical conversation APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from zerg.utils.time import UTCBaseModel


class ConversationBindingResponse(UTCBaseModel):
    id: int
    surface_id: str
    provider: str
    binding_scope: str
    connector_id: int | None = None
    external_conversation_id: str
    binding_metadata: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ConversationSummaryResponse(UTCBaseModel):
    id: int
    kind: str
    title: str | None = None
    status: str
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(..., ge=0)
    binding_count: int = Field(..., ge=0)
    conversation_metadata: dict[str, Any] | None = None


class ConversationDetailResponse(ConversationSummaryResponse):
    bindings: list[ConversationBindingResponse]


class ConversationMessageResponse(UTCBaseModel):
    id: int
    conversation_id: int
    role: str
    direction: str
    sender_kind: str
    sender_display: str | None = None
    content: str
    content_blocks: list[Any] | None = None
    external_message_id: str | None = None
    parent_message_id: int | None = None
    archive_relpath: str | None = None
    message_metadata: dict[str, Any] | None = None
    internal: bool
    sent_at: datetime
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummaryResponse]
    total: int = Field(..., ge=0)


class ConversationMessagesResponse(BaseModel):
    messages: list[ConversationMessageResponse]
    total: int = Field(..., ge=0)
