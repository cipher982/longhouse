"""Agents store service for session and event CRUD operations.

Provides a clean interface for ingesting and querying AI coding sessions
from any provider (Claude Code, Codex, Antigravity, legacy Gemini, Cursor).
"""

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import List
from typing import Optional
from uuid import UUID
from uuid import uuid4

from sqlalchemy import and_
from sqlalchemy import bindparam
from sqlalchemy import case
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
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.internal_sessions import internal_canary_session_clause
from zerg.services.internal_sessions import is_internal_canary_provider_filter
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.provisional_events import visible_transcript_event_predicate
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_observation_reducers import ProviderEventReduction
from zerg.services.session_observation_reducers import reduce_provider_event_observation
from zerg.services.session_observations import record_provider_event_observation
from zerg.services.session_observations import record_source_line_observation
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import is_generic_environment_label
from zerg.session_execution_home import normalize_session_label

from .helpers import _infer_continuation_kind_from_ingest
from .helpers import _infer_continuation_kind_from_session
from .helpers import _infer_execution_home_from_ingest
from .helpers import _infer_origin_label_from_ingest
from .helpers import _infer_origin_label_from_session
from .helpers import _normalize_utc_naive
from .models import CompactionBoundary
from .models import EventIngest
from .models import IngestResult
from .models import RewindSignal
from .models import SessionIngest
from .models import SessionProjectionItem
from .models import SessionProjectionPage
from .models import SourceLineIngest
from .models import SourceRewindHintIngest

logger = logging.getLogger(__name__)


def _is_managed_codex_ingest(
    db: Session,
    session: AgentSession | None,
    data: SessionIngest,
    incoming_execution_home: SessionExecutionHome,
) -> bool:
    provider = str((session.provider if session is not None else data.provider) or "").strip().lower()
    if provider != "codex":
        return False
    if incoming_execution_home == SessionExecutionHome.MANAGED_LOCAL:
        return True
    if session is not None:
        from zerg.services.agents.kernel_capabilities import project_session_capabilities

        caps = project_session_capabilities(db, session_id=session.id)
        return bool(caps.live_control_available or caps.host_reattach_available)
    return False


