"""Timeline list projection served entirely from the bounded live catalog."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from time import monotonic
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_capabilities_from_rows
from zerg.services.live_launch_readiness import LiveLaunchReadinessView
from zerg.services.live_launch_readiness import latest_live_launch_readiness_map
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_runtime import build_fallback_runtime_view
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime_display import TRANSCRIPT_SYNC_DISPLAY_WINDOW
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.services.session_views import build_live_launch_placeholder_response
from zerg.services.session_views import build_session_capabilities_response
from zerg.services.session_views import build_session_runtime_display_response
from zerg.services.session_views import build_session_timeline_card_response
from zerg.services.timeline_session_listing import TimelineSessionCardResponse
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import TimelineSessionsListResponse
from zerg.utils.time import normalize_utc


def _title(session: LiveSessionCatalog, card: LiveTimelineCard) -> str:
    for value in (
        session.anchor_title,
        card.summary_title,
        session.summary_title,
        card.first_user_message_preview,
        session.first_user_message_preview,
        session.project,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized[:255]
    return f"{session.provider.title()} session"


def _pending_response_from_catalog(
    session: LiveSessionCatalog,
    card: LiveTimelineCard,
    *,
    readiness: LiveLaunchReadinessView,
    runtime: LiveRuntimeState | None,
):
    response = build_live_launch_placeholder_response(readiness)
    title = _title(session, card)
    ended_at = normalize_utc(session.ended_at)
    last_activity_at = normalize_utc(card.last_activity_at) or normalize_utc(session.last_activity_at) or response.started_at
    capability_label = "Completed" if ended_at is not None else ("Live" if runtime is not None else "Observe")
    capability_detail = (
        "Session history is available; cold detail may still be catching up."
        if ended_at is not None
        else "Live catalog is available; transcript detail is served by the archive worker."
    )
    capabilities = response.capabilities.model_copy(
        update={
            "display_label": capability_label,
            "display_detail": capability_detail,
            "composer_disabled_reason": None if runtime is not None and ended_at is None else "Read-only catalog view.",
            "staleness_reason": "archive_catching_up" if card.archive_state != "current" else None,
        }
    )
    runtime_updates = {
        "headline": title,
        "detail": capability_detail,
        "phase_label": "Completed" if ended_at is not None else (str(runtime.phase).title() if runtime is not None else "Recent"),
        "is_live": bool(runtime is not None and ended_at is None),
        "is_executing": bool(runtime is not None and runtime.phase in {"thinking", "running"}),
        "is_idle": bool(ended_at is None and (runtime is None or runtime.phase not in {"thinking", "running"})),
    }
    runtime_display = response.runtime_display.model_copy(update=runtime_updates)
    launch_state = readiness.launch_state
    execution_lifetime = readiness.execution_lifetime
    return response.model_copy(
        update={
            "provider": session.provider,
            "project": session.project,
            "device_id": session.device_id,
            "environment": session.environment,
            "cwd": session.cwd,
            "git_repo": session.git_repo,
            "git_branch": session.git_branch,
            "started_at": normalize_utc(session.started_at) or response.started_at,
            "ended_at": ended_at,
            "user_messages": int(card.user_messages or 0),
            "assistant_messages": int(card.assistant_messages or 0),
            "tool_calls": int(card.tool_calls or 0),
            "last_activity_at": last_activity_at,
            "timeline_anchor_at": normalize_utc(runtime.timeline_anchor_at) if runtime is not None else last_activity_at,
            "runtime_phase": str(runtime.phase) if runtime is not None else None,
            "phase_started_at": normalize_utc(runtime.phase_started_at) if runtime is not None else None,
            "last_progress_at": normalize_utc(runtime.last_progress_at) if runtime is not None else None,
            "runtime_source": "live_catalog",
            "terminal_state": str(runtime.terminal_state) if runtime is not None and runtime.terminal_state else None,
            "runtime_version": int(runtime.runtime_version or 0) if runtime is not None else None,
            "status": "completed" if ended_at is not None else (str(runtime.phase) if runtime is not None else "active"),
            "display_phase": runtime_display.phase_label,
            "active_tool": str(runtime.active_tool) if runtime is not None and runtime.active_tool else None,
            "summary": session.summary,
            "summary_title": card.summary_title or session.summary_title,
            "anchor_title": session.anchor_title,
            "timeline_title": title,
            "title_state": "ready" if session.anchor_title or card.summary_title else "degraded",
            "title_source": "ai" if session.anchor_title or card.summary_title else "project",
            "summary_status": "ready" if session.summary else "unavailable",
            "first_user_message": card.first_user_message_preview or session.first_user_message_preview,
            "thread_root_session_id": str(session.session_id),
            "thread_head_session_id": str(session.session_id),
            "thread_continuation_count": 1,
            "origin_label": session.environment,
            "home_label": session.device_name or session.device_id,
            "is_writable_head": ended_at is None,
            "capabilities": capabilities,
            "runtime_display": runtime_display,
            "loop_mode": session.loop_mode or "assist",
            "user_state": session.user_state or "active",
            "launch_state": launch_state,
            "execution_lifetime": execution_lifetime,
            "launch_error_code": readiness.launch_error_code,
            "launch_error_message": readiness.launch_error_message,
        }
    )


def _pending_placeholder_is_current(readiness: LiveLaunchReadinessView | None, *, now: datetime) -> bool:
    if readiness is None:
        return False
    if readiness.launch_state in {"launch_failed", "launch_orphaned"}:
        return True
    updated_at = normalize_utc(readiness.updated_at)
    return updated_at is not None and now - updated_at <= TRANSCRIPT_SYNC_DISPLAY_WINDOW


def _response_from_catalog(
    session: LiveSessionCatalog,
    card: LiveTimelineCard,
    *,
    readiness: LiveLaunchReadinessView | None,
    runtime: LiveRuntimeState | None,
    capability_flags: KernelSessionCapabilities,
    now: datetime,
) -> SessionResponse:
    if card.archive_state == "pending" and _pending_placeholder_is_current(readiness, now=now):
        assert readiness is not None
        return _pending_response_from_catalog(
            session,
            card,
            readiness=readiness,
            runtime=runtime,
        )

    started_at = normalize_utc(session.started_at) or now
    ended_at = normalize_utc(session.ended_at)
    last_activity_at = normalize_utc(card.last_activity_at) or normalize_utc(session.last_activity_at) or started_at
    runtime_overlay = build_runtime_view(state=runtime, session=session, now=now) if runtime is not None else None
    display_runtime_overlay = runtime_overlay or build_fallback_runtime_view(
        session=session,
        last_activity_at=last_activity_at,
        now=now,
    )
    runtime_display = build_session_runtime_display_response(
        runtime_overlay=display_runtime_overlay,
        capability_flags=capability_flags,
        ended_at=ended_at,
        last_activity_at=last_activity_at,
        user_messages=int(card.user_messages or 0),
        assistant_messages=int(card.assistant_messages or 0),
        now=now,
    )
    capabilities = build_session_capabilities_response(
        session=session,
        capability_flags=capability_flags,
        runtime_display=runtime_display,
        kernel_capabilities=capability_flags,
    )
    title = _title(session, card)
    return SessionResponse(
        id=str(session.session_id),
        provider=session.provider,
        project=session.project,
        device_id=session.device_id,
        environment=session.environment,
        cwd=session.cwd,
        git_repo=session.git_repo,
        git_branch=session.git_branch,
        started_at=started_at,
        ended_at=ended_at,
        user_messages=int(card.user_messages or 0),
        assistant_messages=int(card.assistant_messages or 0),
        tool_calls=int(card.tool_calls or 0),
        last_activity_at=last_activity_at,
        timeline_anchor_at=display_runtime_overlay.timeline_anchor_at,
        runtime_phase=runtime_overlay.runtime_phase if runtime_overlay is not None else None,
        phase_started_at=runtime_overlay.phase_started_at if runtime_overlay is not None else None,
        last_progress_at=runtime_overlay.last_progress_at if runtime_overlay is not None else None,
        runtime_source=runtime_overlay.runtime_source if runtime_overlay is not None else None,
        terminal_state=runtime_overlay.terminal_state if runtime_overlay is not None else None,
        runtime_version=runtime_overlay.runtime_version if runtime_overlay is not None else None,
        status=runtime_overlay.status if runtime_overlay is not None else None,
        presence_state=runtime_overlay.presence_state if runtime_overlay is not None else None,
        presence_tool=runtime_overlay.presence_tool if runtime_overlay is not None else None,
        presence_updated_at=runtime_overlay.presence_updated_at if runtime_overlay is not None else None,
        last_live_at=runtime_overlay.last_live_at if runtime_overlay is not None else None,
        display_phase=runtime_overlay.display_phase if runtime_overlay is not None else None,
        active_tool=runtime_overlay.active_tool if runtime_overlay is not None else None,
        confidence=runtime_overlay.confidence if runtime_overlay is not None else None,
        summary=session.summary,
        summary_title=card.summary_title or session.summary_title,
        anchor_title=session.anchor_title,
        timeline_title=title,
        title_state="ready" if session.anchor_title or card.summary_title else "degraded",
        title_source="ai" if session.anchor_title or card.summary_title else "project",
        summary_status="ready" if session.summary else "unavailable",
        first_user_message=card.first_user_message_preview or session.first_user_message_preview,
        thread_root_session_id=str(session.session_id),
        thread_head_session_id=str(session.session_id),
        thread_continuation_count=1,
        origin_label=session.environment,
        home_label=capability_flags.home_label,
        is_writable_head=runtime_display.lifecycle != "closed",
        capabilities=capabilities,
        runtime_display=runtime_display,
        timeline_card=build_session_timeline_card_response(
            runtime_view=display_runtime_overlay,
            runtime_display=runtime_display,
        ),
        loop_mode=session.loop_mode or "assist",
        user_state=session.user_state or "active",
        launch_state=readiness.launch_state if readiness is not None else None,
        execution_lifetime=readiness.execution_lifetime if readiness is not None else None,
        launch_error_code=readiness.launch_error_code if readiness is not None else None,
        launch_error_message=readiness.launch_error_message if readiness is not None else None,
    )


def _live_capability_map(
    db: Session,
    *,
    session_ids: list[str],
    now: datetime,
) -> dict[str, KernelSessionCapabilities]:
    if not session_ids:
        return {}

    threads = (
        db.query(LiveSessionThread)
        .filter(LiveSessionThread.session_id.in_(session_ids), LiveSessionThread.is_primary == 1)
        .order_by(LiveSessionThread.created_at.asc(), LiveSessionThread.id.asc())
        .all()
    )
    thread_by_session: dict[str, LiveSessionThread] = {}
    for thread in threads:
        thread_by_session.setdefault(str(thread.session_id), thread)

    thread_ids = [thread.id for thread in thread_by_session.values()]
    runs_by_thread: dict[str, LiveSessionRun] = {}
    if thread_ids:
        runs = (
            db.query(LiveSessionRun)
            .filter(LiveSessionRun.thread_id.in_(thread_ids))
            .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
            .all()
        )
        for run in runs:
            runs_by_thread.setdefault(str(run.thread_id), run)

    run_ids = [run.id for run in runs_by_thread.values()]
    connections_by_run: dict[str, list[LiveSessionConnection]] = defaultdict(list)
    if run_ids:
        connections = db.query(LiveSessionConnection).filter(LiveSessionConnection.run_id.in_(run_ids)).all()
        for connection in connections:
            connections_by_run[str(connection.run_id)].append(connection)

    projections: dict[str, KernelSessionCapabilities] = {}
    for session_id in session_ids:
        thread = thread_by_session.get(session_id)
        run = runs_by_thread.get(str(thread.id)) if thread is not None else None
        connections = connections_by_run.get(str(run.id), []) if run is not None else []
        projections[session_id] = project_capabilities_from_rows(
            session_id=session_id,
            thread=thread,
            latest_run=run,
            connections=connections,
            now=now,
        )
    return projections


def list_live_catalog_timeline(
    db: Session,
    *,
    params: TimelineSessionListParams,
) -> TimelineSessionsListResponse:
    """List timeline cards without opening the cold database."""

    if params.query is not None or (params.mode or "lexical") != "lexical":
        raise ValueError("search_requires_archive")
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=params.days_back)
    query = db.query(LiveTimelineCard, LiveSessionCatalog).join(
        LiveSessionCatalog,
        LiveSessionCatalog.session_id == LiveTimelineCard.session_id,
    )
    if params.project:
        query = query.filter(LiveTimelineCard.project == params.project)
    if params.provider:
        query = query.filter(LiveTimelineCard.provider == params.provider)
    if params.environment:
        query = query.filter(LiveTimelineCard.environment == params.environment)
    elif not params.include_test:
        query = query.filter(LiveTimelineCard.environment.notin_(("test", "e2e")))
    if params.device_id:
        query = query.filter(LiveTimelineCard.device_id == params.device_id)
    query = query.filter(
        func.coalesce(LiveTimelineCard.last_activity_at, LiveTimelineCard.started_at) >= since,
    )
    if params.hide_autonomous:
        query = query.filter(
            or_(
                LiveTimelineCard.user_messages > 0,
                LiveTimelineCard.archive_state == "pending",
                LiveTimelineCard.launch_actor == "human_ui",
                LiveTimelineCard.launch_surface.in_(("web", "ios", "api")),
            )
        )
    if not params.include_automation:
        query = query.filter(
            or_(
                LiveTimelineCard.origin_kind.is_(None),
                LiveTimelineCard.origin_kind != "hatch_automation",
            )
        )
    total = int(query.count())
    rows = (
        query.order_by(
            func.coalesce(LiveTimelineCard.last_activity_at, LiveTimelineCard.started_at).desc(),
            LiveTimelineCard.session_id.desc(),
        )
        .limit(params.limit)
        .offset(params.offset)
        .all()
    )
    session_id_strings = [str(card.session_id) for card, _session in rows]
    session_ids = [UUID(session_id) for session_id in session_id_strings]
    readiness = latest_live_launch_readiness_map(db, session_ids, now=now)
    capabilities = _live_capability_map(db, session_ids=session_id_strings, now=now)
    runtime_rows = (
        db.query(LiveRuntimeState)
        .filter(LiveRuntimeState.session_id.in_(session_ids))
        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
        .all()
        if session_ids
        else []
    )
    runtime_by_session: dict[str, LiveRuntimeState] = {}
    for runtime in runtime_rows:
        if runtime.session_id is not None:
            runtime_by_session.setdefault(str(runtime.session_id), runtime)

    cards: list[TimelineSessionCardResponse] = []
    for card, session in rows:
        session_id = str(session.session_id)
        projected = _response_from_catalog(
            session,
            card,
            readiness=readiness.get(UUID(session_id)),
            runtime=runtime_by_session.get(session_id),
            capability_flags=capabilities[session_id],
            now=now,
        )
        thread_id = str(session.primary_thread_id or session.session_id)
        cards.append(
            TimelineSessionCardResponse(
                thread_id=thread_id,
                timeline_anchor_at=projected.timeline_anchor_at,
                head=projected,
                detail=projected,
                root=projected,
                continuation_count=1,
                started_origin_label=projected.origin_label or projected.environment,
                head_origin_label=projected.origin_label or projected.environment,
            )
        )
    has_real = any((session.device_id or "") != "demo-mac" for _card, session in rows) or total == 0
    return TimelineSessionsListResponse(sessions=cards, total=total, has_real_sessions=has_real)


def list_live_catalog_sessions(db: Session, *, params: TimelineSessionListParams) -> SessionsListResponse:
    """Machine-facing flat session list from the same bounded card projection."""

    timeline = list_live_catalog_timeline(db, params=params)
    return SessionsListResponse(
        sessions=[card.head for card in timeline.sessions],
        total=timeline.total,
        has_real_sessions=timeline.has_real_sessions,
    )


async def stream_live_catalog_timeline(
    request,
    *,
    session_factory,
    params: TimelineSessionListParams,
    skip_initial_replay: bool,
):
    """SSE list stream driven by the existing timeline pubsub wake signal."""

    bus = get_pubsub()
    sequence = bus.peek_latest_seq(TOPIC_TIMELINE)
    previous: dict[str, str] = {}
    previous_total: int | None = None
    last_heartbeat = monotonic()
    yield {"event": "connected", "data": json.dumps({"message": "Timeline session stream connected"})}

    with bus.subscribe(TOPIC_TIMELINE, since_seq=sequence) as subscription:
        while not await request.is_disconnected():
            if skip_initial_replay:
                skip_initial_replay = False
            else:

                def _load():
                    with session_factory() as db:
                        return list_live_catalog_timeline(db, params=params)

                response = await asyncio.to_thread(_load)
                current = {
                    card.thread_id: json.dumps(card.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                    for card in response.sessions
                }
                for thread_id in sorted(previous.keys() - current.keys()):
                    yield {
                        "event": "session_remove",
                        "data": json.dumps(
                            {
                                "thread_id": thread_id,
                                "total": response.total,
                                "has_real_sessions": response.has_real_sessions,
                            }
                        ),
                    }
                for card in response.sessions:
                    signature = current[card.thread_id]
                    if previous.get(card.thread_id) == signature:
                        continue
                    yield {
                        "event": "session_upsert",
                        "data": json.dumps(
                            {
                                "session": card.model_dump(mode="json"),
                                "total": response.total,
                                "has_real_sessions": response.has_real_sessions,
                            }
                        ),
                    }
                previous = current
                previous_total = response.total

            now = monotonic()
            if now - last_heartbeat >= 30.0:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "total": previous_total,
                        }
                    ),
                }
                last_heartbeat = now
            await subscription.next_message(timeout=5.0)
