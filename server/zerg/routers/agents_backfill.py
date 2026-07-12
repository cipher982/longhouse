"""Agents API — backfill admin, usage stats, and ingest health endpoints."""

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker as _sessionmaker

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import get_db
from zerg.database import live_catalog_enabled
from zerg.database import live_store_configured
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_summaries import summarize_and_persist
from zerg.services.session_views import BackfillEmbeddingsProgressResponse
from zerg.services.session_views import BackfillEmbeddingsResponse
from zerg.services.session_views import BackfillProgressResponse
from zerg.services.session_views import BackfillSummariesResponse
from zerg.services.session_views import CursorRoleBackfillResponse
from zerg.services.session_views import IngestHealthResponse
from zerg.services.session_views import MediaBackfillInlineDataUrlsResponse
from zerg.services.session_views import UsageStatsByProvider
from zerg.services.session_views import UsageStatsResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def _no_ingest_health_db():
    yield None


_settings = get_settings()
_ingest_health_db_dependency = get_db if _settings.testing or not live_store_configured() else _no_ingest_health_db

_backfill_state: dict[str, Any] = {
    "running": False,
    "backfilled": 0,
    "skipped": 0,
    "errors": 0,
    "remaining": 0,
    "total": 0,
}

_embedding_backfill_state: dict[str, Any] = {
    "running": False,
    "embedded": 0,
    "skipped": 0,
    "errors": 0,
    "remaining": 0,
    "total": 0,
}


