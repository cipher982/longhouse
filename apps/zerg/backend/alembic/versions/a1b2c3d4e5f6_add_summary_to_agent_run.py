"""Add summary column to agent_runs table

Revision ID: a1b2c3d4e5f6
Revises: 70b7ee2edc1c
Create Date: 2025-10-06 19:00:00.000000

"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "70b7ee2edc1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add summary column to agent_runs table for Jarvis Task Inbox."""
    # Check if the column already exists (for safety)
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if agent_runs table exists first
    if not inspector.has_table("agent_runs"):
        print("agent_runs table doesn't exist yet - skipping migration")
        return

    # Get table columns
    columns = [col["name"] for col in inspector.get_columns("agent_runs")]

    # Only add column if it doesn't exist
    if "summary" not in columns:
        print("Adding summary column to agent_runs table")
        op.add_column(
            "agent_runs",
            sa.Column("summary", sa.Text(), nullable=True),
        )
        print("Summary column added successfully")
    else:
        print("Summary column already exists - skipping")


def downgrade() -> None:
    """Remove summary column from agent_runs table."""
    # Check if the column exists before trying to drop it
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    if not inspector.has_table("agent_runs"):
        print("agent_runs table doesn't exist - skipping downgrade")
        return

    columns = [col["name"] for col in inspector.get_columns("agent_runs")]

    if "summary" in columns:
        print("Removing summary column from agent_runs table")
        op.drop_column("agent_runs", "summary")
        print("Summary column removed successfully")
    else:
        print("Summary column doesn't exist - skipping downgrade")
