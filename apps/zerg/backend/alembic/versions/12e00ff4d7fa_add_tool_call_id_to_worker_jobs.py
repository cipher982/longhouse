"""add_tool_call_id_to_worker_jobs

Revision ID: 12e00ff4d7fa
Revises: s3t4u5v6w7x8
Create Date: 2026-01-12 21:37:11.652693

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '12e00ff4d7fa'
down_revision: Union[str, Sequence[str], None] = 's3t4u5v6w7x8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tool_call_id column and idempotency index to worker_jobs."""
    op.add_column('worker_jobs', sa.Column('tool_call_id', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_worker_jobs_tool_call_id'), 'worker_jobs', ['tool_call_id'], unique=False)
    op.create_index(
        'ix_worker_jobs_idempotency',
        'worker_jobs',
        ['supervisor_run_id', 'tool_call_id'],
        unique=True,
        postgresql_where=sa.text('supervisor_run_id IS NOT NULL AND tool_call_id IS NOT NULL')
    )


def downgrade() -> None:
    """Remove tool_call_id column and indexes."""
    op.drop_index('ix_worker_jobs_idempotency', table_name='worker_jobs', postgresql_where=sa.text('supervisor_run_id IS NOT NULL AND tool_call_id IS NOT NULL'))
    op.drop_index(op.f('ix_worker_jobs_tool_call_id'), table_name='worker_jobs')
    op.drop_column('worker_jobs', 'tool_call_id')
