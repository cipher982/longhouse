"""Move Zerg tables into the zerg schema.

Revision ID: x1y2z3a4b5c6
Revises: w7x8y9z0a1b2
Create Date: 2026-01-20
"""

from alembic import op
from sqlalchemy import text

revision = "x1y2z3a4b5c6"
down_revision = "w7x8y9z0a1b2"
branch_labels = None
depends_on = None

SCHEMA = "zerg"
TABLES = [
    "account_connector_credentials",
    "fiche_memory_kv",
    "fiche_messages",
    "course_events",
    "courses",
    "threads",
    "fiches",
    "commis_barrier_jobs",
    "canvas_layouts",
    "connector_credentials",
    "connectors",
    "knowledge_documents",
    "knowledge_sources",
    "llm_audit_log",
    "memory_embeddings",
    "memory_files",
    "node_execution_states",
    "runner_enroll_tokens",
    "runner_jobs",
    "runners",
    "sync_operations",
    "thread_messages",
    "triggers",
    "user_tasks",
    "users",
    "waitlist_entries",
    "commis_barriers",
    "commis_jobs",
    "workflow_executions",
    "workflow_templates",
    "workflows",
]


def _table_exists(conn, schema: str, table: str) -> bool:
    return (
        conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = :schema AND table_name = :table
                """
            ),
            {"schema": schema, "table": table},
        ).scalar()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    for table in TABLES:
        if _table_exists(conn, "public", table) and not _table_exists(conn, SCHEMA, table):
            conn.execute(text(f'ALTER TABLE public."{table}" SET SCHEMA {SCHEMA}'))

    # Move alembic_version only if it still lives in public.
    if _table_exists(conn, "public", "alembic_version") and not _table_exists(conn, SCHEMA, "alembic_version"):
        conn.execute(text(f"ALTER TABLE public.alembic_version SET SCHEMA {SCHEMA}"))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    for table in TABLES:
        if _table_exists(conn, SCHEMA, table) and not _table_exists(conn, "public", table):
            conn.execute(text(f'ALTER TABLE {SCHEMA}."{table}" SET SCHEMA public'))
