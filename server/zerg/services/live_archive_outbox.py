"""Durable Live Store receipts plus archive-side input provenance.

``live_archive_outbox`` is a receipt table, not a queue: nothing drains it.
Every writer here therefore stamps ``drained_at`` at insert time. A row left
with ``drained_at IS NULL`` is a writer bug -- the health check reads that
column as pending outbox work and warns forever, because no drain will ever
clear it.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionInput
from zerg.models.agents import SessionTurn
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.live_launch_readiness import MANAGED_LOCAL_LAUNCH_OUTBOX_KIND
from zerg.services.managed_local_launcher import ManagedLocalLaunchPlan
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import VALID_INTENTS
from zerg.utils.time import normalize_utc

MANAGED_LOCAL_LAUNCH_KIND = MANAGED_LOCAL_LAUNCH_OUTBOX_KIND
CONSOLE_SESSION_CREATE_KIND = "console_session_create.v1"
_LAUNCH_RECEIPT_RETENTION = timedelta(days=30)
_LAUNCH_RECEIPT_KINDS = (MANAGED_LOCAL_LAUNCH_KIND,)


def managed_local_launch_idempotency_key(*, session_id: UUID | str) -> str:
    return f"{MANAGED_LOCAL_LAUNCH_KIND}:{str(session_id).strip()}"


def _prune_completed_launch_receipts(db: Session, *, now: datetime) -> None:
    db.query(LiveArchiveOutbox).filter(
        LiveArchiveOutbox.kind.in_(_LAUNCH_RECEIPT_KINDS),
        LiveArchiveOutbox.drained_at.isnot(None),
        LiveArchiveOutbox.drained_at < now - _LAUNCH_RECEIPT_RETENTION,
    ).delete(synchronize_session=False)


def enqueue_managed_local_launch_outbox(
    db: Session,
    *,
    plan: ManagedLocalLaunchPlan,
    owner_id: int,
    git_repo: str | None,
    git_branch: str | None,
    started_at: datetime,
    idempotency_key: str | None = None,
    completed: bool = False,
) -> bool:
    """Persist managed-local launch idempotency evidence."""

    _prune_completed_launch_receipts(db, now=datetime.now(timezone.utc))
    key = idempotency_key or managed_local_launch_idempotency_key(session_id=plan.session_id)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=MANAGED_LOCAL_LAUNCH_KIND,
            payload_json=json.dumps(
                {
                    "launch": _jsonable(
                        {
                            "owner_id": int(owner_id),
                            "git_repo": git_repo,
                            "git_branch": git_branch,
                            "started_at": started_at,
                            "plan": {
                                "session_id": plan.session_id,
                                "provider": plan.provider,
                                "provider_session_id": plan.provider_session_id,
                                "source_name": plan.source_name,
                                "source_runner_id": plan.source_runner_id,
                                "cwd": plan.cwd,
                                "project": plan.project,
                                "display_name": plan.display_name,
                                "managed_session_name": plan.managed_session_name,
                                "permission_mode": plan.permission_mode,
                                "launch_actor": plan.launch_actor,
                                "launch_surface": plan.launch_surface,
                                "environment": getattr(plan, "environment", "development"),
                                "origin_kind": getattr(plan, "origin_kind", None),
                                "hidden_from_default_timeline": getattr(plan, "hidden_from_default_timeline", 1),
                                "managed_transport": plan.managed_transport,
                                "attach_command": plan.attach_command,
                                "provider_config": plan.provider_config,
                            },
                        }
                    )
                },
                sort_keys=True,
            ),
            drained_at=started_at if completed else None,
        )
    )
    return True


def _enqueue_json_outbox(
    db: Session,
    *,
    idempotency_key: str,
    kind: str,
    payload: dict[str, Any],
    completed: bool,
) -> bool:
    now = datetime.now(timezone.utc)
    _prune_completed_launch_receipts(db, now=now)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == idempotency_key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=idempotency_key,
            kind=kind,
            payload_json=json.dumps(payload, sort_keys=True),
            drained_at=now if completed else None,
        )
    )
    return True


def enqueue_console_session_create_outbox(db: Session, *, session: dict[str, Any]) -> bool:
    session_id = str(session.get("session_id") or "").strip()
    return _enqueue_json_outbox(
        db,
        idempotency_key=f"{CONSOLE_SESSION_CREATE_KIND}:{session_id}",
        kind=CONSOLE_SESSION_CREATE_KIND,
        payload={"session": _jsonable(session)},
        # Durable owner provenance, not queued work: catalogd reads this row
        # back to resolve a Console session's owner. Nothing drains it, so it
        # is written already-complete.
        completed=True,
    )


def project_session_input_receipt_to_archive(
    db: Session,
    *,
    source_session_id: UUID | str,
    owner_id: int,
    text: str,
    intent: str,
    client_request_id: str | None,
    delivery_request_id: str,
) -> int:
    """Materialize live input receipt provenance in archive SQLite idempotently."""

    existing_query = db.query(SessionInput).filter(
        SessionInput.session_id == source_session_id,
        SessionInput.owner_id == owner_id,
    )
    if client_request_id:
        existing_query = existing_query.filter(SessionInput.client_request_id == client_request_id)
    else:
        existing_query = existing_query.filter(SessionInput.delivery_request_id == delivery_request_id)
    existing = existing_query.order_by(SessionInput.id.asc()).first()
    if existing is not None:
        input_id = int(existing.id)
        if existing.status == INPUT_STATUS_DELIVERING:
            now = datetime.now(timezone.utc)
            existing.status = INPUT_STATUS_DELIVERED
            existing.delivered_at = now
            existing.updated_at = now
            existing.last_error = None
        _link_session_turn(db, source_session_id=source_session_id, delivery_request_id=delivery_request_id, input_id=input_id)
        return input_id

    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent}")
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    now = datetime.now(timezone.utc)
    row = SessionInput(
        session_id=source_session_id,
        thread_id=ensure_thread_id_for_session(db, source_session_id),
        body=text,
        owner_id=owner_id,
        intent=intent,
        status=INPUT_STATUS_DELIVERED,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
        delivered_at=now,
    )
    db.add(row)
    db.flush()
    input_id = int(row.id)
    _link_session_turn(db, source_session_id=source_session_id, delivery_request_id=delivery_request_id, input_id=input_id)
    return input_id


def _link_session_turn(
    db: Session,
    *,
    source_session_id: UUID | str,
    delivery_request_id: str,
    input_id: int,
) -> None:
    db.query(SessionTurn).filter(
        SessionTurn.session_id == source_session_id,
        SessionTurn.request_id == delivery_request_id,
    ).update({"session_input_id": input_id}, synchronize_session=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = normalize_utc(value) or value
        return {"__longhouse_datetime__": normalized.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
