"""Async catch-up for projections skipped by archive ingest."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.models.agents import AgentSession
from zerg.services.agents import AgentsStore

logger = logging.getLogger(__name__)

PROJECTION_RECONCILER_POLL_SECONDS = float(os.getenv("SESSION_PROJECTION_POLL_SECONDS", "5"))
PROJECTION_RECONCILER_BATCH_SIZE = int(os.getenv("SESSION_PROJECTION_BATCH_SIZE", "25"))
PROJECTION_RECONCILER_WRITE_TIMEOUT_SECONDS = float(os.getenv("SESSION_PROJECTION_WRITE_TIMEOUT_SECONDS", "10"))


@dataclass(frozen=True)
class ProjectionReconcileResult:
    selected: int
    reconciled: int
    errors: int


def select_projection_lag_session_ids(db: Session, *, limit: int) -> list[str]:
    """Return sessions whose derived projections need async catch-up."""
    if limit <= 0:
        return []
    rows = (
        db.query(AgentSession.id)
        .filter(func.coalesce(AgentSession.needs_projection, 0) == 1)
        .order_by(
            AgentSession.last_activity_at.desc().nullslast(),
            AgentSession.started_at.desc().nullslast(),
            AgentSession.id,
        )
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


async def reconcile_projection_lag_once(
    *,
    session_factory: sessionmaker | None = None,
    limit: int = PROJECTION_RECONCILER_BATCH_SIZE,
) -> ProjectionReconcileResult:
    """Reconcile one bounded batch of archive-skipped projections."""
    if limit <= 0:
        return ProjectionReconcileResult(selected=0, reconciled=0, errors=0)

    from zerg.database import get_session_factory
    from zerg.services.write_serializer import get_write_serializer

    factory = session_factory or get_session_factory()

    def _select() -> list[str]:
        db = factory()
        try:
            return select_projection_lag_session_ids(db, limit=limit)
        finally:
            db.close()

    selected_ids = await asyncio.to_thread(_select)
    if not selected_ids:
        return ProjectionReconcileResult(selected=0, reconciled=0, errors=0)

    ws = get_write_serializer()
    reconciled = 0
    errors = 0
    for raw_session_id in selected_ids:
        session_id = UUID(raw_session_id)
        try:
            changed = await ws.execute_with_session_factory(
                factory,
                lambda db, sid=session_id: AgentsStore(db).reconcile_derived_projections(sid),
                label="projection-reconcile",
                timeout_seconds=PROJECTION_RECONCILER_WRITE_TIMEOUT_SECONDS,
            )
            if changed:
                reconciled += 1
        except asyncio.TimeoutError:
            errors += 1
            logger.warning("Projection reconciler timed out for session %s", raw_session_id)
        except Exception:
            errors += 1
            logger.exception("Projection reconciler failed for session %s", raw_session_id)

    return ProjectionReconcileResult(selected=len(selected_ids), reconciled=reconciled, errors=errors)


async def run_projection_reconciler(
    *,
    poll_seconds: float = PROJECTION_RECONCILER_POLL_SECONDS,
    batch_size: int = PROJECTION_RECONCILER_BATCH_SIZE,
) -> None:
    """Run projection catch-up until cancelled."""
    logger.info(
        "Session projection reconciler started (poll=%.1fs batch=%d)",
        poll_seconds,
        batch_size,
    )
    while True:
        try:
            result = await reconcile_projection_lag_once(limit=batch_size)
            if result.reconciled or result.errors:
                logger.debug(
                    "Session projection reconciler processed batch (selected=%d reconciled=%d errors=%d)",
                    result.selected,
                    result.reconciled,
                    result.errors,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session projection reconciler tick failed")

        await asyncio.sleep(max(0.0, poll_seconds))
