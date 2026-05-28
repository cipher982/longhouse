"""Local Longhouse engine health snapshot helpers.

This module is the canonical local status classifier for the CLI and future
desktop surfaces. It combines raw local probes with a small derived state model
without hiding the underlying signals.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.managed_phase_contract import display_label_for_phase
from zerg.managed_phase_contract import is_known_raw_phase
from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_BRIDGE_STATE
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_MISSING
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PROCESS
from zerg.provider_live_proof import collect_provider_live_proof
from zerg.provider_live_route_e2e import collect_provider_live_route_e2e
from zerg.provider_live_route_e2e import expected_route_providers_from_live_proof
from zerg.provider_release_status import collect_provider_release_status
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_log_dir
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path
from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.machine_repair import recommended_machine_repair_command
from zerg.services.machine_state import machine_state_source_hash
from zerg.services.machine_state import read_machine_state
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import machine_control_launch_capability_by_provider
from zerg.services.managed_provider_contracts import machine_control_operations_by_provider
from zerg.services.managed_session_contracts import REASON_BRIDGE_STATE_PATH_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_REPLACED
from zerg.services.managed_session_contracts import collect_managed_session_contract_diagnostics
from zerg.services.provider_support_state import collect_provider_support_state
from zerg.services.shipper.service import get_service_info
from zerg.services.transport_health import TransportHealthAssessment
from zerg.services.transport_health import TransportHealthSample
from zerg.services.transport_health import assess_transport_health
from zerg.services.transport_health import transport_health_sample_from_engine_status_payload

SCHEMA_VERSION = 1
ENGINE_FRESH_SECONDS = 30
ENGINE_STALE_SECONDS = 120
OUTBOX_DEGRADED_AGE_SECONDS = 60
OUTBOX_BROKEN_AGE_SECONDS = 120
DEGRADED_BACKLOG_COUNT = 10
BROKEN_BACKLOG_COUNT = 25
DISK_DEGRADED_BYTES = 5 * 1024 * 1024 * 1024
DISK_BROKEN_BYTES = 1 * 1024 * 1024 * 1024
ACTIVITY_RECENT_MINUTES = 15
ACTIVITY_RECENCY_BANDS = [
    ("0-1m", timedelta(minutes=1)),
    ("1-5m", timedelta(minutes=5)),
    ("5-15m", timedelta(minutes=15)),
    ("15-60m", timedelta(hours=1)),
    ("1-6h", timedelta(hours=6)),
]
RECENT_TOUCH_LIMIT = 4
PROVIDER_HOOK_DIAGNOSTIC_WINDOW = timedelta(hours=24)
PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW = timedelta(hours=1)
PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT = 24
PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT = 8
_PROCESS_SNAPSHOT: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None

CONTROL_PATH_MANAGED = "managed"
CONTROL_PATH_UNMANAGED = "unmanaged"
LIVENESS_MODEL_CODEX_BRIDGE = "codex_bridge"
LIVENESS_MODEL_PROCESS_SCAN = "process_scan"
LIVENESS_MODEL_ENGINE_STATUS = "engine_status"
LAUNCH_CAPABILITY_BY_PROVIDER = machine_control_launch_capability_by_provider()
CODEX_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["codex"]
OPENCODE_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["opencode"]
ANTIGRAVITY_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["antigravity"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_rfc3339(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _max_rfc3339(*values: str | None) -> str | None:
    candidates = [_parse_rfc3339(value) for value in values]
    present = [value for value in candidates if value is not None]
    if not present:
        return None
    return _to_rfc3339(max(present))


def _coerce_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return resolve_longhouse_home()


def _canonical_stable_home() -> Path:
    return (Path.home() / ".longhouse").expanduser().resolve(strict=False)


def _state_root_tracks_machine_runner(base_dir: Path) -> bool:
    return base_dir.expanduser().resolve(strict=False) == _canonical_stable_home()


def _read_trimmed_file(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def _normalize_optional_string(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _normalize_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _looks_like_subagent_control_error(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return "subagent" in lowered and "managed primary" in lowered


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


def _normalize_binding_path(path: str | None) -> str | None:
    normalized = _normalize_optional_string(path)
    if normalized is None:
        return None
    return str(Path(normalized).expanduser().resolve(strict=False))


def _resolve_provider_cli_candidate(candidate: str | None) -> str | None:
    normalized = _normalize_optional_string(candidate)
    if normalized is None:
        return None
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(normalized).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(normalized)


def _provider_cli_reference(path: str | None, *, source: str) -> dict[str, str | None]:
    return {"path": _normalize_optional_string(path), "source": source}


def _collect_provider_cli(*, binary: str, env_var: str | None) -> dict[str, Any]:
    env_candidate = _normalize_optional_string(os.environ.get(env_var)) if env_var else None
    if env_candidate:
        path = _resolve_provider_cli_candidate(env_candidate)
        source = env_var
        resolution_error = None if path else f"{env_var} did not resolve to an executable"
    else:
        path = shutil.which(binary)
        source = PROVIDER_CLI_SOURCE_PATH if path else PROVIDER_CLI_SOURCE_MISSING
        resolution_error = None if path else f"`{binary}` not found on PATH"
    return {
        "path": path,
        "source": source,
        "resolution_error": resolution_error,
        "env_override": env_candidate,
    }


def _collect_provider_clis() -> dict[str, Any]:
    return {
        provider: _collect_provider_cli(
            binary=binary,
            env_var=PROVIDER_CLI_ENV_BY_PROVIDER.get(provider),
        )
        for provider, binary in PROVIDER_CLI_BINARY_BY_PROVIDER.items()
    }


_SHELL_SPAWN_ENOENT_PATTERNS = (
    "posix_spawn '/bin/sh'",
    "spawn /bin/sh ENOENT",
    "spawnSync /bin/sh ENOENT",
)


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


def _apply_managed_session_contract_diagnostics(
    *,
    diagnostics: Mapping[str, Any],
    reasons: list[str],
    suggested_actions: list[str],
    managed_sessions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw_issues = diagnostics.get("issues")
    if not isinstance(raw_issues, list):
        return None
    issues = [issue for issue in raw_issues if isinstance(issue, Mapping)]
    if not issues:
        return None

    issue_reasons_by_session: dict[str, list[str]] = {}
    for issue in issues:
        reason = _normalize_optional_string(issue.get("reason"))
        if reason and reason not in reasons:
            reasons.append(reason)
        action = _normalize_optional_string(issue.get("action"))
        if action:
            _with_action(suggested_actions, action)
        session_id = _normalize_optional_string(issue.get("session_id"))
        if session_id and reason:
            issue_reasons_by_session.setdefault(session_id, []).append(reason)

    for session in managed_sessions:
        session_id = _normalize_optional_string(session.get("session_id"))
        if not session_id or session_id not in issue_reasons_by_session:
            continue
        reason_codes = list(session.get("reason_codes") or [])
        for reason in issue_reasons_by_session[session_id]:
            if reason not in reason_codes:
                reason_codes.append(reason)
        session["reason_codes"] = reason_codes
        if session.get("state") == "attached":
            session["state"] = "degraded"

    return dict(issues[0])


def _managed_contract_headline(diagnostics: Mapping[str, Any], latest_issue: Mapping[str, Any]) -> str:
    raw_issues = diagnostics.get("issues")
    issues = [issue for issue in raw_issues if isinstance(issue, Mapping)] if isinstance(raw_issues, list) else []
    session_ids = set()
    for issue in issues:
        session_id = _normalize_optional_string(issue.get("session_id"))
        if session_id is not None:
            session_ids.add(session_id)
    if len(session_ids) > 1:
        return f"{len(session_ids)} managed provider sessions need attention"
    if len(issues) > 1:
        return f"{len(issues)} managed provider session issues need attention"
    return _normalize_optional_string(latest_issue.get("headline")) or "Managed provider session needs attention"


_THREAD_SUBSCRIPTION_TRANSIENT_STATES = frozenset(
    {
        "waiting_for_thread",
        "waiting_for_turn",
        "waiting_for_rollout",
        "ready_to_subscribe",
        "subscribing",
        "retrying",
    }
)


def _session_context_payloads(path: Path, *, max_lines: int) -> Iterator[Any]:
    try:
        with path.open() as handle:
            for index, raw_line in enumerate(handle):
                if index >= max_lines:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield payload
    except OSError:
        return


def _payload_metadata_context(meta: dict[str, Any]) -> tuple[str | None, str | None]:
    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
    branch = None
    if isinstance(meta.get("git"), dict):
        branch = meta["git"].get("branch") if isinstance(meta["git"].get("branch"), str) else None
    return cwd, branch


def _payload_message_context(
    message: dict[str, Any],
    *,
    cwd: str | None,
    branch: str | None,
) -> tuple[str | None, str | None]:
    cwd = cwd or (message.get("cwd") if isinstance(message.get("cwd"), str) else None)
    branch = branch or (message.get("gitBranch") if isinstance(message.get("gitBranch"), str) else None)
    return cwd, branch


def _session_context_from_payload(payload: Any) -> tuple[str | None, str | None]:
    cwd = None
    branch = None
    if not isinstance(payload, dict):
        return cwd, branch

    if isinstance(payload.get("payload"), dict):
        cwd, branch = _payload_metadata_context(payload["payload"])
    if isinstance(payload.get("message"), dict):
        cwd, branch = _payload_message_context(payload["message"], cwd=cwd, branch=branch)
    return cwd, branch


def _read_session_context(path: Path, *, max_lines: int = 6) -> tuple[str | None, str | None]:
    """Extract cwd and branch from the first few JSONL records when available."""
    for payload in _session_context_payloads(path, max_lines=max_lines):
        cwd, branch = _session_context_from_payload(payload)
        if cwd or branch:
            return cwd, branch

    return None, None


def _derive_workspace_label(source_path: Path, *, cwd: str | None) -> str | None:
    if cwd:
        name = Path(cwd).name.strip()
        if name:
            return name

    parts = source_path.parts
    if "projects" in parts:
        try:
            encoded = parts[parts.index("projects") + 1]
        except (ValueError, IndexError):
            return None
        encoded = encoded.lstrip("-")
        if "-git-" in encoded:
            return encoded.split("-git-", 1)[1] or None
        if encoded:
            return encoded.rsplit("-", 1)[-1] or None
    return None


def _recent_touch_entry(source_path: str, provider: str, last_updated: str) -> dict[str, Any]:
    path = Path(source_path)
    cwd, branch = _read_session_context(path)
    workspace_label = _derive_workspace_label(path, cwd=cwd)
    return {
        "provider": provider,
        "last_updated": last_updated,
        "workspace_label": workspace_label,
        "branch": branch,
        "is_subagent": "subagents" in path.parts,
    }


def _collect_local_config(base_dir: Path) -> dict[str, Any]:
    state_path, machine_state, state_error = read_machine_state(base_dir)
    return {
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "state_error": state_error,
        "config_generation": machine_state.config_generation if machine_state else None,
        "stored_url": machine_state.runtime_url if machine_state else None,
        "machine_name": machine_state.machine_name if machine_state else None,
        "state_hash": machine_state_source_hash(machine_state),
    }


def _candidate_runner_env_paths() -> list[Path]:
    paths = [Path.home() / ".config" / "longhouse" / "runner.env"]
    if os.name != "nt":
        paths.append(Path("/etc/longhouse/runner.env"))
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip("\"'")
        if normalized_key:
            payload[normalized_key] = normalized_value
    return payload


def _runner_config_payload(
    path: Path,
    *,
    exists: bool,
    error: str | None = None,
    runner_name: str | None = None,
    runner_id: str | None = None,
    runner_urls: list[str] | None = None,
    install_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": exists,
        "error": error,
        "runner_name": runner_name,
        "runner_id": runner_id,
        "runner_urls": runner_urls or [],
        "install_mode": install_mode,
    }


def _missing_runner_config() -> dict[str, Any]:
    return _runner_config_payload(_candidate_runner_env_paths()[0], exists=False)


def _runner_urls_from_env(env: dict[str, str]) -> list[str]:
    raw_urls = str(env.get("LONGHOUSE_URLS") or "").strip()
    if raw_urls:
        return [item.strip() for item in raw_urls.split(",") if item.strip()]

    raw_url = str(env.get("LONGHOUSE_URL") or "").strip()
    return [raw_url] if raw_url else []


def _runner_config_from_env(path: Path, env: dict[str, str]) -> dict[str, Any]:
    return _runner_config_payload(
        path,
        exists=True,
        runner_name=str(env.get("RUNNER_NAME") or "").strip() or None,
        runner_id=str(env.get("RUNNER_ID") or "").strip() or None,
        runner_urls=_runner_urls_from_env(env),
        install_mode=str(env.get("RUNNER_INSTALL_MODE") or "").strip() or None,
    )


def _collect_runner_config(*, include_global_runner: bool = True) -> dict[str, Any]:
    if not include_global_runner:
        return _missing_runner_config()

    for path in _candidate_runner_env_paths():
        if not path.exists():
            continue
        try:
            env = _parse_env_file(path)
        except OSError as exc:
            return _runner_config_payload(path, exists=True, error=str(exc))

        return _runner_config_from_env(path, env)

    return _missing_runner_config()


def _extract_machine_name_from_args(arguments: list[str]) -> str | None:
    for index, arg in enumerate(arguments[:-1]):
        if arg == "--machine-name":
            candidate = str(arguments[index + 1] or "").strip()
            return candidate or None
    return None


def _service_file_path(service_file: str | None) -> Path | None:
    raw = str(service_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.exists():
        return None
    return path


def _read_service_plist(path: Path) -> dict[str, Any]:
    payload = plistlib.loads(path.read_bytes())
    return payload if isinstance(payload, dict) else {}


def _systemd_exec_start_arguments(path: Path) -> list[str] | None:
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue
        return shlex.split(line.split("=", 1)[1].strip())
    return None


def _systemd_environment(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("Environment="):
            continue
        value = line.split("=", 1)[1].strip()
        for token in shlex.split(value):
            if "=" not in token:
                continue
            key, env_value = token.split("=", 1)
            env[key] = env_value
    return env


def _service_metadata_from_env(env: dict[str, Any] | None) -> dict[str, str | None]:
    env = env or {}
    return {
        "config_generation": str(env.get("LONGHOUSE_MACHINE_GENERATION") or "").strip() or None,
        "state_hash": str(env.get("LONGHOUSE_MACHINE_STATE_HASH") or "").strip() or None,
    }


def _empty_service_metadata() -> dict[str, str | None]:
    return {
        "config_generation": None,
        "state_hash": None,
    }


def _extract_service_machine_name(service_file: str | None) -> str | None:
    path = _service_file_path(service_file)
    if path is None:
        return None

    try:
        if path.suffix == ".plist":
            payload = _read_service_plist(path)
            arguments = [str(item) for item in payload.get("ProgramArguments") or []]
            return _extract_machine_name_from_args(arguments)

        if path.suffix == ".service":
            arguments = _systemd_exec_start_arguments(path)
            if arguments is not None:
                return _extract_machine_name_from_args(arguments)
    except Exception:
        return None

    return None


def _extract_service_metadata(service_file: str | None) -> dict[str, str | None]:
    metadata = _empty_service_metadata()
    path = _service_file_path(service_file)
    if path is None:
        return metadata

    try:
        if path.suffix == ".plist":
            payload = _read_service_plist(path)
            env = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
            if isinstance(env, dict):
                metadata = _service_metadata_from_env(env)
            return metadata

        if path.suffix == ".service":
            metadata = _service_metadata_from_env(_systemd_environment(path))
    except Exception:
        return metadata

    return metadata


def _can_reconcile_launch_from_state(
    *,
    state_exists: bool,
    state_error: str | None,
    stored_url: str | None,
    machine_name: str | None,
) -> bool:
    return state_exists and not state_error and bool(stored_url) and bool(machine_name)


def _repair_command(*, can_reconcile_from_state: bool) -> str:
    return recommended_machine_repair_command(can_reconcile_from_state=can_reconcile_from_state)


@dataclass
class _LaunchReadinessContext:
    runner: dict[str, Any]
    shipper_db_path: Path
    stored_url: str | None
    machine_name: str | None
    config_generation: str | None
    state_hash: str | None
    state_exists: bool
    state_error: str | None
    runner_expected: bool
    runner_name: str | None
    runner_urls: list[str]
    service_machine_name: str | None
    service_config_generation: str | None
    service_state_hash: str | None
    service_status: str
    service_file_exists: bool
    shipper_state_exists: bool
    can_reconcile_from_state: bool


@dataclass
class _LaunchOverrideContext:
    effective_url: str | None
    effective_machine_name: str | None
    runner_expected: bool
    runner_name: str | None
    runner_urls: list[str]
    reasons: list[str]
    actions: list[str]
    warnings: list[str]
    had_override: bool


def _collect_launch_readiness_context(base_dir: Path, *, service: dict[str, Any]) -> _LaunchReadinessContext:
    config = _collect_local_config(base_dir)
    runner = _collect_runner_config(include_global_runner=_state_root_tracks_machine_runner(base_dir))
    shipper_db_path = get_agent_db_path(base_dir)
    service_file_raw = str(service.get("service_file") or "").strip()
    service_file = Path(service_file_raw) if service_file_raw else None
    service_machine_name = _extract_service_machine_name(service.get("service_file"))
    service_metadata = _extract_service_metadata(service.get("service_file"))

    stored_url = str(config.get("stored_url") or "").strip() or None
    machine_name = str(config.get("machine_name") or "").strip() or None
    config_generation = str(config.get("config_generation") or "").strip() or None
    state_hash = str(config.get("state_hash") or "").strip() or None
    state_exists = bool(config.get("state_exists"))
    state_error = str(config.get("state_error") or "").strip() or None
    runner_expected = bool(runner.get("exists"))
    runner_name = str(runner.get("runner_name") or "").strip() or None
    runner_urls = [str(item).strip() for item in list(runner.get("runner_urls") or []) if str(item).strip()]
    service_config_generation = str(service_metadata.get("config_generation") or "").strip() or None
    service_state_hash = str(service_metadata.get("state_hash") or "").strip() or None
    service_status = str(service.get("status") or "not-installed")
    service_file_exists = bool(service_file and service_file.exists())
    shipper_state_exists = shipper_db_path.exists()
    can_reconcile_from_state = _can_reconcile_launch_from_state(
        state_exists=state_exists,
        state_error=state_error,
        stored_url=stored_url,
        machine_name=machine_name,
    )

    return _LaunchReadinessContext(
        runner=runner,
        shipper_db_path=shipper_db_path,
        stored_url=stored_url,
        machine_name=machine_name,
        config_generation=config_generation,
        state_hash=state_hash,
        state_exists=state_exists,
        state_error=state_error,
        runner_expected=runner_expected,
        runner_name=runner_name,
        runner_urls=runner_urls,
        service_machine_name=service_machine_name,
        service_config_generation=service_config_generation,
        service_state_hash=service_state_hash,
        service_status=service_status,
        service_file_exists=service_file_exists,
        shipper_state_exists=shipper_state_exists,
        can_reconcile_from_state=can_reconcile_from_state,
    )


def _add_launch_machine_state_reasons(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if ctx.state_error:
        reasons.append("machine_state_invalid")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))
    elif not ctx.state_exists and (ctx.service_machine_name or ctx.runner.get("exists")):
        reasons.append("machine_state_missing")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))

    if ctx.state_exists and not ctx.stored_url:
        reasons.append("machine_state_missing_runtime_url")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))

    if ctx.state_exists and not ctx.machine_name:
        reasons.append("machine_state_missing_machine_name")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))


def _add_launch_runner_config_reasons(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.stored_url
        and ctx.runner_urls
        and ctx.stored_url not in ctx.runner_urls
    ):
        reasons.append("config_url_runner_url_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.machine_name
        and ctx.runner_name
        and ctx.machine_name != ctx.runner_name
    ):
        reasons.append("machine_name_runner_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _add_launch_service_config_reasons(
    ctx: _LaunchReadinessContext,
    reasons: list[str],
    warnings: list[str],
    actions: list[str],
) -> None:
    machine_name = ctx.machine_name
    service_machine_name = ctx.service_machine_name
    service_machine_name_mismatch = machine_name and service_machine_name and machine_name != service_machine_name
    if ctx.can_reconcile_from_state and service_machine_name_mismatch:
        reasons.append("service_machine_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    service_state_hash_mismatch = ctx.state_hash and ctx.service_state_hash and ctx.state_hash != ctx.service_state_hash
    if ctx.can_reconcile_from_state and service_state_hash_mismatch:
        reasons.append("service_state_hash_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    if (
        ctx.can_reconcile_from_state
        and ctx.config_generation
        and ctx.service_config_generation
        and ctx.config_generation != ctx.service_config_generation
    ):
        if ctx.state_hash and ctx.service_state_hash and ctx.state_hash == ctx.service_state_hash:
            warnings.append("service_generation_mismatch")
        else:
            reasons.append("service_generation_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _add_launch_shipper_state_reason(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if ctx.service_status != "not-installed" and ctx.service_file_exists and not ctx.shipper_state_exists:
        reasons.append("shipper_state_missing")
        _with_action(actions, f"Inspect or restore shipper state: {ctx.shipper_db_path}")


def _add_launch_service_runner_reason(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.runner_name
        and ctx.service_machine_name
        and ctx.runner_name != ctx.service_machine_name
    ):
        reasons.append("service_runner_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _launch_readiness_configured(ctx: _LaunchReadinessContext) -> bool:
    return any(
        (
            ctx.state_exists,
            ctx.stored_url,
            ctx.machine_name,
            ctx.service_machine_name,
            ctx.runner.get("exists"),
        )
    )


def _launch_readiness_state(*, reasons: list[str], configured: bool) -> tuple[str, str]:
    if reasons:
        return "broken", "Managed launch config is inconsistent"
    if configured:
        return "ready", "Managed launch configuration looks coherent"
    return "unconfigured", "Managed launch has not been configured on this machine"


def _launch_readiness_payload(
    ctx: _LaunchReadinessContext,
    *,
    state: str,
    headline: str,
    reasons: list[str],
    warnings: list[str],
    actions: list[str],
) -> dict[str, Any]:
    return {
        "state": state,
        "headline": headline,
        "reasons": reasons,
        "warnings": warnings,
        "suggested_actions": actions,
        "control_plane_url": ctx.stored_url,
        "stored_url": ctx.stored_url,
        "machine_name": ctx.machine_name,
        "state_exists": ctx.state_exists,
        "state_error": ctx.state_error,
        "config_generation": ctx.config_generation,
        "state_hash": ctx.state_hash,
        "runner_expected": ctx.runner_expected,
        "service_machine_name": ctx.service_machine_name,
        "service_config_generation": ctx.service_config_generation,
        "service_state_hash": ctx.service_state_hash,
        "service_file_exists": ctx.service_file_exists,
        "shipper_db_path": str(ctx.shipper_db_path),
        "shipper_state_exists": ctx.shipper_state_exists,
        "runner": ctx.runner,
    }


def _collect_launch_readiness(base_dir: Path, *, service: dict[str, Any]) -> dict[str, Any]:
    ctx = _collect_launch_readiness_context(base_dir, service=service)
    reasons: list[str] = []
    warnings: list[str] = []
    actions: list[str] = []

    # Keep this ordering stable; the top-level health classifier preserves it.
    _add_launch_machine_state_reasons(ctx, reasons, actions)
    _add_launch_runner_config_reasons(ctx, reasons, actions)
    _add_launch_service_config_reasons(ctx, reasons, warnings, actions)
    _add_launch_shipper_state_reason(ctx, reasons, actions)
    _add_launch_service_runner_reason(ctx, reasons, actions)

    state, headline = _launch_readiness_state(
        reasons=reasons,
        configured=_launch_readiness_configured(ctx),
    )
    return _launch_readiness_payload(
        ctx,
        state=state,
        headline=headline,
        reasons=reasons,
        warnings=warnings,
        actions=actions,
    )


def _drop_launch_reason(reasons: list[str], reason_code: str) -> None:
    while reason_code in reasons:
        reasons.remove(reason_code)


def _launch_override_repair_command(
    readiness: dict[str, Any],
    *,
    stored_url: str | None,
    machine_name: str | None,
) -> str:
    return _repair_command(
        can_reconcile_from_state=_can_reconcile_launch_from_state(
            state_exists=bool(readiness.get("state_exists")),
            state_error=str(readiness.get("state_error") or "").strip() or None,
            stored_url=stored_url,
            machine_name=machine_name,
        )
    )


def _launch_override_context(
    readiness: dict[str, Any],
    *,
    runtime_url_override: str | None,
    machine_name_override: str | None,
) -> _LaunchOverrideContext:
    runner = dict(readiness.get("runner") or {})
    override_machine_name = str(machine_name_override or "").strip()
    stored_machine_name = str(readiness.get("machine_name") or "").strip()
    effective_machine_name = override_machine_name or stored_machine_name or None
    return _LaunchOverrideContext(
        effective_url=str(runtime_url_override or "").strip() or str(readiness.get("stored_url") or "").strip() or None,
        effective_machine_name=effective_machine_name,
        runner_expected=bool(readiness.get("runner_expected")),
        runner_name=str(runner.get("runner_name") or "").strip() or None,
        runner_urls=[str(item).strip() for item in list(runner.get("runner_urls") or []) if str(item).strip()],
        reasons=[str(item) for item in list(readiness.get("reasons") or [])],
        actions=[str(item) for item in list(readiness.get("suggested_actions") or [])],
        warnings=[str(item) for item in list(readiness.get("warnings") or [])],
        had_override=runtime_url_override is not None or machine_name_override is not None,
    )


def _apply_runner_url_override_reason(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> None:
    _drop_launch_reason(ctx.reasons, "config_url_runner_url_mismatch")
    runner_url_mismatch = ctx.effective_url and ctx.runner_urls and ctx.effective_url not in ctx.runner_urls
    if ctx.runner_expected and runner_url_mismatch:
        ctx.reasons.append("config_url_runner_url_mismatch")
        _with_action(
            ctx.actions,
            _launch_override_repair_command(
                readiness,
                stored_url=ctx.effective_url,
                machine_name=ctx.effective_machine_name,
            ),
        )


def _apply_runner_name_override_reason(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> None:
    _drop_launch_reason(ctx.reasons, "machine_name_runner_name_mismatch")
    effective_machine_name = ctx.effective_machine_name
    runner_name_mismatch = effective_machine_name and ctx.runner_name and effective_machine_name != ctx.runner_name
    if ctx.runner_expected and runner_name_mismatch:
        ctx.reasons.append("machine_name_runner_name_mismatch")
        _with_action(
            ctx.actions,
            _launch_override_repair_command(
                readiness,
                stored_url=ctx.effective_url,
                machine_name=ctx.effective_machine_name,
            ),
        )


def _launch_override_state(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> tuple[str, str]:
    state = str(readiness.get("state") or "unconfigured")
    headline = str(readiness.get("headline") or "Managed launch configuration looks coherent")
    if ctx.reasons:
        state = "broken"
        headline = "Managed launch config is inconsistent"
    elif ctx.had_override:
        state = "ready"
        headline = "Managed launch configuration looks coherent"
    return state, headline


def _apply_launch_readiness_overrides(
    readiness: dict[str, Any],
    *,
    runtime_url_override: str | None,
    machine_name_override: str | None,
) -> dict[str, Any]:
    ctx = _launch_override_context(
        readiness,
        runtime_url_override=runtime_url_override,
        machine_name_override=machine_name_override,
    )
    _apply_runner_url_override_reason(readiness, ctx)
    _apply_runner_name_override_reason(readiness, ctx)
    state, headline = _launch_override_state(readiness, ctx)

    readiness.update(
        {
            "state": state,
            "headline": headline,
            "reasons": ctx.reasons,
            "warnings": ctx.warnings,
            "suggested_actions": ctx.actions,
            "control_plane_url": ctx.effective_url,
            "machine_name": ctx.effective_machine_name,
        }
    )
    return readiness


def collect_launch_readiness(
    base_dir: str | Path | None = None,
    *,
    runtime_url_override: str | None = None,
    machine_name_override: str | None = None,
) -> dict[str, Any]:
    """Collect the local managed-launch readiness contract.

    `runtime_url_override` / `machine_name_override` let callers validate a
    concrete launch target without first mutating canonical machine state.
    """

    resolved_base_dir = _coerce_path(base_dir)
    service = _collect_service(resolved_base_dir)
    readiness = _collect_launch_readiness(resolved_base_dir, service=service)
    return _apply_launch_readiness_overrides(
        readiness,
        runtime_url_override=runtime_url_override,
        machine_name_override=machine_name_override,
    )


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


def _collect_service(base_dir: Path) -> dict[str, Any]:
    return get_service_info(str(base_dir))


def _collect_update_info() -> dict[str, Any]:
    """Surface the background CLI update cache as bundle update state.

    Since `longhouse upgrade` now reconciles local runtime artifacts automatically
    (see update_manager._reconcile_runtime_after_upgrade), the CLI's installed
    version is a faithful proxy for the local runtime bundle. Consumers (menu
    bar, doctor, JSON consumers) can rely on `update_available` and
    `upgrade_command` to show a nudge.
    """
    installed_version: str | None = None
    try:
        from zerg.cli.update_manager import current_installed_version

        installed_version = current_installed_version()
    except Exception:
        installed_version = None

    try:
        from zerg.cli.update_manager import load_update_cache

        cache = load_update_cache()
    except Exception:
        cache = None

    if cache is None or (installed_version and cache.installed_version != installed_version):
        return {
            "installed_version": installed_version,
            "latest_version": None,
            "update_available": False,
            "upgrade_command": None,
            "checked_at": None,
            "supported": True,
            "reason": None,
        }

    return {
        "installed_version": installed_version or cache.installed_version,
        "latest_version": cache.latest_version,
        "update_available": bool(cache.update_available),
        "upgrade_command": cache.upgrade_command,
        "checked_at": cache.checked_at,
        "supported": True,
        "reason": cache.error,
    }


def _codex_bridge_state_dir(base_dir: Path) -> Path:
    return get_managed_local_dir("codex-bridge", base_dir=base_dir)


def _compute_process_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import psutil  # imported lazily to keep module import cheap
    except ImportError:
        return [], []

    me = os.getuid()
    process_rows: list[dict[str, Any]] = []
    provider_processes: list[dict[str, Any]] = []

    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"]):
        try:
            info = proc.info
            cmdline = [str(arg) for arg in (info.get("cmdline") or []) if str(arg)]
            command = " ".join(cmdline)
            pid = int(info.get("pid") or 0)
            ppid = int(info.get("ppid") or 0)
            if pid > 0 and command:
                process_rows.append({"pid": pid, "ppid": ppid, "command": command})

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

    previous = _PROCESS_SNAPSHOT
    _PROCESS_SNAPSHOT = _compute_process_snapshot()
    try:
        yield
    finally:
        _PROCESS_SNAPSHOT = previous


def _collect_process_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if _PROCESS_SNAPSHOT is not None:
        return _PROCESS_SNAPSHOT
    return _compute_process_snapshot()


def _collect_process_rows() -> list[dict[str, Any]]:
    process_rows, _ = _collect_process_snapshot()
    return process_rows


def _load_session_binding_rows(base_dir: Path) -> list[dict[str, str | None]]:
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []

    try:
        return [
            {
                "path": str(path or ""),
                "session_id": str(session_id or ""),
                "provider": str(provider or ""),
                "updated_at": str(updated_at or ""),
            }
            for path, session_id, provider, updated_at in conn.execute(
                "SELECT path, session_id, provider, updated_at FROM session_binding ORDER BY updated_at DESC"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _load_persisted_managed_session_phase_rows(base_dir: Path) -> dict[str, dict[str, str | None]]:
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return {}

    try:
        rows = conn.execute(
            """
            SELECT
                session_id,
                provider,
                workspace_path,
                workspace_label,
                phase_kind,
                tool_name,
                phase_source,
                phase_observed_at,
                last_activity_at
            FROM managed_session_state
            ORDER BY phase_observed_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    merged: dict[str, dict[str, str | None]] = {}
    for (
        session_id,
        provider,
        workspace_path,
        workspace_label,
        phase_kind,
        tool_name,
        phase_source,
        phase_observed_at,
        last_activity_at,
    ) in rows:
        normalized_session_id = _normalize_optional_string(session_id)
        normalized_phase = _normalize_optional_string(phase_kind)
        normalized_observed_at = _parse_rfc3339(str(phase_observed_at or ""))
        if normalized_session_id is None or normalized_phase is None or normalized_observed_at is None:
            continue
        merged[normalized_session_id] = {
            "provider": _normalize_optional_string(provider),
            "workspace_path": _normalize_optional_string(workspace_path),
            "workspace_label": _normalize_optional_string(workspace_label),
            "phase": normalized_phase,
            "tool_name": _normalize_optional_string(tool_name),
            "source": _normalize_optional_string(phase_source),
            "observed_at": _to_rfc3339(normalized_observed_at),
            "last_activity_at": _max_rfc3339(last_activity_at, phase_observed_at),
        }
    return merged


