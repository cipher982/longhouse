"""Local Longhouse engine health snapshot helpers.

This module is the canonical local-health classifier for the CLI and future
desktop surfaces. It combines raw local probes with a small derived state model
without hiding the underlying signals.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.shipper.service import get_service_info

SCHEMA_VERSION = 1
ENGINE_FRESH_SECONDS = 30
ENGINE_STALE_SECONDS = 120
OUTBOX_DEGRADED_AGE_SECONDS = 15
OUTBOX_BROKEN_AGE_SECONDS = 120
DEGRADED_BACKLOG_COUNT = 1
BROKEN_BACKLOG_COUNT = 25
DISK_DEGRADED_BYTES = 5 * 1024 * 1024 * 1024
DISK_BROKEN_BYTES = 1 * 1024 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_rfc3339(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _coerce_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser()
    return Path.home() / ".claude"


def _collect_engine_status(claude_dir: Path, *, now: datetime) -> dict[str, Any]:
    status_path = claude_dir / "engine-status.json"
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

    return {
        "path": str(status_path),
        "exists": True,
        "fresh": age_seconds <= ENGINE_FRESH_SECONDS,
        "age_seconds": age_seconds,
        "payload": payload,
        "error": None,
    }


def _collect_outbox(claude_dir: Path, *, now: datetime) -> dict[str, Any]:
    outbox_dir = claude_dir / "outbox"
    if not outbox_dir.exists():
        return {
            "path": str(outbox_dir),
            "file_count": 0,
            "oldest_age_seconds": None,
        }

    files = [path for path in outbox_dir.iterdir() if path.is_file() and path.name.endswith(".json") and not path.name.startswith(".")]
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


def _collect_service() -> dict[str, Any]:
    return get_service_info()


def _with_action(actions: list[str], text: str) -> None:
    if text not in actions:
        actions.append(text)


def _classify_health(
    *,
    service: dict[str, Any],
    engine_status: dict[str, Any],
    outbox: dict[str, Any],
) -> tuple[str, str, str, list[str], list[str]]:
    reasons: list[str] = []
    actions: list[str] = []

    service_status = str(service.get("status") or "not-installed")
    payload = engine_status.get("payload") or {}
    engine_exists = bool(engine_status.get("exists"))
    engine_error = engine_status.get("error")
    engine_age = engine_status.get("age_seconds")
    spool_pending = int(payload.get("spool_pending_count") or 0)
    spool_dead = int(payload.get("spool_dead_count") or 0)
    ship_failures = int(payload.get("consecutive_ship_failures") or 0)
    parse_errors = int(payload.get("parse_error_count_1h") or 0)
    is_offline = bool(payload.get("is_offline") or False)
    disk_free_bytes = payload.get("disk_free_bytes")
    outbox_count = int(outbox.get("file_count") or 0)
    outbox_oldest = outbox.get("oldest_age_seconds")

    if service_status == "not-installed":
        reasons.append("service_not_installed")
        _with_action(actions, "Run: longhouse connect --install")
    elif service_status == "stopped":
        reasons.append("service_stopped")
        _with_action(actions, "Run: longhouse connect --install")

    if engine_error:
        reasons.append("engine_status_unreadable")
        _with_action(actions, "Inspect: ~/.claude/engine-status.json")
    elif not engine_exists:
        reasons.append("engine_status_missing")
        if service_status == "running":
            _with_action(actions, "Wait for the first local status update or inspect engine logs")
        else:
            _with_action(actions, "Run: longhouse connect --install")
    elif engine_age is not None and engine_age > ENGINE_STALE_SECONDS:
        reasons.append("engine_status_stale")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")

    if is_offline:
        reasons.append("engine_offline")
        _with_action(actions, "Verify network reachability to your Longhouse URL")

    if ship_failures > 0:
        reasons.append("ship_failures")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")

    if parse_errors > 0:
        reasons.append("parse_errors")
        _with_action(actions, "Inspect recent dead letters and parser errors")

    if spool_pending >= DEGRADED_BACKLOG_COUNT:
        reasons.append("spool_pending")

    if spool_dead > 0:
        reasons.append("spool_dead")
        _with_action(actions, "Repair dead letters before trusting continuity")

    if outbox_count >= DEGRADED_BACKLOG_COUNT:
        reasons.append("outbox_backlog")
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
        reasons.append("outbox_stuck")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")

    if isinstance(disk_free_bytes, int):
        if disk_free_bytes < DISK_BROKEN_BYTES:
            reasons.append("disk_critically_low")
            _with_action(actions, "Free local disk space before continuing to rely on shipping")
        elif disk_free_bytes < DISK_DEGRADED_BYTES:
            reasons.append("disk_low")
            _with_action(actions, "Consider freeing disk space soon")

    if service_status == "not-installed" and not engine_exists and outbox_count == 0 and spool_pending == 0:
        return (
            "uninstalled",
            "gray",
            "Longhouse local shipping is not installed",
            reasons,
            actions,
        )

    broken = False
    degraded = False

    if service_status == "stopped":
        broken = True
    if engine_error:
        broken = True
    if spool_dead > 0:
        broken = True
    if isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_BROKEN_BYTES:
        broken = True
    if outbox_count >= BROKEN_BACKLOG_COUNT:
        broken = True
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_BROKEN_AGE_SECONDS:
        broken = True
    if spool_pending >= BROKEN_BACKLOG_COUNT:
        broken = True
    if service_status != "running" and (outbox_count > 0 or spool_pending > 0):
        broken = True
    if engine_exists and engine_age is not None and engine_age > ENGINE_STALE_SECONDS and (outbox_count > 0 or spool_pending > 0):
        broken = True

    if not broken:
        if service_status != "running":
            degraded = True
        if not engine_exists:
            degraded = True
        if engine_age is not None and engine_age > ENGINE_FRESH_SECONDS:
            degraded = True
        if is_offline or ship_failures > 0 or parse_errors > 0:
            degraded = True
        if spool_pending >= DEGRADED_BACKLOG_COUNT:
            degraded = True
        if outbox_count >= DEGRADED_BACKLOG_COUNT and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
            degraded = True
        if isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_DEGRADED_BYTES:
            degraded = True

    if broken:
        headline = "Longhouse shipping needs repair"
        if "service_stopped" in reasons:
            headline = "Longhouse engine service is stopped"
        elif "spool_dead" in reasons:
            headline = "Longhouse has dead-lettered data to repair"
        elif "engine_status_stale" in reasons:
            headline = "Longhouse local status is stale while work is pending"
        return ("broken", "red", headline, reasons, actions)

    if degraded:
        headline = "Longhouse shipping is degraded"
        if "engine_offline" in reasons:
            headline = "Longhouse is retrying while offline"
        elif "engine_status_missing" in reasons and service_status == "running":
            headline = "Longhouse is waiting for its first local status update"
        elif "engine_status_stale" in reasons:
            headline = "Longhouse local status is aging"
        return ("degraded", "yellow", headline, reasons, actions)

    return ("healthy", "green", "Longhouse shipping healthy", reasons, actions)


def collect_local_health(claude_dir: str | Path | None = None) -> dict[str, Any]:
    now = _utc_now()
    resolved_claude_dir = _coerce_path(claude_dir)
    service = _collect_service()
    engine_status = _collect_engine_status(resolved_claude_dir, now=now)
    outbox = _collect_outbox(resolved_claude_dir, now=now)
    health_state, severity, headline, reasons, suggested_actions = _classify_health(
        service=service,
        engine_status=engine_status,
        outbox=outbox,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": _to_rfc3339(now),
        "health_state": health_state,
        "severity": severity,
        "headline": headline,
        "reasons": reasons,
        "suggested_actions": suggested_actions,
        "service": service,
        "engine_status": engine_status,
        "outbox": outbox,
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
