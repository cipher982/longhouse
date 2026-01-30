"""Add memories table for persistent agent memory.

Revision ID: 0007_add_memories_table
Revises: 0006_add_commis_sandbox
Create Date: 2026-01-30

Adds a simpler memory table distinct from MemoryFile.
Scope model: fiche_id=NULL is global (user-level), fiche_id=X is fiche-specific.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0007_add_memories_table"
down_revision: Union[str, Sequence[str], None] = "0006_add_commis_sandbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create memories table."""
    op.create_table(
        "memories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("zerg.users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fiche_id", sa.Integer(), sa.ForeignKey("zerg.fiches.id", ondelete="CASCADE"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("type", sa.String(50), nullable=True),  # note, decision, bug, preference, fact
        sa.Column("source", sa.String(100), nullable=True),  # oikos, user, import
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        schema="zerg",
    )

    # Index for efficient lookup: user's global + fiche-specific memories
    op.create_index(
        "ix_memories_user_fiche",
        "memories",
        ["user_id", "fiche_id"],
        schema="zerg",
    )

    # Index for type filtering
    op.create_index(
        "ix_memories_user_type",
        "memories",
        ["user_id", "type"],
        schema="zerg",
    )


def downgrade() -> None:
    """Drop memories table."""
    op.drop_index("ix_memories_user_type", table_name="memories", schema="zerg")
    op.drop_index("ix_memories_user_fiche", table_name="memories", schema="zerg")
    op.drop_table("memories", schema="zerg")
