"""Read-only durable shipping source evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import typer

from zerg.services.longhouse_paths import get_agent_db_path

app = typer.Typer(help="Inspect durable shipping source evidence")


def _source_rows(state_root: Path | None, source_epoch: str | None, limit: int) -> tuple[Path, list[dict[str, Any]]]:
    database = get_agent_db_path(state_root)
    if not database.is_file():
        return database, []
    query = """
        SELECT pending.source_epoch,
               pending.source_path,
               pending.range_start,
               pending.range_end,
               pending.envelope_id,
               pending.raw_bytes,
               pending.event_count,
               pending.has_reply_evidence,
               pending.created_at,
               pending.attempt_count,
               pending.last_attempt_at,
               pending.blocked_at,
               pending.block_kind,
               pending.block_detail,
               epoch.provider,
               epoch.opaque_source_id
        FROM pending_source_envelope AS pending
        LEFT JOIN source_epoch_registry AS epoch
          ON epoch.source_epoch = pending.source_epoch
        WHERE (?1 IS NULL OR pending.source_epoch = ?1)
        ORDER BY pending.blocked_at IS NOT NULL DESC, pending.created_at, pending.source_epoch
        LIMIT ?2
    """
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, (source_epoch, limit)).fetchall()
    return database, [dict(row) for row in rows]


@app.command("inspect")
def inspect_command(
    source_epoch: str | None = typer.Option(None, "--source-epoch", help="Inspect one exact source epoch."),
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Maximum source intents to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable evidence."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Inspect retained durable source evidence without mutating it."""
    database, rows = _source_rows(state_root, source_epoch, limit)
    payload = {
        "schema_version": 1,
        "action_id": "inspect_storage_source",
        "read_only": True,
        "database": str(database),
        "source_epoch": source_epoch,
        "rows": rows,
        "note": (
            "Rows are retained source evidence. Safe metadata conflicts are reconciled by the "
            "Machine Agent; unresolved event-bearing rows must not be retried or discarded blindly."
        ),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo("Durable shipping source evidence (read-only)")
    typer.echo(f"  database: {database}")
    typer.echo(f"  rows: {len(rows)}")
    if not rows:
        typer.echo("  No retained source intents matched the requested scope.")
        return
    for row in rows:
        provider = row.get("provider") or "unknown"
        source = row.get("opaque_source_id") or row.get("source_path") or "unknown"
        block_kind = row.get("block_kind") or "pending"
        risk = (
            "unresolved evidence risk"
            if block_kind
            not in {
                None,
                "source_epoch_conflict",
                "render_generation_revision_conflict",
            }
            else "metadata/reconciliation work"
        )
        typer.echo(
            f"  {provider} {source} epoch={row['source_epoch']} " f"range={row['range_start']}..{row['range_end']} kind={block_kind}"
        )
        typer.echo(f"    risk: {risk}; attempts={row['attempt_count']}; detail={row.get('block_detail') or '-'}")
    typer.echo("  action: inspect this evidence before retrying or discarding it")


__all__ = ["app"]
