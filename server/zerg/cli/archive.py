"""Archive backlog inspection and control commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import dead_letter_archive_path
from zerg.services.archive_backlog import inspect_archive_backlog
from zerg.services.archive_backlog import parse_byte_budget
from zerg.services.archive_backlog import write_archive_control

app = typer.Typer(help="Inspect and control local archive backlog repair")


def _format_bytes(value: Any) -> str:
    size = int(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    scaled = float(size)
    for unit in units:
        if scaled < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size} B"
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{size} B"


@app.command("status")
def status_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Show local archive backlog repair state."""

    summary = collect_archive_backlog(state_root)
    if json_output:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"Archive repair: {summary['state']} ({summary['mode']})")
    typer.echo(f"  pending ranges:   {summary['pending_ranges']}")
    typer.echo(f"  pending paths:    {summary['pending_paths']}")
    typer.echo(f"  pending sessions: {summary['pending_sessions']}")
    typer.echo(f"  pending bytes:    {_format_bytes(summary['pending_bytes'])}")
    typer.echo(f"  huge ranges:      {summary['huge_pending_ranges']} ({_format_bytes(summary['huge_pending_bytes'])})")
    typer.echo(f"  dead letters:     {summary['dead_ranges']} ({_format_bytes(summary['dead_bytes'])})")
    if summary.get("oldest_pending_at"):
        typer.echo(f"  oldest pending:   {summary['oldest_pending_at']}")
    if summary.get("next_retry_at_min"):
        typer.echo(f"  next retry:       {summary['next_retry_at_min']}")


@app.command("inspect")
def inspect_command(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum pending paths to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """List the largest pending archive paths."""

    rows = inspect_archive_backlog(state_root, limit=limit)
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        typer.echo(f"{row['provider']} {_format_bytes(row['pending_bytes'])} " f"{row['pending_ranges']} range(s) {row['file_path']}")


@app.command("pause")
def pause_command(
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Pause local archive repair replay."""

    result = write_archive_control(state_root, mode="paused")
    typer.echo(f"Archive repair paused: {result['path']}")


@app.command("resume")
def resume_command(
    mode: str = typer.Option("trickle", "--mode", help="Resume mode: trickle or drain."),
    budget: str | None = typer.Option(None, "--budget", help="Per-tick byte budget, e.g. 25MB or 250MB."),
    include_huge: bool = typer.Option(False, "--include-huge", help="Allow ranges >=100MB."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Resume local archive repair replay."""

    result = write_archive_control(
        state_root,
        mode=mode,
        max_tick_bytes=parse_byte_budget(budget),
        include_huge=include_huge,
    )
    typer.echo(f"Archive repair resumed in {result['mode']} mode: {result['path']}")


@app.command("drain")
def drain_command(
    budget: str = typer.Option("250MB", "--budget", help="Per-tick byte budget, e.g. 250MB."),
    include_huge: bool = typer.Option(False, "--include-huge", help="Allow ranges >=100MB."),
    max_minutes: int | None = typer.Option(
        None,
        "--max-minutes",
        help="Accepted for operator intent; engine drains by tick budget.",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Switch archive repair to explicit drain mode."""

    _ = max_minutes
    result = write_archive_control(
        state_root,
        mode="drain",
        max_tick_bytes=parse_byte_budget(budget),
        include_huge=include_huge,
    )
    typer.echo(f"Archive repair drain enabled: {result['path']}")


@app.command("dead-letter")
def dead_letter_command(
    file_path: str = typer.Option(..., "--path", help="Exact source path to dead-letter."),
    reason: str = typer.Option(..., "--reason", help="Operator reason recorded on pending ranges."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Move pending ranges for one source path to dead-letter state."""

    changed = dead_letter_archive_path(state_root, file_path=file_path, reason=reason)
    typer.echo(f"Dead-lettered {changed} pending archive range(s).")
