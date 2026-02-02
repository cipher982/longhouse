"""Add agents schema for cross-provider session tracking.

Revision ID: 0002_agents_schema
Revises: 0001_baseline
Create Date: 2026-01-28

Creates sessions and events tables for agent tracking.
This enables Zerg to track sessions from all AI providers (Claude Code,
Codex, Gemini, Cursor, Oikos) in a unified format.

For SQLite-only mode, tables live in the main database (no schema separation).
"""

from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_agents_schema"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agents sessions and events tables."""
    # For SQLite-only mode, tables live in the main database (no schema separation)

    # Create sessions table
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("device_id", sa.String(255), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("git_repo", sa.String(500), nullable=True),
        sa.Column("git_branch", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_messages", sa.Integer(), default=0),
        sa.Column("assistant_messages", sa.Integer(), default=0),
        sa.Column("tool_calls", sa.Integer(), default=0),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    # Create indexes for sessions
    op.create_index("ix_sessions_provider", "sessions", ["provider"])
    op.create_index("ix_sessions_project", "sessions", ["project"])
    op.create_index("ix_sessions_device_id", "sessions", ["device_id"])
    op.create_index("ix_sessions_started_at", "sessions", ["started_at"])
    op.create_index("ix_sessions_project_started", "sessions", ["project", "started_at"])
    op.create_index("ix_sessions_provider_started", "sessions", ["provider", "started_at"])

    # Create events table
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("tool_input_json", sa.JSON(), nullable=True),
        sa.Column("tool_output_text", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("source_offset", sa.BigInteger(), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=True),
        sa.Column("schema_version", sa.Integer(), default=1),
    )

    # Create indexes for events
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_index("ix_events_role", "events", ["role"])
    op.create_index("ix_events_tool_name", "events", ["tool_name"])
    op.create_index("ix_events_timestamp", "events", ["timestamp"])
    op.create_index("ix_events_event_hash", "events", ["event_hash"])
    op.create_index("ix_events_session_timestamp", "events", ["session_id", "timestamp"])
    op.create_index("ix_events_role_tool", "events", ["role", "tool_name"])

    # Create unique deduplication index (partial - only when source_path is not null)
    op.execute("""
        CREATE UNIQUE INDEX ix_events_dedup
        ON events (session_id, source_path, source_offset, event_hash)
        WHERE source_path IS NOT NULL
    """)


def downgrade() -> None:
    """Drop agents tables."""
    op.drop_table("events")
    op.drop_table("sessions")
