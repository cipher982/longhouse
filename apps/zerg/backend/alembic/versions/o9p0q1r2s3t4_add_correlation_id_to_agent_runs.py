"""add correlation_id to agent_runs

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2025-12-27 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "o9p0q1r2s3t4"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add correlation_id column to agent_runs for request tracing
    # This enables end-to-end observability from frontend to backend
    # Use IF NOT EXISTS patterns for idempotency
    conn = op.get_bind()

    # Check if column exists
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'agent_runs' AND column_name = 'correlation_id'"
        )
    )
    if result.fetchone() is None:
        op.add_column(
            "agent_runs",
            sa.Column("correlation_id", sa.String(), nullable=True),
        )

    # Check if index exists
    result = conn.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'agent_runs' AND indexname = 'ix_agent_runs_correlation_id'"
        )
    )
    if result.fetchone() is None:
        op.create_index(
            "ix_agent_runs_correlation_id",
            "agent_runs",
            ["correlation_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_correlation_id", table_name="agent_runs")
    op.drop_column("agent_runs", "correlation_id")
