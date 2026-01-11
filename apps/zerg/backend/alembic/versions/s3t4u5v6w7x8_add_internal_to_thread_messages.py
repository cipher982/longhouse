"""add_internal_to_thread_messages

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2025-01-10 00:00:00.000000

Add internal column to thread_messages for filtering orchestration messages.
Internal messages (continuation prompts, system notifications) should be stored
for LLM context but NOT shown to users in chat history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 's3t4u5v6w7x8'
down_revision: Union[str, Sequence[str], None] = 'r2s3t4u5v6w7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add internal column to thread_messages."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('thread_messages')]

    if 'internal' in columns:
        print("internal column already exists - skipping")
        return

    op.add_column(
        'thread_messages',
        sa.Column('internal', sa.Boolean(), nullable=False, server_default='false')
    )


def downgrade() -> None:
    """Remove internal column from thread_messages."""
    op.drop_column('thread_messages', 'internal')
