"""Agents API — session CRUD, listing, and export endpoints."""

import asyncio
import logging
from datetime import date as date_type
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import List
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import catalog_db_dependency
from zerg.database import get_db
from zerg.database import get_live_session_factory
from zerg.database import live_store_configured
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.routers.agents_search import search_storage_v2_sessions
from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_capabilities import project_capabilities_bulk
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.archive_transcript import ArchiveTranscriptUnavailable
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import timeline_snapshot
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.live_catalog_timeline import list_live_catalog_sessions
from zerg.services.live_catalog_timeline import read_live_catalog_session
from zerg.services.live_session_state import list_active_live_session_ids
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.searchd_supervisor import get_searchd_client
from zerg.services.session_archive import SessionArchiveBundleResponse
from zerg.services.session_archive import SessionArchiveManifestResponse
from zerg.services.session_archive import build_session_archive_bundle
from zerg.services.session_archive import build_session_archive_manifest_item
from zerg.services.session_archive import build_storage_v2_archive_bundle
from zerg.services.session_archive import build_storage_v2_archive_manifest
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.session_coordination import acknowledge_session_message as acknowledge_session_message_for_session
from zerg.services.session_coordination import list_session_messages
from zerg.services.session_coordination import load_session_tail
from zerg.services.session_coordination import query_wall_sessions
from zerg.services.session_coordination import serialize_session_message
from zerg.services.session_graph_projection import build_session_graph_projection
from zerg.services.session_kernel_projection import project_provider_session_id
from zerg.services.session_kernel_projection import project_session_lineage_fields
from zerg.services.session_listing import SessionListingError
from zerg.services.session_listing import SessionListParams
from zerg.services.session_listing import list_agent_sessions
from zerg.services.session_messages import create_session_message
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_pause_requests import load_active_pause_request_map
from zerg.services.session_pause_requests import load_hot_session_projection_map
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_turns import execute_session_turn_write
from zerg.services.session_turns import get_session_turn_by_id
from zerg.services.session_turns import list_session_turns
from zerg.services.session_turns import load_pending_response_turn_map
from zerg.services.session_turns import materialize_managed_transcript_turns
from zerg.services.session_views import ActiveSessionResponse
from zerg.services.session_views import ActiveSessionsResponse
from zerg.services.session_views import EventsListResponse
from zerg.services.session_views import FiltersResponse
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionActionResponse
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionLoopModeResponse
from zerg.services.session_views import SessionNotificationWatchRequest
from zerg.services.session_views import SessionNotificationWatchResponse
from zerg.services.session_views import SessionPreviewMessage
from zerg.services.session_views import SessionPreviewResponse
from zerg.services.session_views import SessionProjectionItemResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.services.session_views import SessionsSummaryResponse
from zerg.services.session_views import SessionSummaryResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionTurnEnvelopeResponse
from zerg.services.session_views import SessionTurnsListResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_views import StartupContextItemResponse
from zerg.services.session_views import StartupContextResponse
from zerg.services.session_views import WallResponse
from zerg.services.session_views import build_active_session_response
from zerg.services.session_views import build_event_input_origin_map
from zerg.services.session_views import build_event_media_ref_map
from zerg.services.session_views import build_event_response
from zerg.services.session_views import build_live_launch_placeholder_response
from zerg.services.session_views import build_session_action_response
from zerg.services.session_views import build_session_response
from zerg.services.session_views import build_session_turn_response
from zerg.services.session_views import build_tool_call_state_map
from zerg.services.session_views import is_session_closed
from zerg.services.session_views import latest_launch_attempts
from zerg.services.session_views import latest_live_launch_readiness
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.session_workspace import build_session_workspace
from zerg.services.session_workspace import get_legacy_workspace_session_factory
from zerg.services.startup_context import STARTUP_CONTEXT_DEFAULT_DAYS_BACK
from zerg.services.startup_context import STARTUP_CONTEXT_DEFAULT_LIMIT
from zerg.services.startup_context import STARTUP_CONTEXT_MAX_DAYS_BACK
from zerg.services.startup_context import STARTUP_CONTEXT_MAX_LIMIT
from zerg.services.startup_context import load_startup_context_items
from zerg.services.startup_context import render_startup_context
from zerg.services.storage_v2_export import build_storage_v2_raw_export
from zerg.services.storage_v2_workspace import build_storage_v2_workspace
from zerg.services.worklog_day_export import WorklogDayExportResponse
from zerg.services.worklog_day_export import WorklogV2Error
from zerg.services.worklog_day_export import build_worklog_day_export
from zerg.services.worklog_day_export import build_worklog_day_export_v2
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.utils.server_timing import ServerTimingRecorder
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()


def _no_session_preferences_db():
    yield None


session_preferences_db_dependency = _no_session_preferences_db if database_module.live_catalog_enabled() else get_db

VALID_USER_STATES = {"active", "parked", "snoozed", "archived"}
_CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id"
_ACTIVE_LIVE_SESSION_CANDIDATE_MULTIPLIER = 5
_ACTIVE_LIVE_SESSION_CANDIDATE_MAX = 1000


def _no_viewer_owner_id() -> int | None:
    return None


def _session_detail_db():
    """Open the legacy archive DB only when the catalog read path is disabled."""

    if database_module.live_catalog_enabled():
        yield None
        return
    with database_module.get_session_factory()() as db:
        yield db


session_detail_db_dependency = get_db if _catalog_db_dependency is get_db else _session_detail_db
machine_session_read_db_dependency = get_db if get_settings().testing else _session_detail_db


def _live_launch_placeholder_for_owner(
    session_id: UUID,
    *,
    owner_id: int | None,
    now: datetime | None = None,
) -> SessionResponse | None:
    live_launch_readiness = latest_live_launch_readiness([session_id], now=now).get(session_id)
    if live_launch_readiness is None:
        return None
    if owner_id is not None and live_launch_readiness.owner_id != str(owner_id):
        return None
    return build_live_launch_placeholder_response(live_launch_readiness, now=now)


def _owner_id_from_agents_auth(db: Session, auth: object) -> int | None:
    if not isinstance(auth, DeviceToken):
        return None
    return _resolve_agents_owner_id(db, auth)


