"""Add user_skills table for DB-backed skills.

Revision ID: d7e8f9g0h1i2
Revises: c6d7e8f9g0h1
Create Date: 2026-01-26
"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7e8f9g0h1i2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9g0h1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Create user_skills table."""
    op.create_table(
        "user_skills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_user_skills_owner_name"),
        schema=SCHEMA,
    )
    op.create_index("ix_user_skills_owner_id", "user_skills", ["owner_id"], unique=False, schema=SCHEMA)
    op.create_index("ix_user_skills_owner_name", "user_skills", ["owner_id", "name"], unique=False, schema=SCHEMA)


def downgrade() -> None:
    """Drop user_skills table."""
    op.drop_index("ix_user_skills_owner_name", table_name="user_skills", schema=SCHEMA)
    op.drop_index("ix_user_skills_owner_id", table_name="user_skills", schema=SCHEMA)
    op.drop_table("user_skills", schema=SCHEMA)
