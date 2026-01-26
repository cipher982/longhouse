"""Add commis_barriers and commis_barrier_jobs tables for parallel commis coordination.

Revision ID: v6w7x8y9z0a1
Revises: u5v6w7x8y9z0
Create Date: 2026-01-15

Implements barrier synchronization pattern for multi-commis execution:
- commis_barriers: tracks batch of parallel commis for a concierge run
- commis_barrier_jobs: individual commis job in a barrier with result caching

Two-Phase Commit Pattern prevents "fast commis" race condition where a
commis completes before the barrier exists.
"""

from alembic import op
import sqlalchemy as sa

revision = "v6w7x8y9z0a1"
down_revision = "085593a495c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create commis_barriers table
    op.create_table(
        "commis_barriers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="waiting"),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("course_id", name="uq_commis_barriers_course_id"),
    )
    op.create_index("ix_commis_barriers_course_id", "commis_barriers", ["course_id"])
    op.create_index("ix_commis_barriers_status", "commis_barriers", ["status"])

    # Create commis_barrier_jobs table
    op.create_table(
        "commis_barrier_jobs",
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
        sa.ForeignKeyConstraint(["barrier_id"], ["commis_barriers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["commis_jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("barrier_id", "job_id", name="uq_commis_barrier_jobs_barrier_job"),
    )
    op.create_index("ix_commis_barrier_jobs_barrier_id", "commis_barrier_jobs", ["barrier_id"])
    op.create_index("ix_commis_barrier_jobs_job_id", "commis_barrier_jobs", ["job_id"])
    op.create_index("ix_commis_barrier_jobs_barrier_job", "commis_barrier_jobs", ["barrier_id", "job_id"])


def downgrade() -> None:
    # Drop commis_barrier_jobs table
    op.drop_index("ix_commis_barrier_jobs_barrier_job", table_name="commis_barrier_jobs")
    op.drop_index("ix_commis_barrier_jobs_job_id", table_name="commis_barrier_jobs")
    op.drop_index("ix_commis_barrier_jobs_barrier_id", table_name="commis_barrier_jobs")
    op.drop_table("commis_barrier_jobs")

    # Drop commis_barriers table
    op.drop_index("ix_commis_barriers_status", table_name="commis_barriers")
    op.drop_index("ix_commis_barriers_course_id", table_name="commis_barriers")
    op.drop_table("commis_barriers")
