"""Local archive backlog inspection and control helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_state_dir

HUGE_RANGE_BYTES = 100 * 1024 * 1024
DEFAULT_TRICKLE_TICK_BYTES = 512 * 1024 * 1024
DEFAULT_DRAIN_TICK_BYTES = 4 * 1024 * 1024 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def archive_control_path(base_dir: Path | None = None) -> Path:
    return get_agent_state_dir(base_dir) / "archive-repair-control.json"


def default_archive_backlog(*, source: str = "missing") -> dict[str, Any]:
    return {
        "source": source,
        "state": "idle",
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
        "providers": [],
        "size_buckets": {},
        "db_exists": False,
    }


def normalize_archive_backlog(raw: Mapping[str, Any] | None, *, source: str) -> dict[str, Any]:
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
            "providers": list(raw.get("providers") or []),
            "size_buckets": dict(raw.get("size_buckets") or {}),
            "db_exists": bool(raw.get("db_exists", True)),
        }
    )
    return result


def collect_archive_backlog(
    base_dir: Path | None = None,
    *,
    engine_status_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_from_status = engine_status_payload.get("archive_backlog") if isinstance(engine_status_payload, Mapping) else None
    if isinstance(raw_from_status, Mapping):
        return normalize_archive_backlog(raw_from_status, source="engine_status")

    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return default_archive_backlog(source="sqlite")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_spool_queue(conn):
            return default_archive_backlog(source="sqlite")
        return _collect_archive_backlog_from_conn(conn, source="sqlite")


def inspect_archive_backlog(base_dir: Path | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return []
    normalized_limit = max(1, min(int(limit), 200))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_spool_queue(conn):
            return []
        rows = conn.execute(
            """
            SELECT provider,
                   file_path,
                   COUNT(*) AS pending_ranges,
                   COUNT(DISTINCT session_id) AS pending_sessions,
                   COALESCE(SUM(
                       CASE WHEN end_offset > start_offset THEN end_offset - start_offset ELSE 0 END
                   ), 0) AS pending_bytes,
                   MIN(created_at) AS oldest_pending_at,
                   MAX(created_at) AS newest_pending_at,
                   MIN(next_retry_at) AS next_retry_at_min,
                   MAX(last_error) AS last_error
            FROM spool_queue
            WHERE status = 'pending'
            GROUP BY provider, file_path
            ORDER BY pending_bytes DESC, newest_pending_at DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def write_archive_control(
    base_dir: Path | None = None,
    *,
    mode: str,
    max_tick_bytes: int | None = None,
    include_huge: bool | None = None,
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    payload: dict[str, Any] = {
        "mode": normalized_mode,
        "updated_at": _utc_now_iso(),
    }
    if max_tick_bytes is not None:
        payload["max_tick_bytes"] = max(1, int(max_tick_bytes))
    if include_huge is not None:
        payload["include_huge"] = bool(include_huge)

    path = archive_control_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return {"path": str(path), **payload}


def dead_letter_archive_path(base_dir: Path | None = None, *, file_path: str, reason: str) -> int:
    normalized_path = str(file_path or "").strip()
    if not normalized_path:
        raise ValueError("file_path is required")
    normalized_reason = str(reason or "").strip() or "operator dead-lettered archive backlog path"
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return 0
    now = _utc_now_iso()
    with sqlite3.connect(db_path) as conn:
        if not _has_spool_queue(conn):
            return 0
        changed = conn.execute(
            """
            UPDATE spool_queue
            SET status = 'dead',
                retry_count = retry_count + 1,
                last_error = ?,
                next_retry_at = ?
            WHERE status = 'pending' AND file_path = ?
            """,
            (normalized_reason, now, normalized_path),
        ).rowcount
        conn.commit()
    return int(changed)


def ready_archive_backlog(base_dir: Path | None = None) -> int:
    """Make pending archive ranges eligible for immediate retry."""
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return 0
    now = _utc_now_iso()
    with sqlite3.connect(db_path) as conn:
        if not _has_spool_queue(conn):
            return 0
        changed = conn.execute(
            """
            UPDATE spool_queue
            SET next_retry_at = ?
            WHERE status = 'pending'
            """,
            (now,),
        ).rowcount
        conn.commit()
    return int(changed)


def _has_spool_queue(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_queue' LIMIT 1").fetchone()
    return row is not None


def parse_byte_budget(value: str | None) -> int | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    multipliers = {
        "b": 1,
        "kb": 1024,
        "k": 1024,
        "mb": 1024 * 1024,
        "m": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
        "g": 1024 * 1024 * 1024,
    }
    for suffix, multiplier in sorted(multipliers.items(), key=lambda item: -len(item[0])):
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)].strip()) * multiplier)
    return int(float(raw))


