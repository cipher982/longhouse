#!/usr/bin/env python3
"""Repair OpenCode sessions that were labeled as generic ``workspace``.

OpenCode can run inside temporary directories whose basename is literally
``workspace``. Longhouse should not group those as a real user project. This
script uses the stored cwd path hierarchy to choose a better project label and
optionally reclassifies known provider-live/probe workspaces as test data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any


KNOWN_TEST_MARKERS = (
    "/.longhouse/canaries/provider-live/opencode/",
    "/.build/canaries/provider-live/opencode/",
    "longhouse-provider-live-proof",
    "longhouse-opencode-no-token",
    "longhouse-opencode-",
    "lh-opencode-",
)

KNOWN_REPO_ROOTS = (
    (("/", "Users", "davidrose", "git", "zerg", "longhouse"), "longhouse"),
    (("/", "Users", "davidrose", "git", "zerg", "control-plane"), "control-plane"),
    (("/", "Users", "davidrose", "git", "sauron", "jobs"), "jobs"),
)

def _parts(cwd: str) -> tuple[str, ...]:
    return PurePosixPath(cwd).parts


def _starts_with(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _after(parts: tuple[str, ...], *prefix: str) -> str | None:
    for index in range(0, len(parts) - len(prefix)):
        if parts[index : index + len(prefix)] == prefix:
            candidate_index = index + len(prefix)
            if candidate_index < len(parts):
                candidate = parts[candidate_index].strip()
                if candidate:
                    return candidate
    return None


def project_from_cwd(cwd: str | None) -> str | None:
    cwd = (cwd or "").strip()
    if not cwd:
        return None

    parts = _parts(cwd)

    if "/.longhouse/canaries/provider-live/opencode/" in cwd:
        return "longhouse-opencode-provider-live"

    for prefix, project in KNOWN_REPO_ROOTS:
        if _starts_with(parts, prefix):
            return project

    worktree = _after(parts, "/", "Users", "davidrose", "git", "_wt")
    if worktree:
        return worktree

    repo = _after(parts, "/", "Users", "davidrose", "git")
    if repo:
        return repo

    return None


def environment_from_cwd(cwd: str | None, current: str | None) -> str | None:
    cwd = (cwd or "").strip()
    if not cwd:
        return current
    if any(marker in cwd for marker in KNOWN_TEST_MARKERS):
        return "test"
    return current


def candidate_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, project, environment, cwd
        FROM sessions
        WHERE lower(provider) = 'opencode'
          AND (
            project = 'workspace'
            OR project IN ('davidrose', 'git', 'tmp')
            OR (project = 'sauron' AND cwd LIKE '%/git/sauron/jobs/%')
          )
          AND cwd IS NOT NULL
        ORDER BY id
        """
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        new_project = project_from_cwd(row["cwd"])
        new_environment = environment_from_cwd(row["cwd"], row["environment"])
        if new_project == row["project"] and new_environment == row["environment"]:
            continue
        candidates.append(
            {
                "id": row["id"],
                "old_project": row["project"],
                "new_project": new_project,
                "old_environment": row["environment"],
                "new_environment": new_environment,
                "cwd": row["cwd"],
            }
        )
    return candidates


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def apply_repair(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    sessions_updated = 0
    cards_updated = 0
    has_timeline_cards = table_exists(conn, "timeline_cards")

    with conn:
        for row in rows:
            result = conn.execute(
                """
                UPDATE sessions
                SET project = ?, environment = ?
                WHERE id = ?
                  AND lower(provider) = 'opencode'
                  AND project = ?
                  AND environment = ?
                """,
                (
                    row["new_project"],
                    row["new_environment"],
                    row["id"],
                    row["old_project"],
                    row["old_environment"],
                ),
            )
            sessions_updated += result.rowcount
            if has_timeline_cards:
                result = conn.execute(
                    """
                    UPDATE timeline_cards
                    SET project = ?, environment = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                      AND lower(provider) = 'opencode'
                      AND project = ?
                    """,
                    (
                        row["new_project"],
                        row["new_environment"],
                        row["id"],
                        row["old_project"],
                    ),
                )
                cards_updated += result.rowcount
    return {"sessions_updated": sessions_updated, "timeline_cards_updated": cards_updated}


def write_rollback(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_change: dict[str, int] = {}
    for row in rows:
        old_project = row["old_project"] if row["old_project"] is not None else "NULL"
        new_project = row["new_project"] if row["new_project"] is not None else "NULL"
        key = f"{old_project}/{row['old_environment']} -> {new_project}/{row['new_environment']}"
        by_change[key] = by_change.get(key, 0) + 1
    return {"candidate_count": len(rows), "by_change": by_change, "sample": rows[:10]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite DB path")
    parser.add_argument("--apply", action="store_true", help="Apply updates")
    parser.add_argument("--rollback-json", help="Path to write rollback row data before applying")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    rows = candidate_rows(conn)
    output: dict[str, Any] = summarize(rows)
    output["applied"] = False

    if args.apply:
        if args.rollback_json:
            write_rollback(Path(args.rollback_json), rows)
            output["rollback_json"] = args.rollback_json
        output.update(apply_repair(conn, rows))
        output["applied"] = True

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
