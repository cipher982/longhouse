"""Durable, cheap status evidence for the process-isolated archive worker."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.config import _resolve_live_database_url
from zerg.config import _sqlite_file_path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive_worker_enabled() -> bool:
    return os.getenv("TESTING", "").strip().lower() not in {"1", "true", "yes", "on"}


def archive_worker_status_path() -> Path | None:
    path = _sqlite_file_path(_resolve_live_database_url(os.getenv("DATABASE_URL", "")))
    if path is None:
        return None
    return path.with_name("archive-worker-status.json")


def archive_worker_lock_path() -> Path | None:
    status_path = archive_worker_status_path()
    if status_path is None:
        return None
    return status_path.with_name("archive-worker.lock")


def write_archive_worker_status(payload: dict[str, Any]) -> None:
    path = archive_worker_status_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "observed_at": _utc_now_iso(),
        **payload,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def read_archive_worker_status() -> dict[str, Any]:
    if not archive_worker_enabled():
        return {"status": "disabled", "enabled": False}
    path = archive_worker_status_path()
    if path is None:
        return {"status": "unknown", "enabled": True, "reason": "status_path_unavailable"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "unknown", "enabled": True, "reason": "status_missing", "path": str(path)}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unknown",
            "enabled": True,
            "reason": "status_unreadable",
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(path),
        }
    payload["enabled"] = True
    payload["path"] = str(path)
    observed_at = payload.get("observed_at")
    if payload.get("status") == "running" and isinstance(observed_at, str):
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            age_seconds = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
            payload["age_seconds"] = round(age_seconds, 3)
            stale_after = 10.0
            if age_seconds > stale_after:
                payload["status"] = "degraded"
                payload["reason"] = "status_stale"
        except ValueError:
            payload["status"] = "unknown"
            payload["reason"] = "status_timestamp_invalid"
    active_started_at = payload.get("active_started_at_unix")
    if payload.get("status") == "running" and isinstance(active_started_at, int | float):
        active_age_seconds = max(0.0, time.time() - float(active_started_at))
        payload["active_age_seconds"] = round(active_age_seconds, 3)
        operation_stale_after = 60.0
        if active_age_seconds > operation_stale_after:
            payload["status"] = "degraded"
            payload["reason"] = "operation_stalled"
    return payload
