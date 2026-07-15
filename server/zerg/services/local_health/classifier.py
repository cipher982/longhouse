from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zerg.services.longhouse_paths import get_agent_log_dir
from zerg.services.longhouse_paths import get_agent_status_path
from zerg.services.managed_session_contracts import REASON_BRIDGE_STATE_PATH_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_REPLACED
from zerg.services.transport_health import TransportHealthAssessment
from zerg.services.transport_health import TransportHealthSample

from ._shared import _with_action
from .archive import _add_archive_backlog_reason
from .constants import _WATCHING_REASONS
from .constants import BROKEN_BACKLOG_COUNT
from .constants import DEGRADED_BACKLOG_COUNT
from .constants import DISK_BROKEN_BYTES
from .constants import DISK_DEGRADED_BYTES
from .constants import ENGINE_FRESH_SECONDS
from .constants import ENGINE_STALE_SECONDS
from .constants import OUTBOX_BROKEN_AGE_SECONDS
from .constants import OUTBOX_DEGRADED_AGE_SECONDS
from .launch_readiness import _can_reconcile_launch_from_state
from .launch_readiness import _repair_command
from .phase import _managed_phase_is_unknown


@dataclass
class _HealthClassificationContext:
    service_status: str
    engine_status_path: str
    engine_log_path: str
    engine_exists: bool
    engine_error: Any
    engine_age: Any
    spool_pending: int
    archive_state: str
    archive_mode: str
    archive_pending_ranges: int
    archive_pending_bytes: int
    archive_dead_ranges: int
    archive_dead_bytes: int
    storage_blocked_sources: int
    storage_outbox_error: str | None
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
        _with_action(actions, "Inspect archive dead letters: longhouse archive status")


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
    _ = reasons, spool_pending


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
    archive_pending_ranges: int,
    archive_pending_bytes: int,
    archive_dead_ranges: int,
    archive_dead_bytes: int,
    storage_blocked_sources: int,
    storage_outbox_error: str | None,
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
    if archive_pending_ranges > 0 or archive_pending_bytes > 0:
        degraded = True
    if archive_dead_ranges > 0 or archive_dead_bytes > 0:
        degraded = True
    if storage_blocked_sources > 0:
        degraded = True
    if storage_outbox_error:
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


def _format_compact_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    scaled = float(max(0, int(value)))
    for unit in units:
        if scaled < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(scaled)} B"
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{max(0, int(value))} B"


