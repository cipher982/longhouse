"""Durable Machine Agent control operation lifecycle."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models import MachineControlOperation

MACHINE_OPERATION_COMMAND_PREFIX = "machine-op:"
MACHINE_OPERATION_TIMEOUT_GRACE_SECS = 30
NONTERMINAL_OPERATION_STATUSES = {"queued", "running"}
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "timed_out"}


class ActiveMachineControlOperationError(RuntimeError):
    """Raised when an active operation already exists for the same target."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def create_provider_live_proof_operation(
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    request_payload: dict[str, Any],
    timeout_secs: int,
) -> MachineControlOperation:
    """Create a running provider-live proof operation and reserve its command id."""

    reap_stale_machine_control_operations(db)
    operation_id = str(uuid4())
    started_at = _now()
    operation = MachineControlOperation(
        id=operation_id,
        owner_id=owner_id,
        device_id=device_id,
        command_type="provider.live_proof",
        command_id=f"{MACHINE_OPERATION_COMMAND_PREFIX}{operation_id}",
        provider=provider,
        status="running",
        request_json=dict(request_payload),
        timeout_secs=timeout_secs,
        started_at=started_at,
        expires_at=started_at + timedelta(seconds=timeout_secs + MACHINE_OPERATION_TIMEOUT_GRACE_SECS),
    )
    db.add(operation)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ActiveMachineControlOperationError("provider live proof already in flight") from exc
    db.refresh(operation)
    return operation


def fail_machine_control_operation(
    db: Session,
    operation: MachineControlOperation,
    *,
    code: str,
    message: str,
) -> None:
    if str(operation.status) in TERMINAL_OPERATION_STATUSES:
        return
    finished_at = _now()
    operation.status = "failed"
    operation.error_json = {"code": code, "message": message}
    operation.finished_at = finished_at
    operation.updated_at = finished_at
    operation.expires_at = None
    db.add(operation)
    db.commit()


def get_machine_control_operation_for_owner(
    db: Session,
    *,
    owner_id: int,
    operation_id: str,
) -> MachineControlOperation | None:
    reap_stale_machine_control_operations(db)
    return (
        db.query(MachineControlOperation)
        .filter(MachineControlOperation.id == operation_id)
        .filter(MachineControlOperation.owner_id == owner_id)
        .first()
    )


def reconcile_machine_control_operation_from_command_result(
    db: Session,
    message: dict[str, Any],
    *,
    owner_id: int,
    device_id: str,
) -> bool:
    """Apply an unmatched Machine Agent command_result to a durable operation."""

    command_id = str(message.get("command_id") or "").strip()
    if not command_id.startswith(MACHINE_OPERATION_COMMAND_PREFIX):
        return False
    operation = (
        db.query(MachineControlOperation)
        .filter(MachineControlOperation.command_id == command_id)
        .filter(MachineControlOperation.owner_id == owner_id)
        .filter(MachineControlOperation.device_id == device_id)
        .first()
    )
    if operation is None:
        return False
    if str(operation.status) in TERMINAL_OPERATION_STATUSES:
        return True

    finished_at = _now()
    operation.finished_at = finished_at
    operation.updated_at = finished_at
    operation.expires_at = None
    if message.get("ok"):
        result = message.get("result")
        operation.status = "succeeded"
        operation.result_json = dict(result) if isinstance(result, dict) else {}
        operation.error_json = None
    else:
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        operation.status = "failed"
        operation.error_json = {
            "code": str(error.get("code") or "machine_control_operation_failed"),
            "message": str(error.get("message") or "Machine Agent control command failed"),
        }
    db.add(operation)
    db.commit()
    return True


def reap_stale_machine_control_operations(db: Session, *, now: datetime | None = None) -> int:
    cutoff = now or _now()
    stale = (
        db.query(MachineControlOperation)
        .filter(MachineControlOperation.status.in_(NONTERMINAL_OPERATION_STATUSES))
        .filter(MachineControlOperation.expires_at.is_not(None))
        .filter(MachineControlOperation.expires_at <= cutoff)
        .all()
    )
    for operation in stale:
        finished_at = _aware(operation.expires_at) or cutoff
        operation.status = "timed_out"
        operation.error_json = {
            "code": "machine_control_operation_timeout",
            "message": "Machine Agent did not report back before the operation lease expired",
        }
        operation.finished_at = finished_at
        operation.updated_at = cutoff
        operation.expires_at = None
        db.add(operation)
    if stale:
        db.commit()
    return len(stale)


def machine_control_operation_to_response(operation: MachineControlOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "device_id": operation.device_id,
        "command_type": operation.command_type,
        "command_id": operation.command_id,
        "provider": operation.provider,
        "status": operation.status,
        "request": operation.request_json or {},
        "result": operation.result_json,
        "error": operation.error_json,
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "timeout_secs": operation.timeout_secs,
    }
