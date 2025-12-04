"""add_worker_jobs_table

Revision ID: f00aae7c144f
Revises: f6g7h8i9j0k1
Create Date: 2025-12-04 11:58:09.553888

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f00aae7c144f'
down_revision: Union[str, Sequence[str], None] = 'f6g7h8i9j0k1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create worker_jobs table for background worker task execution."""
    op.create_table(
        'worker_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('task', sa.Text(), nullable=False),
        sa.Column('model', sa.String(100), nullable=False, server_default='gpt-4o-mini'),
        sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
        sa.Column('worker_id', sa.String(255), nullable=True, index=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Drop worker_jobs table."""
    op.drop_table('worker_jobs')
