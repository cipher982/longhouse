"""Machine Agent control operation status vocabulary and response projection.

The operation lifecycle itself is implemented inline by catalogd; this module
owns the shared status sets, the in-flight conflict error, and the durable-row
to-API projection.
"""

from __future__ import annotations

import json
from typing import Any

from zerg.models import MachineControlOperation

MACHINE_OPERATION_TIMEOUT_GRACE_SECS = 30
NONTERMINAL_OPERATION_STATUSES = {"queued", "running"}
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "timed_out"}


class ActiveMachineControlOperationError(RuntimeError):
    """Raised when an active operation already exists for the same target."""


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
