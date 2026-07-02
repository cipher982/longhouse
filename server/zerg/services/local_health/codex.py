from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_BRIDGE_STATE
from zerg.services.longhouse_paths import get_managed_local_dir

from ._shared import _max_rfc3339
from ._shared import _normalize_optional_int
from ._shared import _normalize_optional_string
from ._shared import _provider_cli_reference
from .bindings import _binding_by_session_id
from .bindings import _normalize_binding_path
from .constants import _THREAD_SUBSCRIPTION_TRANSIENT_STATES
from .constants import CODEX_BRIDGE_LIVE_DROPPED_MARKER
from .constants import CODEX_BRIDGE_LIVE_RETRY_MARKER
from .constants import CODEX_BRIDGE_LIVE_SLOW_MARKER
from .constants import CODEX_BRIDGE_LOG_TAIL_BYTES
from .constants import CODEX_BRIDGE_RUNTIME_FAILED_MARKER
from .constants import CODEX_BRIDGE_RUNTIME_NETWORK_ERROR_MARKER
from .constants import CONTROL_PATH_MANAGED
from .constants import LIVENESS_MODEL_CODEX_BRIDGE
from .phase import _phase_display_label
from .process import _process_row_by_pid
from .process import _process_row_is_zombie


def _codex_source_is_subagent(source: Any) -> bool:
    if not isinstance(source, Mapping):
        return False
    if isinstance(source.get("subagent"), Mapping):
        return True
    if isinstance(source.get("subAgent"), Mapping):
        return True
    if isinstance(source.get("sub_agent"), Mapping):
        return True
    return False


def _codex_rollout_is_subagent(path: str | None) -> bool:
    normalized = _normalize_optional_string(path)
    if normalized is None:
        return False
    try:
        with Path(normalized).expanduser().open() as handle:
            for index, raw_line in enumerate(handle):
                if index >= 32:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") == "session_meta":
                    meta = payload.get("payload")
                    if isinstance(meta, Mapping) and _codex_source_is_subagent(meta.get("source")):
                        return True
                meta = payload.get("session_meta")
                if isinstance(meta, Mapping) and _codex_source_is_subagent(meta.get("source")):
                    return True
                message = payload.get("message")
                if isinstance(message, Mapping) and _codex_source_is_subagent(message.get("source")):
                    return True
    except OSError:
        return False
    return False


