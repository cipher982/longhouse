"""add_agent_run_events

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2025-12-26 12:50:06.000000

Add agent_run_events table for durable event streaming (Resumable SSE v1).
This table persists all supervisor/worker events to enable replay on reconnect.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'm7n8o9p0q1r2'
down_revision: Union[str, Sequence[str], None] = 'l6m7n8o9p0q1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent_run_events table for durable event streaming."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'agent_run_events' in tables:
        print("agent_run_events table already exists - skipping")
        return

    # Create agent_run_events table
    op.create_table(
        'agent_run_events',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('run_id', 'sequence', name='agent_run_events_unique_seq')
    )

    # Create indexes for efficient queries
    op.create_index('idx_run_events_run_id', 'agent_run_events', ['run_id'])
    op.create_index('idx_run_events_created_at', 'agent_run_events', ['created_at'])
    op.create_index('idx_run_events_type', 'agent_run_events', ['event_type'])


def downgrade() -> None:
    """Remove agent_run_events table."""
    op.drop_index('idx_run_events_type', table_name='agent_run_events')
    op.drop_index('idx_run_events_created_at', table_name='agent_run_events')
    op.drop_index('idx_run_events_run_id', table_name='agent_run_events')
    op.drop_table('agent_run_events')
