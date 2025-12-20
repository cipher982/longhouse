"""add_agent_memory_kv_table

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2025-12-18 00:00:00.000000

Add agent_memory_kv table for persistent key-value storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'k5l6m7n8o9p0'
down_revision: Union[str, Sequence[str], None] = 'j4k5l6m7n8o9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_memory_kv table."""
    # Check if table already exists (may have been created by schema init)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'agent_memory_kv' in inspector.get_table_names():
        print("agent_memory_kv table already exists - skipping")
        return

    op.create_table(
        'agent_memory_kv',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'key')
    )

    # Note: We intentionally do NOT create an index on 'tags' because:
    # - JSON columns cannot use btree indexes in Postgres
    # - Tag filtering is done in Python for SQLite compatibility anyway
    # - The primary key (user_id, key) handles the main lookup path

    # Create partial index for expires_at
    # Note: SQLite supports partial indexes starting from version 3.8.0
    op.create_index(
        'ix_agent_memory_kv_expires_at',
        'agent_memory_kv',
        ['expires_at'],
        unique=False,
        postgresql_where=sa.text('expires_at IS NOT NULL'),
        sqlite_where='expires_at IS NOT NULL'
    )


def downgrade() -> None:
    """Drop agent_memory_kv table."""
    op.drop_index('ix_agent_memory_kv_expires_at', table_name='agent_memory_kv')
    op.drop_table('agent_memory_kv')
