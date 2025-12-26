"""add_continuation_of_run_id

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2025-12-26 00:00:00.000000

Add continuation_of_run_id column to agent_runs for durable runs v2.2.
This links continuation runs to their original deferred run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l6m7n8o9p0q1'
down_revision: Union[str, Sequence[str], None] = 'k5l6m7n8o9p0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add continuation_of_run_id column to agent_runs."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('agent_runs')]

    if 'continuation_of_run_id' in columns:
        print("continuation_of_run_id column already exists - skipping")
        return

    op.add_column(
        'agent_runs',
        sa.Column('continuation_of_run_id', sa.Integer(), nullable=True)
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_agent_runs_continuation_of_run_id',
        'agent_runs',
        'agent_runs',
        ['continuation_of_run_id'],
        ['id']
    )


def downgrade() -> None:
    """Remove continuation_of_run_id column from agent_runs."""
    op.drop_constraint('fk_agent_runs_continuation_of_run_id', 'agent_runs', type_='foreignkey')
    op.drop_column('agent_runs', 'continuation_of_run_id')
