"""add_sent_at_to_thread_messages

Revision ID: 74a690bf8231
Revises: 63b589be7120
Create Date: 2025-11-27 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '74a690bf8231'
down_revision: Union[str, Sequence[str], None] = '63b589be7120'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sent_at column to thread_messages table."""
    op.add_column(
        'thread_messages',
        sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True)
    )


def downgrade() -> None:
    """Remove sent_at column from thread_messages table."""
    op.drop_column('thread_messages', 'sent_at')
