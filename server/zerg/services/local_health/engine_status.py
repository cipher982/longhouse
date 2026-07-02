from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path

from ._shared import _max_rfc3339
from ._shared import _normalize_optional_int
from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339
from .constants import CONTROL_PATH_MANAGED
from .constants import CONTROL_PATH_UNMANAGED
from .constants import ENGINE_FRESH_SECONDS
from .constants import LIVENESS_MODEL_ENGINE_STATUS
from .phase import _phase_display_label
from .process import _process_row_by_pid
from .process import _process_row_is_zombie


def _collect_build_identity(*, engine_status: dict[str, Any]) -> dict[str, Any]:
    """Compare CLI build identity against engine build identity.

    Returns the CLI identity, engine identity (short SHA only, since that's
    all we need for drift detection), and whether the running engine daemon
    is behind the installed/on-disk binary.

    We report "engine restart pending" instead of a scary drift pill when:
    - the engine daemon's build commit_short differs from the installed CLI's, OR
    - the engine daemon's current_exe mtime is newer than its daemon_started_at
      (binary replaced on disk since the daemon started — e.g. after `make
      install-engine`).

    This is a benign state, not an error. Daemons don't hot-reload; the user
    just needs to restart the shipper when convenient.
    """
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load as load_build_identity

    installed_block: dict[str, Any]
    try:
        cli = load_build_identity()
        installed_block = cli.as_dict()
        installed_short: str | None = cli.commit_short
    except BuildIdentityMissing as exc:
        installed_block = {"error": "missing", "detail": str(exc)}
        installed_short = None

    # engine-status.json is engine-controlled but user-writable; guard
    # against corrupt payloads so local-health degrades cleanly instead of
    # raising and taking the whole menu bar snapshot down.
    raw_payload = engine_status.get("payload") if engine_status else None
    engine_payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
    raw_engine_build = engine_payload.get("build")
    engine_build: Mapping[str, Any] = raw_engine_build if isinstance(raw_engine_build, Mapping) else {}
    engine_short = engine_build.get("commit_short") if engine_build else None

    binary_mtime_raw = engine_payload.get("binary_mtime") if engine_payload else None
    daemon_started_at_raw = engine_payload.get("daemon_started_at") if engine_payload else None
    binary_mtime = _parse_iso8601(binary_mtime_raw)
    daemon_started_at = _parse_iso8601(daemon_started_at_raw)

    commit_mismatch = bool(installed_short and engine_short and installed_short != engine_short)
    binary_times_present = binary_mtime is not None and daemon_started_at is not None
    binary_newer_than_daemon = bool(binary_times_present and binary_mtime > daemon_started_at)
    engine_restart_pending = commit_mismatch or binary_newer_than_daemon

    return {
        "installed": installed_block,
        "engine": engine_build if engine_build else None,
        "engine_restart_pending": engine_restart_pending,
        "restart_pending_reasons": {
            "commit_mismatch": commit_mismatch,
            "binary_newer_than_daemon": binary_newer_than_daemon,
        },
        "components": [
            component
            for component in (
                {"name": "installed", "commit_short": installed_short} if installed_short else None,
                {"name": "engine", "commit_short": engine_short} if engine_short else None,
            )
            if component is not None
        ],
    }


