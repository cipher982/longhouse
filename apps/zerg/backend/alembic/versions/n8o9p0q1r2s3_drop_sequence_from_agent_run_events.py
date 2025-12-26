"""drop_sequence_from_agent_run_events

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2025-12-26 14:00:00.000000

Drop the sequence column from agent_run_events table. We now use the
auto-incrementing id (BigSerial) for ordering, which is atomic and doesn't
require explicit sequence number calculation. This fixes race conditions
where concurrent emit_run_event() calls could cause IntegrityError.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'n8o9p0q1r2s3'
down_revision: Union[str, Sequence[str], None] = 'm7n8o9p0q1r2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop sequence column and its unique constraint."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'agent_run_events' not in tables:
        print("agent_run_events table doesn't exist - skipping")
        return

    # Drop the unique constraint first
    try:
        op.drop_constraint('agent_run_events_unique_seq', 'agent_run_events', type_='unique')
    except Exception as e:
        print(f"Could not drop constraint (may not exist): {e}")

    # Drop the sequence column
    try:
        op.drop_column('agent_run_events', 'sequence')
    except Exception as e:
        print(f"Could not drop column (may not exist): {e}")


def downgrade() -> None:
    """Add sequence column back (not recommended)."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'agent_run_events' not in tables:
        print("agent_run_events table doesn't exist - skipping")
        return

    # Add sequence column back
    op.add_column('agent_run_events', sa.Column('sequence', sa.Integer(), nullable=True))

    # Backfill sequence numbers based on id order
    conn.execute(sa.text("""
        UPDATE agent_run_events
        SET sequence = subq.row_num
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY id) as row_num
            FROM agent_run_events
        ) subq
        WHERE agent_run_events.id = subq.id
    """))

    # Make sequence NOT NULL
    op.alter_column('agent_run_events', 'sequence', nullable=False)

    # Recreate the unique constraint
    op.create_unique_constraint('agent_run_events_unique_seq', 'agent_run_events', ['run_id', 'sequence'])
