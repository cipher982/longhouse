"""Local Longhouse engine health snapshot helpers.

This module is the canonical local status classifier for the CLI and future
desktop surfaces. It combines raw local probes with a small derived state model
without hiding the underlying signals.
"""

# ruff: noqa: F401

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
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
from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.cursor_transcript import iter_local_cursor_session_summaries
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_log_dir
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path
from zerg.services.longhouse_paths import get_machine_token_path
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

from ._shared import _canonical_stable_home
from ._shared import _coerce_path
from ._shared import _collect_provider_cli
from ._shared import _collect_provider_clis
from ._shared import _max_rfc3339
from ._shared import _normalize_optional_int
from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339
from ._shared import _provider_cli_reference
from ._shared import _read_trimmed_file
from ._shared import _resolve_provider_cli_candidate
from ._shared import _to_rfc3339
from ._shared import _utc_now
from ._shared import _with_action
from .activity import _activity_band_specs
from .activity import _activity_cutoffs
from .activity import _activity_provider_counts
from .activity import _activity_recency_bands
from .activity import _activity_recent_touches
from .activity import _activity_sql_fragments
from .activity import _collect_activity_summary
from .activity import _derive_workspace_label
from .activity import _empty_activity_summary
from .activity import _payload_message_context
from .activity import _payload_metadata_context
from .activity import _populate_activity_aggregate
from .activity import _read_session_context
from .activity import _recent_touch_entry
from .activity import _session_context_from_payload
from .activity import _session_context_payloads
from .archive import _add_archive_backlog_reason
from .bindings import _binding_by_session_id
from .bindings import _collect_provider_binding_diagnostics
from .bindings import _load_session_binding_rows
from .bindings import _normalize_binding_path
from .classifier import _add_canonical_session_reasons
from .classifier import _add_disk_reasons
from .classifier import _add_engine_status_reasons
from .classifier import _add_managed_session_reasons
from .classifier import _add_outbox_reasons
from .classifier import _add_service_status_reasons
from .classifier import _add_spool_pending_reason
from .classifier import _add_transport_health_reasons
from .classifier import _archive_draining_attention_summary
from .classifier import _archive_draining_state_is_watching
from .classifier import _broken_health_headline
from .classifier import _broken_shipping_flag
from .classifier import _classify_health
from .classifier import _collect_health_reasons
from .classifier import _degraded_health_headline
from .classifier import _degraded_shipping_flag
from .classifier import _degraded_state_is_watching
from .classifier import _derive_attention
from .classifier import _format_compact_bytes
from .classifier import _health_classification_context
from .classifier import _health_flags
from .classifier import _HealthClassificationContext
from .classifier import _is_uninstalled_health
from .classifier import _launch_health_flags
from .classifier import _managed_health_flags
from .classifier import _outbox_is_actionable
from .classifier import _repair_action_for_launch_readiness
from .claude import _collect_provider_hook_diagnostics
from .claude import _hook_diagnostic_event_from_payload
from .claude import _hook_error_commands_from_payload
from .claude import _hook_error_looks_like_deleted_cwd_spawn
from .claude import _hook_errors_from_payload
from .claude import _provider_config_dir_for_hook_diagnostics
from .claude import _recent_claude_transcript_paths
from .codex import _bridge_is_alive
from .codex import _codex_bridge_live_runtime_ingest_health
from .codex import _codex_bridge_reason_codes
from .codex import _codex_bridge_state_dir
from .codex import _codex_managed_session_row
from .codex import _codex_orphan_bridge_row
from .codex import _codex_rollout_is_subagent
from .codex import _codex_source_is_subagent
from .codex import _codex_ui_presence
from .codex import _collect_managed_codex_sessions
from .codex import _extract_log_number
from .codex import _find_attached_codex_process
from .codex import _find_bridge_child_process
from .codex import _find_codex_app_server_process
from .codex import _looks_like_subagent_control_error
from .codex import _purge_stale_bridge_files
from .codex import _update_max_health_number
from .constants import _MANAGED_FINISHED_RETENTION_SECONDS
from .constants import _SHELL_SPAWN_ENOENT_PATTERNS
from .constants import _THREAD_SUBSCRIPTION_TRANSIENT_STATES
from .constants import _UUID_RE
from .constants import _WATCHING_REASONS
from .constants import _ZOMBIE_PROCESS_STATUSES
from .constants import ACTIVITY_RECENCY_BANDS
from .constants import ACTIVITY_RECENT_MINUTES
from .constants import ANTIGRAVITY_BIN_ENV
from .constants import BROKEN_BACKLOG_COUNT
from .constants import CODEX_BIN_ENV
from .constants import CODEX_BRIDGE_LIVE_DROPPED_MARKER
from .constants import CODEX_BRIDGE_LIVE_RETRY_MARKER
from .constants import CODEX_BRIDGE_LIVE_SLOW_MARKER
from .constants import CODEX_BRIDGE_LOG_TAIL_BYTES
from .constants import CODEX_BRIDGE_RUNTIME_FAILED_MARKER
from .constants import CODEX_BRIDGE_RUNTIME_NETWORK_ERROR_MARKER
from .constants import CONTROL_PATH_MANAGED
from .constants import CONTROL_PATH_UNMANAGED
from .constants import DEGRADED_BACKLOG_COUNT
from .constants import DISK_BROKEN_BYTES
from .constants import DISK_DEGRADED_BYTES
from .constants import ENGINE_FRESH_SECONDS
from .constants import ENGINE_STALE_SECONDS
from .constants import LAUNCH_CAPABILITY_BY_PROVIDER
from .constants import LIVENESS_MODEL_CODEX_BRIDGE
from .constants import LIVENESS_MODEL_ENGINE_STATUS
from .constants import LIVENESS_MODEL_PROCESS_SCAN
from .constants import LIVENESS_MODEL_TRANSCRIPT
from .constants import OPENCODE_BIN_ENV
from .constants import OUTBOX_BROKEN_AGE_SECONDS
from .constants import OUTBOX_DEGRADED_AGE_SECONDS
from .constants import PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW
from .constants import PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT
from .constants import PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT
from .constants import PROVIDER_HOOK_DIAGNOSTIC_WINDOW
from .constants import RECENT_TOUCH_LIMIT
from .constants import SCHEMA_VERSION
from .contracts import _apply_managed_session_contract_diagnostics
from .contracts import _collect_provider_contracts
from .contracts import _managed_contract_headline
from .cursor import _collect_cursor_discovery
from .engine_status import _collect_build_identity
from .engine_status import _collect_engine_status
from .engine_status import _collect_managed_sessions_from_engine_status
from .engine_status import _collect_outbox
from .engine_status import _collect_resolved_sessions_from_engine_status
from .engine_status import _engine_status_managed_session_row
from .engine_status import _engine_status_payload
from .engine_status import _engine_status_resolved_sessions
from .engine_status import _engine_status_resolved_sessions_issue
from .engine_status import _mark_managed_session_degraded
from .engine_status import _parse_iso8601
from .engine_status import _resolved_engine_managed_session_row
from .engine_status import _resolved_engine_opencode_server_is_live
from .engine_status import _resolved_engine_session_app_server_is_live
from .engine_status import _resolved_engine_unmanaged_process_row
from .engine_status import _resolved_join_key_value
from .engine_status import _resolved_session_mapping
from .engine_status import _resolved_session_state
from .engine_status import _resolved_sessions_unusable_summary
from .engine_status import _validate_resolved_engine_managed_sessions
from .launch_readiness import _add_launch_machine_state_reasons
from .launch_readiness import _add_launch_runner_config_reasons
from .launch_readiness import _add_launch_service_config_reasons
from .launch_readiness import _add_launch_service_runner_reason
from .launch_readiness import _add_launch_shipper_state_reason
from .launch_readiness import _apply_launch_readiness_overrides
from .launch_readiness import _apply_runner_name_override_reason
from .launch_readiness import _apply_runner_url_override_reason
from .launch_readiness import _can_reconcile_launch_from_state
from .launch_readiness import _candidate_runner_env_paths
from .launch_readiness import _collect_launch_readiness
from .launch_readiness import _collect_launch_readiness_context
from .launch_readiness import _collect_local_config
from .launch_readiness import _collect_runner_config
from .launch_readiness import _collect_service
from .launch_readiness import _drop_launch_reason
from .launch_readiness import _empty_service_metadata
from .launch_readiness import _extract_machine_name_from_args
from .launch_readiness import _extract_service_machine_name
from .launch_readiness import _extract_service_metadata
from .launch_readiness import _launch_override_context
from .launch_readiness import _launch_override_repair_command
from .launch_readiness import _launch_override_state
from .launch_readiness import _launch_readiness_configured
from .launch_readiness import _launch_readiness_payload
from .launch_readiness import _launch_readiness_state
from .launch_readiness import _LaunchOverrideContext
from .launch_readiness import _LaunchReadinessContext
from .launch_readiness import _missing_runner_config
from .launch_readiness import _parse_env_file
from .launch_readiness import _read_service_plist
from .launch_readiness import _repair_command
from .launch_readiness import _runner_config_from_env
from .launch_readiness import _runner_config_payload
from .launch_readiness import _runner_urls_from_env
from .launch_readiness import _service_file_path
from .launch_readiness import _service_metadata_from_env
from .launch_readiness import _state_root_tracks_machine_runner
from .launch_readiness import _systemd_environment
from .launch_readiness import _systemd_exec_start_arguments
from .launch_readiness import collect_launch_readiness
from .phase import _load_managed_session_phase_state
from .phase import _load_outbox_session_phase_rows
from .phase import _load_persisted_managed_session_phase_rows
from .phase import _managed_phase_is_unknown
from .phase import _phase_display_label
from .phase import _should_keep_managed_phase_row
from .process import _PROCESS_SNAPSHOT
from .process import _collect_managed_sessions_by_process
from .process import _collect_process_rows
from .process import _collect_process_snapshot
from .process import _collect_unmanaged_processes
from .process import _compute_process_snapshot
from .process import _is_antigravity_cmdline
from .process import _is_claude_cmdline
from .process import _is_codex_cmdline
from .process import _is_opencode_cmdline
from .process import _process_managed_session_row
from .process import _process_row_by_pid
from .process import _process_row_is_zombie
from .process import _process_snapshot_scope
from .process import _provider_for_cmdline
from .process import _scan_provider_processes
from .process import _session_id_from_argv
from .transport_health import _collect_control_channel_health
from .transport_health import _collect_transport_health
from .transport_health import _serialize_transport_health


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