@router.get("/worklog/day", response_model=WorklogDayExportResponse)
async def export_worklog_day(
    date: date_type = Query(..., description="Digest day in the supplied timezone, YYYY-MM-DD"),
    timezone_name: str = Query("America/New_York", alias="timezone", description="IANA timezone for day boundaries"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> WorklogDayExportResponse:
    """Return one day of session messages for machine worklog consumers."""
    try:
        if database_module.live_catalog_enabled():
            owner_id = getattr(_auth, "owner_id", None)
            if owner_id is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "owner_required",
                        "message": "Worklog export requires an owner-bound device token.",
                    },
                )
            catalog = get_catalogd_client()
            search = get_searchd_client()
            if catalog is None or search is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "worklog_projection_unavailable",
                        "message": "The derived worklog projection is temporarily unavailable.",
                    },
                )
            return await build_worklog_day_export_v2(
                catalog=catalog,
                search=search,
                owner_id=str(owner_id),
                day=date,
                timezone_name=timezone_name,
                include_test=include_test,
            )
        if db is None:
            raise RuntimeError("legacy worklog database dependency is unavailable")
        return build_worklog_day_export(
            db,
            day=date,
            timezone_name=timezone_name,
            include_test=include_test,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except WorklogV2Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        reason = exc.code if isinstance(exc, CatalogRemoteError) else "search_unavailable"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "worklog_projection_unavailable",
                "message": "The derived worklog projection is temporarily unavailable.",
                "reason": reason,
            },
        ) from exc


def _active_live_session_candidates(*, limit: int, days_back: int, now: datetime) -> list[UUID] | None:
    if not live_store_configured():
        return None
    if database_module.live_catalog_enabled():
        from zerg.services.catalog_read_gateway import active_session_ids

        result = active_session_ids(
            limit=min(
                _ACTIVE_LIVE_SESSION_CANDIDATE_MAX,
                max(limit, limit * _ACTIVE_LIVE_SESSION_CANDIDATE_MULTIPLIER),
            ),
            days_back=days_back,
            observed_at=now.isoformat(),
        )
        return [UUID(value) for value in result.get("session_ids", [])]
    LiveSessionFactory = get_live_session_factory()
    if LiveSessionFactory is None:
        return None
    candidate_limit = min(
        _ACTIVE_LIVE_SESSION_CANDIDATE_MAX,
        max(limit, limit * _ACTIVE_LIVE_SESSION_CANDIDATE_MULTIPLIER),
    )
    with LiveSessionFactory() as live_db:
        return list_active_live_session_ids(
            live_db,
            limit=candidate_limit,
            days_back=days_back,
            now=now,
        )


def _bounded_preview(value: str | None, *, max_len: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_len]


def _parse_message_session_header(request: Request) -> UUID | None:
    raw = str(request.headers.get(_CURRENT_SESSION_HEADER, "") or "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{_CURRENT_SESSION_HEADER} must be a valid UUID",
        ) from exc


def _build_projection_seam_response(*, db: Session, item) -> SessionProjectionItemResponse:
    item_lineage = project_session_lineage_fields(db, item.session)
    parent_lineage = project_session_lineage_fields(db, item.parent_session) if item.parent_session else None
    return SessionProjectionItemResponse(
        kind="seam",
        session_id=str(item.session.id),
        timestamp=item.session.started_at,
        continued_from_session_id=item_lineage.continued_from_session_id,
        continuation_kind=item_lineage.continuation_kind,
        origin_label=item_lineage.origin_label,
        parent_origin_label=(parent_lineage.origin_label if parent_lineage else None),
        parent_continuation_kind=(parent_lineage.continuation_kind if parent_lineage else None),
        branched_from_event_id=item_lineage.branched_from_event_id,
    )


def _resolve_message_actor_session(
    *,
    db: Session,
    request: Request,
    token: object | None,
    declared_session_id: UUID | None,
) -> AgentSession:
    header_session_id = _parse_message_session_header(request)
    token_session_raw = str(getattr(token, "session_id", "") or "").strip()
    token_session_id: UUID | None = None
    if token_session_raw:
        try:
            token_session_id = UUID(token_session_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated session context is invalid",
            ) from exc

    resolved_session_id = declared_session_id
    if token_session_id is not None:
        if header_session_id is not None and header_session_id != token_session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated session context does not match request header",
            )
        if declared_session_id is not None and declared_session_id != token_session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated session context does not match request body",
            )
        resolved_session_id = token_session_id
    elif header_session_id is not None:
        if declared_session_id is not None and declared_session_id != header_session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Current session header does not match request body",
            )
        resolved_session_id = header_session_id

    if resolved_session_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide {_CURRENT_SESSION_HEADER} or session_id context for this request",
        )

    session = db.query(AgentSession).filter(AgentSession.id == resolved_session_id).first()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {resolved_session_id} not found",
        )

    token_device_id = str(getattr(token, "device_id", "") or "").strip()
    session_device_id = str(getattr(session, "device_id", "") or "").strip()
    if token_device_id and session_device_id and token_device_id != session_device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated device cannot act as the requested session",
        )

    return session


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    hide_autonomous: bool = Query(
        True,
        description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)",
    ),
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in otherwise default-hidden lists",
    ),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort: Optional[str] = Query(
        None,
        description="Sort order: relevance|recency|balanced. Default: recency if no query, relevance if query present.",
    ),
    mode: Optional[str] = Query("lexical", description="Search mode: lexical|semantic|hybrid. Default: lexical."),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionsListResponse:
    """List sessions with optional filters."""
    try:
        params = SessionListParams(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            hide_autonomous=hide_autonomous,
            include_automation=include_automation,
            device_id=device_id,
            days_back=days_back,
            query=query,
            limit=limit,
            offset=offset,
            sort=sort,
            mode=mode,
            context_mode=context_mode,
        )
        if database_module.live_catalog_enabled():
            if query is not None:
                if context_mode != "forensic":
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "code": "search_mode_unsupported",
                            "message": "Storage-v2 search does not yet project active-context boundaries.",
                        },
                    )
                owner_id = getattr(_auth, "owner_id", None)
                if owner_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "code": "owner_required",
                            "message": "Storage-v2 search requires an owner-bound device token.",
                        },
                    )
                sessions = await search_storage_v2_sessions(
                    owner_id=int(owner_id),
                    query=query,
                    project=project,
                    provider=provider,
                    environment=environment,
                    days_back=days_back,
                    limit=limit + offset,
                    include_test=include_test,
                    hide_autonomous=hide_autonomous,
                    include_automation=include_automation,
                    device_id=device_id,
                )
                page = sessions[offset : offset + limit]
                return SessionsListResponse(sessions=page, total=len(sessions), has_real_sessions=bool(sessions))
            if (mode or "lexical") != "lexical":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "search_query_required", "message": "Semantic and hybrid modes require a query."},
                )
            return await asyncio.to_thread(list_live_catalog_sessions, params=params)
        assert db is not None
        owner_id = _owner_id_from_agents_auth(db, _auth)
        result = await list_agent_sessions(db=db, auth=_auth, params=params, owner_id=owner_id)
        if result.headers:
            return JSONResponse(
                content=result.response.model_dump(mode="json"),
                headers=result.headers,
            )

        return result.response
    except SessionListingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CatalogReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list sessions",
        )


