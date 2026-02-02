"""Memory store service for persistent agent memory.

Provides a simple interface for Oikos to save and retrieve memories.
Phase 1: PostgreSQL with keyword search.
Phase 2: Add embeddings for semantic search.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy import or_

from zerg.database import db_session
from zerg.models.models import Memory

logger = logging.getLogger(__name__)


@dataclass
class MemoryRecord:
    """A memory record returned from the store."""

    id: str
    content: str
    type: str | None
    source: str | None
    confidence: float
    created_at: datetime
    fiche_id: int | None = None


class MemoryStore(Protocol):
    """Protocol for memory storage backends."""

    def save(
        self,
        user_id: int,
        content: str,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        source: str | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        """Save a memory.

        Args:
            user_id: Owner user ID
            content: The memory content
            fiche_id: Optional fiche scope (None = global)
            type: Memory type (note, decision, bug, preference, fact)
            source: Where it came from (oikos, user, import)
            confidence: Confidence score 0-1
            expires_at: Optional expiration time

        Returns:
            The saved memory record
        """
        ...

    def search(
        self,
        user_id: int,
        query: str,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Search memories by keyword.

        Args:
            user_id: Owner user ID
            query: Search query
            fiche_id: Optional fiche to include fiche-specific memories
            type: Optional type filter
            limit: Max results

        Returns:
            List of matching memories
        """
        ...

    def list(
        self,
        user_id: int,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        """List memories.

        Args:
            user_id: Owner user ID
            fiche_id: Optional fiche to include fiche-specific memories
            type: Optional type filter
            limit: Max results

        Returns:
            List of memories (most recent first)
        """
        ...

    def delete(self, user_id: int, memory_id: str) -> bool:
        """Delete a memory by ID.

        Args:
            user_id: Owner user ID
            memory_id: Memory UUID

        Returns:
            True if deleted, False if not found
        """
        ...


def _memory_to_record(memory: Memory) -> MemoryRecord:
    """Convert SQLAlchemy model to dataclass."""
    return MemoryRecord(
        id=str(memory.id),
        content=memory.content,
        type=memory.type,
        source=memory.source,
        confidence=memory.confidence,
        created_at=memory.created_at,
        fiche_id=memory.fiche_id,
    )


class SQLMemoryStore:
    """SQLAlchemy-based memory store implementation (SQLite and PostgreSQL compatible).

    Note: Despite the previous name (PostgresMemoryStore), this implementation
    uses only SQLAlchemy abstractions and works with both SQLite and PostgreSQL.
    """

    def save(
        self,
        user_id: int,
        content: str,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        source: str | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        """Save a memory to PostgreSQL."""
        with db_session() as db:
            memory = Memory(
                user_id=user_id,
                fiche_id=fiche_id,
                content=content,
                type=type,
                source=source,
                confidence=confidence,
                expires_at=expires_at,
            )
            db.add(memory)
            db.flush()  # Get the ID
            db.refresh(memory)
            return _memory_to_record(memory)

    def search(
        self,
        user_id: int,
        query: str,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Search memories by keyword (ILIKE).

        Returns global memories + fiche-specific memories if fiche_id provided.
        """
        with db_session() as db:
            # Build base query: user's global + fiche-specific memories
            q = db.query(Memory).filter(Memory.user_id == user_id)

            # Scope: global (fiche_id IS NULL) OR specific fiche
            if fiche_id:
                q = q.filter(
                    or_(
                        Memory.fiche_id.is_(None),
                        Memory.fiche_id == fiche_id,
                    )
                )
            else:
                # Only global memories if no fiche context
                q = q.filter(Memory.fiche_id.is_(None))

            # Filter expired memories
            now = datetime.now(timezone.utc)
            q = q.filter(
                or_(
                    Memory.expires_at.is_(None),
                    Memory.expires_at > now,
                )
            )

            # Keyword search
            pattern = f"%{query}%"
            q = q.filter(Memory.content.ilike(pattern))

            # Type filter
            if type:
                q = q.filter(Memory.type == type)

            # Order by recency
            q = q.order_by(Memory.created_at.desc()).limit(limit)

            return [_memory_to_record(m) for m in q.all()]

    def list(
        self,
        user_id: int,
        *,
        fiche_id: int | None = None,
        type: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        """List memories (most recent first)."""
        with db_session() as db:
            q = db.query(Memory).filter(Memory.user_id == user_id)

            # Scope
            if fiche_id:
                q = q.filter(
                    or_(
                        Memory.fiche_id.is_(None),
                        Memory.fiche_id == fiche_id,
                    )
                )
            else:
                q = q.filter(Memory.fiche_id.is_(None))

            # Filter expired
            now = datetime.now(timezone.utc)
            q = q.filter(
                or_(
                    Memory.expires_at.is_(None),
                    Memory.expires_at > now,
                )
            )

            if type:
                q = q.filter(Memory.type == type)

            q = q.order_by(Memory.created_at.desc()).limit(limit)

            return [_memory_to_record(m) for m in q.all()]

    def delete(self, user_id: int, memory_id: str) -> bool:
        """Delete a memory by ID or ID prefix."""
        with db_session() as db:
            memory = None

            # Allow prefix deletion (>= 8 chars) for usability with short IDs.
            if len(memory_id) < 36:
                prefix = memory_id.strip().lower()
                if not re.fullmatch(r"[0-9a-f-]+", prefix or ""):
                    raise ValueError("Invalid memory ID format")
                if len(prefix) < 8:
                    raise ValueError("Memory ID prefix must be at least 8 characters")

                matches = db.query(Memory).filter(Memory.user_id == user_id).filter(cast(Memory.id, String).ilike(f"{prefix}%")).all()

                if not matches:
                    return False
                if len(matches) > 1:
                    raise ValueError("Memory ID prefix is ambiguous; provide more characters")
                memory = matches[0]
            else:
                memory = (
                    db.query(Memory)
                    .filter(
                        Memory.user_id == user_id,
                        Memory.id == UUID(memory_id),
                    )
                    .first()
                )

            if not memory:
                return False
            db.delete(memory)
            return True


# Backwards-compatible alias (deprecated)
PostgresMemoryStore = SQLMemoryStore

# Singleton instance
_store: SQLMemoryStore | None = None


def get_memory_store() -> SQLMemoryStore:
    """Get the singleton memory store instance."""
    global _store
    if _store is None:
        _store = SQLMemoryStore()
    return _store
