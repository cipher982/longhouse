"""Use case for listing agent sessions.

This keeps the canonical `/api/agents/sessions` route thin while preserving
the current listing, search, runtime-overlay, and response-header behavior.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.services.agents_store import AgentsStore
from zerg.services.session_hybrid_search import list_hybrid_sessions
from zerg.services.session_listing_types import SessionListingError
from zerg.services.session_listing_types import SessionListParams
from zerg.services.session_listing_types import SessionListResult
from zerg.services.session_response_projection import build_session_response_list
from zerg.services.session_response_projection import has_real_sessions
from zerg.services.session_views import SessionsListResponse


async def list_agent_sessions(
    *,
    db: Session,
    auth: object,
    params: SessionListParams,
) -> SessionListResult:
    """List sessions for the machine-facing agents API."""

    _validate_managed_hook_scope(auth, params)
    _validate_context_mode(params.context_mode)
    effective_sort = _resolve_effective_sort(params)

    if params.mode == "hybrid":
        return await list_hybrid_sessions(db=db, params=params)

    return _list_lexical_sessions(db=db, params=params, effective_sort=effective_sort)


def _validate_managed_hook_scope(auth: object, params: SessionListParams) -> None:
    if not isinstance(auth, ManagedLocalHookToken):
        return

    if params.project != auth.project:
        raise SessionListingError(
            403,
            "Managed-local hook token requires a matching project filter",
        )
    if (
        params.provider is not None
        or params.environment is not None
        or params.include_test
        or params.device_id is not None
        or params.query is not None
        or params.offset != 0
        or params.limit > 5
        or params.days_back > 7
        or params.sort not in {None, "recency"}
        or params.mode != "lexical"
        or params.context_mode != "forensic"
        or not params.hide_autonomous
    ):
        raise SessionListingError(
            403,
            "Managed-local hook token only supports bounded recent project lookup",
        )


def _validate_context_mode(context_mode: str) -> None:
    if context_mode not in {"forensic", "active_context"}:
        raise SessionListingError(
            400,
            "context_mode must be one of: forensic, active_context",
        )


def _resolve_effective_sort(params: SessionListParams) -> str:
    if params.sort is None:
        return "relevance" if params.query else "recency"

    if params.sort == "balanced" and not params.query:
        raise SessionListingError(
            400,
            "sort=balanced requires a search query (q param)",
        )

    return params.sort


def _list_lexical_sessions(
    *,
    db: Session,
    params: SessionListParams,
    effective_sort: str,
) -> SessionListResult:
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=params.days_back)

    sessions, total = store.list_sessions(
        project=params.project,
        provider=params.provider,
        environment=params.environment,
        include_test=params.include_test,
        device_id=params.device_id,
        since=since,
        query=params.query,
        limit=params.limit,
        offset=params.offset,
        hide_autonomous=params.hide_autonomous,
        context_mode=params.context_mode,
        anchor_on_activity=effective_sort == "recency",
    )

    if params.query or effective_sort != "recency":
        from zerg.services.search import apply_sort

        bm25_order = [str(s.id) for s in sessions]
        sessions = apply_sort(sessions, effective_sort, bm25_order=bm25_order)

    session_ids = [s.id for s in sessions]
    match_map = store.get_session_matches(session_ids, params.query, context_mode=params.context_mode) if params.query else {}
    response_sessions = build_session_response_list(
        db=db,
        store=store,
        sessions=sessions,
        match_map=match_map,
    )

    if effective_sort == "recency":
        response_sessions.sort(
            key=lambda r: r.timeline_anchor_at or r.last_activity_at or r.started_at,
            reverse=True,
        )

    return SessionListResult(
        response=SessionsListResponse(
            sessions=response_sessions,
            total=total,
            has_real_sessions=has_real_sessions(db, default_when_empty=total == 0),
        )
    )
