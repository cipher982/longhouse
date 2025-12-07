"""add_supervisor_run_id_to_worker_jobs

Revision ID: g1h2i3j4k5l6
Revises: f00aae7c144f
Create Date: 2025-12-07

Adds supervisor_run_id column to worker_jobs table with ON DELETE SET NULL
to prevent ForeignKeyViolation when supervisor runs are cleaned up.
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
    """Add supervisor_run_id column with ON DELETE SET NULL foreign key."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Check if worker_jobs table exists
    if not inspector.has_table("worker_jobs"):
        print("worker_jobs table doesn't exist yet - skipping migration")
        return

    # Get existing columns
    columns = [col["name"] for col in inspector.get_columns("worker_jobs")]

    if "supervisor_run_id" not in columns:
        print("Adding supervisor_run_id column to worker_jobs table")
        # Add the column without foreign key first
        op.add_column(
            "worker_jobs",
            sa.Column("supervisor_run_id", sa.Integer(), nullable=True, index=True),
        )
        # Add foreign key with ON DELETE SET NULL
        op.create_foreign_key(
            "fk_worker_jobs_supervisor_run_id",
            "worker_jobs",
            "agent_runs",
            ["supervisor_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        print("supervisor_run_id column added with ON DELETE SET NULL")
    else:
        # Column exists - check if FK has correct ON DELETE behavior
        # We need to drop and recreate the FK with ON DELETE SET NULL
        print("supervisor_run_id column exists - updating foreign key constraint")

        # Get existing foreign keys
        fks = inspector.get_foreign_keys("worker_jobs")
        fk_name = None
        for fk in fks:
            if "supervisor_run_id" in fk.get("constrained_columns", []):
                fk_name = fk.get("name")
                break

        if fk_name:
            # Drop existing FK and recreate with ON DELETE SET NULL
            print(f"Dropping existing foreign key: {fk_name}")
            op.drop_constraint(fk_name, "worker_jobs", type_="foreignkey")

        # Create new FK with ON DELETE SET NULL
        op.create_foreign_key(
            "fk_worker_jobs_supervisor_run_id",
            "worker_jobs",
            "agent_runs",
            ["supervisor_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        print("Foreign key recreated with ON DELETE SET NULL")


def downgrade() -> None:
    """Remove supervisor_run_id column from worker_jobs table."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    if not inspector.has_table("worker_jobs"):
        print("worker_jobs table doesn't exist - skipping downgrade")
        return

    columns = [col["name"] for col in inspector.get_columns("worker_jobs")]

    if "supervisor_run_id" in columns:
        print("Removing supervisor_run_id column from worker_jobs table")
        # Drop FK first
        op.drop_constraint("fk_worker_jobs_supervisor_run_id", "worker_jobs", type_="foreignkey")
        op.drop_column("worker_jobs", "supervisor_run_id")
        print("supervisor_run_id column removed")
