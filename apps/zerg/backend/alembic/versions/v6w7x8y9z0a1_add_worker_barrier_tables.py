"""Add worker_barriers and barrier_jobs tables for parallel worker coordination.

Revision ID: v6w7x8y9z0a1
Revises: u5v6w7x8y9z0
Create Date: 2026-01-15

Implements barrier synchronization pattern for multi-worker execution:
- worker_barriers: tracks batch of parallel workers for a supervisor run
- barrier_jobs: individual worker job in a barrier with result caching

Two-Phase Commit Pattern prevents "fast worker" race condition where a
worker completes before the barrier exists.
"""

from alembic import op
import sqlalchemy as sa

revision = "v6w7x8y9z0a1"
down_revision = "085593a495c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create worker_barriers table
    op.create_table(
        "worker_barriers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="waiting"),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", name="uq_worker_barriers_run_id"),
    )
    op.create_index("ix_worker_barriers_run_id", "worker_barriers", ["run_id"])
    op.create_index("ix_worker_barriers_status", "worker_barriers", ["status"])

    # Create barrier_jobs table
    op.create_table(
        "barrier_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("barrier_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("tool_call_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="created"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["barrier_id"], ["worker_barriers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["worker_jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("barrier_id", "job_id", name="uq_barrier_jobs_barrier_job"),
    )
    op.create_index("ix_barrier_jobs_barrier_id", "barrier_jobs", ["barrier_id"])
    op.create_index("ix_barrier_jobs_job_id", "barrier_jobs", ["job_id"])
    op.create_index("ix_barrier_jobs_barrier_job", "barrier_jobs", ["barrier_id", "job_id"])


def downgrade() -> None:
    # Drop barrier_jobs table
    op.drop_index("ix_barrier_jobs_barrier_job", table_name="barrier_jobs")
    op.drop_index("ix_barrier_jobs_job_id", table_name="barrier_jobs")
    op.drop_index("ix_barrier_jobs_barrier_id", table_name="barrier_jobs")
    op.drop_table("barrier_jobs")

    # Drop worker_barriers table
    op.drop_index("ix_worker_barriers_status", table_name="worker_barriers")
    op.drop_index("ix_worker_barriers_run_id", table_name="worker_barriers")
    op.drop_table("worker_barriers")