def _parse_iso8601(value: Any) -> datetime | None:
    """Parse an ISO-8601 string emitted by the engine. Returns None on any
    failure — the caller treats a missing/unparseable value as "can't tell"
    and falls back to commit_short comparison alone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # datetime.fromisoformat in Py 3.11+ handles most RFC 3339 shapes,
        # including the fractional seconds + offset the engine emits.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_engine_status(base_dir: Path, *, now: datetime) -> dict[str, Any]:
    status_path = get_agent_status_path(base_dir)
    if not status_path.exists():
        return {
            "path": str(status_path),
            "exists": False,
            "fresh": False,
            "age_seconds": None,
            "payload": None,
            "error": None,
        }

    try:
        age_seconds = int(max(0.0, now.timestamp() - status_path.stat().st_mtime))
    except OSError as exc:
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": None,
            "payload": None,
            "error": str(exc),
        }

    try:
        payload = json.loads(status_path.read_text())
    except Exception as exc:
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": age_seconds,
            "payload": None,
            "error": str(exc),
        }
    if not isinstance(payload, Mapping):
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": age_seconds,
            "payload": None,
            "error": "engine status payload must be a JSON object",
        }

    return {
        "path": str(status_path),
        "exists": True,
        "fresh": age_seconds <= ENGINE_FRESH_SECONDS,
        "age_seconds": age_seconds,
        "payload": payload,
        "error": None,
    }


def _collect_outbox(base_dir: Path, *, now: datetime) -> dict[str, Any]:
    outbox_dir = get_agent_outbox_dir(base_dir)
    if not outbox_dir.exists():
        return {
            "path": str(outbox_dir),
            "file_count": 0,
            "oldest_age_seconds": None,
        }

    files = []
    for path in outbox_dir.iterdir():
        if path.is_file() and path.name.endswith(".json") and not path.name.startswith("."):
            files.append(path)
    if not files:
        return {
            "path": str(outbox_dir),
            "file_count": 0,
            "oldest_age_seconds": None,
        }

    oldest_age_seconds: int | None = None
    for path in files:
        try:
            age_seconds = int(max(0.0, now.timestamp() - path.stat().st_mtime))
        except OSError:
            continue
        oldest_age_seconds = age_seconds if oldest_age_seconds is None else max(oldest_age_seconds, age_seconds)

    return {
        "path": str(outbox_dir),
        "file_count": len(files),
        "oldest_age_seconds": oldest_age_seconds,
    }


def _engine_status_payload(engine_status: dict[str, Any]) -> Mapping[str, Any]:
    raw_payload = engine_status.get("payload") if engine_status else None
    return raw_payload if isinstance(raw_payload, Mapping) else {}


def _engine_status_managed_session_row(
    *,
    raw_row: Mapping[str, Any],
    phase_overlay: dict[str, dict[str, str | None]] | None,
) -> dict[str, Any]:
    session_id = _normalize_optional_string(raw_row.get("session_id"))
    provider = _normalize_optional_string(raw_row.get("provider")) or "unknown"
    state = _normalize_optional_string(raw_row.get("state")) or "unknown"
    observed_at = _normalize_optional_string(raw_row.get("observed_at"))
    raw_phase = _normalize_optional_string(raw_row.get("phase"))
    tool_name = _normalize_optional_string(raw_row.get("tool_name"))
    phase_state = phase_overlay.get(session_id or "") if phase_overlay else None
    overlay_phase = _normalize_optional_string(phase_state.get("phase")) if phase_state else None
    overlay_tool = _normalize_optional_string(phase_state.get("tool_name")) if phase_state else None
    phase_observed_at = _normalize_optional_string(phase_state.get("observed_at")) if phase_state else None
    phase_last_activity_at = _normalize_optional_string(phase_state.get("last_activity_at")) if phase_state else None
    workspace_label = _normalize_optional_string(phase_state.get("workspace_label")) if phase_state else None
    workspace_path = _normalize_optional_string(phase_state.get("workspace_path")) if phase_state else None
    display_phase = overlay_phase if overlay_phase is not None else raw_phase
    display_tool = overlay_tool if overlay_phase is not None else tool_name

    return {
        "session_id": session_id,
        "provider": provider,
        "control_path": CONTROL_PATH_MANAGED,
        "liveness_model": LIVENESS_MODEL_ENGINE_STATUS,
        "provider_cli": None,
        "workspace_label": workspace_label,
        "cwd": workspace_path,
        "branch": None,
        "state": state,
        "raw_phase": display_phase,
        "phase": _phase_display_label(display_phase, display_tool),
        "phase_observed_at": phase_observed_at or observed_at,
        "last_activity_at": _max_rfc3339(observed_at, phase_last_activity_at, phase_observed_at),
        "bridge_status": _normalize_optional_string(raw_row.get("bridge_status")),
        "bridge_pid": None,
        "bridge_heartbeat_at": observed_at,
        "thread_subscription_status": _normalize_optional_string(raw_row.get("thread_subscription_status")),
        "reason_codes": [],
    }


def _collect_managed_sessions_from_engine_status(
    engine_status: dict[str, Any],
    *,
    phase_overlay: dict[str, dict[str, str | None]] | None = None,
) -> list[dict[str, Any]]:
    payload = _engine_status_payload(engine_status)
    raw_rows = payload.get("managed_sessions")
    if not isinstance(raw_rows, list):
        return []

    sessions: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        sessions.append(_engine_status_managed_session_row(raw_row=raw_row, phase_overlay=phase_overlay))

    return sessions


def _engine_status_resolved_sessions(engine_status: dict[str, Any]) -> list[Any] | None:
    payload = _engine_status_payload(engine_status)
    if "sessions" not in payload:
        return None
    raw_rows = payload.get("sessions")
    if not isinstance(raw_rows, list):
        return None
    return raw_rows


def _engine_status_resolved_sessions_issue(engine_status: dict[str, Any]) -> str | None:
    payload = _engine_status_payload(engine_status)
    if "sessions" not in payload:
        return "missing"
    if not isinstance(payload.get("sessions"), list):
        return "invalid"
    return None


def _resolved_session_mapping(raw_row: Any, field_name: str) -> Mapping[str, Any]:
    raw_value = raw_row.get(field_name) if isinstance(raw_row, Mapping) else None
    if isinstance(raw_value, Mapping):
        return raw_value
    return {}


def _resolved_session_state(raw_row: Mapping[str, Any]) -> str:
    normalized_state = _normalize_optional_string(raw_row.get("state"))
    if normalized_state in {"attached", "detached", "degraded"}:
        return normalized_state

    presentation_state = _normalize_optional_string(raw_row.get("presentation_state"))
    if presentation_state == "managed_attached":
        return "attached"
    if presentation_state == "managed_detached":
        return "detached"
    if presentation_state == "managed_degraded":
        return "degraded"
    if presentation_state == "stale_evidence":
        return "degraded"
    return normalized_state or "unknown"


def _resolved_join_key_value(evidence: Mapping[str, Any], prefix: str) -> str | None:
    raw_join_keys = evidence.get("join_keys")
    if not isinstance(raw_join_keys, list):
        return None
    match_prefix = f"{prefix}="
    for raw_key in raw_join_keys:
        key = _normalize_optional_string(raw_key)
        if key and key.startswith(match_prefix):
            return key[len(match_prefix) :] or None
    return None


def _resolved_engine_managed_session_row(
    *,
    raw_row: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _normalize_optional_string(raw_row.get("session_id"))
    provider = _normalize_optional_string(raw_row.get("provider")) or "unknown"
    state = _resolved_session_state(raw_row)
    workspace = _resolved_session_mapping(raw_row, "workspace")
    bridge = _resolved_session_mapping(raw_row, "bridge")
    evidence = _resolved_session_mapping(raw_row, "evidence")

    raw_phase = _normalize_optional_string(raw_row.get("phase"))
    tool_name = _normalize_optional_string(raw_row.get("tool_name"))
    row_phase_observed_at = _normalize_optional_string(raw_row.get("phase_observed_at"))
    last_activity_at = _normalize_optional_string(raw_row.get("last_activity_at"))
    bridge_heartbeat_at = _normalize_optional_string(bridge.get("heartbeat_at"))
    reason_codes = list(raw_row.get("reason_codes") or []) if isinstance(raw_row.get("reason_codes"), list) else []

    return {
        "session_id": session_id,
        "provider": provider,
        "provider_session_id": _normalize_optional_string(raw_row.get("provider_session_id")),
        "control_path": CONTROL_PATH_MANAGED,
        "liveness_model": LIVENESS_MODEL_ENGINE_STATUS,
        "provider_cli": None,
        "workspace_label": _normalize_optional_string(workspace.get("label")),
        "cwd": _normalize_optional_string(workspace.get("cwd")),
        "branch": _normalize_optional_string(workspace.get("branch")),
        "state": state,
        "raw_phase": raw_phase,
        "phase": _phase_display_label(raw_phase, tool_name),
        "phase_observed_at": row_phase_observed_at,
        "last_activity_at": _max_rfc3339(last_activity_at, row_phase_observed_at),
        "bridge_status": _normalize_optional_string(bridge.get("status")),
        "bridge_pid": _normalize_optional_int(bridge.get("bridge_pid")),
        "app_server_pid": _normalize_optional_int(bridge.get("app_server_pid")),
        "launch_mode": _normalize_optional_string(bridge.get("launch_mode")),
        "ui_attached": bridge.get("ui_attached") if isinstance(bridge.get("ui_attached"), bool) else None,
        "ui_presence": _normalize_optional_string(bridge.get("ui_presence")),
        "bridge_heartbeat_at": bridge_heartbeat_at,
        "thread_subscription_status": _normalize_optional_string(bridge.get("thread_subscription_status")),
        "reason_codes": reason_codes,
        "evidence": dict(evidence),
    }


def _resolved_engine_unmanaged_process_row(raw_row: Mapping[str, Any]) -> dict[str, Any]:
    workspace = _resolved_session_mapping(raw_row, "workspace")
    process = _resolved_session_mapping(raw_row, "process")
    evidence = _resolved_session_mapping(raw_row, "evidence")
    cwd = _normalize_optional_string(workspace.get("cwd"))
    observed_at = (
        _normalize_optional_string(raw_row.get("last_activity_at"))
        or _normalize_optional_string(raw_row.get("phase_observed_at"))
        or _normalize_optional_string(evidence.get("hook_seen_at"))
    )
    started_at = (
        _normalize_optional_string(process.get("started_at"))
        or _normalize_optional_string(process.get("process_start_time"))
        or observed_at
    )
    source_path = _resolved_join_key_value(evidence, "source_path")
    return {
        "provider": _normalize_optional_string(raw_row.get("provider")),
        "control_path": CONTROL_PATH_UNMANAGED,
        "liveness_model": LIVENESS_MODEL_ENGINE_STATUS,
        "provider_cli": None,
        "pid": _normalize_optional_int(process.get("pid")),
        "workspace_label": _normalize_optional_string(workspace.get("label")) or (Path(cwd).name if cwd else None),
        "cwd": cwd,
        "branch": _normalize_optional_string(workspace.get("branch")),
        "started_at": started_at,
        "provider_session_id": _normalize_optional_string(raw_row.get("provider_session_id")),
        "source_path": source_path,
        "observed_at": observed_at,
        "evidence": dict(evidence),
    }


def _collect_resolved_sessions_from_engine_status(
    engine_status: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    raw_rows = _engine_status_resolved_sessions(engine_status)
    if raw_rows is None:
        return None

    managed_sessions: list[dict[str, Any]] = []
    unmanaged_processes: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        control_path = _normalize_optional_string(raw_row.get("control_path"))
        presentation_state = _normalize_optional_string(raw_row.get("presentation_state"))
        if control_path == CONTROL_PATH_MANAGED:
            managed_sessions.append(_resolved_engine_managed_session_row(raw_row=raw_row))
        elif control_path == CONTROL_PATH_UNMANAGED or presentation_state == CONTROL_PATH_UNMANAGED:
            unmanaged_processes.append(_resolved_engine_unmanaged_process_row(raw_row))

    unmanaged_processes.sort(
        key=lambda row: _parse_rfc3339(row.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return managed_sessions, unmanaged_processes


def _mark_managed_session_degraded(row: dict[str, Any], reason: str) -> dict[str, Any]:
    reason_codes = list(row.get("reason_codes") or []) if isinstance(row.get("reason_codes"), list) else []
    if reason not in reason_codes:
        reason_codes.append(reason)
    row["reason_codes"] = reason_codes
    if row.get("state") == "attached":
        row["state"] = "degraded"
    if row.get("ui_presence") == "background":
        row["ui_presence"] = "degraded"
    return row


def _resolved_engine_session_app_server_is_live(row: Mapping[str, Any], process_rows: list[dict[str, Any]]) -> bool:
    app_server_pid = _normalize_optional_int(row.get("app_server_pid"))
    if app_server_pid is None:
        return False
    process_row = _process_row_by_pid(process_rows, app_server_pid)
    if process_row is None or _process_row_is_zombie(process_row):
        return False
    return " app-server " in str(process_row.get("command") or "")


def _resolved_engine_opencode_server_is_live(row: Mapping[str, Any], process_rows: list[dict[str, Any]]) -> bool:
    # OpenCode's resolved bridge_pid is the `opencode serve` process pid. The
    # server is live only if that pid is a live, non-zombie opencode process.
    server_pid = _normalize_optional_int(row.get("bridge_pid"))
    if server_pid is None:
        return False
    process_row = _process_row_by_pid(process_rows, server_pid)
    if process_row is None or _process_row_is_zombie(process_row):
        return False
    command = str(process_row.get("command") or "")
    return "opencode" in command and " serve" in command


def _validate_resolved_engine_managed_sessions(
    managed_sessions: list[dict[str, Any]],
    *,
    process_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not process_rows:
        return managed_sessions
    validated: list[dict[str, Any]] = []
    for row in managed_sessions:
        row = dict(row)
        if row.get("provider") == "codex" and row.get("bridge_status") == "ready":
            if not _resolved_engine_session_app_server_is_live(row, process_rows):
                row = _mark_managed_session_degraded(row, "live_control_unavailable")
        elif row.get("provider") == "opencode" and row.get("bridge_status") == "ready":
            if not _resolved_engine_opencode_server_is_live(row, process_rows):
                row = _mark_managed_session_degraded(row, "live_control_unavailable")
        validated.append(row)
    return validated


def _resolved_sessions_unusable_summary(issue: str | None) -> dict[str, Any]:
    summary = {
        "attached_count": 0,
        "detached_count": 0,
        "degraded_count": 0,
        "orphan_bridge_count": 0,
        "latest_activity_at": None,
    }
    if issue == "invalid":
        summary["canonical_sessions_invalid"] = True
    else:
        summary["canonical_sessions_missing"] = True
    return summary


__all__ = [
    "_collect_build_identity",
    "_parse_iso8601",
    "_collect_engine_status",
    "_collect_outbox",
    "_engine_status_payload",
    "_engine_status_managed_session_row",
    "_collect_managed_sessions_from_engine_status",
    "_engine_status_resolved_sessions",
    "_engine_status_resolved_sessions_issue",
    "_resolved_session_mapping",
    "_resolved_session_state",
    "_resolved_join_key_value",
    "_resolved_engine_managed_session_row",
    "_resolved_engine_unmanaged_process_row",
    "_collect_resolved_sessions_from_engine_status",
    "_mark_managed_session_degraded",
    "_resolved_engine_session_app_server_is_live",
    "_resolved_engine_opencode_server_is_live",
    "_validate_resolved_engine_managed_sessions",
    "_resolved_sessions_unusable_summary",
]
