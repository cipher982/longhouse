#!/usr/bin/env python3
"""Saturating raw_json backfill for legacy codec=0 rows.

Runs directly against a SQLite database and keeps going until every
compressible legacy `raw_json` payload has been rewritten to codec=1
(`raw_json_z` zstd blob + cleared text column). This is the fast operator
path for large backlogs; the scheduled job remains the live-instance safety net.

Usage:
    DATABASE_URL=sqlite:////data/longhouse.db uv run python scripts/ops/backfill_raw_json.py
    DATABASE_URL=sqlite:///~/tmp/longhouse.db uv run python scripts/ops/backfill_raw_json.py

Tuning:
    RAW_JSON_BACKFILL_ROWS_PER_TX   rows per write transaction (default 25000)
    RAW_JSON_BACKFILL_WORKERS       compression threads (default cpu_count)
    RAW_JSON_BACKFILL_CHECKPOINT_EVERY  PASSIVE WAL checkpoint cadence in batches (default 20)
    RAW_JSON_BACKFILL_PROGRESS_EVERY    per-table progress log cadence in batches (default 10)
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine.url import make_url

# Make repo-local backend imports work both locally and in containers.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_BACKEND_DIR = _REPO_ROOT / "server"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import zerg.bootstrap_sqlite  # noqa: F401
import sqlite3

from zerg.services.raw_json_compression import compress_raw_json


@dataclass(frozen=True)
class TablePlan:
    name: str
    clear_expr: str
    pending_where: str


@dataclass
class TableStats:
    compressed_rows: int = 0
    batches: int = 0
    compression_failures: int = 0
    write_conflicts: int = 0
    duration_s: float = 0.0
    last_seen_id: int = 0


_EVENTS = TablePlan(
    name="events",
    clear_expr="NULL",
    pending_where="raw_json_codec = 0 AND raw_json IS NOT NULL",
)
_SOURCE_LINES = TablePlan(
    name="source_lines",
    clear_expr="''",
    pending_where="raw_json_codec = 0",
)
_TABLES = (_EVENTS, _SOURCE_LINES)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return int(raw)


def _connect_sqlite(db_url: str) -> sqlite3.Connection:
    parsed = make_url(db_url)
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError(f"Unsupported DATABASE_URL driver: {parsed.drivername}")
    db_path = parsed.database
    if not db_path:
        raise ValueError("DATABASE_URL must point at a file-backed SQLite database")
    db_path = os.path.expanduser(db_path)

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _ensure_pending_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_events_raw_json_pending
        ON events(id)
        WHERE raw_json_codec = 0
          AND raw_json IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_source_lines_raw_json_pending
        ON source_lines(id)
        WHERE raw_json_codec = 0
        """
    )


