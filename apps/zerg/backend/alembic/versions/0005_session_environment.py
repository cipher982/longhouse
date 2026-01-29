"""Add environment column to sessions for test isolation.

Revision ID: 0005_session_environment
Revises: 0004_device_tokens
Create Date: 2026-01-29

Adds:
- environment column to agent_sessions (production, development, test, e2e)
- Enables filtering test sessions from production views
"""

from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_session_environment"
down_revision: Union[str, Sequence[str], None] = "0004_device_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add environment column to sessions."""
    # Add environment column with default 'production'
    op.add_column(
        "sessions",
        sa.Column("environment", sa.String(20), nullable=False, server_default="production"),
        schema="agents",
    )

    # Create index for environment filtering
    op.create_index(
        "ix_sessions_environment",
        "sessions",
        ["environment"],
        schema="agents",
    )


def downgrade() -> None:
    """Remove environment column."""
    op.drop_index("ix_sessions_environment", table_name="sessions", schema="agents")
    op.drop_column("sessions", "environment", schema="agents")
