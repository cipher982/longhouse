"""add_course_events

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2025-12-26 12:50:06.000000

Add course_events table for durable event streaming (Resumable SSE v1).
This table persists all concierge/commis events to enable replay on reconnect.
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
    """Add course_events table for durable event streaming."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'course_events' in tables:
        print("course_events table already exists - skipping")
        return

    # Create course_events table
    op.create_table(
        'course_events',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('course_id', 'sequence', name='course_events_unique_seq')
    )

    # Create indexes for efficient queries
    op.create_index('idx_course_events_course_id', 'course_events', ['course_id'])
    op.create_index('idx_course_events_created_at', 'course_events', ['created_at'])
    op.create_index('idx_course_events_type', 'course_events', ['event_type'])


def downgrade() -> None:
    """Remove course_events table."""
    op.drop_index('idx_course_events_type', table_name='course_events')
    op.drop_index('idx_course_events_created_at', table_name='course_events')
    op.drop_index('idx_course_events_course_id', table_name='course_events')
    op.drop_table('course_events')
