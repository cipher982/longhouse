"""Durable Machine Agent control operation lifecycle."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models import MachineControlOperation
from zerg.models.live_store import LiveMachineControlOperation

MACHINE_OPERATION_COMMAND_PREFIX = "machine-op:"
MANAGED_CONTROL_COMMAND_PREFIX = "managed-control:"
LIVE_CONTROL_COMMAND_PREFIXES = (MACHINE_OPERATION_COMMAND_PREFIX, MANAGED_CONTROL_COMMAND_PREFIX)
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


def create_live_provider_live_proof_operation(
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    request_payload: dict[str, Any],
    timeout_secs: int,
) -> LiveMachineControlOperation:
    """Create a running provider-live proof operation in the Live Store."""

    operation_id = str(uuid4())
    return create_live_machine_control_operation(
        db,
        operation_id=operation_id,
        owner_id=owner_id,
        device_id=device_id,
        command_type="provider.live_proof",
        command_id=f"{MACHINE_OPERATION_COMMAND_PREFIX}{operation_id}",
        provider=provider,
        request_payload=request_payload,
        timeout_secs=timeout_secs,
    )


def create_live_machine_control_operation(
    db: Session,
    *,
    operation_id: str,
    owner_id: int | None,
    device_id: str,
    command_type: str,
    command_id: str,
    request_payload: dict[str, Any],
    timeout_secs: int,
    provider: str | None = None,
    session_id: str | None = None,
) -> LiveMachineControlOperation:
    """Create a running hot-lane machine-control operation."""

    reap_stale_live_machine_control_operations(db)
    started_at = _now()
    operation = LiveMachineControlOperation(
        id=operation_id,
        owner_id=owner_id,
        session_id=session_id,
        device_id=device_id,
        command_type=command_type,
        command_id=command_id,
        provider=provider,
        status="running",
        request_json=_json_dump(dict(request_payload)),
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


def fail_live_machine_control_operation(
    db: Session,
    operation: LiveMachineControlOperation,
    *,
    code: str,
    message: str,
) -> None:
    if str(operation.status) in TERMINAL_OPERATION_STATUSES:
        return
    finished_at = _now()
    operation.status = "failed"
    operation.error_json = _json_dump({"code": code, "message": message})
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


def get_live_machine_control_operation_for_owner(
    db: Session,
    *,
    owner_id: int,
    operation_id: str,
) -> LiveMachineControlOperation | None:
    reap_stale_live_machine_control_operations(db)
    return (
        db.query(LiveMachineControlOperation)
        .filter(LiveMachineControlOperation.id == operation_id)
        .filter(LiveMachineControlOperation.owner_id == owner_id)
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


def reconcile_live_machine_control_operation_from_command_result(
    db: Session,
    message: dict[str, Any],
    *,
    owner_id: int,
    device_id: str,
) -> bool:
    """Apply an unmatched Machine Agent command_result to a Live Store operation."""

    command_id = str(message.get("command_id") or "").strip()
    if not command_id.startswith(LIVE_CONTROL_COMMAND_PREFIXES):
        return False
    operation = (
        db.query(LiveMachineControlOperation)
        .filter(LiveMachineControlOperation.command_id == command_id)
        .filter(LiveMachineControlOperation.owner_id == owner_id)
        .filter(LiveMachineControlOperation.device_id == device_id)
        .first()
    )
    if operation is None:
        return False
    if str(operation.status) in TERMINAL_OPERATION_STATUSES:
        return True

    if message.get("ok"):
        result = message.get("result")
        finish_live_machine_control_operation(
            db,
            operation,
            status="succeeded",
            result=dict(result) if isinstance(result, dict) else {},
        )
    else:
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        finish_live_machine_control_operation(
            db,
            operation,
            status="failed",
            error={
                "code": str(error.get("code") or "machine_control_operation_failed"),
                "message": str(error.get("message") or "Machine Agent control command failed"),
            },
        )
    return True


def finish_live_machine_control_operation(
    db: Session,
    operation: LiveMachineControlOperation,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Mark a Live Store machine-control operation terminal."""

    if str(operation.status) in TERMINAL_OPERATION_STATUSES:
        return
    finished_at = _now()
    operation.status = status
    operation.result_json = _json_dump(result or {}) if result is not None else None
    operation.error_json = _json_dump(error or {}) if error is not None else None
    operation.finished_at = finished_at
    operation.updated_at = finished_at
    operation.expires_at = None
    db.add(operation)
    db.commit()


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


def reap_stale_live_machine_control_operations(db: Session, *, now: datetime | None = None) -> int:
    cutoff = now or _now()
    stale = (
        db.query(LiveMachineControlOperation)
        .filter(LiveMachineControlOperation.status.in_(NONTERMINAL_OPERATION_STATUSES))
        .filter(LiveMachineControlOperation.expires_at.is_not(None))
        .filter(LiveMachineControlOperation.expires_at <= cutoff)
        .all()
    )
    for operation in stale:
        finished_at = _aware(operation.expires_at) or cutoff
        operation.status = "timed_out"
        operation.error_json = _json_dump(
            {
                "code": "machine_control_operation_timeout",
                "message": "Machine Agent did not report back before the operation lease expired",
            }
        )
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
        "request": _json_value(operation.request_json) or {},
        "result": _json_value(operation.result_json),
        "error": _json_value(operation.error_json),
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "timeout_secs": operation.timeout_secs,
    }


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)


def _json_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
