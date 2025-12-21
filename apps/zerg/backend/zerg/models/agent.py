"""Agent models for the agent platform."""

from sqlalchemy import JSON
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
from zerg.models.enums import AgentStatus


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    status = Column(
        SAEnum(AgentStatus, native_enum=False, name="agent_status_enum"),
        default=AgentStatus.IDLE.value,
    )
    system_instructions = Column(Text, nullable=False)
    task_instructions = Column(Text, nullable=False)
    schedule = Column(String, nullable=True)  # CRON expression or interval
    model = Column(String, nullable=False)  # Model to use (no default)
    config = Column(MutableDict.as_mutable(JSON), nullable=True)  # Additional configuration as JSON

    # -------------------------------------------------------------------
    # Tool allowlist – controls which tools this agent can use
    # -------------------------------------------------------------------
    # Empty/NULL means all tools are allowed. Otherwise, it's a JSON array
    # of tool names that the agent is allowed to use. Supports wildcards
    # like "http_*" to allow all HTTP tools.
    allowed_tools = Column(MutableList.as_mutable(JSON), nullable=True)

    # -------------------------------------------------------------------
    # Ownership – every agent belongs to *one* user (creator / owner).
    # -------------------------------------------------------------------

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Bidirectional relationship so ``agent.owner`` returns the User row and
    # ``user.agents`` lists all agents owned by the user.
    owner = relationship("User", backref="agents")
    # Scheduling metadata
    # Next time this agent is currently expected to run.  Updated by the
    # SchedulerService whenever a cron job is (re)scheduled.
    next_run_at = Column(DateTime, nullable=True)
    # Last time a scheduled (or manual) run actually finished successfully.
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # --------------------------------------------------
    # *run_on_schedule* has been removed – the presence of a non-NULL cron string
    # in the *schedule* column now **alone** determines whether the Scheduler
    # service will run the agent.  A NULL / empty schedule means "disabled".
    # --------------------------------------------------
    last_error = Column(Text, nullable=True)  # Store the last error message

    # Define relationship with AgentMessage
    messages = relationship("AgentMessage", back_populates="agent", cascade="all, delete-orphan")
    # Define relationship with Thread
    threads = relationship("Thread", back_populates="agent", cascade="all, delete-orphan")

    # Relationship to execution runs (added in the *Run History* feature).
    runs = relationship("AgentRun", back_populates="agent", cascade="all, delete-orphan")


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    role = Column(String, nullable=False)  # "system", "user", "assistant"
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

    # Define relationship with Agent
    agent = relationship("Agent", back_populates="messages")
