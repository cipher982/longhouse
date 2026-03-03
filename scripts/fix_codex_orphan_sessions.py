#!/usr/bin/env python3
"""Fix Codex orphan sessions created by the incremental-parse session_id bug.

Background
----------
Before fix commit 775439f0 (2026-02-25), the Rust engine derived session_id
from the filename for the *first* parse of a Codex session, then used a
deterministic UUID v5 from the file *path* for each incremental parse.  This
split a single real session into two DB rows:

  - Canonical session  (UUID from filename stem, has project + user messages)
  - Orphan session     (UUID v5 from path, null project, has assistant messages)

This script identifies these pairs, re-parents the orphan's events onto the
canonical session, updates session-level counters, and deletes the orphan.

Safety
------
- Dry-run by default (--dry-run).  Pass --execute to commit changes.
- Operates on the production SQLite DB on the zerg server, or a local path
  via --db.
- Identifies pairs by: same provider=codex, overlapping time window (±5 min),
  same source_path prefix on events.
- Only merges when the canonical session is a valid UUID matching the Codex
  session filename convention.

Usage
-----
    # Dry run (default) — shows what would be merged
    uv run scripts/fix_codex_orphan_sessions.py

    # Execute against prod DB over SSH
    uv run scripts/fix_codex_orphan_sessions.py --execute

    # Execute against a local DB copy
    uv run scripts/fix_codex_orphan_sessions.py --db /path/to/agents.db --execute
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
BACKEND_DIR = REPO_ROOT / "apps" / "zerg" / "backend"
PROD_CONTAINER = "longhouse-david010"


def get_prod_db(tmp_dir: str) -> str:
    """Copy a consistent prod SQLite snapshot from zerg to a local temp path."""
    print("📥 Copying prod DB snapshot from zerg server...")
    local_path = f"{tmp_dir}/agents_prod.db"

    # Use SQLite backup API inside the container so we don't copy a live
    # WAL-backed file byte-for-byte mid-write.
    subprocess.run(
        [
            "ssh",
            "zerg",
            (
                f"docker exec {PROD_CONTAINER} python3 -c "
                "\"import sqlite3;"
                "src=sqlite3.connect('/data/longhouse.db');"
                "dst=sqlite3.connect('/tmp/longhouse.snapshot.db');"
                "src.backup(dst);"
                "dst.close();"
                "src.close()\""
            ),
        ],
        check=True,
    )
    subprocess.run(
        ["ssh", "zerg", f"docker cp {PROD_CONTAINER}:/tmp/longhouse.snapshot.db /tmp/longhouse.snapshot.db"],
        check=True,
    )
    subprocess.run(["scp", "zerg:/tmp/longhouse.snapshot.db", local_path], check=True)
    subprocess.run(["ssh", "zerg", "rm -f /tmp/longhouse.snapshot.db"], check=True)
    subprocess.run(["ssh", "zerg", f"docker exec {PROD_CONTAINER} rm -f /tmp/longhouse.snapshot.db"], check=True)

    print(f"   Copied to {local_path}")
    return local_path


def push_prod_db(local_path: str) -> None:
    """Push the modified DB back to prod using SQLite backup API (atomic-ish)."""
    print("📤 Pushing modified DB back to zerg server...")
    subprocess.run(
        ["scp", local_path, "zerg:/tmp/longhouse_fixed.db"],
        check=True,
    )
    subprocess.run(
        ["ssh", "zerg", f"docker cp /tmp/longhouse_fixed.db {PROD_CONTAINER}:/tmp/longhouse_fixed.db"],
        check=True,
    )
    # Replace live DB contents via SQLite backup API instead of raw file copy.
    subprocess.run(
        [
            "ssh",
            "zerg",
            (
                f"docker exec {PROD_CONTAINER} python3 -c "
                "\"import sqlite3;"
                "src=sqlite3.connect('/tmp/longhouse_fixed.db');"
                "dst=sqlite3.connect('/data/longhouse.db');"
                "src.backup(dst);"
                "dst.close();"
                "src.close()\""
            ),
        ],
        check=True,
    )
    subprocess.run(["ssh", "zerg", "rm /tmp/longhouse_fixed.db"], check=True)
    subprocess.run(["ssh", "zerg", f"docker exec {PROD_CONTAINER} rm -f /tmp/longhouse_fixed.db"], check=True)
    print("   Done.")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def find_and_merge_orphans(engine: sa.Engine, dry_run: bool) -> int:
    """Find orphan Codex sessions and merge them into their canonical counterparts.

    Matching strategy (in order of preference):
    1. source_path: events from the same file share source_path — exact match
    2. Time window fallback: ±5 min started_at, same provider, non-null project

    Returns the number of pairs processed.
    """
    with engine.begin() as conn:
        # Find all codex sessions with null project (orphan candidates)
        orphan_rows = conn.execute(sa.text("""
            SELECT id, started_at, ended_at, user_messages, assistant_messages
            FROM sessions
            WHERE provider = 'codex' AND project IS NULL
            ORDER BY started_at
        """)).fetchall()

        if not orphan_rows:
            print("✅ No orphan Codex sessions found.")
            return 0

        print(f"Found {len(orphan_rows)} orphan Codex sessions (null project)")

        pairs: list[tuple[str, str]] = []  # (canonical_id, orphan_id)
        unmatched: list[str] = []

        for orphan in orphan_rows:
            orphan_id = str(orphan.id)
            started = orphan.started_at

            # Strategy 1: match via shared source_path on events (most reliable)
            match = conn.execute(sa.text("""
                SELECT s.id, s.project
                FROM sessions s
                JOIN events e ON e.session_id = s.id
                WHERE s.provider = 'codex'
                  AND s.project IS NOT NULL
                  AND s.id != :orphan_id
                  AND e.source_path IN (
                    SELECT DISTINCT source_path FROM events
                    WHERE session_id = :orphan_id AND source_path IS NOT NULL
                  )
                GROUP BY s.id, s.project
                LIMIT 1
            """), {"orphan_id": orphan_id}).fetchone()

            if match:
                canonical_id = str(match.id)
                print(f"  📎 [source_path] canonical={canonical_id} (project={match.project}) ← orphan={orphan_id}")
                pairs.append((canonical_id, orphan_id))
                continue

            # Strategy 2: time-window fallback (within ±5 min)
            if started is None:
                unmatched.append(orphan_id)
                continue

            time_match = conn.execute(sa.text("""
                SELECT id, project
                FROM sessions
                WHERE provider = 'codex'
                  AND project IS NOT NULL
                  AND id != :orphan_id
                  AND ABS(JULIANDAY(started_at) - JULIANDAY(:started)) < :window_days
                ORDER BY ABS(JULIANDAY(started_at) - JULIANDAY(:started))
                LIMIT 1
            """), {
                "orphan_id": orphan_id,
                "started": started,
                "window_days": 5 / (24 * 60),
            }).fetchone()

            if time_match:
                canonical_id = str(time_match.id)
                print(f"  📎 [time-window] canonical={canonical_id} (project={time_match.project}) ← orphan={orphan_id}")
                pairs.append((canonical_id, orphan_id))
            else:
                print(f"  ⚠️  No match for orphan {orphan_id} (started={started})")
                unmatched.append(orphan_id)

        if unmatched:
            print(f"\n⚠️  {len(unmatched)} orphans with no match (will not be touched):")
            for uid in unmatched[:10]:
                print(f"    {uid}")

        if not pairs:
            print("No pairs to merge.")
            return 0

        print(f"\n{'DRY RUN — ' if dry_run else ''}Merging {len(pairs)} pairs...")

        merged = 0
        for canonical_id, orphan_id in pairs:
            # Count orphan's events
            event_count = conn.execute(sa.text(
                "SELECT COUNT(*) FROM events WHERE session_id = :sid"
            ), {"sid": orphan_id}).scalar() or 0

            user_count = conn.execute(sa.text(
                "SELECT COUNT(*) FROM events WHERE session_id = :sid AND role = 'user'"
            ), {"sid": orphan_id}).scalar() or 0

            assistant_count = conn.execute(sa.text(
                "SELECT COUNT(*) FROM events WHERE session_id = :sid AND role = 'assistant'"
            ), {"sid": orphan_id}).scalar() or 0

            print(f"    Merging {event_count} events ({user_count}u/{assistant_count}a) "
                  f"from {orphan_id} → {canonical_id}")

            if dry_run:
                continue

            # Re-parent events safely. Canonical sessions may already contain
            # some rows (same source_path/source_offset/event_hash), so direct
            # UPDATE can violate ix_events_dedup. Copy with INSERT OR IGNORE,
            # then delete orphan rows.
            conn.execute(sa.text("""
                INSERT OR IGNORE INTO events (
                    session_id,
                    role,
                    content_text,
                    tool_name,
                    tool_input_json,
                    tool_output_text,
                    tool_call_id,
                    timestamp,
                    source_path,
                    source_offset,
                    event_hash,
                    schema_version,
                    raw_json
                )
                SELECT
                    :canonical,
                    role,
                    content_text,
                    tool_name,
                    tool_input_json,
                    tool_output_text,
                    tool_call_id,
                    timestamp,
                    source_path,
                    source_offset,
                    event_hash,
                    schema_version,
                    raw_json
                FROM events
                WHERE session_id = :orphan
            """), {"canonical": canonical_id, "orphan": orphan_id})
            conn.execute(sa.text(
                "DELETE FROM events WHERE session_id = :orphan"
            ), {"orphan": orphan_id})

            # Re-parent embeddings safely. Canonical sessions may already have
            # rows for (kind, chunk_index, model), so direct UPDATE can hit the
            # uq_session_emb unique constraint. Copy with INSERT OR IGNORE, then
            # delete orphan rows.
            conn.execute(sa.text("""
                INSERT OR IGNORE INTO session_embeddings (
                    session_id,
                    kind,
                    chunk_index,
                    event_index_start,
                    event_index_end,
                    model,
                    dims,
                    embedding,
                    content_hash,
                    created_at
                )
                SELECT
                    :canonical,
                    kind,
                    chunk_index,
                    event_index_start,
                    event_index_end,
                    model,
                    dims,
                    embedding,
                    content_hash,
                    created_at
                FROM session_embeddings
                WHERE session_id = :orphan
            """), {"canonical": canonical_id, "orphan": orphan_id})
            conn.execute(sa.text(
                "DELETE FROM session_embeddings WHERE session_id = :orphan"
            ), {"orphan": orphan_id})

            # Recompute canonical counters from merged event rows so duplicates
            # ignored above don't inflate denormalized session stats.
            canonical_user_count = conn.execute(sa.text("""
                SELECT COUNT(*) FROM events
                WHERE session_id = :sid
                  AND role = 'user'
                  AND LOWER(TRIM(COALESCE(content_text, ''))) != 'warmup'
            """), {"sid": canonical_id}).scalar() or 0

            canonical_assistant_count = conn.execute(sa.text("""
                SELECT COUNT(*) FROM events
                WHERE session_id = :sid
                  AND role = 'assistant'
                  AND tool_name IS NULL
            """), {"sid": canonical_id}).scalar() or 0

            canonical_tool_calls = conn.execute(sa.text("""
                SELECT COUNT(*) FROM events
                WHERE session_id = :sid
                  AND role = 'assistant'
                  AND tool_name IS NOT NULL
            """), {"sid": canonical_id}).scalar() or 0

            canonical_ended_at = conn.execute(sa.text("""
                SELECT MAX(timestamp) FROM events WHERE session_id = :sid
            """), {"sid": canonical_id}).scalar()

            conn.execute(sa.text("""
                UPDATE sessions
                SET
                    user_messages = :u,
                    assistant_messages = :a,
                    tool_calls = :t,
                    ended_at = COALESCE(:ended_at, ended_at)
                WHERE id = :canonical
            """), {
                "u": canonical_user_count,
                "a": canonical_assistant_count,
                "t": canonical_tool_calls,
                "ended_at": canonical_ended_at,
                "canonical": canonical_id,
            })

            # Delete orphan (events already re-parented)
            conn.execute(sa.text(
                "DELETE FROM sessions WHERE id = :orphan"
            ), {"orphan": orphan_id})

            merged += 1

        print(f"\n{'Would merge' if dry_run else 'Merged'} {len(pairs)} pairs "
              f"({'dry run' if dry_run else f'{merged} committed'})")
        return len(pairs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=None,
        help="Path to SQLite DB. If omitted, copies from prod server."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually commit changes (default is dry run)."
    )
    args = parser.parse_args()

    dry_run = not args.execute

    if dry_run:
        print("🔍 DRY RUN MODE — no changes will be committed (pass --execute to apply)\n")

    if args.db:
        db_path = args.db
        push_after = False
    else:
        tmp = tempfile.mkdtemp()
        db_path = get_prod_db(tmp)
        push_after = args.execute

    engine = sa.create_engine(f"sqlite:///{db_path}")

    count = find_and_merge_orphans(engine, dry_run=dry_run)

    if count and push_after:
        push_prod_db(db_path)
        print("\n✅ Done. Prod DB updated.")
    elif count and dry_run:
        print("\n⚠️  Dry run complete. Run with --execute to apply changes.")
    else:
        print("\n✅ Done.")


if __name__ == "__main__":
    main()
