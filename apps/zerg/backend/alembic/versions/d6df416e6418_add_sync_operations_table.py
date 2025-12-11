"""add_sync_operations_table

Revision ID: d6df416e6418
Revises: 51db4599fd92
Create Date: 2025-12-11 18:07:21.814620

Add sync_operations table for conversation synchronization between
Jarvis clients and zerg-backend.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'd6df416e6418'
down_revision: Union[str, Sequence[str], None] = '51db4599fd92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sync_operations table."""
    op.create_table(
        'sync_operations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('op_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('body', JSONB, nullable=False),
        sa.Column('lamport', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sync_operations_id'), 'sync_operations', ['id'], unique=False)
    op.create_index(op.f('ix_sync_operations_op_id'), 'sync_operations', ['op_id'], unique=True)
    op.create_index(op.f('ix_sync_operations_user_id'), 'sync_operations', ['user_id'], unique=False)


def downgrade() -> None:
    """Remove sync_operations table."""
    op.drop_index(op.f('ix_sync_operations_user_id'), table_name='sync_operations')
    op.drop_index(op.f('ix_sync_operations_op_id'), table_name='sync_operations')
    op.drop_index(op.f('ix_sync_operations_id'), table_name='sync_operations')
    op.drop_table('sync_operations')
