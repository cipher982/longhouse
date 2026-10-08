"""CatalogStore: the read-only summary of the 2026-07 legacy corpus conversion ledger.

The conversion itself is retired (its source tables no longer exist); the ledger
tables stay on disk as the record of what it covered.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.models import LegacyMigrationRun

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _legacy_migration_run_dto
from zerg.catalogd.store import _legacy_migration_summary
from zerg.catalogd.store import _read_snapshot


class LegacyMigrationMixin:
    def summarize_legacy_migration_run(self, *, run_id: UUID) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        with _read_snapshot(self.engine) as connection:
            run = connection.execute(select(runs).where(runs.c.run_id == str(run_id))).mappings().first()
            if run is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            return {
                "run": _legacy_migration_run_dto(run),
                "summary": _legacy_migration_summary(connection, str(run_id)),
                "commit_seq": str(_current_commit_seq(connection)),
            }
