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
from zerg.services.archive_backlog import ready_archive_backlog
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


def _format_rate(value: Any, suffix: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(number) < 0.0001:
        number = 0.0
    return f"{number:.1f}{suffix}"


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

    shipper = dict(summary.get("shipper") or {})
    scheduler = dict(shipper.get("ship_scheduler") or {})
    limiter = dict(shipper.get("adaptive_backlog_limiter") or {})
    lanes = dict(shipper.get("ship_lanes") or {})
    archive_lane = dict(lanes.get("archive") or {})

    if scheduler or limiter or archive_lane:
        typer.echo("")
        typer.echo("Shipper controller:")
    if scheduler:
        ready_archive = int(scheduler.get("ready_retry") or 0) + int(scheduler.get("ready_scan") or 0)
        active_archive = int(scheduler.get("in_flight_retry") or 0) + int(scheduler.get("in_flight_scan") or 0)
        typer.echo(
            "  scheduler: "
            f"ready live {scheduler.get('ready_live', 0)}, "
            f"ready archive {ready_archive}, "
            f"active archive {active_archive}, "
            f"archive cap {scheduler.get('backlog_cap', '-')}"
        )
    if limiter:
        typer.echo(
            "  limiter: "
            f"cap {limiter.get('current_cap', '-')}/{limiter.get('ceiling', '-')}, "
            f"pressure {limiter.get('pressure_state', '-')}, "
            f"batch {_format_bytes(limiter.get('archive_target_batch_bytes'))}"
        )
        typer.echo(
            "  host: "
            f"queue ewma {_format_rate(limiter.get('ewma_queue_wait_ms'), 'ms')}, "
            f"exec ewma {_format_rate(limiter.get('ewma_exec_ms'), 'ms')}, "
            f"backpressure {limiter.get('total_backpressure', 0)}"
        )
    if archive_lane:
        typer.echo(
            "  archive 1h: "
            f"{archive_lane.get('successes_1h', 0)}/{archive_lane.get('attempts_1h', 0)} ok, "
            f"{archive_lane.get('backpressure_1h', 0)} backpressure, "
            f"{_format_bytes(archive_lane.get('bytes_1h'))}, "
            f"{archive_lane.get('events_1h', 0)} events"
        )


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
    mode: str = typer.Option("drain", "--mode", help="Resume mode: trickle or drain."),
    budget: str | None = typer.Option(None, "--budget", help="Per-tick byte budget, e.g. 512MB or 4GB."),
    include_huge: bool = typer.Option(True, "--include-huge/--exclude-huge", help="Allow ranges >=100MB."),
    retry_now: bool = typer.Option(False, "--retry-now", help="Make pending archive ranges eligible immediately."),
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
    if retry_now:
        changed = ready_archive_backlog(state_root)
        typer.echo(f"Archive retry clocks reset for {changed} pending range(s).")


@app.command("drain")
def drain_command(
    budget: str = typer.Option("4GB", "--budget", help="Per-tick byte budget, e.g. 4GB."),
    include_huge: bool = typer.Option(True, "--include-huge/--exclude-huge", help="Allow ranges >=100MB."),
    retry_now: bool = typer.Option(False, "--retry-now", help="Make pending archive ranges eligible immediately."),
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
    if retry_now:
        changed = ready_archive_backlog(state_root)
        typer.echo(f"Archive retry clocks reset for {changed} pending range(s).")


@app.command("dead-letter")
def dead_letter_command(
    file_path: str = typer.Option(..., "--path", help="Exact source path to dead-letter."),
    reason: str = typer.Option(..., "--reason", help="Operator reason recorded on pending ranges."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Move pending ranges for one source path to dead-letter state."""

    changed = dead_letter_archive_path(state_root, file_path=file_path, reason=reason)
    typer.echo(f"Dead-lettered {changed} pending archive range(s).")
