"""Shared runner health assessment and API serialization helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any

from zerg.config import get_settings
from zerg.models.models import Runner
from zerg.schemas.runner_schemas import RunnerResponse
from zerg.utils.time import utc_now_naive

DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000
STALE_HEARTBEAT_MULTIPLIER = 3
MIN_STALE_AFTER_SECONDS = 90
KNOWN_INSTALL_MODES = {"desktop", "server"}
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class RunnerHealthAssessment:
    """Derived runner health facts used across API/tool surfaces."""

    effective_status: str
    status_reason: str
    status_summary: str
    heartbeat_interval_ms: int
    stale_after_seconds: int
    last_seen_age_seconds: int | None
    is_stale: bool
    is_connected: bool | None
    install_mode: str | None
    runner_version: str | None
    latest_runner_version: str | None
    version_status: str
    reported_capabilities: list[str]
    configured_capabilities: list[str]
    capabilities_match: bool | None


def _metadata_map(runner: Runner) -> dict[str, Any]:
    raw = runner.runner_metadata
    return raw if isinstance(raw, dict) else {}


def normalize_runner_binary_tag(tag: str | None) -> str | None:
    """Convert a release tag like ``runner-v0.1.3`` to ``0.1.3``."""
    if not tag:
        return None
    normalized = tag.strip()
    if normalized.startswith("runner-v"):
        normalized = normalized[len("runner-v") :]
    elif normalized.startswith("v"):
        normalized = normalized[1:]
    return normalized or None


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = _SEMVER_RE.search(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _heartbeat_interval_ms(metadata: dict[str, Any]) -> int:
    raw = metadata.get("heartbeat_interval_ms")
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        parsed = int(raw)
        if parsed > 0:
            return parsed
    return DEFAULT_HEARTBEAT_INTERVAL_MS


def _stale_after_seconds(heartbeat_interval_ms: int) -> int:
    return max(MIN_STALE_AFTER_SECONDS, ceil((heartbeat_interval_ms * STALE_HEARTBEAT_MULTIPLIER) / 1000))


def _install_mode(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("install_mode")
    return value if value in KNOWN_INSTALL_MODES else None


def _capabilities(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return sorted(normalized)


def assess_runner_health(
    runner: Runner,
    *,
    now: datetime | None = None,
    latest_runner_version: str | None = None,
    is_connected: bool | None = None,
) -> RunnerHealthAssessment:
    """Assess runner availability and health from durable state."""
    now = now or utc_now_naive()
    metadata = _metadata_map(runner)
    heartbeat_interval_ms = _heartbeat_interval_ms(metadata)
    stale_after_seconds = _stale_after_seconds(heartbeat_interval_ms)

    last_seen_age_seconds: int | None = None
    is_stale = False
    if runner.last_seen_at is not None:
        delta = now - runner.last_seen_at
        last_seen_age_seconds = max(0, int(delta.total_seconds()))
        is_stale = last_seen_age_seconds > stale_after_seconds

    configured_capabilities = _capabilities(runner.capabilities or [])
    reported_capabilities = _capabilities(metadata.get("capabilities"))
    capabilities_match: bool | None = None
    if configured_capabilities and reported_capabilities:
        capabilities_match = configured_capabilities == reported_capabilities

    runner_version = metadata.get("runner_version") if isinstance(metadata.get("runner_version"), str) else None
    latest_runner_version = latest_runner_version or normalize_runner_binary_tag(get_settings().runner_binary_tag)
    version_status = "unknown"
    current_tuple = _version_tuple(runner_version)
    latest_tuple = _version_tuple(latest_runner_version)
    if current_tuple and latest_tuple:
        if current_tuple == latest_tuple:
            version_status = "current"
        elif current_tuple < latest_tuple:
            version_status = "outdated"
        else:
            version_status = "ahead"

    effective_status = "offline"
    status_reason = "never_connected"
    status_summary = "Offline. This runner has never connected."

    if runner.status == "revoked":
        effective_status = "revoked"
        status_reason = "revoked"
        status_summary = "Revoked. This runner cannot reconnect."
    elif is_connected is True and runner.last_seen_at is None:
        effective_status = "online"
        status_reason = "connected"
        status_summary = "Online. Live runner connection is active."
    elif runner.last_seen_at is None:
        effective_status = "offline"
        status_reason = "never_connected"
        status_summary = "Offline. This runner has never connected."
    elif is_stale:
        effective_status = "offline"
        status_reason = "stale_heartbeat"
        status_summary = f"Offline. Last heartbeat {last_seen_age_seconds}s ago."
    elif is_connected is False:
        effective_status = "offline"
        status_reason = "disconnected_recently"
        if last_seen_age_seconds is None or last_seen_age_seconds < 10:
            status_summary = "Offline. The runner has no active websocket connection."
        else:
            status_summary = f"Offline. Last heartbeat {last_seen_age_seconds}s ago, but no live runner connection is active."
    else:
        effective_status = "online"
        status_reason = "fresh_heartbeat"
        if last_seen_age_seconds is None or last_seen_age_seconds < 10:
            status_summary = "Online. Heartbeats are current."
        else:
            status_summary = f"Online. Last heartbeat {last_seen_age_seconds}s ago."

    if effective_status == "online" and version_status == "outdated" and runner_version and latest_runner_version:
        status_summary = f"{status_summary[:-1]} but runner v{runner_version} is behind latest v{latest_runner_version}."
    elif effective_status == "online" and capabilities_match is False:
        status_summary = f"{status_summary[:-1]} but local capabilities do not match Longhouse."

    return RunnerHealthAssessment(
        effective_status=effective_status,
        status_reason=status_reason,
        status_summary=status_summary,
        heartbeat_interval_ms=heartbeat_interval_ms,
        stale_after_seconds=stale_after_seconds,
        last_seen_age_seconds=last_seen_age_seconds,
        is_stale=is_stale,
        is_connected=is_connected,
        install_mode=_install_mode(metadata),
        runner_version=runner_version,
        latest_runner_version=latest_runner_version,
        version_status=version_status,
        reported_capabilities=reported_capabilities,
        configured_capabilities=configured_capabilities,
        capabilities_match=capabilities_match,
    )


def build_runner_response(
    runner: Runner,
    *,
    now: datetime | None = None,
    latest_runner_version: str | None = None,
    is_connected: bool | None = None,
) -> RunnerResponse:
    """Serialize a runner with derived health fields."""
    assessment = assess_runner_health(
        runner,
        now=now,
        latest_runner_version=latest_runner_version,
        is_connected=is_connected,
    )
    labels = runner.labels if isinstance(runner.labels, dict) else None
    metadata = runner.runner_metadata if isinstance(runner.runner_metadata, dict) else None
    return RunnerResponse(
        id=runner.id,
        owner_id=runner.owner_id,
        name=runner.name,
        labels=labels,
        capabilities=assessment.configured_capabilities or ["exec.readonly"],
        status=assessment.effective_status,
        status_reason=assessment.status_reason,
        status_summary=assessment.status_summary,
        last_seen_at=runner.last_seen_at,
        last_seen_age_seconds=assessment.last_seen_age_seconds,
        heartbeat_interval_ms=assessment.heartbeat_interval_ms,
        stale_after_seconds=assessment.stale_after_seconds,
        runner_metadata=metadata,
        install_mode=assessment.install_mode,
        runner_version=assessment.runner_version,
        latest_runner_version=assessment.latest_runner_version,
        version_status=assessment.version_status,
        reported_capabilities=assessment.reported_capabilities or None,
        capabilities_match=assessment.capabilities_match,
        created_at=runner.created_at,
        updated_at=runner.updated_at,
    )
