"""Hot-lane launch readiness facts for managed remote launches."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveLaunchReadiness
from zerg.services.session_launch_lifecycle import RemoteExecutionLifetime
from zerg.services.session_launch_lifecycle import RemoteLaunchErrorCode
from zerg.services.session_launch_lifecycle import RemoteLaunchLifecycleState
from zerg.services.session_launch_lifecycle import format_remote_launch_error_message
from zerg.services.session_launch_lifecycle import normalize_remote_execution_lifetime
from zerg.services.session_launch_lifecycle import normalize_remote_launch_error_code
from zerg.utils.time import normalize_utc

LiveLaunchReadinessState = Literal["pending", "dispatched", "adopted", "failed", "abandoned"]
MANAGED_LOCAL_LAUNCH_OUTBOX_KIND = "managed_local_launch.v1"
REMOTE_LAUNCH_OUTBOX_KIND = "remote_launch.v1"
REMOTE_LAUNCH_OUTCOME_OUTBOX_KIND = "remote_launch_outcome.v1"


@dataclass(frozen=True)
class LiveLaunchReadinessView:
    session_id: UUID
    launch_state: RemoteLaunchLifecycleState
    execution_lifetime: RemoteExecutionLifetime
    launch_error_code: RemoteLaunchErrorCode | None
    launch_error_message: str | None
    owner_id: str | None = None
    provider: str | None = None
    device_id: str | None = None
    machine_id: str | None = None
    project: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _owner_key(owner_id: int | str | None) -> str | None:
    if owner_id is None:
        return None
    return str(owner_id)


def _project_live_launch_state(row: LiveLaunchReadiness) -> LiveLaunchReadinessView:
    raw_state = str(row.state or "").strip()
    execution_lifetime = normalize_remote_execution_lifetime(row.execution_lifetime)
    if raw_state == "failed":
        launch_state: RemoteLaunchLifecycleState = "launch_failed"
    elif raw_state == "abandoned":
        launch_state = "launch_orphaned"
    elif raw_state == "adopted":
        launch_state = "live"
    elif raw_state == "dispatched":
        launch_state = "launching_unknown"
    else:
        launch_state = "launching"

    error_code = normalize_remote_launch_error_code(row.error_code) if row.error_code is not None else None
    return LiveLaunchReadinessView(
        session_id=UUID(str(row.session_id)),
        launch_state=launch_state,
        execution_lifetime=execution_lifetime,
        launch_error_code=error_code,
        launch_error_message=format_remote_launch_error_message(error_code, row.error_message),
        owner_id=row.owner_id,
        provider=row.provider,
        device_id=row.device_id,
        machine_id=row.machine_id,
        project=row.project,
        created_at=normalize_utc(row.created_at),
        updated_at=normalize_utc(row.updated_at),
    )


def get_live_launch_readiness_by_client_request(
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    client_request_id: str,
) -> LiveLaunchReadinessView | None:
    row = (
        db.query(LiveLaunchReadiness)
        .filter(LiveLaunchReadiness.owner_id == _owner_key(owner_id))
        .filter(LiveLaunchReadiness.device_id == device_id)
        .filter(LiveLaunchReadiness.provider == provider)
        .filter(LiveLaunchReadiness.client_request_id == client_request_id)
        .order_by(LiveLaunchReadiness.created_at.desc())
        .first()
    )
    if row is None:
        return None
    return _project_live_launch_state(row)


def get_live_launch_readiness_by_session_id(
    db: Session,
    *,
    session_id: UUID,
    now: datetime | None = None,
) -> LiveLaunchReadinessView | None:
    row = db.get(LiveLaunchReadiness, str(session_id))
    if row is None or _live_launch_readiness_expired(row, now=now):
        return None
    return _project_live_launch_state(row)


def latest_live_launch_readiness_map(
    db: Session,
    session_ids: Iterable[UUID],
    *,
    now: datetime | None = None,
) -> dict[UUID, LiveLaunchReadinessView]:
    session_id_strings = [str(session_id) for session_id in session_ids]
    if not session_id_strings:
        return {}
    rows = db.query(LiveLaunchReadiness).filter(LiveLaunchReadiness.session_id.in_(session_id_strings)).all()
    result: dict[UUID, LiveLaunchReadinessView] = {}
    for row in rows:
        if _live_launch_readiness_expired(row, now=now):
            continue
        session_id = UUID(str(row.session_id))
        result[session_id] = _project_live_launch_state(row)
    return result


def _live_launch_readiness_expired(row: LiveLaunchReadiness, *, now: datetime | None = None) -> bool:
    expires_at = normalize_utc(row.expires_at)
    if expires_at is None:
        return False
    cutoff = normalize_utc(now) or _now()
    return expires_at <= cutoff


def upsert_live_launch_readiness(
    db: Session,
    *,
    session_id: UUID,
    owner_id: int,
    device_id: str,
    provider: str,
    execution_lifetime: RemoteExecutionLifetime,
    state: LiveLaunchReadinessState,
    command_id: str,
    client_request_id: str | None,
    machine_id: str | None,
    project: str | None,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> LiveLaunchReadiness:
    row = db.get(LiveLaunchReadiness, str(session_id))
    if row is None:
        row = LiveLaunchReadiness(session_id=str(session_id))
        db.add(row)

    row.owner_id = _owner_key(owner_id)
    row.device_id = device_id
    row.provider = provider
    row.execution_lifetime = execution_lifetime
    row.state = state
    row.command_id = command_id
    row.client_request_id = client_request_id
    row.machine_id = machine_id
    row.project = project
    row.expires_at = normalize_utc(expires_at)
    row.error_code = None
    row.error_message = None
    row.updated_at = now or _now()
    return row


def update_live_launch_readiness_state(
    db: Session,
    *,
    session_id: UUID,
    state: LiveLaunchReadinessState,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_expires: bool = False,
    now: datetime | None = None,
) -> bool:
    row = db.get(LiveLaunchReadiness, str(session_id))
    if row is None:
        return False
    row.state = state
    row.error_code = error_code
    row.error_message = error_message
    if clear_expires:
        row.expires_at = None
    row.updated_at = now or _now()
    return True


def reap_expired_live_launch_readiness(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    if limit <= 0:
        return 0
    cutoff = now or _now()
    rows = (
        db.query(LiveLaunchReadiness)
        .filter(LiveLaunchReadiness.expires_at.isnot(None))
        .filter(LiveLaunchReadiness.expires_at <= cutoff)
        .order_by(LiveLaunchReadiness.expires_at.asc(), LiveLaunchReadiness.created_at.asc())
        .limit(limit)
        .all()
    )
    removed = 0
    for row in rows:
        if _has_pending_launch_outbox(db, session_id=str(row.session_id)):
            continue
        db.delete(row)
        removed += 1
    return removed


def _has_pending_launch_outbox(db: Session, *, session_id: str) -> bool:
    for kind in (MANAGED_LOCAL_LAUNCH_OUTBOX_KIND, REMOTE_LAUNCH_OUTBOX_KIND):
        key = f"{kind}:{session_id}"
        exists = (
            db.query(LiveArchiveOutbox.id)
            .filter(LiveArchiveOutbox.kind == kind)
            .filter(LiveArchiveOutbox.idempotency_key == key)
            .filter(LiveArchiveOutbox.drained_at.is_(None))
            .first()
        )
        if exists is not None:
            return True
    outcome_prefix = f"{REMOTE_LAUNCH_OUTCOME_OUTBOX_KIND}:{session_id}:"
    outcome_exists = (
        db.query(LiveArchiveOutbox.id)
        .filter(LiveArchiveOutbox.kind == REMOTE_LAUNCH_OUTCOME_OUTBOX_KIND)
        .filter(LiveArchiveOutbox.idempotency_key.like(f"{outcome_prefix}%"))
        .filter(LiveArchiveOutbox.drained_at.is_(None))
        .first()
    )
    if outcome_exists is not None:
        return True
    return False
