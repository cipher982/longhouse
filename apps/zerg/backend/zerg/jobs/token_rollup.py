"""Nightly token usage rollup.

Aggregates session token counts into token_daily_stats by date/provider/model.
Recomputes the last 7 days to handle late-arriving sessions (idempotent UPSERT).

Note: AgentSession has no 'model' or 'approx_token_count' columns yet.
Until those are added, model is always 'unknown' and total_tokens is always 0.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import text

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)

_RECOMPUTE_DAYS = 7  # Always recompute last N days to handle late updates


async def run() -> dict[str, Any]:
    """Run token rollup for the last 7 days."""
    rows_written = 0

    with db_session() as db:
        now_utc = datetime.now(timezone.utc)

        for days_ago in range(_RECOMPUTE_DAYS):
            target_date = (now_utc - timedelta(days=days_ago)).strftime("%Y-%m-%d")

            # Aggregate sessions that started on this UTC date.
            # model column does not exist on sessions table yet — use 'unknown'.
            # approx_token_count does not exist yet — use 0.
            agg_rows = db.execute(
                text("""
                    SELECT
                        COALESCE(provider, 'unknown') AS provider,
                        'unknown' AS model,
                        COUNT(*) AS session_count,
                        0 AS total_tokens
                    FROM sessions
                    WHERE DATE(started_at) = :target_date
                    GROUP BY COALESCE(provider, 'unknown')
                """),
                {"target_date": target_date},
            ).fetchall()

            for row in agg_rows:
                db.execute(
                    text("""
                        INSERT INTO token_daily_stats (date, provider, model, session_count, total_tokens)
                        VALUES (:date, :provider, :model, :session_count, :total_tokens)
                        ON CONFLICT (date, provider, model) DO UPDATE SET
                            session_count = excluded.session_count,
                            total_tokens = excluded.total_tokens
                    """),
                    {
                        "date": target_date,
                        "provider": row.provider,
                        "model": row.model,
                        "session_count": row.session_count,
                        "total_tokens": row.total_tokens,
                    },
                )
                rows_written += 1

        db.commit()

    return {"rows_written": rows_written, "days_recomputed": _RECOMPUTE_DAYS}


job_registry.register(
    JobConfig(
        id="token-rollup",
        cron=os.getenv("TOKEN_ROLLUP_CRON", "5 0 * * *"),
        func=run,
        enabled=True,
        timeout_seconds=120,
        tags=["analytics", "tokens", "builtin"],
        description="Nightly token usage rollup by provider/model",
    )
)
