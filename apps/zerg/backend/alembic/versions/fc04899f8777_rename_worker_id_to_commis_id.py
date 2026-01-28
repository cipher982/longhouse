"""Rename worker_id to commis_id in runner_jobs and llm_audit_log.

Revision ID: fc04899f8777
Revises: fc04899f8776
Create Date: 2026-01-27

These tables were missed in the original rebrand migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "fc04899f8777"
down_revision = "fc04899f8776"
branch_labels = None
depends_on = None

SCHEMA = "zerg"


def _column_exists(inspector, table: str, column: str, schema: str | None) -> bool:
    try:
        return any(col["name"] == column for col in inspector.get_columns(table, schema=schema))
    except Exception:
        return False


def _rename_column(inspector, schema: str | None, table: str, old: str, new: str) -> None:
    if _column_exists(inspector, table, old, schema) and not _column_exists(inspector, table, new, schema):
        op.alter_column(table, old, new_column_name=new, schema=schema)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    _rename_column(inspector, SCHEMA, "runner_jobs", "worker_id", "commis_id")
    _rename_column(inspector, SCHEMA, "llm_audit_log", "worker_id", "commis_id")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    _rename_column(inspector, SCHEMA, "runner_jobs", "commis_id", "worker_id")
    _rename_column(inspector, SCHEMA, "llm_audit_log", "commis_id", "worker_id")
