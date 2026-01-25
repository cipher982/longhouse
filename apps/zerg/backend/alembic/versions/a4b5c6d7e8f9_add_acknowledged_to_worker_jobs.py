"""Add acknowledged column to worker_jobs for async inbox model.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-01-25

Adds boolean acknowledged column to track whether the supervisor has seen
a worker's result. Part of the async inbox model (non-blocking spawn_worker).
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Add acknowledged column to worker_jobs table."""
    op.add_column(
        "worker_jobs",
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default="false"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove acknowledged column from worker_jobs table."""
    op.drop_column("worker_jobs", "acknowledged", schema=SCHEMA)
