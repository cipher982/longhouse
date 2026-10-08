"""Local archive backlog inspection and control helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_state_dir
from zerg.services.longhouse_paths import get_agent_status_path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def archive_control_path(base_dir: Path | None = None) -> Path:
    return get_agent_state_dir(base_dir) / "archive-repair-control.json"


def default_archive_backlog(*, source: str = "missing") -> dict[str, Any]:
    return {
        "source": source,
        "state": "complete",
        "mode": "idle",
        "pending_ranges": 0,
        "ready_ranges": 0,
        "deferred_ranges": 0,
        "pending_paths": 0,
        "pending_sessions": 0,
        "pending_bytes": 0,
        "dead_ranges": 0,
        "dead_bytes": 0,
        "huge_pending_ranges": 0,
        "huge_pending_bytes": 0,
        "oldest_pending_at": None,
        "newest_pending_at": None,
        "next_retry_at_min": None,
        "next_retry_at_max": None,
        "next_deferred_retry_at": None,
        "pause_actor": None,
        "pause_reason": None,
        "pause_updated_at": None,
        "archive_bytes_per_sec": None,
        "archive_eta_seconds": None,
        "providers": [],
        "size_buckets": {},
        "shipper": {},
        "db_exists": False,
    }


def normalize_archive_backlog(
    raw: Mapping[str, Any] | None,
    *,
    source: str,
    engine_status_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return default_archive_backlog(source=source)
    result = default_archive_backlog(source=source)
    result.update(
        {
            "state": str(raw.get("state") or result["state"]),
            "mode": str(raw.get("mode") or result["mode"]),
            "pending_ranges": _int(raw.get("pending_ranges")),
            "ready_ranges": _int(raw.get("ready_ranges")),
            "deferred_ranges": _int(raw.get("deferred_ranges")),
            "pending_paths": _int(raw.get("pending_paths")),
            "pending_sessions": _int(raw.get("pending_sessions")),
            "pending_bytes": _int(raw.get("pending_bytes")),
            "dead_ranges": _int(raw.get("dead_ranges")),
            "dead_bytes": _int(raw.get("dead_bytes")),
            "huge_pending_ranges": _int(raw.get("huge_pending_ranges")),
            "huge_pending_bytes": _int(raw.get("huge_pending_bytes")),
            "oldest_pending_at": _optional_str(raw.get("oldest_pending_at")),
            "newest_pending_at": _optional_str(raw.get("newest_pending_at")),
            "next_retry_at_min": _optional_str(raw.get("next_retry_at_min")),
            "next_retry_at_max": _optional_str(raw.get("next_retry_at_max")),
            "next_deferred_retry_at": _optional_str(raw.get("next_deferred_retry_at")),
            "pause_actor": _optional_str(raw.get("pause_actor")),
            "pause_reason": _optional_str(raw.get("pause_reason")),
            "pause_updated_at": _optional_str(raw.get("pause_updated_at")),
            "providers": list(raw.get("providers") or []),
            "size_buckets": dict(raw.get("size_buckets") or {}),
            "db_exists": bool(raw.get("db_exists", True)),
        }
    )
    _attach_shipper_diagnostics(result, engine_status_payload)
    _attach_storage_v2_outbox(result, engine_status_payload)
    _attach_archive_progress(result, engine_status_payload)
    return result


def collect_archive_backlog(
    base_dir: Path | None = None,
    *,
    engine_status_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if engine_status_payload is None:
        engine_status_payload = _read_engine_status_payload(base_dir)

    raw_from_status = engine_status_payload.get("archive_backlog") if isinstance(engine_status_payload, Mapping) else None
    if isinstance(raw_from_status, Mapping):
        return normalize_archive_backlog(
            raw_from_status,
            source="engine_status",
            engine_status_payload=engine_status_payload,
        )

    # The engine reports this block in its status file; with no status there is
    # no separate local store to read (the v1 spool is gone).
    result = default_archive_backlog(source="missing")
    _attach_shipper_diagnostics(result, engine_status_payload)
    _attach_storage_v2_outbox(result, engine_status_payload)
    return result


def write_archive_control(
    base_dir: Path | None = None,
    *,
    mode: str,
    lease_minutes: int = 60,
    actor: str = "cli",
    reason: str | None = None,
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "mode": normalized_mode,
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "actor": str(actor or "cli").strip() or "cli",
    }
    if normalized_mode != "paused":
        payload["expires_at"] = (now + timedelta(minutes=max(1, lease_minutes))).isoformat().replace("+00:00", "Z")
    if reason and reason.strip():
        payload["reason"] = reason.strip()

    path = archive_control_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return {"path": str(path), **payload}


def _read_engine_status_payload(base_dir: Path | None = None) -> dict[str, Any] | None:
    path = get_agent_status_path(base_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _attach_shipper_diagnostics(result: dict[str, Any], engine_status_payload: Mapping[str, Any] | None) -> None:
    """Attach live scheduler/limiter evidence to an archive backlog summary."""
    if not isinstance(engine_status_payload, Mapping):
        return
    shipper: dict[str, Any] = {}
    for key in (
        "adaptive_backlog_limiter",
        "ship_scheduler",
        "ship_lanes",
        "events_per_sec_ewma_10s",
        "bytes_per_sec_ewma_10s",
        "last_ship_at",
        "last_ship_attempt_at",
        "last_ship_result",
        "last_ship_latency_ms",
    ):
        value = engine_status_payload.get(key)
        if value is not None:
            shipper[key] = value
    if shipper:
        result["shipper"] = shipper


def _attach_storage_v2_outbox(result: dict[str, Any], engine_status_payload: Mapping[str, Any] | None) -> None:
    """Add the engine's storage-v2 outbox, the backlog that actually ships now.

    The `archive_backlog` range counters described the retired v1 spool and are
    always zero; the pending storage-v2 envelopes are the work left to send.
    """
    raw = engine_status_payload.get("storage_v2_outbox") if isinstance(engine_status_payload, Mapping) else None
    if not isinstance(raw, Mapping):
        return
    result["storage_v2_outbox"] = {
        "pending_count": _int(raw.get("pending_count")),
        "pending_bytes": _int(raw.get("pending_bytes")),
        "oldest_pending_at": _optional_str(raw.get("oldest_pending_at")),
        "blocked_source_count": _int(raw.get("blocked_source_count")),
    }


def pending_backlog_bytes(result: Mapping[str, Any]) -> int:
    """Bytes still to ship: the storage-v2 outbox when reported, else the range counters."""
    outbox = result.get("storage_v2_outbox")
    if isinstance(outbox, Mapping):
        return _int(outbox.get("pending_bytes"))
    return _int(result.get("pending_bytes"))


def _attach_archive_progress(result: dict[str, Any], engine_status_payload: Mapping[str, Any] | None) -> None:
    """Add the only honest ETA: pending bytes divided by acknowledged archive rate."""
    if not isinstance(engine_status_payload, Mapping):
        return
    lanes = engine_status_payload.get("ship_lanes")
    archive_lane = lanes.get("archive") if isinstance(lanes, Mapping) else None
    if not isinstance(archive_lane, Mapping):
        return
    try:
        bytes_per_second = float(archive_lane.get("bytes_per_sec_ewma_10s") or 0)
    except (TypeError, ValueError):
        return
    if bytes_per_second <= 0:
        return
    result["archive_bytes_per_sec"] = bytes_per_second
    pending_bytes = pending_backlog_bytes(result)
    if pending_bytes > 0:
        result["archive_eta_seconds"] = int(pending_bytes / bytes_per_second)


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized in {"paused", "pause"}:
        return "paused"
    if normalized in {"trickle", "resume"}:
        return "trickle"
    if normalized in {"drain", "drain-now"}:
        return "drain"
    raise ValueError("mode must be paused, trickle, or drain")


def _int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_str(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None
