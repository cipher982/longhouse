"""Commis barrier models for parallel commis coordination.

Implements the barrier synchronization pattern for multi-commis execution:
- CommisBarrier: tracks a batch of parallel commis for a concierge course
- CommisBarrierJob: individual commis job in a barrier with result caching

Two-Phase Commit Pattern:
1. spawn_commis creates CommisJob with status='created' (not queued)
2. After ALL spawn_commis processed, create CommisBarrier + CommisBarrierJob records
3. Atomic: flip all jobs from 'created' to 'queued'
4. Commis can now pick them up (barrier guaranteed to exist)

This prevents the "fast commis" race where a commis finishes before
the barrier exists.
"""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class CommisBarrier(Base):
    """Tracks a batch of parallel commis for a concierge course.

    Implements barrier synchronization: concierge waits until ALL commis
    complete before resuming. Uses atomic counter increment + status guard
    to prevent double-resume race conditions.

    Status transitions:
    - waiting: initial state, waiting for commis to complete
    - resuming: claimed by the commis that triggers resume (prevents double)
    - completed: resume finished, barrier is done
    - failed: concierge or resume failed, barrier abandoned
    """

    __tablename__ = "commis_barriers"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to course - one barrier per course
    # ON DELETE CASCADE: if run is deleted, barrier is automatically removed
    course_id = Column(
        Integer,
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # One barrier per course
        index=True,
    )

    # Counter-based barrier tracking (more reliable than array mutations)
    expected_count = Column(Integer, nullable=False)  # How many commis to wait for
    completed_count = Column(Integer, nullable=False, default=0)

    # Status guard for atomic resume claim
    # waiting -> resuming -> completed
    status = Column(String(20), nullable=False, default="waiting")

    # Timeout handling - prevents deadlock if commis hangs
    deadline_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    jobs = relationship("CommisBarrierJob", back_populates="barrier", cascade="all, delete-orphan")
    course = relationship("Course", backref="commis_barrier", uselist=False)


class CommisBarrierJob(Base):
    """Individual commis job in a barrier.

    Normalized table for safe concurrent updates (avoids ARRAY field mutation issues).
    Stores the tool_call_id mapping critical for ToolMessage generation and
    caches results for batch resume.

    Status transitions:
    - created: job exists but not yet eligible for pickup (two-phase pattern)
    - queued: barrier exists, job can be picked up by commis
    - completed: commis finished successfully
    - failed: commis failed
    - timeout: deadline exceeded before completion
    """

    __tablename__ = "commis_barrier_jobs"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to barrier
    barrier_id = Column(
        Integer,
        ForeignKey("commis_barriers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Foreign key to commis job
    job_id = Column(
        Integer,
        ForeignKey("commis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Critical: tool_call_id needed for ToolMessage generation
    # This maps the commis result back to the original LLM tool call
    tool_call_id = Column(String(64), nullable=False)

    # Status tracking
    status = Column(String(20), nullable=False, default="created")

    # Cached result for batch resume (avoids re-fetching from artifact store)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    # Retry tracking (for adaptive failure handling)
    attempt_count = Column(Integer, nullable=False, default=1)

    # Timestamps
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    barrier = relationship("CommisBarrier", back_populates="jobs")
    commis_job = relationship("CommisJob", backref="barrier_job", uselist=False)

    # Indexes and constraints
    __table_args__ = (
        # Fast lookup by barrier and job
        Index("ix_commis_barrier_jobs_barrier_job", "barrier_id", "job_id"),
        # Prevent duplicate job entries in a barrier (matches uselist=False on relationship)
        UniqueConstraint("barrier_id", "job_id", name="uq_commis_barrier_jobs_barrier_job"),
    )
