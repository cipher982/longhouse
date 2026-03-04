"""Main CLI entry point for Longhouse.

Usage:
    longhouse serve         # Start the server
    longhouse status        # Show configuration
    longhouse config show   # Show effective configuration
    longhouse ship          # One-shot sync
    longhouse connect       # Foreground engine sync (watch + fallback scan)
    longhouse recall        # Search past sessions
    longhouse migrate       # Plan/apply heavy legacy DB migrations
    longhouse onboard       # Run onboarding wizard
    longhouse doctor        # Self-diagnosis
    longhouse --help        # Show help
"""

import json

import typer

from zerg.cli.connect import app as connect_app
from zerg.cli.doctor import doctor
from zerg.cli.mcp_serve import mcp_server
from zerg.cli.onboard import onboard
from zerg.cli.serve import app as serve_app
from zerg.cli.serve import serve
from zerg.cli.serve import status

# Create main app
app = typer.Typer(
    name="longhouse",
    help="Longhouse AI Agent Platform CLI",
    no_args_is_help=True,
)

# Config subcommand group
config_app = typer.Typer(help="Configuration management")


@config_app.command(name="show")
def config_show() -> None:
    """Show effective configuration with sources.

    Displays the merged configuration from file, environment variables,
    and defaults, indicating where each value comes from.
    """
    from zerg.cli.config_file import get_config_path
    from zerg.cli.config_file import get_effective_config_display
    from zerg.cli.config_file import load_config

    config = load_config()
    entries = get_effective_config_display(config)

    config_path = get_config_path()
    typer.echo(f"Config file: {config_path}")
    typer.echo(f"  {'exists' if config_path.exists() else 'not found'}")
    typer.echo("")
    typer.echo("Effective configuration:")
    typer.echo("-" * 50)

    for key, value, source in entries:
        source_indicator = {
            "file": typer.style("[file]", fg=typer.colors.CYAN),
            "env": typer.style("[env]", fg=typer.colors.YELLOW),
            "default": typer.style("[default]", fg=typer.colors.WHITE, dim=True),
        }.get(source, f"[{source}]")
        typer.echo(f"  {key}: {value} {source_indicator}")


# Add subcommands from connect module
app.add_typer(connect_app, name="session", help="Session shipping commands")
app.add_typer(serve_app, name="server", help="Server management commands")
app.add_typer(config_app, name="config", help="Configuration management")

# Top-level commands for quick access
# Server commands (primary use case)
app.command(name="serve")(serve)
app.command(name="status")(status)

# Session shipping commands (convenience aliases)
# Find commands by callback function name to avoid index-position bugs
_cmd_lookup = {cmd.callback.__name__: cmd.callback for cmd in connect_app.registered_commands}
app.command(name="auth")(_cmd_lookup["auth"])
app.command(name="ship")(_cmd_lookup["ship"])
app.command(name="connect")(_cmd_lookup["connect"])
app.command(name="recall")(_cmd_lookup["recall"])


@app.command(name="migrate")
def migrate(
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override (defaults to env)."),
    apply: bool = typer.Option(False, "--apply", help="Apply pending heavy migrations."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Plan or apply explicit heavy SQLite migrations."""
    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.db_migrations import apply_heavy_migrations
    from zerg.db_migrations import plan_heavy_migrations

    engine = make_engine(database_url) if database_url else None
    initialize_database(engine)
    target_engine = engine

    if target_engine is None:
        from zerg.database import default_engine

        target_engine = default_engine
    if target_engine is None:
        raise typer.Exit(code=2)

    plan_before = plan_heavy_migrations(target_engine)
    pending_before = [item.name for item in plan_before if item.pending]

    run_items = []
    if apply:
        run_items = apply_heavy_migrations(target_engine)

    plan_after = plan_heavy_migrations(target_engine)
    pending_after = [item.name for item in plan_after if item.pending]

    payload = {
        "pending_before": pending_before,
        "pending_after": pending_after,
        "plan": [
            {
                "name": item.name,
                "description": item.description,
                "pending": item.pending,
                "reason": item.reason,
                "last_status": item.last_status,
            }
            for item in plan_after
        ],
        "applied": [{"name": item.name, "status": item.status, "details": item.details} for item in run_items],
    }

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        if pending_before:
            typer.echo(f"Pending heavy migrations: {', '.join(pending_before)}")
        else:
            typer.echo("No heavy migrations pending.")
        if run_items:
            for item in run_items:
                details = f" ({item.details})" if item.details else ""
                typer.echo(f"- {item.name}: {item.status}{details}")
        if apply and pending_after:
            typer.echo(f"Still pending: {', '.join(pending_after)}")

    if apply and pending_after:
        raise typer.Exit(code=1)


# Onboarding wizard
app.command(name="onboard")(onboard)

# Self-diagnosis
app.command(name="doctor")(doctor)

# MCP server
app.command(name="mcp-server")(mcp_server)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
