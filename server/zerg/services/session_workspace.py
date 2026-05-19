"""Builders for focused session workspace responses."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.services.agents_store import AgentsStore
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import SessionProjectionItemResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_views import build_event_response
from zerg.services.session_views import build_event_input_origin_map
from zerg.services.session_views import build_session_response
from zerg.services.unmanaged_bindings import load_binding_overlay
from zerg.utils.server_timing import ServerTimingRecorder


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
        runtime_state_map = load_runtime_state_map(db, [item.id for item in thread_sessions])
        transcript_preview_map = load_active_provisional_preview_map(db, [item.id for item in thread_sessions])
        binding_overlay_map = load_binding_overlay(db, [item.id for item in thread_sessions], now=now)
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
                transcript_preview=transcript_preview_map.get(str(item.id)),
                owner_id=owner_id,
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
            transcript_preview=transcript_preview_map.get(str(session.id)),
            owner_id=owner_id,
        )

    active_context_boundary_cache: dict[UUID, int | None] = {}
    head_branch_id_cache: dict[UUID, int | None] = {}
    with timing.span("load_input_origins"):
        input_origin_map = build_event_input_origin_map(
            store,
            [item.event for item in projection.items if item.kind == "event" and item.event is not None],
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

    with timing.span("build_projection"):
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
                        ),
                    )
                )
                continue

            projection_items.append(
                SessionProjectionItemResponse(
                    kind="seam",
                    session_id=str(item.session.id),
                    timestamp=item.session.started_at,
                    continued_from_session_id=(
                        str(item.session.continued_from_session_id) if item.session.continued_from_session_id else None
                    ),
                    continuation_kind=item.session.continuation_kind,
                    origin_label=item.session.origin_label,
                    parent_origin_label=(item.parent_session.origin_label if item.parent_session else None),
                    parent_continuation_kind=(item.parent_session.continuation_kind if item.parent_session else None),
                    branched_from_event_id=item.session.branched_from_event_id,
                )
            )

        return SessionWorkspaceResponse(
            session=session_response,
            thread=SessionThreadResponse(
                root_session_id=str(session.thread_root_session_id or session.id),
                head_session_id=str(head.id if head else session.id),
                sessions=[thread_response_map.get(str(item.id), session_response) for item in thread_sessions],
            ),
            projection=SessionProjectionResponse(
                root_session_id=str(session.thread_root_session_id or session.id),
                focus_session_id=str(session.id),
                head_session_id=str(head.id if head else session.id),
                path_session_ids=[str(path_session.id) for path_session in projection.path_sessions],
                items=projection_items,
                total=projection.total,
                page_offset=projection.page_offset,
                branch_mode=projection.branch_mode,
                abandoned_events=projection.abandoned_events,
            ),
        )
