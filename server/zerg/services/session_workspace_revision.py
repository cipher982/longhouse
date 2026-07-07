"""Durable revision fingerprint for session viewport freshness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLivePreview
from zerg.models.agents import SessionPauseRequest
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionThread
from zerg.services.managed_control_state import _connection_priority as _managed_control_connection_priority
from zerg.services.managed_provider_contracts import provider_for_control_plane
from zerg.services.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.services.session_pause_requests import PENDING_STATUS
from zerg.services.session_pause_requests import is_user_facing_pause_request
from zerg.utils.time import normalize_utc


@dataclass(frozen=True)
class SessionWorkspaceRevision:
    latest_event_id: int
    latest_session_updated_at: datetime | None
    latest_runtime_signal_at: datetime | None
    runtime_version_sum: int
    pause_request_count: int
    pause_request_fingerprint: str | None
    managed_control_count: int
    managed_control_fingerprint: str | None
    live_preview_updated_at: datetime | None
    thread_session_count: int
    fingerprint: str
    signature: tuple[Any, ...]


def load_session_workspace_revision(db: Session, session_id: UUID) -> SessionWorkspaceRevision | None:
    """Return a stable fingerprint for every session-viewport-visible state.

    This revision is intentionally durable-state based. Process-local pubsub
    sequence may accelerate reconnects, but only this fingerprint can decide
    whether a rendered viewport is current enough to skip an initial stream
    invalidation.
    """

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    # Session-identity-kernel cleanup: each session is its own thread root in
    # current projections. Keep the list shape so the revision can widen when
    # thread projection widens again.
    thread_root_id = session.id
    thread_sessions = db.query(AgentSession.id, AgentSession.updated_at).filter(AgentSession.id == thread_root_id).all()
    thread_session_ids = [row.id for row in thread_sessions]
    thread_session_id_strings = [str(item) for item in thread_session_ids]
    latest_session_updated = max((normalize_utc(row.updated_at) for row in thread_sessions if row.updated_at), default=None)

    latest_event_id = db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id.in_(thread_session_id_strings)).scalar() or 0
    latest_event_timestamp = db.query(func.max(AgentEvent.timestamp)).filter(AgentEvent.session_id.in_(thread_session_id_strings)).scalar()
    latest_event_timestamp = normalize_utc(latest_event_timestamp)

    latest_runtime_signal = (
        db.query(func.max(SessionRuntimeState.updated_at)).filter(SessionRuntimeState.session_id.in_(thread_session_id_strings)).scalar()
    )
    latest_runtime_signal = normalize_utc(latest_runtime_signal)
    runtime_version_sum = (
        db.query(func.sum(SessionRuntimeState.runtime_version))
        .filter(SessionRuntimeState.session_id.in_(thread_session_id_strings))
        .scalar()
        or 0
    )

    live_preview_updated_at = (
        db.query(func.max(SessionLivePreview.preview_updated_at))
        .filter(SessionLivePreview.session_id.in_(thread_session_ids))
        .filter(SessionLivePreview.superseded_at.is_(None))
        .scalar()
    )
    live_preview_updated_at = normalize_utc(live_preview_updated_at)

    pause_signature = _pause_request_signature(db, thread_session_ids)
    managed_control_signature = _managed_control_signature(db, thread_session_ids)

    signature = (
        str(thread_root_id),
        latest_session_updated,
        int(latest_event_id or 0),
        latest_runtime_signal,
        int(runtime_version_sum or 0),
        len(thread_session_ids),
        latest_event_timestamp,
        live_preview_updated_at,
        pause_signature,
        managed_control_signature,
        str(session.anchor_title or ""),
    )
    fingerprint = _fingerprint(signature)

    return SessionWorkspaceRevision(
        latest_event_id=int(latest_event_id or 0),
        latest_session_updated_at=latest_session_updated,
        latest_runtime_signal_at=latest_runtime_signal,
        runtime_version_sum=int(runtime_version_sum or 0),
        pause_request_count=len(pause_signature),
        pause_request_fingerprint=_fingerprint(pause_signature) if pause_signature else None,
        managed_control_count=len(managed_control_signature),
        managed_control_fingerprint=_fingerprint(managed_control_signature) if managed_control_signature else None,
        live_preview_updated_at=live_preview_updated_at,
        thread_session_count=len(thread_session_ids),
        fingerprint=fingerprint,
        signature=signature,
    )


def _pause_request_signature(db: Session, session_ids: list[UUID]) -> tuple[tuple[Any, ...], ...]:
    if not session_ids:
        return ()
    rows = (
        db.query(SessionPauseRequest)
        .filter(SessionPauseRequest.session_id.in_(session_ids))
        .filter(SessionPauseRequest.status == PENDING_STATUS)
        .order_by(
            SessionPauseRequest.session_id.asc(),
            SessionPauseRequest.last_seen_at.desc(),
            SessionPauseRequest.occurred_at.desc(),
            SessionPauseRequest.created_at.desc(),
            SessionPauseRequest.id.asc(),
        )
        .all()
    )
    return tuple(
        (
            str(row.session_id),
            str(row.id),
            row.request_key,
            row.status,
            row.kind,
            row.tool_name,
            row.title,
            row.summary,
            bool(row.can_respond),
            _json_key(row.request_payload_json),
            _dt_key(row.occurred_at),
            _dt_key(row.resolved_at),
            _dt_key(row.expires_at),
        )
        for row in rows
        if is_user_facing_pause_request(row)
    )


def _managed_control_signature(db: Session, session_ids: list[UUID]) -> tuple[tuple[Any, ...], ...]:
    if not session_ids:
        return ()
    control_planes = tuple(plane for plane in trusted_non_runner_control_planes() if provider_for_control_plane(plane) is not None)
    if not control_planes:
        return ()
    rows = (
        db.query(SessionThread.session_id, SessionConnection)
        .join(SessionRun, SessionRun.thread_id == SessionThread.id)
        .join(SessionConnection, SessionConnection.run_id == SessionRun.id)
        .filter(
            SessionThread.session_id.in_(session_ids),
            SessionConnection.control_plane.in_(control_planes),
        )
        .order_by(
            SessionThread.session_id.asc(),
            SessionConnection.control_plane.asc(),
            SessionConnection.id.asc(),
        )
        .all()
    )
    best: dict[UUID, SessionConnection] = {}
    for session_id, conn in rows:
        existing = best.get(session_id)
        if existing is None or _managed_control_connection_priority(conn) > _managed_control_connection_priority(existing):
            best[session_id] = conn

    return tuple(
        (
            str(session_id),
            int(conn.id or 0),
            conn.control_plane,
            conn.acquisition_kind,
            conn.state,
            conn.external_name,
            conn.device_id,
            int(conn.can_send_input or 0),
            int(conn.can_interrupt or 0),
            int(conn.can_terminate or 0),
            int(conn.can_tail_output or 0),
            int(conn.can_resume or 0),
            _json_key(conn.capabilities_extra_json),
            _dt_key(conn.acquired_at),
            _dt_key(conn.released_at),
            _dt_key(conn.last_health_at),
        )
        for session_id, conn in sorted(best.items(), key=lambda item: str(item[0]))
    )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(_normalize_for_json(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_key(value: Any) -> Any:
    return _normalize_for_json(value)


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return _dt_key(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize_for_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item) for item in value]
    return value


def _dt_key(value: datetime | None) -> str | None:
    normalized = normalize_utc(value)
    if normalized is None:
        return None
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
