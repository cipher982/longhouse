"""Reason-coded runner diagnostics for UI/Oikos doctor flows."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any

from zerg.models.models import Runner
from zerg.schemas.runner_schemas import RunnerDoctorCheck
from zerg.schemas.runner_schemas import RunnerDoctorResponse
from zerg.services.runner_health import assess_runner_health
from zerg.utils.time import utc_now_naive

KNOWN_INSTALL_MODES = {"desktop", "server"}
RECENT_OFFLINE_WINDOW = timedelta(minutes=10)


def _metadata_map(runner: Runner) -> dict[str, Any]:
    raw = runner.runner_metadata
    return raw if isinstance(raw, dict) else {}


def _normalized_install_mode(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("install_mode")
    return value if value in KNOWN_INSTALL_MODES else None


def _format_last_seen(last_seen_at: datetime | None) -> str:
    if last_seen_at is None:
        return "never"
    delta = utc_now_naive() - last_seen_at
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        mins = int(delta.total_seconds() // 60)
        return f"{mins}m ago"
    if delta < timedelta(days=1):
        hours = int(delta.total_seconds() // 3600)
        return f"{hours}h ago"
    days = delta.days
    return f"{days}d ago"


def _check(key: str, label: str, status: str, message: str) -> RunnerDoctorCheck:
    return RunnerDoctorCheck(key=key, label=label, status=status, message=message)


def diagnose_runner(
    runner: Runner,
    *,
    is_connected: bool | None = None,
) -> RunnerDoctorResponse:
    metadata = _metadata_map(runner)
    health = assess_runner_health(runner, is_connected=is_connected)
    install_mode = _normalized_install_mode(metadata)
    platform = metadata.get("platform") if isinstance(metadata.get("platform"), str) else None
    hostname = metadata.get("hostname") if isinstance(metadata.get("hostname"), str) else None
    reported_capabilities = health.reported_capabilities
    configured_capabilities = health.configured_capabilities
    last_seen_text = _format_last_seen(runner.last_seen_at)

    checks: list[RunnerDoctorCheck] = []
    repair_supported = False
    repair_install_mode: str | None = install_mode

    if health.effective_status == "revoked":
        checks.append(_check("connection", "Connection", "fail", "Runner is revoked and cannot reconnect."))
        checks.append(_check("repair", "Repair", "warn", "Create a new runner if you want this machine to reconnect."))
        return RunnerDoctorResponse(
            severity="error",
            reason_code="runner_revoked",
            summary="This runner was revoked and will not reconnect.",
            recommended_action="Add a new runner for this machine if you want it online again.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=False,
            checks=checks,
        )

    if health.effective_status == "online":
        checks.append(_check("connection", "Connection", "ok", health.status_summary))
    else:
        checks.append(_check("connection", "Connection", "fail", health.status_summary))

    if metadata:
        meta_bits = []
        if hostname:
            meta_bits.append(hostname)
        if platform:
            meta_bits.append(platform)
        if health.runner_version:
            meta_bits.append(f"v{health.runner_version}")
        meta_message = " · ".join(meta_bits) if meta_bits else "Runner reported metadata successfully."
        checks.append(_check("metadata", "Metadata", "ok", meta_message))
    else:
        checks.append(_check("metadata", "Metadata", "fail", "Runner has not reported metadata yet."))

    if install_mode:
        checks.append(_check("install_mode", "Install Mode", "ok", f"Runner reports `{install_mode}` mode."))
    else:
        mode_message = "Runner has not reported install mode yet. Re-enroll once to refresh metadata."
        checks.append(_check("install_mode", "Install Mode", "warn", mode_message))
        if platform == "darwin":
            repair_install_mode = "desktop"

    if not configured_capabilities:
        checks.append(_check("capabilities", "Capabilities", "warn", "Runner has no configured capabilities in Longhouse."))
    elif not reported_capabilities:
        checks.append(_check("capabilities", "Capabilities", "warn", "Runner has not reported local capabilities yet."))
    elif health.capabilities_match is True:
        checks.append(_check("capabilities", "Capabilities", "ok", "Local capabilities match Longhouse."))
    else:
        checks.append(
            _check(
                "capabilities",
                "Capabilities",
                "fail",
                f"Local runner capabilities ({', '.join(reported_capabilities)}) do not match Longhouse ({', '.join(configured_capabilities)}).",
            )
        )

    if not health.runner_version:
        checks.append(_check("version", "Version", "warn", "Runner has not reported its version yet."))
    elif not health.latest_runner_version:
        checks.append(_check("version", "Version", "warn", f"Runner is on v{health.runner_version}. Latest version is unknown."))
    elif health.version_status == "current":
        checks.append(_check("version", "Version", "ok", f"Runner is on the current release (v{health.runner_version})."))
    elif health.version_status == "outdated":
        checks.append(
            _check(
                "version",
                "Version",
                "warn",
                f"Runner is on v{health.runner_version}; latest is v{health.latest_runner_version}.",
            )
        )
    elif health.version_status == "ahead":
        checks.append(
            _check(
                "version",
                "Version",
                "warn",
                f"Runner reports v{health.runner_version}, which is ahead of configured latest v{health.latest_runner_version}.",
            )
        )
    else:
        checks.append(_check("version", "Version", "warn", f"Runner reports v{health.runner_version}, but version status is unknown."))

    if not metadata and runner.last_seen_at is None:
        repair_supported = True
        return RunnerDoctorResponse(
            severity="error",
            reason_code="runner_never_connected",
            summary="This runner has never connected to Longhouse.",
            recommended_action="Generate a repair command and run it on the target machine to finish installation.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=repair_supported,
            checks=checks,
        )

    if reported_capabilities and configured_capabilities and health.capabilities_match is False:
        repair_supported = True
        return RunnerDoctorResponse(
            severity="error",
            reason_code="runner_capabilities_mismatch",
            summary="This runner needs to re-enroll so its local capabilities match Longhouse.",
            recommended_action="Generate a repair command and re-run the installer on this machine.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=repair_supported,
            checks=checks,
        )

    if install_mode is None and metadata:
        repair_supported = True
        return RunnerDoctorResponse(
            severity="warning",
            reason_code="runner_metadata_incomplete",
            summary="This runner is using older metadata and should be re-enrolled once for clearer diagnostics.",
            recommended_action="Generate a repair command and re-run the installer once on this machine.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=repair_supported,
            checks=checks,
        )

    if health.effective_status == "online" and health.version_status == "outdated":
        repair_supported = True
        return RunnerDoctorResponse(
            severity="warning",
            reason_code="runner_version_outdated",
            summary=f"Runner is online but still on v{health.runner_version}; latest is v{health.latest_runner_version}.",
            recommended_action="Generate a repair command and re-run the installer once when convenient to update the binary.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=repair_supported,
            checks=checks,
        )

    if health.effective_status == "online":
        return RunnerDoctorResponse(
            severity="healthy",
            reason_code="healthy",
            summary="Runner is online and looks healthy.",
            recommended_action="No action needed.",
            install_mode=install_mode,
            repair_install_mode=repair_install_mode,
            repair_supported=False,
            checks=checks,
        )

    repair_supported = True
    now = utc_now_naive()
    last_seen_recently = bool(runner.last_seen_at and (now - runner.last_seen_at) <= RECENT_OFFLINE_WINDOW)
    if health.status_reason == "disconnected_recently":
        reason_code = "runner_disconnected_recently"
        summary = "Longhouse has a recent heartbeat on record, but there is no active runner connection."
    elif health.status_reason == "stale_heartbeat":
        reason_code = "runner_stale_heartbeat"
        summary = (
            f"Runner heartbeats went stale {last_seen_text}."
            if runner.last_seen_at
            else "Runner heartbeats went stale and Longhouse now considers it offline."
        )
    else:
        reason_code = "runner_offline_recently_seen" if last_seen_recently else "runner_offline"
        summary = (
            "Runner disconnected recently. The local service likely stopped or the machine went away."
            if last_seen_recently
            else "Runner is offline and needs attention on the target machine."
        )
    recommended_action = (
        "Restart the runner service on the machine. If that does not bring it back, generate a repair command and re-run the installer."
    )
    return RunnerDoctorResponse(
        severity="error",
        reason_code=reason_code,
        summary=summary,
        recommended_action=recommended_action,
        install_mode=install_mode,
        repair_install_mode=repair_install_mode,
        repair_supported=repair_supported,
        checks=checks,
    )
