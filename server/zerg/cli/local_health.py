"""CLI surface for local Longhouse engine health."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zerg.services.local_health import collect_local_health


def _format_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "-"
    if age_seconds < 60:
        return f"{age_seconds}s"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m"
    return f"{age_seconds // 3600}h"


def local_health(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory override (default: ~/.claude or CLAUDE_CONFIG_DIR).",
    ),
) -> None:
    """Show local Longhouse shipping health for this machine."""
    snapshot = collect_local_health(Path(claude_dir) if claude_dir else None)

    if json_output:
        typer.echo(json.dumps(snapshot, indent=2))
        return

    severity = snapshot["severity"]
    color = {
        "green": typer.colors.GREEN,
        "yellow": typer.colors.YELLOW,
        "red": typer.colors.RED,
        "gray": typer.colors.WHITE,
    }.get(severity, typer.colors.WHITE)

    typer.secho(
        f"{snapshot['headline']} ({snapshot['health_state']}, {severity})",
        fg=color,
        bold=True,
    )

    service = snapshot["service"]
    engine_status = snapshot["engine_status"]
    payload = engine_status.get("payload") or {}
    outbox = snapshot["outbox"]

    typer.echo("")
    typer.echo("Service")
    typer.echo(f"  status: {service.get('status', '-')}")
    typer.echo(f"  platform: {service.get('platform', '-')}")
    if service.get("service_name"):
        typer.echo(f"  name: {service['service_name']}")
    if service.get("service_file"):
        typer.echo(f"  file: {service['service_file']}")
    if service.get("log_path"):
        typer.echo(f"  logs: {service['log_path']}")

    typer.echo("")
    typer.echo("Engine")
    typer.echo(f"  status file: {engine_status.get('path', '-')}")
    typer.echo(f"  exists: {'yes' if engine_status.get('exists') else 'no'}")
    typer.echo(f"  age: {_format_age(engine_status.get('age_seconds'))}")
    typer.echo(f"  last ship: {payload.get('last_ship_at') or '-'}")
    typer.echo(f"  spool pending: {payload.get('spool_pending_count', 0)}")
    typer.echo(f"  spool dead: {payload.get('spool_dead_count', 0)}")
    typer.echo(f"  ship failures: {payload.get('consecutive_ship_failures', 0)}")
    typer.echo(f"  offline: {'yes' if payload.get('is_offline') else 'no'}")

    typer.echo("")
    typer.echo("Outbox")
    typer.echo(f"  path: {outbox.get('path', '-')}")
    typer.echo(f"  files: {outbox.get('file_count', 0)}")
    typer.echo(f"  oldest: {_format_age(outbox.get('oldest_age_seconds'))}")

    reasons = snapshot.get("reasons") or []
    if reasons:
        typer.echo("")
        typer.echo("Reasons")
        for reason in reasons:
            typer.echo(f"  - {reason}")

    actions = snapshot.get("suggested_actions") or []
    if actions:
        typer.echo("")
        typer.echo("Next")
        for action in actions:
            typer.echo(f"  - {action}")