def _degraded_health_headline(
    reasons: list[str],
    *,
    service_status: str,
    managed_attached: int,
    managed_detached: int,
    archive_state: str = "idle",
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
    elif "archive_repair_paused" in reasons:
        headline = "Longhouse archive repair is paused"
    elif "archive_repair_draining" in reasons:
        headline = (
            "Uploading archive backlog"
            if archive_state == "uploading"
            else "Scanning local archive"
            if archive_state == "scanning"
            else "Live shipping healthy; archive repair draining"
        )
    elif "archive_dead_lettered" in reasons:
        headline = "Longhouse archive repair needs attention"
    elif "archive_backlog_pending" in reasons:
        headline = "Archive upload blocked" if archive_state == "blocked" else "Longhouse archive repair pending"
    elif "storage_v2_sources_blocked" in reasons:
        headline = "Source upload conflict"
    elif "storage_v2_outbox_unreadable" in reasons:
        headline = "Source upload state unavailable"
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
    archive_repair: dict[str, Any],
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
    archive_state = str(archive_repair.get("state") or "idle")
    archive_mode = str(archive_repair.get("mode") or "idle")
    archive_pending_ranges = int(archive_repair.get("pending_ranges") or 0)
    archive_pending_bytes = int(archive_repair.get("pending_bytes") or 0)
    archive_dead_ranges = int(archive_repair.get("dead_ranges") or 0)
    archive_dead_bytes = int(archive_repair.get("dead_bytes") or 0)
    storage_v2_outbox = payload.get("storage_v2_outbox") or {}
    if not isinstance(storage_v2_outbox, dict):
        storage_v2_outbox = {}
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
        archive_state=archive_state,
        archive_mode=archive_mode,
        archive_pending_ranges=archive_pending_ranges,
        archive_pending_bytes=archive_pending_bytes,
        archive_dead_ranges=archive_dead_ranges,
        archive_dead_bytes=archive_dead_bytes,
        storage_blocked_sources=int(storage_v2_outbox.get("blocked_source_count") or 0),
        storage_outbox_error=str(storage_v2_outbox.get("error") or "").strip() or None,
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
    _add_archive_backlog_reason(
        reasons,
        actions,
        archive_state=context.archive_state,
        archive_mode=context.archive_mode,
        archive_pending_ranges=context.archive_pending_ranges,
        archive_pending_bytes=context.archive_pending_bytes,
        archive_dead_ranges=context.archive_dead_ranges,
        archive_dead_bytes=context.archive_dead_bytes,
    )
    if context.storage_blocked_sources > 0:
        reasons.append("storage_v2_sources_blocked")
        _with_action(actions, "Inspect the blocked source proof in engine-status.json")
    if context.storage_outbox_error:
        reasons.append("storage_v2_outbox_unreadable")
        _with_action(actions, "Inspect the storage-v2 outbox database error in engine-status.json")
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
        and context.archive_pending_ranges == 0
        and context.archive_dead_ranges == 0
        and context.launch_state != "broken"
    )


def _outbox_is_actionable(context: _HealthClassificationContext) -> bool:
    outbox_oldest = context.outbox_oldest
    degraded_outbox_is_old = outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS
    return context.outbox_count >= DEGRADED_BACKLOG_COUNT or (context.outbox_count > 0 and degraded_outbox_is_old)


def _degraded_state_is_watching(
    *,
    context: _HealthClassificationContext,
    reasons: list[str],
) -> bool:
    if context.service_status != "running":
        return False
    if context.engine_error:
        return False
    if context.launch_state == "degraded":
        return False
    if context.canonical_sessions_missing or context.canonical_sessions_invalid:
        return False
    if context.orphan_bridge_count > 0 or context.managed_degraded > 0 or context.managed_detached > 0:
        return False
    if context.unknown_managed_phase_count > 0:
        return False
    if _outbox_is_actionable(context):
        return False
    if context.archive_pending_ranges > 0 or context.archive_pending_bytes > 0:
        return False
    if context.archive_dead_ranges > 0 or context.archive_dead_bytes > 0:
        return False
    if "reported_offline" in reasons and (context.spool_pending > 0 or context.outbox_count > 0):
        return False
    if not reasons:
        return True
    return all(reason in _WATCHING_REASONS for reason in reasons)


def _archive_draining_state_is_watching(
    *,
    context: _HealthClassificationContext,
    reasons: list[str],
) -> bool:
    if context.archive_state not in {"draining", "scanning", "uploading"}:
        return False
    if context.archive_pending_ranges <= 0 and context.archive_pending_bytes <= 0:
        return False
    if context.archive_dead_ranges > 0 or context.archive_dead_bytes > 0:
        return False
    if context.service_status != "running":
        return False
    if context.engine_error:
        return False
    if context.launch_state == "degraded":
        return False
    if context.canonical_sessions_missing or context.canonical_sessions_invalid:
        return False
    if context.orphan_bridge_count > 0 or context.managed_degraded > 0 or context.managed_detached > 0:
        return False
    if context.unknown_managed_phase_count > 0:
        return False
    if _outbox_is_actionable(context):
        return False
    allowed_reasons = set(_WATCHING_REASONS)
    allowed_reasons.add("archive_repair_draining")
    return all(reason in allowed_reasons for reason in reasons)


