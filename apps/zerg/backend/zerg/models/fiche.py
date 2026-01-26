"""Fiche models for the fiche platform."""

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
from zerg.models.enums import FicheStatus


class Fiche(Base):
    __tablename__ = "fiches"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    status = Column(
        SAEnum(FicheStatus, native_enum=False, name="fiche_status_enum"),
        default=FicheStatus.IDLE.value,
    )
    system_instructions = Column(Text, nullable=False)
    task_instructions = Column(Text, nullable=False)
    schedule = Column(String, nullable=True)  # CRON expression or interval
    model = Column(String, nullable=False)  # Model to use (no default)
    config = Column(MutableDict.as_mutable(JSON), nullable=True)  # Additional configuration as JSON

    # -------------------------------------------------------------------
    # Tool allowlist – controls which tools this fiche can use
    # -------------------------------------------------------------------
    # Empty/NULL means all tools are allowed. Otherwise, it's a JSON array
    # of tool names that the fiche is allowed to use. Supports wildcards
    # like "http_*" to allow all HTTP tools.
    allowed_tools = Column(MutableList.as_mutable(JSON), nullable=True)

    # -------------------------------------------------------------------
    # Ownership – every fiche belongs to *one* user (creator / owner).
    # -------------------------------------------------------------------

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Bidirectional relationship so ``fiche.owner`` returns the User row and
    # ``user.fiches`` lists all fiches owned by the user.
    owner = relationship("User", backref="fiches")
    # Scheduling metadata
    # Next time this fiche is currently expected to run.  Updated by the
    # SchedulerService whenever a cron job is (re)scheduled.
    next_course_at = Column(DateTime, nullable=True)
    # Last time a scheduled (or manual) run actually finished successfully.
    last_course_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # --------------------------------------------------
    # *run_on_schedule* has been removed – the presence of a non-NULL cron string
    # in the *schedule* column now **alone** determines whether the Scheduler
    # service will run the fiche.  A NULL / empty schedule means "disabled".
    # --------------------------------------------------
    last_error = Column(Text, nullable=True)  # Store the last error message

    # Define relationship with FicheMessage
    messages = relationship("FicheMessage", back_populates="fiche", cascade="all, delete-orphan")
    # Define relationship with Thread
    threads = relationship("Thread", back_populates="fiche", cascade="all, delete-orphan")

    # Relationship to execution courses (added in the *Course History* feature).
    courses = relationship("Course", back_populates="fiche", cascade="all, delete-orphan")


class FicheMessage(Base):
    __tablename__ = "fiche_messages"

    id = Column(Integer, primary_key=True, index=True)
    fiche_id = Column(Integer, ForeignKey("fiches.id"))
    role = Column(String, nullable=False)  # "system", "user", "assistant"
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

    # Define relationship with Fiche
    fiche = relationship("Fiche", back_populates="messages")
