"""add_runners_tables

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2025-12-15 12:00:00.000000

Add runners, runner_enroll_tokens, and runner_jobs tables for
the Runners v1 execution infrastructure.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i3j4k5l6m7n8'
down_revision: Union[str, Sequence[str], None] = 'h2i3j4k5l6m7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create runners tables."""
    # Check if tables already exist (may have been created by schema init)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    if 'runners' in existing_tables:
        print("runners tables already exist - skipping")
        return

    # Create runners table
    op.create_table(
        'runners',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('labels', sa.JSON(), nullable=True),
        sa.Column('capabilities', sa.JSON(), nullable=False, server_default='["exec.readonly"]'),
        sa.Column('status', sa.String(), nullable=False, server_default='offline'),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Column('auth_secret_hash', sa.String(), nullable=False),
        sa.Column('runner_metadata', sa.JSON(), nullable=True),
    )

    # Create unique constraint for (owner_id, name)
    op.create_unique_constraint('uix_runner_owner_name', 'runners', ['owner_id', 'name'])

    # Create runner_enroll_tokens table
    op.create_table(
        'runner_enroll_tokens',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('token_hash', sa.String(), nullable=False, unique=True, index=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    # Create runner_jobs table
    op.create_table(
        'runner_jobs',
        sa.Column('id', sa.String(), primary_key=True),  # UUID as string
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('worker_id', sa.String(), nullable=True, index=True),
        sa.Column('run_id', sa.String(), nullable=True),
        sa.Column('runner_id', sa.Integer(), sa.ForeignKey('runners.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('command', sa.Text(), nullable=False),
        sa.Column('timeout_secs', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('stdout_trunc', sa.Text(), nullable=True),
        sa.Column('stderr_trunc', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('artifacts', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    """Drop runners tables."""
    op.drop_table('runner_jobs')
    op.drop_table('runner_enroll_tokens')
    op.drop_table('runners')
