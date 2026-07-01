"""Lightweight SQLite diagnostics for operator and watchman surfaces."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

_WATCHED_TABLES = ("sessions", "events", "source_lines", "session_observations")
_MAX_BACKUP_SCAN_ENTRIES = 5000


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
        counts.update(
            {
                "events_thread_id_null": _count_where(db, "events", "thread_id IS NULL") if "thread_id" in events_columns else None,
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
        rows = db.execute(
            text(
                """
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
            )
        ).mappings()
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }

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
    payload.update(
        {
            "db_page_size": page_size,
            "db_page_count": page_count,
            "db_freelist_count": freelist_count,
            "db_page_bytes": page_size * page_count if page_size is not None and page_count is not None else None,
            "db_freelist_bytes": page_size * freelist_count if page_size is not None and freelist_count is not None else None,
            "db_freelist_ratio": freelist_count / page_count if page_count else None,
        }
    )
    return payload
