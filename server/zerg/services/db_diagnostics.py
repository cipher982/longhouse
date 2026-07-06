"""Lightweight SQLite diagnostics for operator and watchman surfaces."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

importlib.import_module("zerg.bootstrap_sqlite")
sqlite3 = importlib.import_module("sqlite3")

_WATCHED_TABLES = ("sessions", "events", "source_lines", "session_observations")
_MAX_BACKUP_SCAN_ENTRIES = 5000
_TABLE_BYTES_CACHE_SCHEMA_VERSION = 1
_DEFAULT_TABLE_BYTES_CACHE_MAX_BYTES = 10 * 1024 * 1024
_LARGE_DB_SAMPLING_SUGGESTION_BYTES = 1024 * 1024 * 1024
_TABLE_BYTES_SQL = """
SELECT
    s.name AS object_name,
    COALESCE(m.type, 'internal') AS object_type,
    CASE
        WHEN m.type = 'index' AND m.tbl_name IS NOT NULL THEN m.tbl_name
        ELSE s.name
    END AS table_name,
    SUM(s.pgsize) AS bytes,
    COUNT(*) AS pages
FROM dbstat AS s
LEFT JOIN sqlite_master AS m ON m.name = s.name
GROUP BY s.name, m.type, m.tbl_name
ORDER BY bytes DESC, object_name ASC
"""


class SQLiteTableBytesTimeout(Exception):
    """Raised when a deadline interrupts a SQLite table-byte scan."""


def sqlite_db_paths(database_url: str) -> tuple[Path, Path] | None:
    try:
        parsed = make_url(database_url)
    except Exception:
        return None
    if not parsed.drivername.startswith("sqlite"):
        return None
    db_raw = parsed.database or ""
    if not db_raw or db_raw == ":memory:":
        return None
    db_path = Path(db_raw).expanduser()
    return db_path, Path(f"{db_path}-wal")


def sqlite_table_bytes_cache_path(database_url: str, output_path: Path | None = None) -> Path | None:
    if output_path is not None:
        return output_path.expanduser()
    paths = sqlite_db_paths(database_url)
    if paths is None:
        return None
    db_path, _wal_path = paths
    return Path(f"{db_path}.table-bytes.json")


def _directory_file_stats(path: Path, *, max_entries: int = _MAX_BACKUP_SCAN_ENTRIES) -> dict[str, int | bool]:
    if not path.exists():
        return {"bytes": 0, "file_count": 0, "truncated": False}

    total = 0
    file_count = 0
    entries_seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        return {"bytes": total, "file_count": file_count, "truncated": True}
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            file_count += 1
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return {"bytes": total, "file_count": file_count, "truncated": False}


def _existing_disk_anchor(path: Path) -> Path:
    current = path if path.exists() else path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _disk_usage_payload(path: Path) -> dict[str, int | float | str | None]:
    try:
        usage = shutil.disk_usage(_existing_disk_anchor(path))
    except OSError:
        return {
            "disk_total_bytes": None,
            "disk_used_bytes": None,
            "disk_free_bytes": None,
            "disk_free_ratio": None,
        }
    free_ratio = usage.free / usage.total if usage.total else None
    return {
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "disk_free_ratio": free_ratio,
    }


def _pragma_int(db: Session | Connection, name: str) -> int | None:
    try:
        row = db.execute(text(f"PRAGMA {name}")).fetchone()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _raw_pragma_int(conn: sqlite3.Connection, name: str) -> int | None:
    try:
        row = conn.execute(f"PRAGMA {name}").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _table_exists(db: Session | Connection, table_name: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
            LIMIT 1
            """
        ),
        {"table_name": table_name},
    ).fetchone()
    return row is not None


