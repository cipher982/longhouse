"""add_seed_registry_table

Revision ID: 1fdb04873bc0
Revises: 0007_add_memories_table
Create Date: 2026-02-02 18:26:26.413711

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1fdb04873bc0'
down_revision: Union[str, Sequence[str], None] = '0007_add_memories_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'seed_registry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('seed_key', sa.String(length=255), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=False),
        sa.Column('namespace', sa.String(length=50), nullable=False, server_default='test'),
        sa.Column('entity_type', sa.String(length=50), nullable=False),
        sa.Column('entity_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_seed_registry'),
        sa.UniqueConstraint('seed_key', 'target', name='uq_seed_registry_key_target'),
    )
    op.create_index('ix_seed_registry_id', 'seed_registry', ['id'])
    op.create_index('ix_seed_registry_namespace', 'seed_registry', ['namespace'])
    op.create_index('ix_seed_registry_target', 'seed_registry', ['target'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_seed_registry_target', table_name='seed_registry')
    op.drop_index('ix_seed_registry_namespace', table_name='seed_registry')
    op.drop_index('ix_seed_registry_id', table_name='seed_registry')
    op.drop_table('seed_registry')
