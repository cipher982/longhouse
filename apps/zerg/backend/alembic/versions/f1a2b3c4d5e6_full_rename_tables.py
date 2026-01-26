"""Full rename for French terminology rebrand.

Revision ID: f1a2b3c4d5e6
Revises: x1y2z3a4b5c6, d4e5f6g7h8i9, p0q1r2s3t4u5, u5v6w7x8y9z0
Create Date: 2026-01-26

Renames core tables/columns from Agent/Run/Worker to Fiche/Course/Commis.
"""
from __future__ import annotations

from typing import Iterable

from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = ("x1y2z3a4b5c6", "d4e5f6g7h8i9", "p0q1r2s3t4u5", "u5v6w7x8y9z0")
branch_labels = None
depends_on = None

SCHEMA = "zerg"


def _resolve_schema(inspector) -> str | None:
    if inspector.has_table("fiches", schema=SCHEMA) or inspector.has_table("agents", schema=SCHEMA):
        return SCHEMA
    return None


def _table_exists(inspector, table: str, schema: str | None) -> bool:
    return inspector.has_table(table, schema=schema)


def _column_exists(inspector, table: str, column: str, schema: str | None) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table, schema=schema))


def _rename_table(inspector, schema: str | None, old: str, new: str) -> None:
    if _table_exists(inspector, old, schema) and not _table_exists(inspector, new, schema):
        op.rename_table(old, new, schema=schema)


def _rename_column(inspector, schema: str | None, table: str, old: str, new: str) -> None:
    if not _table_exists(inspector, table, schema):
        return
    if _column_exists(inspector, table, old, schema) and not _column_exists(inspector, table, new, schema):
        op.alter_column(table, old, new_column_name=new, schema=schema)


def _drop_unique_constraint(inspector, schema: str | None, table: str, name: str) -> None:
    if not _table_exists(inspector, table, schema):
        return
    existing = {c["name"] for c in inspector.get_unique_constraints(table, schema=schema)}
    if name in existing:
        op.drop_constraint(name, table, type_="unique", schema=schema)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    schema = _resolve_schema(inspector)

    # Table renames
    _rename_table(inspector, schema, "agents", "fiches")
    _rename_table(inspector, schema, "agent_runs", "courses")
    _rename_table(inspector, schema, "worker_jobs", "commis_jobs")
    _rename_table(inspector, schema, "agent_run_events", "course_events")
    _rename_table(inspector, schema, "worker_barriers", "commis_barriers")
    _rename_table(inspector, schema, "worker_barrier_jobs", "commis_barrier_jobs")
    _rename_table(inspector, schema, "agent_memory_kv", "fiche_memory_kv")
    _rename_table(inspector, schema, "agent_messages", "fiche_messages")

    # Refresh inspector after table renames
    inspector = sa.inspect(conn)

    # Column renames (agent -> fiche)
    for table in ("threads", "connector_credentials", "triggers", "fiche_messages", "fiche_memory_kv"):
        _rename_column(inspector, schema, table, "agent_id", "fiche_id")

    _rename_column(inspector, schema, "threads", "agent_state", "fiche_state")

    # Course-related renames
    _rename_column(inspector, schema, "courses", "agent_id", "fiche_id")
    _rename_column(inspector, schema, "courses", "continuation_of_run_id", "continuation_of_course_id")

    # Course events table column
    _rename_column(inspector, schema, "course_events", "agent_run_id", "course_id")
    _rename_column(inspector, schema, "course_events", "run_id", "course_id")

    # Commis job columns
    _rename_column(inspector, schema, "commis_jobs", "worker_id", "commis_id")
    _rename_column(inspector, schema, "commis_jobs", "supervisor_run_id", "concierge_course_id")

    # Commis barriers
    _rename_column(inspector, schema, "commis_barriers", "run_id", "course_id")
    _rename_column(inspector, schema, "commis_barrier_jobs", "worker_job_id", "job_id")
    _rename_column(inspector, schema, "commis_barrier_jobs", "worker_barrier_id", "barrier_id")

    # Unique constraint rename: uq_agent_owner_name -> uq_fiche_owner_name
    _drop_unique_constraint(inspector, schema, "fiches", "uq_agent_owner_name")
    if _table_exists(inspector, "fiches", schema):
        op.create_unique_constraint("uq_fiche_owner_name", "fiches", ["owner_id", "name"], schema=schema)

    # Unique constraint rename: uix_agent_connector -> uix_fiche_connector
    _drop_unique_constraint(inspector, schema, "connector_credentials", "uix_agent_connector")
    if _table_exists(inspector, "connector_credentials", schema):
        op.create_unique_constraint(
            "uix_fiche_connector",
            "connector_credentials",
            ["fiche_id", "connector_type"],
            schema=schema,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    schema = _resolve_schema(inspector)

    # Drop renamed constraints
    _drop_unique_constraint(inspector, schema, "connector_credentials", "uix_fiche_connector")

    _drop_unique_constraint(inspector, schema, "fiches", "uq_fiche_owner_name")

    # Column renames (fiche -> agent)
    for table in ("threads", "connector_credentials", "triggers", "fiche_messages", "fiche_memory_kv"):
        _rename_column(inspector, schema, table, "fiche_id", "agent_id")

    _rename_column(inspector, schema, "threads", "fiche_state", "agent_state")

    # Course-related renames
    _rename_column(inspector, schema, "courses", "fiche_id", "agent_id")
    _rename_column(inspector, schema, "courses", "continuation_of_course_id", "continuation_of_run_id")

    # Course events table column
    _rename_column(inspector, schema, "course_events", "course_id", "agent_run_id")

    # Commis job columns
    _rename_column(inspector, schema, "commis_jobs", "commis_id", "worker_id")
    _rename_column(inspector, schema, "commis_jobs", "concierge_course_id", "supervisor_run_id")

    # Commis barriers
    _rename_column(inspector, schema, "commis_barriers", "course_id", "run_id")
    _rename_column(inspector, schema, "commis_barrier_jobs", "job_id", "worker_job_id")
    _rename_column(inspector, schema, "commis_barrier_jobs", "barrier_id", "worker_barrier_id")

    # Recreate legacy constraint names after column renames
    if _table_exists(inspector, "connector_credentials", schema):
        op.create_unique_constraint(
            "uix_agent_connector",
            "connector_credentials",
            ["agent_id", "connector_type"],
            schema=schema,
        )

    if _table_exists(inspector, "fiches", schema):
        op.create_unique_constraint("uq_agent_owner_name", "fiches", ["owner_id", "name"], schema=schema)

    # Refresh inspector after column renames before table renames
    inspector = sa.inspect(conn)

    # Table renames
    _rename_table(inspector, schema, "fiche_messages", "agent_messages")
    _rename_table(inspector, schema, "fiche_memory_kv", "agent_memory_kv")
    _rename_table(inspector, schema, "commis_barrier_jobs", "worker_barrier_jobs")
    _rename_table(inspector, schema, "commis_barriers", "worker_barriers")
    _rename_table(inspector, schema, "course_events", "agent_run_events")
    _rename_table(inspector, schema, "commis_jobs", "worker_jobs")
    _rename_table(inspector, schema, "courses", "agent_runs")
    _rename_table(inspector, schema, "fiches", "agents")
