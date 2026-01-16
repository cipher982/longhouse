"""Worker barrier models for parallel worker coordination.

Implements the barrier synchronization pattern for multi-worker execution:
- WorkerBarrier: tracks a batch of parallel workers for a supervisor run
- BarrierJob: individual worker job in a barrier with result caching

Two-Phase Commit Pattern:
1. spawn_worker creates WorkerJob with status='created' (not queued)
2. After ALL spawn_workers processed, create WorkerBarrier + BarrierJob records
3. Atomic: flip all jobs from 'created' to 'queued'
4. Workers can now pick them up (barrier guaranteed to exist)

This prevents the "fast worker" race where a worker finishes before
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


class WorkerBarrier(Base):
    """Tracks a batch of parallel workers for a supervisor run.

    Implements barrier synchronization: supervisor waits until ALL workers
    complete before resuming. Uses atomic counter increment + status guard
    to prevent double-resume race conditions.

    Status transitions:
    - waiting: initial state, waiting for workers to complete
    - resuming: claimed by the worker that triggers resume (prevents double)
    - completed: resume finished, barrier is done
    - failed: supervisor or resume failed, barrier abandoned
    """

    __tablename__ = "worker_barriers"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to agent run - one barrier per run
    # ON DELETE CASCADE: if run is deleted, barrier is automatically removed
    run_id = Column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # One barrier per run
        index=True,
    )

    # Counter-based barrier tracking (more reliable than array mutations)
    expected_count = Column(Integer, nullable=False)  # How many workers to wait for
    completed_count = Column(Integer, nullable=False, default=0)

    # Status guard for atomic resume claim
    # waiting -> resuming -> completed
    status = Column(String(20), nullable=False, default="waiting")

    # Timeout handling - prevents deadlock if worker hangs
    deadline_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    jobs = relationship("BarrierJob", back_populates="barrier", cascade="all, delete-orphan")
    run = relationship("AgentRun", backref="worker_barrier", uselist=False)


class BarrierJob(Base):
    """Individual worker job in a barrier.

    Normalized table for safe concurrent updates (avoids ARRAY field mutation issues).
    Stores the tool_call_id mapping critical for ToolMessage generation and
    caches results for batch resume.

    Status transitions:
    - created: job exists but not yet eligible for pickup (two-phase pattern)
    - queued: barrier exists, job can be picked up by worker
    - completed: worker finished successfully
    - failed: worker failed
    - timeout: deadline exceeded before completion
    """

    __tablename__ = "barrier_jobs"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to barrier
    barrier_id = Column(
        Integer,
        ForeignKey("worker_barriers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Foreign key to worker job
    job_id = Column(
        Integer,
        ForeignKey("worker_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Critical: tool_call_id needed for ToolMessage generation
    # This maps the worker result back to the original LLM tool call
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
    barrier = relationship("WorkerBarrier", back_populates="jobs")
    worker_job = relationship("WorkerJob", backref="barrier_job", uselist=False)

    # Indexes and constraints
    __table_args__ = (
        # Fast lookup by barrier and job
        Index("ix_barrier_jobs_barrier_job", "barrier_id", "job_id"),
        # Prevent duplicate job entries in a barrier (matches uselist=False on relationship)
        UniqueConstraint("barrier_id", "job_id", name="uq_barrier_jobs_barrier_job"),
    )
