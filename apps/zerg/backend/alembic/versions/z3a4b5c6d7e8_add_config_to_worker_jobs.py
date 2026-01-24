"""Add config column to worker_jobs for cloud execution.

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-01-22

Adds a JSON config column to store execution mode, git repo, and other
flexible configuration for cloud-based agent execution.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "z3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "y2z3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Add config column to worker_jobs table."""
    # Add JSON config column for flexible execution configuration
    # Stores execution_mode, git_repo, and other cloud execution params
    op.add_column(
        "worker_jobs",
        sa.Column("config", sa.JSON(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove config column from worker_jobs table."""
    op.drop_column("worker_jobs", "config", schema=SCHEMA)
