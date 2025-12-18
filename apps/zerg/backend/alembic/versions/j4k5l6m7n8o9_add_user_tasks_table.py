"""add_user_tasks_table

Revision ID: j4k5l6m7n8o9
Revises: 35e451933c7a
Create Date: 2025-12-18 00:00:00.000000

Add user_tasks table for agent-created task management.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j4k5l6m7n8o9'
down_revision: Union[str, Sequence[str], None] = '35e451933c7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create user_tasks table."""
    op.create_table(
        'user_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('due_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_tasks_id'), 'user_tasks', ['id'], unique=False)
    op.create_index(op.f('ix_user_tasks_user_id'), 'user_tasks', ['user_id'], unique=False)
    # Composite index for filtering by user and status
    op.create_index('ix_user_tasks_user_id_status', 'user_tasks', ['user_id', 'status'], unique=False)


def downgrade() -> None:
    """Drop user_tasks table."""
    op.drop_index('ix_user_tasks_user_id_status', table_name='user_tasks')
    op.drop_index(op.f('ix_user_tasks_user_id'), table_name='user_tasks')
    op.drop_index(op.f('ix_user_tasks_id'), table_name='user_tasks')
    op.drop_table('user_tasks')
