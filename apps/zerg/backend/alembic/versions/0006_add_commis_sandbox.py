"""Add sandbox column to commis_jobs table.

Revision ID: 0006_add_commis_sandbox
Revises: 0005_session_environment
Create Date: 2026-01-29

Adds sandbox boolean column for container-based isolated execution.
When True, commis jobs run in Docker containers with process/filesystem isolation.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006_add_commis_sandbox"
down_revision: Union[str, Sequence[str], None] = "0005_session_environment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sandbox column to commis_jobs table."""
    op.add_column(
        "commis_jobs",
        sa.Column("sandbox", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    """Remove sandbox column from commis_jobs table."""
    op.drop_column("commis_jobs", "sandbox")