def _load_outbox_session_phase_rows(base_dir: Path) -> dict[str, dict[str, str | None]]:
    outbox_dir = get_agent_outbox_dir(base_dir)
    if not outbox_dir.exists():
        return {}

    merged: dict[str, dict[str, str | None]] = {}
    for path in sorted(outbox_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        session_id = _normalize_optional_string(payload.get("session_id"))
        phase = _normalize_optional_string(payload.get("state"))
        provider = _normalize_optional_string(payload.get("provider")) or "claude"
        tool_name = _normalize_optional_string(payload.get("tool_name"))
        observed_at = _parse_rfc3339(payload.get("occurred_at"))
        if observed_at is None:
            try:
                observed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                observed_at = None
        if session_id is None or phase is None or observed_at is None:
            continue

        next_row = {
            "provider": provider,
            "phase": phase,
            "tool_name": tool_name,
            "source": f"{provider}_hook",
            "observed_at": _to_rfc3339(observed_at),
            "last_activity_at": _to_rfc3339(observed_at),
        }
        current = merged.get(session_id)
        latest_observed_at = _max_rfc3339(next_row["observed_at"], current.get("observed_at")) if current else None
        if current is None or latest_observed_at == next_row["observed_at"]:
            merged[session_id] = next_row
    return merged


def _phase_display_label(phase: str | None, tool_name: str | None) -> str | None:
    return display_label_for_phase(_normalize_optional_string(phase), _normalize_optional_string(tool_name))


def _managed_phase_is_unknown(raw_phase: str | None) -> bool:
    normalized_phase = _normalize_optional_string(raw_phase)
    if normalized_phase is None:
        return False
    return not is_known_raw_phase(normalized_phase)


_MANAGED_FINISHED_RETENTION_SECONDS = 10 * 60


def _should_keep_managed_phase_row(row: Mapping[str, str | None], *, now: datetime) -> bool:
    # `finished` is a transient turn marker, not the steady-state idle phase.
    # Keep it briefly so local-health can show recent completion, then let it
    # age out. Other phases remain the canonical current state until a newer
    # signal replaces them, so they are intentionally not freshness-gated here.
    phase = _normalize_optional_string(row.get("phase"))
    observed_raw = _normalize_optional_string(row.get("observed_at"))
    if phase != "finished" or observed_raw is None:
        return True
    observed_at = _parse_rfc3339(observed_raw)
    if observed_at is None:
        # A malformed transient completion marker should not linger forever.
        return False
    age = (now - observed_at).total_seconds()
    return age <= _MANAGED_FINISHED_RETENTION_SECONDS


def _load_managed_session_phase_state(base_dir: Path, *, now: datetime) -> dict[str, dict[str, str | None]]:
    merged = _load_persisted_managed_session_phase_rows(base_dir)
    for session_id, row in _load_outbox_session_phase_rows(base_dir).items():
        current = merged.get(session_id)
        latest_observed_at = _max_rfc3339(row.get("observed_at"), current.get("observed_at")) if current else None
        if current is None or latest_observed_at == row.get("observed_at"):
            next_row = dict(row)
            if current is not None:
                for field_name in ("workspace_path", "workspace_label"):
                    if _normalize_optional_string(next_row.get(field_name)) is None:
                        next_row[field_name] = current.get(field_name)
            next_row["last_activity_at"] = _max_rfc3339(
                row.get("last_activity_at"),
                current.get("last_activity_at") if current else None,
            )
            merged[session_id] = next_row
    return {session_id: row for session_id, row in merged.items() if _should_keep_managed_phase_row(row, now=now)}


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


def _process_row_by_pid(process_rows: list[dict[str, Any]], pid: object) -> dict[str, Any] | None:
    pid_int = _normalize_optional_int(pid)
    if not pid_int:
        return None
    for row in process_rows:
        if int(row.get("pid") or 0) == pid_int:
            return row
    return None


def _find_codex_app_server_process(
    process_rows: list[dict[str, Any]],
    *,
    bridge_pid: int,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    by_recorded_pid = _process_row_by_pid(process_rows, state.get("app_server_pid"))
    if by_recorded_pid is not None and " app-server " in str(by_recorded_pid.get("command") or ""):
        return by_recorded_pid
    return _find_bridge_child_process(process_rows, bridge_pid=bridge_pid, needle=" app-server ")


def _binding_by_session_id(base_dir: Path) -> dict[str, dict[str, str | None]]:
    rows = _load_session_binding_rows(base_dir)
    latest: dict[str, dict[str, str | None]] = {}
    for row in rows:
        session_id = _normalize_optional_string(row.get("session_id"))
        if session_id is None or session_id in latest:
            continue
        latest[session_id] = row
    return latest


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
    reason_codes: list[str],
) -> dict[str, Any]:
    bridge_has_thread = _normalize_optional_string(state.get("thread_id")) is not None
    detached_ui_control_ready = bool(app_server is not None and bridge_status == "ready" and bridge_has_thread)
    normalized_state = "attached" if attached_process is not None or detached_ui_control_ready else "detached"
    if reason_codes:
        normalized_state = "degraded"
    workspace_label = _normalize_optional_string(phase_state.get("workspace_label")) if phase_state else None
    if workspace_label is None:
        workspace_label = Path(str(state.get("cwd") or "")).name or None
    phase_observed_at = phase_state.get("observed_at") if phase_state else None
    phase_last_activity_at = phase_state.get("last_activity_at") if phase_state else None

    return {
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
        "bridge_heartbeat_at": bridge_heartbeat_at,
        "thread_subscription_status": thread_subscription_status,
        "thread_subscription_attempts": thread_subscription_attempts,
        "thread_subscription_last_error": thread_subscription_last_error,
        "reason_codes": reason_codes,
    }


def _collect_managed_codex_sessions(
    base_dir: Path,
    *,
    phase_overlay: dict[str, dict[str, str | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state_dir = _codex_bridge_state_dir(base_dir)
    if not state_dir.exists():
        return [], []

    process_rows = _collect_process_rows()
    binding_by_session = _binding_by_session_id(base_dir)
    sessions: list[dict[str, Any]] = []
    orphan_bridges: list[dict[str, Any]] = []

    for path in sorted(state_dir.glob("*.json")):
        try:
            state = json.loads(path.read_text())
        except Exception:
            continue

        bridge_pid = int(state.get("pid") or 0)
        bridge_alive = _bridge_is_alive(path)
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
                reason_codes=reason_codes,
            )
        )

    return sessions, orphan_bridges


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


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
    scanned_rows = scanned_processes if scanned_processes is not None else _scan_provider_processes()
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

    scanned_rows = scanned_processes if scanned_processes is not None else _scan_provider_processes()
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


def _merge_managed_sessions(
    *,
    bridge_sessions: list[dict[str, Any]],
    bridge_orphans: list[dict[str, Any]],
    process_sessions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Combine bridge-file-derived rows with process-scan rows.

    Bridge rows win on session_id collisions (they carry extra Codex-specific
    fields). Process rows fill in everything else — specifically Claude, which
    has no bridge file at all.
    """
    sessions = list(bridge_sessions)
    bridge_ids = {row.get("session_id") for row in sessions if row.get("session_id")}
    for proc_row in process_sessions:
        if proc_row.get("session_id") in bridge_ids:
            continue
        sessions.append(proc_row)

    if not sessions and not bridge_orphans:
        return None, [], []

    latest_activity_at = None
    for row in sessions:
        latest_activity_at = _max_rfc3339(latest_activity_at, row.get("last_activity_at"))
    for row in bridge_orphans:
        latest_activity_at = _max_rfc3339(latest_activity_at, row.get("heartbeat_at"), row.get("started_at"))

    managed_summary = {
        "attached_count": sum(1 for item in sessions if item.get("state") == "attached"),
        "detached_count": sum(1 for item in sessions if item.get("state") == "detached"),
        "degraded_count": sum(1 for item in sessions if item.get("state") == "degraded"),
        "orphan_bridge_count": len(bridge_orphans),
        "latest_activity_at": latest_activity_at,
    }
    return managed_summary, sessions, bridge_orphans


def _empty_activity_summary(db_path: Path) -> dict[str, Any]:
    return {
        "path": str(db_path),
        "exists": db_path.exists(),
        "error": None,
        "sessions_today": 0,
        "sessions_recent": 0,
        "provider_counts_today": {},
        "provider_counts_recent": {},
        "session_recency_bands": [],
        "recent_touches": [],
        "latest_activity_at": None,
        "recent_window_minutes": ACTIVITY_RECENT_MINUTES,
    }


def _activity_cutoffs(now: datetime) -> tuple[str, str, list[str]]:
    local_now = now.astimezone()
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    today_cutoff = _to_rfc3339(start_of_day_utc)
    recent_cutoff = _to_rfc3339(now - timedelta(minutes=ACTIVITY_RECENT_MINUTES))
    band_edges = [_to_rfc3339(now - delta) for _, delta in ACTIVITY_RECENCY_BANDS]
    return today_cutoff, recent_cutoff, band_edges


def _activity_sql_fragments() -> tuple[str, str, str]:
    session_expr = "provider || ':' || COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    provider_session_expr = "COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    session_files_predicate = "path LIKE '%.jsonl'"
    return session_expr, provider_session_expr, session_files_predicate


def _populate_activity_aggregate(
    summary: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    session_expr: str,
    session_files_predicate: str,
    today_cutoff: str,
    recent_cutoff: str,
) -> None:
    aggregate_row = conn.execute(
        f"""
        SELECT
            MAX(last_updated),
            COUNT(DISTINCT CASE
                WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
            END),
            COUNT(DISTINCT CASE
                WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
            END)
        FROM file_state
        WHERE {session_files_predicate}
        """,
        (today_cutoff, recent_cutoff),
    ).fetchone()
    if aggregate_row is not None:
        summary["latest_activity_at"] = aggregate_row[0]
        summary["sessions_today"] = int(aggregate_row[1] or 0)
        summary["sessions_recent"] = int(aggregate_row[2] or 0)


def _activity_provider_counts(
    conn: sqlite3.Connection,
    *,
    provider_session_expr: str,
    session_files_predicate: str,
    cutoff: str,
) -> dict[str, int]:
    provider_counts: dict[str, int] = {}
    for provider, count in conn.execute(
        f"""
        SELECT provider, COUNT(DISTINCT {provider_session_expr})
        FROM file_state
        WHERE {session_files_predicate}
          AND julianday(last_updated) >= julianday(?)
        GROUP BY provider
        """,
        (cutoff,),
    ):
        provider_name = str(provider or "").strip()
        if not provider_name:
            continue
        provider_counts[provider_name] = int(count or 0)
    return provider_counts


def _activity_band_specs(today_cutoff: str, band_edges: list[str]) -> list[dict[str, str | None]]:
    return [
        {"label": "0-1m", "newer_than": band_edges[0], "older_than": None},
        {"label": "1-5m", "newer_than": band_edges[1], "older_than": band_edges[0]},
        {"label": "5-15m", "newer_than": band_edges[2], "older_than": band_edges[1]},
        {"label": "15-60m", "newer_than": band_edges[3], "older_than": band_edges[2]},
        {"label": "1-6h", "newer_than": band_edges[4], "older_than": band_edges[3]},
        {"label": "6h+", "newer_than": today_cutoff, "older_than": band_edges[4]},
    ]


def _activity_recency_bands(
    conn: sqlite3.Connection,
    *,
    session_expr: str,
    session_files_predicate: str,
    band_specs: list[dict[str, str | None]],
) -> list[dict[str, Any]]:
    band_clauses: list[str] = []
    band_params: list[Any] = []
    for spec in band_specs:
        clause = "COUNT(DISTINCT CASE WHEN julianday(last_updated) >= julianday(?)"
        band_params.append(spec["newer_than"])
        older_than = spec["older_than"]
        if older_than is not None:
            clause += " AND julianday(last_updated) < julianday(?)"
            band_params.append(older_than)
        clause += f" THEN {session_expr} END)"
        band_clauses.append(clause)

    band_row = conn.execute(
        f"""
        SELECT {", ".join(band_clauses)}
        FROM file_state
        WHERE {session_files_predicate}
        """,
        tuple(band_params),
    ).fetchone()
    if band_row is None:
        return []
    return [
        {
            "label": spec["label"],
            "session_count": int(band_row[index] or 0),
        }
        for index, spec in enumerate(band_specs)
    ]


def _activity_recent_touches(
    conn: sqlite3.Connection,
    *,
    provider_session_expr: str,
    session_files_predicate: str,
) -> list[dict[str, Any]]:
    recent_touches: list[dict[str, Any]] = []
    for provider, last_updated, path in conn.execute(
        f"""
        SELECT provider, last_updated, path
        FROM (
            SELECT
                provider,
                {provider_session_expr} AS session_key,
                path,
                MAX(last_updated) AS last_updated
            FROM file_state
            WHERE {session_files_predicate}
            GROUP BY provider, session_key
        )
        ORDER BY julianday(last_updated) DESC
        LIMIT ?
        """,
        (RECENT_TOUCH_LIMIT,),
    ):
        provider_name = str(provider or "").strip()
        if not provider_name or not last_updated:
            continue
        recent_touches.append(_recent_touch_entry(str(path), provider_name, str(last_updated)))
    return recent_touches


def _collect_activity_summary(base_dir: Path, *, now: datetime) -> dict[str, Any]:
    db_path = get_agent_db_path(base_dir)
    summary = _empty_activity_summary(db_path)
    if not db_path.exists():
        return summary

    today_cutoff, recent_cutoff, band_edges = _activity_cutoffs(now)
    session_expr, provider_session_expr, session_files_predicate = _activity_sql_fragments()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary

    try:
        _populate_activity_aggregate(
            summary,
            conn,
            session_expr=session_expr,
            session_files_predicate=session_files_predicate,
            today_cutoff=today_cutoff,
            recent_cutoff=recent_cutoff,
        )
        summary["provider_counts_today"] = _activity_provider_counts(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
            cutoff=today_cutoff,
        )
        summary["provider_counts_recent"] = _activity_provider_counts(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
            cutoff=recent_cutoff,
        )
        summary["session_recency_bands"] = _activity_recency_bands(
            conn,
            session_expr=session_expr,
            session_files_predicate=session_files_predicate,
            band_specs=_activity_band_specs(today_cutoff, band_edges),
        )
        summary["recent_touches"] = _activity_recent_touches(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
        )
        return summary
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        conn.close()


def _with_action(actions: list[str], text: str) -> None:
    if text not in actions:
        actions.append(text)


@dataclass
class _HealthClassificationContext:
    service_status: str
    engine_status_path: str
    engine_log_path: str
    engine_exists: bool
    engine_error: Any
    engine_age: Any
    spool_pending: int
    disk_free_bytes: Any
    outbox_count: int
    outbox_oldest: Any
    launch_state: str
    launch_reasons: list[str]
    launch_actions: list[str]
    shipper_state_missing: bool
    managed_attached: int
    managed_detached: int
    managed_degraded: int
    orphan_bridge_count: int
    unknown_managed_phase_count: int
    canonical_sessions_missing: bool
    canonical_sessions_invalid: bool
    repair_action: str


def _repair_action_for_launch_readiness(launch_readiness: dict[str, Any]) -> str:
    return _repair_command(
        can_reconcile_from_state=_can_reconcile_launch_from_state(
            state_exists=bool(launch_readiness.get("state_exists")),
            state_error=str(launch_readiness.get("state_error") or "").strip() or None,
            stored_url=str(launch_readiness.get("stored_url") or "").strip() or None,
            machine_name=str(launch_readiness.get("machine_name") or "").strip() or None,
        )
    )


def _add_transport_health_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    transport_assessment: TransportHealthAssessment | None,
    engine_log_path: str,
) -> None:
    if transport_assessment is None:
        return

    for reason in transport_assessment.reasons:
        if reason not in reasons:
            reasons.append(reason)
    if any(
        reason in transport_assessment.reasons
        for reason in (
            "consecutive_failures",
            "connect_errors",
            "server_errors",
            "rate_limited",
            "retryable_client_errors",
            "payload_rejected",
            "payload_too_large",
        )
    ):
        _with_action(actions, f"Inspect logs: {engine_log_path}")
    if "reported_offline" in transport_assessment.reasons:
        _with_action(actions, "Verify network reachability to your Longhouse URL")
    if "parse_errors" in transport_assessment.reasons:
        _with_action(actions, "Inspect recent dead letters and parser errors")
    if "spool_dead" in transport_assessment.reasons:
        _with_action(actions, "Repair dead letters before trusting continuity")


def _add_service_status_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    service_status: str,
    repair_action: str,
    shipper_state_missing: bool,
) -> None:
    if service_status == "not-installed":
        reasons.append("service_not_installed")
        if not shipper_state_missing:
            _with_action(actions, repair_action)
    elif service_status == "stopped":
        reasons.append("service_stopped")
        if not shipper_state_missing:
            _with_action(actions, repair_action)


def _add_engine_status_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    engine_error: Any,
    engine_exists: bool,
    engine_age: Any,
    engine_status_path: str,
    engine_log_path: str,
    service_status: str,
    repair_action: str,
    shipper_state_missing: bool,
) -> None:
    if engine_error:
        reasons.append("engine_status_unreadable")
        _with_action(actions, f"Inspect: {engine_status_path}")
    elif not engine_exists:
        reasons.append("engine_status_missing")
        if service_status == "running":
            _with_action(actions, "Wait for the first local status update or inspect engine logs")
        elif not shipper_state_missing:
            _with_action(actions, repair_action)
    elif engine_age is not None and engine_age > ENGINE_STALE_SECONDS:
        reasons.append("engine_status_stale")
        _with_action(actions, f"Inspect logs: {engine_log_path}")
    elif engine_age is not None and engine_age > ENGINE_FRESH_SECONDS:
        reasons.append("engine_status_aging")


def _add_canonical_session_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    canonical_sessions_missing: bool,
    canonical_sessions_invalid: bool,
) -> None:
    if canonical_sessions_missing:
        reasons.append("engine_status_sessions_missing")
        _with_action(actions, "Restart or repair Longhouse so the engine emits resolved sessions")
    if canonical_sessions_invalid:
        reasons.append("engine_status_sessions_invalid")
        _with_action(actions, "Inspect engine-status.json or restart Longhouse")


def _add_managed_session_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    orphan_bridge_count: int,
    managed_degraded: int,
    managed_detached: int,
    unknown_managed_phase_count: int,
) -> None:
    if orphan_bridge_count > 0:
        reasons.append("orphaned_managed_bridge")
        _with_action(actions, "Stop orphaned background managed sessions from Longhouse.app")

    if managed_degraded > 0:
        reasons.append("managed_session_control_degraded")
        _with_action(actions, "Inspect degraded managed sessions in Longhouse.app before sending input")

    if managed_detached > 0:
        reasons.append("managed_session_detached")
        _with_action(actions, "Reattach or stop detached managed sessions from Longhouse.app")

    if unknown_managed_phase_count > 0:
        reasons.append("managed_unknown_phase")
        _with_action(actions, "Update the managed phase contract before trusting this managed-session status")


def _add_spool_pending_reason(
    reasons: list[str],
    *,
    spool_pending: int,
) -> None:
    if spool_pending >= DEGRADED_BACKLOG_COUNT and "spool_pending" not in reasons:
        reasons.append("spool_pending")


def _add_outbox_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    outbox_count: int,
    outbox_oldest: Any,
    engine_log_path: str,
) -> None:
    degraded_outbox_is_old = outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS
    outbox_backlog_is_actionable = outbox_count >= BROKEN_BACKLOG_COUNT or (
        outbox_count >= DEGRADED_BACKLOG_COUNT and degraded_outbox_is_old
    )
    if outbox_backlog_is_actionable:
        reasons.append("outbox_backlog")
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
        reasons.append("outbox_stuck")
        _with_action(actions, f"Inspect logs: {engine_log_path}")


def _add_disk_reasons(
    reasons: list[str],
    actions: list[str],
    *,
    disk_free_bytes: Any,
) -> None:
    if isinstance(disk_free_bytes, int):
        if disk_free_bytes < DISK_BROKEN_BYTES:
            reasons.append("disk_critically_low")
            _with_action(actions, "Free local disk space before continuing to rely on shipping")
        elif disk_free_bytes < DISK_DEGRADED_BYTES:
            reasons.append("disk_low")
            _with_action(actions, "Consider freeing disk space soon")


def _launch_health_flags(launch_state: str) -> tuple[bool, bool]:
    if launch_state == "broken":
        return True, False
    if launch_state == "degraded":
        return False, True
    return False, False


def _managed_health_flags(
    *,
    orphan_bridge_count: int,
    managed_degraded: int,
    managed_detached: int,
    unknown_managed_phase_count: int,
) -> tuple[bool, bool]:
    if orphan_bridge_count > 0 or managed_degraded > 0 or unknown_managed_phase_count > 0:
        return True, False
    if managed_detached > 0:
        return False, True
    return False, False


def _broken_shipping_flag(
    *,
    service_status: str,
    engine_error: Any,
    engine_exists: bool,
    engine_age: Any,
    transport_assessment: TransportHealthAssessment | None,
    disk_free_bytes: Any,
    outbox_count: int,
    outbox_oldest: Any,
    spool_pending: int,
) -> bool:
    if service_status == "stopped":
        return True
    if engine_error:
        return True
    if transport_assessment is not None and transport_assessment.status == "broken":
        return True
    if isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_BROKEN_BYTES:
        return True
    if outbox_count >= BROKEN_BACKLOG_COUNT:
        return True
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_BROKEN_AGE_SECONDS:
        return True
    if spool_pending >= BROKEN_BACKLOG_COUNT:
        return True
    if service_status != "running" and (outbox_count > 0 or spool_pending > 0):
        return True
    engine_is_stale = engine_exists and engine_age is not None and engine_age > ENGINE_STALE_SECONDS
    has_pending_work = outbox_count > 0 or spool_pending > 0
    stale_engine_has_pending_work = engine_is_stale and has_pending_work
    return bool(stale_engine_has_pending_work)


def _degraded_shipping_flag(
    *,
    service_status: str,
    engine_exists: bool,
    engine_age: Any,
    transport_assessment: TransportHealthAssessment | None,
    disk_free_bytes: Any,
    outbox_count: int,
    outbox_oldest: Any,
) -> bool:
    if service_status != "running":
        return True
    if not engine_exists:
        return True
    if engine_age is not None and engine_age > ENGINE_FRESH_SECONDS:
        return True
    # Transport severity is delegated to the shared reducer. Keep local overlays
    # here, but let transport_assessment remain the shipping-state source of truth.
    if transport_assessment is not None and transport_assessment.status in ("offline", "degraded"):
        return True
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
        return True
    return bool(isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_DEGRADED_BYTES)


def _health_flags(
    *,
    launch_state: str,
    service_status: str,
    engine_error: Any,
    engine_exists: bool,
    engine_age: Any,
    transport_assessment: TransportHealthAssessment | None,
    disk_free_bytes: Any,
    outbox_count: int,
    outbox_oldest: Any,
    spool_pending: int,
    orphan_bridge_count: int,
    managed_degraded: int,
    managed_detached: int,
    unknown_managed_phase_count: int,
    canonical_sessions_missing: bool,
    canonical_sessions_invalid: bool,
) -> tuple[bool, bool]:
    broken, degraded = _launch_health_flags(launch_state)
    if canonical_sessions_missing or canonical_sessions_invalid:
        degraded = True
    managed_broken, managed_degraded_flag = _managed_health_flags(
        orphan_bridge_count=orphan_bridge_count,
        managed_degraded=managed_degraded,
        managed_detached=managed_detached,
        unknown_managed_phase_count=unknown_managed_phase_count,
    )
    broken = broken or managed_broken
    degraded = degraded or managed_degraded_flag

    if _broken_shipping_flag(
        service_status=service_status,
        engine_error=engine_error,
        engine_exists=engine_exists,
        engine_age=engine_age,
        transport_assessment=transport_assessment,
        disk_free_bytes=disk_free_bytes,
        outbox_count=outbox_count,
        outbox_oldest=outbox_oldest,
        spool_pending=spool_pending,
    ):
        broken = True

    if not broken:
        if _degraded_shipping_flag(
            service_status=service_status,
            engine_exists=engine_exists,
            engine_age=engine_age,
            transport_assessment=transport_assessment,
            disk_free_bytes=disk_free_bytes,
            outbox_count=outbox_count,
            outbox_oldest=outbox_oldest,
        ):
            degraded = True

    return broken, degraded


def _broken_health_headline(reasons: list[str]) -> str:
    headline = "Longhouse shipping needs repair"
    # Priority order matters: users should see the most specific actionable state.
    if any(
        reason in reasons
        for reason in (
            "shipper_state_missing",
            "machine_state_invalid",
            "machine_state_missing",
            "machine_state_missing_runtime_url",
            "machine_state_missing_machine_name",
            "config_url_runner_url_mismatch",
            "machine_name_runner_name_mismatch",
            "service_machine_name_mismatch",
            "service_generation_mismatch",
            "service_state_hash_mismatch",
            "service_runner_name_mismatch",
        )
    ):
        headline = "Longhouse launch config is inconsistent"
        if "shipper_state_missing" in reasons:
            headline = "Longhouse shipper state is missing"
    elif "service_stopped" in reasons:
        headline = "Longhouse engine service is stopped"
    elif "spool_dead" in reasons:
        headline = "Longhouse has dead-lettered data to repair"
    elif "engine_status_stale" in reasons:
        headline = "Longhouse local status is stale while work is pending"
    elif "orphaned_managed_bridge" in reasons:
        headline = "Longhouse has orphaned managed sessions"
    elif "managed_session_control_degraded" in reasons:
        headline = "Longhouse lost managed session control"
    elif "managed_unknown_phase" in reasons:
        headline = "Longhouse saw an unknown managed phase"
    return headline


def _degraded_health_headline(
    reasons: list[str],
    *,
    service_status: str,
    managed_attached: int,
    managed_detached: int,
) -> str:
    headline = "Longhouse shipping is degraded"
    # Priority order matters: users should see the most specific actionable state.
    if "reported_offline" in reasons:
        headline = "Longhouse is retrying while offline"
    elif "engine_status_missing" in reasons and service_status == "running":
        headline = "Longhouse is waiting for its first local status update"
    elif "engine_status_stale" in reasons:
        headline = "Longhouse local status is aging"
    elif "engine_status_aging" in reasons:
        headline = "Longhouse local status is aging"
    elif "engine_status_sessions_missing" in reasons:
        headline = "Longhouse local status needs a newer engine"
    elif "engine_status_sessions_invalid" in reasons:
        headline = "Longhouse local status has invalid session data"
    elif REASON_PROVIDER_SESSION_CWD_MISSING in reasons:
        headline = "A provider session working directory disappeared"
    elif REASON_PROVIDER_SESSION_CWD_REPLACED in reasons:
        headline = "A provider session working directory was replaced"
    elif REASON_BRIDGE_STATE_PATH_MISSING in reasons:
        headline = "A managed provider bridge state file is missing"
    elif "managed_session_detached" in reasons:
        if managed_detached == 1 and managed_attached == 0:
            headline = "Managed session is running in background"
        else:
            headline = "Managed sessions are running in background"
    return headline


def _health_classification_context(
    *,
    service: dict[str, Any],
    engine_status: dict[str, Any],
    transport_sample: TransportHealthSample | None,
    outbox: dict[str, Any],
    launch_readiness: dict[str, Any],
    managed_summary: dict[str, Any] | None,
    managed_sessions: list[dict[str, Any]],
) -> _HealthClassificationContext:
    service_status = str(service.get("status") or "not-installed")
    payload = engine_status.get("payload") or {}
    launch_reasons = [str(item) for item in list(launch_readiness.get("reasons") or [])]
    if transport_sample is not None:
        spool_pending = transport_sample.spool_pending
    else:
        spool_pending = int(payload.get("spool_pending_count") or 0)
    unknown_managed_phase_count = 0
    for session in managed_sessions:
        if _managed_phase_is_unknown(session.get("raw_phase")):
            unknown_managed_phase_count += 1

    return _HealthClassificationContext(
        service_status=service_status,
        engine_status_path=str(engine_status.get("path") or get_agent_status_path()),
        engine_log_path=str(service.get("log_path") or (get_agent_log_dir() / "engine.log.*")),
        engine_exists=bool(engine_status.get("exists")),
        engine_error=engine_status.get("error"),
        engine_age=engine_status.get("age_seconds"),
        spool_pending=spool_pending,
        disk_free_bytes=payload.get("disk_free_bytes"),
        outbox_count=int(outbox.get("file_count") or 0),
        outbox_oldest=outbox.get("oldest_age_seconds"),
        launch_state=str(launch_readiness.get("state") or "unconfigured"),
        launch_reasons=launch_reasons,
        launch_actions=[str(item) for item in list(launch_readiness.get("suggested_actions") or [])],
        shipper_state_missing="shipper_state_missing" in launch_reasons,
        managed_attached=int((managed_summary or {}).get("attached_count") or 0),
        managed_detached=int((managed_summary or {}).get("detached_count") or 0),
        managed_degraded=int((managed_summary or {}).get("degraded_count") or 0),
        orphan_bridge_count=int((managed_summary or {}).get("orphan_bridge_count") or 0),
        unknown_managed_phase_count=unknown_managed_phase_count,
        canonical_sessions_missing=bool((managed_summary or {}).get("canonical_sessions_missing")),
        canonical_sessions_invalid=bool((managed_summary or {}).get("canonical_sessions_invalid")),
        repair_action=_repair_action_for_launch_readiness(launch_readiness),
    )


def _collect_health_reasons(
    context: _HealthClassificationContext,
    *,
    transport_assessment: TransportHealthAssessment | None,
) -> tuple[list[str], list[str]]:
    reasons = list(context.launch_reasons)
    actions: list[str] = []

    for action in context.launch_actions:
        _with_action(actions, action)

    _add_transport_health_reasons(
        reasons,
        actions,
        transport_assessment=transport_assessment,
        engine_log_path=context.engine_log_path,
    )
    _add_service_status_reasons(
        reasons,
        actions,
        service_status=context.service_status,
        repair_action=context.repair_action,
        shipper_state_missing=context.shipper_state_missing,
    )
    _add_engine_status_reasons(
        reasons,
        actions,
        engine_error=context.engine_error,
        engine_exists=context.engine_exists,
        engine_age=context.engine_age,
        engine_status_path=context.engine_status_path,
        engine_log_path=context.engine_log_path,
        service_status=context.service_status,
        repair_action=context.repair_action,
        shipper_state_missing=context.shipper_state_missing,
    )
    _add_canonical_session_reasons(
        reasons,
        actions,
        canonical_sessions_missing=context.canonical_sessions_missing,
        canonical_sessions_invalid=context.canonical_sessions_invalid,
    )
    _add_spool_pending_reason(
        reasons,
        spool_pending=context.spool_pending,
    )
    _add_managed_session_reasons(
        reasons,
        actions,
        orphan_bridge_count=context.orphan_bridge_count,
        managed_degraded=context.managed_degraded,
        managed_detached=context.managed_detached,
        unknown_managed_phase_count=context.unknown_managed_phase_count,
    )
    _add_outbox_reasons(
        reasons,
        actions,
        outbox_count=context.outbox_count,
        outbox_oldest=context.outbox_oldest,
        engine_log_path=context.engine_log_path,
    )
    _add_disk_reasons(reasons, actions, disk_free_bytes=context.disk_free_bytes)

    return reasons, actions


def _is_uninstalled_health(context: _HealthClassificationContext) -> bool:
    return (
        context.service_status == "not-installed"
        and not context.engine_exists
        and context.outbox_count == 0
        and context.spool_pending == 0
        and context.launch_state != "broken"
    )


def _classify_health(
    *,
    service: dict[str, Any],
    engine_status: dict[str, Any],
    transport_sample: TransportHealthSample | None,
    transport_assessment: TransportHealthAssessment | None,
    outbox: dict[str, Any],
    launch_readiness: dict[str, Any],
    managed_summary: dict[str, Any] | None,
    managed_sessions: list[dict[str, Any]],
) -> tuple[str, str, str, list[str], list[str]]:
    context = _health_classification_context(
        service=service,
        engine_status=engine_status,
        transport_sample=transport_sample,
        outbox=outbox,
        launch_readiness=launch_readiness,
        managed_summary=managed_summary,
        managed_sessions=managed_sessions,
    )
    reasons, actions = _collect_health_reasons(
        context,
        transport_assessment=transport_assessment,
    )

    if _is_uninstalled_health(context):
        return (
            "uninstalled",
            "gray",
            "Longhouse local shipping is not installed",
            reasons,
            actions,
        )

    broken, degraded = _health_flags(
        launch_state=context.launch_state,
        service_status=context.service_status,
        engine_error=context.engine_error,
        engine_exists=context.engine_exists,
        engine_age=context.engine_age,
        transport_assessment=transport_assessment,
        disk_free_bytes=context.disk_free_bytes,
        outbox_count=context.outbox_count,
        outbox_oldest=context.outbox_oldest,
        spool_pending=context.spool_pending,
        orphan_bridge_count=context.orphan_bridge_count,
        managed_degraded=context.managed_degraded,
        managed_detached=context.managed_detached,
        unknown_managed_phase_count=context.unknown_managed_phase_count,
        canonical_sessions_missing=context.canonical_sessions_missing,
        canonical_sessions_invalid=context.canonical_sessions_invalid,
    )

    if broken:
        return ("broken", "red", _broken_health_headline(reasons), reasons, actions)

    if degraded:
        return (
            "degraded",
            "yellow",
            _degraded_health_headline(
                reasons,
                service_status=context.service_status,
                managed_attached=context.managed_attached,
                managed_detached=context.managed_detached,
            ),
            reasons,
            actions,
        )

    return ("healthy", "green", "Longhouse shipping healthy", reasons, actions)


def _collect_transport_health(
    engine_status: dict[str, Any],
) -> tuple[TransportHealthSample | None, TransportHealthAssessment | None]:
    if not bool(engine_status.get("exists")):
        return None, None
    if engine_status.get("error"):
        return None, None
    raw_payload = engine_status.get("payload")
    if not isinstance(raw_payload, Mapping):
        return None, None
    sample = transport_health_sample_from_engine_status_payload(raw_payload)
    return sample, assess_transport_health(sample)


def _serialize_transport_health(
    *,
    sample: TransportHealthSample | None,
    assessment: TransportHealthAssessment | None,
) -> dict[str, Any] | None:
    if sample is None or assessment is None:
        return None
    return {
        "source": "engine_status",
        "status": assessment.status,
        "status_reason": assessment.status_reason,
        "status_summary": assessment.status_summary,
        "reasons": list(assessment.reasons),
        "ship_attempts_1h": sample.ship_attempts_1h,
        "ship_successes_1h": sample.ship_successes_1h,
        "ship_success_rate_1h": sample.ship_success_rate_1h,
        "ship_rate_limited_1h": sample.ship_rate_limited_1h,
        "ship_server_errors_1h": sample.ship_server_errors_1h,
        "ship_payload_rejections_1h": sample.ship_payload_rejections_1h,
        "ship_payload_too_large_1h": sample.ship_payload_too_large_1h,
        "ship_retryable_client_errors_1h": sample.ship_retryable_client_errors_1h,
        "ship_connect_errors_1h": sample.ship_connect_errors_1h,
        "ship_attempts_10m": sample.ship_attempts_10m,
        "ship_successes_10m": sample.ship_successes_10m,
        "ship_rate_limited_10m": sample.ship_rate_limited_10m,
        "ship_server_errors_10m": sample.ship_server_errors_10m,
        "ship_retryable_client_errors_10m": sample.ship_retryable_client_errors_10m,
        "ship_connect_errors_10m": sample.ship_connect_errors_10m,
        "last_ship_result": sample.last_ship_result,
        "last_ship_http_status": sample.last_ship_http_status,
        "last_ship_error_kind": sample.last_ship_error_kind,
        "last_ship_error_message": sample.last_ship_error_message,
        "spool_pending": sample.spool_pending,
        "spool_dead": sample.spool_dead,
        "parse_errors_1h": sample.parse_errors_1h,
        "consecutive_failures": sample.consecutive_failures,
        "is_offline": sample.is_offline,
    }


def _collect_control_channel_health(engine_status: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(engine_status.get("exists")) or engine_status.get("error"):
        return None
    raw_payload = engine_status.get("payload")
    if not isinstance(raw_payload, Mapping):
        return None
    raw_control = raw_payload.get("control_channel")
    if not isinstance(raw_control, Mapping):
        return None

    supports = [str(item) for item in list(raw_control.get("supports") or []) if str(item).strip()]
    status = str(raw_control.get("status") or "disabled").strip() or "disabled"
    connected = status == "connected"
    operations_by_provider = machine_control_operations_by_provider(supports, connected=connected)
    control_operations_by_provider = {}
    for provider, operations in sorted(operations_by_provider.items()):
        control_operations_by_provider[provider] = list(operations)
    launchable_providers = sorted(
        provider
        for provider, operations in operations_by_provider.items()
        if "launch" in operations and provider in LAUNCH_CAPABILITY_BY_PROVIDER
    )
    can_launch_codex = "codex" in launchable_providers
    launch_blocked_by = None
    if not launchable_providers:
        launch_blocked_by = "no_launch_support" if connected else "control_down"

    return {
        "source": "engine_status",
        "enabled": bool(raw_control.get("enabled")),
        "status": status,
        "ws_url": raw_control.get("ws_url"),
        "last_connected_at": raw_control.get("last_connected_at"),
        "last_disconnected_at": raw_control.get("last_disconnected_at"),
        "last_error_code": raw_control.get("last_error_code"),
        "last_error_message": raw_control.get("last_error_message"),
        "reconnect_backoff_seconds": raw_control.get("reconnect_backoff_seconds"),
        "supports": supports,
        "control_operations_by_provider": control_operations_by_provider,
        "can_launch_codex": can_launch_codex,
        "can_launch_claude": "claude" in launchable_providers,
        "can_launch_opencode": "opencode" in launchable_providers,
        "launchable_providers": launchable_providers,
        "launch_blocked_by": launch_blocked_by,
    }


def _collect_provider_contracts() -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for contract in all_managed_provider_contracts():
        operations: dict[str, Any] = {}
        for operation, evidence in sorted(contract.operation_evidence.items()):
            supported = bool(getattr(contract, operation, False))
            operations[operation] = {
                "supported": supported,
                "evidence_level": evidence.get("level"),
                "evidence_source": evidence.get("source"),
                "next": evidence.get("next"),
            }
        providers[contract.provider] = {
            "managed_transport": contract.managed_transport.value,
            "control_plane": contract.control_plane,
            "control_plane_aliases": list(contract.control_plane_aliases),
            "machine_control_supports": list(contract.machine_control_supports),
            "operations": operations,
        }
    return {
        "schema_version": 1,
        "providers": providers,
    }


def _collect_managed_session_sources(
    base_dir: Path,
    *,
    engine_status: dict[str, Any],
    phase_overlay: dict[str, dict[str, str | None]] | None,
    fast: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_sessions = _collect_resolved_sessions_from_engine_status(engine_status)
    if resolved_sessions is not None:
        managed_sessions, unmanaged_processes = resolved_sessions
        managed_summary, managed_sessions, orphan_bridges = _merge_managed_sessions(
            bridge_sessions=[],
            bridge_orphans=[],
            process_sessions=managed_sessions,
        )
        return managed_summary, managed_sessions, orphan_bridges, unmanaged_processes

    if fast:
        issue = _engine_status_resolved_sessions_issue(engine_status)
        return _resolved_sessions_unusable_summary(issue), [], [], []
    else:
        with _process_snapshot_scope():
            provider_processes = _scan_provider_processes()
            bridge_sessions, orphan_bridges = _collect_managed_codex_sessions(
                base_dir,
                phase_overlay=phase_overlay,
            )
            bridge_session_ids = {row.get("session_id") for row in bridge_sessions if row.get("session_id")}
            process_sessions = _collect_managed_sessions_by_process(
                existing_session_ids=bridge_session_ids,
                phase_overlay=phase_overlay,
                scanned_processes=provider_processes,
            )
            unmanaged_processes = _collect_unmanaged_processes(scanned_processes=provider_processes)

    managed_summary, managed_sessions, orphan_bridges = _merge_managed_sessions(
        bridge_sessions=bridge_sessions,
        bridge_orphans=orphan_bridges,
        process_sessions=process_sessions,
    )
    return managed_summary, managed_sessions, orphan_bridges, unmanaged_processes


def collect_local_health(claude_dir: str | Path | None = None, *, fast: bool = False) -> dict[str, Any]:
    now = _utc_now()
    resolved_base_dir = _coerce_path(claude_dir)
    phase_overlay = None if fast else _load_managed_session_phase_state(resolved_base_dir, now=now)
    service = _collect_service(resolved_base_dir)
    engine_status = _collect_engine_status(resolved_base_dir, now=now)
    outbox = _collect_outbox(resolved_base_dir, now=now)
    provider_clis = _collect_provider_clis()
    provider_contracts = _collect_provider_contracts()
    provider_live_proof = collect_provider_live_proof(provider_clis, fast=fast, base_dir=resolved_base_dir)
    provider_live_route_e2e = collect_provider_live_route_e2e(
        fast=fast,
        base_dir=resolved_base_dir,
        expected_providers=expected_route_providers_from_live_proof(provider_live_proof),
    )
    provider_release_status = collect_provider_release_status(provider_clis, fast=fast)
    activity_summary = _collect_activity_summary(resolved_base_dir, now=now)
    managed_summary, managed_sessions, orphan_bridges, unmanaged_processes = _collect_managed_session_sources(
        resolved_base_dir,
        engine_status=engine_status,
        phase_overlay=phase_overlay,
        fast=fast,
    )
    launch_readiness = _collect_launch_readiness(resolved_base_dir, service=service)
    transport_sample, transport_assessment = _collect_transport_health(engine_status)
    control_channel = _collect_control_channel_health(engine_status)
    provider_support_state = collect_provider_support_state(
        provider_clis=provider_clis,
        provider_release_status=provider_release_status,
        provider_live_proof=provider_live_proof,
        control_channel=control_channel,
    )
    managed_session_ids = {
        session_id
        for session in managed_sessions
        for session_id in [_normalize_optional_string(session.get("session_id"))]
        if session_id is not None
    }
    managed_session_contracts = collect_managed_session_contract_diagnostics(
        resolved_base_dir,
        session_ids=managed_session_ids,
    )
    provider_hook_diagnostics = _collect_provider_hook_diagnostics(resolved_base_dir, now=now, fast=fast)
    health_state, severity, headline, reasons, suggested_actions = _classify_health(
        service=service,
        engine_status=engine_status,
        transport_sample=transport_sample,
        transport_assessment=transport_assessment,
        outbox=outbox,
        launch_readiness=launch_readiness,
        managed_summary=managed_summary,
        managed_sessions=managed_sessions,
    )
    latest_contract_issue = _apply_managed_session_contract_diagnostics(
        diagnostics=managed_session_contracts,
        reasons=reasons,
        suggested_actions=suggested_actions,
        managed_sessions=managed_sessions,
    )
    if provider_hook_diagnostics.get("state") == "session_cwd_missing":
        if REASON_PROVIDER_SESSION_CWD_MISSING not in reasons:
            reasons.append(REASON_PROVIDER_SESSION_CWD_MISSING)
        latest_hook_issue = provider_hook_diagnostics.get("latest")
        latest_cwd = latest_hook_issue.get("cwd") if isinstance(latest_hook_issue, Mapping) else None
        action = (
            f"Restart or reattach the affected provider session from an existing directory; missing cwd: {latest_cwd}"
            if latest_cwd
            else "Restart or reattach the affected provider session from an existing directory."
        )
        _with_action(suggested_actions, action)
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
            headline = "A provider session working directory disappeared"
    if latest_contract_issue is not None and health_state != "broken":
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
        headline = _managed_contract_headline(managed_session_contracts, latest_contract_issue)
    if int(provider_release_status.get("blocking_count") or 0) > 0:
        if "provider_release_blocked" not in reasons:
            reasons.append("provider_release_blocked")
        suggested_actions.append("Upgrade or downgrade the affected provider CLI before starting managed sessions.")
        health_state = "broken"
        severity = "red"
        headline = "Installed provider release is blocked"
    elif int(provider_release_status.get("warning_count") or 0) > 0:
        if "provider_release_warning" not in reasons:
            reasons.append("provider_release_warning")
        suggested_actions.append("Review provider release status before starting or upgrading managed sessions.")
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
            headline = "Provider release status needs attention"
    if provider_live_route_e2e.get("configured") and provider_live_route_e2e.get("status") != "ok":
        if "provider_live_route_e2e_warning" not in reasons:
            reasons.append("provider_live_route_e2e_warning")
        suggested_actions.append("Run dogfood refresh to refresh the hosted provider-live route proof.")
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
            headline = "Hosted provider-live route proof needs attention"
    elif provider_live_route_e2e.get("configured") and provider_live_route_e2e.get("coverage_status") == "missing":
        if "provider_live_route_e2e_coverage_missing" not in reasons:
            reasons.append("provider_live_route_e2e_coverage_missing")
        suggested_actions.append("Run dogfood refresh to prove every current provider route.")
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
            headline = "Hosted provider-live route proof is incomplete"
    build_identity = _collect_build_identity(engine_status=engine_status)

    return {
        "schema_version": SCHEMA_VERSION,
        "collection_tier": "fast" if fast else "deep",
        "collected_at": _to_rfc3339(now),
        "health_state": health_state,
        "severity": severity,
        "headline": headline,
        "reasons": reasons,
        "suggested_actions": suggested_actions,
        "service": service,
        "engine_status": engine_status,
        "transport_health": _serialize_transport_health(
            sample=transport_sample,
            assessment=transport_assessment,
        ),
        "control_channel": control_channel,
        "outbox": outbox,
        "provider_clis": provider_clis,
        "provider_contracts": provider_contracts,
        "provider_release_status": provider_release_status,
        "provider_live_proof": provider_live_proof,
        "provider_live_route_e2e": provider_live_route_e2e,
        "provider_support_state": provider_support_state,
        "managed_session_contracts": managed_session_contracts,
        "provider_hook_diagnostics": provider_hook_diagnostics,
        "activity_summary": activity_summary,
        "managed_summary": managed_summary,
        "managed_sessions": managed_sessions,
        "unmanaged_processes": unmanaged_processes,
        "orphan_bridges": orphan_bridges,
        "launch_readiness": launch_readiness,
        "build": build_identity,
        "update_info": _collect_update_info(),
        "thresholds": {
            "engine_fresh_seconds": ENGINE_FRESH_SECONDS,
            "engine_stale_seconds": ENGINE_STALE_SECONDS,
            "outbox_degraded_age_seconds": OUTBOX_DEGRADED_AGE_SECONDS,
            "outbox_broken_age_seconds": OUTBOX_BROKEN_AGE_SECONDS,
            "degraded_backlog_count": DEGRADED_BACKLOG_COUNT,
            "broken_backlog_count": BROKEN_BACKLOG_COUNT,
            "disk_degraded_bytes": DISK_DEGRADED_BYTES,
            "disk_broken_bytes": DISK_BROKEN_BYTES,
        },
    }
