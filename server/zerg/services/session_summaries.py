"""Background session summarization and embedding pipeline.

Extracted from the agents router — these are background async tasks, not HTTP
handlers. Summary enrichment is driven by session revision-lag reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.provider_interaction_semantics import title_eligible_provider_event
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_title import freeze_anchor_title
from zerg.services.session_title import sanitize_title

logger = logging.getLogger(__name__)

# Semaphore gates concurrent background embedding calls during bulk ingest.
_PLACEHOLDER_TITLE = "Untitled Session"

# Distributed lock for summary generation — prevents multiple Runtime Host
# replicas from concurrently calling the LLM for the same session.
_summary_lock_instance = f"{socket.gethostname()}:{os.getpid()}"
_SUMMARY_LOCK_STALE_SECONDS = 300  # 5 min: stale locks auto-expire


def _claim_summary_lock(db: Session, session_id: str) -> bool:
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(seconds=_SUMMARY_LOCK_STALE_SECONDS)
    result = (
        db.query(AgentSession)
        .filter(AgentSession.id == session_id)
        .filter(
            or_(
                AgentSession.summary_lock_instance.is_(None),
                AgentSession.summary_lock_at < stale_threshold,
            )
        )
        .update(
            {
                "summary_lock_instance": _summary_lock_instance,
                "summary_lock_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(result or 0) > 0


_PLACEHOLDER_SUMMARY = "No summary generated."
SUMMARY_EVENT_LOAD_LIMIT = int(os.getenv("SESSION_SUMMARY_EVENT_LOAD_LIMIT", "200"))
SUMMARY_EVENT_TEXT_MAX_CHARS = int(os.getenv("SESSION_SUMMARY_EVENT_TEXT_MAX_CHARS", "4000"))
INITIAL_TITLE_WRITE_TIMEOUT_SECONDS = float(os.getenv("SESSION_INITIAL_TITLE_WRITE_TIMEOUT_SECONDS", "5"))
INITIAL_TITLE_RETRY_BASE_SECONDS = int(os.getenv("SESSION_INITIAL_TITLE_RETRY_BASE_SECONDS", "30"))
INITIAL_TITLE_RETRY_MAX_SECONDS = int(os.getenv("SESSION_INITIAL_TITLE_RETRY_MAX_SECONDS", "900"))


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


def events_to_dicts(events: list[AgentEvent], *, provider: str | None = None) -> list[dict]:
    """Convert ORM AgentEvent rows to plain dicts for summarization."""
    result: list[dict] = []
    for event in events:
        event_provider = provider or getattr(getattr(event, "session", None), "provider", None)
        if event.role == "user" and not title_eligible_provider_event(
            event_provider,
            role=event.role,
            content_text=event.content_text,
            raw_json=decode_raw_json(event),
        ):
            continue
        result.append(
            {
                "role": event.role,
                "content_text": event.content_text,
                "tool_name": event.tool_name,
                "tool_input_json": event.tool_input_json,
                "tool_output_text": event.tool_output_text,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            }
        )
    return result


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
    provider = db.query(AgentSession.provider).filter(AgentSession.id == session_id).scalar()
    base_query = (
        db.query(AgentEvent)
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

    events = []
    for row in rows:
        if row.role == "user" and not title_eligible_provider_event(
            provider,
            role=row.role,
            content_text=row.content_text,
            raw_json=decode_raw_json(row),
        ):
            continue
        events.append(
            {
                "role": row.role,
                "content_text": str(row.content_text or "")[:text_chars],
                "tool_name": row.tool_name,
                "tool_input_json": row.tool_input_json,
                "tool_output_text": row.tool_output_text,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            }
        )
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

    event_dicts = events_to_dicts(events, provider=session.provider)

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
        values = dict(
            summary_event_count=len(events),
            last_summarized_event_id=new_last_event_id,
            summary_revision=target_revision,
            **content_values,
        )
        result = write_db.execute(sa_update(AgentSession).where(AgentSession.id == session.id).values(**values))
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


async def record_initial_title_failure(session_id: str, reason: str) -> None:
    """Persist bounded retry evidence without treating a fallback as completion."""
    from zerg.database import get_session_factory
    from zerg.services.write_serializer import get_write_serializer

    factory = get_session_factory()
    db = factory()
    now = datetime.now(timezone.utc)
    try:

        def _do_update(write_db: Session) -> int:
            target = write_db.query(AgentSession).filter(AgentSession.id == session_id).first()
            if target is None or sanitize_title(target.anchor_title):
                return 0
            attempts = int(target.title_attempt_count or 0) + 1
            delay_seconds = min(
                INITIAL_TITLE_RETRY_MAX_SECONDS,
                INITIAL_TITLE_RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 8)),
            )
            target.title_attempt_count = attempts
            target.title_last_attempt_at = now
            target.title_retry_at = now + timedelta(seconds=delay_seconds)
            target.title_last_error = reason[:128]
            return 1

        ws = get_write_serializer()
        await ws.execute_or_direct(_do_update, db, label="initial-title-failure")
    except Exception:
        logger.exception("Failed to record initial-title failure for session %s", session_id)
        db.rollback()
    finally:
        db.close()


async def generate_initial_title_impl(session_id: str) -> bool:
    """Generate and persist a fast stable title from the first user message."""
    from zerg.database import get_session_factory
    from zerg.services.session_hot_cards import upsert_timeline_card_from_session
    from zerg.services.title_generator import generate_initial_session_title

    settings = get_settings()
    if settings.testing:
        return False
    if settings.llm_disabled:
        # Disabled is an intentional capability state, not a failed attempt.
        # Recording retry evidence here schedules a monolith write for every
        # newly ingested session even though no retry can succeed.
        return False

    factory = get_session_factory()
    db = factory()
    client = None
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if not session:
            return False
        if sanitize_title(session.anchor_title):
            return False
        if session.environment in {"test", "e2e"}:
            return False
        if classify_provider_proof_environment(
            cwd=session.cwd,
            first_user_text=session.first_user_message_preview,
        ):
            return False

        first_user_message = (session.first_user_message_preview or "").strip()
        if first_user_message and not title_eligible_provider_event(
            session.provider,
            role="user",
            content_text=first_user_message,
        ):
            first_user_message = ""
        if not first_user_message:
            user_events = (
                db.query(AgentEvent)
                .filter(AgentEvent.session_id == session_id)
                .filter(AgentEvent.role == "user")
                .filter(AgentEvent.content_text.isnot(None))
                .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
                .all()
            )
            first_user_message = next(
                (
                    str(event.content_text or "").strip()
                    for event in user_events
                    if str(event.content_text or "").strip()
                    and str(event.content_text or "").strip().lower() != "warmup"
                    and title_eligible_provider_event(
                        session.provider,
                        role=event.role,
                        content_text=event.content_text,
                        raw_json=decode_raw_json(event),
                    )
                ),
                "",
            )
        if not first_user_message:
            await record_initial_title_failure(session_id, "missing_durable_user_message")
            return False

        metadata = {
            "project": session.project,
            "provider": session.provider,
            "git_branch": session.git_branch,
        }
        transcript_revision = int(getattr(session, "transcript_revision", 0) or 0)

        from zerg.models_config import get_llm_client_for_use_case

        try:
            client, model, _provider = get_llm_client_for_use_case("session_title")
        except ValueError as exc:
            logger.warning("Initial title generation misconfigured for session %s: %s", session_id, exc)
            await record_initial_title_failure(session_id, "model_unconfigured")
            return False

        db.close()
        db = None

        started = time.perf_counter()
        raw_title = await generate_initial_session_title(
            first_user_message=first_user_message,
            client=client,
            model=model,
            metadata=metadata,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        title = sanitize_title(raw_title, max_words=6)
        if not title:
            logger.info("Initial title generation returned no title for session %s in %dms", session_id, elapsed_ms)
            await record_initial_title_failure(session_id, "empty_model_response")
            return False

        def _persist_direct() -> int:
            write_db = factory()
            try:
                target = write_db.query(AgentSession).filter(AgentSession.id == session_id).first()
                if not target:
                    return 0
                if sanitize_title(target.anchor_title):
                    return 0
                target.summary_title = title
                target.anchor_title = freeze_anchor_title(title)
                target.title_retry_at = None
                target.title_last_error = None
                target.title_last_attempt_at = datetime.now(timezone.utc)
                if transcript_revision > 0:
                    target.summary_revision = max(int(target.summary_revision or 0), transcript_revision)
                upsert_timeline_card_from_session(write_db, target)
                write_db.commit()
                return 1
            except Exception:
                write_db.rollback()
                raise
            finally:
                write_db.close()

        try:
            updated = await asyncio.wait_for(
                asyncio.to_thread(_persist_direct),
                timeout=INITIAL_TITLE_WRITE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Initial title write timed out for session %s after %.1fs",
                session_id,
                INITIAL_TITLE_WRITE_TIMEOUT_SECONDS,
            )
            await record_initial_title_failure(session_id, "write_timeout")
            return False
        if updated:
            from zerg.services.session_pubsub import publish_session_title_update

            publish_session_title_update(
                session_id=session_id,
                provider=metadata.get("provider"),
                source="initial_title",
            )
            logger.info("Generated initial title for session %s in %dms: %s", session_id, elapsed_ms, title)
        return bool(updated)
    except Exception:
        if db is not None:
            db.rollback()
        logger.exception("Failed to generate initial title for session %s", session_id)
        await record_initial_title_failure(session_id, "generation_error")
        return False
    finally:
        if db is not None:
            db.close()
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.warning("Failed to close initial title client for session %s: %s", session_id, exc)


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
        if session.environment in {"test", "e2e"}:
            logger.debug("Skipping summary for test session %s", session_id)
            return
        if classify_provider_proof_environment(
            cwd=session.cwd,
            first_user_text=session.first_user_message_preview,
        ):
            logger.debug("Skipping summary for provider proof session %s", session_id)
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
                await _advance_session_revision(
                    db=db,
                    session_id=session_id,
                    column_name="summary_revision",
                    target_revision=transcript_revision,
                    label="summary-revision",
                )
                return

        # Claim a distributed lock before the LLM call so multiple Runtime
        # Host replicas do not both call the provider for the same session.
        if not _claim_summary_lock(db, session_id):
            logger.debug("Session %s summary lock held by another replica", session_id)
            db.close()
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
                "summary_lock_instance": None,
                "summary_lock_at": None,
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
