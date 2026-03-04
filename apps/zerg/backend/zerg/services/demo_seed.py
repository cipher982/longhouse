"""Demo seed/reset helpers shared by startup and API routes."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.demo_sessions import build_demo_agent_sessions

logger = logging.getLogger(__name__)

DEMO_PROVIDER_SESSION_PREFIX = "demo-"


def is_demo_provider_session_id(provider_session_id: str | None) -> bool:
    """Return True when provider_session_id uses the demo seed prefix."""
    return bool(provider_session_id and provider_session_id.startswith(DEMO_PROVIDER_SESSION_PREFIX))


def get_existing_demo_provider_session_ids(db: Session) -> set[str]:
    """Return currently-seeded demo provider session ids."""
    rows = (
        db.query(AgentSession.provider_session_id)
        .filter(AgentSession.provider_session_id.isnot(None))
        .filter(AgentSession.provider_session_id.like(f"{DEMO_PROVIDER_SESSION_PREFIX}%"))
        .all()
    )
    return {row[0] for row in rows if row[0]}


def seed_missing_demo_sessions(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Seed only missing demo sessions.

    Returns:
        (seeded_count, failed_count)
    """
    existing_ids = get_existing_demo_provider_session_ids(db)
    seeded_count = 0
    failed_count = 0

    store = AgentsStore(db)
    sessions = build_demo_agent_sessions(now)

    for session in sessions:
        provider_session_id = session.provider_session_id
        if not is_demo_provider_session_id(provider_session_id):
            logger.warning(
                "Skipping demo seed session with non-demo provider_session_id: %s",
                provider_session_id or "<missing>",
            )
            continue
        if provider_session_id in existing_ids:
            continue

        try:
            store.ingest_session(session)
            seeded_count += 1
            existing_ids.add(provider_session_id)
        except Exception:
            failed_count += 1
            db.rollback()
            logger.exception("Demo seed failed for provider_session_id=%s", provider_session_id)

    # IngestSession commits each session; this commit only persists the FTS rebuild.
    if seeded_count > 0:
        store.rebuild_fts()
        db.commit()

    return seeded_count, failed_count
