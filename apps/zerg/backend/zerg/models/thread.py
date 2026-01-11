"""Thread and ThreadMessage models for agent conversations."""

from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.enums import ThreadType


class Thread(Base):
    __tablename__ = "agent_threads"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    title = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    # Store additional metadata like agent state
    agent_state = Column(MutableDict.as_mutable(JSON), nullable=True)
    memory_strategy = Column(String, default="buffer", nullable=True)
    thread_type = Column(
        SAEnum(ThreadType, native_enum=False, name="thread_type_enum"),
        default=ThreadType.CHAT.value,
        nullable=False,
    )  # Types: chat, scheduled, manual
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Define relationship with Agent
    agent = relationship("Agent", back_populates="threads")
    # Define relationship with ThreadMessage
    messages = relationship("ThreadMessage", back_populates="thread", cascade="all, delete-orphan")


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("agent_threads.id"))
    role = Column(String, nullable=False)  # "system", "user", "assistant", "tool"
    content = Column(Text, nullable=False)
    # Store *list* of tool call dicts emitted by OpenAI ChatCompleteion
    tool_calls = Column(MutableList.as_mutable(JSON), nullable=True)
    tool_call_id = Column(String, nullable=True)  # For tool responses
    name = Column(String, nullable=True)  # For tool messages
    sent_at = Column(DateTime(timezone=True), server_default=func.now())  # When user sent the message (UTC)
    processed = Column(Boolean, default=False, nullable=False)  # Track if message has been processed by agent
    message_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)  # Store additional metadata
    parent_id = Column(Integer, ForeignKey("thread_messages.id"), nullable=True)
    # Internal messages are orchestration artifacts (continuations, system notifications)
    # that should NOT be shown to users in chat history, but are needed for LLM context
    internal = Column(Boolean, default=False, nullable=False, server_default="false")

    # Define relationship with Thread
    thread = relationship("Thread", back_populates="messages")
