"""Builders for focused session workspace responses."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.services.agents.kernel_capabilities import project_capabilities_bulk
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_turns import load_pending_response_turn_map
from zerg.services.session_views import SessionMobileTailResponse
from zerg.services.session_views import SessionProjectionItemResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_views import build_event_input_origin_map
from zerg.services.session_views import build_event_response
from zerg.services.session_views import build_session_response
from zerg.services.session_views import build_tool_call_state_map
from zerg.services.session_views import is_session_closed
from zerg.services.unmanaged_bindings import load_binding_overlay
from zerg.utils.server_timing import ServerTimingRecorder
from zerg.utils.time import normalize_utc


def build_session_workspace(
    *,
    db: Session,
    session_id: UUID,
    branch_mode: str = "head",
    limit: int = 100,
    timing: ServerTimingRecorder | None = None,
    owner_id: int | None = None,
) -> SessionWorkspaceResponse:
    """Build the focused session, thread, and initial projected transcript page."""
    store = AgentsStore(db)
    timing = timing or ServerTimingRecorder()

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

    with timing.span("load_thread"):
        thread_sessions = store.list_thread_sessions(session)
    if not thread_sessions:
        thread_sessions = [session]

    with timing.span("load_head"):
        head = next((item for item in thread_sessions if bool(item.is_writable_head)), None)
        if head is None:
            head = store.get_thread_head(session)

    thread_session_ids = [item.id for item in thread_sessions]
    with timing.span("load_activity"):
        activity_map = store.get_last_activity_map(thread_session_ids)
    with timing.span("load_first_user"):
        first_user_map = store.get_first_message_map(thread_session_ids, role="user", max_len=80)
    with timing.span("load_projection"):
        projection = store.get_session_projection_page(
            session,
            branch_mode=branch_mode,
            limit=limit,
            load_from_end=True,
        )

    thread_cache = store.batch_thread_meta(thread_sessions)
    now = datetime.now(timezone.utc)
    with timing.span("load_runtime"):
        with timing.span("runtime_state"):
            runtime_state_map = load_runtime_state_map(db, thread_session_ids)
        with timing.span("control_state"):
            control_state_map = load_managed_control_state_map(db, thread_session_ids)
        with timing.span("provisional_preview"):
            transcript_preview_map = _load_provisional_preview_map(
                db,
                thread_sessions=thread_sessions,
                runtime_state_map=runtime_state_map,
                now=now,
            )
        with timing.span("pending_turns"):
            pending_response_turn_map = load_pending_response_turn_map(db, thread_session_ids)
        with timing.span("binding_overlay"):
            binding_overlay_map = load_binding_overlay(db, thread_session_ids, now=now)
        with timing.span("kernel_capabilities"):
            kernel_capabilities_map = project_capabilities_bulk(db, session_ids=thread_session_ids)
    with timing.span("build_thread_responses"):
        thread_response_map = {
            str(item.id): build_session_response(
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
                binding_overlay=binding_overlay_map.get(item.id),
                control_overlay=control_state_map.get(item.id),
                transcript_preview=transcript_preview_map.get(str(item.id)),
                owner_id=owner_id,
                kernel_capabilities=kernel_capabilities_map.get(item.id),
                has_pending_response_turn=bool(pending_response_turn_map.get(item.id)),
            )
            for item in thread_sessions
        }

    session_response = thread_response_map.get(str(session.id))
    if session_response is None:
        session_response = build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
            runtime_overlay=resolve_runtime_overlay(
                session,
                last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
                runtime_state_map=runtime_state_map,
                now=now,
            ),
            first_user_message=first_user_map.get(session.id),
            binding_overlay=binding_overlay_map.get(session.id),
            control_overlay=control_state_map.get(session.id),
            transcript_preview=transcript_preview_map.get(str(session.id)),
            owner_id=owner_id,
            kernel_capabilities=kernel_capabilities_map.get(session.id),
            has_pending_response_turn=bool(pending_response_turn_map.get(session.id)),
        )

    with timing.span("build_projection"):
        projection_response = _build_projection_response(
            store=store,
            session=session,
            head=head,
            branch_mode=branch_mode,
            projection=projection,
            mobile_payload=False,
        )

        return SessionWorkspaceResponse(
            session=session_response,
            thread=SessionThreadResponse(
                root_session_id=str(session.thread_root_session_id or session.id),
                head_session_id=str(head.id if head else session.id),
                sessions=[thread_response_map.get(str(item.id), session_response) for item in thread_sessions],
            ),
            projection=projection_response,
        )


def build_session_mobile_tail(
    *,
    db: Session,
    session_id: UUID,
    branch_mode: str = "head",
    limit: int = 50,
    offset: int = 0,
    snapshot_event_id: int | None = None,
    timing: ServerTimingRecorder | None = None,
    owner_id: int | None = None,
) -> SessionMobileTailResponse:
    """Build a compact focused session payload for mobile first paint."""
    store = AgentsStore(db)
    timing = timing or ServerTimingRecorder()

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

    with timing.span("load_thread"):
        thread_sessions = store.list_thread_sessions(session)
    if not thread_sessions:
        thread_sessions = [session]

    with timing.span("load_head"):
        head = next((item for item in thread_sessions if bool(item.is_writable_head)), None)
        if head is None:
            head = store.get_thread_head(session)

    thread_session_ids = [item.id for item in thread_sessions]
    with timing.span("load_activity"):
        activity_map = store.get_last_activity_map(thread_session_ids)
    with timing.span("load_first_user"):
        first_user_map = store.get_first_message_map(thread_session_ids, role="user", max_len=80)
    with timing.span("load_projection"):
        projection = store.get_session_projection_page(
            session,
            branch_mode=branch_mode,
            limit=limit,
            offset=offset,
            load_from_end=True,
        )

    current_snapshot_event_id = _projection_snapshot_event_id(store, projection)
    if snapshot_event_id is not None and current_snapshot_event_id != snapshot_event_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "projection_drift",
                "snapshot_event_id": current_snapshot_event_id,
            },
        )

    thread_cache = store.batch_thread_meta(thread_sessions)
    now = datetime.now(timezone.utc)
    with timing.span("load_runtime"):
        with timing.span("runtime_state"):
            runtime_state_map = load_runtime_state_map(db, thread_session_ids)
        with timing.span("control_state"):
            control_state_map = load_managed_control_state_map(db, thread_session_ids)
        with timing.span("provisional_preview"):
            transcript_preview_map = _load_provisional_preview_map(
                db,
                thread_sessions=thread_sessions,
                runtime_state_map=runtime_state_map,
                now=now,
            )
        with timing.span("pending_turns"):
            pending_response_turn_map = load_pending_response_turn_map(db, thread_session_ids)
        with timing.span("binding_overlay"):
            binding_overlay_map = load_binding_overlay(db, thread_session_ids, now=now)

    with timing.span("build_session"):
        session_response = build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
            runtime_overlay=resolve_runtime_overlay(
                session,
                last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
                runtime_state_map=runtime_state_map,
                now=now,
            ),
            first_user_message=first_user_map.get(session.id),
            binding_overlay=binding_overlay_map.get(session.id),
            control_overlay=control_state_map.get(session.id),
            transcript_preview=transcript_preview_map.get(str(session.id)),
            owner_id=owner_id,
            has_pending_response_turn=bool(pending_response_turn_map.get(session.id)),
        )

    with timing.span("build_projection"):
        projection_response = _build_projection_response(
            store=store,
            session=session,
            head=head,
            branch_mode=branch_mode,
            projection=projection,
            mobile_payload=True,
        )

    return SessionMobileTailResponse(
        session=session_response,
        projection=projection_response,
        snapshot_event_id=current_snapshot_event_id,
    )


def _projection_snapshot_event_id(store: AgentsStore, projection) -> int | None:
    latest_event_ids = [store.get_latest_event_id(path_session.id) for path_session in projection.path_sessions]
    return max((event_id for event_id in latest_event_ids if event_id is not None), default=None)


def _load_provisional_preview_map(
    db: Session,
    *,
    thread_sessions,
    runtime_state_map,
    now: datetime,
):
    preview_session_ids = [
        item.id
        for item in thread_sessions
        if _session_may_have_live_provisional_preview(
            item,
            runtime_state=runtime_state_map.get(str(item.id)),
            now=now,
        )
    ]
    if not preview_session_ids:
        return {}
    return load_active_provisional_preview_map(db, preview_session_ids)


def _session_may_have_live_provisional_preview(session, *, runtime_state, now: datetime) -> bool:
    if normalize_utc(getattr(session, "ended_at", None)) is not None:
        return False

    if runtime_state is None:
        return True

    terminal_state = str(getattr(runtime_state, "terminal_state", "") or "").strip()
    if terminal_state:
        return False

    phase = str(getattr(runtime_state, "phase", "") or "").strip()
    if phase == "finished":
        return False

    freshness_expires_at = normalize_utc(getattr(runtime_state, "freshness_expires_at", None))
    if freshness_expires_at is not None and freshness_expires_at <= now:
        return False

    return True


def _build_projection_response(
    *,
    store: AgentsStore,
    session,
    head,
    branch_mode: str,
    projection,
    mobile_payload: bool,
) -> SessionProjectionResponse:
    active_context_boundary_cache: dict[UUID, int | None] = {}
    head_branch_id_cache: dict[UUID, int | None] = {}
    input_origin_map = build_event_input_origin_map(
        store,
        [item.event for item in projection.items if item.kind == "event" and item.event is not None],
    )

    sessions_by_id: dict[UUID, object] = {}
    for item in projection.items:
        if item.kind == "event" and item.event is not None:
            sessions_by_id.setdefault(item.session.id, item.session)
    tool_call_state_map: dict[int, object] = {}
    for sid, path_session in sessions_by_id.items():
        tool_call_state_map.update(
            build_tool_call_state_map(
                store.get_session_tool_call_events(sid, branch_mode=branch_mode),
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

    projection_items: list[SessionProjectionItemResponse] = []
    for item in projection.items:
        if item.kind == "event" and item.event is not None:
            projection_items.append(
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
                        mobile_payload=mobile_payload,
                    ),
                )
            )
            continue

        projection_items.append(
            SessionProjectionItemResponse(
                kind="seam",
                session_id=str(item.session.id),
                timestamp=item.session.started_at,
                continued_from_session_id=(str(item.session.continued_from_session_id) if item.session.continued_from_session_id else None),
                continuation_kind=item.session.continuation_kind,
                origin_label=item.session.origin_label,
                parent_origin_label=(item.parent_session.origin_label if item.parent_session else None),
                parent_continuation_kind=(item.parent_session.continuation_kind if item.parent_session else None),
                branched_from_event_id=item.session.branched_from_event_id,
            )
        )

    return SessionProjectionResponse(
        root_session_id=str(session.thread_root_session_id or session.id),
        focus_session_id=str(session.id),
        head_session_id=str(head.id if head else session.id),
        path_session_ids=[str(path_session.id) for path_session in projection.path_sessions],
        items=projection_items,
        total=projection.total,
        page_offset=projection.page_offset,
        branch_mode=projection.branch_mode,
        abandoned_events=projection.abandoned_events,
    )