@router.get("/sessions/archive-manifest", response_model=SessionArchiveManifestResponse)
def list_archive_manifest(
    include_test: bool = Query(False, description="Include test/e2e sessions in archive enumeration"),
    hide_autonomous: bool = Query(False, description="Hide autonomous sessions from archive enumeration"),
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in archive enumeration when hiding automation",
    ),
    days_back: int = Query(90, ge=1, le=3650, description="Days to look back"),
    limit: int = Query(100, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionArchiveManifestResponse:
    """List sessions for archive sync/backfill without product-surface pagination limits.

    `days_back` applies to recent session activity, not strictly to session start.
    Full-fidelity archival callers should pass `include_test=true` and
    `hide_autonomous=false` explicitly.
    """
    try:
        if database_module.live_catalog_enabled():
            snapshot = timeline_snapshot(
                {
                    "project": None,
                    "provider": None,
                    "environment": None,
                    "include_test": include_test,
                    "hide_autonomous": hide_autonomous,
                    "include_automation": include_automation,
                    "device_id": None,
                    "days_back": days_back,
                    "limit": limit,
                    "offset": offset,
                }
            )
            return build_storage_v2_archive_manifest(snapshot)
        assert db is not None
        since = datetime.now(timezone.utc) - timedelta(days=days_back)
        store = AgentsStore(db)
        sessions, total = store.list_sessions(
            include_test=include_test,
            since=since,
            limit=limit,
            offset=offset,
            hide_autonomous=hide_autonomous,
            include_automation=include_automation,
            anchor_on_activity=True,
        )
        return SessionArchiveManifestResponse(
            sessions=[build_session_archive_manifest_item(db, session) for session in sessions],
            total=total,
        )
    except HTTPException:
        raise
    except CatalogReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except Exception:
        logger.exception("Failed to list archive manifest")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list archive manifest",
        )


@router.get("/sessions/startup-context", response_model=StartupContextResponse)
def get_startup_context(
    project: str = Query(..., description="Project name to get startup continuity for"),
    limit: int = Query(
        STARTUP_CONTEXT_DEFAULT_LIMIT,
        ge=1,
        le=STARTUP_CONTEXT_MAX_LIMIT,
        description="Max recent sessions to include",
    ),
    days_back: int = Query(
        STARTUP_CONTEXT_DEFAULT_DAYS_BACK,
        ge=1,
        le=STARTUP_CONTEXT_MAX_DAYS_BACK,
        description="Days to look back for recent project activity",
    ),
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> StartupContextResponse:
    """Return a small project-scoped continuity block for session-start hooks."""

    if isinstance(_auth, ManagedLocalHookToken):
        if project != _auth.project:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managed-local hook token requires a matching project filter",
            )

    try:
        items = load_startup_context_items(
            db,
            project=project,
            limit=limit,
            days_back=days_back,
        )
        return StartupContextResponse(
            project=str(project).strip(),
            session_count=len(items),
            items=[
                StartupContextItemResponse(
                    session_id=item.session_id,
                    thread_root_session_id=item.thread_root_session_id,
                    provider=item.provider,
                    started_at=item.started_at,
                    age=item.age,
                    summary_title=item.summary_title,
                    summary=item.summary,
                )
                for item in items
            ],
            startup_context=render_startup_context(project, items),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to build startup context")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build startup context",
        )


@router.get("/sessions/summary", response_model=SessionsSummaryResponse)
def list_session_summaries(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    hide_autonomous: bool = Query(
        True,
        description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)",
    ),
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in otherwise default-hidden summaries",
    ),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionsSummaryResponse:
    """List session summaries for picker UI."""
    try:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            query=query,
            limit=limit,
            offset=offset,
            hide_autonomous=hide_autonomous,
            include_automation=include_automation,
            anchor_on_activity=query is None,
        )

        summaries: List[SessionSummaryResponse] = []
        now = datetime.now(timezone.utc)
        for s in sessions:
            started_at = normalize_utc_datetime(s.started_at)
            end_time = normalize_utc_datetime(s.ended_at) or now
            duration_minutes = int((end_time - started_at).total_seconds() / 60) if started_at else None
            turn_count = s.user_messages or 0

            summaries.append(
                SessionSummaryResponse(
                    id=str(s.id),
                    project=s.project,
                    provider=s.provider,
                    cwd=s.cwd,
                    git_branch=s.git_branch,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    duration_minutes=duration_minutes,
                    turn_count=turn_count,
                    last_user_message=_bounded_preview(s.last_user_message_preview, max_len=200),
                    last_ai_message=_bounded_preview(s.last_assistant_message_preview, max_len=200),
                )
            )

        return SessionsSummaryResponse(sessions=summaries, total=total)

    except Exception:
        logger.exception("Failed to list session summaries")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list session summaries",
        )