class AgentsStore:
    """Service for storing and querying agent sessions."""

    def __init__(self, db: Session):
        self.db = db

    def _thread_root_id(self, session: AgentSession) -> UUID:
        return session.id

    def _coerce_session_lineage_defaults(self, session: AgentSession) -> None:
        pass

    def _has_final_managed_codex_terminal(self, session: AgentSession) -> bool:
        return (
            self.db.query(SessionRuntimeState.runtime_key)
            .filter(SessionRuntimeState.session_id == session.id)
            .filter(SessionRuntimeState.terminal_state == "session_ended")
            .first()
            is not None
        )

    def batch_thread_meta(self, sessions: list[AgentSession]) -> dict[str, tuple[str, int]]:
        """Batch-load thread head ID and continuation count for multiple sessions.

        Returns a dict keyed by thread root ID → (head_session_id, count).
        """
        result: dict[str, tuple[str, int]] = {}
        for s in sessions:
            sid = str(s.id)
            result[sid] = (sid, 1)
        return result

    def _get_thread_sessions(self, session_or_id: UUID | AgentSession) -> list[AgentSession]:
        session = session_or_id if isinstance(session_or_id, AgentSession) else self.get_session(session_or_id)
        if session is None:
            return []
        return [session]

    def get_thread_head(self, session_or_id: UUID | AgentSession) -> AgentSession | None:
        session = session_or_id if isinstance(session_or_id, AgentSession) else self.get_session(session_or_id)
        if session is None:
            return None
        return session

    def get_latest_event_id(self, session_id: UUID) -> int | None:
        head_branch_id = self.get_head_branch_id(session_id)
        stmt = self.db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id == session_id)
        if head_branch_id is not None:
            stmt = stmt.filter(AgentEvent.branch_id == head_branch_id)
        stmt = stmt.filter(durable_transcript_event_predicate())
        return stmt.scalar()

    def _has_novel_source_content(self, session: AgentSession, data: SessionIngest) -> bool:
        source_lines = self._normalize_source_lines_for_ingest(data)
        if not source_lines:
            return bool(data.events)

        head_branch_id = self.get_head_branch_id(session.id)
        source_paths = {line.source_path for line in source_lines}
        latest_by_offset, max_offset_by_path = self._list_branch_source_lines(
            session.id,
            head_branch_id,
            source_paths,
            source_offsets_by_path=self._source_offsets_by_path(source_lines),
        )

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
        return max(
            candidates,
            key=lambda item: (
                1 if item.is_writable_head else 0,
                item.started_at,
                item.created_at,
                str(item.id),
            ),
        )

    def list_thread_sessions(self, session_or_id: UUID | AgentSession) -> list[AgentSession]:
        return self._get_thread_sessions(session_or_id)

    def get_session_lineage_path(self, session_or_id: UUID | AgentSession) -> list[AgentSession]:
        session = session_or_id if isinstance(session_or_id, AgentSession) else self.get_session(session_or_id)
        if session is None:
            return []
        return [session]

    def get_sessions_ordered(self, session_ids: list[UUID | str]) -> list[AgentSession]:
        ordered_ids: list[UUID] = []
        seen: set[UUID] = set()
        for session_id in session_ids:
            session_uuid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
            if session_uuid in seen:
                continue
            ordered_ids.append(session_uuid)
            seen.add(session_uuid)
        if not ordered_ids:
            return []

        sessions = self.db.query(AgentSession).filter(AgentSession.id.in_(ordered_ids)).all()
        session_map: dict[UUID, AgentSession] = {}
        for session in sessions:
            self._coerce_session_lineage_defaults(session)
            session_map[session.id] = session
        return [session_map[session_id] for session_id in ordered_ids if session_id in session_map]

    def get_session_projection_page(
        self,
        session_or_id: UUID | AgentSession,
        *,
        branch_mode: str = "head",
        limit: int = 100,
        offset: int = 0,
        load_from_end: bool = False,
    ) -> SessionProjectionPage:
        path_sessions = self.get_session_lineage_path(session_or_id)
        if not path_sessions:
            return SessionProjectionPage(
                path_sessions=[],
                items=[],
                total=0,
                abandoned_events=0,
                branch_mode=branch_mode,
                page_offset=0,
            )

        event_counts: list[int] = []
        total = 0
        abandoned_events = 0

        for index, path_session in enumerate(path_sessions):
            visible_count = self.count_session_events(path_session.id, branch_mode=branch_mode)
            event_counts.append(visible_count)
            total += visible_count
            if index > 0:
                total += 1
            if branch_mode == "head":
                forensic_total = self.count_session_events(path_session.id, branch_mode="all")
                abandoned_events += max(0, forensic_total - visible_count)

        if load_from_end:
            offset = max(0, total - limit - offset)

        items: list[SessionProjectionItem] = []
        remaining_offset = offset
        remaining_limit = limit

        for index, path_session in enumerate(path_sessions):
            parent_session = path_sessions[index - 1] if index > 0 else None

            if parent_session is not None:
                if remaining_offset > 0:
                    remaining_offset -= 1
                elif remaining_limit > 0:
                    items.append(
                        SessionProjectionItem(
                            kind="seam",
                            session=path_session,
                            parent_session=parent_session,
                        )
                    )
                    remaining_limit -= 1

            session_event_count = event_counts[index]
            if session_event_count <= 0:
                if remaining_limit <= 0:
                    break
                continue

            if remaining_offset >= session_event_count:
                remaining_offset -= session_event_count
                if remaining_limit <= 0:
                    break
                continue

            local_offset = remaining_offset
            fetch_count = min(remaining_limit, session_event_count - local_offset)
            if fetch_count > 0:
                events = self.get_session_events(
                    path_session.id,
                    branch_mode=branch_mode,
                    limit=fetch_count,
                    offset=local_offset,
                )
                items.extend(
                    SessionProjectionItem(
                        kind="event",
                        session=path_session,
                        event=event,
                        parent_session=parent_session,
                    )
                    for event in events
                )
                remaining_limit -= len(events)
            remaining_offset = 0

            if remaining_limit <= 0:
                break

        return SessionProjectionPage(
            path_sessions=path_sessions,
            items=items,
            total=total,
            abandoned_events=abandoned_events,
            branch_mode=branch_mode,
            page_offset=offset,
        )

    def create_continuation_session(
        self,
        parent_session_id: UUID,
        *,
        continuation_kind: str,
        origin_label: str,
        branched_from_event_id: int | None = None,
        started_at: datetime | None = None,
        provider_session_id: str | None = None,
        environment: str | None = None,
        device_id: str | None = None,
    ) -> AgentSession:
        parent = self.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Session {parent_session_id} not found")
        effective_started = started_at or datetime.now(timezone.utc)
        session = AgentSession(
            id=uuid4(),
            provider=parent.provider,
            environment=environment or origin_label,
            project=parent.project,
            device_id=device_id,
            cwd=parent.cwd,
            git_repo=parent.git_repo,
            git_branch=parent.git_branch,
            started_at=effective_started,
            ended_at=None,
            last_activity_at=_normalize_utc_naive(effective_started),
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
        )
        self.db.add(session)
        self.db.flush()
        ensure_primary_thread(self.db, session)
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
        incoming_execution_home = _infer_execution_home_from_ingest(data)

        incoming_started_at = _normalize_utc_naive(data.started_at)
        existing_started_at = _normalize_utc_naive(session.started_at)
        if incoming_started_at and (existing_started_at is None or incoming_started_at < existing_started_at):
            session.started_at = data.started_at

        incoming_ended_at = _normalize_utc_naive(data.ended_at)
        managed_codex_ingest = _is_managed_codex_ingest(self.db, session, data, incoming_execution_home)
        # Phase 4 of session-liveness-honesty: the engine's `ended_at` is
        # max(event.timestamp), which is last-activity — NOT a closure
        # signal. Route it into last_activity_at and leave ended_at alone.
        # Real terminal state comes from SessionRuntimeState via explicit
        # terminal_signal ingest. Managed-Codex continues to reset ended_at
        # to null unless a real terminal has landed.
        if managed_codex_ingest and not self._has_final_managed_codex_terminal(session):
            session.ended_at = None
        if incoming_ended_at:
            current_activity = _normalize_utc_naive(session.last_activity_at)
            if current_activity is None or incoming_ended_at > current_activity:
                session.last_activity_at = data.ended_at

        if data.project and not session.project:
            session.project = data.project
        if data.device_id and not session.device_id:
            session.device_id = data.device_id
        if not session.device_name:
            if data.device_name:
                session.device_name = data.device_name
            elif data.device_id:
                session.device_name = data.device_id.replace("shipper-", "")
        if data.cwd and not session.cwd:
            session.cwd = data.cwd
        if data.git_repo and not session.git_repo:
            session.git_repo = data.git_repo
        if data.git_branch and not session.git_branch:
            session.git_branch = data.git_branch

        incoming_environment = data.environment.strip()
        existing_environment = (session.environment or "").strip()
        if incoming_environment and (
            not existing_environment
            or (is_generic_environment_label(existing_environment) and not is_generic_environment_label(incoming_environment))
        ):
            session.environment = incoming_environment

        if not normalize_session_label(session.origin_label):
            session.origin_label = _infer_origin_label_from_ingest(data)

    def rebuild_fts(self) -> None:
        """Rebuild the FTS5 index when available (SQLite only)."""
        if not self._fts_available():
            return
        try:
            self.db.execute(text("INSERT INTO events_fts(events_fts) VALUES('rebuild')"))
        except Exception as exc:
            logger.warning("FTS5 rebuild failed: %s", exc)

    def _disable_fts_triggers(self) -> bool:
        """Drop FTS triggers to avoid per-row overhead during bulk ingest.

        Returns True if triggers were dropped (and must be re-created).
        """
        if not self._fts_available():
            return False
        try:
            self.db.execute(text("DROP TRIGGER IF EXISTS events_ai"))
            self.db.execute(text("DROP TRIGGER IF EXISTS events_ad"))
            self.db.execute(text("DROP TRIGGER IF EXISTS events_au"))
            return True
        except Exception:
            logger.warning("Failed to drop FTS triggers (non-fatal)", exc_info=True)
            return False

    def _reenable_fts_triggers(self) -> None:
        """Re-create FTS triggers after bulk ingest."""
        try:
            self.db.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                  INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES (new.id, new.content_text, new.tool_output_text, new.tool_name, new.role, new.session_id);
                END
            """)
            )
            self.db.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                  INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES(
                    'delete',
                    old.id,
                    old.content_text,
                    old.tool_output_text,
                    old.tool_name,
                    old.role,
                    old.session_id
                  );
                END
            """)
            )
            self.db.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
                  INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES(
                    'delete',
                    old.id,
                    old.content_text,
                    old.tool_output_text,
                    old.tool_name,
                    old.role,
                    old.session_id
                  );
                  INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES (new.id, new.content_text, new.tool_output_text, new.tool_name, new.role, new.session_id);
                END
            """)
            )
        except Exception:
            logger.exception("Failed to re-create FTS triggers")

    def _restore_fts_after_failed_bulk_ingest(self, session_id: UUID) -> None:
        bind = self.db.get_bind()
        engine = getattr(bind, "engine", bind)
        repair_db = Session(bind=engine)
        try:
            repair_store = AgentsStore(repair_db)
            repair_store._reenable_fts_triggers()
            repair_store._backfill_fts_for_session(session_id)
            repair_db.commit()
        finally:
            repair_db.close()

    def _backfill_fts_for_session(self, session_id) -> None:
        """Batch-insert FTS entries for all events in a session that are missing from FTS."""
        if not self._fts_available():
            return
        try:
            self.db.execute(
                text("""
                    INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                    SELECT e.id, e.content_text, e.tool_output_text, e.tool_name, e.role, e.session_id
                    FROM events e
                WHERE e.session_id = :sid
                  AND COALESCE(e.event_origin, 'durable') = 'durable'
                  AND e.id NOT IN (SELECT rowid FROM events_fts)
                """),
                {"sid": str(session_id)},
            )
        except Exception:
            logger.warning("FTS backfill for session %s failed (non-fatal)", session_id, exc_info=True)

    def _backfill_fts_for_event_ids(self, event_ids: list[int]) -> None:
        """Batch-insert FTS entries for newly inserted rows only."""
        if not self._fts_available() or not event_ids:
            return
        try:
            stmt = text("""
                INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                SELECT e.id, e.content_text, e.tool_output_text, e.tool_name, e.role, e.session_id
                FROM events e
                LEFT JOIN events_fts f ON f.rowid = e.id
                WHERE e.id IN :event_ids
                  AND COALESCE(e.event_origin, 'durable') = 'durable'
                  AND f.rowid IS NULL
            """).bindparams(bindparam("event_ids", expanding=True))
            for index in range(0, len(event_ids), 200):
                batch = event_ids[index : index + 200]
                self.db.execute(stmt, {"event_ids": batch})
        except Exception:
            logger.warning("FTS backfill for %s event ids failed (non-fatal)", len(event_ids), exc_info=True)

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
                      AND COALESCE(e.event_origin, 'durable') = 'durable'
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
                text(
                    """
                    SELECT DISTINCT e.session_id
                    FROM events_fts
                    JOIN events e ON e.id = events_fts.rowid
                    WHERE events_fts MATCH :query
                      AND COALESCE(e.event_origin, 'durable') = 'durable'
                    """
                ),
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
        payload: dict[str, Any] = {
            "role": event.role,
            "content_text": event.content_text,
            "tool_name": event.tool_name,
            "tool_input_json": event.tool_input_json,
            "tool_output_text": event.tool_output_text,
            "tool_call_id": event.tool_call_id,
        }
        if event.raw_json:
            # Stable replays of the same source line should not drift just because
            # the parser normalized timestamp tzinfo differently on a later ingest.
            payload["source_line_hash"] = self._compute_line_hash(event.raw_json)
        else:
            payload["timestamp"] = event.timestamp.isoformat()

        content = json.dumps(payload, sort_keys=True, default=str)
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
        branch_id: int | None,
        source_paths: set[str],
        *,
        source_offsets_by_path: dict[str, set[int]] | None = None,
        include_max_offsets: bool = True,
    ) -> tuple[dict[tuple[str, int], AgentSourceLine], dict[str, int]]:
        """Return latest line per (path, offset) and max offset per path for a branch."""
        latest: dict[tuple[str, int], AgentSourceLine] = {}
        max_offset_by_path: dict[str, int] = {}
        if not source_paths:
            return latest, max_offset_by_path

        query = (
            self.db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).filter(AgentSourceLine.branch_id == branch_id)
        )
        if source_offsets_by_path is None:
            rows = query.filter(AgentSourceLine.source_path.in_(sorted(source_paths))).all()
        else:
            clauses = []
            for source_path in sorted(source_paths):
                offsets = sorted(source_offsets_by_path.get(source_path, set()))
                if offsets:
                    clauses.append(
                        and_(
                            AgentSourceLine.source_path == source_path,
                            AgentSourceLine.source_offset.in_(offsets),
                        )
                    )
            if not clauses:
                return latest, max_offset_by_path
            rows = query.filter(or_(*clauses)).all()
            if include_max_offsets:
                max_rows = (
                    self.db.query(AgentSourceLine.source_path, func.max(AgentSourceLine.source_offset))
                    .filter(AgentSourceLine.session_id == session_id)
                    .filter(AgentSourceLine.branch_id == branch_id)
                    .filter(AgentSourceLine.source_path.in_(sorted(source_paths)))
                    .group_by(AgentSourceLine.source_path)
                    .all()
                )
                max_offset_by_path = {
                    str(source_path): int(max_offset)
                    for source_path, max_offset in max_rows
                    if source_path is not None and max_offset is not None
                }
        for row in rows:
            key = (row.source_path, int(row.source_offset))
            prev = latest.get(key)
            if prev is None or int(row.revision) > int(prev.revision):
                latest[key] = row
            if source_offsets_by_path is None and include_max_offsets:
                max_offset_by_path[row.source_path] = max(
                    max_offset_by_path.get(row.source_path, int(row.source_offset)),
                    int(row.source_offset),
                )
        return latest, max_offset_by_path

    def _source_offsets_by_path(self, source_lines: list[SourceLineIngest]) -> dict[str, set[int]]:
        offsets_by_path: dict[str, set[int]] = {}
        for line in source_lines:
            offsets_by_path.setdefault(line.source_path, set()).add(int(line.source_offset))
        return offsets_by_path

    def _detect_source_rewind_signal(
        self,
        session_id: UUID,
        head_branch_id: int,
        source_lines: list[SourceLineIngest],
    ) -> RewindSignal | None:
        """Detect whether incoming lines rewrite prior offsets.

        Whole-file truncation is handled only via explicit engine rewind hints.
        We intentionally do not infer truncation from "incoming max offset is
        lower than stored max offset" because replayed historical ranges are
        otherwise indistinguishable from a real rewrite.
        """
        if not source_lines:
            return None

        source_paths = {line.source_path for line in source_lines}
        latest_by_offset, _ = self._list_branch_source_lines(
            session_id,
            head_branch_id,
            source_paths,
            source_offsets_by_path=self._source_offsets_by_path(source_lines),
            include_max_offsets=False,
        )
        if not latest_by_offset:
            return None

        for line in source_lines:
            line_offset = int(line.source_offset)
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
        return None

    def _detect_explicit_rewind_signal(
        self,
        rewind_hints: list[SourceRewindHintIngest],
    ) -> RewindSignal | None:
        """Honor explicit engine rewind/truncation hints before heuristics."""
        candidate: RewindSignal | None = None
        for hint in rewind_hints:
            signal = RewindSignal(
                source_path=hint.source_path,
                source_offset=int(hint.source_offset),
                reason=hint.reason,
            )
            if candidate is None or (signal.source_offset, signal.source_path) < (
                candidate.source_offset,
                candidate.source_path,
            ):
                candidate = signal
        return candidate

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
        rewind_hints: list[SourceRewindHintIngest],
    ) -> RewindSignal | None:
        """Detect rewind from source rewrites/truncation or lineage divergence."""
        explicit_signal = self._detect_explicit_rewind_signal(rewind_hints)
        if explicit_signal is not None:
            return explicit_signal
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
        from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

        fallback_thread_id = ensure_thread_id_for_session(self.db, session_id)
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
                    thread_id=row.thread_id or fallback_thread_id,
                    source_path=row.source_path,
                    source_offset=row_offset,
                    branch_id=to_branch_id,
                    revision=1,
                    is_branch_copy=1,
                    raw_json=row.raw_json,
                    raw_json_z=row.raw_json_z,
                    raw_json_codec=row.raw_json_codec,
                    line_hash=row.line_hash,
                )
            )
        if source_copies:
            self.db.bulk_save_objects(source_copies)

        parent_events = (
            self.db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == from_branch_id)
            .filter(durable_transcript_event_predicate())
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
                    thread_id=event.thread_id or fallback_thread_id,
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
                    raw_json_z=event.raw_json_z,
                    raw_json_codec=event.raw_json_codec,
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
        rewind_hints: list[SourceRewindHintIngest],
    ) -> tuple[AgentSessionBranch, RewindSignal | None]:
        """Return branch to ingest into, forking when rewind is detected."""
        head = self._ensure_head_branch(session_id)
        signal = self._detect_rewind_signal(session_id, head.id, source_lines, events, rewind_hints)
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
            .filter(durable_transcript_event_predicate())
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
            .filter(durable_transcript_event_predicate())
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
            .filter(durable_transcript_event_predicate())
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

    def ingest_session(
        self,
        data: SessionIngest,
        *,
        chunk_size: int | None = None,
    ) -> IngestResult:
        """Ingest a session with events, handling deduplication.

        Creates or updates the session and inserts non-duplicate events.

        Args:
            data: Parsed session payload.
            chunk_size: Override the per-chunk commit interval. ``None`` uses
                the default (200). Larger values reduce fsync overhead for
                replay/scan paths; smaller values keep live ingest responsive.

        Returns:
            IngestResult with counts of inserted/skipped events plus commit
        telemetry (``commit_count`` / ``commit_ms_total``).
        """
        store_started = time.monotonic()
        store_stage_ms: dict[str, float] = {}

        def _record_stage(label: str, started: float) -> None:
            store_stage_ms[label] = round((time.monotonic() - started) * 1000, 3)

        stage_started = time.monotonic()
        session_id = data.id if data.id else uuid4()

        existing = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        session_created = False

        if existing:
            self._refresh_existing_session_metadata(existing, data)
            session_id = existing.id
        else:
            # Derive device_name from device_id if not explicitly provided
            device_name = data.device_name
            if not device_name and data.device_id:
                device_name = data.device_id.replace("shipper-", "")

            # Phase 4 of session-liveness-honesty: `data.ended_at` from
            # the engine is max(event.timestamp) — it is last-activity,
            # not a closure signal. Never seed session.ended_at from it.
            # Real terminal state comes from an explicit terminal_signal
            # ingest into SessionRuntimeState (or Phase 6 process-gone).
            session = AgentSession(
                id=session_id,
                provider=data.provider,
                environment=data.environment,
                project=data.project,
                device_id=data.device_id,
                device_name=device_name,
                cwd=data.cwd,
                git_repo=data.git_repo,
                git_branch=data.git_branch,
                started_at=data.started_at,
                ended_at=None,
                last_activity_at=(_normalize_utc_naive(data.ended_at) or _normalize_utc_naive(data.started_at)),
                loop_mode="assist",
            )
            self.db.add(session)
            self.db.flush()
            existing = session
            session_created = True

        # Phase 2: materialize the primary thread for this session and mirror
        # any provider_session_id evidence as a thread alias. Reducers below
        # use observation.thread_id to stamp child rows.
        primary_thread = ensure_primary_thread(self.db, existing)
        thread_id = primary_thread.id
        if data.provider_session_id:
            record_thread_alias(
                self.db,
                thread=primary_thread,
                provider=existing.provider,
                alias_kind="provider_session_id",
                alias_value=str(data.provider_session_id),
            )
        self.db.flush()
        _record_stage("session_setup", stage_started)

        stage_started = time.monotonic()
        source_lines = self._normalize_source_lines_for_ingest(data)
        ingest_branch, rewind_signal = self._resolve_ingest_branch(
            session_id,
            source_lines,
            data.events,
            data.rewind_hints,
        )
        _record_stage("source_branch_resolution", stage_started)

        events_inserted = 0
        events_skipped = 0
        leaf_uuid_hint: str | None = None
        latest_inserted_timestamp: datetime | None = None
        latest_inserted_event_id: int | None = None

        # Chunk commits every N events to release the SQLite write lock
        # periodically. A single 1000+ event transaction can hold the lock for
        # seconds, causing health-check timeouts and cascading failures.
        _INGEST_CHUNK = max(1, chunk_size) if chunk_size is not None else 200
        _FTS_TRIGGER_DISABLE_THRESHOLD = 100
        commit_count = 0
        commit_ms_total = 0.0

        def _commit_with_telemetry() -> None:
            nonlocal commit_count, commit_ms_total
            t0 = time.monotonic()
            self.db.commit()
            commit_ms_total += (time.monotonic() - t0) * 1000
            commit_count += 1

        # Disabling triggers only pays off for genuinely large batches.
        # Small transcript appends should keep trigger maintenance inline.
        fts_triggers_dropped = len(data.events) >= _FTS_TRIGGER_DISABLE_THRESHOLD and self._disable_fts_triggers()
        _since_commit = 0
        inserted_event_ids: list[int] = []
        needs_session_wide_fts_backfill = False
        provider_events_received_at = datetime.now(timezone.utc)
        direct_event_projection = not fts_triggers_dropped

        stage_started = time.monotonic()
        try:
            for event_data in data.events:
                event_hash = self._compute_event_hash(event_data)
                event_uuid, parent_event_uuid = self._extract_event_lineage(event_data.raw_json)
                event_leaf_uuid = self._extract_leaf_uuid(event_data.raw_json)
                if event_leaf_uuid:
                    leaf_uuid_hint = event_leaf_uuid

                observation_result = record_provider_event_observation(
                    self.db,
                    session_id=session_id,
                    thread_id=thread_id,
                    provider=data.provider,
                    device_id=data.device_id,
                    source="agents_ingest",
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
                    event_uuid=event_uuid,
                    parent_event_uuid=parent_event_uuid,
                    received_at=provider_events_received_at,
                    load_observation=not direct_event_projection,
                )
                if direct_event_projection:
                    raw_json_z = compress_raw_json(event_data.raw_json) if event_data.raw_json is not None else None
                    event_stmt = (
                        sqlite_insert(AgentEvent)
                        .values(
                            session_id=session_id,
                            thread_id=thread_id,
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
                            raw_json=None,
                            raw_json_z=raw_json_z,
                            raw_json_codec=CODEC_ZSTD if raw_json_z else CODEC_PLAIN,
                            schema_version=1,
                            event_uuid=event_uuid,
                            parent_event_uuid=parent_event_uuid,
                            event_origin="durable",
                        )
                        .on_conflict_do_nothing()
                    )
                    insert_result = self.db.execute(event_stmt) if observation_result.inserted else None
                    event_inserted = bool(insert_result is not None and insert_result.rowcount and insert_result.rowcount > 0)
                    reduction = ProviderEventReduction(event=None, inserted=event_inserted)
                elif observation_result.observation is not None:
                    reduction = reduce_provider_event_observation(self.db, observation_result.observation)
                else:
                    reduction = None
                if reduction is not None and reduction.inserted:
                    events_inserted += 1
                    if reduction.event is not None and isinstance(reduction.event.id, int):
                        latest_inserted_event_id = reduction.event.id
                    if fts_triggers_dropped:
                        has_inserted_event_id = (
                            reduction.event is not None and isinstance(reduction.event.id, int) and reduction.event.id > 0
                        )
                        if has_inserted_event_id:
                            inserted_event_ids.append(reduction.event.id)
                        else:
                            needs_session_wide_fts_backfill = True
                    normalized_timestamp = _normalize_utc_naive(event_data.timestamp)
                    if normalized_timestamp is not None and (
                        latest_inserted_timestamp is None or normalized_timestamp > latest_inserted_timestamp
                    ):
                        latest_inserted_timestamp = normalized_timestamp
                    _since_commit += 1
                else:
                    events_skipped += 1

                # Release write lock between chunks so health checks and other
                # readers aren't starved during large ingests.
                if _since_commit >= _INGEST_CHUNK:
                    _commit_with_telemetry()
                    _since_commit = 0
        except Exception:
            if fts_triggers_dropped:
                self.db.rollback()
                try:
                    self._restore_fts_after_failed_bulk_ingest(session_id)
                except Exception:
                    logger.exception("Failed to restore FTS triggers after ingest error for session %s", session_id)
            raise
        if events_inserted > 0 and latest_inserted_event_id is None:
            latest_inserted_event_id = self.get_latest_event_id(session_id)
        _record_stage("provider_event_observations", stage_started)

        if fts_triggers_dropped:
            stage_started = time.monotonic()
            self._reenable_fts_triggers()
            if events_inserted > 0:
                if not needs_session_wide_fts_backfill and inserted_event_ids:
                    self._backfill_fts_for_event_ids(inserted_event_ids)
                else:
                    self._backfill_fts_for_session(session_id)
            _record_stage("fts_maintenance", stage_started)

        stage_started = time.monotonic()
        source_paths = {line.source_path for line in source_lines}
        latest_line_by_offset, _ = self._list_branch_source_lines(
            session_id,
            ingest_branch.id,
            source_paths,
            source_offsets_by_path=self._source_offsets_by_path(source_lines),
            include_max_offsets=False,
        )
        latest_state: dict[tuple[str, int], tuple[int, str]] = {
            key: (int(row.revision), row.line_hash) for key, row in latest_line_by_offset.items()
        }
        _record_stage("source_line_lookup", stage_started)

        source_lines_inserted = 0
        source_lines_received_at = datetime.now(timezone.utc)
        _since_commit = 0
        stage_started = time.monotonic()
        for line_data in source_lines:
            line_hash = self._compute_line_hash(line_data.raw_json)
            source_offset = int(line_data.source_offset)
            key = (line_data.source_path, source_offset)
            prev_revision, prev_hash = latest_state.get(key, (0, ""))
            if prev_hash == line_hash:
                continue

            revision = prev_revision + 1
            observation_result = record_source_line_observation(
                self.db,
                session_id=session_id,
                thread_id=thread_id,
                provider=data.provider,
                device_id=data.device_id,
                source="agents_ingest",
                source_path=line_data.source_path,
                source_offset=source_offset,
                branch_id=ingest_branch.id,
                revision=revision,
                line_hash=line_hash,
                raw_json=line_data.raw_json,
                observed_at=source_lines_received_at,
                received_at=source_lines_received_at,
                load_observation=False,
            )
            row_inserted = False
            if observation_result.inserted:
                source_line_stmt = (
                    sqlite_insert(AgentSourceLine)
                    .values(
                        session_id=session_id,
                        thread_id=thread_id,
                        source_path=line_data.source_path,
                        source_offset=source_offset,
                        branch_id=ingest_branch.id,
                        revision=revision,
                        is_branch_copy=0,
                        raw_json="",
                        raw_json_z=compress_raw_json(line_data.raw_json),
                        raw_json_codec=CODEC_ZSTD,
                        line_hash=line_hash,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "session_id",
                            "branch_id",
                            "source_path",
                            "source_offset",
                            "line_hash",
                        ],
                    )
                )
                insert_result = self.db.execute(source_line_stmt)
                row_inserted = bool(insert_result.rowcount and insert_result.rowcount > 0)
            if row_inserted:
                latest_state[key] = (revision, line_hash)
                source_lines_inserted += 1
                _since_commit += 1

            if _since_commit >= _INGEST_CHUNK:
                _commit_with_telemetry()
                _since_commit = 0
        _record_stage("source_line_observations", stage_started)

        stage_started = time.monotonic()
        head_branch_for_counts = self._align_head_branch_from_leaf_uuid(session_id, ingest_branch.id, leaf_uuid_hint)
        self._sync_session_counts_to_head(session_id, head_branch_for_counts)

        transcript_changed = bool(source_lines_inserted) or not source_lines or rewind_signal is not None
        if events_inserted > 0 and not transcript_changed:
            logger.warning(
                "Ingest inserted %s event rows for session %s without a source-line delta; suppressing post-ingest tasks",
                events_inserted,
                session_id,
            )

        session_obj = self.db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session_obj and events_inserted > 0 and transcript_changed:
            session_obj.transcript_revision = int(getattr(session_obj, "transcript_revision", 0) or 0) + 1
            session_obj.needs_embedding = 1
            if latest_inserted_timestamp is not None:
                current = _normalize_utc_naive(session_obj.last_activity_at)
                if current is None or latest_inserted_timestamp > current:
                    session_obj.last_activity_at = latest_inserted_timestamp
        _record_stage("session_projection", stage_started)

        stage_started = time.monotonic()
        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_runtime_events
        from zerg.services.session_runtime import runtime_key_for_session

        runtime_key = runtime_key_for_session(data.provider, str(session_id))
        runtime_events = [
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                provider=data.provider,
                device_id=data.device_id,
                source="agents_ingest",
                kind="binding_signal",
                occurred_at=_normalize_utc_naive(data.started_at) or datetime.now(timezone.utc).replace(tzinfo=None),
                dedupe_key=f"binding:{runtime_key}:{session_id}",
                payload={},
            )
        ]
        if events_inserted > 0 and transcript_changed and latest_inserted_timestamp is not None:
            latest_event_id = self.get_latest_event_id(session_id)
            runtime_events.append(
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider=data.provider,
                    device_id=data.device_id,
                    source="agents_ingest",
                    kind="progress_signal",
                    occurred_at=latest_inserted_timestamp,
                    dedupe_key=f"progress:{session_id}:{latest_event_id or latest_inserted_timestamp.isoformat()}",
                    payload={"progress_kind": "transcript_append"},
                )
            )
        ingest_runtime_events(self.db, runtime_events)
        _record_stage("runtime_events", stage_started)

        stage_started = time.monotonic()
        _commit_with_telemetry()
        _record_stage("commit_after_runtime", stage_started)

        if events_inserted > 0 and transcript_changed:
            from zerg.services.session_turns import materialize_managed_transcript_turns
            from zerg.services.session_turns import maybe_mark_session_turn_durable

            stage_started = time.monotonic()
            maybe_mark_session_turn_durable(self.db, session_id=session_id)
            materialize_managed_transcript_turns(self.db, session_id=session_id, incremental=True)
            _record_stage("turn_materialization", stage_started)
            stage_started = time.monotonic()
            _commit_with_telemetry()
            _record_stage("commit_after_turns", stage_started)

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
            latest_inserted_event_id=latest_inserted_event_id,
            session_created=session_created,
            commit_count=commit_count,
            commit_ms_total=round(commit_ms_total, 3),
            source_lines_inserted=source_lines_inserted,
            store_stage_ms={
                **store_stage_ms,
                "total": round((time.monotonic() - store_started) * 1000, 3),
            },
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
        anchor_on_activity: bool = False,
    ) -> tuple[List[AgentSession], int]:
        """List sessions with optional filters.

        Args:
            environment: Filter to specific environment (production, development, test, e2e)
            include_test: If False (default), excludes test/e2e sessions unless environment is set

        Returns:
            Tuple of (sessions, total_count)
        """
        stmt = select(AgentSession)
        activity_anchor = AgentSession.started_at
        if anchor_on_activity:
            runtime_signal_subq = self._runtime_signal_subquery()
            stmt = stmt.outerjoin(runtime_signal_subq, runtime_signal_subq.c.session_id == AgentSession.id)
            activity_anchor = self._recent_activity_anchor_expr(
                AgentSession.last_activity_at,
                runtime_signal_subq.c.runtime_timeline_anchor_at,
            )
        stmt = self._apply_session_listing_filters(
            stmt,
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            until=until,
            query=query,
            exclude_user_states=exclude_user_states,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            branch_mode=branch_mode,
            time_anchor=activity_anchor if anchor_on_activity else AgentSession.started_at,
        )
        if stmt is None:
            return [], 0

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar() or 0

        # Apply ordering and pagination
        if anchor_on_activity:
            stmt = stmt.order_by(activity_anchor.desc(), AgentSession.started_at.desc()).limit(limit).offset(offset)
        else:
            stmt = stmt.order_by(AgentSession.started_at.desc()).limit(limit).offset(offset)

        sessions = list(self.db.execute(stmt).scalars().all())
        return sessions, total

    def list_session_window_signature(
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
        include_total: bool = False,
    ) -> tuple[
        int | None,
        tuple[tuple[str, datetime | None, datetime | None, datetime | None, int, datetime | None], ...],
    ]:
        """Return a lightweight recency window signature for timeline SSE preflight."""

        runtime_signal_subq = self._runtime_signal_subquery()
        activity_anchor = self._recent_activity_anchor_expr(
            AgentSession.last_activity_at,
            runtime_signal_subq.c.runtime_timeline_anchor_at,
        )

        stmt = (
            select(
                AgentSession.id.label("session_id"),
                AgentSession.updated_at.label("session_updated_at"),
                AgentSession.last_activity_at.label("last_activity_at"),
                runtime_signal_subq.c.runtime_updated_at.label("runtime_updated_at"),
                runtime_signal_subq.c.runtime_version.label("runtime_version"),
                runtime_signal_subq.c.runtime_timeline_anchor_at.label("runtime_timeline_anchor_at"),
            )
            .select_from(AgentSession)
            .outerjoin(runtime_signal_subq, runtime_signal_subq.c.session_id == AgentSession.id)
        )

        stmt = self._apply_session_listing_filters(
            stmt,
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            until=until,
            query=query,
            exclude_user_states=exclude_user_states,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            branch_mode=branch_mode,
            time_anchor=activity_anchor,
        )
        if stmt is None:
            return (0 if include_total else None), ()

        total: int | None = None
        if include_total:
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = self.db.execute(count_stmt).scalar() or 0

        stmt = stmt.order_by(activity_anchor.desc(), AgentSession.started_at.desc()).limit(limit).offset(offset)
        rows = self.db.execute(stmt).all()
        signature_rows = tuple(
            (
                str(row.session_id),
                row.session_updated_at,
                row.last_activity_at,
                row.runtime_updated_at,
                int(row.runtime_version or 0),
                row.runtime_timeline_anchor_at,
            )
            for row in rows
        )
        return total, signature_rows

    def _timeline_thread_ranked_subquery(
        self,
        *,
        project: Optional[str],
        provider: Optional[str],
        environment: Optional[str],
        include_test: bool,
        device_id: Optional[str],
        since: Optional[datetime],
        until: Optional[datetime],
        query: Optional[str],
        exclude_user_states: Optional[list[str]],
        hide_autonomous: bool,
        context_mode: str,
        branch_mode: str,
    ):
        runtime_signal_subq = self._runtime_signal_subquery()
        activity_anchor = self._recent_activity_anchor_expr(
            AgentSession.last_activity_at,
            runtime_signal_subq.c.runtime_timeline_anchor_at,
        )
        # Session-identity-kernel cleanup: legacy ingest paths still create
        # AgentSession rows before the kernel thread is materialized, so
        # ``primary_thread_id`` is often NULL.  Treat each session as its
        # own thread when the explicit pointer is missing.
        thread_id = func.coalesce(AgentSession.primary_thread_id, AgentSession.id).label("thread_id")

        stmt = (
            select(
                AgentSession.id.label("session_id"),
                thread_id,
                AgentSession.started_at.label("started_at"),
                AgentSession.updated_at.label("session_updated_at"),
                activity_anchor.label("thread_anchor"),
                AgentSession.last_activity_at.label("last_activity_at"),
                runtime_signal_subq.c.runtime_updated_at.label("runtime_updated_at"),
                runtime_signal_subq.c.runtime_version.label("runtime_version"),
            )
            .select_from(AgentSession)
            .outerjoin(runtime_signal_subq, runtime_signal_subq.c.session_id == AgentSession.id)
        )

        stmt = self._apply_session_listing_filters(
            stmt,
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            until=until,
            query=query,
            exclude_user_states=exclude_user_states,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            branch_mode=branch_mode,
            time_anchor=activity_anchor,
        )
        if stmt is None:
            return None

        base_subq = stmt.subquery()
        row_number = (
            func.row_number()
            .over(
                partition_by=base_subq.c.thread_id,
                order_by=(
                    base_subq.c.thread_anchor.desc(),
                    base_subq.c.started_at.desc(),
                    base_subq.c.session_id.desc(),
                ),
            )
            .label("rn")
        )
        return select(
            base_subq.c.thread_id,
            base_subq.c.session_id,
            base_subq.c.started_at,
            base_subq.c.session_updated_at,
            base_subq.c.thread_anchor,
            base_subq.c.last_activity_at,
            base_subq.c.runtime_updated_at,
            base_subq.c.runtime_version,
            row_number,
        ).subquery()

    def list_timeline_thread_page(
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
    ) -> tuple[int, tuple[tuple[str, str, datetime | None], ...]]:
        ranked_subq = self._timeline_thread_ranked_subquery(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            until=until,
            query=query,
            exclude_user_states=exclude_user_states,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            branch_mode=branch_mode,
        )
        if ranked_subq is None:
            return 0, ()

        total = (
            self.db.execute(
                select(func.count()).select_from(select(ranked_subq.c.thread_id).where(ranked_subq.c.rn == 1).subquery())
            ).scalar()
            or 0
        )
        rows = tuple(
            self.db.execute(
                select(
                    ranked_subq.c.thread_id,
                    ranked_subq.c.session_id,
                    ranked_subq.c.thread_anchor,
                )
                .where(ranked_subq.c.rn == 1)
                .order_by(ranked_subq.c.thread_anchor.desc(), ranked_subq.c.session_id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return total, tuple((str(row.thread_id), str(row.session_id), row.thread_anchor) for row in rows)

    def list_timeline_thread_window_signature(
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
        include_total: bool = False,
    ) -> tuple[
        int | None,
        tuple[tuple[str, str, datetime | None, datetime | None, datetime | None, int], ...],
    ]:
        ranked_subq = self._timeline_thread_ranked_subquery(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            until=until,
            query=query,
            exclude_user_states=exclude_user_states,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            branch_mode=branch_mode,
        )
        if ranked_subq is None:
            return (0 if include_total else None), ()

        total: int | None = None
        if include_total:
            total = (
                self.db.execute(
                    select(func.count()).select_from(select(ranked_subq.c.thread_id).where(ranked_subq.c.rn == 1).subquery())
                ).scalar()
                or 0
            )

        rows = tuple(
            self.db.execute(
                select(
                    ranked_subq.c.thread_id,
                    ranked_subq.c.session_id,
                    ranked_subq.c.thread_anchor,
                    ranked_subq.c.session_updated_at,
                    ranked_subq.c.last_activity_at,
                    ranked_subq.c.runtime_version,
                )
                .where(ranked_subq.c.rn == 1)
                .order_by(ranked_subq.c.thread_anchor.desc(), ranked_subq.c.session_id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return total, tuple(
            (
                str(row.thread_id),
                str(row.session_id),
                row.thread_anchor,
                row.session_updated_at,
                row.last_activity_at,
                int(row.runtime_version or 0),
            )
            for row in rows
        )

    @staticmethod
    def _latest_non_null_expr(lhs, rhs):
        return case(
            (rhs.is_(None), lhs),
            (lhs.is_(None), rhs),
            (rhs >= lhs, rhs),
            else_=lhs,
        )

    @classmethod
    def _recent_activity_anchor_expr(cls, last_activity_expr, runtime_anchor_expr=None):
        latest_signal = last_activity_expr
        if runtime_anchor_expr is not None:
            latest_signal = cls._latest_non_null_expr(last_activity_expr, runtime_anchor_expr)
        return func.coalesce(latest_signal, AgentSession.started_at)

    @staticmethod
    def _runtime_signal_subquery():
        return (
            select(
                SessionRuntimeState.session_id.label("session_id"),
                func.max(SessionRuntimeState.updated_at).label("runtime_updated_at"),
                func.max(SessionRuntimeState.runtime_version).label("runtime_version"),
                func.max(SessionRuntimeState.timeline_anchor_at).label("runtime_timeline_anchor_at"),
            )
            .where(SessionRuntimeState.session_id.is_not(None))
            .group_by(SessionRuntimeState.session_id)
            .subquery()
        )

    def _apply_session_listing_filters(
        self,
        stmt,
        *,
        project: Optional[str],
        provider: Optional[str],
        environment: Optional[str],
        include_test: bool,
        device_id: Optional[str],
        since: Optional[datetime],
        until: Optional[datetime],
        query: Optional[str],
        exclude_user_states: Optional[list[str]],
        hide_autonomous: bool,
        context_mode: str,
        branch_mode: str,
        time_anchor,
    ):
        if environment:
            stmt = stmt.where(AgentSession.environment == environment)
        elif not include_test:
            stmt = stmt.where(AgentSession.environment.notin_(["test", "e2e"]))

        if project:
            stmt = stmt.where(AgentSession.project.ilike(f"%{project}%"))
        if provider:
            stmt = stmt.where(AgentSession.provider == provider)
        if not is_internal_canary_provider_filter(provider):
            stmt = stmt.where(~internal_canary_session_clause(AgentSession))
        if device_id:
            # The browser now writes `device_id=` for machine filters, but the
            # timeline filter API still returns legacy machine labels sourced
            # from `environment`. Accept either shape while old links and
            # imported sessions are still in circulation.
            stmt = stmt.where(or_(AgentSession.device_id == device_id, AgentSession.environment == device_id))
        if since:
            stmt = stmt.where(time_anchor >= since)
        if until:
            stmt = stmt.where(time_anchor <= until)

        if hide_autonomous:
            # Session-identity-kernel cleanup: ``execution_home`` and
            # ``is_sidechain`` were dropped from ``AgentSession``. Approximate
            # the previous filter with the surviving signals: keep sessions
            # that have user messages or are still open.
            stmt = stmt.where(or_(AgentSession.user_messages > 0, AgentSession.ended_at.is_(None)))

        if exclude_user_states:
            stmt = stmt.where((AgentSession.user_state.notin_(exclude_user_states)) | (AgentSession.user_state.is_(None)))

        if query:
            session_ids = self._fts_session_ids(query, context_mode=context_mode, branch_mode=branch_mode)
            if session_ids is not None:
                if not session_ids:
                    return None
                stmt = stmt.where(AgentSession.id.in_(session_ids))

        return stmt

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
            .where(durable_transcript_event_predicate())
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
        """Return last activity timestamp per session (denormalized column read)."""
        if not session_ids:
            return {}
        rows = (
            self.db.query(AgentSession.id, AgentSession.last_activity_at)
            .filter(AgentSession.id.in_(session_ids))
            .filter(AgentSession.last_activity_at.isnot(None))
            .all()
        )
        return {session_id: ts for session_id, ts in rows}

    def get_last_timestamp_by_role_map(self, session_ids: List[UUID], role: str) -> dict[UUID, datetime]:
        """Return the timestamp of the last event with the given role, per session."""
        if not session_ids:
            return {}
        from sqlalchemy import func as sa_func

        rows = (
            self.db.query(AgentEvent.session_id, sa_func.max(AgentEvent.timestamp))
            .filter(AgentEvent.session_id.in_(session_ids))
            .filter(durable_transcript_event_predicate())
            .filter(AgentEvent.role == role)
            .group_by(AgentEvent.session_id)
            .all()
        )
        return {session_id: ts for session_id, ts in rows}

    def get_last_tool_call_map(self, session_ids: List[UUID]) -> dict[UUID, datetime]:
        """Return the timestamp of the last tool-use event per session."""
        if not session_ids:
            return {}
        from sqlalchemy import func as sa_func

        rows = (
            self.db.query(AgentEvent.session_id, sa_func.max(AgentEvent.timestamp))
            .filter(AgentEvent.session_id.in_(session_ids))
            .filter(durable_transcript_event_predicate())
            .filter(AgentEvent.role == "assistant")
            .filter(AgentEvent.tool_name.isnot(None))
            .group_by(AgentEvent.session_id)
            .all()
        )
        return {session_id: ts for session_id, ts in rows}

    def get_session_preview(self, session_id: UUID, last_n: int) -> List[AgentEvent]:
        """Return last N user/assistant messages for preview (chronological)."""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(AgentEvent.role.in_(["user", "assistant"]))
            .where(AgentEvent.content_text.isnot(None))
            .where(visible_transcript_event_predicate())
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
                    """
                    SELECT e.id
                    FROM events_fts
                    JOIN events e ON e.id = events_fts.rowid
                    WHERE events_fts MATCH :q
                      AND e.session_id = :sid
                      AND COALESCE(e.event_origin, 'durable') = 'durable'
                    """
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
            .where(or_(AgentEvent.raw_json.isnot(None), AgentEvent.raw_json_z.isnot(None)))
            .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        )
        stmt = self._apply_branch_mode_filter(stmt, session_id, branch_mode)
        rows = list(self.db.execute(stmt).scalars().all())
        for event in rows:
            if not self._is_compaction_boundary_raw_json(decode_raw_json(event)):
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
        load_from_end: bool = False,
    ) -> List[AgentEvent]:
        """Get events for a session with optional filtering."""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(visible_transcript_event_predicate())
            .order_by(AgentEvent.timestamp, AgentEvent.id)
        )
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

        if load_from_end:
            total_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
            total = int(self.db.execute(total_stmt).scalar_one() or 0)
            offset = max(0, total - limit - offset)

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
        stmt = (
            select(func.count())
            .select_from(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(visible_transcript_event_predicate())
        )
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
                lines = [decode_raw_json(row) for row in source_lines]
                lines = [line for line in lines if line is not None]
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
                lines = [decode_raw_json(row) for row in normalized_source_lines if row.source_path == primary_path]
                lines = [line for line in lines if line is not None]
            content = "\n".join(lines) + "\n" if lines else ""
            return content.encode("utf-8"), session

        # Legacy fallback path: rebuild from events only.
        events_stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .where(durable_transcript_event_predicate())
            .order_by(AgentEvent.source_offset.asc(), AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .limit(10000)
        )
        events_stmt = self._apply_branch_mode_filter(events_stmt, session_id, branch_mode)
        events = list(self.db.execute(events_stmt).scalars().all())

        # Check if we have raw_json available (lossless path)
        has_raw_json = any(decode_raw_json(event) is not None for event in events)

        lines = []
        if has_raw_json:
            # Lossless path: dedupe by (source_path, source_offset)
            # Multiple events from the same JSONL line share the same offset
            seen_offsets: set[tuple[str | None, int | None]] = set()
            for event in events:
                _raw = decode_raw_json(event)
                if _raw is not None:
                    key = (event.source_path, event.source_offset)
                    if key not in seen_offsets:
                        seen_offsets.add(key)
                        lines.append(_raw)
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
