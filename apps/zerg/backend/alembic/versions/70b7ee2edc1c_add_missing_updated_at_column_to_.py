"""Add missing updated_at column to connectors table

Revision ID: 70b7ee2edc1c
Revises: 458f9a6a8779
Create Date: 2025-09-12 11:28:19.653758

"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70b7ee2edc1c"
down_revision: Union[str, Sequence[str], None] = "458f9a6a8779"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add missing updated_at column to connectors table (if needed)."""
    # Check if the column already exists (for safety)
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if connectors table exists first
    if not inspector.has_table("connectors"):
        print("Connectors table doesn't exist yet - skipping migration")
        return

    # Get table columns
    columns = [col["name"] for col in inspector.get_columns("connectors")]

    # Only add column if it doesn't exist
    if "updated_at" not in columns:
        print("Adding missing updated_at column to connectors table")

        # Add the updated_at column with default value
        op.add_column("connectors", sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True))

        # Update existing rows to have updated_at = created_at
        connection.execute(sa.text("UPDATE connectors SET updated_at = created_at WHERE updated_at IS NULL"))

        # Make the column non-nullable
        op.alter_column("connectors", "updated_at", nullable=False)

        # For PostgreSQL, add trigger to auto-update on modifications
        if connection.dialect.name == "postgresql":
            connection.execute(
                sa.text("""
                CREATE OR REPLACE FUNCTION update_updated_at_column()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ language 'plpgsql';
            """)
            )

            connection.execute(
                sa.text("""
                DROP TRIGGER IF EXISTS update_connectors_updated_at ON connectors;
            """)
            )

            connection.execute(
                sa.text("""
                CREATE TRIGGER update_connectors_updated_at
                    BEFORE UPDATE ON connectors
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();
            """)
            )
    else:
        print("updated_at column already exists - skipping")


def downgrade() -> None:
    """Remove updated_at column from connectors table."""
    # Drop trigger if PostgreSQL
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("DROP TRIGGER IF EXISTS update_connectors_updated_at ON connectors"))
        connection.execute(sa.text("DROP FUNCTION IF EXISTS update_updated_at_column()"))

    # Remove the column
    op.drop_column("connectors", "updated_at")
