"""add_assistant_message_id_to_agent_runs

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2025-01-10 00:00:00.000000

Add assistant_message_id column to agent_runs for durable runs message tracking.
This stores the UUID assigned to the assistant message in supervisor_started,
allowing continuation runs to look up the original message's ID for
continuation_of_message_id (instead of using a sentinel string).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'r2s3t4u5v6w7'
down_revision: Union[str, Sequence[str], None] = 'q1r2s3t4u5v6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add assistant_message_id column to agent_runs."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('agent_runs')]

    if 'assistant_message_id' in columns:
        print("assistant_message_id column already exists - skipping")
        return

    op.add_column(
        'agent_runs',
        sa.Column('assistant_message_id', sa.String(36), nullable=True)
    )


def downgrade() -> None:
    """Remove assistant_message_id column from agent_runs."""
    op.drop_column('agent_runs', 'assistant_message_id')
