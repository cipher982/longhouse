"""Add agents schema for cross-provider session tracking.

Revision ID: 0002_agents_schema
Revises: 0001_baseline
Create Date: 2026-01-28

Creates the 'agents' schema with sessions and events tables.
This enables Zerg to track sessions from all AI providers (Claude Code,
Codex, Gemini, Cursor, Oikos) in a unified format.

The schema is separate from 'zerg' to allow OSS users to run standalone
without the Life Hub dependency.
"""

from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_agents_schema"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agents schema with sessions and events tables."""
    # Create the agents schema
    op.execute("CREATE SCHEMA IF NOT EXISTS agents")

    # Create sessions table
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        schema="agents",
    )

    # Create indexes for sessions
    op.create_index("ix_sessions_provider", "sessions", ["provider"], schema="agents")
    op.create_index("ix_sessions_project", "sessions", ["project"], schema="agents")
    op.create_index("ix_sessions_device_id", "sessions", ["device_id"], schema="agents")
    op.create_index("ix_sessions_started_at", "sessions", ["started_at"], schema="agents")
    op.create_index("ix_sessions_project_started", "sessions", ["project", "started_at"], schema="agents")
    op.create_index("ix_sessions_provider_started", "sessions", ["provider", "started_at"], schema="agents")

    # Create events table
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("tool_input_json", postgresql.JSONB(), nullable=True),
        sa.Column("tool_output_text", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("source_offset", sa.BigInteger(), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=True),
        sa.Column("schema_version", sa.Integer(), default=1),
        schema="agents",
    )

    # Create indexes for events
    op.create_index("ix_events_session_id", "events", ["session_id"], schema="agents")
    op.create_index("ix_events_role", "events", ["role"], schema="agents")
    op.create_index("ix_events_tool_name", "events", ["tool_name"], schema="agents")
    op.create_index("ix_events_timestamp", "events", ["timestamp"], schema="agents")
    op.create_index("ix_events_event_hash", "events", ["event_hash"], schema="agents")
    op.create_index("ix_events_session_timestamp", "events", ["session_id", "timestamp"], schema="agents")
    op.create_index("ix_events_role_tool", "events", ["role", "tool_name"], schema="agents")

    # Create unique deduplication index (partial - only when source_path is not null)
    op.execute("""
        CREATE UNIQUE INDEX ix_events_dedup
        ON agents.events (session_id, source_path, source_offset, event_hash)
        WHERE source_path IS NOT NULL
    """)


def downgrade() -> None:
    """Drop agents schema and all tables."""
    op.execute("DROP SCHEMA IF EXISTS agents CASCADE")
