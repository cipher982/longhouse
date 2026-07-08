"""Revision-lag reconciliation for derived session enrichment."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.models.agents import AgentSession
from zerg.services.internal_sessions import internal_canary_session_clause
from zerg.services.internal_sessions import provider_proof_session_clause
from zerg.services.session_response_projection import SUMMARY_MIN_MEANINGFUL_MESSAGES

logger = logging.getLogger(__name__)

SUMMARY_RECONCILER_POLL_SECONDS = float(os.getenv("SESSION_SUMMARY_POLL_SECONDS", "5"))
SUMMARY_RECONCILER_BATCH_SIZE = int(os.getenv("SESSION_SUMMARY_BATCH_SIZE", "50"))
SUMMARY_SELECT_TIMEOUT_SECONDS = float(os.getenv("SESSION_SUMMARY_SELECT_TIMEOUT_SECONDS", "2"))
SESSION_SUMMARY_CONCURRENCY = int(os.getenv("SESSION_SUMMARY_CONCURRENCY", "2"))

_active_summary_session_ids: set[str] = set()
_active_summary_lock = asyncio.Lock()


@dataclass(frozen=True)
class SummaryReconcileResult:
    fast_forwarded: int
    selected: int
    started: int
    initial_selected: int = 0
    initial_started: int = 0
    initial_titled: int = 0


GenerateSummaryFn = Callable[[str], Awaitable[None]]
GenerateInitialTitleFn = Callable[[str], Awaitable[bool]]


def _meaningful_count_expr():
    return func.coalesce(AgentSession.user_messages, 0) + func.coalesce(AgentSession.assistant_messages, 0)


def _revision_lag_filter():
    return (
        func.coalesce(AgentSession.transcript_revision, 0) > 0,
        func.coalesce(AgentSession.summary_revision, 0) < func.coalesce(AgentSession.transcript_revision, 0),
    )


def select_stale_summary_session_ids(
    db: Session,
    *,
    limit: int,
    exclude_session_ids: Collection[str] = (),
) -> list[str]:
    """Return session IDs needing live summary reconciliation.

    This is the summary work queue: session revision lag, ordered by user value.
    It intentionally does not inspect SessionTask.
    """
    if limit <= 0:
        return []

    meaningful_count = _meaningful_count_expr()
    title_missing = case(
        (or_(AgentSession.summary_title.is_(None), AgentSession.summary_title == ""), 0),
        else_=1,
    )
    query = (
        db.query(AgentSession.id)
        .filter(*_revision_lag_filter())
        .filter(meaningful_count >= SUMMARY_MIN_MEANINGFUL_MESSAGES)
        .filter(AgentSession.environment.notin_(["test", "e2e"]))
        .filter(~internal_canary_session_clause(AgentSession))
        .filter(~provider_proof_session_clause(AgentSession))
        .order_by(
            title_missing,
            AgentSession.last_activity_at.desc().nullslast(),
            AgentSession.started_at.desc().nullslast(),
            AgentSession.id,
        )
        .limit(max(limit, 0) + len(exclude_session_ids))
    )
    excluded = {str(session_id) for session_id in exclude_session_ids}
    return [str(row[0]) for row in query.all() if str(row[0]) not in excluded][:limit]


def select_initial_title_session_ids(
    db: Session,
    *,
    limit: int,
    exclude_session_ids: Collection[str] = (),
) -> list[str]:
    """Return low-content sessions that still deserve a fast first-prompt title."""
    if limit <= 0:
        return []

    meaningful_count = _meaningful_count_expr()
    query = (
        db.query(AgentSession.id)
        .filter(*_revision_lag_filter())
        .filter(meaningful_count < SUMMARY_MIN_MEANINGFUL_MESSAGES)
        .filter(AgentSession.environment.notin_(["test", "e2e"]))
        .filter(~internal_canary_session_clause(AgentSession))
        .filter(~provider_proof_session_clause(AgentSession))
        .filter(or_(AgentSession.anchor_title.is_(None), AgentSession.anchor_title == ""))
        .filter(or_(AgentSession.summary_title.is_(None), AgentSession.summary_title == ""))
        .filter(AgentSession.first_user_message_preview.isnot(None))
        .filter(func.trim(AgentSession.first_user_message_preview) != "")
        .order_by(
            AgentSession.last_activity_at.desc().nullslast(),
            AgentSession.started_at.desc().nullslast(),
            AgentSession.id,
        )
        .limit(max(limit, 0) + len(exclude_session_ids))
    )
    excluded = {str(session_id) for session_id in exclude_session_ids}
    return [str(row[0]) for row in query.all() if str(row[0]) not in excluded][:limit]


def fast_forward_low_content_summary_revisions(
    db: Session,
    *,
    limit: int,
    exclude_session_ids: Collection[str] = (),
) -> int:
    """Advance stale low-content sessions without calling an LLM provider."""
    if limit <= 0:
        return 0

    meaningful_count = _meaningful_count_expr()
    query = (
        db.query(AgentSession.id, AgentSession.transcript_revision)
        .filter(*_revision_lag_filter())
        .filter(meaningful_count < SUMMARY_MIN_MEANINGFUL_MESSAGES)
    )
    excluded = {str(session_id) for session_id in exclude_session_ids}
    if excluded:
        query = query.filter(AgentSession.id.notin_(excluded))
    rows = (
        query.order_by(
            AgentSession.last_activity_at.desc().nullslast(),
            AgentSession.started_at.desc().nullslast(),
            AgentSession.id,
        )
        .limit(limit)
        .all()
    )
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    updated = 0
    for session_id, transcript_revision in rows:
        target_revision = int(transcript_revision or 0)
        if target_revision <= 0:
            continue
        count = (
            db.query(AgentSession)
            .filter(AgentSession.id == session_id)
            .filter(func.coalesce(AgentSession.summary_revision, 0) < target_revision)
            .update(
                {
                    "summary_revision": target_revision,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        updated += int(count or 0)
    return updated


async def _claim_summary_session_ids(session_ids: list[str]) -> list[str]:
    claimed: list[str] = []
    async with _active_summary_lock:
        for session_id in session_ids:
            if session_id in _active_summary_session_ids:
                continue
            _active_summary_session_ids.add(session_id)
            claimed.append(session_id)
    return claimed


async def _release_summary_session_id(session_id: str) -> None:
    async with _active_summary_lock:
        _active_summary_session_ids.discard(session_id)


async def active_summary_session_ids() -> set[str]:
    async with _active_summary_lock:
        return set(_active_summary_session_ids)


async def reconcile_summaries_once(
    *,
    session_factory: sessionmaker | None = None,
    limit: int = SUMMARY_RECONCILER_BATCH_SIZE,
    concurrency: int = SESSION_SUMMARY_CONCURRENCY,
    generate_summary: GenerateSummaryFn | None = None,
    generate_initial_title: GenerateInitialTitleFn | None = None,
) -> SummaryReconcileResult:
    """Reconcile one batch of stale summary revisions."""
    if limit <= 0 or concurrency <= 0:
        return SummaryReconcileResult(fast_forwarded=0, selected=0, started=0)

    from zerg.database import get_session_factory
    from zerg.services.session_summaries import generate_initial_title_impl
    from zerg.services.session_summaries import generate_summary_impl
    from zerg.services.write_serializer import get_write_serializer

    factory = session_factory or get_session_factory()
    generate = generate_summary or generate_summary_impl
    generate_title = generate_initial_title or generate_initial_title_impl
    ws = get_write_serializer()

    initial_selected_ids: list[str] = []
    initial_claimed_ids: list[str] = []
    initial_titled = 0
    fast_forwarded = 0

    active_ids = await active_summary_session_ids()

    def _select_initial_ids() -> list[str]:
        db = factory()
        try:
            return select_initial_title_session_ids(db, limit=limit, exclude_session_ids=active_ids)
        finally:
            db.close()

    try:
        initial_selected_ids = await asyncio.wait_for(
            asyncio.to_thread(_select_initial_ids),
            timeout=SUMMARY_SELECT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Initial title reconciler selection timed out after %.1fs; skipping title tick",
            SUMMARY_SELECT_TIMEOUT_SECONDS,
        )
        initial_selected_ids = []

    initial_claimed_ids = await _claim_summary_session_ids(initial_selected_ids)
    if initial_claimed_ids:
        title_semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _run_title(session_id: str) -> bool:
            async with title_semaphore:
                try:
                    return await generate_title(session_id)
                finally:
                    await _release_summary_session_id(session_id)

        title_tasks = [asyncio.create_task(_run_title(session_id)) for session_id in initial_claimed_ids]
        try:
            title_results = await asyncio.gather(*title_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in title_tasks:
                task.cancel()
            await asyncio.gather(*title_tasks, return_exceptions=True)
            raise
        title_failures = [result for result in title_results if isinstance(result, Exception)]
        if title_failures:
            raise title_failures[0]
        initial_titled = sum(1 for result in title_results if result is True)

    fast_forwarded = await ws.execute_with_session_factory(
        factory,
        lambda db: fast_forward_low_content_summary_revisions(
            db,
            limit=limit,
            exclude_session_ids=initial_claimed_ids,
        ),
        label="summary",
    )

    active_ids = await active_summary_session_ids()

    def _select_stale_ids() -> list[str]:
        db = factory()
        try:
            return select_stale_summary_session_ids(db, limit=limit, exclude_session_ids=active_ids)
        finally:
            db.close()

    try:
        selected_ids = await asyncio.wait_for(
            asyncio.to_thread(_select_stale_ids),
            timeout=SUMMARY_SELECT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Session summary reconciler selection timed out after %.1fs; skipping tick",
            SUMMARY_SELECT_TIMEOUT_SECONDS,
        )
        return SummaryReconcileResult(
            initial_selected=len(initial_selected_ids),
            initial_started=len(initial_claimed_ids),
            initial_titled=initial_titled,
            fast_forwarded=fast_forwarded,
            selected=0,
            started=0,
        )

    claimed_ids = await _claim_summary_session_ids(selected_ids)
    if not claimed_ids:
        return SummaryReconcileResult(
            initial_selected=len(initial_selected_ids),
            initial_started=len(initial_claimed_ids),
            initial_titled=initial_titled,
            fast_forwarded=fast_forwarded,
            selected=len(selected_ids),
            started=0,
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(session_id: str) -> None:
        async with semaphore:
            try:
                await generate(session_id)
            finally:
                await _release_summary_session_id(session_id)

    tasks = [asyncio.create_task(_run_one(session_id)) for session_id in claimed_ids]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failures = [result for result in results if isinstance(result, Exception)]
    if failures:
        raise failures[0]

    return SummaryReconcileResult(
        initial_selected=len(initial_selected_ids),
        initial_started=len(initial_claimed_ids),
        initial_titled=initial_titled,
        fast_forwarded=fast_forwarded,
        selected=len(selected_ids),
        started=len(claimed_ids),
    )


async def run_summary_reconciler(
    *,
    poll_seconds: float = SUMMARY_RECONCILER_POLL_SECONDS,
    batch_size: int = SUMMARY_RECONCILER_BATCH_SIZE,
    concurrency: int = SESSION_SUMMARY_CONCURRENCY,
) -> None:
    """Run the live summary revision-lag reconciler until cancelled."""
    if concurrency <= 0:
        logger.info("Session summary reconciler disabled (SESSION_SUMMARY_CONCURRENCY=%s)", concurrency)
        return

    logger.info(
        "Session summary reconciler started (poll=%.1fs batch=%d concurrency=%d)",
        poll_seconds,
        batch_size,
        concurrency,
    )
    while True:
        try:
            result = await reconcile_summaries_once(limit=batch_size, concurrency=concurrency)
            if result.fast_forwarded or result.started:
                logger.debug(
                    "Session summary reconciler processed batch (fast_forwarded=%d selected=%d started=%d)",
                    result.fast_forwarded,
                    result.selected,
                    result.started,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session summary reconciler tick failed")

        await asyncio.sleep(max(0.0, poll_seconds))
