"""Agents store service for session and event CRUD operations.

Provides a clean interface for ingesting and querying AI coding sessions
from any provider (Claude Code, Codex, Gemini, Cursor, Oikos).
"""

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import bindparam
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas for ingest API
# ---------------------------------------------------------------------------


class EventIngest(BaseModel):
    """Schema for ingesting a single event."""

    role: str = Field(..., description="Message role: user, assistant, tool, system")
    content_text: Optional[str] = Field(None, description="Message text content")
    tool_name: Optional[str] = Field(None, description="Tool name if this is a tool call")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool call parameters")
    tool_output_text: Optional[str] = Field(None, description="Tool result")
    timestamp: datetime = Field(..., description="Event timestamp")
    source_path: Optional[str] = Field(None, description="Original source file path")
    source_offset: Optional[int] = Field(None, description="Byte offset in source file")
    raw_json: Optional[str] = Field(None, description="Original JSONL line for lossless archiving")


class SessionIngest(BaseModel):
    """Schema for ingesting a session with events."""

    id: Optional[UUID] = Field(None, description="Session UUID (generated if not provided)")
    provider: str = Field(..., description="AI provider: claude, codex, gemini, cursor, oikos")
    environment: str = Field(..., description="Environment: production, development, test, e2e")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device/machine identifier")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch name")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    provider_session_id: Optional[str] = Field(None, description="Provider-specific session ID (e.g., Claude Code session UUID)")
    events: List[EventIngest] = Field(default_factory=list, description="Session events")


class IngestResult(BaseModel):
    """Result of an ingest operation."""

    session_id: UUID
    events_inserted: int
    events_skipped: int  # Duplicates that were skipped
    session_created: bool


# ---------------------------------------------------------------------------
# Store class
# ---------------------------------------------------------------------------


