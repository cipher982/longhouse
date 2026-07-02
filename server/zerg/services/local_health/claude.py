from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from ._shared import _canonical_stable_home
from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339
from .constants import _SHELL_SPAWN_ENOENT_PATTERNS
from .constants import PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW
from .constants import PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT
from .constants import PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT
from .constants import PROVIDER_HOOK_DIAGNOSTIC_WINDOW


def _provider_config_dir_for_hook_diagnostics(base_dir: Path) -> Path | None:
    env_dir = _normalize_optional_string(os.environ.get("CLAUDE_CONFIG_DIR"))
    if env_dir is not None:
        return Path(env_dir).expanduser()
    resolved = base_dir.expanduser().resolve(strict=False)
    if resolved == _canonical_stable_home():
        return Path.home() / ".claude"
    if base_dir.name == ".longhouse":
        return base_dir.parent / ".claude"
    return None


def _hook_error_looks_like_deleted_cwd_spawn(error: str | None) -> bool:
    normalized = str(error or "")
    return any(pattern in normalized for pattern in _SHELL_SPAWN_ENOENT_PATTERNS)


def _hook_error_commands_from_payload(payload: Mapping[str, Any]) -> list[str]:
    attachment = payload.get("attachment")
    if isinstance(attachment, Mapping):
        command = _normalize_optional_string(attachment.get("command"))
        return [command] if command else []
    hook_infos = payload.get("hookInfos")
    if isinstance(hook_infos, list):
        commands: list[str] = []
        for item in hook_infos:
            if not isinstance(item, Mapping):
                continue
            command = _normalize_optional_string(item.get("command"))
            if command:
                commands.append(command)
        return commands
    return []


def _hook_errors_from_payload(payload: Mapping[str, Any]) -> list[str]:
    attachment = payload.get("attachment")
    if isinstance(attachment, Mapping):
        stderr = _normalize_optional_string(attachment.get("stderr"))
        return [stderr] if stderr else []
    hook_errors = payload.get("hookErrors")
    if isinstance(hook_errors, list):
        return [str(item) for item in hook_errors if str(item or "").strip()]
    return []


def _hook_diagnostic_event_from_payload(payload: Mapping[str, Any], *, source_path: Path) -> dict[str, Any] | None:
    errors = _hook_errors_from_payload(payload)
    if not any(_hook_error_looks_like_deleted_cwd_spawn(error) for error in errors):
        return None
    cwd = _normalize_optional_string(payload.get("cwd"))
    cwd_exists = Path(cwd).expanduser().exists() if cwd else None
    if cwd_exists is not False:
        return None
    return {
        "session_id": _normalize_optional_string(payload.get("sessionId")),
        "provider": "claude",
        "cwd": cwd,
        "cwd_exists": cwd_exists,
        "timestamp": _normalize_optional_string(payload.get("timestamp")),
        "source_path": str(source_path),
        "commands": _hook_error_commands_from_payload(payload),
        "errors": errors,
    }


def _recent_claude_transcript_paths(provider_config_dir: Path, *, now: datetime) -> list[Path]:
    projects_dir = provider_config_dir / "projects"
    if not projects_dir.exists():
        return []
    cutoff = now.timestamp() - PROVIDER_HOOK_DIAGNOSTIC_WINDOW.total_seconds()
    candidates: list[tuple[float, Path]] = []
    try:
        for path in projects_dir.rglob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                candidates.append((mtime, path))
    except OSError:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[:PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT]]


def _collect_provider_hook_diagnostics(base_dir: Path, *, now: datetime, fast: bool) -> dict[str, Any]:
    if fast:
        return {
            "schema_version": 1,
            "state": "skipped",
            "skipped_reason": "fast_local_health",
            "recent_error_count": 0,
            "deleted_cwd_error_count": 0,
            "events": [],
        }

    provider_config_dir = _provider_config_dir_for_hook_diagnostics(base_dir)
    if provider_config_dir is None:
        return {
            "schema_version": 1,
            "state": "skipped",
            "skipped_reason": "non_standard_longhouse_home",
            "recent_error_count": 0,
            "deleted_cwd_error_count": 0,
            "events": [],
        }

    paths = _recent_claude_transcript_paths(provider_config_dir, now=now)
    events: list[dict[str, Any]] = []
    recent_error_count = 0
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    if not any(pattern in raw_line for pattern in _SHELL_SPAWN_ENOENT_PATTERNS):
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, Mapping):
                        continue
                    recent_error_count += 1
                    event = _hook_diagnostic_event_from_payload(payload, source_path=path)
                    if event is not None:
                        events.append(event)
        except OSError:
            continue

    events.sort(
        key=lambda item: _parse_rfc3339(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    events = events[:PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT]
    actionable_cutoff = now - PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW
    actionable_events = []
    for event in events:
        parsed = _parse_rfc3339(event.get("timestamp"))
        if parsed is not None and parsed >= actionable_cutoff:
            actionable_events.append(event)
    state = "session_cwd_missing" if actionable_events else "stale_session_cwd_missing" if events else "healthy"
    return {
        "schema_version": 1,
        "state": state,
        "provider_config_dir": str(provider_config_dir),
        "scan_window_seconds": int(PROVIDER_HOOK_DIAGNOSTIC_WINDOW.total_seconds()),
        "actionable_window_seconds": int(PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW.total_seconds()),
        "scanned_files": len(paths),
        "recent_error_count": recent_error_count,
        "deleted_cwd_error_count": len(events),
        "actionable_deleted_cwd_error_count": len(actionable_events),
        "events": events,
        "latest": actionable_events[0] if actionable_events else events[0] if events else None,
        "latest_actionable": actionable_events[0] if actionable_events else None,
    }


__all__ = [
    "_provider_config_dir_for_hook_diagnostics",
    "_hook_error_looks_like_deleted_cwd_spawn",
    "_hook_error_commands_from_payload",
    "_hook_errors_from_payload",
    "_hook_diagnostic_event_from_payload",
    "_recent_claude_transcript_paths",
    "_collect_provider_hook_diagnostics",
]
