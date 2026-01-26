"""Add trace_id columns for end-to-end tracing.

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-01-14

Adds trace_id to runs, commis_jobs, and llm_audit_log for
unified debugging. Also adds span_id to llm_audit_log for LLM call
identification.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "u5v6w7x8y9z0"
down_revision = "t4u5v6w7x8y9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add trace_id to runs
    op.add_column("runs", sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_runs_trace_id", "runs", ["trace_id"])

    # Add trace_id to commis_jobs
    op.add_column("commis_jobs", sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_commis_jobs_trace_id", "commis_jobs", ["trace_id"])

    # Add trace_id and span_id to llm_audit_log
    op.add_column("llm_audit_log", sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("llm_audit_log", sa.Column("span_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_llm_audit_log_trace_id", "llm_audit_log", ["trace_id"])


def downgrade() -> None:
    # Remove from llm_audit_log
    op.drop_index("ix_llm_audit_log_trace_id", table_name="llm_audit_log")
    op.drop_column("llm_audit_log", "span_id")
    op.drop_column("llm_audit_log", "trace_id")

    # Remove from commis_jobs
    op.drop_index("ix_commis_jobs_trace_id", table_name="commis_jobs")
    op.drop_column("commis_jobs", "trace_id")

    # Remove from runs
    op.drop_index("ix_runs_trace_id", table_name="runs")
    op.drop_column("runs", "trace_id")
