"""Reason-coded runner diagnostics for UI/Oikos doctor flows."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any

from zerg.models.models import Runner
from zerg.schemas.runner_schemas import RunnerDoctorCheck
from zerg.schemas.runner_schemas import RunnerDoctorResponse
from zerg.utils.time import utc_now_naive

KNOWN_INSTALL_MODES = {"desktop", "server"}
RECENT_OFFLINE_WINDOW = timedelta(minutes=10)


def _metadata_map(runner: Runner) -> dict[str, Any]:
    raw = runner.runner_metadata
    return raw if isinstance(raw, dict) else {}


def _normalized_install_mode(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("install_mode")
    return value if value in KNOWN_INSTALL_MODES else None


def _reported_capabilities(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("capabilities")
    if not isinstance(raw, list):
        return []
    return sorted(str(item) for item in raw if isinstance(item, str))


def _configured_capabilities(runner: Runner) -> list[str]:
    raw = runner.capabilities or []
    return sorted(str(item) for item in raw if isinstance(item, str))


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


def diagnose_runner(runner: Runner) -> RunnerDoctorResponse:
    metadata = _metadata_map(runner)
    install_mode = _normalized_install_mode(metadata)
    platform = metadata.get("platform") if isinstance(metadata.get("platform"), str) else None
    hostname = metadata.get("hostname") if isinstance(metadata.get("hostname"), str) else None
    runner_version = metadata.get("runner_version") if isinstance(metadata.get("runner_version"), str) else None
    reported_capabilities = _reported_capabilities(metadata)
    configured_capabilities = _configured_capabilities(runner)
    last_seen_text = _format_last_seen(runner.last_seen_at)

    checks: list[RunnerDoctorCheck] = []
    repair_supported = False
    repair_install_mode: str | None = install_mode

    if runner.status == "revoked":
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

    if runner.status == "online":
        checks.append(_check("connection", "Connection", "ok", "Runner is online and connected."))
    else:
        message = f"Runner is offline. Last seen {last_seen_text}." if runner.last_seen_at else "Runner is offline and has never connected."
        checks.append(_check("connection", "Connection", "fail", message))

    if metadata:
        meta_bits = []
        if hostname:
            meta_bits.append(hostname)
        if platform:
            meta_bits.append(platform)
        if runner_version:
            meta_bits.append(f"v{runner_version}")
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
    elif reported_capabilities == configured_capabilities:
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

    if reported_capabilities and configured_capabilities and reported_capabilities != configured_capabilities:
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

    if runner.status == "online":
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
