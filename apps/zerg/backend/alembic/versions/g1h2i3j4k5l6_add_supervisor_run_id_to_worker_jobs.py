"""add_concierge_course_id_to_commis_jobs

Revision ID: g1h2i3j4k5l6
Revises: f00aae7c144f
Create Date: 2025-12-07

Adds concierge_course_id column to commis_jobs table with ON DELETE SET NULL
to prevent ForeignKeyViolation when concierge runs are cleaned up.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g1h2i3j4k5l6'
down_revision: Union[str, Sequence[str], None] = 'f00aae7c144f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add concierge_course_id column with ON DELETE SET NULL foreign key."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if commis_jobs table exists
    if not inspector.has_table("commis_jobs"):
        print("commis_jobs table doesn't exist yet - skipping migration")
        return

    # Get existing columns
    columns = [col["name"] for col in inspector.get_columns("commis_jobs")]

    if "concierge_course_id" not in columns:
        print("Adding concierge_course_id column to commis_jobs table")
        # Add the column without foreign key first
        op.add_column(
            "commis_jobs",
            sa.Column("concierge_course_id", sa.Integer(), nullable=True, index=True),
        )
        # Add foreign key with ON DELETE SET NULL
        op.create_foreign_key(
            "fk_commis_jobs_concierge_course_id",
            "commis_jobs",
            "courses",
            ["concierge_course_id"],
            ["id"],
            ondelete="SET NULL",
        )
        print("concierge_course_id column added with ON DELETE SET NULL")
    else:
        # Column exists - check if FK has correct ON DELETE behavior
        # We need to drop and recreate the FK with ON DELETE SET NULL
        print("concierge_course_id column exists - updating foreign key constraint")

        # Get existing foreign keys
        fks = inspector.get_foreign_keys("commis_jobs")
        fk_name = None
        for fk in fks:
            if "concierge_course_id" in fk.get("constrained_columns", []):
                fk_name = fk.get("name")
                break

        if fk_name:
            # Drop existing FK and recreate with ON DELETE SET NULL
            print(f"Dropping existing foreign key: {fk_name}")
            op.drop_constraint(fk_name, "commis_jobs", type_="foreignkey")

        # Create new FK with ON DELETE SET NULL
        op.create_foreign_key(
            "fk_commis_jobs_concierge_course_id",
            "commis_jobs",
            "courses",
            ["concierge_course_id"],
            ["id"],
            ondelete="SET NULL",
        )
        print("Foreign key recreated with ON DELETE SET NULL")


def downgrade() -> None:
    """Remove concierge_course_id column from commis_jobs table."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    if not inspector.has_table("commis_jobs"):
        print("commis_jobs table doesn't exist - skipping downgrade")
        return

    columns = [col["name"] for col in inspector.get_columns("commis_jobs")]

    if "concierge_course_id" in columns:
        print("Removing concierge_course_id column from commis_jobs table")
        # Drop FK first
        op.drop_constraint("fk_commis_jobs_concierge_course_id", "commis_jobs", type_="foreignkey")
        op.drop_column("commis_jobs", "concierge_course_id")
        print("concierge_course_id column removed")
