"""Add created_at column to thread_messages table

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2025-10-29 10:45:00.000000

"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6g7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add created_at column to thread_messages table for chronological message ordering."""
    # Check if the column already exists (for safety)
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if thread_messages table exists first
    if not inspector.has_table("thread_messages"):
        print("thread_messages table doesn't exist yet - skipping migration")
        return

    # Get table columns
    columns = [col["name"] for col in inspector.get_columns("thread_messages")]

    # Add created_at column if it doesn't exist
    if "created_at" not in columns:
        print("Adding created_at column to thread_messages table")
        op.add_column(
            "thread_messages",
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        print("created_at column added successfully")
    else:
        print("created_at column already exists - skipping")


def downgrade() -> None:
    """Remove created_at column from thread_messages table."""
    # Check if the column exists before trying to drop it
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    if not inspector.has_table("thread_messages"):
        print("thread_messages table doesn't exist - skipping downgrade")
        return

    columns = [col["name"] for col in inspector.get_columns("thread_messages")]

    if "created_at" in columns:
        print("Removing created_at column from thread_messages table")
        op.drop_column("thread_messages", "created_at")
        print("created_at column removed successfully")
