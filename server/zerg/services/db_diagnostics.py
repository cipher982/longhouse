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


def _directory_file_bytes(path: Path) -> int:
    if not path.exists():
        return 0

    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


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


def collect_sqlite_deep_counts(db: Session | Connection) -> dict[str, int | None]:
    """Return explicit potentially expensive counts for operator-invoked diagnostics."""
    events_columns = _table_columns(db, "events")
    source_columns = _table_columns(db, "source_lines")
    observation_columns = _table_columns(db, "session_observations")

    return {
        "events_raw_json_pending": _count_where(db, "events", "raw_json_codec = 0 AND raw_json IS NOT NULL")
        if {"raw_json", "raw_json_codec"} <= events_columns
        else None,
        "source_lines_raw_json_pending": _count_where(db, "source_lines", "raw_json_codec = 0")
        if "raw_json_codec" in source_columns
        else None,
        "events_thread_id_null": _count_where(db, "events", "thread_id IS NULL")
        if "thread_id" in events_columns
        else None,
        "source_lines_thread_id_null": _count_where(db, "source_lines", "thread_id IS NULL")
        if "thread_id" in source_columns
        else None,
        "session_observations_thread_id_null": _count_where(db, "session_observations", "thread_id IS NULL")
        if "thread_id" in observation_columns
        else None,
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
        "backup_bytes": _directory_file_bytes(backup_dir),
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
            "db_freelist_bytes": page_size * freelist_count
            if page_size is not None and freelist_count is not None
            else None,
            "db_freelist_ratio": freelist_count / page_count if page_count else None,
        }
    )
    return payload
