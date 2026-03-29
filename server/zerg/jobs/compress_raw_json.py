"""Background migration job: compress legacy raw_json rows to zstd.

Drains all compressible legacy rows from ``events`` and ``source_lines`` in a
single run, converting codec=0 (plain text) rows to codec=1 (zstd-compressed).
Work is chunked only to bound transaction size and memory use; the job keeps
looping until nothing compressible remains.

Legacy ``events`` rows with ``raw_json IS NULL`` are intentionally ignored:
there is no original payload to compress, so they are not part of this
backfill's completion criteria.

Disable via ``COMPRESS_RAW_JSON_ENABLED=0`` (default enabled on SQLite).
Schedule via ``COMPRESS_RAW_JSON_CRON`` (default: every 15 minutes).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)

JOB_ID = "compress-raw-json"
_DEFAULT_CHUNK_SIZE = 10_000
_DEFAULT_SLEEP_MS = 0


@dataclass
class _TableCompressionStats:
    compressed_rows: int = 0
    batches: int = 0
    compression_failures: int = 0
    write_conflicts: int = 0
    drained: bool = True


async def run() -> dict[str, Any]:
    """Compress all compressible legacy raw_json rows for each archival table."""
    from zerg.database import default_engine
    from zerg.services.write_serializer import get_write_serializer

    if default_engine is None:
        return {"status": "skipped", "reason": "no engine"}

    if "sqlite" not in str(default_engine.url):
        return {"status": "skipped", "reason": "not sqlite"}

    ws = get_write_serializer()
    if not ws.is_configured:
        return {"status": "skipped", "reason": "write serializer not configured"}

    batch_size = int(os.getenv("COMPRESS_RAW_JSON_CHUNK_SIZE", str(_DEFAULT_CHUNK_SIZE)))
    sleep_ms = int(os.getenv("COMPRESS_RAW_JSON_BATCH_SLEEP_MS", str(_DEFAULT_SLEEP_MS)))

    await _ensure_pending_indexes()

    events_stats = await _compress_table(
        table="events",
        id_col="id",
        batch_size=batch_size,
        sleep_ms=sleep_ms,
    )
    lines_stats = await _compress_table(
        table="source_lines",
        id_col="id",
        batch_size=batch_size,
        sleep_ms=sleep_ms,
    )

    total = events_stats.compressed_rows + lines_stats.compressed_rows
    failures = events_stats.compression_failures + lines_stats.compression_failures
    conflicts = events_stats.write_conflicts + lines_stats.write_conflicts
    status = "degraded" if failures else "success"

    logger.info(
        "compress-raw-json: status=%s events=%d source_lines=%d total=%d event_batches=%d source_line_batches=%d failures=%d conflicts=%d",
        status,
        events_stats.compressed_rows,
        lines_stats.compressed_rows,
        total,
        events_stats.batches,
        lines_stats.batches,
        failures,
        conflicts,
    )
    return {
        "status": status,
        "events_compressed": events_stats.compressed_rows,
        "source_lines_compressed": lines_stats.compressed_rows,
        "total_compressed": total,
        "events_batches": events_stats.batches,
        "source_lines_batches": lines_stats.batches,
        "compression_failures": failures,
        "write_conflicts": conflicts,
        "events_drained": events_stats.drained,
        "source_lines_drained": lines_stats.drained,
    }


async def _compress_table(*, table: str, id_col: str, batch_size: int, sleep_ms: int) -> _TableCompressionStats:
    """Compress all pending legacy rows from ``table``."""
    from zerg.services.raw_json_compression import CODEC_PLAIN
    from zerg.services.raw_json_compression import CODEC_ZSTD
    from zerg.services.raw_json_compression import compress_raw_json
    from zerg.services.write_serializer import get_write_serializer

    if table not in {"events", "source_lines"}:
        raise ValueError(f"Unsupported table for raw_json compression: {table}")

    ws = get_write_serializer()
    if not ws.is_configured:
        logger.warning("compress-raw-json: write serializer not configured, skipping %s", table)
        return _TableCompressionStats(drained=False)

    pending_filter = "raw_json_codec = :codec AND raw_json IS NOT NULL" if table == "events" else "raw_json_codec = :codec"
    clear_expr = "NULL" if table == "events" else "''"
    stats = _TableCompressionStats()
    last_seen_id = 0

    def _fetch_batch(after_id: int) -> list[tuple[int, str]]:
        """Fetch the next chunk of uncompressed rows."""
        from sqlalchemy import text

        from zerg.database import default_engine

        with default_engine.connect() as conn:  # type: ignore[union-attr]
            rows = conn.execute(
                text(
                    f"""
                    SELECT {id_col}, raw_json
                    FROM {table}
                    WHERE {pending_filter}
                      AND {id_col} > :after_id
                    ORDER BY {id_col}
                    LIMIT :limit
                    """
                ),
                {"codec": CODEC_PLAIN, "after_id": after_id, "limit": batch_size},
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _build_updates(rows: list[tuple[int, str]]) -> tuple[list[dict[str, Any]], int]:
        updates: list[dict[str, Any]] = []
        failures = 0
        for row_id, raw in rows:
            try:
                updates.append(
                    {
                        "blob": compress_raw_json(raw),
                        "codec": CODEC_ZSTD,
                        "plain": CODEC_PLAIN,
                        "row_id": row_id,
                    }
                )
            except Exception:
                failures += 1
                logger.exception("compress-raw-json: compress failed for %s id=%s, skipping", table, row_id)
        return updates, failures

    def _apply_batch(db, updates: list[dict[str, Any]]) -> int:
        from sqlalchemy import text

        result = db.execute(
            text(
                f"""
                UPDATE {table}
                SET raw_json_z = :blob,
                    raw_json_codec = :codec,
                    raw_json = {clear_expr}
                WHERE {id_col} = :row_id
                  AND raw_json_codec = :plain
                """
            ),
            updates,
        )
        rowcount = getattr(result, "rowcount", None)
        if isinstance(rowcount, int) and rowcount >= 0:
            return rowcount
        return len(updates)

    while True:
        rows = await asyncio.to_thread(_fetch_batch, last_seen_id)
        if not rows:
            break

        last_seen_id = int(rows[-1][0])
        stats.batches += 1
        updates, failures = await asyncio.to_thread(_build_updates, rows)
        stats.compression_failures += failures

        if updates:
            updated = await ws.execute(lambda db, _updates=updates: _apply_batch(db, _updates), label=f"compress-{table}")
            stats.compressed_rows += updated
            stats.write_conflicts += max(0, len(updates) - updated)

        if sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)

    stats.drained = stats.compression_failures == 0
    return stats


async def _ensure_pending_indexes() -> None:
    """Create the pending-row indexes lazily so normal ingest startup stays untouched."""
    from sqlalchemy import text

    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()

    def _apply(db) -> None:
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_events_raw_json_pending
                ON events(id)
                WHERE raw_json_codec = 0
                  AND raw_json IS NOT NULL
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_source_lines_raw_json_pending
                ON source_lines(id)
                WHERE raw_json_codec = 0
                """
            )
        )

    await ws.execute(_apply, label="compress-indexes")


_enabled = os.getenv("COMPRESS_RAW_JSON_ENABLED", "1") == "1"

job_registry.register(
    JobConfig(
        id=JOB_ID,
        cron=os.getenv("COMPRESS_RAW_JSON_CRON", "*/15 * * * *"),
        func=run,
        enabled=_enabled,
        timeout_seconds=int(os.getenv("COMPRESS_RAW_JSON_TIMEOUT_SECONDS", "21600")),
        max_attempts=1,
        tags=["maintenance", "builtin", "migration"],
        description="Drain legacy plain-text raw_json rows to zstd (codec 0→1)",
    )
)
