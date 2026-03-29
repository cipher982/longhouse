"""Background migration job: compress legacy raw_json rows to zstd.

Processes events and source_lines in batches, converting codec=0 (plain text)
rows to codec=1 (zstd compressed). Re-entrant and interruptible — safe to run
repeatedly. Each batch is committed independently so progress is never lost.

The job is rate-limited by COMPRESS_RAW_JSON_BATCH_SLEEP_MS to avoid
monopolising the write serializer. Default: 50ms between batches.

Disable via COMPRESS_RAW_JSON_ENABLED=0 (default enabled on SQLite).
Schedule via COMPRESS_RAW_JSON_CRON (default: every 15 minutes).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)

JOB_ID = "compress-raw-json"
_DEFAULT_BATCH = 500
_DEFAULT_SLEEP_MS = 50


async def run() -> dict[str, Any]:
    """Compress a batch of legacy plain-text raw_json rows per table."""
    from zerg.database import default_engine

    if default_engine is None:
        return {"status": "skipped", "reason": "no engine"}

    if "sqlite" not in str(default_engine.url):
        return {"status": "skipped", "reason": "not sqlite"}

    batch_size = int(os.getenv("COMPRESS_RAW_JSON_BATCH_SIZE", str(_DEFAULT_BATCH)))
    sleep_ms = int(os.getenv("COMPRESS_RAW_JSON_BATCH_SLEEP_MS", str(_DEFAULT_SLEEP_MS)))

    events_done = await _compress_table(
        table="events",
        id_col="id",
        batch_size=batch_size,
        sleep_ms=sleep_ms,
    )
    lines_done = await _compress_table(
        table="source_lines",
        id_col="id",
        batch_size=batch_size,
        sleep_ms=sleep_ms,
    )

    total = events_done + lines_done
    logger.info("compress-raw-json: events=%d source_lines=%d total=%d", events_done, lines_done, total)
    return {
        "status": "success",
        "events_compressed": events_done,
        "source_lines_compressed": lines_done,
        "total_compressed": total,
    }


async def _compress_table(*, table: str, id_col: str, batch_size: int, sleep_ms: int) -> int:
    """Compress one batch of legacy rows from *table*. Returns rows processed."""
    from zerg.services.raw_json_compression import CODEC_PLAIN
    from zerg.services.raw_json_compression import CODEC_ZSTD
    from zerg.services.raw_json_compression import compress_raw_json
    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if not ws.is_configured:
        logger.warning("compress-raw-json: write serializer not configured, skipping %s", table)
        return 0

    def _fetch_batch() -> list[tuple[int, str]]:
        """Fetch a batch of uncompressed rows (run in thread)."""
        from sqlalchemy import text

        from zerg.database import default_engine

        with default_engine.connect() as conn:  # type: ignore[union-attr]
            rows = conn.execute(
                text(
                    f"""
                    SELECT {id_col}, raw_json
                    FROM {table}
                    WHERE raw_json_codec = :codec
                      AND raw_json IS NOT NULL
                      AND raw_json != ''
                    LIMIT :limit
                    """
                ),
                {"codec": CODEC_PLAIN, "limit": batch_size},
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    rows = await asyncio.to_thread(_fetch_batch)
    if not rows:
        return 0

    # Compress in memory (CPU-bound but fast; level=3 is ~400 MB/s)
    updates: list[tuple[bytes, int]] = []
    for row_id, raw in rows:
        try:
            blob = compress_raw_json(raw)
            updates.append((blob, row_id))
        except Exception:
            logger.exception("compress-raw-json: compress failed for %s id=%s, skipping", table, row_id)

    if not updates:
        return 0

    # events.raw_json is nullable → set to NULL; source_lines.raw_json is NOT
    # NULL → set to '' sentinel to satisfy the constraint.
    clear_expr = "NULL" if table == "events" else "''"

    def _apply_batch(db) -> None:
        from sqlalchemy import text

        for blob, row_id in updates:
            db.execute(
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
                {"blob": blob, "codec": CODEC_ZSTD, "plain": CODEC_PLAIN, "row_id": row_id},
            )

    await ws.execute(_apply_batch, label=f"compress-{table}")

    if sleep_ms > 0:
        await asyncio.sleep(sleep_ms / 1000.0)

    return len(updates)


_enabled = os.getenv("COMPRESS_RAW_JSON_ENABLED", "1") == "1"

job_registry.register(
    JobConfig(
        id=JOB_ID,
        cron=os.getenv("COMPRESS_RAW_JSON_CRON", "*/15 * * * *"),
        func=run,
        enabled=_enabled,
        timeout_seconds=600,
        max_attempts=1,
        tags=["maintenance", "builtin", "migration"],
        description="Batch-compress legacy plain-text raw_json rows to zstd (codec 0→1)",
    )
)