def _archive_draining_attention_summary(context: _HealthClassificationContext) -> str:
    activity = "uploading" if context.archive_state == "uploading" else "scanning"
    return (
        f"Live shipping is healthy. Longhouse is {activity} "
        f"{_format_compact_bytes(context.archive_pending_bytes)} across "
        f"{context.archive_pending_ranges} range(s)."
    )


def _derive_attention(
    *,
    health_state: str,
    headline: str,
    reasons: list[str],
    suggested_actions: list[str],
    context: _HealthClassificationContext,
) -> dict[str, Any]:
    normalized_state = str(health_state or "").strip().lower()
    if normalized_state == "broken":
        return {
            "state": "repair",
            "headline": headline,
            "summary": "Repair is the fastest path to restore dependable Longhouse shipping on this Mac.",
            "reasons": reasons,
            "suggested_actions": suggested_actions,
        }
    if normalized_state == "degraded":
        if _archive_draining_state_is_watching(context=context, reasons=reasons):
            return {
                "state": "watching",
                "headline": headline,
                "summary": _archive_draining_attention_summary(context),
                "reasons": reasons,
                "suggested_actions": [],
            }
        if _degraded_state_is_watching(context=context, reasons=reasons):
            return {
                "state": "watching",
                "headline": "Longhouse is retrying quietly",
                "summary": "Recent shipping retries are recorded, but no durable backlog or repair step exists yet.",
                "reasons": reasons,
                "suggested_actions": [],
            }
        return {
            "state": "needs_attention",
            "headline": headline,
            "summary": "Longhouse is still running, but this state is persistent or actionable enough to inspect.",
            "reasons": reasons,
            "suggested_actions": suggested_actions,
        }
    if normalized_state == "uninstalled":
        return {
            "state": "quiet",
            "headline": headline,
            "summary": "Longhouse local shipping is not installed on this Mac.",
            "reasons": reasons,
            "suggested_actions": suggested_actions,
        }
    return {
        "state": "quiet",
        "headline": "Longhouse is quiet",
        "summary": "Shipping is healthy on this Mac.",
        "reasons": [],
        "suggested_actions": [],
    }


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
    archive_repair: dict[str, Any],
) -> tuple[str, str, str, list[str], list[str]]:
    context = _health_classification_context(
        service=service,
        engine_status=engine_status,
        transport_sample=transport_sample,
        outbox=outbox,
        launch_readiness=launch_readiness,
        archive_repair=archive_repair,
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
        archive_pending_ranges=context.archive_pending_ranges,
        archive_pending_bytes=context.archive_pending_bytes,
        archive_dead_ranges=context.archive_dead_ranges,
        archive_dead_bytes=context.archive_dead_bytes,
        storage_blocked_sources=context.storage_blocked_sources,
        storage_outbox_error=context.storage_outbox_error,
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
                archive_state=context.archive_state,
            ),
            reasons,
            actions,
        )

    return ("healthy", "green", "Longhouse shipping healthy", reasons, actions)


__all__ = [
    "_HealthClassificationContext",
    "_repair_action_for_launch_readiness",
    "_add_transport_health_reasons",
    "_add_service_status_reasons",
    "_add_engine_status_reasons",
    "_add_canonical_session_reasons",
    "_add_managed_session_reasons",
    "_add_spool_pending_reason",
    "_add_outbox_reasons",
    "_add_disk_reasons",
    "_launch_health_flags",
    "_managed_health_flags",
    "_broken_shipping_flag",
    "_degraded_shipping_flag",
    "_health_flags",
    "_broken_health_headline",
    "_format_compact_bytes",
    "_degraded_health_headline",
    "_health_classification_context",
    "_collect_health_reasons",
    "_is_uninstalled_health",
    "_outbox_is_actionable",
    "_degraded_state_is_watching",
    "_archive_draining_state_is_watching",
    "_archive_draining_attention_summary",
    "_derive_attention",
    "_classify_health",
]
