"""Agents store service for session and event CRUD operations.

Provides a clean interface for ingesting and querying AI coding sessions
from any provider (Claude Code, Codex, Gemini, Cursor, Oikos).
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
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
        session_id = data.id or uuid4()

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

            # Use ON CONFLICT DO NOTHING for deduplication
            stmt = insert(AgentEvent).values(
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
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["session_id", "source_path", "source_offset", "event_hash"],
                    index_where=AgentEvent.source_path.isnot(None),
                )

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

        logger.info(f"Ingested session {session_id}: {events_inserted} inserted, " f"{events_skipped} skipped (duplicates)")

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
        device_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[AgentSession], int]:
        """List sessions with optional filters.

        Returns:
            Tuple of (sessions, total_count)
        """
        stmt = select(AgentSession)

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
            subq = select(AgentEvent.session_id).where(AgentEvent.content_text.ilike(f"%{query}%")).distinct().subquery()
            stmt = stmt.where(AgentSession.id.in_(select(subq.c.session_id)))

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar() or 0

        # Apply ordering and pagination
        stmt = stmt.order_by(AgentSession.started_at.desc()).limit(limit).offset(offset)

        sessions = list(self.db.execute(stmt).scalars().all())
        return sessions, total

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


def ensure_agents_schema(db: Session) -> None:
    """Ensure the agents schema exists in the database.

    Called during app startup to create schema if needed.
    """
    try:
        db.execute(text("CREATE SCHEMA IF NOT EXISTS agents"))
        db.commit()
        logger.info("Ensured agents schema exists")
    except Exception as e:
        logger.warning(f"Could not create agents schema (may already exist): {e}")
        db.rollback()