@router.get("/sessions/wall", response_model=WallResponse)
def wall_query(
    repo: Optional[str] = Query(None, description="Filter by git_repo (substring match)"),
    project: Optional[str] = Query(None, description="Filter by project name"),
    days: int = Query(7, ge=1, le=90, description="Days to look back"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    include_automation: bool = Query(False, description="Include Hatch automation sessions in wall results"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> WallResponse:
    """Wall query: raw signal metadata for sessions on a repo.

    Schema-on-read: returns raw timestamps and facts. The consuming agent
    or UI decides what's relevant — no status bucketing, no pre-computed summaries.
    """
    items = query_wall_sessions(
        db,
        repo=repo,
        project=project,
        days=days,
        limit=limit,
        include_automation=include_automation,
    )
    return WallResponse(sessions=items, total=len(items))


@router.get("/workflows/{workflow_run_id}")
def get_workflow_run(
    workflow_run_id: str,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """Return the subagent threads that belong to a dynamic-workflow run.

    Groups all agents tagged with this ``workflow_run_id`` under their parent
    session and surfaces each agent's attribution labels — the data a UI uses to
    render a workflow as a single collapsible unit.
    """
    store = AgentsStore(db)
    run = store.get_workflow_run(workflow_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow run: {workflow_run_id}")
    return run


@router.get("/sessions/{session_id}/workflows")
def list_session_workflow_runs(
    session_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """List dynamic-workflow runs whose subagent threads live under this session.

    Each entry is one collapsible 'workflow run' node for the session detail UI:
    {workflow_run_id, agent_count, skill}.
    """
    store = AgentsStore(db)
    runs = store.list_workflow_runs_for_session(session_id)
    return {"session_id": str(session_id), "workflow_runs": runs}


@router.get("/sessions/{session_id}/graph")
def get_session_graph(
    session_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """Return provider-neutral child/fork/link graph context for a session."""
    store = AgentsStore(db)
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return build_session_graph_projection(db, session_id)


@router.get("/sessions/{session_id}/tail")
async def session_tail(
    session_id: UUID,
    limit: int = Query(30, ge=1, le=100, description="Number of recent events to return"),
    db: Session | None = Depends(machine_session_read_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """Return the last N events from a session for cross-session reading.

    Tail-biased: fetches the most recent events, then returns them in
    chronological order (oldest first). The reading agent interprets the
    raw log — no summary layer in between.
    """
    if database_module.live_catalog_enabled():
        owner_id = getattr(_auth, "owner_id", None)
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Storage-v2 reads require an owner-scoped token",
            )
        workspace = await build_storage_v2_workspace(
            session_id=session_id,
            owner_id=int(owner_id),
            branch_mode="head",
            limit=limit,
            anchor="tail",
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        projection = workspace["projection"]
        events = [
            {
                "id": event["id"],
                "role": event["role"],
                "content": str(event.get("content_text") or event.get("tool_output_text") or "")[:4000],
                "tool_name": event.get("tool_name"),
                "timestamp": event["timestamp"],
            }
            for item in projection["items"]
            if item.get("kind") == "event"
            and isinstance((event := item.get("event")), dict)
            and event.get("role") in {"user", "assistant", "tool"}
            and (event.get("content_text") is not None or event.get("tool_output_text") is not None)
        ]
        return {"session_id": str(session_id), "events": events, "total": len(events)}

    assert db is not None
    try:
        events = load_session_tail(db, session_id=session_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"session_id": str(session_id), "events": events, "total": len(events)}


@router.get("/sessions/{session_id}/turns", response_model=SessionTurnsListResponse)
async def get_session_turns(
    session_id: UUID,
    limit: int = Query(50, ge=1, le=100, description="Max turns to return"),
    offset: int = Query(0, ge=0, description="Offset within the stable per-session turn order"),
    order: str = Query("asc", description="Turn order: asc|desc"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionTurnsListResponse:
    """List canonical turn timing rows for one session."""
    store = AgentsStore(db)
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if (
        not database_module.archive_database_is_read_only()
        and project_session_capabilities(db, session_id=session.id).managed_transport is not None
    ):
        await execute_session_turn_write(
            db_bind=db.get_bind(),
            label="session-turn-terminal",
            fn=lambda turn_db: materialize_managed_transcript_turns(turn_db, session_id=session.id),
        )

    normalized_order = str(order or "asc").strip().lower()
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="order must be one of: asc, desc",
        )

    turns, total = list_session_turns(
        db,
        session_id=session_id,
        limit=limit,
        offset=offset,
        order=normalized_order,
    )
    return SessionTurnsListResponse(
        turns=[build_session_turn_response(turn) for turn in turns],
        total=total,
    )


@router.get("/sessions/{session_id}/turns/{turn_id}", response_model=SessionTurnEnvelopeResponse)
def get_session_turn_detail(
    session_id: UUID,
    turn_id: int,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionTurnEnvelopeResponse:
    """Get one canonical turn timing row for a session."""
    store = AgentsStore(db)
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    turn = get_session_turn_by_id(db, session_id=session_id, turn_id=turn_id)
    if turn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Turn {turn_id} not found for session {session_id}",
        )

    return SessionTurnEnvelopeResponse(turn=build_session_turn_response(turn))


@router.get("/sessions/active", response_model=ActiveSessionsResponse)
def list_active_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status (working, active, idle, completed)",
    ),
    attention: Optional[str] = Query(None, description="Filter by attention (auto)"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    include_automation: bool = Query(False, description="Include hidden Hatch automation sessions"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ActiveSessionsResponse:
    """Return active/recent session summaries for the live sessions surface."""
    try:
        store = AgentsStore(db)
        now = datetime.now(timezone.utc)
        live_candidate_ids = _active_live_session_candidates(limit=limit, days_back=days_back, now=now)

        if live_candidate_ids is None:
            since = now - timedelta(days=days_back)
            sessions, _total = store.list_sessions(
                project=project,
                provider=None,
                environment=None,
                include_test=False,
                device_id=None,
                since=since,
                query=None,
                limit=limit,
                offset=0,
                exclude_user_states=["archived", "snoozed"],
                anchor_on_activity=True,
            )
        else:
            sessions = []
            unique_live_candidate_ids = list(dict.fromkeys(live_candidate_ids))
            hydrated_live_sessions = store.get_sessions_ordered(unique_live_candidate_ids)
            hydrated_live_ids = {session.id for session in hydrated_live_sessions}
            missing_live_candidate_count = len(
                [session_id for session_id in unique_live_candidate_ids if session_id not in hydrated_live_ids]
            )
            for session in hydrated_live_sessions:
                if not include_automation and int(session.hidden_from_default_timeline or 0) == 1:
                    continue
                if project and session.project != project:
                    continue
                sessions.append(session)
                if len(sessions) >= limit:
                    break
            if missing_live_candidate_count:
                logger.info("Active session catalog included %s archive-missing ids", missing_live_candidate_count)

        session_ids = [s.id for s in sessions]
        last_activity = store.get_last_activity_map(session_ids)
        runtime_state_map = load_runtime_state_map(db, [session.id for session in sessions])
        pause_request_map = load_active_pause_request_map(db, session_ids)
        control_state_map = load_managed_control_state_map(db, [session.id for session in sessions])
        kernel_capabilities_map = project_capabilities_bulk(db, session_ids=session_ids)
        items: List[ActiveSessionResponse] = []
        for s in sessions:
            last_activity_at = normalize_utc_datetime(last_activity.get(s.id) or s.ended_at or s.started_at) or now
            runtime_overlay = resolve_runtime_overlay(
                s,
                last_activity_at=last_activity.get(s.id) or s.ended_at or s.started_at,
                runtime_state_map=runtime_state_map,
                now=now,
            )

            attention_level = "auto"

            if status_filter and runtime_overlay.status != status_filter:
                continue
            if attention and attention_level != attention:
                continue

            items.append(
                build_active_session_response(
                    store,
                    s,
                    last_activity_at=last_activity_at,
                    runtime_overlay=runtime_overlay,
                    attention=attention_level,
                    last_user_message=_bounded_preview(s.last_user_message_preview, max_len=300),
                    last_assistant_message=_bounded_preview(s.last_assistant_message_preview, max_len=300),
                    now=now,
                    control_overlay=control_state_map.get(s.id),
                    kernel_capabilities=kernel_capabilities_map.get(s.id),
                    pause_request=serialize_pause_request_projection(pause_request_map.get(s.id)),
                )
            )

        return ActiveSessionsResponse(
            sessions=items,
            total=len(items),
            last_refresh=now,
        )

    except Exception:
        logger.exception("Failed to list active sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list active sessions",
        )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreviewResponse)
def preview_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionPreviewResponse:
    """Get a preview of a session's recent messages."""
    store = AgentsStore(db)
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    events = store.get_session_preview(session_id, last_n)
    messages = [
        SessionPreviewMessage(
            role=e.role,
            content=e.content_text or "",
            timestamp=e.timestamp,
        )
        for e in events
    ]
    total_messages = (session.user_messages or 0) + (session.assistant_messages or 0)

    return SessionPreviewResponse(
        id=str(session_id),
        messages=messages,
        total_messages=total_messages,
    )


@router.get("/filters", response_model=FiltersResponse)
def get_filters(
    response: Response,
    days_back: int = Query(90, ge=1, le=365, description="Days to look back for distinct values"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> FiltersResponse:
    """Get distinct filter values for UI dropdowns."""
    try:
        store = AgentsStore(db)
        timing = ServerTimingRecorder()
        with timing.span("distinct_filters"):
            filters = store.get_distinct_filters(days_back=days_back)
        timing.apply(response)
        return FiltersResponse(
            projects=filters["projects"],
            providers=filters["providers"],
            machines=filters["machines"],
        )
    except Exception:
        logger.exception("Failed to get filters")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get filters",
        )


@router.post("/sessions/{session_id}/action", response_model=SessionActionResponse)
async def set_session_action(
    session_id: UUID,
    body: SessionActionRequest,
    db: Session | None = Depends(session_preferences_db_dependency),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionActionResponse:
    """Set user-driven bucket state for a session (park/snooze/archive/resume)."""
    action_to_state = {"park": "parked", "snooze": "snoozed", "archive": "archived", "resume": "active"}
    if body.action not in action_to_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{body.action}'. Must be one of: {', '.join(sorted(action_to_state))}",
        )

    new_state = action_to_state[body.action]
    if database_module.live_catalog_enabled():
        from zerg.services.session_preferences import update_session_preferences

        preferences = await update_session_preferences(session_id, user_state=new_state)
        if preferences is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return SessionActionResponse(session_id=str(session_id), user_state=preferences.user_state)

    assert db is not None
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session.user_state = new_state
    session.user_state_at = datetime.now(timezone.utc)
    db.commit()

    return SessionActionResponse(session_id=str(session_id), user_state=new_state)


@router.patch("/sessions/{session_id}/loop-mode", response_model=SessionLoopModeResponse)
async def set_session_loop_mode(
    session_id: UUID,
    body: SessionLoopModeRequest,
    db: Session | None = Depends(session_preferences_db_dependency),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionLoopModeResponse:
    """Set the explicit loop mode for a coding session."""
    if database_module.live_catalog_enabled():
        from zerg.services.session_preferences import update_session_preferences

        preferences = await update_session_preferences(session_id, loop_mode=body.loop_mode.value)
        if preferences is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return SessionLoopModeResponse(session_id=str(session_id), loop_mode=preferences.loop_mode)

    assert db is not None
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session.loop_mode = body.loop_mode.value
    db.commit()

    return SessionLoopModeResponse(session_id=str(session_id), loop_mode=body.loop_mode)


@router.patch("/sessions/{session_id}/notification-watch", response_model=SessionNotificationWatchResponse)
async def set_session_notification_watch(
    session_id: UUID,
    body: SessionNotificationWatchRequest,
    db: Session | None = Depends(session_preferences_db_dependency),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionNotificationWatchResponse:
    """Mute or unmute session attention notifications."""
    if database_module.live_catalog_enabled():
        from zerg.services.session_preferences import update_session_preferences

        preferences = await update_session_preferences(
            session_id,
            notification_muted=bool(body.notification_muted),
        )
        if preferences is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return SessionNotificationWatchResponse(
            session_id=str(session_id),
            notification_muted=preferences.notification_muted,
        )

    assert db is not None
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session.notification_muted = bool(body.notification_muted)
    db.commit()

    return SessionNotificationWatchResponse(
        session_id=str(session_id),
        notification_muted=bool(session.notification_muted),
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: UUID,
    response: Response,
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
    owner_id: int | None = Depends(_no_viewer_owner_id),
) -> SessionResponse:
    """Get a single session by ID."""
    if database_module.live_catalog_enabled():
        try:
            result, provider_session_id, commit_seq = read_live_catalog_session(session_id)
        except CatalogReadError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )
        response.headers["X-Catalog-Commit-Seq"] = commit_seq
        if provider_session_id:
            response.headers["X-Provider-Session-ID"] = provider_session_id
        return result

    assert db is not None
    store = AgentsStore(db)
    timing = ServerTimingRecorder()

    with timing.span("load_session"):
        session = store.get_session(session_id)

    if not session:
        effective_owner_id = owner_id
        if effective_owner_id is None:
            effective_owner_id = _owner_id_from_agents_auth(db, _auth)
        placeholder = _live_launch_placeholder_for_owner(
            session_id,
            owner_id=effective_owner_id,
            now=datetime.now(timezone.utc),
        )
        if placeholder is not None:
            timing.apply(response)
            return placeholder
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    with timing.span("load_activity"):
        activity_map = store.get_last_activity_map([session.id])
    with timing.span("load_first_user"):
        first_user_map = store.get_first_message_map([session.id], role="user", max_len=80)
    now = datetime.now(timezone.utc)
    with timing.span("load_runtime"):
        runtime_state_map = load_runtime_state_map(db, [session.id])
        pause_request_map = load_active_pause_request_map(db, [session.id])
        transcript_preview_map = load_active_provisional_preview_map(db, [session.id])
        pending_response_turn_map = load_pending_response_turn_map(db, [session.id])
        control_state_map = load_managed_control_state_map(db, [session.id])
        launch_attempt_map = latest_launch_attempts(db, [session.id])
        hot_projection_map = load_hot_session_projection_map([session.id])
    with timing.span("build_response"):
        effective_owner_id = owner_id
        if effective_owner_id is None:
            effective_owner_id = _owner_id_from_agents_auth(db, _auth)
        result = build_session_response(
            store,
            session,
            last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
            runtime_overlay=resolve_runtime_overlay(
                session,
                last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
                runtime_state_map=runtime_state_map,
                now=now,
            ),
            first_user_message=first_user_map.get(session.id),
            control_overlay=control_state_map.get(session.id),
            transcript_preview=transcript_preview_map.get(str(session.id)),
            owner_id=effective_owner_id,
            has_pending_response_turn=bool(pending_response_turn_map.get(session.id)),
            pause_request=(
                hot_projection_map[session.id][0]
                if session.id in hot_projection_map
                else serialize_pause_request_projection(pause_request_map.get(session.id))
            ),
            archive_state=(hot_projection_map[session.id][1] if session.id in hot_projection_map else "current"),
            launch_attempt=launch_attempt_map.get(session.id),
        )
    # Expose the provider-native id (when bound) so binding-convergence tooling
    # can group sessions by it; the list endpoint does not carry it. Mirrors the
    # export path's X-Provider-Session-ID header.
    provider_session_id = project_provider_session_id(db, session)
    if provider_session_id:
        response.headers["X-Provider-Session-ID"] = provider_session_id
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/thread", response_model=SessionThreadResponse)
async def get_session_thread(
    session_id: UUID,
    response: Response,
    db: Session | None = Depends(machine_session_read_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
    owner_id: int | None = Depends(_no_viewer_owner_id),
) -> SessionThreadResponse:
    """Get all concrete continuations in a logical thread."""
    if database_module.live_catalog_enabled():
        effective_owner_id = owner_id if owner_id is not None else getattr(_auth, "owner_id", None)
        if effective_owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Storage-v2 reads require an owner-scoped token",
            )
        workspace = await build_storage_v2_workspace(
            session_id=session_id,
            owner_id=int(effective_owner_id),
            branch_mode="head",
            limit=1,
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return workspace["thread"]

    assert db is not None
    store = AgentsStore(db)
    timing = ServerTimingRecorder()

    with timing.span("load_session"):
        session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    with timing.span("load_thread"):
        thread_sessions = store.list_thread_sessions(session)
    with timing.span("load_head"):
        head = store.get_thread_head(session)
    thread_session_ids = [item.id for item in thread_sessions]
    with timing.span("load_activity"):
        activity_map = store.get_last_activity_map(thread_session_ids)
    with timing.span("load_first_user"):
        first_user_map = store.get_first_message_map(thread_session_ids, role="user", max_len=80)
    thread_cache = store.batch_thread_meta(thread_sessions)
    now = datetime.now(timezone.utc)
    with timing.span("load_runtime"):
        runtime_state_map = load_runtime_state_map(db, [item.id for item in thread_sessions])
        pause_request_map = load_active_pause_request_map(db, thread_session_ids)
        transcript_preview_map = load_active_provisional_preview_map(db, [item.id for item in thread_sessions])
        pending_response_turn_map = load_pending_response_turn_map(db, thread_session_ids)
        control_state_map = load_managed_control_state_map(db, [item.id for item in thread_sessions])
        launch_attempt_map = latest_launch_attempts(db, thread_session_ids)

    with timing.span("build_response"):
        effective_owner_id = owner_id
        if effective_owner_id is None:
            effective_owner_id = _owner_id_from_agents_auth(db, _auth)
        result = SessionThreadResponse(
            root_session_id=project_session_lineage_fields(db, session).thread_root_session_id,
            head_session_id=str(head.id if head else session.id),
            sessions=[
                build_session_response(
                    store,
                    item,
                    thread_cache=thread_cache,
                    last_activity_at=activity_map.get(item.id) or item.ended_at or item.started_at,
                    runtime_overlay=resolve_runtime_overlay(
                        item,
                        last_activity_at=activity_map.get(item.id) or item.ended_at or item.started_at,
                        runtime_state_map=runtime_state_map,
                        now=now,
                    ),
                    first_user_message=first_user_map.get(item.id),
                    transcript_preview=transcript_preview_map.get(str(item.id)),
                    control_overlay=control_state_map.get(item.id),
                    owner_id=effective_owner_id,
                    has_pending_response_turn=bool(pending_response_turn_map.get(item.id)),
                    pause_request=serialize_pause_request_projection(pause_request_map.get(item.id)),
                    launch_attempt=launch_attempt_map.get(item.id),
                )
                for item in thread_sessions
            ],
        )
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/events", response_model=EventsListResponse)
async def get_session_events(
    session_id: UUID,
    thread_id: Optional[UUID] = Query(
        None,
        description="Thread lane to inspect; defaults to the primary session thread",
    ),
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    tool_name: Optional[str] = Query(None, description="Exact tool name filter, e.g. Bash"),
    query: Optional[str] = Query(None, description="Content search within session events"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next page"),
    db: Session | None = Depends(machine_session_read_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> EventsListResponse:
    """Get events for a session."""
    if database_module.live_catalog_enabled():
        if offset:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Storage-v2 event pagination uses cursor instead of offset",
            )
        owner_id = getattr(_auth, "owner_id", None)
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Storage-v2 reads require an owner-scoped token",
            )
        workspace = await build_storage_v2_workspace(
            session_id=session_id,
            owner_id=int(owner_id),
            branch_mode=branch_mode,
            limit=limit,
            cursor=cursor,
            anchor=anchor,
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        projection = workspace["projection"]
        role_filter = {value.strip() for value in roles.split(",") if value.strip()} if roles else None
        events = [item["event"] for item in projection["items"] if item.get("kind") == "event" and item.get("event")]
        if role_filter is not None:
            events = [event for event in events if event.get("role") in role_filter]
        if tool_name is not None:
            events = [event for event in events if event.get("tool_name") == tool_name]
        if query is not None:
            needle = query.casefold()
            events = [event for event in events if needle in str(event.get("content_text") or "").casefold()]
        return EventsListResponse(
            events=events,
            total=int(projection["total"]),
            branch_mode=branch_mode,
            abandoned_events=int(projection["abandoned_events"]),
            generation_id=projection.get("generation_id"),
            next_cursor=projection.get("next_cursor"),
            has_more=projection.get("has_more") is True,
        )

    assert db is not None
    store = AgentsStore(db)

    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    role_list = [r.strip() for r in roles.split(",")] if roles else None
    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )
    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )
    if anchor not in {"start", "tail"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="anchor must be one of: start, tail",
        )

    events = store.get_session_events(
        session_id,
        thread_id=thread_id,
        roles=role_list,
        tool_name=tool_name,
        query=query,
        context_mode=context_mode,
        branch_mode=branch_mode,
        limit=limit,
        offset=offset,
        load_from_end=anchor == "tail",
    )
    boundary = store.get_active_context_boundary(session_id, branch_mode=branch_mode)
    head_branch_id = store.get_head_branch_id(session_id)
    input_origin_map = build_event_input_origin_map(store, events)
    media_ref_map = build_event_media_ref_map(db, events)
    tool_call_state_map = build_tool_call_state_map(
        store.get_tool_call_pairing_events_for_page(
            session_id,
            events,
            thread_id=thread_id,
            branch_mode=branch_mode,
        ),
        session_closed=is_session_closed(session),
    )

    total = store.count_session_events(
        session_id,
        thread_id=thread_id,
        roles=role_list,
        tool_name=tool_name,
        query=query,
        context_mode=context_mode,
        branch_mode=branch_mode,
    )
    abandoned_events = 0
    if branch_mode == "head":
        forensic_total = store.count_session_events(
            session_id,
            thread_id=thread_id,
            roles=role_list,
            tool_name=tool_name,
            query=query,
            context_mode=context_mode,
            branch_mode="all",
        )
        abandoned_events = max(0, forensic_total - total)

    return EventsListResponse(
        events=[
            build_event_response(
                store,
                e,
                boundary=boundary,
                head_branch_id=head_branch_id,
                input_origin_map=input_origin_map,
                tool_call_state_map=tool_call_state_map,
                media_ref_map=media_ref_map,
            )
            for e in events
        ],
        total=total,
        branch_mode=branch_mode,
        abandoned_events=abandoned_events,
    )


@router.get("/sessions/{session_id}/projection", response_model=SessionProjectionResponse)
async def get_session_projection(
    session_id: UUID,
    response: Response,
    thread_id: Optional[UUID] = Query(
        None,
        description="Thread lane to project; defaults to the primary session thread",
    ),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next page"),
    db: Session | None = Depends(machine_session_read_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionProjectionResponse:
    """Get the stitched lineage-path projection for a focused session."""
    if database_module.live_catalog_enabled():
        if offset:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Storage-v2 projection pagination uses cursor instead of offset",
            )
        owner_id = getattr(_auth, "owner_id", None)
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Storage-v2 reads require an owner-scoped token",
            )
        workspace = await build_storage_v2_workspace(
            session_id=session_id,
            owner_id=int(owner_id),
            branch_mode=branch_mode,
            limit=limit,
            cursor=cursor,
            anchor=anchor,
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return workspace["projection"]

    assert db is not None
    store = AgentsStore(db)
    timing = ServerTimingRecorder()

    with timing.span("load_session"):
        session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )
    if anchor not in {"start", "tail"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="anchor must be one of: start, tail",
        )

    with timing.span("load_projection"):
        projection = store.get_session_projection_page(
            session,
            thread_id=thread_id,
            branch_mode=branch_mode,
            limit=limit,
            offset=offset,
            load_from_end=anchor == "tail",
        )
    with timing.span("load_head"):
        head = store.get_thread_head(session)
    active_context_boundary_cache: dict[UUID, int | None] = {}
    head_branch_id_cache: dict[UUID, int | None] = {}
    input_origin_map = build_event_input_origin_map(
        store,
        [item.event for item in projection.items if item.kind == "event" and item.event is not None],
    )

    sessions_by_id: dict[UUID, Any] = {}
    for item in projection.items:
        if item.kind == "event" and item.event is not None:
            sessions_by_id.setdefault(item.session.id, item.session)
    tool_call_state_map: dict[int, Any] = {}
    for sid, path_session in sessions_by_id.items():
        page_events = [
            item.event for item in projection.items if item.kind == "event" and item.event is not None and item.session.id == sid
        ]
        tool_call_state_map.update(
            build_tool_call_state_map(
                store.get_tool_call_pairing_events_for_page(
                    sid,
                    page_events,
                    thread_id=thread_id if sid == session.id else None,
                    branch_mode=branch_mode,
                ),
                session_closed=is_session_closed(path_session),
            )
        )

    def get_boundary(current_session_id: UUID) -> int | None:
        if current_session_id not in active_context_boundary_cache:
            active_context_boundary_cache[current_session_id] = store.get_active_context_boundary(
                current_session_id,
                branch_mode=branch_mode,
            )
        return active_context_boundary_cache[current_session_id]

    def get_head_branch_id(current_session_id: UUID) -> int | None:
        if current_session_id not in head_branch_id_cache:
            head_branch_id_cache[current_session_id] = store.get_head_branch_id(current_session_id)
        return head_branch_id_cache[current_session_id]

    with timing.span("build_response"):
        items: list[SessionProjectionItemResponse] = []
        media_ref_map = build_event_media_ref_map(
            db,
            [item.event for item in projection.items if item.kind == "event" and item.event is not None],
        )
        for item in projection.items:
            if item.kind == "event" and item.event is not None:
                action = build_session_action_response(item.event)
                if action is not None:
                    items.append(
                        SessionProjectionItemResponse(
                            kind="action",
                            session_id=str(item.session.id),
                            timestamp=item.event.timestamp,
                            action=action,
                        )
                    )
                    continue
                items.append(
                    SessionProjectionItemResponse(
                        kind="event",
                        session_id=str(item.session.id),
                        timestamp=item.event.timestamp,
                        event=build_event_response(
                            store,
                            item.event,
                            boundary=get_boundary(item.session.id),
                            head_branch_id=get_head_branch_id(item.session.id),
                            input_origin_map=input_origin_map,
                            tool_call_state_map=tool_call_state_map,
                            media_ref_map=media_ref_map,
                        ),
                    )
                )
                continue

            items.append(_build_projection_seam_response(db=db, item=item))

        result = SessionProjectionResponse(
            root_session_id=project_session_lineage_fields(db, session).thread_root_session_id,
            focus_session_id=str(session.id),
            head_session_id=str(head.id if head else session.id),
            path_session_ids=[str(path_session.id) for path_session in projection.path_sessions],
            items=items,
            total=projection.total,
            page_offset=projection.page_offset,
            branch_mode=projection.branch_mode,
            abandoned_events=projection.abandoned_events,
        )
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/workspace")
async def get_session_workspace(
    session_id: UUID,
    response: Response,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next older page"),
    legacy_session_factory=Depends(get_legacy_workspace_session_factory),
    _auth: DeviceToken | ManagedLocalHookToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionWorkspaceResponse | dict[str, object]:
    """Get the focused session, its thread, and the first projection page in one round trip."""
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "no-store"
    owner_value = getattr(_auth, "owner_id", None)
    if owner_value is None and not get_settings().testing:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Storage-v2 reads require an owner-scoped token",
        )
    storage_workspace = None
    if owner_value is not None:
        storage_workspace = await build_storage_v2_workspace(
            session_id=session_id,
            owner_id=int(owner_value),
            branch_mode=branch_mode,
            limit=limit,
            cursor=cursor,
        )
    if storage_workspace is None and not get_settings().testing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    if storage_workspace is None:

        def build_legacy_workspace() -> SessionWorkspaceResponse:
            with legacy_session_factory() as db:
                owner_id = _resolve_agents_owner_id(db, _auth if isinstance(_auth, DeviceToken) else None)
                return build_session_workspace(
                    db=db,
                    session_id=session_id,
                    branch_mode=branch_mode,
                    limit=limit,
                    timing=timing,
                    owner_id=owner_id,
                )

        result = await asyncio.to_thread(build_legacy_workspace)
        timing.apply(response)
        return result
    timing.apply(response)
    return storage_workspace


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode for export: head|all"),
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> Response:
    """Export session as JSONL for Claude Code --resume."""
    if database_module.live_catalog_enabled():
        owner_id = getattr(_auth, "owner_id", None)
        if owner_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner identity is required")
        return await build_storage_v2_raw_export(
            session_id=session_id,
            owner_id=int(owner_id),
            branch_mode=branch_mode,
        )
    assert db is not None
    store = AgentsStore(db)
    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )

    try:
        result = store.export_session_jsonl(session_id, branch_mode=branch_mode)
    except ArchiveTranscriptUnavailable as exc:
        # Fail closed: raw bytes for a known source line are missing from both the
        # monolith and the archive. Surface 503 rather than a truncated transcript.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Transcript raw bytes unavailable for session {session_id}: {exc}",
        )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    jsonl_bytes, session = result

    provider_session_id = project_provider_session_id(db, session)

    headers = {
        "Content-Disposition": f"attachment; filename={session_id}.jsonl",
        "X-Session-CWD": session.cwd or "",
        "X-Session-Provider": session.provider,
        "X-Session-Project": session.project or "",
        "X-Session-Branch-Mode": branch_mode,
    }
    if provider_session_id:
        headers["X-Provider-Session-ID"] = provider_session_id

    return Response(
        content=jsonl_bytes,
        media_type="application/x-ndjson",
        headers=headers,
    )


@router.get("/sessions/{session_id}/archive-bundle", response_model=SessionArchiveBundleResponse)
async def export_session_archive_bundle(
    session_id: UUID,
    branch_mode: str = Query("head", description="Archive bundle branch projection mode. v1 supports head only."),
    db: Session | None = Depends(session_detail_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionArchiveBundleResponse:
    """Export a versioned archive bundle for the current session head."""
    if branch_mode != "head":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be 'head' for archive bundle export",
        )

    try:
        if database_module.live_catalog_enabled():
            owner_id = getattr(_auth, "owner_id", None)
            if owner_id is None:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Archive bundle requires an owner-bound token")
            result = await build_storage_v2_archive_bundle(
                session_id=session_id,
                owner_id=int(owner_id),
                branch_mode=branch_mode,
            )
        else:
            assert db is not None
            result = build_session_archive_bundle(db, session_id, branch_mode=branch_mode)
    except (ArchiveTranscriptUnavailable, RawObjectWorkerError, RawObjectCorruptError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Transcript raw bytes unavailable for session {session_id}: {exc}",
        )
    except (CatalogRemoteError, CatalogUnavailable, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Archive catalog unavailable for session {session_id}: {exc}",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    return result


class SessionMessageCreate(UTCBaseModel):
    """Create a directed message from one session to another."""

    from_session_id: UUID | None = None
    to_session_id: UUID
    text: str
    source_event_id: Optional[int] = None


class SessionMessageAcknowledge(UTCBaseModel):
    """Acknowledge an inbound session message."""

    session_id: UUID | None = None


@router.post("/messages", status_code=status.HTTP_201_CREATED)
async def create_message(
    request: Request,
    payload: SessionMessageCreate,
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """Create a directed session message and attempt delivery when safe."""
    sender_session = _resolve_message_actor_session(
        db=db,
        request=request,
        token=_auth,
        declared_session_id=payload.from_session_id,
    )
    try:
        outcome = await create_session_message(
            db=db,
            owner_id=resolve_session_message_owner_id(db, _auth),
            from_session_id=sender_session.id,
            to_session_id=payload.to_session_id,
            text=payload.text[:4000],
            source_event_id=payload.source_event_id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_404_NOT_FOUND if detail.endswith("not found") else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return serialize_session_message(outcome.message, delivery_status=outcome.delivery_status)


@router.get("/messages")
async def list_messages(
    request: Request,
    session_id: UUID | None = Query(None, description="Session ID to inspect messages for"),
    direction: str = Query("inbound", description="Message direction: inbound|outbound|all"),
    unacknowledged_only: bool = Query(False, description="Only include messages without acknowledged_at"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """List durable session messages without mutating delivery or ack state."""
    if direction not in {"inbound", "outbound", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="direction must be inbound, outbound, or all",
        )

    actor_session = _resolve_message_actor_session(
        db=db,
        request=request,
        token=_auth,
        declared_session_id=session_id,
    )
    resolved_session_id = actor_session.id

    messages = list_session_messages(
        db,
        session_id=resolved_session_id,
        direction=direction,
        unacknowledged_only=unacknowledged_only,
        limit=limit,
    )
    return {
        "messages": [serialize_session_message(message) for message in messages],
        "total": len(messages),
    }


@router.post("/messages/{message_id}/ack")
async def acknowledge_message(
    message_id: int,
    request: Request,
    payload: SessionMessageAcknowledge | None = None,
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict:
    """Acknowledge that the target session has handled a delivered message."""
    actor_session = _resolve_message_actor_session(
        db=db,
        request=request,
        token=_auth,
        declared_session_id=payload.session_id if payload is not None else None,
    )
    try:
        message = acknowledge_session_message_for_session(
            db,
            message_id=message_id,
            target_session_id=actor_session.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_session_message(message)
