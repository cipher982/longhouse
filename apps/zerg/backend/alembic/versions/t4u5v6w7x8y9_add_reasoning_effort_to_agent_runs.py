"""add reasoning_effort to agent_runs

Revision ID: t4u5v6w7x8y9
Revises: 0a09e33fe6b0
Create Date: 2026-01-13 16:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "t4u5v6w7x8y9"
down_revision = "0a09e33fe6b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add reasoning_effort column to agent_runs for continuation inheritance
    # When a supervisor run is resumed, it should use the same reasoning_effort
    # Values: none, low, medium, high
    conn = op.get_bind()

    # Check if column exists (idempotent)
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'agent_runs' AND column_name = 'reasoning_effort'"
        )
    )
    if result.fetchone() is None:
        op.add_column(
            "agent_runs",
            sa.Column("reasoning_effort", sa.String(20), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("agent_runs", "reasoning_effort")
