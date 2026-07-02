from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PROCESS

from ._shared import _max_rfc3339
from ._shared import _normalize_optional_int
from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339
from ._shared import _provider_cli_reference
from .constants import _UUID_RE
from .constants import _ZOMBIE_PROCESS_STATUSES
from .constants import CONTROL_PATH_MANAGED
from .constants import CONTROL_PATH_UNMANAGED
from .constants import LIVENESS_MODEL_PROCESS_SCAN
from .phase import _phase_display_label

_PROCESS_SNAPSHOT: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None


def _compute_process_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import psutil  # imported lazily to keep module import cheap
    except ImportError:
        return [], []

    me = os.getuid()
    process_rows: list[dict[str, Any]] = []
    provider_processes: list[dict[str, Any]] = []

    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "create_time", "status"]):
        try:
            info = proc.info
            cmdline = [str(arg) for arg in (info.get("cmdline") or []) if str(arg)]
            command = " ".join(cmdline)
            pid = int(info.get("pid") or 0)
            ppid = int(info.get("ppid") or 0)
            status = _normalize_optional_string(info.get("status"))
            if pid > 0 and command:
                process_rows.append({"pid": pid, "ppid": ppid, "command": command, "status": status})

            if proc.uids().real != me:
                continue
            if not cmdline:
                continue

            provider = _provider_for_cmdline(cmdline)
            if not provider:
                continue
            if provider == "codex" and any(arg == "app-server" for arg in cmdline[1:]):
                continue

            try:
                env = proc.environ()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                env = {}

            session_id = _normalize_optional_string(env.get("LONGHOUSE_MANAGED_SESSION_ID")) if env else None
            if not session_id:
                session_id = _session_id_from_argv(cmdline)

            device_id = env.get("LONGHOUSE_DEVICE_ID") if env else None

            try:
                cwd = proc.cwd()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                cwd = None

            started_at = (
                datetime.fromtimestamp(float(info["create_time"]), tz=timezone.utc)
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            )

            provider_processes.append(
                {
                    "session_id": session_id,
                    "provider": provider,
                    "provider_cli": _provider_cli_reference(
                        cmdline[0] if cmdline else None,
                        source=PROVIDER_CLI_SOURCE_PROCESS,
                    ),
                    "pid": pid,
                    "cwd": cwd,
                    "workspace_label": Path(cwd).name if cwd else None,
                    "device_id": device_id,
                    "started_at": started_at,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, KeyError, TypeError, ValueError):
            continue

    return process_rows, provider_processes


@contextmanager
def _process_snapshot_scope():
    global _PROCESS_SNAPSHOT

    # Resolved through the package namespace (not the bare module-local name)
    # so `monkeypatch.setattr(zerg.services.local_health, "_compute_process_snapshot", ...)`
    # in tests still takes effect after the package split.
    from zerg.services import local_health as _local_health_pkg

    previous = _PROCESS_SNAPSHOT
    _PROCESS_SNAPSHOT = _local_health_pkg._compute_process_snapshot()
    try:
        yield
    finally:
        _PROCESS_SNAPSHOT = previous


def _collect_process_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if _PROCESS_SNAPSHOT is not None:
        return _PROCESS_SNAPSHOT
    from zerg.services import local_health as _local_health_pkg

    return _local_health_pkg._compute_process_snapshot()


def _collect_process_rows() -> list[dict[str, Any]]:
    process_rows, _ = _collect_process_snapshot()
    return process_rows


def _process_row_by_pid(process_rows: list[dict[str, Any]], pid: object) -> dict[str, Any] | None:
    pid_int = _normalize_optional_int(pid)
    if not pid_int:
        return None
    for row in process_rows:
        if int(row.get("pid") or 0) == pid_int:
            return row
    return None


def _process_row_is_zombie(row: Mapping[str, Any] | None) -> bool:
    status = _normalize_optional_string(row.get("status") if row else None)
    if status is None:
        return False
    normalized = status.lower()
    return normalized in _ZOMBIE_PROCESS_STATUSES or normalized.startswith("z")


def _is_claude_cmdline(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    exe = cmdline[0].rsplit("/", 1)[-1]
    return exe == "claude"


def _is_codex_cmdline(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    exe = cmdline[0].rsplit("/", 1)[-1]
    if exe == "codex":
        return True
    joined = " ".join(cmdline)
    return "/codex/codex" in joined or "codex-darwin" in joined


def _is_opencode_cmdline(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    exe = cmdline[0].rsplit("/", 1)[-1]
    if exe == "opencode":
        return True
    if exe.startswith("longhouse-"):
        return False
    if exe not in {"node", "nodejs", "bun"}:
        return False
    script = cmdline[1].rsplit("/", 1)[-1] if len(cmdline) > 1 else ""
    return script in {"opencode", "opencode.js"}


def _is_antigravity_cmdline(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    exe = cmdline[0].rsplit("/", 1)[-1]
    if exe in {"agy", "antigravity"}:
        return True
    if exe.startswith("longhouse-"):
        return False
    if exe not in {"node", "nodejs", "bun"}:
        return False
    script = cmdline[1].rsplit("/", 1)[-1] if len(cmdline) > 1 else ""
    if script.startswith("longhouse-"):
        return False
    return script in {"agy", "agy.js", "antigravity", "antigravity.js"}


def _provider_for_cmdline(cmdline: list[str]) -> str | None:
    if _is_claude_cmdline(cmdline):
        return "claude"
    if _is_codex_cmdline(cmdline):
        return "codex"
    if _is_opencode_cmdline(cmdline):
        return "opencode"
    if _is_antigravity_cmdline(cmdline):
        return "antigravity"
    return None


def _session_id_from_argv(cmdline: list[str]) -> str | None:
    """Claude emits `--session-id <uuid>`. Codex does not pass it on argv."""
    for index, arg in enumerate(cmdline):
        if arg == "--session-id" and index + 1 < len(cmdline):
            candidate = cmdline[index + 1]
            if _UUID_RE.fullmatch(candidate):
                return candidate
    return None


def _scan_provider_processes() -> list[dict[str, Any]]:
    """Collect live provider CLI processes owned by the current user."""
    _, provider_processes = _collect_process_snapshot()
    return provider_processes


def _process_managed_session_row(
    *,
    proc_row: dict[str, Any],
    session_id: str,
    provider: str,
    started_at: str,
    phase_state: dict[str, str | None] | None,
) -> dict[str, Any]:
    workspace_label = _normalize_optional_string(phase_state.get("workspace_label")) if phase_state else None
    if workspace_label is None:
        workspace_label = _normalize_optional_string(proc_row.get("workspace_label"))
    workspace_path = _normalize_optional_string(phase_state.get("workspace_path")) if phase_state else None
    if workspace_path is None:
        workspace_path = _normalize_optional_string(proc_row.get("cwd"))
    phase_observed_at = phase_state.get("observed_at") if phase_state else None
    phase_last_activity_at = phase_state.get("last_activity_at") if phase_state else None
    provider_cli = proc_row.get("provider_cli") or _provider_cli_reference(None, source=PROVIDER_CLI_SOURCE_PROCESS)

    return {
        "session_id": session_id,
        "provider": provider,
        "control_path": CONTROL_PATH_MANAGED,
        "liveness_model": LIVENESS_MODEL_PROCESS_SCAN,
        "provider_cli": provider_cli,
        "pid": proc_row.get("pid"),
        "workspace_label": workspace_label,
        "cwd": workspace_path,
        "device_id": _normalize_optional_string(proc_row.get("device_id")),
        "started_at": started_at,
        "branch": None,
        "state": "attached",
        "raw_phase": phase_state.get("phase") if phase_state else None,
        "phase": _phase_display_label(
            phase_state.get("phase") if phase_state else None,
            phase_state.get("tool_name") if phase_state else None,
        ),
        "phase_observed_at": phase_observed_at,
        "last_activity_at": _max_rfc3339(started_at, phase_last_activity_at, phase_observed_at) or started_at,
        "bridge_status": None,
        "bridge_pid": None,
        "bridge_heartbeat_at": None,
        "reason_codes": [],
    }


def _collect_managed_sessions_by_process(
    *,
    existing_session_ids: set[str],
    phase_overlay: dict[str, dict[str, str | None]] | None = None,
    scanned_processes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect live managed provider processes via same-uid scan.

    Uses psutil to enumerate provider processes the current user owns, then
    tags them as managed by either env (`LONGHOUSE_MANAGED_SESSION_ID`) or, as
    a fallback for launch contexts where `psutil.Process.environ()` is empty
    (e.g. launchctl LaunchAgents on macOS), by argv (`--session-id <uuid>`).

    Process liveness is the liveness signal; nothing to "orphan" here. Bridge
    files are intentionally not consulted.

    `existing_session_ids` lets callers deduplicate against rows already built
    from bridge-file scans, so the same Codex session isn't reported twice.
    """
    if scanned_processes is not None:
        scanned_rows = scanned_processes
    else:
        from zerg.services import local_health as _local_health_pkg

        scanned_rows = _local_health_pkg._scan_provider_processes()
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session_id in existing_session_ids:
        seen.add(session_id)

    for proc_row in scanned_rows:
        session_id = _normalize_optional_string(proc_row.get("session_id"))
        if not session_id or session_id in seen:
            continue

        started_at = _normalize_optional_string(proc_row.get("started_at"))
        if not started_at:
            continue

        provider = _normalize_optional_string(proc_row.get("provider")) or "unknown"
        phase_state = phase_overlay.get(session_id or "") if phase_overlay else None

        sessions.append(
            _process_managed_session_row(
                proc_row=proc_row,
                session_id=session_id,
                provider=provider,
                started_at=started_at,
                phase_state=phase_state,
            )
        )
        seen.add(session_id)

    return sessions


def _collect_unmanaged_processes(
    *,
    scanned_processes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect live bare Claude/Codex CLI processes not owned by Longhouse."""

    if scanned_processes is not None:
        scanned_rows = scanned_processes
    else:
        from zerg.services import local_health as _local_health_pkg

        scanned_rows = _local_health_pkg._scan_provider_processes()
    processes: list[dict[str, Any]] = []

    for proc_row in scanned_rows:
        if _normalize_optional_string(proc_row.get("session_id")):
            continue

        started_at = _normalize_optional_string(proc_row.get("started_at"))
        if not started_at:
            continue

        provider_cli = proc_row.get("provider_cli") or _provider_cli_reference(None, source=PROVIDER_CLI_SOURCE_PROCESS)
        processes.append(
            {
                "provider": _normalize_optional_string(proc_row.get("provider")),
                "control_path": CONTROL_PATH_UNMANAGED,
                "liveness_model": LIVENESS_MODEL_PROCESS_SCAN,
                "provider_cli": provider_cli,
                "pid": proc_row.get("pid"),
                "workspace_label": _normalize_optional_string(proc_row.get("workspace_label")),
                "cwd": _normalize_optional_string(proc_row.get("cwd")),
                "branch": None,
                "started_at": started_at,
            }
        )

    processes.sort(
        key=lambda row: _parse_rfc3339(row.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return processes


__all__ = [
    "_PROCESS_SNAPSHOT",
    "_compute_process_snapshot",
    "_process_snapshot_scope",
    "_collect_process_snapshot",
    "_collect_process_rows",
    "_process_row_by_pid",
    "_process_row_is_zombie",
    "_is_claude_cmdline",
    "_is_codex_cmdline",
    "_is_opencode_cmdline",
    "_is_antigravity_cmdline",
    "_provider_for_cmdline",
    "_session_id_from_argv",
    "_scan_provider_processes",
    "_process_managed_session_row",
    "_collect_managed_sessions_by_process",
    "_collect_unmanaged_processes",
]
