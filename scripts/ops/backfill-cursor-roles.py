#!/usr/bin/env python3
"""Backfill Cursor user-event roles + <user_query> unwrapping.

Repairs legacy Cursor ``role="user"`` rows that predate the decoder fix
(commit 441f015b4): re-roles Cursor's environment-context injection
(<user_info>/<rules>/...) to ``system`` and unwraps the real user turn from
``<user_query>...</user_query>``. After this, historical Cursor sessions stop
showing the 59KB context dump as "You" on the timeline.

Operates on persisted ``content_text`` + ``role`` only; ``raw_json`` is left
as Cursor's ground-truth original. Resumable, id-cursored, idempotent.

Usage:
    DATABASE_URL=sqlite:////data/longhouse.db uv run python scripts/ops/backfill-cursor-roles.py
    DATABASE_URL=sqlite:///~/tmp/longhouse.db BACKFILL_DRY_RUN=1 uv run python scripts/ops/backfill-cursor-roles.py

Tuning:
    BACKFILL_DRY_RUN            when set, classify and report without writing
    BACKFILL_BATCH_SIZE         rows per transaction (default 1000)
    BACKFILL_PROGRESS_EVERY     log cadence in batches (default 10)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make repo-local backend imports work both locally and in containers.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_BACKEND_DIR = _REPO_ROOT / "server"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import zerg.bootstrap_sqlite  # noqa: F401,E402  (sqlite function registration)
from zerg.database import make_engine  # noqa: E402
from zerg.services.cursor_role_backfill import backfill_cursor_user_roles  # noqa: E402


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return int(raw)


def main() -> int:
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    dry_run = bool(os.getenv("BACKFILL_DRY_RUN"))
    batch_size = max(1, _env_int("BACKFILL_BATCH_SIZE", 1000))
    progress_every = max(0, _env_int("BACKFILL_PROGRESS_EVERY", 10))

    engine = make_engine(db_url)
    SessionLocal = sessionmaker(bind=engine)

    mode = "dry run" if dry_run else "write"
    print(f"starting cursor role backfill ({mode}): batch_size={batch_size}")

    after_id = 0
    batches = 0
    total_scanned = 0
    total_re_roleed = 0
    total_unwrapped = 0
    started = time.monotonic()

    while True:
        with SessionLocal() as db:
            result = backfill_cursor_user_roles(
                db,
                after_id=after_id,
                batch_size=batch_size,
                dry_run=dry_run,
            )
            if result.scanned == 0:
                break
            db.commit()
        after_id = result.last_id or after_id
        batches += 1
        total_scanned += result.scanned
        total_re_roleed += result.re_roleed
        total_unwrapped += result.unwrapped
        if progress_every > 0 and batches % progress_every == 0:
            elapsed = time.monotonic() - started
            print(
                f"progress: batches={batches} scanned={total_scanned} "
                f"re_roleed={total_re_roleed} unwrapped={total_unwrapped} "
                f"last_id={after_id} elapsed={elapsed:.1f}s"
            )

    elapsed = time.monotonic() - started
    print(
        f"backfill complete ({mode}): batches={batches} scanned={total_scanned} "
        f"re_roleed={total_re_roleed} unwrapped={total_unwrapped} "
        f"duration={elapsed:.2f}s"
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