def _looks_like_subagent_control_error(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return "subagent" in lowered and "managed primary" in lowered


def _codex_bridge_state_dir(base_dir: Path) -> Path:
    return get_managed_local_dir("codex-bridge", base_dir=base_dir)


def _codex_bridge_live_runtime_ingest_health(log_file: str | None) -> dict[str, Any] | None:
    path = Path(log_file) if log_file else None
    if path is None:
        return None

    health: dict[str, Any] = {
        "status": "unknown",
        "log_file": str(path),
        "exists": path.exists(),
        "tail_bytes": 0,
        "retry_count": 0,
        "network_error_count": 0,
        "failed_count": 0,
        "dropped_count": 0,
        "cloudflare_502_count": 0,
        "slow_count": 0,
        "slow_max_elapsed_ms": None,
        "slow_max_queue_wait_ms": None,
        "slow_max_exec_ms": None,
    }
    if not path.exists():
        return health

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > CODEX_BRIDGE_LOG_TAIL_BYTES:
                fh.seek(size - CODEX_BRIDGE_LOG_TAIL_BYTES)
            raw = fh.read(CODEX_BRIDGE_LOG_TAIL_BYTES)
    except OSError as exc:
        health["error"] = str(exc)
        return health

    health["tail_bytes"] = len(raw)
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if CODEX_BRIDGE_LIVE_RETRY_MARKER in line:
            health["retry_count"] += 1
        if CODEX_BRIDGE_RUNTIME_NETWORK_ERROR_MARKER in line:
            health["network_error_count"] += 1
        if CODEX_BRIDGE_RUNTIME_FAILED_MARKER in line:
            health["failed_count"] += 1
        if CODEX_BRIDGE_LIVE_DROPPED_MARKER in line:
            health["dropped_count"] += 1
        if "Error 502" in line or "Bad gateway" in line or "Cloudflare Ray ID" in line:
            health["cloudflare_502_count"] += 1
        if CODEX_BRIDGE_LIVE_SLOW_MARKER in line:
            health["slow_count"] += 1
            _update_max_health_number(health, "slow_max_elapsed_ms", _extract_log_number(line, "elapsed_ms"))
            _update_max_health_number(health, "slow_max_queue_wait_ms", _extract_log_number(line, "queue_wait_ms"))
            _update_max_health_number(health, "slow_max_exec_ms", _extract_log_number(line, "exec_ms"))

    terminal_failures = sum(
        [
            health["network_error_count"],
            health["failed_count"],
            health["dropped_count"],
            health["cloudflare_502_count"],
        ]
    )
    if terminal_failures:
        health["status"] = "broken"
    elif health["retry_count"] or health["slow_count"]:
        health["status"] = "degraded"
    else:
        health["status"] = "healthy"
    return health


def _extract_log_number(line: str, name: str) -> float | None:
    match = re.search(rf"{re.escape(name)}=(?:Some\()?([0-9]+(?:\.[0-9]+)?)", line)
    if not match:
        return None
    return float(match.group(1))


def _update_max_health_number(health: dict[str, Any], key: str, value: float | None) -> None:
    if value is None:
        return
    current = health.get(key)
    if current is None or value > float(current):
        health[key] = value


def _bridge_is_alive(state_file: Path) -> bool:
    """Check if a codex bridge daemon is alive via its process-lifetime flock.

    The engine daemon holds an exclusive advisory lock on a sidecar `.lock`
    file for its entire process lifetime. The kernel releases the lock on
    process exit (normal, crash, or SIGKILL), making this probe immune to
    PID reuse.

    Returns True if the lock is held (bridge is alive). Returns False if the
    lock can be acquired or is missing (bridge is gone).
    """
    import fcntl

    lock_path = state_file.with_suffix(".lock")
    if not lock_path.exists():
        return False

    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        return False

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock held by live bridge.
            return True
        # We acquired the lock — bridge is gone. Release immediately.
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    return False


def _purge_stale_bridge_files(state_file: Path) -> None:
    """Remove a dead bridge's state file and its sidecars.

    Called once the flock probe confirms no process owns the bridge.
    """
    for suffix in (".json", ".lock", ".sock"):
        candidate = state_file.with_suffix(suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _find_attached_codex_process(process_rows: list[dict[str, Any]], ws_url: str | None) -> dict[str, Any] | None:
    normalized_ws_url = _normalize_optional_string(ws_url)
    if normalized_ws_url is None:
        return None

    for row in process_rows:
        command = str(row.get("command") or "")
        if normalized_ws_url not in command:
            continue
        if "--remote" not in command:
            continue
        return row
    return None


def _find_bridge_child_process(
    process_rows: list[dict[str, Any]],
    *,
    bridge_pid: int,
    needle: str,
) -> dict[str, Any] | None:
    for row in process_rows:
        if int(row.get("ppid") or 0) != bridge_pid:
            continue
        command = str(row.get("command") or "")
        if needle in command:
            return row
    return None


def _find_codex_app_server_process(
    process_rows: list[dict[str, Any]],
    *,
    bridge_pid: int,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    by_recorded_pid = _process_row_by_pid(process_rows, state.get("app_server_pid"))
    if (
        by_recorded_pid is not None
        and not _process_row_is_zombie(by_recorded_pid)
        and " app-server " in str(by_recorded_pid.get("command") or "")
    ):
        return by_recorded_pid
    child = _find_bridge_child_process(process_rows, bridge_pid=bridge_pid, needle=" app-server ")
    if _process_row_is_zombie(child):
        return None
    return child


def _codex_orphan_bridge_row(
    *,
    state: dict[str, Any],
    bridge_pid: int,
    codex_bin: str | None,
    heartbeat_at: str | None,
    reason_codes: list[str],
    app_server: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "session_id": _normalize_optional_string(state.get("session_id")),
        "provider": "codex",
        "control_path": CONTROL_PATH_MANAGED,
        "liveness_model": LIVENESS_MODEL_CODEX_BRIDGE,
        "provider_cli": _provider_cli_reference(codex_bin, source=PROVIDER_CLI_SOURCE_BRIDGE_STATE),
        "pid": bridge_pid,
    }
    if app_server is not None:
        row["app_server_pid"] = app_server.get("pid")
    row.update(
        {
            "workspace_label": Path(str(state.get("cwd") or "")).name or None,
            "status": "orphan",
            "started_at": heartbeat_at,
            "heartbeat_at": heartbeat_at,
            "reason_codes": reason_codes,
        }
    )
    return row


def _codex_bridge_reason_codes(
    *,
    binding_path: str | None,
    state_thread_path: str | None,
    has_turn_activity: bool,
    last_error: str | None,
    bridge_status: str,
    thread_subscription_status: str | None,
    thread_subscription_last_error: str | None,
    app_server: dict[str, Any] | None,
) -> list[str]:
    reason_codes: list[str] = []
    rollout_missing_after_turn = bool(state_thread_path and not Path(state_thread_path).exists() and has_turn_activity)
    provider_thread_switched = thread_subscription_status == "provider_thread_switched" or any(
        "provider_thread_switched" in str(value or "") for value in (last_error, thread_subscription_last_error)
    )
    thread_subscription_failed = thread_subscription_status == "failed"
    thread_subscription_transitional = thread_subscription_status in _THREAD_SUBSCRIPTION_TRANSIENT_STATES
    thread_subscription_issue = bool(last_error or thread_subscription_failed)
    bridge_thread_is_subagent = False
    if binding_path and state_thread_path and binding_path != state_thread_path:
        thread_subscription_issue = True
        bridge_thread_is_subagent = _codex_rollout_is_subagent(state_thread_path)
    if rollout_missing_after_turn and not thread_subscription_transitional:
        thread_subscription_issue = True

    if provider_thread_switched:
        reason_codes.append("provider_thread_switched")
    elif thread_subscription_issue:
        if (
            bridge_thread_is_subagent
            or _looks_like_subagent_control_error(last_error)
            or _looks_like_subagent_control_error(thread_subscription_last_error)
        ):
            reason_codes.append("control_attached_to_subagent")
        else:
            reason_codes.append("thread_subscription_failed")
    if app_server is None:
        reason_codes.append("live_control_unavailable")
    if bridge_status != "ready":
        reason_codes.append("live_control_unavailable")
    return reason_codes


def _codex_ui_presence(
    *,
    state: str,
    launch_mode: str | None,
    ui_attached: bool,
    detached_ui_control_ready: bool,
) -> str | None:
    if state == "detached":
        return "detached"
    if state == "degraded":
        return "degraded"
    if launch_mode == "tui" and ui_attached:
        return "foreground_tui"
    if launch_mode == "detached_ui" and detached_ui_control_ready:
        return "background"
    return None


def _codex_managed_session_row(
    *,
    state: dict[str, Any],
    session_id: str | None,
    bridge_pid: int,
    codex_bin: str | None,
    bridge_status: str,
    bridge_heartbeat_at: str | None,
    attached_process: dict[str, Any] | None,
    app_server: dict[str, Any] | None,
    phase_state: dict[str, str | None] | None,
    thread_subscription_status: str | None,
    thread_subscription_attempts: int,
    thread_subscription_last_error: str | None,
    live_runtime_ingest_health: dict[str, Any] | None,
    reason_codes: list[str],
) -> dict[str, Any]:
    bridge_has_thread = _normalize_optional_string(state.get("thread_id")) is not None
    detached_ui_control_ready = bool(app_server is not None and bridge_status == "ready" and bridge_has_thread)
    normalized_state = "attached" if attached_process is not None or detached_ui_control_ready else "detached"
    if reason_codes:
        normalized_state = "degraded"
    launch_mode = _normalize_optional_string(state.get("launch_mode"))
    ui_attached = attached_process is not None
    ui_presence = _codex_ui_presence(
        state=normalized_state,
        launch_mode=launch_mode,
        ui_attached=ui_attached,
        detached_ui_control_ready=detached_ui_control_ready,
    )
    workspace_label = _normalize_optional_string(phase_state.get("workspace_label")) if phase_state else None
    if workspace_label is None:
        workspace_label = Path(str(state.get("cwd") or "")).name or None
    phase_observed_at = phase_state.get("observed_at") if phase_state else None
    phase_last_activity_at = phase_state.get("last_activity_at") if phase_state else None

    row = {
        "session_id": session_id,
        "provider": "codex",
        "control_path": CONTROL_PATH_MANAGED,
        "liveness_model": LIVENESS_MODEL_CODEX_BRIDGE,
        "provider_cli": _provider_cli_reference(codex_bin, source=PROVIDER_CLI_SOURCE_BRIDGE_STATE),
        "workspace_label": workspace_label,
        "branch": None,
        "state": normalized_state,
        "raw_phase": phase_state.get("phase") if phase_state else None,
        "phase": _phase_display_label(
            phase_state.get("phase") if phase_state else None,
            phase_state.get("tool_name") if phase_state else None,
        ),
        "phase_observed_at": phase_observed_at,
        "last_activity_at": _max_rfc3339(bridge_heartbeat_at, phase_last_activity_at, phase_observed_at),
        "bridge_status": bridge_status,
        "bridge_pid": bridge_pid,
        "launch_mode": launch_mode,
        "ui_attached": ui_attached,
        "ui_presence": ui_presence,
        "bridge_heartbeat_at": bridge_heartbeat_at,
        "thread_subscription_status": thread_subscription_status,
        "thread_subscription_attempts": thread_subscription_attempts,
        "thread_subscription_last_error": thread_subscription_last_error,
        "reason_codes": reason_codes,
    }
    if live_runtime_ingest_health is not None:
        row["live_runtime_ingest_health"] = live_runtime_ingest_health
    return row


def _collect_managed_codex_sessions(
    base_dir: Path,
    *,
    phase_overlay: dict[str, dict[str, str | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from zerg.services import local_health as _local_health_pkg

    state_dir = _local_health_pkg._codex_bridge_state_dir(base_dir)
    if not state_dir.exists():
        return [], []

    process_rows = _local_health_pkg._collect_process_rows()
    binding_by_session = _binding_by_session_id(base_dir)
    sessions: list[dict[str, Any]] = []
    orphan_bridges: list[dict[str, Any]] = []

    for path in sorted(state_dir.glob("*.json")):
        try:
            state = json.loads(path.read_text())
        except Exception:
            continue

        bridge_pid = int(state.get("pid") or 0)
        bridge_alive = _local_health_pkg._bridge_is_alive(path)
        app_server = _find_codex_app_server_process(process_rows, bridge_pid=bridge_pid, state=state)
        if not bridge_alive:
            if app_server is not None:
                bridge_updated_at = _normalize_optional_string(state.get("updated_at"))
                codex_bin = _normalize_optional_string(state.get("codex_bin"))
                orphan_bridges.append(
                    _codex_orphan_bridge_row(
                        state=state,
                        bridge_pid=bridge_pid,
                        codex_bin=codex_bin,
                        heartbeat_at=bridge_updated_at,
                        app_server=app_server,
                        reason_codes=["bridge_process_missing", "provider_child_alive"],
                    )
                )
                continue
            _purge_stale_bridge_files(path)
            continue

        ws_url = _normalize_optional_string(state.get("ws_url"))
        session_id = _normalize_optional_string(state.get("session_id"))
        bridge_updated_at = _normalize_optional_string(state.get("updated_at"))

        attached_process = _find_attached_codex_process(process_rows, ws_url)
        binding = binding_by_session.get(session_id or "")
        binding_path = _normalize_binding_path(binding.get("path")) if binding else None
        state_thread_path = _normalize_binding_path(state.get("thread_path"))
        last_error = _normalize_optional_string(state.get("last_error"))
        bridge_status = _normalize_optional_string(state.get("status")) or "unknown"
        thread_subscription_status = _normalize_optional_string(state.get("thread_subscription_status"))
        thread_subscription_attempts = _normalize_optional_int(state.get("thread_subscription_attempts")) or 0
        thread_subscription_last_error = _normalize_optional_string(state.get("thread_subscription_last_error"))
        codex_bin = _normalize_optional_string(state.get("codex_bin"))
        log_file = _normalize_optional_string(state.get("log_file"))
        live_runtime_ingest_health = _codex_bridge_live_runtime_ingest_health(log_file)
        bridge_heartbeat_at = bridge_updated_at
        active_turn_id = _normalize_optional_string(state.get("active_turn_id"))
        last_turn_status = _normalize_optional_string(state.get("last_turn_status"))
        has_turn_activity = bool(active_turn_id or last_turn_status)

        if binding is None:
            orphan_bridges.append(
                _codex_orphan_bridge_row(
                    state=state,
                    bridge_pid=bridge_pid,
                    codex_bin=codex_bin,
                    heartbeat_at=bridge_heartbeat_at,
                    reason_codes=["no_managed_session_bound"],
                )
            )
            continue

        reason_codes = _codex_bridge_reason_codes(
            binding_path=binding_path,
            state_thread_path=state_thread_path,
            has_turn_activity=has_turn_activity,
            last_error=last_error,
            bridge_status=bridge_status,
            thread_subscription_status=thread_subscription_status,
            thread_subscription_last_error=thread_subscription_last_error,
            app_server=app_server,
        )
        if live_runtime_ingest_health and live_runtime_ingest_health.get("status") in {"broken", "degraded"}:
            reason_codes.append("live_runtime_ingest_degraded")
        phase_state = phase_overlay.get(session_id or "") if phase_overlay else None

        sessions.append(
            _codex_managed_session_row(
                state=state,
                session_id=session_id,
                bridge_pid=bridge_pid,
                codex_bin=codex_bin,
                bridge_status=bridge_status,
                bridge_heartbeat_at=bridge_updated_at,
                attached_process=attached_process,
                app_server=app_server,
                phase_state=phase_state,
                thread_subscription_status=thread_subscription_status,
                thread_subscription_attempts=thread_subscription_attempts,
                thread_subscription_last_error=thread_subscription_last_error,
                live_runtime_ingest_health=live_runtime_ingest_health,
                reason_codes=reason_codes,
            )
        )

    return sessions, orphan_bridges


__all__ = [
    "_codex_source_is_subagent",
    "_codex_rollout_is_subagent",
    "_looks_like_subagent_control_error",
    "_codex_bridge_state_dir",
    "_codex_bridge_live_runtime_ingest_health",
    "_extract_log_number",
    "_update_max_health_number",
    "_bridge_is_alive",
    "_purge_stale_bridge_files",
    "_find_attached_codex_process",
    "_find_bridge_child_process",
    "_find_codex_app_server_process",
    "_codex_orphan_bridge_row",
    "_codex_bridge_reason_codes",
    "_codex_ui_presence",
    "_codex_managed_session_row",
    "_collect_managed_codex_sessions",
]