class AgentsStore:
    """Service for storing and querying agent sessions."""

    def __init__(self, db: Session):
        self.db = db

    def _fts_available(self) -> bool:
        """Return True if FTS5 index exists for agent events (SQLite only)."""
        bind = self.db.get_bind()
        if bind is None or bind.dialect.name != "sqlite":
            return False
        try:
            row = self.db.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1")).fetchone()
            return row is not None
        except Exception:
            return False

    def rebuild_fts(self) -> None:
        """Rebuild the FTS5 index when available (SQLite only)."""
        if not self._fts_available():
            return
        try:
            self.db.execute(text("INSERT INTO events_fts(events_fts) VALUES('rebuild')"))
        except Exception as exc:
            logger.warning("FTS5 rebuild failed: %s", exc)

    def _fts_query(self, raw: str) -> str:
        """Normalize raw text into a safe FTS query."""
        cleaned = (raw or "").replace('"', '""').strip()
        if not cleaned:
            return cleaned
        # FTS5 treats punctuation as operators; normalize to whitespace for stable matches.
        normalized = re.sub(r"[^\w\s]+", " ", cleaned, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _build_snippet(self, text: Optional[str], query: str, radius: int = 80) -> Optional[str]:
        """Return a short snippet around the first query token match."""
        if not text:
            return None

        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return text[:160] + ("..." if len(text) > 160 else "")

        lowered = text.lower()
        match_index = -1
        match_len = 0
        for term in terms:
            idx = lowered.find(term.lower())
            if idx != -1:
                match_index = idx
                match_len = len(term)
                break

        if match_index == -1:
            return text[:160] + ("..." if len(text) > 160 else "")

        start = max(0, match_index - radius)
        end = min(len(text), match_index + match_len + radius)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    def _fts_match_map(self, session_ids: list[UUID], query: str) -> dict[UUID, dict[str, Any]]:
        """Return best match per session using FTS5."""
        if not session_ids or not self._fts_available():
            return {}
        try:
            stmt = text(
                """
                WITH ranked AS (
                    SELECT
                        e.session_id AS session_id,
                        e.id AS event_id,
                        e.role AS role,
                        e.tool_name AS tool_name,
                        e.content_text AS content_text,
                        e.tool_output_text AS tool_output_text,
                        row_number() OVER (
                            PARTITION BY e.session_id
                            ORDER BY bm25(events_fts)
                        ) AS rn
                    FROM events_fts
                    JOIN events e ON e.id = events_fts.rowid
                    WHERE events_fts MATCH :query
                      AND e.session_id IN :session_ids
                )
                SELECT session_id, event_id, role, tool_name, content_text, tool_output_text
                FROM ranked
                WHERE rn = 1
                """
            ).bindparams(bindparam("session_ids", expanding=True))

            rows = self.db.execute(
                stmt,
                {
                    "query": self._fts_query(query),
                    "session_ids": [str(session_id) for session_id in session_ids],
                },
            ).fetchall()
        except Exception as exc:
            logger.warning("FTS5 match lookup failed: %s", exc)
            return {}

        matches: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            session_id = row.session_id
            if isinstance(session_id, str):
                try:
                    session_id = UUID(session_id)
                except ValueError:
                    # Keep raw value if it can't be parsed; avoids hard failure on bad data.
                    pass
            snippet = (
                self._build_snippet(row.content_text, query)
                or self._build_snippet(row.tool_output_text, query)
                or self._build_snippet(row.tool_name, query)
                or ""
            )
            matches[session_id] = {
                "event_id": row.event_id,
                "snippet": snippet,
                "role": row.role,
            }
        return matches

    def get_session_matches(self, session_ids: list[UUID], query: str) -> dict[UUID, dict[str, Any]]:
        """Return a match map keyed by session id for a query."""
        if not query or not session_ids:
            return {}
        if not self._fts_available():
            raise RuntimeError("FTS5 is required for session search but is not available.")
        return self._fts_match_map(session_ids, query)

    def _fts_session_ids(self, query: str) -> Optional[list[UUID]]:
        """Return session ids matching the FTS query."""
        if not query:
            return None
        if not self._fts_available():
            raise RuntimeError("FTS5 is required for session search but is not available.")
        try:
            rows = self.db.execute(
                text("SELECT DISTINCT session_id FROM events_fts WHERE events_fts MATCH :query"),
                {"query": self._fts_query(query)},
            ).fetchall()
            session_ids: list[UUID] = []
            for row in rows:
                session_id = row[0]
                if isinstance(session_id, str):
                    try:
                        session_id = UUID(session_id)
                    except ValueError:
                        continue
                session_ids.append(session_id)
            return session_ids
        except Exception as exc:
            raise RuntimeError(f"FTS5 search failed: {exc}") from exc

    def _compute_event_hash(self, event: EventIngest) -> str:
        """Compute a hash for deduplication.

        Hash is based on content that uniquely identifies an event.
        """
        content = json.dumps(
            {
                "role": event.role,
                "content_text": event.content_text,
                "tool_name": event.tool_name,
                "tool_input_json": event.tool_input_json,
                "timestamp": event.timestamp.isoformat(),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def ingest_session(self, data: SessionIngest) -> IngestResult:
        """Ingest a session with events, handling deduplication.

        Creates or updates the session and inserts non-duplicate events.

        Returns:
            IngestResult with counts of inserted/skipped events.
        """
        # Use UUID object - SQLAlchemy with_variant handles conversion
        # For Postgres: UUID object stored as native UUID
        # For SQLite: UUID object converted to string
        session_id = data.id if data.id else uuid4()

        # Check if session exists
        existing = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()

        if existing:
            # Update existing session
            existing.ended_at = data.ended_at or existing.ended_at
            session_created = False
        else:
            # Create new session
            session = AgentSession(
                id=session_id,
                provider=data.provider,
                environment=data.environment,
                project=data.project,
                device_id=data.device_id,
                cwd=data.cwd,
                git_repo=data.git_repo,
                git_branch=data.git_branch,
                started_at=data.started_at,
                ended_at=data.ended_at,
                provider_session_id=data.provider_session_id,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
            )
            self.db.add(session)
            self.db.flush()  # Get the ID
            session_created = True

        # Insert events with deduplication
        events_inserted = 0
        events_skipped = 0
        user_count = 0
        assistant_count = 0
        tool_count = 0

        for event_data in data.events:
            event_hash = self._compute_event_hash(event_data)

            # Use ON CONFLICT DO NOTHING for deduplication (SQLite)
            stmt = sqlite_insert(AgentEvent).values(
                session_id=session_id,
                role=event_data.role,
                content_text=event_data.content_text,
                tool_name=event_data.tool_name,
                tool_input_json=event_data.tool_input_json,
                tool_output_text=event_data.tool_output_text,
                timestamp=event_data.timestamp,
                source_path=event_data.source_path,
                source_offset=event_data.source_offset,
                event_hash=event_hash,
                raw_json=event_data.raw_json,
                schema_version=1,
            )

            # Handle deduplication - if source_path is set, use UPSERT
            if event_data.source_path:
                # SQLite: ON CONFLICT DO NOTHING without explicit conflict target
                #
                # SQLite doesn't support targeting partial unique indexes directly in
                # ON CONFLICT clauses. The ix_events_dedup partial index (with sqlite_where)
                # will still prevent duplicates, but we can't explicitly target it.
                #
                # This means ANY unique constraint violation will be silently ignored,
                # not just the dedup index. In practice this is safe because:
                # 1. The 'id' column is auto-generated (no collision possible)
                # 2. The only other unique constraint is ix_events_dedup
                stmt = stmt.on_conflict_do_nothing()

            # Execute insert
            result = self.db.execute(stmt)
            if result.rowcount > 0:
                events_inserted += 1
                # Track counts
                if event_data.role == "user":
                    user_count += 1
                elif event_data.role == "assistant":
                    assistant_count += 1
                    if event_data.tool_name:
                        tool_count += 1
            else:
                events_skipped += 1

        # Update session counts
        session_obj = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session_obj:
            session_obj.user_messages = (session_obj.user_messages or 0) + user_count
            session_obj.assistant_messages = (session_obj.assistant_messages or 0) + assistant_count
            session_obj.tool_calls = (session_obj.tool_calls or 0) + tool_count

        self.db.commit()

        logger.info(f"Ingested session {session_id}: {events_inserted} inserted, {events_skipped} skipped (duplicates)")

        return IngestResult(
            session_id=session_id,
            events_inserted=events_inserted,
            events_skipped=events_skipped,
            session_created=session_created,
        )

    def get_session(self, session_id: UUID) -> Optional[AgentSession]:
        """Get a session by ID."""
        return self.db.query(AgentSession).filter(AgentSession.id == session_id).first()

    def list_sessions(
        self,
        *,
        project: Optional[str] = None,
        provider: Optional[str] = None,
        environment: Optional[str] = None,
        include_test: bool = False,
        device_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[AgentSession], int]:
        """List sessions with optional filters.

        Args:
            environment: Filter to specific environment (production, development, test, e2e)
            include_test: If False (default), excludes test/e2e sessions unless environment is set

        Returns:
            Tuple of (sessions, total_count)
        """
        stmt = select(AgentSession)

        # Environment filtering
        if environment:
            stmt = stmt.where(AgentSession.environment == environment)
        elif not include_test:
            # By default, exclude test and e2e sessions
            stmt = stmt.where(AgentSession.environment.notin_(["test", "e2e"]))

        if project:
            stmt = stmt.where(AgentSession.project.ilike(f"%{project}%"))
        if provider:
            stmt = stmt.where(AgentSession.provider == provider)
        if device_id:
            stmt = stmt.where(AgentSession.device_id == device_id)
        if since:
            stmt = stmt.where(AgentSession.started_at >= since)
        if until:
            stmt = stmt.where(AgentSession.started_at <= until)

        # Content search requires joining events
        if query:
            session_ids = self._fts_session_ids(query)
            if session_ids is not None:
                if not session_ids:
                    return [], 0
                stmt = stmt.where(AgentSession.id.in_(session_ids))

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar() or 0

        # Apply ordering and pagination
        stmt = stmt.order_by(AgentSession.started_at.desc()).limit(limit).offset(offset)

        sessions = list(self.db.execute(stmt).scalars().all())
        return sessions, total

    def get_last_message_map(
        self,
        session_ids: List[UUID],
        *,
        role: str,
        max_len: int | None = None,
    ) -> dict[UUID, str]:
        """Return last message per session for a given role.

        Uses a window function to avoid N+1 queries. Truncates content
        to max_len if provided.
        """
        if not session_ids:
            return {}

        rn = (
            func.row_number()
            .over(
                partition_by=AgentEvent.session_id,
                order_by=AgentEvent.timestamp.desc(),
            )
            .label("rn")
        )

        subq = (
            select(
                AgentEvent.session_id.label("session_id"),
                AgentEvent.content_text.label("content_text"),
                rn,
            )
            .where(AgentEvent.session_id.in_(session_ids))
            .where(AgentEvent.role == role)
            .where(AgentEvent.content_text.isnot(None))
            .subquery()
        )

        stmt = select(subq.c.session_id, subq.c.content_text).where(subq.c.rn == 1)
        rows = self.db.execute(stmt).fetchall()

        result: dict[UUID, str] = {}
        for session_id, content in rows:
            if not content:
                continue
            if max_len is not None:
                content = content[:max_len]
            result[session_id] = content

        return result

    def get_last_activity_map(self, session_ids: List[UUID]) -> dict[UUID, datetime]:
        """Return last activity timestamp per session."""
        if not session_ids:
            return {}

        stmt = (
            select(AgentEvent.session_id, func.max(AgentEvent.timestamp))
            .where(AgentEvent.session_id.in_(session_ids))
            .group_by(AgentEvent.session_id)
        )
        rows = self.db.execute(stmt).fetchall()
        return {session_id: ts for session_id, ts in rows if ts}

    def get_session_preview(self, session_id: UUID, last_n: int) -> List[AgentEvent]:
        """Return last N user/assistant messages for preview (chronological)."""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(AgentEvent.role.in_(["user", "assistant"]))
            .where(AgentEvent.content_text.isnot(None))
            .order_by(AgentEvent.timestamp.desc())
            .limit(last_n)
        )
        rows = list(self.db.execute(stmt).scalars().all())
        rows.reverse()
        return rows

    def get_session_events(
        self,
        session_id: UUID,
        *,
        roles: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AgentEvent]:
        """Get events for a session."""
        stmt = select(AgentEvent).where(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp)

        if roles:
            stmt = stmt.where(AgentEvent.role.in_(roles))

        stmt = stmt.limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_distinct_filters(self, days_back: int = 90) -> dict[str, list[str]]:
        """Get distinct values for filter dropdowns.

        Returns dict with:
          - projects: List of distinct project names
          - providers: List of distinct provider names
        """
        from datetime import timedelta
        from datetime import timezone

        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        # Get distinct projects (non-null)
        projects_stmt = (
            select(AgentSession.project)
            .where(AgentSession.project.isnot(None))
            .where(AgentSession.started_at >= since)
            .distinct()
            .order_by(AgentSession.project)
        )
        projects = [p for (p,) in self.db.execute(projects_stmt).fetchall() if p]

        # Get distinct providers
        providers_stmt = select(AgentSession.provider).where(AgentSession.started_at >= since).distinct().order_by(AgentSession.provider)
        providers = [p for (p,) in self.db.execute(providers_stmt).fetchall() if p]

        return {"projects": projects, "providers": providers}

    def export_session_jsonl(self, session_id: UUID) -> Optional[tuple[bytes, AgentSession]]:
        """Export a session as JSONL bytes for Claude Code --resume.

        If events have raw_json stored (original JSONL lines), those are returned
        verbatim for lossless resume. Otherwise, falls back to synthesized JSONL
        for legacy data.

        Important: Multiple events can be parsed from a single JSONL line (e.g.,
        an assistant message with text + tool_use). We dedupe by source_offset
        to emit each original line only once, preserving the exact original format.

        Returns:
            Tuple of (jsonl_bytes, session) or None if not found.
        """
        session = self.get_session(session_id)
        if not session:
            return None

        # Query events ordered by source_offset for correct file order
        events = (
            self.db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.source_offset.asc(), AgentEvent.timestamp.asc())
            .limit(10000)
            .all()
        )

        # Check if we have raw_json available (lossless path)
        has_raw_json = any(event.raw_json for event in events)

        lines = []
        if has_raw_json:
            # Lossless path: dedupe by (source_path, source_offset)
            # Multiple events from the same JSONL line share the same offset
            seen_offsets: set[tuple[str | None, int | None]] = set()
            for event in events:
                if event.raw_json:
                    key = (event.source_path, event.source_offset)
                    if key not in seen_offsets:
                        seen_offsets.add(key)
                        lines.append(event.raw_json)
                else:
                    # Mixed case: some events have raw_json, some don't
                    # Fall back to synthesized for this event
                    lines.append(self._synthesize_event_jsonl(event))
        else:
            # Legacy path: synthesize JSONL from parsed columns
            for event in events:
                lines.append(self._synthesize_event_jsonl(event))

        content = "\n".join(lines) + "\n" if lines else ""
        return content.encode("utf-8"), session

    def _synthesize_event_jsonl(self, event: AgentEvent) -> str:
        """Synthesize a JSONL line from parsed event columns.

        Used for legacy data that doesn't have raw_json stored.
        """
        line = {
            "role": event.role,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        if event.content_text:
            line["content"] = event.content_text
        if event.tool_name:
            line["tool_name"] = event.tool_name
        if event.tool_input_json:
            line["tool_input"] = event.tool_input_json
        if event.tool_output_text:
            line["tool_output"] = event.tool_output_text
        return json.dumps(line)

    def delete_sessions_by_project_patterns(self, patterns: list[str]) -> int:
        """Delete sessions matching any of the project patterns.

        Used for test cleanup. Patterns are matched with LIKE (e.g., 'test-%').
        Events are cascade-deleted automatically.

        Returns:
            Number of sessions deleted.
        """

        if not patterns:
            return 0

        # Build OR condition for all patterns
        conditions = [AgentSession.project.like(p) for p in patterns]
        sessions = self.db.query(AgentSession).filter(or_(*conditions)).all()

        count = len(sessions)
        for session in sessions:
            self.db.delete(session)

        if count > 0:
            self.db.commit()
            logger.info(f"Deleted {count} test sessions matching patterns: {patterns}")

        return count


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
