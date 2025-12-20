"""fix_sync_op_id_uniqueness

Revision ID: h2i3j4k5l6m7
Revises: d6df416e6418
Create Date: 2025-12-11 22:00:00.000000

Change op_id uniqueness from global to per-user for multi-tenant correctness.
Previously, op_id was globally unique, which could cause conflicts between
different users. Now (user_id, op_id) is unique instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h2i3j4k5l6m7'
down_revision: Union[str, Sequence[str], None] = 'd6df416e6418'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Change op_id uniqueness from global to per-user."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Check if table exists
    if 'sync_operations' not in inspector.get_table_names():
        print("sync_operations table doesn't exist - skipping")
        return

    # Check if constraint already exists
    existing_constraints = [c['name'] for c in inspector.get_unique_constraints('sync_operations')]
    if 'uq_sync_operations_user_op' in existing_constraints:
        print("uq_sync_operations_user_op constraint already exists - skipping")
        return

    # Check if index exists before dropping
    existing_indexes = [idx['name'] for idx in inspector.get_indexes('sync_operations')]
    if 'ix_sync_operations_op_id' in existing_indexes:
        # Drop the global unique index on op_id
        op.drop_index('ix_sync_operations_op_id', table_name='sync_operations')

    # Create a non-unique index on op_id (for query performance) if not exists
    if 'ix_sync_operations_op_id' not in existing_indexes:
        op.create_index('ix_sync_operations_op_id', 'sync_operations', ['op_id'], unique=False)

    # Create composite unique constraint on (user_id, op_id)
    op.create_unique_constraint(
        'uq_sync_operations_user_op',
        'sync_operations',
        ['user_id', 'op_id']
    )


def downgrade() -> None:
    """Revert to global op_id uniqueness."""
    # Drop the composite unique constraint
    op.drop_constraint('uq_sync_operations_user_op', 'sync_operations', type_='unique')

    # Drop the non-unique index
    op.drop_index('ix_sync_operations_op_id', table_name='sync_operations')

    # Recreate the global unique index on op_id
    op.create_index('ix_sync_operations_op_id', 'sync_operations', ['op_id'], unique=True)
