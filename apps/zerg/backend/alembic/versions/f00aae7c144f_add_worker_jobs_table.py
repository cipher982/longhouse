"""add_commis_jobs_table

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
    """Create commis_jobs table for background commis task execution."""
    # Check if table already exists (may have been created by schema init)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'commis_jobs' in inspector.get_table_names():
        print("commis_jobs table already exists - skipping")
        return

    op.create_table(
        'commis_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('task', sa.Text(), nullable=False),
        sa.Column('model', sa.String(100), nullable=False, server_default='gpt-4o-mini'),
        sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
        sa.Column('commis_id', sa.String(255), nullable=True, index=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Drop commis_jobs table."""
    op.drop_table('commis_jobs')