@router.post("/backfill-summaries", response_model=BackfillSummariesResponse)
async def backfill_summaries(
    concurrency: int = Query(5, ge=1, le=200, description="Max concurrent LLM requests"),
    project: Optional[str] = Query(None, description="Optional project filter"),
    force: bool = Query(False, description="Re-summarize sessions that already have summaries"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> BackfillSummariesResponse:
    """Start backfilling missing summaries as a background task."""
    from zerg.models_config import get_llm_client_for_use_case

    if _backfill_state["running"]:
        return BackfillSummariesResponse(
            status="already_running",
            total=_backfill_state["total"],
            message=f"Backfill in progress: {_backfill_state['backfilled']}/{_backfill_state['total']} done",
        )

    query = db.query(AgentSession)
    if not force:
        query = query.filter(AgentSession.summary.is_(None))
    if project:
        query = query.filter(AgentSession.project == project)
    total = query.count()

    if total == 0:
        return BackfillSummariesResponse(status="nothing_to_do", total=0, message="No sessions to backfill")

    try:
        client, model, _provider = get_llm_client_for_use_case("summarization")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Summarization is misconfigured: {e}",
        )

    asyncio.create_task(
        _run_backfill(
            concurrency=concurrency,
            project=project,
            force=force,
            client=client,
            model=model,
            total=total,
        )
    )

    return BackfillSummariesResponse(
        status="started",
        total=total,
        message=f"Backfill started for {total} sessions at concurrency {concurrency}",
    )


@router.get("/backfill-summaries", response_model=BackfillProgressResponse)
async def backfill_progress(
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> BackfillProgressResponse:
    """Check backfill progress."""
    return BackfillProgressResponse(**_backfill_state)


async def _run_backfill(
    *,
    concurrency: int,
    project: str | None,
    force: bool,
    client: Any,
    model: str,
    total: int,
    _engine: Any = None,
) -> None:
    """Background backfill -- processes all matching sessions with a semaphore."""
    from sqlalchemy.pool import NullPool

    from zerg.database import make_engine

    _backfill_state.update(running=True, backfilled=0, skipped=0, errors=0, remaining=total, total=total)
    semaphore = asyncio.Semaphore(concurrency)
    owns_engine = _engine is None

    try:
        if _engine is None:
            settings = get_settings()
            backfill_engine = make_engine(settings.database_url, poolclass=NullPool)
        else:
            backfill_engine = _engine
        SessionFactory = _sessionmaker(bind=backfill_engine)

        with SessionFactory() as db:
            query = db.query(AgentSession)
            if not force:
                query = query.filter(AgentSession.summary.is_(None))
            if project:
                query = query.filter(AgentSession.project == project)
            session_ids = [s.id for s in query.order_by(AgentSession.started_at.desc()).all()]

        async def _process_one(session_id: UUID) -> None:
            async with semaphore:
                try:
                    with SessionFactory() as db:
                        sess = db.get(AgentSession, session_id)
                        if not sess:
                            _backfill_state["skipped"] += 1
                            return

                        events = (
                            db.query(AgentEvent)
                            .filter(AgentEvent.session_id == session_id)
                            .filter(durable_transcript_event_predicate())
                            .order_by(AgentEvent.timestamp, AgentEvent.id)
                            .all()
                        )
                        if not events:
                            _backfill_state["skipped"] += 1
                            return

                        summary = await summarize_and_persist(sess, events, db, client, model)

                        if not summary:
                            _backfill_state["skipped"] += 1
                            return

                        _backfill_state["backfilled"] += 1

                except Exception as exc:
                    logger.error("Backfill failed for session %s: %s: %s", session_id, type(exc).__name__, exc)
                    _backfill_state["errors"] += 1
                finally:
                    _backfill_state["remaining"] = max(0, _backfill_state["remaining"] - 1)

        tasks = [_process_one(sid) for sid in session_ids]
        await asyncio.gather(*tasks)

    except Exception:
        logger.exception("Backfill task crashed")
    finally:
        _backfill_state["running"] = False
        try:
            await client.close()
        except Exception:
            pass
        if owns_engine:
            try:
                backfill_engine.dispose()
            except Exception:
                pass
        logger.info(
            "Backfill complete: %d backfilled, %d skipped, %d errors",
            _backfill_state["backfilled"],
            _backfill_state["skipped"],
            _backfill_state["errors"],
        )


@router.post("/backfill-embeddings", response_model=BackfillEmbeddingsResponse)
async def backfill_embeddings(
    concurrency: int = Query(5, ge=1, le=200, description="Max concurrent embedding requests"),
    project: Optional[str] = Query(None, description="Optional project filter"),
    force: bool = Query(False, description="Re-embed sessions that already have embeddings"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> BackfillEmbeddingsResponse:
    """Start backfilling missing embeddings as a background task."""
    from zerg.models_config import get_embedding_config

    if _embedding_backfill_state["running"]:
        embedded = _embedding_backfill_state["embedded"]
        total_running = _embedding_backfill_state["total"]
        message = f"Embedding backfill in progress: {embedded}/{total_running} done"
        return BackfillEmbeddingsResponse(
            status="already_running",
            total=_embedding_backfill_state["total"],
            message=message,
        )

    config = get_embedding_config()
    if not config:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No embedding provider configured",
        )

    query = db.query(AgentSession)
    if not force:
        query = query.filter(AgentSession.needs_embedding == 1)
    if project:
        query = query.filter(AgentSession.project == project)
    total = query.count()

    if total == 0:
        return BackfillEmbeddingsResponse(status="nothing_to_do", total=0, message="No sessions need embedding")

    asyncio.create_task(
        _run_embedding_backfill(
            concurrency=concurrency,
            project=project,
            force=force,
            config=config,
            total=total,
        )
    )

    return BackfillEmbeddingsResponse(
        status="started",
        total=total,
        message=f"Embedding backfill started for {total} sessions at concurrency {concurrency}",
    )


@router.get("/backfill-embeddings", response_model=BackfillEmbeddingsProgressResponse)
async def backfill_embeddings_progress(
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> BackfillEmbeddingsProgressResponse:
    """Check embedding backfill progress."""
    return BackfillEmbeddingsProgressResponse(**_embedding_backfill_state)


async def _run_embedding_backfill(
    *,
    concurrency: int,
    project: str | None,
    force: bool,
    config: Any,
    total: int,
) -> None:
    """Background embedding backfill."""
    from sqlalchemy.pool import NullPool

    from zerg.database import make_engine
    from zerg.services.session_processing.embeddings import embed_session
    from zerg.services.session_processing.embeddings import mark_session_embedding_complete

    _embedding_backfill_state.update(running=True, embedded=0, skipped=0, errors=0, remaining=total, total=total)
    semaphore = asyncio.Semaphore(concurrency)

    settings = get_settings()
    backfill_engine = make_engine(settings.database_url, poolclass=NullPool)
    SessionFactory = _sessionmaker(bind=backfill_engine)

    try:
        with SessionFactory() as db:
            query = db.query(AgentSession)
            if not force:
                query = query.filter(AgentSession.needs_embedding == 1)
            if project:
                query = query.filter(AgentSession.project == project)
            session_ids = [s.id for s in query.order_by(AgentSession.started_at.desc()).all()]

        async def _process_one(session_id: UUID) -> None:
            async with semaphore:
                try:
                    with SessionFactory() as db:
                        sess = db.get(AgentSession, session_id)
                        if not sess:
                            _embedding_backfill_state["skipped"] += 1
                            return

                        events = (
                            db.query(AgentEvent)
                            .filter(AgentEvent.session_id == session_id)
                            .filter(durable_transcript_event_predicate())
                            .order_by(AgentEvent.timestamp, AgentEvent.id)
                            .all()
                        )
                        if not events:
                            _embedding_backfill_state["skipped"] += 1
                            return

                        session_written = 0
                        while True:
                            written, remaining = await embed_session(
                                str(session_id),
                                sess,
                                events,
                                config,
                                db,
                                transcript_revision=int(getattr(sess, "transcript_revision", 0) or 0),
                            )
                            session_written += written
                            if remaining == 0:
                                await mark_session_embedding_complete(
                                    str(session_id),
                                    transcript_revision=int(getattr(sess, "transcript_revision", 0) or 0),
                                    db=db,
                                )
                                break
                            if written == 0:
                                raise RuntimeError("Embedding reconciliation made no progress")
                        if session_written > 0:
                            _embedding_backfill_state["embedded"] += 1
                        else:
                            _embedding_backfill_state["skipped"] += 1

                except Exception as exc:
                    logger.error("Embedding failed for session %s: %s: %s", session_id, type(exc).__name__, exc)
                    _embedding_backfill_state["errors"] += 1
                finally:
                    _embedding_backfill_state["remaining"] = max(0, _embedding_backfill_state["remaining"] - 1)

        tasks = [_process_one(sid) for sid in session_ids]
        await asyncio.gather(*tasks)

    except Exception:
        logger.exception("Embedding backfill crashed")
    finally:
        _embedding_backfill_state["running"] = False
        if _embedding_backfill_state["embedded"] > 0:
            try:
                from zerg.services.embedding_cache import EmbeddingCache

                EmbeddingCache().invalidate()
            except Exception:
                logger.warning("Failed to invalidate embedding cache after backfill")
        try:
            backfill_engine.dispose()
        except Exception:
            pass
        logger.info(
            "Embedding backfill complete: %d embedded, %d skipped, %d errors",
            _embedding_backfill_state["embedded"],
            _embedding_backfill_state["skipped"],
            _embedding_backfill_state["errors"],
        )


@router.post("/media/backfill-inline-data-urls", response_model=MediaBackfillInlineDataUrlsResponse)
async def backfill_inline_data_url_media(
    dry_run: bool = Query(True, description="When true, scan and report without writing media rows or blobs"),
    max_rows: int = Query(100, ge=1, le=1000, description="Maximum source_lines rows to scan in this batch"),
    max_bytes: int = Query(10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024, description="Decoded byte budget"),
    after_id: int = Query(0, ge=0, description="Only scan source_lines rows with id greater than this value"),
    confirmed_backup_gate: bool = Query(False, description="Required when dry_run=false"),
    disk_floor_bytes: int = Query(
        1024 * 1024 * 1024,
        ge=0,
        description="Minimum free bytes to leave on the media filesystem when writing",
    ),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MediaBackfillInlineDataUrlsResponse:
    """Opportunistically backfill legacy inline image data URLs into media objects."""

    from zerg.services.media_backfill import backfill_inline_data_url_media as run_backfill

    try:
        result = run_backfill(
            db,
            dry_run=dry_run,
            max_rows=max_rows,
            max_bytes=max_bytes,
            after_id=after_id,
            confirmed_backup_gate=confirmed_backup_gate,
            disk_floor_bytes=disk_floor_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    mode = "dry run" if result.dry_run else "write"
    candidates = result.candidate_refs
    scanned = result.scanned_source_lines
    message = f"Inline media backfill {mode}: {candidates} candidate refs in {scanned} source lines"
    return MediaBackfillInlineDataUrlsResponse(
        **result.__dict__,
        message=message,
    )


@router.post("/backfill-cursor-roles", response_model=CursorRoleBackfillResponse)
async def backfill_cursor_roles(
    dry_run: bool = Query(True, description="When true, classify and report without writing"),
    after_id: int = Query(0, ge=0, description="Only scan Cursor user events with id greater than this value"),
    batch_size: int = Query(5000, ge=1, le=20000, description="Max events to scan in this batch"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> CursorRoleBackfillResponse:
    """Repair one batch of legacy Cursor ``role="user"`` events.

    Re-roles Cursor's environment-context injection to ``system`` and unwraps
    the real user turn from ``<user_query>...</user_query>`` for rows that
    predate the decoder fix. Idempotent; paginated via ``after_id`` (loop until
    ``scanned == 0``). ``raw_json`` is never modified.
    """
    from zerg.services.cursor_role_backfill import backfill_cursor_user_roles

    result = backfill_cursor_user_roles(db, after_id=after_id, batch_size=batch_size, dry_run=dry_run)
    if not dry_run:
        db.commit()
    mode = "dry run" if dry_run else "write"
    message = (
        f"Cursor role backfill {mode}: scanned={result.scanned} "
        f"re_roleed={result.re_roleed} unwrapped={result.unwrapped} last_id={result.last_id}"
    )
    return CursorRoleBackfillResponse(
        dry_run=dry_run,
        scanned=result.scanned,
        re_roleed=result.re_roleed,
        unwrapped=result.unwrapped,
        last_id=result.last_id,
        message=message,
    )


@router.get("/ingest-health", response_model=IngestHealthResponse)
async def get_ingest_health(
    db: Session | None = Depends(_ingest_health_db_dependency),
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> IngestHealthResponse:
    """Check ingest freshness -- detects if sessions have stopped shipping."""
    from zerg.services.ingest_health import compute_ingest_health
    from zerg.services.ingest_health import compute_ingest_health_from_catalog_facts
    from zerg.services.media_backfill import compute_media_repair_health

    if live_catalog_enabled():
        owner_id = getattr(_auth, "owner_id", None)
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "owner_required", "message": "A device token is required for ingest health."},
            )
        catalogd = get_catalogd_client()
        if catalogd is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog health is temporarily unavailable."},
            )
        try:
            facts = await catalogd.call("storage.health.v2", {"owner_id": str(owner_id)})
        except (CatalogUnavailable, CatalogRemoteError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog health is temporarily unavailable."},
            ) from exc
        return IngestHealthResponse(**compute_ingest_health_from_catalog_facts(facts))

    assert db is not None
    result = compute_ingest_health(db)
    result.update(compute_media_repair_health(db))
    return IngestHealthResponse(**result)


@router.get("/usage-stats", response_model=UsageStatsResponse)
async def get_usage_stats(
    days: int = Query(30, ge=1, le=365, description="Days to look back (max 365)"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> UsageStatsResponse:
    """Session activity statistics by provider, queried live from sessions table."""
    from sqlalchemy import text as sa_text

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_date = since.strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = db.execute(
        sa_text("""
            SELECT
                COALESCE(provider, 'unknown') AS provider,
                COUNT(*) AS sessions,
                SUM(COALESCE(user_messages, 0) + COALESCE(assistant_messages, 0) + COALESCE(tool_calls, 0)) AS messages
            FROM sessions
            WHERE started_at >= :since
            GROUP BY COALESCE(provider, 'unknown')
            ORDER BY sessions DESC
        """),
        {"since": since.isoformat()},
    ).fetchall()

    by_provider = []
    for row in rows:
        stats = UsageStatsByProvider(
            provider=row.provider,
            sessions=row.sessions,
            messages=row.messages or 0,
        )
        by_provider.append(stats)

    return UsageStatsResponse(
        total_sessions=sum(r.sessions for r in by_provider),
        total_messages=sum(r.messages for r in by_provider),
        date_range={"from": since_date, "to": to_date},
        by_provider=by_provider,
    )
