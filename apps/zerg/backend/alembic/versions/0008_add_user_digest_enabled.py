"""Add digest_enabled column to users table.

Revision ID: 0008_add_user_digest_enabled
Revises: 0007_add_memories_table
Create Date: 2026-02-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_add_user_digest_enabled"
down_revision = "1fdb04873bc0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add digest_enabled column with default False
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("digest_enabled", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("digest_enabled")
