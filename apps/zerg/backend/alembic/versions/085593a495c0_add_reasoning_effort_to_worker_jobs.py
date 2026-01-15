"""add reasoning_effort to worker_jobs

Revision ID: 085593a495c0
Revises: u5v6w7x8y9z0
Create Date: 2026-01-14 21:38:04.939136

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '085593a495c0'
down_revision: Union[str, Sequence[str], None] = 'u5v6w7x8y9z0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add reasoning_effort column to worker_jobs table."""
    op.add_column('worker_jobs', sa.Column('reasoning_effort', sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Remove reasoning_effort column from worker_jobs table."""
    op.drop_column('worker_jobs', 'reasoning_effort')
