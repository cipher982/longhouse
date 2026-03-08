"""Agents store service for session and event CRUD operations.

Provides a clean interface for ingesting and querying AI coding sessions
from any provider (Claude Code, Codex, Gemini, Cursor, Oikos).
"""

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import and_
from sqlalchemy import bindparam
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine

logger = logging.getLogger(__name__)

_GENERIC_ENVIRONMENT_LABELS = {"production", "development", "dev", "test", "e2e"}
_CONTINUATION_KIND_LOCAL = "local"
_CONTINUATION_KIND_CLOUD = "cloud"


def _is_generic_environment_label(value: str | None) -> bool:
    """Return True when the label is a broad environment class, not a machine name."""
    if not value:
        return True

    normalized = value.strip().lower()
    return normalized in _GENERIC_ENVIRONMENT_LABELS or normalized.startswith("test:")


def _normalize_utc_naive(value: datetime | None) -> datetime | None:
    """Normalize aware datetimes to naive UTC for SQLite-safe comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _normalize_label(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _infer_continuation_kind_from_ingest(data: "SessionIngest") -> str:
    if data.continuation_kind:
        return data.continuation_kind
    device_id = (data.device_id or "").strip().lower()
    if device_id.startswith("zerg-commis-"):
        return _CONTINUATION_KIND_CLOUD
    return _CONTINUATION_KIND_LOCAL


def _infer_origin_label_from_ingest(data: "SessionIngest") -> str:
    explicit = _normalize_label(data.origin_label)
    if explicit:
        return explicit
    inferred_kind = _infer_continuation_kind_from_ingest(data)
    if inferred_kind == _CONTINUATION_KIND_CLOUD:
        return "Cloud"
    env = _normalize_label(data.environment)
    if env and not _is_generic_environment_label(env):
        return env
    device_id = _normalize_label(data.device_id)
    if device_id:
        return device_id.replace("shipper-", "")
    if env:
        return env
    return "Local"


def _infer_continuation_kind_from_session(session: AgentSession) -> str:
    if session.continuation_kind:
        return session.continuation_kind
    device_id = (session.device_id or "").strip().lower()
    if device_id.startswith("zerg-commis-"):
        return _CONTINUATION_KIND_CLOUD
    return _CONTINUATION_KIND_LOCAL


def _infer_origin_label_from_session(session: AgentSession) -> str:
    explicit = _normalize_label(session.origin_label)
    if explicit:
        return explicit
    inferred_kind = _infer_continuation_kind_from_session(session)
    if inferred_kind == _CONTINUATION_KIND_CLOUD:
        return "Cloud"
    env = _normalize_label(session.environment)
    if env and not _is_generic_environment_label(env):
        return env
    device_id = _normalize_label(session.device_id)
    if device_id:
        return device_id.replace("shipper-", "")
    if env:
        return env
    return "Local"


@dataclass(frozen=True)
class CompactionBoundary:
    """Active-context boundary marker derived from system metadata events."""

    event_id: int
    timestamp: datetime
    source_path: str | None
    source_offset: int | None


@dataclass(frozen=True)
class RewindSignal:
    """Detected rewind trigger in incoming payload."""

    source_path: str
    source_offset: int
    reason: str


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
    tool_call_id: Optional[str] = Field(None, description="Cross-provider call/result linkage ID (Claude tool_use_id, Codex call_id)")
    timestamp: datetime = Field(..., description="Event timestamp")
    source_path: Optional[str] = Field(None, description="Original source file path")
    source_offset: Optional[int] = Field(None, description="Byte offset in source file")
    raw_json: Optional[str] = Field(None, description="Original JSONL line for lossless archiving")


class SourceLineIngest(BaseModel):
    """Schema for ingesting a source line archive row."""

    source_path: str = Field(..., description="Original source file path")
    source_offset: int = Field(..., description="Byte offset in source file")
    raw_json: str = Field(..., description="Original source line without trailing newline")


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
    thread_root_session_id: Optional[UUID] = Field(None, description="Logical thread root session UUID")
    continued_from_session_id: Optional[UUID] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind: local|cloud|runner")
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label, e.g. Cinder or Cloud")
    branched_from_event_id: Optional[int] = Field(None, description="Event ID where this continuation branched from its parent")
    is_sidechain: bool = Field(False, description="True when session is a Task sub-agent (isSidechain:true in JSONL)")
    events: List[EventIngest] = Field(default_factory=list, description="Session events")
    source_lines: List[SourceLineIngest] = Field(default_factory=list, description="Lossless source-line archive")


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

    def _thread_root_id(self, session: AgentSession) -> UUID:
        return session.thread_root_session_id or session.id

    def _coerce_session_lineage_defaults(self, session: AgentSession) -> None:
        if session.thread_root_session_id is None:
            session.thread_root_session_id = session.id
        if session.continuation_kind is None:
            session.continuation_kind = _infer_continuation_kind_from_session(session)
        if not _normalize_label(session.origin_label):
            session.origin_label = _infer_origin_label_from_session(session)
        if session.is_writable_head is None:
            session.is_writable_head = 1

    def _get_thread_sessions(self, session_or_id: UUID | AgentSession) -> list[AgentSession]:
        session = session_or_id if isinstance(session_or_id, AgentSession) else self.get_session(session_or_id)
        if session is None:
            return []
        root_id = self._thread_root_id(session)
        sessions = (
            self.db.query(AgentSession)
            .filter(or_(AgentSession.thread_root_session_id == root_id, AgentSession.id == root_id))
            .order_by(AgentSession.started_at.asc(), AgentSession.created_at.asc(), AgentSession.id.asc())
            .all()
        )
        for item in sessions:
            self._coerce_session_lineage_defaults(item)
        return sessions

    def get_thread_head(self, session_or_id: UUID | AgentSession) -> AgentSession | None:
        session = session_or_id if isinstance(session_or_id, AgentSession) else self.get_session(session_or_id)
        if session is None:
            return None
        root_id = self._thread_root_id(session)
        head = (
            self.db.query(AgentSession)
            .filter(or_(AgentSession.thread_root_session_id == root_id, AgentSession.id == root_id))
            .filter(AgentSession.is_writable_head == 1)
            .order_by(AgentSession.started_at.desc(), AgentSession.created_at.desc(), AgentSession.id.desc())
            .first()
        )
        if head is None:
            return session
        self._coerce_session_lineage_defaults(head)
        return head

    def get_latest_event_id(self, session_id: UUID) -> int | None:
        head_branch_id = self.get_head_branch_id(session_id)
        stmt = self.db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id == session_id)
        if head_branch_id is not None:
            stmt = stmt.filter(AgentEvent.branch_id == head_branch_id)
        return stmt.scalar()

    def _has_novel_source_content(self, session: AgentSession, data: SessionIngest) -> bool:
        source_lines = self._normalize_source_lines_for_ingest(data)
        if not source_lines:
            return bool(data.events)

        head_branch_id = self.get_head_branch_id(session.id)
        source_paths = {line.source_path for line in source_lines}
        latest_by_offset, max_offset_by_path = self._list_branch_source_lines(session.id, head_branch_id, source_paths)

        for line in source_lines:
            source_offset = int(line.source_offset)
            if source_offset > max_offset_by_path.get(line.source_path, -1):
                return True
            row = latest_by_offset.get((line.source_path, source_offset))
            if row is None:
                return True
            if row.line_hash != self._compute_line_hash(line.raw_json):
                return True
        return False

    def _get_source_continuation_base(self, session: AgentSession, data: SessionIngest) -> AgentSession:
        thread_sessions = self._get_thread_sessions(session)
        desired_kind = _infer_continuation_kind_from_ingest(data)
        desired_origin = _infer_origin_label_from_ingest(data)
        provider_session_id = data.provider_session_id or session.provider_session_id

        candidates = [
            item
            for item in thread_sessions
            if (item.provider_session_id or provider_session_id) == provider_session_id
            and _infer_continuation_kind_from_session(item) == desired_kind
            and _infer_origin_label_from_session(item) == desired_origin
        ]
        if not candidates:
            return session
        return max(candidates, key=lambda item: (item.started_at, item.created_at, str(item.id)))

    def create_continuation_session(
        self,
        parent_session_id: UUID,
        *,
        continuation_kind: str,
        origin_label: str,
        branched_from_event_id: int | None = None,
        environment: str | None = None,
        device_id: str | None = None,
        provider_session_id: str | None = None,
        started_at: datetime | None = None,
    ) -> AgentSession:
        parent = self.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Session {parent_session_id} not found")
        self._coerce_session_lineage_defaults(parent)
        root_id = self._thread_root_id(parent)

        (
            self.db.query(AgentSession)
            .filter(or_(AgentSession.thread_root_session_id == root_id, AgentSession.id == root_id))
            .update({AgentSession.is_writable_head: 0}, synchronize_session=False)
        )

        session = AgentSession(
            id=uuid4(),
            provider=parent.provider,
            environment=environment or origin_label,
            project=parent.project,
            device_id=device_id,
            cwd=parent.cwd,
            git_repo=parent.git_repo,
            git_branch=parent.git_branch,
            started_at=started_at or datetime.now(timezone.utc),
            ended_at=None,
            provider_session_id=provider_session_id or parent.provider_session_id,
            thread_root_session_id=root_id,
            continued_from_session_id=parent.id,
            continuation_kind=continuation_kind,
            origin_label=origin_label,
            branched_from_event_id=branched_from_event_id,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            is_writable_head=1,
            is_sidechain=1 if parent.is_sidechain else 0,
        )
        self.db.add(session)
        self.db.flush()
        return session

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

    def _refresh_existing_session_metadata(self, session: AgentSession, data: SessionIngest) -> None:
        """Backfill richer session metadata when the same session is ingested again."""
        self._coerce_session_lineage_defaults(session)

        incoming_started_at = _normalize_utc_naive(data.started_at)
        existing_started_at = _normalize_utc_naive(session.started_at)
        if incoming_started_at and (existing_started_at is None or incoming_started_at < existing_started_at):
            session.started_at = data.started_at

        incoming_ended_at = _normalize_utc_naive(data.ended_at)
        existing_ended_at = _normalize_utc_naive(session.ended_at)
        if incoming_ended_at and (existing_ended_at is None or incoming_ended_at > existing_ended_at):
            session.ended_at = data.ended_at

        if data.is_sidechain:
            session.is_sidechain = 1

        if data.project and not session.project:
            session.project = data.project
        if data.device_id and not session.device_id:
            session.device_id = data.device_id
        if data.cwd and not session.cwd:
            session.cwd = data.cwd
        if data.git_repo and not session.git_repo:
            session.git_repo = data.git_repo
        if data.git_branch and not session.git_branch:
            session.git_branch = data.git_branch
        if data.provider_session_id and not session.provider_session_id:
            session.provider_session_id = data.provider_session_id
        if data.thread_root_session_id and not session.thread_root_session_id:
            session.thread_root_session_id = data.thread_root_session_id
        if data.continued_from_session_id and not session.continued_from_session_id:
            session.continued_from_session_id = data.continued_from_session_id
        if data.continuation_kind and not session.continuation_kind:
            session.continuation_kind = data.continuation_kind
        if data.origin_label and not session.origin_label:
            session.origin_label = data.origin_label
        if data.branched_from_event_id and not session.branched_from_event_id:
            session.branched_from_event_id = data.branched_from_event_id

        incoming_environment = data.environment.strip()
        existing_environment = (session.environment or "").strip()
        if incoming_environment and (
            not existing_environment
            or (_is_generic_environment_label(existing_environment) and not _is_generic_environment_label(incoming_environment))
        ):
            session.environment = incoming_environment

        if session.thread_root_session_id is None:
            session.thread_root_session_id = session.id
        if session.continuation_kind is None:
            session.continuation_kind = _infer_continuation_kind_from_ingest(data)
        if not _normalize_label(session.origin_label):
            session.origin_label = _infer_origin_label_from_ingest(data)

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

    def get_session_matches(
        self,
        session_ids: list[UUID],
        query: str,
        *,
        context_mode: str = "forensic",
        branch_mode: str = "head",
    ) -> dict[UUID, dict[str, Any]]:
        """Return a match map keyed by session id for a query."""
        if not query or not session_ids:
            return {}
        if context_mode == "active_context" or branch_mode == "head":
            matches: dict[UUID, dict[str, Any]] = {}
            for session_id in session_ids:
                events = self.get_session_events(
                    session_id,
                    query=query,
                    context_mode=context_mode,
                    branch_mode=branch_mode,
                    limit=1,
                    offset=0,
                )
                if not events:
                    continue
                event = events[0]
                snippet = (
                    self._build_snippet(event.content_text, query)
                    or self._build_snippet(event.tool_output_text, query)
                    or self._build_snippet(event.tool_name, query)
                    or ""
                )
                matches[session_id] = {
                    "event_id": event.id,
                    "snippet": snippet,
                    "role": event.role,
                }
            return matches

        if not self._fts_available():
            raise RuntimeError("FTS5 is required for session search but is not available.")
        return self._fts_match_map(session_ids, query)

    def _fts_session_ids(
        self,
        query: str,
        *,
        context_mode: str = "forensic",
        branch_mode: str = "head",
    ) -> Optional[list[UUID]]:
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
            if context_mode != "active_context" and branch_mode != "head":
                return session_ids

            # Branch/head projection and active-context projection both require
            # a second pass to confirm an in-scope event still exists.
            filtered: list[UUID] = []
            for session_id in session_ids:
                if (
                    self.count_session_events(
                        session_id,
                        query=query,
                        context_mode=context_mode,
                        branch_mode=branch_mode,
                    )
                    > 0
                ):
                    filtered.append(session_id)
            return filtered
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
                "tool_output_text": event.tool_output_text,
                "tool_call_id": event.tool_call_id,
                "timestamp": event.timestamp.isoformat(),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def _compute_line_hash(self, raw_json: str) -> str:
        """Compute a stable hash for source-line archive rows."""
        return hashlib.sha256(raw_json.encode()).hexdigest()

    def _extract_event_lineage(self, raw_json: str | None) -> tuple[str | None, str | None]:
        """Extract provider-level lineage IDs from a raw JSONL line."""
        if not raw_json:
            return None, None
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(obj, dict):
            return None, None
        event_uuid = obj.get("uuid")
        parent_uuid = obj.get("parentUuid")
        if not isinstance(event_uuid, str):
            event_uuid = None
        if not isinstance(parent_uuid, str):
            parent_uuid = None
        return event_uuid, parent_uuid

    def _extract_leaf_uuid(self, raw_json: str | None) -> str | None:
        """Extract Claude summary leafUuid hint from a raw JSONL line."""
        if not raw_json:
            return None
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        leaf_uuid = obj.get("leafUuid")
        if not isinstance(leaf_uuid, str):
            return None
        return leaf_uuid

    def _normalize_source_lines_for_ingest(self, data: SessionIngest) -> list[SourceLineIngest]:
        """Return source lines for ingest, falling back to event raw_json rows."""
        source_lines = list(data.source_lines)
        if source_lines:
            return source_lines

        seen: set[tuple[str, int, str]] = set()
        normalized: list[SourceLineIngest] = []
        for event_data in data.events:
            if not event_data.raw_json or not event_data.source_path or event_data.source_offset is None:
                continue
            key = (event_data.source_path, int(event_data.source_offset), event_data.raw_json)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                SourceLineIngest(
                    source_path=event_data.source_path,
                    source_offset=int(event_data.source_offset),
                    raw_json=event_data.raw_json,
                )
            )
        return normalized

    def _get_head_branch(self, session_id: UUID) -> AgentSessionBranch | None:
        """Return the current head branch for a session."""
        return (
            self.db.query(AgentSessionBranch)
            .filter(AgentSessionBranch.session_id == session_id)
            .filter(AgentSessionBranch.is_head == 1)
            .order_by(AgentSessionBranch.id.desc())
            .first()
        )

    def _ensure_head_branch(self, session_id: UUID) -> AgentSessionBranch:
        """Ensure a root/head branch exists for the session."""
        head = self._get_head_branch(session_id)
        if head is not None:
            return head

        root = AgentSessionBranch(
            session_id=session_id,
            parent_branch_id=None,
            branched_at_source_path=None,
            branched_at_offset=None,
            branch_reason="root",
            is_head=1,
        )
        self.db.add(root)
        self.db.flush()
        return root

    def _list_branch_source_lines(
        self,
        session_id: UUID,
        branch_id: int,
        source_paths: set[str],
    ) -> tuple[dict[tuple[str, int], AgentSourceLine], dict[str, int]]:
        """Return latest line per (path, offset) and max offset per path for a branch."""
        latest: dict[tuple[str, int], AgentSourceLine] = {}
        max_offset_by_path: dict[str, int] = {}
        if not source_paths:
            return latest, max_offset_by_path

        rows = (
            self.db.query(AgentSourceLine)
            .filter(AgentSourceLine.session_id == session_id)
            .filter(AgentSourceLine.branch_id == branch_id)
            .filter(AgentSourceLine.source_path.in_(sorted(source_paths)))
            .all()
        )
        for row in rows:
            key = (row.source_path, int(row.source_offset))
            prev = latest.get(key)
            if prev is None or int(row.revision) > int(prev.revision):
                latest[key] = row
            max_offset_by_path[row.source_path] = max(
                max_offset_by_path.get(row.source_path, int(row.source_offset)),
                int(row.source_offset),
            )
        return latest, max_offset_by_path

    def _detect_source_rewind_signal(
        self,
        session_id: UUID,
        head_branch_id: int,
        source_lines: list[SourceLineIngest],
    ) -> RewindSignal | None:
        """Detect whether incoming lines rewrite prior offsets (rewind/truncation)."""
        if not source_lines:
            return None

        source_paths = {line.source_path for line in source_lines}
        latest_by_offset, max_offset_by_path = self._list_branch_source_lines(session_id, head_branch_id, source_paths)
        if not latest_by_offset and not max_offset_by_path:
            return None

        lines_by_path: dict[str, list[int]] = defaultdict(list)
        for line in source_lines:
            line_offset = int(line.source_offset)
            lines_by_path[line.source_path].append(line_offset)
            existing = latest_by_offset.get((line.source_path, line_offset))
            if existing is None:
                continue
            incoming_hash = self._compute_line_hash(line.raw_json)
            if incoming_hash != existing.line_hash:
                return RewindSignal(
                    source_path=line.source_path,
                    source_offset=line_offset,
                    reason="rewrite",
                )

        rewind_candidate: RewindSignal | None = None
        for source_path, offsets in lines_by_path.items():
            if not offsets:
                continue
            incoming_max = max(offsets)
            existing_max = max_offset_by_path.get(source_path)
            if existing_max is None or incoming_max >= existing_max:
                continue
            candidate = RewindSignal(
                source_path=source_path,
                source_offset=min(offsets),
                reason="truncation",
            )
            if rewind_candidate is None or candidate.source_offset < rewind_candidate.source_offset:
                rewind_candidate = candidate
        return rewind_candidate

    def _detect_lineage_rewind_signal(
        self,
        session_id: UUID,
        head_branch_id: int,
        events: list[EventIngest],
    ) -> RewindSignal | None:
        """Detect rewind when incoming lineage forks from an existing parent UUID."""
        lineage_rows: list[tuple[EventIngest, str, str]] = []
        parent_ids: set[str] = set()
        for event in events:
            event_uuid, parent_uuid = self._extract_event_lineage(event.raw_json)
            if not event_uuid or not parent_uuid:
                continue
            lineage_rows.append((event, event_uuid, parent_uuid))
            parent_ids.add(parent_uuid)

        if not lineage_rows:
            return None

        parent_rows = (
            self.db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == head_branch_id)
            .filter(AgentEvent.event_uuid.in_(sorted(parent_ids)))
            .all()
        )
        if not parent_rows:
            return None

        parent_by_uuid: dict[str, AgentEvent] = {}
        for row in parent_rows:
            if row.event_uuid and row.event_uuid not in parent_by_uuid:
                parent_by_uuid[row.event_uuid] = row

        child_rows = (
            self.db.query(AgentEvent.parent_event_uuid, AgentEvent.event_uuid)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == head_branch_id)
            .filter(AgentEvent.parent_event_uuid.in_(sorted(parent_ids)))
            .filter(AgentEvent.event_uuid.isnot(None))
            .all()
        )
        children_by_parent: dict[str, set[str]] = defaultdict(set)
        for parent_uuid, child_uuid in child_rows:
            if not isinstance(parent_uuid, str) or not isinstance(child_uuid, str):
                continue
            children_by_parent[parent_uuid].add(child_uuid)

        candidate: RewindSignal | None = None
        for _event, incoming_uuid, parent_uuid in lineage_rows:
            existing_children = children_by_parent.get(parent_uuid)
            if not existing_children or incoming_uuid in existing_children:
                continue
            parent_event = parent_by_uuid.get(parent_uuid)
            if parent_event is None:
                continue
            if parent_event.source_path is None or parent_event.source_offset is None:
                continue
            signal = RewindSignal(
                source_path=parent_event.source_path,
                source_offset=int(parent_event.source_offset) + 1,
                reason="lineage_divergence",
            )
            if candidate is None or signal.source_offset < candidate.source_offset:
                candidate = signal
        return candidate

    def _detect_rewind_signal(
        self,
        session_id: UUID,
        head_branch_id: int,
        source_lines: list[SourceLineIngest],
        events: list[EventIngest],
    ) -> RewindSignal | None:
        """Detect rewind from source rewrites/truncation or lineage divergence."""
        source_signal = self._detect_source_rewind_signal(session_id, head_branch_id, source_lines)
        if source_signal is not None:
            return source_signal
        return self._detect_lineage_rewind_signal(session_id, head_branch_id, events)

    def _fork_head_branch(
        self,
        session_id: UUID,
        head: AgentSessionBranch,
        signal: RewindSignal,
    ) -> AgentSessionBranch:
        """Fork current head branch and return the new head."""
        head.is_head = 0
        next_head = AgentSessionBranch(
            session_id=session_id,
            parent_branch_id=head.id,
            branched_at_source_path=signal.source_path,
            branched_at_offset=signal.source_offset,
            branch_reason=signal.reason,
            is_head=1,
        )
        self.db.add(next_head)
        self.db.flush()
        self._copy_branch_prefix(session_id, head.id, next_head.id, signal)
        return next_head

    def _copy_branch_prefix(
        self,
        session_id: UUID,
        from_branch_id: int,
        to_branch_id: int,
        signal: RewindSignal,
    ) -> None:
        """Copy pre-rewind head state so the new branch remains fully reconstructable."""
        parent_source_lines = (
            self.db.query(AgentSourceLine)
            .filter(AgentSourceLine.session_id == session_id)
            .filter(AgentSourceLine.branch_id == from_branch_id)
            .order_by(AgentSourceLine.source_path.asc(), AgentSourceLine.source_offset.asc(), AgentSourceLine.revision.asc())
            .all()
        )
        latest_by_offset: dict[tuple[str, int], AgentSourceLine] = {}
        for row in parent_source_lines:
            key = (row.source_path, int(row.source_offset))
            prev = latest_by_offset.get(key)
            if prev is None or int(row.revision) > int(prev.revision):
                latest_by_offset[key] = row

        source_copies = []
        for row in latest_by_offset.values():
            row_offset = int(row.source_offset)
            if row.source_path == signal.source_path and row_offset >= signal.source_offset:
                continue
            source_copies.append(
                AgentSourceLine(
                    session_id=session_id,
                    source_path=row.source_path,
                    source_offset=row_offset,
                    branch_id=to_branch_id,
                    revision=1,
                    is_branch_copy=1,
                    raw_json=row.raw_json,
                    line_hash=row.line_hash,
                )
            )
        if source_copies:
            self.db.bulk_save_objects(source_copies)

        parent_events = (
            self.db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == from_branch_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        event_copies = []
        for event in parent_events:
            event_offset = int(event.source_offset) if event.source_offset is not None else None
            if event.source_path == signal.source_path and event_offset is not None and event_offset >= signal.source_offset:
                continue
            event_copies.append(
                AgentEvent(
                    session_id=session_id,
                    branch_id=to_branch_id,
                    role=event.role,
                    content_text=event.content_text,
                    tool_name=event.tool_name,
                    tool_input_json=event.tool_input_json,
                    tool_output_text=event.tool_output_text,
                    tool_call_id=event.tool_call_id,
                    timestamp=event.timestamp,
                    source_path=event.source_path,
                    source_offset=event.source_offset,
                    event_hash=event.event_hash,
                    schema_version=event.schema_version,
                    raw_json=event.raw_json,
                    event_uuid=event.event_uuid,
                    parent_event_uuid=event.parent_event_uuid,
                )
            )
        if event_copies:
            self.db.bulk_save_objects(event_copies)

    def _resolve_ingest_branch(
        self,
        session_id: UUID,
        source_lines: list[SourceLineIngest],
        events: list[EventIngest],
    ) -> tuple[AgentSessionBranch, RewindSignal | None]:
        """Return branch to ingest into, forking when rewind is detected."""
        head = self._ensure_head_branch(session_id)
        signal = self._detect_rewind_signal(session_id, head.id, source_lines, events)
        if signal is None:
            return head, None
        return self._fork_head_branch(session_id, head, signal), signal

    def _sync_session_counts_to_head(self, session_id: UUID, head_branch_id: int) -> None:
        """Recompute denormalized session counts from the active head branch."""
        session_obj = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session_obj is None:
            return

        user_count = (
            self.db.query(func.count())
            .select_from(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == head_branch_id)
            .filter(AgentEvent.role == "user")
            .filter(
                or_(
                    AgentEvent.content_text.is_(None),
                    func.lower(func.trim(AgentEvent.content_text)) != "warmup",
                )
            )
            .scalar()
            or 0
        )
        assistant_count = (
            self.db.query(func.count())
            .select_from(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == head_branch_id)
            .filter(AgentEvent.role == "assistant")
            .filter(AgentEvent.tool_name.is_(None))
            .scalar()
            or 0
        )
        tool_count = (
            self.db.query(func.count())
            .select_from(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == head_branch_id)
            .filter(AgentEvent.role == "assistant")
            .filter(AgentEvent.tool_name.isnot(None))
            .scalar()
            or 0
        )

        session_obj.user_messages = int(user_count)
        session_obj.assistant_messages = int(assistant_count)
        session_obj.tool_calls = int(tool_count)

    def _align_head_branch_from_leaf_uuid(
        self,
        session_id: UUID,
        fallback_head_branch_id: int,
        leaf_uuid: str | None,
    ) -> int:
        """Align active head branch from Claude leafUuid hint when available."""
        if not leaf_uuid:
            return fallback_head_branch_id

        target_branch_id = (
            self.db.query(AgentEvent.branch_id)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.event_uuid == leaf_uuid)
            .order_by(AgentEvent.id.desc())
            .limit(1)
            .scalar()
        )
        if target_branch_id is None:
            return fallback_head_branch_id

        target_branch_id_int = int(target_branch_id)
        if target_branch_id_int == fallback_head_branch_id:
            return fallback_head_branch_id

        self.db.query(AgentSessionBranch).filter(AgentSessionBranch.session_id == session_id).filter(
            AgentSessionBranch.id != target_branch_id_int
        ).update({"is_head": 0}, synchronize_session=False)
        updated = (
            self.db.query(AgentSessionBranch)
            .filter(AgentSessionBranch.session_id == session_id)
            .filter(AgentSessionBranch.id == target_branch_id_int)
            .update({"is_head": 1}, synchronize_session=False)
        )
        if updated == 0:
            return fallback_head_branch_id
        return target_branch_id_int

    def ingest_session(self, data: SessionIngest) -> IngestResult:
        """Ingest a session with events, handling deduplication.

        Creates or updates the session and inserts non-duplicate events.

        Returns:
            IngestResult with counts of inserted/skipped events.
        """
        session_id = data.id if data.id else uuid4()
        incoming_kind = _infer_continuation_kind_from_ingest(data)
        incoming_origin = _infer_origin_label_from_ingest(data)
        incoming_provider_session_id = data.provider_session_id

        existing = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        session_created = False

        if existing:
            self._coerce_session_lineage_defaults(existing)
            target_session = self._get_source_continuation_base(existing, data)
            self._coerce_session_lineage_defaults(target_session)

            if target_session.is_writable_head != 1 and self._has_novel_source_content(target_session, data):
                continuation_started_at = min((event.timestamp for event in data.events), default=datetime.now(timezone.utc))
                target_session = self.create_continuation_session(
                    target_session.id,
                    continuation_kind=incoming_kind,
                    origin_label=incoming_origin,
                    branched_from_event_id=self.get_latest_event_id(target_session.id),
                    environment=data.environment,
                    device_id=data.device_id,
                    provider_session_id=incoming_provider_session_id,
                    started_at=continuation_started_at,
                )
                session_created = True

            self._refresh_existing_session_metadata(target_session, data)
            existing = target_session
            session_id = target_session.id
        else:
            root_id = data.thread_root_session_id or session_id
            if data.thread_root_session_id and data.thread_root_session_id != session_id:
                (
                    self.db.query(AgentSession)
                    .filter(or_(AgentSession.thread_root_session_id == root_id, AgentSession.id == root_id))
                    .update({AgentSession.is_writable_head: 0}, synchronize_session=False)
                )

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
                thread_root_session_id=root_id,
                continued_from_session_id=data.continued_from_session_id,
                continuation_kind=incoming_kind,
                origin_label=incoming_origin,
                branched_from_event_id=data.branched_from_event_id,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                is_writable_head=1,
                is_sidechain=1 if data.is_sidechain else 0,
            )
            self.db.add(session)
            self.db.flush()
            existing = session
            session_created = True

        source_lines = self._normalize_source_lines_for_ingest(data)
        ingest_branch, rewind_signal = self._resolve_ingest_branch(session_id, source_lines, data.events)

        events_inserted = 0
        events_skipped = 0
        leaf_uuid_hint: str | None = None

        for event_data in data.events:
            event_hash = self._compute_event_hash(event_data)
            event_uuid, parent_event_uuid = self._extract_event_lineage(event_data.raw_json)
            event_leaf_uuid = self._extract_leaf_uuid(event_data.raw_json)
            if event_leaf_uuid:
                leaf_uuid_hint = event_leaf_uuid

            stmt = sqlite_insert(AgentEvent).values(
                session_id=session_id,
                branch_id=ingest_branch.id,
                role=event_data.role,
                content_text=event_data.content_text,
                tool_name=event_data.tool_name,
                tool_input_json=event_data.tool_input_json,
                tool_output_text=event_data.tool_output_text,
                tool_call_id=event_data.tool_call_id,
                timestamp=event_data.timestamp,
                source_path=event_data.source_path,
                source_offset=event_data.source_offset,
                event_hash=event_hash,
                raw_json=event_data.raw_json,
                schema_version=1,
                event_uuid=event_uuid,
                parent_event_uuid=parent_event_uuid,
            )

            if event_data.source_path or event_uuid:
                stmt = stmt.on_conflict_do_nothing()

            result = self.db.execute(stmt)
            if result.rowcount > 0:
                events_inserted += 1
            else:
                events_skipped += 1

        source_paths = {line.source_path for line in source_lines}
        latest_line_by_offset, _ = self._list_branch_source_lines(session_id, ingest_branch.id, source_paths)
        latest_state: dict[tuple[str, int], tuple[int, str]] = {
            key: (int(row.revision), row.line_hash) for key, row in latest_line_by_offset.items()
        }

        source_lines_inserted = 0
        for line_data in source_lines:
            line_hash = self._compute_line_hash(line_data.raw_json)
            source_offset = int(line_data.source_offset)
            key = (line_data.source_path, source_offset)
            prev_revision, prev_hash = latest_state.get(key, (0, ""))
            if prev_hash == line_hash:
                continue

            revision = prev_revision + 1
            stmt = sqlite_insert(AgentSourceLine).values(
                session_id=session_id,
                source_path=line_data.source_path,
                source_offset=source_offset,
                branch_id=ingest_branch.id,
                revision=revision,
                is_branch_copy=0,
                raw_json=line_data.raw_json,
                line_hash=line_hash,
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["session_id", "branch_id", "source_path", "source_offset", "line_hash"],
            )
            result = self.db.execute(stmt)
            if result.rowcount and result.rowcount > 0:
                latest_state[key] = (revision, line_hash)
                source_lines_inserted += 1

        head_branch_for_counts = self._align_head_branch_from_leaf_uuid(session_id, ingest_branch.id, leaf_uuid_hint)
        self._sync_session_counts_to_head(session_id, head_branch_for_counts)

        session_obj = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session_obj and events_inserted > 0:
            session_obj.needs_embedding = 1

        if events_inserted > 0:
            from zerg.services.ingest_task_queue import enqueue_ingest_tasks

            enqueue_ingest_tasks(self.db, str(session_id))

        self.db.commit()

        logger.info(
            "Ingested session %s branch=%s rewind=%s events inserted=%s skipped=%s source_lines_inserted=%s",
            session_id,
            head_branch_for_counts,
            rewind_signal.reason if rewind_signal else "none",
            events_inserted,
            events_skipped,
            source_lines_inserted,
        )

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
        exclude_user_states: Optional[list[str]] = None,
        hide_autonomous: bool = True,
        context_mode: str = "forensic",
        branch_mode: str = "head",
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

        # Exclude autonomous agent runs (Task sub-agents and sessions with no user messages)
        if hide_autonomous:
            stmt = stmt.where(AgentSession.user_messages > 0).where(AgentSession.is_sidechain == 0)

        # Exclude sessions by user_state bucket (archived, snoozed, etc.)
        # NULL user_state is treated as 'active' (legacy rows pre-dating the column).
        if exclude_user_states:
            stmt = stmt.where((AgentSession.user_state.notin_(exclude_user_states)) | (AgentSession.user_state.is_(None)))

        # Content search requires joining events
        if query:
            session_ids = self._fts_session_ids(query, context_mode=context_mode, branch_mode=branch_mode)
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

    def get_first_message_map(
        self,
        session_ids: List[UUID],
        *,
        role: str,
        max_len: int | None = None,
    ) -> dict[UUID, str]:
        """Return first message per session for a given role."""
        if not session_ids:
            return {}

        heads_subq = (
            select(
                AgentSessionBranch.session_id.label("session_id"),
                func.max(AgentSessionBranch.id).label("head_branch_id"),
            )
            .where(AgentSessionBranch.session_id.in_(session_ids))
            .where(AgentSessionBranch.is_head == 1)
            .group_by(AgentSessionBranch.session_id)
            .subquery()
        )
        rn = (
            func.row_number()
            .over(
                partition_by=AgentEvent.session_id,
                order_by=AgentEvent.timestamp.asc(),
            )
            .label("rn")
        )

        subq = (
            select(
                AgentEvent.session_id.label("session_id"),
                AgentEvent.content_text.label("content_text"),
                rn,
            )
            .select_from(AgentEvent)
            .outerjoin(heads_subq, AgentEvent.session_id == heads_subq.c.session_id)
            .where(AgentEvent.session_id.in_(session_ids))
            .where(AgentEvent.role == role)
            .where(AgentEvent.content_text.isnot(None))
            .where(
                or_(
                    heads_subq.c.head_branch_id.is_(None),
                    AgentEvent.branch_id == heads_subq.c.head_branch_id,
                )
            )
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

        heads_subq = (
            select(
                AgentSessionBranch.session_id.label("session_id"),
                func.max(AgentSessionBranch.id).label("head_branch_id"),
            )
            .where(AgentSessionBranch.session_id.in_(session_ids))
            .where(AgentSessionBranch.is_head == 1)
            .group_by(AgentSessionBranch.session_id)
            .subquery()
        )
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
            .select_from(AgentEvent)
            .outerjoin(heads_subq, AgentEvent.session_id == heads_subq.c.session_id)
            .where(AgentEvent.session_id.in_(session_ids))
            .where(AgentEvent.role == role)
            .where(AgentEvent.content_text.isnot(None))
            .where(
                or_(
                    heads_subq.c.head_branch_id.is_(None),
                    AgentEvent.branch_id == heads_subq.c.head_branch_id,
                )
            )
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

        heads_subq = (
            select(
                AgentSessionBranch.session_id.label("session_id"),
                func.max(AgentSessionBranch.id).label("head_branch_id"),
            )
            .where(AgentSessionBranch.session_id.in_(session_ids))
            .where(AgentSessionBranch.is_head == 1)
            .group_by(AgentSessionBranch.session_id)
            .subquery()
        )
        stmt = (
            select(AgentEvent.session_id, func.max(AgentEvent.timestamp))
            .select_from(AgentEvent)
            .outerjoin(heads_subq, AgentEvent.session_id == heads_subq.c.session_id)
            .where(AgentEvent.session_id.in_(session_ids))
            .where(
                or_(
                    heads_subq.c.head_branch_id.is_(None),
                    AgentEvent.branch_id == heads_subq.c.head_branch_id,
                )
            )
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
        stmt = self._apply_branch_mode_filter(stmt, session_id, "head")
        rows = list(self.db.execute(stmt).scalars().all())
        rows.reverse()
        return rows

    def _fts_matching_ids(self, session_id: UUID, query: str) -> Optional[List[int]]:
        """Return event IDs matching query within a session via FTS5, or None to use LIKE fallback.

        Returns:
            List of matching event IDs (may be empty — caller should treat as no-match).
            None if FTS is unavailable or fails — caller should fall back to LIKE.
        """
        if not self._fts_available():
            return None
        try:
            rows = self.db.execute(
                text(
                    "SELECT e.id FROM events_fts "
                    "JOIN events e ON e.id = events_fts.rowid "
                    "WHERE events_fts MATCH :q AND e.session_id = :sid"
                ),
                {"q": self._fts_query(query), "sid": str(session_id)},
            ).fetchall()
            return [r[0] for r in rows]
        except Exception as exc:
            logger.warning("FTS5 within-session search failed, falling back to LIKE: %s", exc)
            return None

    def _apply_query_filter(self, stmt, session_id: UUID, query: str):
        """Apply content search filter to a statement using FTS5 or LIKE fallback.

        LIKE fallback searches both content_text and tool_output_text to match FTS coverage.
        Returns (stmt, should_return_empty) — the latter is True when FTS returns no matches.
        """
        matching_ids = self._fts_matching_ids(session_id, query)
        if matching_ids is not None:
            if not matching_ids:
                return stmt, True
            return stmt.where(AgentEvent.id.in_(matching_ids)), False
        # LIKE fallback: cover same fields as FTS index (content_text + tool_output_text)
        like = f"%{query}%"
        return (
            stmt.where(
                or_(
                    AgentEvent.content_text.ilike(like),
                    AgentEvent.tool_output_text.ilike(like),
                )
            ),
            False,
        )

    def _is_compaction_boundary_raw_json(self, raw_json: str | None) -> bool:
        """Return True when a raw line is a compaction boundary marker."""
        if not raw_json:
            return False
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        row_type = obj.get("type")
        if row_type == "summary":
            return True
        if row_type != "system":
            return False
        subtype = obj.get("subtype")
        return subtype in {"compact_boundary", "microcompact_boundary"}

    def get_active_context_boundary(self, session_id: UUID, *, branch_mode: str = "head") -> CompactionBoundary | None:
        """Return the latest compaction boundary marker for a session."""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(AgentEvent.role == "system")
            .where(AgentEvent.raw_json.isnot(None))
            .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        )
        stmt = self._apply_branch_mode_filter(stmt, session_id, branch_mode)
        rows = list(self.db.execute(stmt).scalars().all())
        for event in rows:
            if not self._is_compaction_boundary_raw_json(event.raw_json):
                continue
            source_offset = int(event.source_offset) if event.source_offset is not None else None
            return CompactionBoundary(
                event_id=int(event.id),
                timestamp=event.timestamp,
                source_path=event.source_path,
                source_offset=source_offset,
            )
        return None

    def is_event_in_active_context(self, event: AgentEvent, boundary: CompactionBoundary | None) -> bool:
        """Return True when an event is inside the active context projection."""
        if boundary is None:
            return True

        event_source_offset = int(event.source_offset) if event.source_offset is not None else None
        if (
            boundary.source_offset is not None
            and event_source_offset is not None
            and boundary.source_path
            and event.source_path == boundary.source_path
        ):
            return event_source_offset >= boundary.source_offset

        if event.timestamp > boundary.timestamp:
            return True
        if event.timestamp < boundary.timestamp:
            return False
        return int(event.id) >= boundary.event_id

    def _apply_active_context_filter(self, stmt, boundary: CompactionBoundary):
        """Apply active-context boundary filtering to an event statement."""
        fallback_predicate = or_(
            AgentEvent.timestamp > boundary.timestamp,
            and_(
                AgentEvent.timestamp == boundary.timestamp,
                AgentEvent.id >= boundary.event_id,
            ),
        )
        if boundary.source_path and boundary.source_offset is not None:
            same_source_offset_predicate = and_(
                AgentEvent.source_path == boundary.source_path,
                AgentEvent.source_offset.isnot(None),
                AgentEvent.source_offset >= boundary.source_offset,
            )
            not_same_source_predicate = and_(
                or_(
                    AgentEvent.source_path.is_(None),
                    AgentEvent.source_path != boundary.source_path,
                    AgentEvent.source_offset.is_(None),
                ),
                fallback_predicate,
            )
            return stmt.where(or_(same_source_offset_predicate, not_same_source_predicate))
        return stmt.where(fallback_predicate)

    def get_head_branch_id(self, session_id: UUID) -> int | None:
        """Return head branch ID for a session, if available."""
        row = (
            self.db.query(AgentSessionBranch.id)
            .filter(AgentSessionBranch.session_id == session_id)
            .filter(AgentSessionBranch.is_head == 1)
            .order_by(AgentSessionBranch.id.desc())
            .first()
        )
        return int(row[0]) if row else None

    def _apply_branch_mode_filter(self, stmt, session_id: UUID, branch_mode: str):
        """Apply branch projection filter to an event query."""
        if branch_mode == "all":
            return stmt
        head_branch_id = self.get_head_branch_id(session_id)
        if head_branch_id is None:
            return stmt
        return stmt.where(AgentEvent.branch_id == head_branch_id)

    def get_session_events(
        self,
        session_id: UUID,
        *,
        roles: Optional[List[str]] = None,
        tool_name: Optional[str] = None,
        query: Optional[str] = None,
        context_mode: str = "forensic",
        branch_mode: str = "head",
        limit: int = 100,
        offset: int = 0,
    ) -> List[AgentEvent]:
        """Get events for a session with optional filtering."""
        stmt = select(AgentEvent).where(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp, AgentEvent.id)
        stmt = self._apply_branch_mode_filter(stmt, session_id, branch_mode)

        if context_mode == "active_context":
            boundary = self.get_active_context_boundary(session_id, branch_mode=branch_mode)
            if boundary is not None:
                stmt = self._apply_active_context_filter(stmt, boundary)

        if roles:
            stmt = stmt.where(AgentEvent.role.in_(roles))

        # tool_name: exact ORM match (NOT FTS — underscores get stripped by _fts_query)
        if tool_name:
            stmt = stmt.where(AgentEvent.tool_name == tool_name)

        if query:
            stmt, empty = self._apply_query_filter(stmt, session_id, query)
            if empty:
                return []

        stmt = stmt.limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count_session_events(
        self,
        session_id: UUID,
        *,
        roles: Optional[List[str]] = None,
        tool_name: Optional[str] = None,
        query: Optional[str] = None,
        context_mode: str = "forensic",
        branch_mode: str = "head",
    ) -> int:
        """Count events for a session with the same filters as get_session_events."""
        stmt = select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == session_id)
        stmt = self._apply_branch_mode_filter(stmt, session_id, branch_mode)

        if context_mode == "active_context":
            boundary = self.get_active_context_boundary(session_id, branch_mode=branch_mode)
            if boundary is not None:
                stmt = self._apply_active_context_filter(stmt, boundary)

        if roles:
            stmt = stmt.where(AgentEvent.role.in_(roles))

        if tool_name:
            stmt = stmt.where(AgentEvent.tool_name == tool_name)

        if query:
            stmt, empty = self._apply_query_filter(stmt, session_id, query)
            if empty:
                return 0

        result = self.db.execute(stmt).scalar()
        return result or 0

    def get_distinct_filters(self, days_back: int = 90) -> dict[str, list[str]]:
        """Get distinct values for filter dropdowns.

        Returns dict with:
          - projects: List of distinct project names
          - providers: List of distinct provider names
          - machines: List of distinct machine names (from environment field)
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

        # Get distinct machine names (stored in environment column)
        machines_stmt = (
            select(AgentSession.environment)
            .where(AgentSession.environment.isnot(None))
            .where(AgentSession.started_at >= since)
            .distinct()
            .order_by(AgentSession.environment)
        )
        machines = [m for (m,) in self.db.execute(machines_stmt).fetchall() if m]

        return {"projects": projects, "providers": providers, "machines": machines}

    def export_session_jsonl(
        self,
        session_id: UUID,
        *,
        branch_mode: str = "head",
    ) -> Optional[tuple[bytes, AgentSession]]:
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

        source_lines_query = self.db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id)
        head_branch_id = self.get_head_branch_id(session_id)
        if branch_mode == "head" and head_branch_id is not None:
            source_lines_query = source_lines_query.filter(AgentSourceLine.branch_id == head_branch_id)
        if branch_mode == "all":
            # Forensic export should reflect the raw archive stream, not branch-prefix copies.
            source_lines_query = source_lines_query.filter(
                or_(
                    AgentSourceLine.is_branch_copy.is_(None),  # legacy rows predating the column
                    AgentSourceLine.is_branch_copy == 0,
                )
            )
            source_lines = source_lines_query.order_by(AgentSourceLine.id.asc()).all()
        else:
            source_lines = source_lines_query.order_by(
                AgentSourceLine.branch_id.asc(),
                AgentSourceLine.source_path.asc(),
                AgentSourceLine.source_offset.asc(),
                AgentSourceLine.revision.asc(),
                AgentSourceLine.id.asc(),
            ).all()
        if source_lines:
            if branch_mode == "all":
                lines = [row.raw_json for row in source_lines]
            else:
                latest_by_offset: dict[tuple[str, int], AgentSourceLine] = {}
                for row in source_lines:
                    key = (row.source_path, int(row.source_offset))
                    prev = latest_by_offset.get(key)
                    if prev is None or int(row.revision) > int(prev.revision):
                        latest_by_offset[key] = row
                normalized_source_lines = sorted(
                    latest_by_offset.values(),
                    key=lambda row: (row.source_path, int(row.source_offset), int(row.id)),
                )

                # Most sessions map to one source_path. In mixed-path edge cases,
                # export the dominant path for stable --resume behavior.
                path_counts: dict[str, int] = {}
                for row in normalized_source_lines:
                    path_counts[row.source_path] = path_counts.get(row.source_path, 0) + 1
                primary_path = max(path_counts.items(), key=lambda item: item[1])[0]
                if len(path_counts) > 1:
                    logger.warning(
                        "Session %s has %d source paths in archive; exporting primary path %s",
                        session_id,
                        len(path_counts),
                        primary_path,
                    )
                lines = [row.raw_json for row in normalized_source_lines if row.source_path == primary_path]
            content = "\n".join(lines) + "\n" if lines else ""
            return content.encode("utf-8"), session

        # Legacy fallback path: rebuild from events only.
        events_stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.source_offset.asc(), AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .limit(10000)
        )
        events_stmt = self._apply_branch_mode_filter(events_stmt, session_id, branch_mode)
        events = list(self.db.execute(events_stmt).scalars().all())

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
