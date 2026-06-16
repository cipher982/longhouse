"""Canonical remote-launch lifecycle projection.

The durable source of truth is ``SessionLaunchAttempt``. Legacy
``AgentSession.launch_*`` fields are compatibility write shims and must not be
read for product state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from typing import cast
from typing import get_args

from zerg.models.agents import SessionLaunchAttempt

RemoteExecutionLifetime = Literal["one_shot", "live_control"]
# Service normalization keeps the historical/live-control default for continuations and stored rows.
DEFAULT_REMOTE_EXECUTION_LIFETIME: RemoteExecutionLifetime = "live_control"
# Browser/iOS "launch new session" requests default to bounded one-shot execution.
DEFAULT_REMOTE_SESSION_LAUNCH_LIFETIME: RemoteExecutionLifetime = "one_shot"
# Browser/iOS continuation requests that carry a follow-up prompt should also
# be bounded; prompt-less continuation remains live-control for attach-style UI.
DEFAULT_REMOTE_CONTINUE_MESSAGE_LIFETIME: RemoteExecutionLifetime = "one_shot"
RemoteLaunchLifecycleState = Literal[
    "launching",
    "live",
    "launching_unknown",
    "launch_failed",
    "launch_orphaned",
]
RemoteLaunchErrorCode = Literal[
    "invalid_request",
    "device_not_enrolled",
    "provider_unsupported",
    "cwd_not_allowed",
    "cwd_not_found",
    "machine_offline",
    "provider_launch_failed",
    "transcript_not_found",
    "launch_timeout",
]
KNOWN_REMOTE_LAUNCH_ERROR_CODES = frozenset(get_args(RemoteLaunchErrorCode))
REMOTE_LAUNCH_ERROR_TITLES: dict[RemoteLaunchErrorCode, str] = {
    "invalid_request": "Launch request is invalid",
    "device_not_enrolled": "Machine is not enrolled",
    "provider_unsupported": "Launch is unavailable on this machine",
    "cwd_not_allowed": "Check the workspace path",
    "cwd_not_found": "Check the workspace path",
    "machine_offline": "Machine is offline",
    "provider_launch_failed": "Provider failed to start",
    "transcript_not_found": "Transcript no longer exists on this machine",
    "launch_timeout": "Launch timed out",
}


@dataclass(frozen=True)
class RemoteLaunchLifecycle:
    state: RemoteLaunchLifecycleState
    execution_lifetime: RemoteExecutionLifetime
    error_code: RemoteLaunchErrorCode | None
    error_message: str | None
    lease_until: datetime | None


def normalize_remote_launch_error_code(
    code: str | None,
    *,
    fallback: RemoteLaunchErrorCode = "provider_launch_failed",
) -> RemoteLaunchErrorCode:
    normalized = (code or "").strip()
    if normalized in KNOWN_REMOTE_LAUNCH_ERROR_CODES:
        return cast(RemoteLaunchErrorCode, normalized)
    return fallback


def normalize_remote_execution_lifetime(value: str | None) -> RemoteExecutionLifetime:
    normalized = str(value or "").strip()
    if normalized == "one_shot":
        return "one_shot"
    if normalized == "live_control":
        return "live_control"
    return DEFAULT_REMOTE_EXECUTION_LIFETIME


def format_remote_launch_error_message(
    code: RemoteLaunchErrorCode | None,
    message: str | None,
) -> str | None:
    title = REMOTE_LAUNCH_ERROR_TITLES.get(code) if code else None
    detail = (message or "").strip()
    if title and detail:
        return f"{title}: {detail}"
    if title:
        return title
    return detail or None


def project_remote_launch_lifecycle(attempt: SessionLaunchAttempt | None) -> RemoteLaunchLifecycle | None:
    """Project a launch attempt into the user-visible lifecycle contract."""

    if attempt is None:
        return None

    raw_state = str(attempt.state or "").strip()
    execution_lifetime = normalize_remote_execution_lifetime(getattr(attempt, "execution_lifetime", None))
    if raw_state == "failed":
        state: RemoteLaunchLifecycleState = "launch_failed"
    elif raw_state == "abandoned":
        state = "launch_orphaned"
    elif raw_state == "adopted":
        state = "live"
    elif attempt.run_id is not None and execution_lifetime == "live_control":
        state = "live"
    elif raw_state == "dispatched":
        state = "launching_unknown"
    else:
        state = "launching"

    error_code = normalize_remote_launch_error_code(attempt.error_code) if attempt.error_code is not None else None
    return RemoteLaunchLifecycle(
        state=state,
        execution_lifetime=execution_lifetime,
        error_code=error_code,
        error_message=format_remote_launch_error_message(error_code, attempt.error_message),
        lease_until=attempt.expires_at,
    )


__all__ = [
    "RemoteLaunchLifecycle",
    "RemoteLaunchErrorCode",
    "RemoteExecutionLifetime",
    "RemoteLaunchLifecycleState",
    "DEFAULT_REMOTE_EXECUTION_LIFETIME",
    "DEFAULT_REMOTE_SESSION_LAUNCH_LIFETIME",
    "DEFAULT_REMOTE_CONTINUE_MESSAGE_LIFETIME",
    "format_remote_launch_error_message",
    "normalize_remote_execution_lifetime",
    "normalize_remote_launch_error_code",
    "project_remote_launch_lifecycle",
]
