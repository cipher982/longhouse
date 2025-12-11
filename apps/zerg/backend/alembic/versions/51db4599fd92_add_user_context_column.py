"""add_user_context_column

Revision ID: 51db4599fd92
Revises: g1h2i3j4k5l6
Create Date: 2025-12-11 11:50:30.789479

Add context JSONB column to users table for storing user-specific context
(servers, integrations, preferences) used in prompt composition.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '51db4599fd92'
down_revision: Union[str, Sequence[str], None] = 'g1h2i3j4k5l6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add context JSONB column to users table."""
    op.add_column('users', sa.Column('context', JSONB, server_default='{}', nullable=False))


def downgrade() -> None:
    """Remove context column from users table."""
    op.drop_column('users', 'context')
