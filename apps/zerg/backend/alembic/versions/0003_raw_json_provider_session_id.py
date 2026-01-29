"""Add raw_json and provider_session_id columns for lossless archiving.

Revision ID: 0003_raw_json_provider_session_id
Revises: 0002_agents_schema
Create Date: 2026-01-28

Adds:
- raw_json column to agent_events for storing original JSONL lines
- provider_session_id column to agent_sessions for Claude Code session UUID tracking
"""

from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_raw_json_provider_session_id"
down_revision: Union[str, Sequence[str], None] = "0002_agents_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add raw_json to events and provider_session_id to sessions."""
    # Add raw_json column to events for lossless archiving
    op.add_column(
        "events",
        sa.Column("raw_json", sa.Text(), nullable=True),
        schema="agents",
    )

    # Add provider_session_id column to sessions
    op.add_column(
        "sessions",
        sa.Column("provider_session_id", sa.String(255), nullable=True),
        schema="agents",
    )

    # Create index for provider_session_id lookups
    op.create_index(
        "ix_sessions_provider_session_id",
        "sessions",
        ["provider_session_id"],
        schema="agents",
    )


def downgrade() -> None:
    """Remove raw_json and provider_session_id columns."""
    op.drop_index("ix_sessions_provider_session_id", table_name="sessions", schema="agents")
    op.drop_column("sessions", "provider_session_id", schema="agents")
    op.drop_column("events", "raw_json", schema="agents")