def _index_exists(db: Session | Connection, index_name: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index' AND name = :index_name
            LIMIT 1
            """
        ),
        {"index_name": index_name},
    ).fetchone()
    return row is not None


def _table_columns(db: Session | Connection, table_name: str) -> set[str]:
    if not _table_exists(db, table_name):
        return set()
    rows = db.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {str(row[1]) for row in rows}


def _sqlite_stat1_estimates(db: Session | Connection) -> dict[str, int]:
    if not _table_exists(db, "sqlite_stat1"):
        return {}
    rows = db.execute(
        text(
            """
            SELECT tbl, stat
            FROM sqlite_stat1
            WHERE tbl IN ('sessions', 'events', 'source_lines', 'session_observations')
            """
        )
    ).fetchall()
    estimates: dict[str, int] = {}
    for table_name, stat in rows:
        first_token = str(stat or "").split(" ", 1)[0]
        try:
            estimates[str(table_name)] = int(first_token)
        except ValueError:
            continue
    return estimates


def collect_sqlite_schema_stats(db: Session | Connection) -> dict[str, Any]:
    tables = {
        table_name: {
            "exists": _table_exists(db, table_name),
            "columns": sorted(_table_columns(db, table_name)),
        }
        for table_name in _WATCHED_TABLES
    }
    return {
        "schema_version": _pragma_int(db, "schema_version"),
        "user_version": _pragma_int(db, "user_version"),
        "sqlite_stat1_exists": _table_exists(db, "sqlite_stat1"),
        "sqlite_stat1_estimated_rows": _sqlite_stat1_estimates(db),
        "raw_json_pending_indexes": {
            "events": _index_exists(db, "ix_events_raw_json_pending"),
            "source_lines": _index_exists(db, "ix_source_lines_raw_json_pending"),
        },
        "tables": tables,
    }


def _count_where(db: Session | Connection, table_name: str, predicate: str) -> int | None:
    if not _table_exists(db, table_name):
        return None
    value = db.execute(text(f"SELECT COUNT(*) FROM {table_name} WHERE {predicate}")).scalar()
    return int(value or 0)


def collect_sqlite_deep_counts(
    db: Session | Connection,
    *,
    include_identity_counts: bool = False,
) -> dict[str, int | bool | None]:
    """Return explicit potentially expensive counts for operator-invoked diagnostics."""
    events_columns = _table_columns(db, "events")
    source_columns = _table_columns(db, "source_lines")
    observation_columns = _table_columns(db, "session_observations")

    counts: dict[str, int | bool | None] = {
        "events_raw_json_pending": _count_where(db, "events", "raw_json_codec = 0 AND raw_json IS NOT NULL")
        if {"raw_json", "raw_json_codec"} <= events_columns and _index_exists(db, "ix_events_raw_json_pending")
        else None,
        "source_lines_raw_json_pending": _count_where(db, "source_lines", "raw_json_codec = 0")
        if "raw_json_codec" in source_columns and _index_exists(db, "ix_source_lines_raw_json_pending")
        else None,
        "identity_counts_skipped": not include_identity_counts,
        "events_thread_id_null": None,
        "source_lines_thread_id_null": None,
        "session_observations_thread_id_null": None,
    }
    if include_identity_counts:
        events_thread_id_null = None
        if "thread_id" in events_columns:
            events_thread_id_null = _count_where(db, "events", "thread_id IS NULL")
        counts.update(
            {
                "events_thread_id_null": events_thread_id_null,
                "source_lines_thread_id_null": _count_where(db, "source_lines", "thread_id IS NULL")
                if "thread_id" in source_columns
                else None,
                "session_observations_thread_id_null": _count_where(db, "session_observations", "thread_id IS NULL")
                if "thread_id" in observation_columns
                else None,
            }
        )
    return counts


def collect_sqlite_table_bytes(db: Session | Connection) -> dict[str, Any]:
    """Return physical SQLite page usage by table and index using dbstat.

    dbstat walks the database pages, so callers should keep this behind an
    explicit operator flag instead of collecting it on every health check.
    """
    try:
        # Autoindexes without sqlite_master rows stay under their dbstat object
        # name. This keeps the collector faithful to SQLite's physical btrees.
        rows = db.execute(text(_TABLE_BYTES_SQL)).mappings()
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }

    return _fold_table_bytes_rows(rows)


def _fold_table_bytes_rows(rows) -> dict[str, Any]:
    tables: dict[str, dict[str, int]] = {}
    total_bytes = 0
    total_pages = 0
    for row in rows:
        object_type = str(row["object_type"])
        table_name = str(row["table_name"])
        object_bytes = int(row["bytes"] or 0)
        object_pages = int(row["pages"] or 0)
        total_bytes += object_bytes
        total_pages += object_pages

        table_entry = tables.setdefault(
            table_name,
            {
                "bytes": 0,
                "pages": 0,
                "table_bytes": 0,
                "index_bytes": 0,
                "object_count": 0,
                "index_count": 0,
            },
        )
        table_entry["bytes"] += object_bytes
        table_entry["pages"] += object_pages
        table_entry["object_count"] += 1
        if object_type == "index":
            table_entry["index_bytes"] += object_bytes
            table_entry["index_count"] += 1
        else:
            table_entry["table_bytes"] += object_bytes

    sorted_tables = dict(sorted(tables.items(), key=lambda item: (-item[1]["bytes"], item[0])))
    return {
        "available": True,
        "error": None,
        "total_bytes": total_bytes,
        "total_pages": total_pages,
        "tables": sorted_tables,
    }


def collect_sqlite_table_bytes_with_deadline(
    conn: sqlite3.Connection,
    *,
    deadline_monotonic: float | None,
    progress_opcodes: int = 100_000,
) -> dict[str, Any]:
    """Return dbstat table bytes through a raw SQLite connection with a deadline."""
    timed_out = False

    def _progress() -> int:
        nonlocal timed_out
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            timed_out = True
            return 1
        return 0

    if deadline_monotonic is not None:
        if not hasattr(conn, "set_progress_handler"):
            return {
                "available": False,
                "error": "sqlite set_progress_handler unavailable",
                "total_bytes": None,
                "total_pages": None,
                "tables": {},
            }
        conn.set_progress_handler(_progress, max(1, progress_opcodes))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_TABLE_BYTES_SQL).fetchall()
    except sqlite3.OperationalError as exc:
        if timed_out or "interrupted" in str(exc).lower():
            raise SQLiteTableBytesTimeout(str(exc)) from exc
        return {
            "available": False,
            "error": str(exc),
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "error": str(exc),
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }
    finally:
        if deadline_monotonic is not None and hasattr(conn, "set_progress_handler"):
            conn.set_progress_handler(None, 0)

    return _fold_table_bytes_rows(rows)


def _raw_sample_metadata(database_url: str, conn: sqlite3.Connection | None = None) -> dict[str, int | None]:
    stats = collect_sqlite_db_stats(database_url)
    page_size = _raw_pragma_int(conn, "page_size") if conn is not None else None
    page_count = _raw_pragma_int(conn, "page_count") if conn is not None else None
    freelist_count = _raw_pragma_int(conn, "freelist_count") if conn is not None else None
    return {
        "db_bytes_at_sample": int(stats["db_bytes"]) if stats and stats.get("db_bytes") is not None else None,
        "wal_bytes_at_sample": int(stats["wal_bytes"]) if stats and stats.get("wal_bytes") is not None else None,
        "page_size_at_sample": page_size,
        "page_count_at_sample": page_count,
        "freelist_count_at_sample": freelist_count,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)
    if hasattr(os, "O_DIRECTORY"):
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def write_sqlite_table_bytes_cache(payload: dict[str, Any], output_path: Path) -> None:
    _atomic_write_json(output_path, payload)


def _sample_payload_base(
    *,
    database_url: str,
    db_path: Path | None,
    started_at: str,
    started_monotonic: float,
    timeout_seconds: int,
    status: str,
    error: str | None,
    table_bytes: dict[str, Any] | None = None,
    metadata: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": _TABLE_BYTES_CACHE_SCHEMA_VERSION,
        "status": status,
        "database_url": database_url,
        "db_path": str(db_path) if db_path is not None else None,
        "started_at": started_at,
        "completed_at": _utc_now_iso(),
        "elapsed_ms": round((time.monotonic() - started_monotonic) * 1000, 1),
        "timeout_seconds": timeout_seconds,
        "error": error,
        "table_bytes": table_bytes
        if table_bytes is not None
        else {
            "available": False,
            "error": error,
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        },
    }
    payload.update(metadata or {})
    return payload


def sample_sqlite_table_bytes_to_cache(
    database_url: str,
    *,
    output_path: Path | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run an explicit table-byte sample and atomically persist its result."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    cache_path = sqlite_table_bytes_cache_path(database_url, output_path)
    if cache_path is None:
        raise ValueError("table-byte sampling only supports file-backed SQLite databases")

    paths = sqlite_db_paths(database_url)
    db_path = paths[0] if paths is not None else None
    started_at = _utc_now_iso()
    started_monotonic = time.monotonic()
    metadata = _raw_sample_metadata(database_url)

    if db_path is None or not db_path.exists():
        payload = _sample_payload_base(
            database_url=database_url,
            db_path=db_path,
            started_at=started_at,
            started_monotonic=started_monotonic,
            timeout_seconds=timeout_seconds,
            status="error",
            error="database file not found",
            metadata=metadata,
        )
        write_sqlite_table_bytes_cache(payload, cache_path)
        return payload

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        metadata = _raw_sample_metadata(database_url, conn)
        table_bytes = collect_sqlite_table_bytes_with_deadline(
            conn,
            deadline_monotonic=started_monotonic + timeout_seconds,
        )
        status = "ok" if table_bytes.get("available") else "unavailable"
        error = None if status == "ok" else str(table_bytes.get("error") or "dbstat unavailable")
    except SQLiteTableBytesTimeout as exc:
        table_bytes = {
            "available": False,
            "error": str(exc) or "table-byte sample timed out",
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }
        status = "timeout"
        error = "table-byte sample timed out"
    except sqlite3.Error as exc:
        table_bytes = {
            "available": False,
            "error": str(exc),
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }
        status = "error"
        error = str(exc)
    finally:
        if conn is not None:
            conn.close()

    payload = _sample_payload_base(
        database_url=database_url,
        db_path=db_path,
        started_at=started_at,
        started_monotonic=started_monotonic,
        timeout_seconds=timeout_seconds,
        status=status,
        error=error,
        table_bytes=table_bytes,
        metadata=metadata,
    )
    write_sqlite_table_bytes_cache(payload, cache_path)
    return payload


def _parse_utc_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _top_table_bytes(table_bytes: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    tables = table_bytes.get("tables")
    if not isinstance(tables, dict):
        return []
    rows: list[dict[str, Any]] = []
    for table_name, payload in tables.items():
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "table": str(table_name),
                "bytes": int(payload.get("bytes") or 0),
                "table_bytes": int(payload.get("table_bytes") or 0),
                "index_bytes": int(payload.get("index_bytes") or 0),
                "pages": int(payload.get("pages") or 0),
            }
        )
    return sorted(rows, key=lambda row: (-row["bytes"], row["table"]))[:limit]


def load_sqlite_table_bytes_cache(
    database_url: str,
    *,
    max_age_seconds: int = 86400,
    output_path: Path | None = None,
    current_stats: dict[str, Any] | None = None,
    include_table_bytes: bool = False,
    max_cache_bytes: int = _DEFAULT_TABLE_BYTES_CACHE_MAX_BYTES,
) -> dict[str, Any]:
    """Return a cheap, doctor-safe table-byte cache summary."""
    cache_path = sqlite_table_bytes_cache_path(database_url, output_path)
    summary: dict[str, Any] = {
        "path": str(cache_path) if cache_path is not None else None,
        "exists": False,
        "status": "unsupported",
        "fresh": False,
        "age_seconds": None,
        "max_age_seconds": max_age_seconds,
        "started_at": None,
        "completed_at": None,
        "elapsed_ms": None,
        "db_bytes_at_sample": None,
        "db_bytes_now": current_stats.get("db_bytes") if current_stats else None,
        "db_bytes_delta": None,
        "top_tables": [],
        "suggested_command": None,
        "error": None,
    }
    if cache_path is None:
        summary["error"] = "table-byte cache only supports file-backed SQLite databases"
        return summary
    if not cache_path.exists():
        summary["status"] = "missing"
        _maybe_add_sampling_suggestion(summary, current_stats=current_stats)
        return summary

    summary["exists"] = True
    try:
        cache_bytes = cache_path.stat().st_size
    except OSError as exc:
        summary["status"] = "error"
        summary["error"] = str(exc)
        return summary
    if cache_bytes > max_cache_bytes:
        summary["status"] = "cache_too_large"
        summary["error"] = f"cache file exceeds {max_cache_bytes} bytes"
        return summary

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary["status"] = "corrupt"
        summary["error"] = str(exc)
        return summary
    if not isinstance(payload, dict):
        summary["status"] = "corrupt"
        summary["error"] = "cache payload is not an object"
        return summary
    if payload.get("schema_version") != _TABLE_BYTES_CACHE_SCHEMA_VERSION:
        summary["status"] = "schema_version_unsupported"
        summary["error"] = f"unsupported schema_version={payload.get('schema_version')!r}"
        return summary

    status = str(payload.get("status") or "unknown")
    completed_at = payload.get("completed_at")
    completed_dt = _parse_utc_iso(completed_at)
    age_seconds = None
    if completed_dt is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - completed_dt).total_seconds()))
    db_bytes_at_sample = payload.get("db_bytes_at_sample")
    db_bytes_now = current_stats.get("db_bytes") if current_stats else None
    db_bytes_delta = None
    if db_bytes_now is not None and db_bytes_at_sample is not None:
        db_bytes_delta = int(db_bytes_now) - int(db_bytes_at_sample)
    table_bytes = payload.get("table_bytes") if isinstance(payload.get("table_bytes"), dict) else {}
    payload_error = payload.get("error")
    cache_error = None
    if payload_error is not None:
        cache_error = payload_error
    elif isinstance(table_bytes, dict):
        cache_error = table_bytes.get("error")
    summary.update(
        {
            "status": status,
            "fresh": bool(status == "ok" and age_seconds is not None and age_seconds <= max_age_seconds),
            "age_seconds": age_seconds,
            "started_at": payload.get("started_at"),
            "completed_at": completed_at,
            "elapsed_ms": payload.get("elapsed_ms"),
            "db_bytes_at_sample": db_bytes_at_sample,
            "db_bytes_now": db_bytes_now,
            "db_bytes_delta": db_bytes_delta,
            "top_tables": _top_table_bytes(table_bytes),
            "error": cache_error,
        }
    )
    if include_table_bytes:
        summary["table_bytes"] = table_bytes
    if not summary["fresh"]:
        _maybe_add_sampling_suggestion(summary, current_stats=current_stats)
    return summary


def _maybe_add_sampling_suggestion(summary: dict[str, Any], *, current_stats: dict[str, Any] | None) -> None:
    db_bytes = current_stats.get("db_bytes") if current_stats else None
    if db_bytes is not None and int(db_bytes) >= _LARGE_DB_SAMPLING_SUGGESTION_BYTES:
        summary["suggested_command"] = "longhouse db sample-table-bytes"


def collect_sqlite_db_stats(
    database_url: str,
    *,
    db: Session | Connection | None = None,
) -> dict[str, Any] | None:
    paths = sqlite_db_paths(database_url)
    if paths is None:
        return None

    db_path, wal_path = paths
    db_exists = db_path.exists()
    db_bytes = db_path.stat().st_size if db_exists else None
    backup_dir = db_path.parent / "backups"
    backup_stats = _directory_file_stats(backup_dir)
    payload: dict[str, Any] = {
        "database_url": database_url,
        "db_path": str(db_path),
        "db_exists": db_exists,
        "db_bytes": db_bytes,
        "wal_path": str(wal_path),
        "wal_exists": wal_path.exists(),
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "backup_dir": str(backup_dir),
        "backup_dir_exists": backup_dir.exists(),
        "backup_bytes": backup_stats["bytes"],
        "backup_file_count": backup_stats["file_count"],
        "backup_scan_truncated": backup_stats["truncated"],
        "backup_scan_max_entries": _MAX_BACKUP_SCAN_ENTRIES,
    }
    payload.update(_disk_usage_payload(db_path))

    disk_total = payload.get("disk_total_bytes")
    disk_free = payload.get("disk_free_bytes")
    payload["db_bytes_ratio_of_disk"] = db_bytes / disk_total if db_bytes is not None and disk_total else None
    payload["db_bytes_ratio_of_free"] = db_bytes / disk_free if db_bytes is not None and disk_free else None

    if db is None:
        return payload

    page_size = _pragma_int(db, "page_size")
    page_count = _pragma_int(db, "page_count")
    freelist_count = _pragma_int(db, "freelist_count")
    freelist_bytes = None
    if page_size is not None and freelist_count is not None:
        freelist_bytes = page_size * freelist_count
    payload.update(
        {
            "db_page_size": page_size,
            "db_page_count": page_count,
            "db_freelist_count": freelist_count,
            "db_page_bytes": page_size * page_count if page_size is not None and page_count is not None else None,
            "db_freelist_bytes": freelist_bytes,
            "db_freelist_ratio": freelist_count / page_count if page_count else None,
        }
    )
    return payload


def collect_sqlite_store_stats(
    database_url: str | None,
    *,
    archive_database_url: str | None = None,
    db: Session | Connection | None = None,
) -> dict[str, Any]:
    """Return cheap file diagnostics for a configured SQLite store.

    This is used for the optional Live Store before route adoption. It must not
    connect to or create the live DB unless the caller supplies an open
    connection; path/free-space visibility is enough for Phase 0.
    """

    if not database_url:
        return {
            "configured": False,
            "status": "disabled",
            "database_url": None,
            "db_path": None,
            "warnings": [],
            "live_archive_outbox": {"checked": False, "reason": "store_disabled"},
            "live_input_receipts": {"checked": False, "reason": "store_disabled"},
        }

    payload = collect_sqlite_db_stats(database_url, db=db)
    if payload is None:
        return {
            "configured": True,
            "status": "unsupported",
            "database_url": database_url,
            "db_path": None,
            "warnings": ["not_file_backed_sqlite"],
            "live_archive_outbox": {"checked": False, "reason": "unsupported_store"},
            "live_input_receipts": {"checked": False, "reason": "unsupported_store"},
        }

    warnings: list[str] = []
    paths = sqlite_db_paths(database_url)
    archive_paths = sqlite_db_paths(archive_database_url or "") if archive_database_url else None
    same_db_path = False
    same_directory = False
    if paths is not None:
        db_path, _wal_path = paths
        expanded_db_path = db_path.expanduser()
        try:
            resolved_db_path = expanded_db_path.resolve()
        except OSError:
            resolved_db_path = expanded_db_path.absolute()
        if str(expanded_db_path).startswith("/tmp/") or str(resolved_db_path).startswith(("/tmp/", "/private/tmp/")):
            warnings.append("tmp_path")
        if archive_paths is not None:
            archive_db_path, _archive_wal_path = archive_paths
            try:
                resolved_archive_path = archive_db_path.expanduser().resolve()
            except OSError:
                resolved_archive_path = archive_db_path.expanduser().absolute()
            same_db_path = resolved_db_path == resolved_archive_path
            same_directory = resolved_db_path.parent == resolved_archive_path.parent
            if same_db_path:
                warnings.append("same_as_archive_db")
            elif same_directory:
                warnings.append("same_directory_as_archive_db")

    payload["configured"] = True
    payload["status"] = "ok" if payload.get("db_exists") else "missing"
    payload["warnings"] = warnings
    payload["same_db_path_as_archive"] = same_db_path
    payload["same_directory_as_archive"] = same_directory
    payload["live_archive_outbox"] = (
        _collect_live_archive_outbox_stats(db)
        if db is not None
        else {
            "checked": False,
            "reason": "no_connection",
        }
    )
    payload["live_input_receipts"] = (
        _collect_live_input_receipt_stats(db)
        if db is not None
        else {
            "checked": False,
            "reason": "no_connection",
        }
    )
    return payload


def _collect_live_archive_outbox_stats(db: Session | Connection) -> dict[str, Any]:
    if not _table_exists(db, "live_archive_outbox"):
        return {
            "checked": True,
            "table_exists": False,
            "pending_count": None,
            "failed_count": None,
            "oldest_pending_created_at": None,
            "max_attempts": None,
        }
    row = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE drained_at IS NULL) AS pending_count,
                COUNT(*) FILTER (WHERE drained_at IS NULL AND last_error IS NOT NULL) AS failed_count,
                MIN(CASE WHEN drained_at IS NULL THEN created_at END) AS oldest_pending_created_at,
                MAX(attempts) AS max_attempts
            FROM live_archive_outbox
            """
        )
    ).fetchone()
    return {
        "checked": True,
        "table_exists": True,
        "pending_count": int(row[0] or 0) if row is not None else 0,
        "failed_count": int(row[1] or 0) if row is not None else 0,
        "oldest_pending_created_at": str(row[2]) if row is not None and row[2] is not None else None,
        "max_attempts": int(row[3] or 0) if row is not None else 0,
    }


