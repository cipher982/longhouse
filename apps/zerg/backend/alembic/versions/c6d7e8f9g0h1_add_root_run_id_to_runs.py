"""Add root_run_id to agent_runs for SSE aliasing through chains.

Revision ID: c6d7e8f9g0h1
Revises: b5c6d7e8f9g0
Create Date: 2026-01-25

Enables SSE event aliasing for continuation chains. For direct continuations,
root_run_id equals continuation_of_run_id. For chain continuations (continuation
of continuation), root_run_id points to the original run.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9g0h1"
down_revision: Union[str, Sequence[str], None] = "b5c6d7e8f9g0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Add root_run_id column to agent_runs table."""
    op.add_column(
        "agent_runs",
        sa.Column("root_run_id", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )
    # Add foreign key constraint
    op.create_foreign_key(
        "fk_agent_runs_root_run_id",
        "agent_runs",
        "agent_runs",
        ["root_run_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    # Add index for fast lookups
    op.create_index(
        "ix_agent_runs_root_run_id",
        "agent_runs",
        ["root_run_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove root_run_id column from agent_runs table."""
    op.drop_index("ix_agent_runs_root_run_id", table_name="agent_runs", schema=SCHEMA)
    op.drop_constraint("fk_agent_runs_root_run_id", "agent_runs", type_="foreignkey", schema=SCHEMA)
    op.drop_column("agent_runs", "root_run_id", schema=SCHEMA)