def _first_pending_ids(conn: sqlite3.Connection) -> dict[str, int | None]:
    pending: dict[str, int | None] = {}
    for table in _TABLES:
        row = conn.execute(
            f"""
            SELECT id
            FROM {table.name}
            WHERE {table.pending_where}
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        pending[table.name] = int(row["id"]) if row else None
    return pending


def _fetch_batch(conn: sqlite3.Connection, table: TablePlan, *, after_id: int, limit: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        f"""
        SELECT id, raw_json
        FROM {table.name}
        WHERE {table.pending_where}
          AND id > ?
        ORDER BY id
        LIMIT ?
        """,
        (after_id, limit),
    ).fetchall()
    return [(int(row["id"]), str(row["raw_json"])) for row in rows]


def _compress_rows(rows: list[tuple[int, str]], *, table_name: str, workers: int) -> tuple[list[tuple[bytes, int]], int]:
    if not rows:
        return [], 0

    try:
        if workers > 1 and len(rows) > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                blobs = list(executor.map(compress_raw_json, (raw for _row_id, raw in rows)))
        else:
            blobs = [compress_raw_json(raw) for _row_id, raw in rows]
        return [(blob, row_id) for (row_id, _raw), blob in zip(rows, blobs, strict=False)], 0
    except Exception:
        updates: list[tuple[bytes, int]] = []
        failures = 0
        for row_id, raw in rows:
            try:
                updates.append((compress_raw_json(raw), row_id))
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(
                    f"compress failed for {table_name} id={row_id}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        return updates, failures


def _apply_batch(conn: sqlite3.Connection, table: TablePlan, updates: list[tuple[bytes, int]]) -> int:
    if not updates:
        return 0

    before = conn.total_changes
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"""
            UPDATE {table.name}
            SET raw_json_z = ?, raw_json_codec = 1, raw_json = {table.clear_expr}
            WHERE id = ?
              AND raw_json_codec = 0
            """,
            updates,
        )
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001
        conn.execute("ROLLBACK")
        raise

    return conn.total_changes - before


def _backfill_table(
    conn: sqlite3.Connection,
    table: TablePlan,
    *,
    rows_per_tx: int,
    workers: int,
    checkpoint_every: int,
    progress_every: int,
) -> TableStats:
    stats = TableStats()
    started = time.monotonic()
    last_seen_id = 0

    while True:
        rows = _fetch_batch(conn, table, after_id=last_seen_id, limit=rows_per_tx)
        if not rows:
            break

        stats.batches += 1
        last_seen_id = rows[-1][0]
        stats.last_seen_id = last_seen_id
        updates, failures = _compress_rows(rows, table_name=table.name, workers=workers)
        updated = _apply_batch(conn, table, updates)

        stats.compression_failures += failures
        stats.compressed_rows += updated
        stats.write_conflicts += max(0, len(updates) - updated)

        if progress_every > 0 and stats.batches % progress_every == 0:
            elapsed = time.monotonic() - started
            print(
                f"{table.name} progress: batches={stats.batches} "
                f"compressed={stats.compressed_rows} last_id={stats.last_seen_id} "
                f"elapsed={elapsed:.1f}s"
            )

        if checkpoint_every > 0 and stats.batches % checkpoint_every == 0:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    stats.duration_s = time.monotonic() - started
    return stats


def main() -> int:
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    rows_per_tx = _env_int("RAW_JSON_BACKFILL_ROWS_PER_TX", 25_000)
    workers = max(1, _env_int("RAW_JSON_BACKFILL_WORKERS", os.cpu_count() or 1))
    checkpoint_every = max(0, _env_int("RAW_JSON_BACKFILL_CHECKPOINT_EVERY", 20))
    progress_every = max(0, _env_int("RAW_JSON_BACKFILL_PROGRESS_EVERY", 10))

    conn = _connect_sqlite(db_url)
    try:
        _ensure_pending_indexes(conn)
        before = _first_pending_ids(conn)
        print(
            "starting raw_json backfill:",
            f"events_first_pending_id={before['events'] or 'none'},",
            f"source_lines_first_pending_id={before['source_lines'] or 'none'},",
            f"rows_per_tx={rows_per_tx}, workers={workers},",
            f"checkpoint_every={checkpoint_every}, progress_every={progress_every}",
        )

        if all(first_id is None for first_id in before.values()):
            print("nothing to do")
            return 0

        results = {
            table.name: _backfill_table(
                conn,
                table,
                rows_per_tx=rows_per_tx,
                workers=workers,
                checkpoint_every=checkpoint_every,
                progress_every=progress_every,
            )
            for table in _TABLES
        }
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        after = _first_pending_ids(conn)

        for table in _TABLES:
            stats = results[table.name]
            remaining_first_id = after[table.name]
            print(
                f"{table.name}: compressed={stats.compressed_rows} "
                f"batches={stats.batches} failures={stats.compression_failures} "
                f"conflicts={stats.write_conflicts} remaining_first_id={remaining_first_id or 'none'} "
                f"duration={stats.duration_s:.2f}s"
            )

        remaining = {name: first_id for name, first_id in after.items() if first_id is not None}
        failures_total = sum(stats.compression_failures for stats in results.values())
        conflicts_total = sum(stats.write_conflicts for stats in results.values())

        if remaining or failures_total or conflicts_total:
            print(
                f"backfill incomplete: remaining={remaining} "
                f"failures={failures_total} conflicts={conflicts_total}",
                file=sys.stderr,
            )
            return 1

        print("backfill complete")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
