"""Database schema initialization for agents."""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def ensure_agents_schema(db: Session) -> None:
    """Ensure the agents schema exists in the database.

    Called during app startup to create schema if needed.
    Only applies to PostgreSQL; SQLite has no schema support.
    """
    engine = db.get_bind()
    if engine.dialect.name != "postgresql":
        return  # SQLite has no schemas

    try:
        db.execute(text("CREATE SCHEMA IF NOT EXISTS agents"))
        db.commit()
        logger.info("Ensured agents schema exists")
    except Exception as e:
        logger.warning(f"Could not create agents schema (may already exist): {e}")
        db.rollback()
