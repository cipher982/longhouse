"""Conversation models for human-visible multi-surface transcripts."""

from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class Conversation(Base):
    """Canonical human-visible conversation across surfaces."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owner_kind_last_message", "owner_id", "kind", "last_message_at"),)

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(32), nullable=False, index=True)
    title = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="active", server_default="active", index=True)
    conversation_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    owner = relationship("User", backref="conversations")
    bindings = relationship("ConversationBinding", back_populates="conversation", cascade="all, delete-orphan")
    messages = relationship("ConversationMessage", back_populates="conversation", cascade="all, delete-orphan")


class ConversationBinding(Base):
    """Map a durable conversation to a surface-native thread identifier."""

    __tablename__ = "conversation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "surface_id",
            "provider",
            "binding_scope",
            "external_conversation_id",
            name="uix_conversation_binding_owner_surface_provider_scope_external",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    surface_id = Column(String(64), nullable=False, index=True)
    provider = Column(String(64), nullable=False, default="default", server_default="default", index=True)
    binding_scope = Column(String(255), nullable=False, default="", server_default="")
    connector_id = Column(Integer, ForeignKey("connectors.id", ondelete="SET NULL"), nullable=True, index=True)
    external_conversation_id = Column(String(255), nullable=False)
    binding_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversation = relationship("Conversation", back_populates="bindings")
    owner = relationship("User")
    connector = relationship("Connector")


class ConversationMessage(Base):
    """Canonical message row for a conversation."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "external_message_id",
            name="uix_conversation_message_external",
        ),
        Index("ix_conversation_messages_conversation_sent_at", "conversation_id", "sent_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(32), nullable=False, index=True)
    direction = Column(String(16), nullable=False, default="incoming", server_default="incoming", index=True)
    sender_kind = Column(String(32), nullable=False, default="human", server_default="human", index=True)
    sender_display = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    content_blocks = Column(MutableList.as_mutable(JSON), nullable=True)
    external_message_id = Column(String(255), nullable=True)
    parent_message_id = Column(Integer, ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True)
    archive_relpath = Column(String(1024), nullable=True)
    message_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)
    internal = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversation = relationship("Conversation", back_populates="messages")
    parent = relationship("ConversationMessage", remote_side=[id])
