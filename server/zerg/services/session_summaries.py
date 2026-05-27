"""Background session summarization and embedding pipeline.

Extracted from the agents router — these are background async tasks, not HTTP
handlers. Summary enrichment is driven by session revision-lag reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.provisional_events import durable_transcript_event_predicate

logger = logging.getLogger(__name__)

# Semaphore gates concurrent background embedding calls during bulk ingest.
_embedding_semaphore = asyncio.Semaphore(5)
_PLACEHOLDER_TITLE = "Untitled Session"
_PLACEHOLDER_SUMMARY = "No summary generated."
SUMMARY_EVENT_LOAD_LIMIT = int(os.getenv("SESSION_SUMMARY_EVENT_LOAD_LIMIT", "200"))
SUMMARY_EVENT_TEXT_MAX_CHARS = int(os.getenv("SESSION_SUMMARY_EVENT_TEXT_MAX_CHARS", "4000"))


@dataclass(frozen=True)
class _SummaryEventChunk:
    events: list[dict]
    last_event_id: int | None
    has_more: bool


def _summary_content_values(summary: Any) -> dict[str, str]:
    """Return only generated summary fields that are worth persisting."""
    values: dict[str, str] = {}
    title = str(getattr(summary, "title", "") or "").strip()
    body = str(getattr(summary, "summary", "") or "").strip()
    if title and title != _PLACEHOLDER_TITLE:
        values["summary_title"] = title
    if body and body != _PLACEHOLDER_SUMMARY:
        values["summary"] = body
    return values


def events_to_dicts(events: list[AgentEvent]) -> list[dict]:
    """Convert ORM AgentEvent rows to plain dicts for summarization."""
    return [
        {
            "role": event.role,
            "content_text": event.content_text,
            "tool_name": event.tool_name,
            "tool_input_json": event.tool_input_json,
            "tool_output_text": event.tool_output_text,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        for event in events
    ]


def _load_summary_event_chunk(
    db: Session,
    *,
    session_id: str,
    cursor_id: int | None,
    limit: int | None = None,
) -> _SummaryEventChunk:
    """Load a bounded user/assistant chunk for incremental summary updates."""
    limit = SUMMARY_EVENT_LOAD_LIMIT if limit is None else limit
    limit = max(1, int(limit or 1))
    text_chars = max(1, int(SUMMARY_EVENT_TEXT_MAX_CHARS or 1))
    text_expr = func.substr(AgentEvent.content_text, 1, text_chars).label("content_text")
    base_query = (
        db.query(
            AgentEvent.id,
            AgentEvent.role,
            text_expr,
            AgentEvent.timestamp,
        )
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role.in_(("user", "assistant")))
        .filter(AgentEvent.content_text.isnot(None))
        .filter(durable_transcript_event_predicate())
    )

    if cursor_id is None:
        # Legacy sessions may have no summary cursor. Summarize the recent tail
        # instead of pulling the full historical transcript into the API process.
        rows = base_query.order_by(AgentEvent.id.desc()).limit(limit + 1).all()
        if len(rows) > limit:
            logger.info(
                "Summary bootstrap for session %s is using last %d messages; older history is intentionally skipped",
                session_id,
                limit,
            )
        rows = list(reversed(rows[:limit]))
        has_more = False
    else:
        rows = base_query.filter(AgentEvent.id > cursor_id).order_by(AgentEvent.id).limit(limit + 1).all()
        has_more = len(rows) > limit
        rows = rows[:limit]

    events = [
        {
            "role": row.role,
            "content_text": row.content_text,
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        }
        for row in rows
    ]
    return _SummaryEventChunk(
        events=events,
        last_event_id=int(rows[-1].id) if rows else None,
        has_more=has_more,
    )


async def _advance_session_revision(
    *,
    db: Session,
    session_id: str,
    column_name: str,
    target_revision: int,
    label: str,
) -> int:
    """Mark summary/embed progress current without doing external work."""
    from sqlalchemy import update as sa_update

    from zerg.services.write_serializer import get_write_serializer

    if target_revision <= 0:
        return 0

    column = getattr(AgentSession, column_name)

    def _do_update(write_db: Session) -> int:
        result = write_db.execute(
            sa_update(AgentSession)
            .where(AgentSession.id == session_id)
            .where(column < target_revision)
            .values(**{column_name: target_revision})
        )
        return int(result.rowcount or 0)

    ws = get_write_serializer()
    return await ws.execute_or_direct(_do_update, db, label=label)


async def summarize_and_persist(
    session: AgentSession,
    events: list[AgentEvent],
    db: Session,
    client: Any,
    model: str,
) -> Any:
    """Summarize session events via LLM and persist to DB.

    Converts events to dicts, calls summarize_events(), writes summary
    fields on the session, and commits. Does NOT manage db session
    lifecycle -- caller is responsible for open/close/rollback.

    Returns the SessionSummary or None if the transcript was empty.
    """
    from sqlalchemy import update as sa_update

    from zerg.services.session_processing import summarize_events
    from zerg.services.write_serializer import get_write_serializer

    event_dicts = events_to_dicts(events)

    summary = await summarize_events(
        event_dicts,
        client=client,
        model=model,
        metadata={
            "project": session.project,
            "provider": session.provider,
            "git_branch": session.git_branch,
        },
    )

    if not summary:
        return None

    new_last_event_id = events[-1].id if events else None
    target_revision = int(getattr(session, "transcript_revision", 0) or 0)

    content_values = _summary_content_values(summary)
    if not content_values:
        logger.warning("Discarding placeholder summary result for session %s", session.id)

    def _do_persist(write_db: Session) -> int:
        result = write_db.execute(
            sa_update(AgentSession)
            .where(AgentSession.id == session.id)
            .values(
                summary_event_count=len(events),
                last_summarized_event_id=new_last_event_id,
                summary_revision=target_revision,
                **content_values,
            )
        )
        return int(result.rowcount or 0)

    ws = get_write_serializer()
    updated = await ws.execute_or_direct(_do_persist, db, label="summary-backfill")
    if updated > 0:
        if "summary" in content_values:
            session.summary = content_values["summary"]
        if "summary_title" in content_values:
            session.summary_title = content_values["summary_title"]
        session.summary_event_count = len(events)
        session.last_summarized_event_id = new_last_event_id
        session.summary_revision = target_revision
    return summary


async def set_structured_title_if_empty(session_id: str) -> None:
    """Set a structured fallback title from project/branch when no LLM title exists."""
    from sqlalchemy import update as sa_update

    from zerg.database import get_session_factory
    from zerg.services.write_serializer import get_write_serializer

    factory = get_session_factory()
    db = factory()
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if not session or session.summary_title:
            return
        parts = [p for p in [session.project, session.git_branch] if p]
        if parts:
            title = " · ".join(parts)
        else:
            date_str = (session.started_at or datetime.now(timezone.utc)).strftime("%b %-d")
            title = f"Session · {date_str}"

        def _do_update(write_db: Session) -> int:
            result = write_db.execute(
                sa_update(AgentSession)
                .where(AgentSession.id == session_id)
                .where(AgentSession.summary_title.is_(None))
                .values(summary_title=title)
            )
            return int(result.rowcount or 0)

        ws = get_write_serializer()
        updated = await ws.execute_or_direct(_do_update, db, label="summary-title")
        if updated == 0:
            logger.debug("Structured title skipped for session %s (title set concurrently)", session_id)
            return
        logger.debug("Set structured title %r for session %s", title, session_id)
    except Exception:
        logger.exception("Failed to set structured title for session %s", session_id)
        db.rollback()
    finally:
        db.close()


async def generate_summary_impl(session_id: str) -> None:
    from sqlalchemy import update

    from zerg.database import get_session_factory
    from zerg.services.session_processing import incremental_summary
    from zerg.services.write_serializer import get_write_serializer

    settings = get_settings()

    if settings.testing:
        logger.debug("Testing mode, skipping summary for %s", session_id)
        return

    session_factory = get_session_factory()
    db: Session | None = session_factory()
    ws = get_write_serializer()
    client = None
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if not session:
            logger.warning("Session %s not found for summary generation", session_id)
            return

        transcript_revision = int(getattr(session, "transcript_revision", 0) or 0)
        summary_revision = int(getattr(session, "summary_revision", 0) or 0)
        if transcript_revision > 0 and summary_revision >= transcript_revision:
            logger.debug(
                "Summary already current for session %s (summary_revision=%s transcript_revision=%s)",
                session_id,
                summary_revision,
                transcript_revision,
            )
            return

        if settings.llm_disabled:
            logger.debug("LLM disabled, marking summary current for %s", session_id)
            await set_structured_title_if_empty(session_id)
            await _advance_session_revision(
                db=db,
                session_id=session_id,
                column_name="summary_revision",
                target_revision=transcript_revision,
                label="summary-revision",
            )
            return

        cursor_id = session.last_summarized_event_id
        expected_summary_event_count = session.summary_event_count or 0
        new_chunk = _load_summary_event_chunk(db, session_id=session_id, cursor_id=cursor_id)

        if not new_chunk.events:
            await _advance_session_revision(
                db=db,
                session_id=session_id,
                column_name="summary_revision",
                target_revision=transcript_revision,
                label="summary-revision",
            )
            logger.debug("No new events for session %s, skipping summary", session_id)
            return

        new_event_dicts = new_chunk.events
        meaningful_roles = {"user", "assistant"}
        meaningful_count = sum(1 for e in new_event_dicts if e["role"] in meaningful_roles and e.get("content_text"))
        if meaningful_count < 2:
            logger.debug("Only %d new messages for session %s, waiting for more", meaningful_count, session_id)
            await set_structured_title_if_empty(session_id)
            await _advance_session_revision(
                db=db,
                session_id=session_id,
                column_name="summary_revision",
                target_revision=transcript_revision,
                label="summary-revision",
            )
            return

        new_last_event_id = new_chunk.last_event_id
        current_summary = session.summary
        current_title = session.summary_title
        metadata = {
            "project": session.project,
            "provider": session.provider,
            "git_branch": session.git_branch,
        }

        from zerg.models_config import get_llm_client_for_use_case

        try:
            client, model, _provider = get_llm_client_for_use_case("summary_update")
        except ValueError:
            try:
                client, model, _provider = get_llm_client_for_use_case("summarization")
            except ValueError as e:
                logger.warning(
                    "Summarization misconfigured -- session %s will NOT be summarized: %s",
                    session_id,
                    e,
                )
                await set_structured_title_if_empty(session_id)
                await _advance_session_revision(
                    db=db,
                    session_id=session_id,
                    column_name="summary_revision",
                    target_revision=transcript_revision,
                    label="summary-revision",
                )
                return

        # Release the read connection before the LLM call. Summary generation is
        # best-effort background work and must not occupy the SQLite pool while
        # realtime ingest/presence/lifecycle requests are waiting.
        db.close()
        db = None

        summary = await incremental_summary(
            session_id=session_id,
            current_summary=current_summary,
            current_title=current_title,
            new_events=new_event_dicts,
            client=client,
            model=model,
            metadata=metadata,
        )

        for _attempt in range(2):
            values: dict = {
                "last_summarized_event_id": new_last_event_id,
                "summary_revision": transcript_revision if not new_chunk.has_more else summary_revision,
            }
            if summary:
                content_values = _summary_content_values(summary)
                if content_values:
                    values.update(content_values)
                else:
                    logger.warning("Discarding placeholder summary result for session %s", session_id)

            stmt = update(AgentSession).where(AgentSession.id == session_id)
            if cursor_id is not None:
                stmt = stmt.where(AgentSession.last_summarized_event_id == cursor_id)
            else:
                stmt = stmt.where(AgentSession.summary_event_count == expected_summary_event_count)

            def _do_update(write_db: Session) -> int:
                result = write_db.execute(stmt.values(**values))
                return int(result.rowcount or 0)

            if ws.is_configured:
                updated = await ws.execute_with_session_factory(session_factory, _do_update, label="summary")
            else:
                fallback_db = session_factory()
                try:
                    updated = await ws.execute_or_direct(_do_update, fallback_db, label="summary")
                finally:
                    fallback_db.close()
            if updated > 0:
                if summary:
                    logger.info("Updated summary for session %s: %s", session_id, summary.title)
                else:
                    logger.debug("No meaningful content for session %s, advanced cursor only", session_id)
                break

            retry_db = session_factory()
            try:
                session = retry_db.query(AgentSession).filter(AgentSession.id == session_id).first()
                if not session:
                    return
                cursor_id = session.last_summarized_event_id
                expected_summary_event_count = session.summary_event_count or 0
                new_chunk = _load_summary_event_chunk(retry_db, session_id=session_id, cursor_id=cursor_id)
                if not new_chunk.events:
                    return
                new_last_event_id = new_chunk.last_event_id
                new_event_dicts = new_chunk.events
                current_summary = session.summary
                current_title = session.summary_title
                metadata = {
                    "project": session.project,
                    "provider": session.provider,
                    "git_branch": session.git_branch,
                }
            finally:
                retry_db.close()
            summary = await incremental_summary(
                session_id=session_id,
                current_summary=current_summary,
                current_title=current_title,
                new_events=new_event_dicts,
                client=client,
                model=model,
                metadata=metadata,
            )
        else:
            logger.warning("CAS conflict persisted for session %s after retry", session_id)

    except Exception:
        if db is not None:
            db.rollback()
        logger.exception("Failed to generate summary for session %s", session_id)
        raise
    finally:
        if db is not None:
            db.close()
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.warning("Failed to close summarization client for session %s: %s", session_id, exc)


async def generate_embeddings_background(session_id: str) -> None:
    """Background task: generate embeddings for a session (semaphore-gated)."""
    async with _embedding_semaphore:
        await generate_embeddings_impl(session_id)


async def generate_embeddings_impl(session_id: str) -> bool:
    from types import SimpleNamespace

    from zerg.database import get_session_factory

    session_factory = get_session_factory()

    db = session_factory()
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if not session:
            return True
        transcript_revision = int(getattr(session, "transcript_revision", 0) or 0)
        embedding_revision = int(getattr(session, "embedding_revision", 0) or 0)
        if transcript_revision > 0 and embedding_revision >= transcript_revision:
            logger.debug(
                "Embeddings already current for session %s (embedding_revision=%s transcript_revision=%s)",
                session_id,
                embedding_revision,
                transcript_revision,
            )
            return True
        if transcript_revision <= 0 and getattr(session, "needs_embedding", 1) == 0:
            return True

        from zerg.models_config import get_embedding_config

        config = get_embedding_config()

        if not config:
            return True

        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.timestamp, AgentEvent.id)
            .all()
        )
        if not events:
            return True

        session_snapshot = SimpleNamespace(
            summary=session.summary,
            summary_title=session.summary_title,
        )
        event_dicts = events_to_dicts(events)

        from zerg.services.embedding_cache import EmbeddingCache
        from zerg.services.session_processing.embeddings import embed_session
        from zerg.services.session_processing.embeddings import mark_session_embedding_complete

        # Release the read connection before embedding API calls. Embeddings are
        # best-effort background work and must not occupy the SQLite pool while
        # realtime ingest/presence/lifecycle requests are waiting.
        db.close()
        db = None

        written, remaining = await embed_session(
            session_id,
            session_snapshot,
            event_dicts,
            config,
            None,
            transcript_revision=transcript_revision or None,
        )
        if written > 0:
            logger.info(
                "Generated %d embeddings for session %s (%d remaining)",
                written,
                session_id,
                remaining,
            )
            EmbeddingCache().invalidate()
        if remaining > 0 and written == 0:
            raise RuntimeError("Embedding reconciliation made no progress")
        if remaining == 0:
            await mark_session_embedding_complete(
                session_id,
                transcript_revision=transcript_revision or None,
            )
            return True
        return False

    except Exception:
        if db is not None:
            db.rollback()
        logger.exception("Failed to generate embeddings for session %s", session_id)
        raise
    finally:
        if db is not None:
            db.close()