def _managed_session_title_url(runtime_url: str, session_id: str) -> str:
    return urllib.parse.urljoin(runtime_url.rstrip("/") + "/", f"api/agents/sessions/{urllib.parse.quote(session_id)}")


_MANAGED_SESSION_TITLE_FETCH_LIMIT = 8
_MANAGED_SESSION_TITLE_FETCH_TIMEOUT_SECONDS = 0.45


def _fetch_managed_session_title(
    runtime_url: str,
    token: str,
    session_id: str,
    *,
    timeout: float = _MANAGED_SESSION_TITLE_FETCH_TIMEOUT_SECONDS,
) -> dict[str, str | None]:
    req = urllib.request.Request(
        _managed_session_title_url(runtime_url, session_id),
        headers={
            "Accept": "application/json",
            "User-Agent": "LonghouseLocalHealth/1.0",
            "X-Agents-Token": token,
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        return {}
    return {
        "summary_title": _normalize_optional_string(payload.get("summary_title")),
        "timeline_title": _normalize_optional_string(payload.get("timeline_title")),
        "first_user_message": _normalize_optional_string(payload.get("first_user_message")),
        "title_state": _normalize_optional_string(payload.get("title_state")),
        "title_source": _normalize_optional_string(payload.get("title_source")),
    }


def _enrich_managed_session_titles(
    base_dir: Path,
    managed_sessions: list[dict[str, Any]],
    *,
    runtime_url: str | None = None,
    token: str | None = None,
) -> None:
    """Best-effort remote title overlay for the tiny live menu-bar row set."""
    if not managed_sessions:
        return

    if runtime_url is None:
        try:
            _state_path, machine_state, _state_error = read_machine_state(base_dir)
        except Exception:
            return
        runtime_url = machine_state.runtime_url if machine_state else None
    runtime_url = _normalize_optional_string(runtime_url)
    if runtime_url is None:
        return
    parsed_runtime_url = urllib.parse.urlparse(runtime_url)
    if parsed_runtime_url.scheme not in {"http", "https"} or not parsed_runtime_url.netloc:
        return

    if token is None:
        token = _read_trimmed_file(get_machine_token_path(base_dir))
    token = _normalize_optional_string(token)
    if token is None:
        return

    by_session_id: dict[str, list[dict[str, Any]]] = {}
    for row in managed_sessions:
        session_id = _normalize_optional_string(row.get("session_id"))
        if session_id is None:
            continue
        if session_id not in by_session_id and len(by_session_id) >= _MANAGED_SESSION_TITLE_FETCH_LIMIT:
            continue
        by_session_id.setdefault(session_id, []).append(row)

    if not by_session_id:
        return

    with ThreadPoolExecutor(max_workers=len(by_session_id)) as executor:
        futures = {
            executor.submit(_fetch_managed_session_title, runtime_url, token, session_id): session_id for session_id in by_session_id
        }
        for future in as_completed(futures):
            session_id = futures[future]
            rows = by_session_id.get(session_id) or []
            if not rows:
                continue
            try:
                title_payload = future.result()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                continue
            for row in rows:
                for key, value in title_payload.items():
                    if value:
                        row[key] = value


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
        if not fast:
            process_rows = _collect_process_rows()
            managed_sessions = _validate_resolved_engine_managed_sessions(
                managed_sessions,
                process_rows=process_rows,
            )
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
    # Session/process discovery stays local on the menu-bar fast path, but the
    # resolved title is a Runtime Host projection. Fetch only this tiny overlay:
    # requests are parallel, bounded to eight rows, and individually timed out.
    _enrich_managed_session_titles(resolved_base_dir, managed_sessions)
    launch_readiness = _collect_launch_readiness(resolved_base_dir, service=service)
    transport_sample, transport_assessment = _collect_transport_health(engine_status)
    archive_repair = collect_archive_backlog(resolved_base_dir, engine_status_payload=engine_status.get("payload"))
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
    provider_binding_diagnostics = _collect_provider_binding_diagnostics(resolved_base_dir, now=now, fast=fast)
    cursor_discovery = _collect_cursor_discovery(fast=fast)
    health_state, severity, headline, reasons, suggested_actions = _classify_health(
        service=service,
        engine_status=engine_status,
        transport_sample=transport_sample,
        transport_assessment=transport_assessment,
        outbox=outbox,
        launch_readiness=launch_readiness,
        archive_repair=archive_repair,
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
    else:
        release_statuses = dict(provider_release_status.get("statuses") or {})
        version_probe_failed_providers = []
        for provider, raw_info in release_statuses.items():
            if dict(raw_info or {}).get("status") == "unknown_local_version":
                version_probe_failed_providers.append(provider)
        version_probe_failed_providers.sort()
        if version_probe_failed_providers:
            if "provider_cli_version_unknown" not in reasons:
                reasons.append("provider_cli_version_unknown")
            _with_action(
                suggested_actions,
                "Check provider CLI version output for: " + ", ".join(version_probe_failed_providers),
            )
            if health_state == "healthy":
                health_state = "degraded"
                severity = "yellow"
                headline = "Provider CLI version check needs attention"
        support_providers = dict(provider_support_state.get("providers") or {})
        support_attention_providers = []
        for provider, raw_info in support_providers.items():
            if dict(raw_info or {}).get("state") == "needs_attention":
                support_attention_providers.append(provider)
        support_attention_providers.sort()
        if support_attention_providers:
            if "provider_support_needs_attention" not in reasons:
                reasons.append("provider_support_needs_attention")
            _with_action(
                suggested_actions,
                "Review provider support proof details for: " + ", ".join(support_attention_providers),
            )
            if health_state == "healthy":
                health_state = "degraded"
                severity = "yellow"
                headline = "Managed provider support needs attention"
    if provider_live_route_e2e.get("configured") and provider_live_route_e2e.get("status") != "ok":
        if "provider_live_route_e2e_warning" not in reasons:
            reasons.append("provider_live_route_e2e_warning")
        suggested_actions.append("Run dogfood refresh to refresh the hosted provider-live route proof.")
        if health_state == "healthy":
            health_state = "degraded"
            severity = "yellow"
            headline = "Hosted provider-live route proof needs attention"
    elif provider_live_route_e2e.get("configured") and provider_live_route_e2e.get("coverage_status") == "missing":
        # Coverage gaps are useful operator detail, but a green proof for the
        # providers it covers must not make managed launch look broken.
        pass
    build_identity = _collect_build_identity(engine_status=engine_status)
    attention_context = _health_classification_context(
        service=service,
        engine_status=engine_status,
        transport_sample=transport_sample,
        outbox=outbox,
        launch_readiness=launch_readiness,
        archive_repair=archive_repair,
        managed_summary=managed_summary,
        managed_sessions=managed_sessions,
    )
    attention = _derive_attention(
        health_state=health_state,
        headline=headline,
        reasons=reasons,
        suggested_actions=suggested_actions,
        context=attention_context,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "collection_tier": "fast" if fast else "deep",
        "collected_at": _to_rfc3339(now),
        "health_state": health_state,
        "severity": severity,
        "headline": headline,
        "reasons": reasons,
        "suggested_actions": suggested_actions,
        "attention": attention,
        "service": service,
        "engine_status": engine_status,
        "transport_health": _serialize_transport_health(
            sample=transport_sample,
            assessment=transport_assessment,
        ),
        "archive_repair": archive_repair,
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
        "provider_binding_diagnostics": provider_binding_diagnostics,
        "cursor_discovery": cursor_discovery,
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
