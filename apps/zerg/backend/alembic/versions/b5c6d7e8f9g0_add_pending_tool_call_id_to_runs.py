"""Add pending_tool_call_id to agent_runs for async inbox model.

Revision ID: b5c6d7e8f9g0
Revises: a4b5c6d7e8f9
Create Date: 2026-01-25

Stores the tool_call_id from wait_for_worker interrupts so resume can
inject results into the correct tool call.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5c6d7e8f9g0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Add pending_tool_call_id column to agent_runs table."""
    op.add_column(
        "agent_runs",
        sa.Column("pending_tool_call_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove pending_tool_call_id column from agent_runs table."""
    op.drop_column("agent_runs", "pending_tool_call_id", schema=SCHEMA)
