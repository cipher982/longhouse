from __future__ import annotations

import json
import os
import subprocess
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

NATIVE_IDENTITY_TIMEOUT_SECONDS = 2.0
ENGINE_PROJECTION_STALE_SECONDS = 60


def _resolve_native_cli(*, binary_path: Any) -> Path | None:
    """Find the installed native facade paired with the running engine.

    The engine status file records the executable that the daemon started from.
    Looking beside that path first avoids accidentally inspecting a different
    ``longhouse`` found earlier on ``PATH``.  The remaining candidates mirror
    the native desktop resolver and are only used when the engine did not
    publish its path.
    """
    candidates: list[Path] = []
    if isinstance(binary_path, str) and binary_path.strip():
        candidates.append(Path(binary_path).expanduser().with_name("longhouse"))

    home = Path.home()
    candidates.extend(
        (
            home / ".local" / "bin" / "longhouse",
            home / "bin" / "longhouse",
            Path("/opt/homebrew/bin/longhouse"),
            Path("/usr/local/bin/longhouse"),
            Path("/usr/bin/longhouse"),
        )
    )
    candidates.extend(Path(entry) / "longhouse" for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _collect_installed_native_identity(*, engine_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read the installed native facade/engine pair's self-reported identity.

    ``longhouse build-identity --json`` verifies and reports both binaries.
    Python's package identity is deliberately not used here: the package and
    native pair are independently upgradeable artifacts.
    """
    binary_path = engine_payload.get("binary_path")
    native_cli = _resolve_native_cli(binary_path=binary_path)
    if native_cli is None:
        return {
            "error": "unavailable",
            "detail": "installed native longhouse facade is unavailable",
        }

    try:
        completed = subprocess.run(
            [str(native_cli), "build-identity", "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=NATIVE_IDENTITY_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return {"error": "unavailable", "detail": f"reading native build identity: {exc}"}

    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"native identity exited with status {completed.returncode}"
        return {"error": "unavailable", "detail": detail}

    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": "corrupt", "detail": f"native identity is not JSON: {exc}"}
    if not isinstance(raw, Mapping):
        return {"error": "corrupt", "detail": "native identity root is not an object"}

    raw_facade = raw.get("facade")
    raw_engine = raw.get("engine")
    if not isinstance(raw_facade, Mapping) or not isinstance(raw_engine, Mapping):
        return {"error": "corrupt", "detail": "native identity is missing facade or engine"}
    if not isinstance(raw.get("engine_path"), str) or not raw["engine_path"].strip():
        return {"error": "corrupt", "detail": "native identity is missing engine path"}
    if not _is_complete_build_identity(raw_facade) or not _is_complete_build_identity(raw_engine):
        return {"error": "corrupt", "detail": "native identity facade or engine is incomplete"}
    if raw_facade["commit"] != raw_engine["commit"]:
        return {"error": "corrupt", "detail": "native facade and engine commits differ"}

    engine_path = raw["engine_path"]
    return {
        "path": str(native_cli),
        "engine_path": engine_path,
        "facade": dict(raw_facade),
        "engine": dict(raw_engine),
    }


def _is_complete_build_identity(identity: Mapping[str, Any] | None) -> bool:
    if not isinstance(identity, Mapping):
        return False
    if identity.get("error") is not None:
        return False
    required_fields = ("version", "commit", "commit_short", "dirty", "built_at", "channel")
    if any(field not in identity for field in required_fields):
        return False
    string_fields_valid = all(isinstance(identity[field], str) and identity[field].strip() for field in required_fields if field != "dirty")
    if not string_fields_valid or not isinstance(identity["dirty"], bool):
        return False
    if identity["channel"] not in {"dev", "release"}:
        return False
    commit = identity["commit"].strip().lower()
    commit_short = identity["commit_short"].strip().lower()
    return commit.startswith(commit_short)


def _identity_commit_short(identity: Mapping[str, Any] | None) -> str | None:
    if not _is_complete_build_identity(identity):
        return None
    assert identity is not None
    value = identity.get("commit_short")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _collect_build_identity(*, engine_status: dict[str, Any]) -> dict[str, Any]:
    """Report Python, installed-native, and running-engine identities separately.

    The Python package and native pair are independently upgradeable.  Restart
    attribution therefore compares the installed native engine identity with
    the engine daemon's compiled identity, never the Python package commit.
    A native identity or timestamp that cannot be read remains explicit
    unknown evidence; a binary replacement still wins when its mtime proves it.
    """
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load as load_build_identity

    python_package: dict[str, Any]
    try:
        python_package_identity = load_build_identity()
        python_package = python_package_identity.as_dict()
    except BuildIdentityMissing as exc:
        python_package = {"error": "missing", "detail": str(exc)}

    # engine-status.json is engine-controlled but user-writable; guard against
    # corrupt payloads so local-health degrades cleanly instead of raising and
    # taking the whole menu bar snapshot down.
    raw_payload = engine_status.get("payload") if engine_status else None
    engine_payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
    raw_engine_build = engine_payload.get("build")
    running_engine: Mapping[str, Any] = raw_engine_build if isinstance(raw_engine_build, Mapping) else {}
    running_engine_short = _identity_commit_short(running_engine)
    installed_native = _collect_installed_native_identity(engine_payload=engine_payload)
    native_engine = installed_native.get("engine") if isinstance(installed_native.get("engine"), Mapping) else None
    native_facade = installed_native.get("facade") if isinstance(installed_native.get("facade"), Mapping) else None
    native_engine_short = _identity_commit_short(native_engine)
    native_engine_commit_mismatch: bool | None = (
        None
        if native_engine_short is None or running_engine_short is None
        else native_engine["commit"].strip().lower() != running_engine["commit"].strip().lower()
    )

    binary_mtime = _parse_iso8601(engine_payload.get("binary_mtime"))
    daemon_started_at = _parse_iso8601(engine_payload.get("daemon_started_at"))
    binary_times_present = binary_mtime is not None and daemon_started_at is not None
    binary_newer_than_daemon: bool | None = binary_mtime > daemon_started_at if binary_times_present else None
    if native_engine_commit_mismatch is True or binary_newer_than_daemon is True:
        engine_restart_pending: bool | None = True
    elif native_engine_commit_mismatch is False:
        engine_restart_pending = False
    else:
        engine_restart_pending = None

    components = []
    for name, identity in (
        ("python_package", python_package),
        ("native_facade", native_facade),
        ("installed_native_engine", native_engine),
        ("running_engine", running_engine),
    ):
        commit_short = _identity_commit_short(identity)
        if commit_short:
            components.append({"name": name, "commit_short": commit_short})

    return {
        "python_package": python_package,
        "installed_native": installed_native,
        "running_engine": dict(running_engine) if running_engine else None,
        "engine_restart_pending": engine_restart_pending,
        "restart_pending_reasons": {
            "native_engine_commit_mismatch": native_engine_commit_mismatch,
            "binary_newer_than_daemon": binary_newer_than_daemon,
        },
        "components": components,
    }


def _parse_iso8601(value: Any) -> datetime | None:
    """Parse an ISO-8601 string emitted by the engine. Returns None on any
    failure — callers preserve unknown timestamp evidence instead of inferring
    that the installed and running binaries match.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # datetime.fromisoformat in Py 3.11+ handles most RFC 3339 shapes,
        # including the fractional seconds + offset the engine emits.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
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
        file_age_seconds = int(max(0.0, now.timestamp() - status_path.stat().st_mtime))
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
            "age_seconds": file_age_seconds,
            "file_age_seconds": file_age_seconds,
            "payload": None,
            "error": str(exc),
        }
    if not isinstance(payload, Mapping):
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": file_age_seconds,
            "file_age_seconds": file_age_seconds,
            "payload": None,
            "error": "engine status payload must be a JSON object",
        }

    raw_projection = payload.get("local_projection")
    projection = raw_projection if isinstance(raw_projection, Mapping) else {}
    pulse_at = _parse_rfc3339(_normalize_optional_string(projection.get("engine_pulse_at")))
    pulse_age_seconds = int(max(0.0, (now - pulse_at).total_seconds())) if pulse_at is not None else None
    effective_age_seconds = pulse_age_seconds if pulse_age_seconds is not None else file_age_seconds
    generated_at = _parse_rfc3339(_normalize_optional_string(projection.get("generated_at")))
    generated_age_seconds = int(max(0.0, (now - generated_at).total_seconds())) if generated_at is not None else None
    reconciliation = projection.get("reconciliation") if isinstance(projection.get("reconciliation"), Mapping) else None
    reconciliation_started_at = _parse_rfc3339(_normalize_optional_string((reconciliation or {}).get("started_at")))
    reconciliation_age_seconds = (
        int(max(0.0, (now - reconciliation_started_at).total_seconds())) if reconciliation_started_at is not None else None
    )
    projection_stale = (
        generated_at is None
        or (generated_age_seconds is not None and generated_age_seconds >= ENGINE_PROJECTION_STALE_SECONDS)
        or (
            str((reconciliation or {}).get("state") or "").strip() == "reconciling"
            and reconciliation_age_seconds is not None
            and reconciliation_age_seconds >= ENGINE_PROJECTION_STALE_SECONDS
        )
    )

    return {
        "path": str(status_path),
        "exists": True,
        "fresh": effective_age_seconds <= ENGINE_FRESH_SECONDS,
        "age_seconds": effective_age_seconds,
        "file_age_seconds": file_age_seconds,
        "projection_generated_at": _normalize_optional_string(projection.get("generated_at")),
        "projection_age_seconds": generated_age_seconds,
        "projection_reconciliation_started_at": _normalize_optional_string((reconciliation or {}).get("started_at")),
        "projection_reconciliation_age_seconds": reconciliation_age_seconds,
        "projection_stale": projection_stale,
        "reconciliation": reconciliation,
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


def _engine_status_resolved_sessions(engine_status: dict[str, Any]) -> list[Any] | None:
    payload = _engine_status_payload(engine_status)
    if "sessions" not in payload:
        return None
    raw_rows = payload.get("sessions")
    if not isinstance(raw_rows, list):
        return None
    return raw_rows


def _resolved_session_mapping(raw_row: Any, field_name: str) -> Mapping[str, Any]:
    raw_value = raw_row.get(field_name) if isinstance(raw_row, Mapping) else None
    if isinstance(raw_value, Mapping):
        return raw_value
    return {}


def _resolved_session_state(raw_row: Mapping[str, Any]) -> str:
    normalized_state = _normalize_optional_string(raw_row.get("state"))
    if normalized_state in {"attached", "detached", "degraded"}:
        return normalized_state
    # A managed row with an unrecognized explicit state is contract drift.
    # Fail visibly instead of allowing it to look healthy; presentation_state
    # remains ignored because it is not a liveness fact.
    return "degraded" if normalized_state is not None else "unknown"


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
        "raw_phase": None,
        "phase": None,
        "phase_observed_at": None,
        "last_activity_at": last_activity_at,
        "timeline_title": _normalize_optional_string(raw_row.get("timeline_title")),
        "summary_title": _normalize_optional_string(raw_row.get("timeline_title")),
        "first_user_message": _normalize_optional_string(raw_row.get("first_user_message")),
        "title_state": _normalize_optional_string(raw_row.get("title_state")),
        "title_source": _normalize_optional_string(raw_row.get("title_source")),
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
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    raw_rows = _engine_status_resolved_sessions(engine_status)
    if raw_rows is None:
        return None
    observed_now = now or datetime.now(timezone.utc)

    # A resolved row describes the session; the managed lease ledger is the
    # freshness authority for control. A missing or malformed lease is unknown,
    # never an implicit attached fallback.
    payload = _engine_status_payload(engine_status)
    raw_managed_leases = payload.get("managed_sessions")
    managed_leases_by_session: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_managed_leases, list):
        for raw_lease in raw_managed_leases:
            if not isinstance(raw_lease, Mapping):
                continue
            session_id = _normalize_optional_string(raw_lease.get("session_id"))
            if session_id:
                managed_leases_by_session[session_id] = raw_lease

    # Phase evidence deliberately lives in the separate engine ledger: a
    # control lease is not an activity claim.  The resolved-session rows are
    # still the right local join surface, though.  Previously this reader
    # discarded the ledger and turned every managed row into an activity-free
    # preview, even while the same status file contained fresh `thinking` /
    # `running` evidence.  Merge only the current ledger row for the matching
    # managed session; freshness is enforced by the engine before emission.
    phase_by_session: dict[str, Mapping[str, Any]] = {}
    raw_ledger = payload.get("phase_ledger")
    if isinstance(raw_ledger, list):
        for item in raw_ledger:
            if not isinstance(item, Mapping):
                continue
            session_id = _normalize_optional_string(item.get("session_id"))
            phase = _normalize_optional_string(item.get("phase"))
            observed_at = _normalize_optional_string(item.get("observed_at"))
            if session_id and phase and observed_at:
                phase_by_session[session_id] = item

    managed_sessions: list[dict[str, Any]] = []
    unmanaged_processes: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        control_path = _normalize_optional_string(raw_row.get("control_path"))
        if control_path == CONTROL_PATH_MANAGED:
            row = _resolved_engine_managed_session_row(raw_row=raw_row)
            session_id = str(row.get("session_id") or "")
            raw_lease = managed_leases_by_session.get(session_id)
            reason = None
            if raw_lease is None:
                reason = "lease_evidence_missing"
            else:
                lease_observed_at = _parse_rfc3339(_normalize_optional_string(raw_lease.get("observed_at")))
                raw_ttl_ms = raw_lease.get("lease_ttl_ms")
                ttl_ms = raw_ttl_ms if type(raw_ttl_ms) is int and raw_ttl_ms > 0 else None
                if lease_observed_at is None or ttl_ms is None:
                    reason = "lease_evidence_invalid"
                elif (observed_now - lease_observed_at).total_seconds() * 1000 > ttl_ms:
                    reason = "lease_expired"
            if reason is not None:
                row["state"] = "unknown"
                row["bridge_status"] = None
                reason_codes = list(row.get("reason_codes") or [])
                if reason not in reason_codes:
                    reason_codes.append(reason)
                row["reason_codes"] = reason_codes
            phase_row = phase_by_session.get(session_id)
            if phase_row is not None:
                raw_phase = _normalize_optional_string(phase_row.get("phase"))
                tool_name = _normalize_optional_string(phase_row.get("tool_name"))
                observed_at = _normalize_optional_string(phase_row.get("observed_at"))
                if raw_phase and observed_at:
                    row["raw_phase"] = raw_phase
                    row["phase"] = _phase_display_label(raw_phase, tool_name)
                    row["phase_observed_at"] = observed_at
                    row["last_activity_at"] = _max_rfc3339(row.get("last_activity_at"), observed_at)
            managed_sessions.append(row)
        elif control_path == CONTROL_PATH_UNMANAGED:
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


__all__ = [
    "_collect_build_identity",
    "_parse_iso8601",
    "_resolve_native_cli",
    "_collect_installed_native_identity",
    "_collect_engine_status",
    "_collect_outbox",
    "_engine_status_payload",
    "_engine_status_resolved_sessions",
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
]
