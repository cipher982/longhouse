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
    """Add summary, created_at, and updated_at columns to agent_runs table for Jarvis Task Inbox."""
    # Check if the columns already exist (for safety)
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if agent_runs table exists first
    if not inspector.has_table("agent_runs"):
        print("agent_runs table doesn't exist yet - skipping migration")
        return

    # Get table columns
    columns = [col["name"] for col in inspector.get_columns("agent_runs")]

    # Add summary column
    if "summary" not in columns:
        print("Adding summary column to agent_runs table")
        op.add_column(
            "agent_runs",
            sa.Column("summary", sa.Text(), nullable=True),
        )
        print("Summary column added successfully")
    else:
        print("Summary column already exists - skipping")

    # Add created_at column (nullable, no default - SQLite limitation)
    if "created_at" not in columns:
        print("Adding created_at column to agent_runs table")
        op.add_column(
            "agent_runs",
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        # Backfill with started_at or current time for existing rows
        connection.execute(
            sa.text("UPDATE agent_runs SET created_at = COALESCE(started_at, datetime('now')) WHERE created_at IS NULL")
        )
        print("created_at column added and backfilled successfully")
    else:
        print("created_at column already exists - skipping")

    # Add updated_at column (nullable, no default - SQLite limitation)
    if "updated_at" not in columns:
        print("Adding updated_at column to agent_runs table")
        op.add_column(
            "agent_runs",
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        # Backfill with finished_at or started_at or current time
        connection.execute(
            sa.text("UPDATE agent_runs SET updated_at = COALESCE(finished_at, started_at, datetime('now')) WHERE updated_at IS NULL")
        )
        print("updated_at column added and backfilled successfully")
    else:
        print("updated_at column already exists - skipping")


def downgrade() -> None:
    """Remove summary, created_at, and updated_at columns from agent_runs table."""
    # Check if the columns exist before trying to drop them
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

    if "updated_at" in columns:
        print("Removing updated_at column from agent_runs table")
        op.drop_column("agent_runs", "updated_at")
        print("updated_at column removed successfully")

    if "created_at" in columns:
        print("Removing created_at column from agent_runs table")
        op.drop_column("agent_runs", "created_at")
        print("created_at column removed successfully")