def _collect_archive_backlog_from_conn(conn: sqlite3.Connection, *, source: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    aggregate = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_ranges,
            COALESCE(SUM(
                CASE
                    WHEN status = 'pending'
                     AND (
                         next_retry_at <= ?
                         OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                     )
                    THEN 1
                    ELSE 0
                END
            ), 0) AS ready_ranges,
            COALESCE(SUM(
                CASE
                    WHEN status = 'pending'
                     AND NOT (
                         next_retry_at <= ?
                         OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                     )
                    THEN 1
                    ELSE 0
                END
            ), 0) AS deferred_ranges,
            COUNT(DISTINCT CASE WHEN status = 'pending' THEN provider || char(31) || file_path END) AS pending_paths,
            COUNT(DISTINCT CASE WHEN status = 'pending' THEN session_id END) AS pending_sessions,
            COALESCE(SUM(
                CASE
                    WHEN status = 'pending' AND end_offset > start_offset THEN end_offset - start_offset
                    ELSE 0
                END
            ), 0) AS pending_bytes,
            COALESCE(SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END), 0) AS dead_ranges,
            COALESCE(SUM(
                CASE WHEN status = 'dead' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END
            ), 0) AS dead_bytes,
            COALESCE(SUM(
                CASE WHEN status = 'pending' AND end_offset - start_offset >= ? THEN 1 ELSE 0 END
            ), 0) AS huge_pending_ranges,
            COALESCE(SUM(
                CASE
                    WHEN status = 'pending' AND end_offset - start_offset >= ? THEN end_offset - start_offset
                    ELSE 0
                END
            ), 0) AS huge_pending_bytes,
            MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest_pending_at,
            MAX(CASE WHEN status = 'pending' THEN created_at END) AS newest_pending_at,
            MIN(CASE WHEN status = 'pending' THEN next_retry_at END) AS next_retry_at_min,
            MAX(CASE WHEN status = 'pending' THEN next_retry_at END) AS next_retry_at_max,
            MIN(
                CASE
                    WHEN status = 'pending'
                     AND NOT (
                         next_retry_at <= ?
                         OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                     )
                    THEN next_retry_at
                END
            ) AS next_deferred_retry_at
        FROM spool_queue
        """,
        (now, now, HUGE_RANGE_BYTES, HUGE_RANGE_BYTES, now),
    ).fetchone()
    pending_ranges = _int(aggregate["pending_ranges"])
    dead_ranges = _int(aggregate["dead_ranges"])
    state = "dead_lettered" if dead_ranges else "pending" if pending_ranges else "idle"
    return {
        "source": source,
        "state": state,
        "mode": "drain" if pending_ranges else "idle",
        "pending_ranges": pending_ranges,
        "ready_ranges": _int(aggregate["ready_ranges"]),
        "deferred_ranges": _int(aggregate["deferred_ranges"]),
        "pending_paths": _int(aggregate["pending_paths"]),
        "pending_sessions": _int(aggregate["pending_sessions"]),
        "pending_bytes": _int(aggregate["pending_bytes"]),
        "dead_ranges": dead_ranges,
        "dead_bytes": _int(aggregate["dead_bytes"]),
        "huge_pending_ranges": _int(aggregate["huge_pending_ranges"]),
        "huge_pending_bytes": _int(aggregate["huge_pending_bytes"]),
        "oldest_pending_at": aggregate["oldest_pending_at"],
        "newest_pending_at": aggregate["newest_pending_at"],
        "next_retry_at_min": aggregate["next_retry_at_min"],
        "next_retry_at_max": aggregate["next_retry_at_max"],
        "next_deferred_retry_at": aggregate["next_deferred_retry_at"],
        "providers": _provider_rows(conn),
        "size_buckets": _size_buckets(conn),
        "db_exists": True,
    }


def _provider_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT provider,
               COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_ranges,
               COUNT(DISTINCT CASE WHEN status = 'pending' THEN file_path END) AS pending_paths,
               COUNT(DISTINCT CASE WHEN status = 'pending' THEN session_id END) AS pending_sessions,
               COALESCE(SUM(
                   CASE
                       WHEN status = 'pending' AND end_offset > start_offset THEN end_offset - start_offset
                       ELSE 0
                   END
               ), 0) AS pending_bytes,
               COALESCE(SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END), 0) AS dead_ranges,
               COALESCE(SUM(
                   CASE WHEN status = 'dead' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END
               ), 0) AS dead_bytes
        FROM spool_queue
        GROUP BY provider
        ORDER BY pending_bytes DESC, provider ASC
        """
    ).fetchall()
    return [dict(row) for row in rows if _int(row["pending_ranges"]) or _int(row["dead_ranges"])]


def _size_buckets(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT
            CASE
                WHEN end_offset - start_offset < 1024 THEN 'tiny_lt_1kb'
                WHEN end_offset - start_offset < 1048576 THEN 'small_lt_1mb'
                WHEN end_offset - start_offset < 10485760 THEN 'medium_lt_10mb'
                WHEN end_offset - start_offset < 104857600 THEN 'large_lt_100mb'
                ELSE 'huge_gte_100mb'
            END AS bucket,
            COUNT(*) AS pending_ranges,
            COALESCE(SUM(
                CASE WHEN end_offset > start_offset THEN end_offset - start_offset ELSE 0 END
            ), 0) AS pending_bytes
        FROM spool_queue
        WHERE status = 'pending'
        GROUP BY bucket
        """
    ).fetchall()
    return {
        str(row["bucket"]): {
            "pending_ranges": _int(row["pending_ranges"]),
            "pending_bytes": _int(row["pending_bytes"]),
        }
        for row in rows
    }


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
