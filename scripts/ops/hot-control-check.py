#!/usr/bin/env python3
"""Binary hot-control health check for alpha dogfood.

This intentionally checks facts the product emits instead of asking a human to
"monitor" behavior subjectively.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
from typing import Any

from zerg.config import get_settings_unchecked
from zerg.database import make_live_engine
from zerg.services.db_diagnostics import collect_sqlite_store_stats
from zerg.services.db_diagnostics import sqlite_db_paths


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_minutes(value: Any) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 60.0)


def _add_failure(failures: list[str], label: str, payload: dict[str, Any]) -> None:
    details = ", ".join(f"{key}={value}" for key, value in payload.items() if value is not None)
    failures.append(f"{label}: {details}" if details else label)


def evaluate(live_store: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if live_store.get("status") != "ok":
        _add_failure(failures, "live_store_not_ok", {"status": live_store.get("status"), "path": live_store.get("db_path")})
        return failures

    outbox = dict(live_store.get("live_archive_outbox") or {})
    if outbox.get("checked") and outbox.get("table_exists"):
        failed_count = int(outbox.get("failed_count") or 0)
        if failed_count:
            _add_failure(failures, "outbox_failed_rows", {"failed_count": failed_count, "max_attempts": outbox.get("max_attempts")})
        oldest_pending = outbox.get("oldest_pending_created_at")
        oldest_pending_age = _age_minutes(oldest_pending)
        if oldest_pending_age is not None and oldest_pending_age > 10:
            _add_failure(
                failures,
                "outbox_projection_lag",
                {"oldest_pending_created_at": oldest_pending, "age_minutes": round(oldest_pending_age, 1)},
            )

    receipts = dict(live_store.get("live_input_receipts") or {})
    if receipts.get("checked") and receipts.get("table_exists"):
        checks = {
            "queued_old_count": "queued_receipts_old",
            "delivering_old_count": "delivering_receipts_stuck",
            "missing_projection_old_count": "delivered_receipts_missing_projection",
            "failed_count": "failed_receipts",
        }
        for key, label in checks.items():
            count = int(receipts.get(key) or 0)
            if count:
                _add_failure(failures, label, {key: count})
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Check hot-control live receipt health.")
    parser.add_argument("--since", default="24h", help="Human label for the dogfood window; thresholds are fixed.")
    args = parser.parse_args()

    settings = get_settings_unchecked()
    live_url = settings.live_database_url
    archive_url = settings.database_url
    if not live_url:
        print("FAIL hot-control: live store is not configured")
        return 1
    live_paths = sqlite_db_paths(live_url)
    if live_paths is not None and not live_paths[0].expanduser().exists():
        print(f"FAIL hot-control: live store DB is missing path={live_paths[0].expanduser()}")
        return 1

    engine = make_live_engine(live_url)
    try:
        with engine.connect() as conn:
            live_store = collect_sqlite_store_stats(live_url, archive_database_url=archive_url, db=conn)
    finally:
        engine.dispose()

    failures = evaluate(live_store)
    window = str(args.since or "24h")
    if failures:
        print(f"FAIL hot-control since={window}")
        for failure in failures:
            print(f"- {failure}")
        return 1

    receipts = dict(live_store.get("live_input_receipts") or {})
    outbox = dict(live_store.get("live_archive_outbox") or {})
    print(
        "PASS hot-control "
        f"since={window} "
        f"outbox_pending={outbox.get('pending_count', 0)} "
        f"receipt_failures={receipts.get('failed_count', 0)} "
        f"projection_lag={receipts.get('missing_projection_old_count', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