def _collect_live_input_receipt_stats(db: Session | Connection) -> dict[str, Any]:
    if not _table_exists(db, "live_session_input_receipts"):
        return {
            "checked": True,
            "table_exists": False,
            "queued_old_count": None,
            "delivering_old_count": None,
            "missing_projection_old_count": None,
            "failed_count": None,
            "oldest_queued_created_at": None,
            "oldest_delivering_updated_at": None,
            "oldest_missing_projection_updated_at": None,
        }
    row = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'queued'
                      AND created_at <= datetime('now', '-2 minutes')
                ) AS queued_old_count,
                COUNT(*) FILTER (
                    WHERE status = 'delivering'
                      AND updated_at <= datetime('now', '-5 minutes')
                ) AS delivering_old_count,
                COUNT(*) FILTER (
                    WHERE status = 'delivered'
                      AND archive_session_input_id IS NULL
                      AND updated_at <= datetime('now', '-5 minutes')
                ) AS missing_projection_old_count,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                MIN(CASE
                    WHEN status = 'queued'
                     AND created_at <= datetime('now', '-2 minutes')
                    THEN created_at END
                ) AS oldest_queued_created_at,
                MIN(CASE
                    WHEN status = 'delivering'
                     AND updated_at <= datetime('now', '-5 minutes')
                    THEN updated_at END
                ) AS oldest_delivering_updated_at,
                MIN(CASE
                    WHEN status = 'delivered'
                     AND archive_session_input_id IS NULL
                     AND updated_at <= datetime('now', '-5 minutes')
                    THEN updated_at END
                ) AS oldest_missing_projection_updated_at
            FROM live_session_input_receipts
            """
        )
    ).fetchone()
    return {
        "checked": True,
        "table_exists": True,
        "queued_old_count": int(row[0] or 0) if row is not None else 0,
        "delivering_old_count": int(row[1] or 0) if row is not None else 0,
        "missing_projection_old_count": int(row[2] or 0) if row is not None else 0,
        "failed_count": int(row[3] or 0) if row is not None else 0,
        "oldest_queued_created_at": str(row[4]) if row is not None and row[4] is not None else None,
        "oldest_delivering_updated_at": str(row[5]) if row is not None and row[5] is not None else None,
        "oldest_missing_projection_updated_at": str(row[6]) if row is not None and row[6] is not None else None,
    }
